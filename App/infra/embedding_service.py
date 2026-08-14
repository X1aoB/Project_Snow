from __future__ import annotations

import os
from threading import Lock

from fastapi import FastAPI
from pydantic import BaseModel, Field


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_model = None
_lock = Lock()


class EmbedRequest(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=32)


def model():
    global _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    return _model


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/embed")
def embed(request: EmbedRequest) -> dict[str, list[list[float]]]:
    vectors = model().encode(
        [value[:4000] for value in request.inputs],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return {"vectors": vectors.tolist()}
