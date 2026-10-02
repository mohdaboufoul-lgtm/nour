"""Nour language layer: prompt assembly, injection scanning, Arabic speech, the self-critic
(DESIGN §3.10, §8 "Wave 1"; SPEC §2 §8 §9 §12 §13 §18).

Like ``nour.core`` and ``nour.config``, this package re-exports nothing: consumers import from the
submodule that owns the name (``nour.language.prompt``, ``nour.language.injection``,
``nour.language.speech``, ``nour.language.critic``). It imports ``nour.core`` and ``nour.config``
only (DESIGN §8 import graph: wave 1 never imports a sibling).
"""
