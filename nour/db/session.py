"""Token-bound sessions and the desk wall (DESIGN §3.7 ``nour/db/session.py``; SPEC §5 §12 §13).

One ``SessionFactory`` per process, bound to the process token. Every session it opens is a
:class:`GuardedSession` carrying a ``DeskWallGuard`` that enforces, on SQLite, what Postgres
grants and row-level security enforce in production (DESIGN §4a):

* the token may touch only the scopes ``scope_allows`` grants it, and only a token ``mint``
  produced is a token at all;
* ``DESK_ROW`` mappers are partitioned on ``desk == token.desk`` even for a query without a WHERE
  (``with_loader_criteria``), and a bulk ``INSERT`` must name the token's desk in every row while a
  bulk ``UPDATE`` may never set ``desk`` — a row can neither be planted in nor moved to the other
  partition;
* flushed ``DESK_ROW`` rows must carry the token's desk; append-only mappers refuse UPDATE/DELETE
  at the ORM level (the DB trigger is the real wall); single-transition columns change only from
  NULL and forward-only statuses only move forward;
* only ORM statements over mapped classes pass, and only the parts of them SQLAlchemy can
  partition. ``with_loader_criteria`` adds ``desk = :desk`` for every ORM *entity* of every SELECT
  (and DML) in the statement: the mapped classes and attributes in its columns, FROM list, JOIN
  targets and WHERE clause, including nested ORM sub-selects, relationship and eager loads. It
  never reaches a *bare* table reference — ``Model.__table__``, ``Model.__table__.c.x``, a
  lightweight ``table()``/``column()``, ``Table.alias()`` or a Core ``join()`` — because a Core
  construct carries no entity. ``_StatementScan`` therefore walks every statement and refuses:

  - raw SQL anywhere: ``text()``, ``literal_column()`` (``*`` excepted: ``count(*)``), an
    identifier built with ``quoted_name(..., quote=False)``, a ``.op()`` custom operator that is
    not plain punctuation, ``prefix_with``/``suffix_with``/``with_hint`` text (SQLite's ``OR
    IGNORE``/``OR ABORT`` insert prefixes excepted) and a non-alphabetic ``extract()`` field;
  - every bare table reference the SELECT (or DML) it sits in would not partition: a bare table
    is admitted only when the same table object is also an ORM entity of that very SELECT (then
    SQLAlchemy dedupes the two into one partitioned FROM), or when the SELECT explicitly
    correlates it (``correlate``/``correlate_except``, as ``any()``/``has()`` do) to an enclosing
    SELECT that partitions it. Columns in ORDER BY / GROUP BY / HAVING, the leaves of a manual
    ``join()`` and the WHERE clause of a DML statement never make a table an entity, because
    SQLAlchemy does not partition from those positions either;
  - a Core statement (no ORM entity at all) over any table, ``session.connection()`` and the
    legacy ``bulk_save_objects`` / ``bulk_insert_mappings`` / ``bulk_update_mappings`` APIs,
    which fire neither hook. Use the mapped class, ``aliased()``, ``session.add()`` or
    ``insert()``/``update()``/``delete()`` over the mapped class.

  What the hook cannot see: a ``column_property`` sub-select written over bare table columns is
  expanded by the mapper at compile time (define such properties with mapped attributes or not
  at all on scoped tables), and a relationship configured ``lazy="joined"`` is joined without a
  statement of its own — its target is still partitioned (the criteria are registered for every
  ``DESK_ROW`` mapper of the registry, not only the ones in the statement; a ``DESK_ROW`` mapper
  without a mapped ``desk`` column, which only a class mapped around ``Base`` can be, fails the
  statement closed with ``DeskWallViolation``), but its scope is checked only when it is named
  in the statement or in a loader option. The engine stays
  reachable through ``session.bind``/``get_bind()`` (the ORM itself needs it) and
  ``SessionFactory.engine``: the wall is this hook and the Postgres roles, not attribute privacy
  (DESIGN §10).

Units of work. ``write()`` is the writer: it serialises the writers of this factory with a lock
(and refuses a nested writer from a thread that already holds one — any factory, since the
harness runs every process in one thread — which on SQLite would otherwise surface as a raw
"database is locked"), opens a connection flagged ``nour_writer`` so SQLite begins with ``BEGIN
IMMEDIATE`` (``make_engine``), sets ``nour.desk`` for the transaction on Postgres, commits on a
clean exit and rolls back on an exception. ``session()`` is a *reader* (plain ``BEGIN``): any
flush, commit of pending rows or DML through it raises ``DeskWallViolation``, so nothing is ever
written outside the writer lock.

The auditor. An ``AuditorToken`` factory takes a **read-only** engine (``make_engine(read_only=
True)``: ``mode=ro`` on SQLite, ``READ ONLY`` transactions on Postgres) for ``session()`` and
refuses ``write()`` outright. Its one writer is :meth:`SessionFactory.append_report`, which
opens a unit of work on the separate ``report_engine`` given at construction and admits exactly
one shape of change: ``INSERT`` into an ``AUDITOR_WRITE`` mapper (``auditor_report``, itself
append-only). On Postgres that transaction is opened ``READ WRITE`` (the role's default is
read-only) and is held to the AUDITOR_WRITE grant alone.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Table, event, inspect, text
from sqlalchemy.dialects.postgresql.dml import OnConflictDoUpdate as PgOnConflictDoUpdate
from sqlalchemy.dialects.sqlite.dml import OnConflictDoUpdate as SqliteOnConflictDoUpdate
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import (
    FromStatement,
    Load,
    Mapper,
    ORMExecuteState,
    QueryableAttribute,
    RelationshipProperty,
    Session,
    SessionTransaction,
    with_loader_criteria,
)
from sqlalchemy.sql import ClauseElement
from sqlalchemy.sql.annotation import Annotated
from sqlalchemy.sql.dml import UpdateBase, ValuesBase
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    ColumnClause,
    Extract,
    TextClause,
    UnaryExpression,
    quoted_name,
)
from sqlalchemy.sql.operators import custom_op
from sqlalchemy.sql.selectable import (
    CTE,
    Alias,
    CompoundSelect,
    FromClause,
    Join,
    Select,
    SelectBase,
    Subquery,
    TableClause,
    TextualSelect,
)

from nour.core.clock import Clock
from nour.core.errors import AppendOnlyViolation, DeskWallViolation, SingleTransitionViolation
from nour.core.tokens import AuditorToken, DeskToken, require_minted
from nour.core.types import Desk
from nour.db.base import MAPPER_INFO_KEY, Scope, scope_allows, scope_of
from nour.db.engine import DESK_SETTING, WRITER_OPTION, is_read_only

READER_INFO_KEY = "nour_reader"
"""``session.info`` flag: the session came from ``SessionFactory.session()`` and may not write."""

REPORT_INFO_KEY = "nour_report_writer"
"""``session.info`` flag: the session came from ``SessionFactory.append_report()``."""

_BULK_API_MESSAGE = (
    "the legacy bulk API bypasses the desk wall (it fires neither do_orm_execute nor "
    "before_flush); use session.add() or insert()/update() statements"
)


_RAW_SQL_MESSAGE = "raw SQL (text()) bypasses the desk wall; use mapped classes through the session"
_VERBATIM_MESSAGE = "{what} is compiled verbatim and bypasses the desk wall; use mapped attributes"
_CORE_MESSAGE = (
    "a Core statement over a table bypasses the desk wall (it has no ORM entity to partition); "
    "use the mapped class"
)
_BARE_MESSAGE = (
    "{what} is referenced outside the ORM entities of its SELECT, so the desk wall cannot "
    "partition it; reference the mapped class (or aliased()) in that SELECT instead"
)

_SAFE_CUSTOM_OP = re.compile(r"[-+*/<>=!~@#?&|%^]{1,4}")
"""``.op()`` operators that cannot name a table or close an expression: punctuation such as
``||``, ``->>``, ``@>``, ``?|`` (comment starters are refused separately)."""
_COMMENT_STARTERS = ("--", "/*", "*/")
_SAFE_LITERAL_COLUMN = re.compile(r"\*|[0-9]+")
"""``literal_column`` texts SQLAlchemy itself emits (``count(*)``, ``EXISTS (SELECT 1 …)``):
nothing that could name a table or close an expression."""
_SAFE_EXTRACT_FIELD = re.compile(r"[A-Za-z_]+")
_DML_PREFIXES = frozenset({"OR IGNORE", "OR ABORT"})
"""SQLite conflict clauses an ``insert().prefix_with()`` may carry: keywords only, never ``OR
REPLACE`` (which deletes the conflicting row of whichever partition owns it)."""


_writing = threading.local()
"""Per-thread flag: a writer unit of work is open. Process-wide rather than per factory because
the harness runs every process in one thread over one SQLite file (DESIGN §7.4), where a second
``BEGIN IMMEDIATE`` from the same thread can only wait out the busy timeout and fail."""


def _base(from_obj: FromClause) -> FromClause:
    """The object SQLAlchemy dedupes FROM lists by: an ORM-annotated table or alias compares equal
    to the plain one underneath, so both resolve to the plain object."""
    if isinstance(from_obj, Annotated):
        plain: Any = from_obj._deannotate()
        return plain
    return from_obj


def _mapper_of(element: Any) -> Mapper[Any] | None:
    """The mapper an ORM-annotated element belongs to (``Model.attr``, ``select(Model)``,
    ``aliased(Model)``, relationship join clauses); ``None`` for a plain Core element."""
    annotations = getattr(element, "_annotations", None)
    if not annotations:
        return None
    mapper = annotations.get("parentmapper")
    if mapper is None:
        mapper = getattr(annotations.get("parententity"), "mapper", None)
    return mapper if isinstance(mapper, Mapper) else None


def _describe(from_obj: FromClause) -> str:
    if isinstance(from_obj, Alias):
        inner = getattr(from_obj.element, "name", None)
        return f"an alias of table {inner!r}" if inner else "an alias"
    name = getattr(from_obj, "name", None)
    return f"table {name!r}" if name else type(from_obj).__name__


class _Scope:
    """One SELECT (or DML statement) in the expression tree. SQLAlchemy partitions each one
    separately, so the admissibility of a bare table reference is decided per scope."""

    __slots__ = ("dml_table", "entities", "parent", "refs", "statement")

    def __init__(self, statement: ClauseElement, parent: _Scope | None) -> None:
        self.statement = statement
        self.parent = parent
        self.entities: set[int] = set()  # ids of the FROM objects SQLAlchemy partitions here
        self.refs: list[FromClause] = []  # bare references that must dedupe into an entity
        self.dml_table: FromClause | None = None

    def enclosing(self) -> Iterator[_Scope]:
        scope = self.parent
        while scope is not None:
            yield scope
            scope = scope.parent


class _StatementScan:
    """Walks one statement for :meth:`DeskWallGuard._on_execute` (module docstring, bullet 4).

    ``run`` raises ``DeskWallViolation`` for verbatim SQL and unsupported statement kinds while
    collecting, per SELECT/DML scope, the FROM objects the ORM will partition ("entities": the
    deannotated table or alias behind every mapped class or attribute in the columns, FROM list,
    JOIN targets and WHERE clause) and the FROM objects referenced without an entity ("refs").
    ``enforce`` then refuses every ref that neither dedupes into an entity of its own scope nor
    is explicitly correlated to an entity of an enclosing scope. ``classes`` are the mapped
    classes touched anywhere (scope check) and ``saw_table`` says whether any table at all was
    referenced (a Core statement over a table is refused outright)."""

    def __init__(self) -> None:
        self.classes: dict[type[Any], None] = {}
        self.saw_table = False
        self._scopes: list[_Scope] = []
        self._descended: set[int] = set()

    # --- entry points

    def run(self, statement: Any) -> None:
        self._scan_statement(statement, None)

    def enforce(self) -> None:
        for scope in self._scopes:
            for ref in scope.refs:
                key = id(ref)
                if key in scope.entities or self._is_excluded(ref, scope):
                    continue
                if self._is_correlated(ref, scope):
                    continue
                raise DeskWallViolation(_BARE_MESSAGE.format(what=_describe(ref)))

    # --- scopes

    def _scan_statement(self, statement: Any, parent: _Scope | None) -> None:
        if isinstance(statement, TextClause | TextualSelect):
            raise DeskWallViolation(_RAW_SQL_MESSAGE)
        scope = _Scope(statement, parent)
        self._scopes.append(scope)
        self._check_decorations(statement)
        self._load_options(getattr(statement, "_with_options", ()))
        if isinstance(statement, CompoundSelect):
            for member in statement.selects:
                self._scan_statement(member, parent)
            self._visit_positions(
                statement,
                scope,
                ("_order_by_clauses", "_limit_clause", "_offset_clause", "_fetch_clause"),
                granting=False,
            )
            self._visit_for_update(statement, scope)
        elif isinstance(statement, Select):
            self._visit_positions(
                statement, scope, ("_raw_columns", "_from_obj", "_where_criteria"), granting=True
            )
            self._visit_joins(statement._setup_joins, scope)
            for memoized in statement._memoized_select_entities:
                # Entities ``with_only_columns`` replaced still drive the joins (whose mapped
                # targets get the criteria on their ON clause) but are no longer partitioned
                # themselves, so they count as plain references here.
                self._visit_all(memoized._raw_columns, scope, granting=False)
                self._visit_joins(memoized._setup_joins, scope)
                self._load_options(memoized._with_options)
            self._visit_positions(
                statement,
                scope,
                (
                    "_having_criteria",
                    "_order_by_clauses",
                    "_group_by_clauses",
                    "_distinct_on",
                    "_limit_clause",
                    "_offset_clause",
                    "_fetch_clause",
                    "_independent_ctes",
                    "_post_select_clause",
                    "_pre_columns_clause",
                    "_post_criteria_clause",
                    "_post_body_clause",
                ),
                granting=False,
            )
            self._visit_for_update(statement, scope)
        elif isinstance(statement, UpdateBase):
            table = statement.table
            scope.dml_table = _base(table)
            self._visit(table, scope, granting=True)
            # The WHERE of an UPDATE/DELETE partitions the target table only: a second table
            # there becomes an unpartitioned UPDATE … FROM, so it stays a plain reference.
            self._visit_positions(
                statement,
                scope,
                (
                    "_where_criteria",
                    "_returning",
                    "_return_defaults_columns",
                    "_independent_ctes",
                    "_post_criteria_clause",
                    "_post_values_clause",
                    "select",
                ),
                granting=False,
            )
            values = getattr(statement, "_values", None) or {}
            self._visit_all(list(values.values()), scope, granting=False)
            for batch in getattr(statement, "_multi_values", None) or ():
                for row in batch:
                    self._visit_all(
                        list(row.values()) if isinstance(row, dict) else list(row),
                        scope,
                        granting=False,
                    )
        elif isinstance(statement, FromStatement):
            self._visit_all(statement._raw_columns, scope, granting=True)
            self._scan_statement(statement.element, scope)
        else:
            raise DeskWallViolation(
                f"{type(statement).__name__} is not a SELECT or DML statement over mapped "
                "classes; it is not admitted through a token session"
            )

    def _visit_positions(
        self, statement: Any, scope: _Scope, attributes: Iterable[str], *, granting: bool
    ) -> None:
        for attribute in attributes:
            self._visit_all(getattr(statement, attribute, None), scope, granting=granting)

    def _visit_for_update(self, statement: Any, scope: _Scope) -> None:
        for_update = getattr(statement, "_for_update_arg", None)
        if for_update is not None:
            self._visit_all(getattr(for_update, "of", None), scope, granting=False)

    def _visit_joins(self, joins: Iterable[Any], scope: _Scope) -> None:
        for target, onclause, from_, _flags in joins:
            self._join_target(target, scope, granting=True)
            if onclause is not None:
                self._join_target(onclause, scope, granting=False)
            if from_ is not None:
                self._visit(from_, scope, granting=True)

    def _join_target(self, target: Any, scope: _Scope, *, granting: bool) -> None:
        """``.join(Model)`` / ``.join(Model.relationship)``: SQLAlchemy adds the loader criteria
        to the ON clause of a mapped join target, so the target is an entity of the scope."""
        if isinstance(target, QueryableAttribute):
            prop = target.property
            if isinstance(prop, RelationshipProperty):
                self.classes.setdefault(prop.parent.class_, None)
                self.classes.setdefault(prop.mapper.class_, None)
                of_type = getattr(target, "_of_type", None)
                entity = inspect(of_type) if of_type is not None else prop.entity
                self.saw_table = True
                scope.entities.add(id(_base(entity.selectable)))
                return
            self._visit(target.__clause_element__(), scope, granting=granting)
            return
        self._visit(target, scope, granting=granting)

    def _load_options(self, options: Iterable[Any]) -> None:
        """Loader options name the mappers a relationship load will touch (``selectinload``
        statements are hooked on their own; ``joinedload`` joins inside this statement)."""
        for option in options:
            if not isinstance(option, Load):
                continue
            for element in getattr(option, "context", ()):
                path = getattr(getattr(element, "path", None), "path", ())
                for step in path:
                    for attribute in ("mapper", "parent"):
                        mapper = getattr(step, attribute, None)
                        if isinstance(mapper, Mapper):
                            self.classes.setdefault(mapper.class_, None)

    # --- elements

    def _visit_all(self, value: Any, scope: _Scope, *, granting: bool) -> None:
        if value is None:
            return
        if isinstance(value, ClauseElement):
            self._visit(value, scope, granting=granting)
        elif isinstance(value, dict):
            self._visit_all(list(value.values()), scope, granting=granting)
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                self._visit_all(item, scope, granting=granting)

    def _visit(self, element: Any, scope: _Scope, *, granting: bool) -> None:
        if not isinstance(element, ClauseElement):
            return  # bound values, strings, option objects
        if isinstance(element, SelectBase | UpdateBase | FromStatement):
            self._scan_statement(element, scope)
            return
        self._check_verbatim(element)
        if isinstance(element, TableClause):
            self._reference(element, scope, granting=granting, mapper=_mapper_of(element))
            return
        if isinstance(element, Alias):  # also Lateral, TableSample, TableValuedAlias
            if isinstance(element.element, TableClause):
                self._reference(element, scope, granting=granting, mapper=_mapper_of(element))
            else:
                self._descend(element.element, scope, granting=granting)
            return
        if isinstance(element, Subquery | CTE):
            self._descend(element.element, scope, granting=False)
            return
        if isinstance(element, Join):
            # A manual join partitions only the entity the join is annotated with (its mapped
            # left side when built by the ORM); every leaf is a plain reference.
            entity = element._annotations.get("parententity")
            if granting and entity is not None:
                self.saw_table = True
                scope.entities.add(id(_base(entity.selectable)))
            self._visit(element.left, scope, granting=False)
            self._visit(element.right, scope, granting=False)
            self._visit(element.onclause, scope, granting=False)
            return
        if isinstance(element, ColumnClause):
            mapper = _mapper_of(element)
            if mapper is not None:
                self.classes.setdefault(mapper.class_, None)
            table = element.table
            if table is None:
                return
            if isinstance(table, TableClause) or (
                isinstance(table, Alias) and isinstance(table.element, TableClause)
            ):
                self._reference(table, scope, granting=granting, mapper=mapper)
            else:
                self._descend(table, scope, granting=False)
            return
        for child in element.get_children():
            self._visit(child, scope, granting=granting)

    def _descend(self, element: Any, scope: _Scope, *, granting: bool) -> None:
        """Enter a sub-select, CTE, join or values construct once per statement."""
        key = id(element)
        if key in self._descended:
            return
        self._descended.add(key)
        self._visit(element, scope, granting=granting)

    def _reference(
        self,
        from_obj: FromClause,
        scope: _Scope,
        *,
        granting: bool,
        mapper: Mapper[Any] | None,
    ) -> None:
        """A table (or alias of a table) the scope renders: an entity when the element that
        named it is ORM-annotated (``mapper``) and sits in a partitioning position, otherwise a
        reference to resolve in ``enforce``."""
        base = _base(from_obj)
        table = base.element if isinstance(base, Alias) else base
        self.saw_table = True
        if isinstance(table, Table):
            mapped = table.info.get(MAPPER_INFO_KEY)
            if mapped is not None:
                self.classes.setdefault(mapped, None)
        if mapper is not None:
            self.classes.setdefault(mapper.class_, None)
        if granting and mapper is not None:
            scope.entities.add(id(base))
        else:
            scope.refs.append(base)

    # --- admissibility of a bare reference

    @staticmethod
    def _is_excluded(ref: FromClause, scope: _Scope) -> bool:
        """The ``excluded`` pseudo-table of an upsert names the row being inserted, not a FROM."""
        return (
            scope.dml_table is not None
            and isinstance(ref, Alias)
            and ref.name == "excluded"
            and _base(ref.element) is scope.dml_table
        )

    @staticmethod
    def _is_correlated(ref: FromClause, scope: _Scope) -> bool:
        """``correlate()`` / ``correlate_except()`` hand the reference to an enclosing SELECT, so
        it is admissible when one of those partitions it (``any()``/``has()`` rely on this).
        Implicit auto-correlation is not trusted: SQLAlchemy never correlates away a sub-select's
        only FROM, which is exactly the leaking shape."""
        statement = scope.statement
        if not isinstance(statement, Select):
            return False
        if not any(id(ref) in outer.entities for outer in scope.enclosing()):
            return False
        except_ = statement._correlate_except
        if except_ is not None and not any(_base(f) is ref for f in except_):
            return True
        return any(_base(f) is ref for f in statement._correlate)

    # --- verbatim SQL

    @staticmethod
    def _check_verbatim(element: ClauseElement) -> None:
        if isinstance(element, TextClause | TextualSelect):
            raise DeskWallViolation(_RAW_SQL_MESSAGE)
        if (
            isinstance(element, ColumnClause)
            and element.is_literal
            and not _SAFE_LITERAL_COLUMN.fullmatch(str(element.name))
        ):
            raise DeskWallViolation(_VERBATIM_MESSAGE.format(what="literal_column()"))
        name = getattr(element, "name", None)
        if isinstance(name, quoted_name) and name.quote is False:
            raise DeskWallViolation(
                _VERBATIM_MESSAGE.format(what="an identifier with quoted_name(quote=False)")
            )
        if isinstance(element, BinaryExpression | UnaryExpression):
            for operator in (element.operator, getattr(element, "modifier", None)):
                if isinstance(operator, custom_op) and (
                    not _SAFE_CUSTOM_OP.fullmatch(operator.opstring)
                    or any(seq in operator.opstring for seq in _COMMENT_STARTERS)
                ):
                    raise DeskWallViolation(
                        _VERBATIM_MESSAGE.format(what=f"the custom operator {operator.opstring!r}")
                    )
        if isinstance(element, Extract) and not _SAFE_EXTRACT_FIELD.fullmatch(element.field):
            raise DeskWallViolation(_VERBATIM_MESSAGE.format(what="the extract() field"))

    @staticmethod
    def _check_decorations(statement: Any) -> None:
        """``prefix_with`` / ``suffix_with`` / ``with_hint`` text is rendered as given and never
        appears in the expression tree."""
        for attribute in ("_prefixes", "_suffixes"):
            for clause, _dialect in getattr(statement, attribute, None) or ():
                words = " ".join(str(getattr(clause, "text", clause)).split()).upper()
                if (
                    attribute == "_prefixes"
                    and isinstance(statement, UpdateBase)
                    and words in _DML_PREFIXES
                ):
                    continue
                raise DeskWallViolation(_VERBATIM_MESSAGE.format(what=f"{attribute[1:-2]}_with()"))
        if getattr(statement, "_hints", None) or getattr(statement, "_statement_hints", None):
            raise DeskWallViolation(_VERBATIM_MESSAGE.format(what="with_hint()"))


class GuardedSession(Session):
    """The ``Session`` subclass every ``SessionFactory`` opens: the paths SQLAlchemy offers around
    the ORM hooks are closed here (SPEC §5; DESIGN §4a)."""

    def connection(self, *args: Any, **kwargs: Any) -> Connection:
        raise DeskWallViolation(
            "session.connection() bypasses the desk wall; execute ORM statements through the "
            "session (migrations run on the engine, never through a token session)"
        )

    def bulk_save_objects(self, *args: Any, **kwargs: Any) -> None:
        raise DeskWallViolation(f"bulk_save_objects: {_BULK_API_MESSAGE}")

    def bulk_insert_mappings(self, *args: Any, **kwargs: Any) -> None:
        raise DeskWallViolation(f"bulk_insert_mappings: {_BULK_API_MESSAGE}")

    def bulk_update_mappings(self, *args: Any, **kwargs: Any) -> None:
        raise DeskWallViolation(f"bulk_update_mappings: {_BULK_API_MESSAGE}")


class DeskWallGuard:
    """§5 the SQLite-side wall (mirrored by PG grants/RLS):
    do_orm_execute → raise DeskWallViolation for any mapper whose __scope__ the token may not read; add
      with_loader_criteria(desk == token.desk) to every DESK_ROW mapper (a query without a desk filter is still partitioned).
    before_flush → every new/dirty DESK_ROW row must carry desk == token.desk; forbidden scopes raise; dirty or
      deleted __append_only__ rows raise AppendOnlyViolation; __single_transition__ columns may change only from NULL;
      __forward_only__ status may only move forward.

    Beyond the DESIGN summary (see the module docstring): every statement is walked by
    ``_StatementScan`` — raw/verbatim SQL, Core statements and bare table references the ORM would
    not partition are refused and the loader criteria are registered for every DESK_ROW mapper of
    the registry; bulk DML on a DESK_ROW mapper is checked for the desk it writes (and may not
    upsert); reader sessions may not write; the auditor's report writer may only INSERT into
    AUDITOR_WRITE mappers."""

    def __init__(self, token: DeskToken | AuditorToken) -> None:
        if not isinstance(token, DeskToken | AuditorToken):
            raise TypeError(
                f"DeskWallGuard takes a DeskToken or AuditorToken, got {type(token).__name__}"
            )
        require_minted(token, "DeskWallGuard")
        self.token = token
        self._criteria_cache: dict[frozenset[Mapper[Any]], tuple[Any, ...]] = {}

    @property
    def desk(self) -> Desk | None:
        return self.token.desk if isinstance(self.token, DeskToken) else None

    def install(self, session: Session) -> None:
        event.listen(session, "do_orm_execute", self._on_execute)
        event.listen(session, "before_flush", self._before_flush)

    # --- scope checks

    def check_scope(self, mapped_class: type[Any], *, write: bool) -> None:
        scope = scope_of(mapped_class)
        if not scope_allows(scope, self.token, write=write):
            verb = "write" if write else "read"
            raise DeskWallViolation(
                f"{self._who()} may not {verb} {mapped_class.__name__} (scope {scope.value})"
            )

    def _who(self) -> str:
        return type(self.token).__name__

    # --- statement hook

    def _on_execute(self, state: ORMExecuteState) -> None:
        statement = state.statement
        info = state.session.info
        scan = _StatementScan()
        scan.run(statement)  # raw/verbatim SQL and foreign statement kinds raise here
        classes: dict[type[Any], None] = {m.class_: None for m in state.all_mappers}
        classes.update(scan.classes)
        is_dml = isinstance(statement, UpdateBase)
        if not state.is_orm_statement:
            if classes or is_dml or scan.saw_table or not isinstance(statement, SelectBase):
                raise DeskWallViolation(_CORE_MESSAGE)
            return  # a scalar select such as select(literal(1)): no table, nothing to guard
        scan.enforce()  # every bare table reference must dedupe into a partitioned entity
        write = is_dml
        if write and info.get(READER_INFO_KEY):
            raise DeskWallViolation("reader session: use SessionFactory.write() to change rows")
        if info.get(REPORT_INFO_KEY) and (state.is_update or state.is_delete):
            raise DeskWallViolation("append_report() may only INSERT auditor rows")
        for cls in classes:
            self.check_scope(cls, write=write)
            if (
                write
                and (state.is_update or state.is_delete)
                and getattr(cls, "__append_only__", False)
            ):
                raise AppendOnlyViolation(f"{cls.__name__} is append-only; UPDATE/DELETE refused")
            if scope_of(cls) is Scope.DESK_ROW:
                desk = self.desk
                if desk is None:
                    raise DeskWallViolation(
                        f"{self._who()} has no desk partition for {cls.__name__}"
                    )
                if state.is_insert or state.is_update:
                    self._check_dml_desk(cls, state, desk)
        options = self._partition_options(classes)
        if options:
            state.statement = state.statement.options(*options)

    def _partition_options(self, classes: Iterable[type[Any]]) -> tuple[Any, ...]:
        """``with_loader_criteria(desk == token.desk)`` for every ``DESK_ROW`` mapper of every
        registry the statement touches — not only the mappers visible in the statement, because
        a relationship join (``join(Child.parent)``), a joined eager load or a lazy load reaches
        its target through the mapper, where the criteria must already be registered."""
        desk = self.desk
        if desk is None:
            return ()
        options: list[Any] = []
        seen: set[int] = set()
        for cls in classes:
            registry = inspect(cls).registry
            if id(registry) in seen:
                continue
            seen.add(id(registry))
            mappers = registry.mappers
            cached = self._criteria_cache.get(mappers)
            if cached is None:
                cached = tuple(self._desk_criteria(mappers, desk))
                self._criteria_cache[mappers] = cached
            options.extend(cached)
        return tuple(options)

    @staticmethod
    def _desk_criteria(mappers: Iterable[Mapper[Any]], desk: Desk) -> Iterator[Any]:
        """One loader criterion per ``DESK_ROW`` mapper, in a stable order. A ``DESK_ROW`` mapper
        without a mapped ``desk`` column — a class mapped around ``Base`` (which refuses and
        un-maps that shape) or re-marked after mapping — cannot be partitioned, so the statement
        is refused rather than run against an open partition (fail closed, SPEC §5)."""
        for mapper in sorted(mappers, key=lambda m: m.class_.__qualname__):
            cls = mapper.class_
            if scope_of(cls) is not Scope.DESK_ROW:
                continue
            desk_column = getattr(cls, "desk", None)
            if not isinstance(desk_column, QueryableAttribute):
                raise DeskWallViolation(
                    f"{cls.__name__} is marked DESK_ROW but maps no desk column, so the desk "
                    "wall cannot partition it; every DESK_ROW mapper derives from Base with "
                    "DeskMixin"
                )
            yield with_loader_criteria(cls, desk_column == desk, include_aliases=True)

    # --- bulk DML on a DESK_ROW mapper: the desk VALUE, not only the WHERE

    def _check_dml_desk(self, cls: type[Any], state: ORMExecuteState, desk: Desk) -> None:
        """An ``insert(Model)`` must give every row ``desk == token.desk`` (literally, never through
        an expression or a sub-select) and may not carry ``ON CONFLICT DO UPDATE`` (on SQLite the
        conflicting row may belong to the other partition; Postgres RLS refuses that itself); an
        ``update(Model)`` may not set ``desk`` at all."""
        statement = state.statement
        rows = self._dml_rows(statement, state.parameters)
        if state.is_update:
            if any("desk" in row for row in rows):
                raise DeskWallViolation(
                    f"{self._who()} may not UPDATE {cls.__name__}.desk: rows never change partition"
                )
            return
        if isinstance(statement, ValuesBase) and getattr(statement, "select", None) is not None:
            raise DeskWallViolation(
                f"INSERT … FROM SELECT into {cls.__name__} cannot be checked for its desk; "
                "insert explicit rows"
            )
        post_values = getattr(statement, "_post_values_clause", None)
        if isinstance(post_values, SqliteOnConflictDoUpdate | PgOnConflictDoUpdate):
            raise DeskWallViolation(
                f"an upsert (ON CONFLICT DO UPDATE) into {cls.__name__} could rewrite the "
                "conflicting row of the other partition; use on_conflict_do_nothing() or a "
                "separate update()"
            )
        if not rows:
            raise DeskWallViolation(
                f"an INSERT into {cls.__name__} must name desk={desk.value!r} in every row"
            )
        for row in rows:
            if "desk" not in row:
                raise DeskWallViolation(
                    f"an INSERT into {cls.__name__} must name desk={desk.value!r} in every row"
                )
            value = row["desk"]
            if isinstance(value, BindParameter):
                value = value.effective_value
            if isinstance(value, ClauseElement) or not isinstance(value, str):
                raise DeskWallViolation(
                    f"{cls.__name__}.desk must be a literal desk value in an INSERT"
                )
            try:
                given = Desk(value)
            except ValueError:
                raise DeskWallViolation(
                    f"{cls.__name__}.desk must be a literal desk value in an INSERT"
                ) from None
            if given is not desk:
                raise DeskWallViolation(
                    f"{self._who()} may not write a {cls.__name__} row for desk {given.value!r}"
                )

    @staticmethod
    def _dml_rows(statement: Any, parameters: Any) -> list[dict[str, Any]]:
        """The rows a DML statement writes, keyed by column name: ``.values(...)`` (single or
        multi-row, dicts or positional tuples) plus any executemany parameters."""
        rows: list[dict[str, Any]] = []

        def column_name(key: Any) -> str:
            name = getattr(key, "name", None)
            return name if isinstance(name, str) else str(key)

        table = getattr(statement, "table", None)
        table_columns = [column_name(c) for c in table.c] if table is not None else []
        values = getattr(statement, "_values", None)
        if values:
            rows.append({column_name(key): value for key, value in values.items()})
        for batch in getattr(statement, "_multi_values", None) or ():
            for row in batch:
                if isinstance(row, dict):
                    rows.append({column_name(key): value for key, value in row.items()})
                else:
                    rows.append(dict(zip(table_columns, row, strict=False)))
        if parameters:
            batches = parameters if isinstance(parameters, list | tuple) else [parameters]
            for row in batches:
                if isinstance(row, dict):
                    rows.append({column_name(key): value for key, value in row.items()})
        return rows

    # --- flush hook

    def _before_flush(self, session: Session, _flush_context: Any, _instances: Any) -> None:
        new = list(session.new)
        dirty = [
            obj for obj in session.dirty if session.is_modified(obj, include_collections=False)
        ]
        deleted = list(session.deleted)
        if session.info.get(READER_INFO_KEY) and (new or dirty or deleted):
            raise DeskWallViolation("reader session: use SessionFactory.write() to change rows")
        if session.info.get(REPORT_INFO_KEY) and (dirty or deleted):
            raise DeskWallViolation("append_report() may only INSERT auditor rows")
        for obj in new:
            cls = type(obj)
            self.check_scope(cls, write=True)
            self._check_desk(obj)
        for obj in dirty:
            cls = type(obj)
            self.check_scope(cls, write=True)
            if getattr(cls, "__append_only__", False):
                raise AppendOnlyViolation(f"{cls.__name__} is append-only; rows cannot change")
            self._check_desk(obj)
            self._check_single_transition(obj)
            self._check_forward_only(obj)
        for obj in deleted:
            cls = type(obj)
            self.check_scope(cls, write=True)
            if getattr(cls, "__append_only__", False):
                raise AppendOnlyViolation(f"{cls.__name__} is append-only; rows cannot be deleted")

    def _check_desk(self, obj: Any) -> None:
        cls = type(obj)
        if scope_of(cls) is not Scope.DESK_ROW:
            return
        desk = self.desk
        value = getattr(obj, "desk", None)
        if value is None:
            raise DeskWallViolation(
                f"{cls.__name__} row must carry desk={desk.value if desk else None!r}"
            )
        if desk is None or Desk(value) is not desk:
            raise DeskWallViolation(
                f"{self._who()} may not write a {cls.__name__} row for desk {Desk(value).value!r}"
            )

    @staticmethod
    def _change(obj: Any, column: str) -> tuple[bool, Any, Any]:
        history = inspect(obj).attrs[column].history
        if not history.has_changes():
            return False, None, None
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        return True, old, new

    def _check_single_transition(self, obj: Any) -> None:
        for column in getattr(type(obj), "__single_transition__", ()):
            changed, old, new = self._change(obj, column)
            if changed and old is not None and new != old:
                raise SingleTransitionViolation(
                    f"{type(obj).__name__}.{column} was already set and may not change"
                )

    def _check_forward_only(self, obj: Any) -> None:
        for column, order in getattr(type(obj), "__forward_only__", {}).items():
            changed, old, new = self._change(obj, column)
            if not changed or old is None or new == old:
                continue
            old_value = old.value if hasattr(old, "value") else old
            new_value = new.value if hasattr(new, "value") else new
            if new_value not in order or old_value not in order:
                raise SingleTransitionViolation(
                    f"{type(obj).__name__}.{column}: unknown state in {old_value!r} -> {new_value!r}"
                )
            if order.index(new_value) <= order.index(old_value):
                raise SingleTransitionViolation(
                    f"{type(obj).__name__}.{column} may only move forward "
                    f"({old_value!r} -> {new_value!r})"
                )


class SessionFactory:
    """One per process, bound to a token. Writers use BEGIN IMMEDIATE (SQLite) / SET LOCAL nour.desk (PG).

    ``report_engine`` (keyword-only, additive to the DESIGN §3.7 signature) is the auditor's
    writable engine for :meth:`append_report`; it is refused for a desk token. An
    ``AuditorToken`` factory requires ``engine`` to be read-only (``make_engine(read_only=True)``).
    """

    def __init__(
        self,
        engine: Engine,
        token: DeskToken | AuditorToken,
        clock: Clock,
        *,
        report_engine: Engine | None = None,
    ) -> None:
        if not isinstance(token, DeskToken | AuditorToken):
            raise TypeError(
                f"SessionFactory takes a DeskToken or AuditorToken, got {type(token).__name__}"
            )
        require_minted(token, "SessionFactory")
        if isinstance(token, AuditorToken):
            if not is_read_only(engine):
                raise DeskWallViolation(
                    "an AuditorToken factory needs a read-only engine "
                    "(make_engine(url, read_only=True)); auditor_report rows go through "
                    "append_report() on report_engine="
                )
            if report_engine is not None and is_read_only(report_engine):
                raise ValueError("report_engine must be writable: it is where auditor_report lands")
        elif report_engine is not None:
            raise TypeError(
                "report_engine is for the AuditorToken only; desks write through write()"
            )
        self.engine = engine
        self.token = token
        self.clock = clock
        self.report_engine = report_engine
        self.guard = DeskWallGuard(token)
        self._write_lock = threading.Lock()

    @property
    def desk(self) -> Desk | None:
        return self.guard.desk

    @property
    def is_auditor(self) -> bool:
        return isinstance(self.token, AuditorToken)

    def _new_session(
        self, bind: Engine | Connection, *, reader: bool, report: bool = False
    ) -> Session:
        session = GuardedSession(bind=bind, expire_on_commit=False, autoflush=True)
        session.info[READER_INFO_KEY] = reader
        session.info[REPORT_INFO_KEY] = report
        self.guard.install(session)
        if self.engine.dialect.name == "postgresql":
            desk = self.desk
            auditor = self.is_auditor

            def _after_begin(
                _session: Session, _tx: SessionTransaction, connection: Connection
            ) -> None:
                if auditor:
                    mode = "READ WRITE" if report else "READ ONLY"
                    connection.execute(text(f"SET TRANSACTION {mode}"))
                elif desk is not None:
                    connection.execute(
                        text("SELECT set_config(:name, :desk, true)"),
                        {"name": DESK_SETTING, "desk": desk.value},
                    )

            event.listen(session, "after_begin", _after_begin)
        return session

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A reader session (plain ``BEGIN``): any flush or DML through it raises
        ``DeskWallViolation``; read-only for an ``AuditorToken`` (read-only engine). Rolls back
        whatever is pending when the block ends."""
        session = self._new_session(self.engine, reader=True)
        try:
            yield session
        finally:
            session.close()

    @contextmanager
    def _unit_of_work(self, engine: Engine, *, report: bool) -> Iterator[Session]:
        if getattr(_writing, "active", False):
            raise RuntimeError(
                "SessionFactory.write() is not re-entrant: this thread already holds a writer "
                "unit of work; commit the outer one first (a nested BEGIN IMMEDIATE on SQLite "
                "would only wait out the busy timeout)"
            )
        with self._write_lock:
            _writing.active = True
            try:
                connection = engine.connect().execution_options(**{WRITER_OPTION: True})
                try:
                    session = self._new_session(connection, reader=False, report=report)
                    try:
                        yield session
                        if session.in_transaction():
                            session.commit()
                    except BaseException:
                        session.rollback()
                        raise
                    finally:
                        session.close()
                finally:
                    connection.close()
            finally:
                _writing.active = False

    @contextmanager
    def write(self) -> Iterator[Session]:
        """The writer unit of work: raises for an ``AuditorToken``; serialises writers of this
        factory and refuses re-entry from the same thread; ``BEGIN IMMEDIATE`` on SQLite; commits
        on clean exit, rolls back on an exception."""
        if self.is_auditor:
            raise DeskWallViolation(
                "AuditorToken sessions are read-only; write() is refused (append_report() is the "
                "auditor's one writer)"
            )
        with self._unit_of_work(self.engine, report=False) as session:
            yield session

    @contextmanager
    def append_report(self) -> Iterator[Session]:
        """The auditor's one writer (DESIGN §3.11 ``AuditorRunner`` writes ``auditor_report``):
        a unit of work on ``report_engine`` that admits ``INSERT`` into ``AUDITOR_WRITE``
        mappers only — every other mapper, every UPDATE/DELETE and every Core/raw statement is
        refused. Raises ``DeskWallViolation`` for a desk token or a factory built without a
        ``report_engine``."""
        if not self.is_auditor:
            raise DeskWallViolation(
                "append_report() is the AuditorToken's writer; desks use write()"
            )
        if self.report_engine is None:
            raise DeskWallViolation(
                "this auditor factory has no report_engine; pass one to write auditor_report"
            )
        with self._unit_of_work(self.report_engine, report=True) as session:
            yield session
