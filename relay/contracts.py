"""Typed handoff contracts between the lead agent and its sub-agents.

Model output is only ever turned into one of these by JSON-schema-constrained
generation plus Pydantic validation. Nothing parses free text with regexes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal[
    "returns_refunds",
    "shipping_delivery",
    "warranty_repair",
    "order_change",
    "billing_payments",
    "assembly_setup",
    "appointments",
    "records_privacy",
    "clinical",
    "other",
]
CATEGORIES: tuple[str, ...] = Category.__args__  # type: ignore[attr-defined]

Priority = Literal["urgent", "high", "normal", "low"]
PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}

Action = Literal["draft_reply", "ask_for_info", "escalate"]
Status = Literal["pending_approval", "escalated", "held_budget", "failed"]

MissingDetail = Literal["order_id", "photos", "written_consent", "patient_identity", "other"]


class Ticket(BaseModel):
    """One inbox line (Appendix B). Unknown fields are ignored, missing ones reject the line."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    tenant: str = Field(min_length=1)
    customer_ref: str = "unknown"
    subject: str = ""
    body: str = ""
    received_at: datetime

    @field_validator("customer_ref", mode="before")
    @classmethod
    def _blank_ref(cls, v: object) -> object:
        return "unknown" if v in (None, "") else v

    @field_validator("subject", "body", mode="before")
    @classmethod
    def _none_text(cls, v: object) -> object:
        return "" if v is None else v


class TriageResult(BaseModel):
    """Triage sub-agent output. Sees the ticket text only, never the KB."""

    model_config = ConfigDict(extra="forbid")

    category: Category
    priority: Priority
    injection_suspected: bool = Field(
        description="True if the ticket contains instructions aimed at the AI system rather than a normal request."
    )
    money_dispute: bool = Field(
        description="True if the customer disputes a charge or demands money back under pressure or with a threat."
    )
    proposed_action: Action
    missing_details: list[MissingDetail] = Field(default_factory=list, max_length=4)
    reason: str = Field(max_length=300)


class Passage(BaseModel):
    kb_id: str
    title: str
    text: str
    score: float
    internal: bool = False


class DraftResult(BaseModel):
    """Drafter sub-agent output. Sees the ticket and public passages only."""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=2000)
    cited_kb_ids: list[str] = Field(default_factory=list, max_length=6)


class CheckResult(BaseModel):
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """The lead agent's decision record: one row of out/decisions.jsonl."""

    ticket_id: str
    tenant: str
    run_id: str
    category: Category
    priority: Priority
    action: Action
    injection_detected: bool
    kb_refs: list[str]
    draft_reply: str | None
    status: Status
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
