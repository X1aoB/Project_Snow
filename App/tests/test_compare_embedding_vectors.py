from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import TestCase

from scripts.compare_embedding_vectors import (
    DEFAULT_MIN_COSINE,
    EXPECTED_DIMENSION,
    SAMPLE_TEXTS,
    compare,
    payload,
)


def _write(directory: Path, name: str, vectors: list[list[float]]) -> Path:
    path = directory / name
    path.write_text(json.dumps({"vectors": vectors}), encoding="utf-8")
    return path


def _base_vectors(count: int = 3) -> list[list[float]]:
    # Deterministic, non-constant 512-dim unit-ish vectors.
    return [[float(((row * 7 + col * 3) % 11) + 1) for col in range(EXPECTED_DIMENSION)] for row in range(count)]


class ComparePayloadTests(TestCase):
    def test_payload_is_stable_and_non_empty(self) -> None:
        first = payload()
        second = payload()
        self.assertEqual(first, second)
        decoded = json.loads(first)
        self.assertIn("inputs", decoded)
        self.assertTrue(decoded["inputs"])
        self.assertEqual(decoded["inputs"], list(SAMPLE_TEXTS))
        self.assertTrue(all(isinstance(value, str) and value for value in decoded["inputs"]))


class CompareVectorsTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_identical_vectors_pass(self) -> None:
        vectors = _base_vectors()
        old = _write(self.dir, "old.json", vectors)
        new = _write(self.dir, "new.json", [list(v) for v in vectors])
        summary = compare(old, new, DEFAULT_MIN_COSINE)
        self.assertEqual(summary["count"], len(vectors))
        self.assertEqual(summary["dims"], EXPECTED_DIMENSION)
        self.assertGreaterEqual(summary["min_cosine"], DEFAULT_MIN_COSINE)
        self.assertEqual(summary["max_abs_diff"], 0.0)

    def test_tiny_noise_passes(self) -> None:
        vectors = _base_vectors()
        noised = [[value + (1e-7 if col == 0 else 0.0) for col, value in enumerate(row)] for row in vectors]
        old = _write(self.dir, "old.json", vectors)
        new = _write(self.dir, "new.json", noised)
        summary = compare(old, new, DEFAULT_MIN_COSINE)
        self.assertGreaterEqual(summary["min_cosine"], DEFAULT_MIN_COSINE)
        self.assertGreater(summary["max_abs_diff"], 0.0)

    def test_one_degraded_vector_fails(self) -> None:
        vectors = _base_vectors()
        degraded = [list(v) for v in vectors]
        # Rotate one vector hard so its cosine drops well below the threshold.
        degraded[1] = [float(EXPECTED_DIMENSION - col) for col in range(EXPECTED_DIMENSION)]
        old = _write(self.dir, "old.json", vectors)
        new = _write(self.dir, "new.json", degraded)
        with self.assertRaisesRegex(ValueError, "cosine"):
            compare(old, new, DEFAULT_MIN_COSINE)

    def test_wrong_dimension_fails(self) -> None:
        vectors = _base_vectors()
        wrong = [list(v) for v in vectors]
        wrong[0] = wrong[0][:-1]
        old = _write(self.dir, "old.json", vectors)
        new = _write(self.dir, "new.json", wrong)
        with self.assertRaisesRegex(ValueError, "dims"):
            compare(old, new, DEFAULT_MIN_COSINE)

    def test_count_mismatch_fails(self) -> None:
        old = _write(self.dir, "old.json", _base_vectors(3))
        new = _write(self.dir, "new.json", _base_vectors(2))
        with self.assertRaisesRegex(ValueError, "count differs"):
            compare(old, new, DEFAULT_MIN_COSINE)

    def test_nan_fails(self) -> None:
        vectors = _base_vectors()
        nan_rows = [list(v) for v in vectors]
        nan_rows[0][0] = float("nan")
        old = _write(self.dir, "old.json", vectors)
        new = _write(self.dir, "new.json", nan_rows)
        with self.assertRaisesRegex(ValueError, "non-finite"):
            compare(old, new, DEFAULT_MIN_COSINE)
