"""``config/moona.yaml`` and ``prompts/moona/system.md`` as frozen models (docs/MOONA.md §4;
SPEC §18 in the house style: every number that rules him is owner-editable data, never a value in
code or in his judgement).

``load_moona(config_dir, prompts_dir)`` reads the two files, validates them and cross-checks the
template, collecting every violation into one ``ConfigError`` the way ``nour.config.loader``
does. They are read by this loader and not by ``load_config`` on purpose: ``NourConfig``, its
prompt set and the spend tiers are closed sets the phase-0 tests pin, and Moona is a sub-agent
with his own rails (``nour/moona/__init__.py``).

Amounts are written in the wallet currency as whole units (``50``) or decimal strings
(``"1.00"``), never floats, exactly like ``spend_tiers.yaml``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError, field_validator, model_validator

from nour.config.loader import read_yaml
from nour.config.schema import ConfigModel
from nour.core.errors import ConfigError
from nour.core.hashing import content_hash
from nour.core.types import Hash, Money

CONFIG_FILE = "moona.yaml"
PROMPT_FILE = Path("moona") / "system.md"
"""``prompts/moona/system.md``: a sub-directory, so the desk loader's ``prompts/*.md`` glob never
sees it (that set is pinned) and Moona renders it with his own sandbox."""

PROMPT_VARIABLES: frozenset[str] = frozenset({"moona", "tools", "today"})
"""The only names the template may reference; ``today`` is rendered last (the cache prefix)."""

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MONEY_FIELDS = ("seed", "death_floor", "upkeep_per_day")


def _to_money(value: Any, currency: str, label: str) -> Any:
    """``50`` / ``"1.00"`` / ``Decimal`` → ``{"fils": …, "currency": currency}``; Money and dicts
    pass through; a float is refused (it cannot carry an amount exactly)."""
    if value is None or isinstance(value, Money | dict):
        return value
    if isinstance(value, bool | float):
        raise ValueError(f"{label}: amounts are whole units or a decimal string, got {value!r}")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label}: not a money amount: {value!r}") from exc
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ValueError(f"{label}: {value!r} is finer than one minor unit")
    return {"fils": int(minor), "currency": currency}


class _BadAmount:
    """A yaml amount ``_to_money`` refused, carried to the field so pydantic reports it there."""

    def __init__(self, message: str) -> None:
        self.message = message


def _money_or_marker(value: Any, currency: str, label: str) -> Any:
    try:
        return _to_money(value, currency, label)
    except ValueError as exc:
        return _BadAmount(str(exc))


def _refuse_marker(value: Any) -> Any:
    if isinstance(value, _BadAmount):
        raise ValueError(value.message)
    return value


class MoonaModel(ConfigModel):
    """Which model he thinks with and the one way a live model is attached."""

    role: Literal["primary", "fallback"] = "primary"
    max_tokens: int = 1024
    adapter: str | None = None
    """``"package.module:ClassName"`` of a live ``ModelPort`` (constructed with no arguments);
    ``null`` means simulation only (``nour.fakes.model.ScriptedModel``). Resolved by name at run
    time so ``nour.moona`` never imports a later wave (DESIGN §8)."""

    @field_validator("max_tokens")
    @classmethod
    def _tokens(cls, value: int) -> int:
        if value < 64:
            raise ValueError("max_tokens must be at least 64")
        return value


class MoonaRails(ConfigModel):
    """The live marketplace and payment rail, by dotted name; ``null`` = fakes only."""

    marketplace_adapter: str | None = None
    payments_adapter: str | None = None


class MoonaLimits(ConfigModel):
    """The structural limits on what he may do with his own money."""

    max_single_spend_fraction: Decimal
    """No single purchase above this fraction of the balance (``0.5``: never bet more than half
    his life on one thing); refused by the wallet, not by the prompt."""
    notify_spend_fraction: Decimal
    """A purchase above this fraction of the balance is flagged to the owner in the journal."""
    max_rest_hours: int
    tick_minutes: int
    max_tool_calls_per_tick: int
    low_balance_warning_fraction: Decimal
    """Below this fraction of the seed the prompt says so in plain words."""

    @model_validator(mode="after")
    def _ranges(self) -> MoonaLimits:
        for name in ("max_single_spend_fraction", "notify_spend_fraction"):
            value = getattr(self, name)
            if not Decimal("0") < value <= Decimal("1"):
                raise ValueError(f"{name} must be in (0, 1]")
        if not Decimal("0") < self.low_balance_warning_fraction < Decimal("1"):
            raise ValueError("low_balance_warning_fraction must be in (0, 1)")
        if self.max_rest_hours < 1 or self.tick_minutes < 1 or self.max_tool_calls_per_tick < 1:
            raise ValueError("max_rest_hours, tick_minutes and max_tool_calls_per_tick are >= 1")
        return self


class MoonaRules(ConfigModel):
    """Constitution hard rules 5 and 6 restated for him and enforced in code (docs/MOONA.md §5)."""

    hard_rules: list[str]
    disclosure: str
    """Appended to every outbound message (SPEC §3 disclosure, in his own name by owner decision)."""
    blocked_merchant_patterns: list[str]
    """Case-insensitive regexes over the merchant of a spend; a match is refused before any port."""
    never_claim_patterns: list[str]
    """Case-insensitive regexes over outbound text; a match (a claim to be human) is refused."""

    @field_validator("hard_rules")
    @classmethod
    def _rules(cls, value: list[str]) -> list[str]:
        if not value or any(not rule.strip() for rule in value):
            raise ValueError("hard_rules lists at least one non-empty rule")
        return value

    @field_validator("disclosure")
    @classmethod
    def _disclosure(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("disclosure must not be empty")
        return value.strip()

    @field_validator("blocked_merchant_patterns", "never_claim_patterns")
    @classmethod
    def _compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return value

    def blocked_merchant(self, merchant: str) -> str | None:
        """The pattern that blocks ``merchant``, or ``None``."""
        for pattern in self.blocked_merchant_patterns:
            if re.search(pattern, merchant, re.IGNORECASE):
                return pattern
        return None

    def dishonest_claim(self, text: str) -> str | None:
        """The pattern an outbound ``text`` trips, or ``None``."""
        for pattern in self.never_claim_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return pattern
        return None


class MoonaSimulation(ConfigModel):
    """The fake economy ``moona simulate`` runs (``nour.moona.fakes``): owner-editable so the
    rehearsal is as hard as the owner wants it."""

    input_fils_per_1k_tokens: int
    output_fils_per_1k_tokens: int
    requests_per_day: int
    budget_min: Money
    budget_max: Money
    never_pay_fraction: Decimal
    pay_delay_hours_min: int
    pay_delay_hours_max: int

    @field_validator("budget_min", "budget_max", mode="before")
    @classmethod
    def _amount_marker(cls, value: Any) -> Any:
        return _refuse_marker(value)

    @model_validator(mode="after")
    def _ranges(self) -> MoonaSimulation:
        if self.input_fils_per_1k_tokens < 0 or self.output_fils_per_1k_tokens < 0:
            raise ValueError("token rates are not negative")
        if self.requests_per_day < 0:
            raise ValueError("requests_per_day is not negative")
        if self.budget_min.currency != self.budget_max.currency:
            raise ValueError("budget_min and budget_max share a currency")
        if not 0 < self.budget_min.fils <= self.budget_max.fils:
            raise ValueError("0 < budget_min <= budget_max")
        if not Decimal("0") <= self.never_pay_fraction <= Decimal("1"):
            raise ValueError("never_pay_fraction is in [0, 1]")
        if not 0 <= self.pay_delay_hours_min <= self.pay_delay_hours_max:
            raise ValueError("0 <= pay_delay_hours_min <= pay_delay_hours_max")
        return self


class MoonaConfig(ConfigModel):
    """Everything that rules Moona (docs/MOONA.md §4). ``system_template`` is the text of
    ``prompts/moona/system.md`` so ``config_hash`` covers the prompt as ``NourConfig`` does."""

    name: str
    currency: str
    seed: Money
    death_floor: Money
    upkeep_per_day: Money
    fx_aed_per_unit: Decimal
    model: MoonaModel
    rails: MoonaRails
    limits: MoonaLimits
    rules: MoonaRules
    persona: str
    simulation: MoonaSimulation
    system_template: str

    @model_validator(mode="before")
    @classmethod
    def _amounts_to_money(cls, data: Any) -> Any:
        """Convert every amount; a bad one is left as it is (pydantic then reports it on its
        own field, next to every other violation, instead of this validator stopping the rest)."""
        if not isinstance(data, dict):
            return data
        currency = data.get("currency")
        if not isinstance(currency, str):
            return data
        out = dict(data)
        for name in _MONEY_FIELDS:
            if name in out:
                out[name] = _money_or_marker(out[name], currency, name)
        simulation = out.get("simulation")
        if isinstance(simulation, dict):
            simulation = dict(simulation)
            for name in ("budget_min", "budget_max"):
                if name in simulation:
                    simulation[name] = _money_or_marker(
                        simulation[name], currency, f"simulation.{name}"
                    )
            out["simulation"] = simulation
        return out

    @field_validator("seed", "death_floor", "upkeep_per_day", mode="before")
    @classmethod
    def _amount_marker(cls, value: Any) -> Any:
        return _refuse_marker(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty")
        return value.strip()

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        if not _CURRENCY_RE.match(value):
            raise ValueError("currency is a 3-letter upper-case ISO code")
        return value

    @field_validator("persona", "system_template")
    @classmethod
    def _text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> MoonaConfig:
        for name in _MONEY_FIELDS:
            amount: Money = getattr(self, name)
            if amount.currency != self.currency:
                raise ValueError(f"{name} is not in {self.currency}")
        for name in ("budget_min", "budget_max"):
            if getattr(self.simulation, name).currency != self.currency:
                raise ValueError(f"simulation.{name} is not in {self.currency}")
        if self.seed.fils <= 0:
            raise ValueError("seed must be positive: a wallet starts with money or not at all")
        if self.death_floor.fils < 0:
            raise ValueError("death_floor is not negative")
        if self.death_floor >= self.seed:
            raise ValueError("death_floor must be below the seed")
        if self.upkeep_per_day.fils < 0:
            raise ValueError("upkeep_per_day is not negative")
        if self.fx_aed_per_unit <= 0:
            raise ValueError("fx_aed_per_unit must be positive")
        if self.currency == "AED" and self.fx_aed_per_unit != 1:
            raise ValueError("fx_aed_per_unit is 1 for an AED wallet")
        return self

    @property
    def config_hash(self) -> Hash:
        """``sha256:<hex>`` over the whole config, template included; on every journal row."""
        return content_hash(self.model_dump(mode="python"))


# ---------------------------------------------------------------------------- loading


def _flatten(label: str, exc: ValidationError) -> list[str]:
    return [
        f"{label}: {'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()
    ]


def check_template(template: str) -> list[str]:
    """Render the template once under ``StrictUndefined`` with the documented names only."""
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    context = {"moona": _Placeholder(), "tools": "(tools)", "today": "2026-10-05"}
    try:
        env.from_string(template).render(**context)
    except TemplateError as exc:
        return [f"prompts/{PROMPT_FILE.as_posix()}: {type(exc).__name__}: {exc}"]
    return []


class _Placeholder:
    """Stands in for the config object in the template check (any attribute, any depth)."""

    def __getattr__(self, name: str) -> _Placeholder:
        if name.startswith("_"):
            raise AttributeError(name)
        return self

    def __str__(self) -> str:
        return "<placeholder>"

    def __iter__(self) -> Any:
        return iter(())


def load_moona(config_dir: Path, prompts_dir: Path) -> MoonaConfig:
    """Read ``config/moona.yaml`` and ``prompts/moona/system.md``; raise ``ConfigError`` listing
    every violation at once (shape, ranges, currencies, an undefined template variable)."""
    config_dir = Path(config_dir)
    prompts_dir = Path(prompts_dir)
    violations: list[str] = []
    raw: dict[str, Any] | None = None
    config_path = config_dir / CONFIG_FILE
    try:
        raw = read_yaml(config_path)
    except FileNotFoundError:
        violations.append(f"{CONFIG_FILE}: file not found ({config_path})")
    except (ValueError, OSError) as exc:
        violations.append(f"{CONFIG_FILE}: {exc}")
    template: str | None = None
    prompt_path = prompts_dir / PROMPT_FILE
    try:
        template = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        violations.append(f"prompts/{PROMPT_FILE.as_posix()}: file not found ({prompt_path})")
    except OSError as exc:
        violations.append(f"prompts/{PROMPT_FILE.as_posix()}: {exc}")
    if template is not None:
        violations.extend(check_template(template))
    cfg: MoonaConfig | None = None
    if raw is not None and template is not None:
        if "system_template" in raw:
            violations.append(f"{CONFIG_FILE}: system_template is read from the prompt file")
        else:
            try:
                cfg = MoonaConfig.model_validate({**raw, "system_template": template})
            except ValidationError as exc:
                violations.extend(_flatten(CONFIG_FILE, exc))
    if violations:
        raise ConfigError(violations)
    assert cfg is not None
    return cfg
