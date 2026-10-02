"""Nour config: the owner-editable files under ``config/`` and ``prompts/`` as frozen models
(DESIGN §3.6; SPEC §15 §18).

Like ``nour.core``, this package re-exports nothing: consumers import from the submodule that
owns the name (``nour.config.schema`` for the models, ``nour.config.loader`` for ``load_config``
and ``compute_config_hash``, ``nour.config.settings`` for ``Settings``), so the import graph stays
explicit for ``tests/unit/test_wave_imports.py``.

``nour.config`` imports ``nour.core`` only (DESIGN §8 "Import graph").
"""
