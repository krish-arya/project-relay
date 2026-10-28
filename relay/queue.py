"""Durable work queue in Postgres.

- claim: relay_claim_job() leases one ready job with FOR UPDATE SKIP LOCKED,
  so several workers never take the same job; an expired lease is reclaimable
- complete: inside the same tenant transaction that writes the decision
- retry: exponential backoff with full jitter, up to max_attempts
- dead letter: the job is marked dead and a case is opened with an owner,
  a severity and the reason
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from relay.config import Settings
from relay.db import audit, global_tx, tenant_tx


@dataclass
class Job:
    id: int
    tenant_id: str
    ticket_id: str
    kind: str
    attempts: int
    max_attempts: int
    payload: dict
    run_id: str


def enqueue(cur: psycopg.Cursor, tenant_id: str, ticket_id: str, kind: str, key: str, run_id: str,
            max_attempts: int, payload: dict | None = None) -> bool:
    cur.execute(
        """INSERT INTO jobs (tenant_id, ticket_id, kind, payload, idempotency_key, run_id, max_attempts)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING""",
        (tenant_id, ticket_id, kind, Jsonb(payload or {}), key, run_id, max_attempts),
    )
    return cur.rowcount == 1


def claim(conn: psycopg.Connection, worker: str, lease_s: int) -> Job | None:
    with global_tx(conn) as cur:
        cur.execute("SELECT * FROM relay_claim_job(%s, %s)", (worker, lease_s))
        row = cur.fetchone()
    if row is None:
        return None
    with tenant_tx(conn, row["tenant_id"]) as cur:
        cur.execute("SELECT payload, run_id FROM jobs WHERE id = %s", (row["job_id"],))
        extra = cur.fetchone()
    return Job(row["job_id"], row["tenant_id"], row["ticket_id"], row["kind"], row["attempts"],
               row["max_attempts"], extra["payload"], extra["run_id"])


def complete(cur: psycopg.Cursor, job: Job, worker: str) -> bool:
    """Mark done, only if this worker still holds the lease. False means another worker took over."""
    cur.execute(
        """UPDATE jobs SET status = 'done', lease_until = NULL, updated_at = now()
            WHERE id = %s AND status = 'leased' AND leased_by = %s""",
        (job.id, worker),
    )
    return cur.rowcount == 1


def backoff_s(attempt: int, settings: Settings, rng: random.Random | None = None) -> float:
    """Full jitter: uniform(0, min(max, base * 2^(attempt-1)))."""
    cap = min(settings.backoff_max_s, settings.backoff_base_s * (2 ** max(attempt - 1, 0)))
    return (rng or random).uniform(0, cap)


def fail(conn: psycopg.Connection, job: Job, error: str, error_class: str, settings: Settings,
         severity: str = "sev2") -> str:
    """Schedule a retry, or dead-letter the job into a case. Returns 'retry' or 'dead'."""
    with tenant_tx(conn, job.tenant_id) as cur:
        if job.attempts < job.max_attempts:
            delay = backoff_s(job.attempts, settings)
            cur.execute(
                """UPDATE jobs SET status = 'queued', lease_until = NULL, leased_by = NULL, last_error = %s,
                                  run_after = now() + make_interval(secs => %s), updated_at = now()
                    WHERE id = %s""",
                (f"{error_class}: {error}"[:500], delay, job.id),
            )
            audit(cur, job.tenant_id, job.ticket_id, "worker", "retry_scheduled", reason=error_class,
                  details={"attempt": job.attempts, "delay_s": round(delay, 2), "error": error[:300]})
            return "retry"
        cur.execute(
            """UPDATE jobs SET status = 'dead', lease_until = NULL, last_error = %s, updated_at = now() WHERE id = %s""",
            (f"{error_class}: {error}"[:500], job.id),
        )
        cur.execute(
            """INSERT INTO cases (tenant_id, ticket_id, source, owner, severity, reason)
               VALUES (%s, %s, 'dead_letter', 'platform-oncall', %s, %s)
               ON CONFLICT (tenant_id, ticket_id, source) DO NOTHING""",
            (job.tenant_id, job.ticket_id, severity,
             f"{job.kind} job failed {job.attempts} times; last error {error_class}: {error[:200]}"),
        )
        cur.execute("UPDATE tickets SET state = 'dead_lettered' WHERE ticket_id = %s", (job.ticket_id,))
        audit(cur, job.tenant_id, job.ticket_id, "worker", "dead_lettered", to_state="dead_lettered",
              reason=error_class, details={"attempts": job.attempts})
        return "dead"


def stats(conn: psycopg.Connection) -> dict[str, int]:
    with global_tx(conn) as cur:
        cur.execute("SELECT * FROM relay_queue_stats()")
        return {r["status"]: r["n"] for r in cur.fetchall()}
