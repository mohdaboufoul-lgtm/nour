"""Phase 0 gate — capability definition of done (SPEC §16: "every capability in section 7 has a tier
in config, a log entry format, a test scenario in the weekly regression set, and an owner who has
seen it run once"; DESIGN §7.3 ``tests/gate/test_capability_dod.py``).

For every row of ``config/capabilities.yaml`` (DESIGN §3.6 ``Capability``):

* phase-0 rows have a tier in config, every category they name is known to the config, every tool
  they name is a registered ``ToolSpec`` on the desk(s) the row names (or the row names a routine),
  the registry validates against the config (the log-entry format: action + category of every audit
  row), and a ``@pytest.mark.weekly`` scenario references the capability id;
* phase > 0 rows are registered as stubs that raise ``NotInPhase``.

The owner-has-seen-it-run item is the gate review itself, not a test.
"""

from __future__ import annotations

import pytest

pytest.importorskip("nour.testing.harness")

from pathlib import Path
from typing import Any

# Waves 1-6 modules: ruff classifies an import by its on-disk path, so their isort section
# flips from third- to first-party as each wave lands; hand-sorted in the final order instead.
# isort: off
from nour.core.errors import NotInPhase
from nour.core.types import Desk, DeskScope
# isort: on

SCENARIOS_DIR = Path(__file__).resolve().parents[1] / "scenarios"
WEEKLY_MARKER = "pytest.mark.weekly"
TIMER_ROUTINES = frozenset(
    {
        "morning_brief",
        "evening_close",
        "nightly_reflection",
        "weekly_review",
        "auditor_run",
        "notify_sweep",
        "owner_silence_check",
        "cadence_tick",
    }
)


@pytest.fixture(scope="module")
def h(tmp_path_factory: pytest.TempPathFactory) -> Any:
    from nour.testing.harness import Harness

    built = Harness.build(tmp_path_factory.mktemp("dod"))
    try:
        yield built
    finally:
        built.close()


@pytest.fixture(scope="module")
def weekly_scenarios() -> dict[str, str]:
    """File name → source text of every ``tests/scenarios/test_*.py`` that carries ``@pytest.mark.weekly``."""
    sources = {
        f.name: f.read_text(encoding="utf-8") for f in sorted(SCENARIOS_DIR.glob("test_*.py"))
    }
    return {name: text for name, text in sources.items() if WEEKLY_MARKER in text}


def _registries(h: Any, scope: DeskScope) -> list[Any]:
    # ASSUMPTION: the per-desk ``ToolRegistry`` built by ``build_desk`` is reachable as
    # ``h.desks[desk].registry`` (DESIGN §3.20 ``DeskProcess.registry``); a BOTH row must be
    # registered on both desks, a GOVERNANCE row has no tool registry (routines only).
    if scope == DeskScope.OPERATOR:
        return [h.desks[Desk.OPERATOR].registry]
    if scope == DeskScope.ASSISTANT:
        return [h.desks[Desk.ASSISTANT].registry]
    if scope == DeskScope.BOTH:
        return [h.desks[Desk.OPERATOR].registry, h.desks[Desk.ASSISTANT].registry]
    return []


def _spec(registry: Any, name: str) -> Any | None:
    try:
        return registry.spec(name)
    except KeyError:
        return None


def _routine_registered(h: Any, routine: str) -> bool:
    # ASSUMPTION: a routine is one of the scheduler's timer names (DESIGN §3.15), a governance
    # service the harness exposes (``kill``, ``auditor``, ``confirmations``, ...) or a
    # ``Service.method`` of nour/tools/routines.py (``BriefService.morning``).
    if routine in TIMER_ROUTINES or hasattr(h, routine):
        return True
    if "." in routine:
        import nour.tools.routines as routines

        cls_name, _, method = routine.partition(".")
        cls = getattr(routines, cls_name, None)
        return cls is not None and callable(getattr(cls, method, None))
    return False


def _raises_not_in_phase(handler: Any) -> bool:
    """A stub handler ignores its arguments and raises NotInPhase (ToolSpec.handler=None is the
    executor-level equivalent)."""
    try:
        handler(None, None)
    except NotInPhase:
        return True
    except Exception:
        return False
    return False


@pytest.mark.gate
def test_registries_validate_against_config(h: Any) -> None:
    """Every registered ToolSpec names a category the config knows and every phase-0 capability has
    its tools (DESIGN §3.16 ``ToolRegistry.validate``) — the log-entry format of SPEC §16."""
    for desk in (Desk.OPERATOR, Desk.ASSISTANT):
        problems = h.desks[desk].registry.validate(h.cfg)
        assert problems == [], (desk, problems)


@pytest.mark.gate
def test_phase_0_capabilities_are_done(h: Any, weekly_scenarios: dict[str, str]) -> None:
    capabilities = h.cfg.capabilities.capabilities
    assert capabilities, "config/capabilities.yaml has no rows"
    phase_0 = [row for row in capabilities if row.phase == 0]
    assert phase_0, "no phase-0 capability rows"
    assert weekly_scenarios, f"no @weekly scenario files under {SCENARIOS_DIR}"
    known_categories = h.cfg.known_categories()
    problems: list[str] = []

    for row in phase_0:
        if not row.tier.strip():
            problems.append(f"{row.id}: no tier in config")
        for category in row.categories:
            if category not in known_categories:
                problems.append(f"{row.id}: category {category!r} unknown to the config")
        if not row.tools and not row.routine:
            problems.append(f"{row.id}: names neither a tool nor a routine")
        for registry in _registries(h, row.desk):
            for tool in row.tools:
                spec = _spec(registry, tool)
                if spec is None:
                    problems.append(f"{row.id}: tool {tool!r} is not registered for {row.desk}")
                    continue
                if spec.phase != 0 or spec.handler is None:
                    problems.append(
                        f"{row.id}: tool {tool!r} is registered as a phase {spec.phase} stub"
                    )
                if spec.category not in known_categories:
                    problems.append(
                        f"{row.id}: tool {tool!r} logs category {spec.category!r} unknown to the config"
                    )
        if row.desk == DeskScope.GOVERNANCE and row.tools and not row.routine:
            problems.append(f"{row.id}: a governance row needs a routine, it has no tool registry")
        if row.routine and not _routine_registered(h, row.routine):
            problems.append(f"{row.id}: routine {row.routine!r} is not registered")
        if not any(row.id in text for text in weekly_scenarios.values()):
            problems.append(f"{row.id}: no @weekly scenario under tests/scenarios references it")

    assert not problems, "\n".join(problems)


@pytest.mark.gate
def test_later_phase_capabilities_are_stubs(h: Any) -> None:
    later = [row for row in h.cfg.capabilities.capabilities if row.phase > 0]
    assert later, "no phase 1-3 capability rows"
    problems: list[str] = []

    for row in later:
        if not row.tools and not row.routine:
            problems.append(f"{row.id}: names neither a tool nor a routine")
        # A routine-only later-phase row (renewals engine, reconciliation) has no tool to stub.
        for registry in _registries(h, row.desk):
            for tool in row.tools:
                spec = _spec(registry, tool)
                if spec is None:
                    problems.append(
                        f"{row.id}: phase {row.phase} tool {tool!r} has no stub registered for {row.desk}"
                    )
                    continue
                if spec.phase != row.phase:
                    problems.append(
                        f"{row.id}: stub {tool!r} says phase {spec.phase}, the register says {row.phase}"
                    )
                if spec.handler is not None and not _raises_not_in_phase(spec.handler):
                    problems.append(
                        f"{row.id}: {tool!r} has a live handler in phase 0 instead of a NotInPhase stub"
                    )

    assert not problems, "\n".join(problems)
