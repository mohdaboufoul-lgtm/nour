"""Wallet money helpers (docs/MOONA.md §2; SPEC §10 "integer minor units").

Moona's wallet is kept in one currency (``config/moona.yaml: currency``, USD by default, because
the owner funds him in dollars) while every model cost in Nour is metered in AED fils
(``ModelUsage.cost_fils``). The dirham is pegged to the dollar, so the conversion is one fixed
number from the config (``fx_aed_per_unit``) and never a market lookup; it rounds **up**: Moona
always pays at least the true cost, and a fraction of a cent rounds against him, never for him.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation

from nour.core.errors import CurrencyMismatch
from nour.core.ports import ModelUsage
from nour.core.types import Money

MINOR_PER_UNIT = 100
_MINOR = Decimal("0.01")


def money(amount: Decimal | int | str, currency: str) -> Money:
    """``money("12.34", "USD")`` → 1234 minor units of USD: ``Money.aed`` for any currency.

    Rejects floats and bools (a float cannot carry a money amount exactly), anything that is not
    a number, non-finite values and anything finer than one minor unit.
    """
    if isinstance(amount, bool | float):
        raise TypeError("money takes Decimal, int or str, never float or bool")
    try:
        value = Decimal(amount)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"not a money amount: {amount!r}") from exc
    if not value.is_finite():
        raise ValueError(f"not a money amount: {amount!r}")
    minor = value * MINOR_PER_UNIT
    if minor != minor.to_integral_value():
        raise ValueError(f"{amount!r} is finer than one minor unit of {currency}")
    return Money(fils=int(minor), currency=currency)


def same_currency(amount: Money, currency: str, what: str) -> Money:
    """``amount`` when it is in ``currency``; ``CurrencyMismatch`` otherwise (the wallet never
    mixes currencies, SPEC §10)."""
    if not isinstance(amount, Money):
        raise TypeError(f"{what} takes Money, got {type(amount).__name__}")
    if amount.currency != currency:
        raise CurrencyMismatch(f"{what}: {amount.currency} into a {currency} wallet")
    return amount


def fils_to_wallet(fils: int, *, currency: str, aed_per_unit: Decimal) -> Money:
    """AED fils → wallet minor units at the fixed peg, rounded up (never below the true cost).

    ``aed_per_unit`` is how many AED one unit of ``currency`` buys (``3.6725`` for USD, ``1`` for
    AED); it must be positive. A negative ``fils`` is refused: a cost is never a credit.
    """
    if isinstance(fils, bool) or not isinstance(fils, int):
        raise TypeError("fils_to_wallet takes an int number of fils")
    if fils < 0:
        raise ValueError("a cost in fils is never negative")
    if not isinstance(aed_per_unit, Decimal) or aed_per_unit <= 0:
        raise ValueError("aed_per_unit must be a positive Decimal")
    if currency == "AED":
        return Money(fils=fils, currency="AED")
    minor = (Decimal(fils) / aed_per_unit).to_integral_value(rounding=ROUND_CEILING)
    return Money(fils=int(minor), currency=currency)


def model_cost(usage: ModelUsage, *, currency: str, aed_per_unit: Decimal) -> Money:
    """What one model call costs the wallet: ``usage.cost_fils`` converted with
    :func:`fils_to_wallet` (zero stays zero; a vendor that reports no cost charges nothing, which
    is why the live adapter must report it, docs/MOONA.md §6)."""
    if not isinstance(usage, ModelUsage):
        raise TypeError("model_cost takes a ModelUsage")
    return fils_to_wallet(usage.cost_fils, currency=currency, aed_per_unit=aed_per_unit)


def fraction_of(amount: Money, fraction: Decimal) -> Money:
    """``fraction`` of ``amount`` in the same currency, rounded up to the minor unit (used for
    the spend limits: "no single purchase above half the wallet")."""
    if not isinstance(fraction, Decimal) or fraction < 0:
        raise ValueError("fraction must be a non-negative Decimal")
    minor = (Decimal(amount.fils) * fraction).to_integral_value(rounding=ROUND_CEILING)
    return Money(fils=int(minor), currency=amount.currency)


def as_text(amount: Money) -> str:
    """``USD 12.34`` (the one format every prompt, journal line and CLI table uses)."""
    return f"{amount.currency} {amount.as_decimal().quantize(_MINOR)}"
