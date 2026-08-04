"""Run the first 40-question dialogue harness without touching canonical data.

Use ``--dry-run`` to inspect retrieval context and exact prompts.  Without it,
the script calls the configured MVP OpenAI-compatible endpoint and stores model
responses, citations and usage in ``App/runtime/mvp/tests``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from backend.snow_app.config import Settings
from backend.snow_app.mvp_policy import source_layer
from backend.snow_app.mvp_service import MVPService, MVPError
from backend.snow_app.repository import RuntimeRepository

from .common import RUNTIME_ROOT, ensure_runtime, utc_now, write_json, write_jsonl


def _question_rows(service: MVPService) -> list[dict[str, Any]]:
    bundle = service._question_bundle()  # noqa: SLF001 - this is an internal test harness
    return list(bundle.get("questions", []))


def run_tests(
    run_name: str = "first-40",
    limit: int = 40,
    dry_run: bool = False,
    no_vector: bool = False,
    character_id: str | None = None,
) -> dict[str, Any]:
    settings = Settings.from_environment()
    repository = RuntimeRepository(settings)
    if no_vector:
        repository.vector_search = lambda query, limit=40: []  # type: ignore[method-assign]
    service = MVPService(settings, repository)
    questions = _question_rows(service)
    if character_id:
        questions = [question for question in questions if question.get("character_id") == character_id]
    questions = questions[: max(1, min(limit, len(questions)))]
    output_dir = ensure_runtime("mvp", "tests")
    output_path = output_dir / f"{run_name}.jsonl"
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for ordinal, question in enumerate(questions, start=1):
        character = question["character_id"]
        message = question["text"]
        base = {
            "test_id": f"{run_name}-{ordinal:03d}",
            "ordinal": ordinal,
            "question_id": question["question_id"],
            "character_id": character,
            "character_name": question["character_name"],
            "category": question["category"],
            "question": message,
            "started_at": utc_now(),
        }
        try:
            context = service.retrieve(character, message, limit=8)
            prompt = service._prompt(  # noqa: SLF001 - exact prompt inspection is the purpose of dry-run
                context["character"], message, context
            )
            result = {
                **base,
                "status": "prepared" if dry_run else "generated",
                "retrieval": {
                    "fusion": context["fusion"],
                    "vector_available": context["vector_available"],
                    "hits": [
                        {
                            "document_id": hit["citation"]["document_id"],
                            "page_id": hit["citation"]["page_id"],
                            "title": hit["citation"]["title"],
                            "source_type": hit["citation"]["source_type"],
                            "narrative_scope": source_layer(
                                hit["citation"]["source_type"],
                                bool((hit.get("metadata") or {}).get("requires_costume_context")),
                            ),
                            "excerpt": str(hit.get("text", ""))[:700],
                        }
                        for hit in context["hits"]
                    ],
                    "provisional_relation_ids": [
                        item.get("candidate_id") for item in context["provisional_relations"]
                    ],
                },
                "prompt": prompt if dry_run else None,
            }
            if not dry_run:
                response = service.chat(character, message, session_id=f"mvp-test-{run_name}", limit=8)
                result.update(
                    {
                        "response": response,
                        "prompt": None,
                    }
                )
            rows.append(result)
        except (MVPError, KeyError, FileNotFoundError, ValueError) as exc:
            failure = {**base, "status": "failed", "error": str(exc)}
            rows.append(failure)
            failures.append(failure)

    write_jsonl(output_path, rows)
    report = {
        "stage": "MVP",
        "job": "run_mvp_dialogue_tests",
        "run_name": run_name,
        "generated_at": utc_now(),
        "dry_run": dry_run,
        "no_vector": no_vector,
        "requested": limit,
        "questions": len(questions),
        "successful": len(rows) - len(failures),
        "failed": len(failures),
        "characters": sorted({question["character_id"] for question in questions}),
        "output": str(output_path),
        "failures": failures,
        "policy": "测试输出隔离在 runtime/mvp/tests，不改变关系候选、persona 或正式图谱。",
    }
    write_json(RUNTIME_ROOT / "reports" / f"run_mvp_dialogue_tests_{run_name}.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", default="first-40")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--character-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-vector", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run_tests(args.run_name, args.limit, args.dry_run, args.no_vector, args.character_id),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
