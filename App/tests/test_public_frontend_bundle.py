from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from urllib.parse import urlparse

from scripts.prepare_public_frontend import IDENTITY_FILE, MANIFEST_FILE, prepare, verify_bundle

APP_ROOT = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Bundle Fixture",
            "-c",
            "user.email=bundle@invalid.local",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    ).stdout.strip()


def fixture_repo(directory: Path) -> Path:
    """Isolated Git index/commits; real immutable baseline objects, no shared edits."""
    repo = directory / "source"
    git(directory, "clone", "--shared", "--no-checkout", str(APP_ROOT.parent), str(repo))
    git(repo, "read-tree", "HEAD")
    # New review inputs can be tested before their integration commit. Remaining
    # source trees deliberately need not exist in this clone's working tree.
    names = [
        "config/public_frontend_release.json",
        "compat/public-0.9.6-r1.patch",
        "compat/public-0.9.6-r1.manifest.json",
    ]
    for name in names:
        target = repo / "App" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((APP_ROOT / name).read_bytes())
    git(repo, "add", "--", *["App/" + name for name in names])
    git(repo, "commit", "--allow-empty", "-m", "Synthetic committed frontend selector")
    return repo


def select(repo: Path, edition: str) -> None:
    path = repo / "App/config/public_frontend_release.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edition"] = edition
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    git(repo, "add", "--", "App/config/public_frontend_release.json")
    git(repo, "commit", "--allow-empty", "-m", "Synthetic selector change")


class PublicFrontendBundleTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.repo = fixture_repo(cls.root)
        select(cls.repo, "compat-096")
        cls.compat = cls.root / "compat"
        cls.compat_identity = prepare(cls.repo / "App", cls.compat)
        cls.compat_sha = git(cls.repo, "rev-parse", "HEAD")
        select(cls.repo, "current")
        cls.current = cls.root / "current"
        cls.current_identity = prepare(cls.repo / "App", cls.current)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_compat_full_tree_is_fingerprinted_and_original_patch_is_unchanged(self):
        original = (APP_ROOT / "compat/public-0.9.6.patch").read_bytes()
        self.assertEqual(
            hashlib.sha256(original).hexdigest(),
            "351c868c4b047f1775664ac964f6abb624caeba5df7b720c4af1641a66887d2d",
        )
        self.assertEqual(self.compat_identity["track"], "compat")
        self.assertEqual(self.compat_identity["version"], "compat-096-r1")
        self.assertEqual(
            verify_bundle(self.compat, expected_source_commit=self.compat_sha), self.compat_identity
        )
        self.assertIn(
            "composerDraftKey", (self.compat / "public_frontend/app.js").read_text(encoding="utf-8")
        )
        self.assertTrue((self.compat / "public_frontend/privacy/index.html").is_file())
        self.assertTrue((self.compat / "frontend/shared/immersive.css").is_file())
        self.assertTrue((self.compat / "frontend/assets/immersive/scenes/generic.svg").is_file())
        html = (self.compat / "public_frontend/index.html").read_text(encoding="utf-8")
        self.assertRegex(html, r"/app\.[0-9a-f]{16}\.js")
        self.assertRegex(html, r"/shared/immersive\.[0-9a-f]{16}\.css")
        self.assertNotIn("小吉终端", html)

    def test_current_and_compat_are_distinct_single_bundles(self):
        self.assertEqual(self.current_identity["track"], "current")
        self.assertNotEqual(self.current_identity["bundle_sha256"], self.compat_identity["bundle_sha256"])
        self.assertIn("小吉终端", (self.current / "public_frontend/index.html").read_text(encoding="utf-8"))
        self.assertEqual(
            {path.name for path in self.current.iterdir()},
            {"public_frontend", "frontend", IDENTITY_FILE, MANIFEST_FILE},
        )

    def test_build_is_deterministic_and_ignores_uncommitted_static_and_selector(self):
        source = self.repo / "App/public_frontend"
        source.mkdir(parents=True, exist_ok=True)
        (source / "app.js").write_text("uncommitted poison", encoding="utf-8")
        selector = self.repo / "App/config/public_frontend_release.json"
        original = selector.read_bytes()
        try:
            selector.write_text("not valid JSON", encoding="utf-8")
            again = self.root / "again"
            self.assertEqual(prepare(self.repo / "App", again), self.current_identity)
            self.assertEqual(
                (again / MANIFEST_FILE).read_bytes(), (self.current / MANIFEST_FILE).read_bytes()
            )
        finally:
            selector.write_bytes(original)

    def test_verify_rejects_tamper_missing_extra_and_wrong_source(self):
        for kind in ("tamper", "missing", "extra", "identity"):
            with self.subTest(kind=kind), TemporaryDirectory() as temporary:
                bundle = Path(temporary) / "bundle"
                shutil.copytree(self.compat, bundle)
                target = bundle / "public_frontend/app.js"
                if kind == "tamper":
                    target.write_bytes(target.read_bytes() + b"\nchanged")
                elif kind == "missing":
                    target.unlink()
                elif kind == "extra":
                    (bundle / "public_frontend/extra.js").write_bytes(b"unexpected")
                else:
                    value = json.loads((bundle / IDENTITY_FILE).read_bytes())
                    value["track"] = "current"
                    (bundle / IDENTITY_FILE).write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    verify_bundle(bundle)
        with self.assertRaisesRegex(ValueError, "another source commit"):
            verify_bundle(self.compat, expected_source_commit="f" * 40)

    def test_verify_works_with_backend_files_but_rejects_overwrite(self):
        with TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "app"
            shutil.copytree(self.compat, bundle)
            (bundle / "backend").mkdir()
            (bundle / "backend/main.py").write_text("# unrelated image code", encoding="utf-8")
            self.assertEqual(verify_bundle(bundle), self.compat_identity)
            with self.assertRaisesRegex(ValueError, "already exists"):
                prepare(self.repo / "App", bundle)

    def test_committed_delta_tampering_fails_before_output_publication(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = fixture_repo(root)
            select(repo, "compat-096")
            delta = repo / "App/compat/public-0.9.6-r1.patch"
            delta.write_bytes(delta.read_bytes() + b"\nunreviewed change\n")
            git(repo, "add", "--", "App/compat/public-0.9.6-r1.patch")
            git(repo, "commit", "-m", "Synthetic corrupted patch")
            with self.assertRaisesRegex(ValueError, "patch hash mismatch"):
                prepare(repo / "App", root / "rejected")
            self.assertFalse((root / "rejected").exists())

    def test_valid_patch_digest_cannot_hide_an_undeclared_file_change(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = fixture_repo(root)
            select(repo, "compat-096")
            target = "App/frontend/shared/immersive.css"
            before = git(repo, "show", "502ec99412bef843c37e4b31a53df8fa9faeb33c:" + target) + "\n"
            extra = "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    (before + "/* undeclared */\n").splitlines(True),
                    fromfile="a/" + target,
                    tofile="b/" + target,
                )
            )
            patch = repo / "App/compat/public-0.9.6-r1.patch"
            patch.write_bytes(patch.read_bytes() + extra.encode("utf-8"))
            manifest_path = repo / "App/compat/public-0.9.6-r1.manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["patch_sha256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            git(
                repo,
                "add",
                "--",
                "App/compat/public-0.9.6-r1.patch",
                "App/compat/public-0.9.6-r1.manifest.json",
            )
            git(repo, "commit", "-m", "Synthetic undeclared patch target")
            with self.assertRaisesRegex(ValueError, "paths differ"):
                prepare(repo / "App", root / "rejected")
            self.assertFalse((root / "rejected").exists())


@skipUnless(os.getenv("RUN_PUBLIC_E2E") == "1", "browser bundle regression tier")
class PublicFrontendBundleBrowserTests(TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import test_public_frontend_e2e as fixture

        cls.fixture = fixture
        cls.temporary = TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        repo = fixture_repo(cls.root)
        cls.bundles = {}
        for edition in ("compat-096", "current"):
            select(repo, edition)
            cls.bundles[edition] = cls.root / edition
            prepare(repo / "App", cls.bundles[edition])
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.PublicFrontendHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        self.fixture.PublicFrontendE2ETests.setUp(self)
        self.missing_assets = []

    def route_bundle(self, page, phase):
        def serve(route):
            path = urlparse(route.request.url).path
            if path.startswith("/public/v1/") or path.startswith("/media/") or path == "/announcements.json":
                # Use the existing synthetic API/media/announcement data fixture;
                # HTML, scripts, styles, icons, scenes and privacy stay bundled.
                route.continue_()
                return
            root = self.bundles[phase[0]]
            if path.startswith("/shared/"):
                target = root / "frontend" / path.lstrip("/")
            elif path.startswith("/assets/immersive/"):
                target = root / "frontend" / path.lstrip("/")
            else:
                target = root / "public_frontend" / path.lstrip("/")
                if path.endswith("/"):
                    target /= "index.html"
            # No fallback to canonical shared/privacy/module resources: this
            # exercises every static URL against the selected image tree.
            if target.is_file() and target.resolve().is_relative_to(root):
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                route.fulfill(status=200, content_type=content_type, body=target.read_bytes())
            else:
                self.missing_assets.append(path)
                route.fulfill(status=404, body="missing selected-bundle resource")

        # API requests must reach the fixture directly: its deterministic arrival
        # test waits on a server-side Event while Playwright is not dispatching
        # Python route callbacks. Intercepting even to continue would deadlock it.
        page.route(
            re.compile(
                r"^" + re.escape(self.base_url) + r"/(?!(?:public/v1/|media/|announcements\.json(?:\?|$)))"
            ),
            serve,
        )

    def open(self, page):
        page.goto(self.base_url, wait_until="networkidle")
        if page.locator("#accept-experience-notice").is_visible():
            page.locator("#accept-experience-notice").click()
        page.wait_for_function("document.querySelector('#connection-status').textContent === '服务已连接'")

    def test_complete_bundles_old_new_old_keep_v4_draft_pending_uuid_and_privacy(self):
        from tests.test_public_frontend_reliability import PublicFrontendReliabilityTests as helpers

        with self.fixture.sync_playwright() as playwright:
            browser = self.fixture._launch_browser(playwright)
            page = browser.new_page()
            phase = ["compat-096"]
            self.route_bundle(page, phase)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.open(page)
            helpers.seed(page, count=2)
            snapshot = {
                "request_id": "de305d54-75b4-431b-adb2-eb6b9e546014",
                "character_id": "25b23cb64398",
                "communication_channel": "text",
                "message": "synthetic pending",
                "state_package": "fixed-subject.signature",
                "local_day_key": "2026-09-07",
            }
            page.evaluate(
                """async snapshot => {
              const db=await new Promise(resolve=>{
                const r=indexedDB.open('project-snow-public',4);r.onsuccess=()=>resolve(r.result)
              });
              await new Promise(resolve=>{
                const tx=db.transaction('messages','readwrite');
                const store=tx.objectStore('messages');const r=store.get('fixture-000001');
                r.onsuccess=()=>store.put({...r.result,status:'pending',
                  requestId:snapshot.request_id,requestSnapshot:snapshot});
                tx.oncomplete=resolve
              });db.close();
            }""",
                snapshot,
            )
            for edition, draft in (
                ("compat-096", "S1兼容草稿"),
                ("current", "S2新版草稿"),
                ("compat-096", "回退草稿"),
            ):
                phase[0] = edition
                self.open(page)
                if edition == "current":
                    self.assertEqual(page.locator("#message-input").input_value(), "S1兼容草稿")
                elif draft == "回退草稿":
                    self.assertEqual(page.locator("#message-input").input_value(), "S2新版草稿")
                record = helpers.read_record(page, "messages", "fixture-000001")
                self.assertEqual(record["requestId"], snapshot["request_id"])
                self.assertEqual(record["requestSnapshot"], snapshot)
                self.assertEqual(helpers.stored_count(page), 2)
                page.locator("#message-input").fill(draft)
                helpers.wait_saved_draft(page, draft)
                page.goto(self.base_url + "/privacy/", wait_until="networkidle")
                self.assertTrue(page.locator("body").inner_text())
            self.assertEqual(self.fixture.PublicFrontendHandler.chat_payloads, [])
            self.assertEqual(errors, [])
            self.assertEqual(self.missing_assets, [])
            browser.close()

    def test_both_selected_bundles_preserve_typing_during_arrival_storage_completion(self):
        # Reuse the deterministic real-IDB transaction gate and channel/character
        # draft assertions already used to reproduce the main CI failure.
        method = (
            self.fixture.PublicFrontendE2ETests.test_arrival_storage_completion_preserves_newly_typed_draft
        )
        original_launch = self.fixture._launch_browser
        for edition in ("compat-096", "current"):
            with self.subTest(edition=edition):
                self.setUp()

                def launch(playwright, edition=edition):
                    browser = original_launch(playwright)
                    original_new_page = browser.new_page

                    def new_page(**kwargs):
                        page = original_new_page(**kwargs)
                        if edition == "compat-096":
                            # The old adapter assigns oncomplete; the current
                            # adapter uses addEventListener. Normalize the event
                            # registration only so the same deterministic gate
                            # can hold either adapter's real transaction event.
                            page.add_init_script("""const completions = new WeakMap();
                              Object.defineProperty(IDBTransaction.prototype, 'oncomplete', {
                                configurable:true,
                                get(){return completions.get(this) || null},
                                set(callback){
                                  const old = completions.get(this);
                                  if(old) this.removeEventListener('complete',old);
                                  completions.set(this,callback);
                                  if(callback) this.addEventListener('complete',callback);
                                }
                              });""")
                        self.route_bundle(page, [edition])
                        return page

                    browser.new_page = new_page
                    return browser

                self.fixture._launch_browser = launch
                try:
                    # The borrowed test calls this fixture's model helper only;
                    # responses stay synthetic and never reach a provider.
                    self._configure_model = self.fixture.PublicFrontendE2ETests._configure_model
                    method(self)
                    self.assertEqual(self.missing_assets, [])
                finally:
                    self.fixture._launch_browser = original_launch
