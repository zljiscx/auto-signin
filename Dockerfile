FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖和 Chrome（直接下载 .deb 包，避免仓库签名问题）
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    xz-utils \
    && wget -q -O google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y ./google-chrome.deb || apt-get install -f -y \
    && rm google-chrome.deb \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

RUN mkdir -p /app/data

EXPOSE 5678

CMD ["python", "app.py"]
