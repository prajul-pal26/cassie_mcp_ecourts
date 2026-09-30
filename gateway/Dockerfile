# cassie-gateway (FastAPI build) — single-process ASGI image.
#
# IMPORTANT: run exactly ONE worker. The rate bucket, circuit breaker,
# in-memory cache coordination, and job executor are per-process singletons
# (see app/core/runtime.py). Multiple workers would each build their own
# state and defeat the shared rate cap — same constraint as the Flask build.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8000 \
    ECOURTS_DATA_DIR=/data

WORKDIR /srv

# ffmpeg: ITAT solves the leaked audio captcha by decoding MP3 via ffmpeg.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persistent volume for the SQLite cache + job store.
VOLUME ["/data"]
EXPOSE 8000

# Single worker — see note above.
# --proxy-headers + --forwarded-allow-ips: honor Fly's X-Forwarded-Proto so
# request.base_url is https (ITAT order-PDF links are built from it).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
