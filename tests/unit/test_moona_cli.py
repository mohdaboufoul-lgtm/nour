"""nour/moona/cli.py (docs/MOONA.md §8): birth once, status, a simulation, the owner's kill,
``run`` refused without live rails, and the read commands over the store."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nour.moona.cli import app

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv(
        "NOUR_MOONA_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'moona.sqlite3'}"
    )
    monkeypatch.setenv("NOUR_CONFIG_DIR", "config")
    monkeypatch.setenv("NOUR_PROMPTS_DIR", "prompts")
    monkeypatch.setenv("NOUR_ENV", "test")
    monkeypatch.setenv("NOUR_SECRETS_BACKEND", "fake")
    return {}


def test_birth_status_and_kill(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1 and "not born yet" in result.output
    result = runner.invoke(app, ["birth"])
    assert result.exit_code == 0, result.output
    assert "Moona is born with USD 50.00" in result.output
    result = runner.invoke(app, ["birth"])
    assert result.exit_code == 1 and "one wallet, one life" in result.output
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Moona: alive" in result.output and "balance     USD 50.00" in result.output
    result = runner.invoke(app, ["ledger"])
    assert (
        result.exit_code == 0 and "seed" in result.output and "balance USD 50.00" in result.output
    )
    result = runner.invoke(app, ["verify"])
    assert result.exit_code == 0 and "chain: ok" in result.output
    result = runner.invoke(app, ["freeze", "--reason", "Pausing him."])
    assert result.exit_code == 0 and "frozen" in result.output
    result = runner.invoke(app, ["thaw", "--reason", "Back."])
    assert result.exit_code == 0
    result = runner.invoke(app, ["kill", "--reason", "Experiment over."])
    assert result.exit_code == 0 and "dead" in result.output
    result = runner.invoke(app, ["kill", "--reason", "Again."])
    assert result.exit_code == 1 and "refused" in result.output
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0 and "dead (killed)" in result.output
    result = runner.invoke(app, ["journal"])
    assert result.exit_code == 0 and "birth" in result.output and "death" in result.output
    result = runner.invoke(app, ["inbox"])
    assert result.exit_code == 0 and "(nothing)" in result.output


def test_run_is_refused_without_live_rails(env: dict[str, str]) -> None:
    runner.invoke(app, ["birth"])
    result = runner.invoke(app, ["run", "--ticks", "1"])
    assert (
        result.exit_code == 1
        and "no live rails" in result.output
        and "docs/MOONA.md" in result.output
    )
    result = runner.invoke(app, ["run", "--ticks", "1", "--i-understand-real-money"])
    assert result.exit_code == 1 and "no live rails" in result.output


def test_simulate_reports_a_life(env: dict[str, str]) -> None:
    result = runner.invoke(
        app,
        [
            "simulate",
            "--days",
            "2",
            "--policy",
            "survivor",
            "--seed",
            "3",
            "--start",
            "2026-10-05",
            "-v",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Moona (survivor, seed 3): alive after 2 day(s) of 2" in result.output
    assert (
        "chain ok" in result.output
        and "2026-10-05" in result.output
        and "2026-10-06" in result.output
    )
    result = runner.invoke(app, ["simulate", "--days", "1", "--policy", "nope"])
    assert result.exit_code == 2 and "unknown policy" in result.output
    result = runner.invoke(app, ["simulate", "--days", "1", "--start", "yesterday"])
    assert result.exit_code == 2
    result = runner.invoke(app, ["policies"])
    assert result.exit_code == 0 and "survivor" in result.output and "thinker" in result.output


def test_simulate_leaves_no_store_behind_unless_asked(env: dict[str, str], tmp_path: Path) -> None:
    keep = tmp_path / "kept.sqlite3"
    result = runner.invoke(
        app,
        [
            "simulate",
            "--days",
            "1",
            "--policy",
            "thinker",
            "--keep-store",
            f"sqlite+pysqlite:///{keep}",
        ],
    )
    assert result.exit_code == 0, result.output
    assert keep.exists()
    assert not (tmp_path / "moona.sqlite3").exists()  # the real store was never touched
