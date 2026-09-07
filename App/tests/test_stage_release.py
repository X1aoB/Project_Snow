from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.verify_stage_release import validate_release


class StageReleaseTests(TestCase):
    def fixture(self, directory):
        root = Path(directory)
        content = b"\x89PNG\r\n\x1a\nsynthetic-header-for-hash-validation"
        (root / "neutral.png").write_bytes(content)
        asset = {"url":"/assets/stage/neutral.png","sha256":hashlib.sha256(content).hexdigest(),"media_type":"image/png","source":{"kind":"fixture","reference":"synthetic"},"approval":{"status":"approved","approved_by":"fixture-reviewer","approved_at":"2026-09-07T00:00:00Z","evidence":"synthetic approval; never for release"}}
        ids = {f"{number:012x}" for number in range(22)}
        return root, ids, {"schema":"project-snow-stage-1","version":"fixture","characters":[{"character_id":identifier,"states":{"neutral":copy.deepcopy(asset)},"motions":["none","lean_in"]} for identifier in sorted(ids)]}

    def test_validates_full_roster_and_exact_bytes(self):
        with TemporaryDirectory() as directory:
            root, ids, manifest = self.fixture(directory)
            self.assertEqual(validate_release(manifest,root,ids)["characters"],22)
            (root/"neutral.png").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError,"hash_mismatch"):
                validate_release(manifest,root,ids)

    def test_missing_approval_partial_roster_and_path_escape_cannot_release(self):
        with TemporaryDirectory() as directory:
            root, ids, original = self.fixture(directory)
            for mutation, error in [
                (lambda value:value["characters"].pop(),"22_characters"),
                (lambda value:value["characters"][0]["states"]["neutral"]["approval"].update(status="pending"),"approval_required"),
                (lambda value:value["characters"][0]["states"]["neutral"].update(url="/assets/stage/../outside.png"),"path_invalid"),
                (lambda value:value["characters"][0].update(motions=["execute-script"]),"motion_invalid"),
            ]:
                with self.subTest(error=error):
                    manifest = copy.deepcopy(original)
                    mutation(manifest)
                    with self.assertRaisesRegex(ValueError,error):
                        validate_release(manifest,root,ids)
