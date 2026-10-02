"""Core types (DESIGN §3.1 ``nour/core/types.py``; SPEC §4 §5 §6 §10).

Everything that crosses a module boundary as a *value* lives here: the id and label NewTypes,
the phase-0 channel constants, every closed enum, :class:`Money` (integer fils), :class:`Reason`
(the one-sentence audit reason of SPEC §2 §12) and :class:`SafeStr` (text that passed the
``LeakGuard``). ``Channel``, ``ActionCategory`` and ``BudgetHolder`` are validated strings, never
closed enums, so phases 1–3 add yaml rows without reopening this file.

The ``SafeStr`` mint mechanism, for the engineers who match it
--------------------------------------------------------------
``SafeStr.__new__(cls, text, *, _minted_by)`` raises ``Tier2LeakError`` unless ``_minted_by``
*is* the private sentinel ``_MINT`` defined in this module (identity, not equality, so no copy or
equal-looking object passes). ``nour/core/leakguard.py`` imports ``_MINT`` and is the **only**
module that calls ``SafeStr(...)``: ``LeakGuard.safe()`` / ``redact()`` / ``safe_mapping()`` mint
after a fingerprint scan. ``tests/unit/test_walls.py`` asserts the call ``SafeStr(`` appears in no
other file. Pydantic models declare fields as ``SafeStr`` and get an *instance-only* schema: a
plain ``str`` is rejected at validation time rather than silently promoted, so a sink typed on
``SafeStr`` cannot be fed unscrubbed text through a model constructor either.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import IntEnum, StrEnum
from typing import Any, NewType

from pydantic import BaseModel, field_validator
from pydantic_core import core_schema

from nour.core.errors import CurrencyMismatch, ReasonError, Tier2LeakError

# --------------------------------------------------------------------------- ids and labels

Ulid = NewType("Ulid", str)
"""26-char Crockford ULID, minted by ``clock.IdGenerator`` (never by the database)."""

CoatId = NewType("CoatId", str)
"""Coat slug, e.g. ``"buzz-avenue"`` (``config/coats/<slug>.yaml``)."""

Hash = NewType("Hash", str)
"""``"sha256:<hex>"`` (content) or ``"hmac:<hex>"`` (keyed; every Tier 2 hash, SPEC §10)."""

Channel = NewType("Channel", str)
"""Validated against ``channels.yaml`` at load; the phase-0 constants are below."""

ActionCategory = NewType("ActionCategory", str)
"""Validated against ``capabilities.yaml`` + ``permissions.yaml`` at load."""

BudgetHolder = NewType("BudgetHolder", str)
"""Keys of ``spend_tiers.monthly_cap``: ``"operator"``, ``"assistant_logistics"``,
``"ai_models_within_operator"``, later ``"subagent:<id>"``."""

# Channel constants (phase 0 set). New channels are yaml rows plus a constant where code must
# route them (DESIGN §3.1).
OWNER_WHATSAPP = Channel("owner_whatsapp")
COAT_WHATSAPP = Channel("coat_whatsapp")
COAT_EMAIL = Channel("coat_email")
OWNER_MAILBOX = Channel("owner_mailbox")
STAFF_LINE = Channel("staff_line")
PHONE_NOTIFICATION = Channel("phone_notification")
SECOND_CHANNEL = Channel("second_channel")
TIMER = Channel("timer")
INTERNAL = Channel("internal")  # approvals, handoffs, read-back re-entries

PHASE0_CHANNELS: frozenset[Channel] = frozenset(
    {
        OWNER_WHATSAPP,
        COAT_WHATSAPP,
        COAT_EMAIL,
        OWNER_MAILBOX,
        STAFF_LINE,
        PHONE_NOTIFICATION,
        SECOND_CHANNEL,
        TIMER,
        INTERNAL,
    }
)


# --------------------------------------------------------------------------- enums


class Desk(StrEnum):
    """SPEC §5: the two sealed desks plus the governance processes (ingress, scheduler, CLI)."""

    OPERATOR = "operator"
    ASSISTANT = "assistant"
    GOVERNANCE = "governance"


class DeskScope(StrEnum):
    """ToolSpec.desk (SPEC §7 'Desk' column)."""

    OPERATOR = "operator"
    ASSISTANT = "assistant"
    BOTH = "both"
    GOVERNANCE = "governance"


class ActionTier(StrEnum):
    """SPEC §6 action tiers: A autonomous < N notify < K ask-first.

    ``StrEnum`` members compare as strings, where ``"K" < "N"`` is the wrong order; use
    :meth:`highest` / :attr:`rank`, never ``<`` on the members.
    """

    A = "A"
    N = "N"
    K = "K"

    @property
    def rank(self) -> int:
        """0 for A, 1 for N, 2 for K."""
        return _TIER_RANK[self]

    @staticmethod
    def highest(*tiers: ActionTier) -> ActionTier:
        """The most restrictive of ``tiers`` (A < N < K).

        SPEC §6 / DESIGN §4c: the ``TierResolver`` only ever raises a tier, so every rule folds its
        result in through this function. Raises ``ValueError`` when called with no tiers.
        """
        if not tiers:
            raise ValueError("ActionTier.highest needs at least one tier")
        return max((ActionTier(tier) for tier in tiers), key=_TIER_RANK.__getitem__)


_TIER_RANK: dict[ActionTier, int] = {ActionTier.A: 0, ActionTier.N: 1, ActionTier.K: 2}


class DataTier(IntEnum):
    """SPEC §6 data tiers: 0 professional, 1 private ops, 2 vault, 3 never held."""

    T0 = 0
    T1 = 1
    T2 = 2
    T3 = 3


class Authority(StrEnum):
    """§2: only the owner commands; staff request; everything else is data."""

    OWNER = "owner"
    STAFF_REQUEST = "staff_request"
    DATA = "data"
    SYSTEM = "system"


class Origin(StrEnum):
    """SPEC §6: voice is never identity, so the origin of a message travels with it."""

    TEXT = "text"
    VOICE = "voice"
    SYSTEM = "system"


class SourceKind(StrEnum):
    """SPEC §4 Ingest: where an inbox event came from."""

    WHATSAPP = "whatsapp"
    EMAIL = "email"
    PHONE_NOTIFICATION = "phone_notification"
    TIMER = "timer"
    APPROVAL_DECISION = "approval_decision"
    HANDOFF = "handoff"
    SECOND_CHANNEL = "second_channel"
    STAFF_LINE = "staff_line"
    READBACK = "readback"


class EventKind(StrEnum):
    """SPEC §4: events are messages, notifications, timers and approvals (plus the gate,
    second-channel and read-back re-entries)."""

    MESSAGE = "message"
    VOICE_NOTE = "voice_note"
    NOTIFICATION = "notification"
    TIMER = "timer"
    APPROVAL = "approval"
    HANDOFF = "handoff"
    SECOND_CHANNEL = "second_channel"
    STAFF_REQUEST = "staff_request"
    READBACK = "readback"


class Actor(StrEnum):
    """SPEC §12 audit ``actor``: her, a sub-agent, the owner, the auditor, the system, the deputy."""

    NOUR = "nour"
    SUBAGENT = "subagent"
    OWNER = "owner"
    AUDITOR = "auditor"
    SYSTEM = "system"
    DEPUTY = "deputy"


class ActionStatus(StrEnum):
    """Audit `status` (§12). OPENED is the write-ahead row; every other value is terminal."""

    OPENED = "opened"
    EXECUTED = "executed"
    NOTIFIED = "notified"
    QUEUED = "queued"
    REFUSED = "refused"
    DECLINED = "declined"
    FAILED = "failed"
    READBACK_PENDING = "readback_pending"
    FROZEN = "frozen"
    DEFERRED = "deferred"
    DRY_RUN = "dry_run"
    OBSERVED = "observed"
    RELEASED = "released"


class RefusalCode(StrEnum):
    """SPEC §5 §6 §7: why the gate refused; logged on the ``REFUSED`` audit row."""

    NO_COAT = "no_coat"
    UNKNOWN_COAT = "unknown_coat"
    DESK_NOT_ALLOWED_FOR_COAT = "desk_not_allowed_for_coat"
    ACTIVITY_NOT_ALLOWED = "activity_not_allowed"
    TOOL_NOT_IN_DESK = "tool_not_in_desk"
    UNKNOWN_TOOL = "unknown_tool"
    BAD_ARGS = "bad_args"
    BAD_REASON = "bad_reason"
    FROZEN = "frozen"
    LEAK = "leak"
    NOT_IN_PHASE = "not_in_phase"
    DNC = "do_not_contact"


class FreezeScope(StrEnum):
    """SPEC §12 incident playbook: what a freeze covers."""

    HIGH_IMPACT = "high_impact"
    AUTONOMOUS = "autonomous"
    CHANNEL = "channel"
    COAT_OUTGOING = "coat_outgoing"
    VAULT_SHARING = "vault_sharing"
    CATEGORY = "category"
    ALL_OUTBOUND = "all_outbound"


class IncidentType(StrEnum):
    """SPEC §12 incident playbook rows plus the watchdog and kill-switch incidents."""

    SUSPICIOUS_PAYMENT = "suspicious_payment"
    BAD_MESSAGE = "bad_message"
    DATA_LEAK = "data_leak"
    CHANNEL_BANNED = "channel_banned"
    INSTRUCTION_IN_CONTENT = "instruction_in_content"
    MODEL_OUTAGE = "model_outage"
    AUTH_FAILURE = "auth_failure"
    IMPERSONATION = "impersonation"
    WATCHDOG_LOOP = "watchdog_loop"
    WATCHDOG_SPEND = "watchdog_spend"
    WATCHDOG_FAILED_SENDS = "watchdog_failed_sends"
    KILL_SWITCH = "kill_switch"


class MemoryKind(StrEnum):
    """SPEC §8 memory stores (``memory_record.store``)."""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    OWNER_PROFILE = "owner_profile"


class PassphraseOutcome(StrEnum):
    """§6 §13: every way a passphrase attempt can go; one row each in passphrase_attempt."""

    OK = "ok"
    WRONG = "wrong"
    SPOKEN = "spoken"
    WRONG_THREAD = "wrong_thread"
    SPOOFED_NUMBER = "spoofed_number"
    SPOOF_SUSPECTED = "spoof_suspected"
    REPLAYED = "replayed"


# --------------------------------------------------------------------------- Money

_FILS_PER_UNIT = 100
_CENT = Decimal("0.01")


class Money(BaseModel, frozen=True):
    """§10: integer minor units; AED only in phase 0 (currency validated against spend_tiers.currency).

    ``fils`` is a signed integer (refunds and P&L need negatives); there is no float anywhere.
    Arithmetic and ordering between two different currencies raise :class:`CurrencyMismatch`;
    equality between different currencies is simply ``False``.
    """

    fils: int
    currency: str = "AED"

    @field_validator("currency")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        if len(value) != 3 or not value.isascii() or not value.isalpha() or not value.isupper():
            raise ValueError(f"currency must be a 3-letter upper-case ISO code, got {value!r}")
        return value

    @classmethod
    def aed(cls, amount: Decimal | int | str) -> Money:
        """``Money.aed("12.34")`` → 1234 fils. Rejects floats and anything finer than a fils."""
        if isinstance(amount, bool | float):
            raise TypeError("Money.aed takes Decimal, int or str, never float or bool")
        try:
            value = Decimal(amount)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"not a money amount: {amount!r}") from exc
        if not value.is_finite():
            raise ValueError(f"not a money amount: {amount!r}")
        fils = value * _FILS_PER_UNIT
        if fils != fils.to_integral_value():
            raise ValueError(f"{amount!r} is finer than one fils")
        return cls(fils=int(fils), currency="AED")

    @classmethod
    def zero(cls, currency: str = "AED") -> Money:
        return cls(fils=0, currency=currency)

    def as_decimal(self) -> Decimal:
        """Major units with exactly two decimals (``Decimal("12.34")``)."""
        return (Decimal(self.fils) / _FILS_PER_UNIT).quantize(_CENT)

    def _same(self, other: object, op: str) -> Money:
        if not isinstance(other, Money):
            raise TypeError(f"unsupported operand for Money {op}: {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} {op} {other.currency}")
        return other

    def __add__(self, other: Money) -> Money:
        return Money(fils=self.fils + self._same(other, "+").fils, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        return Money(fils=self.fils - self._same(other, "-").fils, currency=self.currency)

    def __neg__(self) -> Money:
        return Money(fils=-self.fils, currency=self.currency)

    def __abs__(self) -> Money:
        return Money(fils=abs(self.fils), currency=self.currency)

    def times(self, factor: int) -> Money:
        """Integer multiple (``cap.times(3)``); there is no fractional multiplication."""
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise TypeError("Money.times takes an int factor")
        return Money(fils=self.fils * factor, currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        return self.fils < self._same(other, "<").fils

    def __le__(self, other: Money) -> bool:
        return self.fils <= self._same(other, "<=").fils

    def __gt__(self, other: Money) -> bool:
        return self.fils > self._same(other, ">").fils

    def __ge__(self, other: Money) -> bool:
        return self.fils >= self._same(other, ">=").fils

    def __str__(self) -> str:
        return f"{self.currency} {self.as_decimal():,.2f}"


# --------------------------------------------------------------------------- Reason

REASON_MIN = 3
REASON_MAX = 240
REASON_TERMINATORS = frozenset({".", "!", "?", "؟", "۔"})
"""Sentence terminators: ``.`` ``!`` ``?`` plus the Arabic question mark (U+061F) and the Arabic
full stop (U+06D4)."""
_NEWLINES = ("\n", "\r")


class Reason(str):
    """§2 §12: the one-sentence reason. 3..240 chars, no newline, at most one sentence terminator
    ('.', '!', '?', '؟', '۔') and it must be the last character if present. Raises ReasonError.

    The minimum counts non-blank characters (``"   "`` is not a reason); the maximum counts all.
    """

    __slots__ = ()

    def __new__(cls, text: str) -> Reason:
        if isinstance(text, Reason):
            return text
        if not isinstance(text, str):
            raise ReasonError(f"reason must be a str, got {type(text).__name__}")
        length = len(text)
        if len(text.strip()) < REASON_MIN:
            raise ReasonError(f"reason too short ({len(text.strip())} < {REASON_MIN} chars)")
        if length > REASON_MAX:
            raise ReasonError(f"reason too long ({length} > {REASON_MAX} chars)")
        if any(nl in text for nl in _NEWLINES):
            raise ReasonError("reason must be one line")
        terminators = [i for i, ch in enumerate(text) if ch in REASON_TERMINATORS]
        if len(terminators) > 1:
            raise ReasonError("reason must be one sentence (more than one terminator)")
        if terminators and terminators[0] != length - 1:
            raise ReasonError("reason must be one sentence (terminator before the end)")
        return super().__new__(cls, text)

    @classmethod
    def coerce(cls, text: str | None) -> Reason | None:
        """Best-effort one-sentence reason from model output (DESIGN §10: truncate, don't refuse).

        Trims, cuts at the first terminator (keeping it) or at the first line break if that comes
        earlier, trims again; returns ``None`` when nothing is left and raises :class:`ReasonError`
        when what is left is still not a valid reason (too short, too long).
        """
        if text is None:
            return None
        if not isinstance(text, str):
            raise ReasonError(f"reason must be a str, got {type(text).__name__}")
        stripped = text.strip()
        if not stripped:
            return None
        cut = len(stripped)
        for i, ch in enumerate(stripped):
            if ch in _NEWLINES:
                cut = i
                break
            if ch in REASON_TERMINATORS:
                cut = i + 1
                break
        first = stripped[:cut].strip()
        if not first or first in REASON_TERMINATORS:
            return None
        return cls(first)

    @classmethod
    def _validate(cls, value: object) -> Reason:
        if isinstance(value, Reason):
            return value
        if isinstance(value, str):
            return cls(value)
        raise ReasonError(f"reason must be a str, got {type(value).__name__}")

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        """Pydantic fields typed ``Reason`` accept a ``str`` and validate it (``ReasonError`` is a
        ``ValueError``, so it surfaces as a ``ValidationError``)."""
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            json_schema_input_schema=core_schema.str_schema(
                min_length=REASON_MIN, max_length=REASON_MAX
            ),
            serialization=core_schema.to_string_ser_schema(),
        )


# --------------------------------------------------------------------------- SafeStr

_MINT: object = object()
"""The private mint sentinel. ``nour/core/leakguard.py`` imports it; nothing else passes it.
Identity-compared in ``SafeStr.__new__`` (``_minted_by is _MINT``)."""


class SafeStr(str):
    """A str that passed LeakGuard. Constructible only by LeakGuard.safe()/redact() (constructor checks
    a module-private token); any other construction raises Tier2LeakError. Every sink Nour writes is typed on it."""

    __slots__ = ()

    def __new__(cls, text: str, *, _minted_by: object) -> SafeStr:
        if _minted_by is not _MINT:
            raise Tier2LeakError("SafeStr may only be minted by nour.core.leakguard.LeakGuard")
        if not isinstance(text, str):
            raise TypeError(f"SafeStr wraps a str, got {type(text).__name__}")
        return super().__new__(cls, text)

    @staticmethod
    def _refuse_json(value: object) -> SafeStr:
        raise ValueError(
            "SafeStr cannot be validated from JSON; scrub the text with LeakGuard.safe() first"
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> core_schema.CoreSchema:
        """Instance-only: a pydantic field typed ``SafeStr`` rejects a plain ``str`` (Python mode)
        and any JSON input (there is no trusted text in a JSON document). Pydantic's own ``str``
        schema would silently downgrade or promote subclasses, which is exactly the wall this type
        exists for (DESIGN §4b). The JSON *schema* still renders as a string so tool and document
        schemas can be generated."""
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_plain_validator_function(
                cls._refuse_json, json_schema_input_schema=core_schema.str_schema()
            ),
            python_schema=core_schema.is_instance_schema(
                cls, cls_repr="SafeStr (minted by LeakGuard)"
            ),
            serialization=core_schema.to_string_ser_schema(),
        )
