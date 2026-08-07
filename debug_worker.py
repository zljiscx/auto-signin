import subprocess
import time
import logging
import os
import threading
import socket
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

        # 构建启动命令（使用 --headless=new 并添加更多稳定参数）
        cmd = [
            '/usr/bin/chromium',
            '--headless=new',
            f'--remote-debugging-port={DEBUG_PORT}',
            '--remote-debugging-address=0.0.0.0',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-features=IsolateOrigins,site-per-process',
            '--enable-unsafe-swiftshader',
            f'--user-data-dir={USER_DATA_DIR}',
            sign_url
        ]
        logger.info('启动调试浏览器: ' + ' '.join(cmd))
        
        # 将标准错误重定向到文件以便调试
        with open('/tmp/chromium_debug.log', 'w') as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=f)
        
        # 等待浏览器启动（给予足够时间）
        time.sleep(5)
        
        # 检查进程是否存活
        if proc.poll() is not None:
            logger.error(f'Chromium 进程已退出，返回码: {proc.returncode}')
            with open('/tmp/chromium_debug.log', 'r') as f:
                logger.error(f'Chromium 错误输出:\n{f.read()}')
            raise RuntimeError('Chromium 启动失败，请查看 /tmp/chromium_debug.log')
        
        # 检查端口是否监听
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', DEBUG_PORT))
        if result != 0:
            logger.error(f'端口 {DEBUG_PORT} 未监听，Chromium 可能未正确绑定')
            # 尝试读取日志最后几行
            with open('/tmp/chromium_debug.log', 'r') as f:
                lines = f.readlines()
                if lines:
                    logger.error('Chromium 输出（最后10行）:\n' + ''.join(lines[-10:]))
            raise RuntimeError('端口未监听')
        sock.close()
        
        # 连接已有浏览器
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
