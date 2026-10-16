"""Opt-in paid integration probe using synthetic sessions and a durable CNY cap.

Run manually with --execute, an existing local env file and an evidence directory.
No provider response bodies, credentials or private provider IDs are written.
Ordinary CI imports/tests the budget only and never executes this entry point.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from scripts.paid_probe_budget import BudgetBlocked, PaidProbeBudget


TARIFF = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
FLASH_MODELS = {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}


class GuardedProvider:
    def __init__(self, client, ledger, model):
        self.client, self.ledger, self.model = client, ledger, model
        self.case = "unassigned"
        self.request_id = ""
        self.limit = 1
        self.calls = 0

    def begin(self, case, request_id, limit=2):
        self.case, self.request_id, self.limit, self.calls = case, request_id, limit, 0

    def post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        if str(url) != "https://api.deepseek.com/v1/chat/completions" or body.get("model") != self.model or body.get("stream"):
            raise BudgetBlocked("unexpected provider route or model")
        if not self.request_id or self.calls >= self.limit:
            raise BudgetBlocked("per-action provider call cap")
        output_bound = body.get("max_tokens")
        if type(output_bound) is not int or not 0 < output_bound <= 8192:
            raise BudgetBlocked("bounded output required")
        # UTF-8 bytes conservatively bound text tokenization; extra room covers
        # message framing. This probe sends no images/tools or hidden context.
        prompt_bound = len(json.dumps(body.get("messages", []), ensure_ascii=False).encode()) + 4096
        call_id = self.ledger.reserve(case=self.case, request_id=self.request_id,
            prompt_bound=prompt_bound, output_bound=output_bound)
        self.calls += 1
        # An exception or missing usage deliberately leaves the reservation open.
        response = self.client.post(url, **kwargs)
        if response.status_code != 200:
            raise BudgetBlocked("provider outcome requires reconciliation")
        self.ledger.confirm(call_id, response.json().get("usage") or {})
        return response

    def stream(self, *args, **kwargs):
        raise BudgetBlocked("streaming upstream is disabled for this bounded probe")

    def close(self):
        self.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Validate isolated setup and a zero-model join only")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.execute and not args.preflight:
        raise SystemExit("Explicit --execute required; no provider calls made")
    logging.disable(logging.CRITICAL)
    from dotenv import dotenv_values
    import keyring
    from fastapi.testclient import TestClient
    from backend.snow_app.config import Settings
    from backend.snow_app.mvp_policy import MVP_CHARACTERS
    from backend.snow_app.public_main import create_app
    from backend.snow_app.public_store import PublicStore
    from backend.snow_app.public_security import verify_state
    from scripts.prepare_ci_runtime import load_registry, write_character_views
    from tests.test_public_api import _settings

    configuration = dotenv_values(args.env_file)
    model = str(configuration.get("MVP_CHAT_MODEL") or "")
    base = str(configuration.get("MVP_CHAT_BASE_URL") or "").rstrip("/")
    if model not in FLASH_MODELS or base not in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}:
        raise SystemExit("Configured model requires a newly verified tariff; no provider calls made")
    credential = str(configuration.get("MVP_CHAT_API_KEY") or "")
    if not credential:
        credential = str(keyring.get_password("ProjectSnow", str(configuration.get("MVP_CHAT_CREDENTIAL_REF") or "")) or "")
    if not credential:
        raise SystemExit("Existing credential is unavailable; no provider calls made")
    args.output.mkdir(parents=True, exist_ok=True)
    ledger = PaidProbeBudget(args.output / "budget.json", source=TARIFF)
    if ledger.read()["calls"]:
        raise SystemExit("Existing probe ledger requires reconciliation/review; automatic rerun refused")
    report = {"schema": "snow-immersive-paid-probe-1", "started_at": datetime.now(timezone.utc).isoformat(),
              "tariff_source": TARIFF, "currency": "CNY", "cap_cny": "20", "synthetic_sessions": True,
              "normal_upstream_tls": True, "cases": [], "passed": False}
    report_path = args.output / "report.json"

    def save_report():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def need(condition, code):
        if not condition:
            raise RuntimeError(code)

    with TemporaryDirectory(prefix="snow-paid-synthetic-") as directory, ExitStack() as cleanup:
        runtime = Path(directory)
        write_character_views(load_registry(), runtime / "mvp" / "character_views.jsonl")
        settings = replace(_settings(), enabled_providers=("deepseek",),
            database_url="sqlite+pysqlite:///" + (runtime / "public.sqlite3").as_posix(),
            dialogue_domains_v2=True, scene_actions_v2=True, session_memory_v2=True,
            chat_latency_v2=True, verified_block_stream=False,
            qdrant_url="http://127.0.0.1:9", embedding_url="http://127.0.0.1:9", neo4j_uri="bolt://127.0.0.1:9")
        internal = Settings(data_root=runtime, runtime_root=runtime, chat_enabled=False,
            embedding_model="synthetic", allowed_origins=["http://testserver"])
        store = PublicStore(settings.database_url)
        cleanup.callback(store.engine.dispose)
        store.create_schema()
        app = create_app(settings, internal, store)
        guarded = GuardedProvider(app.state.chat_service.mvp._model_http_client, ledger, model)
        app.state.chat_service.mvp._model_http_client = guarded
        with TestClient(app) as client:
            headers = {"Origin": "http://testserver", "X-Snow-Stream-Version": "2"}
            issued = client.post("/public/v1/byok/session", headers=headers, json={"provider": "deepseek",
                "api_key": credential, "turnstile_token": "development-bypass", "accepted_transit_notice": True,
                "accepted_cost_notice": True, "accepted_local_history_notice": True})
            need(issued.status_code == 200, "synthetic_session_creation_failed")
            opaque = issued.json()["credential"]
            character = MVP_CHARACTERS[0].character_id
            segments = {item.character_id: str(uuid4()) for item in MVP_CHARACTERS[:2]}
            current = {}

            def scene(action, who=character):
                body = {"protocol": "public-scene-action-v2", "request_id": str(uuid4()), "character_id": who,
                    "segment_id": segments[who], "action": action,
                    "state_package": current.get("state_package", ""),
                    "session_context_package": current.get("session_context_package", "") if current.get("character_id") == who else ""}
                response = client.post("/public/v2/scene/action", headers=headers, json=body)
                need(response.status_code == 200, "scene_action_failed")
                return response.json()

            def arrival(case, joined):
                body = {"protocol": "public-presence-arrival-v2", "arrival_id": joined["request_id"],
                    "arrival_ticket": joined["arrival_ticket"], "character_id": joined["character_id"],
                    "segment_id": segments[joined["character_id"]], "provider": "deepseek", "credential": opaque,
                    "model": model, "state_package": joined["state_package"],
                    "session_context_package": joined["session_context_package"]}
                guarded.begin(case, body["arrival_id"], 1)
                response = client.post("/public/v2/presence/arrival", headers=headers, json=body)
                need(response.status_code == 200, "arrival_endpoint_failed")
                result = response.json()
                need(result.get("decision") == "noticed" and result.get("reaction") and not result.get("terminal_error"), "arrival_generation_failed")
                need(guarded.calls == 1, "arrival_provider_count")
                replay = client.post("/public/v2/presence/arrival", headers={**headers, "X-Snow-Recovery-Only": "1"}, json=body)
                need(replay.status_code == 200 and replay.json().get("reaction") == result["reaction"] and guarded.calls == 1, "arrival_recovery_failed")
                report["cases"].append({"case": case, "request_id": body["arrival_id"], "provider_calls": guarded.calls,
                    "passed": True, "recovery_extra_calls": 0, "reply_sha256": hashlib.sha256(json.dumps(result["reaction"], sort_keys=True).encode()).hexdigest()})
                save_report()

            def chat(case, message, channel, location=""):
                request_id = str(uuid4())
                body = {"protocol": "public-dialogue-v2", "segment_id": segments[character],
                    "session_context_package": current["session_context_package"],
                    "chat": {"request_id": request_id, "character_id": character, "provider": "deepseek", "credential": opaque,
                        "model": model, "message": message, "communication_channel": channel,
                        "state_package": current["state_package"], "movement_location_id": location}}
                guarded.begin(case, request_id)
                response = client.post("/public/v2/chat/stream", headers=headers, json=body)
                need(response.status_code == 200, "chat_endpoint_failed")
                result = {}
                done = False
                for packet in response.text.split("\n\n"):
                    if packet.startswith(("event: meta\n", "event: state\n", "event: done\n")):
                        result.update(json.loads(packet.split("data: ", 1)[1]))
                        done |= packet.startswith("event: done\n")
                need(done and result.get("content_blocks") and not result.get("terminal_error"), "chat_generation_failed")
                need(1 <= guarded.calls <= 2, "chat_provider_count")
                status = (result.get("scene_action") or {}).get("status", "")
                before = verify_state(settings, current["state_package"])
                after = verify_state(settings, result["state_package"])
                if case == "future_invitation":
                    need(before["analyst_location"] == after["analyst_location"], "future_invitation_moved_now")
                if case == "immediate_invitation" and status in {"applied", "joint_movement"}:
                    from backend.snow_app.session_context import verify_session_context
                    from backend.snow_app.public_main import _anonymous_cookie_name
                    from backend.snow_app.public_security import subject_hash
                    context = verify_session_context(settings, result["session_context_package"],
                        subject_hash=subject_hash(client.cookies.get(_anonymous_cookie_name(settings))), character_id=character,
                        segment_id=segments[character], catalog_version=app.state.dialogue_v2_service.catalog_version)
                    need(context.active_invitation is None, "completed_invitation_remains")
                report["cases"].append({"case": case, "request_id": request_id, "provider_calls": guarded.calls,
                    "passed": True, "scene_status": status, "blocks": len(result["content_blocks"]),
                    "reply_sha256": hashlib.sha256(json.dumps(result["content_blocks"], sort_keys=True).encode()).hexdigest()})
                save_report()
                return {**result, "character_id": character}

            if args.preflight:
                scene("join_character")
                print(json.dumps({"preflight_passed": True, "provider_calls": 0}))
                return 0
            try:
                current = scene("join_character")
                arrival("first_entry", current)
                current = chat("immediate_invitation", "我们现在一起去公园散散步，好吗？", "in_person", "park")
                current = chat("future_invitation", "我们明天下午一起去公园散步，好吗？", "in_person", "park")
                current = scene("open_communicator")
                current = chat("communicator_greeting", "今天休息得怎么样？想和你随便聊一会儿。", "text")
                current = chat("communicator_layers", "和我分享两三件今天的小事吧，可以像聊天一样分成几条短消息。", "text")
                current = scene("join_character")
                arrival("reentry", current)
                current = scene("join_character", MVP_CHARACTERS[1].character_id)
                arrival("other_character", current)
                report["passed"] = True
            except Exception as exc:
                # Error types only: provider errors can contain private response text.
                report["failure_type"] = type(exc).__name__
                if type(exc) is RuntimeError:
                    report["failure_code"] = str(exc)
            finally:
                data = ledger.read()
                report["provider_calls"] = len(data["calls"])
                report["unresolved_calls"] = sum(row["status"] != "confirmed" for row in data["calls"])
                report["confirmed_cost_ceiling_cny"] = str(sum((Decimal(row.get("cost_ceiling_cny", "0")) for row in data["calls"]), Decimal(0)))
                report["completed_at"] = datetime.now(timezone.utc).isoformat()
                save_report()
    print(json.dumps({key: report[key] for key in ("passed", "provider_calls", "unresolved_calls", "confirmed_cost_ceiling_cny")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
