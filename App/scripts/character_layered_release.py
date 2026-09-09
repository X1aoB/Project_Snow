"""Validate approved native layers before a unified character release is built.

The review files remain private. Every published derivative is made from a
hash-pinned, visually approved layer stack and its identical flattened preview.
"""

from __future__ import annotations

from typing import Any
from pathlib import Path
import base64
import hashlib
import os
import re
from urllib.parse import urlsplit

import numpy as np
from PIL import Image


SCOPE = "layered_character_presentation"
STRATEGY = "layered_sprite"
ARM_PARTS = {"hand", "wrist", "forearm", "elbow", "upper_arm", "shoulder_or_sleeve_attachment"}
LAYER_ROLES = {"neutral_base", "neutral_underlay", "reviewed_pose_underlay", "head_face", "connected_gesture", "front_hair"}
REVIEW_FLAGS = (
    "approved_neutral_base_unchanged", "head_face_is_one_contiguous_layer",
    "head_hair_matches_approved_expression_source", "no_layer_seam_or_duplicate_feature",
    "no_runtime_resampling_or_warping", "flattened_preview_matches_layer_stack",
    "identity_and_costume_consistent", "proportions_consistent",
)
BROWSER_COMPOSITING = "browser_canvas_source_over"


class NativeBrowserCompositor:
    """Compare native PNG layers with a preview using the review renderer.

    A release may reuse one context for every character. Only hash-checked local
    PNG bytes are passed to an isolated page; no review server is required.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None

    def __enter__(self) -> NativeBrowserCompositor:
        from playwright.sync_api import sync_playwright

        if self._playwright is not None:
            raise ValueError("native browser compositor is already open")
        channel = os.environ.get("PROJECT_SNOW_PLAYWRIGHT_CHANNEL", "").strip()
        if channel not in ("", "chrome"):
            raise ValueError("PROJECT_SNOW_PLAYWRIGHT_CHANNEL must be 'chrome' or empty")
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True, **({"channel": channel} if channel else {}))
            self._page = self._browser.new_page(device_scale_factor=1)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._page = self._browser = self._playwright = None

    @staticmethod
    def _png(path: Path, digest: str) -> str:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"asset changed before native browser comparison: {path}")
        return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")

    def compare(
        self, canvas: tuple[int, int], layers: list[dict[str, Any]],
        preview: Path, preview_sha256: str,
    ) -> dict[str, Any]:
        if self._page is None:
            raise ValueError("browser compositing requires an open NativeBrowserCompositor")
        payload = {
            "width": canvas[0], "height": canvas[1],
            "preview": self._png(preview, preview_sha256),
            "layers": [{
                "src": self._png(layer["path"], layer["asset_sha256"]),
                "x": layer["position"]["x"], "y": layer["position"]["y"],
                "width": layer["dimensions"]["width"], "height": layer["dimensions"]["height"],
            } for layer in layers],
        }
        result = self._page.evaluate("""async p => {
          const canvas = () => {const c=document.createElement('canvas');
            c.width=p.width;c.height=p.height;return c;};
          const decode = async (src,w,h) => {const im=new Image();im.src=src;await im.decode();
            if(im.naturalWidth!==w || im.naturalHeight!==h)throw Error('Native image size changed');return im;};
          const current=canvas(), expected=canvas(), ctx=current.getContext('2d');
          for(const l of p.layers)ctx.drawImage(await decode(l.src,l.width,l.height),l.x,l.y);
          expected.getContext('2d').drawImage(await decode(p.preview,p.width,p.height),0,0);
          const a=ctx.getImageData(0,0,p.width,p.height).data;
          const b=expected.getContext('2d').getImageData(0,0,p.width,p.height).data;
          let alpha=0,rgb=0,pixels=0;
          for(let i=0;i<a.length;i+=4){let diff=a[i+3]!==b[i+3];
            alpha=Math.max(alpha,Math.abs(a[i+3]-b[i+3]));
            for(let k=0;k<3;k++){const d=Math.abs(a[i+k]*a[i+3]-b[i+k]*b[i+3]);
              rgb=Math.max(rgb,d);diff ||= d!==0;}if(diff)pixels++;}
          return {alpha_delta:alpha,premultiplied_rgb_delta:rgb,different_visible_pixels:pixels};
        }""", payload)
        return {"renderer": BROWSER_COMPOSITING, "browser_version": self._browser.version, **result}


def _size(value: object, label: str) -> tuple[int, int]:
    if not isinstance(value, dict) or any(
        type(value.get(k)) is not int or not 0 < value[k] <= 4096 for k in ("width", "height")
    ):
        raise ValueError(f"invalid native canvas or layer size: {label}")
    return value["width"], value["height"]


def validate_policy(root: Path, approval: dict[str, Any], character_id: str, io: Any) -> dict[str, Any]:
    policy = approval.get("presentation_policy") or {}
    if not isinstance(policy, dict) or (
        policy.get("render_strategy") != STRATEGY or policy.get("asset_scope") != SCOPE
        or policy.get("micro_feature_mask_compositing") != "forbidden"
        or policy.get("partial_limb_compositing") != "forbidden"
    ):
        raise ValueError(f"layered presentation policy is missing: {character_id}")
    path = io._hashed_private_file(root, policy.get("policy_path"), policy.get("policy_sha256"), "presentation policy")
    shared = io._json_object(path)
    authoring = shared.get("authoring_policy") or {}
    if (
        shared.get("schema_version") != "project-snow-character-presentation-pipeline-2"
        or shared.get("publication_status") != "pending_not_public"
        or authoring.get("standard_expression_asset_scope") != SCOPE
        or authoring.get("neutral_base") != "approved_rgba_pixel_locked"
        or authoring.get("expression_layer") != "single_contiguous_head_face_layer_from_approved_expression"
        or authoring.get("micro_feature_mask_compositing") != "forbidden"
        or authoring.get("feathered_face_recompositing") != "forbidden"
        or authoring.get("complete_character_redraw_for_standard_expression") != "forbidden"
        or authoring.get("partial_hand_or_limb_compositing") != "forbidden"
    ):
        raise ValueError(f"shared layered presentation policy is invalid: {character_id}")
    return policy


def _neutral(root: Path, approval: dict[str, Any], character_id: str, io: Any) -> tuple[Path, Image.Image]:
    review = approval.get("neutral_review") or {}
    if review.get("approved") is not True:
        raise ValueError(f"neutral cutout is not approved: {character_id}")
    path = io._hashed_private_file(root, review.get("asset_path"), review.get("asset_sha256"), "neutral master")
    neutral, _ = io._rgba_array(path, "neutral master")
    # New neutral decisions bind the immutable review submission. Earlier
    # calibration characters instead bind their processing provenance.
    if review.get("submission_path"):
        submission_path = io._hashed_private_file(root, review["submission_path"], review.get("submission_sha256"), "neutral submission")
        submission = io._json_object(submission_path)
        event_path = io._private_file(root, review.get("approval_event_path"), "neutral approval event")
        event = io._json_object(event_path)
        event_rows = event.get("characters") or []
        matched = [item for item in event_rows if isinstance(item, dict) and item.get("character_id") == character_id]
        if (event.get("review_status") != "approved" or event.get("approved_by") != "user" or len(matched) != 1
                or (event.get("submission") or {}).get("sha256") != io._digest_path(submission_path)
                or (matched[0].get("locked_neutral") or {}).get("sha256") != io._digest_path(path)
                or not submission):
            raise ValueError(f"neutral batch decision mismatch: {character_id}")
    else:
        provenance = io._hashed_private_file(root, review.get("processing_manifest_path"), review.get("processing_manifest_sha256"), "neutral provenance")
        record = io._json_object(provenance)
        if record.get("character_id") != character_id or record.get("publication_status") != "pending_not_public":
            raise ValueError(f"neutral processing provenance mismatch: {character_id}")
        if record.get("schema_version") == "project-snow-neutral-completion-1":
            scope, audit = record.get("authorization_scope") or {}, record.get("audit") or {}
            if (scope.get("operation") != "fill_only_source_UI_occluded_character_pixels"
                    or scope.get("canvas_expansion") is not False or scope.get("existing_pixel_modification") is not False
                    or audit.get("outside_mask_changed_pixels") != 0 or audit.get("existing_visible_changed_pixels") != 0
                    or audit.get("canvas_dimensions_unchanged") is not True):
                raise ValueError(f"neutral completion exceeds its authorization: {character_id}")
    return path, neutral


def _reviewed_pose_binding(
    root: Path, approval: dict[str, Any], row: dict[str, Any], io: Any,
) -> dict[str, Any]:
    """Authorize an existing complete pose only from the exact approved U4 item."""
    cid, key = approval["character_id"], row.get("review_item_key")
    event_ref = row.get("approval_event") or {}
    if not isinstance(key, str) or not key or not isinstance(event_ref, dict):
        raise ValueError(f"reviewed pose requires an explicit approved item binding: {cid}")
    event_path = io._hashed_private_file(root, event_ref.get("path"), event_ref.get("sha256"), "pose approval event")
    event = io._json_object(event_path)
    characters = event.get("characters")
    if (event.get("review_status") != "approved" or event.get("approved_by") != "user"
            or not isinstance(characters, list)):
        raise ValueError(f"reviewed pose event is not user approved: {cid}/{key}")
    matches = [c for c in characters if isinstance(c, dict) and c.get("character_id") == cid]
    if len(matches) != 1 or not isinstance(matches[0].get("approved_items"), list):
        raise ValueError(f"reviewed pose character decision mismatch: {cid}/{key}")
    choices = [i for i in matches[0]["approved_items"] if isinstance(i, dict) and i.get("key") == key]
    if len(choices) != 1 or choices[0].get("selected_for_runtime") is not True:
        raise ValueError(f"reviewed pose is not selected for runtime: {cid}/{key}")
    chosen = choices[0]
    submission_ref = event.get("submission") or {}
    if not isinstance(submission_ref, dict):
        raise ValueError(f"reviewed pose submission reference is invalid: {cid}/{key}")
    submission_path = io._hashed_private_file(root, submission_ref.get("path"), submission_ref.get("sha256"), "pose review submission")
    submission = io._json_object(submission_path)
    if submission.get("sealed") is not True or submission.get("round") != "U4":
        raise ValueError(f"reviewed pose must bind the sealed U4 submission: {cid}/{key}")
    data_ref = submission.get("review_data") or {}
    if not isinstance(data_ref, dict):
        raise ValueError(f"reviewed pose data reference is invalid: {cid}/{key}")
    data_path = io._hashed_private_file(root, data_ref.get("path"), data_ref.get("sha256"), "pose review data")
    data = io._json_object(data_path)
    groups = data.get("groups")
    if data.get("round") != "U4" or not isinstance(groups, list):
        raise ValueError(f"reviewed pose has invalid U4 data: {cid}/{key}")
    groups = [g for g in groups if isinstance(g, dict) and g.get("characterId") == cid]
    if len(groups) != 1 or not isinstance(groups[0].get("items"), list):
        raise ValueError(f"reviewed pose has no unique U4 character: {cid}/{key}")
    items = [i for i in groups[0]["items"] if isinstance(i, dict) and i.get("key") == key]
    if len(items) != 1:
        raise ValueError(f"reviewed pose has no unique U4 item: {cid}/{key}")
    reviewed = items[0]

    def reference(path, digest):
        asset = io._hashed_private_file(root, path, digest, f"approved pose asset:{cid}/{key}")
        return asset.relative_to(root.resolve()).as_posix(), digest

    def ui_reference(raw, digest):
        if not isinstance(raw, str) or not raw:
            raise ValueError("reviewed pose URL is missing")
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc or parts.query or parts.fragment or Path(raw).is_absolute():
            raise ValueError("reviewed pose URL must be a local relative path")
        try:
            relative = (data_path.parent / raw).resolve().relative_to(root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("reviewed pose URL escaped the private art root") from exc
        return reference(relative, digest)

    preview = chosen.get("preview") or {}
    if not isinstance(preview, dict):
        raise ValueError(f"reviewed pose approved preview is invalid: {cid}/{key}")
    if (reference(row.get("asset_path"), row.get("asset_sha256"))
            != reference(preview.get("path"), preview.get("sha256"))
            or reference(row.get("asset_path"), row.get("asset_sha256"))
            != ui_reference(reviewed.get("preview"), reviewed.get("previewSha256"))):
        raise ValueError(f"reviewed pose preview differs from approved U4: {cid}/{key}")
    approved_layers, reviewed_layers = chosen.get("layers"), reviewed.get("layers")
    layers = row["layers"]
    if (not isinstance(approved_layers, list) or not isinstance(reviewed_layers, list)
            or len(layers) != len(approved_layers) or len(layers) != len(reviewed_layers)):
        raise ValueError(f"reviewed pose layer count differs from approved U4: {cid}/{key}")
    for current, accepted, prior in zip(layers, approved_layers, reviewed_layers):
        if not isinstance(current, dict) or not isinstance(accepted, dict) or not isinstance(prior, dict):
            raise ValueError(f"reviewed pose layer is invalid: {cid}/{key}")
        position = current.get("position") or {}
        dimensions = current.get("dimensions") or {}
        if not isinstance(position, dict) or not isinstance(dimensions, dict):
            raise ValueError(f"reviewed pose geometry is invalid: {cid}/{key}")
        xy = [position.get("x"), position.get("y")]
        size = [dimensions.get("width"), dimensions.get("height")]
        if (reference(current.get("asset_path"), current.get("asset_sha256"))
                != reference(accepted.get("path"), accepted.get("sha256"))
                or reference(current.get("asset_path"), current.get("asset_sha256"))
                != ui_reference(prior.get("url"), prior.get("sha256"))
                or xy != accepted.get("position_xy") or xy != [prior.get("x"), prior.get("y")]
                or size != accepted.get("dimensions") or size != [prior.get("width"), prior.get("height")]):
            raise ValueError(f"reviewed pose layers differ from approved U4: {cid}/{key}")
    return {"approval_event_sha256": event_ref["sha256"], "submission_sha256": submission_ref["sha256"],
            "review_item_key": key, "exact_approved_U4_item": True}


def validate_presentation(
    root: Path, approval: dict[str, Any], row: dict[str, Any], key: str,
    neutral_path: Path, neutral: Image.Image, io: Any,
    *, compositor: NativeBrowserCompositor | None = None,
) -> dict[str, Any]:
    label = f"{approval['character_id']}/{key}"
    review = row.get("layered_review") or {}
    if (row.get("approved") is not True or not str(row.get("user_decision") or "").strip()
            or row.get("asset_scope") != SCOPE or row.get("render_strategy") != STRATEGY
            or not isinstance(review, dict) or any(review.get(flag) is not True for flag in REVIEW_FLAGS)):
        raise ValueError(f"layered visual review is incomplete: {label}")
    if (row.get("base_asset_path") != neutral_path.relative_to(root).as_posix()
            or row.get("base_asset_sha256") != io._digest_path(neutral_path)):
        raise ValueError(f"layered neutral binding mismatch: {label}")
    source_path = io._hashed_private_file(root, row.get("asset_path"), row.get("asset_sha256"), f"preview:{label}")
    preview, expected = io._rgba_array(source_path, label)
    canvas = _size(row.get("canvas"), label)
    if canvas != neutral.size or preview.size != canvas:
        raise ValueError(f"native canvas drifted: {label}")
    layers = row.get("layers")
    if not isinstance(layers, list) or not 1 <= len(layers) <= 8:
        raise ValueError(f"layer stack is missing or too large: {label}")
    pose_binding = None
    if any(isinstance(layer, dict) and layer.get("role") == "reviewed_pose_underlay" for layer in layers):
        pose_binding = _reviewed_pose_binding(root, approval, row, io)
    resolved_layers = []
    roles = []
    pixels = 0
    flattened = Image.new("RGBA", canvas)
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or layer.get("role") not in LAYER_ROLES:
            raise ValueError(f"unsupported layer role: {label}:{index}")
        role = layer["role"]
        size = _size(layer.get("dimensions"), label)
        position = layer.get("position")
        if (not isinstance(position, dict) or any(type(position.get(k)) is not int or position[k] < 0 for k in ("x", "y"))
                or position["x"] + size[0] > canvas[0] or position["y"] + size[1] > canvas[1]
                or layer.get("composite") != "source-over"):
            raise ValueError(f"invalid layer geometry or blend: {label}:{index}")
        path = io._hashed_private_file(root, layer.get("asset_path"), layer.get("asset_sha256"), f"layer:{label}:{index}")
        if index == 0 and role in {"neutral_underlay", "reviewed_pose_underlay"}:
            # A complete approved connected pose may replace the whole base.
            # Only its first cleared underlay may be entirely transparent.
            with Image.open(path) as candidate:
                if candidate.mode != "RGBA" or candidate.format != "PNG":
                    raise ValueError(f"approved underlay must be an RGBA PNG: {label}:{index}")
                image = candidate.copy()
            values = np.asarray(image)
            if not np.any(values[:, :, 3] == 0):
                raise ValueError(f"approved underlay must contain a transparent background: {label}:{index}")
        else:
            image, values = io._rgba_array(path, f"layer:{label}:{index}")
        if image.size != size:
            raise ValueError(f"layer dimensions mismatch: {label}:{index}")
        if index == 0:
            if role not in {"neutral_base", "neutral_underlay", "reviewed_pose_underlay"} or size != canvas or position != {"x": 0, "y": 0}:
                raise ValueError(f"first layer must be the native neutral or its cleared underlay: {label}")
            original = np.asarray(neutral)
            if role == "neutral_base" and not np.array_equal(values, original):
                raise ValueError(f"neutral layer changed: {label}")
            alpha_reduced = values[:, :, 3] <= original[:, :, 3]
            visible_rgb_unchanged = (values[:, :, 3] == 0) | np.all(values[:, :, :3] == original[:, :, :3], axis=2)
            if role == "neutral_underlay" and not np.all(alpha_reduced & visible_rgb_unchanged):
                raise ValueError(f"underlay may only reduce alpha without changing visible RGB: {label}")
        elif role in {"neutral_base", "neutral_underlay", "reviewed_pose_underlay"}:
            raise ValueError(f"duplicate neutral layer: {label}")
        pixels += size[0] * size[1]
        flattened.alpha_composite(image, (position["x"], position["y"]))
        resolved_layers.append({**layer, "path": path})
        roles.append(role)
    if pixels > canvas[0] * canvas[1] * 4 or roles.count("head_face") > 1:
        raise ValueError(f"layer budget or single head-face constraint failed: {label}")
    if key != "neutral" and roles.count("head_face") != 1:
        raise ValueError(f"one contiguous head-face layer is required: {label}")
    actual = np.asarray(flattened)
    compositing = row.get("preview_compositing", "pil_source_over")
    if compositing == BROWSER_COMPOSITING:
        if compositor is None:
            raise ValueError(f"native browser compositing context is required: {label}")
        comparison = compositor.compare(canvas, resolved_layers, source_path, row["asset_sha256"])
        if any(comparison[field] != 0 for field in (
            "alpha_delta", "premultiplied_rgb_delta", "different_visible_pixels",
        )):
            raise ValueError(f"flattened preview differs from approved browser layer stack: {label}")
    elif compositing == "pil_source_over":
        # Fully transparent hidden RGB is not part of the rendered result.
        if not np.array_equal(actual[:,:,3], expected[:,:,3]) or not np.array_equal(actual[actual[:,:,3] > 0], expected[expected[:,:,3] > 0]):
            raise ValueError(f"flattened preview differs from approved layer stack: {label}")
        comparison = {"renderer": compositing, "different_visible_pixels": 0}
    else:
        raise ValueError(f"unsupported preview compositing semantics: {label}")
    if key == "neutral":
        if len(layers) != 1 or not np.array_equal(expected, np.asarray(neutral)):
            raise ValueError(f"neutral presentation differs from locked master: {label}")
    else:
        region = row.get("replacement_region") or {}
        mask_path = io._hashed_private_file(root, region.get("asset_path"), region.get("asset_sha256"), f"replacement:{label}")
        with Image.open(mask_path) as image:
            if image.format != "PNG" or image.mode != "L" or image.size != canvas:
                raise ValueError(f"replacement region must be a native binary PNG: {label}")
            mask = np.asarray(image)
        if not np.isin(mask, [0, 255]).all() or not np.any(mask):
            raise ValueError(f"replacement region is not a binary region: {label}")
        if np.any(np.any(expected != np.asarray(neutral), axis=2) & (mask == 0)):
            raise ValueError(f"pixels outside reviewed replacement region changed: {label}")
        io._hashed_private_file(root, row.get("source_reference_path"), row.get("source_reference_sha256"), f"selected raw:{label}")
    if "connected_gesture" in roles:
        connection = row.get("connected_gesture") or {}
        chains = connection.get("arm_chains") or []
        if not chains or connection.get("old_limbs_removed") is not True or any(
            not isinstance(chain, dict) or chain.get("side") not in {"left", "right"}
            or set(chain.get("parts") or []) != ARM_PARTS for chain in chains
        ):
            raise ValueError(f"complete connected-arm review is missing: {label}")
    round_number = row.get("approved_round")
    if type(round_number) is not int or round_number < 1 or not str(row.get("approved_variant") or "").strip():
        raise ValueError(f"approved candidate is missing: {label}")
    return {
        "path": source_path, "sha256": io._digest_path(source_path),
        "approved_variant": row["approved_variant"], "approved_round": round_number,
        "asset_scope": SCOPE, "render_strategy": STRATEGY,
        "face_crop_box_xyxy": io._face_crop(row.get("face_crop_box_xyxy", approval.get("face_crop_box_xyxy")), canvas, label),
        "stage_layout": io._stage_layout(row.get("stage_layout", approval.get("stage_layout")), label),
        "canvas": {"width": canvas[0], "height": canvas[1]}, "layers": resolved_layers,
        "compositing_validation": comparison,
        **({"reviewed_pose_binding": pose_binding} if pose_binding else {}),
    }


def _approved_cues(
    root: Path, approval: dict[str, Any], raw_performances: object, character_id: str, io: Any,
) -> None:
    if not isinstance(raw_performances, dict):
        raise ValueError(f"approved character performances must be an object: {character_id}")
    inventory = approval.get("approved_cue_inventory")
    if inventory is None:
        if not 3 <= len(raw_performances) <= 6:
            raise ValueError(f"three to six approved character performances are required: {character_id}")
        return
    if not isinstance(inventory, dict):
        raise ValueError(f"approved cue inventory is invalid: {character_id}")
    cues = inventory.get("cues")
    if (not isinstance(cues, list) or len(cues) > 6
            or any(not isinstance(cue, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", cue) for cue in cues)
            or len(cues) != len(set(cues)) or set(cues) != set(raw_performances)):
        raise ValueError(f"approved cue inventory differs from selected performances: {character_id}")
    event_path = io._hashed_private_file(
        root, inventory.get("approval_event_path"), inventory.get("approval_event_sha256"), "cue approval event",
    )
    event = io._json_object(event_path)
    rows = event.get("characters")
    if not isinstance(rows, list):
        raise ValueError(f"cue approval event has no character decisions: {character_id}")
    matched = [r for r in rows if isinstance(r, dict) and r.get("character_id") == character_id]
    if event.get("review_status") != "approved" or event.get("approved_by") != "user" or len(matched) != 1:
        raise ValueError(f"cue inventory is not explicitly user approved: {character_id}")
    decided = matched[0].get("approved_cues")
    if (not isinstance(decided, list) or any(not isinstance(cue, str) for cue in decided)
            or len(decided) != len(set(decided)) or set(decided) != set(cues)):
        raise ValueError(f"cue inventory differs from user decision: {character_id}")
    source = event.get("submission") or {}
    source_path = io._hashed_private_file(root, source.get("path"), source.get("sha256"), "cue decision submission")
    if io._json_object(source_path).get("sealed") is not True:
        raise ValueError(f"cue decision does not bind a sealed review: {character_id}")


def validate_character(
    root: Path, source_row: dict[str, Any], io: Any,
    *, approval_path: Path | None = None, compositor: NativeBrowserCompositor | None = None,
) -> dict[str, Any]:
    character_id = str(source_row["character_id"])
    path = approval_path if approval_path is not None else root / "characters" / character_id / "APPROVALS.pending.json"
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"approval manifest must remain inside the private art root: {character_id}") from exc
    path = io._private_file(root, relative, "character approval manifest")
    approval = io._json_object(path)
    if (approval.get("schema_version") != "project-snow-character-expression-approvals-1"
            or approval.get("character_id") != character_id):
        raise ValueError(f"approval identity or schema mismatch: {character_id}")
    policy = validate_policy(root, approval, character_id, io)
    if approval.get("all_18_layered_presentations_approved") is not True:
        raise ValueError(f"all layered presentations are not approved: {character_id}")
    presentations = approval.get("approved_presentations") or {}
    if set(presentations) != set(io.EXPRESSION_STATES):
        raise ValueError(f"presentation approval must contain the exact 18-state contract: {character_id}")
    neutral_path, neutral = _neutral(root, approval, character_id, io)
    states = {state: validate_presentation(root, approval, presentations[state], state, neutral_path, neutral, io, compositor=compositor) for state in io.EXPRESSION_STATES}
    raw_performances = approval.get("approved_narrative_presentations", {})
    _approved_cues(root, approval, raw_performances, character_id, io)
    performances = {}
    for cue, row in raw_performances.items():
        if (not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", cue) or not isinstance(row, dict)
                or not str(row.get("label") or "").strip() or not str(row.get("usage_context") or "").strip()
                or row.get("fallback_expression") not in io.EXPRESSION_STATES):
            raise ValueError(f"character performance metadata is incomplete: {character_id}/{cue}")
        design_path = io._hashed_private_file(root, row.get("design_path"), row.get("design_sha256"), f"performance design:{character_id}/{cue}")
        design = io._json_object(design_path)
        if design.get("character_id") != character_id:
            raise ValueError(f"performance design belongs to another character: {character_id}/{cue}")
        performances[cue] = {
            **validate_presentation(root, approval, row, f"performance.{cue}", neutral_path, neutral, io, compositor=compositor),
            "label": row["label"], "usage_context": row["usage_context"], "fallback_expression": row["fallback_expression"],
        }
    return {
        "approval_manifest_path": path, "approval_manifest_sha256": io._digest_path(path),
        "face_crop_box_xyxy": states["neutral"]["face_crop_box_xyxy"], "presentation_policy": policy,
        "stage_layout": io._stage_layout(approval.get("stage_layout"), character_id),
        "states": states, "performances": performances,
        "source_dimensions": {"width": neutral.width, "height": neutral.height},
    }


def render_layers(release_root: Path, character_id: str, record: dict[str, Any], io: Any) -> list[dict[str, Any]]:
    directory = release_root / "expressions" / character_id / "layers"
    result = []
    for layer in record["layers"]:
        # Chromium premultiplies decoded PNG and WebP pixels differently.
        # Preserve the approved native PNG bytes so semi-transparent source-over
        # composition is exactly the reviewed result; derivatives may use WebP.
        payload = layer["path"].read_bytes()
        digest = io._digest_bytes(payload)
        if digest != layer["asset_sha256"]:
            raise ValueError("native PNG changed after approval validation")
        name = f"{layer['role']}.{digest[:16]}.png"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(payload)
        result.append({
            "asset_path": f"expressions/{character_id}/layers/{name}", "asset_sha256": digest,
            "dimensions": layer["dimensions"], "position": layer["position"], "composite": "source-over",
        })
    return result
