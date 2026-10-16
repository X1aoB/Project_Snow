"""Durable, conservative CNY budget for explicitly authorized manual probes.

This module has no provider or credential access. Unknown outcomes keep their
reservation and block further calls until an operator reconciles the evidence.
"""
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4


class BudgetBlocked(RuntimeError):
    pass


class PaidProbeBudget:
    def __init__(self, path, *, cap_cny="20", input_per_million="2", output_per_million="8", source=""):
        self.path = Path(path)
        self.cap = Decimal(cap_cny)
        self.input_rate = Decimal(input_per_million)
        self.output_rate = Decimal(output_per_million)
        self.source = source
        if not (0 < self.cap <= 20 and self.input_rate > 0 and self.output_rate > 0):
            raise ValueError("invalid authorized budget or tariff")

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise BudgetBlocked("another budget operation is active") from exc
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink()

    def read(self):
        if not self.path.exists():
            return {"schema": "snow-paid-probe-budget-1", "currency": "CNY", "cap_cny": str(self.cap),
                    "input_ceiling_per_million": str(self.input_rate), "output_ceiling_per_million": str(self.output_rate),
                    "tariff_source": self.source, "calls": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema") != "snow-paid-probe-budget-1" or Decimal(data["cap_cny"]) != self.cap:
            raise BudgetBlocked("budget identity mismatch")
        if Decimal(data["input_ceiling_per_million"]) != self.input_rate or Decimal(data["output_ceiling_per_million"]) != self.output_rate:
            raise BudgetBlocked("tariff changed: explicit reconciliation required")
        return data

    def save(self, data):
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def cost(self, prompt_tokens, completion_tokens):
        return (Decimal(prompt_tokens) * self.input_rate + Decimal(completion_tokens) * self.output_rate) / 1_000_000

    def reserve(self, *, case, request_id, prompt_bound, output_bound):
        if any(type(n) is not int or n < 0 for n in (prompt_bound, output_bound)):
            raise ValueError("nonnegative integer token bounds required")
        amount = self.cost(prompt_bound, output_bound)
        with self.locked():
            data = self.read()
            if any(row["status"] != "confirmed" for row in data["calls"]):
                raise BudgetBlocked("unresolved paid outcome must be reconciled first")
            spent = sum((Decimal(row["cost_ceiling_cny"]) for row in data["calls"]), Decimal(0))
            if spent + amount > self.cap:
                raise BudgetBlocked("authorized cumulative CNY cap would be exceeded")
            call_id = str(uuid4())
            data["calls"].append({"call_id": call_id, "case": case, "request_id": request_id,
                "prompt_bound": prompt_bound, "output_bound": output_bound, "reserved_cny": str(amount), "status": "reserved"})
            self.save(data)
            return call_id

    def confirm(self, call_id, usage):
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if any(type(n) is not int or n < 0 for n in (prompt, completion)):
            raise BudgetBlocked("missing or invalid usage: reservation retained")
        with self.locked():
            data = self.read()
            row = next(item for item in data["calls"] if item["call_id"] == call_id)
            if row["status"] != "reserved" or prompt > row["prompt_bound"] or completion > row["output_bound"]:
                raise BudgetBlocked("usage reconciliation requires review")
            row.update(status="confirmed", prompt_tokens=prompt, completion_tokens=completion,
                       cost_ceiling_cny=str(self.cost(prompt, completion)))
            self.save(data)
