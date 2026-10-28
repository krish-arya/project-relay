"""Human approval gate.

Ticket states touched here:
    pending_approval --approve--> approved   (outbox row written, exactly once)
    pending_approval --reject---> redrafting (first rejection: drafter runs once more)
    redrafting       --worker---> pending_approval | escalated
    pending_approval --reject---> escalated  (second rejection: case for a human)

Every transition takes a row lock on the ticket and writes an audit row in the
same transaction. Approving twice, or racing two approvers, cannot produce a
second outbox row: outbox has UNIQUE (tenant_id, ticket_id).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg

from relay import queue
from relay.config import Settings
from relay.db import audit, known_tenants, tenant_tx
from relay.policy import SEVERITY, load_policy


class ApprovalError(Exception):
    pass


@dataclass
class Located:
    tenant_id: str
    ticket_id: str


def locate(conn: psycopg.Connection, ticket_id: str, tenant: str | None) -> Located:
    """Find which tenant a ticket belongs to (IDs are unique per tenant, not globally)."""
    tenants = [tenant] if tenant else list(known_tenants(conn))
    if tenant and tenant not in known_tenants(conn):
        raise ApprovalError(f"unknown tenant {tenant!r}")
    found = []
    for t in tenants:
        with tenant_tx(conn, t) as cur:
            cur.execute("SELECT 1 FROM tickets WHERE ticket_id = %s", (ticket_id,))
            if cur.fetchone():
                found.append(t)
    if not found:
        raise ApprovalError(f"ticket {ticket_id} not found")
    if len(found) > 1:
        raise ApprovalError(f"ticket {ticket_id} exists in several tenants ({', '.join(found)}); pass --tenant")
    return Located(found[0], ticket_id)


def pending(conn: psycopg.Connection, tenant: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for t in [tenant] if tenant else list(known_tenants(conn)):
        with tenant_tx(conn, t) as cur:
            cur.execute(
                """SELECT a.tenant_id, a.ticket_id, a.version, a.action, a.draft, a.kb_refs, a.created_at,
                          d.category, d.priority, t.subject
                     FROM approvals a
                     JOIN tickets t USING (tenant_id, ticket_id)
                     JOIN decisions d USING (tenant_id, ticket_id)
                    WHERE a.state = 'pending'
                    ORDER BY a.created_at, a.ticket_id"""
            )
            rows.extend(cur.fetchall())
    return rows


def show(conn: psycopg.Connection, ticket_id: str, tenant: str | None = None) -> dict:
    loc = locate(conn, ticket_id, tenant)
    with tenant_tx(conn, loc.tenant_id) as cur:
        cur.execute("SELECT * FROM tickets WHERE ticket_id = %s", (ticket_id,))
        ticket = cur.fetchone()
        cur.execute("SELECT * FROM decisions WHERE ticket_id = %s", (ticket_id,))
        decision = cur.fetchone()
        cur.execute("SELECT * FROM approvals WHERE ticket_id = %s ORDER BY version", (ticket_id,))
        approvals = cur.fetchall()
        cur.execute("SELECT * FROM cases WHERE ticket_id = %s ORDER BY id", (ticket_id,))
        cases = cur.fetchall()
        cur.execute("SELECT * FROM outbox WHERE ticket_id = %s", (ticket_id,))
        outbox = cur.fetchone()
        cur.execute("SELECT actor, action, from_state, to_state, reason, at FROM audit_log WHERE ticket_id = %s ORDER BY id",
                    (ticket_id,))
        audit_rows = cur.fetchall()
    return {"ticket": ticket, "decision": decision, "approvals": approvals, "cases": cases,
            "outbox": outbox, "audit": audit_rows}


def approve(conn: psycopg.Connection, ticket_id: str, by: str, tenant: str | None = None) -> dict:
    loc = locate(conn, ticket_id, tenant)
    with tenant_tx(conn, loc.tenant_id) as cur:
        cur.execute("SELECT state FROM tickets WHERE ticket_id = %s FOR UPDATE", (ticket_id,))
        state = cur.fetchone()["state"]
        cur.execute("SELECT * FROM approvals WHERE ticket_id = %s AND state = 'pending' FOR UPDATE", (ticket_id,))
        pend = cur.fetchone()
        if pend is None:
            cur.execute("SELECT approved_by, approved_at FROM outbox WHERE ticket_id = %s", (ticket_id,))
            sent = cur.fetchone()
            if sent:
                raise ApprovalError(f"{ticket_id} was already approved by {sent['approved_by']} at {sent['approved_at']}")
            raise ApprovalError(f"{ticket_id} has nothing awaiting approval (state: {state})")
        approved_at = datetime.now(timezone.utc)
        key = idempotency_key(loc.tenant_id, ticket_id, pend["version"], pend["draft"])
        cur.execute(
            """INSERT INTO outbox (tenant_id, ticket_id, text, approved_by, approved_at, idempotency_key)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
            (loc.tenant_id, ticket_id, pend["draft"], by, approved_at, key),
        )
        if cur.rowcount != 1:
            raise ApprovalError(f"{ticket_id} already has an outbox message; refusing to send twice")
        cur.execute(
            "UPDATE approvals SET state = 'approved', decided_by = %s, decided_at = %s WHERE ticket_id = %s AND version = %s",
            (by, approved_at, ticket_id, pend["version"]),
        )
        cur.execute("UPDATE tickets SET state = 'approved' WHERE ticket_id = %s", (ticket_id,))
        audit(cur, loc.tenant_id, ticket_id, by, "approved", from_state=state, to_state="approved",
              details={"version": pend["version"], "idempotency_key": key, "action": pend["action"]})
    return {"tenant": loc.tenant_id, "ticket_id": ticket_id, "version": pend["version"], "idempotency_key": key}


def reject(conn: psycopg.Connection, settings: Settings, ticket_id: str, by: str, reason: str,
           tenant: str | None = None) -> dict:
    if not reason or not reason.strip():
        raise ApprovalError("a rejection needs a reason")
    loc = locate(conn, ticket_id, tenant)
    with tenant_tx(conn, loc.tenant_id) as cur:
        cur.execute("SELECT state, rejection_count FROM tickets WHERE ticket_id = %s FOR UPDATE", (ticket_id,))
        t = cur.fetchone()
        cur.execute("SELECT * FROM approvals WHERE ticket_id = %s AND state = 'pending' FOR UPDATE", (ticket_id,))
        pend = cur.fetchone()
        if pend is None:
            raise ApprovalError(f"{ticket_id} has nothing awaiting approval (state: {t['state']})")
        cur.execute(
            """UPDATE approvals SET state = 'rejected', decided_by = %s, decided_at = now(), reason = %s
                WHERE ticket_id = %s AND version = %s""",
            (by, reason, ticket_id, pend["version"]),
        )
        count = t["rejection_count"] + 1
        if count == 1:
            cur.execute("UPDATE tickets SET state = 'redrafting', rejection_count = %s WHERE ticket_id = %s",
                        (count, ticket_id))
            cur.execute("SELECT run_id FROM decisions WHERE ticket_id = %s", (ticket_id,))
            run_id = cur.fetchone()["run_id"]
            queue.enqueue(cur, loc.tenant_id, ticket_id, "redraft", f"redraft:{loc.tenant_id}:{ticket_id}:v{pend['version']}",
                          run_id, settings.max_attempts, {"version": pend["version"], "reason": reason})
            audit(cur, loc.tenant_id, ticket_id, by, "rejected", from_state=t["state"], to_state="redrafting",
                  reason=reason, details={"version": pend["version"]})
            return {"tenant": loc.tenant_id, "ticket_id": ticket_id, "next": "redrafting"}
        cur.execute("UPDATE tickets SET state = 'escalated', rejection_count = %s WHERE ticket_id = %s", (count, ticket_id))
        cur.execute(
            """UPDATE decisions SET action = 'escalate', status = 'escalated', draft_reply = NULL, updated_at = now()
                WHERE ticket_id = %s RETURNING priority""",
            (ticket_id,),
        )
        priority = cur.fetchone()["priority"]
        owner = load_policy(loc.tenant_id).owners.get("default", "support-lead")
        cur.execute(
            """INSERT INTO cases (tenant_id, ticket_id, source, owner, severity, reason)
               VALUES (%s, %s, 'rejected_twice', %s, %s, %s) ON CONFLICT DO NOTHING""",
            (loc.tenant_id, ticket_id, owner, SEVERITY[priority], f"draft rejected twice; last reason: {reason}"),
        )
        audit(cur, loc.tenant_id, ticket_id, by, "rejected", from_state=t["state"], to_state="escalated",
              reason=reason, details={"version": pend["version"], "rejections": count})
        return {"tenant": loc.tenant_id, "ticket_id": ticket_id, "next": "escalated"}


def idempotency_key(tenant: str, ticket_id: str, version: int, text: str) -> str:
    digest = hashlib.sha256(f"{tenant}\x1f{ticket_id}\x1f{version}\x1f{text}".encode()).hexdigest()
    return f"out-{digest[:32]}"
