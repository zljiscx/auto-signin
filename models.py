import os
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'auto_sign.db')
os.makedirs(DATA_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _add_column_if_not_exists(conn, table, column, col_type):
    cursor = conn.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cursor.fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")

def init_db():
    with get_db() as conn:
        # 站点表
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                login_url TEXT,
                sign_url TEXT NOT NULL,
                cookies TEXT,
                has_captcha INTEGER DEFAULT 0,
                has_cloudflare INTEGER DEFAULT 0,
                username TEXT,
                password TEXT,
                enabled INTEGER DEFAULT 1,
                last_sign_time TEXT,
                sign_success INTEGER DEFAULT 0
            )
        ''')
        _add_column_if_not_exists(conn, 'sites', 'username_selector', 'TEXT')
        _add_column_if_not_exists(conn, 'sites', 'password_selector', 'TEXT')
        _add_column_if_not_exists(conn, 'sites', 'captcha_img_selector', 'TEXT')
        _add_column_if_not_exists(conn, 'sites', 'captcha_input_selector', 'TEXT')
        _add_column_if_not_exists(conn, 'sites', 'submit_selector', 'TEXT')
        _add_column_if_not_exists(conn, 'sites', 'sign_button_selector', 'TEXT')
        _add_column_if_not_exists(conn, 'sites', 'login_first', 'INTEGER DEFAULT 0')
        # ========== 多模式签到支持 ==========
        # mode: browser=浏览器模拟 / api=HTTP接口直连
        _add_column_if_not_exists(conn, 'sites', 'mode', "TEXT DEFAULT 'browser'")
        # api_config: API模式请求配置(JSON字符串)
        _add_column_if_not_exists(conn, 'sites', 'api_config', 'TEXT')
        # success_rule: 签到成功判定规则(JSON字符串)，不配置时使用内置关键词
        _add_column_if_not_exists(conn, 'sites', 'success_rule', 'TEXT')
        # 清理已废弃的浏览器接管字段（该方案需保持浏览器常开，已移除）
        try:
            conn.execute('ALTER TABLE sites DROP COLUMN browser_address')
        except Exception:
            pass

        # 签到日志表
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sign_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                site_name TEXT NOT NULL,
                sign_time TEXT NOT NULL,
                success INTEGER DEFAULT 0,
                message TEXT,
                is_manual INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0
            )
        ''')

        # 配置表
        conn.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        defaults = {
            'sign_time': '05:05',
            'retry_times': '3',
            'ocr_api_key': '',
            'ocr_secret_key': '',
            'headless': '1',
            'wecom_webhook_key': '',
            'cf_timeout': '60'
        }
        for k, v in defaults.items():
            conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (k, v))

        # ========== 新增：签到时间表 ==========
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sign_times (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_str TEXT NOT NULL UNIQUE,
                enabled INTEGER DEFAULT 1,
                remark TEXT DEFAULT ''
            )
        ''')
        # 兼容旧数据：时间表为空时，从原配置迁移默认时间
        count = conn.execute('SELECT COUNT(*) as cnt FROM sign_times').fetchone()['cnt']
        if count == 0:
            default_time = conn.execute("SELECT value FROM config WHERE key='sign_time'").fetchone()
            default_time_str = default_time['value'] if default_time else '05:05'
            conn.execute('INSERT INTO sign_times (time_str, remark) VALUES (?, ?)',
                        (default_time_str, '默认签到时间'))

        conn.commit()

# ---------- 站点 CRUD ----------
def get_all_sites():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM sites ORDER BY id').fetchall()
        return [dict(row) for row in rows]

def get_site(sid):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM sites WHERE id=?', (sid,)).fetchone()
        return dict(row) if row else None

def add_site(data):
    with get_db() as conn:
        cursor = conn.execute('''
            INSERT INTO sites (
                name, login_url, sign_url, has_captcha, has_cloudflare,
                username, password, enabled, cookies,
                username_selector, password_selector,
                captcha_img_selector, captcha_input_selector, submit_selector,
                sign_button_selector, login_first,
                mode, api_config, success_rule
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['name'], data['login_url'], data['sign_url'],
            data.get('has_captcha', 0), data.get('has_cloudflare', 0),
            data.get('username', ''), data.get('password', ''),
            data.get('enabled', 1), data.get('cookies'),
            data.get('username_selector', ''),
            data.get('password_selector', ''),
            data.get('captcha_img_selector', ''),
            data.get('captcha_input_selector', ''),
            data.get('submit_selector', ''),
            data.get('sign_button_selector', ''),
            data.get('login_first', 0),
            data.get('mode', 'browser') or 'browser',
            data.get('api_config'),
            data.get('success_rule')
        ))
        conn.commit()
        return cursor.lastrowid

def update_site(sid, data):
    with get_db() as conn:
        sql = '''UPDATE sites SET
            name=?, login_url=?, sign_url=?, has_captcha=?, has_cloudflare=?,
            username=?, password=?, enabled=?,
            username_selector=?, password_selector=?,
            captcha_img_selector=?, captcha_input_selector=?, submit_selector=?,
            sign_button_selector=?, login_first=?, mode=?'''
        params = [
            data['name'], data['login_url'], data['sign_url'],
            data.get('has_captcha', 0), data.get('has_cloudflare', 0),
            data.get('username', ''), data.get('password', ''),
            data.get('enabled', 1),
            data.get('username_selector', ''),
            data.get('password_selector', ''),
            data.get('captcha_img_selector', ''),
            data.get('captcha_input_selector', ''),
            data.get('submit_selector', ''),
            data.get('sign_button_selector', ''),
            data.get('login_first', 0),
            data.get('mode', 'browser') or 'browser'
        ]
        if 'cookies' in data:
            sql += ', cookies=?'
            params.append(data['cookies'])
        if 'api_config' in data:
            sql += ', api_config=?'
            params.append(data['api_config'])
        if 'success_rule' in data:
            sql += ', success_rule=?'
            params.append(data['success_rule'])
        sql += ' WHERE id=?'
        params.append(sid)
        conn.execute(sql, params)
        conn.commit()

def delete_site(sid):
    with get_db() as conn:
        conn.execute('DELETE FROM sites WHERE id=?', (sid,))
        conn.execute('DELETE FROM sign_logs WHERE site_id=?', (sid,))
        conn.commit()

def update_site_cookies(sid, cookies_json):
    with get_db() as conn:
        conn.execute('UPDATE sites SET cookies=? WHERE id=?', (cookies_json, sid))
        conn.commit()

def update_site_sign_result(sid, success, time_str=None):
    with get_db() as conn:
        if time_str is None:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('UPDATE sites SET last_sign_time=?, sign_success=? WHERE id=?',
                     (time_str, 1 if success else 0, sid))
        conn.commit()

def update_site_cookies_and_result(sid, cookies_json, success, time_str=None):
    with get_db() as conn:
        if time_str is None:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('UPDATE sites SET cookies=?, last_sign_time=?, sign_success=? WHERE id=?',
                     (cookies_json, time_str, 1 if success else 0, sid))
        conn.commit()

# ---------- 签到日志 ----------
def add_sign_log(site_id, site_name, success, message, is_manual=False, duration=0):
    with get_db() as conn:
        time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            INSERT INTO sign_logs (site_id, site_name, sign_time, success, message, is_manual, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (site_id, site_name, time_str, 1 if success else 0, message, 1 if is_manual else 0, duration))
        conn.commit()

def get_recent_sign_logs(limit=100):
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM sign_logs ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
        return [dict(row) for row in rows]

# ---------- 配置 ----------
def get_config(key):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

def set_config(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

def get_all_configs():
    with get_db() as conn:
        rows = conn.execute('SELECT key, value FROM config').fetchall()
        return {row['key']: row['value'] for row in rows}

# ========== 新增：签到时间 CRUD ==========
def get_all_sign_times():
    with get_db() as conn:
        rows = conn.execute('SELECT * FROM sign_times ORDER BY time_str').fetchall()
        return [dict(row) for row in rows]

def get_sign_time(tid):
    with get_db() as conn:
        row = conn.execute('SELECT * FROM sign_times WHERE id=?', (tid,)).fetchone()
        return dict(row) if row else None

def add_sign_time(time_str, remark=''):
    with get_db() as conn:
        cursor = conn.execute('INSERT INTO sign_times (time_str, remark) VALUES (?, ?)',
                            (time_str, remark))
        conn.commit()
        return cursor.lastrowid

def update_sign_time(tid, time_str, enabled=1, remark=''):
    with get_db() as conn:
        conn.execute('''
            UPDATE sign_times SET time_str=?, enabled=?, remark=?
            WHERE id=?
        ''', (time_str, enabled, remark, tid))
        conn.commit()

def delete_sign_time(tid):
    with get_db() as conn:
        conn.execute('DELETE FROM sign_times WHERE id=?', (tid,))
        conn.commit()

# ========== 新增：判断站点今日是否已成功签到 ==========
def is_site_today_signed_success(site_id):
    site = get_site(site_id)
    if not site or not site.get('last_sign_time'):
        return False
    today = datetime.now().strftime('%Y-%m-%d')
    return site['last_sign_time'].startswith(today) and site.get('sign_success', 0) == 1
