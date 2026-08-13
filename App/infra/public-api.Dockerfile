FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
RUN useradd --create-home --uid 10001 snow
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY public_frontend ./public_frontend
COPY infra/public_smoke.py ./public_smoke.py
USER snow
CMD ["python", "-m", "uvicorn", "backend.snow_app.public_main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
