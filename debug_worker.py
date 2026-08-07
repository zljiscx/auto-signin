# debug_worker.py
import threading
import time
import logging
from DrissionPage import ChromiumPage, ChromiumOptions

logger = logging.getLogger(__name__)

USER_DATA_DIR = '/app/chrome_data'   # 固定目录
DEBUG_PORT = 9222

_current_page = None
_lock = threading.Lock()

def start_debug_session(sign_url):
    """启动调试浏览器（无头模式但支持远程调试），打开指定签到页"""
    global _current_page
    with _lock:
        # 关闭已有调试进程
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None
        # 启动新浏览器
        co = ChromiumOptions()
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        co.set_argument(f'--user-data-dir={USER_DATA_DIR}')
        co.set_argument(f'--remote-debugging-port={DEBUG_PORT}')
        co.set_argument('--headless=new')   # 新无头模式，支持远程调试
        page = ChromiumPage(co)
        page.get(sign_url)
        logger.info(f"调试浏览器已启动，访问 {sign_url}")
        _current_page = page
        # 守护线程保持浏览器存活
        def keep_alive():
            while True:
                time.sleep(10)
        threading.Thread(target=keep_alive, daemon=True).start()

def stop_debug_session():
    global _current_page
    with _lock:
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None
            logger.info("调试浏览器已关闭")