"""Additive rendezvous authority and durable, content-free paid-attempt ledger.

These rows deliberately have no foreign key to the ten-minute request cache.
Old binaries may clean that cache without restoring a daily allowance.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text


PROACTIVE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public_rendezvous_authority (
    subject_hash VARCHAR(64) NOT NULL,
    rendezvous_id VARCHAR(160) NOT NULL,
    character_id VARCHAR(32) NOT NULL,
    schedule_date VARCHAR(16) NOT NULL,
    world_revision INTEGER NOT NULL CHECK (world_revision >= 0),
    closed INTEGER NOT NULL DEFAULT 0 CHECK (closed IN (0,1)),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (subject_hash, rendezvous_id)
);
CREATE INDEX IF NOT EXISTS public_rendezvous_authority_expiry_idx
    ON public_rendezvous_authority (expires_at);
CREATE TABLE IF NOT EXISTS public_proactive_day (
    subject_hash VARCHAR(64) NOT NULL,
    schedule_date VARCHAR(16) NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved BETWEEN 0 AND 3),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (subject_hash, schedule_date)
);
CREATE TABLE IF NOT EXISTS public_proactive_attempt (
    request_id VARCHAR(128) PRIMARY KEY,
    subject_hash VARCHAR(64) NOT NULL,
    schedule_date VARCHAR(16) NOT NULL,
    rendezvous_id VARCHAR(160) NOT NULL,
    character_id VARCHAR(32) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (status IN ('processing','completed','failed','unknown')),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE (subject_hash, rendezvous_id)
);
CREATE INDEX IF NOT EXISTS public_proactive_attempt_day_idx
    ON public_proactive_attempt (subject_hash, schedule_date);
CREATE INDEX IF NOT EXISTS public_proactive_attempt_expiry_idx
    ON public_proactive_attempt (expires_at);
"""


def _now():
    return datetime.now(UTC)


def _expired(value, now):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed) <= now


class ProactiveStoreMixin:
    def _record_rendezvous(self, connection, change):
        """Called in the same transaction as the committed scene/chat result.

        A closed ID is never reopened, even by a higher revision from a fork of
        an old signed browser snapshot. Resolve/read-only replays never register.
        """
        if not change:
            return
        params = dict(change)
        # Use the same lock order as paid admission: subject/day first, then
        # authority. Two worker commits cannot leave two active promises for
        # the same character, or race a cancellation against quota admission.
        connection.execute(text("""INSERT INTO public_proactive_day
            (subject_hash,schedule_date,reserved,expires_at) VALUES (:subject,:day,0,:expires)
            ON CONFLICT (subject_hash,schedule_date) DO NOTHING"""), params)
        lock = " FOR UPDATE" if connection.dialect.name == "postgresql" else ""
        connection.execute(text("""SELECT reserved FROM public_proactive_day
            WHERE subject_hash=:subject AND schedule_date=:day""" + lock), params).scalar_one()
        for rid in change["closed_ids"]:
            connection.execute(text("""INSERT INTO public_rendezvous_authority
                (subject_hash,rendezvous_id,character_id,schedule_date,world_revision,closed,expires_at)
                VALUES (:subject,:rid,:character,:day,:revision,1,:expires)
                ON CONFLICT (subject_hash,rendezvous_id) DO UPDATE SET closed=1"""),
                {**params, "rid": rid})
        if change.get("created_id"):
            connection.execute(text("""UPDATE public_rendezvous_authority SET closed=1
                WHERE subject_hash=:subject AND character_id=:character AND schedule_date=:day
                  AND rendezvous_id<>:rid"""), {**params, "rid": change["created_id"]})
            connection.execute(text("""INSERT INTO public_rendezvous_authority
                (subject_hash,rendezvous_id,character_id,schedule_date,world_revision,closed,expires_at)
                VALUES (:subject,:rid,:character,:day,:revision,0,:expires)
                ON CONFLICT (subject_hash,rendezvous_id) DO NOTHING"""),
                {**params, "rid": change["created_id"]})

    def lookup_proactive(self, request_id, subject, request_hash):
        """Read-only recovery; it never claims, frees quota or fences a lease."""
        now = _now()
        with self.begin() as connection:
            row = connection.execute(text("""SELECT attempt.*,
                cache.response_json,cache.expires_at AS result_expires,
                lease.lease_expires_at
                FROM public_proactive_attempt AS attempt
                LEFT JOIN public_request_cache AS cache ON cache.request_id=attempt.request_id
                LEFT JOIN public_request_leases AS lease ON lease.request_id=attempt.request_id
                WHERE attempt.request_id=:request_id"""), {"request_id": request_id}).mappings().first()
        if not row:
            return "missing", None
        if row["subject_hash"] != subject or row["request_hash"] != request_hash:
            return "conflict", None
        if row["response_json"] and row["result_expires"] and not _expired(row["result_expires"], now):
            return "completed", json.loads(row["response_json"])
        if row["status"] == "processing" and row["lease_expires_at"] and not _expired(row["lease_expires_at"], now):
            return "processing", None
        return "unknown", None

    def claim_proactive(self, request_id, subject, request_hash, *, day, rendezvous_id,
                        character_id, expires, owner_token):
        """Serialize one subject/day across workers, then reserve exactly once."""
        now = _now()
        with self.begin() as connection:
            params = {"request_id": request_id, "subject": subject, "hash": request_hash,
                      "day": day, "rid": rendezvous_id, "character": character_id,
                      "expires": expires, "now": now, "owner": owner_token,
                      "lease": now + timedelta(seconds=45), "cache_expiry": now + timedelta(days=1)}
            connection.execute(text("""INSERT INTO public_proactive_day
                (subject_hash,schedule_date,reserved,expires_at) VALUES (:subject,:day,0,:expires)
                ON CONFLICT (subject_hash,schedule_date) DO NOTHING"""), params)
            lock = " FOR UPDATE" if connection.dialect.name == "postgresql" else ""
            count = connection.execute(text("""SELECT reserved FROM public_proactive_day
                WHERE subject_hash=:subject AND schedule_date=:day""" + lock), params).scalar_one()
            existing = connection.execute(text("""SELECT subject_hash,request_hash,request_id
                FROM public_proactive_attempt WHERE request_id=:request_id
                OR (subject_hash=:subject AND rendezvous_id=:rid)"""), params).mappings().first()
            if existing:
                if existing["request_id"] != request_id:
                    return "rendezvous_already_used", None
                return ("existing" if existing["subject_hash"] == subject and existing["request_hash"] == request_hash
                        else "conflict"), None
            authority = connection.execute(text("""SELECT closed,character_id,schedule_date,expires_at
                FROM public_rendezvous_authority WHERE subject_hash=:subject AND rendezvous_id=:rid""" + lock), params).mappings().first()
            if (not authority or authority["closed"] or authority["character_id"] != character_id
                    or authority["schedule_date"] != day or _expired(authority["expires_at"], now)):
                return "rendezvous_not_active", None
            if count >= 3:
                return "proactive_daily_limit", None
            connection.execute(text("""UPDATE public_proactive_day SET reserved=reserved+1
                WHERE subject_hash=:subject AND schedule_date=:day"""), params)
            connection.execute(text("""INSERT INTO public_proactive_attempt
                (request_id,subject_hash,schedule_date,rendezvous_id,character_id,request_hash,status,created_at,expires_at)
                VALUES (:request_id,:subject,:day,:rid,:character,:hash,'processing',:now,:expires)"""), params)
            connection.execute(text("""INSERT INTO public_request_cache
                (request_id,subject_hash,request_hash,status,response_json,created_at,expires_at)
                VALUES (:request_id,:subject,:hash,'processing',NULL,:now,:cache_expiry)"""), params)
            connection.execute(text("""INSERT INTO public_request_leases
                (request_id,owner_token,lease_expires_at) VALUES (:request_id,:owner,:lease)"""), params)
            return "claimed", None

    def complete_proactive(self, request_id, result, *, owner_token):
        now = _now()
        with self.begin() as connection:
            # Reuse the ordinary ownership fence. A late worker cannot replace
            # an already terminal response after its lease was reconciled.
            self._lock_request(connection, request_id)
            params = {"id": request_id, "owner": owner_token, "now": now,
                      "response": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                      "expires": now + timedelta(minutes=10), "status": result["status"]}
            updated = connection.execute(text("""UPDATE public_request_cache
                SET status='completed',response_json=:response,expires_at=:expires
                WHERE request_id=:id AND status='processing' AND EXISTS
                (SELECT 1 FROM public_request_leases WHERE request_id=:id
                 AND owner_token=:owner AND lease_expires_at>:now)"""), params)
            if updated.rowcount != 1:
                raise RuntimeError("proactive request ownership lost")
            connection.execute(text("""UPDATE public_proactive_attempt SET status=:status
                WHERE request_id=:id AND status='processing'"""), params)
            connection.execute(text("DELETE FROM public_request_leases WHERE request_id=:id"), params)
