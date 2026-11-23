# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM docker:28-cli AS dockercli

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    STATIC_DIR=/app/frontend/dist
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir -p /data
COPY backend/ ./backend/
COPY deploy/login_browser/seccomp.json /app/deploy/login_browser/seccomp.json
COPY deploy/login_browser/LICENSE.playwright deploy/login_browser/THIRD_PARTY.md /app/deploy/login_browser/
COPY --from=frontend /build/dist ./frontend/dist
EXPOSE 8000
# A single worker owns PTYs and session state; do not add --workers > 1.
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers", "--ws-max-size", "1048576", "--timeout-graceful-shutdown", "30"]

FROM runtime AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
COPY translation/ ./translation/
COPY scripts/ ./scripts/
RUN python -m unittest discover -s tests -v

FROM runtime AS production
