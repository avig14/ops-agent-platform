# One image: the built React app + FastAPI. Postgres is external (Neon in production, compose locally).
# Build:  docker build -t ops-agent-platform .
# Run:    docker run --env-file .env -e DATABASE_URL=... -p 8000:8000 ops-agent-platform

# ---- 1. build the frontend ----
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 2. the app ----
FROM python:3.11-slim AS app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=prod \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY --from=web /web/dist /app/frontend/dist
RUN useradd --system --no-create-home --uid 10001 appuser && chown -R appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)"
# Migrations and demo seed are idempotent, so every start is safe. Render and similar hosts set $PORT.
CMD ["sh", "-c", "python -m app.migrate && python -m app.seed && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --loop app.loop:selector_loop --proxy-headers --forwarded-allow-ips='*'"]
