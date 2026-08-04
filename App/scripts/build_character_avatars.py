"""Build local chat avatars from already-crawled Wiki character pages."""

from __future__ import annotations

import argparse
from io import BytesIO
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup
import httpx
from PIL import Image


APP_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = APP_ROOT.parent
REGISTRY_PATH = APP_ROOT / "backend" / "snow_app" / "mvp_character_registry.json"
OUTPUT_ROOT = APP_ROOT / "frontend" / "assets" / "characters"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def compact(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value or "").casefold())


def resolve_source_path(value: str) -> Path:
    raw = Path(value)
    if raw.is_absolute():
        return raw
    parts = [part for part in re.split(r"[\\/]+", value) if part not in {"", "."}]
    return PROJECT_ROOT.joinpath(*parts)


def image_score(image: Any, names: set[str]) -> int:
    alt = compact(image.get("alt"))
    if not alt:
        return -100
    if any(term in alt for term in ("皮肤", "巧克力", "表情", "播放", "物品", "武器", "动作")):
        return -80
    score = 0
    if "头像" in alt:
        score += 140
    if "立绘" in alt:
        score += 110
    if any(name and name in alt for name in names):
        score += 70
    width = int(image.get("data-file-width") or image.get("width") or 0)
    height = int(image.get("data-file-height") or image.get("height") or 0)
    if width and height:
        if min(width, height) < 120:
            return -60
        ratio = width / height
        if 0.85 <= ratio <= 1.15:
            score += 35
        elif 0.62 <= ratio <= 0.82:
            score += 20
        if min(width, height) >= 180:
            score += 10
    if "/thumb/" not in str(image.get("src") or ""):
        score += 5
    return score


def select_image(page_path: Path, names: set[str]) -> tuple[str, str] | None:
    if not page_path.exists():
        return None
    soup = BeautifulSoup(page_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    candidates = []
    for position, image in enumerate(soup.select("img[src]")):
        source = str(image.get("src") or "").strip()
        if not source.startswith("https://patchwiki.biligame.com/images/sonw/"):
            continue
        candidates.append((image_score(image, names), -position, source, str(image.get("alt") or "")))
    if not candidates:
        return None
    score, _, source, alt = max(candidates)
    return (source, alt) if score >= 20 else None


def extension_for(content_type: str, source_url: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get(content_type.split(";", 1)[0].strip().casefold()) or Path(source_url.split("?", 1)[0]).suffix or ".img"


def optimized_png(content: bytes, *, max_dimension: int = 512) -> tuple[bytes, tuple[int, int]]:
    with Image.open(BytesIO(content)) as image:
        image.seek(0)
        normalized = image.convert("RGBA" if image.mode in {"RGBA", "LA", "P"} else "RGB")
        normalized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        output = BytesIO()
        normalized.save(output, format="PNG", optimize=True)
        return output.getvalue(), normalized.size


def build(data_root: Path, *, timeout: float = 30.0) -> dict[str, Any]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    profiles = read_jsonl(data_root / "Manifest" / "character_profiles_index.jsonl")
    armors = read_jsonl(data_root / "Manifest" / "character_armors_index.jsonl")
    sources: dict[str, list[dict[str, Any]]] = {}
    for item in [*profiles, *armors]:
        character_id = str(item.get("character_id") or "")
        if character_id and item.get("local_path"):
            sources.setdefault(character_id, []).append(item)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    results = []
    headers = {"User-Agent": "Project-Snow-Local-Avatar-Builder/0.2"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=timeout) as client:
        for character in registry.get("characters", []):
            character_id = str(character["character_id"])
            names = {
                compact(character.get("display_name")),
                compact(character.get("source_name")),
                *(compact(item) for item in character.get("aliases", [])),
            }
            selection = None
            selected_page = None
            for source in sources.get(character_id, []):
                page_path = resolve_source_path(str(source["local_path"]))
                candidate = select_image(page_path, names)
                if candidate:
                    selection = candidate
                    selected_page = source
                    if "头像" in candidate[1]:
                        break
            row = {
                "character_id": character_id,
                "character_name": character["display_name"],
                "local_path": None,
                "source_page": (selected_page or {}).get("canonical_url"),
                "source_url": selection[0] if selection else None,
                "source_alt": selection[1] if selection else None,
                "status": "fallback",
                "license": "CC BY-NC-SA 4.0 unless page-specific notice applies",
            }
            if selection:
                try:
                    response = client.get(selection[0])
                    response.raise_for_status()
                    if not response.headers.get("content-type", "").casefold().startswith("image/"):
                        raise ValueError("response is not an image")
                    content = response.content
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("image exceeds 8 MiB")
                    optimized, dimensions = optimized_png(content)
                    destination = OUTPUT_ROOT / f"{character_id}.png"
                    destination.write_bytes(optimized)
                    row.update(
                        {
                            "local_path": f"/assets/characters/{destination.name}",
                            "status": "downloaded",
                            "content_hash": sha256(content).hexdigest(),
                            "source_content_length": len(content),
                            "content_length": len(optimized),
                            "width": dimensions[0],
                            "height": dimensions[1],
                        }
                    )
                except (httpx.HTTPError, OSError, ValueError, Image.UnidentifiedImageError) as exc:
                    row["error"] = str(exc)
            results.append(row)

    manifest = {
        "schema_version": "project-snow-avatar-1.0",
        "registry_version": registry.get("version"),
        "policy": "Local non-commercial test assets; Data remains read-only.",
        "characters": results,
    }
    (OUTPUT_ROOT / "avatars.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "characters": len(results),
        "downloaded": sum(item["status"] == "downloaded" for item in results),
        "fallback": sum(item["status"] != "downloaded" for item in results),
        "manifest": str(OUTPUT_ROOT / "avatars.json"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "Data")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    print(json.dumps(build(args.data_root.resolve(), timeout=args.timeout), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
