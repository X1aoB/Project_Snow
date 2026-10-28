from __future__ import annotations

import asyncio
from unittest import TestCase
from unittest.mock import patch

import httpx

from backend.snow_app.public_providers import (
    PROVIDERS,
    ProviderHTTPPool,
    ProviderRequestError,
    discover_models,
)


class PublicProviderTests(TestCase):
    def test_process_pool_reuses_client_until_closed(self) -> None:
        created = []

        class Client:
            def __init__(self, **_kwargs):
                self.closed = False

            async def get(self, url, **_kwargs):
                return httpx.Response(200, json={"data": []}, request=httpx.Request("GET", url))

            async def post(self, url, **_kwargs):
                return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=httpx.Request("POST", url))

            async def aclose(self):
                self.closed = True

        def factory(**kwargs):
            client = Client(**kwargs)
            created.append(client)
            return client

        async def exercise() -> None:
            pool = ProviderHTTPPool()
            await pool.get("https://example.invalid/one")
            await pool.get("https://example.invalid/two")
            self.assertEqual(len(created), 1)
            await pool.close()
            self.assertTrue(created[0].closed)

        with patch("backend.snow_app.public_providers.httpx.AsyncClient", side_effect=factory):
            asyncio.run(exercise())

    def test_redirect_is_rejected_without_following_location(self) -> None:
        request = httpx.Request("GET", "https://api.openai.com/v1/models")
        response = httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}, request=request)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                return response

        with patch("backend.snow_app.public_providers.httpx.AsyncClient", return_value=Client()):
            with self.assertRaisesRegex(ProviderRequestError, "provider_redirect_rejected"):
                asyncio.run(discover_models(PROVIDERS["openai"], "sk-test"))

    def test_fixed_adapter_url_is_used_for_model_discovery(self) -> None:
        captured = {}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **_kwargs):
                captured["url"] = url
                return httpx.Response(
                    200,
                    json={"data": [{"id": "gpt-test"}]},
                    request=httpx.Request("GET", url),
                )

        with patch("backend.snow_app.public_providers.httpx.AsyncClient", return_value=Client()):
            models = asyncio.run(discover_models(PROVIDERS["openai"], "sk-test"))
        self.assertEqual(captured["url"], "https://api.openai.com/v1/models")
        self.assertEqual(models, ["gpt-test"])

    def test_new_official_specs_present_with_metadata(self) -> None:
        for provider_id, host in (
            ("anthropic", "https://api.anthropic.com/v1/"),
            ("xai", "https://api.x.ai/v1"),
            ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
        ):
            spec = PROVIDERS[provider_id]
            self.assertEqual(spec.base_url, host)
            self.assertEqual(spec.kind, "official")
            self.assertTrue(spec.console_url)
            self.assertTrue(spec.documentation_url and spec.privacy_url)
        self.assertTrue(PROVIDERS["anthropic"].caveat)

    def test_relay_sentinel_and_builder(self) -> None:
        from backend.snow_app.public_providers import relay_provider_spec
        self.assertEqual(PROVIDERS["relay"].kind, "relay")
        self.assertTrue(PROVIDERS["relay"].caveat)
        spec = relay_provider_spec("https://relay.example.com/v1")
        self.assertEqual(spec.provider_id, "relay")
        self.assertEqual(spec.kind, "relay")
        self.assertEqual(spec.base_url, "https://relay.example.com/v1")

    def test_anthropic_sends_dual_auth_headers(self) -> None:
        captured: dict[str, object] = {}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **kwargs):
                captured["headers"] = kwargs.get("headers")
                return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5"}]},
                                      request=httpx.Request("GET", url))

        with patch("backend.snow_app.public_providers.httpx.AsyncClient", return_value=Client()):
            asyncio.run(discover_models(PROVIDERS["anthropic"], "sk-ant"))
        headers = captured["headers"]
        self.assertEqual(headers["Authorization"], "Bearer sk-ant")
        self.assertEqual(headers["x-api-key"], "sk-ant")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")

    def relay_transport(self, handler):
        """Patch the module's AsyncClient so real httpx streaming runs over *handler*."""
        real_client = httpx.AsyncClient

        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        return patch("backend.snow_app.public_providers.httpx.AsyncClient", side_effect=factory)

    def test_relay_response_byte_cap_stops_reading_early(self) -> None:
        from backend.snow_app.public_providers import (
            RELAY_RESPONSE_BYTE_CAP,
            relay_provider_spec,
            simple_completion,
        )

        sent = []

        async def oversized():
            # Far more than the cap; a buffering client would read all of it.
            for _ in range(4 * RELAY_RESPONSE_BYTE_CAP // 65536):
                sent.append(65536)
                yield b"x" * 65536

        spec = relay_provider_spec("https://relay.example.com/v1")
        with self.relay_transport(lambda request: httpx.Response(200, content=oversized())):
            with self.assertRaises(ProviderRequestError) as caught:
                asyncio.run(simple_completion(spec, "sk-relay", "model-x",
                                              system_prompt="s", user_prompt="u"))
        self.assertEqual(caught.exception.code, "provider_response_too_large")
        self.assertLessEqual(sum(sent), RELAY_RESPONSE_BYTE_CAP + 2 * 65536)

    def test_relay_completion_and_discovery_under_the_cap_still_parse(self) -> None:
        from backend.snow_app.public_providers import relay_provider_spec, simple_completion

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, str(request.url), request.headers.get("Authorization")))
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "relay-b"}, {"id": "relay-a"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": " 你好 "}}],
                                             "usage": {"total_tokens": 3}})

        spec = relay_provider_spec("https://relay.example.com/v1")
        with self.relay_transport(handler):
            models = asyncio.run(discover_models(spec, "sk-relay"))
            content, usage = asyncio.run(simple_completion(spec, "sk-relay", "relay-a",
                                                           system_prompt="s", user_prompt="u"))
        self.assertEqual(models, ["relay-a", "relay-b"])
        self.assertEqual((content, usage), ("你好", {"total_tokens": 3}))
        self.assertEqual(seen, [
            ("GET", "https://relay.example.com/v1/models", "Bearer sk-relay"),
            ("POST", "https://relay.example.com/v1/chat/completions", "Bearer sk-relay"),
        ])

    def test_relay_discovery_is_capped_and_redirects_are_not_followed(self) -> None:
        from backend.snow_app.public_providers import RELAY_RESPONSE_BYTE_CAP, relay_provider_spec

        spec = relay_provider_spec("https://relay.example.com/v1")
        huge = b"[" + b" " * (RELAY_RESPONSE_BYTE_CAP + 1) + b"]"
        with self.relay_transport(lambda request: httpx.Response(200, content=huge)):
            with self.assertRaises(ProviderRequestError) as caught:
                asyncio.run(discover_models(spec, "sk-relay"))
        self.assertEqual(caught.exception.code, "provider_response_too_large")
        redirect = httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        with self.relay_transport(lambda request: redirect):
            with self.assertRaises(ProviderRequestError) as caught:
                asyncio.run(discover_models(spec, "sk-relay"))
        self.assertEqual(caught.exception.code, "provider_redirect_rejected")

    def test_gemini_discovery_returns_bare_model_ids(self) -> None:
        listing = {"data": [{"id": "models/gemini-2.5-flash"}, {"id": "models/gemini-2.5-pro"},
                            {"id": "gemini-2.0-flash"}]}
        with self.relay_transport(lambda request: httpx.Response(200, json=listing)):
            models = asyncio.run(discover_models(PROVIDERS["gemini"], "gm-key"))
        self.assertEqual(models, ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-pro"])

    def test_pool_never_streams_to_the_subscription_broker(self) -> None:
        from backend.snow_app.public_subscription import SubscriptionError

        async def exercise() -> None:
            pool = ProviderHTTPPool(subscription_broker=object())
            async with pool.stream("POST", "https://codex-subscription.invalid/v1/chat/completions"):
                raise AssertionError("the broker host must not be streamed")

        with self.assertRaises(SubscriptionError):
            asyncio.run(exercise())
