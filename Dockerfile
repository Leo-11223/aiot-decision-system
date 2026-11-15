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
# 雲端主機偵測不到實體核心數：固定執行緒數，避免梯度提升模型開過多執行緒反而極慢
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 LOKY_MAX_CPU_COUNT=1
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

# 以一般使用者執行（不用 root）；outputs 需可寫入（資料庫、成效報表）
RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
