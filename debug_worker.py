import subprocess
import time
import logging
import os
import threading
from DrissionPage import ChromiumPage

logger = logging.getLogger(__name__)

USER_DATA_DIR = '/app/chrome_data'
DEBUG_PORT = 9222

_current_page = None
_lock = threading.Lock()

def start_debug_session(sign_url):
    global _current_page
    with _lock:
        # 关闭旧的调试进程
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None

        os.makedirs(USER_DATA_DIR, exist_ok=True)

        # 构建启动命令（确保监听 0.0.0.0）
        cmd = [
            '/usr/bin/chromium',
            '--headless',
            f'--remote-debugging-port={DEBUG_PORT}',
            '--remote-debugging-address=0.0.0.0',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            f'--user-data-dir={USER_DATA_DIR}',
            sign_url
        ]
        logger.info('启动调试浏览器: ' + ' '.join(cmd))
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(5)  # 等待浏览器启动

        # 连接已启动的浏览器（内部访问 127.0.0.1）
        try:
            page = ChromiumPage(addr_or_opts=f'127.0.0.1:{DEBUG_PORT}')
            _current_page = page
            logger.info(f'调试浏览器已启动，访问 {sign_url}')
            # 守护线程保持进程
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
