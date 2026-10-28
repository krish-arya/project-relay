"""Inbox ingestion: validate each line, reject unknown tenants, store, enqueue.

Idempotent: a ticket is keyed by (tenant, id); the job by an idempotency key
derived from it. Re-ingesting the same inbox adds nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from pydantic import ValidationError

from relay import queue
from relay.config import Settings
from relay.contracts import Ticket
from relay.db import audit, global_tx, known_tenants, tenant_tx


@dataclass
class IngestStats:
    accepted: int = 0
    duplicates: int = 0
    rejected: list[dict] = field(default_factory=list)


def ingest_file(conn: psycopg.Connection, settings: Settings, path: Path, run_id: str) -> IngestStats:
    tenants = known_tenants(conn)
    stats = IngestStats()
    source = str(path).replace("\\", "/")
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not raw.strip():
            continue
        claimed_tenant, ticket_id = None, None
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                claimed_tenant, ticket_id = obj.get("tenant"), obj.get("id")
            ticket = Ticket.model_validate(obj)
        except (ValueError, ValidationError) as e:
            _reject(conn, stats, source, line_no, raw, claimed_tenant, ticket_id, f"invalid ticket: {_short(e)}")
            continue
        if ticket.tenant not in tenants:
            # Fail closed: never fall back to a default tenant.
            _reject(conn, stats, source, line_no, raw, ticket.tenant, ticket.id, f"unknown tenant {ticket.tenant!r}")
            continue
        if store_ticket(conn, settings, ticket, run_id):
            stats.accepted += 1
        else:
            stats.duplicates += 1
    return stats


def store_ticket(conn: psycopg.Connection, settings: Settings, ticket: Ticket, run_id: str) -> bool:
    """Insert the ticket and its processing job. Returns False if it was already known."""
    with tenant_tx(conn, ticket.tenant) as cur:
        cur.execute(
            """INSERT INTO tickets (tenant_id, ticket_id, customer_ref, subject, body, received_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, ticket_id) DO NOTHING""",
            (ticket.tenant, ticket.id, ticket.customer_ref, ticket.subject, ticket.body, ticket.received_at),
        )
        if cur.rowcount == 0:
            audit(cur, ticket.tenant, ticket.id, "ingest", "duplicate_ignored", details={"run_id": run_id})
            return False
        audit(cur, ticket.tenant, ticket.id, "ingest", "received", to_state="received", details={"run_id": run_id})
        queue.enqueue(cur, ticket.tenant, ticket.id, "process", f"process:{ticket.tenant}:{ticket.id}",
                      run_id, settings.max_attempts)
        return True


def _reject(conn, stats, source, line_no, raw, tenant, ticket_id, reason) -> None:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with global_tx(conn) as cur:
        cur.execute(
            """INSERT INTO rejected_inputs (source, line_no, claimed_tenant, ticket_id, reason, raw_sha256)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (source, raw_sha256) DO NOTHING""",
            (source, line_no, _str(tenant), _str(ticket_id), reason, digest),
        )
    stats.rejected.append({"line": line_no, "tenant": _str(tenant), "ticket_id": _str(ticket_id), "reason": reason})


def _str(v) -> str | None:
    return None if v is None else str(v)[:200]


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])
    return str(e)[:200]
