# debug_worker.py
import subprocess
import time
import logging
import os
import threading
import socket
import requests
from DrissionPage import ChromiumPage

logger = logging.getLogger(__name__)

USER_DATA_DIR = '/app/chrome_data'
DEBUG_PORT = 9222

_current_page = None
_lock = threading.Lock()

def start_debug_session(sign_url):
    global _current_page
    with _lock:
        # 清理旧进程
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None

        # 清理锁定文件
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        for f in ['SingletonLock', 'SingletonSocket', 'SingletonCookie']:
            lock_path = os.path.join(USER_DATA_DIR, f)
            if os.path.exists(lock_path):
                os.remove(lock_path)
                logger.info(f'已删除锁定文件: {lock_path}')

        # 启动 Chromium（去掉可能导致问题的参数）
        cmd = [
            '/usr/bin/chromium',
            '--headless',                # 使用旧模式，更稳定
            f'--remote-debugging-port={DEBUG_PORT}',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            f'--user-data-dir={USER_DATA_DIR}',
            sign_url
        ]
        logger.info('启动 Chromium: ' + ' '.join(cmd))
        with open('/tmp/chromium.log', 'w') as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=f)

        # 等待端口开放（最多 30 秒）
        port_ready = False
        for i in range(30):
            time.sleep(1)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', DEBUG_PORT))
            sock.close()
            if result == 0:
                port_ready = True
                logger.info(f'端口 {DEBUG_PORT} 已监听 (尝试 {i+1} 次)')
                break
        if not port_ready:
            logger.error(f'端口 {DEBUG_PORT} 在 30 秒内未监听')
            with open('/tmp/chromium.log', 'r') as f:
                logger.error(f'Chromium 错误日志:\n{f.read()}')
            raise RuntimeError('端口未监听')

        # 验证调试接口
        try:
            resp = requests.get(f'http://127.0.0.1:{DEBUG_PORT}/json/version', timeout=5)
            if resp.status_code == 200:
                logger.info('调试接口可用')
                logger.debug(resp.json())
            else:
                logger.error(f'调试接口返回状态码 {resp.status_code}')
                raise RuntimeError('调试接口不可用')
        except Exception as e:
            logger.error(f'调试接口请求异常: {e}')
            with open('/tmp/chromium.log', 'r') as f:
                logger.error(f'Chromium 错误日志:\n{f.read()}')
            raise

        # 连接浏览器
        try:
            page = ChromiumPage(addr_or_opts=f'127.0.0.1:{DEBUG_PORT}')
            _current_page = page
            logger.info(f'调试浏览器已启动，访问 {sign_url}')
            def keep_alive():
                while True:
                    time.sleep(10)
            threading.Thread(target=keep_alive, daemon=True).start()
        except Exception as e:
            logger.error(f'连接调试浏览器失败: {e}')
            raise

def stop_debug_session():
    global _current_page
    with _lock:
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None
            logger.info('调试浏览器已关闭')
