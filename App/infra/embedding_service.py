from __future__ import annotations

import os
from contextlib import asynccontextmanager
from threading import Lock

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


_model = None
_lock = Lock()
_expected_dimension = int(os.getenv("EMBEDDING_DIMENSION", "512"))


class EmbedRequest(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=32)


def model():
    global _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            candidate = SentenceTransformer(
                os.getenv("EMBEDDING_MODEL", "/models/bge-small-zh-v1.5")
            )
            dimension = candidate.get_sentence_embedding_dimension()
            if dimension != _expected_dimension:
                raise RuntimeError(
                    f"embedding dimension mismatch: expected {_expected_dimension}, got {dimension}"
                )
            _model = candidate
    return _model


@asynccontextmanager
async def lifespan(_: FastAPI):
    model()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str | int]:
    if _model is None:
        raise HTTPException(status_code=503, detail={"status": "loading"})
    return {"status": "ok", "dimension": _expected_dimension}


@app.post("/embed")
def embed(request: EmbedRequest) -> dict[str, list[list[float]]]:
    vectors = model().encode(
        [value[:4000] for value in request.inputs],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return {"vectors": vectors.tolist()}
