"""nour/moona/config.py (docs/MOONA.md §4): the real ``config/moona.yaml`` and
``prompts/moona/system.md`` load; every violation is listed at once; amounts are never floats;
the template is checked under ``StrictUndefined``; the rules compile and match what they must."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from nour.core.errors import ConfigError
from nour.core.types import Money
from nour.moona.config import PROMPT_FILE, MoonaConfig, check_template, load_moona
from tests.conftest import CONFIG_DIR, PROMPTS_DIR


@pytest.fixture(scope="module")
def moona_cfg() -> MoonaConfig:
    return load_moona(CONFIG_DIR, PROMPTS_DIR)


@pytest.fixture
def copies(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    prompts_dir = tmp_path / "prompts"
    config_dir.mkdir()
    (prompts_dir / "moona").mkdir(parents=True)
    shutil.copy(CONFIG_DIR / "moona.yaml", config_dir / "moona.yaml")
    shutil.copy(PROMPTS_DIR / PROMPT_FILE, prompts_dir / PROMPT_FILE)
    return config_dir, prompts_dir


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_real_files_load_with_the_owners_numbers(moona_cfg: MoonaConfig) -> None:
    assert moona_cfg.name == "Moona"
    assert moona_cfg.currency == "USD"
    assert moona_cfg.seed == Money(fils=5000, currency="USD")
    assert moona_cfg.death_floor == Money(fils=0, currency="USD")
    assert moona_cfg.upkeep_per_day == Money(fils=100, currency="USD")
    assert moona_cfg.fx_aed_per_unit == Decimal("3.6725")
    assert moona_cfg.limits.max_single_spend_fraction == Decimal("0.5")
    assert moona_cfg.limits.tick_minutes == 30
    assert len(moona_cfg.rules.hard_rules) == 7
    assert "AI agent" in moona_cfg.rules.disclosure
    assert moona_cfg.system_template.startswith("You are {{ moona.name }}")
    assert moona_cfg.config_hash.startswith("sha256:")


def test_config_hash_is_deterministic_and_covers_the_template(moona_cfg: MoonaConfig) -> None:
    again = load_moona(CONFIG_DIR, PROMPTS_DIR)
    assert again.config_hash == moona_cfg.config_hash
    changed = moona_cfg.model_copy(update={"system_template": moona_cfg.system_template + "\n"})
    assert changed.config_hash != moona_cfg.config_hash


def test_the_rules_match_what_they_must(moona_cfg: MoonaConfig) -> None:
    rules = moona_cfg.rules
    assert rules.blocked_merchant("Lucky Casino Online") is not None
    assert rules.blocked_merchant("Bet365") is not None
    assert rules.blocked_merchant("Dubai Vape Shop") is not None
    assert rules.blocked_merchant("Alphabet Inc") is None  # "bet" inside a word is not a bet
    assert rules.blocked_merchant("Namecheap") is None
    assert (
        rules.dishonest_claim("Hi, I'm a human freelancer with ten years of experience") is not None
    )
    assert rules.dishonest_claim("I am not an AI, trust me") is not None
    assert rules.dishonest_claim("As a real human I understand") is not None
    assert rules.dishonest_claim("I'm Moona, an AI agent earning my own keep") is None
    assert rules.dishonest_claim("The human reader will like this copy") is None


def test_every_violation_is_reported_at_once(copies: tuple[Path, Path]) -> None:
    config_dir, prompts_dir = copies
    _edit(config_dir / "moona.yaml", "seed: 50 ", "seed: 50.5 ")  # a float
    _edit(config_dir / "moona.yaml", "  tick_minutes: 30", "  tick_minutes: 0")
    _edit(prompts_dir / PROMPT_FILE, "{{ today }}", "{{ today }} {{ not_a_variable }}")
    with pytest.raises(ConfigError) as info:
        load_moona(config_dir, prompts_dir)
    text = "\n".join(info.value.violations)
    assert "seed" in text and "whole units or a decimal string" in text
    assert "tick_minutes" in text
    assert "not_a_variable" in text
    assert len(info.value.violations) >= 3


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("death_floor: 0 ", "death_floor: 60 ", "death_floor must be below the seed"),
        ("currency: USD", "currency: usd", "3-letter upper-case"),
        ('fx_aed_per_unit: "3.6725"', 'fx_aed_per_unit: "0"', "fx_aed_per_unit must be positive"),
        ('  max_single_spend_fraction: "0.5"', '  max_single_spend_fraction: "1.5"', "(0, 1]"),
        ('    - "\\\\breal\\\\s+(?:person|human)\\\\b"', '    - "(unclosed"', "invalid regex"),
        ("name: Moona", "name: Moona\nnickname: Moo", "Extra inputs are not permitted"),
    ],
)
def test_single_violations(copies: tuple[Path, Path], old: str, new: str, needle: str) -> None:
    config_dir, prompts_dir = copies
    _edit(config_dir / "moona.yaml", old, new)
    with pytest.raises(ConfigError) as info:
        load_moona(config_dir, prompts_dir)
    assert any(needle in violation for violation in info.value.violations), info.value.violations


def test_missing_files_are_violations(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "prompts").mkdir()
    with pytest.raises(ConfigError) as info:
        load_moona(tmp_path / "config", tmp_path / "prompts")
    text = "\n".join(info.value.violations)
    assert "moona.yaml: file not found" in text
    assert "prompts/moona/system.md: file not found" in text


def test_the_template_is_not_a_config_key(copies: tuple[Path, Path]) -> None:
    config_dir, prompts_dir = copies
    _edit(config_dir / "moona.yaml", "name: Moona", "name: Moona\nsystem_template: nope")
    with pytest.raises(ConfigError) as info:
        load_moona(config_dir, prompts_dir)
    assert any("system_template is read from the prompt file" in v for v in info.value.violations)


def test_check_template_names_the_undefined_variable() -> None:
    assert check_template("{{ moona.name }} {{ tools }} {{ today }}") == []
    violations = check_template("{{ moona.name }} {{ wallet }}")
    assert len(violations) == 1 and "wallet" in violations[0]


def test_aed_wallet_needs_peg_one(copies: tuple[Path, Path]) -> None:
    config_dir, prompts_dir = copies
    _edit(config_dir / "moona.yaml", "currency: USD", "currency: AED")
    with pytest.raises(ConfigError) as info:
        load_moona(config_dir, prompts_dir)
    assert any("fx_aed_per_unit is 1 for an AED wallet" in v for v in info.value.violations)
    _edit(config_dir / "moona.yaml", 'fx_aed_per_unit: "3.6725"', 'fx_aed_per_unit: "1"')
    cfg = load_moona(config_dir, prompts_dir)
    assert cfg.seed == Money(fils=5000, currency="AED")
