FROM python:3.10-slim

# 安装 Chromium 和依赖
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    socat \
    fonts-liberation \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 创建数据目录（权限）
RUN mkdir -p /app/data && chmod 755 /app/data
RUN mkdir -p /app/chrome_data && chmod 755 /app/chrome_data

# 暴露端口
EXPOSE 56789

# 设置时区
ENV TZ=Asia/Shanghai

# 启动命令
CMD ["python", "app.py"]
