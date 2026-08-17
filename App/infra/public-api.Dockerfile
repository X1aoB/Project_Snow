FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN useradd --create-home --uid 10001 snow \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-public.txt ./
RUN pip install --no-cache-dir -r requirements-public.txt
COPY backend ./backend
COPY config/public_knowledge ./config/public_knowledge
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY public_frontend ./public_frontend
COPY frontend/shared ./frontend/shared
COPY frontend/assets/immersive ./frontend/assets/immersive
COPY infra/public_smoke.py ./public_smoke.py
COPY infra/public-entrypoint.sh ./infra/public-entrypoint.sh
RUN chmod 0755 ./infra/public-entrypoint.sh
USER root
ENTRYPOINT ["/app/infra/public-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.snow_app.public_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
