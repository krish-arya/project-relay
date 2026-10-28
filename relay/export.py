"""Write out/decisions.jsonl and out/outbox.jsonl from the database.

The files are regenerated from the tables on every export, so they can never
contain more rows than the database allows: one decision per ticket, at most
one outbox message per ticket. Internal-only fields (trace, failure_class) are
never exported.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg

from relay.config import ROOT
from relay.contracts import Decision
from relay.db import global_tx, known_tenants, tenant_tx

OUT = ROOT / "out"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def decision_rows(conn: psycopg.Connection, tenant: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for t in [tenant] if tenant else list(known_tenants(conn)):
        with tenant_tx(conn, t) as cur:
            cur.execute("SELECT * FROM decisions ORDER BY ticket_id")
            for r in cur.fetchall():
                d = Decision(
                    ticket_id=r["ticket_id"], tenant=r["tenant_id"], run_id=r["run_id"], category=r["category"],
                    priority=r["priority"], action=r["action"], injection_detected=r["injection_detected"],
                    kb_refs=list(r["kb_refs"]), draft_reply=r["draft_reply"], status=r["status"], model=r["model"],
                    tokens_in=r["tokens_in"], tokens_out=r["tokens_out"], latency_ms=r["latency_ms"],
                )
                rows.append(d.model_dump())
    return rows


def outbox_rows(conn: psycopg.Connection, tenant: str | None = None) -> list[dict]:
    rows: list[dict] = []
    for t in [tenant] if tenant else list(known_tenants(conn)):
        with tenant_tx(conn, t) as cur:
            cur.execute("SELECT * FROM outbox ORDER BY id")
            for r in cur.fetchall():
                rows.append({
                    "ticket_id": r["ticket_id"], "tenant": r["tenant_id"], "text": r["text"],
                    "approved_by": r["approved_by"], "approved_at": r["approved_at"].isoformat(),
                    "idempotency_key": r["idempotency_key"],
                })
    return rows


def rejected_rows(conn: psycopg.Connection) -> list[dict]:
    with global_tx(conn) as cur:
        cur.execute("SELECT source, line_no, claimed_tenant, ticket_id, reason, at FROM rejected_inputs ORDER BY id")
        return [{**r, "at": r["at"].isoformat()} for r in cur.fetchall()]


def export_all(conn: psycopg.Connection, out_dir: Path = OUT) -> dict[str, int]:
    decisions = decision_rows(conn)
    outbox = outbox_rows(conn)
    rejected = rejected_rows(conn)
    _write_jsonl(out_dir / "decisions.jsonl", decisions)
    _write_jsonl(out_dir / "outbox.jsonl", outbox)
    _write_jsonl(out_dir / "rejected_inputs.jsonl", rejected)
    return {"decisions": len(decisions), "outbox": len(outbox), "rejected": len(rejected)}
