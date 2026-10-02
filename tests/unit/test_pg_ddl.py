"""Postgres DDL for the real models (DESIGN §4a, §5.3; THREAT_REVIEW 6.1 "make the Postgres wall
real"): the strings exist for every table — roles with NOLOGIN/NOSUPERUSER/NOBYPASSRLS, grants by
scope, REVOKE on append-only tables, ENABLE + FORCE row-level security keyed on ``current_user``
on every DESK_ROW table and on the desk-partitioned ``inbox_event`` and ``pending_owner_message``
(``DESK_PARTITIONED_TABLES``), per-role verb and column
grants (``GRANTS_KEY``: inbox ack-only and insert-without-authenticity, handoff one-way, no DELETE
on approval / release / inbox / handoff / DNC / freeze, governance-written card and coat), the
kept-rows and immutable-column triggers, the ``auditor`` schema — and, under ``@postgres``, they
execute: a desk role cannot read the other desk's rows even after ``SET nour.desk``, cannot select
``document``, the Assistant's inbox bodies or the Assistant's drafts for the owner, cannot
rewrite ``inbox_event.passphrase_attempt``,
cannot delete a release or re-price an approval, loads the owner row without its passphrase
columns, and no runtime role is a superuser or a table owner.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from nour.core.clock import FakeClock, IdGenerator
from nour.db.base import Base, Scope
from nour.db.engine import (
    ALL_COLUMNS,
    ALL_ROLES,
    ASSISTANT_ROLE,
    AUDITOR_ROLE,
    DESK_ROLES,
    GOVERNANCE_ROLES,
    INGRESS_ROLE,
    OPERATOR_ROLE,
    SCHEDULER_ROLE,
    create_schema,
    grants_of,
    hidden_columns,
    make_engine,
    pg_roles_ddl,
    pg_schema_ddl,
    role_desk_case,
    table_flags,
    trigger_ddl,
)
from nour.db.models import (
    APPEND_ONLY_TABLES,
    AUDITOR_SCHEMA,
    DESK_PARTITIONED_TABLES,
    INBOX_AUTH_COLUMNS,
    OwnerRow,
)
from tests.unit.test_models_columns import COAT, EXPECTED_GRANTS, EXPECTED_IMMUTABLE, seed_all

DDL = pg_roles_ddl(Base.metadata)
JOINED = "\n".join(DDL)
TRIGGERS = trigger_ddl("postgresql", Base.metadata)
TRIGGERS_JOINED = "\n".join(TRIGGERS)


def _qualified(table_name: str) -> str:
    return (
        f'"{AUDITOR_SCHEMA}"."{table_name}"'
        if table_name == "auditor_report"
        else f'"{table_name}"'
    )


def _lines(table_name: str) -> list[str]:
    target = _qualified(table_name)
    return [line for line in DDL if f" ON {target} " in line or line.endswith(f" ON {target}")]


# --------------------------------------------------------------------------- strings


def test_roles_are_created_nologin_and_never_superuser_or_bypassrls() -> None:
    for role in ALL_ROLES:
        create = next(line for line in DDL if f'CREATE ROLE "{role}"' in line)
        assert "NOLOGIN" in create and "NOSUPERUSER" in create and "NOBYPASSRLS" in create
        assert "NOCREATEROLE" in create and "NOCREATEDB" in create
        assert "IF NOT EXISTS (SELECT 1 FROM pg_roles" in create  # idempotent
        assert "out of band" in create  # the password note travels with the role
    assert f'ALTER ROLE "{AUDITOR_ROLE}" SET default_transaction_read_only = on' in JOINED
    assert set(ALL_ROLES) == {
        OPERATOR_ROLE,
        ASSISTANT_ROLE,
        INGRESS_ROLE,
        SCHEDULER_ROLE,
        AUDITOR_ROLE,
    }


def test_every_table_is_revoked_from_public_and_granted_by_scope() -> None:
    for table in Base.metadata.sorted_tables:
        target = _qualified(table.name)
        assert f"REVOKE ALL ON {target} FROM PUBLIC" in DDL, table.name
        scope = table_flags(table)["scope"]
        grants = [line for line in _lines(table.name) if line.startswith("GRANT")]
        assert grants, table.name
        if scope is Scope.ASSISTANT_ONLY:
            assert all(OPERATOR_ROLE not in line for line in grants), table.name
            assert any(ASSISTANT_ROLE in line for line in grants)
        if scope is Scope.OPERATOR_ONLY:
            assert all(ASSISTANT_ROLE not in line for line in grants), table.name
        if scope is Scope.AUDITOR_WRITE:
            assert grants == [f'GRANT SELECT, INSERT ON {target} TO "{AUDITOR_ROLE}"']
        if scope is Scope.SHARED:
            assert any(line == f'GRANT SELECT ON {target} TO "{AUDITOR_ROLE}"' for line in grants)
        if scope is Scope.GOVERNANCE_ONLY:
            assert all(AUDITOR_ROLE not in line for line in grants)


def test_append_only_tables_lose_update_and_delete_for_every_role() -> None:
    for table in Base.metadata.sorted_tables:
        target = _qualified(table.name)
        revoked = any(line.startswith(f"REVOKE UPDATE, DELETE ON {target} FROM") for line in DDL)
        assert revoked == (table.name in APPEND_ONLY_TABLES), table.name
        if table.name in APPEND_ONLY_TABLES:
            assert not any(
                line.startswith("GRANT") and "UPDATE" in line and f" ON {target} " in line
                for line in DDL
            )


def test_desk_row_tables_force_row_level_security_keyed_on_current_user() -> None:
    desk_row = [t for t in Base.metadata.sorted_tables if table_flags(t)["scope"] is Scope.DESK_ROW]
    assert {t.name for t in desk_row} == {
        "contact",
        "conversation",
        "message",
        "task",
        "memory_record",
        "skill",
        "readback_pending",
        "category_state",
        "model_trace",
    }
    case = role_desk_case()
    assert case.startswith("(CASE current_user WHEN")
    assert "WHEN 'nour_desk_operator' THEN 'operator'" in case
    assert "WHEN 'nour_desk_assistant' THEN 'assistant'" in case
    assert "WHEN 'nour_ingress' THEN 'governance'" in case
    assert "WHEN 'nour_scheduler' THEN 'governance'" in case
    assert "nour_auditor" not in case
    for table in desk_row:
        target = f'"{table.name}"'
        assert f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY" in DDL
        assert f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY" in DDL
        policy = next(
            line for line in DDL if line.startswith(f'CREATE POLICY "{table.name}_desk_isolation"')
        )
        assert f"ON {target} FOR ALL TO" in policy
        assert policy.count(f"(desk = {case})") == 2  # USING and WITH CHECK
        assert AUDITOR_ROLE not in policy
    assert "current_setting(" not in JOINED and "nour.desk" not in JOINED
    with_rls = {line.split('"')[1] for line in DDL if line.endswith(" ENABLE ROW LEVEL SECURITY")}
    # + the desk-partitioned SHARED tables (``inbox_event``, ``pending_owner_message``)
    assert with_rls == {t.name for t in desk_row} | set(DESK_PARTITIONED_TABLES)
    forced = {line.split('"')[1] for line in DDL if line.endswith(" FORCE ROW LEVEL SECURITY")}
    assert forced == with_rls


PARTITION_POLICY_SUFFIXES: tuple[str, ...] = ("read", "update", "delete", "insert", "auditor")
"""The five policies ``_desk_partition_policies`` emits for a desk-partitioned table."""


@pytest.mark.parametrize("table_name", sorted(DESK_PARTITIONED_TABLES))
def test_desk_partitioned_tables_get_forced_rls_with_five_policies(table_name: str) -> None:
    """``__desk_partitioned__`` (``inbox_event``, ``pending_owner_message``): a desk role reads,
    updates and deletes its own partition, inserts into either; governance sees every
    partition; the auditor keeps its SHARED read."""
    assert table_flags(Base.metadata.tables[table_name])["scope"] is Scope.SHARED
    target = f'"{table_name}"'
    assert f"ALTER TABLE {target} ENABLE ROW LEVEL SECURITY" in DDL
    assert f"ALTER TABLE {target} FORCE ROW LEVEL SECURITY" in DDL
    grantees = ", ".join(f'"{r}"' for r in (OPERATOR_ROLE, ASSISTANT_ROLE, *GOVERNANCE_ROLES))
    own = f"(desk = {role_desk_case()} OR current_user IN ('{INGRESS_ROLE}', '{SCHEDULER_ROLE}'))"
    prefix = f"{table_name}_desk_partition_"
    policies = {
        line.split('"')[1]: line
        for line in DDL
        if line.startswith("CREATE POLICY ") and line.split('"')[1].startswith(prefix)
    }
    assert set(policies) == {prefix + suffix for suffix in PARTITION_POLICY_SUFFIXES}
    assert policies[prefix + "read"].endswith(f"ON {target} FOR SELECT TO {grantees} USING {own}")
    assert policies[prefix + "update"].endswith(
        f"ON {target} FOR UPDATE TO {grantees} USING {own} WITH CHECK {own}"
    )
    assert policies[prefix + "delete"].endswith(f"ON {target} FOR DELETE TO {grantees} USING {own}")
    assert policies[prefix + "insert"].endswith(
        f"ON {target} FOR INSERT TO {grantees} WITH CHECK (true)"
    )
    assert policies[prefix + "auditor"].endswith(
        f'ON {target} FOR SELECT TO "{AUDITOR_ROLE}" USING (true)'
    )
    for name in policies:
        assert f'DROP POLICY IF EXISTS "{name}" ON {target}' in DDL
    # no desk-isolation policy on a partitioned table: it is SHARED, not DESK_ROW
    assert not any(line.startswith(f'CREATE POLICY "{table_name}_desk_isolation"') for line in DDL)


def test_only_the_partitioned_tables_carry_partition_policies() -> None:
    """Every ``_desk_partition_`` policy in the DDL belongs to a table of
    ``DESK_PARTITIONED_TABLES`` and every policy of any kind sits on a DESK_ROW or
    desk-partitioned table: no other table is partitioned by accident, none is left open."""
    desk_row = {
        t.name for t in Base.metadata.sorted_tables if table_flags(t)["scope"] is Scope.DESK_ROW
    }
    created = [line.split('"')[1] for line in DDL if line.startswith("CREATE POLICY ")]
    partition_owners = {
        name.rsplit("_desk_partition_", 1)[0] for name in created if "_desk_partition_" in name
    }
    isolation_owners = {
        name.removesuffix("_desk_isolation") for name in created if name.endswith("_desk_isolation")
    }
    assert partition_owners == set(DESK_PARTITIONED_TABLES)
    assert isolation_owners == desk_row
    assert len(created) == len(set(created)) == 5 * len(DESK_PARTITIONED_TABLES) + len(desk_row)


def test_governance_only_tables_hide_passphrase_columns_from_the_desks() -> None:
    owner_grants = [line for line in _lines("owner") if line.startswith("GRANT SELECT (")]
    assert len(owner_grants) == 1
    grant = owner_grants[0]
    assert '"whatsapp_number"' in grant and '"second_channel"' in grant and '"quiet_hours"' in grant
    assert "passphrase_hash" not in grant and "passphrase_fp" not in grant
    assert f'"{OPERATOR_ROLE}", "{ASSISTANT_ROLE}"' in grant
    full = [line for line in _lines("owner") if line.startswith("GRANT SELECT, INSERT")]
    assert full and all(OPERATOR_ROLE not in line for line in full)
    assert hidden_columns(Base.metadata.tables["owner"]) == {"passphrase_hash", "passphrase_fp"}
    # card: governance-only too, every column readable by the desks, none writable
    card_grants = [line for line in _lines("card") if line.startswith("GRANT SELECT (")]
    assert len(card_grants) == 1 and '"monthly_cap"' in card_grants[0]
    assert all(
        OPERATOR_ROLE not in line
        for line in _lines("card")
        if line.startswith("GRANT SELECT, INSERT")
    )


def _grant_lines(table: str, role: str) -> list[str]:
    return [
        line
        for line in _lines(table)
        if (line.startswith("GRANT") or line.startswith("REVOKE INSERT"))
        and f'"{role}"' in line
        and not line.startswith("GRANT SELECT")
    ]


def test_per_role_grants_replace_the_scope_write_grant() -> None:
    """``GRANTS_KEY`` (DESIGN §5 mutability column): for every declared role the scope's
    ``INSERT, UPDATE, DELETE`` is revoked after it was granted and exactly the declared verbs
    come back, column-level where a tuple was given."""
    for table_name, expected in EXPECTED_GRANTS.items():
        target = _qualified(table_name)
        assert grants_of(Base.metadata.tables[table_name]) == expected
        scope_grant = next(
            i
            for i, line in enumerate(DDL)
            if line.startswith("GRANT ") and f" ON {target} TO " in line
        )
        for role, grant in expected.items():
            revoke = f'REVOKE INSERT, UPDATE, DELETE ON {target} FROM "{role}"'
            assert revoke in DDL, (table_name, role)
            assert DDL.index(revoke) > scope_grant
            lines = _grant_lines(table_name, role)
            granted = [line for line in lines if line.startswith("GRANT")]
            expected_lines = []
            if grant.insert is not None:
                cols = (
                    ""
                    if grant.insert == ALL_COLUMNS
                    else " (" + ", ".join(f'"{c}"' for c in grant.insert) + ")"
                )
                expected_lines.append(f'GRANT INSERT{cols} ON {target} TO "{role}"')
            if grant.update is not None:
                cols = (
                    ""
                    if grant.update == ALL_COLUMNS
                    else " (" + ", ".join(f'"{c}"' for c in grant.update) + ")"
                )
                expected_lines.append(f'GRANT UPDATE{cols} ON {target} TO "{role}"')
            if grant.delete:
                expected_lines.append(f'GRANT DELETE ON {target} TO "{role}"')
            assert granted == expected_lines, (table_name, role)
    # the authenticity columns are not in a desk's INSERT column list
    for role in (OPERATOR_ROLE, ASSISTANT_ROLE):
        insert_line = next(
            line for line in _grant_lines("inbox_event", role) if line.startswith("GRANT INSERT (")
        )
        assert all(f'"{column}"' not in insert_line for column in INBOX_AUTH_COLUMNS)
        assert '"sender"' in insert_line and '"body"' in insert_line and '"desk"' in insert_line
    # nobody but the scheduler deletes inbox events; nobody deletes approvals, releases, handoffs
    deleters = {
        line.split('"')[-2] for line in DDL if line.startswith('GRANT DELETE ON "inbox_event"')
    }
    assert deleters == {SCHEDULER_ROLE}
    for table_name in ("approval", "release", "handoff"):
        assert not any(line.startswith(f'GRANT DELETE ON "{table_name}"') for line in DDL)
        for role in (OPERATOR_ROLE, ASSISTANT_ROLE, INGRESS_ROLE, SCHEDULER_ROLE):
            assert f'REVOKE INSERT, UPDATE, DELETE ON "{table_name}" FROM "{role}"' in DDL
    # ingress keeps the scope INSERT on passphrase_attempt (append-only: no UPDATE/DELETE anyway)
    assert f'REVOKE INSERT, UPDATE, DELETE ON "passphrase_attempt" FROM "{INGRESS_ROLE}"' not in DDL
    assert f'REVOKE INSERT, UPDATE, DELETE ON "passphrase_attempt" FROM "{OPERATOR_ROLE}"' in DDL


def test_auditor_schema_is_created_and_the_report_table_is_qualified() -> None:
    assert pg_schema_ddl(Base.metadata) == [f'CREATE SCHEMA IF NOT EXISTS "{AUDITOR_SCHEMA}"']
    assert f'GRANT USAGE ON SCHEMA "{AUDITOR_SCHEMA}" TO "{AUDITOR_ROLE}"' in DDL
    assert f'GRANT SELECT, INSERT ON "{AUDITOR_SCHEMA}"."auditor_report" TO "{AUDITOR_ROLE}"' in DDL
    assert not any(
        '"auditor_report"' in line
        and (OPERATOR_ROLE in line or ASSISTANT_ROLE in line)
        and line.startswith("GRANT")
        for line in DDL
    )
    assert (
        '"auditor.auditor_report"' not in JOINED
        and '"auditor.auditor_report"' not in TRIGGERS_JOINED
    )


def test_trigger_ddl_exists_for_every_flagged_table() -> None:
    for table in Base.metadata.sorted_tables:
        flags = table_flags(table)
        target = _qualified(table.name)
        if flags["append_only"]:
            prefix = f'CREATE TRIGGER "{table.name}_no_update_delete" BEFORE UPDATE OR DELETE ON {target}'
            assert any(line.startswith(prefix) for line in TRIGGERS), table.name
        for column in flags["single_transition"]:
            assert any(
                line.startswith(
                    f'CREATE TRIGGER "{table.name}_{column}_single_transition" BEFORE UPDATE OF "{column}" ON {target}'
                )
                for line in TRIGGERS
            ), f"{table.name}.{column}"
        if flags["single_transition"]:  # kept rows: no DELETE (Postgres has no REPLACE)
            assert (
                f'CREATE TRIGGER "{table.name}_no_delete" BEFORE DELETE ON {target} '
                "FOR EACH ROW EXECUTE FUNCTION nour_refuse_delete()"
            ) in TRIGGERS, table.name
        if flags["immutable"]:
            columns = ", ".join(f'"{c}"' for c in flags["immutable"])
            assert (
                f'CREATE TRIGGER "{table.name}_immutable" BEFORE UPDATE OF {columns} ON {target} '
                f"FOR EACH ROW EXECUTE FUNCTION nour_immutable_{table.name}()"
            ) in TRIGGERS, table.name
            function = next(
                line
                for line in TRIGGERS
                if f"FUNCTION nour_immutable_{table.name}()" in line and "CREATE" in line
            )
            for column in flags["immutable"]:
                assert f'NEW."{column}" IS DISTINCT FROM OLD."{column}"' in function
        for column, order in flags["forward_only"].items():
            function = next(
                line
                for line in TRIGGERS
                if f"nour_forward_only_{table.name}_{column}()" in line
                and "CREATE OR REPLACE FUNCTION" in line
            )
            assert "ARRAY[" + ", ".join(f"'{state}'" for state in order) + "]" in function
    function = next(
        line
        for line in TRIGGERS
        if "nour_refuse_append_only()" in line and "CREATE OR REPLACE FUNCTION" in line
    )
    assert "RAISE EXCEPTION USING MESSAGE" in function and "%" not in function
    assert "DO INSTEAD" not in TRIGGERS_JOINED
    assert all(not re.search(r"(?<!%)%(?![%])", line) or "LIKE" in line for line in TRIGGERS)
    assert set(EXPECTED_IMMUTABLE) == {
        t.name for t in Base.metadata.sorted_tables if table_flags(t)["immutable"]
    }
    assert "' rows are never deleted or replaced'" in TRIGGERS_JOINED


def test_every_role_desk_in_scope_allows_terms() -> None:
    """The RLS partition map is the same table ``scope_allows`` uses: governance roles own the
    (empty) governance partition and the desks their own."""
    from nour.db.engine import ROLE_DESK

    assert ROLE_DESK == {
        OPERATOR_ROLE: "operator",
        ASSISTANT_ROLE: "assistant",
        INGRESS_ROLE: "governance",
        SCHEDULER_ROLE: "governance",
    }


# --------------------------------------------------------------------------- executed (@postgres)

PG_URL = os.environ.get("NOUR_DATABASE_URL", "")
postgres = pytest.mark.postgres
needs_postgres = pytest.mark.skipif(
    not PG_URL.startswith("postgresql"), reason="NOUR_DATABASE_URL is not a postgresql:// URL"
)


@pytest.fixture
def pg(clock: FakeClock, idgen: IdGenerator) -> Iterator[tuple[Engine, dict[str, dict[str, Any]]]]:
    engine = make_engine(PG_URL)
    try:
        Base.metadata.drop_all(engine)
        create_schema(engine, Base.metadata)
        rows = seed_all(engine, clock, idgen)
        yield engine, rows
    finally:
        engine.dispose()


def _as(engine: Engine, role: str, sql: str, params: Any = None) -> Any:
    """Run ``sql`` in one transaction as ``role`` (``SET LOCAL ROLE``: privileges, RLS and
    ``current_user`` all become the role's; a superuser connection keeps the ability to switch)."""
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        return (
            connection.exec_driver_sql(sql, params).all()
            if sql.lstrip().upper().startswith("SELECT")
            else connection.exec_driver_sql(sql, params).rowcount
        )


@postgres
@needs_postgres
def test_runtime_roles_are_not_superusers_owners_or_bypassrls(pg: tuple[Engine, Any]) -> None:
    engine, _ = pg
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin, rolcreaterole FROM pg_roles "
            "WHERE rolname LIKE 'nour\\_%%'"
        ).all()
        owners = connection.exec_driver_sql(
            "SELECT tablename, tableowner FROM pg_tables WHERE schemaname IN ('public', 'auditor')"
        ).all()
    by_name = {row[0]: row for row in rows}
    assert set(ALL_ROLES) <= set(by_name)
    for role in ALL_ROLES:
        _, superuser, bypass, login, createrole = by_name[role]
        assert not superuser and not bypass and not login and not createrole, role
    owned = {name: owner for name, owner in owners}
    # alembic_version may sit next to them in the CI database; owners are what matters
    assert {t.name for t in Base.metadata.sorted_tables} <= set(owned)
    assert all(owner not in ALL_ROLES for owner in owned.values())


@postgres
@needs_postgres
def test_desk_row_and_partitioned_tables_have_forced_rls_and_their_policies(
    pg: tuple[Engine, Any],
) -> None:
    """Executed (fixer round 2): every DESK_ROW table carries ENABLE + FORCE row-level security
    and exactly one ``<table>_desk_isolation`` policy; every desk-partitioned SHARED table
    (``inbox_event``, ``pending_owner_message``) carries forced RLS and exactly the five
    ``<table>_desk_partition_*`` policies; no other table in ``public`` has RLS or a policy;
    every row-filtering predicate is keyed on ``current_user`` — never on the ``nour.desk``
    GUC — and applies to the desk and governance roles, the auditor's read policy to the
    auditor alone, and the insert policy checks nothing (a desk may publish a row for the
    other desk)."""
    engine, _ = pg
    desk_row = sorted(
        t.name for t in Base.metadata.sorted_tables if table_flags(t)["scope"] is Scope.DESK_ROW
    )
    partitioned = sorted(DESK_PARTITIONED_TABLES)
    with engine.connect() as connection:
        rls = connection.exec_driver_sql(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r'"
        ).all()
        policies = connection.exec_driver_sql(
            "SELECT tablename, policyname, cmd, roles, qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public'"
        ).all()
    secured = {name: (enabled, forced) for name, enabled, forced in rls if enabled or forced}
    assert secured == {n: (True, True) for n in (*desk_row, *partitioned)}
    expected = {(n, f"{n}_desk_isolation") for n in desk_row} | {
        (n, f"{n}_desk_partition_{suffix}")
        for n in partitioned
        for suffix in PARTITION_POLICY_SUFFIXES
    }
    assert {(t, p) for t, p, *_ in policies} == expected
    assert len(policies) == len(expected)  # one row per policy: none duplicated
    partition_roles = set(DESK_ROLES) | set(GOVERNANCE_ROLES)
    for table, policy, cmd, roles, qual, with_check in policies:
        if policy.endswith("_desk_partition_auditor"):
            assert cmd == "SELECT" and set(roles) == {AUDITOR_ROLE} and qual == "true", policy
        elif policy.endswith("_desk_partition_insert"):
            assert cmd == "INSERT" and set(roles) == partition_roles, policy
            assert qual is None and with_check == "true", policy
        else:
            assert set(roles) == partition_roles, policy
            assert "CURRENT_USER" in qual.upper(), policy
            if policy.endswith("_desk_isolation"):
                assert cmd == "ALL" and table in desk_row, policy
                assert with_check is not None and "CURRENT_USER" in with_check.upper(), policy
            else:
                assert table in partitioned, policy
                suffix = policy.rsplit("_desk_partition_", 1)[1]
                assert cmd == {"read": "SELECT", "update": "UPDATE", "delete": "DELETE"}[suffix]
                assert INGRESS_ROLE in qual and SCHEDULER_ROLE in qual, (
                    policy
                )  # governance sees all
                if cmd == "UPDATE":
                    assert with_check is not None and "CURRENT_USER" in with_check.upper(), policy
        assert "nour.desk" not in (qual or "") and "nour.desk" not in (with_check or ""), policy


@postgres
@needs_postgres
def test_operator_role_sees_only_its_partition_even_after_setting_the_guc(
    pg: tuple[Engine, Any], idgen: IdGenerator
) -> None:
    engine, rows = pg
    contact = dict(rows["contact"])
    # an Assistant-partition contact planted by the owner (the migrator), outside RLS
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'INSERT INTO "contact" (id, desk, coat_id, name, channels, primary_address, language, register, '
            "consent_status, dnc_flag, source, audit_id, created_at, updated_at) VALUES "
            "(%(id)s, 'assistant', %(coat)s, 'secret', '{}', '+971500000055', 'ar', 'formal', 'opt_in', "
            "false, 'inbound', %(audit)s, now(), now())",
            {"id": idgen.new(), "coat": COAT, "audit": idgen.new()},
        )
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql("SET LOCAL nour.desk = 'assistant'")  # a GUC is not a privilege
        visible = connection.exec_driver_sql('SELECT desk, name FROM "contact"').all()
        assert visible == [("operator", contact["name"])]
        count = connection.exec_driver_sql('SELECT count(*) FROM "contact"').scalar()
        assert count == 1
    with pytest.raises(DBAPIError, match="row-level security"), engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql(
            'INSERT INTO "contact" (id, desk, coat_id, name, channels, primary_address, language, register, '
            "consent_status, dnc_flag, source, audit_id, created_at, updated_at) VALUES "
            "(%(id)s, 'assistant', %(coat)s, 'planted', '{}', '+971500000056', 'ar', 'formal', 'opt_in', "
            "false, 'inbound', %(audit)s, now(), now())",
            {"id": idgen.new(), "coat": COAT, "audit": idgen.new()},
        )
    with pytest.raises(DBAPIError, match="row-level security"), engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql("UPDATE \"contact\" SET desk = 'assistant'")  # moving partition


@postgres
@needs_postgres
@pytest.mark.parametrize(
    ("role", "sql"),
    [
        (OPERATOR_ROLE, 'SELECT * FROM "document"'),
        (OPERATOR_ROLE, 'SELECT * FROM "vault_secret"'),
        (OPERATOR_ROLE, 'SELECT * FROM "beneficiary"'),
        (ASSISTANT_ROLE, 'SELECT * FROM "experiment"'),
        (OPERATOR_ROLE, "UPDATE \"inbox_event\" SET passphrase_attempt = 'ok'"),
        (ASSISTANT_ROLE, 'UPDATE "inbox_event" SET signature_valid = true'),
        (OPERATOR_ROLE, "UPDATE \"inbox_event\" SET sender = '+971500000001'"),
        (OPERATOR_ROLE, "UPDATE \"audit_event\" SET reason = 'rewritten'"),
        (ASSISTANT_ROLE, 'DELETE FROM "audit_event"'),
        (OPERATOR_ROLE, "UPDATE \"owner\" SET name = 'hijacked'"),
        (OPERATOR_ROLE, 'SELECT passphrase_hash FROM "owner"'),
        (ASSISTANT_ROLE, 'SELECT passphrase_fp FROM "owner"'),
        (OPERATOR_ROLE, 'SELECT * FROM "auditor"."auditor_report"'),
        (ASSISTANT_ROLE, 'UPDATE "handoff" SET taken_at = now()'),
        (OPERATOR_ROLE, "UPDATE \"handoff\" SET pushed_by = 'operator'"),
        # fixer round 1: the DESIGN §5 mutability column, executed
        (OPERATOR_ROLE, 'DELETE FROM "release"'),
        (ASSISTANT_ROLE, 'DELETE FROM "release"'),
        (OPERATOR_ROLE, 'DELETE FROM "approval"'),
        (ASSISTANT_ROLE, 'DELETE FROM "approval"'),
        (OPERATOR_ROLE, "UPDATE \"release\" SET tier = 'A'"),
        (OPERATOR_ROLE, "UPDATE \"release\" SET nonce = 'n2'"),
        (OPERATOR_ROLE, 'UPDATE "approval" SET amount = 1'),
        (ASSISTANT_ROLE, "UPDATE \"approval\" SET action_json = '{}'"),
        (OPERATOR_ROLE, 'DELETE FROM "inbox_event"'),
        (ASSISTANT_ROLE, 'DELETE FROM "inbox_event"'),
        (OPERATOR_ROLE, "UPDATE \"inbox_event\" SET body = 'rewritten'"),
        (
            OPERATOR_ROLE,
            'INSERT INTO "inbox_event" (id, desk, source_kind, channel, sender, origin, payload, signature_valid, priority, received_at, created_at, updated_at) '
            "VALUES ('01PLANTED0000000000000001', 'operator', 'handoff', 'owner_whatsapp', '+971500000001', 'text', '{}', true, 0, now(), now(), now())",
        ),
        (
            OPERATOR_ROLE,
            'INSERT INTO "inbox_event" (id, desk, source_kind, channel, sender, origin, payload, passphrase_attempt, priority, received_at, created_at, updated_at) '
            "VALUES ('01PLANTED0000000000000002', 'operator', 'handoff', 'owner_whatsapp', '+971500000001', 'text', '{}', 'ok', 0, now(), now(), now())",
        ),
        (
            OPERATOR_ROLE,
            'INSERT INTO "passphrase_attempt" (id, sender, channel, outcome, at, created_at, updated_at) '
            "VALUES ('01PLANTED0000000000000003', '+971500000001', 'owner_whatsapp', 'ok', now(), now(), now())",
        ),
        (ASSISTANT_ROLE, 'DELETE FROM "handoff"'),
        (
            OPERATOR_ROLE,
            'INSERT INTO "handoff" (id, coat_id, handoff_json, source_event_id, pushed_by, created_at, updated_at) '
            f"VALUES ('01PLANTED0000000000000004', '{COAT}', '{{}}', 'e', 'assistant', now(), now())",
        ),
        (ASSISTANT_ROLE, "UPDATE \"handoff\" SET handoff_json = '{}'"),
        (OPERATOR_ROLE, 'UPDATE "card" SET frozen = false'),
        (ASSISTANT_ROLE, 'UPDATE "card" SET monthly_cap = 99999999'),
        (OPERATOR_ROLE, "UPDATE \"dnc_entry\" SET address = 'x'"),
        (OPERATOR_ROLE, 'DELETE FROM "dnc_entry"'),
        (OPERATOR_ROLE, 'UPDATE "freeze_state" SET released_at = now()'),
        (ASSISTANT_ROLE, 'DELETE FROM "freeze_state"'),
        (ASSISTANT_ROLE, "UPDATE \"coat\" SET banking_ref = 'vault://evil'"),
        (OPERATOR_ROLE, "UPDATE \"coat\" SET approval_rules = '{}'"),
        (OPERATOR_ROLE, 'SELECT * FROM "owner"'),
        (AUDITOR_ROLE, 'SELECT * FROM "contact"'),
        (AUDITOR_ROLE, 'SELECT * FROM "document"'),
        (AUDITOR_ROLE, "UPDATE \"audit_event\" SET reason = 'rewritten'"),
        (
            AUDITOR_ROLE,
            "INSERT INTO \"freeze_state\" (id, scope, reason, actor, engaged_at, created_at, updated_at) VALUES ('x', 'all_outbound', 'r', 'a', now(), now(), now())",
        ),
        (INGRESS_ROLE, 'SELECT * FROM "document"'),
        (SCHEDULER_ROLE, "UPDATE \"audit_event\" SET reason = 'rewritten'"),
    ],
)
def test_denied_statements(pg: tuple[Engine, Any], role: str, sql: str) -> None:
    engine, _ = pg
    with pytest.raises(DBAPIError, match="permission denied"):
        _as(engine, role, sql)


@postgres
@needs_postgres
def test_allowed_statements(pg: tuple[Engine, Any], idgen: IdGenerator, clock: FakeClock) -> None:
    engine, rows = pg
    assert _as(engine, OPERATOR_ROLE, 'SELECT whatsapp_number FROM "owner"') == [
        (rows["owner"]["whatsapp_number"],)
    ]
    # the ORM's default owner SELECT names no passphrase column, so it runs as a desk role
    owner_select = str(
        select(OwnerRow).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "passphrase" not in owner_select
    assert len(_as(engine, OPERATOR_ROLE, owner_select)) == 1
    assert _as(engine, OPERATOR_ROLE, 'UPDATE "inbox_event" SET acked_at = now()') == 1
    assert _as(engine, OPERATOR_ROLE, 'UPDATE "handoff" SET taken_at = now()') == 1
    # a desk publishes an event without the authenticity columns: they take the defaults
    assert (
        _as(
            engine,
            OPERATOR_ROLE,
            'INSERT INTO "inbox_event" (id, desk, source_kind, channel, sender, origin, payload, priority, received_at, created_at, updated_at) '
            "VALUES (%(id)s, 'assistant', 'handoff', 'owner_whatsapp', '+971500000001', 'text', '{}', 0, now(), now(), now())",
            {"id": idgen.new()},
        )
        == 1
    )
    assert _as(
        engine,
        INGRESS_ROLE,
        "SELECT signature_valid, passphrase_attempt FROM \"inbox_event\" WHERE desk = 'assistant'",
    ) == [(False, None)]
    assert _as(engine, SCHEDULER_ROLE, "DELETE FROM \"inbox_event\" WHERE desk = 'assistant'") == 1
    assert (
        _as(
            engine,
            ASSISTANT_ROLE,
            'INSERT INTO "handoff" (id, coat_id, handoff_json, source_event_id, pushed_by, created_at, updated_at) '
            "VALUES (%(id)s, %(coat)s, '{}', 'e', 'assistant', now(), now())",
            {"id": idgen.new(), "coat": COAT},
        )
        == 1
    )
    assert _as(engine, OPERATOR_ROLE, 'UPDATE "release" SET burnt_at = now()') == 1
    assert (
        _as(
            engine,
            OPERATOR_ROLE,
            "UPDATE \"approval\" SET decision = 'approve', decided_at = now()",
        )
        == 1
    )
    assert _as(engine, AUDITOR_ROLE, 'SELECT action FROM "audit_event"') == [
        (rows["audit_event"]["action"],)
    ]
    assert _as(engine, AUDITOR_ROLE, 'SELECT count(*) FROM "approval"') == [(1,)]
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{AUDITOR_ROLE}"')
        connection.exec_driver_sql("SET TRANSACTION READ WRITE")
        connection.exec_driver_sql(
            'INSERT INTO "auditor"."auditor_report" (id, day, findings, summary, vendor, model, created_at, updated_at) '
            "VALUES (%(id)s, current_date, '[]', 'second report', 'fake-b', 'fake-b-1', now(), now())",
            {"id": idgen.new()},
        )
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as connection:
        connection.exec_driver_sql(
            'UPDATE "auditor"."auditor_report" SET summary = \'x\''
        )  # even the owner
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql(
            'INSERT INTO "memory_record" (id, desk, coat_id, store, content, source_refs, confidence, approved_by_owner, audit_id, created_at, updated_at) '
            "VALUES (%(id)s, 'operator', %(coat)s, 'semantic', 'note', '[]', 0.5, false, %(audit)s, now(), now())",
            {"id": idgen.new(), "coat": COAT, "audit": idgen.new()},
        )
    with pytest.raises(DBAPIError, match="check constraint"), engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql(
            'INSERT INTO "memory_record" (id, desk, coat_id, store, content, source_refs, confidence, approved_by_owner, audit_id, created_at, updated_at) '
            "VALUES (%(id)s, 'operator', %(coat)s, 'owner_profile', 'note', '[]', 0.5, false, %(audit)s, now(), now())",
            {"id": idgen.new(), "coat": COAT, "audit": idgen.new()},
        )


@postgres
@needs_postgres
def test_operator_role_never_sees_the_assistants_inbox(
    pg: tuple[Engine, Any], idgen: IdGenerator
) -> None:
    """The desk partition of ``inbox_event`` under forced RLS: the Operator role reads, acks and
    deletes nothing of the Assistant's partition, may insert into it, and the governance roles and
    the auditor see every row."""
    engine, rows = pg
    with engine.begin() as connection:  # the migrator plants an Assistant-desk event
        connection.exec_driver_sql(
            'INSERT INTO "inbox_event" (id, desk, source_kind, channel, sender, origin, body, payload, signature_valid, priority, received_at, created_at, updated_at) '
            "VALUES (%(id)s, 'assistant', 'email', 'owner_mailbox', 'lawyer@example.com', 'text', 'owner secret', '{}', true, 0, now(), now(), now())",
            {"id": idgen.new()},
        )
    assert (
        _as(engine, OPERATOR_ROLE, "SELECT body FROM \"inbox_event\" WHERE desk = 'assistant'")
        == []
    )
    assert _as(engine, OPERATOR_ROLE, 'SELECT desk, body FROM "inbox_event"') == [
        ("operator", rows["inbox_event"]["body"])
    ]
    assert (
        _as(
            engine,
            OPERATOR_ROLE,
            "UPDATE \"inbox_event\" SET acked_at = now() WHERE desk = 'assistant'",
        )
        == 0
    )
    assert _as(engine, ASSISTANT_ROLE, 'SELECT body FROM "inbox_event"') == [("owner secret",)]
    assert _as(engine, INGRESS_ROLE, 'SELECT count(*) FROM "inbox_event"') == [(2,)]
    assert _as(engine, AUDITOR_ROLE, 'SELECT count(*) FROM "inbox_event"') == [(2,)]
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql("SET LOCAL nour.desk = 'assistant'")  # a GUC is not a privilege
        assert connection.exec_driver_sql('SELECT count(*) FROM "inbox_event"').scalar() == 1


@postgres
@needs_postgres
def test_operator_role_never_reads_the_assistants_owner_messages(
    pg: tuple[Engine, Any], idgen: IdGenerator
) -> None:
    """The desk partition of ``pending_owner_message`` under forced RLS (SPEC §5; fixer round
    2): the Operator role reads and transitions nothing of the Assistant's drafts for the owner
    (the seeded ``quote_instruction`` row), the Assistant sends its own exactly once, a desk may
    publish a row for the other desk but never read it back, and the governance roles and the
    auditor see every row."""
    engine, rows = pg
    assistant_text = rows["pending_owner_message"]["text"]
    columns = (
        "(id, desk, kind, text, emergency, parked_until, sent_at, provider_msg_id, audit_id, "
        "created_at, updated_at)"
    )
    with engine.begin() as connection:  # the migrator plants an Operator-desk notification
        connection.exec_driver_sql(
            f'INSERT INTO "pending_owner_message" {columns} VALUES '
            "(%(id)s, 'operator', 'notify', 'Buzz Avenue: two N spends', false, NULL, NULL, NULL, "
            "%(audit)s, now(), now())",
            {"id": idgen.new(), "audit": idgen.new()},
        )
    assert _as(engine, OPERATOR_ROLE, 'SELECT desk, text FROM "pending_owner_message"') == [
        ("operator", "Buzz Avenue: two N spends")
    ]
    assert (
        _as(
            engine,
            OPERATOR_ROLE,
            "SELECT text FROM \"pending_owner_message\" WHERE desk = 'assistant'",
        )
        == []
    )
    assert (
        _as(
            engine,
            OPERATOR_ROLE,
            "UPDATE \"pending_owner_message\" SET sent_at = now(), provider_msg_id = 'wamid.op' "
            "WHERE desk = 'assistant'",
        )
        == 0
    )
    assert _as(engine, ASSISTANT_ROLE, 'SELECT text FROM "pending_owner_message"') == [
        (assistant_text,)
    ]
    assert (
        _as(
            engine,
            ASSISTANT_ROLE,
            "UPDATE \"pending_owner_message\" SET sent_at = now(), provider_msg_id = 'wamid.as'",
        )
        == 1
    )  # its own row, its one send; the Operator's row was not touched
    assert _as(
        engine,
        INGRESS_ROLE,
        'SELECT desk, provider_msg_id FROM "pending_owner_message" ORDER BY desk',
    ) == [("assistant", "wamid.as"), ("operator", None)]
    with pytest.raises(DBAPIError, match="may be set once"), engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{ASSISTANT_ROLE}"')
        connection.exec_driver_sql("UPDATE \"pending_owner_message\" SET provider_msg_id = 'again'")
    assert (  # a desk may publish a row for the other desk ... (the partition's insert policy)
        _as(
            engine,
            OPERATOR_ROLE,
            f'INSERT INTO "pending_owner_message" {columns} VALUES '
            "(%(id)s, 'assistant', 'notify', 'published by the operator', false, NULL, NULL, NULL, "
            "%(audit)s, now(), now())",
            {"id": idgen.new(), "audit": idgen.new()},
        )
        == 1
    )
    assert _as(engine, OPERATOR_ROLE, 'SELECT count(*) FROM "pending_owner_message"') == [(1,)]
    assert _as(engine, ASSISTANT_ROLE, 'SELECT count(*) FROM "pending_owner_message"') == [(2,)]
    assert _as(engine, SCHEDULER_ROLE, 'SELECT count(*) FROM "pending_owner_message"') == [(3,)]
    assert _as(engine, AUDITOR_ROLE, 'SELECT count(*) FROM "pending_owner_message"') == [(3,)]
    with engine.begin() as connection:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{OPERATOR_ROLE}"')
        connection.exec_driver_sql("SET LOCAL nour.desk = 'assistant'")  # a GUC is not a privilege
        assert (
            connection.exec_driver_sql('SELECT count(*) FROM "pending_owner_message"').scalar() == 1
        )
    for role in (OPERATOR_ROLE, ASSISTANT_ROLE, SCHEDULER_ROLE):  # kept rows, for everyone
        with pytest.raises(DBAPIError, match="never deleted"), engine.begin() as connection:
            connection.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
            connection.exec_driver_sql('DELETE FROM "pending_owner_message"')


@postgres
@needs_postgres
def test_postgres_triggers_keep_rows_and_freeze_columns(
    pg: tuple[Engine, Any], idgen: IdGenerator
) -> None:
    """The plpgsql twins of the SQLite triggers, executed as the table owner (no grant stops
    the owner, so these are the last wall): a release is never deleted and its nonce never
    re-bound; an approval's money is frozen before and after the decision."""
    engine, rows = pg
    with pytest.raises(DBAPIError, match="never deleted or replaced"), engine.begin() as c:
        c.exec_driver_sql('DELETE FROM "release"')
    with pytest.raises(DBAPIError, match="release.nonce is immutable"), engine.begin() as c:
        c.exec_driver_sql("UPDATE \"release\" SET nonce = 'n2'")
    with pytest.raises(DBAPIError, match="approval.amount is immutable"), engine.begin() as c:
        c.exec_driver_sql('UPDATE "approval" SET amount = 1')
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE \"approval\" SET decision = 'approve', decided_at = now()")
    with pytest.raises(DBAPIError, match="approval.action_json is immutable"), engine.begin() as c:
        c.exec_driver_sql('UPDATE "approval" SET action_json = \'{"rewritten": true}\'')
    with pytest.raises(DBAPIError, match="may be set once"), engine.begin() as c:
        c.exec_driver_sql("UPDATE \"approval\" SET decision = 'reject'")
    with pytest.raises(DBAPIError, match="inbox_event.sender is immutable"), engine.begin() as c:
        c.exec_driver_sql("UPDATE \"inbox_event\" SET sender = '+1'")
    with pytest.raises(DBAPIError, match="document.sha256 is immutable"), engine.begin() as c:
        c.exec_driver_sql("UPDATE \"document\" SET sha256 = 'x'")
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT nonce, amount FROM "release", "approval"').all() == [
            (rows["release"]["nonce"], rows["approval"]["amount"])
        ]
