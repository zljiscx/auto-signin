# debug_worker.py（最终版）
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
_socat_proc = None
_lock = threading.Lock()

def start_debug_session(sign_url):
    global _current_page, _socat_proc
    with _lock:
        # 清理旧进程
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None
        if _socat_proc:
            try:
                _socat_proc.terminate()
            except:
                pass
            _socat_proc = None

        os.makedirs(USER_DATA_DIR, exist_ok=True)

        # 1. 启动 Chromium（绑定到 127.0.0.1）
        cmd = [
            '/usr/bin/chromium',
            '--headless=new',
            f'--remote-debugging-port={DEBUG_PORT}',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--disable-software-rasterizer',
            '--disable-features=IsolateOrigins,site-per-process',
            '--enable-unsafe-swiftshader',
            f'--user-data-dir={USER_DATA_DIR}',
            sign_url
        ]
        logger.info('启动 Chromium: ' + ' '.join(cmd))
        with open('/tmp/chromium.log', 'w') as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=f)
        
        # 等待浏览器启动
        time.sleep(5)
        
        # 检查进程是否存活
        if proc.poll() is not None:
            logger.error(f'Chromium 进程退出，返回码: {proc.returncode}')
            with open('/tmp/chromium.log', 'r') as f:
                logger.error(f'Chromium 错误输出:\n{f.read()}')
            raise RuntimeError('Chromium 启动失败')
        
        # 2. 启动 socat 转发（将 127.0.0.1:9222 转发到 0.0.0.0:9222）
        # 注意：需要容器内已安装 socat
        socat_cmd = [
            'socat',
            f'TCP-LISTEN:{DEBUG_PORT},fork,reuseaddr',
            f'TCP:127.0.0.1:{DEBUG_PORT}'
        ]
        logger.info('启动 socat 转发: ' + ' '.join(socat_cmd))
        _socat_proc = subprocess.Popen(socat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)  # 等待 socat 启动
        
        # 3. 验证端口是否可访问（通过 0.0.0.0 或 127.0.0.1）
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', DEBUG_PORT))
        sock.close()
        if result != 0:
            raise RuntimeError(f'端口 {DEBUG_PORT} 未监听')
        
        # 4. 连接浏览器（DrissionPage 连接 127.0.0.1）
        try:
            page = ChromiumPage(addr_or_opts=f'127.0.0.1:{DEBUG_PORT}')
            _current_page = page
            logger.info(f'调试浏览器已启动，访问 {sign_url}')
            # 守护线程保持存活
            def keep_alive():
                while True:
                    time.sleep(10)
            threading.Thread(target=keep_alive, daemon=True).start()
        except Exception as e:
            logger.error(f'连接调试浏览器失败: {e}')
            raise

def stop_debug_session():
    global _current_page, _socat_proc
    with _lock:
        if _current_page:
            try:
                _current_page.close()
            except:
                pass
            _current_page = None
        if _socat_proc:
            try:
                _socat_proc.terminate()
            except:
                pass
            _socat_proc = None
        logger.info('调试会话已关闭')
