"""Nour core: the wave-0 foundation every later module imports (DESIGN §3.0, §8 "Wave 0").

This package deliberately re-exports nothing. Every consumer imports from the submodule that
owns the name (``nour.core.types``, ``nour.core.errors``, ``nour.core.tokens``, ``nour.core.clock``,
``nour.core.hashing``, ``nour.core.leakguard``, ``nour.core.tier2``, ``nour.core.ports``,
``nour.core.contracts``), so the import graph stays explicit and the AST walls in
``tests/unit/test_walls.py`` can confine sealed constructors to single files.

``nour.core`` imports nothing from ``nour.config`` or ``nour.db`` (DESIGN §8 "Import graph").
"""
