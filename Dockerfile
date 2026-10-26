FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 APP_ENV=production DATABASE_URL=sqlite+aiosqlite:////data/growthos.db
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# /data must be a persistent volume. The app creates the directory and runs migrations on startup.
VOLUME /data
EXPOSE 8000
# One worker only: the orchestrator and rate limiter are in-process and SQLite has a single writer.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
