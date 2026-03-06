FROM python:3.12-slim

# 시스템 패키지: Playwright 브라우저 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    ffmpeg openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 의존성
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright Chromium 설치
RUN playwright install chromium

# 앱 복사
COPY server.py .
COPY downloader/ downloader/
COPY templates/ templates/

# downloads 디렉토리 (볼륨 마운트 포인트)
RUN mkdir -p /app/downloads

EXPOSE 5000

CMD ["gunicorn", "server:app", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "0"]
