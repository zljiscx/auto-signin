import json
import os
import logging
import shutil
import subprocess
import time
from datetime import datetime
from urllib.parse import quote, urlparse
from flask import Flask, flash, render_template, request, redirect, url_for, Response, jsonify
from models import (
    init_db, get_all_sites, get_site, add_site, update_site,
    delete_site, get_config, set_config, get_all_configs
)
from scheduler import start_scheduler, stop_scheduler
from signer import sign_site
from utils import (
    parse_cookies_input, encrypt_data, decrypt_data, normalize_cookies, get_user_data_dir, is_docker
)

app = Flask(__name__)
app.secret_key = os.urandom(24)
logging.basicConfig(level=logging.INFO)

init_db()

sign_time = get_config('sign_time') or '05:05'
start_scheduler(sign_time)


@app.route('/')
def index():
    sites = get_all_sites()
    return render_template('index.html', sites=sites)


@app.route('/add', methods=['GET', 'POST'])
def add():
    if request.method == 'POST':
        data = {
            'name': request.form['name'].strip(),
            'login_url': request.form['login_url'].strip(),
            'sign_url': request.form['sign_url'].strip(),
            'has_captcha': 1 if request.form.get('has_captcha') else 0,
            'has_cloudflare': 1 if request.form.get('has_cloudflare') else 0,
            'username': request.form.get('username', '').strip(),
            'password': encrypt_data(request.form.get('password', '').strip()),
            'enabled': 1 if request.form.get('enabled') else 0,
            'username_selector': request.form.get('username_selector', '').strip(),
            'password_selector': request.form.get('password_selector', '').strip(),
            'captcha_img_selector': request.form.get('captcha_img_selector', '').strip(),
            'captcha_input_selector': request.form.get('captcha_input_selector', '').strip(),
            'submit_selector': request.form.get('submit_selector', '').strip(),
            'sign_button_selector': request.form.get('sign_button_selector', '').strip()
        }
        cookies_raw = request.form.get('cookies', '').strip()
        if cookies_raw:
            try:
                parsed = parse_cookies_input(cookies_raw)
                if parsed:
                    cookies_list = json.loads(parsed)
                    default_domain = urlparse(data['login_url'] or data['sign_url']).netloc
                    normalized = normalize_cookies(cookies_list, default_domain)
                    data['cookies'] = json.dumps(normalized, ensure_ascii=False)
                else:
                    data['cookies'] = None
            except ValueError as e:
                flash(f'Cookies格式错误: {str(e)}', 'danger')
                return render_template('add_edit.html', site=None, cookies_text=cookies_raw)
        else:
            data['cookies'] = None

        try:
            new_id = add_site(data)
            flash(f'站点「{data["name"]}」添加成功 (ID: {new_id})', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            flash(f'添加失败: {str(e)}', 'danger')
            return render_template('add_edit.html', site=None, cookies_text=cookies_raw)
    return render_template('add_edit.html', site=None, cookies_text='')


@app.route('/edit/<int:sid>', methods=['GET', 'POST'])
def edit(sid):
    site = get_site(sid)
    if not site:
        flash('站点不存在', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        # 旧明文（用于比较）
        old_password_plain = decrypt_data(site.get('password', '')) if site.get('password') else ''
        old_cookies_plain = decrypt_data(site.get('cookies', '')) if site.get('cookies') else ''

        new_password_plain = request.form.get('password', '').strip()
        new_cookies_raw = request.form.get('cookies', '').strip()

        # 密码处理
        if new_password_plain == old_password_plain:
            encrypted_password = site.get('password')
        else:
            encrypted_password = encrypt_data(new_password_plain) if new_password_plain else None

        # Cookies 处理
        if new_cookies_raw:
            try:
                parsed = parse_cookies_input(new_cookies_raw)
                if parsed:
                    cookies_list = json.loads(parsed)
                    default_domain = urlparse(site['login_url'] or site['sign_url']).netloc
                    normalized = normalize_cookies(cookies_list, default_domain)
                    new_cookies_parsed = json.dumps(normalized, ensure_ascii=False)
                    if new_cookies_parsed == old_cookies_plain:
                        encrypted_cookies = site.get('cookies')
                    else:
                        encrypted_cookies = encrypt_data(new_cookies_parsed) if new_cookies_parsed else None
                else:
                    encrypted_cookies = None
            except ValueError as e:
                flash(f'Cookies格式错误: {str(e)}', 'danger')
                # 保留用户输入
                site_display = dict(site)
                if site_display.get('password'):
                    site_display['password'] = decrypt_data(site_display['password'])
                return render_template('add_edit.html', site=site_display, cookies_text=new_cookies_raw)
        else:
            # 清空 cookies
            if old_cookies_plain is not None:
                encrypted_cookies = None
            else:
                encrypted_cookies = site.get('cookies')

        data = {
            'name': request.form['name'].strip(),
            'login_url': request.form['login_url'].strip(),
            'sign_url': request.form['sign_url'].strip(),
            'has_captcha': 1 if request.form.get('has_captcha') else 0,
            'has_cloudflare': 1 if request.form.get('has_cloudflare') else 0,
            'username': request.form.get('username', '').strip(),
            'password': encrypted_password,
            'enabled': 1 if request.form.get('enabled') else 0,
            'username_selector': request.form.get('username_selector', '').strip(),
            'password_selector': request.form.get('password_selector', '').strip(),
            'captcha_img_selector': request.form.get('captcha_img_selector', '').strip(),
            'captcha_input_selector': request.form.get('captcha_input_selector', '').strip(),
            'submit_selector': request.form.get('submit_selector', '').strip(),
            'sign_button_selector': request.form.get('sign_button_selector', '').strip(),
            'cookies': encrypted_cookies
        }

        try:
            update_site(sid, data)
            flash(f'站点「{data["name"]}」更新成功', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            flash(f'更新失败: {str(e)}', 'danger')
            site_display = dict(site)
            if site_display.get('password'):
                site_display['password'] = decrypt_data(site_display['password'])
            return render_template('add_edit.html', site=site_display, cookies_text=new_cookies_raw)

    # GET 请求：解密并显示简化 cookies
    site_display = dict(site)
    if site_display.get('password'):
        site_display['password'] = decrypt_data(site_display['password'])

    cookies_text = ''
    if site_display.get('cookies'):
        try:
            decrypted = decrypt_data(site_display['cookies'])
            if decrypted:
                cookies_list = json.loads(decrypted)
                if isinstance(cookies_list, list):
                    # 只保留 name 和 value
                    simplified = [{'name': c['name'], 'value': c['value']} for c in cookies_list if
                                  'name' in c and 'value' in c]
                    cookies_text = json.dumps(simplified, ensure_ascii=False, separators=(',', ':'))
                else:
                    cookies_text = decrypted
        except Exception as e:
            logging.warning(f"解密 cookies 失败: {e}")
            cookies_text = ''

    return render_template('add_edit.html', site=site_display, cookies_text=cookies_text)


@app.route('/delete/<int:sid>')
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


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        try:
            new_time = request.form['sign_time'].strip()
            retry = request.form['retry_times'].strip()
            api_key = request.form.get('ocr_api_key', '').strip()
            secret_key = request.form.get('ocr_secret_key', '').strip()
            headless = '1' if request.form.get('headless') else '0'
            wecom_key_raw = request.form.get('wecom_webhook_key', '').strip()

            if ':' not in new_time or len(new_time.split(':')) != 2:
                flash('签到时间格式错误，请使用 HH:MM', 'danger')
                return redirect(url_for('settings'))

            set_config('sign_time', new_time)
            set_config('retry_times', retry)
            set_config('ocr_api_key', api_key)
            set_config('ocr_secret_key', secret_key)
            set_config('headless', headless)

            # 加密保存 Webhook Key
            if wecom_key_raw:
                set_config('wecom_webhook_key', encrypt_data(wecom_key_raw))
            else:
                set_config('wecom_webhook_key', '')

            stop_scheduler()
            start_scheduler(new_time)
            flash('全局设置已保存，调度器已重启', 'success')
        except Exception as e:
            flash(f'保存设置失败: {str(e)}', 'danger')
        return redirect(url_for('settings'))

    configs = get_all_configs()
    # 解密 Webhook Key 用于显示
    if configs.get('wecom_webhook_key'):
        try:
            configs['wecom_webhook_key'] = decrypt_data(configs['wecom_webhook_key'])
        except:
            configs['wecom_webhook_key'] = ''
    return render_template('settings.html', configs=configs)


@app.route('/export')
def export():
    sites = get_all_sites()
    date_str = datetime.now().strftime('%Y-%m-%d')
    filename = f"站点备份-{date_str}.json"
    encoded_filename = quote(filename)
    json_str = json.dumps(sites, ensure_ascii=False, indent=2)
    response = Response(json_str, mimetype='application/json')
    response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"
    return response


@app.route('/import', methods=['POST'])
def import_data():
    file = request.files.get('file')
    if not file:
        flash('未选择文件', 'danger')
        return redirect(url_for('index'))
    try:
        raw = file.read().decode('utf-8')
        data = json.loads(raw)
        if not isinstance(data, list):
            flash('无效的JSON格式，应为数组', 'danger')
            return redirect(url_for('index'))
        count = 0
        for item in data:
            cookies = item.get('cookies')
            if cookies:
                if isinstance(cookies, str):
                    try:
                        cookies_list = json.loads(cookies)
                    except:
                        cookies_list = []
                elif isinstance(cookies, list):
                    cookies_list = cookies
                else:
                    cookies_list = []
                if cookies_list:
                    default_domain = urlparse(item.get('login_url') or item.get('sign_url')).netloc
                    normalized = normalize_cookies(cookies_list, default_domain)
                    cookies = json.dumps(normalized, ensure_ascii=False)
                else:
                    cookies = None
            # 其他字段直接取
            add_site({
                'name': item.get('name', '').strip(),
                'login_url': item.get('login_url', '').strip(),
                'sign_url': item.get('sign_url', '').strip(),
                'has_captcha': int(item.get('has_captcha', 0)),
                'has_cloudflare': int(item.get('has_cloudflare', 0)),
                'username': item.get('username', '').strip(),
                'password': item.get('password', ''),
                'enabled': int(item.get('enabled', 1)),
                'cookies': cookies,
                'username_selector': item.get('username_selector', '').strip(),
                'password_selector': item.get('password_selector', '').strip(),
                'captcha_img_selector': item.get('captcha_img_selector', '').strip(),
                'captcha_input_selector': item.get('captcha_input_selector', '').strip(),
                'submit_selector': item.get('submit_selector', '').strip(),
                'sign_button_selector': item.get('sign_button_selector', '').strip()
            })
            count += 1
        flash(f'成功导入 {count} 个站点', 'success')
    except json.JSONDecodeError as e:
        flash(f'JSON解析错误: {str(e)}', 'danger')
    except Exception as e:
        flash(f'导入失败: {str(e)}', 'danger')
    return redirect(url_for('index'))


@app.route('/manual_sign/<int:sid>')
def manual_sign(sid):
    site = get_site(sid)
    if not site:
        flash('站点不存在', 'danger')
        return redirect(url_for('index'))
    if not site['enabled']:
        flash('该站点当前处于禁用状态，请先启用', 'warning')
        return redirect(url_for('index'))
    configs = get_all_configs()
    ocr_config = {
        'api_key': configs.get('ocr_api_key', ''),
        'secret_key': configs.get('ocr_secret_key', '')
    }
    retry_times = int(configs.get('retry_times', 3))
    try:
        success, msg = sign_site(site, ocr_config, retry_times)
        flash(f'站点「{site["name"]}」签到结果: {"✅ 成功" if success else "❌ 失败"} - {msg}',
              'success' if success else 'danger')
    except Exception as e:
        flash(f'签到过程异常: {str(e)}', 'danger')
    return redirect(url_for('index'))


@app.route('/upload_user_data/<int:sid>', methods=['POST'])
def upload_user_data(sid):
    site = get_site(sid)
    if not site:
        return jsonify({'success': False, 'message': '站点不存在'}), 404

    if 'files' not in request.files:
        return jsonify({'success': False, 'message': '未选择文件'}), 400

    files = request.files.getlist('files')
    if not files:
        return jsonify({'success': False, 'message': '文件列表为空'}), 400

    target_dir = get_user_data_dir()

    # 杀死 Chromium 进程（释放可能占用的文件）
    try:
        if is_docker():
            subprocess.run(['pkill', '-f', 'chromium'], capture_output=True, check=False)
        else:
            subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], capture_output=True, check=False)
        time.sleep(2)
    except Exception as e:
        logging.warning(f"结束 Chromium 进程时出错: {e}")

    # 清空目标目录内容（保留目录本身，因为是挂载点）
    if os.path.exists(target_dir):
        for item in os.listdir(target_dir):
            item_path = os.path.join(target_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            except Exception as e:
                logging.warning(f"删除 {item_path} 失败: {e}")
    else:
        os.makedirs(target_dir, exist_ok=True)

    # 保存上传的文件
    for file in files:
        rel_path = file.filename
        full_path = os.path.join(target_dir, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        file.save(full_path)

    return jsonify({'success': True, 'message': f'用户数据已上传，共 {len(files)} 个文件。下次签到将使用新数据。'})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=56789)
