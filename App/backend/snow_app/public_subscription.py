"""Ephemeral, single-process relay for a user's own local Codex connector.

No OpenAI login material or upstream URLs enter this broker. Tokens, pairing
codes and jobs live only in bounded memory; restart requires fresh pairing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import ConfigDict, Field, model_validator

from .public_contracts import ReasoningEffort, StrictModel
from .public_providers import ProviderRequestError
from .public_security import issue_byok_credential

PROVIDER = "codex_subscription"
MODEL = "gpt-5.6-luna"
EFFORT = "max"
BASE_URL = "https://codex-subscription.invalid/v1"
PROTOCOL_VERSION = 2
PAIR_SECONDS = 300
MAX_CONNECTIONS = 128
MAX_JOB_BYTES = 256 * 1024
SAFE_ERRORS = frozenset({
    "subscription_result_unknown", "subscription_not_connected",
    "subscription_model_unavailable", "subscription_effort_unavailable", "subscription_login_required",
    "subscription_rate_limited", "subscription_generation_failed",
    "subscription_protocol_error", "subscription_connector_stopped",
})


class SubscriptionError(ProviderRequestError):
    def __init__(self, code: str, status_code: int = 409, *, submitted: bool = False):
        super().__init__(code, status_code)
        self.submitted = submitted


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class _Job:
    job_id: str
    wire: dict[str, Any]
    deadline: float
    model: str
    future: Future = field(default_factory=Future)
    delivered: bool = False
    terminal: bool = False


@dataclass
class _Connection:
    subject: str
    relay_hash: str
    pairing_hash: str
    pairing_expires: float
    expires: float
    connector_hash: str = ""
    protocol_version: int = PROTOCOL_VERSION
    models: list[dict[str, Any]] = field(default_factory=list)
    last_seen: float = 0
    blocked: bool = False
    job: _Job | None = None
    poll_waiter: Future | None = None
    receipts: dict[str, float] = field(default_factory=dict)


class SubscriptionBroker:
    def __init__(self, enabled: bool = False, *, clock=time.time, max_connections=MAX_CONNECTIONS):
        try:
            one_worker = int(os.getenv("WEB_CONCURRENCY", "1")) == 1
        except ValueError:
            one_worker = False
        self.enabled = bool(enabled and one_worker)
        self._clock = clock
        self._max_connections = max_connections
        self._lock = threading.RLock()
        self._subjects: dict[str, _Connection] = {}
        self._relays: dict[str, _Connection] = {}
        self._pairings: dict[str, _Connection] = {}
        self._connectors: dict[str, _Connection] = {}

    def require_enabled(self):
        if not self.enabled:
            raise SubscriptionError("subscription_disabled", 404)

    def _wake(self, connection):
        if connection.poll_waiter and not connection.poll_waiter.done():
            connection.poll_waiter.set_result(None)

    def _remove(self, connection):
        self._subjects.pop(connection.subject, None)
        self._relays.pop(connection.relay_hash, None)
        self._pairings.pop(connection.pairing_hash, None)
        self._connectors.pop(connection.connector_hash, None)
        connection.blocked = True
        self._wake(connection)
        if connection.job and not connection.job.future.done():
            connection.job.terminal = True
            connection.job.wire = {}
            connection.job.future.set_exception(SubscriptionError("subscription_result_unknown", submitted=True))

    def _prune(self):
        now = self._clock()
        for connection in list(self._subjects.values()):
            if connection.expires <= now or (not connection.connector_hash and connection.pairing_expires <= now):
                self._remove(connection)
            else:
                connection.receipts = {key: expires for key, expires in connection.receipts.items() if expires > now}

    def pair(self, subject: str, lifetime_seconds: int) -> tuple[str, str, float]:
        self.require_enabled()
        with self._lock:
            self._prune()
            previous = self._subjects.get(subject)
            if previous:
                self._remove(previous)
            if len(self._subjects) >= self._max_connections:
                raise SubscriptionError("subscription_capacity_reached", 503)
            relay, code = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            now = self._clock()
            connection = _Connection(subject, _digest(relay), _digest(code), now + PAIR_SECONDS,
                                     now + max(1, min(43200, lifetime_seconds)))
            self._subjects[subject] = connection
            self._relays[connection.relay_hash] = connection
            self._pairings[connection.pairing_hash] = connection
            return relay, code, connection.expires

    def connect(self, code: str, *, protocol_version: int = 1,
                models: list[dict[str, Any]] | None = None) -> tuple[str, float]:
        self.require_enabled()
        catalogue = normalize_catalogue(models, protocol_version)
        with self._lock:
            self._prune()
            connection = self._pairings.pop(_digest(code), None)
            if not connection or connection.connector_hash:
                raise SubscriptionError("subscription_pairing_expired", 401)
            token = secrets.token_urlsafe(32)
            connection.connector_hash = _digest(token)
            connection.protocol_version = protocol_version
            connection.models = catalogue
            connection.last_seen = self._clock()
            self._connectors[connection.connector_hash] = connection
            return token, connection.expires

    def _connected(self, connection):
        return bool(connection.connector_hash and not connection.blocked and (
            (connection.job and not connection.job.terminal) or self._clock() - connection.last_seen <= 45
        ))

    def status(self, subject):
        with self._lock:
            self._prune()
            connection = self._subjects.get(subject)
            status = "disconnected"
            if self.enabled and connection:
                status = "connected" if self._connected(connection) else (
                    "waiting" if not connection.connector_hash else "disconnected"
                )
            return {"status": status, "model": MODEL, "effort": EFFORT,
                    "models": json.loads(json.dumps(connection.models)) if status == "connected" else [],
                    "protocol_version": connection.protocol_version if connection else PROTOCOL_VERSION,
                    "expires_at": _timestamp(connection.expires) if connection else None}

    def disconnect(self, subject):
        with self._lock:
            connection = self._subjects.get(subject)
            if connection:
                self._remove(connection)

    def authenticate(self, token: str) -> str:
        self.require_enabled()
        with self._lock:
            self._prune()
            connection = self._connectors.get(_digest(token))
            if not connection:
                raise SubscriptionError("credential_invalid", 401)
            return connection.connector_hash

    def catalogue(self, relay: str) -> list[dict[str, Any]]:
        self.require_enabled()
        with self._lock:
            self._prune()
            connection = self._relays.get(_digest(relay))
            if not connection or not self._connected(connection):
                raise SubscriptionError("subscription_not_connected")
            return json.loads(json.dumps(connection.models))

    def preflight(self, relay: str, model: str, effort: str | None = None) -> str:
        self.require_enabled()
        with self._lock:
            self._prune()
            connection = self._relays.get(_digest(relay))
            if not connection or not self._connected(connection):
                raise SubscriptionError("subscription_not_connected")
            candidate = next((item for item in connection.models if item["id"] == model), None)
            if candidate is None:
                raise SubscriptionError("subscription_model_unavailable", 422)
            selected_effort = effort if effort is not None else (
                EFFORT if model == MODEL else candidate["default_reasoning_effort"]
            )
            if selected_effort not in candidate["reasoning_efforts"]:
                raise SubscriptionError("subscription_effort_unavailable", 422)
            if connection.job and not connection.job.terminal:
                raise SubscriptionError("subscription_busy")
            return selected_effort

    async def poll(self, token: str, *, wait_seconds: float = 20):
        connector_hash = self.authenticate(token)
        with self._lock:
            connection = self._connectors.get(connector_hash)
            if connection is None:
                raise SubscriptionError("credential_invalid", 401)
            if connection.poll_waiter is not None:
                raise SubscriptionError("subscription_poll_in_progress", 429)
            connection.last_seen = self._clock()
            if connection.blocked:
                raise SubscriptionError("subscription_result_unknown")
            waiter = Future()
            connection.poll_waiter = waiter
            if connection.job and not connection.job.terminal:
                waiter.set_result(None)
        try:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(waiter)), min(20, max(0, wait_seconds)))
            except TimeoutError:
                pass
            with self._lock:
                self._prune()
                if self._connectors.get(connector_hash) is not connection:
                    raise SubscriptionError("credential_invalid", 401)
                connection.last_seen = self._clock()
                if connection.blocked:
                    raise SubscriptionError("subscription_result_unknown")
                job = connection.job
                if job and not job.terminal:
                    job.delivered = True
                    wire = json.loads(json.dumps(job.wire))
                    wire["timeout_seconds"] = max(0, round(job.deadline - self._clock(), 2))
                    return {"job": wire}
                return {"job": None}
        finally:
            with self._lock:
                if connection.poll_waiter is waiter:
                    connection.poll_waiter = None
                if not waiter.done():
                    waiter.set_result(None)

    def _new_job(self, relay, body, timeout):
        model = str(body.get("model") or "")
        effort = self.preflight(relay, model, body.get("reasoning_effort"))
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 128 or any(
            not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}
            or not isinstance(message.get("content"), str) for message in messages
        ):
            raise SubscriptionError("subscription_protocol_error", 422)
        response_format = body.get("response_format")
        if response_format is not None and (
            not isinstance(response_format, dict) or response_format.get("type") not in {"json_object", "json_schema", "text"}
        ):
            raise SubscriptionError("subscription_protocol_error", 422)
        wire = {"job_id": secrets.token_urlsafe(24), "model": model, "effort": effort,
                "messages": [{"role": item["role"], "content": item["content"]} for item in messages],
                "response_format": response_format, "timeout_seconds": timeout}
        if len(json.dumps({"job": wire}, ensure_ascii=False).encode()) > MAX_JOB_BYTES:
            raise SubscriptionError("subscription_request_too_large", 413)
        with self._lock:
            # Two threads can pass the earlier validation simultaneously.
            self.preflight(relay, model, effort)
            connection = self._relays[_digest(relay)]
            job = _Job(wire["job_id"], wire, self._clock() + timeout, model)
            connection.job = job
            self._wake(connection)
            return connection, job

    def _unknown(self, connection, job):
        with self._lock:
            if not job.terminal:
                connection.blocked = True
                job.terminal = True
                job.wire = {}
                if not job.future.done():
                    job.future.set_exception(SubscriptionError("subscription_result_unknown", submitted=True))
                self._wake(connection)

    def _release_completed(self, connection, job):
        with self._lock:
            if job.terminal and not connection.blocked and connection.job is job:
                # Receipts retain only IDs for duplicate acknowledgements. Prompt
                # and result text need not stay resident until the next request.
                connection.job = None

    def complete(self, relay, body, timeout=180):
        timeout = _timeout_seconds(timeout)
        connection, job = self._new_job(relay, body, timeout)
        try:
            return job.future.result(timeout=max(0.01, min(180, timeout)))
        except FutureTimeout:
            if job.future.done():
                return job.future.result()
            self._unknown(connection, job)
            raise SubscriptionError("subscription_result_unknown", submitted=True) from None
        finally:
            self._release_completed(connection, job)

    async def complete_async(self, relay, body, timeout=180):
        timeout = _timeout_seconds(timeout)
        connection, job = self._new_job(relay, body, timeout)
        wrapped = asyncio.wrap_future(job.future)
        wrapped.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), max(0.01, min(180, timeout)))
        except (TimeoutError, asyncio.CancelledError):
            self._unknown(connection, job)
            # Consume the future exception after cancellation without losing the
            # terminal state that fences any subsequent provider operation.
            job.future.exception()
            raise SubscriptionError("subscription_result_unknown", submitted=True) from None
        finally:
            self._release_completed(connection, job)

    def result(self, token, job_id, content, error, usage):
        connector_hash = self.authenticate(token)
        with self._lock:
            connection = self._connectors.get(connector_hash)
            if connection is None:
                raise SubscriptionError("credential_invalid", 401)
            connection.last_seen = self._clock()
            if job_id in connection.receipts:
                return {"accepted": True}
            job = connection.job
            if not job or job.job_id != job_id:
                raise SubscriptionError("subscription_job_invalid", 409)
            if not job.terminal:
                job.terminal = True
                job.wire = {}
                if error:
                    if error not in SAFE_ERRORS:
                        error = "subscription_generation_failed"
                    if error in {"subscription_result_unknown", "subscription_connector_stopped"}:
                        connection.blocked = True
                    job.future.set_exception(SubscriptionError(error, submitted=True))
                else:
                    safe_usage = {key: value for key, value in usage.items()
                                  if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                                  and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000000}
                    job.future.set_result({"id": job_id, "model": job.model,
                                           "choices": [{"message": {"role": "assistant", "content": content}}],
                                           "usage": safe_usage})
            connection.receipts[job_id] = self._clock() + 600
            while len(connection.receipts) > 32:
                connection.receipts.pop(next(iter(connection.receipts)))
            return {"accepted": True}

    def close(self):
        with self._lock:
            for connection in list(self._subjects.values()):
                self._remove(connection)


def _timestamp(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def _timeout_seconds(value):
    if isinstance(value, httpx.Timeout):
        value = value.read
    try:
        return max(0.01, min(180, float(value or 180)))
    except (TypeError, ValueError):
        return 180


def relay_authorization(headers):
    authorization = str((headers or {}).get("Authorization") or "")
    if not authorization.startswith("Bearer "):
        raise SubscriptionError("credential_invalid", 401)
    return authorization[7:]


class SubscriptionSyncClient:
    """Route the fixed internal adapter without ever opening an HTTP socket."""
    def __init__(self, broker, upstream):
        self.broker, self.upstream = broker, upstream

    def post(self, url, **kwargs):
        if str(url).startswith("https://codex-subscription.invalid"):
            if str(url) != BASE_URL + "/chat/completions":
                raise SubscriptionError("subscription_protocol_error", 422)
            payload = self.broker.complete(relay_authorization(kwargs.get("headers")), kwargs.get("json") or {},
                                           kwargs.get("timeout", 180))
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))
        return self.upstream.post(url, **kwargs)

    def close(self):
        self.upstream.close()


class PairRequest(StrictModel):
    accepted_transit_notice: bool
    accepted_cost_notice: bool
    accepted_local_history_notice: bool


class SubscriptionModel(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
    display_name: str = Field(min_length=1, max_length=200)
    reasoning_efforts: list[ReasoningEffort] = Field(min_length=1, max_length=8)
    default_reasoning_effort: ReasoningEffort

    @model_validator(mode="after")
    def validate_efforts(self):
        if len(set(self.reasoning_efforts)) != len(self.reasoning_efforts):
            raise ValueError("duplicate reasoning effort")
        if self.default_reasoning_effort not in self.reasoning_efforts:
            raise ValueError("unsupported default reasoning effort")
        return self


def normalize_catalogue(models, protocol_version):
    if protocol_version == 1:
        if models is not None:
            raise SubscriptionError("subscription_protocol_error", 422)
        return [{"id": MODEL, "display_name": "Luna", "reasoning_efforts": [EFFORT],
                 "default_reasoning_effort": EFFORT}]
    if protocol_version != 2 or not isinstance(models, list) or not 1 <= len(models) <= 128:
        raise SubscriptionError("subscription_protocol_error", 422)
    try:
        catalogue = [SubscriptionModel.model_validate(item).model_dump() for item in models]
    except ValueError:
        raise SubscriptionError("subscription_protocol_error", 422) from None
    if len({item["id"] for item in catalogue}) != len(catalogue):
        raise SubscriptionError("subscription_protocol_error", 422)
    return catalogue


class ConnectorConnect(StrictModel):
    pairing_code: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")
    protocol_version: Literal[1, 2]
    model: Literal["gpt-5.6-luna"] | None = None
    effort: Literal["max"] | None = None
    models: list[SubscriptionModel] | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_protocol(self):
        if self.protocol_version == 1:
            if self.model != MODEL or self.effort != EFFORT or self.models is not None:
                raise ValueError("invalid v1 connector")
        elif self.models is None or self.model is not None or self.effort is not None:
            raise ValueError("invalid v2 connector")
        return self


class ConnectorPoll(StrictModel):
    connector_token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class ConnectorResult(ConnectorPoll):
    job_id: str = Field(min_length=32, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    content: str | None = Field(default=None, max_length=48000)
    error: str | None = Field(default=None, max_length=64)
    usage: dict[str, Any] = Field(default_factory=dict)


def install_subscription_routes(app: FastAPI, settings, broker: SubscriptionBroker, async_store):
    @app.post("/public/v1/subscription/pair")
    async def pair(request: Request, payload: PairRequest):
        broker.require_enabled()
        if not all((payload.accepted_transit_notice, payload.accepted_cost_notice, payload.accepted_local_history_notice)):
            raise HTTPException(422, {"code": "byok_notices_required"})
        await async_store.call("consume_limits", request.state.subject_hash,
                               [("subscription_pair_hour", "hour", 10), ("subscription_pair_day", "day", 30)])
        relay, code, _expires = broker.pair(request.state.subject_hash, settings.byok_lifetime_hours * 3600)
        credential, expires = issue_byok_credential(settings, anonymous_id=request.state.anonymous_id,
                                                    provider=PROVIDER, api_key=relay,
                                                    lifetime=timedelta(hours=settings.byok_lifetime_hours))
        return {"provider": PROVIDER, "credential": credential, "expires_at": expires.isoformat(), "stored": False,
                "pairing_code": code, "pairing_expires_at": _timestamp(time.time() + PAIR_SECONDS),
                "model": MODEL, "effort": EFFORT, "protocol_version": PROTOCOL_VERSION}

    @app.get("/public/v1/subscription/status")
    async def status(request: Request):
        broker.require_enabled()
        return broker.status(request.state.subject_hash)

    @app.post("/public/v1/subscription/disconnect")
    async def disconnect(request: Request):
        broker.require_enabled()
        broker.disconnect(request.state.subject_hash)
        return {"disconnected": True}

    @app.post("/public/v1/subscription/connector/connect")
    async def connect(payload: ConnectorConnect):
        token, expires = broker.connect(payload.pairing_code, protocol_version=payload.protocol_version,
                                        models=[item.model_dump() for item in payload.models] if payload.models else None)
        return {"connector_token": token, "expires_at": _timestamp(expires),
                "model": MODEL, "effort": EFFORT, "protocol_version": payload.protocol_version}

    @app.post("/public/v1/subscription/connector/poll")
    async def poll(payload: ConnectorPoll):
        return await broker.poll(payload.connector_token)

    @app.post("/public/v1/subscription/connector/result")
    async def result(payload: ConnectorResult):
        if bool(payload.error) == bool(payload.content) or (payload.error and payload.error not in SAFE_ERRORS):
            raise HTTPException(422, {"code": "subscription_protocol_error"})
        return broker.result(payload.connector_token, payload.job_id, payload.content, payload.error, payload.usage)
