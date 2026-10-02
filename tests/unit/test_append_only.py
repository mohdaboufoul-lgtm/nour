"""Append-only tables at the database level (DESIGN §4h, §7.3 ``tests/unit/test_append_only.py``;
SPEC §12): on every member of ``APPEND_ONLY_TABLES`` a raw-SQL UPDATE, DELETE, ``INSERT OR
REPLACE`` / ``REPLACE INTO`` and upsert ``ON CONFLICT DO UPDATE`` fails inside SQLite itself
(the triggers ``create_schema`` installs) — on a primary-key collision *and* on a collision with
any UNIQUE group, through a bare ``sqlite3`` connection without the ``recursive_triggers``
pragma — the ORM guard refuses the same changes earlier with ``AppendOnlyViolation``, and the
``postgres``-marked variant executes the plpgsql triggers against a real server.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from nour.core.clock import FakeClock, IdGenerator
from nour.core.errors import AppendOnlyViolation, DeskWallViolation
from nour.core.tokens import AnyToken
from nour.db.base import Base
from nour.db.engine import create_schema, make_engine, table_flags, unique_groups
from nour.db.models import (
    APPEND_ONLY_TABLES,
    AuditEventRow,
    AuditorReportRow,
    CardAuthorizationRow,
    DecisionJournalRow,
    FoundInstructionRow,
    ModelTraceRow,
    PassphraseAttemptRow,
    TimerSlotRow,
)
from nour.db.session import SessionFactory
from tests.unit.test_models_columns import seed_all, uniquified

# a text column to try to rewrite, per append-only table
REWRITE: dict[str, str] = {
    "audit_event": "reason",
    "decision_journal": "reason_text",
    "passphrase_attempt": "outcome",
    "found_instruction": "quote",
    "card_authorization": "merchant",
    "timer_slot": "timer_name",
    "model_trace": "vendor",
    "auditor_report": "summary",
}

MAPPERS: dict[str, type[Any]] = {
    "audit_event": AuditEventRow,
    "decision_journal": DecisionJournalRow,
    "passphrase_attempt": PassphraseAttemptRow,
    "found_instruction": FoundInstructionRow,
    "card_authorization": CardAuthorizationRow,
    "timer_slot": TimerSlotRow,
    "model_trace": ModelTraceRow,
    "auditor_report": AuditorReportRow,
}

# the token whose scope may write each table through the ORM
WRITER: dict[str, str] = {
    "audit_event": "operator",
    "decision_journal": "operator",
    "passphrase_attempt": "governance",
    "found_instruction": "operator",
    "card_authorization": "operator",
    "timer_slot": "governance",
    "model_trace": "operator",
}


def _q(name: str) -> str:
    return f'"{name}"'


@pytest.fixture
def seeded(engine: Engine, clock: FakeClock, idgen: IdGenerator) -> dict[str, dict[str, Any]]:
    return seed_all(engine, clock, idgen)


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.exec_driver_sql(f"SELECT count(*) FROM {_q(table)}").scalar() or 0)


def test_the_eight_tables_are_flagged_and_listed() -> None:
    assert set(REWRITE) == set(MAPPERS) == APPEND_ONLY_TABLES
    assert all(MAPPERS[name].__append_only__ for name in APPEND_ONLY_TABLES)
    for table in Base.metadata.sorted_tables:
        assert table_flags(table)["append_only"] == (table.name in APPEND_ONLY_TABLES)


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_raw_sql_update_and_delete_fail_at_the_database(
    table: str, seeded: dict[str, dict[str, Any]], engine: Engine
) -> None:
    column = REWRITE[table]
    assert _count(engine, table) == 1
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.exec_driver_sql(f"UPDATE {_q(table)} SET {_q(column)} = 'rewritten'")
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.exec_driver_sql(f"DELETE FROM {_q(table)}")
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.exec_driver_sql(f"UPDATE {_q(table)} SET {_q(column)} = 'rewritten' WHERE 1 = 1")
    with engine.connect() as connection:
        value = connection.exec_driver_sql(f"SELECT {_q(column)} FROM {_q(table)}").scalar()
    assert value == seeded[table][column]
    assert _count(engine, table) == 1


@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_replace_and_upsert_cannot_rewrite_a_row(
    table: str, seeded: dict[str, dict[str, Any]], engine: Engine
) -> None:
    """``INSERT OR REPLACE`` deletes the conflicting row without a BEFORE DELETE trigger unless
    recursive triggers are on; the ``_no_replace`` BEFORE INSERT trigger closes that path on
    any connection, and ``ON CONFLICT DO UPDATE`` hits the update trigger."""
    row = seeded[table]
    column_list, placeholders, params = driver_row(table, row)
    rewrite = REWRITE[table]
    for prefix in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.exec_driver_sql(
                f"{prefix} {_q(table)} ({column_list}) VALUES ({placeholders})", params
            )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.exec_driver_sql(
            f"INSERT INTO {_q(table)} ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(\"id\") DO UPDATE SET {_q(rewrite)} = 'rewritten'",
            params,
        )
    with engine.connect() as connection:
        value = connection.exec_driver_sql(f"SELECT {_q(rewrite)} FROM {_q(table)}").scalar()
    assert value == row[rewrite] and _count(engine, table) == 1


UNIQUE_CASES: list[tuple[str, tuple[str, ...]]] = sorted(
    (table.name, group)
    for table in Base.metadata.sorted_tables
    if table.name in APPEND_ONLY_TABLES
    for group in unique_groups(table)
)


@pytest.mark.parametrize(
    ("table", "group"), UNIQUE_CASES, ids=lambda v: v if isinstance(v, str) else "+".join(v)
)
def test_replace_on_a_unique_collision_cannot_rewrite_a_row_on_a_bare_connection(
    table: str,
    group: tuple[str, ...],
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    idgen: IdGenerator,
) -> None:
    """SQLite's REPLACE resolution also deletes a row that collides on a UNIQUE group, without a
    BEFORE DELETE firing unless ``recursive_triggers`` is on — which only ``make_engine`` sets.
    A plain ``sqlite3`` connection (no pragma) inserting a *fresh* id with the seeded unique
    values must still be refused by the ``_no_replace`` trigger, for every unique group."""
    original = seeded[table]
    fresh = uniquified(Base.metadata.tables[_key(table)], original, idgen)
    for column in group:
        fresh[column] = original[column]
    marker = REWRITE[table] if REWRITE[table] not in group else "id"
    if marker != "id":
        fresh[marker] = "REWRITTEN"
    column_list, placeholders, params = driver_row(table, fresh)
    raw = sqlite3.connect(engine.url.database or "")
    try:
        assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        for prefix in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                raw.execute(f"{prefix} {_q(table)} ({column_list}) VALUES ({placeholders})", params)
        rows = raw.execute(f"SELECT {_q(marker)} FROM {_q(table)}").fetchall()
    finally:
        raw.close()
    assert rows == [(original[marker],)]  # the seeded row, untouched; the fresh id never landed


def _key(table: str) -> str:
    return "auditor.auditor_report" if table == "auditor_report" else table


def driver_row(table: str, row: dict[str, Any]) -> tuple[str, str, tuple[Any, ...]]:
    """``(column list, placeholders, params)`` for a driver-level insert of ``row``."""
    columns = [c for c in Base.metadata.tables[_key(table)].columns.keys() if c in row]
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(_q(c) for c in columns)
    return column_list, placeholders, tuple(_sql_value(row[c]) for c in columns)


def _sql_value(value: Any) -> Any:
    """Bind-ready value for a driver-level insert of a sample row, encoded exactly as
    ``UtcDateTime`` / ``JSON`` store it (UTC, naive, ``YYYY-MM-DD HH:MM:SS.ffffff``), so a
    datetime unique key such as ``timer_slot.slot`` really collides."""
    if isinstance(value, dict | list):
        return json.dumps(value)
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
        )
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return bytes(value)
    return value


@pytest.mark.parametrize("table", sorted(WRITER))
def test_orm_update_and_delete_raise_before_the_database_is_reached(
    table: str,
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> None:
    mapper = MAPPERS[table]
    column = REWRITE[table]
    factory = session_factory(tokens[WRITER[table]])
    with pytest.raises(AppendOnlyViolation), factory.write() as session:
        row = session.scalars(select(mapper)).one()
        setattr(row, column, "rewritten")
        session.flush()
    with pytest.raises(AppendOnlyViolation), factory.write() as session:
        row = session.scalars(select(mapper)).one()
        session.delete(row)
        session.flush()
    with pytest.raises(AppendOnlyViolation), factory.write() as session:
        session.execute(update(mapper).values({column: "rewritten"}))
    with pytest.raises(AppendOnlyViolation), factory.write() as session:
        session.execute(delete(mapper))
    with engine.connect() as connection:
        value = connection.exec_driver_sql(f"SELECT {_q(column)} FROM {_q(table)}").scalar()
    assert value == seeded[table][column]


def test_auditor_report_cannot_change_even_through_the_auditors_own_writer(
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> None:
    auditor = session_factory(tokens["auditor"])
    with pytest.raises((DeskWallViolation, AppendOnlyViolation)), auditor.append_report() as s:
        report = s.scalars(select(AuditorReportRow)).one()
        report.summary = "rewritten"
        s.flush()
    with pytest.raises((DeskWallViolation, AppendOnlyViolation)), auditor.append_report() as s:
        s.execute(update(AuditorReportRow).values(summary="rewritten"))
    with pytest.raises((DeskWallViolation, AppendOnlyViolation)), auditor.append_report() as s:
        s.execute(delete(AuditorReportRow))
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT summary FROM auditor_report").scalar()
            == (seeded["auditor_report"]["summary"])
        )


def test_create_schema_installed_the_three_triggers_per_table(engine: Engine) -> None:
    with engine.connect() as connection:
        names = {
            name
            for (name,) in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    for name in APPEND_ONLY_TABLES:
        assert {f"{name}_no_update", f"{name}_no_delete", f"{name}_no_replace"} <= names
    for other in Base.metadata.sorted_tables:
        if other.name not in APPEND_ONLY_TABLES:
            assert f"{other.name}_no_update" not in names


# --------------------------------------------------------------------------- postgres variant

PG_URL = os.environ.get("NOUR_DATABASE_URL", "")


@pytest.mark.postgres
@pytest.mark.skipif(not PG_URL.startswith("postgresql"), reason="NOUR_DATABASE_URL is not Postgres")
@pytest.mark.parametrize("table", sorted(APPEND_ONLY_TABLES))
def test_postgres_triggers_refuse_update_and_delete(
    table: str, clock: FakeClock, idgen: IdGenerator
) -> None:
    engine = make_engine(PG_URL)
    try:
        Base.metadata.drop_all(engine)
        create_schema(engine, Base.metadata)
        seeded = seed_all(engine, clock, idgen)
        column = REWRITE[table]
        qualified = f'"auditor"."{table}"' if table == "auditor_report" else _q(table)
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.exec_driver_sql(f"UPDATE {qualified} SET {_q(column)} = 'rewritten'")
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
            connection.exec_driver_sql(f"DELETE FROM {qualified}")
        with engine.connect() as connection:
            value = connection.exec_driver_sql(f"SELECT {_q(column)} FROM {qualified}").scalar()
        assert value == seeded[table][column]
    finally:
        engine.dispose()
