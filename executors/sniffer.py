# -*- coding: utf-8 -*-
"""
请求嗅探器（第3期）

用浏览器真实打开站点并点击签到，抓取过程中发出的接口请求（XHR/Fetch），
把签到动作还原成可直接复用的 API 请求配置。

典型用法：在能过验证的环境（如本机 Windows）嗅探一次，
把生成的接口配置用到 Docker 环境，之后即可脱离浏览器签到。
"""
import json
import time
import logging

from models import get_config
from utils import clean_headers, is_candidate, score_candidate
from .browser import (DrissionPageDriver, is_login_page,
                      perform_login, click_sign_button, _wait_cloudflare)

logger = logging.getLogger(__name__)


def packet_to_candidate(packet):
    """把 DrissionPage 捕获的数据包转换成接口候选"""
    try:
        req = packet.request
        url = req.url
        method = (req.method or 'GET').upper()
        headers = clean_headers(dict(req.headers))

        post_data = req.postData
        if post_data in (False, None):
            body = ''
        elif isinstance(post_data, (dict, list)):
            body = json.dumps(post_data, ensure_ascii=False, separators=(',', ':'))
        else:
            body = str(post_data)

        body_type = 'none'
        if body:
            ctype = ''
            for k, v in (req.headers or {}).items():
                if str(k).lower() == 'content-type':
                    ctype = str(v).lower()
            if 'json' in ctype or body.strip().startswith(('{', '[')):
                body_type = 'json'
            else:
                body_type = 'raw'

        response_text = ''
        try:
            if packet.response is not None:
                resp_body = packet.response.body
                response_text = resp_body if isinstance(resp_body, str) else str(resp_body)
        except Exception:
            response_text = ''

        return {
            'method': method,
            'url': url,
            'headers': headers,
            'body': body,
            'body_type': body_type,
            'resource_type': getattr(packet, 'resourceType', '') or '',
            'score': score_candidate(url),
            'response_preview': (response_text or '')[:200]
        }
    except Exception as e:
        logger.debug('解析数据包失败: %s' % e)
        return None


class RequestSniffer(object):
    """在浏览器中执行一次签到动作，抓取其发出的接口请求"""

    def __init__(self, ctx):
        self.ctx = ctx
        self.driver = None

    def capture(self, wait_after_click=8, max_packets=40):
        """
        :return: (ok: bool, message: str, candidates: list)
        """
        ctx = self.ctx
        site = dict(ctx.site)
        site['_plain_password'] = ctx.password
        sign_url = site.get('sign_url', '')
        login_url = site.get('login_url', '')
        has_cloudflare = bool(site.get('has_cloudflare', 0))
        cf_timeout = int(get_config('cf_timeout') or 60) if has_cloudflare else 0
        headless = get_config('headless') == '1'

        if not sign_url:
            return False, '未配置签到地址，无法嗅探', []

        try:
            self.driver = DrissionPageDriver(headless=headless)
            page = self.driver.page

            # 1. 打开签到页（带Cookie）
            self.driver.open(sign_url)
            self.driver.wait_for_load(5)
            if ctx.cookies:
                self.driver.set_cookies(ctx.cookies)
                self.driver.open(sign_url)
                self.driver.wait_for_load(10)
            else:
                self.driver.wait_for_load(10)
            time.sleep(3)

            if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout):
                return False, 'Cloudflare验证未通过，无法继续嗅探', []

            # 2. 未登录则先登录
            if is_login_page(self.driver) or (login_url and login_url in (page.url or '')):
                if not perform_login(self.driver, site, ctx.ocr_config, cf_timeout):
                    return False, '登录失败，无法继续嗅探', []
                time.sleep(1)
                ctx.set_cookies(self.driver.get_cookies())
                self.driver.open(sign_url)
                self.driver.wait_for_load(10)
                time.sleep(2)
                if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout):
                    return False, 'Cloudflare验证未通过，无法继续嗅探', []

            # 3. 开始监听（只收 XHR/Fetch，忽略静态资源）
            page.listen.start(targets=True, method=True, res_type=('XHR', 'Fetch'))

            # 4. 触发签到动作
            clicked = click_sign_button(self.driver, site)
            ctx.log('嗅探：签到按钮点击结果 = %s' % bool(clicked))

            # 5. 收集数据包
            packets = []
            deadline = time.time() + wait_after_click
            while time.time() < deadline and len(packets) < max_packets:
                try:
                    packet = page.listen.wait(count=1, timeout=2)
                except Exception:
                    break
                if not packet:
                    continue
                if isinstance(packet, list):
                    packets.extend(packet)
                else:
                    packets.append(packet)

            try:
                page.listen.stop()
            except Exception:
                pass

            # 6. 转换成候选并排序
            candidates = []
            seen = set()
            for p in packets:
                item = packet_to_candidate(p)
                if not item:
                    continue
                if not is_candidate(item['url'], item.get('resource_type')):
                    continue
                key = '%s %s' % (item['method'], item['url'])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(item)

            candidates.sort(key=lambda x: x['score'], reverse=True)
            if not candidates:
                return False, '未捕获到接口请求：该站点签到可能不发送XHR请求（纯页面跳转），请改用cURL或手动填写', []

            # 7. 嗅探过程中Cookie可能刷新，一并回传
            try:
                ctx.set_cookies(self.driver.get_cookies())
            except Exception:
                pass

            return True, '共捕获 %s 个候选请求' % len(candidates), candidates
        except Exception as e:
            logger.exception('嗅探异常')
            return False, '嗅探异常: %s' % e, []
        finally:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
                self.driver = None


def candidates_to_api_config(candidate, use_site_cookies=True):
    """把选中的候选请求转成可直接保存的 api_config 字符串"""
    cfg = {
        'use_site_cookies': bool(use_site_cookies),
        'refresh_cookie_by_browser': False,
        'timeout': 20,
        'follow_redirect': True,
        'verify_ssl': True,
        'steps': [{
            'name': '签到',
            'method': candidate.get('method') or 'GET',
            'url': candidate.get('url') or '',
            'headers': candidate.get('headers') or {},
            'body': candidate.get('body') or '',
            'body_type': candidate.get('body_type') or 'none',
            'extract': {}
        }]
    }
    return json.dumps(cfg, ensure_ascii=False, indent=2)
