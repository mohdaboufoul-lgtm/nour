"""Config loader (DESIGN §3.6 ``nour/config/loader.py``; SPEC §2 §18).

``load_config(config_dir, prompts_dir)`` reads every file under ``config/`` and ``prompts/``,
validates each against ``nour.config.schema`` and then cross-validates the set. It never stops at
the first problem: every violation is collected and raised as one ``ConfigError`` whose
``violations`` list names the file, the key and the rule, so the owner fixes a whole file at once
(SPEC §18: the config files are the owner's rails). What is checked:

* shape of every file (unknown keys, wrong types, missing tiers, bad weekdays …);
* every ``ActionCategory`` named anywhere (permissions, spend_tiers.always_K, calendar emergency
  categories, coat approval rules) exists in ``capabilities.yaml``;
* coat ``desks_allowed`` ⊆ ``Desk``, ``slug`` == file stem, unique slugs;
* ``models.yaml`` vendor separation: fallback and auditor on a different vendor from primary
  (SPEC §4 §12 §17);
* coat ``tone_guide_ref`` / ``knowledge_pack_ref`` resolve to files under ``config/``;
* the constitution's recorded hash (latest change-log row) equals the hash of the file as it is
  (SPEC §2 amendment process: an edit without a change-log entry refuses to start);
* every prompt template renders under ``StrictUndefined`` with the documented context.

Canonical hashed region of the constitution
-------------------------------------------
``constitution_hash(text)`` normalises line endings to ``"\\n"`` and hashes, as UTF-8, everything
in ``config/constitution.md`` *before* the first line that reads exactly ``## Change log``
(after stripping). The heading line itself and the table beneath it are excluded, so writing the
hash into the table does not change the hash. The value is recorded as ``sha256:<hex>`` in the
"Constitution hash" column of the change log; the latest row must match the file. Any edit above
the heading therefore needs a new dated row carrying the new hash (``constitution_hash`` is the
function the CLI uses to print it).

Prompt templates
----------------
Every ``*.md`` in ``prompts/`` except ``README.md`` is a template (keyed by file name in
``NourConfig.prompts``). Templates are checked with a ``SandboxedEnvironment(undefined=
StrictUndefined)`` — the same environment ``PromptAssembler`` uses — against
``PROMPT_CONTEXT_VARIABLES``, the union of the documented inputs of the system, auditor and
critic prompts, with the loaded config objects standing in for ``coat``, ``permissions``,
``spend_tiers``, ``constitution``, ``calendar`` and ``mandate`` so a misspelt attribute fails
too. The literal bank placeholders ``{{bank.<coat>.<field>}}`` (prompts/README.md: filled by the
document renderer, not by Jinja) are wrapped in ``{% raw %}`` before parsing by
``protect_bank_placeholders``; the assembler must apply the same protection.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

import yaml
from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError

from nour.config.schema import (
    CalendarConfig,
    CapabilitiesConfig,
    ChangeLogEntry,
    ChannelsConfig,
    CoatConfig,
    ConfigModel,
    Constitution,
    DeputyConfig,
    ModelsConfig,
    NourConfig,
    PermissionsConfig,
    SpendTiersConfig,
)
from nour.core.errors import ConfigError
from nour.core.hashing import content_hash, sha256_hex
from nour.core.types import ActionCategory, CoatId, Desk, Hash

CHANGELOG_HEADING = "## Change log"
HARD_RULES_HEADING = "## Hard rules"
KILL_SWITCH_HEADING = "## Kill switch"
HASH_COLUMN = "constitution hash"

PROMPT_FILES_IGNORED: frozenset[str] = frozenset({"README.md"})

PROMPT_CONTEXT_VARIABLES: frozenset[str] = frozenset(
    {
        # prompts/nour.system.md (SPEC §18 skeleton)
        "coat",
        "desk",
        "today",
        "constitution",
        "permissions",
        "spend_tiers",
        "persona",
        # prompts/auditor.system.md
        "audit_day",
        "audit_events",
        "spend_expectation",
        "coats",
        "approvals",
        "known_counterparts",
        "previous_findings",
        "calendar",
        # prompts/critic.system.md
        "draft",
        "mandate",
        "counterpart",
        "tone_guide",
        "knowledge_pack",
        "observed_content",
        "disclosure_requested",
        "approval_id",
        "tier",
        "owner_name_send",
    }
)
"""The only names a prompt template may reference (documented inputs of the three prompts)."""

_BANK_PLACEHOLDER = re.compile(r"\{\{\s*bank\.[^{}]*\}\}")
_T = TypeVar("_T")
_M = TypeVar("_M", bound=ConfigModel)


# ---------------------------------------------------------------------------- constitution


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def hashed_region(text: str) -> str:
    """The canonical region: everything before the ``## Change log`` heading line."""
    text = _normalise_newlines(text)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == CHANGELOG_HEADING:
            return "\n".join(lines[:index])
    return text


def constitution_hash(text: str) -> Hash:
    """``sha256:<hex>`` over :func:`hashed_region` (see the module docstring)."""
    return Hash("sha256:" + sha256_hex(hashed_region(text).encode("utf-8")))


def _section(text: str, heading: str) -> str:
    """The body under ``heading`` up to the next ``## `` heading (stripped); '' when absent."""
    lines = _normalise_newlines(text).split("\n")
    body: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == heading:
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            body.append(line)
    return "\n".join(body).strip()


def _table_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(set(cell) <= set("-: ") for cell in cells):
            continue  # the |---|---| separator
        rows.append(cells)
    return rows


def parse_changelog(text: str) -> list[ChangeLogEntry]:
    """Rows of the ``## Change log`` table: Date | Change | Confirmed by | Constitution hash.

    The hash column is optional in the header (older tables); without it every entry has
    ``constitution_hash=None`` and the loader reports the latest entry as unhashed.
    """
    rows = _table_rows(_section(text, CHANGELOG_HEADING))
    if not rows:
        return []
    header = [cell.lower() for cell in rows[0]]
    try:
        date_col = header.index("date")
        change_col = header.index("change")
        by_col = header.index("confirmed by")
    except ValueError as exc:
        raise ValueError(
            "change log header must be | Date | Change | Confirmed by | [Constitution hash] |"
        ) from exc
    hash_col = header.index(HASH_COLUMN) if HASH_COLUMN in header else None
    entries: list[ChangeLogEntry] = []
    for cells in rows[1:]:
        if len(cells) <= max(date_col, change_col, by_col):
            raise ValueError(f"change log row has too few columns: {cells}")
        recorded: str | None = None
        if hash_col is not None and len(cells) > hash_col and cells[hash_col]:
            recorded = cells[hash_col]
        entries.append(
            ChangeLogEntry(
                date=dt.date.fromisoformat(cells[date_col]),
                change=cells[change_col],
                confirmed_by=cells[by_col],
                constitution_hash=Hash(recorded) if recorded else None,
            )
        )
    return entries


def parse_constitution(text: str) -> Constitution:
    """Build the :class:`Constitution` from the markdown (no hash check; the loader does that)."""
    hard_rules = _section(text, HARD_RULES_HEADING)
    if not hard_rules:
        raise ValueError(f"constitution has no {HARD_RULES_HEADING!r} section")
    return Constitution(
        text=_normalise_newlines(text),
        hard_rules=hard_rules,
        changelog=parse_changelog(text),
        sha256=constitution_hash(text),
        kill_section=_section(text, KILL_SWITCH_HEADING),
    )


def constitution_violations(constitution: Constitution) -> list[str]:
    """§2 amendment process: the latest change-log row must carry the hash of the file as it is."""
    latest = constitution.latest_entry
    if latest is None:
        return ["constitution.md: the change log has no entries"]
    if latest.constitution_hash is None:
        return [
            "constitution.md: the latest change-log entry records no constitution hash "
            f"(expected {constitution.sha256})"
        ]
    if latest.constitution_hash != constitution.sha256:
        return [
            "constitution.md: constitution hash is stale; the file hashes to "
            f"{constitution.sha256} but the latest change-log entry ({latest.date}) records "
            f"{latest.constitution_hash}; add a dated change-log entry with the new hash"
        ]
    if not constitution.kill_phrases():
        return [f"constitution.md: {KILL_SWITCH_HEADING!r} lists no kill phrases"]
    return []


# ---------------------------------------------------------------------------- prompts


def protect_bank_placeholders(template: str) -> str:
    """Wrap every literal ``{{bank.<coat>.<field>}}`` in ``{% raw %}`` so Jinja leaves it alone."""
    return _BANK_PLACEHOLDER.sub(lambda m: "{% raw %}" + m.group(0) + "{% endraw %}", template)


def check_prompt_templates(prompts: Mapping[str, str], context: Mapping[str, Any]) -> list[str]:
    """Render each template with ``StrictUndefined``; return one violation per failing file."""
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    violations: list[str] = []
    for name in sorted(prompts):
        try:
            env.from_string(protect_bank_placeholders(prompts[name])).render(**context)
        except TemplateError as exc:
            violations.append(f"prompts/{name}: {type(exc).__name__}: {exc}")
    return violations


class _Placeholder:
    """Stands in for a config object that failed to load, so the template check still runs."""

    def __getattr__(self, name: str) -> _Placeholder:
        if name.startswith("_"):
            raise AttributeError(name)
        return self

    def __str__(self) -> str:
        return "<placeholder>"

    def __iter__(self) -> Any:
        return iter(())


def prompt_check_context(
    *,
    coat: CoatConfig | None,
    constitution: Constitution | None,
    permissions: PermissionsConfig | None,
    spend_tiers: SpendTiersConfig | None,
    calendar: CalendarConfig | None,
    persona: str,
) -> dict[str, Any]:
    """The dummy render context: real objects where loaded, placeholders otherwise, strings and
    flags for the per-call inputs. Keys are exactly ``PROMPT_CONTEXT_VARIABLES``."""
    stand_in = _Placeholder()
    context: dict[str, Any] = {
        "coat": coat if coat is not None else stand_in,
        "desk": Desk.ASSISTANT.value,
        "today": dt.date(2026, 10, 5).isoformat(),
        "constitution": constitution if constitution is not None else stand_in,
        "permissions": permissions if permissions is not None else stand_in,
        "spend_tiers": spend_tiers if spend_tiers is not None else stand_in,
        "persona": persona,
        "audit_day": dt.date(2026, 10, 5).isoformat(),
        "audit_events": "",
        "spend_expectation": "",
        "coats": "",
        "approvals": "",
        "known_counterparts": "",
        "previous_findings": "",
        "calendar": calendar if calendar is not None else stand_in,
        "draft": "",
        "mandate": coat.mandate if coat is not None else stand_in,
        "counterpart": "",
        "tone_guide": "",
        "knowledge_pack": "",
        "observed_content": "",
        "disclosure_requested": False,
        "approval_id": "",
        "tier": "A",
        "owner_name_send": False,
    }
    assert set(context) == PROMPT_CONTEXT_VARIABLES
    return context


# ---------------------------------------------------------------------------- collecting loader


def _flatten(label: str, exc: ValidationError) -> list[str]:
    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        msg = str(err["msg"])
        out.append(f"{label}: {loc or '<root>'}: {msg}")
    return out


class _Collector:
    """Accumulates violations; ``attempt`` runs a loader step and records what it raises."""

    def __init__(self) -> None:
        self.violations: list[str] = []

    def attempt(self, label: str, step: Callable[[], _T]) -> _T | None:
        try:
            return step()
        except ValidationError as exc:
            self.violations.extend(_flatten(label, exc))
        except FileNotFoundError as exc:
            self.violations.append(f"{label}: file not found ({exc.filename})")
        except yaml.YAMLError as exc:
            self.violations.append(f"{label}: invalid yaml: {exc}")
        except (ValueError, TypeError, OSError) as exc:
            self.violations.append(f"{label}: {exc}")
        return None

    def add(self, *messages: str) -> None:
        self.violations.extend(messages)


def read_yaml(path: Path) -> dict[str, Any]:
    """``yaml.safe_load`` that insists on a mapping at the top level."""
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be a mapping")
    return data


def _load_coat(
    path: Path, config_dir: Path, collector: _Collector
) -> tuple[CoatConfig | None, list[str]]:
    """One coat yaml plus its tone/knowledge files; returns the coat (or None) and ref errors."""
    label = f"coats/{path.name}"
    raw = collector.attempt(label, lambda: read_yaml(path))
    if raw is None:
        return None, []
    ref_errors: list[str] = []
    contents: dict[str, str] = {}
    for key in ("tone_guide_ref", "knowledge_pack_ref"):
        ref = raw.get(key)
        field = key.removesuffix("_ref")
        if not isinstance(ref, str) or not ref:
            contents[field] = ""
            continue  # the shape error is reported by the model validation below
        target = (config_dir / ref).resolve()
        if not target.is_file():
            ref_errors.append(f"{label}: {key} {ref!r} does not exist under {config_dir}")
            contents[field] = ""
        else:
            contents[field] = target.read_text(encoding="utf-8")
    coat = collector.attempt(label, lambda: CoatConfig.model_validate({**raw, **contents}))
    if coat is not None and coat.slug != path.stem:
        ref_errors.append(f"{label}: slug {coat.slug!r} must equal the file name {path.stem!r}")
    return coat, ref_errors


def _unknown_categories(
    label: str, names: frozenset[ActionCategory], known: frozenset[ActionCategory]
) -> list[str]:
    unknown = sorted(set(names) - set(known))
    return [f"{label}: unknown category {name!r} (not in capabilities.yaml)" for name in unknown]


def load_config(config_dir: Path, prompts_dir: Path) -> NourConfig:
    """§18. Raises ConfigError listing EVERY violation: unknown category names, coat desk not in Desk, vendor
    clashes (§4), coat refs that do not exist, a constitution whose sha256 is not the one recorded by the latest
    change-log entry (§2 amendment process: refuse to start), templates with undefined variables."""
    config_dir = Path(config_dir)
    prompts_dir = Path(prompts_dir)
    collector = _Collector()

    def yaml_model(name: str, model: type[_M]) -> _M | None:
        def step() -> _M:
            return model.model_validate(read_yaml(config_dir / name))

        return collector.attempt(name, step)

    permissions = yaml_model("permissions.yaml", PermissionsConfig)
    spend_tiers = yaml_model("spend_tiers.yaml", SpendTiersConfig)
    calendar = yaml_model("calendar.yaml", CalendarConfig)
    channels = yaml_model("channels.yaml", ChannelsConfig)
    deputy = yaml_model("deputy.yaml", DeputyConfig)
    models = yaml_model("models.yaml", ModelsConfig)
    capabilities = yaml_model("capabilities.yaml", CapabilitiesConfig)

    coats: dict[CoatId, CoatConfig] = {}
    coats_dir = config_dir / "coats"
    coat_files = sorted(coats_dir.glob("*.yaml")) if coats_dir.is_dir() else []
    if not coat_files:
        collector.add(f"coats/: no coat files found under {coats_dir}")
    for path in coat_files:
        coat, ref_errors = _load_coat(path, config_dir, collector)
        collector.add(*ref_errors)
        if coat is None:
            continue
        if coat.slug in coats:
            collector.add(f"coats/{path.name}: duplicate coat slug {coat.slug!r}")
            continue
        coats[coat.slug] = coat

    constitution = collector.attempt(
        "constitution.md",
        lambda: parse_constitution((config_dir / "constitution.md").read_text(encoding="utf-8")),
    )
    if constitution is not None:
        collector.add(*constitution_violations(constitution))

    persona = collector.attempt(
        "persona.md", lambda: (config_dir / "persona.md").read_text(encoding="utf-8")
    )
    if persona is not None and not persona.strip():
        collector.add("persona.md: is empty")

    prompts: dict[str, str] = {}
    if not prompts_dir.is_dir():
        collector.add(f"prompts/: directory {prompts_dir} does not exist")
    else:
        for path in sorted(prompts_dir.glob("*.md")):
            if path.name in PROMPT_FILES_IGNORED:
                continue
            text = collector.attempt(
                f"prompts/{path.name}", functools.partial(path.read_text, encoding="utf-8")
            )
            if text is not None:
                prompts[path.name] = text
        if not prompts:
            collector.add(f"prompts/: no prompt templates (*.md) found under {prompts_dir}")

    # --- cross-validation (only over the parts that loaded)
    if capabilities is not None:
        known = capabilities.categories()
        if permissions is not None:
            collector.add(
                *_unknown_categories("permissions.yaml", permissions.referenced_categories(), known)
            )
        if spend_tiers is not None:
            collector.add(
                *_unknown_categories(
                    "spend_tiers.yaml always_K", spend_tiers.referenced_categories(), known
                )
            )
        if calendar is not None:
            collector.add(
                *_unknown_categories(
                    "calendar.yaml quiet_hours.emergency_categories",
                    calendar.referenced_categories(),
                    known,
                )
            )
        for slug, coat in coats.items():
            collector.add(
                *_unknown_categories(
                    f"coats/{slug}.yaml approval_rules",
                    coat.approval_rules.referenced_categories(),
                    known,
                )
            )

    first_coat = coats[sorted(coats)[0]] if coats else None
    collector.add(
        *check_prompt_templates(
            prompts,
            prompt_check_context(
                coat=first_coat,
                constitution=constitution,
                permissions=permissions,
                spend_tiers=spend_tiers,
                calendar=calendar,
                persona=persona or "",
            ),
        )
    )

    if collector.violations:
        raise ConfigError(collector.violations)

    assert permissions and spend_tiers and channels and calendar and deputy
    assert models and capabilities and constitution and persona is not None
    draft = NourConfig(
        constitution=constitution,
        persona=persona,
        coats=coats,
        permissions=permissions,
        spend_tiers=spend_tiers,
        channels=channels,
        calendar=calendar,
        deputy=deputy,
        models=models,
        capabilities=capabilities,
        prompts=prompts,
        config_hash=NourConfig.HASH_PLACEHOLDER,
    )
    return draft.model_copy(update={"config_hash": compute_config_hash(draft)})


def compute_config_hash(cfg: NourConfig) -> Hash:
    """``sha256:<hex>`` over the canonical JSON of the whole config except ``config_hash`` itself;
    logged on every audit row (``audit_event.config_hash``) so a replay knows which rails applied."""
    return content_hash(cfg.model_dump(mode="python", exclude={"config_hash"}))
