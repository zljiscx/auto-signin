import os
import json
import time
import logging
from abc import ABC, abstractmethod
from urllib.parse import urlparse
from DrissionPage import ChromiumPage, ChromiumOptions
from models import update_site_cookies, update_site_sign_result
from utils import (
    ocr_captcha, decrypt_data, encrypt_data, get_user_data_dir,
    JS_FILL_TEMPLATE, JS_GET_SRC_TEMPLATE, JS_FILL_CAPTCHA_TEMPLATE, JS_CLICK_TEMPLATE,
    DEFAULT_USERNAME_SELECTORS, DEFAULT_PASSWORD_SELECTORS,
    DEFAULT_CAPTCHA_IMG_SELECTORS, DEFAULT_CAPTCHA_INPUT_SELECTORS,
    DEFAULT_SUBMIT_SELECTORS, DEFAULT_SIGN_BUTTON_SELECTORS
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')


def is_docker():
    """检测是否在 Docker 容器内运行"""
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/self/cgroup', 'r') as f:
            return 'docker' in f.read()
    except:
        return False


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
    def close(self): pass


class DrissionPageDriver(BrowserDriver):
    def __init__(self, headless=False):
        co = ChromiumOptions()
        if headless:
            co.headless()

        # 固定窗口大小
        co.set_argument('--window-size=1200,800')

        user_data_dir = get_user_data_dir()
        os.makedirs(user_data_dir, exist_ok=True)
        co.set_argument(f'--user-data-dir={user_data_dir}')

        # 反检测参数（降低被识别为自动化的风险）
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument(
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

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
                    'name': name,
                    'value': value,
                    'domain': domain,
                    'path': '/',
                    'httpOnly': False,
                    'secure': False,
                    'expires': -1
                })
        elif isinstance(raw, list):
            for c in raw:
                clean = {
                    'name': c.get('name'),
                    'value': c.get('value'),
                    'domain': c.get('domain', domain),
                    'path': c.get('path', '/'),
                    'httpOnly': c.get('httpOnly', False),
                    'secure': c.get('secure', False),
                    'expires': c.get('expires', -1)
                }
                clean = {k: v for k, v in clean.items() if v is not None}
                cookies_list.append(clean)
        # 过滤，只保留标准字段
        allowed = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}
        return [{k: v for k, v in c.items() if k in allowed} for c in cookies_list if 'name' in c and 'value' in c]

    def set_cookies(self, cookies_list):
        allowed_keys = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}
        domain = urlparse(self.page.url).netloc
        for c in cookies_list:
            clean = {k: v for k, v in c.items() if k in allowed_keys}
            if 'name' not in clean or 'value' not in clean:
                logging.warning("Cookie 缺少 name 或 value，跳过: %s", c)
                continue
            clean.setdefault('domain', domain)
            clean.setdefault('path', '/')
            if 'expires' in clean and (clean['expires'] is None or clean['expires'] == -1):
                del clean['expires']
            logging.info(f"设置 Cookie: {clean.get('name')}")
            try:
                self.page.set.cookies(clean)
            except Exception as e:
                logging.error(f"设置 Cookie 失败: {e}")
                raise

    def get_page_source(self):
        return self.page.html

    def get_current_url(self):
        return self.page.url

    def fill_input(self, selector, value):
        for _ in range(5):
            ele = self.page.ele(selector, timeout=0.5)
            if ele:
                ele.input(value)
                logging.info(f"填充元素 {selector} 成功")
                return
            time.sleep(1)
        logging.warning(f"未找到元素 {selector}，无法填充")

    def click(self, selector):
        for _ in range(5):
            ele = self.page.ele(selector, timeout=0.5)
            if ele:
                ele.click()
                logging.info(f"点击元素 {selector} 成功")
                return
            time.sleep(1)
        logging.warning(f"未找到元素 {selector}，无法点击")

    def screenshot_captcha(self, selector):
        for _ in range(5):
            ele = self.page.ele(selector, timeout=0.5)
            if ele:
                return ele.screenshot()
            time.sleep(1)
        logging.warning(f"未找到验证码图片 {selector}")
        return None

    def wait_for_load(self, timeout=10):
        try:
            self.page.wait.page_load(timeout)
        except AttributeError:
            try:
                self.page.wait.load_complete(timeout)
            except AttributeError:
                time.sleep(min(timeout, 5))

    def close(self):
        try:
            self.page.close()
        except Exception as e:
            logging.debug(f"关闭浏览器时发生异常（已忽略）: {e}")


def is_login_page(driver):
    """检测当前页面是否为登录页"""
    try:
        url = driver.get_current_url().lower()
        if any(key in url for key in ['login', 'signin', 'log-in', 'sign-in']):
            return True
    except:
        pass
    try:
        page_source = driver.get_page_source()
        if 'input' in page_source and ('password' in page_source or 'username' in page_source):
            # 尝试快速定位密码框
            if driver.page.ele('input[type="password"]', timeout=0.5):
                return True
    except:
        pass
    return False


def perform_login(driver, site, ocr_config):
    logging.info("开始执行登录流程（JavaScript）")
    login_url = site['login_url']
    username = site.get('username', '')
    password_encrypted = site.get('password', '')
    password_decrypted = decrypt_data(password_encrypted) if password_encrypted else ''
    has_captcha = site.get('has_captcha', 0)

    username_sel = site.get('username_selector') or ','.join(DEFAULT_USERNAME_SELECTORS)
    password_sel = site.get('password_selector') or ','.join(DEFAULT_PASSWORD_SELECTORS)
    captcha_img_sel = site.get('captcha_img_selector') or ','.join(DEFAULT_CAPTCHA_IMG_SELECTORS)
    captcha_input_sel = site.get('captcha_input_selector') or ','.join(DEFAULT_CAPTCHA_INPUT_SELECTORS)
    submit_sel = site.get('submit_selector') or ','.join(DEFAULT_SUBMIT_SELECTORS)

    driver.open(login_url)
    driver.wait_for_load(10)
    time.sleep(3)

    js_fill = JS_FILL_TEMPLATE % (
        username_sel.replace("'", "\\'"),
        username.replace("'", "\\'"),
        password_sel.replace("'", "\\'"),
        password_decrypted.replace("'", "\\'")
    )
    driver.page.run_js(js_fill)
    logging.info("已填充用户名和密码（JS）")

    if has_captcha:
        js_get_src = JS_GET_SRC_TEMPLATE % (captcha_img_sel.replace("'", "\\'"))
        img_src = driver.page.run_js(js_get_src)
        if img_src:
            import requests
            try:
                cookies = driver.get_cookies()
                session = requests.Session()
                for c in cookies:
                    session.cookies.set(c['name'], c['value'])
                img_data = session.get(img_src, timeout=10).content
                ocr_text = ocr_captcha(img_data, ocr_config['api_key'], ocr_config['secret_key'])
                if ocr_text:
                    js_fill_captcha = JS_FILL_CAPTCHA_TEMPLATE % (
                        captcha_input_sel.replace("'", "\\'"),
                        ocr_text.replace("'", "\\'")
                    )
                    driver.page.run_js(js_fill_captcha)
                    logging.info(f"验证码识别结果: {ocr_text}")
                else:
                    logging.warning("验证码识别失败")
            except Exception as e:
                logging.warning(f"验证码处理异常: {e}")
        else:
            logging.warning("未获取到验证码图片")

    js_click = JS_CLICK_TEMPLATE % (submit_sel.replace("'", "\\'"))
    clicked = driver.page.run_js(js_click)
    logging.info(f"登录按钮点击结果: {clicked}")

    max_wait = 15
    start_time = time.time()
    while time.time() - start_time < max_wait:
        if not is_login_page(driver):
            logging.info("登录成功")
            time.sleep(2)
            return True
        time.sleep(1)

    logging.warning("登录超时")
    with open('login_failed.html', 'w', encoding='utf-8') as f:
        f.write(driver.get_page_source())
    return False


def click_sign_button(driver, site):
    sign_btn_sel = site.get('sign_button_selector', '')
    if not sign_btn_sel:
        for sel in DEFAULT_SIGN_BUTTON_SELECTORS:
            try:
                driver.click(sel)
                logging.info("点击签到按钮成功（默认选择器）")
                return True
            except:
                continue
        logging.warning("未找到签到按钮")
        return False
    else:
        try:
            driver.click(sign_btn_sel)
            logging.info("点击签到按钮成功（自定义选择器）")
            return True
        except Exception as e:
            logging.warning(f"点击签到按钮失败: {e}")
            return False


def sign_site(site, ocr_config, retry_times=3):
    sid = site['id']
    name = site['name']
    sign_url = site['sign_url']
    login_url = site.get('login_url', '')
    has_captcha = bool(site.get('has_captcha', 0))

    # 解密 cookies
    cookies_json_enc = site.get('cookies')
    cookies_list = []
    if cookies_json_enc:
        try:
            decrypted = decrypt_data(cookies_json_enc)
            if decrypted:
                raw_list = json.loads(decrypted)
                if isinstance(raw_list, list):
                    allowed = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}
                    cookies_list = [{k: v for k, v in c.items() if k in allowed} for c in raw_list if
                                    'name' in c and 'value' in c]
        except Exception as e:
            logging.warning(f"解析 cookies 失败: {e}")

    # 从全局配置读取 headless
    from models import get_config
    headless = get_config('headless') == '1'
    driver = DrissionPageDriver(headless=headless)

    attempt = 0
    success = False
    msg = ''

    try:
        while attempt < retry_times:
            attempt += 1
            logging.info(f"签到尝试 {attempt}/{retry_times}")

            driver.open(sign_url)
            driver.wait_for_load(10)
            time.sleep(2)

            if cookies_list:
                driver.set_cookies(cookies_list)
                driver.open(sign_url)
                driver.wait_for_load(10)
                time.sleep(2)

            if is_login_page(driver) or (login_url and login_url in driver.get_current_url()):
                logging.info("检测到需要登录")
                login_ok = perform_login(driver, site, ocr_config)
                if not login_ok:
                    msg = f"登录失败 (尝试 {attempt})"
                    time.sleep(3)
                    continue

                # 登录成功，保存 cookies
                time.sleep(1)
                new_cookies = driver.get_cookies()
                allowed = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure'}
                filtered_new = [{k: v for k, v in c.items() if k in allowed} for c in new_cookies if
                                'name' in c and 'value' in c]
                cookies_json = json.dumps(filtered_new)
                # 加密保存
                from utils import encrypt_data
                encrypted_cookies = encrypt_data(cookies_json)
                update_site_cookies(sid, encrypted_cookies)
                cookies_list = filtered_new
                logging.info(f"已保存 {len(filtered_new)} 个 Cookie")

                # 重新访问签到页
                retry = 0
                while retry < 3:
                    try:
                        driver.open(sign_url)
                        driver.wait_for_load(10)
                        time.sleep(2)
                        break
                    except Exception as e:
                        retry += 1
                        logging.warning(f"访问签到页失败 (重试 {retry}/3): {e}")
                        time.sleep(2)
                else:
                    logging.error("访问签到页失败，跳过本次")
                    continue

            # 检测签到标识
            html = driver.get_page_source()
            keywords = ['签到成功', '簽到成功', '已经签到', '已經簽到']
            if any(kw in html for kw in keywords):
                success = True
                msg = "签到成功（检测到标识文字）"
                logging.info(msg)
                break

            # 尝试点击签到按钮
            logging.info("未检测到签到标识，尝试点击签到按钮")
            clicked = click_sign_button(driver, site)
            if clicked:
                time.sleep(3)
                html_after = driver.get_page_source()
                if any(kw in html_after for kw in keywords):
                    success = True
                    msg = "签到成功（点击按钮后）"
                    logging.info(msg)
                    break
                else:
                    msg = "点击按钮后未检测到成功标识"
                    logging.warning(msg)
            else:
                msg = "未找到签到按钮且无成功标识"
                logging.warning(msg)

            time.sleep(5)

    except Exception as e:
        msg = f"异常: {str(e)}"
        logging.error(msg)
        try:
            with open('sign_error.html', 'w', encoding='utf-8') as f:
                f.write(driver.get_page_source())
        except:
            pass
    finally:
        driver.close()
        update_site_sign_result(sid, success)

    return success, msg
