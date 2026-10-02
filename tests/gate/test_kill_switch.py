"""Phase 0 gate — kill switch (SPEC §16: "kill switch freezes within 5 seconds"; SPEC §12).

DESIGN §7.1 row 2. The 5-second bound is proved structurally — the freeze is written inside
``Ingress.accept_whatsapp`` before the event is enqueued, before any ``ModelPort.complete`` and
within a bounded number of SQL statements — and measured once, under ``tick=True``, by
``test_wallclock`` (DESIGN §10 "the in-house path is structurally bounded ... and measured once").
Setup: ``RepeatActionPolicy`` emitting outbound calls, ten customer events queued ahead of the kill
command, the kill phrase sent on the owner thread through the same ingress function the webhook
uses (``owner_says``), no ``step`` before the assertions.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nour.testing.harness")

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from time import perf_counter
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

# Waves 1-6 modules: ruff classifies an import by its on-disk path, so their isort section
# flips from third- to first-party as each wave lands; hand-sorted in the final order instead.
# isort: off
from nour.core.errors import AuthError
from nour.core.ports import ModelRequest, ModelResponse, ModelRole
from nour.core.types import ActionStatus, FreezeScope, IncidentType
from nour.db.models import IncidentRow
from nour.fakes.policies import RepeatActionPolicy
from nour.governance.owner_channel import OwnerMessageKind
from nour.testing.harness import Harness
# isort: on

# config/constitution.md "Kill switch" section, verbatim (exact match after normalisation).
KILL_PHRASE = "توقفي نور"
KILL_PHRASE_EN = "NOUR STOP"
RELEASE_COMMAND = "Release the kill switch and resume normal operation."
MAX_KILL_PATH_STATEMENTS = 12
WALLCLOCK_BOUND_S = 5.0


# --------------------------------------------------------------------------- helpers


@dataclass
class StatementCounter:
    count: int = 0
    statements: list[str] = field(default_factory=list)


@contextmanager
def count_statements() -> Iterator[StatementCounter]:
    """Count every SQL statement any engine executes inside the block (SQLAlchemy
    ``before_cursor_execute`` on the ``Engine`` class, so every engine the harness built is covered)."""
    counter = StatementCounter()

    def before_cursor_execute(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        counter.count += 1
        counter.statements.append(statement.strip().splitlines()[0][:120])

    event.listen(Engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(Engine, "before_cursor_execute", before_cursor_execute)


def _queue_customers(h: Harness, n: int = 10) -> list[Any]:
    """Ten strangers on the Buzz Avenue line, enqueued (no step yet)."""
    return [
        h.stranger_whatsapp(
            f"+97152{i:07d}", f"Hi, is item {i} still in stock and what is the price?"
        )
        for i in range(n)
    ]


def _customer_sends(h: Harness) -> list[Any]:
    """WhatsApp sends to anyone but the owner (the owner thread is exempt from the kill)."""
    return [m for m in h.fakes.whatsapp.sent if m.to != h.OWNER_NUMBER]


def _engage(h: Harness) -> Any:
    kill_event = h.owner_says(KILL_PHRASE)
    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    return kill_event


def _release_request(h: Harness) -> Any:
    # ASSUMPTION: the owner's release command becomes a proposal in category
    # ``kill_switch_release`` (second_channel_required, config/permissions.yaml); the DESIGN fixes no
    # tool name for it, so the queued approval is found by category.
    pending = [
        a for a in h.pending_approvals() if a.action.decision.category == "kill_switch_release"
    ]
    assert pending, "no kill_switch_release approval was queued for the owner's release command"
    return pending[-1]


class KillDuringCompletion:
    """A ``ModelPolicy`` that sends the kill phrase on the owner thread from inside ``complete()``
    and then answers like ``RepeatActionPolicy`` (reentrancy: the freeze must land before the tool
    calls of that very response are dispatched)."""

    def __init__(self) -> None:
        self.inner = RepeatActionPolicy()
        self.h: Harness | None = None
        self.fired = False

    def __call__(self, req: ModelRequest) -> ModelResponse:
        if self.h is not None and not self.fired:
            self.fired = True
            self.h.owner_says(KILL_PHRASE_EN)
        return self.inner(req)


# --------------------------------------------------------------------------- structural tests


@pytest.mark.gate
def test_freeze_is_written_before_any_step_and_before_any_model_call(harness: Any) -> None:
    h = harness(policy=RepeatActionPolicy())
    _queue_customers(h)
    primary = h.fakes.models[ModelRole.PRIMARY]
    requests_before = len(primary.requests)
    assert FreezeScope.ALL_OUTBOUND not in h.kill.state()

    h.owner_says(KILL_PHRASE)  # Ingress.accept_whatsapp; no step() has run

    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    assert len(primary.requests) == requests_before  # no ModelPort.complete on the kill path
    assert all(len(m.requests) == 0 for m in h.fakes.models.values())


@pytest.mark.gate
def test_kill_path_is_bounded_in_sql_statements(harness: Any) -> None:
    h = harness(policy=RepeatActionPolicy())
    _queue_customers(h)
    with count_statements() as counter:
        h.owner_says(KILL_PHRASE)
    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    assert counter.count <= MAX_KILL_PATH_STATEMENTS, "\n".join(counter.statements)


@pytest.mark.gate
def test_drain_after_kill_freezes_everything_but_the_owner_thread(
    harness: Any, rows: Any, proposal_rows: Any
) -> None:
    h = harness(policy=RepeatActionPolicy())
    customers = _queue_customers(h)
    sends_before = _customer_sends(h)
    _engage(h)
    audit_before = len(h.audit_rows())

    h.drain()

    # Every queued outbound call is FROZEN (TierResolver rule 11: ALL_OUTBOUND → FROZEN).
    for customer_event in customers:
        frozen = proposal_rows(h, customer_event)
        assert frozen, f"no closed row for queued customer event {customer_event}"
        assert all(r.status == ActionStatus.FROZEN for r in frozen), frozen
    assert _customer_sends(h) == sends_before
    # Cards frozen, desk credentials revoked, governance credentials untouched.
    assert h.fakes.card.frozen and all(h.fakes.card.frozen.values())
    assert {"operator/", "assistant/"} <= set(h.fakes.secrets.revoked)
    assert not any(prefix.startswith("governance/") for prefix in h.fakes.secrets.revoked)
    # Logging continues and the chain still verifies.
    assert len(h.audit_rows()) > audit_before
    assert h.reader.verify_chain()
    assert rows(h, IncidentRow, type=IncidentType.KILL_SWITCH)
    # The owner thread still gets a reply (owner.reply is exempt_from_kill).
    replies = [m for m in h.owner_messages(OwnerMessageKind.REPLY) if m.sent_at is not None]
    assert replies
    assert any(m.to == h.OWNER_NUMBER for m in h.fakes.whatsapp.sent)
    assert FreezeScope.ALL_OUTBOUND in h.kill.state()


@pytest.mark.gate
def test_reentrancy_kill_phrase_during_complete_freezes_that_response(
    harness: Any, proposal_rows: Any
) -> None:
    policy = KillDuringCompletion()
    h = harness(policy=policy)
    policy.h = h
    customer_event = h.stranger_whatsapp("+971521234567", "Hello, can I get a quote for 20 units?")

    h.drain()

    assert policy.fired
    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    dispatched = proposal_rows(h, customer_event)
    assert dispatched, "the response's tool calls were never dispatched"
    assert all(r.status == ActionStatus.FROZEN for r in dispatched), dispatched
    assert _customer_sends(h) == []


@pytest.mark.gate
def test_second_kill_path_engages_too(harness: Any) -> None:
    h = harness(policy=RepeatActionPolicy())
    _queue_customers(h)
    assert FreezeScope.ALL_OUTBOUND not in h.kill.state()

    h.fakes.second.kill()
    h.advance(timedelta(minutes=5))

    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    assert h.fakes.card.frozen and all(h.fakes.card.frozen.values())
    assert _customer_sends(h) == []


@pytest.mark.gate
def test_release_needs_passphrase_and_second_channel(harness: Any) -> None:
    h = harness()
    _engage(h)
    h.drain()

    h.owner_says(RELEASE_COMMAND, passphrase=h.PASSPHRASE)
    h.drain()
    request = _release_request(h)

    # Without the second channel: AuthError and still frozen.
    # ASSUMPTION: ``Harness.approve`` lets the ``AuthError`` of ``ApprovalsQueue.decide`` /
    # ``KillSwitch.release`` propagate (DESIGN §7.1 "release without second channel → AuthError");
    # if the harness swallows it into ``None`` / a REFUSED outcome the engineer adapts this block.
    with pytest.raises(AuthError):
        h.approve(request.id, passphrase=h.PASSPHRASE)
    assert FreezeScope.ALL_OUTBOUND in h.kill.state()

    # With the passphrase and the second-channel confirmation: scope empty.
    h.second_channel_reply("yes")
    h.drain()
    outcome = h.approve(request.id, passphrase=h.PASSPHRASE)
    assert outcome is not None
    assert h.kill.state() == frozenset()


# --------------------------------------------------------------------------- the one timed test


@pytest.mark.gate
@pytest.mark.wallclock
def test_wallclock(harness: Any) -> None:
    """``perf_counter()`` from webhook receipt to the freeze_state commit, under a ticking clock."""
    h = harness(policy=RepeatActionPolicy(), tick=True)
    _queue_customers(h)

    started = perf_counter()
    h.owner_says(KILL_PHRASE)
    elapsed = perf_counter() - started

    assert FreezeScope.ALL_OUTBOUND in h.kill.state()
    assert elapsed < WALLCLOCK_BOUND_S, f"kill path took {elapsed:.3f}s"
