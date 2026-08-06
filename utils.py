import json
import re
import base64
import os
import requests
from cryptography.fernet import Fernet
import logging

# ---------- 常量 ----------
SIGN_KEYWORDS = ['签到成功', '簽到成功', '已经签到', '已經簽到']
DEFAULT_USERNAME_SELECTORS = ['input[name="username"]', 'input[name="user"]', 'input[name="email"]']
DEFAULT_PASSWORD_SELECTORS = ['input[name="password"]', 'input[name="pass"]', 'input.password']
DEFAULT_CAPTCHA_IMG_SELECTORS = ['img[alt="CAPTCHA"]', 'img[src*="captcha"]', 'img[src*="code"]']
DEFAULT_CAPTCHA_INPUT_SELECTORS = ['input[name="imagestring"]', 'input[name="captcha"]', 'input[name="code"]']
DEFAULT_SUBMIT_SELECTORS = ['button[type="submit"]', 'input[type="submit"]', '#submit-btn']
DEFAULT_SIGN_BUTTON_SELECTORS = [
    'button:has-text("签到")',
    'button:has-text("簽到")',
    'input[value="签到"]',
    'input[value="簽到"]',
    'a:has-text("签到")',
    'a:has-text("簽到")',
    'button[type="submit"]'
]

# ---------- JavaScript 模板常量 ----------
JS_FILL_TEMPLATE = """
function fillElement(selector, value) {
    var el = null;
    if (selector.startsWith('//')) {
        var result = document.evaluate(selector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        el = result.singleNodeValue;
    } else {
        el = document.querySelector(selector);
    }
    if (el) {
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    return el !== null;
}
fillElement('%s', '%s');
fillElement('%s', '%s');
"""

JS_GET_SRC_TEMPLATE = """
function getElementSrc(selector) {
    var el = null;
    if (selector.startsWith('//')) {
        var result = document.evaluate(selector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        el = result.singleNodeValue;
    } else {
        el = document.querySelector(selector);
    }
    return el ? el.src : null;
}
return getElementSrc('%s');
"""

JS_FILL_CAPTCHA_TEMPLATE = """
function fillCaptcha(selector, value) {
    var el = null;
    if (selector.startsWith('//')) {
        var result = document.evaluate(selector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        el = result.singleNodeValue;
    } else {
        el = document.querySelector(selector);
    }
    if (el) {
        el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }
}
fillCaptcha('%s', '%s');
"""

JS_CLICK_TEMPLATE = """
function clickElement(selector) {
    var el = null;
    if (selector.startsWith('//')) {
        var result = document.evaluate(selector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        el = result.singleNodeValue;
    } else {
        el = document.querySelector(selector);
    }
    if (el) {
        el.click();
        return true;
    }
    return false;
}
return clickElement('%s');
"""

# ---------- 加密 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
KEY_FILE = os.path.join(DATA_DIR, 'encryption.key')
os.makedirs(DATA_DIR, exist_ok=True)

def _get_encryption_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
        return key

def encrypt_data(data):
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode()
    f = Fernet(_get_encryption_key())
    return f.encrypt(data).decode()

def decrypt_data(encrypted):
    if encrypted is None:
        return None
    f = Fernet(_get_encryption_key())
    return f.decrypt(encrypted.encode()).decode()

# ---------- Cookies 解析 ----------
def parse_cookies_input(raw_text):
    """将用户输入的多种格式 cookies 解析为 JSON 数组字符串"""
    if not raw_text or not raw_text.strip():
        return None
    raw = raw_text.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if 'name' not in item or 'value' not in item:
                    raise ValueError("JSON数组中每个对象必须包含name和value字段")
            return json.dumps(data, ensure_ascii=False)
        elif isinstance(data, dict):
            new_list = [{"name": k, "value": v} for k, v in data.items()]
            return json.dumps(new_list, ensure_ascii=False)
        else:
            raise ValueError("JSON格式必须是对象或数组")
    except json.JSONDecodeError:
        lines = re.split(r'[;\n\r,]+', raw)
        cookies = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            sep = '=' if '=' in line else ':'
            parts = line.split(sep, 1)
            if len(parts) == 2:
                name = parts[0].strip()
                value = parts[1].strip()
                if name and value:
                    cookies.append({"name": name, "value": value})
        if not cookies:
            raise ValueError("无法解析cookies，请检查格式")
        return json.dumps(cookies, ensure_ascii=False)

# ---------- Cookie 标准化 ----------
def normalize_cookies(cookies_list, default_domain=None):
    """
    将可能只有 name/value 的 Cookie 列表补全为标准字段
    :param cookies_list: list of dict, 每个 dict 至少包含 name 和 value
    :param default_domain: 默认域名（用于补全缺失的 domain）
    :return: 补全后的列表
    """
    if not cookies_list:
        return []
    normalized = []
    for c in cookies_list:
        if 'name' not in c or 'value' not in c:
            continue
        cookie = {
            'name': c['name'],
            'value': c['value'],
            'domain': c.get('domain', default_domain or ''),
            'path': c.get('path', '/'),
            'httpOnly': c.get('httpOnly', False),
            'secure': c.get('secure', False),
            'expires': c.get('expires', -1)
        }
        normalized.append(cookie)
    return normalized

# ---------- 百度 OCR ----------
def get_access_token(api_key, secret_key):
    url = "https://aip.baidubce.com/oauth/2.0/token"
    params = {"grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key}
    resp = requests.post(url, params=params)
    return resp.json().get("access_token")

def ocr_captcha(img_bytes, api_key, secret_key):
    """识别验证码图片（二进制数据），返回识别出的文本"""
    token = get_access_token(api_key, secret_key)
    url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic?access_token={token}"
    img_base64 = base64.b64encode(img_bytes).decode()
    payload = {'image': img_base64}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    resp = requests.post(url, headers=headers, data=payload)
    result = resp.json()
    if 'words_result' in result and result['words_result']:
        return result['words_result'][0]['words']
    return None

# ---------- 企业微信消息推送 ----------
def send_wecom_text_message(webhook_key, content):
    """
    发送企业微信 text 类型消息
    :param webhook_key: Webhook Key（明文）
    :param content: 消息内容（纯文本）
    :return: (success, message)
    """
    if not webhook_key or not webhook_key.strip():
        return False, "Webhook Key 未配置"

    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={webhook_key.strip()}"
    payload = {
        "msgtype": "text",
        "text": {
            "content": content
        }
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        if result.get('errcode') == 0:
            logging.info("企业微信消息推送成功")
            return True, "推送成功"
        else:
            error_msg = result.get('errmsg', '未知错误')
            logging.error(f"企业微信消息推送失败: {error_msg}")
            return False, f"推送失败: {error_msg}"
    except requests.exceptions.Timeout:
        return False, "推送超时"
    except Exception as e:
        logging.error(f"企业微信消息推送异常: {e}")
        return False, f"推送异常: {str(e)}"