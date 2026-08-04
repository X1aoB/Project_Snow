"""Build a portable SQLite FTS5 lexical index for exact lore recall."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .common import RUNTIME_ROOT, ensure_runtime, load_runtime_jsonl, utc_now, write_json


def search_terms(text: str) -> str:
    """Generate CJK bigrams plus ordinary words for FTS5 Unicode tokenization."""
    terms: list[str] = []
    for segment in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
            terms.append(segment)
            terms.extend(segment[index : index + 2] for index in range(len(segment) - 1))
            if len(segment) == 1:
                terms.append(segment)
        else:
            terms.append(segment.lower())
    return " ".join(terms)


def build_lexical_index() -> dict[str, Any]:
    documents = load_runtime_jsonl("documents.jsonl")
    if not documents:
        raise RuntimeError("Lakehouse documents are missing. Run python -m pipelines.build_lakehouse first.")
    index_path = ensure_runtime("indexes") / "lexical.sqlite3"
    connection = sqlite3.connect(index_path)
    try:
        connection.execute("DROP TABLE IF EXISTS documents")
        connection.execute("DROP TABLE IF EXISTS documents_fts")
        connection.execute(
            """
            CREATE TABLE documents (
              document_id TEXT PRIMARY KEY,
              page_id TEXT NOT NULL,
              source_type TEXT NOT NULL,
              title TEXT NOT NULL,
              text TEXT NOT NULL,
              canonical_url TEXT,
              local_path TEXT,
              metadata_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE VIRTUAL TABLE documents_fts USING fts5(document_id UNINDEXED, title, text, terms, tokenize='unicode61')"
        )
        rows = [
            (
                document["document_id"],
                document["page_id"],
                document["source_type"],
                document["title"],
                document["text"],
                document.get("canonical_url"),
                document.get("local_path"),
                json.dumps(document["metadata"], ensure_ascii=False, sort_keys=True),
            )
            for document in documents
        ]
        connection.executemany("INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.executemany(
            "INSERT INTO documents_fts(document_id, title, text, terms) VALUES (?, ?, ?, ?)",
            [
                (document["document_id"], document["title"], document["text"], search_terms(f"{document['title']} {document['text']}"))
                for document in documents
            ],
        )
        connection.execute("CREATE INDEX documents_source_idx ON documents(source_type)")
        connection.execute("CREATE INDEX documents_page_idx ON documents(page_id)")
        connection.commit()
    finally:
        connection.close()
    report = {
        "stage": "B",
        "job": "build_lexical_index",
        "generated_at": utc_now(),
        "documents": len(documents),
        "database": str(index_path),
        "engine": "SQLite FTS5 unicode61",
    }
    write_json(RUNTIME_ROOT / "reports" / "build_lexical_index.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(build_lexical_index(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
