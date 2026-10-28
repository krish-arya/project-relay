"""Worker: claims jobs, runs the lead agent, commits each result atomically.

Exactly-once effects come from the database, not from the worker being careful:
- decisions has PRIMARY KEY (tenant_id, ticket_id): a second decision is a no-op
- completing a job requires still holding its lease, in the same transaction
  as the decision; a worker that lost its lease rolls everything back
Killing a worker mid-ticket therefore loses only the model calls in flight.
"""

from __future__ import annotations

import os
import socket
import sys
import time

import psycopg
from psycopg.types.json import Jsonb

from relay import lead, queue
from relay.config import Settings
from relay.contracts import Decision, Ticket
from relay.db import audit, tenant_tx
from relay.gateway import Gateway, ModelUnavailable


class LeaseLost(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Worker:
    def __init__(self, settings: Settings, conn: psycopg.Connection, gw: Gateway, name: str | None = None):
        self.settings = settings
        self.conn = conn
        self.gw = gw
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"

    def run_until_empty(self, *, max_jobs: int | None = None, poll_s: float = 0.5,
                        max_idle_s: float | None = None) -> dict[str, int]:
        """Process jobs until none are queued or leased. max_idle_s stops waiting on
        jobs this worker cannot claim (e.g. another worker's live lease)."""
        counts = {"done": 0, "retry": 0, "dead": 0}
        idle_since: float | None = None
        while max_jobs is None or sum(counts.values()) < max_jobs:
            job = queue.claim(self.conn, self.name, self.settings.lease_s)
            if job is None:
                pending = queue.stats(self.conn)
                if not (pending.get("queued", 0) or pending.get("leased", 0)):
                    break
                idle_since = idle_since or time.monotonic()
                if max_idle_s is not None and time.monotonic() - idle_since > max_idle_s:
                    log(f"[worker] giving up after {max_idle_s}s idle; unclaimable jobs: {pending}")
                    break
                time.sleep(poll_s)  # retries waiting out their backoff, or another worker's lease
                continue
            idle_since = None
            counts[self.handle(job)] += 1
        return counts

    def handle(self, job: queue.Job) -> str:
        try:
            if job.kind == "process":
                self._process(job)
            elif job.kind == "redraft":
                self._redraft(job)
            return "done"
        except ModelUnavailable as e:
            return self._failed(job, str(e), e.error_class)
        except LeaseLost:
            log(f"[worker] lost lease on job {job.id}; another worker owns it")
            return "retry"
        except Exception as e:  # noqa: BLE001 - classified and retried, never crashes the run
            return self._failed(job, repr(e), "internal_error")

    def _failed(self, job: queue.Job, error: str, error_class: str) -> str:
        result = queue.fail(self.conn, job, error, error_class, self.settings)
        log(f"[worker] {job.tenant_id}/{job.ticket_id} {job.kind} attempt {job.attempts}: {error_class} -> {result}")
        if result == "dead" and job.kind == "process":
            self._write_failed_decision(job, error_class)
        return result

    def _load_ticket(self, job: queue.Job) -> tuple[Ticket, dict | None]:
        with tenant_tx(self.conn, job.tenant_id) as cur:
            cur.execute("SELECT * FROM tickets WHERE ticket_id = %s", (job.ticket_id,))
            t = cur.fetchone()
            cur.execute("SELECT * FROM decisions WHERE ticket_id = %s", (job.ticket_id,))
            existing = cur.fetchone()
        ticket = Ticket(id=t["ticket_id"], tenant=t["tenant_id"], customer_ref=t["customer_ref"],
                        subject=t["subject"], body=t["body"], received_at=t["received_at"])
        return ticket, existing

    # -- process --------------------------------------------------------------

    def _process(self, job: queue.Job) -> None:
        ticket, existing = self._load_ticket(job)
        if existing is not None:
            with tenant_tx(self.conn, job.tenant_id) as cur:
                if not queue.complete(cur, job, self.name):
                    raise LeaseLost()
                audit(cur, job.tenant_id, job.ticket_id, self.name, "skip_already_decided", details={"job": job.id})
            return
        outcome = lead.run(self.conn, self.gw, self.settings, ticket, job.run_id)
        d = outcome.decision
        with tenant_tx(self.conn, job.tenant_id) as cur:
            inserted = _insert_decision(cur, d, outcome.failure_class, outcome.trace)
            if inserted:
                if d.status == "pending_approval" and d.draft_reply:
                    cur.execute(
                        """INSERT INTO approvals (tenant_id, ticket_id, version, action, draft, kb_refs)
                           VALUES (%s, %s, 1, %s, %s, %s)""",
                        (d.tenant, d.ticket_id, d.action, d.draft_reply, d.kb_refs),
                    )
                if outcome.case:
                    _open_case(cur, d.tenant, d.ticket_id, outcome.case)
                cur.execute("UPDATE tickets SET state = %s WHERE ticket_id = %s", (d.status, d.ticket_id))
                audit(cur, d.tenant, d.ticket_id, "lead_agent", "decided", from_state="received", to_state=d.status,
                      reason="; ".join(outcome.trace.get("routing", {}).get("reasons", [])) or None,
                      details={"action": d.action, "run_id": d.run_id, "failure": outcome.failure_class})
            if not queue.complete(cur, job, self.name):
                raise LeaseLost()
        log(f"[worker] {d.tenant}/{d.ticket_id}: {d.action} -> {d.status} "
            f"({d.tokens_in}+{d.tokens_out} tok, {d.latency_ms} ms)")

    # -- redraft after a human rejection ---------------------------------------

    def _redraft(self, job: queue.Job) -> None:
        ticket, existing = self._load_ticket(job)
        version = int(job.payload["version"])
        feedback = [f"human reviewer: {job.payload['reason']}"]
        outcome = lead.run(self.conn, self.gw, self.settings, ticket, job.run_id, feedback=feedback)
        d = outcome.decision
        with tenant_tx(self.conn, job.tenant_id) as cur:
            cur.execute("SELECT state FROM tickets WHERE ticket_id = %s FOR UPDATE", (ticket.id,))
            if cur.fetchone()["state"] != "redrafting":
                # Already handled (duplicate job delivery); nothing to do.
                if not queue.complete(cur, job, self.name):
                    raise LeaseLost()
                return
            tokens_in = (existing or {}).get("tokens_in", 0) + d.tokens_in
            tokens_out = (existing or {}).get("tokens_out", 0) + d.tokens_out
            if d.status == "pending_approval" and d.draft_reply:
                cur.execute(
                    """INSERT INTO approvals (tenant_id, ticket_id, version, action, draft, kb_refs)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (d.tenant, d.ticket_id, version + 1, d.action, d.draft_reply, d.kb_refs),
                )
            elif outcome.case:
                _open_case(cur, d.tenant, d.ticket_id, outcome.case)
            cur.execute(
                """UPDATE decisions SET action = %s, status = %s, draft_reply = %s, kb_refs = %s,
                          tokens_in = %s, tokens_out = %s, model = %s, updated_at = now(),
                          trace = trace || jsonb_build_object('redraft', %s::jsonb)
                    WHERE ticket_id = %s""",
                (d.action, d.status, d.draft_reply, d.kb_refs, tokens_in, tokens_out,
                 _merge_models((existing or {}).get("model"), d.model), Jsonb(outcome.trace), d.ticket_id),
            )
            cur.execute("UPDATE tickets SET state = %s WHERE ticket_id = %s", (d.status, d.ticket_id))
            audit(cur, d.tenant, d.ticket_id, "drafter", "redrafted", from_state="redrafting", to_state=d.status,
                  reason=job.payload["reason"], details={"version": version + 1})
            if not queue.complete(cur, job, self.name):
                raise LeaseLost()
        log(f"[worker] {d.tenant}/{d.ticket_id}: redraft v{version + 1} -> {d.status}")

    def _write_failed_decision(self, job: queue.Job, error_class: str) -> None:
        if job.kind != "process":
            return
        decision = Decision(
            ticket_id=job.ticket_id, tenant=job.tenant_id, run_id=job.run_id, category="other", priority="normal",
            action="escalate", injection_detected=False, kb_refs=[], draft_reply=None, status="failed",
            model="none", tokens_in=0, tokens_out=0, latency_ms=0,
        )
        with tenant_tx(self.conn, job.tenant_id) as cur:
            _insert_decision(cur, decision, f"dead_letter:{error_class}", {"dead_letter": error_class})
            cur.execute("UPDATE tickets SET state = 'failed' WHERE ticket_id = %s", (job.ticket_id,))


def _insert_decision(cur: psycopg.Cursor, d: Decision, failure_class: str | None, trace: dict) -> bool:
    cur.execute(
        """INSERT INTO decisions (tenant_id, ticket_id, run_id, category, priority, action, injection_detected,
                                  kb_refs, draft_reply, status, model, tokens_in, tokens_out, latency_ms,
                                  failure_class, trace)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (tenant_id, ticket_id) DO NOTHING""",
        (d.tenant, d.ticket_id, d.run_id, d.category, d.priority, d.action, d.injection_detected, d.kb_refs,
         d.draft_reply, d.status, d.model, d.tokens_in, d.tokens_out, d.latency_ms, failure_class, Jsonb(trace)),
    )
    return cur.rowcount == 1


def _open_case(cur: psycopg.Cursor, tenant: str, ticket_id: str, case: dict) -> None:
    cur.execute(
        """INSERT INTO cases (tenant_id, ticket_id, source, owner, severity, reason)
           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, ticket_id, source) DO NOTHING""",
        (tenant, ticket_id, case["source"], case["owner"], case["severity"], case["reason"][:1000]),
    )


def _merge_models(a: str | None, b: str) -> str:
    parts: list[str] = []
    for m in (a or "").split("+") + b.split("+"):
        if m and m != "none" and m not in parts:
            parts.append(m)
    return "+".join(parts) or "none"
