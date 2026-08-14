FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/models/huggingface
WORKDIR /app
RUN useradd --create-home --uid 10001 embedding
RUN pip install --no-cache-dir --upgrade \
    pip==26.2.1 \
    setuptools==84.0.0 \
    wheel==0.48.0 \
    jaraco.context==6.1.2
RUN pip install --no-cache-dir --no-deps \
    --index-url https://download.pytorch.org/whl/cpu \
    torch==2.13.0+cpu
RUN pip install --no-cache-dir \
    fastapi==0.141.1 \
    uvicorn==0.52.3 \
    sentence-transformers==5.7.0 \
    transformers==5.15.0 \
    msgpack==1.2.1
RUN find /usr/local/lib/python3.12/site-packages -type d \
    \( -name 'msgpack-1.1.2.dist-info' -o -name 'setuptools-70.3.0.dist-info' \) \
    -prune -exec rm -rf -- {} + \
    && pip check
COPY infra/embedding_service.py /app/embedding_service.py
USER embedding
CMD ["python", "-m", "uvicorn", "embedding_service:app", "--host", "0.0.0.0", "--port", "8000"]
