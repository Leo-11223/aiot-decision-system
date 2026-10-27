# AIoT 決策閉環：一個容器同時提供前端網頁與後端 API
#
# 本機測試（需安裝 Docker Desktop）：
#   docker build -t aiot .
#   docker run -p 8000:8000 aiot
#   瀏覽器開 http://localhost:8000

# ---------- 第 1 階段：打包前端 ----------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- 第 2 階段：後端＋預先訓練模型 ----------
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 PORT=8000
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY aiot/ aiot/
COPY backend/ backend/
COPY data/ data/
COPY run.py .
COPY --from=web /web/dist frontend/dist

# 打包時就把模型訓練好存檔，雲端主機啟動時只需讀檔
RUN python -m backend.warm

EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
