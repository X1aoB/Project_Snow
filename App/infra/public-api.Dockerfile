FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
ARG APP_REVISION=""
ARG APP_BUILD_TIME=""
ENV APP_REVISION=${APP_REVISION} APP_BUILD_TIME=${APP_BUILD_TIME}
WORKDIR /app
RUN useradd --create-home --uid 10001 snow \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-public.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-public.txt
COPY backend ./backend
COPY config/public_knowledge ./config/public_knowledge
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY .build/public-ui/public_frontend ./public_frontend
COPY .build/public-ui/frontend/shared ./frontend/shared
COPY .build/public-ui/frontend/assets/immersive ./frontend/assets/immersive
COPY .build/public-ui/frontend-identity.json .build/public-ui/frontend-bundle-manifest.json ./
COPY scripts/prepare_public_frontend.py ./scripts/prepare_public_frontend.py
RUN python ./scripts/prepare_public_frontend.py --verify --output /app
COPY infra/public_smoke.py ./public_smoke.py
COPY infra/public-entrypoint.sh ./infra/public-entrypoint.sh
RUN chmod 0755 ./infra/public-entrypoint.sh
USER root
ENTRYPOINT ["/app/infra/public-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.snow_app.public_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--no-access-log"]
