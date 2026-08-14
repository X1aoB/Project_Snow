from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.snow_app.data_release import DataReleaseError, verify_data_release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_root", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    try:
        manifest = verify_data_release(args.release_root, args.expected_version)
    except DataReleaseError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"status": "ok", "data_version": manifest["data_version"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
