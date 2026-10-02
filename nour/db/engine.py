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
when a row with the same primary key — or the same value in any UNIQUE group, which REPLACE
resolves the same way — already exists, so the wall holds even on a connection that did not come
from ``make_engine``.

Every engine is created with ``hide_parameters=True``: SQLAlchemy would otherwise print the bound
parameters of a failing statement into the exception text (and INFO logs), and a parameter can be
a contact name, a transcript or, on a refused ``EncryptedBytes`` write, the very plaintext that was
refused (SPEC §2 hard rule 10: nothing from Tier 2 in logs).

``create_schema`` runs ``metadata.create_all`` and then the trigger DDL on both dialects plus the
schema/role/grant/RLS DDL on Postgres. The trigger generators below are pure functions over
names, so the alembic migration (wave 1) emits exactly the same statements:

* ``append_only_ddl`` — ``__append_only__`` tables: no UPDATE, no DELETE, no REPLACE;
* ``single_transition_ddl`` — ``__single_transition__`` columns change only from NULL, and
  ``keep_rows_ddl`` keeps the rows of such a table (no DELETE, no REPLACE: an "insert + ONE
  transition" row cannot be re-armed by deleting and re-inserting it, DESIGN §4c);
* ``immutable_columns_ddl`` — ``__immutable__`` columns never change after INSERT;
* ``forward_only_ddl`` — ``__forward_only__`` statuses only move forward.

A table that lives in a Postgres schema (``auditor_report`` in schema ``auditor``, DESIGN §3.8)
is addressed as ``"auditor"."auditor_report"`` on Postgres; SQLite has no schemas, so there the
schema is translated away (``schema_translate_map``, installed on the engine by ``create_schema``)
and the triggers target the bare table name.

Grants. ``pg_roles_ddl`` turns every mapper's ``__scope__`` into table grants and, for DESK_ROW and
desk-partitioned tables, row-level security keyed on ``current_user``; ``Table.info[GRANTS_KEY]``
narrows a role's write verbs on one table (column-level INSERT/UPDATE, no DELETE), and
``grant_for_desk`` hands the same map to the ``DeskWallGuard`` so SQLite enforces it per token.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from sqlalchemy import Index, MetaData, Table, UniqueConstraint, create_engine, event
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
"""The Postgres session setting ``SessionFactory.write`` sets at the start of every writer
transaction (``SET LOCAL nour.desk = '<desk>'``). It is **informational only** (it labels the
transaction in logs and ``pg_stat_activity``): a GUC is not a privilege — the same connection
could set it to the other desk — so the RLS policies of :func:`pg_roles_ddl` key on
``current_user``, the connecting role, never on this setting (THREAT_REVIEW 6.1)."""

ROLE_DESK: dict[str, str] = {
    OPERATOR_ROLE: "operator",
    ASSISTANT_ROLE: "assistant",
    INGRESS_ROLE: "governance",
    SCHEDULER_ROLE: "governance",
}
"""§5 §13: the desk partition each connecting role owns on a ``DESK_ROW`` table. The governance
roles map to the (empty) ``governance`` partition, exactly as ``scope_allows`` gives a
``GovernanceToken`` DESK_ROW access to its own partition only; ``nour_auditor`` has no row."""

GRANTS_KEY = "nour_grants"
"""Optional ``Table.info[GRANTS_KEY]``: ``{role: {"insert": ..., "update": ..., "delete": bool}}``.

A role listed here has *exactly* the write verbs given on that table: on Postgres its scope's
table-wide ``INSERT, UPDATE, DELETE`` grant is revoked and replaced (SELECT stays with the
scope), on SQLite the ``DeskWallGuard`` enforces the same map per token (:func:`grant_for_desk`).
A role that is not listed keeps the scope defaults. ``insert`` and ``update`` are ``True`` (every
column), ``False``/``()`` (not granted) or a tuple of column names (a column-level grant: a desk
may ``UPDATE`` only the ack columns of ``inbox_event`` and may ``INSERT`` an event without the
ingress-recorded ``signature_valid`` / ``passphrase_attempt``); ``delete`` is a bool. Declared
through ``__table_args__ = (..., {"info": {GRANTS_KEY: {...}}})`` (THREAT_REVIEW 6.1: executed
grants, not application discipline)."""

ALL_COLUMNS: tuple[str, ...] = ("*",)
"""The ``RoleGrant`` value for "every column"."""

HIDDEN_COLUMN_PREFIX = "passphrase"
"""Columns of a GOVERNANCE_ONLY table whose name starts with this are never readable by a desk
(``owner.passphrase_hash``, ``owner.passphrase_fp``; THREAT_REVIEW 6.3): Postgres gives the desks
a column-level SELECT grant without them and the ``DeskWallGuard`` refuses a desk statement or
loader option that names one (:func:`hidden_columns`)."""

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


def _collision(target: str, pk: Sequence[str], uniques: Sequence[Sequence[str]]) -> str:
    """``EXISTS (SELECT 1 FROM target WHERE <same pk> OR <same unique group> ...)``: the rows
    SQLite's REPLACE conflict resolution would silently delete for the row being inserted."""
    groups = [tuple(pk)] + [tuple(group) for group in uniques if group]
    chains = [" AND ".join(f"{_q(col)} = NEW.{_q(col)}" for col in group) for group in groups]
    where = chains[0] if len(chains) == 1 else " OR ".join(f"({chain})" for chain in chains)
    return f"EXISTS (SELECT 1 FROM {target} WHERE {where})"


def append_only_ddl(
    dialect: str,
    table: str,
    *,
    schema: str | None = None,
    pk: Sequence[str] = ("id",),
    uniques: Sequence[Sequence[str]] = (),
) -> list[str]:
    """§12: ``BEFORE UPDATE`` / ``BEFORE DELETE`` triggers that abort (SQLite ``RAISE(ABORT)``,
    Postgres plpgsql ``RAISE EXCEPTION``; never a ``RULE … DO INSTEAD NOTHING``).

    SQLite also gets a ``BEFORE INSERT`` trigger that aborts when a row with the same primary key
    (``pk``) or the same value in any UNIQUE group (``uniques``) exists: ``INSERT OR REPLACE`` /
    ``REPLACE INTO`` would otherwise replace the row without a ``BEFORE DELETE`` firing — on a
    UNIQUE collision as much as on a primary-key one (an upsert's ``ON CONFLICT DO UPDATE`` is
    caught by the update trigger on both dialects). ``schema`` qualifies the table on Postgres;
    trigger and function names stay bare.
    """
    dialect = _check_dialect(dialect)
    if not pk:
        raise ValueError("append_only_ddl needs the primary key column(s)")
    message = f"{table} is append-only"
    target = _target(table, schema)
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_update')} BEFORE UPDATE ON {target} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_delete')} BEFORE DELETE ON {target} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_replace')} BEFORE INSERT ON {target} "
            f"WHEN {_collision(target, pk, uniques)} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
        ]
    function = "nour_refuse_append_only"
    return [
        # ``USING MESSAGE`` rather than a ``%`` format: psycopg parses ``%`` placeholders in
        # every statement it receives with a (possibly empty) parameter mapping, so a bare ``%``
        # in DDL executed through the driver is an "incomplete placeholder" error.
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || ' is append-only'; END $$",
        f"DROP TRIGGER IF EXISTS {_q(table + '_no_update_delete')} ON {target}",
        f"CREATE TRIGGER {_q(table + '_no_update_delete')} BEFORE UPDATE OR DELETE ON {target} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
    ]


def keep_rows_ddl(
    dialect: str,
    table: str,
    *,
    schema: str | None = None,
    pk: Sequence[str] = ("id",),
    uniques: Sequence[Sequence[str]] = (),
) -> list[str]:
    """DESIGN §4c §5 ("insert + ONE burn / ONE transition"): the rows of a single-transition
    table are kept — ``BEFORE DELETE`` aborts on both dialects, and on SQLite a ``BEFORE INSERT``
    trigger aborts when the new row collides with an existing one on the primary key or any
    UNIQUE group, so ``INSERT OR REPLACE`` cannot re-arm a burnt ``release`` nonce or an answered
    ``approval`` by deleting and re-inserting the row. Postgres has no REPLACE and its
    ``ON CONFLICT DO UPDATE`` runs the UPDATE triggers.
    """
    dialect = _check_dialect(dialect)
    if not pk:
        raise ValueError("keep_rows_ddl needs the primary key column(s)")
    message = f"{table} rows are never deleted or replaced"
    target = _target(table, schema)
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_delete')} BEFORE DELETE ON {target} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
            f"CREATE TRIGGER IF NOT EXISTS {_q(table + '_no_replace')} BEFORE INSERT ON {target} "
            f"WHEN {_collision(target, pk, uniques)} "
            f"BEGIN SELECT RAISE(ABORT, {_sql_str(message)}); END",
        ]
    function = "nour_refuse_delete"
    return [
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME || "
        "' rows are never deleted or replaced'; END $$",
        f"DROP TRIGGER IF EXISTS {_q(table + '_no_delete')} ON {target}",
        f"CREATE TRIGGER {_q(table + '_no_delete')} BEFORE DELETE ON {target} "
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


def immutable_columns_ddl(
    dialect: str, table: str, columns: Sequence[str], *, schema: str | None = None
) -> list[str]:
    """DESIGN §5 ``__immutable__``: one ``BEFORE UPDATE OF <columns>`` trigger per table that
    aborts when any listed column actually changes (``NEW IS NOT OLD`` / ``IS DISTINCT FROM``;
    re-writing the same value is not a change). The message names the column."""
    dialect = _check_dialect(dialect)
    if not columns:
        raise ValueError("immutable_columns_ddl needs at least one column")
    target = _target(table, schema)
    name = f"{table}_immutable"
    column_list = ", ".join(_q(column) for column in columns)
    if dialect == "sqlite":
        body = " ".join(
            f"SELECT RAISE(ABORT, {_sql_str(f'{table}.{column} is immutable')}) "
            f"WHERE NEW.{_q(column)} IS NOT OLD.{_q(column)};"
            for column in columns
        )
        return [
            f"CREATE TRIGGER IF NOT EXISTS {_q(name)} BEFORE UPDATE OF {column_list} ON {target} "
            f"BEGIN {body} END"
        ]
    function = f"nour_immutable_{table}"
    checks = " ".join(
        f"IF NEW.{_q(column)} IS DISTINCT FROM OLD.{_q(column)} THEN RAISE EXCEPTION "
        f"{_sql_str(f'{table}.{column} is immutable')}; END IF;"
        for column in columns
    )
    return [
        f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
        f"BEGIN {checks} RETURN NEW; END $$",
        f"DROP TRIGGER IF EXISTS {_q(name)} ON {target}",
        f"CREATE TRIGGER {_q(name)} BEFORE UPDATE OF {column_list} ON {target} "
        f"FOR EACH ROW EXECUTE FUNCTION {function}()",
    ]


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
            "immutable": (),
            "desk_partitioned": False,
        }
    return info


def unique_groups(table: Table) -> list[tuple[str, ...]]:
    """Every UNIQUE column group of ``table`` (``UniqueConstraint`` and unique ``Index``),
    sorted, without the primary key: what REPLACE collides on besides the key."""
    groups: list[tuple[str, ...]] = []
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            groups.append(tuple(column.name for column in constraint.columns))
    for index in table.indexes:
        if isinstance(index, Index) and index.unique:
            groups.append(tuple(column.name for column in index.columns))
    primary = tuple(column.name for column in table.primary_key.columns)
    # sorted: ``Table.constraints`` / ``indexes`` are sets, and the migration and
    # ``create_schema`` must emit byte-identical trigger DDL from their own metadata objects
    return sorted({group for group in groups if group and group != primary})


def schemas_of(metadata: MetaData) -> list[str]:
    """Every Postgres schema a table of ``metadata`` lives in (DESIGN §3.8: ``auditor``)."""
    return sorted({table.schema for table in metadata.sorted_tables if table.schema})


def trigger_ddl(dialect: str, metadata: MetaData) -> list[str]:
    """Every append-only / keep-rows / single-transition / immutable / forward-only statement
    for ``metadata``.

    On Postgres a table in a schema is targeted as ``"schema"."table"``; on SQLite, which has no
    schemas, the schema is dropped (``create_schema`` translates it away for ``create_all`` too).
    """
    dialect = _check_dialect(dialect)
    out: list[str] = []
    for table in metadata.sorted_tables:
        flags = table_flags(table)
        schema = table.schema if dialect == "postgresql" else None
        pk = [column.name for column in table.primary_key.columns]
        uniques = unique_groups(table)
        if flags["append_only"]:
            out.extend(append_only_ddl(dialect, table.name, schema=schema, pk=pk, uniques=uniques))
        if flags["single_transition"]:
            out.extend(
                single_transition_ddl(
                    dialect, table.name, flags["single_transition"], schema=schema
                )
            )
            out.extend(keep_rows_ddl(dialect, table.name, schema=schema, pk=pk, uniques=uniques))
        immutable = tuple(flags.get("immutable", ()))
        if immutable:
            out.extend(immutable_columns_ddl(dialect, table.name, immutable, schema=schema))
        for column, order in flags["forward_only"].items():
            out.extend(forward_only_ddl(dialect, table.name, column, order, schema=schema))
    return out


def _qualified(table: Table) -> str:
    return _target(table.name, table.schema)


def pg_schema_ddl(metadata: MetaData) -> list[str]:
    """``CREATE SCHEMA IF NOT EXISTS`` for every schema ``metadata`` uses; runs before
    ``create_all`` on Postgres (``auditor_report`` lives in schema ``auditor``)."""
    return [f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}" for schema in schemas_of(metadata)]


# --------------------------------------------------------------------------- grants


class RoleGrant(NamedTuple):
    """One role's write verbs on one table (``Table.info[GRANTS_KEY]``, normalised): ``insert``
    and ``update`` are ``None`` (not granted), :data:`ALL_COLUMNS` or the granted columns in
    declaration order; ``delete`` is a bool."""

    insert: tuple[str, ...] | None
    update: tuple[str, ...] | None
    delete: bool

    def may_insert(self, columns: Iterable[str]) -> bool:
        """May a row naming exactly ``columns`` be inserted?"""
        return _allows(self.insert, columns)

    def may_update(self, columns: Iterable[str]) -> bool:
        """May an UPDATE that sets ``columns`` run?"""
        return _allows(self.update, columns)


def _allows(granted: tuple[str, ...] | None, columns: Iterable[str]) -> bool:
    if granted is None:
        return False
    if granted == ALL_COLUMNS:
        return True
    return set(columns) <= set(granted)


def _grant_columns(table: Table, role: str, verb: str, value: Any) -> tuple[str, ...] | None:
    if value is True:
        return ALL_COLUMNS
    if value is False or value is None:
        return None
    if isinstance(value, str | bytes):
        raise ValueError(
            f"{table.name}: {GRANTS_KEY}[{role!r}][{verb!r}] must be a tuple of columns"
        )
    columns = tuple(value)
    known = {column.name for column in table.columns}
    unknown = sorted(set(columns) - known)
    if unknown:
        raise ValueError(
            f"{table.name}: {GRANTS_KEY}[{role!r}][{verb!r}] names unknown columns {unknown}"
        )
    return columns or None


def grants_of(table: Table) -> dict[str, RoleGrant]:
    """``Table.info[GRANTS_KEY]`` normalised and validated: unknown roles, unknown verbs and
    unknown columns raise ``ValueError`` (a grant that names nothing real would silently grant
    nothing, or everything)."""
    declared: Mapping[str, Mapping[str, Any]] = table.info.get(GRANTS_KEY) or {}
    out: dict[str, RoleGrant] = {}
    for role in sorted(declared):
        if role not in ALL_ROLES:
            raise ValueError(f"{table.name}: {GRANTS_KEY} names unknown role {role!r}")
        verbs = dict(declared[role])
        unknown_verbs = sorted(set(verbs) - {"insert", "update", "delete"})
        if unknown_verbs:
            raise ValueError(
                f"{table.name}: {GRANTS_KEY}[{role!r}] has unknown verbs {unknown_verbs}"
            )
        out[role] = RoleGrant(
            insert=_grant_columns(table, role, "insert", verbs.get("insert", False)),
            update=_grant_columns(table, role, "update", verbs.get("update", False)),
            delete=bool(verbs.get("delete", False)),
        )
    return out


def roles_of_desk(desk: str) -> tuple[str, ...]:
    """The Postgres roles whose partition is ``desk`` (:data:`ROLE_DESK` inverted): a desk is one
    role, ``governance`` is ``nour_ingress`` and ``nour_scheduler``."""
    return tuple(role for role, owned in ROLE_DESK.items() if owned == desk)


def grant_for_desk(table: Table, desk: str) -> RoleGrant | None:
    """The grant the ``DeskWallGuard`` enforces for a token of ``desk`` on ``table``: the union
    of the grants of the desk's roles — declared ones as declared, undeclared ones with the
    scope defaults (one token kind cannot tell ``nour_ingress`` from ``nour_scheduler``, so a
    GovernanceToken may do what either may) — or ``None`` when the table declares nothing for
    any of them."""
    grants = grants_of(table)
    roles = roles_of_desk(desk)
    if not any(role in grants for role in roles):
        return None
    # a role the table does not list keeps its scope defaults (every verb; the append-only
    # and scope checks of the guard still apply before any grant is consulted)
    default = RoleGrant(ALL_COLUMNS, ALL_COLUMNS, True)
    mine = [grants.get(role, default) for role in roles]
    return RoleGrant(
        insert=_union(grant.insert for grant in mine),
        update=_union(grant.update for grant in mine),
        delete=any(grant.delete for grant in mine),
    )


def _union(parts: Iterable[tuple[str, ...] | None]) -> tuple[str, ...] | None:
    merged: list[str] = []
    for part in parts:
        if part is None:
            continue
        if part == ALL_COLUMNS:
            return ALL_COLUMNS
        merged.extend(column for column in part if column not in merged)
    return tuple(merged) or None


def hidden_columns(table: Table) -> frozenset[str]:
    """The columns of ``table`` a desk may never read: the ``passphrase*`` columns of a
    GOVERNANCE_ONLY table (:data:`HIDDEN_COLUMN_PREFIX`); empty for every other scope."""
    if table_flags(table)["scope"] is not Scope.GOVERNANCE_ONLY:
        return frozenset()
    return frozenset(
        column.name for column in table.columns if column.name.startswith(HIDDEN_COLUMN_PREFIX)
    )


def _role_grants_ddl(table: Table, name: str) -> list[str]:
    """``Table.info[GRANTS_KEY]`` → per role: revoke the scope's write verbs, then grant exactly
    the declared ones (column-level where a tuple was given)."""
    out: list[str] = []
    for role, grant in grants_of(table).items():
        out.append(f"REVOKE INSERT, UPDATE, DELETE ON {name} FROM {_q(role)}")
        if grant.insert is not None:
            out.append(f"GRANT INSERT{_column_list(grant.insert)} ON {name} TO {_q(role)}")
        if grant.update is not None:
            out.append(f"GRANT UPDATE{_column_list(grant.update)} ON {name} TO {_q(role)}")
        if grant.delete:
            out.append(f"GRANT DELETE ON {name} TO {_q(role)}")
    return out


def _column_list(columns: tuple[str, ...]) -> str:
    if columns == ALL_COLUMNS:
        return ""
    return " (" + ", ".join(_q(column) for column in columns) + ")"


def pg_roles_ddl(metadata: MetaData) -> list[str]:
    """§5 §13: roles nour_ingress, nour_desk_operator, nour_desk_assistant, nour_scheduler, nour_auditor; GRANTs by
    Scope; REVOKE UPDATE, DELETE on append-only tables; RLS policies on DESK_ROW tables keyed on current_user.

    Hardening (THREAT_REVIEW 6.1, binding for phase 0): every role is created ``NOLOGIN NOSUPERUSER
    NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS`` — LOGIN and the password are granted out of
    band by the operator, one role per process, and none of them owns a table (the migrator does);
    every DESK_ROW table gets ``ENABLE`` **and** ``FORCE ROW LEVEL SECURITY`` with one policy whose
    predicate compares ``desk`` with the connecting role's partition (:func:`role_desk_case`),
    never with a session GUC; a desk-partitioned SHARED table (``inbox_event``) gets forced RLS
    too — a desk role reads, updates and deletes its own partition, inserts into either (that is
    how a desk hands an event to the other one), the governance roles see every partition and the
    auditor keeps its SHARED read; per-role verb and column grants come from
    :data:`GRANTS_KEY`; the desks' SELECT on a GOVERNANCE_ONLY table is a column grant without
    :func:`hidden_columns`.

    ``nour_auditor`` defaults to read-only transactions; its one writer, the ``auditor_report``
    insert (``SessionFactory.append_report``), opens its transaction ``READ WRITE`` explicitly and
    is held to the AUDITOR_WRITE grant alone."""
    out: list[str] = []
    for role in ALL_ROLES:
        comment = (
            f"Nour runtime role ({role}): one per process. LOGIN and the password are granted "
            "out of band by the operator (ALTER ROLE ... LOGIN PASSWORD ...); never a superuser, "
            "never BYPASSRLS, never a table owner (DESIGN 4a; THREAT_REVIEW 6.1)."
        )
        out.append(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_sql_str(role)}) "
            f"THEN CREATE ROLE {_q(role)} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
            f"NOBYPASSRLS; COMMENT ON ROLE {_q(role)} IS {_sql_str(comment)}; END IF; END $$"
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
            if flags.get("desk_partitioned"):
                out.extend(_desk_partition_policies(table, name, grantees))
        elif scope is Scope.DESK_ROW:
            partitioned = DESK_ROLES + GOVERNANCE_ROLES
            grantees = ", ".join(_q(r) for r in partitioned)
            out.append(f"GRANT {change} ON {name} TO {grantees}")
            out.append(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
            out.append(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
            policy = _q(f"{table.name}_desk_isolation")
            predicate = f"(desk = {role_desk_case()})"
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
            hidden = hidden_columns(table)
            readable = [c.name for c in table.columns if c.name not in hidden]
            if readable:
                cols = ", ".join(_q(c) for c in readable)
                desks = ", ".join(_q(r) for r in DESK_ROLES)
                out.append(f"GRANT SELECT ({cols}) ON {name} TO {desks}")
        elif scope is Scope.AUDITOR_WRITE:
            out.append(f"GRANT SELECT, INSERT ON {name} TO {_q(AUDITOR_ROLE)}")
        out.extend(_role_grants_ddl(table, name))
        if append_only:
            out.append(f"REVOKE UPDATE, DELETE ON {name} FROM {roles_csv}, PUBLIC")
    return out


def role_desk_case() -> str:
    """The SQL expression RLS policies compare ``desk`` with: the partition of the *connecting
    role* (``current_user``), which only a new connection with another role's credentials can
    change — never a session setting the process could flip (THREAT_REVIEW 6.1). A role outside
    :data:`ROLE_DESK` (the table owner under ``FORCE ROW LEVEL SECURITY``, the auditor) gets
    ``NULL`` and therefore no row."""
    whens = " ".join(
        f"WHEN {_sql_str(role)} THEN {_sql_str(desk)}" for role, desk in ROLE_DESK.items()
    )
    return f"(CASE current_user {whens} END)"


def _desk_partition_policies(table: Table, name: str, grantees: str) -> list[str]:
    """Forced RLS for a desk-partitioned SHARED table (``__desk_partitioned__``): the desks and
    the governance roles read, update and delete rows of their own partition — governance
    (``nour_ingress``, ``nour_scheduler``) every partition — and insert into any; the auditor
    keeps the plain SHARED read."""
    governance = ", ".join(_sql_str(role) for role in GOVERNANCE_ROLES)
    own = f"(desk = {role_desk_case()} OR current_user IN ({governance}))"
    out = [
        f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY",
    ]
    policies = [
        ("read", f"FOR SELECT TO {grantees} USING {own}"),
        ("update", f"FOR UPDATE TO {grantees} USING {own} WITH CHECK {own}"),
        ("delete", f"FOR DELETE TO {grantees} USING {own}"),
        ("insert", f"FOR INSERT TO {grantees} WITH CHECK (true)"),
        ("auditor", f"FOR SELECT TO {_q(AUDITOR_ROLE)} USING (true)"),
    ]
    for suffix, clause in policies:
        policy = _q(f"{table.name}_desk_partition_{suffix}")
        out.append(f"DROP POLICY IF EXISTS {policy} ON {name}")
        out.append(f"CREATE POLICY {policy} ON {name} {clause}")
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
        # no_parameters: the DDL goes to the driver as ``cursor.execute(sql)`` with no parameter
        # collection at all, so psycopg never scans it for ``%`` placeholders.
        connection = connection.execution_options(no_parameters=True)
        for statement in statements:
            connection.exec_driver_sql(statement)
