import json
import os
import logging
import atexit
from datetime import datetime
from urllib.parse import quote, urlparse
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, flash, render_template, request, redirect, url_for, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
from models import (init_db, get_all_sites, get_site, add_site, update_site, delete_site, get_config, set_config,
                    get_all_configs, get_recent_sign_logs, get_all_sign_times, add_sign_time,
                    update_sign_time, delete_sign_time, get_sign_time, update_site_cookies)
from scheduler import start_scheduler, stop_scheduler, restart_scheduler
from sign_service import run_single_sign
from utils import (parse_cookies_input, encrypt_data, decrypt_data, normalize_cookies, get_encryption_key_info,
                   set_encryption_key_file, parse_curl, parse_har)
from executors import list_executors, get_mode_label, normalize_api_config, SignContext
from presets import get_presets

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

init_db()

# 启动调度器
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
    start_scheduler()


# ---------- 管理员密码相关 ----------
def get_admin_password_hash():
    env_pwd = os.environ.get('ADMIN_PASSWORD', '').strip()
    if env_pwd:
        return generate_password_hash(env_pwd)
    return get_config('admin_password')


def need_setup():
    return get_admin_password_hash() is None


def verify_admin_password(input_pwd):
    if not input_pwd:
        return False
    pwd_hash = get_admin_password_hash()
    if not pwd_hash:
        return False
    return check_password_hash(pwd_hash, input_pwd)


@app.before_request
def before_request_check():
    endpoint = request.endpoint
    if endpoint in ('setup', 'static'):
        return
    if need_setup():
        return redirect(url_for('setup'))


# ---------- 登录认证装饰器 ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            flash('请先登录', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)

    return decorated


# ---------- 首次密码设置 ----------
@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if not need_setup():
        return redirect(url_for('login'))
    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()
        if len(password) < 6:
            flash('密码长度不能少于6位', 'danger')
            return render_template('setup.html')
        if password != confirm:
            flash('两次输入的密码不一致', 'danger')
            return render_template('setup.html')
        hashed_pwd = generate_password_hash(password)
        set_config('admin_password', hashed_pwd)
        flash('管理员密码设置成功，请登录', 'success')
        return redirect(url_for('login'))
    return render_template('setup.html')


# ---------- 登录/登出 ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if need_setup():
        return redirect(url_for('setup'))
    if session.get('logged_in'):
        return redirect(url_for('index'))
    if request.method == 'POST':
        input_pwd = request.form.get('password', '')
        if verify_admin_password(input_pwd):
            session['logged_in'] = True
            flash('登录成功', 'success')
            return redirect(url_for('index'))
        else:
            flash('密码错误', 'danger')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('已退出登录', 'success')
    return redirect(url_for('login'))


# ---------- 首页 ----------
@app.route('/')
@login_required
def index():
    sites = get_all_sites()
    for s in sites:
        s['mode_label'] = get_mode_label(s.get('mode'))
    return render_template('index.html', sites=sites)


# ---------- 站点表单公共处理 ----------
def _render_form(site, cookies_text, form=None):
    """渲染站点编辑页，把JSON配置拆解成表单默认值，并保留用户刚提交的内容"""
    form = form or {}
    site = site or {}

    cfg = {}
    if site.get('api_config'):
        try:
            cfg = json.loads(site['api_config']) or {}
        except Exception:
            cfg = {}
    cfg = normalize_api_config(cfg)
    step = (cfg.get('steps') or [{}])[0] if cfg else {}

    rule = {}
    if site.get('success_rule'):
        try:
            rule = json.loads(site['success_rule']) or {}
        except Exception:
            rule = {}

    values = {
        'mode': site.get('mode') or 'browser',
        'api_method': step.get('method') or 'GET',
        'api_url': step.get('url') or '',
        'api_headers': '\n'.join('%s: %s' % (k, v) for k, v in (step.get('headers') or {}).items()),
        'api_body': step.get('body') or '',
        'api_body_type': step.get('body_type') or 'none',
        'api_extract': '\n'.join('%s=%s' % (k, v) for k, v in (step.get('extract') or {}).items()),
        'api_use_cookies': bool(cfg.get('use_site_cookies', True)),
        'api_refresh_cookie': bool(cfg.get('refresh_cookie_by_browser', False)),
        'api_follow_redirect': bool(cfg.get('follow_redirect', True)),
        'api_verify_ssl': bool(cfg.get('verify_ssl', True)),
        'api_timeout': cfg.get('timeout') or 20,
        'success_status': ','.join(str(x) for x in (rule.get('status') or [])),
        'success_contains': ','.join(rule.get('contains') or []),
        'success_not_contains': ','.join(rule.get('not_contains') or []),
        'success_json_path': rule.get('json_path') or '',
        'success_json_equals': '' if rule.get('json_equals') is None else str(rule.get('json_equals')),
    }

    checkbox_keys = ('api_use_cookies', 'api_refresh_cookie', 'api_follow_redirect', 'api_verify_ssl')
    for key in list(values.keys()):
        if key in checkbox_keys:
            values[key] = (key in form) if form else values[key]
        elif form.get(key) is not None:
            values[key] = form.get(key)

    api_editor = form.get('api_editor') or 'simple'
    api_config_json = form.get('api_config_json')
    if api_config_json is None:
        api_config_json = site.get('api_config') or ''

    return render_template('add_edit.html', site=site or None, cookies_text=cookies_text,
                           executors=list_executors(), api_editor=api_editor,
                           api_config_json=api_config_json, v=values, presets=get_presets())


def _run_test_and_flash(site_id, name):
    """保存后立即执行一次签到测试，并把结果写入提示"""
    try:
        success, msg = run_single_sign(site_id, is_manual=True)
        status = "✅ 成功" if success else "❌ 失败"
        flash(f'站点「{name}」测试结果: {status} - {msg}', 'success' if success else 'danger')
    except Exception as e:
        flash(f'站点「{name}」测试异常: {str(e)}', 'danger')


def _to_json_str(value):
    """导入时把可能是对象的值统一成JSON字符串"""
    if value is None or value == '':
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return None


def _parse_kv_lines(text, sep=None):
    """
    把 'A: 1\\nB: 2' 或 'a=1\\nb=2' 形式的文本解析为字典
    :param sep: 指定分隔符；不指定时优先用冒号（适合请求头）
    """
    result = {}
    for line in (text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        s = sep or (':' if ':' in line else ('=' if '=' in line else None))
        if not s or s not in line:
            continue
        k, v = line.split(s, 1)
        k = k.strip()
        if k:
            result[k] = v.strip()
    return result


def _build_api_config(form):
    """根据编辑方式组装 api_config JSON 字符串"""
    editor = form.get('api_editor', 'simple')
    if editor == 'json':
        raw = (form.get('api_config_json') or '').strip()
        if not raw:
            return ''
        try:
            cfg = json.loads(raw)
        except Exception as e:
            raise ValueError('API配置JSON格式错误：%s' % e)
        if not isinstance(cfg, dict):
            raise ValueError('API配置必须是一个JSON对象')
        if not normalize_api_config(cfg).get('steps'):
            raise ValueError('API配置中缺少有效的请求地址(url)')
        return json.dumps(cfg, ensure_ascii=False)

    url = (form.get('api_url') or '').strip()
    if not url:
        return ''
    method = (form.get('api_method') or 'GET').strip().upper()
    headers = _parse_kv_lines(form.get('api_headers'))
    extract = _parse_kv_lines(form.get('api_extract'), sep='=')
    body = form.get('api_body') or ''
    body_type = form.get('api_body_type') or ('none' if not body.strip() else 'raw')
    if not body.strip():
        body_type = 'none'
    try:
        timeout = int(form.get('api_timeout') or 20)
    except Exception:
        timeout = 20
    cfg = {
        'use_site_cookies': 1 if form.get('api_use_cookies') else 0,
        'refresh_cookie_by_browser': 1 if form.get('api_refresh_cookie') else 0,
        'timeout': timeout,
        'follow_redirect': 1 if form.get('api_follow_redirect') else 0,
        'verify_ssl': 1 if form.get('api_verify_ssl') else 0,
        'steps': [{
            'name': '签到',
            'method': method,
            'url': url,
            'headers': headers,
            'body': body,
            'body_type': body_type,
            'extract': extract
        }]
    }
    return json.dumps(cfg, ensure_ascii=False)


def _build_success_rule(form):
    """组装 success_rule JSON 字符串"""
    def split_words(text):
        return [w.strip() for w in (text or '').replace('，', ',').split(',') if w.strip()]

    rule = {}
    status_text = (form.get('success_status') or '').strip()
    if status_text:
        status_list = []
        for part in status_text.replace('，', ',').split(','):
            part = part.strip()
            if not part:
                continue
            try:
                status_list.append(int(part))
            except Exception:
                raise ValueError('期望状态码必须是数字，多个用逗号分隔')
        if status_list:
            rule['status'] = status_list

    contains = split_words(form.get('success_contains'))
    if contains:
        rule['contains'] = contains
    not_contains = split_words(form.get('success_not_contains'))
    if not_contains:
        rule['not_contains'] = not_contains
    json_path = (form.get('success_json_path') or '').strip()
    if json_path:
        rule['json_path'] = json_path
        rule['json_equals'] = (form.get('success_json_equals') or '').strip()
    return json.dumps(rule, ensure_ascii=False) if rule else None


def _collect_site_form(form):
    """
    从表单收集站点字段（供添加与编辑共用）
    密码与Cookie由调用方单独处理
    """
    mode = (form.get('mode') or 'browser').strip().lower()
    if mode not in [m for m, _, _ in list_executors()]:
        mode = 'browser'

    data = {
        'name': (form.get('name') or '').strip(),
        'login_url': (form.get('login_url') or '').strip(),
        'sign_url': (form.get('sign_url') or '').strip(),
        'has_captcha': 1 if form.get('has_captcha') else 0,
        'has_cloudflare': 1 if form.get('has_cloudflare') else 0,
        'username': (form.get('username') or '').strip(),
        'enabled': 1 if form.get('enabled') else 0,
        'username_selector': (form.get('username_selector') or '').strip(),
        'password_selector': (form.get('password_selector') or '').strip(),
        'captcha_img_selector': (form.get('captcha_img_selector') or '').strip(),
        'captcha_input_selector': (form.get('captcha_input_selector') or '').strip(),
        'submit_selector': (form.get('submit_selector') or '').strip(),
        'sign_button_selector': (form.get('sign_button_selector') or '').strip(),
        'login_first': 1 if form.get('login_first') else 0,
        'mode': mode,
        'api_config': _build_api_config(form),
        'success_rule': _build_success_rule(form)
    }

    # API模式下签到地址可留空，自动从接口地址推导，用于列表展示与Cookie域名识别
    if not data['sign_url'] and data['api_config']:
        try:
            steps = normalize_api_config(json.loads(data['api_config'])).get('steps') or []
            if steps:
                parsed = urlparse(steps[0]['url'])
                data['sign_url'] = '%s://%s' % (parsed.scheme, parsed.netloc)
        except Exception:
            pass
    return data


# ---------- cURL 导入 ----------
@app.route('/api/parse_curl', methods=['POST'])
@login_required
def api_parse_curl():
    """解析cURL命令，返回请求参数供页面填充"""
    payload = request.get_json(silent=True) or {}
    curl_text = payload.get('curl', '')
    try:
        parsed = parse_curl(curl_text)
        return {'ok': True, 'data': parsed}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# ---------- 接口嗅探（用浏览器点一次签到，抓出真实接口） ----------
@app.route('/api/sniff/<int:sid>', methods=['POST'])
@login_required
def api_sniff(sid):
    site = get_site(sid)
    if not site:
        return {'ok': False, 'error': '站点不存在'}
    try:
        from executors.sniffer import RequestSniffer
        configs = get_all_configs()
        ocr_config = {
            'api_key': configs.get('ocr_api_key', ''),
            'secret_key': configs.get('ocr_secret_key', '')
        }
        ctx = SignContext(site, ocr_config, is_manual=True)
        ok, message, candidates = RequestSniffer(ctx).capture()
        # 嗅探过程中登录刷新过Cookie，顺手存起来，省得用户手动粘贴
        if ok and ctx.cookies:
            try:
                update_site_cookies(sid, encrypt_data(json.dumps(ctx.cookies, ensure_ascii=False)))
            except Exception as e:
                logger.warning('嗅探后保存Cookie失败: %s' % e)
        return {'ok': ok, 'message': message, 'candidates': candidates}
    except Exception as e:
        logger.exception('嗅探失败')
        return {'ok': False, 'error': str(e)}


# ---------- HAR 文件导入 ----------
@app.route('/api/import_har', methods=['POST'])
@login_required
def api_import_har():
    file = request.files.get('har_file')
    if not file:
        return {'ok': False, 'error': '未选择HAR文件'}
    try:
        raw = file.read().decode('utf-8', errors='ignore')
        include_all = request.form.get('include_all') == '1'
        candidates, stats = parse_har(raw, limit=200, include_all=include_all)
        if not candidates:
            if stats.get('total'):
                return {'ok': False, 'stats': stats,
                        'error': 'HAR中共 %s 条请求，全部被识别为静态资源。'
                                 '请确认：① 导出前确实执行了签到动作；② Network面板勾选了「保留日志」；'
                                 '可勾选「包含全部请求」再试一次。' % stats['total']}
            return {'ok': False, 'stats': stats,
                    'error': 'HAR中没有解析到任何HTTP请求，请确认导出的是完整HAR文件'}
        return {'ok': True, 'candidates': candidates, 'stats': stats}
    except json.JSONDecodeError:
        return {'ok': False, 'error': '文件不是有效的JSON，请确认导出的是HAR格式'}
    except Exception as e:
        return {'ok': False, 'error': 'HAR解析失败：%s' % e}


# ---------- 站点添加 ----------
@app.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    if request.method == 'POST':
        try:
            data = _collect_site_form(request.form)
        except ValueError as e:
            flash(str(e), 'danger')
            return render_template('add_edit.html', site=None, cookies_text=request.form.get('cookies', '').strip(),
                                   executors=list_executors(), api_editor=request.form.get('api_editor', 'simple'),
                                   api_config_json=request.form.get('api_config_json', ''))
        data['password'] = encrypt_data(request.form.get('password', '').strip())
        cookies_raw = request.form.get('cookies', '').strip()
        if cookies_raw:
            try:
                parsed = parse_cookies_input(cookies_raw)
                if parsed:
                    cookies_list = json.loads(parsed)
                    domain = urlparse(data['login_url'] or data['sign_url']).netloc
                    normalized = normalize_cookies(cookies_list, domain)
                    data['cookies'] = encrypt_data(json.dumps(normalized, ensure_ascii=False))
                else:
                    data['cookies'] = None
            except ValueError as e:
                flash(f'Cookies格式错误: {str(e)}', 'danger')
                return _render_form(None, cookies_raw, request.form)
        else:
            data['cookies'] = None
        try:
            new_id = add_site(data)
            flash(f'站点「{data["name"]}」添加成功 (ID: {new_id})', 'success')
            if request.form.get('save_then_sniff'):
                return redirect(url_for('edit', sid=new_id, sniff=1))
            if request.form.get('save_and_test'):
                _run_test_and_flash(new_id, data['name'])
            return redirect(url_for('index'))
        except Exception as e:
            flash(f'添加失败: {str(e)}', 'danger')
            return _render_form(None, cookies_raw, request.form)
    return _render_form(None, '', None)


# ---------- 站点编辑 ----------
@app.route('/edit/<int:sid>', methods=['GET', 'POST'])
@login_required
def edit(sid):
    site = get_site(sid)
    if not site:
        flash('站点不存在', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        old_pass_enc = site.get('password', '')
        old_cookies_enc = site.get('cookies', '')
        new_pass = request.form.get('password', '').strip()
        new_cookies_raw = request.form.get('cookies', '').strip()

        # 密码处理：空=不修改，-=清除，其他=新密码加密存储
        if new_pass == '-':
            encrypted_password = ''
        elif new_pass:
            encrypted_password = encrypt_data(new_pass)
        else:
            encrypted_password = old_pass_enc

        if new_cookies_raw:
            try:
                parsed = parse_cookies_input(new_cookies_raw)
                if parsed:
                    cookies_list = json.loads(parsed)
                    domain = urlparse(site['login_url'] or site['sign_url']).netloc
                    normalized = normalize_cookies(cookies_list, domain)
                    encrypted_cookies = encrypt_data(json.dumps(normalized, ensure_ascii=False))
                else:
                    encrypted_cookies = None
            except ValueError as e:
                flash(f'Cookies格式错误: {str(e)}', 'danger')
                site_display = dict(site)
                site_display['password'] = ''
                return _render_form(site_display, new_cookies_raw, request.form)
        else:
            encrypted_cookies = None if old_cookies_enc else old_cookies_enc

        try:
            data = _collect_site_form(request.form)
        except ValueError as e:
            flash(str(e), 'danger')
            site_display = dict(site)
            site_display['password'] = ''
            return _render_form(site_display, new_cookies_raw, request.form)
        data['password'] = encrypted_password
        data['cookies'] = encrypted_cookies
        try:
            update_site(sid, data)
            flash(f'站点「{data["name"]}」更新成功', 'success')
            if request.form.get('save_then_sniff'):
                return redirect(url_for('edit', sid=sid, sniff=1))
            if request.form.get('save_and_test'):
                _run_test_and_flash(sid, data['name'])
            return redirect(url_for('index'))
        except Exception as e:
            flash(f'更新失败: {str(e)}', 'danger')
            site_display = dict(site)
            site_display['password'] = ''
            return _render_form(site_display, new_cookies_raw, request.form)

    site_display = dict(site)
    site_display['password'] = ''
    cookies_text = ''
    if site_display.get('cookies'):
        try:
            decrypted = decrypt_data(site_display['cookies'])
            if decrypted:
                cookies_list = json.loads(decrypted)
                if isinstance(cookies_list, list):
                    simplified = [{'name': c['name'], 'value': c['value']} for c in cookies_list if
                                  'name' in c and 'value' in c]
                    cookies_text = json.dumps(simplified, ensure_ascii=False, separators=(',', ':'))
        except Exception:
            pass
    return _render_form(site_display, cookies_text, None)


@app.route('/delete/<int:sid>', methods=['POST'])
@login_required
def delete(sid):
    site = get_site(sid)
    if site:
        try:
            delete_site(sid)
            flash(f'站点「{site["name"]}」已删除', 'warning')
        except Exception as e:
            flash(f'删除失败: {str(e)}', 'danger')
    else:
        flash('站点不存在', 'danger')
    return redirect(url_for('index'))


# ---------- 系统设置 ----------
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        try:
            old_configs = get_all_configs()
            retry = request.form['retry_times'].strip()
            headless = '1' if request.form.get('headless') else '0'

            api_key_input = request.form.get('ocr_api_key', '').strip()
            if api_key_input == '-':
                api_key = ''
            elif api_key_input == '':
                api_key = old_configs.get('ocr_api_key', '')
            else:
                api_key = api_key_input

            secret_key_input = request.form.get('ocr_secret_key', '').strip()
            if secret_key_input == '-':
                secret_key = ''
            elif secret_key_input == '':
                secret_key = old_configs.get('ocr_secret_key', '')
            else:
                secret_key = secret_key_input

            wecom_key_input = request.form.get('wecom_webhook_key', '').strip()
            if wecom_key_input == '-':
                wecom_key_encrypted = ''
            elif wecom_key_input == '':
                wecom_key_encrypted = old_configs.get('wecom_webhook_key', '')
            else:
                wecom_key_encrypted = encrypt_data(wecom_key_input)

            set_config('retry_times', retry)
            set_config('ocr_api_key', api_key)
            set_config('ocr_secret_key', secret_key)
            set_config('headless', headless)
            set_config('wecom_webhook_key', wecom_key_encrypted)

            cf_timeout_val = request.form.get('cf_timeout', '').strip()
            if cf_timeout_val:
                set_config('cf_timeout', cf_timeout_val)

            flash('全局设置已保存', 'success')
        except Exception as e:
            flash(f'保存失败: {str(e)}', 'danger')
        return redirect(url_for('settings'))

    configs = get_all_configs()
    sign_times = get_all_sign_times()

    wecom_configured = bool(configs.get('wecom_webhook_key', '')) and decrypt_data(
        configs['wecom_webhook_key']) is not None
    display_configs = {
        'retry_times': configs.get('retry_times', ''),
        'headless': configs.get('headless', '0'),
        'ocr_api_configured': bool(configs.get('ocr_api_key', '')),
        'ocr_secret_configured': bool(configs.get('ocr_secret_key', '')),
        'wecom_configured': wecom_configured,
        'cf_timeout': configs.get('cf_timeout', '60')
    }
    return render_template('settings.html', configs=display_configs, sign_times=sign_times)


# ========== 新增：签到时间点管理路由 ==========
@app.route('/settings/time/add', methods=['POST'])
@login_required
def add_sign_time_route():
    time_str = request.form.get('time_str', '').strip()
    remark = request.form.get('remark', '').strip()

    if ':' not in time_str or len(time_str.split(':')) != 2:
        flash('签到时间格式错误，请使用 HH:MM 格式', 'danger')
        return redirect(url_for('settings'))
    try:
        hour, minute = time_str.split(':')
        if not (0 <= int(hour) < 24 and 0 <= int(minute) < 60):
            raise ValueError
    except:
        flash('时间值不合法，小时0-23，分钟0-59', 'danger')
        return redirect(url_for('settings'))

    try:
        new_id = add_sign_time(time_str, remark)
        restart_scheduler()
        flash(f'签到时间 {time_str} 添加成功 (ID: {new_id})，调度器已刷新', 'success')
    except Exception as e:
        flash(f'添加失败: {str(e)}', 'danger')
    return redirect(url_for('settings'))


@app.route('/settings/time/edit/<int:tid>', methods=['POST'])
@login_required
def edit_sign_time_route(tid):
    time_item = get_sign_time(tid)
    if not time_item:
        flash('时间点不存在', 'danger')
        return redirect(url_for('settings'))

    time_str = request.form.get('time_str', '').strip()
    remark = request.form.get('remark', '').strip()
    enabled = int(request.form.get('enabled', 0))

    if ':' not in time_str or len(time_str.split(':')) != 2:
        flash('签到时间格式错误，请使用 HH:MM 格式', 'danger')
        return redirect(url_for('settings'))
    try:
        hour, minute = time_str.split(':')
        if not (0 <= int(hour) < 24 and 0 <= int(minute) < 60):
            raise ValueError
    except:
        flash('时间值不合法，小时0-23，分钟0-59', 'danger')
        return redirect(url_for('settings'))

    try:
        update_sign_time(tid, time_str, enabled, remark)
        restart_scheduler()
        flash(f'签到时间 {time_str} 更新成功，调度器已刷新', 'success')
    except Exception as e:
        flash(f'更新失败: {str(e)}', 'danger')
    return redirect(url_for('settings'))


@app.route('/settings/time/delete/<int:tid>', methods=['POST'])
@login_required
def delete_sign_time_route(tid):
    time_item = get_sign_time(tid)
    if not time_item:
        flash('时间点不存在', 'danger')
        return redirect(url_for('settings'))

    all_times = get_all_sign_times()
    if len(all_times) <= 1:
        flash('至少需要保留一个签到时间点', 'danger')
        return redirect(url_for('settings'))

    try:
        delete_sign_time(tid)
        restart_scheduler()
        flash(f'签到时间 {time_item["time_str"]} 已删除，调度器已刷新', 'success')
    except Exception as e:
        flash(f'删除失败: {str(e)}', 'danger')
    return redirect(url_for('settings'))


# ---------- 配置备份 ----------
@app.route('/settings/backup', methods=['POST'])
@login_required
def backup_config():
    try:
        db_configs = get_all_configs()
        key_info = get_encryption_key_info()
        flask_secret = os.environ.get('FLASK_SECRET_KEY', '')
        system_keys = {
            'encryption_key': key_info['key'],
            'encryption_key_source': key_info['source'],
            'flask_secret_key': flask_secret
        }
        backup_data = {
            'backup_version': 2,
            'backup_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'database_configs': {},
            'sign_times': get_all_sign_times(),
            'system_keys': system_keys
        }
        # 将加密的配置项解密为明文导出
        for key, value in db_configs.items():
            if key == 'wecom_webhook_key' and value:
                decrypted_wecom = decrypt_data(value)
                backup_data['database_configs'][key] = decrypted_wecom or ''
            else:
                backup_data['database_configs'][key] = value
        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = f"系统配置备份-{date_str}.json"
        json_str = json.dumps(backup_data, ensure_ascii=False, indent=2)
        resp = Response(json_str, mimetype='application/json')
        encoded = quote(filename)
        resp.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded}"
        return resp
    except Exception as e:
        flash(f'备份失败: {str(e)}', 'danger')
        return redirect(url_for('settings'))


# ---------- 配置还原 ----------
@app.route('/settings/restore', methods=['POST'])
@login_required
def restore_config():
    file = request.files.get('config_file')
    if not file:
        flash('未选择备份文件', 'danger')
        return redirect(url_for('settings'))
    try:
        raw = file.read().decode('utf-8')
        backup_data = json.loads(raw)

        if not isinstance(backup_data, dict):
            raise ValueError('无效的备份文件格式')
        if backup_data.get('backup_version') not in (1, 2):
            raise ValueError('不支持的备份版本，请使用对应版本的程序')
        if 'database_configs' not in backup_data:
            raise ValueError('备份文件缺少配置数据')

        db_configs = backup_data.get('database_configs', {})
        system_keys = backup_data.get('system_keys', {})

        for key, value in db_configs.items():
            str_value = str(value) if value is not None else ''
            # v2明文格式：wecom_webhook_key 需加密后存储；v1加密格式直接使用
            if key == 'wecom_webhook_key' and backup_data.get('backup_version', 1) >= 2 and str_value:
                str_value = encrypt_data(str_value)
            set_config(key, str_value)

        # 还原签到时间点（全量覆盖）
        backup_sign_times = backup_data.get('sign_times', [])
        if backup_sign_times:
            from models import get_db
            with get_db() as conn:
                conn.execute('DELETE FROM sign_times')
                conn.commit()
            for t in backup_sign_times:
                try:
                    add_sign_time(t.get('time_str', '05:05'), t.get('remark', ''))
                except:
                    pass

        key_info = get_encryption_key_info()
        new_enc_key = system_keys.get('encryption_key')
        key_msg = ''
        if new_enc_key:
            if key_info['source'] == 'env':
                key_msg = '加密密钥当前由环境变量控制，无法通过页面修改，请手动更新 ENCRYPTION_KEY 环境变量后重启服务'
            else:
                if set_encryption_key_file(new_enc_key):
                    key_msg = '加密密钥已更新，加解密立即生效'
                else:
                    key_msg = '加密密钥写入失败，请检查 data 目录权限'

        flask_key_msg = ''
        if system_keys.get('flask_secret_key'):
            flask_key_msg = '会话密钥 FLASK_SECRET_KEY 需手动配置到环境变量，重启服务后登录会话才可保持有效'

        restart_scheduler()

        msg = '配置还原成功，数据库设置已立即生效'
        if key_msg:
            msg += f'；{key_msg}'
        if flask_key_msg:
            msg += f'；{flask_key_msg}'
        flash(msg, 'success')
    except json.JSONDecodeError:
        flash('备份文件解析失败，不是有效的 JSON 文件', 'danger')
    except ValueError as e:
        flash(f'还原失败: {str(e)}', 'danger')
    except Exception as e:
        flash(f'还原失败: {str(e)}', 'danger')
    return redirect(url_for('settings'))


# ---------- 导入导出 ----------
@app.route('/export', methods=['POST'])
@login_required
def export():
    sites = get_all_sites()
    export_sites = []
    for site in sites:
        site_copy = dict(site)
        # 解密cookies为明文
        if site_copy.get('cookies'):
            try:
                decrypted = decrypt_data(site_copy['cookies'])
                if decrypted:
                    cookies_list = json.loads(decrypted)
                    simplified = [{'name': c['name'], 'value': c['value']} for c in cookies_list
                                  if 'name' in c and 'value' in c]
                    site_copy['cookies'] = json.dumps(simplified, ensure_ascii=False)
                else:
                    site_copy['cookies'] = None
            except Exception:
                site_copy['cookies'] = None
        # 解密密码为明文
        if site_copy.get('password'):
            try:
                decrypted_pwd = decrypt_data(site_copy['password'])
                site_copy['password'] = decrypted_pwd or ''
            except Exception:
                site_copy['password'] = ''
        export_sites.append(site_copy)
    date_str = datetime.now().strftime('%Y-%m-%d')
    filename = f"站点备份-{date_str}.json"
    encoded = quote(filename)
    json_str = json.dumps(export_sites, ensure_ascii=False, indent=2)
    resp = Response(json_str, mimetype='application/json')
    resp.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded}"
    return resp


@app.route('/import', methods=['POST'])
@login_required
def import_data():
    file = request.files.get('file')
    if not file:
        flash('未选择文件', 'danger')
        return redirect(url_for('index'))
    try:
        raw = file.read().decode('utf-8')
        data = json.loads(raw)
        if not isinstance(data, list):
            flash('无效JSON格式，应为数组', 'danger')
            return redirect(url_for('index'))

        existing_sites = get_all_sites()
        existing_pairs = {(s['login_url'].strip(), s['sign_url'].strip()) for s in existing_sites}
        count = 0
        skip = 0
        for item in data:
            try:
                login_url = item.get('login_url', '').strip()
                sign_url = item.get('sign_url', '').strip()
                if (login_url, sign_url) in existing_pairs:
                    skip += 1
                    continue
                cookies = item.get('cookies')
                cookies_final = None
                if cookies:
                    decrypted = decrypt_data(cookies)
                    cookies_list = []
                    if decrypted:
                        try:
                            cookies_list = json.loads(decrypted)
                        except:
                            pass
                    else:
                        # 明文JSON格式（新版导出）
                        try:
                            cookies_list = json.loads(cookies) if isinstance(cookies, str) else cookies
                        except:
                            pass
                        # 明文 key=value 格式兜底
                        if not cookies_list and isinstance(cookies, str):
                            try:
                                from utils import parse_cookies_input
                                parsed = parse_cookies_input(cookies)
                                if parsed:
                                    cookies_list = json.loads(parsed)
                            except:
                                pass
                    if cookies_list:
                        domain = urlparse(login_url or sign_url).netloc
                        normalized = normalize_cookies(cookies_list, domain)
                        cookies_final = encrypt_data(json.dumps(normalized, ensure_ascii=False))
                password = item.get('password', '')
                if password:
                    decrypted_pwd = decrypt_data(password)
                    if decrypted_pwd is not None:
                        password = encrypt_data(decrypted_pwd) if decrypted_pwd else ''
                    else:
                        # 明文密码，加密后存储
                        password = encrypt_data(password)
                add_site({
                    'name': item.get('name', '').strip(),
                    'login_url': login_url,
                    'sign_url': sign_url,
                    'has_captcha': int(item.get('has_captcha', 0)),
                    'has_cloudflare': int(item.get('has_cloudflare', 0)),
                    'username': item.get('username', '').strip(),
                    'password': password,
                    'enabled': int(item.get('enabled', 1)),
                    'cookies': cookies_final,
                    'username_selector': item.get('username_selector', '').strip(),
                    'password_selector': item.get('password_selector', '').strip(),
                    'captcha_img_selector': item.get('captcha_img_selector', '').strip(),
                    'captcha_input_selector': item.get('captcha_input_selector', '').strip(),
                    'submit_selector': item.get('submit_selector', '').strip(),
                    'sign_button_selector': item.get('sign_button_selector', '').strip(),
                    'login_first': int(item.get('login_first', 0)),
                    'mode': (item.get('mode') or 'browser').strip().lower(),
                    'api_config': _to_json_str(item.get('api_config')),
                    'success_rule': _to_json_str(item.get('success_rule'))
                })
                count += 1
                existing_pairs.add((login_url, sign_url))
            except Exception as e:
                skip += 1
                logger.warning(f"导入失败: {item.get('name', '未知')} - {e}")
        msg = f'成功导入 {count} 个站点'
        if skip > 0:
            msg += f'，跳过 {skip} 个重复/失败站点'
        flash(msg, 'success' if skip == 0 else 'warning')
    except json.JSONDecodeError as e:
        flash(f'JSON解析错误: {str(e)}', 'danger')
    except Exception as e:
        flash(f'导入失败: {str(e)}', 'danger')
    return redirect(url_for('index'))


# ---------- 手动签到 ----------
@app.route('/manual_sign/<int:sid>', methods=['POST'])
@login_required
def manual_sign(sid):
    site = get_site(sid)
    if not site:
        flash('站点不存在', 'danger')
        return redirect(url_for('index'))
    if not site['enabled']:
        flash('站点已禁用，请先启用', 'warning')
        return redirect(url_for('index'))
    success, msg = run_single_sign(sid, is_manual=True)
    status = "✅" if success else "❌"
    flash(f'站点「{site["name"]}」签到结果: {status} - {msg}', 'success' if success else 'danger')
    return redirect(url_for('index'))


# ---------- 签到日志 ----------
@app.route('/logs')
@login_required
def logs():
    logs = get_recent_sign_logs(limit=100)
    return render_template('logs.html', logs=logs)


# ---------- 优雅停机 ----------
def graceful_shutdown():
    logger.info("正在优雅关闭...")
    stop_scheduler()
    logger.info("调度器已停止")


atexit.register(graceful_shutdown)

if __name__ == '__main__':
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('FLASK_PORT', 56789))
    app.run(debug=debug, host=host, port=port)
