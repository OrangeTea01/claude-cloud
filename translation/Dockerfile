# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRANSLATION_MODELS_DIR=/models \
    TRANSLATION_THREADS=2
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY translation/requirements.txt /app/translation/requirements.txt
RUN pip install --no-cache-dir -r /app/translation/requirements.txt
COPY translation/ /app/translation/
RUN python -m translation.download_models
# Fail the image build if inference tries to fetch anything at first use.
RUN --network=none python -m translation.smoke
RUN useradd --uid 10001 --create-home translator
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "translation.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-proxy-headers", "--no-access-log"]
