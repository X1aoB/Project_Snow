FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217 AS system-base

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

FROM system-base AS builder

ARG EMBEDDING_MODEL_ID=BAAI/bge-small-zh-v1.5
ARG EMBEDDING_MODEL_REVISION=7999e1d3359715c523056ef9478215996d62a620

RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    HF_HOME=/tmp/huggingface
RUN pip install --no-cache-dir --upgrade \
        pip==26.2.1 \
        setuptools==84.0.0 \
        wheel==0.48.0 \
        jaraco.context==6.1.2 \
    && pip install --no-cache-dir --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        torch==2.13.0+cpu \
    && pip install --no-cache-dir \
        fastapi==0.141.1 \
        uvicorn==0.52.3 \
        sentence-transformers==5.7.0 \
        transformers==5.15.0 \
        msgpack==1.2.1 \
    && pip check \
    && EMBEDDING_MODEL_ID="$EMBEDDING_MODEL_ID" \
       EMBEDDING_MODEL_REVISION="$EMBEDDING_MODEL_REVISION" \
       python -c "import os; from sentence_transformers import SentenceTransformer; model = SentenceTransformer(os.environ['EMBEDDING_MODEL_ID'], revision=os.environ['EMBEDDING_MODEL_REVISION']); vector = model.encode(['Project Snow 离线模型构建检查'], normalize_embeddings=True, show_progress_bar=False); assert vector.shape == (1, 512), vector.shape; model.save_pretrained('/models/bge-small-zh-v1.5')" \
    && rm -rf /tmp/huggingface \
    && mkdir -p /models/huggingface \
    && rm -rf \
        /opt/venv/bin/pip \
        /opt/venv/bin/pip3 \
        /opt/venv/bin/pip3.12 \
        /opt/venv/bin/wheel \
        /opt/venv/lib/python3.12/site-packages/pip \
        /opt/venv/lib/python3.12/site-packages/pip-*.dist-info \
        /opt/venv/lib/python3.12/site-packages/setuptools \
        /opt/venv/lib/python3.12/site-packages/setuptools-*.dist-info \
        /opt/venv/lib/python3.12/site-packages/wheel \
        /opt/venv/lib/python3.12/site-packages/wheel-*.dist-info

FROM system-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    EMBEDDING_MODEL=/models/bge-small-zh-v1.5 \
    EMBEDDING_DIMENSION=512 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN useradd --create-home --uid 10001 embedding
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=10001:10001 /models /models
COPY infra/embedding_service.py /app/embedding_service.py
USER embedding
CMD ["python", "-m", "uvicorn", "embedding_service:app", "--host", "0.0.0.0", "--port", "8000"]
