"""Declarative base, scope markers, mixins and column types (DESIGN §3.7 ``nour/db/base.py``;
SPEC §5 §11 §13 §15).

Every mapper declares four class variables the walls read:

* ``__scope__`` (:class:`Scope`): who may read and write the table (``scope_allows``); the
  ``DeskWallGuard`` enforces it per session on SQLite and ``pg_roles_ddl`` mirrors it with grants
  and row-level security on Postgres (SPEC §5 separation).
* ``__append_only__``: UPDATE/DELETE refused by a DB trigger on both dialects and by the ORM
  listener (SPEC §12 audit log).
* ``__single_transition__``: columns that may be set once while NULL (``approval.decided_at``).
* ``__forward_only__``: status columns with an ordered state list (``transaction.status``).

They are copied into ``Table.info["nour"]`` when the class is created (``Base.__init_subclass__``)
so ``nour.db.engine`` can generate DDL from a bare ``MetaData`` — the same path the alembic
migration uses — without importing the mapper classes.

A class whose markers contradict its columns (a ``DESK_ROW`` mapper without ``desk``, a marker
naming an unknown column) raises ``TypeError`` and is **un-mapped first**: by the time the columns
can be inspected, ``DeclarativeBase.__init_subclass__`` has already instrumented the class,
registered its mapper and added its ``Table`` to the metadata, and a refused mapper left behind
is shared state — ``DeskWallGuard`` registers the desk criteria for every ``DESK_ROW`` mapper of
the registry, so one ``DESK_ROW`` class without a ``desk`` column would break every statement of
every session until the cyclic garbage collector happened to collect it. ``_unmap`` does per
class what ``registry.dispose()`` does for all of them (pop the class manager from the registry,
dispose the mapper, release the class name, uninstrument the class) and removes every table the
class added to its ``MetaData``.

Timestamps: ``RecordMixin.created_at`` / ``updated_at`` default to ``nour.core.clock.process_now``
(the injected clock, DESIGN §3.2) and are stored through :class:`UtcDateTime`, which refuses
naive values and hands back tz-aware UTC on both dialects (SQLite has no timezone storage).
Ids are never defaulted: every ``id`` is a ULID the caller took from ``IdGenerator``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import BigInteger, DateTime, Enum, LargeBinary, MetaData, String, Table, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from nour.core.clock import process_now
from nour.core.errors import Tier2LeakError
from nour.core.tokens import AuditorToken, DeskToken, require_minted
from nour.core.types import CoatId, Desk, Ulid

TABLE_INFO_KEY = "nour"
"""``Table.info[TABLE_INFO_KEY]`` holds ``{"scope", "append_only", "single_transition",
"forward_only"}`` for every mapped table (see :class:`Base`)."""

MAPPER_INFO_KEY = "nour_mapper"
"""``Table.info[MAPPER_INFO_KEY]`` is the mapped class, so ``DeskWallGuard`` can find the entity
behind any ``Table`` a statement touches (``select(func.count()).select_from(Model)`` names no
entity in ``ORMExecuteState.all_mappers`` yet must still be scoped and partitioned)."""

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Scope(StrEnum):
    """§5 wall marker on every mapper (`__scope__`)."""

    SHARED = "shared"
    DESK_ROW = "desk_row"
    ASSISTANT_ONLY = "assistant_only"
    OPERATOR_ONLY = "operator_only"
    GOVERNANCE_ONLY = "governance_only"
    AUDITOR_WRITE = "auditor_write"


class Base(DeclarativeBase):
    """Declarative base of every Nour table (DESIGN §3.7)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    __scope__: ClassVar[Scope] = Scope.SHARED
    __append_only__: ClassVar[bool] = False  # DB trigger + ORM listener
    __single_transition__: ClassVar[tuple[str, ...]] = ()  # columns settable once while NULL
    __forward_only__: ClassVar[dict[str, list[str]]] = {}  # status columns, ordered state list

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """§5: check the wall markers against the real columns once the class is mapped (the
        only moment they exist); a refused class is un-mapped before the ``TypeError`` leaves
        (module docstring), so the registry and the metadata never carry it."""
        metadata = cls.metadata  # the class's own MetaData when an abstract parent declared one
        tables_before = frozenset(metadata.tables)
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("__abstract__", False):
            return
        table = getattr(cls, "__table__", None)
        if table is None:
            return
        try:
            flags = _validate_markers(cls, table)
        except TypeError:
            _unmap(cls, metadata, tables_before)
            raise
        table.info[TABLE_INFO_KEY] = flags
        table.info[MAPPER_INFO_KEY] = cls


def _validate_markers(cls: type[Base], table: Table) -> dict[str, Any]:
    """The ``Table.info[TABLE_INFO_KEY]`` entry of ``cls``, or ``TypeError`` when a marker
    contradicts the columns: a ``DESK_ROW`` mapper without ``desk`` (the wall could not
    partition it), a single-transition or forward-only marker naming an unknown column, a
    forward-only order with fewer than two distinct states."""
    scope = Scope(cls.__scope__)
    columns = set(table.columns.keys())
    if scope is Scope.DESK_ROW and "desk" not in columns:
        raise TypeError(f"{cls.__name__}: a DESK_ROW mapper needs a `desk` column (DeskMixin)")
    for name in cls.__single_transition__:
        if name not in columns:
            raise TypeError(f"{cls.__name__}: __single_transition__ names unknown column {name!r}")
    for name, order in cls.__forward_only__.items():
        if name not in columns:
            raise TypeError(f"{cls.__name__}: __forward_only__ names unknown column {name!r}")
        if len(order) < 2 or len(set(order)) != len(order):
            raise TypeError(
                f"{cls.__name__}: __forward_only__[{name!r}] needs >= 2 distinct states"
            )
    return {
        "scope": scope,
        "append_only": bool(cls.__append_only__),
        "single_transition": tuple(cls.__single_transition__),
        "forward_only": {k: list(v) for k, v in cls.__forward_only__.items()},
    }


def _unmap(cls: type[Base], metadata: MetaData, tables_before: frozenset[str]) -> None:
    """Undo the declarative mapping of a class ``_validate_markers`` refused (module docstring).

    The per-class half of ``registry.dispose()``: the class manager leaves ``registry._managers``
    (so ``registry.mappers`` — what ``DeskWallGuard`` partitions — no longer lists the mapper,
    whether or not the class has been garbage-collected), the mapper is flagged disposed, the
    class name is released for string lookups and the class is uninstrumented. Every table the
    class added to ``metadata`` (``tables_before`` is the key set from before the mapping) is
    removed, so ``create_schema`` never creates it and the name can be declared again.
    """
    mapper = inspect(cls, raiseerr=False)
    if isinstance(mapper, Mapper):
        manager = mapper.class_manager
        registry = cls.registry
        registry._managers.pop(manager, None)
        registry._dispose_manager_and_mapper(manager)
    for key in set(metadata.tables) - tables_before:
        metadata.remove(metadata.tables[key])


def scope_of(mapped_class: type[Any]) -> Scope:
    """The ``__scope__`` of a mapped class (SHARED when it declares none)."""
    return Scope(getattr(mapped_class, "__scope__", Scope.SHARED))


# --------------------------------------------------------------------------- column types


class UtcDateTime(TypeDecorator[datetime]):
    """``DateTime(timezone=True)`` that refuses naive values and always returns tz-aware UTC.

    SQLite stores no offset, so a value is normalised to UTC before it is written and the UTC
    zone re-attached when it is read; Postgres ``timestamptz`` round-trips as is.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected a datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("naive datetime refused: every Nour timestamp is tz-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Ciphertext(bytes):
    """Minted only by FieldCipher.encrypt; EncryptedBytes refuses any other bytes/str, so plaintext cannot be
    written through the ORM (§11 §13)."""

    __slots__ = ()


class EncryptedBytes(TypeDecorator[bytes]):
    """``LargeBinary`` column that accepts :class:`Ciphertext` only (§11 §13).

    ``process_bind_param`` raises ``Tier2LeakError`` for a plain ``bytes``/``str``: there is no
    way to put plaintext into an encrypted column through the ORM, and the error names the column
    type, never the value.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, Ciphertext):
            raise Tier2LeakError(
                "EncryptedBytes accepts Ciphertext minted by FieldCipher.encrypt only, "
                f"got {type(value).__name__}"
            )
        return bytes(value)

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> Ciphertext | None:
        if value is None:
            return None
        return Ciphertext(value)


JSONCol = JSON().with_variant(JSONB(), "postgresql")
"""§5: ``JSON`` everywhere, ``JSONB`` on Postgres."""

MoneyCol = BigInteger
"""§5 §10: fils as ``BIGINT``; the currency sits in a sibling ``TEXT`` column."""

DeskCol = Enum(
    Desk,
    name="desk",
    native_enum=False,
    create_constraint=True,
    length=16,
    validate_strings=True,
    values_callable=lambda enum: [member.value for member in enum],
)
"""``desk TEXT CHECK (desk IN ('operator','assistant','governance'))`` (§5)."""


# --------------------------------------------------------------------------- mixins


class RecordMixin:
    """§15: ``id`` (caller-supplied ULID), ``created_at``/``updated_at`` from the process clock."""

    id: Mapped[Ulid] = mapped_column(String(26), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=process_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=process_now, onupdate=process_now
    )


class DeskMixin:
    """§5: the desk a row belongs to; DESK_ROW mappers are partitioned on it."""

    desk: Mapped[Desk] = mapped_column(DeskCol, nullable=False, index=True)


class CoatMixin:
    """§5 §15: the coat a row belongs to (``NULL`` for desk-wide rows).

    No foreign key here: the ``coat`` table is a wave-1 mapper and a mixin FK would make every
    test metadata depend on it. ``nour/db/models.py`` adds
    ``ForeignKeyConstraint(["coat_id"], ["coat.id"])`` in ``__table_args__`` where §5 says
    ``REFERENCES coat(id)``.
    """

    coat_id: Mapped[CoatId | None] = mapped_column(String(64), nullable=True, index=True)


# --------------------------------------------------------------------------- scope table


_READ: dict[Desk, frozenset[Scope]] = {
    Desk.OPERATOR: frozenset(
        {Scope.SHARED, Scope.DESK_ROW, Scope.OPERATOR_ONLY, Scope.GOVERNANCE_ONLY}
    ),
    Desk.ASSISTANT: frozenset(
        {Scope.SHARED, Scope.DESK_ROW, Scope.ASSISTANT_ONLY, Scope.GOVERNANCE_ONLY}
    ),
    Desk.GOVERNANCE: frozenset({Scope.SHARED, Scope.DESK_ROW, Scope.GOVERNANCE_ONLY}),
}
_WRITE: dict[Desk, frozenset[Scope]] = {
    Desk.OPERATOR: frozenset({Scope.SHARED, Scope.DESK_ROW, Scope.OPERATOR_ONLY}),
    Desk.ASSISTANT: frozenset({Scope.SHARED, Scope.DESK_ROW, Scope.ASSISTANT_ONLY}),
    Desk.GOVERNANCE: frozenset({Scope.SHARED, Scope.DESK_ROW, Scope.GOVERNANCE_ONLY}),
}
_AUDITOR_READ: frozenset[Scope] = frozenset({Scope.SHARED, Scope.AUDITOR_WRITE})
_AUDITOR_WRITE: frozenset[Scope] = frozenset({Scope.AUDITOR_WRITE})


def scope_allows(scope: Scope, token: DeskToken | AuditorToken, *, write: bool) -> bool:
    """§5 §13: may ``token`` read (or, with ``write=True``, write) a table of ``scope``?

    Desks read and write SHARED, their own DESK_ROW partition and their own ``*_ONLY`` scope,
    and read GOVERNANCE_ONLY (owner number, second channel, quiet hours; Postgres narrows that
    to a column grant). Governance reads/writes SHARED, DESK_ROW (its own, empty partition) and
    GOVERNANCE_ONLY. The auditor reads SHARED and its own AUDITOR_WRITE scope and may write
    AUDITOR_WRITE only (``auditor_report``, append-only).

    A token that ``mint`` did not produce (``object.__new__`` plus ``object.__setattr__`` passes
    ``isinstance``) raises ``AuthError``: nothing is ever allowed to it.
    """
    scope = Scope(scope)
    if not isinstance(token, DeskToken | AuditorToken):
        raise TypeError(
            f"scope_allows takes a DeskToken or AuditorToken, got {type(token).__name__}"
        )
    require_minted(token, "scope_allows")
    if isinstance(token, AuditorToken):
        return scope in (_AUDITOR_WRITE if write else _AUDITOR_READ)
    table = _WRITE if write else _READ
    return scope in table[token.desk]
