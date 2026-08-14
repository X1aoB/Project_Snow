from __future__ import annotations

import asyncio
from unittest import TestCase
from unittest.mock import patch

import httpx

from backend.snow_app.public_providers import PROVIDERS, ProviderRequestError, discover_models


class PublicProviderTests(TestCase):
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
