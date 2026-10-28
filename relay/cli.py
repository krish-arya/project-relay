"""relay: command line for operators.

  relay demo                         migrate, seed, process both sample inboxes, print the approval queue
  relay run --inbox FILE [...]       ingest any inbox in the Appendix B format and process it
  relay worker                       drain the queue (safe to run several, safe to kill)
  relay list | show | approve | reject   the human approval gate
  relay eval --runs N                triage + routing over the golden set, writes eval/ and docs/EVAL.md
  relay report --view internal|client [--tenant X]
  relay requeue-held                 re-queue tickets held for budget after raising the budget
  relay doctor                       check Postgres and Ollama
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
import psycopg

from relay import approval, export, queue
from relay.config import ROOT, Settings, load_settings
from relay.db import audit, connect, known_tenants, migrate, tenant_tx
from relay.gateway import Gateway, make_provider
from relay.ingest import ingest_file
from relay.runs import create_run, finish_run
from relay.seed import seed
from relay.worker import Worker

DEMO_INBOXES = [ROOT / "data" / "northwind" / "tickets.jsonl", ROOT / "data" / "harbor" / "tickets.jsonl"]


def _gateway(settings: Settings, conn: psycopg.Connection) -> Gateway:
    return Gateway(settings, make_provider(settings), conn)


def cmd_doctor(settings: Settings, _args=None) -> int:
    ok = True
    try:
        with psycopg.connect(settings.database_admin_url, connect_timeout=5) as c:
            c.execute("SELECT 1")
        print(f"[ok] postgres reachable ({settings.database_admin_url.split('@')[-1]})")
    except psycopg.Error as e:
        ok = False
        print(f"[fail] postgres: {str(e).strip().splitlines()[0]}\n       start it with: docker compose up -d --wait")
    if settings.model_provider == "ollama":
        try:
            tags = httpx.get(f"{settings.ollama_url}/api/tags", timeout=5).json()
            have = {m["name"] for m in tags.get("models", [])}
            have |= {n.removesuffix(":latest") for n in have}
            for m in sorted({settings.model_triage, settings.model_draft, settings.model_embed}):
                if m in have:
                    print(f"[ok] model {m}")
                else:
                    ok = False
                    print(f"[fail] model {m} missing: run  ollama pull {m}")
        except httpx.HTTPError as e:
            ok = False
            print(f"[fail] ollama at {settings.ollama_url}: {e}\n       start it with: ollama serve")
    else:
        print(f"[ok] model provider: {settings.model_provider} (no Ollama needed)")
    return 0 if ok else 1


def cmd_migrate(settings: Settings, _args=None) -> int:
    applied = migrate(settings)
    print(f"migrations applied: {', '.join(applied) if applied else 'none (up to date)'}")
    return 0


def cmd_seed(settings: Settings, _args=None) -> int:
    with connect(settings) as conn:
        run_id = create_run(conn, "seed", settings.token_budget_run)
        counts = seed(conn, _gateway(settings, conn), run_id)
        finish_run(conn, run_id)
    print("seeded KB articles (re-embedded): " + ", ".join(f"{t}={n}" for t, n in counts.items()))
    return 0


def _process(settings: Settings, inboxes: list[Path], kind: str) -> int:
    with connect(settings) as conn:
        run_id = create_run(conn, kind, settings.token_budget_run)
        print(f"run {run_id}")
        for path in inboxes:
            stats = ingest_file(conn, settings, path, run_id)
            print(f"ingested {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: "
                  f"{stats.accepted} new, {stats.duplicates} already known, {len(stats.rejected)} rejected")
            for r in stats.rejected:
                print(f"  rejected line {r['line']} ({r['ticket_id']}): {r['reason']}")
        started = time.perf_counter()
        counts = Worker(settings, conn, _gateway(settings, conn)).run_until_empty()
        finish_run(conn, run_id)
        written = export.export_all(conn)
        print(f"worker: {counts['done']} done, {counts['retry']} retried, {counts['dead']} dead-lettered "
              f"in {time.perf_counter() - started:.1f}s")
        print(f"wrote out/decisions.jsonl ({written['decisions']} rows), out/outbox.jsonl ({written['outbox']} rows)")
        _print_queue(conn)
    return 0


def cmd_run(settings: Settings, args) -> int:
    paths = [Path(p) for p in args.inbox]
    for p in paths:
        if not p.exists():
            print(f"inbox not found: {p}", file=sys.stderr)
            return 2
    return _process(settings, paths, "run")


def cmd_demo(settings: Settings, _args=None) -> int:
    started = time.perf_counter()
    if cmd_doctor(settings) != 0:
        return 1
    cmd_migrate(settings)
    cmd_seed(settings)
    code = _process(settings, DEMO_INBOXES, "demo")
    print(f"demo finished in {time.perf_counter() - started:.0f}s")
    return code


def cmd_worker(settings: Settings, args) -> int:
    with connect(settings) as conn:
        counts = Worker(settings, conn, _gateway(settings, conn)).run_until_empty(max_jobs=args.max_jobs)
        export.export_all(conn)
    print(f"worker: {counts}")
    return 0


def _print_queue(conn: psycopg.Connection, tenant: str | None = None) -> None:
    rows = approval.pending(conn, tenant)
    print(f"\nAPPROVAL QUEUE ({len(rows)} awaiting a human)")
    if not rows:
        return
    print(f"{'TICKET':<8} {'TENANT':<10} {'ACTION':<13} {'CATEGORY':<18} {'PRIO':<7} DRAFT")
    for r in rows:
        draft = " ".join(r["draft"].split())
        print(f"{r['ticket_id']:<8} {r['tenant_id']:<10} {r['action']:<13} {r['category']:<18} {r['priority']:<7} "
              f"{draft[:70]}{'...' if len(draft) > 70 else ''}")
    print("\nnext: relay show <ticket> | relay approve <ticket> | relay reject <ticket> --reason \"...\"")


def cmd_list(settings: Settings, args) -> int:
    with connect(settings) as conn:
        _print_queue(conn, args.tenant)
        escalated = []
        for t in [args.tenant] if args.tenant else list(known_tenants(conn)):
            with tenant_tx(conn, t) as cur:
                cur.execute("""SELECT c.ticket_id, c.tenant_id, c.owner, c.severity, c.reason FROM cases c
                               WHERE c.status = 'open' ORDER BY c.severity, c.ticket_id""")
                escalated.extend(cur.fetchall())
        print(f"\nOPEN CASES ({len(escalated)})")
        for c in escalated:
            print(f"{c['ticket_id']:<8} {c['tenant_id']:<10} {c['severity']:<5} {c['owner']:<17} {c['reason'][:80]}")
    return 0


def cmd_show(settings: Settings, args) -> int:
    with connect(settings) as conn:
        info = approval.show(conn, args.ticket, args.tenant)
    t, d = info["ticket"], info["decision"]
    print(f"{t['ticket_id']} [{t['tenant_id']}] state={t['state']}  from {t['customer_ref']}  received {t['received_at']}")
    print(f"subject: {t['subject']}\nbody:    {t['body']}\n")
    if d:
        print(f"decision: action={d['action']} status={d['status']} category={d['category']} priority={d['priority']} "
              f"injection={d['injection_detected']} kb_refs={list(d['kb_refs'])}")
        print(f"          model={d['model']} tokens={d['tokens_in']}+{d['tokens_out']} latency={d['latency_ms']}ms")
        reasons = d["trace"].get("routing", {}).get("reasons", [])
        if reasons:
            print(f"          why: {'; '.join(reasons)}")
    for a in info["approvals"]:
        print(f"\ndraft v{a['version']} ({a['action']}) [{a['state']}]"
              + (f" by {a['decided_by']}: {a['reason'] or ''}" if a["decided_by"] else ""))
        print(f"  {a['draft']}")
    for c in info["cases"]:
        print(f"\ncase #{c['id']} [{c['status']}] {c['severity']} owner={c['owner']} source={c['source']}\n  {c['reason']}")
    if info["outbox"]:
        o = info["outbox"]
        print(f"\noutbox: sent, approved by {o['approved_by']} at {o['approved_at']} ({o['idempotency_key']})")
    print("\naudit:")
    for a in info["audit"]:
        arrow = f"{a['from_state'] or ''}->{a['to_state']}" if a["to_state"] else ""
        print(f"  {a['at']:%Y-%m-%d %H:%M:%S} {a['actor']:<22} {a['action']:<22} {arrow:<30} {a['reason'] or ''}")
    return 0


def cmd_approve(settings: Settings, args) -> int:
    with connect(settings) as conn:
        try:
            res = approval.approve(conn, args.ticket, args.by or settings.operator, args.tenant)
        except approval.ApprovalError as e:
            print(f"not approved: {e}", file=sys.stderr)
            return 1
        export.export_all(conn)
    print(f"approved {res['tenant']}/{res['ticket_id']} v{res['version']} -> out/outbox.jsonl ({res['idempotency_key']})")
    return 0


def cmd_reject(settings: Settings, args) -> int:
    with connect(settings) as conn:
        try:
            res = approval.reject(conn, settings, args.ticket, args.by or settings.operator, args.reason, args.tenant)
        except approval.ApprovalError as e:
            print(f"not rejected: {e}", file=sys.stderr)
            return 1
        print(f"rejected {res['tenant']}/{res['ticket_id']} -> {res['next']}")
        if res["next"] == "redrafting" and not args.no_redraft:
            Worker(settings, conn, _gateway(settings, conn)).run_until_empty()
            info = approval.show(conn, args.ticket, res["tenant"])
            latest = info["approvals"][-1]
            if latest["state"] == "pending":
                print(f"new draft v{latest['version']} awaiting approval:\n  {latest['draft']}")
            else:
                print(f"redraft did not pass the checker; ticket is now {info['ticket']['state']}")
        export.export_all(conn)
    return 0


def cmd_requeue_held(settings: Settings, _args) -> int:
    n = 0
    with connect(settings) as conn:
        run_id = create_run(conn, "requeue", settings.token_budget_run)
        for t in known_tenants(conn):
            with tenant_tx(conn, t) as cur:
                cur.execute("DELETE FROM decisions WHERE status = 'held_budget' RETURNING ticket_id")
                for r in cur.fetchall():
                    queue.enqueue(cur, t, r["ticket_id"], "process", f"process:{t}:{r['ticket_id']}:{run_id}",
                                  run_id, settings.max_attempts)
                    cur.execute("UPDATE tickets SET state = 'received' WHERE ticket_id = %s", (r["ticket_id"],))
                    audit(cur, t, r["ticket_id"], settings.operator, "requeued_after_budget", from_state="held_budget",
                          to_state="received")
                    n += 1
    print(f"re-queued {n} held tickets; now run: relay worker")
    return 0


def cmd_export(settings: Settings, _args) -> int:
    with connect(settings) as conn:
        print(json.dumps(export.export_all(conn)))
    return 0


def cmd_eval(settings: Settings, args) -> int:
    from relay.evaluate import run_eval

    return run_eval(settings, runs=args.runs, include_probes=not args.no_probes)


def cmd_report(settings: Settings, args) -> int:
    from relay.report import client_report, internal_report

    with connect(settings) as conn:
        if args.view == "client":
            if not args.tenant:
                print("--tenant is required for the client view", file=sys.stderr)
                return 2
            print(client_report(conn, args.tenant))
        else:
            print(internal_report(conn))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("migrate")
    sub.add_parser("seed")
    sub.add_parser("demo")
    sub.add_parser("export")
    sub.add_parser("requeue-held")
    r = sub.add_parser("run")
    r.add_argument("--inbox", action="append", required=True, help="ticket file (JSON Lines); repeatable")
    w = sub.add_parser("worker")
    w.add_argument("--max-jobs", type=int, default=None)
    ls = sub.add_parser("list")
    ls.add_argument("--tenant")
    for name in ("show", "approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("ticket")
        s.add_argument("--tenant")
        if name != "show":
            s.add_argument("--by", help="approver name (default: RELAY_OPERATOR)")
        if name == "reject":
            s.add_argument("--reason", required=True)
            s.add_argument("--no-redraft", action="store_true", help="queue the redraft but do not run it now")
    e = sub.add_parser("eval")
    e.add_argument("--runs", type=int, default=5)
    e.add_argument("--no-probes", action="store_true")
    rep = sub.add_parser("report")
    rep.add_argument("--view", choices=["internal", "client"], default="internal")
    rep.add_argument("--tenant")
    return p


COMMANDS = {
    "doctor": cmd_doctor, "migrate": cmd_migrate, "seed": cmd_seed, "demo": cmd_demo, "run": cmd_run,
    "worker": cmd_worker, "list": cmd_list, "show": cmd_show, "approve": cmd_approve, "reject": cmd_reject,
    "export": cmd_export, "eval": cmd_eval, "report": cmd_report, "requeue-held": cmd_requeue_held,
}


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    return COMMANDS[args.cmd](load_settings(), args)


if __name__ == "__main__":
    raise SystemExit(main())
