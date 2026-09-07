# 基础镜像：Python 3.11 精简版（Debian 12 Bookworm，兼容 x86/ARM 架构）
FROM python:3.11-slim-bookworm

# Python 环境优化：禁用字节码、关闭输出缓冲
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 1. 安装系统依赖：Chromium 浏览器 + 中文字体 + 时区
#    （Python 依赖均有官方预编译wheel，无需编译工具链，可显著减小镜像体积）
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    fonts-wqy-zenhei \
    tzdata \
    ca-certificates \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    # 清理缓存，减小镜像体积
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. 指定 Chrome 路径（DrissionPage 自动识别，显式声明更稳妥）
ENV CHROMIUM_PATH=/usr/bin/chromium

# 3. 创建工作目录
WORKDIR /app

# 4. 先复制依赖文件，利用 Docker 缓存加速构建（gunicorn 已含在 requirements.txt 中）
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 5. 复制项目全部代码
COPY . .

# 6. 创建数据目录（挂载宿主机后会覆盖，提前创建避免权限问题）
RUN mkdir -p /app/data /app/debug

# 7. 暴露服务端口
EXPOSE 56789

# 8. 启动命令：单 worker gunicorn（保证调度器唯一，不重复执行）
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:56789", "--timeout", "300", "app:app"]