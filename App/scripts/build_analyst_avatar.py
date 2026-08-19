"""Fetch the approved Wiki analyst portrait for the private media builder."""

from __future__ import annotations

import argparse
from io import BytesIO
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
from PIL import Image


APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = APP_ROOT / "config" / "public_media" / "analyst_avatar.json"
DEFAULT_OUTPUT_ROOT = APP_ROOT / "frontend" / "assets" / "analyst"


def _optimized_png(content: bytes, *, max_dimension: int = 512) -> tuple[bytes, tuple[int, int]]:
    with Image.open(BytesIO(content)) as image:
        image.seek(0)
        normalized = image.convert("RGBA" if image.mode in {"RGBA", "LA", "P"} else "RGB")
        normalized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output = BytesIO()
        normalized.save(output, format="PNG", optimize=True)
        return output.getvalue(), normalized.size


def build(*, config_path: Path, output_root: Path, timeout: float = 30.0) -> dict[str, Any]:
    metadata = json.loads(config_path.read_text(encoding="utf-8"))
    source_url = str(metadata.get("source_url") or "").strip()
    if not source_url.startswith("https://patchwiki.biligame.com/images/sonw/"):
        raise ValueError("analyst source URL must point to the approved Wiki image host")
    output_root.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Project-Snow-Analyst-Avatar-Builder/0.9.1"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        response = client.get(source_url)
        response.raise_for_status()
        if not response.headers.get("content-type", "").casefold().startswith("image/"):
            raise ValueError("analyst source response is not an image")
        original = response.content
    if len(original) > 8 * 1024 * 1024:
        raise ValueError("analyst source image exceeds 8 MiB")
    optimized, dimensions = _optimized_png(original)
    destination = output_root / "analyst-default.png"
    destination.write_bytes(optimized)
    license_status = str(metadata.get("license_status") or "").casefold().strip()
    license_verified = license_status in {
        "verified",
        "verified_explicit",
        "verified_site_policy_no_page_exception",
    }
    result = {
        **metadata,
        "local_path": str(destination.relative_to(APP_ROOT).as_posix()),
        "content_hash": sha256(original).hexdigest(),
        "content_length": len(optimized),
        "width": dimensions[0],
        "height": dimensions[1],
        "publishable": license_verified,
    }
    (output_root / "analyst.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    result = build(
        config_path=args.config.resolve(),
        output_root=args.output_root.resolve(),
        timeout=args.timeout,
    )
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
