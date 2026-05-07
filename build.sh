#!/usr/bin/env bash
set -e

pip install -r requirements.txt

# Chrome 확장 zip을 extension/ 현재 내용으로 재빌드 — 다운로드 링크와 소스가 어긋나는 사고 방지
# Render의 Debian 이미지에는 zip 명령이 없을 수 있어 Python zipfile 사용
if [ -d extension ]; then
  python -c "
import os, zipfile
src = 'extension'; dst = 'static/geo-audit-extension.zip'
os.makedirs('static', exist_ok=True)
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for f in files:
            if f == '.DS_Store': continue
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, src))
print(f'[build] extension zip rebuilt: {dst}')
"
fi

# Chromium 시스템 의존성 수동 설치 (Render 등 Debian 계열)
if command -v apt-get &> /dev/null; then
  apt-get update -qq
  apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libxshmfence1 libx11-xcb1 \
    libxfixes3 fonts-noto-cjk \
    2>/dev/null || echo "Warning: some apt packages failed, continuing..."
fi

# Playwright Chromium 설치
python -m playwright install chromium --with-deps 2>/dev/null \
  || python -m playwright install chromium \
  || { echo "ERROR: Playwright Chromium 설치 실패"; exit 1; }
