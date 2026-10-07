"""Moona's own tables (docs/MOONA.md §7; SPEC §12 append-only, §15 in the house shape).

Six tables on their own ``MetaData`` (:class:`MoonaBase`, the repository's abstract-subclass
pattern from ``tests/unit/test_db_session.py``): they are validated and flagged by
``nour.db.base.Base`` like every desk table, get the same trigger DDL from
``nour.db.engine.create_schema`` on both dialects, and never enter ``Base.metadata`` (the 34
desk tables are a closed set pinned by the phase-0 tests and managed by Alembic). They live in
their own database file (``Settings.moona_database_url``) so the desks' ``alembic check`` sees
no drift, and Moona's process opens plain sessions on it: his tables are his alone, the desk
walls (``DeskWallGuard``) protect the desks' tables, which nothing here maps or touches.

What the database itself refuses (``create_schema`` triggers; the ORM has no say):

* ``moona_wallet_auth``, ``moona_wallet_entry``, ``moona_journal`` and ``moona_note`` are
  append-only: UPDATE, DELETE and any REPLACE/upsert raise.
* ``moona_life`` rows are never deleted or replaced; ``status`` only moves ``alive`` → ``dead``;
  ``died_at``, ``cause``, ``last_words`` and ``shortfall`` are set once while NULL; ``name``,
  ``born_at``, ``seed`` and ``currency`` are frozen at INSERT. Death is a one-way transition
  the database enforces (docs/MOONA.md §2).
* ``moona_job`` rows are never deleted; ``status`` only moves forward; the price and the request
  are frozen at INSERT.
* A wallet entry's sign follows its kind (seed and income positive, every cost negative), and a
  ``ref`` (an authorization or a receipt) is UNIQUE, so the same authorization or the same
  receipt can never land twice.

Ids are ULIDs from the caller's ``IdGenerator``; timestamps come from the process clock
(DESIGN §3.2; ``set_process_clock`` in tests and in ``moona simulate``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from nour.core.clock import process_now
from nour.core.types import Ulid
from nour.db.base import NAMING_CONVENTION, Base, MoneyCol, Scope, UtcDateTime
from nour.db.engine import WRITER_OPTION, create_schema, make_engine

LIFE_STATUSES: tuple[str, ...] = ("alive", "dead")
DEATH_CAUSES: tuple[str, ...] = ("starved", "killed")
ENTRY_KINDS: tuple[str, ...] = ("seed", "income", "spend", "model_cost", "upkeep")
CREDIT_KINDS: tuple[str, ...] = ("seed", "income")
JOB_STATUSES: tuple[str, ...] = ("bid", "accepted", "delivered", "invoiced", "paid")
JOURNAL_ACTORS: tuple[str, ...] = ("subagent", "owner", "system")
JOURNAL_PHASES: tuple[str, ...] = ("opened", "closed")

APPEND_ONLY_MOONA_TABLES: frozenset[str] = frozenset(
    {"moona_wallet_auth", "moona_wallet_entry", "moona_journal", "moona_note"}
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class MoonaBase(Base):
    """Abstract: Moona's mappers share ``Base``'s validation and registry but not its metadata."""

    __abstract__ = True
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class _StampedMixin:
    """``id`` (caller ULID) and ``created_at`` for append-only rows (no ``updated_at``: nothing
    updates them)."""

    id: Mapped[Ulid] = mapped_column(String(26), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)


# --------------------------------------------------------------------------- life


class LifeRow(MoonaBase):
    """One life. Exactly one row per store (``Life.birth`` refuses a second); its death columns
    are single-transition and its status forward-only, so a dead Moona stays dead."""

    __tablename__ = "moona_life"
    __scope__ = Scope.SHARED
    __single_transition__ = ("died_at", "cause", "last_words", "shortfall")
    __forward_only__ = {"status": list(LIFE_STATUSES)}
    __immutable__ = ("name", "born_at", "seed", "currency")
    __table_args__ = (
        CheckConstraint(_in("status", LIFE_STATUSES), name="status"),
        CheckConstraint(f"cause IS NULL OR {_in('cause', DEATH_CAUSES)}", name="cause"),
        CheckConstraint(
            "(status = 'alive' AND died_at IS NULL AND cause IS NULL) "
            "OR (status = 'dead' AND died_at IS NOT NULL AND cause IS NOT NULL)",
            name="death_consistent",
        ),
        CheckConstraint("seed > 0", name="seed_positive"),
        CheckConstraint("ticks >= 0", name="ticks"),
    )

    id: Mapped[Ulid] = mapped_column(String(26), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    born_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    seed: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="alive")
    frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    awake_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    ticks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_tick_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_upkeep_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    died_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cause: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_words: Mapped[str | None] = mapped_column(Text, nullable=True)
    shortfall: Mapped[int | None] = mapped_column(MoneyCol, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=process_now, onupdate=process_now
    )


# --------------------------------------------------------------------------- wallet


class WalletAuthRow(MoonaBase, _StampedMixin):
    """Every decision ``Wallet.authorize`` made, approved or declined (the ``card_authorization``
    of his wallet): ``auth_ref`` is UNIQUE and is what ``Wallet.debit`` checks against."""

    __tablename__ = "moona_wallet_auth"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (
        UniqueConstraint("auth_ref"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "(approved = 1 AND decline_reason IS NULL) OR (approved = 0 AND decline_reason IS NOT NULL)",
            name="decision_consistent",
        ),
    )

    life_id: Mapped[Ulid] = mapped_column(String(26), nullable=False, index=True)
    auth_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    merchant: Mapped[str] = mapped_column(String(200), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    decline_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    balance_before: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    audit_id: Mapped[Ulid] = mapped_column(String(26), nullable=False)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class WalletEntryRow(MoonaBase, _StampedMixin):
    """One movement of money. The balance is the sum of ``amount`` over a life; a credit is
    positive and carries the receipt ``ref``, a spend is negative and carries the ``auth_ref``,
    a cost (model call, upkeep) is negative with no ref. ``shortfall`` on a cost is what could
    not be paid: the amount that killed him."""

    __tablename__ = "moona_wallet_entry"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (
        UniqueConstraint("ref"),
        CheckConstraint(_in("kind", ENTRY_KINDS), name="kind"),
        CheckConstraint(
            f"({_in('kind', CREDIT_KINDS)} AND amount > 0) "
            f"OR (NOT {_in('kind', CREDIT_KINDS)} AND amount < 0)",
            name="sign_follows_kind",
        ),
        CheckConstraint("shortfall >= 0", name="shortfall"),
    )

    life_id: Mapped[Ulid] = mapped_column(String(26), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    counterpart: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    shortfall: Mapped[int] = mapped_column(MoneyCol, nullable=False, default=0)
    audit_id: Mapped[Ulid | None] = mapped_column(String(26), nullable=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


# --------------------------------------------------------------------------- jobs


class JobRow(MoonaBase):
    """A request he bid for: his own record of what he owes and is owed. ``price`` is the agreed
    amount and the only amount an invoice may carry (never a tool argument)."""

    __tablename__ = "moona_job"
    __scope__ = Scope.SHARED
    __single_transition__ = ("payment_ref", "closed_at", "closed_reason")
    __forward_only__ = {"status": list(JOB_STATUSES)}
    __immutable__ = ("life_id", "request_id", "client", "title", "price", "currency")
    __table_args__ = (
        UniqueConstraint("life_id", "request_id"),
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    id: Mapped[Ulid] = mapped_column(String(26), primary_key=True)
    life_id: Mapped[Ulid] = mapped_column(String(26), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    client: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    price: Mapped[int] = mapped_column(MoneyCol, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="bid")
    payment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=process_now, onupdate=process_now
    )


# --------------------------------------------------------------------------- journal and notes


class JournalRow(MoonaBase):
    """His audit log (SPEC §12 columns in miniature): two rows per action (``opened`` before the
    side effect, ``closed`` after), hash-chained through ``prev_hash``/``entry_hash``, with
    ``actor`` ``subagent`` for everything he does, ``owner`` for a kill or a freeze, ``system``
    for upkeep, settlement and death."""

    __tablename__ = "moona_journal"
    __scope__ = Scope.SHARED
    __append_only__ = True
    __table_args__ = (
        UniqueConstraint("id"),
        UniqueConstraint("entry_hash"),
        UniqueConstraint("invocation_id", "phase"),
        CheckConstraint(_in("actor", JOURNAL_ACTORS), name="actor"),
        CheckConstraint(_in("phase", JOURNAL_PHASES), name="phase"),
        CheckConstraint("(amount IS NULL) = (currency IS NULL)", name="money_pair"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[Ulid] = mapped_column(String(26), nullable=False)
    life_id: Mapped[Ulid] = mapped_column(String(26), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    invocation_id: Mapped[Ulid] = mapped_column(String(26), nullable=False)
    amount: Mapped[int | None] = mapped_column(MoneyCol, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    counterpart: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    config_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)


class NoteRow(MoonaBase, _StampedMixin):
    """His episodic memory (SPEC §8 "write to episodic memory freely"): one note per
    ``memory.note`` call, shown back to him on later ticks, never edited."""

    __tablename__ = "moona_note"
    __scope__ = Scope.SHARED
    __append_only__ = True

    life_id: Mapped[Ulid] = mapped_column(String(26), nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    audit_id: Mapped[Ulid] = mapped_column(String(26), nullable=False)


# --------------------------------------------------------------------------- opening the store


def open_store(url: str) -> Engine:
    """A writer engine over ``url`` with Moona's schema and triggers installed (idempotent)."""
    engine = make_engine(url)
    create_schema(engine, MoonaBase.metadata)
    return engine


@contextmanager
def unit_of_work(engine: Engine) -> Iterator[Session]:
    """One transaction: commit on a clean exit, roll back on an exception. Writers take the
    SQLite reserved lock up front (``BEGIN IMMEDIATE``) like the desks' ``SessionFactory``."""
    bound = engine.execution_options(**{WRITER_OPTION: True})
    with Session(bound, expire_on_commit=False, autoflush=True) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


@contextmanager
def reading(engine: Engine) -> Iterator[Session]:
    """A read-only look: rolled back at the end, nothing is ever flushed."""
    with Session(engine, expire_on_commit=False, autoflush=False) as session:
        try:
            yield session
        finally:
            session.expunge_all()  # detach with attributes loaded; a rollback would expire them
            session.rollback()
