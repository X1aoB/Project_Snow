from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.prepare_embedding_context import INPUTS, prepare


class EmbeddingContextTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('# synthetic ' + name + '\n').encode())

    def test_legacy_missing_or_stale_image_rebuilds_after_failed_release(self):
        previous = prepare(self.root)['inputs_sha256']
        recipe = self.root / 'infra/embedding.Dockerfile'
        recipe.write_text(recipe.read_text() + 'ARG SYSTEM_SECURITY_REVISION=new\n')
        # The security commit failed before publication. A later test-only
        # commit says changed=false, but the registry still has the old image.
        for fingerprint in ('', '<no value>', previous):
            with self.subTest(fingerprint=fingerprint):
                self.assertTrue(prepare(self.root, fingerprint, changed=False)['rebuild'])

    def test_identical_context_reuses_but_explicit_security_rebuild_wins(self):
        fingerprint = prepare(self.root)['inputs_sha256']
        self.assertFalse(prepare(self.root, fingerprint)['rebuild'])
        self.assertTrue(prepare(self.root, fingerprint, changed=True)['rebuild'])

    def test_every_build_input_changes_identity_without_unrelated_application_files(self):
        fingerprint = prepare(self.root)['inputs_sha256']
        (self.root / 'unrelated.py').write_text('application_only = True\n')
        self.assertEqual(prepare(self.root)['inputs_sha256'], fingerprint)
        for name in INPUTS:
            with self.subTest(name=name):
                path = self.root / name
                raw = path.read_bytes()
                path.write_bytes(raw + b'# changed\n')
                self.assertTrue(prepare(self.root, fingerprint)['rebuild'])
                path.write_bytes(raw)

    def test_context_is_complete_and_never_deletes_unexpected_files(self):
        report = prepare(self.root)
        context = Path(report['context'])
        self.assertEqual({path.relative_to(context).as_posix() for path in context.rglob('*') if path.is_file()}, set(INPUTS))
        extra = context / 'must-preserve.txt'
        extra.write_text('unrelated')
        with self.assertRaises(ValueError):
            prepare(self.root)
        self.assertEqual(extra.read_text(), 'unrelated')

    def test_checkout_line_endings_do_not_change_canonical_context(self):
        fingerprint = prepare(self.root)['inputs_sha256']
        for name in INPUTS:
            path = self.root / name
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(prepare(self.root)['inputs_sha256'], fingerprint)
