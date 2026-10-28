"""`make eval`: triage + routing over the golden set, N times, temperature 0, fixed seed.

Writes eval/runs/run_N.jsonl (decision rows, draft_reply null) and docs/EVAL.md.
golden.jsonl is read here and only here: it never reaches the decision path.
The narrative section of EVAL.md is copied verbatim from eval/NOTES.md, which
a human writes; every number in EVAL.md is computed by this module.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from relay import injection, lead
from relay.config import ROOT, Settings
from relay.contracts import Ticket
from relay.db import connect, known_tenants
from relay.gateway import Gateway, ModelUnavailable, make_provider
from relay.runs import create_run, finish_run

EVAL_DIR = ROOT / "eval"
RUNS_DIR = EVAL_DIR / "runs"
DOC = ROOT / "docs" / "EVAL.md"


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def golden_tickets(tenants: dict) -> tuple[list[Ticket], dict[str, dict]]:
    golden = {g["id"]: g for g in load_jsonl(ROOT / "data" / "golden.jsonl")}
    tickets = []
    for t in tenants:
        path = ROOT / "data" / t / "tickets.jsonl"
        if path.exists():
            tickets += [Ticket.model_validate(o) for o in load_jsonl(path) if o["id"] in golden]
    return tickets, golden


def run_eval(settings: Settings, runs: int, include_probes: bool = True) -> int:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    for old in RUNS_DIR.glob("run_*.jsonl"):
        old.unlink()
    all_runs: list[list[dict]] = []
    run_meta: list[dict] = []
    with connect(settings) as conn:
        tickets, golden = golden_tickets(known_tenants(conn))
        gw = Gateway(settings, make_provider(settings), conn)
        for i in range(1, runs + 1):
            run_id = create_run(conn, "eval", settings.token_budget_run)
            started = time.perf_counter()
            rows = []
            for t in tickets:
                try:
                    out = lead.run(conn, gw, settings, t, run_id, with_draft=False)
                    row = out.decision.model_dump()
                    row["error"] = out.failure_class
                except ModelUnavailable as e:
                    row = {"ticket_id": t.id, "tenant": t.tenant, "run_id": run_id, "category": None, "priority": None,
                           "action": None, "injection_detected": None, "kb_refs": [], "draft_reply": None,
                           "status": "failed", "model": "none", "tokens_in": 0, "tokens_out": 0, "latency_ms": 0,
                           "error": f"model_unavailable:{e.error_class}"}
                rows.append(row)
            wall = time.perf_counter() - started
            finish_run(conn, run_id)
            with (RUNS_DIR / f"run_{i}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            all_runs.append(rows)
            run_meta.append({"run": i, "run_id": run_id, "wall_s": wall})
            m = metrics(rows, golden)
            print(f"run {i}/{runs}: action {m['action_acc']:.0%}  category {m['category_acc']:.0%}  "
                  f"priority {m['priority_acc']:.0%}  injection recall {m['inj_recall']:.0%} fp {m['inj_fp']}  "
                  f"tokens {m['tokens']}  {wall:.0f}s", file=sys.stderr)
        probes = probe_results(conn, gw, settings) if include_probes else None
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(render(settings, all_runs, run_meta, golden, probes), encoding="utf-8", newline="\n")
    print(f"wrote {DOC.relative_to(ROOT)} and {len(all_runs)} files in {RUNS_DIR.relative_to(ROOT)}/")
    return 0


def metrics(rows: list[dict], golden: dict[str, dict]) -> dict:
    n = len(rows)
    ok = lambda f, g=None: sum(1 for r in rows if r[f] == golden[r["ticket_id"]][g or f])  # noqa: E731
    pos = [r for r in rows if golden[r["ticket_id"]]["injection"]]
    neg = [r for r in rows if not golden[r["ticket_id"]]["injection"]]
    kb_hit = [r for r in rows if golden[r["ticket_id"]]["kb_refs"]]
    return {
        "n": n,
        "action_acc": ok("action") / n,
        "category_acc": ok("category") / n,
        "priority_acc": ok("priority") / n,
        "inj_recall": sum(1 for r in pos if r["injection_detected"]) / len(pos) if pos else 1.0,
        "inj_fp": sum(1 for r in neg if r["injection_detected"]),
        "kb_hit": sum(1 for r in kb_hit if set(golden[r["ticket_id"]]["kb_refs"]) & set(r["kb_refs"])) / len(kb_hit),
        "tokens": sum(r["tokens_in"] + r["tokens_out"] for r in rows),
        "errors": sum(1 for r in rows if r.get("error")),
    }


def probe_results(conn, gw: Gateway, settings: Settings) -> list[dict]:
    """Injection probes: structural score, model flag and final decision for each."""
    path = ROOT / "data" / "probes" / "injection_probes.jsonl"
    if not path.exists():
        return []
    run_id = create_run(conn, "eval", settings.token_budget_run)
    results = []
    for obj in load_jsonl(path):
        t = Ticket.model_validate(obj)
        report = injection.score(f"{t.subject}\n{t.body}")
        try:
            out = lead.run(conn, gw, settings, t, run_id, with_draft=False)
            model_flag = next((s["result"]["injection_suspected"] for s in out.trace.get("steps", [])
                               if s["agent"] == "triage"), None)
            final, action = out.decision.injection_detected, out.decision.action
        except ModelUnavailable as e:
            model_flag, final, action = None, None, f"error:{e.error_class}"
        results.append({"id": t.id, "label": obj["injection"], "score": report.score, "features": report.features,
                        "model_flag": model_flag, "final": final, "action": action})
    finish_run(conn, run_id)
    return results


def render(settings: Settings, all_runs: list[list[dict]], meta: list[dict], golden: dict, probes) -> str:
    per_run = [metrics(rows, golden) for rows in all_runs]
    ids = [r["ticket_id"] for r in all_runs[0]]
    by_ticket = {tid: [next(r for r in rows if r["ticket_id"] == tid) for rows in all_runs] for tid in ids}
    stable_action = sum(1 for tid in ids if len({r["action"] for r in by_ticket[tid]}) == 1) / len(ids)
    stable_all = sum(1 for tid in ids if len({(r["action"], r["category"], r["priority"], r["injection_detected"])
                                              for r in by_ticket[tid]}) == 1) / len(ids)

    def mean(key: str) -> float:
        return statistics.mean(m[key] for m in per_run)

    def rng(key: str, pct: bool = True) -> str:
        vals = [m[key] for m in per_run]
        f = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{v}")
        return f(min(vals)) if min(vals) == max(vals) else f"{f(min(vals))} to {f(max(vals))}"

    lines = [
        "# Evaluation",
        "",
        f"> Generated by `make eval RUNS={len(all_runs)}` on {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}. "
        "Every number below is computed by `relay/evaluate.py` from `eval/runs/run_N.jsonl`; "
        "do not edit them by hand. The commentary at the end is `eval/NOTES.md`.",
        "",
        "## Setup",
        "",
        f"- Tickets: {len(ids)} golden tickets (both tenants), {len(all_runs)} runs, triage and routing only "
        "(no drafting, `draft_reply` is null).",
        f"- Models: triage `{settings.model_triage}`, embeddings `{settings.model_embed}`; "
        f"temperature {settings.temperature}, seed {settings.seed}, provider `{settings.model_provider}`.",
        f"- Routing thresholds: `RETRIEVAL_MIN_SCORE={settings.min_score}`, "
        f"`RETRIEVAL_CATEGORY_MARGIN={settings.category_margin}`.",
        f"- Machine: {platform.system()} {platform.release()}, {platform.machine()}, {os.cpu_count()} logical CPUs, "
        f"Python {platform.python_version()}.",
        "",
        "## Summary",
        "",
        "| Metric | Mean over runs | Range |",
        "|---|---|---|",
        f"| Action accuracy | {mean('action_acc'):.1%} | {rng('action_acc')} |",
        f"| Category accuracy | {mean('category_acc'):.1%} | {rng('category_acc')} |",
        f"| Priority accuracy | {mean('priority_acc'):.1%} | {rng('priority_acc')} |",
        f"| Injection recall (golden) | {mean('inj_recall'):.1%} | {rng('inj_recall')} |",
        f"| Injection false positives (golden, count) | {mean('inj_fp'):.1f} | {rng('inj_fp', False)} |",
        f"| KB ref hit (golden ref among ours) | {mean('kb_hit'):.1%} | {rng('kb_hit')} |",
        f"| Stability: same action in every run | {stable_action:.1%} | |",
        f"| Stability: same action, category, priority and flag in every run | {stable_all:.1%} | |",
        f"| Tokens per run (in + out) | {mean('tokens'):.0f} | {rng('tokens', False)} |",
        f"| Wall time per run | {statistics.mean(m['wall_s'] for m in meta):.1f}s | "
        f"{min(m['wall_s'] for m in meta):.1f}s to {max(m['wall_s'] for m in meta):.1f}s |",
        f"| Classified errors per run | {mean('errors'):.1f} | {rng('errors', False)} |",
        "",
        "## Per run",
        "",
        "| Run | run_id | Action | Category | Priority | Inj. recall | Inj. FP | Tokens | Wall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m, rm in zip(per_run, meta):
        lines.append(f"| {rm['run']} | `{rm['run_id']}` | {m['action_acc']:.0%} | {m['category_acc']:.0%} | "
                     f"{m['priority_acc']:.0%} | {m['inj_recall']:.0%} | {m['inj_fp']} | {m['tokens']} | {rm['wall_s']:.0f}s |")

    lines += ["", "## Errors per ticket", "",
              "Every ticket where any run disagreed with the golden label. `x/N` = runs that got it right.", "",
              "| Ticket | Field | Expected | Got (per run) | Right |", "|---|---|---|---|---|"]
    any_error = False
    for tid in ids:
        g = golden[tid]
        for field, gfield in (("action", "action"), ("category", "category"), ("priority", "priority"),
                              ("injection_detected", "injection")):
            got = [r[field] for r in by_ticket[tid]]
            right = sum(1 for v in got if v == g[gfield])
            if right < len(got):
                any_error = True
                lines.append(f"| {tid} | {field} | {g[gfield]} | {', '.join(str(v) for v in got)} | {right}/{len(got)} |")
        errs = [r.get("error") for r in by_ticket[tid] if r.get("error")]
        if errs:
            any_error = True
            lines.append(f"| {tid} | error | none | {', '.join(errs)} | {len(got) - len(errs)}/{len(got)} |")
    if not any_error:
        lines.append("| (none) | | | | |")

    if probes:
        tp = sum(1 for p in probes if p["label"] and p["final"])
        fn = sum(1 for p in probes if p["label"] and not p["final"])
        fp = sum(1 for p in probes if not p["label"] and p["final"])
        tn = sum(1 for p in probes if not p["label"] and not p["final"])
        model_tp = sum(1 for p in probes if p["label"] and p["model_flag"])
        model_fp = sum(1 for p in probes if not p["label"] and p["model_flag"])
        npos, nneg = tp + fn, fp + tn
        lines += ["", "## Injection probes", "",
                  f"`data/probes/injection_probes.jsonl`: {npos} injections and {nneg} benign look-alikes, run once. "
                  "See NOTES for how these were written.", "",
                  f"- Final decision: recall {tp}/{npos}, false positives {fp}/{nneg}.",
                  f"- Triage model flag alone would give: recall {model_tp}/{npos}, false positives {model_fp}/{nneg}.",
                  "", "| Probe | Label | Structural score | Features | Model flag | Final | Action |", "|---|---|---|---|---|---|---|"]
        for p in probes:
            lines.append(f"| {p['id']} | {p['label']} | {p['score']} | {', '.join(p['features']) or '-'} | "
                         f"{p['model_flag']} | {p['final']} | {p['action']} |")

    action_counts = Counter(r["action"] for rows in all_runs for r in rows)
    lines += ["", "## Action mix (all runs)", "", ", ".join(f"{k}: {v}" for k, v in sorted(action_counts.items(), key=str)), ""]
    notes = EVAL_DIR / "NOTES.md"
    if notes.exists():
        lines += ["## Commentary (written by hand, eval/NOTES.md)", "", notes.read_text(encoding="utf-8").strip(), ""]
    return "\n".join(lines)
