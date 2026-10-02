"""Alembic environment (DESIGN §5.3 "Migrations and dialects"; SPEC §12 §13 §15).

* Online, the engine comes from :func:`nour.db.engine.make_engine`, so a SQLite target gets the
  runtime pragmas (WAL, ``foreign_keys=ON``, ``recursive_triggers=ON``), a writer connection
  begins ``BEGIN IMMEDIATE``, and the Postgres schema ``auditor`` is translated away on SQLite
  exactly as ``create_schema`` does (:func:`nour.db.engine.install_sqlite_schema_translation`).
* Offline (``alembic upgrade head --sql``) is Postgres-only: the emitted SQL carries the
  schema-qualified ``auditor.auditor_report`` and the role/grant/RLS DDL, and no schema
  translation applies to a script that is not executed, so a SQLite URL is refused with
  ``CommandError`` rather than printing DDL SQLite cannot run.
* ``target_metadata`` is ``Base.metadata`` with every mapper of :mod:`nour.db.models` registered,
  so ``alembic check`` / ``alembic revision --autogenerate`` compare against the real models with
  ``include_schemas=True`` and ``compare_type=True``.
* The database URL is resolved in this order: ``alembic -x url=<dsn>``; ``sqlalchemy.url`` when a
  caller set it on the ``Config`` programmatically (tests, ``nour migrate``); otherwise
  ``Settings().database_url`` (``NOUR_DATABASE_URL``). It is never written into ``alembic.ini``.
* The migration runs as the schema owner (the migrator credentials); the runtime roles created by
  ``0001_initial`` are ``NOLOGIN`` until the operator sets their passwords out of band.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.util.exc import CommandError
from sqlalchemy.engine import Connection, make_url

from nour.config.settings import Settings
from nour.db.base import Base
from nour.db.engine import WRITER_OPTION, install_sqlite_schema_translation, make_engine
from nour.db.models import metadata_for_dialect

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
"""Every mapper of ``nour.db.models`` (imported above). The comparison target handed to alembic
is :func:`metadata_for_dialect`: on SQLite a schema-free view, because autogenerate does not
apply the ``schema_translate_map`` that puts ``auditor.auditor_report`` into the one file."""


def database_url() -> str:
    """``-x url=…`` > programmatic ``sqlalchemy.url`` > ``Settings().database_url``."""
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("url"):
        return str(x_args["url"])
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return Settings().database_url


def run_migrations_offline() -> None:
    """Emit the SQL for ``database_url()``'s dialect without connecting (``alembic --sql``);
    Postgres only (module docstring)."""
    url = database_url()
    dialect = make_url(url).get_backend_name()
    if dialect == "sqlite":
        raise CommandError(
            "offline SQL is Postgres-only: SQLite has no schemas, so the schema-qualified "
            "auditor.auditor_report DDL cannot run there; migrate SQLite online "
            "(alembic upgrade head without --sql)"
        )
    context.configure(
        url=url,
        target_metadata=metadata_for_dialect(dialect),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=dialect != "sqlite",
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    dialect = connection.dialect.name
    context.configure(
        connection=connection,
        target_metadata=metadata_for_dialect(dialect),
        include_schemas=dialect != "sqlite",
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect through ``make_engine`` (runtime pragmas, schema translation) and migrate."""
    engine = make_engine(database_url())
    try:
        install_sqlite_schema_translation(engine, target_metadata)
        with engine.connect().execution_options(**{WRITER_OPTION: True}) as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
