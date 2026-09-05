from __future__ import annotations

from email.message import Message
from io import BytesIO
from unittest import TestCase
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from scripts import check_production_health as health


class ProductionHealthTests(TestCase):
    def response(self, body=b'{"status":"ok","version":"0.9.6"}', status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.headers = {"content-type": "application/json"}
        response.read.return_value = body
        return response

    def http_error(self, status, **headers):
        message = Message()
        for key, value in headers.items():
            message[key.replace('_', '-')] = value
        return HTTPError(health.DEFAULT_URL, status, "test", message, BytesIO(b"<html>blocked</html>"))

    @patch.object(health, "build_opener")
    def test_validates_application_liveness_and_identifies_probe(self, build):
        build.return_value.open.return_value = self.response()
        self.assertEqual(health.check(), "0.9.6")
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, health.DEFAULT_URL)
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(request.get_header("Cache-control"), "no-cache")
        self.assertIn("Project-Snow", request.get_header("User-agent"))
        self.assertIsInstance(build.call_args.args[0], health.NoRedirect)

    @patch.object(health.time, "sleep")
    @patch.object(health, "build_opener")
    def test_cloudflare_403_reports_edge_error_without_json_traceback(self, build, sleep):
        build.return_value.open.side_effect = self.http_error(
            403, server="cloudflare", cf_ray="test-ray", cf_mitigated="challenge",
        )
        with self.assertRaisesRegex(health.HealthCheckFailure, "HTTP 403.*cf-ray=test-ray.*Cloudflare challenged"):
            health.check()
        self.assertEqual(build.return_value.open.call_count, 1)
        sleep.assert_not_called()

    @patch.object(health.time, "sleep")
    @patch.object(health, "build_opener")
    def test_transient_network_and_upstream_errors_retry_then_recover(self, build, sleep):
        build.return_value.open.side_effect = [
            URLError("connection reset"), self.http_error(503), self.http_error(521), self.response(),
        ]
        self.assertEqual(health.check(), "0.9.6")
        self.assertEqual(sleep.call_count, 3)

    @patch.object(health.time, "sleep")
    @patch.object(health, "build_opener")
    def test_retries_are_bounded_and_outage_still_fails(self, build, sleep):
        build.return_value.open.side_effect = lambda *a, **kw: self.http_error(503)
        with self.assertRaisesRegex(health.HealthCheckFailure, "HTTP 503"):
            health.check()
        self.assertEqual(build.return_value.open.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    @patch.object(health, "build_opener")
    def test_redirect_to_login_is_not_followed_or_accepted(self, build):
        build.return_value.open.side_effect = self.http_error(302, location="https://login.example/")
        with self.assertRaisesRegex(health.HealthCheckFailure, "HTTP 302"):
            health.check()
        self.assertIsNone(health.NoRedirect().redirect_request(None, None, 302, "", {}, "https://login.example/"))

    @patch.object(health, "build_opener")
    def test_success_status_requires_valid_liveness_payload(self, build):
        cases = [b"", b"<html>challenge</html>", b"[]", b'{"status":"down","version":"0.9.6"}',
                 b'{"status":"ok"}', b'{"status":"ok","version":" "}', b"x" * (health.MAX_BODY_BYTES + 1)]
        for body in cases:
            with self.subTest(body=body[:60]):
                build.return_value.open.return_value = self.response(body)
                with self.assertRaises(health.HealthCheckFailure):
                    health.check()
