"""nour/config (DESIGN §3.6; SPEC §2 §6 §9 §10 §12 §14 §18): schema, loader, settings.

Proves (MODULES.md "core"): ``load_config`` loads the real repo files and the assembled
``NourConfig`` exposes every key modelled; it lists every violation at once; it refuses a vendor
clash (fallback or auditor == primary) and a constitution whose hash is not the one recorded by
the latest change-log entry; ``CalendarConfig`` rules on concrete Dubai datetimes incl. the
Friday slot and a blocked date; ``SpendTiersConfig.band_for`` at 200/201/1000/1001; ``Settings``
env parsing.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from nour.config.loader import (
    PROMPT_CONTEXT_VARIABLES,
    check_prompt_templates,
    compute_config_hash,
    constitution_hash,
    hashed_region,
    load_config,
    parse_constitution,
    prompt_check_context,
    protect_bank_placeholders,
)
from nour.config.schema import (
    BlockedDate,
    CalendarConfig,
    CapabilitiesConfig,
    ModelEndpoint,
    ModelsConfig,
    NourConfig,
    SpendTiersConfig,
)
from nour.config.settings import Settings
from nour.core.errors import ConfigError, CurrencyMismatch
from nour.core.types import (
    PHASE0_CHANNELS,
    ActionTier,
    BudgetHolder,
    CoatId,
    DataTier,
    Desk,
    DeskScope,
    Money,
)

REPO = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO / "config"
PROMPTS_DIR = REPO / "prompts"
DUBAI = ZoneInfo("Asia/Dubai")


def dubai(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=DUBAI)


@pytest.fixture
def config_copy(tmp_path: Path) -> tuple[Path, Path]:
    """A writable copy of the real config/ and prompts/ trees."""
    config = tmp_path / "config"
    prompts = tmp_path / "prompts"
    shutil.copytree(CONFIG_DIR, config)
    shutil.copytree(PROMPTS_DIR, prompts)
    return config, prompts


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _violations(config: Path, prompts: Path) -> list[str]:
    with pytest.raises(ConfigError) as info:
        load_config(config, prompts)
    return info.value.violations


# --------------------------------------------------------------------------- the real files load


def test_real_config_loads_every_file(cfg: NourConfig) -> None:
    assert isinstance(cfg, NourConfig)
    assert set(cfg.coats) == {CoatId("buzz-avenue")}
    assert set(cfg.prompts) == {
        "nour.system.md",
        "auditor.system.md",
        "critic.system.md",
        "face_lock.md",
    }
    assert cfg.persona.startswith("# Persona")
    assert cfg.config_hash.startswith("sha256:") and len(cfg.config_hash) == len("sha256:") + 64
    assert cfg.config_hash == compute_config_hash(cfg)


def test_config_hash_is_deterministic_and_covers_the_content(cfg: NourConfig) -> None:
    again = load_config(CONFIG_DIR, PROMPTS_DIR)
    assert again.config_hash == cfg.config_hash
    tweaked = cfg.model_copy(update={"persona": cfg.persona + "\n(edited)"})
    assert compute_config_hash(tweaked) != cfg.config_hash


def test_permissions_keys(cfg: NourConfig) -> None:
    p = cfg.permissions
    assert set(p.data_tiers) == {DataTier.T0, DataTier.T1, DataTier.T2, DataTier.T3}
    assert p.data_tiers[DataTier.T2].never_in == ["memory", "logs", "prompts"]
    assert p.data_tiers[DataTier.T2].exceptions == [
        "receiving bank details on the company's own invoices (SPEC §10)"
    ]
    assert p.data_tiers[DataTier.T3].assistant_desk == "never"
    assert set(p.action_tiers) == {ActionTier.A, ActionTier.N, ActionTier.K}
    assert p.action_tiers[ActionTier.N].notify_within_minutes == 60
    assert p.action_tiers[ActionTier.A].notify_within_minutes is None
    assert "scheduling" in p.action_tiers[ActionTier.A].examples
    auth = p.command_authentication
    assert auth.ordinary.requires == ["owner_number"]
    assert auth.high_impact.spoken_passphrase_accepted is False
    assert auth.constitutional.requires == [
        "owner_number",
        "typed_passphrase_same_thread",
        "second_channel_confirmation",
    ]
    assert auth.voice_is_identity is False
    assert p.high_impact_actions == [
        "money_out",
        "new_beneficiary",
        "new_account",
        "vault_retrieval",
        "document_sharing",
        "send_in_owner_name",
        "autonomy_change",
    ]
    g = p.graduated_autonomy
    assert g.new_category_default_tier is ActionTier.K
    assert g.new_category_min_days == 14
    assert g.promotion_unedited_rate == pytest.approx(0.9)
    assert g.promotion_requires_passphrase is True
    assert g.demote_on == ["cost_money", "lost_customer", "legal_exposure"]
    assert g.max_promotion_steps_per_review == 1
    assert "instruction_found_in_observed_content" in p.ask_every_time
    # DESIGN §3.6 defaults, not in the yaml yet
    assert "money_out" in p.readback_categories and "crm_update" in p.readback_categories
    assert p.second_channel_required == [
        "constitution_change",
        "kill_switch_release",
        "deputy_activation",
    ]


def test_spend_tiers_keys_and_money(cfg: NourConfig) -> None:
    s = cfg.spend_tiers
    assert s.currency == "AED"
    assert s.monthly_cap[BudgetHolder("operator")] == Money.aed(3000)
    assert s.monthly_cap[BudgetHolder("assistant_logistics")] is None
    assert s.monthly_cap[BudgetHolder("ai_models_within_operator")] is None
    assert [(b.max, b.tier) for b in s.sorted_bands()] == [
        (Money.aed(200), ActionTier.A),
        (Money.aed(1000), ActionTier.N),
        (None, ActionTier.K),
    ]
    assert s.always_K == [
        "new_beneficiary",
        "new_account",
        "recurring_subscription",
        "customer_refund",
    ]
    assert s.watchdog.daily_spend_multiple_freeze == 3
    assert s.watchdog.failed_sends_per_hour_freeze == 10
    assert s.watchdog.loop_repeat_freeze == 3


@pytest.mark.parametrize(
    ("aed", "tier"),
    [
        (0, ActionTier.A),
        (200, ActionTier.A),
        (201, ActionTier.N),
        (1000, ActionTier.N),
        (1001, ActionTier.K),
        (5000, ActionTier.K),
    ],
)
def test_band_for_boundaries(cfg: NourConfig, aed: int, tier: ActionTier) -> None:
    assert cfg.spend_tiers.band_for(Money.aed(aed)) is tier


def test_band_for_refuses_another_currency(cfg: NourConfig) -> None:
    with pytest.raises(CurrencyMismatch):
        cfg.spend_tiers.band_for(Money(fils=100, currency="USD"))


def test_spend_tiers_needs_exactly_one_open_band() -> None:
    base = {
        "currency": "AED",
        "monthly_cap": {"operator": 3000},
        "always_K": [],
        "watchdog": {"daily_spend_multiple_freeze": 3, "failed_sends_per_hour_freeze": 10},
    }
    with pytest.raises(ValidationError, match="open-ended"):
        SpendTiersConfig.model_validate({**base, "bands": [{"max": 200, "tier": "A"}]})
    with pytest.raises(ValidationError, match="increasing"):
        SpendTiersConfig.model_validate(
            {
                **base,
                "bands": [
                    {"max": 1000, "tier": "A"},
                    {"max": 200, "tier": "N"},
                    {"max": None, "tier": "K"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="finer than one fils"):
        SpendTiersConfig.model_validate(
            {**base, "bands": [{"max": "200.001", "tier": "A"}, {"max": None, "tier": "K"}]}
        )


def test_calendar_keys(cfg: NourConfig) -> None:
    c = cfg.calendar
    assert c.timezone == "Asia/Dubai"
    ow = c.outreach_window
    assert (ow.start, ow.end) == (dt.time(9, 0), dt.time(20, 0))
    assert ow.blocked_weekday_slots[0].day == "friday"
    assert ow.blocked_weekday_slots[0].start == dt.time(11, 30)
    assert ow.blocked_weekday_slots[0].reason == "Friday midday"
    assert ow.prayer_times.block is True and ow.prayer_times.buffer_minutes == 20
    assert ow.prayer_times.source == "computed"
    assert ow.ramadan.adjust_hours is True
    assert ow.ramadan.outreach_window.start == dt.time(10, 0)
    ramadan = ow.ramadan.dates[0]
    assert (ramadan.start, ramadan.end, ramadan.confirm) == (
        dt.date(2027, 2, 8),
        dt.date(2027, 3, 9),
        True,
    )
    assert ramadan.source is not None and ramadan.source.startswith("https://")
    assert ramadan.basis is not None and "Umm al-Qura" in ramadan.basis
    names = [d.name for d in ow.blocked_dates]
    assert (
        names[0] == "Eid Al Etihad (National Day)"
        and names.count("Eid Al Etihad (National Day)") == 2
    )
    new_year = next(d for d in ow.blocked_dates if d.name == "New Year's Day")
    assert new_year.date == dt.date(2027, 1, 1) and new_year.confirm is False
    assert new_year.basis is None
    pt = c.prayer_times
    assert pt.method == "dubai"
    assert pt.fajr_angle == pytest.approx(18.2)
    assert pt.isha_rule == "angle 18.2"
    assert (pt.latitude, pt.longitude) == (pytest.approx(25.2048), pytest.approx(55.2708))
    assert pt.library.startswith("adhanpy")
    assert pt.madhab == "shafi"
    adj = pt.method_adjustments_minutes
    assert (adj.fajr, adj.sunrise, adj.dhuhr, adj.asr, adj.maghrib, adj.isha) == (0, -3, 3, 3, 3, 0)
    q = c.quiet_hours
    assert (q.start, q.end, q.emergencies_only) == (dt.time(22, 0), dt.time(7, 0), True)
    assert q.emergency_categories == [
        "active_fraud_attempt",
        "payment_failure",
        "legal_deadline_within_24h",
        "customer_safety_issue",
        "system_compromise",
    ]
    r = c.daily_rhythm
    assert (r.morning_brief, r.evening_close, r.nightly_reflection) == (
        dt.time(7, 30),
        dt.time(20, 30),
        dt.time(23, 30),
    )
    assert (r.weekly_review.day, r.weekly_review.time) == ("monday", dt.time(8, 0))
    assert (r.initiative_budget_per_day, r.notify_within_minutes) == (5, 60)
    assert (r.auditor_run, r.notify_sweep_minutes, r.owner_silence_check) == (
        dt.time(0, 30),
        15,
        dt.time(8, 0),
    )
    assert c.study_slot_minutes_per_day == 30


def test_channels_keys(cfg: NourConfig) -> None:
    ch = cfg.channels
    assert ch.owner_thread.channel == "whatsapp" and ch.owner_thread.desk is DeskScope.BOTH
    assert ch.owner_thread.allowed_senders == "owner_number_only"
    assert ch.owner_thread.passphrase_for_high_impact is True
    wa = ch.whatsapp_business
    assert wa.cold_mass_messaging is False and wa.template_messages_for_first_contact is True
    assert wa.daily_send_cap_start == 50
    assert wa.raise_cap_weekly_while_block_rate_under == pytest.approx(0.01)
    assert wa.block_rate_freeze_threshold == pytest.approx(0.01)
    em = ch.email
    assert em.require_spf_dkim_dmarc is True and em.cold_daily_cap == 40 and em.warmup_weeks == 4
    assert em.warmup_schedule == [10, 20, 30, 40]
    assert em.consumer_mail_opt_out == "one_click"
    assert ch.owner_mailboxes.desk is DeskScope.ASSISTANT
    assert ch.owner_mailboxes.oauth_scopes == ["read", "draft", "send"]
    assert ch.owner_mailboxes.password_held is False
    assert (
        ch.ai_voice_line.desk is DeskScope.OPERATOR and ch.ai_voice_line.audio_retention_days == 7
    )
    assert ch.ai_voice_line.disclose_ai_on_request and ch.ai_voice_line.record_only_where_lawful
    assert ch.ai_voice_line.transcripts_kept is True
    assert ch.staff_request_lines.can_command is False
    assert ch.staff_request_lines.money_requires_owner_approval is True
    assert ch.cadences.default_follow_up_days == [0, 3, 7, 14]
    assert ch.cadences.then_monthly and ch.cadences.stop_on_any_reply
    assert ch.consent.do_not_contact_shared_across_coats is True
    assert (
        ch.consent.consumer_messages_carry_opt_out
        and ch.consent.business_contacts_removal_in_one_message
    )
    assert ch.watchdog.failed_sends_per_hour_freeze == 10
    assert ch.extra_channels == []
    assert ch.known_channels() == PHASE0_CHANNELS


def test_deputy_keys(cfg: NourConfig) -> None:
    d = cfg.deputy
    assert d.deputy.name == "<owner sets>" and d.deputy.contact_channels == []
    assert d.thresholds.owner_silent_days_pause_commitments == 3
    assert d.thresholds.owner_silent_days_activate_deputy == 7
    assert "pause_nour" in d.deputy_powers and "read_audit_log" in d.deputy_powers
    assert d.deputy_never_gets == [
        "vault",
        "owner_mailboxes",
        "constitution_changes",
        "new_beneficiaries",
        "kill_switch_release",
    ]
    assert d.owner_return.ends_deputy_mode_on == "passphrase"
    assert d.owner_return.report == "everything done in the owner's absence"


def test_coat_keys_and_loaded_files(cfg: NourConfig) -> None:
    coat = cfg.coat(CoatId("buzz-avenue"))
    assert coat.name == "Buzz Avenue" and coat.slug == "buzz-avenue"
    assert coat.legal_entity.startswith("Buzz Avenue")
    assert coat.desks_allowed == [Desk.OPERATOR, Desk.ASSISTANT]
    assert coat.allows(Desk.OPERATOR) and not coat.allows(Desk.GOVERNANCE)
    ident = coat.identity
    assert ident.title == "AI assistant, Buzz Avenue"
    assert (
        ident.email == "nour@<company-domain>" and ident.whatsapp_line == "<business-line-number>"
    )
    assert ident.signature_ref == "templates/signature.buzz-avenue.html"
    assert ident.letterhead_ref == "templates/letterhead.buzz-avenue.pdf"
    assert ident.verification_page == "https://<company-domain>/nour"
    assert coat.tone_guide_ref == "coats/buzz-avenue.tone.md"
    assert coat.knowledge_pack_ref == "coats/buzz-avenue.knowledge.md"
    assert coat.tone_guide.startswith("# Buzz Avenue — Tone and register guide")
    assert coat.knowledge_pack.startswith("# Buzz Avenue — Knowledge pack")
    m = coat.mandate
    assert (m.price_floor_pct_of_list, m.discount_max_pct) == (85, 15)
    assert m.payment_terms_allowed == ["advance", "net15", "net30"]
    assert m.templates_allowed == ["quote", "invoice", "proposal", "nda_standard"]
    assert m.owner_only == ["contracts_over_aed_20000", "exclusivity", "credit_terms_beyond_net30"]
    rules = coat.approval_rules
    assert rules.default_new_category is ActionTier.K
    assert rules.autonomous_categories == [] and rules.notify_categories == [
        "scheduling",
        "supplier_inquiry",
    ]
    assert rules.category_started_at == {}
    assert coat.allowed_activities == ["sell", "support", "procure", "chase_invoices"]
    assert coat.banking_ref == "vault://buzz-avenue/banking/receiving"
    assert (
        coat.channels.whatsapp_daily_cap,
        coat.channels.email_cold_daily_cap,
        coat.channels.warmup_weeks,
    ) == (50, 40, 4)
    assert cfg.coats_for(Desk.OPERATOR) == [coat]
    with pytest.raises(KeyError):
        cfg.coat(CoatId("no-such-coat"))


def test_models_keys(cfg: NourConfig) -> None:
    m = cfg.models
    assert (m.primary.vendor, m.primary.model, m.primary.region) == (
        "anthropic",
        "claude-fable-5-1",
        None,
    )
    assert (m.fallback.vendor, m.fallback.model) == ("openai", "gpt-6.1-sol")
    assert (m.critic.vendor, m.critic.model) == ("anthropic", "claude-sonnet-5-5")
    assert (m.auditor.vendor, m.auditor.model) == ("openai", "gpt-6.1-sol")
    assert m.private_tier2 is None
    assert m.vendor_violations() == []


def test_capabilities_cover_every_spec_row_and_every_category(cfg: NourConfig) -> None:
    caps = cfg.capabilities
    assert len(caps.capabilities) == 30  # SPEC §7 has thirty rows
    assert [c.phase for c in caps.capabilities] == sorted(c.phase for c in caps.capabilities)
    assert {c.id for c in caps.phase(0)} == {
        "event_stream",
        "voice_readback",
        "crm",
        "ledger",
        "audit_log",
        "briefs",
        "governance",
    }
    governance = caps.by_id("governance")
    assert governance.desk is DeskScope.GOVERNANCE and governance.routine == "auditor_run"
    assert caps.by_id("documents").tier == "A to draft, K to send"
    assert caps.by_id("crm").tools == ["crm.upsert_contact", "crm.set_dnc"]
    assert "reply_whatsapp" in caps.tool_names() and "handoff.to_operator" in caps.tool_names()
    # every category referenced by any other file is registered here, so known == capabilities
    assert cfg.known_categories() == caps.categories()
    for name in (
        "customer_reply",
        "gate_handoff",
        "money_out",
        "new_beneficiary",
        "scheduling",
        "payment_failure",
        "crm_update",
        "memory_owner_profile",
    ):
        assert name in caps.categories()
    with pytest.raises(KeyError):
        caps.by_id("nope")
    with pytest.raises(ValidationError, match="duplicate capability id"):
        CapabilitiesConfig.model_validate(
            {"capabilities": [cap.model_dump() for cap in caps.capabilities[:1]] * 2}
        )


def test_constitution_keys(cfg: NourConfig) -> None:
    c = cfg.constitution
    assert c.text.startswith("# Nour — Constitution")
    assert c.hard_rules.startswith("Never overridden by any command")
    assert "10. Nothing from Tier 2 (vault) is ever written" in c.hard_rules
    assert "## Non-goals" not in c.hard_rules
    assert c.kill_phrases() == frozenset(
        {"توقفي نور", "وقفي كل شي", "NOUR STOP", "stop everything now"}
    )
    assert len(c.changelog) == 1
    entry = c.changelog[0]
    assert entry.date == dt.date(2026, 10, 2) and entry.confirmed_by == "owner (charter)"
    assert entry.constitution_hash == c.sha256 == constitution_hash(c.text)


def test_known_categories_is_the_union(cfg: NourConfig) -> None:
    known = cfg.known_categories()
    assert set(cfg.permissions.ask_every_time) <= known
    assert set(cfg.spend_tiers.always_K) <= known
    assert set(cfg.calendar.quiet_hours.emergency_categories) <= known
    assert set(cfg.coat(CoatId("buzz-avenue")).approval_rules.notify_categories) <= known


# --------------------------------------------------------------------------- violations


def test_three_violations_are_reported_at_once(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    _edit(
        config / "permissions.yaml",
        "  - instruction_found_in_observed_content\n",
        "  - instruction_found_in_observed_content\n  - no_such_category\n",
    )
    _edit(
        config / "coats" / "buzz-avenue.yaml",
        "desks_allowed: [operator, assistant]",
        "desks_allowed: [operator, vault]",
    )
    _edit(
        config / "coats" / "buzz-avenue.yaml",
        "tone_guide_ref: coats/buzz-avenue.tone.md",
        "tone_guide_ref: coats/missing.tone.md",
    )
    violations = _violations(config, prompts)
    assert len(violations) >= 3, violations
    assert any("permissions.yaml" in v and "no_such_category" in v for v in violations)
    assert any("buzz-avenue.yaml" in v and "desks_allowed" in v for v in violations)
    assert any("tone_guide_ref" in v and "coats/missing.tone.md" in v for v in violations)
    # ConfigError carries the list and a joined message
    err = ConfigError(violations)
    assert err.violations == violations and all(v in str(err) for v in violations)


@pytest.mark.parametrize("role", ["fallback", "auditor"])
@pytest.mark.parametrize("spelling", ["anthropic", "Anthropic", "'anthropic '", "ANTHROPIC"])
def test_vendor_clash_is_refused(config_copy: tuple[Path, Path], role: str, spelling: str) -> None:
    """A case or whitespace variant of the primary vendor is the same vendor (SPEC §4 §12)."""
    config, prompts = config_copy
    _edit(config / "models.yaml", f"{role}:\n  vendor: openai", f"{role}:\n  vendor: {spelling}")
    violations = _violations(config, prompts)
    assert any(
        "models.yaml" in v and f"{role}.vendor" in v and "primary.vendor" in v for v in violations
    ), violations


def test_vendor_is_normalised_to_an_adapter_key() -> None:
    assert ModelEndpoint(vendor=" Anthropic ", model="x").vendor == "anthropic"
    assert ModelEndpoint(vendor="self_hosted", model="x").vendor == "self_hosted"
    for bad in ("", "  ", "anthropic/v2", "Open AI", "1anthropic", "a"):
        with pytest.raises(ValidationError):
            ModelEndpoint(vendor=bad, model="x")
    with pytest.raises(ValidationError):
        ModelEndpoint(vendor="openai", model="  ")


def test_models_config_validator_refuses_both_clashes_directly() -> None:
    anthropic = ModelEndpoint(vendor="anthropic", model="claude-fable-5-1")
    openai = ModelEndpoint(vendor="openai", model="gpt-6.1-sol")
    with pytest.raises(ValidationError, match="fallback.vendor"):
        ModelsConfig(
            primary=anthropic,
            fallback=ModelEndpoint(vendor="Anthropic", model="other"),
            critic=anthropic,
            auditor=openai,
        )
    assert (
        ModelsConfig(
            primary=anthropic, fallback=openai, critic=anthropic, auditor=openai
        ).vendor_violations()
        == []
    )
    with pytest.raises(ValidationError, match="fallback.vendor"):
        ModelsConfig(primary=anthropic, fallback=anthropic, critic=anthropic, auditor=openai)
    with pytest.raises(ValidationError, match="auditor.vendor"):
        ModelsConfig(primary=anthropic, fallback=openai, critic=anthropic, auditor=anthropic)


def test_stale_constitution_hash_is_refused(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    _edit(
        config / "constitution.md", "## Non-goals", "11. A new rule nobody logged.\n\n## Non-goals"
    )
    violations = _violations(config, prompts)
    assert any("constitution.md" in v and "stale" in v for v in violations), violations
    assert len(violations) == 1


def test_constitution_without_hash_column_is_refused(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    text = (config / "constitution.md").read_text(encoding="utf-8")
    head, _, _table = text.partition("| Date |")
    (config / "constitution.md").write_text(
        head
        + "| Date | Change | Confirmed by |\n|---|---|---|\n| 2026-10-02 | Initial | owner |\n",
        encoding="utf-8",
    )
    violations = _violations(config, prompts)
    assert any("records no constitution hash" in v for v in violations), violations


def test_a_change_log_entry_with_the_new_hash_fixes_it(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    path = config / "constitution.md"
    _edit(path, "## Non-goals", "11. A new rule, logged below.\n\n## Non-goals")
    new_hash = constitution_hash(path.read_text(encoding="utf-8"))
    _edit(path, "| owner (charter) | sha256:", "| owner (charter) | sha256:")  # unchanged row stays
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"| 2026-10-03 | Added rule 11 | owner (passphrase + second channel) | {new_hash} |\n"
        )
    loaded = load_config(config, prompts)
    assert loaded.constitution.sha256 == new_hash
    assert loaded.constitution.changelog[-1].change == "Added rule 11"


def test_hashed_region_excludes_the_change_log() -> None:
    text = "# C\n\n## Hard rules\n\n1. Rule.\n\n## Kill switch\n\n- STOP\n\n## Change log\n\n| Date | Change | Confirmed by |\n|---|---|---|\n"
    before = constitution_hash(text)
    assert hashed_region(text) == "# C\n\n## Hard rules\n\n1. Rule.\n\n## Kill switch\n\n- STOP\n"
    assert constitution_hash(text + "| 2026-10-02 | x | owner |\n") == before
    assert constitution_hash(text.replace("\n", "\r\n")) == before
    assert constitution_hash(text.replace("1. Rule.", "1. Rule!")) != before


def test_parse_constitution_sections() -> None:
    text = (
        "# C\n\n## Authority\n\n- owner\n\n## Hard rules\n\nNever.\n\n1. One.\n2. Two.\n\n## Kill switch\n\n"
        "Intro line.\n\n- توقفي نور\n* STOP NOW\n\n## Change log\n\n| Date | Change | Confirmed by | Constitution hash |\n"
        "|---|---|---|---|\n| 2026-10-02 | first | owner | sha256:abc |\n| 2026-10-03 | second | owner | |\n"
    )
    c = parse_constitution(text)
    assert c.hard_rules == "Never.\n\n1. One.\n2. Two."
    assert c.kill_phrases() == frozenset({"توقفي نور", "STOP NOW"})
    assert [e.change for e in c.changelog] == ["first", "second"]
    assert c.changelog[0].constitution_hash == "sha256:abc"
    assert c.changelog[1].constitution_hash is None and c.latest_entry is c.changelog[1]
    with pytest.raises(ValueError, match="Hard rules"):
        parse_constitution("# C\n\n## Change log\n")


def test_prompt_with_undefined_variable_is_refused(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    _edit(prompts / "nour.system.md", "## Persona\n", "## Persona\n{{ not_a_variable }}\n")
    _edit(prompts / "critic.system.md", "{{ draft }}", "{{ draft.nmae }}")
    violations = _violations(config, prompts)
    assert any("prompts/nour.system.md" in v and "not_a_variable" in v for v in violations), (
        violations
    )
    assert any("prompts/critic.system.md" in v for v in violations), violations


def test_bank_placeholders_are_not_template_variables() -> None:
    template = "Use `{{bank.<coat>.iban}}` and {{bank.buzz-avenue.swift}} but {{ draft }}."
    protected = protect_bank_placeholders(template)
    assert protected.count("{% raw %}") == 2
    context = prompt_check_context(
        coat=None, constitution=None, permissions=None, spend_tiers=None, calendar=None, persona=""
    )
    assert check_prompt_templates({"x.md": template}, context) == []
    bad = check_prompt_templates({"y.md": "{{ coat.name }} {{ nope }}"}, context)
    assert len(bad) == 1 and "prompts/y.md" in bad[0] and "nope" in bad[0]
    assert set(context) == PROMPT_CONTEXT_VARIABLES


def test_unknown_yaml_key_is_a_violation(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    _edit(config / "spend_tiers.yaml", "currency: AED\n", "currency: AED\ncurency_typo: USD\n")
    violations = _violations(config, prompts)
    assert any("spend_tiers.yaml" in v and "curency_typo" in v for v in violations), violations


def test_missing_file_is_a_violation(config_copy: tuple[Path, Path]) -> None:
    config, prompts = config_copy
    (config / "deputy.yaml").unlink()
    (config / "coats" / "buzz-avenue.yaml").rename(config / "coats" / "other-name.yaml")
    violations = _violations(config, prompts)
    assert any("deputy.yaml" in v and "not found" in v for v in violations), violations
    assert any("other-name.yaml" in v and "slug" in v for v in violations), violations


# --------------------------------------------------------------------------- calendar rules


@pytest.fixture(scope="module")
def cal() -> CalendarConfig:
    return load_config(CONFIG_DIR, PROMPTS_DIR).calendar


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (dubai(2026, 10, 5, 23, 10), True),  # Monday night
        (dubai(2026, 10, 5, 22, 0), True),  # starts at 22:00
        (dubai(2026, 10, 5, 21, 59), False),
        (dubai(2026, 10, 6, 6, 59), True),  # still quiet before 07:00
        (dubai(2026, 10, 6, 7, 0), False),  # ends at 07:00
        (dubai(2026, 10, 6, 12, 0), False),
    ],
)
def test_in_quiet_hours(cal: CalendarConfig, when: dt.datetime, expected: bool) -> None:
    assert cal.in_quiet_hours(when) is expected


def test_quiet_hours_accept_utc_input(cal: CalendarConfig) -> None:
    assert cal.in_quiet_hours(dubai(2026, 10, 5, 23, 10).astimezone(dt.UTC)) is True
    assert cal.in_quiet_hours(dt.datetime(2026, 10, 5, 23, 10)) is True  # noqa: DTZ001 - naive means local


@pytest.mark.parametrize(
    ("when", "expected", "why"),
    [
        (dubai(2026, 10, 5, 10, 0), True, "Monday mid-morning"),
        (dubai(2026, 10, 5, 8, 59), False, "before 09:00"),
        (dubai(2026, 10, 5, 9, 0), True, "window opens"),
        (dubai(2026, 10, 5, 19, 59), True, "last minute of the window"),
        (dubai(2026, 10, 5, 20, 0), False, "window closes"),
        (dubai(2026, 10, 5, 12, 10), False, "Dhuhr 12:10, inside the 20-minute buffer"),
        (dubai(2026, 10, 5, 12, 31), True, "buffer over"),
        (dubai(2026, 10, 9, 12, 0), False, "Friday midday slot"),
        (dubai(2026, 10, 9, 13, 29), False, "Friday slot runs to 13:30"),
        (dubai(2026, 10, 9, 13, 30), True, "Friday slot over"),
        (dubai(2026, 10, 9, 11, 29), True, "Friday before the slot"),
        (dubai(2026, 12, 2, 10, 0), False, "Eid Al Etihad, blocked date"),
        (dubai(2026, 12, 3, 15, 0), False, "second day of the block"),
        (dubai(2026, 12, 4, 10, 0), True, "day after the block"),
        (dubai(2027, 3, 13, 10, 0), False, "day after a provisional (confirm: true) Eid range"),
        (dubai(2027, 2, 10, 9, 30), False, "Ramadan window starts 10:00"),
        (dubai(2027, 2, 10, 10, 30), True, "inside the Ramadan window"),
        (dubai(2027, 2, 10, 16, 0), False, "Ramadan window ends 16:00"),
    ],
)
def test_in_outreach_window(
    cal: CalendarConfig, when: dt.datetime, expected: bool, why: str
) -> None:
    assert cal.in_outreach_window(when) is expected, why


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (dubai(2026, 10, 5, 10, 0), dubai(2026, 10, 5, 10, 0)),  # already open: itself
        (dubai(2026, 10, 5, 8, 0), dubai(2026, 10, 5, 9, 0)),
        (dubai(2026, 10, 5, 21, 0), dubai(2026, 10, 6, 9, 0)),
        (dubai(2026, 10, 5, 12, 10), dubai(2026, 10, 5, 12, 30)),  # Dhuhr buffer end
        (dubai(2026, 10, 9, 12, 0), dubai(2026, 10, 9, 13, 30)),  # Friday slot end
        (dubai(2026, 12, 2, 10, 0), dubai(2026, 12, 4, 9, 0)),  # two blocked days
        (dubai(2027, 2, 9, 21, 0), dubai(2027, 2, 10, 10, 0)),  # next day is Ramadan: 10:00
        (dubai(2027, 3, 12, 11, 0), dubai(2027, 3, 14, 9, 0)),  # provisional margin day skipped
    ],
)
def test_next_outreach_open(cal: CalendarConfig, when: dt.datetime, expected: dt.datetime) -> None:
    result = cal.next_outreach_open(when)
    assert result == expected
    assert result.tzinfo is not None and cal.in_outreach_window(result)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (dubai(2026, 10, 5, 23, 10), dubai(2026, 10, 6, 7, 0)),
        (dubai(2026, 10, 6, 6, 30), dubai(2026, 10, 6, 7, 0)),
        (dubai(2026, 10, 6, 7, 0), dubai(2026, 10, 7, 7, 0)),
        (dubai(2026, 10, 6, 12, 0), dubai(2026, 10, 7, 7, 0)),
    ],
)
def test_next_quiet_end(cal: CalendarConfig, when: dt.datetime, expected: dt.datetime) -> None:
    assert cal.next_quiet_end(when) == expected


def test_prayer_block_until_takes_naive_or_aware_input(cal: CalendarConfig) -> None:
    """Every CalendarConfig method takes a Dubai-local datetime, naive meaning local."""
    aware = cal.prayer_block_until(dubai(2026, 10, 5, 12, 10))
    dhuhr = cal.prayer_times_for(dt.date(2026, 10, 5))["dhuhr"]
    assert aware is not None and aware == dhuhr + dt.timedelta(minutes=20)
    assert cal.prayer_block_until(dt.datetime(2026, 10, 5, 12, 10)) == aware  # noqa: DTZ001 - naive means local
    assert cal.prayer_block_until(dubai(2026, 10, 5, 12, 10).astimezone(dt.UTC)) == aware
    assert cal.prayer_block_until(dt.datetime(2026, 10, 5, 10, 0)) is None  # noqa: DTZ001


def test_prayer_times_match_the_awqaf_table(cal: CalendarConfig) -> None:
    times = cal.prayer_times_for(dt.date(2026, 10, 2))  # docs/adapters/calendar.md §3
    as_text = {name: at.strftime("%H:%M") for name, at in times.items()}
    assert as_text == {
        "fajr": "04:54",
        "sunrise": "06:08",
        "dhuhr": "12:11",
        "asr": "15:35",
        "maghrib": "18:08",
        "isha": "19:22",
    }
    assert all(
        at.tzinfo is not None and at.utcoffset() == dt.timedelta(hours=4) for at in times.values()
    )


def test_blocked_date_shapes() -> None:
    single = BlockedDate(name="x", date=dt.date(2027, 1, 1))
    assert single.covers(dt.date(2027, 1, 1)) and not single.covers(dt.date(2027, 1, 2))
    provisional = BlockedDate(
        name="y", start=dt.date(2027, 3, 9), end=dt.date(2027, 3, 12), confirm=True
    )
    assert provisional.covers(dt.date(2027, 3, 13))
    assert not provisional.covers(dt.date(2027, 3, 13), provisional_margin=False)
    with pytest.raises(ValidationError):
        BlockedDate(
            name="z", date=dt.date(2027, 1, 1), start=dt.date(2027, 1, 1), end=dt.date(2027, 1, 2)
        )
    with pytest.raises(ValidationError):
        BlockedDate(name="z", start=dt.date(2027, 1, 2), end=dt.date(2027, 1, 1))
    with pytest.raises(ValidationError):
        BlockedDate(name="z")


# --------------------------------------------------------------------------- settings


def test_settings_defaults_without_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(__import__("os").environ):
        if name.upper().startswith("NOUR_"):
            monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.env == "test" and s.dry_run is True and s.desk is None
    assert s.database_url == "sqlite+pysqlite:///nour.db" and s.is_sqlite
    assert s.config_dir == Path("config") and s.prompts_dir == Path("prompts")
    assert s.region == "me-central-1" and s.secrets_backend == "fake"
    assert s.stamp_key_name == "auth/stamp-key"
    assert s.leakguard_key_name == "governance/leakguard-key"


def test_settings_parse_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOUR_ENV", "staging")
    monkeypatch.setenv("NOUR_DRY_RUN", "false")
    monkeypatch.setenv("NOUR_DESK", "operator")
    monkeypatch.setenv("NOUR_DATABASE_URL", "postgresql+psycopg://nour:nour@db/nour")
    monkeypatch.setenv("NOUR_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NOUR_SECRETS_BACKEND", "aws")
    monkeypatch.setenv("NOUR_REGION", "uaenorth")
    monkeypatch.setenv("NOUR_TIMEZONE", "Asia/Dubai")  # infrastructure knob: ignored
    monkeypatch.setenv("NOUR_PASSPHRASE", "never-a-setting")  # ignored (SPEC §6 §13)
    s = Settings()
    assert s.env == "staging" and s.dry_run is False and s.desk is Desk.OPERATOR
    assert s.database_url.startswith("postgresql") and not s.is_sqlite
    assert s.config_dir == tmp_path / "cfg"
    assert s.secrets_backend == "aws" and s.region == "uaenorth"
    assert not hasattr(s, "passphrase") and not hasattr(s, "timezone")
    assert "never-a-setting" not in repr(s)


def test_settings_speak_one_vocabulary_with_env_example_and_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DESIGN §3.6: test|staging|prod and fake|aws|azure, spelled the same in .env.example and
    the CI workflow; the old spellings (development, production, env) are refused loudly."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in example.splitlines()
        if line.startswith(("NOUR_ENV=", "NOUR_SECRETS_BACKEND="))
    )
    assert values == {"NOUR_ENV": "test", "NOUR_SECRETS_BACKEND": "fake"}
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "NOUR_ENV: test" in ci and "NOUR_SECRETS_BACKEND: fake" in ci
    monkeypatch.setenv("NOUR_ENV", values["NOUR_ENV"])
    monkeypatch.setenv("NOUR_SECRETS_BACKEND", values["NOUR_SECRETS_BACKEND"])
    monkeypatch.setenv("NOUR_DESK", "")
    s = Settings()
    assert s.env == "test" and s.secrets_backend == "fake" and s.desk is None
    monkeypatch.setenv("NOUR_ENV", " Staging ")  # whitespace and case are forgiven
    monkeypatch.setenv("NOUR_SECRETS_BACKEND", "AWS")
    assert Settings().env == "staging" and Settings().secrets_backend == "aws"
    for env in ("development", "dev", "production", "testing", "stage"):
        monkeypatch.setenv("NOUR_ENV", env)
        with pytest.raises(ValidationError, match="env"):
            Settings()
    monkeypatch.setenv("NOUR_ENV", "test")
    for backend in ("env", "memory", "vault"):
        monkeypatch.setenv("NOUR_SECRETS_BACKEND", backend)
        with pytest.raises(ValidationError, match="secrets_backend"):
            Settings()


def test_settings_refuse_fake_secrets_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOUR_ENV", "prod")
    monkeypatch.setenv("NOUR_SECRETS_BACKEND", "fake")
    with pytest.raises(ValidationError, match="secrets manager"):
        Settings()
    monkeypatch.setenv("NOUR_SECRETS_BACKEND", "azure")
    assert Settings().env == "prod"
    with pytest.raises(ValidationError):
        Settings(env="nowhere")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(desk="vault")  # type: ignore[arg-type]


def test_settings_init_arguments_win_over_the_environment(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setenv("NOUR_DRY_RUN", "false")
    assert settings.dry_run is True
    assert Settings(dry_run=True).dry_run is True
    assert settings.config_dir == CONFIG_DIR and settings.prompts_dir == PROMPTS_DIR
    with pytest.raises(ValidationError):
        settings.dry_run = False  # frozen
