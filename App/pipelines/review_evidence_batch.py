"""Operate the auditable Qwen3.8-Max Batch evidence-review workflow.

`estimate` and `status` are read-only.  `create` uploads evidence to the
configured DashScope account and therefore requires an explicit confirmation
flag.  Admission and rollback are separate explicit operations.
"""

from __future__ import annotations

import argparse
import json

from backend.snow_app.config import Settings
from backend.snow_app.repository import RuntimeRepository
from backend.snow_app.review_automation import ReviewAutomationService


def _service() -> ReviewAutomationService:
    settings = Settings.from_environment()
    repository = RuntimeRepository(settings)
    return ReviewAutomationService(settings, repository)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    estimate = commands.add_parser("estimate", help="Calculate calls, tokens, and cost without writing or calling a provider.")
    estimate.add_argument("--mode", choices=("test", "calibration", "production"), default="production")

    create = commands.add_parser("create", help="Upload a JSONL file and create the first Batch phase.")
    create.add_argument("--mode", choices=("test", "calibration", "production"), required=True)
    create.add_argument("--estimate-hash", required=True)
    create.add_argument("--calibration-run-id")
    create.add_argument("--confirm-submit", action="store_true")

    status = commands.add_parser("status", help="Read a local run manifest.")
    status.add_argument("run_id")
    sync = commands.add_parser("sync", help="Synchronise one provider phase and advance the state machine.")
    sync.add_argument("run_id")
    admit = commands.add_parser("admit", help="Apply calibrated machine decisions to review and graph artifacts.")
    admit.add_argument("run_id")
    admit.add_argument("--confirm-apply", action="store_true")
    rollback = commands.add_parser("rollback", help="Rollback only artifacts created by one admitted run.")
    rollback.add_argument("run_id")
    rollback.add_argument("--confirm-rollback", action="store_true")
    commands.add_parser("list", help="List local automation runs.")
    return parser


def main() -> None:
    args = _parser().parse_args()
    service = _service()
    if args.command == "estimate":
        result = service.estimate(args.mode)
    elif args.command == "create":
        if not args.confirm_submit:
            raise SystemExit("Refusing provider submission without --confirm-submit.")
        result = service.create_run(args.mode, args.estimate_hash, args.calibration_run_id)
    elif args.command == "status":
        result = service.get_run(args.run_id)
    elif args.command == "sync":
        result = service.sync_run(args.run_id)
    elif args.command == "admit":
        if not args.confirm_apply:
            raise SystemExit("Refusing automatic admission without --confirm-apply.")
        result = service.admit_run(args.run_id)
    elif args.command == "rollback":
        if not args.confirm_rollback:
            raise SystemExit("Refusing rollback without --confirm-rollback.")
        result = service.rollback_run(args.run_id)
    else:
        result = {"runs": service.list_runs()}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
