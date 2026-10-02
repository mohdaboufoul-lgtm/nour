"""Nour DB foundation: declarative base, scopes, column types, engine and the desk-walled session
(DESIGN §3.7; SPEC §5 §12 §13 §15).

Re-exports nothing: ``nour.db.base`` (``Base``, ``Scope``, mixins, ``Ciphertext``,
``EncryptedBytes``, ``JSONCol``, ``MoneyCol``, ``scope_allows``), ``nour.db.engine``
(``make_engine``, ``create_schema``, the DDL generators) and ``nour.db.session``
(``SessionFactory``, ``DeskWallGuard``) are imported by name. The ORM models themselves are
wave 1 (``nour/db/models.py``).

``nour.db`` imports ``nour.core`` only (DESIGN §8 "Import graph").
"""
