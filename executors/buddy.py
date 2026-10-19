# -*- coding: utf-8 -*-
"""Buddy 令牌式签到：签到 + 每日对话 + 连登奖励，三者独立门控；令牌来自站点 token_store。"""
import json
import re
import time
import uuid
import datetime
import base64
import logging

import requests
from urllib.parse import urlparse

from utils import encrypt_data
from .base import BaseExecutor, SignResult, register_executor

logger = logging.getLogger(__name__)

ENDPOINT_CHECKIN = "/v2/billing/meter/daily-checkin"
ENDPOINT_STATUS = "/v2/billing/meter/checkin-activity-status"
ENDPOINT_REFRESH = "/v2/plugin/auth/token/refresh"  # 网关代持 client_secret
# ---- 对话 ----
ENDPOINT_CONV = "/v2/as/conversations/"
ENDPOINT_SESSION = "/console/as/conversations/%s/session"
ENDPOINT_REPORT = "/v2/report"
ENDPOINT_CHAT = "/v2/chat/completions"

MODEL_NAMES = {"hy3": "Hy3", "fast-model": "Fast"}
DEFAULT_CHAT = {
    "enabled": True,
    "model": "hy3",
    "prompt": "你好，请只回复一个字：好",
    "timeout": 90,
}

DEFAULT_API_BASES = ["https://copilot.tencent.com", "https://www.codebuddy.cn"]
DEFAULT_DOMAIN = "www.workbuddy.cn"

# ---- 连登奖励 ----
ENDPOINT_GROWTH = "/v2/activity/growth"
# (tier 标识, 状态字段前缀, 展示名, 解锁天数)；tier 必须传档位标识，传天数会被判 unknown
GROWTH_TIERS = (
    ("7d", "starter", "入门", 7),
    ("14d", "advanced", "进阶", 14),
    ("28d", "legendary", "巅峰", 28),
)
DEFAULT_GROWTH = {"enabled": True}

# 签到自身重试 3 次、间隔 10 秒；只包裹签到，对话与领奖不参与
CHECKIN_RETRY_TIMES = 3
CHECKIN_RETRY_INTERVAL = 10


# ---- JWT 解析（不校验签名） ----
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


def _dig(obj, key):
    """在可能被 data/result 包裹的响应里取字段。"""
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if isinstance(obj.get(k), dict):
                v = _dig(obj[k], key)
                if v is not None:
                    return v
    return None


@register_executor
class BuddyExecutor(BaseExecutor):
    mode = 'buddy'
    label = 'Buddy专用(令牌)'
    description = 'Buddy加油站/WorkBuddy 专用令牌式签到：接口内置、无需配置，在站点「令牌」框粘贴 token_store，自动续期保持永久登录；签到失败自身重试3次(间隔10秒)，每日对话与连登奖励领取三者独立门控'

    def __init__(self, ctx):
        BaseExecutor.__init__(self, ctx)
        cfg = ctx.api_config or {}
        self.api_bases = cfg.get('api_bases') or list(DEFAULT_API_BASES)
        self.timeout = int(cfg.get('timeout') or 20)
        self.domain_cfg = (cfg.get('domain') or '').strip()
        self.chat_cfg = self._load_chat_cfg(cfg.get('chat'))
        self.growth_cfg = self._load_growth_cfg(cfg.get('growth'))

    def run(self):
        ctx = self.ctx
        token, exp_ms, nickname, uid, domain, store = self._load_token()
        if not token:
            ctx.log('未配置令牌：请在站点编辑页「令牌」框粘贴 token_store（含 access_token / refresh_token）', 'warning')
            return SignResult(False, '签到失败', None)

        # 先续期再签到，成功后回写新令牌
        token, uid, domain, store, refresh_note = self._maybe_refresh(token, uid, domain, store)
        if refresh_note:
            ctx.log('Buddy 续期: ' + refresh_note)
        exp_ms = store.get("expires_at") or exp_ms

        now_ms = int(time.time() * 1000)
        if exp_ms and exp_ms < now_ms:
            ctx.log('accessToken 已过期，请在站点编辑页「令牌」框重新粘贴最新 token_store', 'warning')
            return SignResult(False, '签到失败', None)

        status, lines, checkin_data = self._claim_with_retry(token, uid, domain)

        # 对话与签到独立：无论签到成败都执行，当日已成功发起则跳过
        chat_note = ''
        if self._chat_enabled():
            chat_note = self._run_chat_once(token, uid, nickname, domain, store)

        growth_lines = self._run_growth_once(token, uid, domain, store)

        streak = today_c = total = None
        if status != 'fail':
            status_info = self._fetch_status(token, uid, domain)
            if status_info:
                today_c = _pick(status_info, "today_credit", "todayCredit")
                streak = _pick(status_info, "streak_days", "streakDays",
                               "continuous_days", "continuousDays")
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

        exp_date = (time.strftime("%Y-%m-%d", time.localtime(exp_ms / 1000))
                    if exp_ms else "")
        buddy_lines = ["【Buddy加油站今日状态】"]
        if status == 'fail':
            buddy_lines.append("  签到：失败（%s）" % (lines[0] if lines else '未知错误'))
        else:
            if streak is not None:
                buddy_lines.append("  连续签到：%s 天" % streak)
            if today_c is not None:
                buddy_lines.append("  今日积分：%s" % today_c)
            if total is not None:
                buddy_lines.append("  累计积分：%s" % total)
        if exp_date:
            buddy_lines.append("  已续期至：%s" % exp_date)
        if chat_note:
            buddy_lines.append("  每日登录：%s" % chat_note)
        for ln in growth_lines:
            buddy_lines.append("  " + ln)
        appendix = "\n".join(buddy_lines) if len(buddy_lines) > 1 else ""

        # 签到失败仍返回状态块（对话、领奖已执行）
        if status == 'fail':
            return SignResult(False, '签到失败', None, '', appendix)

        detail = json.dumps(checkin_data, ensure_ascii=False)[:500] if checkin_data else ""
        msg = '今日已签到' if status == 'already' else '签到成功'
        return SignResult(True, msg, None, detail, appendix)

    def _claim_with_retry(self, token, uid, domain):
        """签到 + 自身重试；返回 (status, lines, checkin_data)。"""
        status, lines, checkin_data = None, [], {}
        for attempt in range(1, CHECKIN_RETRY_TIMES + 1):
            status, lines, checkin_data = self._do_claim(token, uid, domain)
            if status != 'fail':
                break
            err = lines[0] if lines else '未知错误'
            if attempt < CHECKIN_RETRY_TIMES:
                self.ctx.log('Buddy 签到第 %d/%d 次失败: %s，%d 秒后重试'
                             % (attempt, CHECKIN_RETRY_TIMES, err, CHECKIN_RETRY_INTERVAL), 'warning')
                time.sleep(CHECKIN_RETRY_INTERVAL)
            else:
                self.ctx.log('Buddy 签到第 %d/%d 次失败: %s'
                             % (attempt, CHECKIN_RETRY_TIMES, err), 'error')
        return status, lines, checkin_data

    # ---- 令牌加载 ----
    def _load_token(self):
        """只从 token_store 取令牌，不读本机登录态。"""
        store = dict(self.ctx.token_store or {})
        at = store.get("access_token")
        if not at:
            return "", 0, "", "", DEFAULT_DOMAIN, {}
        uid = store.get("uid") or _jwt_sub(at)
        nickname = (store.get("username")
                    or _jwt_payload(at).get("nickname")
                    or _jwt_payload(at).get("name") or "")
        exp_ms = store.get("expires_at") or (_jwt_exp(at) or 0) * 1000
        domain = store.get("domain") or self.domain_cfg or DEFAULT_DOMAIN
        return at, exp_ms, nickname, uid, domain, store

    # ---- 续期 ----
    def _maybe_refresh(self, token, uid, domain, store):
        """每日首次续期，成功回写 token_store。"""
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
        if data.get("refreshToken"):           # 会轮换，必须回存
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
        """调网关续期，成功返回 data，失败返回 None。"""
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

    # ---- 签到 / 状态 ----
    def _do_claim(self, token, uid, domain):
        """签到一次；服务端故障会复用 code=10001，故按 HTTP 状态码优先判定。"""
        st, body = self._call(ENDPOINT_CHECKIN, token, uid, domain)
        if st is None:
            return 'fail', ["网络不可达：所有 API 域名均失败"], {}
        try:
            resp = json.loads(body)
        except Exception:
            return 'fail', ["响应非 JSON：HTTP %s %s" % (st, (body or "")[:200])], {}
        code = resp.get("code")
        msg = resp.get("msg", "")
        data = resp.get("data") or {}

        # 5xx 一律失败，交给上层重试
        if st >= 500:
            return 'fail', ["服务端故障 HTTP %s code=%s msg=%s" % (st, code, msg)], data
        # 成功：2xx 且 code==0
        if 200 <= st < 300 and code == 0:
            credit = _pick(data, "credit", "today_credit", "daily_credit")
            lines = []
            if credit is not None:
                lines.append("本次获得积分：%s" % credit)
            return 'success', lines, data
        # 已签到：10001 且文案含「已签到」（正常为 HTTP 400，5xx 已拦）
        if code == 10001 and "已签到" in msg:
            return 'already', [], data
        # 其余（未知码、401/403、其它 4xx）均失败
        return 'fail', ["签到异常 HTTP %s code=%s msg=%s" % (st, code, msg)], data

    def _fetch_status(self, token, uid, domain):
        """签到状态；须用 POST。"""
        st, body = self._call(ENDPOINT_STATUS, token, uid, domain)
        if st == 200:
            try:
                return json.loads(body).get("data", {}) or {}
            except Exception:
                return {}
        return {}

    # ---- 连登奖励 ----
    def _load_growth_cfg(self, override):
        """默认配置 + 站点 api_config.growth 覆盖。"""
        c = dict(DEFAULT_GROWTH)
        if isinstance(override, dict):
            c.update({k: v for k, v in override.items() if v not in (None, "")})
        return c

    def _growth_enabled(self):
        return bool(self.growth_cfg.get("enabled", True))

    def _growth_call(self, path, token, uid, domain, payload=None):
        """成长接口：payload 为 None 走 GET，否则 POST。"""
        extra = {"X-Product": "SaaS"}
        body, method = None, "GET"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            method = "POST"
        st, text = self._call(ENDPOINT_GROWTH + path, token, uid, domain,
                              extra, body=body, method=method)
        if st is None:
            return None, {}
        try:
            return st, json.loads(text)
        except Exception:
            return st, {}

    def _run_growth_once(self, token, uid, domain, store):
        """每日一次领奖检查，返回推送行；无可领或已检查返回 []。"""
        if not self._growth_enabled():
            return []
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        state = dict(store.get("growth_state") or {})
        if state.get("last_checked_date") == today:
            self.ctx.log('Buddy 领奖: 今日(%s)已检查，跳过' % today)
            return []
        try:
            ok, lines = self._claim_growth_rewards(token, uid, domain)
        except Exception as e:
            self.ctx.log('Buddy 领奖异常: %s' % e, 'error')
            return ["领奖：异常（%s）" % str(e)[:120]]
        for ln in lines:
            self.ctx.log('Buddy 领奖: ' + ln)
        if ok:
            state["last_checked_date"] = today
            state["last_checked_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            store["growth_state"] = state
            self._persist_token_store(store)
        return lines

    def _claim_growth_rewards(self, token, uid, domain):
        """领取可领档位（服务端按档幂等），返回 (ok, 行)。"""
        st, summary = self._growth_call("/redeem/summary", token, uid, domain)
        if st is None:
            return False, ["领奖：查询失败（网络不可达）"]
        if st in (401, 403):
            return False, ["领奖：查询被拒绝（HTTP %s）" % st]
        if not (200 <= st < 300):
            return False, ["领奖：状态查询失败（HTTP %s）" % st]

        got, errors = [], []
        for tier, status_key, _label, days in GROWTH_TIERS:
            state = _dig(summary, status_key + "_status")
            if not state or state in ("claimed", "locked"):
                continue
            code, resp = self._growth_call("/redeem", token, uid, domain,
                                           {"tier": tier, "client_token": "u-%s" % uuid.uuid4()})
            # tier 被判 unknown 时退回天数重试（400 不会重复领取）
            if code == 400 and "tier" in str(_dig(resp, "msg") or "").lower():
                code, resp = self._growth_call("/redeem", token, uid, domain,
                                               {"tier": days, "client_token": "u-%s" % uuid.uuid4()})
            if code is not None and 200 <= code < 300:
                got.append("领%d天档：%s" % (days, self._reward_text(resp)))
            elif code == 403:
                continue            # 连登天数不足，不算失败
            else:
                errors.append("领%d天档：领取失败（HTTP %s %s）"
                              % (days, code, _dig(resp, "msg") or ""))

        if got:
            return True, got + errors
        if errors:
            return False, errors
        return True, []             # 无可领档位：静默

    @staticmethod
    def _reward_text(resp):
        """发放数量拼成「+100 积分 +1 补登卡」。"""
        bits = []
        for key, unit in (("credit_granted", "积分"), ("energy_granted", "能量"),
                          ("cards_granted", "补登卡"), ("chances_granted", "抽奖次数")):
            v = _dig(resp, key)
            if isinstance(v, int) and v:
                bits.append("+%s %s" % (v, unit))
        return " ".join(bits) if bits else "已领取"

    # ---- 每日对话 ----
    def _load_chat_cfg(self, override):
        """默认配置 + 站点 api_config.chat 覆盖。"""
        c = dict(DEFAULT_CHAT)
        if isinstance(override, dict):
            c.update({k: v for k, v in override.items() if v not in (None, "")})
        return c

    def _chat_enabled(self):
        return bool(self.chat_cfg.get("enabled", True))

    def _run_chat_once(self, token, uid, nickname, domain, store):
        """每日一次对话；当日已成功则跳过。返回 '完成' / '失败' / ''（禁用）。"""
        if not self._chat_enabled():
            return ''
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        state = dict(store.get("chat_state") or {})
        if state.get("last_chat_date") == today:
            self.ctx.log('Buddy 对话: 今日(%s)已成功发起，跳过（不重复发送）' % today)
            return '完成'
        try:
            ok, line = self._do_chat(token, uid, nickname, domain, store, state, self.chat_cfg)
        except Exception as e:
            self.ctx.log('Buddy 对话异常: ' + str(e), 'error')
            return '失败'
        self.ctx.log('Buddy 对话: ' + line)
        return '完成' if ok else '失败'

    def _do_chat(self, token, uid, username, domain, store, state, chat_cfg):
        """发提问并判定回复含「好」，成功写当日记录。返回 (ok, 行)。"""
        prompt = chat_cfg.get("prompt") or DEFAULT_CHAT["prompt"]
        model = chat_cfg.get("model") or "hy3"
        timeout = int(chat_cfg.get("timeout") or 90)
        if not state.get("machineId"):
            state["machineId"] = str(uuid.uuid4())
        if not state.get("qimei36"):
            state["qimei36"] = uuid.uuid4().hex + "1a60f"

        cid, stok, link, reused = self._resolve_conversation(token, uid, domain, state)
        ev = self._build_chat_event(uid, username, model, prompt, state, cid)
        rst, _ = self._report_chat(token, uid, domain, ev)

        ok, detail = False, "未执行"
        if cid and stok and link:
            aok, ares = self._acp_prompt(link, stok, cid, prompt, uid, domain, timeout)
            if aok:
                text = self._extract_acp_text(ares)
                if self._reply_has_ok(text):
                    ok = True
                    detail = "模型=%s 回复：%s" % (model, text[:120])
                else:
                    self.ctx.log("Buddy 对话 ACP 回复未含「好」(%s)，回退 chat/completions"
                                 % (text[:80] or str(ares)[:80]), "warning")
            else:
                self.ctx.log("Buddy 对话 ACP 失败(%s)，回退 chat/completions" % str(ares)[:120], "warning")
        if not ok:
            ok, detail = self._chat_completions(token, uid, domain, prompt, model, timeout)

        if ok:
            state["last_chat_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
            state["last_chat_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state["last_chat_model"] = model
        store["chat_state"] = state
        self._persist_token_store(store)

        line = "成功（%s，会话%s）" % (detail, "复用" if reused else "新建")
        if not ok:
            line = "失败：%s" % detail
        if rst is None:
            line += " | ⚠️ 上报未送达"
        return ok, line

    def _resolve_conversation(self, token, uid, domain, state):
        """优先复用已保存会话，拿不到才新建。"""
        old = state.get("conversationId")
        if old:
            for base in self.api_bases:
                st, body = self._call(ENDPOINT_SESSION % old, token, uid, domain, method="GET", base=base)
                if st == 200:
                    try:
                        d = json.loads(body).get("data") or {}
                        if d.get("token") and d.get("link"):
                            return old, d["token"], d["link"], True
                    except Exception:
                        pass
        cid, stok, link = self._create_conversation(token, uid, domain)
        if cid:
            state["conversationId"] = cid
        return cid, stok, link, False

    def _create_conversation(self, token, uid, domain):
        """创建会话，返回 (conversation_id, session_token, acp_link)。"""
        for base in self.api_bases:
            st, body = self._call(ENDPOINT_CONV, token, uid, domain, body=b"{}")
            if st is None:
                continue
            try:
                d = json.loads(body).get("data") or {}
                sess = d.get("session") or {}
                if d.get("id") and sess.get("link") and sess.get("token"):
                    return d["id"], sess["token"], sess["link"]
            except Exception:
                pass
        return None, None, None

    def _report_chat(self, token, uid, domain, ev):
        """上报 chat_request_send（连续登录记账信号）。"""
        body = json.dumps([ev], ensure_ascii=False).encode("utf-8")
        extra = {"X-Product": "SaaS", "X-Requested-With": "WorkBuddy"}
        st, rbody = self._call(ENDPOINT_REPORT, token, uid, domain, extra, body)
        code = None
        if st is not None:
            try:
                code = json.loads(rbody).get("code")
            except Exception:
                pass
        return st, code

    def _acp_prompt(self, link, session_token, conv_id, prompt, uid, domain, timeout):
        """ACP 发问：bootstrap 取连接 id，再 session/prompt。"""
        import http.client
        import ssl
        u = urlparse(link)
        host, base_path = u.hostname, (u.path or "/acp")
        ctx = ssl.create_default_context()
        boot = json.dumps({
            "initialize": {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": 1,
                "clientCapabilities": {"_meta": {"codebuddy.ai": {"cwd": "/workspace"}},
                                       "fs": {"readTextFile": False, "writeTextFile": False}}}},
            "sessionLoad": {"jsonrpc": "2.0", "id": 2, "method": "session/load", "params": {
                "sessionId": conv_id, "cwd": "/workspace", "mcpServers": [],
                "_meta": {"codebuddy.ai": {"round": 3}}}},
        }).encode()
        hdr = {"Authorization": "Bearer " + (session_token or ""),
               "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "X-User-Id": uid or "",
               "X-Domain": domain or DEFAULT_DOMAIN}

        c1 = http.client.HTTPSConnection(host, 443, context=ctx, timeout=timeout)
        c1.request("POST", base_path + "/bootstrap", body=boot,
                   headers=dict(hdr, **{"Content-Length": str(len(boot))}))
        r1 = c1.getresponse()
        conn_id = r1.getheader("Acp-Connection-Id")
        if not conn_id:
            c1.close()
            return False, "bootstrap 未返回 Acp-Connection-Id"

        p = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                        "params": {"sessionId": conv_id,
                                   "prompt": [{"type": "text", "text": prompt}]}}).encode()
        c2 = http.client.HTTPSConnection(host, 443, context=ctx, timeout=timeout)
        c2.request("POST", base_path, body=p,
                   headers=dict(hdr, **{"Content-Length": str(len(p)),
                                        "Acp-Connection-Id": conn_id}))
        r2 = c2.getresponse()
        if r2.status != 200:
            err = r2.read(300).decode("utf-8", "replace")
            c1.close()
            c2.close()
            return False, "HTTP %s %s" % (r2.status, err[:120])
        buf = b""
        t0 = time.time()
        try:
            while time.time() - t0 < timeout:
                d = r2.read1(1024)
                if not d:
                    break
                buf += d
                if b"stopReason" in buf or len(buf) > 30000:
                    break
        except Exception:
            pass
        c1.close()
        c2.close()
        return True, buf.decode("utf-8", "replace")

    @staticmethod
    def _extract_acp_text(raw):
        """抽取 SSE 回复文本；不能用 unicode_escape（服务端可能直传 UTF-8 中文）。"""
        parts = re.findall(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        out = []
        for p in parts:
            if "\\" in p:
                try:
                    out.append(json.loads('"%s"' % p))   # 交给 JSON 解析器正确反转义
                    continue
                except Exception:
                    pass
            out.append(p)
        return "".join(out).strip()

    def _chat_completions(self, token, uid, domain, prompt, model, timeout):
        """ACP 不可用时的回退：流式 chat/completions。"""
        import urllib.request
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                           "stream": True}, ensure_ascii=False).encode("utf-8")
        last = "无内容返回"
        for base in self.api_bases:
            req = urllib.request.Request(base.rstrip("/") + ENDPOINT_CHAT, data=body, method="POST")
            req.add_header("Authorization", "Bearer " + (token or ""))
            req.add_header("X-User-Id", uid or "")
            req.add_header("X-Domain", domain or DEFAULT_DOMAIN)
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "text/event-stream")
            req.add_header("User-Agent", "WorkBuddy/5.2.3")
            try:
                r = urllib.request.urlopen(req, timeout=timeout)
            except Exception as e:
                last = str(e)[:200]
                continue
            parts = []
            try:
                for raw in r:
                    s = raw.decode("utf-8", "replace").strip()
                    if not s.startswith("data:"):
                        continue
                    p = s[5:].strip()
                    if p == "[DONE]":
                        break
                    try:
                        d = json.loads(p)
                    except Exception:
                        continue
                    for ch in (d.get("choices") or []):
                        c = (ch.get("delta") or {}).get("content")
                        if c:
                            parts.append(c)
            finally:
                r.close()
            text = "".join(parts).strip()
            if text and self._reply_has_ok(text):
                return True, "模型=%s 回复：%s" % (model, text[:120])
            last = "回复未含「好」：%s" % text[:80] if text else "无内容返回"
        return False, last

    @staticmethod
    def _reply_has_ok(text):
        return "好" in (text or "")

    @staticmethod
    def _build_chat_event(uid, username, model, prompt, state, conv_id=None):
        now = int(time.time() * 1000)
        conv_id = conv_id or str(uuid.uuid4())
        req_id = uuid.uuid4().hex
        return {
            "eventCode": "chat_request_send",
            "timestamp": now,
            "reportDelay": 0,
            "mode": "craft",
            "conversationId": conv_id,
            "requestId": req_id,
            "inputLength": len(prompt),
            "requestModelId": model,
            "requestModelName": MODEL_NAMES.get(model, model),
            "isPlan": False,
            "isAutoExecuteTerminal": False,
            "isAutoModify": False,
            "codebaseEnable": False,
            "maxToken": 0,
            "maxSteps": 500,
            "temperature": 0,
            "maxRetries": 0,
            "mentionContexts": [],
            "knowledgeId": [],
            "knowledgeName": [],
            "codebaseId": "",
            "mentionContextCount": 0,
            "command": "",
            "recommendId": "",
            "skillId": "",
            "skillCount": 0,
            "totalCount": 0,
            "presentAt": now - 100,
            "traceId": req_id,
            "rootRequestId": req_id,
            "parentConversationId": conv_id,
            "agentName": "cli",
            "agentType": "main",
            "timezone": "Asia/Shanghai",
            "qimei36": state["qimei36"],
            "userId": uid,
            "username": username or "",
            "userNickname": username or "",
            "product": "SaaS",
            "releaseDate": 1789036585355,
            "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
            "os": "win32",
            "arch": "x64",
            "osVersion": "10.0.19045",
            "cpuModel": "AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx  ",
            "cpuCores": 8,
            "memorySize": 18,
            "vcsType": "unknown",
            "vcsRepo": "",
            "vcsBranchName": "",
            "vcsRevId": "",
            "codebuddy.session_id": conv_id,
            "codebuddy.conversation_request_id": req_id,
            "extName": "workbuddy-desktop",
            "extVersion": "5.5.6",
            "ideName": "WorkBuddy",
            "ideType": "WorkBuddy",
            "machineId": state["machineId"],
            "sessionId": str(uuid.uuid4()),
            "ideVersion": "5.5.6",
        }

    # ---- HTTP 工具 ----
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

    def _call(self, path, token, uid, domain, extra=None, body=None, method="POST", timeout=None, base=None):
        """按 api_bases 顺序尝试，仅网络异常才换域名；指定 base 时只请求该域名。"""
        last_err = None
        to = timeout if timeout is not None else self.timeout
        bases = [base] if base else self.api_bases
        for base in bases:
            url = base.rstrip("/") + path
            try:
                hd = self._headers(token, uid, domain, extra)
                if body is not None:
                    payload = body if isinstance(body, (str, bytes)) else json.dumps(body).encode("utf-8")
                    r = requests.request(method, url, headers=hd, data=payload, timeout=to, allow_redirects=False)
                else:
                    r = requests.request(method, url, headers=hd, timeout=to, allow_redirects=False)
                return r.status_code, r.text
            except Exception as e:
                last_err = str(e)
                self.ctx.log("WARN %s 不可达: %s" % (base, e), "warning")
        return None, last_err

    # ---- 持久化 ----
    def _persist_token_store(self, store):
        try:
            from models import update_site_token_store
            update_site_token_store(self.ctx.site_id,
                                    encrypt_data(json.dumps(store, ensure_ascii=False)))
        except Exception as e:
            logger.warning("持久化令牌失败: %s" % e)
