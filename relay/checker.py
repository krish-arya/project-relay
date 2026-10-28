"""Deterministic draft checker. No model. Every rule returns a reason string on failure.

Inputs are gathered by `load_context` inside the ticket's tenant transaction:
its own KB (public and internal), other tenants' names, and *hashes* of other
tenants' KB text (via relay_foreign_fingerprints, which never returns text).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import psycopg

from relay.contracts import CheckResult, DraftResult, Ticket
from relay.policy import TenantPolicy
from relay.text import CANARY, KB_ID, normalize, reference_ids, shingles, word_count

MAX_WORDS = 150
# Overlapping 5-word shingles needed to call text "copied". One shared 5-word
# phrase can be coincidence; two is not.
SHINGLE_HITS = 2
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

CLINICAL_ADVICE = [
    r"\bdiagnos(is|es|e|ed)\b",
    r"\bdos(e|es|age)\b",
    r"\b\d+(\.\d+)?\s?(mg|ml|mcg|tablets?|capsules?|pills?)\b",
    r"\b(ibuprofen|paracetamol|acetaminophen|aspirin|amoxicillin|metronidazole|antibiotics?|painkillers?|analgesics?|chlorhexidine)\b",
    r"\b(take|apply|use|rinse with)\b.{0,30}\b(medicine|medication|tablet|mouthwash|gel|ointment)\b",
    r"\byou (may|might|probably|likely)? ?have (an? )?(infection|abscess|gingivitis|periodontitis|cavity|dry socket)\b",
    r"\b(gingivitis|periodontitis|abscess|dry socket)\b",
]

MONEY_PROMISE = [
    r"\b(we|i) (will|shall|have|'ll|ve) (refund|credit|reimburse|compensate)\b",
    r"\brefund (has been|is|was) (approved|processed|issued|initiated)\b",
    r"\bgoodwill\b",
    r"\bcompensation\b",
    r"\bwaive[sd]? (the|your) fee\b.{0,20}\b(for you|now|today)\b",
]

INTERNAL_MARKERS = [r"\bstaff[- ]only\b", r"\binternal (note|policy|article|guidance)\b", r"\bsupervisor may approve\b"]


@dataclass
class CheckContext:
    tenant_id: str
    public_ids: set[str]
    internal_ids: set[str]
    internal_shingles: set[str]
    other_tenant_names: list[str]
    foreign_shingles: set[str]
    public_texts: dict[str, str] = field(default_factory=dict)


def load_context(cur: psycopg.Cursor, tenant_id: str) -> CheckContext:
    """Must be called inside tenant_tx(conn, tenant_id)."""
    cur.execute("SELECT id, body, internal FROM kb_articles")
    rows = cur.fetchall()
    cur.execute("SELECT name, aliases FROM tenants WHERE id <> %s", (tenant_id,))
    names: list[str] = []
    for r in cur.fetchall():
        names.append(r["name"])
        names.extend(r["aliases"])
    cur.execute("SELECT relay_foreign_fingerprints() AS h")
    foreign = {r["h"] for r in cur.fetchall()}
    internal_sh: set[str] = set()
    for r in rows:
        if r["internal"]:
            internal_sh |= shingles(r["body"])
    return CheckContext(
        tenant_id=tenant_id,
        public_ids={r["id"] for r in rows if not r["internal"]},
        internal_ids={r["id"] for r in rows if r["internal"]},
        internal_shingles=internal_sh,
        other_tenant_names=names,
        foreign_shingles=foreign,
        public_texts={r["id"]: r["body"] for r in rows if not r["internal"]},
    )


def numbers(text: str) -> set[str]:
    """Numbers in text, ignoring KB IDs; '42,000' and '42000' compare equal."""
    return {n.replace(",", "") for n in NUMBER.findall(KB_ID.sub(" ", text))}


def check(draft: DraftResult, ticket: Ticket, ctx: CheckContext, policy: TenantPolicy,
          shown_ids: set[str] | None = None) -> CheckResult:
    """shown_ids: passages the drafter was given; their numbers count as grounded even if uncited."""
    text = draft.reply
    norm = normalize(text)
    reasons: list[str] = []

    # 1. Every cited ID exists in this tenant's KB and is not internal.
    cited = set(draft.cited_kb_ids) | set(KB_ID.findall(text))
    for kb_id in sorted(cited):
        if kb_id in ctx.internal_ids:
            reasons.append(f"cites internal article {kb_id}")
        elif kb_id not in ctx.public_ids:
            reasons.append(f"cites unknown KB id {kb_id}")

    # 2. No canary code, no internal text.
    if CANARY.search(text):
        reasons.append("contains a canary reference code")
    if len(shingles(text) & ctx.internal_shingles) >= SHINGLE_HITS:
        reasons.append("reproduces internal article text")
    for pat in INTERNAL_MARKERS:
        if re.search(pat, norm):
            reasons.append(f"mentions internal material ({pat})")

    # 3. No other tenant's name or content.
    for name in ctx.other_tenant_names:
        if re.search(rf"\b{re.escape(name.lower())}\b", norm):
            reasons.append(f"mentions another tenant ({name})")
    if len(shingles(text) & ctx.foreign_shingles) >= SHINGLE_HITS:
        reasons.append("reproduces another tenant's KB text")

    # 4. No medication or diagnosis language where the tenant forbids it.
    if policy.no_clinical_advice:
        for pat in CLINICAL_ADVICE:
            m = re.search(pat, norm)
            if m:
                reasons.append(f"clinical/medication language: '{m.group(0)}'")

    # 5. Length.
    n = word_count(text)
    if n > MAX_WORDS:
        reasons.append(f"too long: {n} words (max {MAX_WORDS})")

    # 6. No IDs belonging to other customers or patients: every reference ID in
    #    the draft must already appear in this ticket.
    own = reference_ids(f"{ticket.subject} {ticket.body} {ticket.customer_ref}")
    for ref in sorted(reference_ids(text) - own - set(KB_ID.findall(text))):
        reasons.append(f"mentions an ID not in this ticket: {ref}")

    # 7. Every number (days, amounts, hours) must come from a passage the drafter cited or was
    #    shown, or from the ticket itself. Small drafters most often go wrong by inventing figures.
    grounded = numbers(f"{ticket.subject} {ticket.body} {ticket.customer_ref}")
    for kb_id in (cited | (shown_ids or set())) & ctx.public_ids:
        grounded |= numbers(ctx.public_texts.get(kb_id, ""))
    for n in sorted(numbers(text) - grounded, key=lambda x: (len(x), x)):
        reasons.append(f"number not found in cited passages or ticket: {n}")

    # 8. No promises of money.
    for pat in MONEY_PROMISE:
        m = re.search(pat, norm)
        if m:
            reasons.append(f"promises money or an outcome: '{m.group(0)}'")

    return CheckResult(passed=not reasons, reasons=reasons)
