"""Build the B-stage manifest-backed document lakehouse.

This job intentionally discovers corpus pages only through specialized index
manifests. It never walks Data/Source recursively.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
from typing import Any

from .common import RUNTIME_ROOT, ensure_runtime, iter_corpus_documents, utc_now, write_json, write_jsonl


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def build_lakehouse() -> dict[str, Any]:
    output = ensure_runtime("lakehouse")
    documents_path = output / "documents.jsonl"
    documents = list(iter_corpus_documents())
    if not documents:
        raise RuntimeError("No active corpus documents were built. Check Data/Manifest and source paths.")
    document_count = write_jsonl(documents_path, documents)

    per_source = collections.Counter(document["source_type"] for document in documents)
    per_character = collections.Counter(
        document["metadata"].get("character_id")
        for document in documents
        if document["metadata"].get("character_id")
    )
    source_registry = [
        {
            "source_type": source_type,
            "document_count": count,
            "source_priority": max(
                document["metadata"]["source_priority"]
                for document in documents
                if document["source_type"] == source_type
            ),
        }
        for source_type, count in sorted(per_source.items())
    ]
    write_jsonl(output / "source_registry.jsonl", source_registry)

    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - checked in installation docs
        raise RuntimeError("duckdb is required. Install App/requirements.txt before building the lakehouse.") from exc

    duckdb_root = ensure_runtime("duckdb")
    database_path = duckdb_root / "project_snow.duckdb"
    parquet_path = output / "documents.parquet"
    registry_path = output / "source_registry.parquet"
    connection = duckdb.connect(str(database_path))
    try:
        connection.execute("DROP TABLE IF EXISTS documents")
        connection.execute("DROP TABLE IF EXISTS source_registry")
        connection.execute(
            "CREATE TABLE documents AS SELECT * FROM read_json_auto(?)",
            [str(documents_path.resolve())],
        )
        connection.execute(
            "CREATE TABLE source_registry AS SELECT * FROM read_json_auto(?)",
            [str((output / "source_registry.jsonl").resolve())],
        )
        connection.execute("CREATE INDEX documents_id_idx ON documents(document_id)")
        connection.execute("CREATE INDEX documents_page_idx ON documents(page_id)")
        connection.execute(
            f"COPY documents TO '{_sql_path(parquet_path)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        connection.execute(
            f"COPY source_registry TO '{_sql_path(registry_path)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()

    report = {
        "stage": "B",
        "job": "build_lakehouse",
        "generated_at": utc_now(),
        "documents": document_count,
        "source_types": dict(sorted(per_source.items())),
        "characters_with_direct_documents": len(per_character),
        "outputs": {
            "documents_jsonl": str(documents_path),
            "documents_parquet": str(parquet_path),
            "duckdb": str(database_path),
        },
        "source_root": "Data/Manifest/*_index.jsonl",
        "data_write_policy": "Data/ was not modified.",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_lakehouse.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(build_lakehouse(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
