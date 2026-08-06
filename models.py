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
            'wecom_webhook_key': ''  # 新增：企业微信 Webhook Key
        }
        for k, v in defaults.items():
            conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (k, v))
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
                sign_button_selector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            data.get('sign_button_selector', '')
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
            sign_button_selector=?'''
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
            data.get('sign_button_selector', '')
        ]
        if 'cookies' in data:
            sql += ', cookies=?'
            params.append(data['cookies'])
        sql += ' WHERE id=?'
        params.append(sid)
        conn.execute(sql, params)
        conn.commit()


def delete_site(sid):
    with get_db() as conn:
        conn.execute('DELETE FROM sites WHERE id=?', (sid,))
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
    """原子性更新 cookies、签到时间和结果"""
    with get_db() as conn:
        if time_str is None:
            time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('UPDATE sites SET cookies=?, last_sign_time=?, sign_success=? WHERE id=?',
                     (cookies_json, time_str, 1 if success else 0, sid))
        conn.commit()


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
