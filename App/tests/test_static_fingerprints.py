from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.fingerprint_public_frontend import FINGERPRINT_LENGTH, fingerprint


class StaticFingerprintTests(TestCase):
    def test_real_frontend_manifest_fingerprints_modules_and_brand_icons(self):
        source = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for path in ["public_frontend", "frontend/shared", "frontend/assets/immersive/scenes"]:
                shutil.copytree(source/path,root/path)
            result = fingerprint(root)
            for name in ["xiaoji-icon.png","xiaoji-icon-192.png","xiaoji-icon-32.png","xiaoji-icon.ico"]:
                url = result["assets"][f"/shared/brand/{name}"]
                self.assertTrue((root/"frontend"/url.lstrip("/")).is_file())
            module_url = result["assets"]["/modules/runtime.js"]
            self.assertIn(module_url,(root/"public_frontend/app.js").read_text(encoding="utf-8"))
            self.assertEqual(result,fingerprint(root))

    def test_build_copies_content_addressed_assets_and_rewrites_only_html(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            assets = {
                "public_frontend/app.js": (
                    b'const SCENE_KEYS = new Set(["generic", "lounge"]);\n'
                    b'function render(visualKey) {\n  $("scene-backdrop").src = '
                    b'`/assets/immersive/scenes/${visualKey}.svg`;\n}\n'
                ),
                "public_frontend/app.css": b"body { color: #123; }\n",
                "public_frontend/privacy/privacy.js": b"export const privacy = true;\n",
                "frontend/shared/immersive.css": b":root { --snow: 1; }\n",
            }
            for relative_path, payload in assets.items():
                path = root / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            scene_payloads = {
                "generic": b"<svg><title>generic</title></svg>\n",
                "lounge": b"<svg><title>lounge</title></svg>\n",
            }
            for scene_name, payload in scene_payloads.items():
                path = root / "frontend/assets/immersive/scenes" / f"{scene_name}.svg"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            (root / "public_frontend/index.html").write_text(
                '<link href="/shared/immersive.css?v=0.9.2">'
                '<link href="/app.css?v=0.9.2">'
                '<img src="/assets/immersive/scenes/generic.svg">'
                '<script src="/app.js?v=0.9.2"></script>',
                encoding="utf-8",
            )
            (root / "public_frontend/privacy/index.html").write_text(
                '<link href="/shared/immersive.css?v=0.9.2">'
                '<link href="/app.css?v=0.9.2">'
                '<script src="/privacy/privacy.js?v=0.9.2"></script>',
                encoding="utf-8",
            )

            result = fingerprint(root)
            repeated = fingerprint(root)

            self.assertEqual(result, repeated)
            self.assertEqual(result["fingerprint_length"], FINGERPRINT_LENGTH)
            for relative_path, payload in assets.items():
                source = root / relative_path
                expected_payload = (
                    source.read_bytes()
                    if relative_path == "public_frontend/app.js"
                    else payload
                )
                digest = hashlib.sha256(expected_payload).hexdigest()[:FINGERPRINT_LENGTH]
                fingerprinted = source.with_name(
                    f"{source.stem}.{digest}{source.suffix}"
                )
                if relative_path != "public_frontend/app.js":
                    self.assertEqual(source.read_bytes(), payload)
                self.assertEqual(fingerprinted.read_bytes(), expected_payload)

            built_app_js = (root / "public_frontend/app.js").read_text(encoding="utf-8")
            self.assertEqual(built_app_js.encode(), assets["public_frontend/app.js"])
            self.assertEqual(set(result["scene_assets"]), set(scene_payloads))
            for scene_name, scene_url in result["scene_assets"].items():
                self.assertRegex(
                    scene_url,
                    rf"^/assets/immersive/scenes/{scene_name}\.[0-9a-f]{{16}}\.svg$",
                )
                scene_path = root / "frontend" / scene_url.removeprefix("/")
                self.assertTrue(scene_path.is_file(), scene_url)
                self.assertEqual(scene_path.read_bytes(), scene_payloads[scene_name])

            rendered_html = "\n".join(
                (root / relative_path).read_text(encoding="utf-8")
                for relative_path in (
                    "public_frontend/index.html",
                    "public_frontend/privacy/index.html",
                )
            )
            self.assertNotIn("?v=", rendered_html)
            self.assertEqual(
                len(re.findall(r"\.[0-9a-f]{16}\.(?:css|js)", rendered_html)),
                6,
            )
            self.assertIn(result["scene_assets"]["generic"], rendered_html)
            scene_manifest = re.search(r'<meta name="snow-scene-assets" content="([^"]+)"', rendered_html)
            self.assertIsNotNone(scene_manifest)
            self.assertEqual(json.loads(html.unescape(scene_manifest.group(1))), result["scene_assets"])
            self.assertNotIn(
                'src="/assets/immersive/scenes/generic.svg"', rendered_html
            )

    def test_declared_module_dependencies_are_hashed_before_entry_without_source_rewriting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "public_frontend"
            (public / "modules").mkdir(parents=True)
            (public / "privacy").mkdir()
            scenes = root / "frontend/assets/immersive/scenes"
            scenes.mkdir(parents=True)
            (scenes / "generic.svg").write_text("<svg />")
            module = b"export const mode = 'native';"
            (public / "modules/runtime.js").write_bytes(module)
            (public / "app.js").write_text('import {mode} from "/modules/runtime.js"; console.log(mode);')
            (public / "index.html").write_text('<head></head><script src="/app.js"></script>')
            (public / "privacy/index.html").write_text("<head></head>")
            (public / "assets-manifest.json").write_text(json.dumps({
                "schema_version": "project-snow-frontend-build-1", "assets": [
                    {"source": "public_frontend/modules/runtime.js", "public_url": "/modules/runtime.js", "required_html": False},
                    {"source": "public_frontend/app.js", "public_url": "/app.js", "dependencies": ["/modules/runtime.js"]},
                ],
            }))
            result = fingerprint(root)
            self.assertEqual(result, fingerprint(root))
            self.assertIn(result["assets"]["/modules/runtime.js"], (public / "app.js").read_text())
            self.assertEqual((public / "modules/runtime.js").read_bytes(), module)
            self.assertIn("snow-scene-assets", (public / "index.html").read_text())
