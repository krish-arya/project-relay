"""Two views of the same data.

internal: runs, models, tokens, latency, queue and cases (for the operator).
client:   outcomes for one tenant only (counts by category and action, replies
          approved, median time to draft). It never shows models, prompts,
          tokens or agent names, and it reads through that tenant's RLS scope.
"""

from __future__ import annotations

import statistics

import psycopg

from relay import queue
from relay.db import global_tx, known_tenants, tenant_tx


def internal_report(conn: psycopg.Connection) -> str:
    out: list[str] = []
    with global_tx(conn) as cur:
        cur.execute("""SELECT run_id, kind, started_at, finished_at, tokens_used, token_budget
                         FROM runs ORDER BY started_at DESC LIMIT 10""")
        runs = cur.fetchall()
    out.append("RECENT RUNS")
    out.append(f"{'run_id':<31} {'kind':<8} {'tokens':>8} {'budget':>8} {'duration':>9}")
    for r in runs:
        dur = f"{(r['finished_at'] - r['started_at']).total_seconds():.0f}s" if r["finished_at"] else "running"
        out.append(f"{r['run_id']:<31} {r['kind']:<8} {r['tokens_used']:>8} {r['token_budget']:>8} {dur:>9}")

    out.append("\nMODEL CALLS BY TENANT, TASK AND MODEL")
    out.append(f"{'tenant':<10} {'task':<7} {'model':<18} {'calls':>6} {'failed':>6} {'tok in':>8} {'tok out':>8} {'p50 ms':>7} {'p95 ms':>7}")
    cases: list[dict] = []
    statuses: dict[str, dict[str, int]] = {}
    for t in known_tenants(conn):
        with tenant_tx(conn, t) as cur:
            cur.execute(
                """SELECT task, model, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS failed,
                          sum(tokens_in) AS tin, sum(tokens_out) AS tout,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE ok) AS p50,
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE ok) AS p95
                     FROM model_calls GROUP BY task, model ORDER BY task, model"""
            )
            for r in cur.fetchall():
                out.append(f"{t:<10} {r['task']:<7} {r['model']:<18} {r['calls']:>6} {r['failed']:>6} {r['tin']:>8} "
                           f"{r['tout']:>8} {r['p50'] or 0:>7.0f} {r['p95'] or 0:>7.0f}")
            cur.execute("SELECT status, count(*) AS n FROM decisions GROUP BY status")
            statuses[t] = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT ticket_id, source, owner, severity, status, reason FROM cases ORDER BY severity, ticket_id")
            cases += [{**r, "tenant": t} for r in cur.fetchall()]

    out.append("\nDECISIONS BY STATUS")
    for t, s in statuses.items():
        out.append(f"{t:<10} " + ", ".join(f"{k}={v}" for k, v in sorted(s.items())))
    out.append(f"\nQUEUE: {queue.stats(conn) or 'empty'}")
    out.append(f"\nCASES ({len(cases)})")
    for c in cases:
        out.append(f"{c['tenant']:<10} {c['ticket_id']:<8} {c['severity']:<5} {c['source']:<15} {c['owner']:<17} {c['reason'][:70]}")
    return "\n".join(out)


def client_report(conn: psycopg.Connection, tenant: str) -> str:
    tenants = known_tenants(conn)
    if tenant not in tenants:
        raise SystemExit(f"unknown tenant {tenant!r}")
    with tenant_tx(conn, tenant) as cur:
        cur.execute("SELECT category, action, count(*) AS n FROM decisions GROUP BY category, action ORDER BY category, action")
        mix = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM outbox")
        sent = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM tickets")
        total = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM approvals WHERE state = 'pending'")
        waiting = cur.fetchone()["n"]
        cur.execute("""SELECT extract(epoch FROM a.created_at - t.ingested_at) AS s
                         FROM approvals a JOIN tickets t USING (tenant_id, ticket_id) WHERE a.version = 1""")
        draft_times = [float(r["s"]) for r in cur.fetchall()]
    label = {"draft_reply": "answered", "ask_for_info": "asked customer for details", "escalate": "handed to a specialist"}
    out = [f"Support summary for {tenants[tenant]['name']}", "",
           f"Tickets received: {total}", f"Replies approved and sent: {sent}", f"Replies awaiting review: {waiting}"]
    if draft_times:
        out.append(f"Median time to a draft reply: {statistics.median(draft_times):.0f} seconds")
    out += ["", f"{'Topic':<20} {'Outcome':<28} {'Tickets':>7}"]
    for r in mix:
        out.append(f"{r['category'].replace('_', ' '):<20} {label[r['action']]:<28} {r['n']:>7}")
    return "\n".join(out)
