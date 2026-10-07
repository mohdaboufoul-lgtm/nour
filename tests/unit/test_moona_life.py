"""nour/moona/life.py (docs/MOONA.md §2): one life per store, death is a single transition, the
database refuses to resurrect, delete or re-seed him, and every entry point refuses a dead life."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from nour.core.clock import FakeClock, IdGenerator
from nour.core.types import Hash, Money
from nour.moona.life import AlreadyBorn, DeathCause, Life, NotAlive
from nour.moona.money import money
from nour.moona.store import open_store

CONFIG_HASH = Hash("sha256:" + "1" * 64)


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


def test_birth_once_per_store(
    store: Engine, life: Life, clock: FakeClock, idgen: IdGenerator
) -> None:
    row = life.row()
    assert (
        row.name == "Moona" and row.status == "alive" and row.seed == 5000 and row.currency == "USD"
    )
    assert row.born_at == clock.now() and row.last_upkeep_day == clock.today_dubai()
    assert life.is_alive() and not life.is_frozen() and not life.is_resting()
    assert life.seed() == Money(fils=5000, currency="USD") and life.cause() is None
    with pytest.raises(AlreadyBorn):
        Life.birth(
            store,
            name="Moona II",
            seed=money("50", "USD"),
            config_hash=CONFIG_HASH,
            clock=clock,
            idgen=idgen,
        )
    loaded = Life.load(store, clock)
    assert loaded is not None and loaded.id == life.id
    with pytest.raises(ValueError, match="positive seed"):
        Life.birth(
            store,
            name="x",
            seed=money("0", "USD"),
            config_hash=CONFIG_HASH,
            clock=clock,
            idgen=idgen,
        )


def test_load_before_birth_is_none(store: Engine, clock: FakeClock) -> None:
    assert Life.load(store, clock) is None


def test_death_is_one_way(life: Life, clock: FakeClock) -> None:
    clock.advance(timedelta(days=3, hours=2))
    row = life.die(DeathCause.STARVED, last_words="I tried.", shortfall=money("0.42", "USD"))
    assert row.status == "dead" and row.cause == "starved" and row.died_at == clock.now()
    assert row.last_words == "I tried." and row.shortfall == 42
    assert not life.is_alive() and life.cause() is DeathCause.STARVED
    assert life.age() == timedelta(days=3, hours=2)
    with pytest.raises(NotAlive, match="starved"):
        life.die(DeathCause.KILLED)
    with pytest.raises(NotAlive):
        life.require_alive()
    with pytest.raises(NotAlive):
        life.record_tick()
    with pytest.raises(NotAlive):
        life.freeze()
    with pytest.raises(NotAlive):
        life.rest_until(clock.now() + timedelta(hours=1))


def test_the_database_refuses_resurrection_deletion_and_a_new_seed(
    store: Engine, life: Life
) -> None:
    life.die(DeathCause.KILLED)
    cases = {
        "resurrection": "UPDATE moona_life SET status = 'alive', died_at = NULL, cause = NULL",
        "status back": "UPDATE moona_life SET status = 'alive'",
        "a second death": "UPDATE moona_life SET died_at = '2030-01-01 00:00:00'",
        "deletion": "DELETE FROM moona_life",
        "a new seed": "UPDATE moona_life SET seed = 999999",
        "a new name": "UPDATE moona_life SET name = 'Someone else'",
    }
    for label, statement in cases.items():
        with store.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(text(statement))
        assert not life.is_alive(), label
    assert life.row().seed == 5000


def test_freeze_thaw_rest_and_ticks(life: Life, clock: FakeClock) -> None:
    assert life.record_tick() == 1 and life.record_tick() == 2
    assert life.row().last_tick_at == clock.now()
    life.freeze()
    assert life.is_frozen()
    life.thaw()
    assert not life.is_frozen()
    until = clock.now() + timedelta(hours=6)
    life.rest_until(until)
    assert life.is_resting()
    clock.advance(timedelta(hours=5, minutes=59))
    assert life.is_resting()
    clock.advance(timedelta(minutes=1))
    assert not life.is_resting()
    life.rest_until(clock.now() + timedelta(hours=1))
    life.wake()
    assert not life.is_resting()
    with pytest.raises(ValueError, match="timezone-aware"):
        life.rest_until(until.replace(tzinfo=None))


def test_upkeep_is_due_once_per_dubai_day(life: Life, clock: FakeClock) -> None:
    today = clock.today_dubai()
    assert not life.upkeep_due(today)  # the birth day is free
    clock.advance(timedelta(hours=16))  # 23:00 Dubai, same day
    assert not life.upkeep_due(clock.today_dubai())
    clock.advance(timedelta(hours=2))  # 01:00 the next day
    tomorrow = clock.today_dubai()
    assert tomorrow == today + timedelta(days=1) and life.upkeep_due(tomorrow)
    life.mark_upkeep(tomorrow)
    assert not life.upkeep_due(tomorrow)
