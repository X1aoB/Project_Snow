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
