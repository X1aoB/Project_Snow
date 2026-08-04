"""Build a local Chinese embedding index and optionally project it to Qdrant."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from .common import RUNTIME_ROOT, ensure_runtime, load_runtime_jsonl, read_jsonl, utc_now, write_json, write_jsonl


def _reusable_vectors(vector_path, model_name: str) -> dict[str, list[float]]:
    """Reuse vectors only when the previous local index used the same model."""
    report_path = RUNTIME_ROOT / "reports" / "build_vector_index.json"
    if not vector_path.exists() or not report_path.exists():
        return {}
    try:
        previous_report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if previous_report.get("model") != model_name:
        return {}
    return {
        row["document_id"]: row["vector"]
        for row in read_jsonl(vector_path)
        if row.get("document_id") and isinstance(row.get("vector"), list)
    }


def build_vector_index(model_name: str, batch_size: int, push_qdrant: bool = False) -> dict[str, Any]:
    documents = load_runtime_jsonl("documents.jsonl")
    if not documents:
        raise RuntimeError("Lakehouse documents are missing. Run build_lakehouse first.")
    output = ensure_runtime("vectors")
    vector_path = output / "local_vectors.jsonl"
    vectors_by_id = _reusable_vectors(vector_path, model_name)
    missing_documents = [document for document in documents if document["document_id"] not in vectors_by_id]
    if missing_documents:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("sentence-transformers is required. Install App/requirements.txt before building missing vectors.") from exc
        model = SentenceTransformer(model_name)
        vectors = model.encode(
            [document["text"] for document in missing_documents],
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=True,
        )
        vectors_by_id.update(
            {
                document["document_id"]: vector.tolist()
                for document, vector in zip(missing_documents, vectors, strict=True)
            }
        )
    vector_rows = [
        {"document_id": document["document_id"], "vector": vectors_by_id[document["document_id"]]}
        for document in documents
    ]
    write_jsonl(vector_path, vector_rows)
    dimension = len(vector_rows[0]["vector"]) if vector_rows else 0
    qdrant_result = "not_requested"
    if push_qdrant:
        qdrant_result = _push_to_qdrant(documents, vector_rows, dimension)
    report = {
        "stage": "B",
        "job": "build_vector_index",
        "generated_at": utc_now(),
        "model": model_name,
        "documents": len(documents),
        "reused_documents": len(documents) - len(missing_documents),
        "embedded_documents": len(missing_documents),
        "dimension": dimension,
        "local_index": str(vector_path),
        "qdrant": qdrant_result,
    }
    write_json(RUNTIME_ROOT / "reports" / "build_vector_index.json", report)
    return report


def _push_to_qdrant(documents: list[dict[str, Any]], vectors: list[dict[str, Any]], dimension: int) -> str:
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("qdrant-client is required to push vectors.") from exc
    url = os.getenv("QDRANT_URL", "http://localhost:6333")
    collection = os.getenv("QDRANT_COLLECTION", "project_snow_documents")
    client = QdrantClient(url=url)
    client.recreate_collection(
        collection_name=collection,
        vectors_config=models.VectorParams(size=dimension, distance=models.Distance.COSINE),
    )
    points = []
    for document, vector in zip(documents, vectors, strict=True):
        payload = {
            "document_id": document["document_id"],
            "page_id": document["page_id"],
            "source_type": document["source_type"],
            "title": document["title"],
            "canonical_url": document.get("canonical_url"),
            "metadata": document["metadata"],
        }
        points.append(models.PointStruct(id=document["document_id"], vector=vector["vector"], payload=payload))
    client.upsert(collection_name=collection, points=points, wait=True)
    return f"pushed:{collection}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("EMBEDDING_BATCH_SIZE", "16")))
    parser.add_argument("--push-qdrant", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build_vector_index(args.model, args.batch_size, args.push_qdrant), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
