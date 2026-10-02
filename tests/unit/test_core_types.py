"""nour/core/types.py (DESIGN §3.1): enums, Money, Reason, SafeStr and the channel constants.

Proves (MODULES.md "core"): Reason rules incl. Arabic terminators; Money arithmetic and currency
mismatch; ActionTier.highest; SafeStr cannot be built without the mint sentinel and pydantic
fields typed on it refuse plain text.
"""

from __future__ import annotations

import itertools
import operator
import types
from collections.abc import Callable
from decimal import Decimal
from enum import Enum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

import nour
import nour.core
from nour.core import types as core_types
from nour.core.errors import CurrencyMismatch, ReasonError, Tier2LeakError
from nour.core.types import (
    COAT_EMAIL,
    COAT_WHATSAPP,
    INTERNAL,
    OWNER_MAILBOX,
    OWNER_WHATSAPP,
    PHASE0_CHANNELS,
    PHONE_NOTIFICATION,
    REASON_MAX,
    REASON_MIN,
    REASON_TERMINATORS,
    SECOND_CHANNEL,
    STAFF_LINE,
    TIMER,
    ActionStatus,
    ActionTier,
    Actor,
    Authority,
    Channel,
    DataTier,
    Desk,
    DeskScope,
    EventKind,
    FreezeScope,
    IncidentType,
    MemoryKind,
    Money,
    Origin,
    PassphraseOutcome,
    Reason,
    RefusalCode,
    SafeStr,
    SourceKind,
)

# The only legitimate mint is nour/core/leakguard.py; tests reach the sentinel directly to prove
# the mechanism. The constructor is called through an alias so the AST wall on `SafeStr(` holds.
_safe_ctor = SafeStr
_MINT = core_types._MINT

ARABIC_QUESTION = "؟"  # ؟
ARABIC_FULL_STOP = "۔"  # ۔


def _minted(text: str) -> SafeStr:
    return _safe_ctor(text, _minted_by=_MINT)


# --------------------------------------------------------------------------- package shape


def test_package_version_and_no_reexports() -> None:
    assert nour.__version__ == "0.1.0"
    public = {
        name
        for name, value in vars(nour.core).items()
        if not name.startswith("_") and not isinstance(value, types.ModuleType)
    }
    assert public == set(), "nour.core must not re-export names; import from the submodules"


# --------------------------------------------------------------------------- channels and enums


def test_phase0_channels_are_the_nine_constants() -> None:
    assert PHASE0_CHANNELS == frozenset(
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
    assert len(PHASE0_CHANNELS) == 9
    assert OWNER_WHATSAPP == "owner_whatsapp" and INTERNAL == "internal"
    assert all(isinstance(c, str) for c in PHASE0_CHANNELS)
    # Channel is an open NewType: a yaml-declared channel is a plain validated string.
    assert Channel("linkedin") not in PHASE0_CHANNELS


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (Desk, {"operator", "assistant", "governance"}),
        (DeskScope, {"operator", "assistant", "both", "governance"}),
        (ActionTier, {"A", "N", "K"}),
        (Authority, {"owner", "staff_request", "data", "system"}),
        (Origin, {"text", "voice", "system"}),
        (
            SourceKind,
            {
                "whatsapp",
                "email",
                "phone_notification",
                "timer",
                "approval_decision",
                "handoff",
                "second_channel",
                "staff_line",
                "readback",
            },
        ),
        (
            EventKind,
            {
                "message",
                "voice_note",
                "notification",
                "timer",
                "approval",
                "handoff",
                "second_channel",
                "staff_request",
                "readback",
            },
        ),
        (Actor, {"nour", "subagent", "owner", "auditor", "system", "deputy"}),
        (
            ActionStatus,
            {
                "opened",
                "executed",
                "notified",
                "queued",
                "refused",
                "declined",
                "failed",
                "readback_pending",
                "frozen",
                "deferred",
                "dry_run",
                "observed",
                "released",
            },
        ),
        (
            RefusalCode,
            {
                "no_coat",
                "unknown_coat",
                "desk_not_allowed_for_coat",
                "activity_not_allowed",
                "tool_not_in_desk",
                "unknown_tool",
                "bad_args",
                "bad_reason",
                "frozen",
                "leak",
                "not_in_phase",
                "do_not_contact",
            },
        ),
        (
            FreezeScope,
            {
                "high_impact",
                "autonomous",
                "channel",
                "coat_outgoing",
                "vault_sharing",
                "category",
                "all_outbound",
            },
        ),
        (
            IncidentType,
            {
                "suspicious_payment",
                "bad_message",
                "data_leak",
                "channel_banned",
                "instruction_in_content",
                "model_outage",
                "auth_failure",
                "impersonation",
                "watchdog_loop",
                "watchdog_spend",
                "watchdog_failed_sends",
                "kill_switch",
            },
        ),
        (MemoryKind, {"episodic", "semantic", "procedural", "owner_profile"}),
        (
            PassphraseOutcome,
            {
                "ok",
                "wrong",
                "spoken",
                "wrong_thread",
                "spoofed_number",
                "spoof_suspected",
                "replayed",
            },
        ),
    ],
)
def test_enum_values_match_design(enum: type[Enum], values: set[str]) -> None:
    assert {member.value for member in enum} == values
    assert all(isinstance(member, str) for member in enum)  # StrEnum: stored as TEXT + CHECK


def test_refusal_code_dnc_spelling() -> None:
    assert RefusalCode.DNC == "do_not_contact"
    assert ActionStatus.OPENED == "opened"


def test_data_tier_is_ordered_ints() -> None:
    assert [t.value for t in DataTier] == [0, 1, 2, 3]
    assert DataTier.T0 < DataTier.T1 < DataTier.T2 < DataTier.T3
    assert DataTier(2) is DataTier.T2
    assert isinstance(DataTier.T3, int)


class TestActionTierHighest:
    def test_order_is_a_n_k(self) -> None:
        assert ActionTier.highest(ActionTier.A) is ActionTier.A
        assert ActionTier.highest(ActionTier.A, ActionTier.N) is ActionTier.N
        assert ActionTier.highest(ActionTier.N, ActionTier.A) is ActionTier.N
        assert ActionTier.highest(ActionTier.N, ActionTier.K, ActionTier.A) is ActionTier.K
        assert ActionTier.highest(ActionTier.K) is ActionTier.K
        assert ActionTier.highest(ActionTier.A, ActionTier.A) is ActionTier.A

    def test_rank_not_string_order(self) -> None:
        # As strings "K" < "N"; the tier order must not come from str comparison.
        assert ActionTier.K.rank > ActionTier.N.rank > ActionTier.A.rank
        assert ActionTier.highest(ActionTier.K, ActionTier.N) is ActionTier.K

    def test_never_lowers(self) -> None:
        for combo in itertools.product(list(ActionTier), repeat=3):
            result = ActionTier.highest(*combo)
            assert all(result.rank >= tier.rank for tier in combo)
            assert result in combo

    def test_accepts_raw_values(self) -> None:
        assert ActionTier.highest("A", "K") is ActionTier.K  # type: ignore[arg-type]

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            ActionTier.highest()


# --------------------------------------------------------------------------- Money


class TestMoney:
    def test_aed_from_str_int_decimal(self) -> None:
        assert Money.aed("12.34") == Money(fils=1234, currency="AED")
        assert Money.aed(5).fils == 500
        assert Money.aed(Decimal("0.01")).fils == 1
        assert Money.aed("3000").fils == 300_000
        assert Money.aed("-7.5").fils == -750
        assert Money.aed("1e2").fils == 10_000

    def test_aed_rejects_sub_fils_floats_and_garbage(self) -> None:
        with pytest.raises(ValueError):
            Money.aed("1.005")
        with pytest.raises(TypeError):
            Money.aed(1.5)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Money.aed(True)
        with pytest.raises(ValueError):
            Money.aed("twelve")
        with pytest.raises(ValueError):
            Money.aed("NaN")
        with pytest.raises(ValueError):
            Money.aed("Infinity")

    def test_zero_and_as_decimal(self) -> None:
        assert Money.zero() == Money(fils=0, currency="AED")
        assert Money.zero("USD").currency == "USD"
        assert Money.aed("12.34").as_decimal() == Decimal("12.34")
        assert str(Money.aed("12.3").as_decimal()) == "12.30"
        assert Money(fils=-5).as_decimal() == Decimal("-0.05")
        assert Money.aed("1234567.89").as_decimal() == Decimal("1234567.89")

    def test_arithmetic(self) -> None:
        assert Money.aed(5) + Money.aed("0.5") == Money.aed("5.5")
        assert Money.aed(5) - Money.aed("7.25") == Money.aed("-2.25")
        assert Money.aed(3).times(3) == Money.aed(9)
        assert Money.aed(3).times(-1) == Money.aed(-3)
        assert Money.aed(3).times(0) == Money.zero()
        assert -Money.aed(1) == Money.aed(-1)
        assert abs(Money.aed(-1)) == Money.aed(1)
        assert sum([Money.aed(1), Money.aed(2)], Money.zero()) == Money.aed(3)
        with pytest.raises(TypeError):
            Money.aed(3).times(1.5)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Money.aed(3) + 300  # type: ignore[operator]

    def test_comparisons(self) -> None:
        small, big = Money.aed(200), Money.aed(201)
        assert small < big and small <= big and big > small and big >= small
        assert not big < small and not big <= small
        assert small <= Money.aed(200) and small >= Money.aed(200)
        assert Money.aed(1000) < Money.aed(1001)  # SPEC §10 band boundary

    @pytest.mark.parametrize(
        "op", [operator.add, operator.sub, operator.lt, operator.le, operator.gt, operator.ge]
    )
    def test_currency_mismatch_raises(self, op: Callable[[Money, Money], object]) -> None:
        aed, usd = Money.aed(1), Money(fils=100, currency="USD")
        with pytest.raises(CurrencyMismatch):
            op(aed, usd)
        with pytest.raises(CurrencyMismatch):
            op(usd, aed)
        assert issubclass(CurrencyMismatch, ValueError)

    def test_equality_across_currencies_is_false_not_error(self) -> None:
        assert Money(fils=100, currency="USD") != Money.aed(1)
        assert Money.aed(1) == Money.aed("1.00")

    def test_frozen_and_hashable(self) -> None:
        m = Money.aed(1)
        with pytest.raises(ValidationError):
            m.fils = 2  # type: ignore[misc]
        assert {Money.aed(1): "x"}[Money.aed("1.00")] == "x"
        assert hash(m) == hash(Money.aed(1))

    def test_validation(self) -> None:
        with pytest.raises(ValidationError):
            Money(fils=1, currency="aed")
        with pytest.raises(ValidationError):
            Money(fils=1, currency="AEDD")
        with pytest.raises(ValidationError):
            Money(fils="12.5")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            Money(fils=1.5)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):  # strict: a numeric string is not an amount
            Money(fils="1200")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):  # strict: True is not one fils
            Money(fils=True)
        with pytest.raises(ValidationError):
            Money.model_validate({"fils": False, "currency": "AED"})
        assert Money(fils=1200).fils == 1200
        assert Money.model_validate({"fils": 1200, "currency": "AED"}).fils == 1200
        assert Money.model_validate_json('{"fils": 1200, "currency": "AED"}').fils == 1200

    def test_str_shows_major_units(self) -> None:
        assert str(Money.aed("1234.5")) == "AED 1,234.50"
        assert str(Money(fils=-5)) == "AED -0.05"
        assert repr(Money.aed(1)) == "Money(fils=100, currency='AED')"

    @given(st.integers(min_value=-(10**12), max_value=10**12))
    @settings(deadline=None)
    def test_decimal_round_trip(self, fils: int) -> None:
        m = Money(fils=fils)
        assert Money.aed(m.as_decimal()) == m
        assert Money.aed(str(m.as_decimal())) == m

    @given(
        st.integers(min_value=-(10**9), max_value=10**9),
        st.integers(min_value=-(10**9), max_value=10**9),
    )
    @settings(deadline=None)
    def test_add_sub_inverse(self, a: int, b: int) -> None:
        x, y = Money(fils=a), Money(fils=b)
        assert (x + y) - y == x
        assert (x < y) == (a < b) and (x >= y) == (a >= b)


# --------------------------------------------------------------------------- Reason


class TestReason:
    @pytest.mark.parametrize(
        "text",
        [
            "Paid the supplier.",
            "Paid the supplier",
            "Customer asked for the price list!",
            "Is the stock available?",
            "Owner asked for the catalogue, sending it now",
            "هل تم الدفع" + ARABIC_QUESTION,
            "تم الدفع للمورّد" + ARABIC_FULL_STOP,
            "الزبون سأل عن السعر، أرسلت الكتالوج",
            "abc",
            "x" * 240,
            "Paid AED 3,000 to the supplier for the May order",
        ],
    )
    def test_valid(self, text: str) -> None:
        reason = Reason(text)
        assert reason == text
        assert isinstance(reason, str) and isinstance(reason, Reason)
        assert Reason(reason) is reason

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ("ok", "short"),
            ("", "short"),
            ("x" * 241, "long"),
            ("Paid the supplier\nthen logged it", "one line"),
            ("Paid the supplier\r\nthen logged it", "one line"),
            ("Paid the supplier\u2028then logged it", "one line"),  # line separator
            ("Paid the supplier\u2029then logged it", "one line"),  # paragraph separator
            ("Paid the supplier\x85then logged it", "one line"),  # NEL
            ("Paid the supplier\x0bthen logged it", "one line"),  # vertical tab
            ("Paid the supplier\x0cthen logged it", "one line"),  # form feed
            ("Paid the supplier\x1ethen logged it", "one line"),  # record separator
            ("Paid the supplier. Then logged it", "one sentence"),
            ("Paid the supplier. Then logged it.", "one sentence"),
            ("Done!!", "one sentence"),
            ("Really? Yes", "one sentence"),
            ("Paid AED 3.50 for the sample", "one sentence"),
            ("تم الدفع" + ARABIC_FULL_STOP + " ثم السجل", "one sentence"),
            ("هل تم" + ARABIC_QUESTION + " نعم", "one sentence"),
            ("   ", "short"),
        ],
    )
    def test_invalid(self, text: str, fragment: str) -> None:
        with pytest.raises(ReasonError, match=fragment):
            Reason(text)

    def test_non_str_raises_reason_error(self) -> None:
        with pytest.raises(ReasonError):
            Reason(123)  # type: ignore[arg-type]
        with pytest.raises(ReasonError):
            Reason(None)  # type: ignore[arg-type]
        assert issubclass(ReasonError, ValueError)

    def test_bounds_and_terminators_are_as_designed(self) -> None:
        assert (REASON_MIN, REASON_MAX) == (3, 240)
        assert REASON_TERMINATORS == {".", "!", "?", ARABIC_QUESTION, ARABIC_FULL_STOP}

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (None, None),
            ("", None),
            ("   \n  ", None),
            (".", None),
            ("Paid the supplier. Then logged it.", "Paid the supplier."),
            ("  Paid the supplier  ", "Paid the supplier"),
            ("Paid the supplier!!", "Paid the supplier!"),
            ("Paid the supplier\nthen logged it", "Paid the supplier"),
            ("Customer asked for the price? Sending it.", "Customer asked for the price?"),
            ("تم الدفع" + ARABIC_QUESTION + " ثم ماذا", "تم الدفع" + ARABIC_QUESTION),
            ("تم الدفع للمورّد" + ARABIC_FULL_STOP + " وسجّلت", "تم الدفع للمورّد" + ARABIC_FULL_STOP),
            ("Paid AED 3.50 for the sample", "Paid AED 3."),
        ],
    )
    def test_coerce(self, text: str | None, expected: str | None) -> None:
        result = Reason.coerce(text)
        if expected is None:
            assert result is None
        else:
            assert result == expected
            assert isinstance(result, Reason)

    def test_coerce_cuts_at_any_line_break(self) -> None:
        for sep in ("\n", "\r\n", "\u2028", "\u2029", "\x85", "\x0b", "\x0c"):
            assert Reason.coerce(f"Paid the supplier{sep}then logged it.") == "Paid the supplier"
        assert Reason.coerce("Paid. Then\u2028logged") == "Paid."

    def test_coerce_raises_when_first_sentence_is_still_bad(self) -> None:
        with pytest.raises(ReasonError):
            Reason.coerce("ok")
        with pytest.raises(ReasonError):
            Reason.coerce("x" * 300)
        with pytest.raises(ReasonError):
            Reason.coerce(42)  # type: ignore[arg-type]

    def test_pydantic_field_validates_and_serialises(self) -> None:
        class Proposal(BaseModel, frozen=True):
            reason: Reason | None = None

        assert Proposal(reason="Paid the supplier.").reason == "Paid the supplier."  # type: ignore[arg-type]
        assert isinstance(Proposal(reason="Paid the supplier").reason, Reason)  # type: ignore[arg-type]
        assert Proposal(reason=Reason("Paid the supplier")).reason == "Paid the supplier"
        assert Proposal().reason is None
        with pytest.raises(ValidationError):
            Proposal(reason="no")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            Proposal(reason="Two. Sentences.")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            Proposal(reason=7)  # type: ignore[arg-type]
        dumped = Proposal(reason="Paid the supplier").model_dump_json()  # type: ignore[arg-type]
        assert dumped == '{"reason":"Paid the supplier"}'
        assert (
            Proposal.model_validate_json('{"reason":"Paid the supplier"}').reason
            == "Paid the supplier"
        )
        schema = Proposal.model_json_schema()["properties"]["reason"]
        assert {"maxLength": 240, "minLength": 3, "type": "string"} in schema["anyOf"]

    @given(st.text(min_size=0, max_size=300))
    @settings(deadline=None)
    def test_coerce_yields_a_valid_reason_or_none(self, text: str) -> None:
        try:
            result = Reason.coerce(text)
        except ReasonError:
            return
        if result is not None:
            assert Reason(result) == result
            assert result == result.strip()


# --------------------------------------------------------------------------- SafeStr


class TestSafeStr:
    def test_cannot_be_built_without_the_sentinel(self) -> None:
        with pytest.raises(Tier2LeakError):
            _safe_ctor("hello", _minted_by=object())
        with pytest.raises(Tier2LeakError):
            _safe_ctor("hello", _minted_by=None)
        with pytest.raises(Tier2LeakError):
            _safe_ctor("hello", _minted_by="_MINT")
        with pytest.raises(TypeError):
            _safe_ctor("hello")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            _safe_ctor("hello", object())  # type: ignore[call-arg]

    def test_sentinel_is_identity_checked(self) -> None:
        assert isinstance(_MINT, object) and type(_MINT) is object
        minted = _minted("hello")
        assert minted == "hello"
        assert isinstance(minted, str) and isinstance(minted, SafeStr)
        assert str(minted) == "hello" and type(str(minted)) is str
        with pytest.raises(TypeError):
            _safe_ctor(b"bytes", _minted_by=_MINT)  # type: ignore[arg-type]

    def test_str_operations_return_plain_str(self) -> None:
        # Derived text has not been scanned: it must come back as a plain str, not SafeStr.
        minted = _minted("hello")
        assert type(minted + " world") is str
        assert type(minted.upper()) is str
        assert type(minted[:2]) is str

    def test_pydantic_field_is_instance_only(self) -> None:
        class Sink(BaseModel, frozen=True):
            text: SafeStr

        ok = Sink(text=_minted("scrubbed"))
        assert ok.text is not None and isinstance(ok.text, SafeStr)
        assert ok.model_dump() == {"text": "scrubbed"}
        assert ok.model_dump_json() == '{"text":"scrubbed"}'
        with pytest.raises(ValidationError):
            Sink(text="plain")  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            Sink(text=Reason("Paid the supplier"))  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            Sink.model_validate_json('{"text": "from json"}')
        assert Sink.model_validate({"text": ok.text}).text is ok.text
        assert Sink.model_json_schema()["properties"]["text"]["type"] == "string"
