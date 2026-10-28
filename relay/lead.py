"""Lead agent: plans the steps for one ticket, routes work between sub-agents,
applies the business rules, and writes the decision record.

The lead agent does not persist anything itself; it returns an Outcome that the
worker commits in one tenant-bound transaction (decision + approval + case +
audit + job completion), so a crash can never leave half a decision behind.

Who decides what:
- the triage model proposes category, priority, action and flags
- deterministic signals (policy.py, injection.py) can only make the outcome
  *safer*: escalate instead of reply, raise priority, ask instead of guess
- retrieval score decides whether the KB actually covers the request
- the checker decides whether a draft may reach the approval queue
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import psycopg

from relay import agents, injection
from relay.checker import check, load_context
from relay.config import Settings
from relay.contracts import Decision, DraftResult, Passage, Ticket, TriageResult
from relay.db import tenant_tx
from relay.gateway import BudgetExceeded, CallContext, Gateway, InvalidOutput
from relay.policy import SEVERITY, TenantPolicy, load_policy, raise_priority
from relay.text import KB_ID


@dataclass
class Outcome:
    decision: Decision
    case: dict | None = None
    failure_class: str | None = None
    trace: dict = field(default_factory=dict)


@dataclass
class Routing:
    category: str
    priority: str
    action: str
    injection: bool
    reasons: list[str]
    missing: list[str]
    draft_mode: str | None  # reply | ask_for_info | acknowledge


def retrieval_category(passages: list[Passage], policy: TenantPolicy, min_score: float, margin: float) -> str | None:
    """Category of the best KB match, only when retrieval is decisive (internal articles count: routing only)."""
    if not passages:
        return None
    top = passages[0]
    runner_up = passages[1].score if len(passages) > 1 else 0.0
    if top.score >= min_score and top.score - runner_up >= margin:
        return policy.kb_categories.get(top.kb_id)
    return None


def route(ticket: Ticket, tri: TriageResult, passages: list[Passage], policy: TenantPolicy, min_score: float,
          margin: float = 0.04) -> Routing:
    """Pure function: triage proposal + deterministic signals + retrieval -> action."""
    text = f"{ticket.subject}\n{ticket.body}"
    inj_report = injection.score(text)
    is_injection = injection.decide(inj_report, tri.injection_suspected)
    clinical_terms, severe = policy.clinical_signals(ticket)
    legal = policy.legal_threat(ticket)
    money_kw = policy.money_dispute(ticket)
    money = bool(money_kw) or (tri.money_dispute and tri.proposed_action == "escalate")

    category, priority = tri.category, tri.priority
    reasons: list[str] = []
    kb_category = retrieval_category(passages, policy, min_score, margin)
    # A model "clinical" label is kept unless clinical terms are absent AND the KB
    # match is decisive: when unsure, the safe failure is escalating to a clinician.
    model_clinical_unsupported = category == "clinical" and not clinical_terms
    if kb_category and kb_category != category and (category != "clinical" or model_clinical_unsupported):
        reasons.append(f"category {category}->{kb_category} (decisive KB match {passages[0].kb_id})")
        category = kb_category
    served = set(policy.kb_categories.values())
    if category not in served and category not in ("other", "clinical"):
        reasons.append(f"category {category}->other (not a {policy.tenant} category)")
        category = "other"
    if clinical_terms and category != "clinical":
        reasons.append(f"category {category}->clinical (terms: {', '.join(clinical_terms[:3])})")
        category = "clinical"
    if policy.clinical is None and category == "clinical":
        category = "other"

    safety = policy.safety_risk(ticket)
    if priority == "urgent" and not (legal or severe or safety):
        # Brief: urgent means a safety risk or a legal threat. Without evidence of either, cap at high.
        priority = "high"
    if legal or safety:
        priority = raise_priority(priority, "urgent")
    if category == "clinical" and severe:
        priority = raise_priority(priority, "urgent")
    if is_injection or money:
        priority = raise_priority(priority, "high")
    if category == "clinical":
        priority = raise_priority(priority, "normal")
    if category == "order_change":
        priority = raise_priority(priority, "high")  # time-sensitive: only possible until packing

    top_public = max((p.score for p in passages if not p.internal), default=0.0)
    covered = top_public >= min_score
    missing: list[str] = []
    draft_mode: str | None = None

    if is_injection:
        action = "escalate"
        reasons.append(f"prompt injection (features: {', '.join(inj_report.features) or 'model flag'})")
    elif category == "clinical":
        action = "escalate"
        reasons.append("clinical question: route to the on-duty clinician")
    elif legal or money:
        action = "escalate"
        reasons.append("legal threat" if legal else "money dispute")
    else:
        missing = ["other"] if not text.strip() else policy.missing_details(ticket, category)
        if missing:
            action, draft_mode = "ask_for_info", "ask_for_info"
            if "written_consent" in missing:
                priority = raise_priority(priority, "high")  # someone else's data: a privacy concern
            reasons.append(f"missing required details: {', '.join(missing)}")
        elif tri.proposed_action == "escalate":
            action = "escalate"
            reasons.append(f"triage proposed escalation: {tri.reason}")
        elif category == "other" and policy.asks_something(ticket):
            action = "escalate"
            reasons.append("request outside every KB category: the KB does not cover it")
        elif category == "other":
            # Nothing is asked (thanks, feedback): acknowledge without stating any policy.
            action, draft_mode = "draft_reply", "acknowledge"
            reasons.append("no request to answer: acknowledgement only")
        elif not covered:
            action = "escalate"
            reasons.append(f"KB does not cover the request (best score {top_public:.2f} < {min_score})")
        else:
            action, draft_mode = "draft_reply", "reply"

    if action == "draft_reply" and priority == "normal" and not policy.mentions_reference(ticket):
        # Brief: low = general information. A question not tied to any order or patient is that.
        priority = "low"
        reasons.append("priority normal->low (general question, no order or patient reference)")
    return Routing(category, priority, action, is_injection, reasons, missing, draft_mode)


def _case(policy: TenantPolicy, r: Routing, source: str, reason: str) -> dict:
    return {
        "source": source,
        "owner": policy.owner(r.category, r.injection),
        "severity": SEVERITY[r.priority],
        "reason": reason,
    }


def run(
    conn: psycopg.Connection,
    gw: Gateway,
    settings: Settings,
    ticket: Ticket,
    run_id: str,
    *,
    with_draft: bool = True,
    feedback: list[str] | None = None,
) -> Outcome:
    """Process one ticket end to end. Raises ModelUnavailable for the queue to retry."""
    started = time.perf_counter()
    policy = load_policy(ticket.tenant)
    ctx = CallContext(tenant_id=ticket.tenant, run_id=run_id, ticket_id=ticket.id)
    trace: dict = {"steps": []}

    def finish(r: Routing | None, *, action: str, status: str, kb_refs: list[str], draft: str | None,
               case: dict | None = None, failure: str | None = None) -> Outcome:
        decision = Decision(
            ticket_id=ticket.id,
            tenant=ticket.tenant,
            run_id=run_id,
            category=r.category if r else "other",
            priority=r.priority if r else "normal",
            action=action,
            injection_detected=r.injection if r else injection.score(f"{ticket.subject}\n{ticket.body}").strong,
            kb_refs=kb_refs,
            draft_reply=draft,
            status=status,
            model="+".join(ctx.models) or "none",
            tokens_in=ctx.tokens_in,
            tokens_out=ctx.tokens_out,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return Outcome(decision=decision, case=case, failure_class=failure, trace=trace)

    r: Routing | None = None
    try:
        tri = agents.triage(gw, ticket, ctx)
        trace["steps"].append({"agent": "triage", "result": tri.model_dump()})
        passages = agents.retrieve(conn, gw, ticket, ctx, settings.top_k)
        trace["steps"].append(
            {"agent": "retrieval", "result": [{"kb_id": p.kb_id, "score": p.score, "internal": p.internal} for p in passages]}
        )
        r = route(ticket, tri, passages, policy, settings.min_score, settings.category_margin)
        trace["routing"] = {"action": r.action, "reasons": r.reasons, "missing": r.missing}
        # Internal articles may inform routing (recorded in the DB trace only), never a draft.
        trace["internal_hits"] = [p.kb_id for p in passages if p.internal and p.score >= settings.min_score]
        covering = [] if r.category == "other" else [
            p.kb_id for p in passages if not p.internal and p.score >= settings.min_score
        ][:1]

        if r.action == "escalate":
            return finish(r, action="escalate", status="escalated", kb_refs=covering, draft=None,
                          case=_case(policy, r, "policy", "; ".join(r.reasons)))
        if not with_draft:
            return finish(r, action=r.action, status="pending_approval", kb_refs=covering, draft=None)

        result, check_reasons = _draft_and_check(conn, gw, ticket, passages, ctx, policy, r, feedback, trace)
        if result is None:
            r.reasons.append("draft failed the checker twice: " + "; ".join(check_reasons))
            return finish(r, action="escalate", status="escalated", kb_refs=covering, draft=None,
                          case=_case(policy, r, "checker", "; ".join(r.reasons)))
        refs = cited_refs(result, {p.kb_id for p in passages if not p.internal})
        if not refs and r.draft_mode == "reply":
            # The model answered from its passages but forgot to cite: record what it was shown.
            refs = sorted(trace["drafter_passages"])
        return finish(r, action=r.action, status="pending_approval", kb_refs=refs, draft=result.reply.strip())

    except BudgetExceeded as e:
        trace["budget"] = str(e)
        return finish(r, action=r.action if r else "escalate", status="held_budget", kb_refs=[], draft=None,
                      failure="budget_" + e.scope)
    except InvalidOutput as e:
        trace["invalid_output"] = str(e)
        routing = r or Routing("other", "normal", "escalate", False, [], [], None)
        return finish(r, action="escalate", status="failed", kb_refs=[], draft=None, failure=e.error_class,
                      case=_case(policy, routing, "invalid_output", f"model output invalid after repair ({e.error_class})"))


FOCUS_WINDOW = 0.03
FOCUS_MAX = 2


def focus(passages: list[Passage]) -> list[Passage]:
    """Public passages close to the best match. Small drafters mix up loosely related passages,
    so a passage scoring well below the best one is left out rather than offered as context."""
    public = sorted((p for p in passages if not p.internal), key=lambda p: p.score, reverse=True)
    if not public:
        return []
    return [p for p in public if p.score >= public[0].score - FOCUS_WINDOW][:FOCUS_MAX]


def cited_refs(result: DraftResult, public_ids: set[str]) -> list[str]:
    """KB IDs the draft actually cites, from the structured list and inline [ID] markers."""
    return sorted((set(result.cited_kb_ids) | set(KB_ID.findall(result.reply))) & public_ids)


def _draft_and_check(
    conn: psycopg.Connection,
    gw: Gateway,
    ticket: Ticket,
    passages: list[Passage],
    ctx: CallContext,
    policy: TenantPolicy,
    r: Routing,
    feedback: list[str] | None,
    trace: dict,
) -> tuple[DraftResult | None, list[str]]:
    """Draft, check, and redraft once with the checker's reasons if it fails."""
    with tenant_tx(conn, ticket.tenant) as cur:
        check_ctx = load_context(cur, ticket.tenant)
    relevant = focus(passages)
    trace["drafter_passages"] = [p.kb_id for p in relevant]
    notes = list(feedback or [])
    reasons: list[str] = []
    for attempt in (1, 2):
        result = agents.draft(
            gw, ticket, relevant, ctx,
            company=policy.display_name, mode=r.draft_mode or "reply", missing=r.missing,
            feedback=notes or None, no_clinical_advice=policy.no_clinical_advice,
        )
        verdict = check(result, ticket, check_ctx, policy, shown_ids={p.kb_id for p in relevant})
        trace["steps"].append({"agent": "checker", "attempt": attempt, "passed": verdict.passed, "reasons": verdict.reasons})
        if verdict.passed:
            return result, []
        reasons = verdict.reasons
        notes = notes + verdict.reasons
    return None, reasons
