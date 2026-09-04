from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase

from scripts import voice_composition_viability_ops as viability
from scripts import voice_corpus_routing_ops as routing
from scripts import voice_paralinguistic_ops as para
from tests.test_voice_corpus_routing_ops import RECORDED_AT, RoutingFixture
from tests.test_voice_paralinguistic_ops import _pretty_bytes


class ViabilityFixture(RoutingFixture):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.policy_path = root / viability.POLICY_RELATIVE_PATH
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy_path.write_text(
            "# Fixed policy\n\n"
            + "\n".join(viability.REQUIRED_POLICY_LINES)
            + "\n",
            encoding="utf-8",
        )
        self.policy_sha = hashlib.sha256(self.policy_path.read_bytes()).hexdigest()
        self.routing_receipt: dict | None = None
        self.routing_byte_sha: str | None = None

    def create_routing_receipt(self) -> dict:
        receipt, destination = self.build_routing(recorded_at=RECORDED_AT)
        status, stored = routing.write_routing_receipt(
            self.root, receipt, destination
        )
        if status not in {"created", "existing_valid"}:
            raise AssertionError(status)
        self.routing_receipt = stored
        self.routing_byte_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
        return stored

    def build_viability(
        self, *, recorded_at: str = RECORDED_AT
    ) -> tuple[dict, Path]:
        if self.routing_receipt is None:
            self.create_routing_receipt()
        return viability.build_viability_receipt(
            self.root,
            self.routing_receipt["routing_id"],
            reviewer_id="xiaob",
            recorded_at=recorded_at,
            expected_routing_sha256=self.routing_byte_sha,
            expected_package_sha256=self.hashes["package"],
            expected_queue_sha256=self.hashes["queue"],
            expected_policy_sha256=self.policy_sha,
        )


class VoiceCompositionViabilityOpsTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = ViabilityFixture(Path(self.temporary.name) / "Voice")

    def test_dry_run_covers_four_slots_without_writing(self) -> None:
        receipt, destination = self.fixture.build_viability()

        self.assertFalse(destination.parent.exists())
        self.assertEqual(receipt["summary"]["character_count"], 2)
        self.assertEqual(receipt["summary"]["slot_count"], 4)
        self.assertEqual(
            receipt["summary"]["full_acoustic_qc_completed_slot_count"], 0
        )
        self.assertTrue(
            all(
                slot["full_acoustic_qc_status"] == "not_run_after_routing"
                for slot in receipt["slots"]
            )
        )
        self.assertTrue(
            all(value is False for value in receipt["scope_limits"].values())
        )

    def test_duration_candidate_branch_uses_retained_lexical_only(self) -> None:
        route_receipt = self.fixture.create_routing_receipt()
        route_copy = copy.deepcopy(route_receipt)
        package_copy = copy.deepcopy(self.fixture.package)
        queue_copy = copy.deepcopy(self.fixture.queue)
        durations = {5: 3.0, 6: 3.0, 7: 4.0}
        for ordinal, duration in durations.items():
            route_copy["routes"][ordinal - 1]["audio"][
                "duration_seconds"
            ] = duration
            clip = next(
                item
                for item in package_copy["clips"]
                if item["ordinal"] == ordinal
            )
            clip["duration_seconds"] = duration
        vidya = next(
            item
            for item in queue_copy["characters"]
            if item["character_name"] == "薇蒂雅"
        )
        proposal = next(item for item in vidya["proposals"] if item["slot"] == "B")
        proposal["predicted_composite_qc"]["duration_seconds"] = 10.3

        slots, _, _ = viability._build_viability(
            route_copy, package_copy, queue_copy
        )
        result = next(
            item
            for item in slots
            if item["character_name"] == "薇蒂雅" and item["slot"] == "B"
        )
        self.assertEqual(result["retained_lexical_duration_seconds"], 10.3)
        self.assertTrue(result["duration_eligible"])
        self.assertEqual(
            result["duration_status"], "duration_candidate_pending_full_qc"
        )

    def test_write_validate_and_retry_are_idempotent(self) -> None:
        receipt, destination = self.fixture.build_viability()
        status, stored = viability.write_viability_receipt(
            self.fixture.root, receipt, destination
        )

        self.assertEqual(status, "created")
        validation = viability.validate_viability_receipt(
            self.fixture.root, receipt["viability_id"]
        )
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(validation["all_scope_gates_closed"])

        later, same_destination = self.fixture.build_viability(
            recorded_at="2026-09-03T06:00:00+00:00"
        )
        retry_status, retry_stored = viability.write_viability_receipt(
            self.fixture.root, later, same_destination
        )
        self.assertEqual(retry_status, "existing_valid")
        self.assertEqual(retry_stored, stored)

    def test_missing_fixed_policy_rule_is_rejected(self) -> None:
        self.fixture.create_routing_receipt()
        self.fixture.policy_path.write_text(
            "\n".join(viability.REQUIRED_POLICY_LINES[:-1]) + "\n",
            encoding="utf-8",
        )
        self.fixture.policy_sha = hashlib.sha256(
            self.fixture.policy_path.read_bytes()
        ).hexdigest()

        with self.assertRaisesRegex(
            viability.VoiceCompositionViabilityError, "missing rules"
        ):
            self.fixture.build_viability()

    def test_routing_receipt_tampering_is_rejected(self) -> None:
        receipt = self.fixture.create_routing_receipt()
        path = (
            self.fixture.root
            / routing.OUTPUT_DIRECTORY
            / f"{receipt['routing_id']}.json"
        )
        with path.open("ab") as stream:
            stream.write(b"tampered")

        with self.assertRaises(para.VoiceParalinguisticError):
            self.fixture.build_viability()

    def test_opened_viability_scope_is_rejected_even_when_rehashed(self) -> None:
        receipt, destination = self.fixture.build_viability()
        viability.write_viability_receipt(self.fixture.root, receipt, destination)
        tampered = json.loads(destination.read_text(encoding="utf-8"))
        tampered["scope_limits"]["composition_approved"] = True
        tampered["receipt_sha256"] = para._semantic_sha256(
            {
                key: value
                for key, value in tampered.items()
                if key != "receipt_sha256"
            }
        )
        destination.write_bytes(_pretty_bytes(tampered))

        with self.assertRaisesRegex(
            para.VoiceParalinguisticError,
            "composition_approved must remain false",
        ):
            viability.validate_viability_receipt(
                self.fixture.root, receipt["viability_id"]
            )

    def test_cli_requires_analysis_only_confirmation_before_execute(self) -> None:
        routing_receipt = self.fixture.create_routing_receipt()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = viability.main(
                [
                    "record",
                    "--voice-root",
                    str(self.fixture.root),
                    "--routing-id",
                    routing_receipt["routing_id"],
                    "--execute",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--confirm-analysis-only", stderr.getvalue())
        self.assertFalse((self.fixture.root / viability.OUTPUT_DIRECTORY).exists())
