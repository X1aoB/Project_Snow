FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/models/huggingface
WORKDIR /app
RUN useradd --create-home --uid 10001 embedding
RUN pip install --no-cache-dir fastapi==0.116.1 uvicorn==0.35.0 sentence-transformers==5.1.0
COPY infra/embedding_service.py /app/embedding_service.py
USER embedding
CMD ["python", "-m", "uvicorn", "embedding_service:app", "--host", "0.0.0.0", "--port", "8000"]
