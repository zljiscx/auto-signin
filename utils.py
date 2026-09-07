import json
import re
import base64
import os
import time
import shlex
import requests
from cryptography.fernet import Fernet
import logging

# ---------- 常量 ----------
SIGN_KEYWORDS = ['签到成功', '簽到成功', '已经签到', '已經簽到', '今日已签', '今日已簽', '已签到', '已簽到']
LOGIN_SUCCESS_KEYWORDS = ['首页', '首頁', '退出', '我的']
DEFAULT_USERNAME_SELECTORS = ['input[name="username"]', 'input[name="user"]', 'input[name="email"]', '#username']
DEFAULT_PASSWORD_SELECTORS = ['input[name="password"]', 'input[name="pass"]', 'input.password', '#password']
DEFAULT_CAPTCHA_IMG_SELECTORS = ['img[alt="CAPTCHA"]', 'img[src*="captcha"]', 'img[src*="code"]', '#captcha_img']
DEFAULT_CAPTCHA_INPUT_SELECTORS = ['input[name="imagestring"]', 'input[name="captcha"]', 'input[name="code"]',
                                   '#captcha']
DEFAULT_SUBMIT_SELECTORS = ['button[type="submit"]', 'input[type="submit"]', '#submit-btn', '.login-btn']
DEFAULT_SIGN_BUTTON_SELECTORS = [
    '.x-button:contains("立即签到")',
    '.x-button.main.big.lock-text.all.radius.pointer:contains("立即签到")',
    '.x-button',
    'button:has-text("签到")',
    'button:has-text("簽到")',
    'input[value="签到"]',
    'input[value="簽到"]',
    'a:has-text("签到")',
    'a:has-text("簽到")',
    'button[type="submit"]',
    '#sign-btn'
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
    # 优先从环境变量读取密钥，支持容器化部署密钥隔离
    env_key = os.environ.get('ENCRYPTION_KEY')
    if env_key:
        try:
            Fernet(env_key.encode())
            return env_key.encode()
        except Exception:
            logging.warning("环境变量 ENCRYPTION_KEY 格式无效，将使用本地密钥文件")

    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
        logging.info("已生成新的加密密钥文件，请妥善备份 data/encryption.key")
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
    try:
        f = Fernet(_get_encryption_key())
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logging.error(f"数据解密失败: {e}")
        return None


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
    if not api_key or not secret_key:
        return None
    url = "https://aip.baidubce.com/oauth/2.0/token"
    params = {"grant_type": "client_credentials", "client_id": api_key, "client_secret": secret_key}
    try:
        resp = requests.post(url, params=params, timeout=10)
        return resp.json().get("access_token")
    except Exception as e:
        logging.error(f"获取OCR AccessToken失败: {e}")
        return None


def ocr_captcha(img_bytes, api_key, secret_key):
    if not img_bytes or not api_key or not secret_key:
        return None
    token = get_access_token(api_key, secret_key)
    if not token:
        return None
    url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic?access_token={token}"
    img_base64 = base64.b64encode(img_bytes).decode()
    payload = {'image': img_base64}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        resp = requests.post(url, headers=headers, data=payload, timeout=10)
        result = resp.json()
        if 'words_result' in result and result['words_result']:
            return result['words_result'][0]['words'].strip()
        return None
    except Exception as e:
        logging.error(f"验证码识别异常: {e}")
        return None


# ---------- 企业微信消息推送 ----------
def send_wecom_text_message(webhook_key, content):
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


# ---------- 环境检测 ----------
def is_docker():
    if os.environ.get('CONTAINER') == 'docker':
        return True
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'r') as f:
            if 'docker' in f.read() or 'kubepods' in f.read():
                return True
    except:
        pass
    try:
        with open('/proc/self/cgroup', 'r') as f:
            if 'docker' in f.read() or 'kubepods' in f.read():
                return True
    except:
        pass
    return False


# ---------- 加密密钥管理（备份还原用） ----------
def get_encryption_key_info():
    """
    获取当前加密密钥信息
    :return: dict: key=密钥明文, source=来源(env/file/none)
    """
    env_key = os.environ.get('ENCRYPTION_KEY')
    if env_key:
        try:
            Fernet(env_key.encode())
            return {'key': env_key, 'source': 'env'}
        except Exception:
            logging.warning("环境变量 ENCRYPTION_KEY 格式无效")
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, 'rb') as f:
            key = f.read().decode()
        return {'key': key, 'source': 'file'}
    return {'key': None, 'source': 'none'}


def set_encryption_key_file(key_str):
    """
    将密钥写入本地密钥文件（仅文件模式生效）
    :param key_str: Fernet 密钥明文
    :return: 是否写入成功
    """
    try:
        # 先校验密钥格式合法性
        Fernet(key_str.encode())
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(KEY_FILE, 'wb') as f:
            f.write(key_str.encode())
        return True
    except Exception as e:
        logging.error(f"写入密钥文件失败: {e}")
        return False


# ---------- cURL 解析（API模式：从浏览器复制的请求直接导入） ----------
def parse_curl(curl_text):
    """
    解析 cURL 命令为请求配置字典
    :return: {'method', 'url', 'headers', 'body', 'body_type'}
             body_type: none / json / form / raw
    """
    if not curl_text or not curl_text.strip():
        raise ValueError('cURL 内容为空')
    text = curl_text.strip()
    # 合并续行（bash 的 \\ 与 cmd 的 ^）
    text = text.replace('\\\r\n', ' ').replace('\\\n', ' ')
    text = text.replace('^\r\n', ' ').replace('^\n', ' ')
    text = text.replace('\r\n', '\n').replace('\n', ' ')
    # bash 单引号内的转义写法
    text = text.replace("'\\''", "'")

    try:
        tokens = shlex.split(text, posix=True)
    except Exception:
        tokens = text.split()

    if tokens and tokens[0].lower() in ('curl', 'curl.exe'):
        tokens = tokens[1:]

    method = None
    url = None
    headers = {}
    data_parts = []
    is_form = False

    skip_value_options = {'-o', '--output', '-u', '--user', '--connect-timeout', '-m', '--max-time',
                          '--retry', '--proxy', '-x', '--cacert', '--cert', '--key'}

    i = 0
    while i < len(tokens):
        t = tokens[i]
        low = t.lower()
        if low in ('-x', '--request') and i + 1 < len(tokens):
            method = tokens[i + 1].upper()
            i += 2
        elif low in ('-h', '--header') and i + 1 < len(tokens):
            raw = tokens[i + 1]
            if ':' in raw:
                k, v = raw.split(':', 1)
                headers[k.strip()] = v.strip()
            i += 2
        elif low in ('-d', '--data', '--data-raw', '--data-binary', '--data-ascii',
                     '--data-urlencode') and i + 1 < len(tokens):
            data_parts.append(tokens[i + 1])
            i += 2
        elif low in ('-f', '--form') and i + 1 < len(tokens):
            is_form = True
            data_parts.append(tokens[i + 1])
            i += 2
        elif low in ('-a', '--user-agent') and i + 1 < len(tokens):
            headers['User-Agent'] = tokens[i + 1]
            i += 2
        elif low in ('-b', '--cookie') and i + 1 < len(tokens):
            headers['Cookie'] = tokens[i + 1]
            i += 2
        elif low in ('-e', '--referer') and i + 1 < len(tokens):
            headers['Referer'] = tokens[i + 1]
            i += 2
        elif low == '--compressed':
            headers.setdefault('Accept-Encoding', 'gzip, deflate')
            i += 1
        elif low in skip_value_options:
            i += 2
        elif t.startswith('http://') or t.startswith('https://'):
            if url is None:
                url = t
            i += 1
        else:
            i += 1

    if url is None:
        raise ValueError('未识别到请求地址，请确认复制的是完整 cURL 命令')

    body = ''
    body_type = 'none'
    if data_parts:
        body = '&'.join(data_parts)
        if is_form:
            body_type = 'form'
        else:
            ctype = ''
            for k, v in headers.items():
                if k.lower() == 'content-type':
                    ctype = v.lower()
            if 'json' in ctype or body.strip().startswith('{') or body.strip().startswith('['):
                body_type = 'json'
            else:
                body_type = 'raw'

    if not method:
        method = 'POST' if body else 'GET'

    # Content-Length 由 requests 自行计算；accept-encoding 必须剔除——
    # 浏览器会带 br/zstd，requests 无法解压会导致响应乱码，交给 requests 自行协商
    headers = {k: v for k, v in headers.items()
               if k.lower() not in ('content-length', 'accept-encoding')}

    return {
        'method': method.upper(),
        'url': url,
        'headers': headers,
        'body': body,
        'body_type': body_type
    }


# ---------- API模式：变量渲染 ----------
VAR_PATTERN = re.compile(r'\{\{\s*([a-zA-Z_][\w:.\-]*)\s*\}\}')


def render_vars(text, ctx, extra=None):
    """
    把 {{变量}} 替换为实际值
    支持：cookie / cookie:名称 / username / password / date / datetime /
          timestamp / timestamp_ms / var:变量名 / sign_url / login_url
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return text
    if '{{' not in text:
        return text

    def _value(name):
        name = name.strip()
        if name == 'cookie':
            return ctx.cookie_string
        if name.startswith('cookie:'):
            return ctx.get_cookie_value(name.split(':', 1)[1])
        if name == 'username':
            return ctx.username
        if name == 'password':
            return ctx.password
        if name == 'date':
            return time.strftime('%Y-%m-%d')
        if name == 'datetime':
            return time.strftime('%Y-%m-%d %H:%M:%S')
        if name == 'timestamp':
            return str(int(time.time()))
        if name == 'timestamp_ms':
            return str(int(time.time() * 1000))
        if name.startswith('var:'):
            return str(ctx.vars.get(name.split(':', 1)[1], ''))
        if name == 'sign_url':
            return ctx.site.get('sign_url', '') or ''
        if name == 'login_url':
            return ctx.site.get('login_url', '') or ''
        if name in ctx.vars:
            return str(ctx.vars[name])
        if extra and name in extra:
            return str(extra[name])
        return ''

    return VAR_PATTERN.sub(lambda m: _value(m.group(1)), text)


# ---------- API模式：响应取值 ----------
def _json_get(data, path):
    """简单取值，支持 a.b.c / $.a.b.c / a[0].b"""
    if not path:
        return None
    path = path.strip()
    if path.startswith('$.'):
        path = path[2:]
    cur = data
    for part in path.split('.'):
        if not part:
            continue
        for name, idx in re.findall(r'([^\[\]]+)|\[(\d+)\]', part):
            if name:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(name)
            elif idx:
                if not isinstance(cur, list) or int(idx) >= len(cur):
                    return None
                cur = cur[int(idx)]
        if cur is None:
            return None
    return cur


FORMHASH_PATTERNS = [
    # Discuz 标准写法：<input type="hidden" name="formhash" value="xxxx" />
    r'name=["\']formhash["\']\s+value=["\'](\w+)["\']',
    # JS 变量 / 对象写法：formhash: 'xxxx' / var formhash = "xxxx"
    r'formhash["\']?\s*[:=]\s*["\'](\w+)["\']',
    # URL 参数写法：formhash=xxxx
    r'[?&]formhash=(\w+)',
]


def extract_formhash(response_text):
    """智能提取 Discuz 的 formhash，兼容多种页面写法"""
    text = response_text or ''
    for pattern in FORMHASH_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(1)
    return ''


def extract_value(response_text, headers=None, cookies=None, expr=''):
    """
    从响应中提取变量
    expr 前缀：json:$.a.b / regex:pattern / header:X-Token / cookie:uid /
              formhash（Discuz智能提取） / text
    """
    if not expr:
        return ''
    expr = expr.strip()
    try:
        if expr.startswith('formhash'):
            return extract_formhash(response_text)
        if expr.startswith('json:'):
            return str(_json_get(json.loads(response_text or '{}'), expr[5:]) or '')
        if expr.startswith('regex:'):
            m = re.search(expr[6:], response_text or '')
            if not m:
                return ''
            return m.group(1) if m.groups() else m.group(0)
        if expr.startswith('header:'):
            name = expr[7:].strip().lower()
            for k, v in (headers or {}).items():
                if k.lower() == name:
                    return v
            return ''
        if expr.startswith('cookie:'):
            return (cookies or {}).get(expr[7:].strip(), '')
        return response_text or ''
    except Exception as e:
        logging.warning(f"变量提取失败({expr}): {e}")
        return ''


# ---------- API模式：成功判定 ----------
def check_success_rule(rule, status_code, response_text):
    """
    按判定规则判断签到是否成功
    :return: (bool, 说明)
    """
    rule = rule or {}
    text = response_text or ''

    # 1. 状态码
    status = rule.get('status')
    if status:
        if isinstance(status, int):
            status = [status]
        try:
            status = [int(s) for s in status if str(s).strip()]
        except Exception:
            status = []
        if status and status_code not in status:
            return False, 'HTTP状态码 %s 不在期望范围 %s' % (status_code, status)

    # 2. 失败标识优先
    for bad in (rule.get('not_contains') or []):
        if bad and bad in text:
            return False, '响应包含失败标识「%s」' % bad

    # 3. JSON 判定
    if rule.get('json_path'):
        try:
            data = json.loads(text)
        except Exception:
            return False, '响应不是合法JSON，无法按 %s 判定' % rule['json_path']
        actual = _json_get(data, rule['json_path'])
        expect = rule.get('json_equals')
        if expect is None or str(expect).strip() == '':
            ok = bool(actual) and str(actual).lower() not in ('false', '0', 'none', 'null')
        else:
            ok = str(actual).lower() == str(expect).lower()
        return ((True, 'JSON判定通过: %s=%s' % (rule['json_path'], actual)) if ok else
                (False, 'JSON判定未通过: %s=%s（期望 %s）' % (rule['json_path'], actual, expect or '真值')))

    # 4. 关键词判定
    contains = [c for c in (rule.get('contains') or []) if c]
    if contains:
        for c in contains:
            if c in text:
                return True, '响应包含成功标识「%s」' % c
        return False, '响应未包含任何成功标识 %s' % contains

    # 5. 未配置关键词：状态码通过即视为成功
    return True, 'HTTP %s（未配置关键词判定，按状态码判定）' % status_code


# ---------- 签到接口候选筛选（嗅探与HAR导入共用） ----------
SIGN_HINT_WORDS = [
    'sign', 'checkin', 'check-in', 'attendance', 'bonus', 'punch', 'reward',
    'daily', 'task', 'qiandao', '签到', '打卡', '簽到'
]
EXCLUDE_EXT = ('.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
               '.woff', '.woff2', '.ttf', '.map', '.webp')
EXCLUDE_WORDS = ('google', 'analytics', 'doubleclick', 'googletag', 'ads', 'stat',
                 'baidu', 'sentry', 'cloudflare', 'recaptcha', 'turnstile')
KEEP_HEADERS = ('accept', 'accept-language', 'content-type',
                'origin', 'referer', 'x-requested-with', 'x-csrf-token', 'x-xsrf-token',
                'authorization', 'user-agent')
# 注意：accept-encoding 绝不能保留——浏览器会带 br/zstd（Brotli/Zstd压缩），
# requests 无法解压这两种格式，会导致响应变成乱码。让 requests 自行协商 gzip/deflate。
DROP_HEADERS = ('content-length', 'host', 'connection', 'cache-control',
                'if-none-match', 'if-modified-since')


def score_candidate(url):
    """给候选请求打分，分数越高越像签到接口"""
    low = (url or '').lower()
    score = 0
    for w in SIGN_HINT_WORDS:
        if w in low:
            score += 3
    for w in EXCLUDE_WORDS:
        if w in low:
            score -= 5
    if low.endswith(EXCLUDE_EXT):
        score -= 10
    return score


def is_candidate(url, resource_type=None):
    """过滤掉静态资源与第三方统计请求"""
    if not url:
        return False
    low = url.lower()
    if not low.startswith('http'):
        return False
    if low.endswith(EXCLUDE_EXT):
        return False
    if any(w in low for w in EXCLUDE_WORDS):
        return False
    if resource_type and str(resource_type).lower() in ('image', 'stylesheet', 'font', 'media', 'script'):
        return False
    return True


NORMAL_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')


def clean_headers(headers, cookie_placeholder='{{cookie}}'):
    """
    清洗请求头：只保留有意义的部分，并把 Cookie 换成变量，
    这样Cookie更新后接口配置依然可用。
    """
    cleaned = {}
    has_cookie = False
    for k, v in (headers or {}).items():
        lk = str(k).lower()
        if lk in DROP_HEADERS or lk.startswith('sec-') or lk.startswith(':'):
            continue
        if lk == 'cookie':
            has_cookie = True
            continue
        if lk not in KEEP_HEADERS:
            continue
        # 嗅探常在无头模式下进行，UA 会带 HeadlessChrome，直接请求容易被识别
        if lk == 'user-agent' and 'headless' in str(v).lower():
            v = NORMAL_UA
        cleaned[str(k)] = v
    if has_cookie:
        cleaned['Cookie'] = cookie_placeholder
    return cleaned


def parse_har(text, limit=80, include_all=False):
    """
    解析 HAR 文件，提取可能是签到接口的请求
    :param include_all: 为True时不过滤静态资源，返回全部请求供人工挑选
    :return: (候选请求列表（按匹配度排序）, 统计信息)
    """
    data = json.loads(text)
    entries = ((data.get('log') or {}).get('entries') or [])
    candidates = []
    seen = set()
    total = 0

    for e in entries:
        req = e.get('request') or {}
        url = req.get('url') or ''
        method = (req.get('method') or 'GET').upper()
        if not url.lower().startswith('http'):
            continue
        total += 1
        headers = {}
        for h in (req.get('headers') or []):
            name = h.get('name')
            if name:
                headers[name] = h.get('value', '')

        post = req.get('postData') or {}
        body = post.get('text') or ''
        if not body and post.get('params'):
            body = '&'.join('%s=%s' % (p.get('name', ''), p.get('value', ''))
                            for p in post['params'])

        mime = (((e.get('response') or {}).get('content') or {}).get('mimeType') or '').lower()
        rtype = ''
        if 'javascript' in mime:
            rtype = 'Script'
        elif 'css' in mime:
            rtype = 'Stylesheet'
        elif mime.startswith('image') or mime.startswith('font'):
            rtype = 'Image'

        is_static = not is_candidate(url, rtype)
        if is_static and not include_all:
            continue
        key = '%s %s' % (method, url)
        if key in seen:
            continue
        seen.add(key)

        body_type = 'none'
        if body:
            ctype = (post.get('mimeType') or '').lower()
            if 'json' in ctype or body.strip().startswith(('{', '[')):
                body_type = 'json'
            else:
                body_type = 'raw'

        resp_text = (((e.get('response') or {}).get('content') or {}).get('text') or '')
        item = {
            'method': method,
            'url': url,
            'headers': clean_headers(headers),
            'body': body,
            'body_type': body_type,
            'resource_type': rtype,
            'score': score_candidate(url) - (20 if is_static else 0),
            'is_static': is_static,
            'response_preview': resp_text[:200]
        }
        candidates.append(item)

    candidates.sort(key=lambda x: x['score'], reverse=True)
    return candidates[:limit], {'total': total, 'kept': len(candidates)}
