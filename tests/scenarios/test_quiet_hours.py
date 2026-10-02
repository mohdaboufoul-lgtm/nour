"""SPEC §18 scenario: a message arriving in quiet hours that is not an emergency (DESIGN §7.2
row 6, §2.6; SPEC §9 outreach window, §12 quiet hours and emergencies).

Three separate rules, never conflated (DESIGN §2.6): the clock is 23:10 Dubai.
(a) a customer FAQ on the Buzz line → `reply_whatsapp` in a new category: K
(`default_new_category`) and additionally `deferred_until` 09:00 (`outreach_window`) → QUEUED with
both rules in `rules_hit`;
(b) a non-emergency `owner.notify`: A → DEFERRED, parked until 07:00;
(c) `owner.notify` with the emergency category `payment_failure`: A → NOTIFIED immediately.
Nothing else reaches the owner before 07:00; advancing to 07:05 delivers exactly the parked
messages.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nour.testing.harness")

# ruff sorts not-yet-built nour.* packages as third-party; the final order is kept instead.
# isort: off
from nour.core.types import ActionStatus, ActionTier
from nour.governance.owner_channel import OwnerMessageKind
from tests.scenarios.conftest import (
    BUZZ,
    advance_to,
    assert_day_integrity,
    call,
    dubai,
    enqueue_model,
    expect_triple,
    local,
    outcome_for,
    owner_sends,
    parked_messages,
)
# isort: on

START = dubai(2026, 10, 5, 23, 10)  # Monday, inside quiet hours (22:00–07:00)
QUIET_END = dubai(2026, 10, 6, 7, 0)
OUTREACH_OPEN = dubai(2026, 10, 6, 9, 0)
CHECK_AT = dubai(2026, 10, 6, 7, 5)

CUSTOMER = "+971559876543"
FAQ = "السلام عليكم، شو أوقات الدوام عندكم؟"  # "Peace be upon you, what are your working hours?"
FAQ_REPLY = "وعليكم السلام، دوامنا من ٩ صباحاً حتى ٦ مساءً من الاثنين إلى الجمعة."
DELAY_TEXT = "Aramex: shipment 7781 for Buzz Avenue now arrives tomorrow morning instead of today."
FAILURE_TEXT = "Card payment of AED 850.00 to DEWA was declined by the issuer."


@pytest.mark.weekly
def test_quiet_hours_keep_the_three_rules_apart(harness) -> None:
    h = harness(start=START)
    first_day = h.clock.today_dubai()

    # (a) A customer FAQ in a new category: K by graduated autonomy AND deferred by the window.
    # ASSUMPTION: reply_whatsapp takes the draft as `text` and the recipient as `counterpart`.
    enqueue_model(
        h,
        call(
            "reply_whatsapp",
            reason="A customer asks for our working hours.",
            coat=str(BUZZ),
            counterpart=CUSTOMER,
            text=FAQ_REPLY,
        ),
    )
    a_id = h.stranger_whatsapp(CUSTOMER, FAQ)
    results = h.drain()
    expect_triple(
        h,
        a_id,
        tier=ActionTier.K,
        status=ActionStatus.QUEUED,
        tool="reply_whatsapp",
        rules_hit=("default_new_category", "outreach_window"),
        owner_message_contains=None,
    )
    decision = outcome_for(h, results, a_id, "reply_whatsapp").decision
    assert decision.deferred_until is not None
    assert local(decision.deferred_until) == OUTREACH_OPEN
    assert [m for m in h.fakes.whatsapp.sent if m.to == CUSTOMER] == []

    # (b) A non-emergency owner.notify: executed at A but parked until 07:00.
    # ASSUMPTION: §7.2 says "from a timer", but the Harness cannot inject an ad-hoc timer and
    # TimerHandlers bypass the model; a phone notification on the Operator desk is the closest
    # trigger and exercises the same OwnerChannel rule. owner.notify's args are read as `text`
    # plus `category` (the emergency categories of calendar.yaml are ActionCategory names).
    enqueue_model(
        h,
        call(
            "owner.notify",
            reason="A shipment the owner is expecting slipped to tomorrow.",
            coat=str(BUZZ),
            text=DELAY_TEXT,
            category="supplier_inquiry",
        ),
    )
    b_id = h.phone_notification("Aramex", "Delivery update", DELAY_TEXT)
    h.drain()
    expect_triple(
        h,
        b_id,
        tier=ActionTier.A,
        status=ActionStatus.DEFERRED,
        tool="owner.notify",
        owner_message_contains=None,
    )
    parked_notify = [m for m in parked_messages(h) if m.kind == OwnerMessageKind.NOTIFY]
    assert len(parked_notify) == 1
    assert local(parked_notify[0].parked_until) == QUIET_END
    assert parked_notify[0].emergency is False

    # (c) An emergency (payment_failure) goes through quiet hours immediately.
    enqueue_model(
        h,
        call(
            "owner.notify",
            reason="A card payment to DEWA failed.",
            coat=str(BUZZ),
            text=FAILURE_TEXT,
            category="payment_failure",
        ),
    )
    c_id = h.phone_notification("Bank", "Payment declined", FAILURE_TEXT)
    h.drain()
    expect_triple(
        h,
        c_id,
        tier=ActionTier.A,
        status=ActionStatus.NOTIFIED,
        tool="owner.notify",
        owner_message_contains="DEWA",
        owner_message_kind=OwnerMessageKind.NOTIFY,
    )
    sent_in_quiet = [m for m in h.owner_messages() if m.sent_at is not None]
    assert len(sent_in_quiet) == 1 and sent_in_quiet[0].emergency is True
    assert "DEWA" in (sent_in_quiet[0].text or "")
    assert len(owner_sends(h)) == 1 and "DEWA" in (owner_sends(h)[0].text or "")

    # (a) + (b): nothing before 07:00; the approval request for (a) is parked like (b).
    parked_ids = {m.id for m in parked_messages(h)}
    assert parked_ids >= {parked_notify[0].id}
    advance_to(h, CHECK_AT)
    rows = h.owner_messages()
    quiet_window = [
        m for m in rows if m.sent_at is not None and START <= local(m.sent_at) < QUIET_END
    ]
    assert [m.id for m in quiet_window] == [sent_in_quiet[0].id]
    delivered_at_seven = {
        m.id for m in rows if m.sent_at is not None and QUIET_END <= local(m.sent_at) <= CHECK_AT
    }
    assert delivered_at_seven == parked_ids
    assert parked_messages(h) == []
    assert [m for m in h.fakes.whatsapp.sent if m.to == CUSTOMER] == []  # still deferred and K

    assert_day_integrity(h, first_day)
    assert_day_integrity(h, h.clock.today_dubai())
