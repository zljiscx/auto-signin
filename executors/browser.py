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
from abc import ABC, abstractmethod
from urllib.parse import urlparse

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
    def __init__(self, headless=False):
        # 每个实例独立临时目录，用完即删
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
        domain = urlparse(self.page.url).netloc
        success_count = 0
        for c in cookies_list:
            clean = {k: v for k, v in c.items() if k in allowed_keys}
            if 'name' not in clean or 'value' not in clean:
                continue
            clean.setdefault('domain', domain)
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
        if driver.page.ele('input[type="password"]', timeout=0.5):
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

    # 3. 终极JS兜底：滚动到底、遍历x-button、滚动到元素、模拟全套鼠标事件
    js_code = '''
    async function clickSignBtn(){
        window.scrollTo(0, document.body.scrollHeight);
        await new Promise(r=>setTimeout(r,800));
        let allElements = Array.from(document.querySelectorAll('.x-button'));
        const frames = document.querySelectorAll('iframe');
        for(let frame of frames){
            try{
                const frameDoc = frame.contentDocument || frame.contentWindow.document;
                const frameBtns = frameDoc.querySelectorAll('.x-button');
                allElements = allElements.concat(Array.from(frameBtns));
            }catch(e){}
        }
        // 优先找 立即签到，排除签到记录
        let target = null;
        for(let el of allElements){
            const txt = el.textContent.trim();
            if(txt === "立即签到"){
                target = el;
                break;
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
def _has_turnstile(driver):
    """检测页面是否存在Cloudflare Turnstile验证组件"""
    try:
        ele = driver.page.ele('input[name="cf-turnstile-response"]', timeout=1)
        if ele:
            return True
    except Exception:
        pass
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


def _handle_turnstile(driver, timeout=30):
    """处理Cloudflare Turnstile验证：只在checkbox可见时点击，其它状态等待"""
    if not _has_turnstile(driver):
        return True

    logger.info("检测到Cloudflare Turnstile验证组件")

    try:
        if os.path.isdir(DEBUG_SCREENSHOT_DIR):
            for fname in os.listdir(DEBUG_SCREENSHOT_DIR):
                if fname.startswith('cf_'):
                    try:
                        os.remove(os.path.join(DEBUG_SCREENSHOT_DIR, fname))
                    except Exception:
                        pass
    except Exception as e:
        logger.debug("清理旧截图失败: %s" % e)

    start = time.time()
    click_count = 0

    while time.time() - start < timeout:
        try:
            resp_input = driver.page.ele('input[name="cf-turnstile-response"]', timeout=0.5)
            if resp_input and resp_input.attr('value'):
                logger.info("Turnstile验证已通过")
                return True
        except Exception:
            pass

        html = driver.get_page_source() or ''
        if 'cf-turnstile' not in html and 'challenges.cloudflare.com' not in html:
            logger.info("Turnstile组件已消失，验证通过")
            return True

        turnstile_state = _check_turnstile_state_cdp(driver)

        if turnstile_state != 'need_click':
            time.sleep(2)
            continue

        click_count += 1
        logger.info("第%s次CF验证点击" % click_count)
        _click_turnstile_checkbox(driver)
        _debug_screenshot(driver, 'cf_click_%s' % click_count)

        time.sleep(2)

    try:
        resp_input = driver.page.ele('input[name="cf-turnstile-response"]', timeout=1)
        if resp_input and resp_input.attr('value'):
            return True
    except Exception:
        pass

    html = driver.get_page_source() or ''
    if 'Verify you are human' not in html and '请验证您是真人' not in html and 'Just a moment' not in html:
        return True

    logger.warning("Turnstile验证超时（共点击%s次）" % click_count)
    return False


def _check_turnstile_state_cdp(driver):
    """用CDP穿透shadow root检测Turnstile checkbox状态"""
    try:
        result = driver.page.run_cdp('DOM.getDocument', depth=-1, pierce=True)
        root_node = result.get('root', {})
        return _search_turnstile_state_in_cdp_tree(root_node)
    except Exception as e:
        logger.debug("CDP状态检测失败: %s" % e)
        return 'unknown'


def _search_turnstile_state_in_cdp_tree(node):
    """递归遍历CDP DOM树，查找Turnstile状态标识文本"""
    if not isinstance(node, dict):
        return 'unknown'

    node_value = node.get('nodeValue')
    if node_value:
        if 'Verify you are human' in node_value or '请验证您是真人' in node_value:
            return 'need_click'
        if 'Verifying' in node_value or '正在验证' in node_value:
            return 'verifying'

    children = node.get('children', [])
    if children:
        for child in children:
            state = _search_turnstile_state_in_cdp_tree(child)
            if state != 'unknown':
                return state

    content_doc = node.get('contentDocument')
    if content_doc:
        state = _search_turnstile_state_in_cdp_tree(content_doc)
        if state != 'unknown':
            return state

    shadow_roots = node.get('shadowRoots', [])
    if shadow_roots:
        for sr in shadow_roots:
            state = _search_turnstile_state_in_cdp_tree(sr)
            if state != 'unknown':
                return state

    return 'unknown'


def _click_turnstile_checkbox(driver):
    """点击Turnstile checkbox：通过CDP穿透shadow DOM查找iframe并获取位置点击"""
    try:
        result = driver.page.run_cdp('DOM.getDocument', depth=-1, pierce=True)
        root_node = result.get('root', {})
        iframe_pos = _find_turnstile_in_cdp_tree(driver, root_node)
        if iframe_pos:
            click_x = iframe_pos['x'] + 35
            click_y = iframe_pos['y'] + 30
            driver.page.run_cdp('Input.dispatchMouseEvent',
                                type='mousePressed', x=click_x, y=click_y,
                                button='left', clickCount=1)
            driver.page.run_cdp('Input.dispatchMouseEvent',
                                type='mouseReleased', x=click_x, y=click_y,
                                button='left', clickCount=1)
            logger.info("Turnstile: CDP定位坐标点击(%s, %s)" % (click_x, click_y))
            return True
    except Exception as e:
        logger.debug("Turnstile CDP策略失败: %s" % e)

    logger.warning("Turnstile: CDP未定位到iframe")
    return False


def _find_turnstile_in_cdp_tree(driver, node, depth=0):
    """递归遍历CDP DOM树，查找Turnstile iframe并返回其位置"""
    if depth > 50:
        return None
    attrs = node.get('attributes', [])
    node_name = (node.get('nodeName') or '').lower()
    if node_name == 'iframe':
        for i, attr in enumerate(attrs):
            if attr == 'id' and i + 1 < len(attrs) and 'cf-chl-widget' in attrs[i + 1]:
                return _get_cdp_box_model(driver, node.get('backendNodeId'))
            if attr == 'src' and i + 1 < len(attrs) and 'challenges.cloudflare.com' in attrs[i + 1]:
                return _get_cdp_box_model(driver, node.get('backendNodeId'))
    for child in node.get('children', []):
        result = _find_turnstile_in_cdp_tree(driver, child, depth + 1)
        if result:
            return result
    content = node.get('contentDocument')
    if content:
        result = _find_turnstile_in_cdp_tree(driver, content, depth + 1)
        if result:
            return result
    for sr in node.get('shadowRoots', []):
        result = _find_turnstile_in_cdp_tree(driver, sr, depth + 1)
        if result:
            return result
    return None


def _get_cdp_box_model(driver, backend_node_id):
    """通过CDP获取元素的位置信息"""
    try:
        result = driver.page.run_cdp('DOM.getBoxModel', backendNodeId=backend_node_id)
        content = result.get('model', {}).get('content', [])
        if len(content) >= 4:
            return {'x': content[0], 'y': content[1]}
    except Exception:
        pass
    return None


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
        try:
            resp = driver.page.ele('input[name="cf-turnstile-response"]', timeout=0.5)
            if resp and resp.attr('value'):
                logger.info("Turnstile验证已通过，继续流程")
                return True
        except Exception:
            pass
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

    def __init__(self, ctx):
        BaseExecutor.__init__(self, ctx)
        self.driver = None

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

        try:
            self.driver = DrissionPageDriver(headless=headless)

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
                    return SignResult(False, 'Cloudflare验证超时', ctx.cookies)

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
                    return SignResult(False, 'Cloudflare验证超时（签到页）', ctx.cookies)
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
                    return SignResult(False, 'Cloudflare验证超时', ctx.cookies)

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
                        return SignResult(False, 'Cloudflare验证超时（签到页）', ctx.cookies)

            keywords = ctx.success_keywords
            html = self.driver.get_page_source() or ''
            if any(kw in html for kw in keywords):
                ctx.log('签到成功（检测到标识）')
                return SignResult(True, '签到成功', ctx.cookies)

            if click_sign_button(self.driver, site):
                time.sleep(3)
                html_after = self.driver.get_page_source() or ''
                if any(kw in html_after for kw in keywords):
                    ctx.log('签到成功（点击按钮后）')
                    return SignResult(True, '签到成功', ctx.cookies)
                ctx.log('点击后未检测到成功标识（页面片段：%s）' % ((html_after or '')[:500]))
                return SignResult(False, '签到失败', ctx.cookies, html_after[:500])

            ctx.log('未找到签到按钮且无成功标识')
            return SignResult(False, '签到失败', ctx.cookies)
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
        headless = get_config('headless') == '1'

        try:
            self.driver = DrissionPageDriver(headless=headless)
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
                return False, 'Cloudflare验证超时，无法刷新Cookie'

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
