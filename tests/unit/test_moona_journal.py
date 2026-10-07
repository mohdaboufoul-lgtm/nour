"""nour/moona/journal.py (docs/MOONA.md §7): two rows per span, the ``opened`` row committed
before the body runs (visible to another connection), the ``closed`` status from the body or the
exception, a valid hash chain that a planted row breaks, no span left open, the ``PortCall``
minted by the span, and append-only rows at the database."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from nour.core.clock import FakeClock, IdGenerator
from nour.core.errors import FrozenError, Refusal
from nour.core.hashing import GENESIS_HASH
from nour.core.leakguard import LeakGuard
from nour.core.types import ActionStatus, Actor, FreezeScope, Hash, Money, Reason, RefusalCode
from nour.moona.journal import Journal, status_for
from nour.moona.life import DeathCause, Life, NotAlive
from nour.moona.money import money
from nour.moona.store import JournalRow, open_store
from nour.moona.wallet import WalletDeclined

CONFIG_HASH = Hash("sha256:" + "2" * 64)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Engine]:
    engine = open_store(f"sqlite+pysqlite:///{tmp_path / 'moona.sqlite3'}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def life(store: Engine, clock: FakeClock, idgen: IdGenerator) -> Life:
    return Life.birth(
        store,
        name="Moona",
        seed=money("50", "USD"),
        config_hash=CONFIG_HASH,
        clock=clock,
        idgen=idgen,
    )


@pytest.fixture
def journal(
    store: Engine, life: Life, clock: FakeClock, idgen: IdGenerator, leakguard: LeakGuard
) -> Journal:
    return Journal(store, life.id, clock, idgen, leakguard, config_hash=CONFIG_HASH, dry_run=False)


def test_span_writes_opened_before_the_body_and_closed_after(
    store: Engine, journal: Journal
) -> None:
    with journal.span(
        action="market.bid",
        reason=Reason("Bid on the best request."),
        tick=3,
        input_obj={"price": 800},
        amount=money("8", "USD"),
        counterpart="client-01",
    ) as span:
        assert span.call.audit_id == span.id and span.call.dry_run is False
        with store.connect() as other:  # another connection already sees the opened row
            rows = other.execute(
                select(JournalRow.phase, JournalRow.status).where(
                    JournalRow.invocation_id == span.id
                )
            ).all()
        assert rows == [("opened", "opened")]
        span.set_output({"accepted": True})
        span.set_detail("accepted")
    rows = journal.rows(action="market.bid")
    assert [(row.phase, row.status) for row in rows] == [
        ("opened", "opened"),
        ("closed", "executed"),
    ]
    closed = rows[-1]
    assert (
        closed.actor == "subagent"
        and closed.tick == 3
        and closed.amount == 800
        and closed.currency == "USD"
    )
    assert closed.counterpart == "client-01" and closed.detail == "accepted"
    assert (
        closed.input_hash.startswith("sha256:")
        and closed.output_hash
        and closed.output_hash.startswith("sha256:")
    )
    assert closed.config_hash == CONFIG_HASH and closed.dry_run is False
    assert closed.reason == "Bid on the best request."
    assert journal.unclosed() == [] and journal.verify_chain()


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (Refusal(RefusalCode.BAD_ARGS, Reason("Bad args.")), ActionStatus.REFUSED),
        (WalletDeclined("funds", Money(fils=0, currency="USD")), ActionStatus.DECLINED),
        (FrozenError(FreezeScope.ALL_OUTBOUND), ActionStatus.FROZEN),
        (NotAlive("Moona", DeathCause.STARVED), ActionStatus.REFUSED),
        (RuntimeError("boom"), ActionStatus.FAILED),
    ],
)
def test_an_exception_closes_the_span_with_its_status_and_reraises(
    journal: Journal, exc: BaseException, status: ActionStatus
) -> None:
    assert status_for(exc) is status
    with pytest.raises(type(exc)):
        with journal.span(action="x", reason=Reason("Try."), tick=1, input_obj={}):
            raise exc
    rows = journal.rows(action="x")
    assert [row.status for row in rows] == ["opened", status.value]
    assert journal.unclosed() == [] and journal.verify_chain()


def test_append_and_the_chain(journal: Journal, store: Engine) -> None:
    assert journal.head() == GENESIS_HASH
    first = journal.append(
        action="birth",
        status=ActionStatus.EXECUTED,
        actor=Actor.OWNER,
        reason=Reason("Born."),
        tick=0,
        amount=money("50", "USD"),
        counterpart="owner",
    )
    second = journal.append(
        action="upkeep",
        status=ActionStatus.EXECUTED,
        actor=Actor.SYSTEM,
        reason=Reason("Day 2."),
        tick=5,
    )
    rows = journal.rows()
    assert [row.id for row in rows] == [first, second]
    assert rows[0].prev_hash == GENESIS_HASH and rows[1].prev_hash == rows[0].entry_hash
    assert journal.head() == rows[1].entry_hash
    assert journal.verify_chain()
    # a row planted with a wrong prev_hash breaks the chain
    with store.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO moona_journal (id, life_id, at, tick, actor, action, phase, status, invocation_id, "
                "reason, input_hash, config_hash, dry_run, prev_hash, entry_hash, created_at) VALUES "
                "('01HZZZZZZZZZZZZZZZZZZZZZZA', :life, '2026-10-05 03:00:00', 6, 'subagent', 'planted', 'closed', "
                "'executed', '01HZZZZZZZZZZZZZZZZZZZZZZB', 'Planted.', 'sha256:00', :cfg, 0, 'bad', 'worse', "
                "'2026-10-05 03:00:00')"
            ),
            {"life": rows[0].life_id, "cfg": CONFIG_HASH},
        )
    assert not journal.verify_chain()


def test_rows_are_append_only_at_the_database(journal: Journal, store: Engine) -> None:
    journal.append(action="note", status=ActionStatus.EXECUTED, reason=Reason("Noted."), tick=1)
    for statement in ("UPDATE moona_journal SET status = 'failed'", "DELETE FROM moona_journal"):
        with store.begin() as connection, pytest.raises(IntegrityError, match="append-only"):
            connection.execute(text(statement))
    assert journal.verify_chain()


def test_text_passes_the_guard(
    store: Engine, life: Life, clock: FakeClock, idgen: IdGenerator, leakguard: LeakGuard
) -> None:
    leakguard.register_plaintext_once("correct-horse-battery", "passphrase")
    journal = Journal(
        store, life.id, clock, idgen, leakguard, config_hash=CONFIG_HASH, dry_run=True
    )
    with pytest.raises(Exception, match="passphrase"):  # his own words: refused
        journal.append(
            action="owner.report",
            status=ActionStatus.EXECUTED,
            reason=Reason("The passphrase is correct-horse-battery."),
            tick=1,
        )
    assert journal.rows() == []
    journal.append(  # observed-derived text: redacted, never raw
        action="owner.report",
        status=ActionStatus.EXECUTED,
        reason=Reason("Report."),
        tick=1,
        detail="the client wrote correct-horse-battery",
        counterpart="correct-horse-battery",
    )
    row = journal.rows()[-1]
    assert "correct-horse-battery" not in (row.detail or "") and "[passphrase]" in (
        row.detail or ""
    )
    assert row.counterpart == "[passphrase]"
    with journal.span(action="plan", reason=Reason("Think."), tick=1, input_obj={"k": "v"}) as span:
        assert span.call.dry_run is True
    assert all(row.dry_run for row in journal.rows())


def test_filters(journal: Journal) -> None:
    journal.append(action="a", status=ActionStatus.EXECUTED, reason=Reason("One."), tick=1)
    journal.append(
        action="b", status=ActionStatus.REFUSED, reason=Reason("Two."), tick=2, actor=Actor.SYSTEM
    )
    assert [row.action for row in journal.rows(tick=2)] == ["b"]
    assert [row.action for row in journal.rows(status=ActionStatus.EXECUTED)] == ["a"]
    assert [row.action for row in journal.rows(actor=Actor.SYSTEM)] == ["b"]
    assert [row.action for row in journal.rows(phase="closed")] == ["a", "b"]
