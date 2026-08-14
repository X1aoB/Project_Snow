"""Complete unresolved review candidates with DeepSeek V4 Pro."""

from __future__ import annotations

import argparse
import json

from backend.snow_app.config import Settings
from backend.snow_app.deepseek_review_completion import DeepSeekReviewCompletionService
from backend.snow_app.repository import RuntimeRepository


def _service() -> DeepSeekReviewCompletionService:
    settings = Settings.from_environment()
    return DeepSeekReviewCompletionService(settings, RuntimeRepository(settings))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("estimate")
    create = commands.add_parser("create")
    create.add_argument("--selection-hash")
    create.add_argument("--confirm-submit", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("run_id")
    run.add_argument("--concurrency", type=int, default=12)
    status = commands.add_parser("status")
    status.add_argument("run_id")
    admit = commands.add_parser("admit")
    admit.add_argument("run_id")
    admit.add_argument("--confirm-apply", action="store_true")
    rollback = commands.add_parser("rollback")
    rollback.add_argument("run_id")
    rollback.add_argument("--confirm-rollback", action="store_true")
    commands.add_parser("list")
    args = parser.parse_args()
    service = _service()
    if args.command == "estimate":
        result = service.estimate()
    elif args.command == "create":
        if not args.confirm_submit:
            raise SystemExit("Refusing provider submission without --confirm-submit.")
        result = service.create_run(args.selection_hash)
    elif args.command == "run":
        result = service.run(args.run_id, args.concurrency)
    elif args.command == "status":
        result = service.get_run(args.run_id)
    elif args.command == "admit":
        if not args.confirm_apply:
            raise SystemExit("Refusing automatic admission without --confirm-apply.")
        result = service.admit(args.run_id)
    elif args.command == "rollback":
        if not args.confirm_rollback:
            raise SystemExit("Refusing rollback without --confirm-rollback.")
        result = service.rollback(args.run_id)
    else:
        result = {"runs": service.list_runs()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
