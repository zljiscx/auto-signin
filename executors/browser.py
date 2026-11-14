# -*- coding: utf-8 -*-
"""
浏览器模式执行器

通过 DrissionPage 驱动 Chromium 模拟真人操作完成登录与签到。
适用于：需要渲染页面、点击按钮、处理图片验证码/Cloudflare 的站点。
浏览器签到逻辑集中在本文件，行为与历史版本保持一致。
"""
import os
import time
import json
import shutil
import tempfile
import logging
import subprocess
import threading
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests
from DrissionPage import ChromiumPage, ChromiumOptions

from models import get_config
from utils import (
    ocr_captcha, is_docker,
    JS_FILL_TEMPLATE, JS_GET_SRC_TEMPLATE, JS_FILL_CAPTCHA_TEMPLATE, JS_CLICK_TEMPLATE,
    DEFAULT_USERNAME_SELECTORS, DEFAULT_PASSWORD_SELECTORS,
    DEFAULT_CAPTCHA_IMG_SELECTORS, DEFAULT_CAPTCHA_INPUT_SELECTORS,
    DEFAULT_SUBMIT_SELECTORS, DEFAULT_SIGN_BUTTON_SELECTORS,
    LOGIN_SUCCESS_KEYWORDS
)
from .base import BaseExecutor, SignResult, register_executor

logger = logging.getLogger(__name__)

# 调试截图目录，Docker下挂载此目录查看截图
DEBUG_SCREENSHOT_DIR = os.environ.get('DEBUG_SCREENSHOT_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'debug'))

# ---------------- 桌面浏览器模式（CDP） ----------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 本地拉起的桌面浏览器使用固定 profile：保留登录态与 cf_clearance，下次运行免登录
DESKTOP_PROFILE_DIR = os.path.join(PROJECT_ROOT, 'chrome_data', 'desktop')
DESKTOP_LOCAL_PORT = 9333
# 远端 CDP 候选地址：容器宿主(zljiscx) -> 组网固定入口 -> 本机
DESKTOP_CANDIDATE_ADDRESSES = ('172.20.0.1:9222', '100.100.1.10:9222', '127.0.0.1:9222')
# CF 挑战页（interstitial）特征：命中才考虑重载，避免误伤内嵌 Turnstile 的正常页面
CF_CHALLENGE_MARKERS = (
    'Just a moment', 'cf-browser-verification', 'cf_chl_opt',
    'Checking your browser before accessing', 'Attention Required! | Cloudflare',
    'Enable JavaScript and cookies to continue',
)
NO_PROXY_MAP = {'http': None, 'https': None}


def _ensure_no_proxy(*hosts):
    """把 CDP 地址加入 NO_PROXY：容器/PC 上的全局代理会让 requests 与驱动连不上本机端口"""
    items = [x.strip() for x in (os.environ.get('NO_PROXY') or '').split(',') if x.strip()]
    for h in ('127.0.0.1', 'localhost') + tuple(hosts):
        h = (h or '').split(':')[0]
        if h and h not in items:
            items.append(h)
    if items:
        val = ','.join(items)
        os.environ['NO_PROXY'] = val
        os.environ['no_proxy'] = val


def cdp_version(address, timeout=3):
    """读取 CDP 的 /json/version，返回版本字典；不可达返回 None"""
    try:
        r = requests.get('http://%s/json/version' % address, timeout=timeout, proxies=NO_PROXY_MAP)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _cdp_tabs(address, timeout=3):
    try:
        return requests.get('http://%s/json' % address, timeout=timeout, proxies=NO_PROXY_MAP).json() or []
    except Exception:
        return []


def forget_page_cache(address=None):
    """清 DrissionPage 两级缓存：页面实例(_PAGES) 与浏览器连接(BROWSERS)。

    只清其中一层的话，重连拿到的仍是已断开的旧对象，表现为「与页面的连接已断开」。
    """
    try:
        from DrissionPage._base.browser import Browser
        for bid, browser in list(Browser.BROWSERS.items()):
            if address is None or getattr(browser, 'address', None) == address:
                Browser.BROWSERS.pop(bid, None)
    except Exception as e:
        logger.debug('清理浏览器缓存异常: %s' % e)
    try:
        for key, page in list(ChromiumPage._PAGES.items()):
            if address is None or getattr(page, 'address', None) == address:
                ChromiumPage._PAGES.pop(key, None)
    except Exception as e:
        logger.debug('清理页面缓存异常: %s' % e)


def probe_alive(driver, timeout=5):
    """探活：断开后首次调用会抛异常，但半死连接可能一直阻塞，所以放线程里限时"""
    box = {}

    def target():
        try:
            box['ok'] = driver.run_js('return 1') is not None
        except Exception:
            box['ok'] = False

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    return not th.is_alive() and bool(box.get('ok'))


def ensure_page_tab(address):
    """保证至少有一个 page 标签：远程连接会接管标签，关光了就再也连不上"""
    if any(t.get('type') == 'page' for t in _cdp_tabs(address)):
        return True
    try:
        # 实测 GET /json/new 返回 405，必须用 PUT
        requests.put('http://%s/json/new?about:blank' % address, timeout=5, proxies=NO_PROXY_MAP)
        return True
    except Exception as e:
        logger.debug('补建标签页失败: %s' % e)
        return False


# 远程浏览器连接复用：断开后重连会被 Chrome 踢掉，整进程只连一次
_REMOTE_DRIVERS = {}


def get_remote_driver(address):
    """取得（或建立）该地址的共享驱动；连接已死时清缓存重连"""
    driver = _REMOTE_DRIVERS.get(address)
    if driver is not None and probe_alive(driver):
        return driver
    forget_page_cache(address)
    driver = DrissionPageDriver(headless=False, address=address)
    _REMOTE_DRIVERS[address] = driver
    return driver


def find_local_browser(path_cfg=''):
    """定位本机 Chrome/Edge；path_cfg 为设置页显式配置的路径，优先使用"""
    cands = [path_cfg] if path_cfg else []
    if os.name == 'nt':
        for env in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            base = os.environ.get(env)
            if not base:
                continue
            cands.append(os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
            cands.append(os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    else:
        cands += ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser']
    for path in cands:
        if path and os.path.isfile(path):
            return path
    return ''


def start_local_browser(port=DESKTOP_LOCAL_PORT, path_cfg=''):
    """拉起本地桌面浏览器（有头、固定 profile、开放 CDP 端口），返回可连接的地址"""
    address = '127.0.0.1:%d' % port
    if cdp_version(address, 2):
        logger.info('复用已运行的本地桌面浏览器: %s' % address)
        return address
    exe = find_local_browser(path_cfg)
    if not exe:
        raise RuntimeError('未找到本地 Chrome/Edge，请在设置页填写「桌面浏览器路径」')
    os.makedirs(DESKTOP_PROFILE_DIR, exist_ok=True)
    args = [
        exe,
        '--remote-debugging-port=%d' % port,
        '--remote-debugging-address=127.0.0.1',
        '--user-data-dir=%s' % DESKTOP_PROFILE_DIR,
        '--no-first-run', '--no-default-browser-check',
        '--disable-blink-features=AutomationControlled',
        '--remote-allow-origins=*',
        '--window-size=1280,900',
    ]
    kwargs = {}
    if os.name == 'nt':
        # 独立进程组/会话：浏览器是长驻共享资源，不应随调用方（脚本/服务）退出被带走
        kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                   | getattr(subprocess, 'DETACHED_PROCESS', 0))
    logger.info('拉起本地桌面浏览器: %s（profile=%s）' % (exe, DESKTOP_PROFILE_DIR))
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        raise RuntimeError('启动本地浏览器失败: %s' % e)

    for _ in range(40):
        if cdp_version(address, 1):
            return address
        time.sleep(0.5)
    raise RuntimeError('本地桌面浏览器启动超时（%s 未监听，profile 可能被其它进程占用）' % address)


def resolve_desktop_address(address_cfg='', path_cfg='', port=DESKTOP_LOCAL_PORT):
    """解析桌面浏览器(CDP)地址，返回 (address, 说明)；address 为 None 时说明即失败原因。

    取值顺序：设置页显式地址（local 表示强制本地拉起）> Windows 本地拉起 >
    候选远端地址探测。NAS 无桌面环境，只能连远端浏览器。
    """
    cfg = (address_cfg or '').strip()
    if cfg.lower().startswith('http'):
        cfg = cfg.split('://', 1)[1]
    if cfg.lower() == 'auto':
        cfg = ''

    if cfg:
        if cfg.lower() == 'local':
            try:
                return start_local_browser(port, path_cfg), '本地拉起'
            except RuntimeError as e:
                return None, str(e)
        addr = cfg.rstrip('/')
        _ensure_no_proxy(addr)
        ver = cdp_version(addr)
        if ver:
            return addr, ver.get('Browser') or '远程浏览器'
        return None, ('桌面浏览器地址 %s 无法连接：请确认浏览器已启动、端口已转发'
                      '（NAS 上还需组网/SSH 隧道就绪）' % addr)

    if os.name == 'nt':
        try:
            return start_local_browser(port, path_cfg), '本地拉起'
        except RuntimeError as e:
            logger.warning('本地桌面浏览器不可用（%s），改为探测远端 CDP' % e)

    for addr in DESKTOP_CANDIDATE_ADDRESSES:
        _ensure_no_proxy(addr)
        ver = cdp_version(addr, 2)
        if ver:
            return addr, ver.get('Browser') or '远程浏览器'
    return None, ('未找到可用的桌面浏览器：Windows 请安装 Chrome/Edge 或在设置页填写地址；'
                  'NAS 请在设置页填写桌面浏览器地址（如 100.100.1.10:9222）')


def _terminate_process(pid, wait_seconds=3):
    """兜底结束浏览器进程，避免残留进程占满内存（容器环境尤其重要）"""
    import signal

    def _alive():
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    if not _alive():
        return
    for sig in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGKILL', None)):
        if sig is None:
            continue
        try:
            os.kill(pid, sig)
        except Exception:
            return
        end = time.time() + wait_seconds
        while time.time() < end:
            if not _alive():
                return
            time.sleep(0.2)


def _debug_screenshot(driver, name):
    """保存调试截图到DEBUG_SCREENSHOT_DIR（使用CDP，兼容无头模式）"""
    try:
        os.makedirs(DEBUG_SCREENSHOT_DIR, exist_ok=True)
        path = os.path.join(DEBUG_SCREENSHOT_DIR, '%s.png' % name)
        import base64
        result = driver.page.run_cdp('Page.captureScreenshot', format='png')
        with open(path, 'wb') as f:
            f.write(base64.b64decode(result['data']))
        logger.info("调试截图已保存: %s" % path)
    except Exception as e:
        logger.debug("截图保存失败: %s" % e)


def _page_text(driver, limit=300):
    """取页面可见文本：HTML 前 500 字符永远是 <head>，对定位失败没有帮助"""
    try:
        text = driver.run_js('return document.body ? document.body.innerText : "";') or ''
        return ' '.join(str(text).split())[:limit]
    except Exception:
        return ''


def _accept_dialog(driver, timeout=0.8):
    """关掉 alert/confirm 弹窗（签到确认框），返回弹窗文本；无弹窗返回空串"""
    try:
        text = driver.page.handle_alert(accept=True, timeout=timeout)
        if text:
            logger.info("处理页面弹窗: %s" % text)
            return str(text)
    except Exception as e:
        logger.debug("弹窗处理异常: %s" % e)
    return ''


def _clean_cookie_text(value):
    """剔除控制字符：CDP 收到换行/制表符会整批拒收（曾出现「成功设置 0/6 个 Cookie」）"""
    if value is None:
        return ''
    return ''.join(ch for ch in str(value) if ch >= ' ')


def _looks_like_cf_challenge(html):
    """是否为 Cloudflare 挑战页（interstitial），内嵌 Turnstile 的正常页面不算"""
    html = html or ''
    return any(k in html for k in CF_CHALLENGE_MARKERS)


class BrowserDriver(ABC):
    @abstractmethod
    def open(self, url): pass

    @abstractmethod
    def get_cookies(self): pass

    @abstractmethod
    def set_cookies(self, cookies_list): pass

    @abstractmethod
    def get_page_source(self): pass

    @abstractmethod
    def get_current_url(self): pass

    @abstractmethod
    def fill_input(self, selector, value): pass

    @abstractmethod
    def click(self, selector): pass

    @abstractmethod
    def screenshot_captcha(self, selector): pass

    @abstractmethod
    def wait_for_load(self, timeout=10): pass

    @abstractmethod
    def run_js(self, js_code): pass

    @abstractmethod
    def close(self): pass


class DrissionPageDriver(BrowserDriver):
    def __init__(self, headless=False, address=None):
        self.is_remote = bool(address)
        self.address = (address or '').strip() or None
        self.site_domain = None       # 供 set_cookies 给 cf_ 开头的 cookie 指定域名
        self.temp_dir = None
        try:
            self.cf_retry = int(get_config('cf_retry') or 3)
        except Exception:
            self.cf_retry = 3
        try:
            self.cf_backoff = int(get_config('cf_backoff') or 5)
        except Exception:
            self.cf_backoff = 5

        if self.is_remote:
            # 远程模式：接管已有浏览器，复用它真实指纹与 profile；不创建临时目录、不启动本地进程
            _ensure_no_proxy(self.address)
            if not cdp_version(self.address):
                raise RuntimeError('CDP 连接失败：%s 不可达' % self.address)
            ensure_page_tab(self.address)
            co = ChromiumOptions()
            co.set_address(self.address)
            logger.info('连接已有浏览器(CDP): %s' % self.address)
            self.page = ChromiumPage(co)
            try:
                # 跨机/跨境连 CDP 时导航等待需要放宽，否则慢页面被判超时
                self.page.set.timeouts(page_load=40)
            except Exception:
                pass
            return

        # 本地模式：每个实例独立临时目录，用完即删
        self.temp_dir = tempfile.mkdtemp(prefix='autosign_chrome_')
        co = ChromiumOptions()
        if headless:
            co.headless()
        if is_docker():
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-dev-shm-usage')
        # 低性能NAS适配
        co.set_argument('--disable-gpu')
        co.set_argument('--disable-software-rasterizer')
        co.set_argument('--mute-audio')
        co.set_argument('--user-data-dir=%s' % self.temp_dir)
        logger.info("浏览器临时目录: %s" % self.temp_dir)
        self.page = ChromiumPage(co)

    def open(self, url):
        self.page.get(url)
        # 挑战页退避重载：CF 托管挑战常先返回一张 interstitial 页，重载后即可拿到真页面
        for i in range(max(0, self.cf_retry)):
            if not _looks_like_cf_challenge(self.get_page_source()):
                return
            logger.info('检测到 Cloudflare 挑战页，%d 秒后重新加载（%d/%d）'
                        % (self.cf_backoff, i + 1, self.cf_retry))
            time.sleep(self.cf_backoff)
            try:
                self.page.get(url)
            except Exception as e:
                logger.debug('挑战页重载失败: %s' % e)
                return

    def get_cookies(self):
        raw = self.page.cookies()
        domain = urlparse(self.page.url).netloc
        cookies_list = []
        if isinstance(raw, dict):
            for name, value in raw.items():
                cookies_list.append({
                    'name': name, 'value': value, 'domain': domain,
                    'path': '/', 'httpOnly': False, 'secure': False, 'expires': -1
                })
        elif isinstance(raw, list):
            for c in raw:
                clean = {
                    'name': c.get('name'), 'value': c.get('value'),
                    'domain': c.get('domain', domain), 'path': c.get('path', '/'),
                    'httpOnly': c.get('httpOnly', False), 'secure': c.get('secure', False),
                    'expires': c.get('expires', -1)
                }
                clean = {k: v for k, v in clean.items() if v is not None}
                cookies_list.append(clean)
        allowed = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}
        return [{k: v for k, v in c.items() if k in allowed} for c in cookies_list if 'name' in c and 'value' in c]

    def set_cookies(self, cookies_list):
        allowed_keys = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}
        page_domain = urlparse(self.page.url).netloc
        success_count = 0
        for c in cookies_list:
            clean = {k: v for k, v in c.items() if k in allowed_keys}
            name = _clean_cookie_text(clean.get('name'))
            if not name:
                continue
            clean['name'] = name
            clean['value'] = _clean_cookie_text(clean.get('value'))
            # cf_clearance 等绑定具体域名，显式指定站点域名，避免沿用页面跳转后的空域名
            if name.startswith('cf_') or name.startswith('__cf'):
                clean['domain'] = self.site_domain or clean.get('domain') or page_domain
            clean.setdefault('domain', page_domain)
            clean.setdefault('path', '/')
            if 'expires' in clean and (clean['expires'] is None or clean['expires'] == -1):
                del clean['expires']
            try:
                self.page.set.cookies(clean)
                success_count += 1
            except Exception as e:
                logger.debug("设置Cookie %s 失败: %s" % (clean.get('name'), e))
        logger.info("成功设置 %s/%s 个Cookie" % (success_count, len(cookies_list)))

    def get_page_source(self):
        return self.page.html

    def get_current_url(self):
        return self.page.url

    def fill_input(self, selector, value):
        for _ in range(5):
            ele = self.page.ele(selector, timeout=0.5)
            if ele:
                ele.input(value)
                return True
            time.sleep(1)
        logger.warning("未找到元素 %s" % selector)
        return False

    def click(self, selector):
        for _ in range(5):
            ele = self.page.ele(selector, timeout=0.5)
            if ele:
                ele.click()
                return True
            time.sleep(1)
        logger.warning("未找到元素 %s" % selector)
        return False

    def screenshot_captcha(self, selector):
        for _ in range(5):
            ele = self.page.ele(selector, timeout=0.5)
            if ele:
                return ele.screenshot()
            time.sleep(1)
        return None

    def wait_for_load(self, timeout=10):
        try:
            self.page.wait.page_load(timeout)
        except AttributeError:
            try:
                self.page.wait.load_complete(timeout)
            except AttributeError:
                time.sleep(min(timeout, 5))

    def run_js(self, js_code):
        return self.page.run_js(js_code)

    def get_frame(self, locator):
        """获取iframe对象，支持CSS/XPath选择器"""
        try:
            return self.page.get_frame(locator)
        except Exception:
            return None

    def close(self):
        if self.is_remote:
            self._close_remote_tab()
            return
        # 先记录进程号：DrissionPage 关闭时依赖 psutil，新版 psutil 可能抛错导致进程残留
        try:
            pid = self.page.process_id
        except Exception:
            pid = None
        try:
            self.page.close()
        except Exception as e:
            logger.debug("关闭浏览器异常: %s" % e)
        if pid:
            _terminate_process(pid)
        # 清理临时目录
        try:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception as e:
            logger.warning("清理临时目录失败: %s" % e)

    def _close_remote_tab(self):
        """远程模式结束：标签退回 about:blank，连接保留给下次复用"""
        if not self.address:
            return
        try:
            self.page.get('about:blank')
        except Exception as e:
            logger.debug('远程页面复位失败: %s' % e)
            _REMOTE_DRIVERS.pop(self.address, None)


# ---------------- 页面判断 ----------------
def is_logged_in(driver, keywords=None):
    """通过页面内容关键词判断是否已登录"""
    html = driver.get_page_source() or ''
    words = keywords or LOGIN_SUCCESS_KEYWORDS
    return any(kw in html for kw in words)


def is_login_page(driver):
    """多维度判断是否为登录页（放宽以兼容非 NexusPHP 架构，如 React/Vue SPA）"""
    url = (driver.get_current_url() or '').lower()
    login_keywords = ['login', 'signin', 'log-in', 'sign-in', '登录', '登入', '/login', '/signin']
    if any(k in url for k in login_keywords):
        return True
    try:
        # 存在密码输入框即大概率为登录页（用户名框命名多样：email/phone/account/identity 等）
        if driver.run_js("return !!document.querySelector('input[type=\"password\"]');"):
            return True
    except Exception:
        pass
    return False


# 登录失败提示关键词：出现即说明登录未成功，可提前结束等待（避免空等 30s）
LOGIN_FAIL_KEYWORDS = [
    '账号或密码错误', '帐号或密码错误', '用户名或密码错误', '用户名或密码不对',
    '密码错误', '密码不正确', '验证码错误', '验证码不正确', '验证码失效',
    '请先登录', '请先登陆', 'incorrect', 'invalid username', 'wrong password',
    'invalid credentials', 'login failed',
]


def _login_failed(html):
    """页面是否出现登录失败提示，返回命中的关键词或 None"""
    html = html or ''
    for kw in LOGIN_FAIL_KEYWORDS:
        if kw in html:
            return kw
    return None


def detect_login_fields(driver):
    """通用登录字段自适应识别：兼容 NexusPHP 之外的现代 SPA / React / Vue 等架构。

    返回 {username, password, submit} 选择器（CSS 或 XPath）；识别不到的字段为 None。
    优先使用 id/name/type/autocomplete/placeholder 等稳定属性，避免依赖会变化的
    CSS-Modules / Tailwind 哈希类名。用户显式配置的选择器始终优先，本函数仅作兜底。
    """
    js = r'''
    return (function(){
      function lower(s){ return (s||'').toLowerCase(); }
      function hasAny(s, keys){ s = lower(s); for (var i=0;i<keys.length;i++){ if(s.indexOf(keys[i])>=0) return true; } return false; }
      var PW = document.querySelector('input[type=password]');
      if(!PW) return JSON.stringify({username:null, password:null, submit:null});
      var form = PW.closest ? PW.closest('form') : null;
      var scope = form || document;
      var USER_KEYS = ['user','username','email','account','login','phone','mobile','identity','name','mail'];
      var USER_PH  = ['邮箱','用户名','手机','账号','账户','用户','email','phone','mail'];
      var LOGIN_TXT = ['登录','登入','登陆','提交','sign in','log in','login','submit','signin','sign in now'];
      var username = null, best = -1;
      var inputs = scope.querySelectorAll('input');
      for (var i=0;i<inputs.length;i++){
        var el = inputs[i];
        if (el === PW) continue;
        var t = lower(el.getAttribute('type'));
        if (t==='password'||t==='hidden'||t==='submit'||t==='button'||t==='checkbox'||t==='radio'||t==='file') continue;
        var score = 0;
        var ac = lower(el.getAttribute('autocomplete'));
        var nm = lower((el.getAttribute('name')||'') + ' ' + (el.getAttribute('id')||''));
        var ph = lower(el.getAttribute('placeholder'));
        if (/username|email|tel|nickname|account/.test(ac)) score += 100;
        if (hasAny(nm, USER_KEYS)) score += 60;
        if (hasAny(ph, USER_PH)) score += 60;
        if (t==='email'||t==='tel') score += 40;
        if (score > best){ best = score; username = el; }
      }
      function cssOf(el){
        var id = el.getAttribute('id');
        if (id && /^[A-Za-z][\w\-:]*$/.test(id)) return el.tagName.toLowerCase() + '#' + id;
        var name = el.getAttribute('name');
        if (name) return el.tagName.toLowerCase() + '[name="' + name + '"]';
        var ac = el.getAttribute('autocomplete');
        if (ac) return el.tagName.toLowerCase() + '[autocomplete="' + ac + '"]';
        var ph = el.getAttribute('placeholder');
        if (ph) return el.tagName.toLowerCase() + '[placeholder="' + ph + '"]';
        var ty = el.getAttribute('type');
        if (ty) return el.tagName.toLowerCase() + '[type="' + ty + '"]';
        return el.tagName.toLowerCase();
      }
      function findSubmit(ctx){
        var btns = ctx.querySelectorAll('button, input[type=submit]');
        for (var i=0;i<btns.length;i++){
          var b = btns[i];
          var isSubmit = (b.tagName==='INPUT' && lower(b.getAttribute('type'))==='submit') ||
                         (b.tagName==='BUTTON' && lower(b.getAttribute('type'))!=='button');
          if (isSubmit && hasAny(lower(b.textContent||b.value||''), LOGIN_TXT)) return b;
        }
        var all = ctx.querySelectorAll('button');
        for (var i=0;i<all.length;i++){
          if (hasAny(lower(all[i].textContent||''), LOGIN_TXT)) return all[i];
        }
        var sub = ctx.querySelector('button[type=submit], input[type=submit]');
        if (sub) return sub;
        return null;
      }
      var submit = findSubmit(scope);
      if (!submit && scope !== document) submit = findSubmit(document);
      function btnSel(b){
        var id = b.getAttribute('id');
        if (id && /^[A-Za-z][\w\-:]*$/.test(id)) return b.tagName.toLowerCase() + '#' + id;
        var txt = (b.textContent||b.value||'').trim();
        var t = lower(b.getAttribute('type'));
        if (txt){
          var x = txt.replace(/'/g, "\\'").slice(0, 12);
          if (t === 'submit') return "//" + b.tagName.toLowerCase() + "[@type='submit' and contains(normalize-space(.), '" + x + "')]";
          return "//" + b.tagName.toLowerCase() + "[contains(normalize-space(.), '" + x + "')]";
        }
        return b.tagName.toLowerCase() + "[type=submit]";
      }
      return JSON.stringify({
        username: username ? cssOf(username) : null,
        password: cssOf(PW),
        submit: submit ? btnSel(submit) : null
      });
    })()
    '''
    try:
        raw = driver.run_js(js)
        if not raw:
            return None
        data = json.loads(raw)
        logger.info("自动识别登录字段: username=%s password=%s submit=%s" % (
            data.get('username'), data.get('password'), data.get('submit')))
        return data
    except Exception as e:
        logger.debug("登录字段自动识别失败: %s" % e)
        return None


def _selector_exists(driver, sel):
    """判断选择器（支持 CSS 与 XPath，逗号分隔的多个选择器任一命中即可）是否在当前页面真实存在。"""
    if not sel:
        return False
    js = r'''
    return (function(sel){
      var list = (sel.trim().indexOf('//') === 0) ? [sel.trim()] : sel.split(',');
      for (var i=0;i<list.length;i++){
        var s = list[i].trim();
        if(!s) continue;
        try {
          if (s.indexOf('//') === 0){
            var r = document.evaluate(s, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            if (r.singleNodeValue) return true;
          } else if (document.querySelector(s)) {
            return true;
          }
        } catch(e){}
      }
      return false;
    })(%s)
    ''' % json.dumps(sel)
    try:
        return bool(driver.run_js(js))
    except Exception:
        return False


def _pick_selector(driver, candidates):
    """按候选优先级选择第一个在页面真实存在的选择器；兜底返回最后一个候选（填充时安全跳过）。"""
    for c in candidates:
        if c and _selector_exists(driver, c):
            return c
    return candidates[-1] if candidates else ''


def perform_login(driver, site, ocr_config, cf_timeout=0, logger_=logger):
    logger_.info("开始登录流程")
    login_url = site.get('login_url', '')
    username = (site.get('username') or '').strip()
    password_decrypted = site.get('_plain_password') or ''
    # 纯Cookie场景，账号密码都为空，跳过登录
    if not username and not password_decrypted:
        logger_.info("账号密码均为空，使用Cookie直接访问，跳过登录")
        return True
    if not username or not password_decrypted:
        logger_.warning("用户名或密码为空，无法登录")
        return False

    username_cfg = (site.get('username_selector') or '').strip()
    password_cfg = (site.get('password_selector') or '').strip()
    submit_cfg = (site.get('submit_selector') or '').strip()
    captcha_img_sel = site.get('captcha_img_selector') or ','.join(DEFAULT_CAPTCHA_IMG_SELECTORS)
    captcha_input_sel = site.get('captcha_input_selector') or ','.join(DEFAULT_CAPTCHA_INPUT_SELECTORS)

    # 如果当前不在登录页，先导航到登录页
    if not is_login_page(driver):
        driver.open(login_url)
        driver.wait_for_load(10)
        # 登录页可能也需要Cloudflare验证
        if cf_timeout > 0:
            if not _wait_cloudflare(driver, cf_timeout):
                logger_.warning("登录页Cloudflare验证超时")
                return False
        time.sleep(2)

    # 通用自适应（自愈式）：
    #   1) 始终探测真实表单字段；
    #   2) 用户配置的选择器只要在当前页面真能定位到元素就优先使用（尊重手动配置）；
    #   3) 配置失效（定位不到）时回退到自动探测，最后回退 DEFAULT_*_SELECTORS（保证原有功能无损）。
    detected = detect_login_fields(driver) or {}
    username_sel = _pick_selector(driver, [username_cfg, detected.get('username'), ','.join(DEFAULT_USERNAME_SELECTORS)])
    password_sel = _pick_selector(driver, [password_cfg, detected.get('password'), ','.join(DEFAULT_PASSWORD_SELECTORS)])
    submit_sel = _pick_selector(driver, [submit_cfg, detected.get('submit'), ','.join(DEFAULT_SUBMIT_SELECTORS)])
    logger_.info("登录字段选择器 -> 账号:%s 密码:%s 提交:%s" % (username_sel, password_sel, submit_sel))

    js_fill = JS_FILL_TEMPLATE % (
        json.dumps(username_sel), json.dumps(username),
        json.dumps(password_sel), json.dumps(password_decrypted)
    )
    driver.run_js(js_fill)

    if site.get('has_captcha', 0):
        js_get_src = JS_GET_SRC_TEMPLATE % (json.dumps(captcha_img_sel))
        img_src = driver.run_js(js_get_src)
        if img_src:
            import requests
            try:
                cookies = driver.get_cookies()
                session = requests.Session()
                for c in cookies:
                    session.cookies.set(c['name'], c['value'], domain=c.get('domain', ''), path=c.get('path', '/'))
                img_data = session.get(img_src, timeout=10).content
                ocr_text = ocr_captcha(img_data, ocr_config.get('api_key', ''), ocr_config.get('secret_key', ''))
                if ocr_text:
                    js_fill_cap = JS_FILL_CAPTCHA_TEMPLATE % (
                        json.dumps(captcha_input_sel), json.dumps(ocr_text)
                    )
                    driver.run_js(js_fill_cap)
            except Exception as e:
                logger_.warning("验证码处理异常: %s" % e)

    js_click = JS_CLICK_TEMPLATE % (json.dumps(submit_sel))
    driver.run_js(js_click)

    # 结果判定：离开登录页 / 出现已登录标识 => 成功；出现错误提示 => 提前失败
    max_wait = 30
    start = time.time()
    logout_kw = ('退出', '登出', 'logout', '安全退出', '欢迎您回来')
    while time.time() - start < max_wait:
        html = driver.get_page_source() or ''
        if not is_login_page(driver):
            time.sleep(1)
            return True
        if any(k in html for k in logout_kw):
            time.sleep(1)
            return True
        failed = _login_failed(html)
        if failed:
            logger_.warning("登录失败（页面提示：%s）" % failed)
            return False
        time.sleep(1)
    return False


def click_sign_button(driver, site, logger_=logger):
    sign_btn_sel = site.get('sign_button_selector', '')
    # 1. 用户自定义选择器优先
    if sign_btn_sel:
        try:
            ele = driver.page.ele(sign_btn_sel, timeout=2)
            if ele:
                ele.scroll_to_see()
                time.sleep(0.5)
                ele.click()
                return True
        except Exception as e:
            logger_.debug("自定义选择器点击失败:%s" % e)
        # DrissionPage 不认 'input[value="签到"]' 这类纯 CSS 写法，再用 JS 试一次
        try:
            if driver.run_js(JS_CLICK_TEMPLATE % json.dumps(sign_btn_sel)):
                return True
        except Exception as e:
            logger_.debug("自定义CSS选择器点击失败:%s" % e)

    # 2. 遍历默认class选择器
    for sel in DEFAULT_SIGN_BUTTON_SELECTORS:
        try:
            ele = driver.page.ele(sel, timeout=1)
            if ele:
                ele.scroll_to_see()
                time.sleep(0.3)
                ele.click()
                return True
        except Exception:
            continue

    # 3. 终极JS兜底：滚动到底、遍历可点击元素、按文案定位、模拟全套鼠标事件
    js_code = '''
    async function clickSignBtn(){
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise(r=>setTimeout(r,800));
        let allElements = Array.from(document.querySelectorAll(
            '.x-button, button, input[type=submit], input[type=button], a'));
        const frames = document.querySelectorAll('iframe');
        for(let frame of frames){
            try{
                const frameDoc = frame.contentDocument || frame.contentWindow.document;
                const frameBtns = frameDoc.querySelectorAll(
                    '.x-button, button, input[type=submit], input[type=button], a');
                allElements = allElements.concat(Array.from(frameBtns));
            }catch(e){}
        }
        function txtOf(el){ return (el.textContent || el.value || '').trim(); }
        // 优先“立即签到”，排除签到记录/已签到
        let target = null;
        for(let el of allElements){
            if(txtOf(el) === "立即签到"){ target = el; break; }
        }
        if(!target){
            for(let el of allElements){
                const t = txtOf(el);
                if(t.indexOf("立即签到") >= 0){ target = el; break; }
            }
        }
        if(!target) return false;
        target.scrollIntoView({behavior:'smooth',block:'end'});
        await new Promise(r=>setTimeout(r,600));
        target.style.display = 'block';
        target.style.visibility = 'visible';
        target.style.opacity = '1';
        // uniapp 全套触摸/鼠标事件
        target.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
        target.dispatchEvent(new MouseEvent('click',{bubbles:true}));
        target.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
        target.dispatchEvent(new MouseEvent('tap',{bubbles:true}));
        return true;
    }
    return clickSignBtn();
    '''

    return driver.run_js(js_code)


# ---------------- Cloudflare 处理 ----------------
def _turnstile_token_len(driver):
    """读 Turnstile 响应 token 长度；-1 表示页面没有该字段。

    DrissionPage 的 ele() 不认 'input[name=...]' 这类纯 CSS 写法，统一走 JS 取值。
    """
    try:
        return int(driver.run_js(
            'var e=document.querySelector(\'input[name="cf-turnstile-response"]\');'
            "return e ? (e.value || '').length : -1;"))
    except Exception:
        return -1


def _has_turnstile(driver):
    """检测页面是否存在Cloudflare Turnstile验证组件"""
    if _turnstile_token_len(driver) >= 0:
        return True
    try:
        ele = driver.page.ele('tag:iframe@src()=challenges.cloudflare.com', timeout=1)
        if ele:
            return True
    except Exception:
        pass
    try:
        html = driver.get_page_source()
        if 'challenges.cloudflare.com' in html or 'cf-turnstile' in html:
            return True
    except Exception:
        pass
    return False


def _turnstile_rect(driver):
    """取 Turnstile 组件在视口中的位置：跨域 iframe 的文字读不到，只能靠坐标点"""
    js = '''
    var sels = ['.cf-turnstile', '[data-sitekey]', 'iframe[src*="challenges.cloudflare.com"]'];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (!el) continue;
      el.scrollIntoView({block: 'center'});
      var r = el.getBoundingClientRect();
      if (r.width > 20 && r.height > 20) {
        return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height});
      }
    }
    return '';
    '''
    try:
        raw = driver.run_js(js)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _click_turnstile(driver, rect):
    """在组件左侧复选框位置派发真实鼠标事件（CDP 事件被浏览器视为可信操作）"""
    x = rect['x'] + 30
    y = rect['y'] + rect['h'] / 2
    page = driver.page
    try:
        page.run_cdp('Input.dispatchMouseEvent', type='mouseMoved', x=x, y=y)
        time.sleep(0.2)
        page.run_cdp('Input.dispatchMouseEvent', type='mousePressed',
                     x=x, y=y, button='left', clickCount=1)
        time.sleep(0.08)
        page.run_cdp('Input.dispatchMouseEvent', type='mouseReleased',
                     x=x, y=y, button='left', clickCount=1)
        return True
    except Exception as e:
        logger.debug("Turnstile点击失败: %s" % e)
        return False


def _handle_turnstile(driver, timeout=30):
    """处理 Cloudflare Turnstile：以响应 token 为准，点击只按组件坐标"""
    if not _has_turnstile(driver):
        return True

    logger.info("检测到Cloudflare Turnstile验证组件")

    start = time.time()
    click_count = 0

    while time.time() - start < timeout:
        token_len = _turnstile_token_len(driver)
        if token_len > 20:
            logger.info("Turnstile验证已通过（token %d 字符）" % token_len)
            return True

        rect = _turnstile_rect(driver)
        if rect:
            click_count += 1
            logger.info("第%s次点击Turnstile复选框（%.0fx%.0f）" % (click_count, rect['w'], rect['h']))
            _click_turnstile(driver, rect)
            time.sleep(3)
            continue

        time.sleep(2)

    token_len = _turnstile_token_len(driver)
    if token_len > 20:
        logger.info("Turnstile验证已通过（token %d 字符）" % token_len)
        return True

    logger.warning("Turnstile验证未通过：%d 秒内未拿到响应token（点击%s次）。"
                   "若组件不可见，说明该浏览器被判定为机器人，换真实桌面浏览器再试"
                   % (timeout, click_count))
    return False


def _wait_cloudflare(driver, timeout=60, is_manual=False):
    """Cloudflare验证处理：支持传统interstitial和Turnstile两种模式"""
    logger.info("开始Cloudflare验证处理，超时: %s秒" % timeout)
    start = time.time()

    _turnstile_had_challenge = False
    if _has_turnstile(driver):
        _turnstile_had_challenge = True
        logger.info("检测到Turnstile验证组件")
        return _handle_turnstile(driver, timeout)

    turnstile_detected = False
    while time.time() - start < timeout:
        html = driver.get_page_source() or ''
        if _turnstile_token_len(driver) > 20:
            logger.info("Turnstile验证已通过，继续流程")
            return True
        if 'cf-turnstile' not in html and 'challenges.cloudflare.com' not in html:
            if _turnstile_had_challenge:
                logger.info("Turnstile组件已消失，验证通过")
                return True
        if '安全验证' in html or '正在验证' in html or 'Performing security verification' in html:
            logger.info("检测到传统Cloudflare挑战页面，等待自动通过...")
            time.sleep(3)
        elif _has_turnstile(driver):
            _turnstile_had_challenge = True
            turnstile_detected = True
            break
        else:
            logger.info("未检测到Cloudflare验证，直接通过")
            return True

    if turnstile_detected:
        remaining = max(5, timeout - int(time.time() - start))
        logger.info("等待过程中检测到Turnstile，剩余超时: %s秒" % remaining)
        return _handle_turnstile(driver, remaining)

    html = driver.get_page_source() or ''
    if '安全验证' in html or '正在验证' in html or 'Performing security verification' in html:
        logger.warning("传统Cloudflare挑战等待超时")
        return False
    logger.info("Cloudflare验证处理完成")
    return True


# ---------------- 执行器 ----------------
@register_executor
class BrowserExecutor(BaseExecutor):
    mode = 'browser'
    label = '浏览器模式'
    description = '模拟真人打开网页完成登录与签到，适合有按钮点击、验证码、Cloudflare 的站点'
    min_cf_timeout = 0        # 桌面(CDP)模式需要更长的挑战等待，由子类抬高下限

    def __init__(self, ctx):
        BaseExecutor.__init__(self, ctx)
        self.driver = None

    def _make_driver(self, headless):
        """创建浏览器驱动；桌面(CDP)模式覆写本方法改为复用已有浏览器"""
        return DrissionPageDriver(headless=headless)

    # 供外部（如API模式刷新Cookie）复用的站点字典
    def _site_for_login(self):
        site = dict(self.ctx.site)
        site['_plain_password'] = self.ctx.password
        return site

    def run(self):
        ctx = self.ctx
        site = self._site_for_login()
        sign_url = site.get('sign_url', '')
        login_url = site.get('login_url', '')
        has_cloudflare = bool(site.get('has_cloudflare', 0))
        login_first = bool(site.get('login_first', 0))
        cookies_list = ctx.cookies

        headless = get_config('headless') == '1'
        cf_timeout = int(get_config('cf_timeout') or 60) if has_cloudflare else 0
        cf_timeout = max(cf_timeout, self.min_cf_timeout)

        try:
            self.driver = self._make_driver(headless)

            if login_first and login_url:
                # ===== 登录优先模式 =====
                self.driver.open(login_url)
                self.driver.wait_for_load(5)
                if cookies_list:
                    self.driver.set_cookies(cookies_list)
                    self.driver.open(login_url)
                    self.driver.wait_for_load(10)
                else:
                    self.driver.wait_for_load(10)
                time.sleep(4)
                self._scroll_page()

                if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout, ctx.is_manual):
                    return SignResult(False, 'Cloudflare验证未通过（Turnstile无token，建议改用桌面模式）', ctx.cookies)

                if is_logged_in(self.driver):
                    ctx.log('登录优先模式：已登录，更新Cookie并前往签到页')
                    ctx.set_cookies(self.driver.get_cookies())
                    cookies_list = ctx.cookies
                else:
                    ctx.log('登录优先模式：未登录，执行登录流程')
                    if not perform_login(self.driver, site, ctx.ocr_config, cf_timeout):
                        return SignResult(False, '登录失败', ctx.cookies)
                    time.sleep(1)
                    ctx.set_cookies(self.driver.get_cookies())
                    cookies_list = ctx.cookies

                self.driver.open(sign_url)
                self.driver.wait_for_load(10)
                time.sleep(2)
                if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout, ctx.is_manual):
                    return SignResult(False, 'Cloudflare验证未通过（签到页，建议改用桌面模式）', ctx.cookies)
            else:
                # ===== 正常模式 =====
                self.driver.open(sign_url)
                self.driver.wait_for_load(5)
                if cookies_list:
                    self.driver.set_cookies(cookies_list)
                    self.driver.open(sign_url)
                    self.driver.wait_for_load(10)
                else:
                    self.driver.wait_for_load(10)
                time.sleep(4)
                self._scroll_page()

                if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout, ctx.is_manual):
                    return SignResult(False, 'Cloudflare验证未通过（Turnstile无token，建议改用桌面模式）', ctx.cookies)

                if is_login_page(self.driver) or (login_url and login_url in (self.driver.get_current_url() or '')):
                    if not perform_login(self.driver, site, ctx.ocr_config, cf_timeout):
                        return SignResult(False, '登录失败', ctx.cookies)
                    time.sleep(1)
                    ctx.set_cookies(self.driver.get_cookies())
                    cookies_list = ctx.cookies
                    self.driver.open(sign_url)
                    self.driver.wait_for_load(10)
                    time.sleep(2)
                    if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout, ctx.is_manual):
                        return SignResult(False, 'Cloudflare验证未通过（签到页，建议改用桌面模式）', ctx.cookies)

            keywords = ctx.success_keywords
            html = self.driver.get_page_source() or ''
            if any(kw in html for kw in keywords):
                ctx.log('签到成功（检测到标识）')
                return SignResult(True, '签到成功', ctx.cookies)

            _accept_dialog(self.driver)
            if click_sign_button(self.driver, site):
                # 有的站点点完弹 confirm，弹窗不关页面不会跳转
                for _ in range(8):
                    time.sleep(1)
                    _accept_dialog(self.driver)
                    try:
                        html_after = self.driver.get_page_source() or ''
                    except Exception:
                        html_after = ''
                    if any(kw in html_after for kw in keywords):
                        ctx.log('签到成功（点击按钮后）')
                        return SignResult(True, '签到成功', ctx.cookies)
                text = _page_text(self.driver)
                ctx.log('点击后未检测到成功标识（页面文字：%s）' % (text or '空'))
                _debug_screenshot(self.driver, 'sign_no_success')
                return SignResult(False, '签到失败', ctx.cookies, text[:500])

            text = _page_text(self.driver)
            ctx.log('未找到签到按钮且无成功标识（页面文字：%s）' % (text or '空'))
            return SignResult(False, '签到失败', ctx.cookies, text[:500])
        finally:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
                self.driver = None

    def login_and_refresh_cookies(self):
        """
        仅执行浏览器登录并取回Cookie（不签到）。
        供 API 模式"先用浏览器刷新Cookie"使用，解决接口需要登录态的场景。
        """
        ctx = self.ctx
        site = self._site_for_login()
        login_url = site.get('login_url') or site.get('sign_url')
        if not login_url:
            return False, '未配置登录地址，无法刷新Cookie'

        has_cloudflare = bool(site.get('has_cloudflare', 0))
        cf_timeout = int(get_config('cf_timeout') or 60) if has_cloudflare else 0
        cf_timeout = max(cf_timeout, self.min_cf_timeout)
        headless = get_config('headless') == '1'

        try:
            self.driver = self._make_driver(headless)
            self.driver.open(login_url)
            self.driver.wait_for_load(5)
            if ctx.cookies:
                self.driver.set_cookies(ctx.cookies)
                self.driver.open(login_url)
                self.driver.wait_for_load(10)
            else:
                self.driver.wait_for_load(10)
            time.sleep(3)

            if has_cloudflare and not _wait_cloudflare(self.driver, cf_timeout, ctx.is_manual):
                return False, 'Cloudflare验证未通过，无法刷新Cookie'

            if is_logged_in(self.driver) and self._page_shows_logout(self.driver):
                ctx.set_cookies(self.driver.get_cookies())
                self._capture_browser_headers(ctx, self.driver)
                return True, '已登录，Cookie刷新成功'

            # Cookie无效（页面无「退出」标识，可能只是游客页面被关键词误判）
            if not ctx.username or not ctx.password:
                return False, ('Cookie不含有效登录态（页面上没有「退出」标识），且未配置账号密码。'
                               '请在登录状态重新复制完整Cookie（Discuz类站点必须包含 *_auth 字段）')

            if not perform_login(self.driver, site, ctx.ocr_config, cf_timeout):
                return False, '浏览器登录失败'

            time.sleep(1)
            ctx.set_cookies(self.driver.get_cookies())
            self._capture_browser_headers(ctx, self.driver)
            return True, '浏览器登录成功，Cookie已刷新'
        except Exception as e:
            return False, '刷新Cookie异常: %s' % e
        finally:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
                self.driver = None

    @staticmethod
    def _page_shows_logout(driver):
        """页面上出现「退出」类入口才认为是真实登录态（游客页面导航栏也含「首页」「我的」，
        仅靠 LOGIN_SUCCESS_KEYWORDS 会误判）"""
        html = driver.get_page_source() or ''
        return any(kw in html for kw in ('退出', '登出', 'logout', '安全退出', '欢迎您回来'))

    @staticmethod
    def _capture_browser_headers(ctx, driver):
        """
        登录成功后提取浏览器真实请求头（UA / Accept / Accept-Language / Referer），
        写入 ctx.browser_headers 并把 UA 同步到变量池 {{ua}}。
        浏览器登录+API签到 模式下，API 接口请求会统一使用这些头，
        避免 WAF 校验「Cookie 与请求头不匹配」导致签到失败。
        """
        headers = {}
        try:
            info = driver.run_js(r'''
            return (function(){
              return JSON.stringify({ ua: navigator.userAgent, lang: navigator.language });
            })();
            ''')
            data = json.loads(info) if info else {}
            ua = (data.get('ua') or getattr(driver.page, 'user_agent', '') or '').strip()
            if ua:
                headers['User-Agent'] = ua
                ctx.vars['ua'] = ua
            lang = (data.get('lang') or '').strip()
            if lang:
                headers['Accept-Language'] = lang if ',' in lang else (lang + ',*;q=0.8')
            url = driver.get_current_url() or ''
            if url:
                headers['Referer'] = url
            headers['Accept'] = ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                                 'image/avif,image/webp,image/apng,*/*;q=0.8')
            if headers:
                ctx.browser_headers = headers
                ctx.log('记录浏览器请求头供API签到统一使用: %s'
                        % ', '.join('%s=%s' % (k, str(v)[:40]) for k, v in headers.items()))
        except Exception as e:
            logger.debug('提取浏览器请求头失败: %s' % e)

    def _scroll_page(self):
        """页面上下滚动，触发懒加载元素"""
        try:
            self.driver.run_js("window.scrollTo(0,0);")
            time.sleep(1)
            self.driver.run_js("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)
        except Exception as e:
            logger.debug('页面滚动失败: %s' % e)


class DesktopBrowserUnavailable(Exception):
    """桌面浏览器(CDP)不可用，直接告警而不走重试"""


@register_executor
class DesktopBrowserExecutor(BrowserExecutor):
    mode = 'desktop'
    label = '桌面浏览器模式(CDP)'
    description = '复用已有桌面 Chromium/Chrome（真实 GPU 指纹与登录态）经 CDP 驱动，专解 Cloudflare 托管挑战站点'
    min_cf_timeout = 60

    def __init__(self, ctx):
        BrowserExecutor.__init__(self, ctx)
        self.browser_address = None
        self.site_domain = ''

    def _site_for_login(self):
        site = BrowserExecutor._site_for_login(self)
        # 该模式就是给 CF 站点准备的：站点未勾选也走一遍挑战等待（无挑战时约 1 秒直接通过）
        site['has_cloudflare'] = 1
        return site

    def _make_driver(self, headless):
        addr, note = resolve_desktop_address(
            get_config('desktop_browser_address') or '',
            get_config('desktop_browser_path') or '',
        )
        if not addr:
            raise DesktopBrowserUnavailable(note)
        self.browser_address = addr
        self.site_domain = urlparse(
            self.ctx.site.get('login_url') or self.ctx.site.get('sign_url') or '').netloc
        self.ctx.log('桌面浏览器(CDP)：%s（%s）' % (addr, note))
        last_err = None
        for _ in range(3):
            try:
                driver = get_remote_driver(addr)
                if probe_alive(driver):
                    driver.site_domain = self.site_domain
                    return driver
                last_err = '调试连接不可用'
            except Exception as e:
                last_err = e
            # 连接已坏：丢掉共享实例并清 DrissionPage 缓存后重连
            _REMOTE_DRIVERS.pop(addr, None)
            forget_page_cache(addr)
            time.sleep(2)
        raise DesktopBrowserUnavailable('连接桌面浏览器失败: %s' % last_err)

    def run(self):
        try:
            return BrowserExecutor.run(self)
        except DesktopBrowserUnavailable as e:
            self.ctx.log('桌面浏览器不可用：%s' % e, 'error')
            return SignResult(False, '桌面浏览器不可用')
