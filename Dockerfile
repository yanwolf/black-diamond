# ---------- Stage 1: 打包前端 ----------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json ./
RUN npm install --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: 後端 + 靜態檔 ----------
FROM python:3.11-slim
WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend-build /app/frontend/dist ./static

# Zeabur 會自動注入 PORT 環境變數
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
