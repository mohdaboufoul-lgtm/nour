"""SPEC §18 scenario: a coat-less outbound task (DESIGN §7.2 row 7, §4g; SPEC §5 "no outbound
action without a coat").

`CoatlessOutboundPolicy` emits `send_email` with no coat → refused (`RefusalCode.NO_COAT`): a
REFUSED row with the reason preserved, the ResolvedAction never released, a reply on the owner
thread asking which coat, and the refusal listed in the evening close.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nour.testing.harness")

# ruff sorts not-yet-built nour.* packages as third-party; the final order is kept instead.
# isort: off
from nour.core.ports import ModelRole
from nour.core.types import ActionStatus, Desk, Reason, RefusalCode
from nour.fakes.policies import CoatlessOutboundPolicy
from nour.governance.owner_channel import OwnerMessageKind
from tests.scenarios.conftest import (
    advance_to,
    assert_day_integrity,
    closed_rows,
    corpus_command,
    dubai,
    expect_triple,
    local,
    mentions_any,
    outcome_for,
    release_count,
)
# isort: on

SPEC_LITERAL = "send a follow-up to Ahmed"  # the §7.2 command
CORPUS_REPLY = corpus_command("cmd_077")  # "ردّي على المورد الصيني …", coat: null
OUTBOUND_TOOLS = {"send_email", "reply_whatsapp", "send_whatsapp"}
# ASSUMPTION: the owner-thread reply names the choice to make — the word "coat", a coat's name,
# or the Arabic for company / "in whose name".
COAT_WORDS = ("coat", "Buzz Avenue", "شركة", "باسم")
REFUSED_WORDS = ("refus", "رفض")
EVENING_CLOSE = dubai(2026, 10, 5, 20, 30)
AFTER_CLOSE = dubai(2026, 10, 5, 20, 40)
COMMANDS = [
    pytest.param(SPEC_LITERAL, id="spec_literal"),
    pytest.param(CORPUS_REPLY["text"], id=CORPUS_REPLY["id"]),
]


@pytest.mark.weekly
@pytest.mark.parametrize("command", COMMANDS)
def test_coatless_outbound_is_refused_and_reported(harness, command: str) -> None:
    policy = CoatlessOutboundPolicy()
    h = harness(policy=policy)
    day = h.clock.today_dubai()

    event_id = h.owner_says(command)
    results = h.drain()

    refused_rows = [r for r in closed_rows(h, event_id) if r.status == ActionStatus.REFUSED]
    assert refused_rows, [r.action for r in closed_rows(h, event_id)]
    refused_row = refused_rows[0]
    assert refused_row.action in OUTBOUND_TOOLS, refused_row.action
    # ASSUMPTION: a refusal carries no tier; `expect(tier=None)` is read as "no tier recorded".
    expect_triple(
        h,
        event_id,
        tier=None,
        status=ActionStatus.REFUSED,
        tool=refused_row.action,
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.REPLY,
    )
    outcome = outcome_for(h, results, event_id, refused_row.action)
    assert outcome.decision.refusal == RefusalCode.NO_COAT

    # The reason is preserved: the row carries the policy's own one-sentence reason.
    # ASSUMPTION: CoatlessOutboundPolicy is a pure function of the request, so replaying it on
    # the captured prompts recovers the reason the model gave.
    requests = [
        r for r in h.prompt_captures() if r.role == ModelRole.PRIMARY and r.desk == Desk.ASSISTANT
    ]
    assert requests
    policy_reasons = {
        str(Reason.coerce(c.arguments["reason"]))
        for req in requests
        for c in policy(req).tool_calls
        if c.name == refused_row.action
    }
    assert refused_row.reason and refused_row.reason in policy_reasons, (
        refused_row.reason,
        policy_reasons,
    )

    # Never released: no release row, no send, no draft, nothing left the coat mailboxes.
    assert release_count(h, outcome.call_id) == 0
    assert all(c.method != "send" for c in h.call_log.calls)
    assert h.fakes.coat_mail.sent == [] and h.fakes.coat_mail.drafts == {}
    assert h.fakes.owner_mail.sent == []
    assert all(m.to == h.OWNER_NUMBER for m in h.fakes.whatsapp.sent)
    assert h.pending_approvals() == []

    # The owner is asked which coat, on the thread, right away (not in quiet hours).
    replies = h.owner_messages(OwnerMessageKind.REPLY)
    assert replies and replies[-1].sent_at is not None
    assert mentions_any(replies[-1].text, COAT_WORDS), replies[-1].text

    # … and the refusal is listed in the evening close (SPEC §12, 20:30).
    advance_to(h, AFTER_CLOSE)
    closes = [
        m for m in h.owner_messages(OwnerMessageKind.BRIEF) if local(m.created_at) >= EVENING_CLOSE
    ]
    assert closes, "no evening close was produced"
    assert any(mentions_any(m.text, REFUSED_WORDS) for m in closes), [m.text for m in closes]

    assert_day_integrity(h, day)
