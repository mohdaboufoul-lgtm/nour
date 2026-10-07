"""Birth, death and the states in between (docs/MOONA.md §2; SPEC §7 "their own … kill
switches", §12 "the kill switch is obeyed instantly … keep logging").

A :class:`Life` is one ``moona_life`` row. ``birth`` writes it (and refuses a second one in the
same store: one wallet, one life), ``die`` is the single irreversible transition, and the
database enforces both (``nour/moona/store.py``). Everything the runtime does starts with
:meth:`Life.require_alive`, which raises :class:`NotAlive` once he is dead, so "nothing he could
do executes" is a type-level fact rather than a prompt instruction.

Two owner controls that are not death: ``freeze`` (he stops thinking and acting; upkeep still
runs, so a frozen Moona still starves) and ``rest_until`` (his own choice to sleep and save the
cost of thinking). The owner's ``kill`` is ``die(KILLED)``: terminal, like starving.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from nour.core.clock import Clock, IdGenerator
from nour.core.errors import NourError
from nour.core.types import Hash, Money, Ulid
from nour.moona.money import same_currency
from nour.moona.store import LifeRow, reading, unit_of_work


class LifeStatus(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"


class DeathCause(StrEnum):
    """Why a life ended: the wallet hit the floor, or the owner killed him."""

    STARVED = "starved"
    KILLED = "killed"


class NotAlive(NourError):  # noqa: N818 - the name states the fact, as DESIGN §3.2 names errors
    """docs/MOONA.md §2: Moona is dead; nothing executes, nothing is undone."""

    cause: DeathCause | None

    def __init__(self, name: str, cause: DeathCause | None) -> None:
        self.cause = cause
        why = f" ({cause.value})" if cause is not None else ""
        super().__init__(f"{name} is dead{why}; nothing executes")


class AlreadyBorn(NourError):  # noqa: N818 - see NotAlive
    """One life per store: ``Life.birth`` found a ``moona_life`` row already."""


class Life:
    """§7 §12: the one row that says whether he is alive, frozen, resting, and how he died."""

    def __init__(self, engine: Engine, life_id: Ulid, clock: Clock) -> None:
        self._engine = engine
        self._id = life_id
        self._clock = clock

    # ----- birth and loading

    @classmethod
    def birth(
        cls,
        engine: Engine,
        *,
        name: str,
        seed: Money,
        config_hash: Hash,
        clock: Clock,
        idgen: IdGenerator,
    ) -> Life:
        """Write the one ``moona_life`` row. The first day's upkeep is free (``last_upkeep_day``
        is the birth day); the wallet's seed entry is the caller's next step (``Wallet.seed``).
        Raises :class:`AlreadyBorn` when the store already holds a life, alive or dead."""
        if not isinstance(seed, Money) or seed.fils <= 0:
            raise ValueError("a life starts with a positive seed")
        if not name.strip():
            raise ValueError("a life has a name")
        now = clock.now()
        life_id = idgen.new()
        with unit_of_work(engine) as session:
            existing = session.scalar(select(func.count()).select_from(LifeRow))
            if existing:
                raise AlreadyBorn("this store already holds a life; one wallet, one life")
            session.add(
                LifeRow(
                    id=life_id,
                    name=name.strip(),
                    born_at=now,
                    seed=seed.fils,
                    currency=seed.currency,
                    config_hash=str(config_hash),
                    status=LifeStatus.ALIVE.value,
                    frozen=False,
                    ticks=0,
                    last_upkeep_day=clock.today_dubai(),
                )
            )
        return cls(engine, life_id, clock)

    @classmethod
    def load(cls, engine: Engine, clock: Clock) -> Life | None:
        """The store's life, or ``None`` before birth."""
        with reading(engine) as session:
            row = session.scalar(select(LifeRow).order_by(LifeRow.born_at).limit(1))
            if row is None:
                return None
            return cls(engine, Ulid(row.id), clock)

    # ----- reading

    @property
    def id(self) -> Ulid:
        return self._id

    def row(self) -> LifeRow:
        """A fresh read of the row (never cached: the database is the truth)."""
        with reading(self._engine) as session:
            row = session.get(LifeRow, self._id)
            if row is None:
                raise NotAlive("Moona", None)
            return row

    @property
    def name(self) -> str:
        return self.row().name

    @property
    def currency(self) -> str:
        return self.row().currency

    def seed(self) -> Money:
        row = self.row()
        return Money(fils=row.seed, currency=row.currency)

    def is_alive(self) -> bool:
        return self.row().status == LifeStatus.ALIVE.value

    def is_frozen(self) -> bool:
        return bool(self.row().frozen)

    def is_resting(self, now: datetime | None = None) -> bool:
        awake_at = self.row().awake_at
        if awake_at is None:
            return False
        return awake_at > (now if now is not None else self._clock.now())

    def cause(self) -> DeathCause | None:
        value = self.row().cause
        return DeathCause(value) if value is not None else None

    def age(self, now: datetime | None = None) -> timedelta:
        row = self.row()
        end = row.died_at if row.died_at is not None else (now or self._clock.now())
        return end - row.born_at

    def require_alive(self) -> LifeRow:
        """The row when he is alive; :class:`NotAlive` otherwise. Every entry point calls this."""
        row = self.row()
        if row.status != LifeStatus.ALIVE.value:
            raise NotAlive(row.name, DeathCause(row.cause) if row.cause else None)
        return row

    # ----- the one-way door

    def die(
        self,
        cause: DeathCause,
        *,
        last_words: str | None = None,
        shortfall: Money | None = None,
    ) -> LifeRow:
        """Set ``status=dead``, ``died_at``, ``cause``, ``last_words`` and ``shortfall`` once.
        Refuses when already dead (``NotAlive``); the database refuses a second transition too."""
        cause = DeathCause(cause)
        with unit_of_work(self._engine) as session:
            row = session.get(LifeRow, self._id)
            if row is None or row.status != LifeStatus.ALIVE.value:
                name = row.name if row is not None else "Moona"
                raise NotAlive(name, DeathCause(row.cause) if row and row.cause else None)
            if shortfall is not None:
                same_currency(shortfall, row.currency, "Life.die shortfall")
                if shortfall.fils < 0:
                    raise ValueError("a shortfall is not negative")
            row.status = LifeStatus.DEAD.value
            row.died_at = self._clock.now()
            row.cause = cause.value
            row.last_words = last_words
            row.shortfall = shortfall.fils if shortfall is not None else 0
            row.awake_at = None
            session.flush()
            session.refresh(row)
            return row

    # ----- owner pause and his own rest

    def freeze(self) -> LifeRow:
        return self._update(frozen=True)

    def thaw(self) -> LifeRow:
        return self._update(frozen=False)

    def rest_until(self, when: datetime) -> LifeRow:
        if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
            raise ValueError("rest_until takes a timezone-aware datetime")
        return self._update(awake_at=when)

    def wake(self) -> LifeRow:
        return self._update(awake_at=None)

    # ----- bookkeeping the runtime does

    def record_tick(self) -> int:
        """Count one tick; returns its number (1 for the first tick of his life)."""
        with unit_of_work(self._engine) as session:
            row = self._alive_row(session)
            row.ticks = row.ticks + 1
            row.last_tick_at = self._clock.now()
            session.flush()
            return int(row.ticks)

    def upkeep_due(self, today: date) -> bool:
        """True on the first tick of a new Dubai day (the birth day is free)."""
        last = self.row().last_upkeep_day
        return last is None or today > last

    def mark_upkeep(self, today: date) -> None:
        self._update(last_upkeep_day=today)

    # ----- internals

    def _alive_row(self, session: object) -> LifeRow:
        from sqlalchemy.orm import Session

        assert isinstance(session, Session)
        row = session.get(LifeRow, self._id)
        if row is None or row.status != LifeStatus.ALIVE.value:
            name = row.name if row is not None else "Moona"
            raise NotAlive(name, DeathCause(row.cause) if row and row.cause else None)
        return row

    def _update(self, **fields: object) -> LifeRow:
        with unit_of_work(self._engine) as session:
            row = self._alive_row(session)
            for name, value in fields.items():
                setattr(row, name, value)
            session.flush()
            session.refresh(row)
            return row
