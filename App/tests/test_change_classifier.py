from __future__ import annotations

from unittest import TestCase

from scripts.classify_changes import classify


class ChangeClassifierTests(TestCase):
    def test_docs_only_does_not_build_runtime_images(self) -> None:
        result = classify(["README.md", "docs/deployment.md"])
        self.assertTrue(result["docs_only"])
        self.assertFalse(result["app_image"])
        self.assertFalse(result["embedding"])

    def test_public_ui_builds_only_the_application_image(self) -> None:
        result = classify(["App/public_frontend/app.js"])
        self.assertTrue(result["ui"])
        self.assertTrue(result["app_image"])
        self.assertFalse(result["embedding"])

    def test_shared_immersive_assets_build_the_application_image(self) -> None:
        result = classify([
            "App/frontend/shared/immersive.css",
            "App/frontend/assets/immersive/scenes/generic.svg",
        ])
        self.assertTrue(result["ui"])
        self.assertTrue(result["app_image"])

    def test_embedding_isolated_from_application_image(self) -> None:
        result = classify(["App/infra/embedding_service.py"])
        self.assertTrue(result["embedding"])
        self.assertFalse(result["app_image"])

    def test_deployment_change_runs_deploy_contracts(self) -> None:
        result = classify(["App/ops/deploy.sh"])
        self.assertTrue(result["deploy"])
        self.assertFalse(result["app_image"])

    def test_release_artifact_digest_index_is_a_deployment_change(self) -> None:
        result = classify(["App/config/public_release_artifacts.json"])
        self.assertTrue(result["deploy"])
        self.assertFalse(result["app_image"])

    def test_root_release_helpers_are_deployment_changes(self) -> None:
        for path in (
            "App/scripts/bootstrap-release-runner.ps1",
            "App/scripts/bootstrap_release_host.py",
            "App/scripts/cloudflare_origin_firewall.py",
            "App/scripts/install_release_archive.py",
            "App/infra/OriginEdge.Caddyfile",
            "App/ops/project-snow-origin-firewall.service",
            "App/ops/project-snow-origin-firewall.timer",
        ):
            with self.subTest(path=path):
                result = classify([path])
                self.assertTrue(result["deploy"])
                self.assertFalse(result["app_image"])

    def test_promotion_script_is_a_deployment_change(self) -> None:
        result = classify(["App/scripts/promote.ps1"])
        self.assertTrue(result["deploy"])
        self.assertFalse(result["app_image"])

    def test_unknown_source_change_is_conservative(self) -> None:
        result = classify(["App/new_runtime_component.py"])
        self.assertTrue(result["api"])
        self.assertTrue(result["app_image"])

    def test_main_full_gate_does_not_force_embedding_rebuild(self) -> None:
        result = classify(["App/public_frontend/app.css"], force_full=True)
        self.assertTrue(result["full"])
        self.assertTrue(result["api"])
        self.assertTrue(result["data"])
        self.assertFalse(result["embedding"])
