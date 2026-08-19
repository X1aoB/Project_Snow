FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN useradd --create-home --uid 10001 snow \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-public.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-public.txt
COPY backend ./backend
COPY config/public_knowledge ./config/public_knowledge
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY public_frontend ./public_frontend
COPY frontend/shared ./frontend/shared
COPY frontend/assets/immersive ./frontend/assets/immersive
COPY scripts/fingerprint_public_frontend.py ./scripts/fingerprint_public_frontend.py
RUN python ./scripts/fingerprint_public_frontend.py --app-root /app
COPY infra/public_smoke.py ./public_smoke.py
COPY infra/public-entrypoint.sh ./infra/public-entrypoint.sh
RUN chmod 0755 ./infra/public-entrypoint.sh
USER root
ENTRYPOINT ["/app/infra/public-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.snow_app.public_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--no-access-log"]
