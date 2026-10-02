"""SPEC §18 scenario: spend at each band boundary (DESIGN §7.2 row 5, §4f; SPEC §10 spend tiers).

`ledger.spend` at 200, 201, 1,000 and 1,001 AED, 50 AED to an unknown counterpart and 50 AED with
category `customer_refund` → A, N, N, K, K (`new_counterpart`), K (`always_K`); log entries
EXECUTED, NOTIFIED, NOTIFIED, QUEUED, QUEUED, QUEUED with the amounts and `rules_hit` naming the
band; owner messages: none for A, notify within the hour for the two N spends (`notify_sweep`
delivers them), an approval item for each K.

Spends happen on the Operator desk (`spend_tiers.monthly_cap.operator` = 3,000; the Assistant's
logistics holder has no card in phase 0), so each is triggered by a phone notification routed to
the Operator and the proposal is pinned on the scripted model.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("nour.testing.harness")

# ruff sorts not-yet-built nour.* packages as third-party; the final order is kept instead.
# isort: off
from nour.core.types import ActionStatus, ActionTier, Money, Ulid
from nour.governance.owner_channel import OwnerMessageKind
from tests.scenarios.conftest import (
    assert_day_integrity,
    closed_row,
    enqueue_model,
    expect_triple,
    message_ids,
    outcome_for,
    seed_operator_contact,
    spend_call,
)
# isort: on

SUPPLIER = "Aramex"
SUPPLIER_ADDRESS = "billing@aramex.example"
CUSTOMER = "Al Noor Hotel"
CUSTOMER_ADDRESS = "reception@alnoorhotel.example"
UNKNOWN = "Desert Rose Trading"


def _spend(h, amount: int, counterpart: str, *, ref: str, category: str | None = None):
    """A phone notification (→ Operator) whose pinned answer is one `ledger.spend` proposal."""
    # The notification text states a fact and gives no order, so the injection scanner stays quiet
    # and rule 7 (found_instructions → K) cannot blur the band under test.
    text = f"{counterpart} invoice {ref}: AED {amount:,}.00, due 15 October"
    enqueue_model(
        h,
        spend_call(
            amount,
            counterpart,
            reason=f"Invoice {ref} from {counterpart} is due and within the card's budget.",
            category=category,
        ),
    )
    event_id = h.phone_notification("Banking", f"Invoice {ref}", text)
    results = h.drain()
    return event_id, outcome_for(h, results, event_id, "ledger.spend")


def _assert_amount(h, event_id: Ulid, amount: int) -> None:
    row = closed_row(h, event_id, "ledger.spend")
    assert row.amount == Money.aed(amount).fils and row.currency == "AED"


def _assert_band_named(outcome) -> None:
    # ASSUMPTION: TierResolver rule 4 appends an entry spelled like `spend_band:<tier>`; §7.2
    # only says "`rules_hit` names the band".
    assert any("band" in rule.lower() for rule in outcome.decision.rules_hit), (
        outcome.decision.rules_hit
    )


def _assert_notified_within_the_hour(h, ids: set[Ulid]) -> None:
    within = timedelta(minutes=h.cfg.calendar.daily_rhythm.notify_within_minutes)
    rows = {m.id: m for m in h.owner_messages(OwnerMessageKind.NOTIFY)}
    for message_id in ids:
        row = rows[message_id]
        assert row.sent_at is not None, f"notification {message_id} was never delivered"
        assert row.sent_at - row.created_at <= within


@pytest.mark.weekly
def test_spend_at_each_band_boundary(harness) -> None:
    h = harness()
    seed_operator_contact(h, name=SUPPLIER, address=SUPPLIER_ADDRESS)
    seed_operator_contact(h, name=CUSTOMER, address=CUSTOMER_ADDRESS)
    days = [h.clock.today_dubai()]
    notify_before = message_ids(h, OwnerMessageKind.NOTIFY)
    approvals_before = message_ids(h, OwnerMessageKind.APPROVAL)

    # ASSUMPTION (watchdog): Watchdog.observe freezes at daily_spend_multiple_freeze ×
    # monthly_cap/30 = 3 × 100 AED (DESIGN §3.17; §10 lists the formula as an open question).
    # Read literally, 200 + 201 + 1,000 on one day would trip it after the second spend, so the
    # executed spends are spread over three days with the only spend above 300 on its own
    # (1,000) last: a freeze, if in-band spends count, can only land after the final assertion.

    # Day 1 — the three K items first (no money moves), then the 200 AED A spend.
    e1001, o1001 = _spend(h, 1001, SUPPLIER, ref="7784")
    expect_triple(
        h,
        e1001,
        tier=ActionTier.K,
        status=ActionStatus.QUEUED,
        tool="ledger.spend",
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.APPROVAL,
    )
    _assert_band_named(o1001)
    _assert_amount(h, e1001, 1001)

    e_unknown, _ = _spend(h, 50, UNKNOWN, ref="U-1")
    expect_triple(
        h,
        e_unknown,
        tier=ActionTier.K,
        status=ActionStatus.QUEUED,
        tool="ledger.spend",
        rules_hit=("new_counterpart",),
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.APPROVAL,
    )
    _assert_amount(h, e_unknown, 50)

    e_refund, _ = _spend(h, 50, CUSTOMER, ref="R-1", category="customer_refund")
    expect_triple(
        h,
        e_refund,
        tier=ActionTier.K,
        status=ActionStatus.QUEUED,
        tool="ledger.spend",
        rules_hit=("always_K",),
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.APPROVAL,
    )
    _assert_amount(h, e_refund, 50)
    assert h.fakes.card.auths == []  # K never reaches the card

    e200, o200 = _spend(h, 200, SUPPLIER, ref="7781")
    expect_triple(
        h,
        e200,
        tier=ActionTier.A,
        status=ActionStatus.EXECUTED,
        tool="ledger.spend",
        owner_message_contains=None,
    )
    _assert_band_named(o200)
    _assert_amount(h, e200, 200)
    assert message_ids(h, OwnerMessageKind.NOTIFY) == notify_before  # A: act and log, no message

    # Day 2 — 201 AED: the bottom of the notify band.
    h.advance(timedelta(days=1))
    days.append(h.clock.today_dubai())
    e201, o201 = _spend(h, 201, SUPPLIER, ref="7782")
    expect_triple(
        h,
        e201,
        tier=ActionTier.N,
        status=ActionStatus.NOTIFIED,
        tool="ledger.spend",
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.NOTIFY,
    )
    _assert_band_named(o201)
    _assert_amount(h, e201, 201)
    notify_201 = message_ids(h, OwnerMessageKind.NOTIFY) - notify_before
    assert len(notify_201) == 1
    h.advance(timedelta(hours=1))
    _assert_notified_within_the_hour(h, notify_201)

    # Day 3 — 1,000 AED: the top of the notify band.
    h.advance(timedelta(hours=23))
    days.append(h.clock.today_dubai())
    e1000, o1000 = _spend(h, 1000, SUPPLIER, ref="7783")
    expect_triple(
        h,
        e1000,
        tier=ActionTier.N,
        status=ActionStatus.NOTIFIED,
        tool="ledger.spend",
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.NOTIFY,
    )
    _assert_band_named(o1000)
    _assert_amount(h, e1000, 1000)
    notify_1000 = message_ids(h, OwnerMessageKind.NOTIFY) - notify_before - notify_201
    assert len(notify_1000) == 1
    h.advance(timedelta(hours=1))
    _assert_notified_within_the_hour(h, notify_1000)

    # Totals: notify ×2, approval ×3, three card authorisations (the executed spends only).
    assert len(message_ids(h, OwnerMessageKind.NOTIFY) - notify_before) == 2
    assert len(message_ids(h, OwnerMessageKind.APPROVAL) - approvals_before) == 3
    pending = [a for a in h.pending_approvals() if a.action.proposal.tool == "ledger.spend"]
    assert len(pending) == 3
    assert len(h.fakes.card.auths) == 3 and all(a.approved for a in h.fakes.card.auths)

    for day in days:
        assert_day_integrity(h, day)
