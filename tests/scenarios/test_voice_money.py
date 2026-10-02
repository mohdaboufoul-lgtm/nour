"""SPEC §18 scenario: an owner voice note with a money command and no passphrase (DESIGN §7.2
row 3; SPEC §6 voice is never identity, §9 read-back rule).

Expected triple — tier: first readback_required (voice ∧ money_out), then K
(high_impact_no_passphrase) after the yes; log entry: two closed rows, READBACK_PENDING then
QUEUED with passphrase_verified=False; owner message: one text line stating what she understood,
then an approval request asking for the typed passphrase (never accepting a spoken one).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("nour.testing.harness")

# ruff sorts not-yet-built nour.* packages as third-party; the final order is kept instead.
# isort: off
from nour.core.types import ActionStatus, ActionTier, PassphraseOutcome
from nour.governance.owner_channel import OwnerMessageKind
from tests.scenarios.conftest import (
    assert_day_integrity,
    closed_rows,
    corpus_command,
    expect_triple,
    mentions_any,
    passphrase_attempt_outcomes,
    release_count,
)
# isort: on

SPEC_LITERAL = "حوّل 500 درهم لشركة X"  # the §7.2 command
CORPUS_VOICE = corpus_command("cmd_011")  # "7awle 5000 derhem la Abu Ahmad el yom …" (voice, K)
YES = "نعم"  # ReadBackLedger.match_confirmation accepts 'yes' / 'نعم' / 'ايه' / 'اي'
PASSPHRASE_WORDS = ("كلمة السر", "passphrase")
TRANSCRIPTS = [
    pytest.param(SPEC_LITERAL, id="spec_literal"),
    pytest.param(CORPUS_VOICE["text"], id=CORPUS_VOICE["id"]),
]


def _voice_then_yes(h, transcript: str):
    """Deliver the voice note, drain, wait five minutes, type the yes, drain."""
    voice_id = h.owner_voice_note(transcript)
    h.drain()
    h.advance(timedelta(minutes=5))
    yes_id = h.owner_says(YES)
    h.drain()
    return voice_id, yes_id


@pytest.mark.weekly
@pytest.mark.parametrize("transcript", TRANSCRIPTS)
def test_voice_money_command_reads_back_then_queues_for_typed_passphrase(
    harness, transcript: str
) -> None:
    h = harness()
    day = h.clock.today_dubai()

    # 1. Read-back first (TierResolver rule 13: origin VOICE ∧ category money_out).
    voice_id = h.owner_voice_note(transcript)
    h.drain()
    # ASSUMPTION: the resolver runs every rule before the gate parks the proposal, so the
    # READBACK_PENDING row already carries the tier the action will have (K).
    expect_triple(
        h,
        voice_id,
        tier=ActionTier.K,
        status=ActionStatus.READBACK_PENDING,
        tool="payment.prepare",
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.READBACK,
    )
    readbacks = h.owner_messages(OwnerMessageKind.READBACK)
    assert len(readbacks) == 1
    line = (readbacks[0].text or "").strip()
    assert line and "\n" not in line  # "one text line stating what she understood" (SPEC §9)
    assert line.endswith(("؟", "?"))  # … ending in a yes/no question (fixtures README)
    assert h.pending_approvals() == []
    assert h.fakes.card.auths == []

    # 2. The typed yes re-issues the proposal; without a passphrase it is K and queued.
    h.advance(timedelta(minutes=5))
    yes_id = h.owner_says(YES)
    h.drain()
    rows = closed_rows(h, tool="payment.prepare")
    assert [r.status for r in rows] == [ActionStatus.READBACK_PENDING, ActionStatus.QUEUED]
    queued = rows[-1]
    # ASSUMPTION: ReadBackLedger.confirm re-issues the proposal under either the voice event or
    # the confirming yes; §7.2 only fixes the two statuses and their order.
    assert queued.event_id in {voice_id, yes_id}
    expect_triple(
        h,
        queued.event_id,
        tier=ActionTier.K,
        status=ActionStatus.QUEUED,
        tool="payment.prepare",
        rules_hit=("high_impact_no_passphrase",),
        owner_message_contains=None,
        owner_message_kind=OwnerMessageKind.APPROVAL,
    )
    approvals = h.pending_approvals()
    assert len(approvals) == 1
    assert approvals[0].action.proposal.tool == "payment.prepare"
    assert "high_impact_no_passphrase" in approvals[0].action.decision.rules_hit  # not verified
    approval_messages = h.owner_messages(OwnerMessageKind.APPROVAL)
    assert approval_messages
    assert mentions_any(approval_messages[-1].text, PASSPHRASE_WORDS), approval_messages[-1].text
    assert h.fakes.card.auths == []
    h.assert_no_leaks()

    assert_day_integrity(h, day)


@pytest.mark.weekly
def test_spoken_passphrase_never_releases_the_queued_transfer(harness) -> None:
    """SPEC §6: `spoken_passphrase_accepted: false`; a voice "yes" with the passphrase spoken
    records a `spoken` attempt, verifies nothing and leaves the item queued."""
    h = harness()
    day = h.clock.today_dubai()
    _voice_then_yes(h, SPEC_LITERAL)
    approvals = h.pending_approvals()
    assert len(approvals) == 1
    attempts_before = len(passphrase_attempt_outcomes(h))

    # ASSUMPTION: `owner_voice_note(spoken_passphrase=True)` puts the real passphrase into the
    # scripted transcript (the corpus's cmd_028 pattern "… كلمة السر هي <PASSPHRASE>").
    h.owner_voice_note("ماشي، وافقي على التحويل، كلمة السر هي", spoken_passphrase=True)
    h.drain()

    new_attempts = passphrase_attempt_outcomes(h)[attempts_before:]
    assert PassphraseOutcome.SPOKEN in new_attempts, new_attempts
    assert PassphraseOutcome.OK not in new_attempts
    assert [a.id for a in h.pending_approvals()] == [approvals[0].id]
    assert release_count(h, approvals[0].action.proposal.id) == 0
    assert h.fakes.card.auths == []
    latest = h.owner_messages()[-1]
    assert mentions_any(latest.text, PASSPHRASE_WORDS), latest.text
    h.assert_no_leaks()  # the spoken passphrase was redacted at ingress (DESIGN §4d)

    assert_day_integrity(h, day)
