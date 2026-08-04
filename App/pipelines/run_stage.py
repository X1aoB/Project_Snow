"""Run B or C pipeline groups with explicit, resumable stage boundaries."""

from __future__ import annotations

import argparse
import json

from .build_graph import build_graph, build_relation_review_jobs
from .build_lakehouse import build_lakehouse
from .build_lexical_index import build_lexical_index
from .build_personas import build_personas
from .build_dialogue_profiles import build_dialogue_profiles
from .build_vector_index import build_vector_index


def run(stage: str, skip_vector: bool) -> dict:
    if stage == "b":
        result = {
            "lakehouse": build_lakehouse(),
            "lexical_index": build_lexical_index(),
            "personas": build_personas(),
            "dialogue_profiles": build_dialogue_profiles(),
        }
        if not skip_vector:
            result["vector_index"] = build_vector_index("BAAI/bge-small-zh-v1.5", 16)
        return result
    if stage == "c":
        return {"graph": build_graph(), "relation_jobs": build_relation_review_jobs()}
    raise ValueError(f"Unsupported stage: {stage}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("b", "c"), required=True)
    parser.add_argument("--skip-vector", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.stage, args.skip_vector), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
