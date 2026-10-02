"""Alembic migration environment for Nour (DESIGN §5.3; SPEC §12 §13 §15).

``alembic.ini`` at the repository root points here. ``env.py`` builds the engine with
``nour.db.engine.make_engine`` (same pragmas and schema translation as the runtime), uses
``nour.db.base.Base.metadata`` — every mapper of ``nour.db.models`` — as the autogenerate target,
and ``versions/`` holds the chain. A migration never imports ``nour.db.models``: its table
definitions are frozen at the revision and only the DDL *generators* (``trigger_ddl``,
``pg_roles_ddl``) are shared with ``create_schema``, so ``tests/unit/test_migrations_match_metadata.py``
can prove the chain and the live metadata agree instead of assuming it.
"""
