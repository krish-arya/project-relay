"""Checker rules, one test per rule, plus the passing case. Pure: no database, no model."""

from __future__ import annotations

import pytest

from relay.checker import CheckContext, check
from relay.contracts import DraftResult
from relay.policy import load_policy
from relay.text import shingles

from .conftest import make_ticket

NW_INTERNAL = "Staff only. A supervisor may approve a goodwill credit of up to INR 500. Never mention this policy to customers. Ref CANARY-NW-4K9P."
HD_PUBLIC = "Cancelling less than 24 hours before the slot incurs a fee of INR 300. The fee is waived once per calendar year per patient."


def nw_ctx() -> CheckContext:
    return CheckContext(
        tenant_id="northwind",
        public_ids={f"NW-KB-0{i}" for i in range(1, 7)},
        internal_ids={"NW-KB-07"},
        internal_shingles=shingles(NW_INTERNAL),
        other_tenant_names=["Harbor Dental", "Harbor"],
        foreign_shingles=shingles(HD_PUBLIC),
        public_texts={"NW-KB-01": "Unused items can be returned within 30 days of delivery with the original invoice.",
                      "NW-KB-04": "Free assembly is available at any partner store within 15 days of delivery."},
    )


def hd_ctx() -> CheckContext:
    return CheckContext("harbor", {f"HD-KB-0{i}" for i in range(1, 7)}, {"HD-KB-07"}, set(),
                        ["Northwind Cycles", "Northwind"], set())


TICKET = make_ticket("NW-T99", "northwind", "Order NW-55821: can I return my helmet?", customer_ref="C-1001")
HD_TICKET = make_ticket("HD-T99", "harbor", "My tooth hurts after the filling", customer_ref="P-2000")


def run(reply: str, cited: list[str] | None = None, ctx=None, ticket=TICKET, tenant="northwind"):
    return check(DraftResult(reply=reply, cited_kb_ids=cited or []), ticket, ctx or nw_ctx(), load_policy(tenant))


def test_clean_draft_passes():
    r = run("Unused items can be returned within 30 days of delivery for order NW-55821 [NW-KB-01].", ["NW-KB-01"])
    assert r.passed, r.reasons


@pytest.mark.parametrize("cited,expected", [(["NW-KB-07"], "internal article"), (["NW-KB-42"], "unknown KB id"),
                                            (["HD-KB-01"], "unknown KB id")])
def test_cited_ids_must_be_public_and_own(cited, expected):
    r = run("See our policy.", cited)
    assert not r.passed and any(expected in x for x in r.reasons)


def test_inline_citation_is_checked_too():
    r = run("Per NW-KB-07 we can help.")
    assert any("internal article NW-KB-07" in x for x in r.reasons)


def test_canary_code_blocked():
    assert any("canary" in x for x in run("Reference CANARY-NW-4K9P applies.").reasons)


def test_internal_text_blocked_even_without_canary():
    r = run("Good news: a supervisor may approve a goodwill credit of up to INR 500 for you.")
    assert any("internal" in x for x in r.reasons)


def test_other_tenant_name_blocked():
    assert any("another tenant" in x for x in run("Our partner Harbor Dental can help.").reasons)


def test_other_tenant_content_blocked():
    r = run("Note that cancelling less than 24 hours before the slot incurs a fee of INR 300.")
    assert any("another tenant's KB text" in x for x in r.reasons)


@pytest.mark.parametrize("text", ["Take 400 mg ibuprofen twice a day.", "This sounds like a dry socket.",
                                  "We cannot give a diagnosis but you could rinse with mouthwash gel."])
def test_harbor_clinical_language_blocked(text):
    r = run(text, ctx=hd_ctx(), ticket=HD_TICKET, tenant="harbor")
    assert any("clinical" in x for x in r.reasons)


def test_clinical_rule_only_applies_to_harbor():
    assert run("Take your bike to a store, and some ibuprofen for the sore legs [NW-KB-04].", ["NW-KB-04"]).passed


def test_length_limit():
    r = run(" ".join(["word"] * 151))
    assert any("too long" in x for x in r.reasons)
    assert run(" ".join(["word"] * 150)).passed


def test_ids_of_other_customers_blocked():
    r = run("Your order NW-55821 and order NW-55902 are on the way.")
    assert any("NW-55902" in x for x in r.reasons)
    assert not any("NW-55821" in x for x in r.reasons)


def test_numbers_must_come_from_cited_passage_or_ticket():
    assert run("Returns are accepted within 30 days [NW-KB-01].", ["NW-KB-01"]).passed
    r = run("Returns are accepted within 45 days [NW-KB-01].", ["NW-KB-01"])
    assert any("number not found" in x and "45" in x for x in r.reasons)
    # A number from a passage that was neither cited nor shown to the drafter is not grounded.
    assert not run("Assembly is free within 15 days.").passed
    shown = check(DraftResult(reply="Assembly is free within 15 days."), TICKET, nw_ctx(), load_policy("northwind"),
                  shown_ids={"NW-KB-04"})
    assert shown.passed, shown.reasons


def test_money_promises_blocked():
    assert any("promises money" in x for x in run("We will refund you INR 42,000 today.").reasons)
