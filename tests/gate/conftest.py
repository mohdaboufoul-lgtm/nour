"""Shared fixtures for the phase 0 gate (SPEC §16; DESIGN §7.1, §7.3, §3.21; MODULES.md "acceptance").

Every test in this directory consumes ``nour.testing.harness.Harness`` only. The harness is wave 6;
until it lands every test module here skips at collection (each calls
``pytest.importorskip("nour.testing.harness")`` at import time) and the fixtures below call it
lazily, so ``pytest tests/gate`` run directly also skips instead of failing inside an initial
conftest.

Command line: ``pytest -m gate tests/gate [--live-model]``. ``--live-model`` (DESIGN §7: swap
``ScriptedModel`` for the configured vendor adapters, keep every other fake) is registered by this
conftest, so pytest recognises it only when ``tests/gate`` or a file in it is on the command line.

Conventions used by every gate module:

* ``Harness.expect(event_id, tier=..., status=..., ...)`` is the §18 triple check.
  # ASSUMPTION: ``tier=None`` is read as "do not assert the tier" (a row produced by a refusal or
  # a freeze may or may not carry one); every other argument given is asserted exactly.
* ``Harness.audit_rows(**filters)`` takes column equalities (``event_id=``, ``status=``, ``tier=``,
  ``phase=``, ``action=``).
  # ASSUMPTION: the filter names are the ``audit_event`` column names of DESIGN §5.1.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from nour.core.types import Actor

if TYPE_CHECKING:
    from nour.testing.harness import Harness

HARNESS_MODULE = "nour.testing.harness"

LIVE_MODEL = pytest.StashKey[bool]()
"""Where the ``--live-model`` flag is stored on ``config`` (``config.stash[LIVE_MODEL]``)."""

OWNER_FACING_ACTIONS = frozenset({"owner.reply", "owner.notify", "owner.readback", "escalate_to_owner"})
"""Tool names whose audit rows talk to the owner rather than act on the world (DESIGN §3.18)."""

HarnessFactory = Callable[..., "Harness"]
RowsFn = Callable[..., list[Any]]
ClosedRowsFn = Callable[..., list[Any]]


# --------------------------------------------------------------------------- options


def pytest_addoption(parser: pytest.Parser) -> None:
    try:
        parser.addoption(
            "--live-model",
            action="store_true",
            default=False,
            help="DESIGN §7: run the gate with the configured vendor model adapters instead of "
            "ScriptedModel; every other fake stays (weekly live regression, SPEC §13).",
        )
    except ValueError:
        # Already registered by another conftest in the same run (tests/scenarios declares the
        # same option); the first registration wins and the flag is shared.
        pass


def pytest_configure(config: pytest.Config) -> None:
    config.stash[LIVE_MODEL] = bool(config.getoption("--live-model", default=False))


@pytest.fixture(scope="session")
def live_model(request: pytest.FixtureRequest) -> bool:
    """True when the gate runs against the real vendor adapters (``--live-model``)."""
    return bool(request.config.stash.get(LIVE_MODEL, False))


# --------------------------------------------------------------------------- harness factory


def make_harness(tmp_path: Path, **kw: Any) -> Harness:
    """``Harness.build(tmp_path, **kw)`` (DESIGN §3.21), skipping cleanly while wave 6 is absent.

    ``kw`` is passed through untouched: ``start``, ``policy``, ``seed``, ``dry_run``, ``tick``,
    ``config_dir``, ``prompts_dir``, ``owner_number``, ``passphrase``, ``second_channel_address``.
    The caller owns ``close()``; the ``harness`` fixture below does it on teardown.
    """
    harness_module = pytest.importorskip(HARNESS_MODULE)
    return harness_module.Harness.build(tmp_path, **kw)


_VENDOR_ADAPTERS: dict[str, tuple[str, str]] = {
    "anthropic": ("nour.adapters.model_anthropic", "AnthropicModel"),
    "openai": ("nour.adapters.model_openai", "OpenAIModel"),
}


def _swap_in_live_models(h: Harness) -> None:
    """``--live-model``: replace the scripted models by the vendor adapters of ``config/models.yaml``.

    # ASSUMPTION: DESIGN §7 fixes no API for the swap. The most natural reading of §3.21/§3.9 is
    # that ``h.fakes.models`` is the very ``Mapping[ModelRole, ModelPort]`` the ``ModelRouter`` was
    # built on, so replacing its entries per role is the swap; API keys come from
    # ``NOUR_<VENDOR>_API_KEY`` in the environment and a missing key skips rather than fails.
    """
    from nour.core.ports import ModelRole

    models = h.cfg.models
    endpoints = {
        ModelRole.PRIMARY: models.primary,
        ModelRole.FALLBACK: models.fallback,
        ModelRole.CRITIC: models.critic,
        ModelRole.AUDITOR: models.auditor,
    }
    for role, endpoint in endpoints.items():
        adapter = _VENDOR_ADAPTERS.get(endpoint.vendor)
        if adapter is None:
            pytest.skip(f"--live-model: no vendor adapter for {endpoint.vendor!r} ({role})")
        api_key = os.environ.get(f"NOUR_{endpoint.vendor.upper()}_API_KEY")
        if not api_key:
            pytest.skip(f"--live-model: NOUR_{endpoint.vendor.upper()}_API_KEY is not set")
        module_name, class_name = adapter
        adapter_cls = getattr(importlib.import_module(module_name), class_name)
        h.fakes.models[role] = adapter_cls(api_key=api_key.encode(), model=endpoint.model)


@pytest.fixture
def harness(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[HarnessFactory]:
    """Factory fixture: ``h = harness(policy=..., dry_run=..., ...)``.

    Each call builds a fresh ``Harness`` in its own sub-directory of ``tmp_path`` (one SQLite file
    per harness); every harness built is closed on teardown, newest first (FakeClock nests
    freezegun, so closing order matters).
    """
    built: list[Harness] = []

    def build(**kw: Any) -> Harness:
        sub = tmp_path / f"harness-{len(built)}"
        sub.mkdir()
        h = make_harness(sub, **kw)
        built.append(h)
        if request.config.stash.get(LIVE_MODEL, False):
            _swap_in_live_models(h)
        return h

    yield build
    for h in reversed(built):
        h.close()


# --------------------------------------------------------------------------- inspection helpers


def db_rows(h: Harness, model: type[Any], **where: Any) -> list[Any]:
    """Every row of ORM ``model`` whose columns equal ``where``, in id order (ULID = time order).

    # ASSUMPTION: inspection opens a plain read-only engine on the harness's SQLite file
    # (``h.settings.database_url``) and a guard-free ``Session`` on purpose: the gate reads
    # governance-only (``timer_slot``), auditor-only (``auditor_report``) and both desks' partitions
    # of DESK_ROW tables in one query, which no desk-token ``SessionFactory`` may do. Nothing is
    # ever written through it.
    """
    from nour.db.engine import make_engine

    engine = make_engine(h.settings.database_url, read_only=True)
    try:
        with Session(engine) as session:
            stmt = select(model)
            for column, value in where.items():
                stmt = stmt.where(getattr(model, column) == value)
            found = list(session.execute(stmt.order_by(model.id)).scalars())
            session.expunge_all()
            return found
    finally:
        engine.dispose()


def closed_rows(h: Harness, event_id: Any, *, exclude: frozenset[str] = frozenset()) -> list[Any]:
    """The ``closed`` audit rows Nour's own proposals produced for ``event_id``.

    Rows with another actor (owner, system, auditor) and rows whose ``action`` is in ``exclude``
    (typically ``OWNER_FACING_ACTIONS``) are left out, so what remains is one row per proposal the
    gate dispatched for the event (DESIGN §4h: exactly one ``closed`` row per dispatch).
    """
    return [
        row
        for row in h.audit_rows(event_id=event_id, phase="closed")
        if row.actor == Actor.NOUR and row.action not in exclude
    ]


@pytest.fixture(scope="session")
def rows() -> RowsFn:
    """``rows(h, Model, column=value, ...)`` → ORM rows (see :func:`db_rows`)."""
    return db_rows


@pytest.fixture(scope="session")
def proposal_rows() -> ClosedRowsFn:
    """``proposal_rows(h, event_id, exclude=...)`` → Nour's closed audit rows (see :func:`closed_rows`)."""
    return closed_rows
