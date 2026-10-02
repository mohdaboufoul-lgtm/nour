"""Error hierarchy (DESIGN §3.2 ``nour/core/errors.py``).

Every domain error derives from :class:`NourError` so a loop can catch "anything Nour refused"
in one clause while the two value-shaped errors (:class:`ReasonError`, :class:`CurrencyMismatch`)
stay ``ValueError`` subclasses: pydantic turns a ``ValueError`` raised inside a validator into a
``ValidationError``, which is what a malformed model argument should become at the gate.

Import rule: this module imports ``nour.core.types`` only under ``TYPE_CHECKING`` (annotations),
because ``types.py`` imports :class:`ReasonError` and :class:`Tier2LeakError` at runtime. Nothing
here constructs a type, so the cycle never executes.

Class names are the ones DESIGN §3.2 fixes (``Refusal``, ``DeskWallViolation``, ...), so the
``N818`` "Error suffix" convention is suppressed per line rather than renaming what every later
module imports.

Messages never carry Tier 2 text: an error that reports a leak names the *label* of the
registered value (SPEC §2 "Nothing from Tier 2 ... is ever written into her memory, logs or
prompts"), never the value, because exceptions end up in logs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nour.core.types import FreezeScope, Money, Reason, RefusalCode


class NourError(Exception):
    """Base of every domain error Nour raises on purpose."""


class ConfigError(NourError):
    """SPEC §18: ``load_config`` lists every violation at once instead of stopping at the first.

    ``violations`` keeps the individual messages so a test can assert on each one.
    """

    violations: list[str]

    def __init__(self, violations: Sequence[str] | str) -> None:
        self.violations = [violations] if isinstance(violations, str) else list(violations)
        super().__init__("; ".join(self.violations) if self.violations else "invalid configuration")


class Refusal(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §5 §6: the gate refused a proposal.

    Carries the :class:`~nour.core.types.RefusalCode` and the one-sentence
    :class:`~nour.core.types.Reason`. Logged as a ``REFUSED`` audit row and never raised past the
    gate (DESIGN §4h).
    """

    code: RefusalCode
    reason: Reason

    def __init__(self, code: RefusalCode, reason: Reason) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


class DeskWallViolation(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §5: a desk reached for the other desk's rows, mappers, memory or token."""


class Tier2LeakError(NourError):
    """SPEC §2 §11: a Tier 2 value was about to enter a prompt, memory, log, hash or sink.

    Never put the offending text in the message; name the label or the sink.
    """


class WrongTierExecution(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §6: an action tried to execute at a tier below the one the resolver computed."""


class ReleaseTokenError(NourError):
    """SPEC §6 (DESIGN §4c): missing, foreign, reused or expired ``ReleaseToken``."""


class FrozenError(NourError):
    """SPEC §12: the kill switch or a watchdog freeze covers this action."""

    scope: FreezeScope

    def __init__(self, scope: FreezeScope, message: str | None = None) -> None:
        self.scope = scope
        super().__init__(message or f"frozen: {scope}")


class AuthError(NourError):
    """SPEC §6: a forged or unverifiable ``AuthStamp``, a decision without the required proof,
    or a capability token presented without the seal that only ``mint`` holds."""


class CardDeclined(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §10: the card issuer declined; the cap lives in the issuer, not in judgement."""

    reason: str
    remaining: Money

    def __init__(self, reason: str, remaining: Money) -> None:
        self.reason = reason
        self.remaining = remaining
        super().__init__(f"card declined: {reason} (remaining {remaining})")


class ReadBackRequired(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §9: a voice command that moves money or changes a record needs a text read-back."""


class ReasonError(ValueError):
    """SPEC §2 §12: the one-sentence reason is missing or malformed (see ``types.Reason``)."""


class CurrencyMismatch(ValueError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §10: arithmetic or comparison between two ``Money`` values of different currencies.

    Not in the DESIGN §3.2 list by name (it only says "currency mismatch raises"); added so a
    ledger can catch exactly this case. It is a ``ValueError`` like ``ReasonError``.
    """


class AppendOnlyViolation(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §12: an UPDATE or DELETE on an append-only mapper reached the ORM listener
    (the DB trigger is the real wall; this is the early, readable failure)."""


class SingleTransitionViolation(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """DESIGN §3.7: a column that may be set once while NULL was changed again, or a
    forward-only status moved backwards."""


class ModelUnavailable(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §12 outage row: the model vendor failed; the router switches to the fallback."""


class ScopeViolation(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §13: a desk asked the secrets manager for a key outside its own prefix."""


class Revoked(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §12: the kill switch revoked this token or credential."""


class NotInPhase(NourError):  # noqa: N818 - name fixed by DESIGN §3.2
    """SPEC §7: a capability declared for a later phase was called in phase ``phase``'s absence."""

    phase: int

    def __init__(self, phase: int, message: str | None = None) -> None:
        self.phase = phase
        super().__init__(message or f"not available before phase {phase}")
