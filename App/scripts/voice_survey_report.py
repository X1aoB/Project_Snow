"""Print voice-survey counts and shares.  Never prints a comment or a subject.

    python scripts/voice_survey_report.py --database-url "$PUBLIC_DATABASE_URL"
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping

from sqlalchemy import create_engine, text

SINGLE = ("use_voice", "pass_through_acceptance", "payment_method", "monthly_budget", "hosted_interest",
          "account_willingness", "hosted_budget", "history_hosting", "sync_importance", "adult")
MULTI = ("use_scenes", "priorities", "characters", "account_methods", "account_concerns", "hosted_models", "app_platforms")


def summarize(rows: Iterable[Mapping]) -> dict:
    """Aggregate stored rows.  ``rows`` need only ``answers_json`` and ``comment``."""
    respondents = 0
    with_comment = 0
    counts: dict[str, Counter] = {name: Counter() for name in (*SINGLE, *MULTI)}
    for row in rows:
        respondents += 1
        with_comment += bool(str(row["comment"] or "").strip())
        answers = json.loads(row["answers_json"])
        for name in SINGLE:
            counts[name][str(answers.get(name, ""))] += 1
        for name in MULTI:
            counts[name].update(str(value) for value in answers.get(name, []))

    def share(count: int) -> float:
        return round(count / respondents, 4) if respondents else 0.0

    return {
        "respondents": respondents,
        "with_comment": with_comment,
        "questions": {
            name: {value: {"count": count, "share": share(count)} for value, count in counter.most_common()}
            for name, counter in counts.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.getenv("PUBLIC_DATABASE_URL") or os.getenv("DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("provide --database-url or set PUBLIC_DATABASE_URL")
    engine = create_engine(args.database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT answers_json, comment FROM public_voice_survey_response")).mappings().all()
    finally:
        engine.dispose()
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
