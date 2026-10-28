FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 APP_ENV=production
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
# DATABASE_URL (hosted Postgres), JWT_SECRET, ENCRYPTION_KEY and the Google sign-in client come from the environment.
# Migrations run on startup. One worker only: the orchestrator and the rate limiter are in-process.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
