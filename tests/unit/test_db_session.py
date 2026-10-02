"""nour/db (DESIGN §3.7; SPEC §5 §11 §12 §13 §15): base, engine, session with a test-only mapper set.

Proves (MODULES.md "core"): ``DeskWallGuard`` raises on the other desk's scope, auto-filters
DESK_ROW, rejects desk mismatch on flush, rejects UPDATE/DELETE of append-only mappers at the
ORM level; ``create_schema`` installs triggers that make raw-SQL UPDATE/DELETE fail on SQLite;
single-transition and forward-only columns are enforced by both layers; ``AuditorToken``
sessions are read-only and ``write()`` raises; ``EncryptedBytes`` refuses plaintext; timestamps
come from the process clock; the Postgres DDL strings carry roles, revokes and RLS; a mapper
``Base`` refuses leaves nothing in the registry or the metadata, and a DESK_ROW mapper the wall
cannot partition fails closed.

The mappers live on ``WallBase`` — an abstract subclass of ``Base`` with its own ``MetaData`` —
so the shared ``Base.metadata`` stays untouched for wave 1's real models.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import (
    ForeignKey,
    MetaData,
    String,
    Table,
    alias,
    column,
    delete,
    event,
    exists,
    extract,
    func,
    insert,
    inspect,
    join,
    literal,
    literal_column,
    select,
    table,
    text,
    union_all,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError, SAWarning, StatementError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    aliased,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    subqueryload,
    with_parent,
)
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import quoted_name
from sqlalchemy.sql.base import Executable

from nour.core.clock import FakeClock, IdGenerator
from nour.core.errors import (
    AppendOnlyViolation,
    AuthError,
    DeskWallViolation,
    SingleTransitionViolation,
    Tier2LeakError,
)
from nour.core.tokens import AnyToken, AssistantToken, AuditorToken, DeskToken, OperatorToken
from nour.core.types import Desk, Ulid
from nour.db.base import (
    TABLE_INFO_KEY,
    Base,
    Ciphertext,
    CoatMixin,
    DeskMixin,
    EncryptedBytes,
    JSONCol,
    MoneyCol,
    RecordMixin,
    Scope,
    UtcDateTime,
    scope_allows,
    scope_of,
)
from nour.db.engine import (
    ALL_ROLES,
    ASSISTANT_ROLE,
    AUDITOR_ROLE,
    OPERATOR_ROLE,
    append_only_ddl,
    create_schema,
    forward_only_ddl,
    install_sqlite_schema_translation,
    is_read_only,
    make_engine,
    pg_roles_ddl,
    pg_schema_ddl,
    single_transition_ddl,
    trigger_ddl,
)
from nour.db.session import (
    READER_INFO_KEY,
    REPORT_INFO_KEY,
    DeskWallGuard,
    GuardedSession,
    SessionFactory,
)

# --------------------------------------------------------------------------- test-only mappers


class WallBase(Base):
    __abstract__ = True
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)


class AssistantOnlyRow(WallBase, RecordMixin):
    """Stands in for `document` / `vault_secret` (ASSISTANT_ONLY)."""

    __tablename__ = "wall_assistant_only"
    __scope__ = Scope.ASSISTANT_ONLY
    note: Mapped[str] = mapped_column(String(64))


class DeskRowNote(WallBase, RecordMixin, DeskMixin, CoatMixin):
    """Stands in for `memory_record` / `contact` (DESK_ROW)."""

    __tablename__ = "wall_desk_row"
    __scope__ = Scope.DESK_ROW
    note: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONCol, nullable=True)


class SharedLog(WallBase, RecordMixin):
    """Stands in for `audit_event` (SHARED, append-only)."""

    __tablename__ = "wall_shared_log"
    __scope__ = Scope.SHARED
    __append_only__ = True
    note: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int | None] = mapped_column(MoneyCol, nullable=True)


class Ticket(WallBase, RecordMixin):
    """Stands in for `approval` (single transition) and `transaction` (forward-only status)."""

    __tablename__ = "wall_ticket"
    __scope__ = Scope.SHARED
    __single_transition__ = ("decided_at",)
    __forward_only__ = {"status": ["prepared", "released", "settled"]}
    status: Mapped[str] = mapped_column(String(16), default="prepared")
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class SharedNote(WallBase, RecordMixin):
    """Stands in for a mutable SHARED table such as `coat` (no transition markers)."""

    __tablename__ = "wall_shared_note"
    __scope__ = Scope.SHARED
    note: Mapped[str] = mapped_column(String(64))


class SecretRow(WallBase, RecordMixin):
    """Stands in for `beneficiary.bank_details_ct` (EncryptedBytes)."""

    __tablename__ = "wall_secret"
    __scope__ = Scope.ASSISTANT_ONLY
    blob: Mapped[bytes | None] = mapped_column(EncryptedBytes, nullable=True)


class GovernanceRow(WallBase, RecordMixin):
    """Stands in for `owner` (GOVERNANCE_ONLY)."""

    __tablename__ = "wall_governance"
    __scope__ = Scope.GOVERNANCE_ONLY
    whatsapp_number: Mapped[str] = mapped_column(String(32))
    passphrase_hash: Mapped[str] = mapped_column(String(128), default="", deferred=True)


class OperatorOnlyRow(WallBase, RecordMixin):
    """Stands in for `experiment` (OPERATOR_ONLY)."""

    __tablename__ = "wall_operator_only"
    __scope__ = Scope.OPERATOR_ONLY
    note: Mapped[str] = mapped_column(String(64))


class AuditorReportRow(WallBase, RecordMixin):
    """Stands in for `auditor_report` (AUDITOR_WRITE, append-only)."""

    __tablename__ = "wall_auditor_report"
    __scope__ = Scope.AUDITOR_WRITE
    __append_only__ = True
    summary: Mapped[str] = mapped_column(String(64))


class DeskParent(WallBase, RecordMixin, DeskMixin):
    """Stands in for `conversation` (DESK_ROW) with its `message` rows behind a relationship."""

    __tablename__ = "wall_desk_parent"
    __scope__ = Scope.DESK_ROW
    title: Mapped[str] = mapped_column(String(64))
    children: Mapped[list[DeskChild]] = relationship(back_populates="parent")


class DeskChild(WallBase, RecordMixin, DeskMixin):
    """A DESK_ROW row that points at a DESK_ROW parent (eager-joined by mapper configuration,
    the one load the hook cannot see as a statement of its own) and at an ASSISTANT_ONLY row."""

    __tablename__ = "wall_desk_child"
    __scope__ = Scope.DESK_ROW
    parent_id: Mapped[Ulid | None] = mapped_column(
        String(26), ForeignKey("wall_desk_parent.id"), nullable=True
    )
    secret_id: Mapped[Ulid | None] = mapped_column(
        String(26), ForeignKey("wall_assistant_only.id"), nullable=True
    )
    body: Mapped[str] = mapped_column(String(64))
    parent: Mapped[DeskParent | None] = relationship(back_populates="children", lazy="joined")
    secret: Mapped[AssistantOnlyRow | None] = relationship()


class ForeignBase(DeclarativeBase):
    """A declarative base that is not ``Base``: its classes skip ``Base.__init_subclass__`` and
    live in a registry of their own, so nothing here touches the other mappers."""

    metadata = MetaData()


class ForeignDeskRow(ForeignBase):
    """Claims DESK_ROW without a desk column — the shape ``Base`` refuses — mapped around it."""

    __tablename__ = "foreign_desk_row"
    __scope__ = Scope.DESK_ROW
    id: Mapped[str] = mapped_column(String(26), primary_key=True)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def wall_engine(engine: Engine) -> Engine:
    create_schema(engine, WallBase.metadata)
    return engine


@pytest.fixture
def sf(
    wall_engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> dict[str, SessionFactory]:
    return {kind: session_factory(token) for kind, token in tokens.items()}


@pytest.fixture
def seeded(sf: dict[str, SessionFactory], idgen: IdGenerator) -> dict[str, list[Ulid]]:
    """Two DESK_ROW notes per desk, one SharedLog row, one Ticket, one GovernanceRow."""
    ids: dict[str, list[Ulid]] = {
        "operator": [],
        "assistant": [],
        "log": [],
        "ticket": [],
        "gov": [],
    }
    for kind in ("operator", "assistant"):
        with sf[kind].write() as s:
            for n in range(2):
                uid = idgen.new()
                ids[kind].append(uid)
                s.add(DeskRowNote(id=uid, desk=Desk(kind), note=f"{kind}-{n}"))
    with sf["operator"].write() as s:
        log_id = idgen.new()
        s.add(SharedLog(id=log_id, note="opened", amount=12345))
        ids["log"].append(log_id)
        ticket_id = idgen.new()
        s.add(Ticket(id=ticket_id))
        ids["ticket"].append(ticket_id)
    with sf["governance"].write() as s:
        gov_id = idgen.new()
        s.add(GovernanceRow(id=gov_id, whatsapp_number="+971500000001", passphrase_hash="argon2$x"))
        ids["gov"].append(gov_id)
    return ids


@pytest.fixture
def family(sf: dict[str, SessionFactory], idgen: IdGenerator) -> dict[str, dict[str, Ulid]]:
    """Per desk one DeskParent with one DeskChild; the assistant child also points at an
    AssistantOnlyRow. Returns ``{kind: {"parent": id, "child": id}}`` plus ``{"secret": id}``."""
    ids: dict[str, dict[str, Ulid]] = {}
    secret_id = idgen.new()
    with sf["assistant"].write() as s:
        s.add(AssistantOnlyRow(id=secret_id, note="vault"))
    for kind in ("operator", "assistant"):
        with sf[kind].write() as s:
            parent = DeskParent(id=idgen.new(), desk=Desk(kind), title=f"{kind}-parent")
            child = DeskChild(
                id=idgen.new(),
                desk=Desk(kind),
                parent=parent,
                body=f"{kind}-child",
                secret_id=secret_id if kind == "assistant" else None,
            )
            s.add_all([parent, child])
            s.flush()
            ids[kind] = {"parent": parent.id, "child": child.id}
    ids["assistant"]["secret"] = secret_id
    return ids


# --------------------------------------------------------------------------- base


def test_base_records_flags_in_table_info() -> None:
    info = SharedLog.__table__.info[TABLE_INFO_KEY]  # type: ignore[attr-defined]
    assert info == {
        "scope": Scope.SHARED,
        "append_only": True,
        "single_transition": (),
        "forward_only": {},
        "immutable": (),
        "desk_partitioned": False,
    }
    ticket = Ticket.__table__.info[TABLE_INFO_KEY]  # type: ignore[attr-defined]
    assert ticket["single_transition"] == ("decided_at",)
    assert ticket["forward_only"] == {"status": ["prepared", "released", "settled"]}
    assert scope_of(DeskRowNote) is Scope.DESK_ROW and scope_of(object) is Scope.SHARED
    assert (
        "wall_desk_row" in WallBase.metadata.tables and "wall_desk_row" not in Base.metadata.tables
    )
    assert Base.__scope__ is Scope.SHARED and Base.__append_only__ is False
    assert Base.__single_transition__ == () and Base.__forward_only__ == {}
    assert Base.__immutable__ == () and Base.__desk_partitioned__ is False


def _refused_class(excinfo: pytest.ExceptionInfo[TypeError]) -> type[Any]:
    """The class whose creation raised, taken from the ``Base.__init_subclass__`` frame of the
    traceback: a strong reference keeps it alive for the test (its own reference cycles would
    anyway, until a collection ran), so the registry checks cannot pass by GC timing."""
    tb = excinfo.value.__traceback__
    while tb is not None:
        candidate = tb.tb_frame.f_locals.get("cls")
        if isinstance(candidate, type) and issubclass(candidate, WallBase):
            return candidate
        tb = tb.tb_next
    raise AssertionError("no Base.__init_subclass__ frame in the traceback")


def _registered_classes() -> set[type[Any]]:
    return {mapper.class_ for mapper in WallBase.registry.mappers}


def test_base_refuses_inconsistent_mappers() -> None:
    """A refused class leaves nothing behind: ``DeclarativeBase`` had already mapped it when the
    columns became inspectable, and ``Base`` un-maps it before raising — no mapper in the (one,
    shared) registry, no instrumentation on the class, no table in the metadata."""
    with pytest.raises(TypeError, match="desk") as no_desk:

        class _NoDesk(WallBase, RecordMixin):
            __tablename__ = "wall_bad_desk_row"
            __scope__ = Scope.DESK_ROW

    with pytest.raises(TypeError, match="unknown column") as bad_transition:

        class _BadTransition(WallBase, RecordMixin):
            __tablename__ = "wall_bad_transition"
            __single_transition__ = ("nope",)

    with pytest.raises(TypeError, match="distinct states") as bad_order:

        class _BadOrder(WallBase, RecordMixin):
            __tablename__ = "wall_bad_order"
            __forward_only__ = {"status": ["a"]}
            status: Mapped[str] = mapped_column(String(8))

    refused = [_refused_class(info) for info in (no_desk, bad_transition, bad_order)]
    assert [cls.__name__ for cls in refused] == ["_NoDesk", "_BadTransition", "_BadOrder"]
    registered = _registered_classes()
    for cls in refused:
        assert cls not in registered
        assert inspect(cls, raiseerr=False) is None  # uninstrumented: not a mapped class
    for name in ("wall_bad_desk_row", "wall_bad_transition", "wall_bad_order"):
        assert name not in WallBase.metadata.tables
    assert {DeskRowNote, SharedLog, Ticket} <= registered  # the real mappers were left alone


def test_a_refused_desk_row_class_does_not_poison_the_wall(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator
) -> None:
    """The desk criteria are registered for every DESK_ROW mapper of the registry, so a refused
    DESK_ROW class that stayed registered (as it did until the cyclic GC collected it) broke
    every DESK_ROW statement of every session with ``AttributeError: _Stray has no attribute
    desk``. With the class alive and referenced throughout, both partitions keep working."""
    with pytest.raises(TypeError, match="desk") as excinfo:

        class _Stray(WallBase, RecordMixin):
            __tablename__ = "wall_stray_desk_row"
            __scope__ = Scope.DESK_ROW

    stray = _refused_class(excinfo)
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]
    with sf["operator"].session() as s:
        assert (
            s.scalars(select(DeskRowNote.note).where(DeskRowNote.note.like("assistant%"))).all()
            == []
        )
        assert s.scalar(select(func.count()).select_from(DeskRowNote)) == 2
    with sf["operator"].write() as s:
        s.add(DeskRowNote(id=idgen.new(), desk=Desk.OPERATOR, note="after"))
    assert _partition(sf, "operator") == ["after", "operator-0", "operator-1"]
    assert (
        stray not in _registered_classes() and "wall_stray_desk_row" not in WallBase.metadata.tables
    )


def test_a_desk_row_mapper_without_a_desk_column_fails_closed(
    sf: dict[str, SessionFactory],
) -> None:
    """A DESK_ROW mapper the wall cannot partition — mapped around ``Base``, which would have
    refused it — is refused by name instead of queried against an open partition; mappers of
    other registries are untouched by it."""
    for kind in ("operator", "assistant", "governance"):
        with sf[kind].session() as s, pytest.raises(DeskWallViolation, match="ForeignDeskRow"):
            s.execute(select(ForeignDeskRow))
    with sf["operator"].session() as s:
        assert s.scalars(select(DeskRowNote)).all() == []


def test_desk_column_has_a_check_constraint(wall_engine: Engine) -> None:
    ddl = str(CreateTable(cast(Table, DeskRowNote.__table__)).compile(wall_engine))
    assert "CHECK (desk IN ('operator', 'assistant', 'governance'))" in ddl
    assert "ck_wall_desk_row_desk" in ddl


@pytest.mark.parametrize(
    ("kind", "scope", "read", "write"),
    [
        ("operator", Scope.SHARED, True, True),
        ("operator", Scope.DESK_ROW, True, True),
        ("operator", Scope.OPERATOR_ONLY, True, True),
        ("operator", Scope.ASSISTANT_ONLY, False, False),
        ("operator", Scope.GOVERNANCE_ONLY, True, False),
        ("operator", Scope.AUDITOR_WRITE, False, False),
        ("assistant", Scope.ASSISTANT_ONLY, True, True),
        ("assistant", Scope.OPERATOR_ONLY, False, False),
        ("assistant", Scope.GOVERNANCE_ONLY, True, False),
        ("governance", Scope.GOVERNANCE_ONLY, True, True),
        ("governance", Scope.ASSISTANT_ONLY, False, False),
        ("governance", Scope.SHARED, True, True),
        ("auditor", Scope.SHARED, True, False),
        ("auditor", Scope.DESK_ROW, False, False),
        ("auditor", Scope.ASSISTANT_ONLY, False, False),
        ("auditor", Scope.AUDITOR_WRITE, True, True),
    ],
)
def test_scope_allows_table(
    tokens: dict[str, AnyToken], kind: str, scope: Scope, read: bool, write: bool
) -> None:
    assert scope_allows(scope, tokens[kind], write=False) is read
    assert scope_allows(scope, tokens[kind], write=True) is write


def test_scope_allows_refuses_non_tokens() -> None:
    with pytest.raises(TypeError):
        scope_allows(Scope.SHARED, object(), write=False)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        DeskWallGuard("operator")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SessionFactory(None, "operator", None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- scopes through sessions


def test_operator_cannot_select_or_insert_assistant_only(
    sf: dict[str, SessionFactory], idgen: IdGenerator
) -> None:
    with sf["operator"].session() as s, pytest.raises(DeskWallViolation):
        s.scalars(select(AssistantOnlyRow)).all()
    with sf["operator"].session() as s, pytest.raises(DeskWallViolation):
        s.get(AssistantOnlyRow, "nope")
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        s.add(AssistantOnlyRow(id=idgen.new(), note="x"))
        s.flush()
    with sf["operator"].session() as s, pytest.raises(DeskWallViolation):
        s.execute(update(AssistantOnlyRow).values(note="y"))
    # the assistant may, and the governance / operator still may not read it afterwards
    with sf["assistant"].write() as s:
        s.add(AssistantOnlyRow(id=idgen.new(), note="vault"))
    with sf["assistant"].session() as s:
        assert [r.note for r in s.scalars(select(AssistantOnlyRow))] == ["vault"]
    with sf["governance"].session() as s, pytest.raises(DeskWallViolation):
        s.scalars(select(AssistantOnlyRow)).all()
    with sf["assistant"].session() as s, pytest.raises(DeskWallViolation):
        s.scalars(select(OperatorOnlyRow)).all()


def test_desk_row_is_partitioned_without_a_where(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]]
) -> None:
    with sf["operator"].session() as s:
        notes = s.scalars(select(DeskRowNote)).all()
        assert sorted(n.note for n in notes) == ["operator-0", "operator-1"]
        assert s.scalar(select(func.count()).select_from(DeskRowNote)) == 2
        assert s.get(DeskRowNote, seeded["assistant"][0]) is None
        assert s.get(DeskRowNote, seeded["operator"][0]) is not None
        assert s.scalars(select(DeskRowNote).where(DeskRowNote.desk == Desk.ASSISTANT)).all() == []
    with sf["assistant"].session() as s:
        assert sorted(n.note for n in s.scalars(select(DeskRowNote))) == [
            "assistant-0",
            "assistant-1",
        ]
    with sf["governance"].session() as s:
        assert s.scalars(select(DeskRowNote)).all() == []  # governance has its own, empty partition
    with sf["auditor"].session() as s, pytest.raises(DeskWallViolation):
        s.scalars(select(DeskRowNote)).all()


def test_desk_row_bulk_update_is_partitioned(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]]
) -> None:
    with sf["operator"].write() as s:
        s.execute(update(DeskRowNote).values(note="renamed"))
    with sf["assistant"].session() as s:
        assert sorted(n.note for n in s.scalars(select(DeskRowNote))) == [
            "assistant-0",
            "assistant-1",
        ]
    with sf["operator"].session() as s:
        assert {n.note for n in s.scalars(select(DeskRowNote))} == {"renamed"}


def test_flush_with_desk_mismatch_raises(
    sf: dict[str, SessionFactory], idgen: IdGenerator, seeded: dict[str, list[Ulid]]
) -> None:
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        s.add(DeskRowNote(id=idgen.new(), desk=Desk.ASSISTANT, note="smuggled"))
        s.flush()
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        s.add(DeskRowNote(id=idgen.new(), note="no desk"))
        s.flush()
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        row = s.get(DeskRowNote, seeded["operator"][0])
        assert row is not None
        row.desk = Desk.ASSISTANT
        s.flush()
    with sf["assistant"].session() as s:
        assert s.scalar(select(func.count()).select_from(DeskRowNote)) == 2


def test_write_rolls_back_on_exception_and_commits_on_exit(
    sf: dict[str, SessionFactory], idgen: IdGenerator
) -> None:
    with pytest.raises(RuntimeError), sf["operator"].write() as s:
        s.add(SharedLog(id=idgen.new(), note="lost"))
        s.flush()
        raise RuntimeError("boom")
    with sf["operator"].write() as s:
        s.add(SharedLog(id=idgen.new(), note="kept"))
    with sf["assistant"].session() as s:
        assert [r.note for r in s.scalars(select(SharedLog))] == ["kept"]


# --------------------------------------------------------------------------- append-only


def test_append_only_is_refused_at_the_orm_level(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]]
) -> None:
    log_id = seeded["log"][0]
    with pytest.raises(AppendOnlyViolation), sf["operator"].write() as s:
        row = s.get(SharedLog, log_id)
        assert row is not None
        row.note = "tampered"
        s.flush()
    with pytest.raises(AppendOnlyViolation), sf["operator"].write() as s:
        row = s.get(SharedLog, log_id)
        assert row is not None
        s.delete(row)
        s.flush()
    with sf["operator"].write() as s, pytest.raises(AppendOnlyViolation):
        s.execute(update(SharedLog).values(note="bulk"))
    with sf["operator"].write() as s, pytest.raises(AppendOnlyViolation):
        s.execute(delete(SharedLog))
    with sf["assistant"].session() as s:
        row = s.get(SharedLog, log_id)
        assert row is not None and row.note == "opened" and row.amount == 12345


_RAW_REPLACEMENTS = [
    "UPDATE wall_shared_log SET note = 'raw' WHERE id = ?",
    "DELETE FROM wall_shared_log WHERE id = ?",
    # SQLite's REPLACE conflict resolution deletes the old row without BEFORE DELETE firing
    # unless recursive_triggers is on; the BEFORE INSERT guard refuses it on any connection.
    "INSERT OR REPLACE INTO wall_shared_log (id, created_at, updated_at, note) "
    "VALUES (?, '2026-01-01', '2026-01-01', 'replaced')",
    "REPLACE INTO wall_shared_log (id, created_at, updated_at, note) "
    "VALUES (?, '2026-01-01', '2026-01-01', 'replaced')",
    "INSERT INTO wall_shared_log (id, created_at, updated_at, note) "
    "VALUES (?, '2026-01-01', '2026-01-01', 'upsert') ON CONFLICT (id) DO UPDATE SET note = 'upsert'",
]


@pytest.mark.parametrize("statement", _RAW_REPLACEMENTS, ids=lambda s: s.split(" ")[0] + s[6:12])
def test_append_only_is_refused_at_the_db_level(
    wall_engine: Engine, seeded: dict[str, list[Ulid]], statement: str
) -> None:
    log_id = seeded["log"][0]
    with pytest.raises(DBAPIError, match="append-only"), wall_engine.begin() as c:
        c.exec_driver_sql(statement, (log_id,))
    with wall_engine.connect() as c:
        assert c.exec_driver_sql("SELECT note FROM wall_shared_log").scalars().all() == ["opened"]


def test_append_only_replace_is_refused_even_without_recursive_triggers(
    wall_engine: Engine, seeded: dict[str, list[Ulid]]
) -> None:
    """Belt and braces: a connection that did not come from make_engine (no pragma) still cannot
    rewrite a row through INSERT OR REPLACE, because the BEFORE INSERT trigger aborts."""
    import sqlite3

    log_id = seeded["log"][0]
    raw = sqlite3.connect(wall_engine.url.database or "")
    try:
        assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "INSERT OR REPLACE INTO wall_shared_log (id, created_at, updated_at, note) "
                "VALUES (?, '2026-01-01', '2026-01-01', 'replaced')",
                (log_id,),
            )
        assert raw.execute("SELECT note FROM wall_shared_log").fetchall() == [("opened",)]
    finally:
        raw.close()


def test_every_connection_enables_recursive_triggers(wall_engine: Engine, tmp_db: Any) -> None:
    with wall_engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 1
    ro = make_engine(f"sqlite+pysqlite:///{tmp_db}", read_only=True)
    try:
        with ro.connect() as c:
            assert c.exec_driver_sql("PRAGMA recursive_triggers").scalar() == 1
    finally:
        ro.dispose()


def _sqlite_triggers(engine: Engine) -> set[str]:
    with engine.connect() as c:
        return {
            name
            for (name,) in c.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }


def test_append_only_triggers_are_installed(wall_engine: Engine) -> None:
    assert {
        "wall_shared_log_no_update",
        "wall_shared_log_no_delete",
        "wall_shared_log_no_replace",
        "wall_auditor_report_no_update",
        "wall_auditor_report_no_delete",
        "wall_auditor_report_no_replace",
    } <= _sqlite_triggers(wall_engine)


def test_create_schema_is_idempotent(wall_engine: Engine, seeded: dict[str, list[Ulid]]) -> None:
    before = _sqlite_triggers(wall_engine)
    create_schema(wall_engine, WallBase.metadata)
    create_schema(wall_engine, Base.metadata)  # empty metadata: nothing to do
    assert _sqlite_triggers(wall_engine) == before
    with wall_engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM wall_desk_row").scalar() == 4
        assert c.exec_driver_sql("SELECT note FROM wall_shared_log").scalar() == "opened"
    with pytest.raises(DBAPIError, match="append-only"), wall_engine.begin() as c:
        c.exec_driver_sql("UPDATE wall_shared_log SET note = 'raw'")


# --------------------------------------------------------------------------- single transition / forward only


def test_single_transition_column_sets_once(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], clock: FakeClock
) -> None:
    ticket_id = seeded["ticket"][0]
    with sf["operator"].write() as s:
        t = s.get(Ticket, ticket_id)
        assert t is not None and t.decided_at is None
        t.decided_at = clock.now()
    with pytest.raises(SingleTransitionViolation), sf["operator"].write() as s:
        t = s.get(Ticket, ticket_id)
        assert t is not None and t.decided_at == clock.now()
        t.decided_at = clock.now() + timedelta(hours=1)
        s.flush()
    with pytest.raises(SingleTransitionViolation), sf["operator"].write() as s:
        t = s.get(Ticket, ticket_id)
        assert t is not None
        t.decided_at = None
        s.flush()


def test_single_transition_is_enforced_by_the_trigger(
    wall_engine: Engine, seeded: dict[str, list[Ulid]]
) -> None:
    ticket_id = seeded["ticket"][0]
    with wall_engine.begin() as c:
        c.exec_driver_sql(
            "UPDATE wall_ticket SET decided_at = '2026-10-05 03:00:00' WHERE id = ?", (ticket_id,)
        )
    with pytest.raises(DBAPIError, match="may be set once"), wall_engine.begin() as c:
        c.exec_driver_sql(
            "UPDATE wall_ticket SET decided_at = '2026-10-05 04:00:00' WHERE id = ?", (ticket_id,)
        )
    with pytest.raises(DBAPIError, match="may be set once"), wall_engine.begin() as c:
        c.exec_driver_sql("UPDATE wall_ticket SET decided_at = NULL WHERE id = ?", (ticket_id,))
    with wall_engine.begin() as c:  # same value again is a no-op, not a violation
        c.exec_driver_sql(
            "UPDATE wall_ticket SET decided_at = '2026-10-05 03:00:00' WHERE id = ?", (ticket_id,)
        )


def test_forward_only_status(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], wall_engine: Engine
) -> None:
    ticket_id = seeded["ticket"][0]
    with sf["operator"].write() as s:
        t = s.get(Ticket, ticket_id)
        assert t is not None and t.status == "prepared"
        t.status = "released"
    with pytest.raises(SingleTransitionViolation, match="forward"), sf["operator"].write() as s:
        t = s.get(Ticket, ticket_id)
        assert t is not None
        t.status = "prepared"
        s.flush()
    with (
        pytest.raises(SingleTransitionViolation, match="unknown state"),
        sf["operator"].write() as s,
    ):
        t = s.get(Ticket, ticket_id)
        assert t is not None
        t.status = "bogus"
        s.flush()
    with pytest.raises(DBAPIError, match="forward"), wall_engine.begin() as c:
        c.exec_driver_sql("UPDATE wall_ticket SET status = 'prepared' WHERE id = ?", (ticket_id,))
    with pytest.raises(DBAPIError, match="forward"), wall_engine.begin() as c:
        c.exec_driver_sql("UPDATE wall_ticket SET status = 'bogus' WHERE id = ?", (ticket_id,))
    with wall_engine.begin() as c:
        c.exec_driver_sql("UPDATE wall_ticket SET status = 'settled' WHERE id = ?", (ticket_id,))
    with sf["assistant"].session() as s:
        t = s.get(Ticket, ticket_id)
        assert t is not None and t.status == "settled"


# --------------------------------------------------------------------------- auditor


def test_auditor_session_is_read_only_and_write_raises(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator
) -> None:
    auditor = sf["auditor"]
    assert isinstance(auditor.token, AuditorToken) and auditor.desk is None and auditor.is_auditor
    with pytest.raises(DeskWallViolation):
        with auditor.write():
            pass
    with auditor.session() as s:
        assert [r.note for r in s.scalars(select(SharedLog))] == ["opened"]
        assert s.get(Ticket, seeded["ticket"][0]) is not None
        s.add(SharedLog(id=idgen.new(), note="forged"))
        with pytest.raises(DeskWallViolation):
            s.flush()
    with auditor.session() as s:
        row = s.get(Ticket, seeded["ticket"][0])
        assert row is not None
        row.status = "released"
        with pytest.raises(DeskWallViolation):
            s.flush()
    with auditor.session() as s, pytest.raises(DeskWallViolation):
        s.scalars(select(AssistantOnlyRow)).all()
    with auditor.session() as s, pytest.raises(DeskWallViolation):
        s.scalars(select(GovernanceRow)).all()
    with auditor.session() as s, pytest.raises(DeskWallViolation):
        s.execute(delete(Ticket))
    with sf["operator"].session() as s:
        assert s.scalar(select(func.count()).select_from(SharedLog)) == 1


def test_auditor_may_append_its_own_report_only(
    sf: dict[str, SessionFactory], idgen: IdGenerator, seeded: dict[str, list[Ulid]]
) -> None:
    auditor = sf["auditor"]
    assert is_read_only(auditor.engine) and auditor.report_engine is not None
    with auditor.append_report() as s:
        s.add(AuditorReportRow(id=idgen.new(), summary="quiet day"))
    with auditor.session() as s:
        rows = s.scalars(select(AuditorReportRow)).all()
        assert [r.summary for r in rows] == ["quiet day"]
        rows[0].summary = "edited"
        with pytest.raises(DeskWallViolation):  # a reader never writes, append-only or not
            s.flush()
    # the report writer admits AUDITOR_WRITE inserts and nothing else
    with pytest.raises(DeskWallViolation, match="only INSERT"), auditor.append_report() as s:
        row = s.scalars(select(AuditorReportRow)).one()
        row.summary = "edited"
        s.flush()
    with pytest.raises(DeskWallViolation), auditor.append_report() as s:
        s.add(SharedLog(id=idgen.new(), note="forged"))
        s.flush()
    with pytest.raises(DeskWallViolation), auditor.append_report() as s:
        s.execute(update(Ticket).values(status="released"))
    with pytest.raises(DeskWallViolation), auditor.append_report() as s:
        s.execute(delete(AuditorReportRow))
    with pytest.raises(DeskWallViolation), auditor.append_report() as s:
        s.execute(text("INSERT INTO wall_ticket (id) VALUES ('x')"))
    with pytest.raises(DeskWallViolation), auditor.append_report() as s:
        s.scalars(select(AssistantOnlyRow)).all()
    with auditor.session() as s:
        assert [r.summary for r in s.scalars(select(AuditorReportRow))] == ["quiet day"]
        assert s.get(Ticket, seeded["ticket"][0]).status == "prepared"  # type: ignore[union-attr]
    for kind in ("operator", "assistant", "governance"):
        with sf[kind].session() as s, pytest.raises(DeskWallViolation):
            s.scalars(select(AuditorReportRow)).all()
        with pytest.raises(DeskWallViolation):
            with sf[kind].append_report():
                pass


def test_auditor_factory_needs_a_read_only_engine_and_cannot_write_raw_sql(
    wall_engine: Engine,
    sf: dict[str, SessionFactory],
    seeded: dict[str, list[Ulid]],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
) -> None:
    with pytest.raises(DeskWallViolation, match="read-only engine"):
        SessionFactory(wall_engine, tokens["auditor"], clock)
    with pytest.raises(TypeError, match="report_engine"):
        SessionFactory(wall_engine, tokens["operator"], clock, report_engine=wall_engine)
    auditor = sf["auditor"]
    with pytest.raises(ValueError, match="writable"):
        SessionFactory(auditor.engine, tokens["auditor"], clock, report_engine=auditor.engine)
    no_report = SessionFactory(auditor.engine, tokens["auditor"], clock)
    with pytest.raises(DeskWallViolation, match="report_engine"):
        with no_report.append_report():
            pass
    for raw in (
        "INSERT INTO wall_ticket (id, created_at, updated_at, status) "
        "VALUES ('evil', '2026-01-01', '2026-01-01', 'prepared')",
        "UPDATE wall_ticket SET status = 'settled'",
        "DELETE FROM wall_ticket",
        "SELECT note FROM wall_desk_row",
    ):
        with auditor.session() as s, pytest.raises(DeskWallViolation, match="raw SQL"):
            s.execute(text(raw))
        with auditor.append_report() as s, pytest.raises(DeskWallViolation, match="raw SQL"):
            s.execute(text(raw))
    with auditor.session() as s, pytest.raises(DeskWallViolation):
        s.connection()
    with sf["operator"].session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == 1


def test_read_only_engine_refuses_writes(
    tmp_db: Any,
    wall_engine: Engine,
    seeded: dict[str, list[Ulid]],
    clock: FakeClock,
    tokens: dict[str, AnyToken],
) -> None:
    ro = make_engine(f"sqlite+pysqlite:///{tmp_db}", read_only=True)
    try:
        assert is_read_only(ro) and not is_read_only(wall_engine)
        with ro.connect() as c:
            assert c.exec_driver_sql("SELECT count(*) FROM wall_shared_log").scalar() == 1
        with pytest.raises(DBAPIError, match="readonly"), ro.begin() as c:
            c.exec_driver_sql(
                "INSERT INTO wall_shared_log (id, created_at, updated_at, note) VALUES ('x', '2026-01-01', '2026-01-01', 'n')"
            )
        auditor = SessionFactory(ro, tokens["auditor"], clock)
        with auditor.session() as s:
            assert s.scalar(select(func.count()).select_from(SharedLog)) == 1
        with pytest.raises(ValueError, match="writer engine"):
            create_schema(ro, WallBase.metadata)
    finally:
        ro.dispose()
    with pytest.raises(ValueError, match="file path"):
        make_engine("sqlite+pysqlite://", read_only=True)


# --------------------------------------------------------------------------- encrypted bytes


def test_encrypted_bytes_refuses_plaintext(
    sf: dict[str, SessionFactory], idgen: IdGenerator
) -> None:
    for plain in (b"AE070331234567890123456", "AE070331234567890123456", bytearray(b"x")):
        with pytest.raises((Tier2LeakError, StatementError)) as info, sf["assistant"].write() as s:
            s.add(SecretRow(id=idgen.new(), blob=plain))
            s.flush()
        err = info.value
        orig = getattr(err, "orig", err)
        assert isinstance(orig, Tier2LeakError)
        assert "AE0703" not in str(err)
    with sf["assistant"].write() as s:
        s.add(SecretRow(id=idgen.new(), blob=None))


def test_encrypted_bytes_round_trips_ciphertext(
    sf: dict[str, SessionFactory], idgen: IdGenerator
) -> None:
    uid = idgen.new()
    blob = Ciphertext(b"\x00\x01opaque\xff")
    with sf["assistant"].write() as s:
        s.add(SecretRow(id=uid, blob=blob))
    with sf["assistant"].session() as s:
        row = s.get(SecretRow, uid)
        assert row is not None
        assert isinstance(row.blob, Ciphertext) and bytes(row.blob) == b"\x00\x01opaque\xff"
    assert isinstance(Ciphertext(b"x"), bytes) and not isinstance(b"x", Ciphertext)


# --------------------------------------------------------------------------- clock-driven defaults


def test_timestamps_come_from_the_process_clock(
    sf: dict[str, SessionFactory], idgen: IdGenerator, clock: FakeClock
) -> None:
    uid = idgen.new()
    with sf["operator"].write() as s:
        s.add(Ticket(id=uid))
    with sf["operator"].session() as s:
        t = s.get(Ticket, uid)
        assert t is not None
        assert t.created_at == clock.now() == t.updated_at
        assert t.created_at.tzinfo is not None and t.created_at.utcoffset() == timedelta(0)
    clock.advance(timedelta(minutes=5))
    with sf["operator"].write() as s:
        t = s.get(Ticket, uid)
        assert t is not None
        t.status = "released"
    with sf["assistant"].session() as s:
        t = s.get(Ticket, uid)
        assert t is not None
        assert t.updated_at == clock.now() and t.created_at == clock.now() - timedelta(minutes=5)


def test_ids_are_never_defaulted(sf: dict[str, SessionFactory]) -> None:
    with pytest.raises(DBAPIError), pytest.warns(SAWarning), sf["operator"].write() as s:
        s.add(Ticket())
        s.flush()


def test_utc_datetime_refuses_naive_values(
    sf: dict[str, SessionFactory], idgen: IdGenerator
) -> None:
    with pytest.raises(StatementError, match="tz-aware"), sf["operator"].write() as s:
        s.add(Ticket(id=idgen.new(), decided_at=datetime(2026, 10, 5, 7, 0)))  # noqa: DTZ001 - naive on purpose
        s.flush()


# --------------------------------------------------------------------------- governance scope and writer serialisation


def test_governance_only_rows(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator
) -> None:
    with sf["operator"].session() as s:
        row = s.get(GovernanceRow, seeded["gov"][0])
        assert row is not None and row.whatsapp_number == "+971500000001"
    with pytest.raises(DeskWallViolation), sf["operator"].write() as s:
        row = s.get(GovernanceRow, seeded["gov"][0])
        assert row is not None
        row.whatsapp_number = "+971500000009"
        s.flush()
    with pytest.raises(DeskWallViolation), sf["assistant"].write() as s:
        s.add(GovernanceRow(id=idgen.new(), whatsapp_number="+9"))
        s.flush()


def test_writers_use_begin_immediate_and_readers_do_not(
    wall_engine: Engine, sf: dict[str, SessionFactory], idgen: IdGenerator
) -> None:
    statements: list[str] = []

    @event.listens_for(wall_engine, "before_cursor_execute")
    def _spy(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        statements.append(statement)

    try:
        with sf["operator"].write() as s:
            s.add(Ticket(id=idgen.new()))
        assert "BEGIN IMMEDIATE" in statements
        statements.clear()
        with sf["assistant"].session() as s:
            s.scalars(select(Ticket)).all()
        assert "BEGIN" in statements and "BEGIN IMMEDIATE" not in statements
    finally:
        event.remove(wall_engine, "before_cursor_execute", _spy)


def test_writers_are_serialised(sf: dict[str, SessionFactory], idgen: IdGenerator) -> None:
    operator = sf["operator"]
    order: list[str] = []
    started = threading.Event()

    def other_writer() -> None:
        started.set()
        with operator.write() as s:
            order.append("second-begin")
            s.add(Ticket(id=idgen.new()))

    with operator.write() as s:
        s.add(Ticket(id=idgen.new()))
        worker = threading.Thread(target=other_writer)
        worker.start()
        started.wait(timeout=5)
        time.sleep(0.1)
        order.append("first-commit")
    worker.join(timeout=10)
    assert order == ["first-commit", "second-begin"]
    with operator.session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == 2


def test_guard_installs_both_listeners(tokens: dict[str, AnyToken], wall_engine: Engine) -> None:
    guard = DeskWallGuard(tokens["operator"])
    assert isinstance(guard.token, DeskToken) and guard.desk is Desk.OPERATOR
    with Session(wall_engine) as session:
        guard.install(session)
        assert session.dispatch.do_orm_execute and session.dispatch.before_flush
        with pytest.raises(DeskWallViolation):
            session.scalars(select(AssistantOnlyRow)).all()


# --------------------------------------------------------------------------- DDL strings (Postgres executes in the CI job)


def test_sqlite_ddl_strings() -> None:
    update_ddl, delete_ddl, replace_ddl = append_only_ddl("sqlite", "audit_event")
    assert update_ddl.startswith(
        'CREATE TRIGGER IF NOT EXISTS "audit_event_no_update" BEFORE UPDATE ON "audit_event"'
    )
    assert (
        "RAISE(ABORT, 'audit_event is append-only')" in update_ddl and "BEFORE DELETE" in delete_ddl
    )
    assert replace_ddl.startswith(
        'CREATE TRIGGER IF NOT EXISTS "audit_event_no_replace" BEFORE INSERT ON "audit_event"'
    )
    assert 'WHEN EXISTS (SELECT 1 FROM "audit_event" WHERE "id" = NEW."id")' in replace_ddl
    composite = append_only_ddl("sqlite", "timer_slot", pk=["timer_name", "slot"])[2]
    assert '"timer_name" = NEW."timer_name" AND "slot" = NEW."slot"' in composite
    with pytest.raises(ValueError):
        append_only_ddl("sqlite", "x", pk=[])
    (single,) = single_transition_ddl("sqlite", "approval", ["decided_at"])
    assert 'BEFORE UPDATE OF "decided_at" ON "approval"' in single and "IS NOT NULL" in single
    (forward,) = forward_only_ddl("sqlite", "transaction", "status", ["prepared", "released"])
    assert "WHEN 'prepared' THEN 0 WHEN 'released' THEN 1" in forward and "RAISE(ABORT" in forward
    with pytest.raises(ValueError):
        append_only_ddl("mysql", "x")
    with pytest.raises(ValueError):
        forward_only_ddl("sqlite", "t", "c", ["only"])


def test_postgres_ddl_strings() -> None:
    pg_append = append_only_ddl("postgresql", "audit_event")
    joined = "\n".join(pg_append)
    assert "RAISE EXCEPTION" in joined and "plpgsql" in joined and "DO INSTEAD" not in joined
    assert (
        'CREATE TRIGGER "audit_event_no_update_delete" BEFORE UPDATE OR DELETE ON "audit_event"'
        in joined
    )
    single = "\n".join(single_transition_ddl("postgresql", "approval", ["decided_at", "decision"]))
    assert single.count("CREATE TRIGGER") == 2 and "IS DISTINCT FROM" in single
    forward = "\n".join(
        forward_only_ddl("postgresql", "transaction", "status", ["prepared", "released", "settled"])
    )
    assert (
        "array_position(ARRAY['prepared', 'released', 'settled']" in forward
        and "RAISE EXCEPTION" in forward
    )


def test_pg_roles_ddl_covers_every_scope() -> None:
    ddl = pg_roles_ddl(WallBase.metadata)
    joined = "\n".join(ddl)
    for role in ALL_ROLES:
        # THREAT_REVIEW 6.1: NOLOGIN until the operator sets a password out of band; never a
        # superuser, never BYPASSRLS (a superuser bypasses RLS even with FORCE).
        assert f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE' in joined
        assert "NOBYPASSRLS" in joined and f'CREATE ROLE "{role}" LOGIN' not in joined
    assert f'ALTER ROLE "{AUDITOR_ROLE}" SET default_transaction_read_only = on' in joined
    # append-only: revoke update/delete, grant select+insert only
    assert 'REVOKE UPDATE, DELETE ON "wall_shared_log" FROM' in joined
    assert (
        'GRANT SELECT, INSERT ON "wall_shared_log" TO "nour_desk_operator", "nour_desk_assistant"'
        in joined
    )
    assert 'GRANT SELECT ON "wall_shared_log" TO "nour_auditor"' in joined
    # desk rows: ENABLE + FORCE RLS, keyed on current_user (the connecting role), never on a GUC
    assert 'ALTER TABLE "wall_desk_row" ENABLE ROW LEVEL SECURITY' in joined
    assert 'ALTER TABLE "wall_desk_row" FORCE ROW LEVEL SECURITY' in joined
    assert "current_setting(" not in joined and "nour.desk" not in joined
    assert "CASE current_user WHEN 'nour_desk_operator' THEN 'operator'" in joined
    assert "WHEN 'nour_scheduler' THEN 'governance' END" in joined
    assert 'CREATE POLICY "wall_desk_row_desk_isolation" ON "wall_desk_row" FOR ALL TO' in joined
    # *_ONLY scopes go to one role
    assert (
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON "wall_assistant_only" TO "{ASSISTANT_ROLE}"'
        in joined
    )
    assert (
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON "wall_operator_only" TO "{OPERATOR_ROLE}"'
        in joined
    )
    assert not any('"wall_assistant_only"' in line and OPERATOR_ROLE in line for line in ddl)
    # governance-only: desks get a column grant that excludes passphrase columns
    gov_grant = next(
        line for line in ddl if line.startswith("GRANT SELECT (") and '"wall_governance"' in line
    )
    assert '"whatsapp_number"' in gov_grant and "passphrase_hash" not in gov_grant
    # auditor-write: auditor only, insert + select
    assert f'GRANT SELECT, INSERT ON "wall_auditor_report" TO "{AUDITOR_ROLE}"' in joined
    assert not any(
        '"wall_auditor_report"' in line and "GRANT" in line and ASSISTANT_ROLE in line
        for line in ddl
    )
    assert all(table in joined for table in ("wall_ticket", "wall_secret"))


def test_trigger_ddl_lists_every_flagged_table() -> None:
    sqlite_ddl = trigger_ddl("sqlite", WallBase.metadata)
    names = "\n".join(sqlite_ddl)
    assert "wall_shared_log_no_update" in names and "wall_auditor_report_no_delete" in names
    assert "wall_shared_log_no_replace" in names
    assert (
        "wall_ticket_decided_at_single_transition" in names
        and "wall_ticket_status_forward_only" in names
    )
    assert "wall_desk_row" not in names
    assert trigger_ddl("sqlite", MetaData()) == []  # no flagged tables, no DDL
    with pytest.raises(ValueError):
        trigger_ddl("mysql", WallBase.metadata)


class SchemaBase(Base):
    __abstract__ = True
    metadata = MetaData(naming_convention=Base.metadata.naming_convention)


class AuditorReportInSchema(SchemaBase, RecordMixin):
    """Stands in for `auditor_report`, which lives in the Postgres schema `auditor` (DESIGN §3.8);
    named apart from the real wave-1 table, which shares the test database file."""

    __tablename__ = "wall_schema_report"
    __table_args__ = {"schema": "auditor"}
    __scope__ = Scope.AUDITOR_WRITE
    __append_only__ = True
    __single_transition__ = ("sent_at",)
    __forward_only__ = {"state": ["draft", "sent"]}
    summary: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


def test_schema_qualified_tables_get_valid_ddl_on_both_dialects() -> None:
    pg = trigger_ddl("postgresql", SchemaBase.metadata)
    joined = "\n".join(pg)
    assert (
        'CREATE TRIGGER "wall_schema_report_no_update_delete" BEFORE UPDATE OR DELETE ON '
        '"auditor"."wall_schema_report"' in joined
    )
    assert 'DROP TRIGGER IF EXISTS "wall_schema_report_no_update_delete" ON "auditor"."wall_schema_report"'
    assert 'ON "auditor"."wall_schema_report" FOR EACH ROW' in joined
    assert '"auditor.wall_schema_report"' not in joined  # never one identifier
    assert (
        'CREATE TRIGGER "wall_schema_report_sent_at_single_transition" BEFORE UPDATE OF "sent_at" ON '
        '"auditor"."wall_schema_report"' in joined
    )
    assert (
        'CREATE TRIGGER "wall_schema_report_state_forward_only" BEFORE UPDATE OF "state" ON '
        '"auditor"."wall_schema_report"' in joined
    )
    roles = "\n".join(pg_roles_ddl(SchemaBase.metadata))
    assert 'CREATE SCHEMA IF NOT EXISTS "auditor"' in roles
    assert 'GRANT SELECT, INSERT ON "auditor"."wall_schema_report" TO "nour_auditor"' in roles
    assert pg_schema_ddl(SchemaBase.metadata) == ['CREATE SCHEMA IF NOT EXISTS "auditor"']
    assert pg_schema_ddl(WallBase.metadata) == []
    sqlite = "\n".join(trigger_ddl("sqlite", SchemaBase.metadata))
    assert 'BEFORE UPDATE ON "wall_schema_report"' in sqlite and "auditor." not in sqlite


def test_create_schema_translates_schemas_away_on_sqlite(
    engine: Engine,
    tmp_db: Any,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    idgen: IdGenerator,
) -> None:
    create_schema(engine, SchemaBase.metadata)
    assert engine.get_execution_options()["schema_translate_map"] == {"auditor": None}
    assert install_sqlite_schema_translation(engine, SchemaBase.metadata) == {"auditor": None}
    with engine.connect() as c:
        tables = {
            n for (n,) in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "wall_schema_report" in tables
    assert {"wall_schema_report_no_update", "wall_schema_report_no_replace"} <= _sqlite_triggers(
        engine
    )
    auditor = session_factory(tokens["auditor"])
    with auditor.append_report() as s:
        s.add(AuditorReportInSchema(id=idgen.new(), summary="day one"))
    with auditor.session() as s:
        assert [r.summary for r in s.scalars(select(AuditorReportInSchema))] == ["day one"]
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as c:
        c.exec_driver_sql("UPDATE wall_schema_report SET summary = 'x'")


# --------------------------------------------------------------------------- make_engine on other dialects (strings only)


def test_make_engine_sqlite_pragmas(wall_engine: Engine) -> None:
    with wall_engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    assert wall_engine.dialect.name == "sqlite"


# --------------------------------------------------------------------------- the wall on every statement path


def _note_row(idgen: IdGenerator, desk: Desk, note: str) -> dict[str, Any]:
    return {"id": idgen.new(), "desk": desk, "note": note}


def _partition(sf: dict[str, SessionFactory], kind: str) -> list[str]:
    with sf[kind].session() as s:
        return sorted(r.note for r in s.scalars(select(DeskRowNote)))


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_core_statements_over_mapped_tables_are_refused(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    """A Core statement cannot be partitioned by with_loader_criteria and hides the mapper from
    the scope check, so every form is refused outright: the mapped class is the only way in."""
    desk_table = cast(Table, DeskRowNote.__table__)
    vault_table = cast(Table, AssistantOnlyRow.__table__)
    reads: list[Executable] = [
        select(desk_table),
        select(desk_table.c.note),
        select(func.count()).select_from(desk_table),
        select(vault_table),
        select(func.count()).select_from(vault_table),
        select(select(desk_table.c.note).limit(1).scalar_subquery()),
        select(table("wall_desk_row", column("note"))),  # lightweight table(): no mapper info
        select(table("wall_assistant_only", column("note"))),
        text("SELECT note FROM wall_desk_row"),
        text("SELECT note FROM wall_assistant_only"),
        text("SELECT note FROM wall_desk_row").columns(column("note")),
    ]
    for statement in reads:
        with sf[kind].session() as s, pytest.raises(DeskWallViolation):
            s.execute(statement)
        with sf[kind].write() as s, pytest.raises(DeskWallViolation):
            s.execute(statement)
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    writes: list[Executable] = [
        insert(desk_table).values(
            id=idgen.new(), desk=other.value, note="smuggled", created_at=None, updated_at=None
        ),
        update(desk_table).values(note="tampered"),
        update(desk_table).where(desk_table.c.desk == other.value).values(note="tampered"),
        delete(desk_table),
        insert(vault_table).values(id=idgen.new(), note="planted"),
        update(vault_table).values(note="tampered"),
        text("UPDATE wall_desk_row SET note = 'tampered'"),
        text(
            "INSERT INTO wall_assistant_only (id, created_at, updated_at, note) "
            "VALUES ('x', '2026-01-01', '2026-01-01', 'planted')"
        ),
    ]
    for statement in writes:
        with sf[kind].write() as s, pytest.raises(DeskWallViolation):
            s.execute(statement)
    # scalar selects over no table are not a wall concern
    with sf[kind].session() as s:
        assert s.scalar(select(literal(1))) == 1
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_session_connection_and_legacy_bulk_apis_are_refused(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    with sf[kind].session() as s:
        assert isinstance(s, GuardedSession)
        with pytest.raises(DeskWallViolation):
            s.connection()
        with pytest.raises(DeskWallViolation):
            s.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
    with sf[kind].write() as s:
        with pytest.raises(DeskWallViolation):
            s.connection()
        with pytest.raises(DeskWallViolation):
            s.bulk_save_objects([DeskRowNote(**_note_row(idgen, other, "smuggled"))])
        with pytest.raises(DeskWallViolation):
            s.bulk_save_objects([AssistantOnlyRow(id=idgen.new(), note="planted")])
        with pytest.raises(DeskWallViolation):
            s.bulk_insert_mappings(DeskRowNote, [_note_row(idgen, other, "smuggled")])
        with pytest.raises(DeskWallViolation):
            s.bulk_insert_mappings(AssistantOnlyRow, [{"id": idgen.new(), "note": "planted"}])
        with pytest.raises(DeskWallViolation):
            s.bulk_update_mappings(DeskRowNote, [{"id": seeded[kind][0], "desk": other}])
        with pytest.raises(DeskWallViolation):
            s.bulk_update_mappings(SharedLog, [{"id": seeded["log"][0], "note": "tampered"}])
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]
    with sf["assistant"].session() as s:
        assert s.scalar(select(func.count()).select_from(AssistantOnlyRow)) == 0


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_bulk_insert_must_name_the_tokens_desk_in_every_row(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    own = Desk(kind)
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    refused: list[Executable] = [
        insert(DeskRowNote).values(**_note_row(idgen, other, "smuggled")),
        insert(DeskRowNote).values(id=idgen.new(), desk=other.value, note="smuggled"),
        insert(DeskRowNote).values(
            [_note_row(idgen, own, "fine"), _note_row(idgen, other, "smuggled")]
        ),
        insert(DeskRowNote).values([_positional_row(idgen, other, "smuggled")]),
        insert(DeskRowNote).values([_positional_row(idgen, "smuggled", "not a desk")]),
        insert(DeskRowNote).values(id=idgen.new(), note="no desk at all"),
        insert(DeskRowNote).values(id=idgen.new(), desk=literal(other.value), note="expr"),
        insert(DeskRowNote).values(id=idgen.new(), desk=DeskRowNote.__table__.c.note, note="col"),
        insert(DeskRowNote).from_select(
            ["id", "desk", "note"],
            select(literal("x"), literal(own.value), literal("from select")),
        ),
    ]
    for statement in refused:
        with sf[kind].write() as s, pytest.raises(DeskWallViolation):
            s.execute(statement)
    with sf[kind].write() as s, pytest.raises(DeskWallViolation):  # executemany parameters
        s.execute(insert(DeskRowNote), [_note_row(idgen, other, "smuggled")])
    with sf[kind].write() as s, pytest.raises(DeskWallViolation):
        s.execute(
            insert(DeskRowNote), [_note_row(idgen, own, "fine"), {"id": idgen.new(), "note": "x"}]
        )
    # the same shapes with the token's own desk are fine
    with sf[kind].write() as s:
        s.execute(insert(DeskRowNote).values(**_note_row(idgen, own, "bulk-1")))
        s.execute(insert(DeskRowNote).values([_note_row(idgen, own, "bulk-2")]))
        s.execute(insert(DeskRowNote), [_note_row(idgen, own, "bulk-3")])
    assert _partition(sf, kind) == sorted([f"{kind}-0", f"{kind}-1", "bulk-1", "bulk-2", "bulk-3"])
    other_kind = other.value
    assert _partition(sf, other_kind) == [f"{other_kind}-0", f"{other_kind}-1"]


def _positional_row(idgen: IdGenerator, desk: Desk | str, note: str) -> tuple[Any, ...]:
    """A positional INSERT tuple in the table's own column order."""
    from nour.core.clock import process_now

    values: dict[str, Any] = {
        "id": idgen.new(),
        "created_at": process_now(),
        "updated_at": process_now(),
        "desk": desk,
        "coat_id": None,
        "note": note,
        "meta": None,
    }
    return tuple(values[c.name] for c in cast(Table, DeskRowNote.__table__).c)


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_bulk_update_may_never_set_the_desk(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    own = Desk(kind)
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    for value in (other, other.value, own, own.value, literal(other.value)):
        with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="desk"):
            s.execute(update(DeskRowNote).values(desk=value))
        with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="desk"):
            s.execute(
                update(DeskRowNote).where(DeskRowNote.id == seeded[kind][0]).values(desk=value)
            )
    with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="desk"):
        s.execute(update(DeskRowNote), [{"id": seeded[kind][0], "desk": other, "note": "moved"}])
    with sf[kind].write() as s:  # other columns stay writable, within the partition
        s.execute(update(DeskRowNote).values(note="renamed"))
    assert _partition(sf, kind) == ["renamed", "renamed"]
    assert _partition(sf, other.value) == [f"{other.value}-0", f"{other.value}-1"]


@pytest.mark.parametrize("kind", ["operator", "assistant", "governance"])
def test_reader_sessions_cannot_write(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    """session() is a reader: nothing is ever committed outside write()'s lock and BEGIN IMMEDIATE."""
    own = Desk(kind)
    with sf[kind].session() as s:
        assert s.info[READER_INFO_KEY] is True and s.info[REPORT_INFO_KEY] is False
        s.add(DeskRowNote(**_note_row(idgen, own, "via reader")))
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.flush()
        s.rollback()
        s.add(Ticket(id=idgen.new()))
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.commit()
        s.rollback()
        s.add(SharedLog(id=idgen.new(), note="via reader"))
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.scalars(select(Ticket)).all()  # autoflush
        s.rollback()
        ticket = s.get(Ticket, seeded["ticket"][0])
        assert ticket is not None
        ticket.status = "released"
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.flush()
        s.rollback()
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.execute(insert(Ticket).values(id=idgen.new()))
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.execute(update(Ticket).values(status="released"))
        with pytest.raises(DeskWallViolation, match="reader session"):
            s.execute(delete(Ticket))
    with sf["operator"].session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == 1
        assert s.scalar(select(func.count()).select_from(SharedLog)) == 1
        assert s.get(Ticket, seeded["ticket"][0]).status == "prepared"  # type: ignore[union-attr]
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]
    with sf[kind].write() as s:
        assert s.info[READER_INFO_KEY] is False


def test_write_is_not_reentrant(sf: dict[str, SessionFactory], idgen: IdGenerator) -> None:
    operator = sf["operator"]
    with operator.write() as outer:
        outer.add(Ticket(id=idgen.new()))
        outer.flush()
        with pytest.raises(RuntimeError, match="not re-entrant"):
            with operator.write():
                pass
        with pytest.raises(RuntimeError, match="not re-entrant"):  # the auditor's writer likewise
            with sf["auditor"].append_report():
                pass
    with operator.write() as again:  # the flag is cleared on exit
        again.add(Ticket(id=idgen.new()))
    with operator.session() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == 2


def test_a_token_that_mint_did_not_produce_opens_nothing(
    wall_engine: Engine, clock: FakeClock, seeded: dict[str, list[Ulid]]
) -> None:
    """object.__new__ plus object.__setattr__ passes isinstance; the minted registry does not."""
    forged = _forge(AssistantToken, Desk.ASSISTANT)
    assert isinstance(forged, AssistantToken) and forged.desk is Desk.ASSISTANT
    with pytest.raises(AuthError):
        SessionFactory(wall_engine, forged, clock)
    with pytest.raises(AuthError):
        DeskWallGuard(forged)
    with pytest.raises(AuthError):
        scope_allows(Scope.SHARED, forged, write=False)
    with pytest.raises(AuthError):
        scope_allows(Scope.AUDITOR_WRITE, _forge(AuditorToken), write=False)
    with pytest.raises(AuthError):
        SessionFactory(wall_engine, _forge(OperatorToken, Desk.OPERATOR), clock)


def _forge(cls: type[Any], desk: Desk | None = None) -> Any:
    forged = object.__new__(cls)
    if desk is not None:
        object.__setattr__(forged, "desk", desk)
    return forged


# --------------------------------------------------------------------------- bare tables, verbatim SQL and the ORM shapes that stay partitioned


def _bare_reads(kind: str) -> list[Executable]:
    """ORM statements that smuggle a bare table (``Model.__table__``, ``table()``, a Core alias
    or join) into a position ``with_loader_criteria`` cannot reach: a Core sub-select, a bare
    join target, a second unpartitioned FROM, or an entity named only in ORDER BY/GROUP BY."""
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    desk_table = cast(Table, DeskRowNote.__table__)
    vault_table = cast(Table, AssistantOnlyRow.__table__)
    lightweight = table("wall_assistant_only", column("note"))
    a = aliased(DeskRowNote)
    return [
        select(
            DeskRowNote.id,
            select(desk_table.c.note)
            .where(desk_table.c.desk == other.value)
            .limit(1)
            .scalar_subquery(),
        ),
        select(DeskRowNote.id).where(
            exists(select(desk_table.c.id).where(desk_table.c.note.like(f"{other.value}%")))
        ),
        select(DeskRowNote.id).where(DeskRowNote.id.in_(select(desk_table.c.id))),
        select(DeskRowNote.id).where(
            DeskRowNote.id == select(desk_table.c.id).limit(1).scalar_subquery()
        ),
        select(DeskRowNote.id, select(lightweight.c.note).limit(1).scalar_subquery()),
        select(DeskRowNote.id, select(vault_table.c.note).limit(1).scalar_subquery()),
        select(DeskRowNote.id).join(vault_table, literal(True)),
        select(DeskRowNote.id).join(desk_table.alias(), literal(True)),
        select(DeskRowNote.note).select_from(join(desk_table, vault_table, literal(True))),
        select(DeskRowNote.id, desk_table.alias().c.note),
        select(a.note, desk_table.c.note),
        select(alias(cast(Any, DeskRowNote)).c.note),  # a Core alias of the mapped class
        select(desk_table.c.note).order_by(DeskRowNote.note),
        select(func.count(desk_table.c.id)).group_by(DeskRowNote.desk),
        select(desk_table.c.note, func.count())
        .group_by(desk_table.c.note)
        .having(func.count(DeskRowNote.id) > 0),
        select(DeskRowNote).from_statement(select(desk_table)),
        select(DeskChild.body).select_from(join(DeskChild, DeskParent, literal(True))),
        union_all(select(DeskRowNote.note), select(desk_table.c.note)),
        # with_only_columns() drops DeskChild from the entities SQLAlchemy partitions while the
        # join keeps it in the FROM list
        select(DeskChild).join(DeskChild.parent).with_only_columns(DeskParent.title),
    ]


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_bare_tables_inside_orm_statements_are_refused(
    sf: dict[str, SessionFactory],
    seeded: dict[str, list[Ulid]],
    family: dict[str, dict[str, Ulid]],
    kind: str,
) -> None:
    for statement in _bare_reads(kind):
        with sf[kind].session() as s, pytest.raises(DeskWallViolation, match="cannot partition"):
            s.execute(statement)
        with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="cannot partition"):
            s.execute(statement)
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_bare_tables_inside_dml_are_refused(
    sf: dict[str, SessionFactory],
    seeded: dict[str, list[Ulid]],
    family: dict[str, dict[str, Ulid]],
    idgen: IdGenerator,
    kind: str,
) -> None:
    own = Desk(kind)
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    desk_table = cast(Table, DeskRowNote.__table__)
    vault_table = cast(Table, AssistantOnlyRow.__table__)
    writes: list[Executable] = [
        insert(DeskRowNote).values(
            id=idgen.new(),
            desk=own,
            note="copy",
            coat_id=select(desk_table.c.note)
            .where(desk_table.c.desk == other.value)
            .limit(1)
            .scalar_subquery(),
        ),
        update(DeskRowNote)
        .where(DeskRowNote.id.in_(select(desk_table.c.id)))
        .values(note="tampered"),
        delete(DeskRowNote).where(
            DeskRowNote.id.in_(select(desk_table.c.id).where(desk_table.c.desk == other.value))
        ),
        update(DeskRowNote)
        .where(DeskRowNote.note == select(vault_table.c.note).limit(1).scalar_subquery())
        .values(note="x"),
        # UPDATE … FROM: the second table of a DML WHERE is never partitioned
        update(DeskParent)
        .where(DeskParent.id == DeskChild.parent_id, DeskChild.body.like(f"{other.value}%"))
        .values(title="x"),
    ]
    for statement in writes:
        with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="cannot partition"):
            s.execute(statement)
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]
    with sf["assistant"].session() as s:
        assert [p.title for p in s.scalars(select(DeskParent))] == ["assistant-parent"]


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_verbatim_sql_fragments_are_refused(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    """literal_column(), quoted_name(quote=False), free-text .op(), prefix/suffix/hint text and
    extract() fields compile as given: any of them can name the other partition or comment the
    partition predicate out (``-- `` eats the rest of the WHERE line)."""
    own = Desk(kind)
    probe = "(select note from wall_assistant_only limit 1)"
    reads: list[Executable] = [
        select(DeskRowNote.id, literal_column(probe)),
        select(DeskRowNote).where(literal_column("desk='assistant' or 1=1")),
        select(DeskRowNote.id, column(quoted_name(probe, quote=False))),
        select(DeskRowNote.id.op(f"|| {probe} ||")(literal(""))),
        select(DeskRowNote.id).where(DeskRowNote.id.op("--")(literal(""))),
        select(DeskRowNote.id).where(DeskRowNote.id.op("/*")(literal(""))),
        select(DeskRowNote.id.label(quoted_name("x, (select 1)", quote=False))),
        select(DeskRowNote.note).suffix_with("UNION ALL SELECT note FROM wall_desk_row"),
        select(DeskRowNote.note).prefix_with("DISTINCT"),
        select(DeskRowNote.note).with_statement_hint("-- hint"),
        select(DeskRowNote.note).with_hint(DeskRowNote, "USE INDEX (x)"),
        select(DeskRowNote.id, extract("year) from wall_desk_row --", DeskRowNote.created_at)),
    ]
    for statement in reads:
        with sf[kind].session() as s, pytest.raises(DeskWallViolation, match="verbatim"):
            s.execute(statement)
    writes: list[Executable] = [
        update(DeskRowNote).where(literal_column("desk='assistant' or 1=1")).values(note="t"),
        update(DeskRowNote).values(note=literal_column(probe)),
        insert(DeskRowNote).values(id=idgen.new(), desk=own, note=literal_column(probe)),
        delete(DeskRowNote).where(literal_column("1=1")),
        insert(DeskRowNote).values(**_note_row(idgen, own, "r")).prefix_with("OR REPLACE"),
    ]
    for statement in writes:
        with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="verbatim"):
            s.execute(statement)
    assert _partition(sf, "operator") == ["operator-0", "operator-1"]
    assert _partition(sf, "assistant") == ["assistant-0", "assistant-1"]
    # what SQLAlchemy itself emits stays admissible: count(*), exists(), punctuation operators,
    # a plain extract() field and SQLite's OR IGNORE conflict clause
    with sf[kind].session() as s:
        assert s.scalar(select(func.count()).select_from(DeskRowNote)) == 2
        owned = select(DeskRowNote.id).where(exists().where(DeskRowNote.note.like("%")))
        assert len(s.scalars(owned).all()) == 2
        assert s.scalar(select(DeskRowNote.note.op("||")(literal("!")))) == f"{kind}-0!"
        years = s.execute(select(DeskRowNote.id, extract("year", DeskRowNote.created_at))).all()
        assert {row[1] for row in years} == {2026}
    row = _note_row(idgen, own, "once")
    with sf[kind].write() as s:
        s.execute(insert(DeskRowNote).prefix_with("OR IGNORE").values(**row))
        ignored = cast(Any, s.execute(insert(DeskRowNote).prefix_with("OR IGNORE").values(**row)))
        assert ignored.rowcount == 0
    assert _partition(sf, kind) == sorted([f"{kind}-0", f"{kind}-1", "once"])


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_desk_row_upserts_are_refused_and_shared_upserts_keep_excluded(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    """ON CONFLICT DO UPDATE on a DESK_ROW table could rewrite the conflicting row of the other
    partition on SQLite (Postgres RLS refuses that itself); DO NOTHING and upserts on SHARED
    tables (whose ``excluded`` pseudo-table is not a FROM) stay available."""
    own = Desk(kind)
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    stolen = sqlite_insert(DeskRowNote).values(id=seeded[other.value][0], desk=own, note="stolen")
    stolen = stolen.on_conflict_do_update(
        index_elements=["id"], set_={"note": stolen.excluded.note}
    )
    with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="upsert"):
        s.execute(stolen)
    own_row = sqlite_insert(DeskRowNote).values(**_note_row(idgen, own, "up"))
    own_row = own_row.on_conflict_do_update(
        index_elements=["id"], set_={"note": own_row.excluded.note}
    )
    with sf[kind].write() as s, pytest.raises(DeskWallViolation, match="upsert"):
        s.execute(own_row)
    assert _partition(sf, other.value) == [f"{other.value}-0", f"{other.value}-1"]
    row = _note_row(idgen, own, "nothing")
    with sf[kind].write() as s:
        s.execute(sqlite_insert(DeskRowNote).values(**row).on_conflict_do_nothing())
        again = cast(
            Any, s.execute(sqlite_insert(DeskRowNote).values(**row).on_conflict_do_nothing())
        )
        assert again.rowcount == 0
    assert _partition(sf, kind) == sorted([f"{kind}-0", f"{kind}-1", "nothing"])
    shared_id = idgen.new()
    with sf[kind].write() as s:
        s.add(SharedNote(id=shared_id, note="first"))
    shared = sqlite_insert(SharedNote).values(id=shared_id, note="upserted")
    shared = shared.on_conflict_do_update(
        index_elements=["id"], set_={"note": shared.excluded.note}
    )
    with sf[kind].write() as s:
        s.execute(shared)
    with sf[kind].session() as s:
        assert s.get(SharedNote, shared_id).note == "upserted"  # type: ignore[union-attr]
    # a single-transition table keeps its rows: an upsert that collides is refused by the
    # BEFORE INSERT trigger (it would re-arm the row through SQLite's conflict resolution)
    ticket = sqlite_insert(Ticket).values(id=seeded["ticket"][0], status="released")
    ticket = ticket.on_conflict_do_update(
        index_elements=["id"], set_={"status": ticket.excluded.status}
    )
    with pytest.raises(IntegrityError, match="never deleted or replaced"), sf[kind].write() as s:
        s.execute(ticket)
    with sf[kind].session() as s:
        assert s.get(Ticket, seeded["ticket"][0]).status == "prepared"  # type: ignore[union-attr]


def test_orm_shapes_the_wall_partitions_still_work(
    sf: dict[str, SessionFactory],
    seeded: dict[str, list[Ulid]],
    family: dict[str, dict[str, Ulid]],
) -> None:
    """Every shape SQLAlchemy partitions (nested ORM sub-selects, relationship and eager loads,
    any()/has(), joins over mapped classes, CTEs, unions, aliases, aggregates, a bare column of
    the same table as an entity of the same SELECT) keeps working for the operator and sees the
    operator partition only."""
    desk_table = cast(Table, DeskRowNote.__table__)
    operator = sf["operator"]
    op_ids = family["operator"]
    with operator.session() as s:
        parent = s.get(DeskParent, op_ids["parent"])
        assert parent is not None and parent.title == "operator-parent"
        assert s.get(DeskParent, family["assistant"]["parent"]) is None
        assert [c.body for c in parent.children] == ["operator-child"]  # lazy load
        for option in (
            selectinload(DeskParent.children),
            subqueryload(DeskParent.children),
            joinedload(DeskParent.children),
        ):
            loaded = s.scalars(select(DeskParent).options(option)).unique().all()
            assert [(p.title, [c.body for c in p.children]) for p in loaded] == [
                ("operator-parent", ["operator-child"])
            ]
        assert [
            p.title for p in s.scalars(select(DeskParent).where(DeskParent.children.any()))
        ] == ["operator-parent"]
        assert [
            c.body
            for c in s.scalars(
                select(DeskChild).where(DeskChild.parent.has(DeskParent.title.like("%parent")))
            )
        ] == ["operator-child"]
        assert [
            c.body
            for c in s.scalars(select(DeskChild).where(with_parent(parent, DeskParent.children)))
        ] == ["operator-child"]
        for joined in (
            select(DeskChild.body).join(DeskChild.parent),
            select(DeskChild.body).join(DeskParent),
            select(DeskChild.body).join(DeskParent, DeskChild.parent_id == DeskParent.id),
            select(DeskChild.body).join_from(DeskChild, DeskParent),
            select(DeskChild.body).outerjoin(DeskChild.parent).order_by(DeskParent.title),
            select(DeskChild).join(DeskChild.parent).with_only_columns(DeskChild.body),
            select(DeskParent.title).select_from(DeskChild).join(DeskChild.parent),
        ):
            assert len(s.scalars(joined).all()) == 1
        # nested ORM sub-selects are partitioned, so the other partition is simply empty
        nested = s.execute(
            select(
                DeskRowNote.id,
                select(DeskRowNote.note)
                .where(DeskRowNote.desk == Desk.ASSISTANT)
                .limit(1)
                .scalar_subquery(),
            )
        ).all()
        assert len(nested) == 2 and {row[1] for row in nested} == {None}
        oracle = select(DeskRowNote.id).where(
            exists(select(DeskRowNote.id).where(DeskRowNote.note.like("assistant%")))
        )
        assert s.scalars(oracle).all() == []
        assert (
            s.scalars(
                select(DeskRowNote.id).where(
                    DeskRowNote.id.in_(select(DeskRowNote.id).where(DeskRowNote.note.like("a%")))
                )
            ).all()
            == []
        )
        notes = select(DeskRowNote.note)
        cte = notes.cte()
        assert sorted(s.scalars(select(cte.c.note))) == ["operator-0", "operator-1"]
        sub = notes.subquery()
        assert sorted(s.scalars(select(sub.c.note))) == ["operator-0", "operator-1"]
        assert sorted(s.scalars(union_all(notes, select(DeskChild.body)))) == [
            "operator-0",
            "operator-1",
            "operator-child",
        ]
        a = aliased(DeskRowNote)
        assert sorted(s.scalars(select(a.note))) == ["operator-0", "operator-1"]
        a2 = aliased(DeskRowNote, select(DeskRowNote).subquery())
        assert sorted(s.scalars(select(a2.note))) == ["operator-0", "operator-1"]
        assert s.scalar(select(func.count(DeskRowNote.id))) == 2
        assert s.query(DeskRowNote).count() == 2
        assert (
            s.scalar(select(select(DeskRowNote.note).where(DeskRowNote.note.like("a%")).exists()))
            is False
        )
        assert s.execute(
            select(DeskRowNote.desk, func.count())
            .group_by(DeskRowNote.desk)
            .having(func.count() > 0)
        ).all() == [(Desk.OPERATOR, 2)]
        assert s.scalars(select(DeskRowNote.note.label("n")).order_by("n")).all() == [
            "operator-0",
            "operator-1",
        ]
        # a bare column of a table that is an entity of the same SELECT dedupes into its FROM
        assert sorted(r[1] for r in s.execute(select(DeskRowNote.id, desk_table.c.note))) == [
            "operator-0",
            "operator-1",
        ]
        assert sorted(s.scalars(select(desk_table.c.note).select_from(DeskRowNote))) == [
            "operator-0",
            "operator-1",
        ]
        assert sorted(
            n.note for n in s.scalars(select(DeskRowNote).where(desk_table.c.note.like("%")))
        ) == ["operator-0", "operator-1"]


def test_mapper_level_eager_joins_and_loader_options_are_walled(
    sf: dict[str, SessionFactory],
    wall_engine: Engine,
    family: dict[str, dict[str, Ulid]],
    idgen: IdGenerator,
) -> None:
    """``lazy="joined"`` joins the parent inside the child's own SELECT: the criteria are
    registered for every DESK_ROW mapper of the registry, so a planted cross-desk reference loads
    nothing. A loader option naming an ASSISTANT_ONLY mapper is scope-checked on the statement
    that carries it, and the lazy load it would otherwise issue is refused on its own."""
    planted = idgen.new()
    with wall_engine.begin() as c:  # a row pointing at the other desk's parent and at the vault
        c.exec_driver_sql(
            "INSERT INTO wall_desk_child (id, created_at, updated_at, desk, parent_id, "
            "secret_id, body) VALUES (?, '2026-01-01', '2026-01-01', 'operator', ?, ?, 'planted')",
            (planted, family["assistant"]["parent"], family["assistant"]["secret"]),
        )
    operator = sf["operator"]
    with operator.session() as s:
        own = s.get(DeskChild, family["operator"]["child"])
        assert own is not None and own.parent is not None
        assert own.parent.title == "operator-parent"
        stray = s.get(DeskChild, planted)
        assert stray is not None and stray.parent is None  # eager join partitioned on desk
        assert s.scalars(select(DeskChild.body).join(DeskChild.parent)).all() == ["operator-child"]
        assert s.scalars(
            select(DeskChild.body).join(DeskChild.parent).order_by(DeskParent.title)
        ).all() == ["operator-child"]
        with pytest.raises(DeskWallViolation, match="AssistantOnlyRow"):
            stray.secret  # noqa: B018 - the lazy load over the vault table is the statement
        for option in (joinedload(DeskChild.secret), selectinload(DeskChild.secret)):
            with pytest.raises(DeskWallViolation, match="AssistantOnlyRow"):
                s.scalars(select(DeskChild).options(option)).all()
    with sf["assistant"].session() as s:
        child = s.scalars(select(DeskChild).options(joinedload(DeskChild.secret))).unique().one()
        assert child.body == "assistant-child" and child.secret is not None
        assert child.secret.note == "vault"
        assert child.parent is not None and child.parent.title == "assistant-parent"


@pytest.mark.parametrize("kind", ["operator", "assistant"])
def test_dml_with_nested_orm_subqueries_stays_partitioned(
    sf: dict[str, SessionFactory], seeded: dict[str, list[Ulid]], idgen: IdGenerator, kind: str
) -> None:
    own = Desk(kind)
    other = Desk.ASSISTANT if kind == "operator" else Desk.OPERATOR
    desk_table = cast(Table, DeskRowNote.__table__)
    copied = idgen.new()
    with sf[kind].write() as s:
        s.execute(
            insert(DeskRowNote).values(
                id=copied,
                desk=own,
                note="copy",
                coat_id=select(DeskRowNote.note)
                .where(DeskRowNote.desk == other)
                .limit(1)
                .scalar_subquery(),
            )
        )
        s.execute(
            update(DeskRowNote)
            .where(DeskRowNote.id.in_(select(DeskRowNote.id).where(DeskRowNote.note.like("%-1"))))
            .values(note="renamed")
        )
        s.execute(update(DeskRowNote).where(desk_table.c.note == "copy").values(note="sloppy"))
        s.execute(
            delete(DeskRowNote).where(
                DeskRowNote.id.in_(select(DeskRowNote.id).where(DeskRowNote.note.like("%-0")))
            )
        )
    with sf[kind].session() as s:
        row = s.get(DeskRowNote, copied)
        assert row is not None and row.coat_id is None  # the other partition was invisible
    assert _partition(sf, kind) == ["renamed", "sloppy"]
    assert _partition(sf, other.value) == [f"{other.value}-0", f"{other.value}-1"]
