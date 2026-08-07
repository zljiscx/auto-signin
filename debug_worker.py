# debug_worker.py
import threading
import time
import logging
import os
from DrissionPage import ChromiumPage, ChromiumOptions

logger = logging.getLogger(__name__)

USER_DATA_DIR = '/app/chrome_data'
DEBUG_PORT = 9222

_current_page = None
_lock = threading.Lock()

def start_debug_session(sign_url):
    global _current_page
    with _lock:
        # 关闭已有调试进程
        if _current_page:
            try:
                _current_page.close()
            except Exception as e:
                logger.error(f"关闭旧调试浏览器失败: {e}")
            _current_page = None

        os.makedirs(USER_DATA_DIR, exist_ok=True)

        try:
            co = ChromiumOptions()
            # 基础反检测参数
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-dev-shm-usage')
            co.set_argument('--disable-blink-features=AutomationControlled')
            co.set_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            # 用户数据目录
            co.set_argument(f'--user-data-dir={USER_DATA_DIR}')
            # 远程调试：端口 + 绑定所有地址（关键！）
            co.set_argument(f'--remote-debugging-port={DEBUG_PORT}')
            co.set_argument('--remote-debugging-address=0.0.0.0')
            # 启用新无头模式（支持远程调试）
            co.set_argument('--headless=new')
            co.set_argument('--disable-gpu')
            co.set_argument('--window-size=1200,800')

            logger.info(f"启动调试浏览器，访问 {sign_url}")
            page = ChromiumPage(co)
            page.get(sign_url)
            logger.info(f"调试浏览器已启动，远程调试地址: http://0.0.0.0:{DEBUG_PORT}")
            _current_page = page

            def keep_alive():
                while True:
                    time.sleep(10)
            threading.Thread(target=keep_alive, daemon=True).start()
        except Exception as e:
            logger.error(f"启动调试浏览器失败: {e}")
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
            logger.info("调试浏览器已关闭")
