"""Engines, schema creation and the dialect DDL (DESIGN §3.7 ``nour/db/engine.py``; SPEC §5 §12
§13 §15).

``make_engine`` is the only place dialect differences live (DESIGN §5.3): SQLite gets WAL,
``foreign_keys=ON``, ``recursive_triggers=ON``, a busy timeout and ``BEGIN IMMEDIATE`` for writer
connections (those whose execution options carry ``nour_writer=True``, set by
``SessionFactory.write``); a read-only engine opens the file through a ``mode=ro`` URI so the
auditor process cannot write even by accident. Postgres read-only engines run every transaction
``READ ONLY``.

``recursive_triggers`` matters for the append-only wall: SQLite's ``INSERT OR REPLACE`` (and
``REPLACE INTO``) deletes the conflicting row *without* firing ``BEFORE DELETE`` triggers unless
recursive triggers are on, so an audit row could be rewritten in place through an upsert. Every
connection sets the pragma, and ``append_only_ddl`` adds a ``BEFORE INSERT`` trigger that aborts
when a row with the same primary key already exists, so the wall holds even on a connection that
did not come from ``make_engine``.

Every engine is created with ``hide_parameters=True``: SQLAlchemy would otherwise print the bound
parameters of a failing statement into the exception text (and INFO logs), and a parameter can be
a contact name, a transcript or, on a refused ``EncryptedBytes`` write, the very plaintext that was
refused (SPEC §2 hard rule 10: nothing from Tier 2 in logs).

``create_schema`` runs ``metadata.create_all`` and then the trigger DDL on both dialects plus the
schema/role/grant/RLS DDL on Postgres. The three trigger generators below are pure functions over
names, so the alembic migration (wave 1) emits exactly the same statements. A table that lives in
a Postgres schema (``auditor_report`` in schema ``auditor``, DESIGN §3.8) is addressed as
``"auditor"."auditor_report"`` on Postgres; SQLite has no schemas, so there the schema is
translated away (``schema_translate_map``, installed on the engine by ``create_schema``) and the
triggers target the bare table name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import MetaData, Table, create_engine, event
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.engine.url import URL

from nour.db.base import TABLE_INFO_KEY, Scope

INGRESS_ROLE = "nour_ingress"
OPERATOR_ROLE = "nour_desk_operator"
ASSISTANT_ROLE = "nour_desk_assistant"
SCHEDULER_ROLE = "nour_scheduler"
AUDITOR_ROLE = "nour_auditor"
ALL_ROLES: tuple[str, ...] = (
    INGRESS_ROLE,
    OPERATOR_ROLE,
    ASSISTANT_ROLE,
    SCHEDULER_ROLE,
    AUDITOR_ROLE,
)
DESK_ROLES: tuple[str, ...] = (OPERATOR_ROLE, ASSISTANT_ROLE)
GOVERNANCE_ROLES: tuple[str, ...] = (INGRESS_ROLE, SCHEDULER_ROLE)

DESK_SETTING = "nour.desk"
"""The Postgres session setting RLS policies key on: ``SET LOCAL nour.desk = '<desk>'`` at the
start of every writer transaction (``SessionFactory.write``)."""

WRITER_OPTION = "nour_writer"
"""Connection execution option a writer session sets so SQLite begins with ``BEGIN IMMEDIATE``."""

READ_ONLY_OPTION = "nour_read_only"
"""Engine execution option ``make_engine(read_only=True)`` records."""

_SQLITE_WRITER_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
)
_SQLITE_EVERY_CONNECTION_PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA recursive_triggers=ON",
)
_SQLITE_BUSY_TIMEOUT_MS = 5000


# --------------------------------------------------------------------------- engines


def _sqlite_read_only_url(url: URL) -> URL:
    database = url.database or ""
    if not database or database == ":memory:":
        raise ValueError("a read-only SQLite engine needs a file path (DESIGN §5.3)")
    if database.startswith("file:"):
        database = database[len("file:") :]
    database = database.split("?", 1)[0]
    return URL.create(url.drivername, database=f"file:{database}?mode=ro", query={"uri": "true"})


def make_engine(url: str, *, read_only: bool = False) -> Engine:
    """SQLite: WAL, foreign_keys=ON, recursive_triggers=ON, BEGIN IMMEDIATE for writers,
    ``mode=ro`` URI for ``read_only``; Postgres: psycopg, ``READ ONLY`` transactions for the
    auditor."""
    parsed = make_url(url)
    dialect = parsed.get_backend_name()
    if dialect == "sqlite":
        if read_only:
            parsed = _sqlite_read_only_url(parsed)
        engine = create_engine(
            parsed, connect_args={"check_same_thread": False}, hide_parameters=True
        )
        _install_sqlite_events(engine, read_only=read_only)
    elif dialect == "postgresql":
        options: dict[str, Any] = {}
        if read_only:
            options["postgresql_readonly"] = True
        engine = create_engine(
            parsed, pool_pre_ping=True, execution_options=options, hide_parameters=True
        )
    else:
        engine = create_engine(parsed, hide_parameters=True)
    engine.update_execution_options(**{READ_ONLY_OPTION: read_only})
    return engine


def is_read_only(engine: Engine) -> bool:
    return bool(engine.get_execution_options().get(READ_ONLY_OPTION, False))


def _install_sqlite_events(engine: Engine, *, read_only: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        # pysqlite would otherwise emit BEGIN lazily (and never for SELECT); we drive it from the
        # "begin" hook so writers can take the reserved lock up front.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            for pragma in _SQLITE_EVERY_CONNECTION_PRAGMAS:
                cursor.execute(pragma)
            if read_only:
                cursor.execute("PRAGMA query_only=ON")
            else:
                for pragma in _SQLITE_WRITER_PRAGMAS:
                    cursor.execute(pragma)
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Connection) -> None:
        writer = bool(connection.get_execution_options().get(WRITER_OPTION, False))
        connection.exec_driver_sql("BEGIN IMMEDIATE" if writer and not read_only else "BEGIN")


# --------------------------------------------------------------------------- DDL generators


def _q(name: str) -> str:
    """Quote an identifier for either dialect."""
    return '"' + name.replace('"', '""') + '"'


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _check_dialect(dialect: str) -> str:
    if dialect not in ("sqlite", "postgresql"):
        raise ValueError(f"unsupported dialect {dialect!r}; sqlite or postgresql")
    return dialect


def _target(table: str, schema: str | None) -> str:
    """The quoted ``ON`` target: ``"table"`` or ``"schema"."table"`` (never one identifier)."""
    return _q(table) if schema is None else f"{_q(schema)}.{_q(table)}"


def append_only_ddl(
    dialect: str, table: str, *, schema: str | None = None, pk: Sequence[str] = ("id",)
) -> list[str]:
    """§12: ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers that abort (SQLite ``RAISE(ABORT)``,
    Postgres plpgsql ``RAISE EXCEPTION``; never a ``RULE … DO INSTEAD NOTHING``).

    SQLite also gets a ``BEFORE INSERT`` trigger that aborts when a row with the same primary key
    (``pk``) exists: ``INSERT OR REPLACE`` / ``REPLACE INTO`` would otherwise replace the row
    without a ``BEFORE DELETE`` firing (an upsert's ``ON CONFLICT DO UPDATE`` is caught by the
    update trigger on both dialects). ``schema`` qualifies the table on Postgres; trigger and
    function names stay bare.
    """
    dialect = _check_dialect(dialect)
    if not pk:
        raise ValueError("append_only_ddl needs the primary key column(s)")
    message = f"{table} is append-only"
    target = _target(table, schema)
    if dialect == "sqlite":
        same_key = " AND ".join(f"{_q(col)} = NEW.{_q(col)}" for col in pk)
        return [
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_update')} BEFORE UPDATE ON {target} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_delete')} BEFORE DELETE ON {target} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_replace')} BEFORE INSERT ON {target} "
            f"WHEN EXISTS (SELECT 1 FROM {target} WHERE {same_key}) "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
        ]
    function = "nour_refuse_append_only"
    return [
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END $$",
        f"DROP TRIGGER IF EXISTS {_q(table + '_no_update_delete')} ON {target}",
        f"CREATE TRIGGER {_q(table + '_no_update_delete')} BEFORE UPDATE OR DELETE ON {target} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
    ]


def single_transition_ddl(
    dialect: str, table: str, columns: Sequence[str], *, schema: str | None = None
) -> list[str]:
    """DESIGN §3.7: each column may change only while it is NULL (set once, never unset)."""
    dialect = _check_dialect(dialect)
    target = _target(table, schema)
    out: list[str] = []
    for column in columns:
        message = f"{table}.{column} may be set once"
        name = f"{table}_{column}_single_transition"
        if dialect == "sqlite":
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS {_q(name)} BEFORE UPDATE OF {_q(column)} ON {target} "
                f"WHEN OLD.{_q(column)} IS NOT NULL AND NEW.{_q(column)} IS NOT OLD.{_q(column)} "
                f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END"
            )
        else:
            function = f"nour_single_transition_{table}_{column}"
            out.extend(
                [
                    f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                    f"BEGIN IF OLD.{_q(column)} IS NOT NULL AND NEW.{_q(column)} IS DISTINCT FROM "
                    f"OLD.{_q(column)} THEN RAISE EXCEPTION {_sql_str(message)}; END IF; RETURN NEW; END $$",
                    f"DROP TRIGGER IF EXISTS {_q(name)} ON {target}",
                    f"CREATE TRIGGER {_q(name)} BEFORE UPDATE OF {_q(column)} ON {target} "
                    f"FOR EACH ROW EXECUTE FUNCTION {function}()",
                ]
            )
    return out


def forward_only_ddl(
    dialect: str, table: str, column: str, order: Sequence[str], *, schema: str | None = None
) -> list[str]:
    """DESIGN §3.7: ``column`` may move only forward along ``order``; unknown states are refused."""
    dialect = _check_dialect(dialect)
    if len(order) < 2:
        raise ValueError("forward_only_ddl needs at least two ordered states")
    message = f"{table}.{column} may only move forward"
    name = f"{table}_{column}_forward_only"
    target = _target(table, schema)
    if dialect == "sqlite":
        cases = " ".join(f"WHEN {_sql_str(state)} THEN {rank}" for rank, state in enumerate(order))
        rank_new = f"(CASE NEW.{_q(column)} {cases} ELSE -1 END)"
        rank_old = f"(CASE OLD.{_q(column)} {cases} ELSE -1 END)"
        return [
            f"CREATE TRIGGER IF NOT EXISTS {_q(name)} BEFORE UPDATE OF {_q(column)} ON {target} "
            f"WHEN NEW.{_q(column)} IS NOT OLD.{_q(column)} AND {rank_new} <= {rank_old} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END"
        ]
    function = f"nour_forward_only_{table}_{column}"
    states = "ARRAY[" + ", ".join(_sql_str(state) for state in order) + "]"
    return [
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        f"BEGIN IF NEW.{_q(column)} IS DISTINCT FROM OLD.{_q(column)} AND "
        f"COALESCE(array_position({states}, NEW.{_q(column)}::text), -1) <= "
        f"COALESCE(array_position({states}, OLD.{_q(column)}::text), -1) "
        f"THEN RAISE EXCEPTION {_sql_str(message)}; END IF; RETURN NEW; END $$",
        f"DROP TRIGGER IF EXISTS {_q(name)} ON {target}",
        f"CREATE TRIGGER {_q(name)} BEFORE UPDATE OF {_q(column)} ON {target} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
    ]


def table_flags(table: Table) -> dict[str, Any]:
    """The ``__scope__``/``__append_only__``/… flags ``Base`` recorded for ``table``."""
    info = table.info.get(TABLE_INFO_KEY)
    if info is None:
        return {
            "scope": Scope.SHARED,
            "append_only": False,
            "single_transition": (),
            "forward_only": {},
        }
    return info


def schemas_of(metadata: MetaData) -> list[str]:
    """Every Postgres schema a table of ``metadata`` lives in (DESIGN §3.8: ``auditor``)."""
    return sorted({table.schema for table in metadata.sorted_tables if table.schema})


def trigger_ddl(dialect: str, metadata: MetaData) -> list[str]:
    """Every append-only / single-transition / forward-only statement for ``metadata``.

    On Postgres a table in a schema is targeted as ``"schema"."table"``; on SQLite, which has no
    schemas, the schema is dropped (``create_schema`` translates it away for ``create_all`` too).
    """
    dialect = _check_dialect(dialect)
    out: list[str] = []
    for table in metadata.sorted_tables:
        flags = table_flags(table)
        schema = table.schema if dialect == "postgresql" else None
        if flags["append_only"]:
            pk = [column.name for column in table.primary_key.columns]
            out.extend(append_only_ddl(dialect, table.name, schema=schema, pk=pk))
        if flags["single_transition"]:
            out.extend(
                single_transition_ddl(
                    dialect, table.name, flags["single_transition"], schema=schema
                )
            )
        for column, order in flags["forward_only"].items():
            out.extend(forward_only_ddl(dialect, table.name, column, order, schema=schema))
    return out


def _qualified(table: Table) -> str:
    return _target(table.name, table.schema)


def pg_schema_ddl(metadata: MetaData) -> list[str]:
    """``CREATE SCHEMA IF NOT EXISTS`` for every schema ``metadata`` uses; runs before
    ``create_all`` on Postgres (``auditor_report`` lives in schema ``auditor``)."""
    return [f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}" for schema in schemas_of(metadata)]


def pg_roles_ddl(metadata: MetaData) -> list[str]:
    """§5 §13: roles nour_ingress, nour_desk_operator, nour_desk_assistant, nour_scheduler, nour_auditor; GRANTs by
    Scope; REVOKE UPDATE, DELETE on append-only tables; RLS policies on DESK_ROW tables keyed on current_setting('nour.desk').

    ``nour_auditor`` defaults to read-only transactions; its one writer, the ``auditor_report``
    insert (``SessionFactory.append_report``), opens its transaction ``READ WRITE`` explicitly and
    is held to the AUDITOR_WRITE grant alone."""
    out: list[str] = []
    for role in ALL_ROLES:
        out.append(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_sql_str(role)}) "
            f"THEN CREATE ROLE {_q(role)} LOGIN; END IF; END $$"
        )
    out.append(f"ALTER ROLE {_q(AUDITOR_ROLE)} SET default_transaction_read_only = on")
    schemas = schemas_of(metadata)
    out.extend(pg_schema_ddl(metadata))
    roles_csv = ", ".join(_q(role) for role in ALL_ROLES)
    out.append(f"GRANT USAGE ON SCHEMA public TO {roles_csv}")
    for schema in schemas:
        out.append(f"GRANT USAGE ON SCHEMA {_q(schema)} TO {_q(AUDITOR_ROLE)}")

    for table in metadata.sorted_tables:
        flags = table_flags(table)
        scope: Scope = flags["scope"]
        name = _qualified(table)
        append_only = flags["append_only"]
        change = "SELECT, INSERT" if append_only else "SELECT, INSERT, UPDATE, DELETE"
        out.append(f"REVOKE ALL ON {name} FROM PUBLIC")
        if scope is Scope.SHARED:
            grantees = ", ".join(_q(r) for r in DESK_ROLES + GOVERNANCE_ROLES)
            out.append(f"GRANT {change} ON {name} TO {grantees}")
            out.append(f"GRANT SELECT ON {name} TO {_q(AUDITOR_ROLE)}")
        elif scope is Scope.DESK_ROW:
            partitioned = DESK_ROLES + GOVERNANCE_ROLES
            grantees = ", ".join(_q(r) for r in partitioned)
            out.append(f"GRANT {change} ON {name} TO {grantees}")
            out.append(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
            policy = _q(f"{table.name}_desk_isolation")
            predicate = f"(desk = current_setting({_sql_str(DESK_SETTING)}, true))"
            out.append(f"DROP POLICY IF EXISTS {policy} ON {name}")
            out.append(
                f"CREATE POLICY {policy} ON {name} FOR ALL TO {grantees} "
                f"USING {predicate} WITH CHECK {predicate}"
            )
        elif scope is Scope.ASSISTANT_ONLY:
            out.append(f"GRANT {change} ON {name} TO {_q(ASSISTANT_ROLE)}")
        elif scope is Scope.OPERATOR_ONLY:
            out.append(f"GRANT {change} ON {name} TO {_q(OPERATOR_ROLE)}")
        elif scope is Scope.GOVERNANCE_ONLY:
            grantees = ", ".join(_q(r) for r in GOVERNANCE_ROLES)
            out.append(f"GRANT {change} ON {name} TO {grantees}")
            readable = [c.name for c in table.columns if not c.name.startswith("passphrase")]
            if readable:
                cols = ", ".join(_q(c) for c in readable)
                desks = ", ".join(_q(r) for r in DESK_ROLES)
                out.append(f"GRANT SELECT ({cols}) ON {name} TO {desks}")
        elif scope is Scope.AUDITOR_WRITE:
            out.append(f"GRANT SELECT, INSERT ON {name} TO {_q(AUDITOR_ROLE)}")
        if append_only:
            out.append(f"REVOKE UPDATE, DELETE ON {name} FROM {roles_csv}, PUBLIC")
    return out


# --------------------------------------------------------------------------- schema creation


def install_sqlite_schema_translation(engine: Engine, metadata: MetaData) -> dict[str, None]:
    """Map every schema ``metadata`` uses to ``None`` on a SQLite engine (SQLite has no schemas;
    ``auditor.auditor_report`` becomes ``auditor_report`` in the one database file). The map is
    installed as an engine-wide execution option, so every later connection, ORM statement and
    DDL through ``engine`` sees the same tables. Returns the map (empty when nothing to map)."""
    if engine.dialect.name != "sqlite":
        return {}
    current = dict(engine.get_execution_options().get("schema_translate_map") or {})
    wanted = {schema: None for schema in schemas_of(metadata)}
    if any(schema not in current for schema in wanted):
        current.update(wanted)
        engine.update_execution_options(schema_translate_map=current)
    return {schema: None for schema in current}


def create_schema(engine: Engine, metadata: MetaData) -> None:
    """create_all + append_only_ddl + single_transition_ddl + forward_only_ddl on BOTH dialects (triggers that
    RAISE) + pg_roles_ddl on Postgres. Tests call this; production runs the same DDL from the alembic migration."""
    if is_read_only(engine):
        raise ValueError("create_schema needs a writer engine")
    dialect = engine.dialect.name
    if dialect == "postgresql":
        schema_statements = pg_schema_ddl(metadata)
        if schema_statements:
            with engine.begin() as connection:
                for statement in schema_statements:
                    connection.exec_driver_sql(statement)
    else:
        install_sqlite_schema_translation(engine, metadata)
    metadata.create_all(engine)
    statements = trigger_ddl(dialect, metadata)
    if dialect == "postgresql":
        statements.extend(pg_roles_ddl(metadata))
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
