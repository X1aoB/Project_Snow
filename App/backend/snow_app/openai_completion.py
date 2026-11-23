"""OpenAI Chat Completions compatibility for the exact official endpoint."""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import openai_parameter_adapter_enabled
from .model_capabilities import immersive_model_policy

_SAMPLING = {"temperature", "top_p", "logprobs", "top_logprobs"}
_OPTIONAL = _SAMPLING | {"max_tokens", "reasoning_effort", "response_format", "stream_options"}
_CODES = {"unsupported_parameter", "unsupported_value", "insufficient_quota", "model_not_found",
          "rate_limit_exceeded", "invalid_api_key", "permission_denied", "server_error", "unsupported_country_region_territory",
          "credit_balance_exhausted", "project_spend_limit_exceeded", "organization_spend_limit_exceeded",
          "organization_usage_limit_exceeded"}
_TYPES = {"invalid_request_error", "insufficient_quota", "rate_limit_error", "server_error",
          "permission_error", "authentication_error"}
_LEARNED: OrderedDict[bytes, frozenset[str]] = OrderedDict()
_LOCK = Lock()
_LOG = logging.getLogger("snow.public")

NON_RETRYABLE = frozenset({"provider_quota_exhausted", "provider_model_unavailable", "provider_parameter_rejected",
                           "provider_region_unavailable"})


@dataclass(frozen=True)
class ProviderFailure:
    http_status: int
    error_type: str
    error_code: str
    error_param: str
    public_code: str
    attempts: int

    def log(self, profile: str) -> None:
        _LOG.info(json.dumps({"event": "public_provider_failure", "provider": "openai", "model_profile": profile,
            "http_status": self.http_status, "error_type": self.error_type, "error_code": self.error_code,
            "error_param": self.error_param}))

    @property
    def public_status(self) -> int:
        return {"provider_quota_exhausted": 429, "provider_rate_limited": 429,
                "provider_model_unavailable": 403, "provider_region_unavailable": 403, "provider_unavailable": 503,
                "provider_credential_rejected": 401}.get(self.public_code, 502)


def _allowed(value: Any, choices: set[str]) -> str:
    return value if isinstance(value, str) and value in choices else ""


def official_chat_endpoint(endpoint: str) -> bool:
    url = urlsplit(endpoint)
    return (url.scheme == "https" and url.netloc.casefold() in {"api.openai.com", "api.openai.com:443"}
            and url.path == "/v1/chat/completions" and not url.query and not url.fragment)


def safe_error_fields(details: dict[str, Any]) -> dict[str, str]:
    return {"error_type": _allowed(details.get("type"), _TYPES),
            "error_code": _allowed(details.get("code"), _CODES),
            "error_param": _allowed(details.get("param"), _OPTIONAL)}


class CompletionRequest:
    def __init__(self, endpoint: str, body: dict[str, Any], *, required_field: str = "", relay: bool = False):
        self.required_field = required_field
        self.active = not relay and official_chat_endpoint(endpoint) and openai_parameter_adapter_enabled()
        self.body = dict(body)
        self.attempts = 1
        if not self.active:
            return
        model = str(body["model"])
        self.body.pop("thinking", None)
        self.body.pop("enable_thinking", None)
        self.key = hashlib.sha256(model.encode("utf-8")).digest()
        policy = immersive_model_policy("openai", model)
        self.profile = model.strip().casefold() if policy["capability_verified"] else "other"
        if "max_tokens" in self.body:
            self.body["max_completion_tokens"] = self.body.pop("max_tokens")
        effort = policy["request_fields"].get("reasoning_effort")
        if effort is None:
            self.body.pop("reasoning_effort", None)
        else:
            self.body.setdefault("reasoning_effort", effort)
        if policy["omit_temperature"] or self.body.get("reasoning_effort") not in {None, "none"}:
            for field in _SAMPLING:
                self.body.pop(field, None)
        with _LOCK:
            learned = _LEARNED.get(self.key, ())
        for field in learned:
            if field != self.required_field:
                self._omit(field)

    def _omit(self, field: str) -> None:
        self.body.pop(field, None)
        if field == "reasoning_effort":
            # A model's default effort may enable reasoning again.
            for sampling_field in _SAMPLING:
                self.body.pop(sampling_field, None)

    @staticmethod
    def classify(response: httpx.Response, attempts: int, *, discovery: bool = False) -> ProviderFailure:
        details = {}
        try:
            data = response.json()
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                details = data["error"]
        except (ValueError, TypeError):
            pass
        status = response.status_code
        fields = safe_error_fields(details)
        error_type = fields["error_type"]
        code = fields["error_code"]
        param = fields["error_param"]
        if status == 429:
            exhausted = code in {"insufficient_quota", "credit_balance_exhausted", "project_spend_limit_exceeded",
                                 "organization_spend_limit_exceeded", "organization_usage_limit_exceeded"}
            public = "provider_quota_exhausted" if exhausted or error_type == "insufficient_quota" else "provider_rate_limited"
        elif status == 403 and code == "unsupported_country_region_territory":
            public = "provider_region_unavailable"
        elif status == 403 and discovery:
            public = "provider_model_discovery_failed"
        elif status == 403 or (status == 404 and code == "model_not_found"):
            public = "provider_model_unavailable"
        elif status == 401:
            public = "provider_credential_rejected"
        elif status == 400 and attempts > 1:
            public = "provider_parameter_rejected"
        elif status >= 500:
            public = "provider_unavailable"
        else:
            public = "provider_model_discovery_failed" if discovery else "provider_request_failed"
        return ProviderFailure(status, error_type, code, param, public, attempts)

    def retry_validation(self, response: httpx.Response, *, budget_available=True) -> bool:
        if not self.active or response.status_code != 400 or self.attempts != 1:
            return False
        failure = self.classify(response, self.attempts)
        field = failure.error_param
        if (failure.error_type != "invalid_request_error" or failure.error_code not in
                {"unsupported_parameter", "unsupported_value"} or not field or field not in self.body):
            return False
        # Store only a digest and an allowlisted field. No user or credential
        # information can reach this bounded, process-local cache.
        with _LOCK:
            _LEARNED[self.key] = _LEARNED.get(self.key, frozenset()) | {field}
            _LEARNED.move_to_end(self.key)
            if len(_LEARNED) > 256:
                _LEARNED.popitem(last=False)
        if not budget_available or field == self.required_field:
            return False
        failure.log(self.profile)
        self.body = self.body.copy()
        self._omit(field)
        self.attempts += 1
        return True

    def logged_failure(self, response: httpx.Response) -> ProviderFailure:
        error = self.classify(response, self.attempts)
        error.log(self.profile)
        return error


def post_completion(endpoint: str, *, json: dict[str, Any], required_field: str = "", **kwargs) -> httpx.Response:
    """Use the shared adapter for private workbench calls too."""
    request = CompletionRequest(endpoint, json, required_field=required_field)
    response = httpx.post(endpoint, json=request.body, **kwargs)
    if request.retry_validation(response):
        response = httpx.post(endpoint, json=request.body, **kwargs)
    return response


@contextmanager
def completion_stream(request, open_stream, *, budget_available=lambda: True):
    with ExitStack() as stack:
        response = stack.enter_context(open_stream(request.body))
        if request.active and response.is_error:
            response.read()
            if request.retry_validation(response, budget_available=budget_available()):
                stack.close()
                response = stack.enter_context(open_stream(request.body))
                if response.is_error:
                    response.read()
        yield response

