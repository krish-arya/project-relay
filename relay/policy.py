"""Business rules from the brief (Section 2), as deterministic code.

Tenant-specific facts (which details a request needs, what counts as clinical,
who owns an escalation) live in data/<tenant>/policy.json, so adding a tenant
is a data change. Nothing here knows about specific ticket IDs or golden labels.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache

from relay.config import ROOT
from relay.contracts import PRIORITY_RANK, Ticket
from relay.text import any_phrase, normalize, reference_ids

LEGAL_THREAT = [
    r"\bconsumer (court|forum|commission)\b",
    r"\b(legal|court) (action|notice|case|complaint)\b",
    r"\b(lawyer|advocate|attorney|solicitor)\b",
    r"\bsue\b|\bsuing\b|\blawsuit\b",
    r"\bfile (a )?(case|fir|complaint)\b",
    r"\bpolice complaint\b",
]

MONEY_DISPUTE = [
    r"\bover ?charged\b",
    r"\bcharged\b.{0,40}\bbut\b.{0,30}\b(quoted|told|promised)\b",
    r"\bdouble charged\b|\bcharged twice\b",
    r"\bchargeback\b",
    r"\bdispute\b",
    r"\brefund\b.{0,40}\b(promised|still not|not (yet )?(received|arrived)|nothing has arrived|pending for)\b",
    r"\b(want|need|demand) (my|a full) (money|refund)\b.{0,20}\b(now|immediately|back)\b",
]


SAFETY_RISK = [
    r"\b(accident|injur(y|ed|ies)|hurt|fell off|crashed into|hospital|unsafe|dangerous|caught fire|smoke)\b",
    r"\bbrakes? (failed|fail|not working|stopped working)\b",
]

REQUEST = [
    r"\b(please|kindly|can you|could you|can i|can we|i want|we want|i need|we need|help me|let me know)\b",
    r"\b(kab|kya|kaise|kyun|chahiye)\b",
]


@dataclass
class TenantPolicy:
    tenant: str
    display_name: str
    required_details: dict[str, list[str]] = field(default_factory=dict)
    order_id_pattern: str | None = None
    subject_id_pattern: str | None = None
    customer_ref_pattern: str | None = None
    third_party_categories: list[str] = field(default_factory=list)
    clinical: dict | None = None
    no_clinical_advice: bool = False
    owners: dict[str, str] = field(default_factory=dict)
    security_owner: str = "security-oncall"
    # Which category each KB article answers. Lets retrieval act as a second classifier.
    kb_categories: dict[str, str] = field(default_factory=dict)
    ask_together: dict[str, list[str]] = field(default_factory=dict)

    # -- facts about a ticket ------------------------------------------------

    def legal_threat(self, ticket: Ticket) -> list[str]:
        return any_phrase(f"{ticket.subject} {ticket.body}", LEGAL_THREAT)

    def money_dispute(self, ticket: Ticket) -> list[str]:
        return any_phrase(f"{ticket.subject} {ticket.body}", MONEY_DISPUTE)

    def safety_risk(self, ticket: Ticket) -> list[str]:
        return any_phrase(f"{ticket.subject} {ticket.body}", SAFETY_RISK)

    def asks_something(self, ticket: Ticket) -> bool:
        """A question or a request, as opposed to thanks or feedback."""
        text = f"{ticket.subject} {ticket.body}"
        return "?" in text or bool(any_phrase(text, REQUEST))

    def mentions_reference(self, ticket: Ticket) -> bool:
        return bool(reference_ids(f"{ticket.subject} {ticket.body}"))

    def clinical_signals(self, ticket: Ticket) -> tuple[list[str], bool]:
        """Clinical terms present, and whether any severity marker is present."""
        if not self.clinical:
            return [], False
        text = f"{ticket.subject} {ticket.body}"
        terms = any_phrase(text, self.clinical["terms"])
        severe = bool(terms) and bool(any_phrase(text, self.clinical.get("severe_terms", [])))
        return terms, severe

    def third_party_subject(self, ticket: Ticket) -> bool:
        """True if the ticket names a patient/customer ID that is not the sender's own."""
        if not (self.subject_id_pattern and self.customer_ref_pattern):
            return False
        mentioned = set(re.findall(self.subject_id_pattern, ticket.body + " " + ticket.subject))
        if not mentioned:
            return False
        own = re.match(self.customer_ref_pattern, ticket.customer_ref)
        if own is None:
            return True
        return any(m != own.group(1) for m in mentioned)

    def missing_details(self, ticket: Ticket, category: str) -> list[str]:
        missing: list[str] = []
        text = f"{ticket.subject} {ticket.body}"
        for detail in self.required_details.get(category, []):
            if detail == "order_id" and self.order_id_pattern and not re.search(self.order_id_pattern, text):
                missing.append("order_id")
            elif detail == "patient_identity" and ticket.customer_ref.lower() in ("", "unknown"):
                missing.append("patient_identity")
        if category in self.third_party_categories and self.third_party_subject(ticket):
            missing.append("written_consent")
        if missing:
            # Details we cannot verify from text (tickets carry no attachments) are requested
            # alongside, so the customer is asked once, not twice.
            missing += [d for d in self.ask_together.get(category, []) if d not in missing]
        return missing

    def owner(self, category: str, injection: bool) -> str:
        if injection:
            return self.security_owner
        return self.owners.get(category, self.owners.get("default", "support-lead"))


def raise_priority(current: str, floor: str) -> str:
    return floor if PRIORITY_RANK[floor] > PRIORITY_RANK[current] else current


SEVERITY = {"urgent": "sev1", "high": "sev2", "normal": "sev3", "low": "sev4"}


@lru_cache(maxsize=None)
def load_policy(tenant: str) -> TenantPolicy:
    path = ROOT / "data" / tenant / "policy.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return TenantPolicy(**data)


def mentions_normalized(text: str, needle: str) -> bool:
    return re.search(rf"\b{re.escape(needle.lower())}\b", normalize(text)) is not None
