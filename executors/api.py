# -*- coding: utf-8 -*-
"""
API 接口模式执行器

直接发送 HTTP 请求完成签到，不启动浏览器。
适用于：前后端分离站点、有可用接口的老站、App 同源接口，
以及任何被 Cloudflare / 验证码挡住但在浏览器里能抓到签到请求的站点。

配置（站点 api_config，JSON）示例：
{
  "use_site_cookies": true,       // 自动带上站点已保存的Cookie
  "refresh_cookie_by_browser": false, // 是否先用浏览器登录刷新Cookie
  "timeout": 20,
  "follow_redirect": true,
  "verify_ssl": true,
  "step_delay": 0,
  "variables": {},                 // 静态变量
  "steps": [
    {
      "name": "签到",
      "method": "GET",
      "url": "https://example.com/attendance.php?action=sign",
      "headers": {"Cookie": "{{cookie}}", "User-Agent": "..."},
      "body": "",
      "body_type": "none",         // none / json / form / raw
      "extract": {"token": "json:$.data.token"}
    }
  ]
}
"""
import json
import logging
import re
import time

import requests

from utils import render_vars, extract_value, check_success_rule, parse_curl, NORMAL_UA as DEFAULT_UA
from .base import BaseExecutor, SignResult, register_executor, filter_cookies

logger = logging.getLogger(__name__)


def normalize_api_config(cfg):
    """
    归一化 API 配置：兼容"单请求"写法与"多步骤"写法
    单请求写法：{"method":"GET","url":"...","headers":{},"body":""}
    """
    cfg = cfg or {}
    steps = cfg.get('steps')
    if not steps:
        if cfg.get('url'):
            steps = [{
                'name': cfg.get('name') or '签到',
                'method': cfg.get('method') or 'GET',
                'url': cfg.get('url'),
                'headers': cfg.get('headers') or {},
                'body': cfg.get('body') or '',
                'body_type': cfg.get('body_type') or 'none',
                'extract': cfg.get('extract') or {}
            }]
        else:
            steps = []
    result = dict(cfg)
    result['steps'] = [s for s in steps if s.get('url')]
    return result


@register_executor
class ApiExecutor(BaseExecutor):
    mode = 'api'
    label = 'API接口模式'
    description = '直接发送HTTP请求签到，无需浏览器，速度快且不受人机验证影响'

    def __init__(self, ctx):
        BaseExecutor.__init__(self, ctx)
        self.session = None

    def run(self):
        ctx = self.ctx
        cfg = normalize_api_config(ctx.api_config)
        steps = cfg.get('steps') or []
        if not steps:
            return SignResult(False, 'API模式未配置任何请求，请先填写接口信息或导入cURL', ctx.cookies)

        # 1. 可选：先用浏览器登录刷新Cookie（解决接口依赖登录态、Cookie会过期的问题）
        if cfg.get('refresh_cookie_by_browser'):
            from .browser import BrowserExecutor
            ctx.log('API模式：先用浏览器登录刷新Cookie')
            ok, reason = BrowserExecutor(ctx).login_and_refresh_cookies()
            ctx.log('刷新Cookie结果: %s - %s' % (ok, reason))
            if not ok:
                return SignResult(False, 'Cookie刷新失败：%s' % reason, ctx.cookies)

        timeout = int(cfg.get('timeout') or 20)
        follow_redirect = bool(cfg.get('follow_redirect', True))
        verify_ssl = bool(cfg.get('verify_ssl', True))
        step_delay = float(cfg.get('step_delay') or 0)
        use_site_cookies = bool(cfg.get('use_site_cookies', True))
        proxy = (cfg.get('proxy') or '').strip() or None

        # 2. 静态变量入池
        for k, v in (cfg.get('variables') or {}).items():
            ctx.vars[k] = render_vars(v, ctx)

        self.session = requests.Session()
        if proxy:
            self.session.proxies = {'http': proxy, 'https': proxy}

        last_status = 0
        last_text = ''
        last_name = ''
        last_headers = {}

        try:
            for index, step in enumerate(steps):
                name = step.get('name') or ('步骤%s' % (index + 1))
                last_name = name
                try:
                    status, text, headers = self._do_request(
                        step, timeout, follow_redirect, verify_ssl, use_site_cookies)
                except Exception as e:
                    ctx.log('步骤「%s」请求异常: %s' % (name, e), 'error')
                    return SignResult(False, '步骤「%s」请求失败：%s' % (name, e), ctx.cookies)

                last_status, last_text, last_headers = status, text, headers
                ctx.log('步骤「%s」完成，HTTP %s，响应长度 %s' % (name, status, len(text or '')))

                # Cookie 滚动更新
                self._sync_cookies()

                # 变量提取
                for var_name, expr in (step.get('extract') or {}).items():
                    value = extract_value(text, headers, self._cookie_dict(), expr)
                    ctx.vars[var_name] = value
                    if value:
                        ctx.log('提取变量 %s = %s' % (var_name, value[:80]))
                    else:
                        snippet = ' '.join((text or '').split())[:150]
                        ctx.log('提取变量 %s 失败（响应片段：%s）' % (var_name, snippet), 'warning')

                if step_delay > 0 and index < len(steps) - 1:
                    time.sleep(step_delay)
        finally:
            if self.session:
                try:
                    self.session.close()
                except Exception:
                    pass

        # 3. Cloudflare 拦截识别：命中即明确报错，避免用户对判定结果困惑
        low_headers = {str(k).lower(): str(v).lower() for k, v in (last_headers or {}).items()}
        cf_page_markers = ('challenge-error-text', 'just a moment', 'cf-browser-verification',
                           'cf_chl_opt', 'checking your browser')
        if (low_headers.get('cf-mitigated') == 'challenge'
                or any(m in (last_text or '').lower() for m in cf_page_markers)):
            return SignResult(False, '请求被Cloudflare拦截（返回人机验证页）。'
                                     '该站点开启了CF人机验证，API模式无法通过，'
                                     '请改用浏览器模式或放弃自动签到', ctx.cookies)

        # 4. 判定
        rule = ctx.success_rule or {}
        if not rule:
            # 未单独配置判定规则时，使用内置签到关键词兜底
            rule = {'contains': ctx.success_keywords}
        ok, reason = check_success_rule(rule, last_status, last_text)
        message = '%s：%s' % (last_name, reason)
        if not ok:
            # 判定失败时附带响应片段（跳过head，直接取正文），方便用户反推该用什么关键词
            m = re.search(r'<body[^>]*>(.*)', last_text or '', re.S | re.I)
            snippet_src = m.group(1) if m else (last_text or '')
            snippet = ' '.join(snippet_src.split())[:200]
            if snippet:
                message += '（响应片段：%s）' % snippet
        return SignResult(ok, message, ctx.cookies, (last_text or '')[:500])

    # ---------- 内部实现 ----------
    def _do_request(self, step, timeout, follow_redirect, verify_ssl, use_site_cookies):
        ctx = self.ctx
        method = (step.get('method') or 'GET').upper()
        url = render_vars(step.get('url'), ctx)
        headers = {}
        for k, v in (step.get('headers') or {}).items():
            headers[str(k)] = render_vars(v, ctx)
        # content-length 交给 requests 计算；accept-encoding 强制剥离：
        # 旧配置里可能保留了浏览器的 br/zstd，requests 无法解压会导致响应乱码
        headers = {k: v for k, v in headers.items()
                   if str(k).lower() not in ('content-length', 'accept-encoding')}

        if use_site_cookies and ctx.cookies:
            if not any(str(k).lower() == 'cookie' for k in headers):
                headers['Cookie'] = ctx.cookie_string
            for c in ctx.cookies:
                try:
                    self.session.cookies.set(c['name'], c['value'],
                                             domain=(c.get('domain') or '').lstrip('.') or None,
                                             path=c.get('path') or '/')
                except Exception:
                    pass

        ua_key = next((k for k in headers if str(k).lower() == 'user-agent'), None)
        if ua_key is None or not str(headers[ua_key]).strip():
            # 未配置或{{ua}}无值（未开启浏览器刷新Cookie）时使用默认UA
            if ua_key is not None:
                del headers[ua_key]
            headers['User-Agent'] = DEFAULT_UA

        body = render_vars(step.get('body') or '', ctx)
        body_type = (step.get('body_type') or 'none').lower()
        kwargs = {
            'headers': headers,
            'timeout': timeout,
            'allow_redirects': follow_redirect,
            'verify': verify_ssl
        }

        if body and body_type == 'json':
            headers.setdefault('Content-Type', 'application/json;charset=UTF-8')
            kwargs['data'] = body.encode('utf-8')
        elif body and body_type == 'form':
            kwargs['data'] = self._parse_body_pairs(body)
        elif body:
            headers.setdefault('Content-Type', 'application/x-www-form-urlencoded;charset=UTF-8')
            kwargs['data'] = body.encode('utf-8')

        resp = self.session.request(method, url, **kwargs)
        # 修正编码，避免中文响应被识别成 latin-1 导致关键词判定失败
        if not resp.encoding or 'charset' not in (resp.headers.get('Content-Type') or '').lower():
            resp.encoding = resp.apparent_encoding or 'utf-8'
        return resp.status_code, resp.text or '', dict(resp.headers)

    @staticmethod
    def _parse_body_pairs(body):
        """把 a=1&b=2 解析为字典，避免对已编码内容二次编码"""
        from urllib.parse import unquote_plus
        pairs = {}
        for part in body.split('&'):
            if not part:
                continue
            if '=' in part:
                k, v = part.split('=', 1)
                pairs[unquote_plus(k)] = unquote_plus(v)
            else:
                pairs[unquote_plus(part)] = ''
        return pairs

    def _cookie_dict(self):
        return {c.name: c.value for c in self.session.cookies}

    def _sync_cookies(self):
        """把响应中更新的Cookie合并回上下文，实现Cookie滚动续期"""
        if not self.session:
            return
        try:
            current = {c['name']: c for c in self.ctx.cookies}
            changed = False
            for c in self.session.cookies:
                old = current.get(c.name)
                if old is None:
                    self.ctx.cookies.append({
                        'name': c.name, 'value': c.value,
                        'domain': (c.domain or '').lstrip('.'), 'path': c.path or '/'
                    })
                    changed = True
                elif old.get('value') != c.value:
                    old['value'] = c.value
                    changed = True
            if changed:
                self.ctx.cookies = filter_cookies(self.ctx.cookies)
                self.ctx.cookies_changed = True
        except Exception as e:
            logger.debug('同步Cookie失败: %s' % e)


def build_config_from_curl(curl_text):
    """把 cURL 文本转换成可直接存储的 api_config 字符串"""
    parsed = parse_curl(curl_text)
    cfg = {
        'use_site_cookies': True,
        'timeout': 20,
        'follow_redirect': True,
        'verify_ssl': True,
        'steps': [{
            'name': '签到',
            'method': parsed['method'],
            'url': parsed['url'],
            'headers': parsed['headers'],
            'body': parsed['body'],
            'body_type': parsed['body_type'],
            'extract': {}
        }]
    }
    return json.dumps(cfg, ensure_ascii=False, indent=2), parsed
