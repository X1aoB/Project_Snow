"""Validate a reviewed 22-character release before it can be enabled.

This checks recorded approval, provenance and exact bytes. It does not confer
human approval and never enables the release or writes source artwork.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import re

STATES = ("neutral", "gentle_smile", "happy", "amused", "teasing", "relieved", "serious", "focused", "thinking", "confused", "skeptical", "concerned", "surprised", "embarrassed", "sad", "disappointed", "annoyed", "angry")
MOTIONS = ("none", "lean_in", "tremble", "recoil", "startle")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_release(manifest: dict, asset_root: Path, expected_ids: set[str]) -> dict:
    require(manifest.get("schema") == "project-snow-stage-1" and nonempty(manifest.get("version")), "stage_manifest_invalid")
    characters = manifest.get("characters")
    require(isinstance(characters, list) and len(characters) == 22 and len(expected_ids) == 22, "stage_requires_22_characters")
    root = asset_root.resolve(strict=True)
    seen: set[str] = set()
    asset_count = 0
    for character in characters:
        require(isinstance(character, dict), "stage_character_invalid")
        character_id = character.get("character_id")
        require(isinstance(character_id, str) and bool(re.fullmatch(r"[0-9a-f]{12}", character_id)) and character_id not in seen, "stage_character_invalid")
        seen.add(character_id)
        motions = character.get("motions")
        require(isinstance(motions, list) and "none" in motions and all(motion in MOTIONS for motion in motions), "stage_motion_invalid")
        states = character.get("states")
        require(isinstance(states, dict) and "neutral" in states and set(states).issubset(STATES), "stage_states_invalid")
        for asset in states.values():
            require(isinstance(asset, dict), "stage_asset_invalid")
            url = asset.get("url")
            require(isinstance(url, str) and bool(re.fullmatch(r"/assets/stage/[a-zA-Z0-9_./-]+", url)) and ".." not in url, "stage_asset_path_invalid")
            digest = asset.get("sha256")
            require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "stage_asset_hash_invalid")
            require(asset.get("media_type") in {"image/png", "image/webp"}, "stage_asset_type_invalid")
            source = asset.get("source")
            require(isinstance(source, dict) and nonempty(source.get("kind")) and nonempty(source.get("reference")), "stage_source_required")
            approval = asset.get("approval")
            require(isinstance(approval, dict) and approval.get("status") == "approved" and all(nonempty(approval.get(key)) for key in ("approved_by", "approved_at", "evidence")), "stage_user_approval_required")
            try:
                datetime.fromisoformat(approval["approved_at"].replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("stage_approval_date_invalid") from error
            path = (root / url.removeprefix("/assets/stage/")).resolve(strict=True)
            require(path.is_relative_to(root) and path.is_file(), "stage_asset_outside_root")
            require(path.stat().st_size <= 8 * 1024 * 1024, "stage_asset_too_large")
            content = path.read_bytes()
            require(hashlib.sha256(content).hexdigest() == digest, "stage_asset_hash_mismatch")
            require(content.startswith(b"\x89PNG\r\n\x1a\n") if asset["media_type"] == "image/png" else content[:4] == b"RIFF" and content[8:12] == b"WEBP", "stage_asset_type_mismatch")
            asset_count += 1
    require(seen == expected_ids, "stage_roster_mismatch")
    return {"status": "verified", "version": manifest["version"], "characters": len(seen), "assets": asset_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--asset-root", type=Path, required=True, help="Directory mounted at /assets/stage/")
    parser.add_argument("--character-roster", type=Path, required=True, help="JSON array or {characters:[{character_id:...}]} containing the 22 release IDs")
    args = parser.parse_args()
    payload = args.manifest.read_bytes()
    require(len(payload) <= 2 * 1024 * 1024, "stage_manifest_too_large")
    roster = json.loads(args.character_roster.read_text(encoding="utf-8"))
    entries = roster["characters"] if isinstance(roster, dict) else roster
    ids = {entry if isinstance(entry, str) else entry["character_id"] for entry in entries}
    result = validate_release(json.loads(payload), args.asset_root, ids)
    result["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
