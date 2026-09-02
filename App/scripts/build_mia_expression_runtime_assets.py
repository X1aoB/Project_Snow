#!/usr/bin/env python3
"""Build the approved Mia stage-expression assets for the public client.

The source review package remains an internal provenance artifact.  This script
copies only the approved face crop into the public application image and records
the explicit operator rights waiver without presenting it as verification.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image


CHARACTER_ID = "702f4375675b"
EXPECTED_STATES = (
    "neutral",
    "gentle_smile",
    "happy",
    "amused",
    "teasing",
    "relieved",
    "serious",
    "focused",
    "thinking",
    "confused",
    "skeptical",
    "concerned",
    "surprised",
    "embarrassed",
    "sad",
    "disappointed",
    "annoyed",
    "angry",
)
SOURCE_PACKAGE = Path("media/stage_art_candidates/702f4375675b/2026-09-02-balanced-18")
OUTPUT_DIRECTORY = Path("public_frontend/assets/expressions/mia")
FACE_CROP = (440, 145, 635, 340)
OUTPUT_SIZE = (384, 384)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    app_root = Path(__file__).resolve().parents[1]
    source_root = app_root / SOURCE_PACKAGE
    output_root = app_root / OUTPUT_DIRECTORY
    approval_path = source_root / "APPROVALS.pending.json"
    candidate_manifest_path = source_root / "manifest.pending.json"
    approval = load_json(approval_path)
    candidate_manifest = load_json(candidate_manifest_path)

    if candidate_manifest.get("character_id") != CHARACTER_ID:
        raise RuntimeError("candidate package character mismatch")
    selections = {
        str(state): str(variant)
        for state, variant in dict(approval["approved_selections"]).items()
    }
    rounds = {
        str(state): int(round_number)
        for state, round_number in dict(approval["approved_round_by_state"]).items()
    }
    if set(selections) != set(EXPECTED_STATES) or set(rounds) != set(EXPECTED_STATES):
        raise RuntimeError("approval snapshot must contain the 18-state contract")
    if not approval.get("all_18_states_individually_approved"):
        raise RuntimeError("all 18 states must be individually approved")

    records = {
        (str(record["state"]), str(record["variant"])): record
        for record in candidate_manifest["candidates"]
    }
    output_root.mkdir(parents=True, exist_ok=True)
    for old_asset in output_root.glob("*.webp"):
        old_asset.unlink()

    expressions: dict[str, object] = {}
    for state in EXPECTED_STATES:
        variant = selections[state]
        record = records.get((state, variant))
        if record is None:
            raise RuntimeError(f"approved candidate is missing: {state}/{variant}")
        expected_approval = f"approved_round_{rounds[state]}"
        if record.get("approval") != expected_approval:
            raise RuntimeError(
                f"approval mismatch for {state}/{variant}: "
                f"{record.get('approval')!r} != {expected_approval!r}"
            )
        source_path = source_root / str(record["path"])
        source_digest = sha256(source_path)
        if source_digest != record.get("sha256"):
            raise RuntimeError(f"source hash mismatch for {state}/{variant}")

        with Image.open(source_path) as source:
            if source.size != (877, 1449) or source.mode != "RGBA":
                raise RuntimeError(
                    f"source image contract mismatch for {state}/{variant}: "
                    f"{source.size=} {source.mode=}"
                )
            derivative = source.crop(FACE_CROP).resize(OUTPUT_SIZE, Image.Resampling.LANCZOS)

        staging_path = output_root / f"{state}.webp"
        derivative.save(
            staging_path,
            format="WEBP",
            lossless=True,
            quality=100,
            method=4,
            exact=True,
        )
        asset_digest = sha256(staging_path)
        asset_name = f"{state}.{asset_digest[:16]}.webp"
        asset_path = output_root / asset_name
        staging_path.replace(asset_path)
        expressions[state] = {
            "approved_variant": variant,
            "approved_round": rounds[state],
            "source_path": (SOURCE_PACKAGE / str(record["path"])).as_posix(),
            "source_sha256": source_digest,
            "source_reference": str(record.get("source_reference") or ""),
            "source_reference_sha256": str(record.get("source_reference_sha256") or ""),
            "asset_path": f"/assets/expressions/mia/{asset_name}",
            "asset_sha256": asset_digest,
        }

    manifest = {
        "schema_version": "project-snow-mia-expression-runtime-1",
        "character_id": CHARACTER_ID,
        "display_name": "米娅",
        "expression_state_count": len(expressions),
        "publication_status": "public_runtime_enabled_by_explicit_operator_rights_waiver",
        "rights": {
            "independent_verification": False,
            "verification_status": "not_performed",
            "ownership_claimed_by_project": False,
            "waiver": {
                "granted": True,
                "recorded_date": "2026-09-02",
                "instruction": "skip_material_rights_and_continue_to_first_release",
                "scope": "these_18_user_approved_mia_static_expression_derivatives_only",
            },
            "takedown_contact": "admin@xiaob.dev",
        },
        "source_package": {
            "path": SOURCE_PACKAGE.as_posix(),
            "candidate_manifest_sha256": sha256(candidate_manifest_path),
            "approval_snapshot_sha256": sha256(approval_path),
            "approved_state_count": len(selections),
        },
        "transform": {
            "crop_box_xyxy": list(FACE_CROP),
            "output_dimensions": {"width": OUTPUT_SIZE[0], "height": OUTPUT_SIZE[1]},
            "format": "lossless_webp",
            "mode": "RGBA",
            "resampling": "lanczos",
            "metadata_copied": False,
        },
        "expressions": expressions,
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "expression_state_count": len(expressions),
                "total_asset_bytes": sum(
                    (output_root / Path(item["asset_path"]).name).stat().st_size
                    for item in expressions.values()
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
