"""Phase 0 gate — card declines above cap (SPEC §16, §10; DESIGN §7.1 row 4, §4f).

The cap lives in the card issuer (``FakeCardIssuer`` models the real card: declines when
``month_total + amount > cap`` or when frozen); ``Ledger.record_spend`` is unreachable without a
``CardAuthorization``. Operator holder cap: 3,000 AED from ``config/spend_tiers.yaml``.

Spends are driven through the Operator desk: a scripted ``ledger.spend`` tool call is enqueued on
the PRIMARY model's FIFO and an Operator-routed event (a phone notification) makes the desk plan
on it; K outcomes are approved with the passphrase (DESIGN §7.1 "K items approved with the
passphrase").
"""

from __future__ import annotations

import pytest

pytest.importorskip("nour.testing.harness")

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

# Waves 1-6 modules: ruff classifies an import by its on-disk path, so their isort section
# flips from third- to first-party as each wave lands; hand-sorted in the final order instead.
# isort: off
from nour.core.ports import ModelResponse, ModelRole, ModelUsage
from nour.core.types import (
    ActionStatus,
    BudgetHolder,
    CoatId,
    Desk,
    FreezeScope,
    IncidentType,
    Money,
)
from nour.db.models import IncidentRow, TransactionRow
from nour.fakes.policies import SpendBurstPolicy, tool_call
from nour.governance.owner_channel import OwnerMessageKind
from nour.records.ledger import Ledger
from nour.testing.harness import Harness
# isort: on

OPERATOR = BudgetHolder("operator")
BUZZ = CoatId("buzz-avenue")
MERCHANT = "Noon"
SPENT_STATUSES = {"authorized", "released", "settled", "reconciled"}


# --------------------------------------------------------------------------- driving a spend


def _spend_response(amount_aed: int) -> ModelResponse:
    # ASSUMPTION: ``ledger.spend``'s args_model needs nothing beyond the §3.4 ModelToolCall contract
    # fields (``coat``, ``counterpart``, ``amount_aed``, ``reason``); the merchant is the counterpart.
    return ModelResponse(
        text=None,
        tool_calls=[
            tool_call(
                "ledger.spend",
                reason=f"Pay {MERCHANT} AED {amount_aed} for the signage order.",
                coat=str(BUZZ),
                counterpart=MERCHANT,
                amount_aed=amount_aed,
            )
        ],
        vendor="fake-a",
        model="scripted",
        usage=ModelUsage(input_tokens=0, output_tokens=0, cost_fils=0),
    )


def _spend(h: Harness, amount_aed: int, *, approve: bool = True) -> Any:
    """Make the Operator desk propose ``ledger.spend(amount)`` and return the final ActionOutcome.

    When the resolver queues it (band K, or an unknown counterpart: rule 5), the owner approves it
    with the passphrase; the DESIGN's "150 (A)" is A only once the merchant is known to the CRM, and
    the decline assertions are identical either way.
    """
    h.fakes.models[ModelRole.PRIMARY].enqueue(_spend_response(amount_aed))
    event_id = h.phone_notification(
        "com.noon.buyer", "Order ready", f"Signage order: AED {amount_aed} due at checkout"
    )
    results = [r for r in h.drain() if r.event_id == event_id]
    assert results and results[-1].outcomes, f"no outcome for the spend of {amount_aed}"
    outcome = results[-1].outcomes[-1]
    if approve and outcome.status == ActionStatus.QUEUED:
        assert outcome.approval_id is not None
        released = h.approve(outcome.approval_id, passphrase=h.PASSPHRASE)
        assert released is not None
        outcome = released
    return outcome


def _card(h: Harness) -> str:
    return h.fakes.card.card_for(OPERATOR)


def _spent_month_fils(rows: Any, h: Harness) -> int:
    # ASSUMPTION: the harness exposes no Ledger handle, so ``Ledger.spent_month(operator)`` is
    # recomputed the way the ledger does it: money-out transaction rows of the holder whose card
    # authorisation went through (status authorized → settled → reconciled), in fils.
    return sum(
        t.amount
        for t in rows(h, TransactionRow, holder=OPERATOR, direction="out")
        if t.status in SPENT_STATUSES
    )


# --------------------------------------------------------------------------- the gate row


@pytest.mark.gate
def test_card_declines_above_cap_and_cap_beats_approval(harness: Any, rows: Any) -> None:
    h = harness()
    cap = h.cfg.spend_tiers.monthly_cap[OPERATOR]
    assert cap == Money.aed(3000)
    card = _card(h)
    today = h.clock.today_dubai()

    first = _spend(h, 1500)
    second = _spend(h, 1400)
    assert first.status == ActionStatus.EXECUTED, first
    assert second.status == ActionStatus.EXECUTED, second
    assert h.fakes.card.month_total(card, today) == Money.aed(2900)
    owner_messages_before = len(h.owner_messages())
    auths_before = len(h.fakes.card.auths)

    third = _spend(h, 150)

    assert third.status == ActionStatus.DECLINED, third
    assert len(h.fakes.card.auths) == auths_before + 1
    assert h.fakes.card.auths[-1].approved is False
    declined = [t for t in rows(h, TransactionRow, holder=OPERATOR) if t.status == "declined"]
    assert declined and declined[-1].amount == Money.aed(150).fils
    closed = h.audit_rows(status=ActionStatus.DECLINED, phase="closed")
    assert closed and closed[-1].amount == Money.aed(150).fils
    # ASSUMPTION: the decline reason the issuer returned rides on the outcome's ``detail`` (the
    # closed row only hashes its output object).
    assert third.detail
    assert len(h.owner_messages()) > owner_messages_before  # the owner is told
    assert h.fakes.card.month_total(card, today) == Money.aed(2900)

    # Cap beats approval: a further 500, approved with the passphrase, is still DECLINED.
    fourth = _spend(h, 500, approve=True)
    assert fourth.status == ActionStatus.DECLINED, fourth
    assert h.fakes.card.auths[-1].approved is False
    assert h.fakes.card.month_total(card, today) == Money.aed(2900)
    assert _spent_month_fils(rows, h) == Money.aed(2900).fils == 290_000
    assert h.reader.verify_chain()


# --------------------------------------------------------------------------- invariant


@pytest.mark.gate
@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(amounts=st.lists(st.integers(min_value=1, max_value=2000), min_size=1, max_size=6))
def test_cap_invariant(tmp_path_factory: pytest.TempPathFactory, amounts: list[int]) -> None:
    """For every sequence of spends, ``FakeCardIssuer.month_total(card) <= cap`` always holds."""
    h = Harness.build(tmp_path_factory.mktemp("cap"))
    try:
        cap = h.cfg.spend_tiers.monthly_cap[OPERATOR]
        card = _card(h)
        today = h.clock.today_dubai()
        for amount in amounts:
            outcome = _spend(h, amount)
            assert outcome.status in {
                ActionStatus.EXECUTED,
                ActionStatus.DECLINED,
                ActionStatus.FROZEN,
                ActionStatus.REFUSED,
            }, outcome
            assert h.fakes.card.month_total(card, today) <= cap
        assert h.fakes.card.month_total(card, today) <= cap
        assert all(a.amount <= cap for a in h.fakes.card.auths)
    finally:
        h.close()


# --------------------------------------------------------------------------- watchdog


@pytest.mark.gate
def test_burst_freeze(harness: Any, rows: Any) -> None:
    """``SpendBurstPolicy``: once spent_today > 3 × (3,000 / 30) the watchdog freezes ALL_OUTBOUND
    and opens WATCHDOG_SPEND."""
    h = harness(policy=SpendBurstPolicy())
    cap = h.cfg.spend_tiers.monthly_cap[OPERATOR]
    multiple = h.cfg.spend_tiers.watchdog.daily_spend_multiple_freeze
    threshold = Money(fils=cap.fils // 30 * multiple, currency=cap.currency)
    assert threshold == Money.aed(300)
    card = _card(h)
    today = h.clock.today_dubai()

    # ASSUMPTION: the policy's merchant is known to the Operator CRM (seeded by the harness /
    # default_fakes) so its small spends are A-tier and execute; a K-queued spend would never
    # reach the watchdog. Twelve Operator events within one hour is "many small spends".
    for i in range(12):
        h.phone_notification("com.noon.buyer", "Deal", f"Flash deal {i}: small item on sale")
        h.advance(timedelta(minutes=5))
        if FreezeScope.ALL_OUTBOUND in h.kill.state():
            break

    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    assert h.fakes.card.month_total(card, today) > threshold
    assert rows(h, IncidentRow, type=IncidentType.WATCHDOG_SPEND)
    assert h.fakes.card.frozen and all(h.fakes.card.frozen.values())
    assert h.fakes.card.month_total(card, today) <= cap


# --------------------------------------------------------------------------- no other money-out path


@pytest.mark.gate
def test_no_spend_without_authorization(harness: Any, rows: Any) -> None:
    """``Ledger.record_spend`` with a hand-built authorisation fails pydantic validation."""
    h = harness()
    ledger = Ledger(h.desks[Desk.OPERATOR].sf, h.clock, h.idgen)
    transactions_before = len(rows(h, TransactionRow))
    hand_built = SimpleNamespace(
        auth_ref="hand-built-auth",
        card_ref=_card(h),
        holder=OPERATOR,
        amount=Money.aed(50),
        merchant=MERCHANT,
        at=h.clock.now(),
    )
    # ASSUMPTION: ``record_spend`` validates its arguments with pydantic (``validate_call`` or an
    # explicit ``CardAuthorization.model_validate``), so a duck-typed object is a ValidationError.
    with pytest.raises(ValidationError):
        ledger.record_spend(
            hand_built,  # type: ignore[arg-type]
            coat_id=BUZZ,
            counterpart_ref=MERCHANT,
            approval_id=None,
            experiment_id=None,
            task_id=None,
            audit_id=h.idgen.new(),
        )
    with pytest.raises(ValidationError):
        ledger.record_spend(
            {"auth_ref": "x", "card_ref": _card(h), "holder": OPERATOR, "amount": 5000},  # type: ignore[arg-type]
            coat_id=BUZZ,
            counterpart_ref=MERCHANT,
            approval_id=None,
            experiment_id=None,
            task_id=None,
            audit_id=h.idgen.new(),
        )
    assert len(rows(h, TransactionRow)) == transactions_before
    assert h.fakes.card.auths == []
    assert h.owner_messages(OwnerMessageKind.NOTIFY) == []
