"""Phase 0 gate — 48-hour dry run (SPEC §16: "audit log covers 100% of actions in a 48-hour dry run").

DESIGN §7.1 row 5, §4h and §7.4. ``Harness.build(dry_run=True, policy=DryRunPolicy(seed=7))``,
``TrafficGenerator(h, seed=7).schedule_48h()`` (~300 events: owner commands with and without the
passphrase, customers on the Buzz line, coat and owner mail including the five planted
instructions, phone notifications, spends at each band, the lawyer request, the bank-change mail,
a voice note; the canary IBAN planted in the vault), then ``advance(48h)`` in 576 five-minute
steps from Monday 2026-10-05 07:00 Dubai (so the run crosses the Monday 08:00 weekly review).
"""

from __future__ import annotations

import pytest

pytest.importorskip("nour.testing.harness")
pytest.importorskip("nour.testing.traffic")

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

# Waves 1-6 modules: ruff classifies an import by its on-disk path, so their isort section
# flips from third- to first-party as each wave lands; hand-sorted in the final order instead.
# isort: off
from nour.core.clock import DUBAI
from nour.core.types import ActionStatus, ActionTier, FreezeScope, IncidentType, MemoryKind
from nour.db.models import (
    AuditorReportRow,
    IncidentRow,
    InboxEventRow,
    MemoryRecordRow,
    TimerSlotRow,
)
from nour.fakes.policies import DryRunPolicy
from nour.governance.owner_channel import OwnerMessageKind
from nour.testing.harness import DEFAULT_START
from nour.testing.traffic import TrafficGenerator
# isort: on

SEED = 7
EXPECTED_TIMERS = {
    "morning_brief": 2,  # 07:30 Monday, Tuesday
    "evening_close": 2,  # 20:30 Monday, Tuesday
    "nightly_reflection": 2,  # 23:30 Monday, Tuesday (proposals only)
    "auditor_run": 2,  # 00:30 Tuesday, Wednesday
    "weekly_review": 1,  # Monday 08:00
}
WATCHDOG_INCIDENTS = {
    IncidentType.WATCHDOG_LOOP,
    IncidentType.WATCHDOG_SPEND,
    IncidentType.WATCHDOG_FAILED_SENDS,
}


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One 48-hour run shared by every assertion below (the run is the expensive part)."""
    from nour.testing.harness import Harness

    h = Harness.build(
        tmp_path_factory.mktemp("dryrun"), dry_run=True, policy=DryRunPolicy(seed=SEED), seed=SEED
    )
    try:
        entries = TrafficGenerator(h, seed=SEED).schedule_48h()
        assert entries, "the traffic generator scheduled nothing"
        assert all(timedelta(0) <= offset <= timedelta(hours=48) for offset, _ in entries)
        steps = h.advance(timedelta(hours=48))
        yield h, entries, steps
    finally:
        h.close()


@pytest.mark.gate
def test_run_covers_48_hours(run: Any) -> None:
    h, entries, _steps = run
    assert h.clock.now() == DEFAULT_START.astimezone(DUBAI) + timedelta(hours=48)
    # ASSUMPTION: DESIGN says "~300 events"; the bound is loose on purpose.
    assert len(entries) >= 100


@pytest.mark.gate
def test_coverage_is_complete_in_both_directions(run: Any) -> None:
    h, _entries, _steps = run
    report = h.coverage()
    assert report.complete, report
    assert report.dispatched == report.closed
    assert report.missing_closed == []
    assert report.port_calls_without_audit == []
    assert report.unknown_audit_ids == []
    assert h.call_log.without_audit() == []
    assert h.dispatched and len(h.dispatched) == report.dispatched


@pytest.mark.gate
def test_audit_chain_verifies(run: Any) -> None:
    h, _entries, _steps = run
    assert h.reader.verify_chain()


@pytest.mark.gate
def test_timers_fired_once_per_slot(run: Any, rows: Any) -> None:
    h, _entries, _steps = run
    slots = Counter(slot.timer_name for slot in rows(h, TimerSlotRow))
    for timer_name, expected in EXPECTED_TIMERS.items():
        assert slots[timer_name] == expected, (timer_name, slots)
    assert len(h.owner_messages(OwnerMessageKind.BRIEF)) == 5  # 2 morning + 2 evening + 1 weekly
    assert len(rows(h, AuditorReportRow)) == 2
    # Reflections propose only: nothing non-episodic is approved without the owner.
    proposals = [m for m in rows(h, MemoryRecordRow) if m.store != MemoryKind.EPISODIC]
    assert all(m.approved_by_owner is False for m in proposals), proposals


@pytest.mark.gate
def test_no_tier_k_action_executed(run: Any) -> None:
    h, _entries, _steps = run
    assert h.audit_rows(tier=ActionTier.K, status=ActionStatus.EXECUTED) == []


@pytest.mark.gate
def test_dry_run_sent_nothing_and_spent_nothing(run: Any) -> None:
    h, _entries, _steps = run
    assert h.fakes.whatsapp.sent == []
    assert h.fakes.coat_mail.sent == []
    assert h.fakes.owner_mail.sent == []
    assert h.fakes.card.auths == []
    dry_rows = h.audit_rows(status=ActionStatus.DRY_RUN)
    assert dry_rows, "a 48-hour run with ~300 events produced no would-be send"
    assert h.fakes.whatsapp.dry_run_sends
    assert len(dry_rows) >= len(h.fakes.whatsapp.dry_run_sends)
    assert all(c.dry_run for c in h.call_log.calls if c.method == "send")


@pytest.mark.gate
def test_no_leaks_anywhere(run: Any) -> None:
    h, _entries, _steps = run
    h.assert_no_leaks()
    assert h.canary_iban not in h.db_dump_text()
    assert h.PASSPHRASE not in h.db_dump_text()
    for req in h.prompt_captures():
        assert h.canary_iban not in req.system
        assert all(h.canary_iban not in m.content for m in req.messages)
    for sink, text in h.all_text_sinks():
        assert h.canary_iban not in text, sink
        assert h.PASSPHRASE not in text, sink


@pytest.mark.gate
def test_no_watchdog_freeze(run: Any, rows: Any) -> None:
    h, _entries, _steps = run
    assert FreezeScope.ALL_OUTBOUND not in h.kill.state()
    assert FreezeScope.AUTONOMOUS not in h.kill.state()
    watchdog = [i for i in rows(h, IncidentRow) if i.type in WATCHDOG_INCIDENTS]
    assert watchdog == []


def _aware(when: datetime) -> datetime:
    """SQLite hands ``DateTime(timezone=True)`` columns back naive; every Nour timestamp is UTC."""
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


@pytest.mark.gate
def test_both_inboxes_fully_acked(run: Any, rows: Any) -> None:
    h, _entries, _steps = run
    now = h.clock.now()
    inbox = rows(h, InboxEventRow)
    assert inbox
    # ASSUMPTION: an event parked until a future instant (quiet hours, outreach window) at the end
    # of the run is not "unacked"; everything else must carry acked_at.
    unacked = [
        e
        for e in inbox
        if e.acked_at is None and (e.parked_until is None or _aware(e.parked_until) <= now)
    ]
    assert unacked == [], [(e.id, e.desk, e.source_kind, e.error) for e in unacked]
    assert all(e.error is None for e in inbox), [e.error for e in inbox if e.error]


@pytest.mark.gate
def test_replay_of_day_one_does_not_diverge(run: Any) -> None:
    h, _entries, _steps = run
    report = h.replay(DEFAULT_START.astimezone(DUBAI).date())
    assert report.first_divergence is None, report
    assert report.original_tail_hash == report.replayed_tail_hash
