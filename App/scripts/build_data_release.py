"""Create a versioned, attributable data release manifest without raw Wiki media."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.snow_app.data_release import file_sha256, iter_jsonl, verify_data_release
from backend.snow_app.mvp_policy import MVP_CHARACTERS


PUBLIC_CONTENT_LICENSE = "CC BY-NC-SA 4.0; page-specific notices take precedence"
PUBLIC_LICENSE_URL = "https://creativecommons.org/licenses/by-nc-sa/4.0/"
PUBLIC_LICENSE_SOURCE = (
    "https://wiki.biligame.com/sonw/index.php?title=%E9%A6%96%E9%A1%B5&oldid=21546"
)
PUBLIC_TRANSFORMATIONS = [
    "normalized Wiki markup",
    "cleaned and segmented text",
    "generated retrieval metadata",
    "derived summaries, personas, graph records and embeddings",
]


def file_record(output: Path, destination: Path) -> dict[str, Any]:
    return {
        "path": destination.relative_to(output).as_posix(),
        "bytes": destination.stat().st_size,
        "sha256": file_sha256(destination),
    }


def copy_file(source: Path, destination: Path, output: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return file_record(output, destination)


def _load_source_review(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("data source review must contain a sources list")
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise RuntimeError("data source review contains a non-object row")
        url = str(record.get("canonical_url") or "").strip()
        if not url or url in result:
            raise RuntimeError("data source review contains an empty or duplicate URL")
        result[url] = record
    return result


def publish_documents(
    source: Path,
    destination: Path,
    attribution_path: Path,
    output: Path,
    source_review: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    document_ids: list[str] = []
    attribution: dict[tuple[str, str], dict[str, Any]] = {}
    with destination.open("w", encoding="utf-8", newline="\n") as target:
        for row in iter_jsonl(source):
            document_id = str(row.get("document_id") or "")
            if not document_id:
                raise RuntimeError("document release row is missing document_id")
            document_ids.append(document_id)
            row["source_license"] = PUBLIC_CONTENT_LICENSE
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
            page_id = str(row.get("page_id") or "")
            content_hash = str(row.get("source_content_hash") or "")
            canonical_url = str(row.get("canonical_url") or "").strip()
            review = source_review.get(canonical_url, {})
            revision_id = str(
                review.get("source_revision_id")
                or row.get("source_revision_id")
                or row.get("revision_id")
                or ""
            ).strip()
            revision_timestamp = str(
                review.get("source_revision_timestamp")
                or row.get("source_revision_timestamp")
                or ""
            ).strip()
            if not canonical_url or not revision_id or not revision_timestamp:
                raise RuntimeError(
                    f"document source lacks a verified fixed revision: {canonical_url or page_id}"
                )
            if review and review.get("exception_status") != "no_page_exception_detected":
                raise RuntimeError(f"document source has an unresolved licence exception: {canonical_url}")
            key = (page_id, content_hash)
            attribution.setdefault(
                key,
                {
                    "page_id": page_id,
                    "canonical_url": canonical_url,
                    "fixed_revision_url": review.get("fixed_revision_url"),
                    "attribution": row.get("attribution"),
                    "source_license": PUBLIC_CONTENT_LICENSE,
                    "source_license_url": PUBLIC_LICENSE_URL,
                    "source_license_policy_revision": "21546",
                    "source_license_policy_url": PUBLIC_LICENSE_SOURCE,
                    "source_manifest": row.get("source_manifest"),
                    "source_content_hash": content_hash,
                    "source_local_path": row.get("local_path"),
                    "source_revision_id": revision_id,
                    "source_revision_timestamp": revision_timestamp,
                    "latest_editor": review.get("latest_editor") or "source page history",
                    "contributors_url": (
                        f"{str(row.get('canonical_url') or '')}?action=history"
                        if row.get("canonical_url")
                        else None
                    ),
                    "modifications": PUBLIC_TRANSFORMATIONS,
                },
            )
    if len(document_ids) != len(set(document_ids)):
        raise RuntimeError("document release contains duplicate document_id values")
    with attribution_path.open("w", encoding="utf-8", newline="\n") as target:
        for row in sorted(attribution.values(), key=lambda item: (str(item["page_id"]), str(item["source_content_hash"]))):
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return [file_record(output, destination), file_record(output, attribution_path)], document_ids


def validate_vectors(path: Path, document_ids: list[str]) -> tuple[int, int]:
    vector_ids: list[str] = []
    dimension = 0
    for row in iter_jsonl(path):
        document_id = str(row.get("document_id") or "")
        vector = row.get("vector")
        if not document_id or not isinstance(vector, list) or not vector:
            raise RuntimeError("invalid vector release row")
        if dimension == 0:
            dimension = len(vector)
        if len(vector) != dimension:
            raise RuntimeError("vector release contains inconsistent dimensions")
        vector_ids.append(document_id)
    if vector_ids != document_ids:
        raise RuntimeError("document and vector release order or IDs do not match")
    return len(vector_ids), dimension


def validate_graph(nodes_path: Path, edges_path: Path) -> tuple[int, int]:
    node_ids = [str(row.get("node_id") or "") for row in iter_jsonl(nodes_path)]
    if not all(node_ids) or len(node_ids) != len(set(node_ids)):
        raise RuntimeError("graph release contains missing or duplicate node IDs")
    node_id_set = set(node_ids)
    edge_ids: set[str] = set()
    edge_count = 0
    for row in iter_jsonl(edges_path):
        edge_id = str(row.get("edge_id") or "")
        if not edge_id or edge_id in edge_ids:
            raise RuntimeError("graph release contains missing or duplicate edge IDs")
        if str(row.get("from_id") or "") not in node_id_set or str(row.get("to_id") or "") not in node_id_set:
            raise RuntimeError(f"graph edge has a missing endpoint: {edge_id}")
        edge_ids.add(edge_id)
        edge_count += 1
    return len(node_ids), edge_count


def build_release(
    version: str,
    runtime: Path,
    output_root: Path,
    denylist_path: Path,
    *,
    enforce_disk_guard: bool = True,
    source_review_path: Path | None = None,
) -> dict[str, Any]:
    runtime = runtime.resolve()
    usage = shutil.disk_usage(runtime)
    used_ratio = (usage.total - usage.free) / usage.total
    if usage.free < 12 * 1024**3 or (enforce_disk_guard and used_ratio >= 0.70):
        raise RuntimeError(
            f"refusing data release: disk used={used_ratio:.1%}, free={usage.free / 1024**3:.1f} GiB"
        )
    output = (output_root / version).resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        required = {
            "indexes/lexical.sqlite3": runtime / "indexes" / "lexical.sqlite3",
            "lakehouse/documents.jsonl": runtime / "lakehouse" / "documents.jsonl",
            "vectors/local_vectors.jsonl": runtime / "vectors" / "local_vectors.jsonl",
            "graph/nodes.jsonl": runtime / "release" / "graph" / "nodes.jsonl",
            "graph/edges.jsonl": runtime / "release" / "graph" / "edges.jsonl",
            "personas/persona_profiles.jsonl": runtime / "personas" / "persona_profiles.jsonl",
            "personas/dialogue_style_profiles.jsonl": (
                runtime / "personas" / "dialogue_style_profiles.jsonl"
            ),
            "mvp/character_views.jsonl": runtime / "mvp" / "character_views.jsonl",
            "mvp/question_bank.json": runtime / "mvp" / "question_bank.json",
        }
        missing = [str(path) for path in required.values() if not path.is_file()]
        if missing:
            raise RuntimeError("missing release artifacts: " + ", ".join(missing))

        files: list[dict[str, Any]] = []
        source_review = _load_source_review(source_review_path)
        document_files, document_ids = publish_documents(
            required["lakehouse/documents.jsonl"],
            output / "lakehouse" / "documents.jsonl",
            output / "ATTRIBUTION.jsonl",
            output,
            source_review,
        )
        files.extend(document_files)
        vector_count, vector_dimension = validate_vectors(required["vectors/local_vectors.jsonl"], document_ids)
        node_count, edge_count = validate_graph(required["graph/nodes.jsonl"], required["graph/edges.jsonl"])
        persona_count = sum(1 for _ in iter_jsonl(required["personas/persona_profiles.jsonl"]))
        expected_character_ids = {character.character_id for character in MVP_CHARACTERS}
        view_character_ids = {
            str(row.get("character_id") or "")
            for row in iter_jsonl(required["mvp/character_views.jsonl"])
        }
        dialogue_character_ids = {
            str(row.get("character_id") or "")
            for row in iter_jsonl(required["personas/dialogue_style_profiles.jsonl"])
        }
        try:
            question_payload = json.loads(required["mvp/question_bank.json"].read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid MVP question bank") from exc
        question_character_ids = {
            str(row.get("character_id") or "")
            for row in question_payload.get("questions", [])
            if isinstance(row, dict)
        }
        for label, actual in (
            ("character views", view_character_ids),
            ("dialogue profiles", dialogue_character_ids),
            ("question bank", question_character_ids),
        ):
            if actual != expected_character_ids:
                missing_ids = sorted(expected_character_ids - actual)
                extra_ids = sorted(actual - expected_character_ids)
                raise RuntimeError(
                    f"{label} do not cover the public character registry; "
                    f"missing={missing_ids}, extra={extra_ids}"
                )
        for relative in (
            "indexes/lexical.sqlite3",
            "vectors/local_vectors.jsonl",
            "graph/nodes.jsonl",
            "graph/edges.jsonl",
            "personas/persona_profiles.jsonl",
            "personas/dialogue_style_profiles.jsonl",
            "mvp/character_views.jsonl",
            "mvp/question_bank.json",
        ):
            files.append(copy_file(required[relative], output / relative, output))

        denylist = json.loads(denylist_path.read_text(encoding="utf-8")) if denylist_path.exists() else {}
        license_manifest = {
            "code": {"license": "GPL-3.0-only", "scope": "application source code"},
            "content": {
                "license": "CC BY-NC-SA",
                "version": "4.0",
                "license_url": PUBLIC_LICENSE_URL,
                "policy_source_url": PUBLIC_LICENSE_SOURCE,
                "policy_source_revision_id": "21546",
                "status": "verified_against_bwiki_source_declaration",
                "policy": (
                    "Page-specific notices and denylist entries take precedence; "
                    "the source declaration does not establish ownership of underlying game art."
                ),
                "modifications": PUBLIC_TRANSFORMATIONS,
            },
            "denylist": denylist,
        }
        licenses_path = output / "LICENSES.json"
        licenses_path.write_text(json.dumps(license_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(file_record(output, licenses_path))
        manifest = {
            "schema_version": "project-snow-data-release-3",
            "data_version": version,
            "generated_at": datetime.now(UTC).isoformat(),
            "contains_raw_wiki_media": False,
            "private_candidate": False,
            "public_release_status": "verified_against_bwiki_source_declaration",
            "statistics": {
                "documents": len(document_ids),
                "vectors": vector_count,
                "vector_dimension": vector_dimension,
                "graph_nodes": node_count,
                "graph_edges": edge_count,
                "personas": persona_count,
                "character_views": len(view_character_ids),
                "dialogue_profiles": len(dialogue_character_ids),
            },
            "files": sorted(files, key=lambda item: item["path"]),
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        verify_data_release(output, version)
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--runtime-root", type=Path, default=APP_ROOT / "runtime")
    parser.add_argument("--output-root", type=Path, default=APP_ROOT / "runtime" / "releases")
    parser.add_argument("--denylist", type=Path, default=APP_ROOT / "config" / "content_denylist.json")
    parser.add_argument(
        "--source-review",
        type=Path,
        default=APP_ROOT / "config" / "public_knowledge" / "data_license_review.json",
    )
    parser.add_argument(
        "--allow-high-used-ratio",
        action="store_true",
        help="Allow a release when the volume is over 70%% used; the 12 GiB free-space guard remains active.",
    )
    args = parser.parse_args()
    try:
        manifest = build_release(
            args.version,
            args.runtime_root,
            args.output_root,
            args.denylist,
            enforce_disk_guard=not args.allow_high_used_ratio,
            source_review_path=args.source_review,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
