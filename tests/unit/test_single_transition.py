"""Single-transition, immutable and forward-only columns on the real models (DESIGN §3.7, §4c,
§5, §7.3 ``tests/unit/test_single_transition.py``; SPEC §6 §10 §12): ``approval`` decides once,
``release`` burns once, ``handoff`` is taken once, ``freeze_state`` is released once,
``second_channel_challenge`` is consumed once, ``pending_owner_message`` is sent once,
``readback_pending`` is confirmed once, ``incident`` is resolved once; ``transaction.status``
only moves forward. "Insert + ONE transition" holds end to end: the rows of those tables are
never deleted or replaced (raw ``DELETE``, ``INSERT OR REPLACE``, ``REPLACE INTO``, an upsert
and an ORM delete all fail, so a burnt nonce or an answered approval cannot be re-armed by
re-inserting it), and every other column of ``approval``, ``release``, ``handoff``, ``incident``
and ``document`` is immutable after INSERT. Both layers are proved: the ORM guard
(``SingleTransitionViolation`` / ``AppendOnlyViolation``) and the SQLite triggers on raw SQL.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from nour.core.clock import FakeClock, IdGenerator
from nour.core.errors import AppendOnlyViolation, SingleTransitionViolation
from nour.core.tokens import AnyToken
from nour.core.types import Desk
from nour.db.base import Base
from nour.db.engine import table_flags, trigger_ddl
from nour.db.models import (
    APPEND_ONLY_TABLES,
    TRANSACTION_STATES,
    ApprovalRow,
    DocumentRow,
    FreezeStateRow,
    HandoffRow,
    IncidentRow,
    PendingOwnerMessageRow,
    ReadbackPendingRow,
    ReleaseRow,
    SecondChannelChallengeRow,
    TransactionRow,
)
from nour.db.session import SessionFactory
from tests.unit.test_append_only import driver_row
from tests.unit.test_models_columns import COAT, EXPECTED_IMMUTABLE, seed_all, uniquified

EXPECTED_SINGLE_TRANSITION: dict[str, tuple[str, ...]] = {
    "approval": (
        "decided_at",
        "decision",
        "reason",
        "passphrase_verified",
        "second_channel_confirmed",
        "decided_via",
        "decided_by_event_id",
    ),
    "release": ("burnt_at",),
    "handoff": ("taken_at",),
    "freeze_state": ("released_at", "released_by_approval_id"),
    "second_channel_challenge": ("consumed_at", "consumed_by", "approved"),
    "pending_owner_message": ("sent_at", "provider_msg_id"),
    "readback_pending": ("confirmed_at", "confirmed_by_event_id"),
    "incident": ("resolved_at", "postmortem_ref"),
}

KEPT_ROW_MAPPERS: dict[str, type[Any]] = {
    "approval": ApprovalRow,
    "release": ReleaseRow,
    "handoff": HandoffRow,
    "freeze_state": FreezeStateRow,
    "second_channel_challenge": SecondChannelChallengeRow,
    "pending_owner_message": PendingOwnerMessageRow,
    "readback_pending": ReadbackPendingRow,
    "incident": IncidentRow,
}

# the token whose scope and grants may write each kept-rows table through the ORM
KEPT_ROW_WRITER: dict[str, str] = {
    "approval": "operator",
    "release": "operator",
    "handoff": "operator",
    "freeze_state": "governance",
    "second_channel_challenge": "governance",
    "pending_owner_message": "assistant",
    "readback_pending": "assistant",
    "incident": "operator",
}

# (mapper, table, column, first value, second value, writing token)
CASES: list[tuple[type[Any], str, str, Any, Any, str]] = [
    (ApprovalRow, "approval", "decision", "approve", "reject", "operator"),
    (ApprovalRow, "approval", "decided_via", "owner_thread", "second_channel", "operator"),
    (ReleaseRow, "release", "burnt_at", "T1", "T2", "operator"),
    (HandoffRow, "handoff", "taken_at", "T1", "T2", "operator"),
    (FreezeStateRow, "freeze_state", "released_at", "T1", "T2", "governance"),
    (FreezeStateRow, "freeze_state", "released_by_approval_id", "A1", "A2", "governance"),
    (
        SecondChannelChallengeRow,
        "second_channel_challenge",
        "consumed_by",
        "owner",
        "x",
        "governance",
    ),
    (SecondChannelChallengeRow, "second_channel_challenge", "approved", True, False, "governance"),
    (PendingOwnerMessageRow, "pending_owner_message", "sent_at", "T1", "T2", "assistant"),
    (PendingOwnerMessageRow, "pending_owner_message", "provider_msg_id", "m1", "m2", "assistant"),
    (ReadbackPendingRow, "readback_pending", "confirmed_at", "T1", "T2", "assistant"),
    (IncidentRow, "incident", "resolved_at", "T1", "T2", "operator"),
    (IncidentRow, "incident", "postmortem_ref", "postmortems/1.md", "postmortems/2.md", "operator"),
]


def _value(raw: Any, clock: FakeClock, idgen: IdGenerator) -> Any:
    if raw == "T1":
        return clock.now()
    if raw == "T2":
        return clock.now() + timedelta(minutes=5)
    if raw in ("A1", "A2"):
        return idgen.new()
    return raw


def _q(name: str) -> str:
    return f'"{name}"'


@pytest.fixture
def seeded(engine: Engine, clock: FakeClock, idgen: IdGenerator) -> dict[str, dict[str, Any]]:
    return seed_all(engine, clock, idgen)


def _sqlite_triggers(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {
            name
            for (name,) in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }


# --------------------------------------------------------------------------- markers and triggers


def test_markers_match_the_design() -> None:
    for table in Base.metadata.sorted_tables:
        flags = table_flags(table)
        assert flags["single_transition"] == EXPECTED_SINGLE_TRANSITION.get(table.name, ()), (
            table.name
        )
        assert set(flags["immutable"]) == EXPECTED_IMMUTABLE.get(table.name, set()), table.name
        if table.name == "transaction":
            assert flags["forward_only"] == {"status": list(TRANSACTION_STATES)}
        else:
            assert flags["forward_only"] == {}, table.name
    assert set(KEPT_ROW_MAPPERS) == set(EXPECTED_SINGLE_TRANSITION)
    assert TransactionRow.__forward_only__["status"] == [
        "prepared",
        "authorized",
        "declined",
        "released",
        "settled",
        "reconciled",
    ]


def test_triggers_exist_for_every_marked_column_and_table(engine: Engine) -> None:
    names = _sqlite_triggers(engine)
    for table, columns in EXPECTED_SINGLE_TRANSITION.items():
        for column in columns:
            assert f"{table}_{column}_single_transition" in names
        # insert + ONE transition: the rows are kept (no DELETE, no REPLACE)
        assert {f"{table}_no_delete", f"{table}_no_replace"} <= names, table
    for table in EXPECTED_IMMUTABLE:
        assert f"{table}_immutable" in names, table
    assert "transaction_status_forward_only" in names
    for other in Base.metadata.sorted_tables:
        if other.name not in EXPECTED_IMMUTABLE:
            assert f"{other.name}_immutable" not in names, other.name
        if other.name not in EXPECTED_SINGLE_TRANSITION and other.name not in APPEND_ONLY_TABLES:
            assert f"{other.name}_no_delete" not in names, other.name
    ddl = "\n".join(trigger_ddl("sqlite", Base.metadata))
    assert ddl.count("_single_transition") == sum(map(len, EXPECTED_SINGLE_TRANSITION.values()))
    assert ddl.count('_immutable"') == len(EXPECTED_IMMUTABLE)
    assert ddl.count('_no_delete"') == len(EXPECTED_SINGLE_TRANSITION) + len(APPEND_ONLY_TABLES)


def test_immutable_triggers_name_every_frozen_column() -> None:
    """One SQLite trigger per table lists every immutable column in its ``UPDATE OF`` and names
    the column it refuses; the Postgres function checks each with ``IS DISTINCT FROM``."""
    sqlite_ddl = trigger_ddl("sqlite", Base.metadata)
    pg_ddl = "\n".join(trigger_ddl("postgresql", Base.metadata))
    for table, columns in EXPECTED_IMMUTABLE.items():
        statement = next(
            s
            for s in sqlite_ddl
            if s.startswith(f'CREATE TRIGGER IF NOT EXISTS "{table}_immutable"')
        )
        for column in columns:
            assert f'"{column}"' in statement.split(" ON ")[0], (table, column)
            assert (
                f'RAISE(ABORT, \'{table}.{column} is immutable\') WHERE NEW."{column}" IS NOT OLD."{column}"'
                in statement
            )
            assert (
                f'IF NEW."{column}" IS DISTINCT FROM OLD."{column}" THEN RAISE EXCEPTION \'{table}.{column} is immutable\''
                in pg_ddl
            )
        assert f'CREATE TRIGGER "{table}_immutable" BEFORE UPDATE OF' in pg_ddl
        assert f"EXECUTE FUNCTION nour_immutable_{table}()" in pg_ddl


# --------------------------------------------------------------------------- single transition


@pytest.mark.parametrize(("mapper", "table", "column", "first", "second", "kind"), CASES)
def test_column_is_set_once_through_the_orm(
    mapper: type[Any],
    table: str,
    column: str,
    first: Any,
    second: Any,
    kind: str,
    seeded: dict[str, dict[str, Any]],
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
    idgen: IdGenerator,
) -> None:
    factory = session_factory(tokens[kind])
    value1, value2 = _value(first, clock, idgen), _value(second, clock, idgen)
    with factory.write() as session:  # NULL -> value: allowed
        row = session.scalars(select(mapper)).one()
        assert getattr(row, column) is None
        setattr(row, column, value1)
    with pytest.raises(SingleTransitionViolation, match="may not change"), factory.write() as s:
        row = s.scalars(select(mapper)).one()
        setattr(row, column, value2)
        s.flush()
    with pytest.raises(SingleTransitionViolation), factory.write() as s:  # unsetting is a change
        row = s.scalars(select(mapper)).one()
        setattr(row, column, None)
        s.flush()
    with factory.session() as session:
        assert getattr(session.scalars(select(mapper)).one(), column) == value1


@pytest.mark.parametrize(("mapper", "table", "column", "first", "second", "kind"), CASES)
def test_column_is_set_once_at_the_database(
    mapper: type[Any],
    table: str,
    column: str,
    first: Any,
    second: Any,
    kind: str,
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    clock: FakeClock,
    idgen: IdGenerator,
) -> None:
    target = Base.metadata.tables[table]
    value1, value2 = _value(first, clock, idgen), _value(second, clock, idgen)
    with engine.begin() as connection:
        connection.execute(update(target).values({column: value1}))
    with pytest.raises(DBAPIError, match="may be set once"), engine.begin() as connection:
        connection.execute(update(target).values({column: value2}))
    with pytest.raises(DBAPIError, match="may be set once"), engine.begin() as connection:
        connection.execute(update(target).values({column: None}))
    with engine.begin() as connection:  # the same value again is not a change
        connection.execute(update(target).values({column: value1}))
    with engine.connect() as connection:
        stored = connection.execute(select(target.c[column])).scalar()
    assert stored == value1


def test_approval_decision_columns_move_together_once(
    seeded: dict[str, dict[str, Any]],
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
    idgen: IdGenerator,
) -> None:
    factory = session_factory(tokens["assistant"])
    with factory.write() as session:
        approval = session.scalars(select(ApprovalRow)).one()
        approval.decided_at = clock.now()
        approval.decision = "approve"
        approval.reason = "Looks right."
        approval.passphrase_verified = True
        approval.second_channel_confirmed = False
        approval.decided_via = "owner_thread"
        approval.decided_by_event_id = idgen.new()
    with pytest.raises(SingleTransitionViolation), factory.write() as session:
        approval = session.scalars(select(ApprovalRow)).one()
        approval.decision = "reject"
        session.flush()
    # a bulk update carries no row history for the ORM guard: the trigger is the wall
    with pytest.raises(DBAPIError, match="may be set once"), factory.write() as session:
        session.execute(update(ApprovalRow).values(passphrase_verified=False))
    with factory.session() as session:
        approval = session.scalars(select(ApprovalRow)).one()
        assert approval.decision == "approve" and approval.passphrase_verified is True


def test_bulk_update_through_the_orm_is_checked_by_the_trigger(
    seeded: dict[str, dict[str, Any]],
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
) -> None:
    """A bulk ``update(Model)`` carries no row history for the ORM guard; the trigger is the
    wall that still holds."""
    factory = session_factory(tokens["operator"])
    with factory.write() as session:
        session.execute(update(ReleaseRow).values(burnt_at=clock.now()))
    with pytest.raises(DBAPIError, match="may be set once"), factory.write() as session:
        session.execute(update(ReleaseRow).values(burnt_at=clock.now() + timedelta(seconds=1)))


# --------------------------------------------------------------------------- kept rows


@pytest.mark.parametrize("table", sorted(KEPT_ROW_MAPPERS))
def test_rows_of_a_single_transition_table_are_never_deleted_or_replaced_at_the_database(
    table: str, seeded: dict[str, dict[str, Any]], engine: Engine, idgen: IdGenerator
) -> None:
    """The DB wall of DESIGN §4c: raw ``DELETE``, SQLite's ``INSERT OR REPLACE`` / ``REPLACE
    INTO`` (same key, or a fresh id colliding on a UNIQUE group such as ``release.nonce``) and
    an upsert ``ON CONFLICT DO UPDATE`` that would reset the transition all fail."""
    target = Base.metadata.tables[table]
    row = seeded[table]
    transition = EXPECTED_SINGLE_TRANSITION[table][0]
    with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
        c.exec_driver_sql(f"DELETE FROM {_q(table)}")
    with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
        c.exec_driver_sql(f"DELETE FROM {_q(table)} WHERE id = ?", (row["id"],))
    same_key = dict(row)
    columns, placeholders, params = driver_row(table, same_key)
    for prefix in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
        with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
            c.exec_driver_sql(f"{prefix} {_q(table)} ({columns}) VALUES ({placeholders})", params)
    upsert = sqlite_insert(target).values(**row)
    upsert = upsert.on_conflict_do_update(index_elements=["id"], set_={transition: None})
    with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
        c.execute(upsert)
    # a fresh id that collides on a UNIQUE group (release.nonce, challenge.token_hash, ...)
    fresh = uniquified(target, row, idgen)
    for group in [g for g in _unique_groups(table)]:
        colliding = dict(fresh)
        for column in group:
            colliding[column] = row[column]
        columns, placeholders, params = driver_row(table, colliding)
        with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
            c.exec_driver_sql(
                f"INSERT OR REPLACE INTO {_q(table)} ({columns}) VALUES ({placeholders})", params
            )
    with engine.connect() as connection:
        stored = connection.execute(select(target)).all()
    assert len(stored) == 1 and stored[0]._mapping["id"] == row["id"]


def _unique_groups(table: str) -> list[tuple[str, ...]]:
    from nour.db.engine import unique_groups

    return unique_groups(Base.metadata.tables[table])


@pytest.mark.parametrize("table", sorted(KEPT_ROW_MAPPERS))
def test_rows_of_a_single_transition_table_cannot_be_deleted_through_the_orm(
    table: str,
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> None:
    mapper = KEPT_ROW_MAPPERS[table]
    factory = session_factory(tokens[KEPT_ROW_WRITER[table]])
    with pytest.raises(AppendOnlyViolation, match="never deleted"), factory.write() as session:
        row = session.scalars(select(mapper)).one()
        session.delete(row)
        session.flush()
    with pytest.raises(AppendOnlyViolation, match="never deleted"), factory.write() as session:
        session.execute(delete(mapper))
    with engine.connect() as connection:
        assert connection.exec_driver_sql(f"SELECT count(*) FROM {_q(table)}").scalar() == 1


def test_a_burnt_release_cannot_be_re_armed(
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
    idgen: IdGenerator,
) -> None:
    """The blocker scenario: burn the token, then try every way of putting an unburnt row with
    the same nonce back — delete + re-insert in one unit of work, ``INSERT OR REPLACE``, an
    upsert, or re-binding the nonce / call / tier of the burnt row."""
    factory = session_factory(tokens["operator"])
    release = seeded["release"]
    with factory.write() as session:
        session.execute(update(ReleaseRow).values(burnt_at=clock.now()))
    with pytest.raises(AppendOnlyViolation, match="never deleted"), factory.write() as session:
        row = session.scalars(select(ReleaseRow)).one()
        session.delete(row)
        session.flush()
        session.add(
            ReleaseRow(
                id=idgen.new(),
                desk=Desk.OPERATOR,
                call_id=release["call_id"],
                nonce=release["nonce"],
                tier="K",
                approval_id=None,
                minted_at=clock.now(),
                minted_by="approval",
                burnt_at=None,
            )
        )
    unburnt = {**release, "id": idgen.new(), "burnt_at": None}
    columns, placeholders, params = driver_row("release", unburnt)
    with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
        c.exec_driver_sql(
            f"INSERT OR REPLACE INTO release ({columns}) VALUES ({placeholders})", params
        )
    with pytest.raises(IntegrityError, match="never deleted or replaced"), engine.begin() as c:
        # even a plain INSERT of the same nonce is refused by the BEFORE INSERT trigger — it fires
        # before the unique index is checked, which is what closes INSERT OR REPLACE
        c.exec_driver_sql(f"INSERT INTO release ({columns}) VALUES ({placeholders})", params)
    for column, value in (("nonce", "nonce-2"), ("call_id", idgen.new()), ("tier", "K")):
        with pytest.raises(DBAPIError, match=f"release.{column} is immutable"), engine.begin() as c:
            c.exec_driver_sql(f"UPDATE release SET {column} = ?", (value,))
        with pytest.raises(SingleTransitionViolation, match="immutable"), factory.write() as s:
            row = s.scalars(select(ReleaseRow)).one()
            setattr(row, column, value)
            s.flush()
        with pytest.raises(SingleTransitionViolation, match="immutable"), factory.write() as s:
            s.execute(update(ReleaseRow).values({column: value}))
    with factory.session() as session:
        row = session.scalars(select(ReleaseRow)).one()
        assert row.nonce == release["nonce"] and row.burnt_at is not None and row.tier == "A"


def test_an_approval_is_frozen_except_for_its_one_decision(
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
    idgen: IdGenerator,
) -> None:
    """Before the decision only the decision columns may change; after it nothing may (DESIGN
    §5 "UPDATE allowed only while decision IS NULL", enforced the stronger way: the identity
    and money columns are immutable from INSERT)."""
    factory = session_factory(tokens["operator"])
    approval = seeded["approval"]
    frozen = {
        "amount": 9_999_999,
        "action_json": '{"rewritten": true}',
        "item_ref": "draft:other",
        "seq": 999,
        "category": "x",
        "currency": "USD",
        "expires_at": "2099-01-01 00:00:00.000000",
        "trigger_event_id": idgen.new(),
    }
    for stage in ("before", "after"):
        for column, value in frozen.items():
            with (
                pytest.raises(DBAPIError, match=f"approval.{column} is immutable"),
                engine.begin() as c,
            ):
                c.exec_driver_sql(f"UPDATE approval SET {column} = ?", (value,))
        with pytest.raises(SingleTransitionViolation, match="immutable"), factory.write() as s:
            row = s.scalars(select(ApprovalRow)).one()
            row.amount = 9_999_999
            s.flush()
        with pytest.raises(SingleTransitionViolation, match="immutable"), factory.write() as s:
            row = s.scalars(select(ApprovalRow)).one()
            row.action_json = {"rewritten": True}
            s.flush()
        with pytest.raises(SingleTransitionViolation, match="immutable"), factory.write() as s:
            s.execute(update(ApprovalRow).values(amount=1))
        if stage == "before":
            with factory.write() as s:  # the one transition
                row = s.scalars(select(ApprovalRow)).one()
                row.decided_at = clock.now()
                row.decision = "approve"
                row.decided_via = "owner_thread"
                row.decided_by_event_id = idgen.new()
                row.passphrase_verified = True
    with pytest.raises(DBAPIError, match="may be set once"), engine.begin() as c:
        c.exec_driver_sql("UPDATE approval SET decision = 'reject'")
    with engine.begin() as c:  # rewriting the same values is not a change
        c.exec_driver_sql(
            "UPDATE approval SET amount = ?, seq = ?", (approval["amount"], approval["seq"])
        )
    with factory.session() as session:
        row = session.scalars(select(ApprovalRow)).one()
        assert row.amount == approval["amount"] and row.seq == approval["seq"]
        assert row.action_json == approval["action_json"] and row.decision == "approve"


def test_document_versions_are_immutable_except_the_share_log(
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> None:
    factory = session_factory(tokens["assistant"])
    document = seeded["document"]
    for column, value in (
        ("storage_ref", "vault/objects/other"),
        ("sha256", "sha256:tampered"),
        ("title", "renamed"),
        ("content_text", "rewritten"),
        ("tier", 0),
        ("metadata", '{"pages": 99}'),
    ):
        with (
            pytest.raises(DBAPIError, match=f"document.{column} is immutable"),
            engine.begin() as c,
        ):
            c.exec_driver_sql(f"UPDATE document SET {column} = ?", (value,))
    attribute_cases: list[tuple[str, Any]] = [
        ("storage_ref", "x"),
        ("sha256", "y"),
        ("meta", {"pages": 99}),  # the attribute behind the ``metadata`` column
    ]
    for attribute, value in attribute_cases:
        with pytest.raises(SingleTransitionViolation, match="immutable"), factory.write() as s:
            row = s.scalars(select(DocumentRow)).one()
            setattr(row, attribute, value)
            s.flush()
    with factory.write() as session:  # the share log appends
        row = session.scalars(select(DocumentRow)).one()
        row.share_log = [*row.share_log, {"to": "bank", "at": "2026-10-05T07:00:00+04:00"}]
    with factory.session() as session:
        row = session.scalars(select(DocumentRow)).one()
        assert row.share_log == [{"to": "bank", "at": "2026-10-05T07:00:00+04:00"}]
        assert row.storage_ref == document["storage_ref"] and row.sha256 == document["sha256"]


def test_incident_rows_change_only_to_resolve(
    seeded: dict[str, dict[str, Any]],
    engine: Engine,
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
    clock: FakeClock,
) -> None:
    factory = session_factory(tokens["operator"])
    for column, value in (
        ("type", "kill_switch"),
        ("detected_by", "owner"),
        ("first_response", "nothing"),
        ("frozen_scope", "all_outbound"),
        ("details", '{"edited": true}'),
    ):
        with (
            pytest.raises(DBAPIError, match=f"incident.{column} is immutable"),
            engine.begin() as c,
        ):
            c.exec_driver_sql(f"UPDATE incident SET {column} = ?", (value,))
    with factory.write() as session:
        row = session.scalars(select(IncidentRow)).one()
        row.resolved_at = clock.now()
        row.postmortem_ref = "postmortems/1.md"
    with pytest.raises(SingleTransitionViolation, match="may not change"), factory.write() as s:
        row = s.scalars(select(IncidentRow)).one()
        row.resolved_at = clock.now() + timedelta(hours=1)
        s.flush()
    with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
        c.exec_driver_sql("DELETE FROM incident")
    with factory.session() as session:
        row = session.scalars(select(IncidentRow)).one()
        assert row.postmortem_ref == "postmortems/1.md" and row.type == "instruction_in_content"


# --------------------------------------------------------------------------- forward-only


def test_transaction_status_moves_forward_only_through_the_orm(
    seeded: dict[str, dict[str, Any]],
    session_factory: Callable[[AnyToken], SessionFactory],
    tokens: dict[str, AnyToken],
) -> None:
    factory = session_factory(tokens["operator"])
    for state in ("authorized", "released", "settled"):
        with factory.write() as session:
            tx = session.scalars(select(TransactionRow)).one()
            tx.status = state
    for backwards in ("released", "prepared", "authorized"):
        with pytest.raises(SingleTransitionViolation), factory.write() as session:
            tx = session.scalars(select(TransactionRow)).one()
            tx.status = backwards
            session.flush()
    with pytest.raises(SingleTransitionViolation, match="unknown state"), factory.write() as s:
        tx = s.scalars(select(TransactionRow)).one()
        tx.status = "bogus"
        s.flush()
    with factory.write() as session:
        tx = session.scalars(select(TransactionRow)).one()
        tx.status = "reconciled"
    with factory.session() as session:
        assert session.scalars(select(TransactionRow.status)).one() == "reconciled"


def test_transaction_status_moves_forward_only_at_the_database(
    seeded: dict[str, dict[str, Any]], engine: Engine
) -> None:
    target = Base.metadata.tables["transaction"]
    with engine.begin() as connection:
        connection.execute(update(target).values(status="declined"))
    with pytest.raises(DBAPIError, match="may only move forward"), engine.begin() as connection:
        connection.execute(update(target).values(status="authorized"))
    with pytest.raises(DBAPIError, match="may only move forward"), engine.begin() as connection:
        connection.execute(update(target).values(status="prepared"))
    with pytest.raises((DBAPIError, IntegrityError)), engine.begin() as connection:
        connection.execute(update(target).values(status="bogus"))  # CHECK and trigger both refuse
    with engine.begin() as connection:
        connection.execute(update(target).values(status="declined"))  # same state: not a change
        connection.execute(update(target).values(status="reconciled"))
    with engine.connect() as connection:
        assert connection.execute(select(target.c.status)).scalar() == "reconciled"


def test_sample_rows_reference_the_coat(seeded: dict[str, dict[str, Any]]) -> None:
    assert seeded["approval"]["coat_id"] == COAT
