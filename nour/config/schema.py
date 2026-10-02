"""Config schemas (DESIGN §3.6 ``nour/config/schema.py``; SPEC §6 §7 §9 §10 §12 §14 §15 §18).

One frozen pydantic model per file (and per block) under ``config/`` and ``prompts/``, modelled
key for key against the real repository files: every field name is a yaml key and every yaml key
is a field (``extra="forbid"`` on every model, so a misspelt key is a ``ConfigError`` at load,
never a silently ignored setting). ``Channel``, ``ActionCategory`` and ``BudgetHolder`` stay
validated strings (DESIGN §3.1): categories are checked against ``capabilities.yaml`` by the
loader, never by a closed enum, so phases 1–3 add yaml rows without reopening ``nour.core``.

Owner-specific secrets (number, passphrase, card refs) are never in config: they live in the
``owner`` / ``card`` rows set by the CLI (DESIGN §3.6).

Dates and times: ``datetime`` is imported as ``dt`` throughout because several models have a
field literally named ``date``/``time`` (the yaml keys); annotations therefore read ``dt.date``.
Every ``CalendarConfig`` method takes a Dubai-local datetime (tz-aware, or naive meaning local)
and returns tz-aware datetimes in ``timezone``.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, ClassVar, Literal
from zoneinfo import ZoneInfo

from adhanpy.calculation.CalculationMethod import CalculationMethod
from adhanpy.PrayerTimes import PrayerTimes as _AdhanPrayerTimes
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nour.core.types import (
    PHASE0_CHANNELS,
    ActionCategory,
    ActionTier,
    BudgetHolder,
    Channel,
    CoatId,
    DataTier,
    Desk,
    DeskScope,
    Hash,
    Money,
)

WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
"""Weekday names as ``calendar.yaml`` spells them (``blocked_weekday_slots[].day``,
``daily_rhythm.weekly_review.day``); index == ``date.weekday()``."""

PRAYER_NAMES: tuple[str, ...] = ("fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha")
"""The five prayers plus sunrise, in the order adhanpy reports them."""

_FILS_PER_UNIT = 100


class ConfigModel(BaseModel):
    """Base of every config model: frozen, no extra keys, no silent coercion of field names."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _weekday(value: str) -> str:
    name = value.strip().lower()
    if name not in WEEKDAYS:
        raise ValueError(f"unknown weekday {value!r}; expected one of {', '.join(WEEKDAYS)}")
    return name


# ---------------------------------------------------------------------------- permissions.yaml


class DataTierRule(ConfigModel):
    """§6 data tiers: one row of ``permissions.data_tiers`` (keyed by ``DataTier``)."""

    name: str
    content: str
    assistant_desk: str
    operator_desk: str
    leaves_to_third_parties: str
    exceptions: list[str] = []
    never_in: list[str] = []


class ActionTierRule(ConfigModel):
    """§6 action tiers: one row of ``permissions.action_tiers`` (keyed by ``ActionTier``)."""

    name: str
    rule: str
    examples: list[str] = []
    notify_within_minutes: int | None = None


class CommandClass(ConfigModel):
    """§6 command authentication: what one command class requires and covers."""

    requires: list[str]
    covers: list[str] = []
    spoken_passphrase_accepted: bool | None = None


class CommandAuthentication(ConfigModel):
    """§6: ordinary / high-impact / constitutional; ``voice_is_identity`` is always false."""

    ordinary: CommandClass
    high_impact: CommandClass
    constitutional: CommandClass
    voice_is_identity: bool = False

    @field_validator("voice_is_identity")
    @classmethod
    def _voice_is_never_identity(cls, value: bool) -> bool:
        if value:
            raise ValueError("voice_is_identity must be false (SPEC §6: voice is never identity)")
        return value


class GraduatedAutonomy(ConfigModel):
    """§6 graduated autonomy parameters."""

    new_category_default_tier: ActionTier
    new_category_min_days: int
    promotion_unedited_rate: float
    promotion_window_days: int
    promotion_min_items: int
    promotion_requires_passphrase: bool
    demote_on: list[str]
    max_promotion_steps_per_review: int


class PermissionsConfig(ConfigModel):
    """§6. Categories are strings validated against capabilities.yaml; nothing here is a core enum.

    ``readback_categories`` and ``second_channel_required`` are not in the shipped yaml yet; the
    defaults are the DESIGN §3.6 lists and the owner may override them in ``permissions.yaml``.
    """

    data_tiers: dict[DataTier, DataTierRule]
    action_tiers: dict[ActionTier, ActionTierRule]
    command_authentication: CommandAuthentication
    high_impact_actions: list[ActionCategory]
    graduated_autonomy: GraduatedAutonomy
    ask_every_time: list[ActionCategory]
    readback_categories: list[ActionCategory] = [
        ActionCategory("money_out"),
        ActionCategory("payment_prepare"),
        ActionCategory("send_in_owner_name"),
        ActionCategory("crm_update"),
        ActionCategory("beneficiary_change"),
        ActionCategory("memory_semantic"),
        ActionCategory("memory_owner_profile"),
        ActionCategory("document_sharing"),
    ]
    second_channel_required: list[ActionCategory] = [
        ActionCategory("constitution_change"),
        ActionCategory("kill_switch_release"),
        ActionCategory("deputy_activation"),
    ]

    @field_validator("data_tiers")
    @classmethod
    def _all_data_tiers(cls, value: dict[DataTier, DataTierRule]) -> dict[DataTier, DataTierRule]:
        missing = [tier.value for tier in DataTier if tier not in value]
        if missing:
            raise ValueError(f"data_tiers is missing tier(s) {missing}")
        return value

    @field_validator("action_tiers")
    @classmethod
    def _all_action_tiers(
        cls, value: dict[ActionTier, ActionTierRule]
    ) -> dict[ActionTier, ActionTierRule]:
        missing = [tier.value for tier in ActionTier if tier not in value]
        if missing:
            raise ValueError(f"action_tiers is missing tier(s) {missing}")
        return value

    def referenced_categories(self) -> frozenset[ActionCategory]:
        """Every category name this file mentions (checked against capabilities.yaml)."""
        return frozenset(
            self.high_impact_actions
            + self.ask_every_time
            + self.readback_categories
            + self.second_channel_required
        )


# ---------------------------------------------------------------------------- spend_tiers.yaml


def _to_money(value: Any, currency: str, label: str) -> Any:
    """``200`` / ``"200"`` / ``Decimal`` → ``{"fils": 20000, "currency": currency}``; Money and
    dicts pass through; ``None`` stays ``None`` (``<owner sets>``)."""
    if value is None or isinstance(value, Money | dict):
        return value
    if isinstance(value, bool | float):
        raise ValueError(f"{label}: amounts are whole AED or a decimal string, got {value!r}")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label}: not a money amount: {value!r}") from exc
    fils = amount * _FILS_PER_UNIT
    if fils != fils.to_integral_value():
        raise ValueError(f"{label}: {value!r} is finer than one fils")
    return {"fils": int(fils), "currency": currency}


class SpendBand(ConfigModel):
    """§10 one row of ``spend_tiers.bands``; ``max: null`` is the open-ended top band."""

    max: Money | None
    tier: ActionTier


class WatchdogConfig(ConfigModel):
    """§12 automatic freezes (``spend_tiers.watchdog``)."""

    daily_spend_multiple_freeze: int
    failed_sends_per_hour_freeze: int
    loop_repeat_freeze: int = 3


class SpendTiersConfig(ConfigModel):
    """§10. monthly_cap keys are BudgetHolders; None means '<owner sets>' and the holder has no card yet.

    Amounts in the yaml are whole AED (``3000``, ``200``); the ``currency`` key of the same file
    turns them into :class:`Money` before validation, so every amount here carries a currency.
    """

    currency: str
    monthly_cap: dict[BudgetHolder, Money | None]
    bands: list[SpendBand]
    always_K: list[ActionCategory]  # noqa: N815 - the yaml key (SPEC §18) is spelled always_K
    watchdog: WatchdogConfig

    @model_validator(mode="before")
    @classmethod
    def _amounts_to_money(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        currency = data.get("currency")
        if not isinstance(currency, str):
            return data
        out = dict(data)
        caps = data.get("monthly_cap")
        if isinstance(caps, dict):
            out["monthly_cap"] = {
                holder: _to_money(amount, currency, f"monthly_cap.{holder}")
                for holder, amount in caps.items()
            }
        bands = data.get("bands")
        if isinstance(bands, list):
            converted: list[Any] = []
            for index, band in enumerate(bands):
                if isinstance(band, dict) and "max" in band:
                    band = {**band, "max": _to_money(band["max"], currency, f"bands[{index}].max")}
                converted.append(band)
            out["bands"] = converted
        return out

    @model_validator(mode="after")
    def _bands_are_well_formed(self) -> SpendTiersConfig:
        if not self.bands:
            raise ValueError("bands must list at least one band")
        open_ended = [band for band in self.bands if band.max is None]
        if len(open_ended) != 1:
            raise ValueError("bands must contain exactly one open-ended band (max: null)")
        for band in self.bands:
            if band.max is not None and band.max.currency != self.currency:
                raise ValueError(f"band max {band.max} is not in {self.currency}")
        for holder, cap in self.monthly_cap.items():
            if cap is not None and cap.currency != self.currency:
                raise ValueError(f"monthly_cap.{holder} {cap} is not in {self.currency}")
        bounded = [band.max for band in self.bands if band.max is not None]
        if any(bounded[i] >= bounded[i + 1] for i in range(len(bounded) - 1)):
            raise ValueError("band maxima must be strictly increasing")
        return self

    def sorted_bands(self) -> list[SpendBand]:
        """Bands by ascending ``max``; the open-ended band last."""
        return sorted(
            self.bands,
            key=lambda band: (band.max is None, band.max.fils if band.max is not None else 0),
        )

    def band_for(self, amount: Money) -> ActionTier:
        """§10 per-transaction band: the first band whose ``max`` is >= ``amount`` (200 → A,
        201 → N, 1000 → N, 1001 → K with the shipped file). A currency mismatch raises
        ``CurrencyMismatch`` through ``Money``'s comparison."""
        for band in self.sorted_bands():
            if band.max is None or amount <= band.max:
                return band.tier
        return ActionTier.K  # unreachable with exactly one open-ended band; K is the safe answer

    def referenced_categories(self) -> frozenset[ActionCategory]:
        return frozenset(self.always_K)


# ---------------------------------------------------------------------------- calendar.yaml


class WindowTimes(ConfigModel):
    """A same-day ``{start, end}`` window (``ramadan.outreach_window``)."""

    start: dt.time
    end: dt.time

    @model_validator(mode="after")
    def _ordered(self) -> WindowTimes:
        if self.start >= self.end:
            raise ValueError(f"window start {self.start} must be before end {self.end}")
        return self


class BlockedSlot(ConfigModel):
    """§9 a recurring weekday slot with no outreach (Friday midday for Jumu'ah)."""

    day: str
    start: dt.time
    end: dt.time
    reason: str | None = None

    _weekday = field_validator("day")(_weekday)

    @model_validator(mode="after")
    def _ordered(self) -> BlockedSlot:
        if self.start >= self.end:
            raise ValueError(f"slot start {self.start} must be before end {self.end}")
        return self

    def covers(self, local: dt.datetime) -> bool:
        return WEEKDAYS[local.weekday()] == self.day and self.start <= local.time() < self.end


class PrayerTimes(ConfigModel):
    """``outreach_window.prayer_times``: whether and how wide prayer times block outreach."""

    block: bool
    buffer_minutes: int
    source: Literal["computed", "table"]


class BlockedDate(ConfigModel):
    """One holiday or Ramadan period: a single ``date`` or an inclusive ``start``..``end`` range.

    ``confirm: true`` marks an expected lunar date the Cabinet has not announced yet; per
    docs/adapters/calendar.md §4 a provisional entry also blocks the day after its range.
    """

    name: str
    date: dt.date | None = None
    start: dt.date | None = None
    end: dt.date | None = None
    confirm: bool = False
    source: str | None = None
    basis: str | None = None

    @model_validator(mode="after")
    def _one_shape(self) -> BlockedDate:
        if self.date is not None:
            if self.start is not None or self.end is not None:
                raise ValueError(f"{self.name}: give either date or start/end, not both")
        else:
            if self.start is None or self.end is None:
                raise ValueError(f"{self.name}: needs date, or both start and end")
            if self.start > self.end:
                raise ValueError(f"{self.name}: start {self.start} is after end {self.end}")
        return self

    @property
    def first_day(self) -> dt.date:
        return self.date if self.date is not None else self.start  # type: ignore[return-value]

    @property
    def last_day(self) -> dt.date:
        return self.date if self.date is not None else self.end  # type: ignore[return-value]

    def covers(self, day: dt.date, *, provisional_margin: bool = True) -> bool:
        """True when ``day`` is inside the entry (plus one day after a ``confirm: true`` range)."""
        last = self.last_day
        if self.confirm and provisional_margin:
            last = last + dt.timedelta(days=1)
        return self.first_day <= day <= last


class RamadanRules(ConfigModel):
    """§14: narrower outreach window while Ramadan runs (dates confirmed yearly by the owner)."""

    adjust_hours: bool
    outreach_window: WindowTimes
    dates: list[BlockedDate] = []


class OutreachWindow(ConfigModel):
    """§9 §14 when the Operator may reach out; replies to inbound messages are not outreach."""

    start: dt.time
    end: dt.time
    blocked_weekday_slots: list[BlockedSlot] = []
    prayer_times: PrayerTimes
    ramadan: RamadanRules
    blocked_dates: list[BlockedDate] = []

    @model_validator(mode="after")
    def _ordered(self) -> OutreachWindow:
        if self.start >= self.end:
            raise ValueError(f"outreach window start {self.start} must be before end {self.end}")
        return self


class QuietHours(ConfigModel):
    """§12 quiet hours (22:00–07:00 wraps midnight); emergency categories bypass them only."""

    start: dt.time
    end: dt.time
    emergencies_only: bool
    emergency_categories: list[ActionCategory]


class WeeklyReview(ConfigModel):
    """§12 Monday 08:00 weekly review."""

    day: str
    time: dt.time

    _weekday = field_validator("day")(_weekday)


class DailyRhythm(ConfigModel):
    """§12 daily rhythm; the three defaults are DESIGN §3.6 additions the yaml may override."""

    morning_brief: dt.time
    evening_close: dt.time
    nightly_reflection: dt.time
    weekly_review: WeeklyReview
    initiative_budget_per_day: int
    notify_within_minutes: int
    auditor_run: dt.time = dt.time(0, 30)
    notify_sweep_minutes: int = 15
    owner_silence_check: dt.time = dt.time(8, 0)


class MethodAdjustments(ConfigModel):
    """Per-prayer minute offsets the chosen calculation method applies."""

    fajr: int = 0
    sunrise: int = 0
    dhuhr: int = 0
    asr: int = 0
    maghrib: int = 0
    isha: int = 0


class PrayerComputation(ConfigModel):
    """Top-level ``prayer_times`` block: offline computation parameters for Dubai
    (docs/adapters/calendar.md §3). ``madhab`` and ``method_adjustments_minutes`` describe what
    the named method applies so a library swap can reproduce the same table from config alone."""

    method: str
    fajr_angle: float
    isha_rule: str
    latitude: float
    longitude: float
    library: str
    madhab: str
    method_adjustments_minutes: MethodAdjustments = MethodAdjustments()

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        name = value.strip().upper()
        if name not in CalculationMethod.__members__:
            raise ValueError(
                f"unknown prayer-time method {value!r}; adhanpy knows "
                f"{', '.join(m.lower() for m in CalculationMethod.__members__)}"
            )
        return value.strip().lower()

    @field_validator("latitude")
    @classmethod
    def _lat(cls, value: float) -> float:
        if not -90 <= value <= 90:
            raise ValueError("latitude out of range")
        return value

    @field_validator("longitude")
    @classmethod
    def _lon(cls, value: float) -> float:
        if not -180 <= value <= 180:
            raise ValueError("longitude out of range")
        return value


@lru_cache(maxsize=1024)
def _prayer_times(
    latitude: float, longitude: float, method: str, timezone: str, day: dt.date
) -> dict[str, dt.datetime]:
    """adhanpy times for one day, cached (docs/adapters/calendar.md: cache per day)."""
    tz = ZoneInfo(timezone)
    computed = _AdhanPrayerTimes(
        (latitude, longitude),
        dt.datetime(day.year, day.month, day.day, tzinfo=tz),
        CalculationMethod[method.upper()],
        time_zone=tz,
    )
    return {name: getattr(computed, name).astimezone(tz) for name in PRAYER_NAMES}


class CalendarConfig(ConfigModel):
    """§9 §12 §14: three separate rule sets (outreach window, quiet hours, rhythm).

    ``in_outreach_window`` applies docs/adapters/calendar.md §4 in order: blocked dates (with
    the provisional margin), blocked weekday slots, the Ramadan window when it applies, the daily
    window, then the computed prayer-time buffers. Quiet hours are a different rule
    (``OwnerChannel``, DESIGN §2.6) and are deliberately not part of it.
    """

    timezone: str
    outreach_window: OutreachWindow
    prayer_times: PrayerComputation
    quiet_hours: QuietHours
    daily_rhythm: DailyRhythm
    study_slot_minutes_per_day: int

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001 - zoneinfo raises several error types
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    # --- helpers

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def localise(self, when: dt.datetime) -> dt.datetime:
        """Naive → assumed local; aware → converted to ``timezone``."""
        if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
            return when.replace(tzinfo=self.tz)
        return when.astimezone(self.tz)

    def _at(self, day: dt.date, at: dt.time) -> dt.datetime:
        return dt.datetime.combine(day, at, tzinfo=self.tz)

    def is_blocked_date(self, day: dt.date) -> bool:
        return any(entry.covers(day) for entry in self.outreach_window.blocked_dates)

    def is_ramadan(self, day: dt.date) -> bool:
        ramadan = self.outreach_window.ramadan
        return ramadan.adjust_hours and any(
            entry.covers(day, provisional_margin=False) for entry in ramadan.dates
        )

    def window_for(self, day: dt.date) -> tuple[dt.time, dt.time]:
        """``(start, end)`` for ``day``: the Ramadan window inside Ramadan, else the default."""
        if self.is_ramadan(day):
            window = self.outreach_window.ramadan.outreach_window
            return window.start, window.end
        return self.outreach_window.start, self.outreach_window.end

    def prayer_times_for(self, day: dt.date) -> dict[str, dt.datetime]:
        """The six computed times (tz-aware, in ``timezone``) for ``day``."""
        p = self.prayer_times
        return _prayer_times(p.latitude, p.longitude, p.method, self.timezone, day)

    def prayer_block_until(self, local: dt.datetime) -> dt.datetime | None:
        """If ``local`` sits inside ``[prayer - buffer, prayer + buffer)``, the end of that
        buffer; else ``None``. ``source: table`` has no table in phase 0 and blocks nothing.
        Like every other method, a naive ``local`` means Dubai-local."""
        local = self.localise(local)
        rule = self.outreach_window.prayer_times
        if not rule.block or rule.source != "computed":
            return None
        buffer = dt.timedelta(minutes=rule.buffer_minutes)
        for name, at in self.prayer_times_for(local.date()).items():
            if name == "sunrise":
                continue
            if at - buffer <= local < at + buffer:
                return at + buffer
        return None

    # --- the four rules

    def in_quiet_hours(self, local: dt.datetime) -> bool:
        """§12: 22:00–07:00 (wrapping midnight) with the shipped file."""
        local = self.localise(local)
        now, start, end = local.time(), self.quiet_hours.start, self.quiet_hours.end
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def in_outreach_window(self, local: dt.datetime) -> bool:
        """§9 §14: True when the Operator may send outreach at ``local``."""
        local = self.localise(local)
        day = local.date()
        if self.is_blocked_date(day):
            return False
        if any(slot.covers(local) for slot in self.outreach_window.blocked_weekday_slots):
            return False
        start, end = self.window_for(day)
        if not start <= local.time() < end:
            return False
        return self.prayer_block_until(local) is None

    def next_outreach_open(self, local: dt.datetime) -> dt.datetime:
        """The first instant >= ``local`` inside the outreach window (``local`` itself when it
        already is); this is ``TierDecision.deferred_until`` for an out-of-window send."""
        current = self.localise(local)
        for _ in range(10_000):
            if self.in_outreach_window(current):
                return current
            current = self._next_candidate(current)
        raise ValueError("no outreach slot found within the search horizon")  # pragma: no cover

    def _next_candidate(self, local: dt.datetime) -> dt.datetime:
        day = local.date()
        start, end = self.window_for(day)
        if self.is_blocked_date(day) or local.time() >= end:
            next_day = day + dt.timedelta(days=1)
            return self._at(next_day, self.window_for(next_day)[0])
        if local.time() < start:
            return self._at(day, start)
        for slot in self.outreach_window.blocked_weekday_slots:
            if slot.covers(local):
                return self._at(day, slot.end)
        until = self.prayer_block_until(local)
        if until is not None:
            return until
        return local + dt.timedelta(minutes=1)  # pragma: no cover - every block is handled above

    def next_quiet_end(self, local: dt.datetime) -> dt.datetime:
        """The next ``quiet_hours.end`` strictly after ``local`` (``parked_until`` of a message
        parked in quiet hours, DESIGN §2.6): 07:00 today before 07:00, else 07:00 tomorrow."""
        local = self.localise(local)
        candidate = self._at(local.date(), self.quiet_hours.end)
        if candidate <= local:
            candidate = self._at(local.date() + dt.timedelta(days=1), self.quiet_hours.end)
        return candidate

    def referenced_categories(self) -> frozenset[ActionCategory]:
        return frozenset(self.quiet_hours.emergency_categories)


# ---------------------------------------------------------------------------- channels.yaml


class OwnerThreadRules(ConfigModel):
    """§6 §9 the owner's WhatsApp thread."""

    channel: str
    desk: DeskScope
    use: list[str]
    allowed_senders: str
    passphrase_for_high_impact: bool


class WhatsAppRules(ConfigModel):
    """§9 §13 one WhatsApp Business line per coat; caps and the block-rate freeze."""

    desk: DeskScope
    use: list[str]
    cold_mass_messaging: bool
    template_messages_for_first_contact: bool
    daily_send_cap_start: int
    raise_cap_weekly_while_block_rate_under: float
    block_rate_freeze_threshold: float

    @field_validator("cold_mass_messaging")
    @classmethod
    def _never_cold_mass(cls, value: bool) -> bool:
        if value:
            raise ValueError("cold_mass_messaging must be false (SPEC §7: never cold mass)")
        return value


class EmailRules(ConfigModel):
    """§9 one address per coat on the company domain; cold caps and the warm-up schedule."""

    desk: DeskScope
    use: list[str]
    require_spf_dkim_dmarc: bool
    cold_daily_cap: int
    warmup_weeks: int
    warmup_schedule: list[int] = []
    consumer_mail_opt_out: str

    @model_validator(mode="after")
    def _schedule_matches_weeks(self) -> EmailRules:
        if self.warmup_schedule and len(self.warmup_schedule) != self.warmup_weeks:
            raise ValueError(
                f"warmup_schedule has {len(self.warmup_schedule)} entries for "
                f"{self.warmup_weeks} warm-up weeks"
            )
        if any(cap > self.cold_daily_cap for cap in self.warmup_schedule):
            raise ValueError("warmup_schedule caps may not exceed cold_daily_cap")
        return self


class OwnerMailboxRules(ConfigModel):
    """§9 delegated owner mailboxes: scopes ⊆ {read, draft, send}, never a password."""

    desk: DeskScope
    use: list[str]
    oauth_scopes: list[str]
    password_held: bool

    @field_validator("oauth_scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - {"read", "draft", "send"})
        if unknown:
            raise ValueError(f"oauth_scopes outside read/draft/send: {unknown}")
        return value

    @field_validator("password_held")
    @classmethod
    def _no_password(cls, value: bool) -> bool:
        if value:
            raise ValueError("password_held must be false (SPEC §2 hard rule 2)")
        return value


class VoiceLineRules(ConfigModel):
    """§9 the AI voice line per coat (phase 1 body)."""

    desk: DeskScope
    use: list[str]
    disclose_ai_on_request: bool
    record_only_where_lawful: bool
    transcripts_kept: bool
    audio_retention_days: int


class StaffLineRules(ConfigModel):
    """§9 §13 staff request lines: staff request, never command."""

    desk: DeskScope
    use: list[str]
    can_command: bool
    money_requires_owner_approval: bool

    @field_validator("can_command")
    @classmethod
    def _staff_never_command(cls, value: bool) -> bool:
        if value:
            raise ValueError("can_command must be false (SPEC §2: staff request, never command)")
        return value


class CadenceRules(ConfigModel):
    """§9 follow-up cadence."""

    default_follow_up_days: list[int]
    then_monthly: bool
    stop_on_any_reply: bool


class ConsentRules(ConfigModel):
    """§9 consent: a no anywhere is a no everywhere."""

    do_not_contact_shared_across_coats: bool
    consumer_messages_carry_opt_out: bool
    business_contacts_removal_in_one_message: bool


class ChannelWatchdog(ConfigModel):
    """§12 failed-send freeze threshold (mirrors spend_tiers.watchdog)."""

    failed_sends_per_hour_freeze: int


class ChannelsConfig(ConfigModel):
    """§9 §13 channels, caps and warm-up schedules."""

    owner_thread: OwnerThreadRules
    whatsapp_business: WhatsAppRules
    email: EmailRules
    owner_mailboxes: OwnerMailboxRules
    ai_voice_line: VoiceLineRules
    staff_request_lines: StaffLineRules
    cadences: CadenceRules
    consent: ConsentRules
    watchdog: ChannelWatchdog
    extra_channels: list[str] = []

    def known_channels(self) -> frozenset[Channel]:
        """``PHASE0_CHANNELS`` ∪ ``extra_channels`` (DESIGN §3.1: channels are yaml rows)."""
        return PHASE0_CHANNELS | frozenset(Channel(name) for name in self.extra_channels)


# ---------------------------------------------------------------------------- deputy.yaml


class DeputyContact(ConfigModel):
    """One ``contact_channels`` entry: ``{type, value}``."""

    type: str
    value: str


class DeputyIdentity(ConfigModel):
    """§14 who the deputy is (``<owner sets>`` until the owner fills it in)."""

    name: str
    contact_channels: list[DeputyContact] = []


class DeputyThresholds(ConfigModel):
    """§14 owner-silence thresholds in days."""

    owner_silent_days_pause_commitments: int
    owner_silent_days_activate_deputy: int

    @model_validator(mode="after")
    def _ordered(self) -> DeputyThresholds:
        if self.owner_silent_days_activate_deputy < self.owner_silent_days_pause_commitments:
            raise ValueError("deputy activation cannot come before commitments are paused")
        return self


class OwnerReturn(ConfigModel):
    """§14 how deputy mode ends."""

    ends_deputy_mode_on: str
    report: str


class DeputyConfig(ConfigModel):
    """§14 incapacity protocol."""

    deputy: DeputyIdentity
    thresholds: DeputyThresholds
    deputy_powers: list[str]
    deputy_never_gets: list[str]
    owner_return: OwnerReturn


# ---------------------------------------------------------------------------- coats/<slug>.yaml


class CoatIdentity(ConfigModel):
    """§5 coat identity: title, address, line, signature, letterhead, verification page."""

    title: str
    email: str
    whatsapp_line: str
    signature_ref: str
    letterhead_ref: str
    verification_page: str | None = None


class Mandate(ConfigModel):
    """§5 what she may offer under this coat and what only the owner signs."""

    price_floor_pct_of_list: int
    discount_max_pct: int
    payment_terms_allowed: list[str]
    templates_allowed: list[str]
    owner_only: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> Mandate:
        if not 0 <= self.discount_max_pct <= 100 or not 0 <= self.price_floor_pct_of_list <= 100:
            raise ValueError("percentages must be within 0..100")
        if self.discount_max_pct > 100 - self.price_floor_pct_of_list:
            raise ValueError(
                "discount_max_pct would take the price below price_floor_pct_of_list "
                "(SPEC §5: she never quotes below the floor)"
            )
        return self


class ApprovalRules(ConfigModel):
    """§6 per-coat tier rules; every category starts at ``default_new_category``."""

    default_new_category: ActionTier
    autonomous_categories: list[ActionCategory] = []
    notify_categories: list[ActionCategory] = []
    category_started_at: dict[ActionCategory, dt.date] = {}

    def referenced_categories(self) -> frozenset[ActionCategory]:
        return frozenset(
            list(self.autonomous_categories)
            + list(self.notify_categories)
            + list(self.category_started_at)
        )


class CoatChannels(ConfigModel):
    """§9 per-coat caps."""

    whatsapp_daily_cap: int
    email_cold_daily_cap: int
    warmup_weeks: int


class CoatConfig(ConfigModel):
    """§5 coat bundle; `slug` is the CoatId. ``tone_guide`` / ``knowledge_pack`` hold the contents
    of the files ``tone_guide_ref`` / ``knowledge_pack_ref`` point at (loaded by the loader)."""

    name: str
    slug: CoatId
    legal_entity: str
    desks_allowed: list[Desk] = Field(min_length=1)
    identity: CoatIdentity
    tone_guide_ref: str
    knowledge_pack_ref: str
    mandate: Mandate
    approval_rules: ApprovalRules
    allowed_activities: list[str]
    banking_ref: str
    channels: CoatChannels
    tone_guide: str
    knowledge_pack: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not value or value != value.lower() or any(ch.isspace() for ch in value):
            raise ValueError(f"slug must be a lower-case identifier, got {value!r}")
        return value

    @field_validator("banking_ref")
    @classmethod
    def _vault_ref(cls, value: str) -> str:
        if not value.startswith("vault://"):
            raise ValueError("banking_ref must be a vault:// reference (SPEC §10)")
        return value

    def allows(self, desk: Desk) -> bool:
        return desk in self.desks_allowed


# ---------------------------------------------------------------------------- models.yaml


_VENDOR_RE = re.compile(r"^[a-z][a-z0-9_]+$")


class ModelEndpoint(ConfigModel):
    """One model role: vendor is the adapter name (``anthropic``, ``openai``, ``self_hosted``…),
    never a substring of the model id (docs/adapters/models.md §3.5).

    ``vendor`` is normalised (stripped, lower-cased) and must be an adapter registry key
    (``^[a-z][a-z0-9_]+$``), so ``Anthropic``, ``anthropic `` and ``anthropic`` are one vendor
    and the clash check below cannot be slipped by a case or whitespace variant.
    """

    vendor: str
    model: str
    region: str | None = None

    @field_validator("vendor")
    @classmethod
    def _vendor_key(cls, value: str) -> str:
        key = value.strip().lower()
        if not _VENDOR_RE.match(key):
            raise ValueError(
                f"vendor must be an adapter name matching {_VENDOR_RE.pattern}, got {value!r}"
            )
        return key

    @field_validator("model")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class ModelsConfig(ConfigModel):
    """§4 §17: validator asserts fallback.vendor != primary.vendor AND auditor.vendor != primary.vendor."""

    primary: ModelEndpoint
    fallback: ModelEndpoint
    critic: ModelEndpoint
    auditor: ModelEndpoint
    private_tier2: ModelEndpoint | None = None

    def vendor_violations(self) -> list[str]:
        """Each vendor clash as one message (empty when the file is sound)."""
        out: list[str] = []
        if self.fallback.vendor == self.primary.vendor:
            out.append(
                f"fallback.vendor {self.fallback.vendor!r} must differ from primary.vendor "
                "(SPEC §4: a different vendor's model as fallback)"
            )
        if self.auditor.vendor == self.primary.vendor:
            out.append(
                f"auditor.vendor {self.auditor.vendor!r} must differ from primary.vendor "
                "(SPEC §12: the auditor runs on a different model)"
            )
        return out

    @model_validator(mode="after")
    def _vendors_differ(self) -> ModelsConfig:
        violations = self.vendor_violations()
        if violations:
            raise ValueError("; ".join(violations))
        return self


# ---------------------------------------------------------------------------- capabilities.yaml


class Capability(ConfigModel):
    """One §7 row. `tools` names the ToolSpecs (or routines) that implement it; `categories` the ActionCategory names."""

    id: str
    capability: str
    desk: DeskScope
    tier: str
    phase: int
    categories: list[ActionCategory]
    tools: list[str]
    routine: str | None = None
    notes: str | None = None

    @field_validator("phase")
    @classmethod
    def _phase(cls, value: int) -> int:
        if not 0 <= value <= 3:
            raise ValueError("phase must be 0..3 (SPEC §16)")
        return value


class CapabilitiesConfig(ConfigModel):
    """§7 the capability register as data; the source of every known ``ActionCategory``."""

    capabilities: list[Capability]

    @field_validator("capabilities")
    @classmethod
    def _unique_ids(cls, value: list[Capability]) -> list[Capability]:
        seen: set[str] = set()
        for cap in value:
            if cap.id in seen:
                raise ValueError(f"duplicate capability id {cap.id!r}")
            seen.add(cap.id)
        return value

    def categories(self) -> frozenset[ActionCategory]:
        return frozenset(cat for cap in self.capabilities for cat in cap.categories)

    def phase(self, phase: int) -> list[Capability]:
        return [cap for cap in self.capabilities if cap.phase == phase]

    def by_id(self, capability_id: str) -> Capability:
        for cap in self.capabilities:
            if cap.id == capability_id:
                return cap
        raise KeyError(capability_id)

    def tool_names(self) -> frozenset[str]:
        return frozenset(name for cap in self.capabilities for name in cap.tools)


# ---------------------------------------------------------------------------- constitution.md


class ChangeLogEntry(ConfigModel):
    """One row of the constitution change-log table (§2 amendment process)."""

    date: dt.date
    change: str
    confirmed_by: str
    constitution_hash: Hash | None = None


class Constitution(ConfigModel):
    """§2: the only hand-edited file. ``sha256`` is over the canonical hashed region defined in
    ``nour.config.loader`` (everything above the ``## Change log`` heading)."""

    text: str
    hard_rules: str
    changelog: list[ChangeLogEntry]
    sha256: Hash
    kill_section: str = ""

    def kill_phrases(self) -> frozenset[str]:
        """§12: fixed tokens parsed from the "Kill switch" section of constitution.md (the bullet
        list, one phrase per bullet, stripped). Matching/normalisation is ``detect_kill_command``'s."""
        phrases: list[str] = []
        for line in self.kill_section.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ")):
                phrase = stripped[2:].strip()
                if phrase:
                    phrases.append(phrase)
        return frozenset(phrases)

    @property
    def latest_entry(self) -> ChangeLogEntry | None:
        return self.changelog[-1] if self.changelog else None


# ---------------------------------------------------------------------------- the assembled config


class NourConfig(ConfigModel):
    """Everything under ``config/`` and ``prompts/`` after cross-validation (DESIGN §3.6)."""

    constitution: Constitution
    persona: str
    coats: dict[CoatId, CoatConfig]
    permissions: PermissionsConfig
    spend_tiers: SpendTiersConfig
    channels: ChannelsConfig
    calendar: CalendarConfig
    deputy: DeputyConfig
    models: ModelsConfig
    capabilities: CapabilitiesConfig
    prompts: dict[str, str]
    config_hash: Hash

    HASH_PLACEHOLDER: ClassVar[Hash] = Hash("sha256:" + "0" * 64)

    def coat(self, coat_id: CoatId) -> CoatConfig:
        """``KeyError`` → the gate refuses ``UNKNOWN_COAT``."""
        return self.coats[coat_id]

    def known_categories(self) -> frozenset[ActionCategory]:
        """capabilities ∪ high_impact ∪ ask_every_time ∪ always_K ∪ coat rules ∪ emergency
        (after a clean load these are all subsets of ``capabilities.categories()``)."""
        known: set[ActionCategory] = set(self.capabilities.categories())
        known |= self.permissions.referenced_categories()
        known |= self.spend_tiers.referenced_categories()
        known |= self.calendar.referenced_categories()
        for coat in self.coats.values():
            known |= coat.approval_rules.referenced_categories()
        return frozenset(known)

    def coats_for(self, desk: Desk) -> list[CoatConfig]:
        return [coat for coat in self.coats.values() if coat.allows(desk)]
