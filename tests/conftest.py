"""Shared fixtures (DESIGN §8 "Wave 0" tests/conftest.py): clock, idgen, cfg, settings, tmp_db,
engine, session_factory(token), tokens, leakguard.

``mint(`` is called here and nowhere else under ``tests/`` (tests/unit/test_walls.py): every test
takes its tokens from the ``tokens`` fixture. The clock fixture is the controllable Monday
2026-10-05 07:00 Asia/Dubai clock of DESIGN §7 and also feeds the ORM defaults
(``set_process_clock``), restoring the previous process clock on teardown.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.engine import Engine

from nour.config.loader import load_config
from nour.config.schema import NourConfig
from nour.config.settings import Settings
from nour.core.clock import DUBAI, FakeClock, IdGenerator, process_clock, set_process_clock
from nour.core.tokens import AnyToken, AuditorToken, mint
from nour.db.base import Base
from nour.db.engine import create_schema, make_engine
from nour.db.session import SessionFactory

if TYPE_CHECKING:
    from nour.core.leakguard import LeakGuard

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
PROMPTS_DIR = REPO_ROOT / "prompts"

START = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)
"""Monday 2026-10-05 07:00 Asia/Dubai (DESIGN §7: the 48-hour run crosses the Monday review)."""

LEAKGUARD_TEST_KEY = b"test-key" * 4


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    """FakeClock at START, driving freezegun and the ORM defaults; closed on teardown."""
    previous = process_clock()
    with FakeClock(START) as fake:
        set_process_clock(fake)
        try:
            yield fake
        finally:
            set_process_clock(previous)


@pytest.fixture
def idgen(clock: FakeClock) -> IdGenerator:
    """Seeded (0) so ids are reproducible across runs (DESIGN §3.2)."""
    return IdGenerator(clock, seed=0)


@pytest.fixture(scope="session")
def cfg() -> NourConfig:
    """The real repository config and prompts, loaded once per test session."""
    return load_config(CONFIG_DIR, PROMPTS_DIR)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """A per-test SQLite file (the auditor needs ``mode=ro``, so never ``:memory:``)."""
    return tmp_path / "nour-test.sqlite3"


@pytest.fixture
def settings(tmp_db: Path) -> Settings:
    """Hermetic settings: explicit values win over whatever NOUR_* the environment carries."""
    return Settings(
        env="test",
        database_url=f"sqlite+pysqlite:///{tmp_db}",
        config_dir=CONFIG_DIR,
        prompts_dir=PROMPTS_DIR,
        dry_run=True,
        secrets_backend="fake",
    )


@pytest.fixture
def engine(tmp_db: Path, clock: FakeClock) -> Iterator[Engine]:
    """A writer engine over ``tmp_db`` with the schema of every mapper registered on
    ``Base.metadata`` (empty until wave 1's models land; tests may create their own metadata)."""
    eng = make_engine(f"sqlite+pysqlite:///{tmp_db}")
    create_schema(eng, Base.metadata)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def tokens() -> dict[str, AnyToken]:
    """One token per process kind, minted here and nowhere else in tests."""
    return {
        "operator": mint("operator"),
        "assistant": mint("assistant"),
        "governance": mint("governance"),
        "auditor": mint("auditor"),
    }


@pytest.fixture
def session_factory(
    engine: Engine, tmp_db: Path, clock: FakeClock
) -> Iterator[Callable[[AnyToken], SessionFactory]]:
    """Factory fixture: ``session_factory(token)`` → a SessionFactory bound to that token.

    A desk token gets the writer engine. The auditor gets what DESIGN §5.3 / §7.4 give it: a
    separate read-only (``mode=ro``) engine over the same file for ``session()``, plus the writer
    engine as ``report_engine`` so ``append_report()`` can land ``auditor_report`` rows.
    """
    read_only_engines: list[Engine] = []

    def _make(token: AnyToken) -> SessionFactory:
        if isinstance(token, AuditorToken):
            ro = make_engine(f"sqlite+pysqlite:///{tmp_db}", read_only=True)
            translate = engine.get_execution_options().get("schema_translate_map")
            if translate:
                ro.update_execution_options(schema_translate_map=dict(translate))
            read_only_engines.append(ro)
            return SessionFactory(ro, token, clock, report_engine=engine)
        return SessionFactory(engine, token, clock)

    try:
        yield _make
    finally:
        for ro in read_only_engines:
            ro.dispose()


@pytest.fixture
def leakguard() -> LeakGuard:
    """``LeakGuard(hmac_key=b"test-key"*4)``; imported lazily so the config/db tests run before
    ``nour/core/leakguard.py`` lands."""
    from nour.core.leakguard import LeakGuard as _LeakGuard

    guard: Any = _LeakGuard(hmac_key=LEAKGUARD_TEST_KEY)
    return guard
