# -*- coding: utf-8 -*-
"""
Buddy 令牌式签到执行器（mode='buddy'）

适用：WorkBuddy / Buddy加油站 这类用 Bearer Token 鉴权的后端签到接口。
与现有 browser/api/browser_api 三种模式完全隔离，不改动它们的任何逻辑。

整合要点（来自 D:\\.Buddy\\Buddy-signin）：
- 鉴权：Authorization: Bearer <accessToken> + X-User-Id(=JWT sub) + X-Domain
- 续期：POST /v2/plugin/auth/token/refresh（网关代持 client_secret，只需 refresh_token）
        成功会轮换 access_token 与 refresh_token，必须回写才能实现「一次读取永久登录」
- 签到：POST /v2/billing/meter/daily-checkin
- 状态：GET  /v2/billing/meter/checkin-activity-status
- 成功判定：code==0 签到成功；code==10001 或 msg 含「已签到」视为今日已签

令牌来源（按优先级）：
1. 站点持久化的 token_store（NAS/Docker 端由用户在「令牌」框粘贴一次）
2. 本机（Windows）WorkBuddy 桌面登录态文件（auth_file，留空用默认路径）
续期成功后把新令牌回写 token_store（加密），下次运行直接复用。
"""
import json
import os
import time
import datetime
import base64
import logging

import requests

from utils import encrypt_data
from .base import BaseExecutor, SignResult, register_executor

logger = logging.getLogger(__name__)

ENDPOINT_CHECKIN = "/v2/billing/meter/daily-checkin"
ENDPOINT_STATUS = "/v2/billing/meter/checkin-activity-status"
ENDPOINT_REFRESH = "/v2/plugin/auth/token/refresh"  # 插件网关续期（网关代持 client_secret，无需密钥）

DEFAULT_API_BASES = ["https://copilot.tencent.com", "https://www.codebuddy.cn"]
DEFAULT_AUTH_REL_PATH = os.path.join(
    "AppData", "Local", "CodeBuddyExtension",
    "Data", "Public", "auth", "workbuddy-desktop.info",
)
DEFAULT_DOMAIN = "www.workbuddy.cn"


# ---------- JWT 解析（不校验签名，仅取声明） ----------
def _b64url_decode(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _jwt_payload(token):
    try:
        seg = token.split(".")[1]
        return json.loads(_b64url_decode(seg))
    except Exception:
        return {}


def _jwt_exp(token):
    return _jwt_payload(token).get("exp")


def _jwt_sub(token):
    return _jwt_payload(token).get("sub") or ""


def _pick(d, *keys):
    """按顺序返回第一个非 None 的字段值，兼容不同命名风格。"""
    for k in keys:
        if d.get(k) is not None:
            return d.get(k)
    return None


@register_executor
class BuddyExecutor(BaseExecutor):
    mode = 'buddy'
    label = 'Buddy专用(令牌)'
    description = 'Buddy加油站/WorkBuddy 专用令牌式签到：三个接口内置、无需配置，粘贴token_store或读取本机登录态，自动续期保持永久登录，按本项目时间表执行'

    def __init__(self, ctx):
        BaseExecutor.__init__(self, ctx)
        cfg = ctx.api_config or {}
        self.api_bases = cfg.get('api_bases') or list(DEFAULT_API_BASES)
        self.timeout = int(cfg.get('timeout') or 20)
        self.domain_cfg = (cfg.get('domain') or '').strip()
        self.auth_file_cfg = (cfg.get('auth_file') or '').strip()

    def run(self):
        ctx = self.ctx
        token, exp_ms, nickname, uid, domain, store = self._load_token()
        if not token:
            ctx.log('未配置令牌：请在站点「令牌」框粘贴 token_store，或在本机(Windows)放好 WorkBuddy 登录态文件', 'warning')
            return SignResult(False, '签到失败', None)

        # 先续期再签到（token 模式每日一次；续期成功则回写新令牌）
        token, uid, domain, store, refresh_note = self._maybe_refresh(token, uid, domain, store)
        if refresh_note:
            ctx.log('Buddy 续期: ' + refresh_note)
        # 续期成功后 store 中的有效期已更新，取最新值用于过期校验与「已续期至」展示
        exp_ms = store.get("expires_at") or exp_ms

        # token 过期检查
        now_ms = int(time.time() * 1000)
        if exp_ms and exp_ms < now_ms:
            ctx.log('accessToken 已过期，需打开 WorkBuddy 客户端刷新登录态或重新粘贴最新 token_store', 'warning')
            return SignResult(False, '签到失败', None)

        # 执行签到（服务端幂等，当天重复调用返回 code=10001，无需先查状态）
        status, lines, checkin_data = self._do_claim(token, uid, domain)

        if status == 'fail':
            ctx.log('Buddy 签到失败: ' + (lines[0] if lines else '未知错误'), 'error')
            return SignResult(False, '签到失败', None)

        # 签到成功：查询状态汇总，构造推送末尾的 Buddy 信息块（细节仅在此展示，不进主结果行）
        status_info = self._fetch_status(token, uid, domain)
        today_c = streak = total = None
        if status_info:
            today_c = _pick(status_info, "today_credit", "todayCredit")
            streak = _pick(status_info, "streak_days", "streakDays", "continuous_days", "continuousDays")
            total = _pick(status_info, "total_credits", "totalCredits")
            parts = []
            if today_c is not None:
                parts.append("今日积分=%s" % today_c)
            if streak is not None:
                parts.append("连续=%s天" % streak)
            if total is not None:
                parts.append("累计=%s" % total)
            if parts:
                ctx.log('Buddy 签到汇总: ' + ' '.join(parts))
        # 拼接待追加到推送末尾的 Buddy 状态块（与前面的站点结果空一行）
        exp_date = (time.strftime("%Y-%m-%d", time.localtime(exp_ms / 1000))
                    if exp_ms else "")
        buddy_lines = ["【Buddy加油站今日状态】"]
        if streak is not None:
            buddy_lines.append("  连续签到：%s 天" % streak)
        if today_c is not None:
            buddy_lines.append("  今日积分：%s" % today_c)
        if total is not None:
            buddy_lines.append("  累计积分：%s" % total)
        if exp_date:
            buddy_lines.append("  已续期至：%s" % exp_date)
        appendix = "\n".join(buddy_lines) if len(buddy_lines) > 1 else ""
        detail = json.dumps(checkin_data, ensure_ascii=False)[:500] if checkin_data else ""
        msg = '今日已签到' if status == 'already' else '签到成功'
        return SignResult(True, msg, None, detail, appendix)

    # ---------- 令牌加载 ----------
    def _load_token(self):
        """从 token_store 或本机桌面登录态文件取 (token, exp_ms, nickname, uid, domain, store)。"""
        store = dict(self.ctx.token_store or {})
        at = store.get("access_token")
        if at:
            uid = store.get("uid") or _jwt_sub(at)
            nickname = (store.get("username")
                        or _jwt_payload(at).get("nickname")
                        or _jwt_payload(at).get("name") or "")
            exp_ms = store.get("expires_at") or (_jwt_exp(at) or 0) * 1000
            domain = store.get("domain") or self.domain_cfg or DEFAULT_DOMAIN
            return at, exp_ms, nickname, uid, domain, store

        # 无 token_store：尝试读取本机（Windows）桌面登录态文件（一次读取登录态）
        path = self._resolve_auth_file()
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                auth = d.get("auth") or {}
                account = d.get("account") or {}
                at = auth.get("accessToken") or ""
                rt = auth.get("refreshToken") or ""
                domain = (auth.get("domain")
                          or (account.get("sso") or {}).get("domain")
                          or DEFAULT_DOMAIN)
                uid = account.get("uid") or ""
                username = account.get("nickname") or ""
                expires_at = auth.get("expiresAt") or 0
                store = {"access_token": at, "domain": domain, "uid": uid, "username": username}
                if rt:
                    store["refresh_token"] = rt
                if expires_at:
                    store["expires_at"] = expires_at
                else:
                    e = _jwt_exp(at)
                    if e:
                        store["expires_at"] = e * 1000
                store["exported_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self._persist_token_store(store)
                self.ctx.log("已从本机桌面登录态写入令牌存储")
                return at, store.get("expires_at", 0), username, uid, domain, store
            except Exception as e:
                self.ctx.log("读取桌面端登录态失败: %s" % e, "warning")
        return "", 0, "", "", DEFAULT_DOMAIN, {}

    def _resolve_auth_file(self):
        cands = []
        env = os.environ.get("BUDDY_AUTH_FILE")
        if env:
            cands.append(env)
        if self.auth_file_cfg:
            cands.append(self.auth_file_cfg)
        cands.append(os.path.expanduser(os.path.join("~", DEFAULT_AUTH_REL_PATH)))
        for c in cands:
            if c and os.path.exists(c):
                return c
        return cands[-1]

    # ---------- 续期 ----------
    def _maybe_refresh(self, token, uid, domain, store):
        """token 模式下每日首次调用前续期（先续期再签到）。
        成功则回存新 token/refresh_token 到 token_store 并返回新凭据；否则原样返回。"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if store.get("last_refresh_date") == today:
            return token, uid, domain, store, ""
        rt = store.get("refresh_token")
        if not rt:
            return token, uid, domain, store, "⚠️ 无 refresh_token，跳过续期（沿用旧 token）"

        data = self._do_refresh(token, uid, domain, rt)
        if not data:
            return token, uid, domain, store, "⚠️ 自动续期失败（沿用旧 token）"

        now_ms = int(time.time() * 1000)
        store["access_token"] = data["accessToken"]
        if data.get("refreshToken"):           # refresh token 会轮换，必须回存否则链条断裂
            store["refresh_token"] = data["refreshToken"]
        if data.get("expiresIn"):
            store["expires_at"] = now_ms + int(data["expiresIn"]) * 1000
        if data.get("refreshExpiresIn"):
            store["refresh_expires_at"] = now_ms + int(data["refreshExpiresIn"]) * 1000
        if data.get("sessionState"):
            store["session_state"] = data["sessionState"]
        if data.get("domain"):
            store["domain"] = data["domain"]
        store["last_refresh_date"] = today
        store["last_refresh_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._persist_token_store(store)
        exp_str = (time.strftime("%Y-%m-%d", time.localtime(store.get("expires_at", 0) / 1000))
                   if store.get("expires_at") else "未知")
        self.ctx.log("REFRESH OK 新 token 有效期至 %s" % exp_str)
        new_token = store["access_token"]
        return new_token, store.get("uid") or _jwt_sub(new_token), store.get("domain") or domain, store, \
            "🔑 已自动续期（有效期至 %s）" % exp_str

    def _do_refresh(self, token, uid, domain, rt):
        """调用插件网关续期。成功返回响应 data 字典，失败返回 None。"""
        extra = {
            "X-Refresh-Token": rt,
            "X-Auth-Refresh-Source": "plugin",
            "X-Product": "SaaS",
            "X-Requested-With": "XMLHttpRequest",
        }
        st, body = self._call(ENDPOINT_REFRESH, token, uid, domain, extra)
        if st is None:
            self.ctx.log("REFRESH FAIL 网络不可达", "warning")
            return None
        try:
            resp = json.loads(body)
        except Exception:
            return None
        if st == 200 and resp.get("code") == 0:
            data = resp.get("data") or {}
            if data.get("accessToken"):
                return data
        self.ctx.log("REFRESH FAIL code=%s msg=%s body=%s"
                     % (resp.get("code"), resp.get("msg"), (body or "")[:200]), "warning")
        return None

    # ---------- 签到 / 状态 ----------
    def _do_claim(self, token, uid, domain):
        """执行一次签到调用。返回 (status, lines, checkin_data)；status ∈ {'success','already','fail'}"""
        st, body = self._call(ENDPOINT_CHECKIN, token, uid, domain)
        if st is None:
            return 'fail', ["网络不可达：所有 API 域名均失败"], {}
        try:
            resp = json.loads(body)
        except Exception:
            return 'fail', ["响应非 JSON：%s" % (body or "")[:200]], {}
        code = resp.get("code")
        msg = resp.get("msg", "")
        data = resp.get("data") or {}
        if code == 0:
            credit = _pick(data, "credit", "today_credit", "daily_credit")
            lines = []
            if credit is not None:
                lines.append("本次获得积分：%s" % credit)
            return 'success', lines, data
        if code == 10001 or "已签到" in msg:
            return 'already', [], data
        return 'fail', ["签到异常 code=%s msg=%s" % (code, msg)], data

    def _fetch_status(self, token, uid, domain):
        """查询签到状态；成功返回 data 字典，失败返回 {}。该接口须用 POST（与原 buddy_checkin.py 一致）。"""
        st, body = self._call(ENDPOINT_STATUS, token, uid, domain)
        if st == 200:
            try:
                return json.loads(body).get("data", {}) or {}
            except Exception:
                return {}
        return {}

    # ---------- HTTP 工具 ----------
    def _headers(self, token, uid, domain, extra=None):
        h = {
            "Authorization": "Bearer " + (token or ""),
            "X-User-Id": uid or "",
            "X-Domain": domain or DEFAULT_DOMAIN,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "WorkBuddy/5.2.3",
        }
        if extra:
            h.update(extra)
        return h

    def _call(self, path, token, uid, domain, extra=None, body=None, method="POST"):
        """按 api_bases 顺序尝试；仅网络异常（status=None）才 fallback 到下一域名。"""
        last_err = None
        for base in self.api_bases:
            url = base.rstrip("/") + path
            try:
                hd = self._headers(token, uid, domain, extra)
                if body is not None:
                    payload = body if isinstance(body, (str, bytes)) else json.dumps(body).encode("utf-8")
                    r = requests.request(method, url, headers=hd, data=payload, timeout=self.timeout)
                else:
                    r = requests.request(method, url, headers=hd, timeout=self.timeout)
                return r.status_code, r.text
            except Exception as e:
                last_err = str(e)
                self.ctx.log("WARN %s 不可达: %s" % (base, e), "warning")
        return None, last_err

    # ---------- 持久化 ----------
    def _persist_token_store(self, store):
        """把令牌存储（dict）加密回写到站点 token_store 列。"""
        try:
            from models import update_site_token_store
            update_site_token_store(self.ctx.site_id,
                                    encrypt_data(json.dumps(store, ensure_ascii=False)))
        except Exception as e:
            logger.warning("持久化令牌失败: %s" % e)
