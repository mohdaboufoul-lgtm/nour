"""``FakeCardIssuer``: the capped card (DESIGN §3.9 §4f §6; SPEC §2 §10 §12 §16).

"Spending caps are enforced by the card, not by judgment" (SPEC §2). The cap lives *here*, set
at ``issue(holder, monthly_cap)`` from ``spend_tiers.monthly_cap``: ``authorize`` declines with
``limit`` when ``month_total + amount > cap`` and with ``frozen`` when the card is frozen (kill
switch, SPEC §12), whatever approval the caller holds ("cap beats approval", DESIGN §7.1). A
``CardAuthorization`` exists only on an approved decision, which is the only object
``Ledger.record_spend`` accepts (DESIGN §4f), so money out without the issuer's yes is a type error.

Dry run (SPEC §8: "no sends, no spend"): under ``call.dry_run`` the same decision is computed, so
caps are still exercised, but nothing is held or counted and the authorization's ``auth_ref``
starts with ``dryrun-`` (:meth:`FakeCardIssuer.is_dry_run_ref`). A dry-run ``CardAuthorization``
is type-identical to a live one on purpose (the cap check must run end to end), so
``Ledger.record_spend`` and the spend handler must refuse when ``call.dry_run`` and may use
``is_dry_run_ref`` as a belt; ``authorizations(card_ref)`` never lists one. **Protective effects
are not spends**: ``freeze`` and ``unfreeze`` apply under dry run too (a kill switch running in
the phase-1 dry-run mode must still freeze the card, and its release must still release it);
they are also listed in ``dry_run_ops`` so a test can see they ran under a dry-run ``PortCall``.

Months are Dubai calendar months read from the injected clock (``month_total`` follows the clock;
THREAT_REVIEW 5.14: a real issuer's cycle may differ, and the mismatch fails safe). Decline
reasons use the internal enum ``docs/adapters/card.md`` §3 maps every provider onto. No field
anywhere holds a PAN (SPEC §6 Tier 3).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from nour.core.clock import DUBAI, Clock
from nour.core.errors import CurrencyMismatch
from nour.core.ports import CallLog, CardAuthorization, CardDecision, PortCall
from nour.core.types import BudgetHolder, Money
from nour.fakes import FakePort

DRY_RUN_PREFIX = "dryrun-"
"""``auth_ref`` prefix of an authorization computed under ``call.dry_run`` (never held)."""


class DeclineReason(StrEnum):
    """The internal decline enum every provider reason maps onto (docs/adapters/card.md §3)."""

    LIMIT = "limit"
    FROZEN = "frozen"
    MCC_BLOCKED = "mcc_blocked"
    FUNDS = "funds"
    WEBHOOK = "webhook"
    OTHER = "other"


class FakeCardIssuer(FakePort):
    """Models the real card: declines when month_total + amount > cap or when frozen; cap set at issue().

    ``auths`` holds every decision in order (approved and declined, so
    ``auths[-1].approved is False`` is the gate's assertion); ``frozen`` the freeze flag per
    card; ``dry_run_auths`` the decisions computed under ``call.dry_run`` (nothing held, nothing
    counted); ``dry_run_ops`` the freeze/unfreeze operations that ran under a dry-run call (they
    still apply: protective effects are not spends).
    """

    port_name: str = "card"

    def __init__(self, call_log: CallLog | None, clock: Clock | None) -> None:
        super().__init__(call_log, clock)
        self.auths: list[CardDecision] = []
        self.dry_run_auths: list[CardDecision] = []
        self.frozen: dict[str, bool] = {}
        self.caps: dict[str, Money] = {}
        self.holders: dict[str, BudgetHolder] = {}
        self.dry_run_ops: list[tuple[str, str]] = []
        self._holds: dict[str, list[CardAuthorization]] = {}
        self._seq = 0

    # ----- issuing

    def issue(self, holder: BudgetHolder, monthly_cap: Money) -> str:
        """One card per budget holder; re-issuing the same holder resets its cap (never its
        spend). Returns the card ref, which carries no PAN."""
        if not isinstance(monthly_cap, Money):
            raise TypeError("issue takes the monthly cap as Money")
        if monthly_cap.fils < 0:
            raise ValueError("a monthly cap is not negative")
        card_ref = f"card-{holder}"
        self.caps[card_ref] = monthly_cap
        self.holders[card_ref] = holder
        self.frozen.setdefault(card_ref, False)
        self._holds.setdefault(card_ref, [])
        return card_ref

    def card_for(self, holder: BudgetHolder) -> str:
        """The card ref issued for ``holder``; ``KeyError`` when the holder has no card (a
        ``null`` cap in ``spend_tiers.yaml``)."""
        for card_ref, owner in self.holders.items():
            if owner == holder:
                return card_ref
        raise KeyError(f"no card issued for budget holder {holder!r}")

    @staticmethod
    def is_dry_run_ref(auth_ref: str) -> bool:
        """Whether ``auth_ref`` names an authorization computed under dry run (never held, never
        counted): the belt ``Ledger.record_spend`` may wear on top of ``call.dry_run``."""
        return isinstance(auth_ref, str) and auth_ref.startswith(DRY_RUN_PREFIX)

    # ----- authorization (the cap check)

    def authorize(
        self, call: PortCall, card_ref: str, amount: Money, merchant: str
    ) -> CardDecision:
        """The issuer's answer. Declines ``frozen`` first, then ``limit`` when
        ``month_total + amount > cap``; otherwise records a hold and returns the
        ``CardAuthorization``. ``remaining`` is :meth:`remaining` (never below zero, even after a
        re-issue lowered the cap under the month's spend). Under ``call.dry_run`` the same
        decision is computed and returned (so caps are still exercised) but nothing is held or
        counted and the ``auth_ref`` is ``dryrun-…``."""
        self._record("authorize", call, card_ref=card_ref, amount=amount, merchant=merchant)
        self._maybe_fail("authorize")
        cap = self._cap(card_ref)
        if not isinstance(amount, Money):
            raise TypeError("authorize takes the amount as Money")
        if amount.currency != cap.currency:
            raise CurrencyMismatch(f"{amount.currency} card is {cap.currency}")
        if amount.fils <= 0:
            raise ValueError("an authorization amount is positive")
        total = self.month_total(card_ref)
        remaining = self.remaining(card_ref)
        decision: CardDecision
        if self.frozen.get(card_ref, False):
            decision = CardDecision(
                approved=False,
                authorization=None,
                decline_reason=DeclineReason.FROZEN.value,
                remaining=remaining,
            )
        elif total + amount > cap:
            decision = CardDecision(
                approved=False,
                authorization=None,
                decline_reason=DeclineReason.LIMIT.value,
                remaining=remaining,
            )
        else:
            self._seq += 1
            prefix = DRY_RUN_PREFIX if call.dry_run else "auth-"
            authorization = CardAuthorization(
                auth_ref=f"{prefix}{self._seq:06d}",
                card_ref=card_ref,
                holder=self.holders[card_ref],
                amount=amount,
                merchant=merchant,
                at=self.clock.now(),
            )
            decision = CardDecision(
                approved=True,
                authorization=authorization,
                decline_reason=None,
                remaining=remaining - amount,
            )
        if call.dry_run:
            self.dry_run_auths.append(decision)
            return decision
        if decision.authorization is not None:
            self._holds[card_ref].append(decision.authorization)
        self.auths.append(decision)
        return decision

    # ----- freeze (SPEC §12 kill switch, watchdog) — protective: applies under dry run too

    def freeze(self, call: PortCall, card_ref: str) -> None:
        """Freeze the card. Applies under ``call.dry_run`` as well (a kill switch is not a spend;
        the operation is additionally listed in ``dry_run_ops``)."""
        self._record("freeze", call, card_ref=card_ref)
        self._maybe_fail("freeze")
        self._cap(card_ref)
        if call.dry_run:
            self.dry_run_ops.append(("freeze", card_ref))
        self.frozen[card_ref] = True

    def unfreeze(self, call: PortCall, card_ref: str) -> None:
        """Release the card (the kill-switch release, gated upstream by passphrase and second
        channel). Applies under ``call.dry_run`` as well, mirroring :meth:`freeze`."""
        self._record("unfreeze", call, card_ref=card_ref)
        self._maybe_fail("unfreeze")
        self._cap(card_ref)
        if call.dry_run:
            self.dry_run_ops.append(("unfreeze", card_ref))
        self.frozen[card_ref] = False

    def is_frozen(self, card_ref: str) -> bool:
        self._cap(card_ref)
        return self.frozen[card_ref]

    # ----- totals (Dubai calendar month from the clock)

    def month_total(self, card_ref: str, month: date | None = None) -> Money:
        """Approved authorizations on ``card_ref`` in the Dubai calendar month of ``month``
        (default: the clock's current month)."""
        cap = self._cap(card_ref)
        month = month if month is not None else self.clock.today_dubai()
        total = Money.zero(cap.currency)
        for auth in self._holds[card_ref]:
            local = auth.at.astimezone(DUBAI)
            if (local.year, local.month) == (month.year, month.month):
                total = total + auth.amount
        return total

    def remaining(self, card_ref: str, month: date | None = None) -> Money:
        """``cap - month_total`` (never below zero)."""
        left = self._cap(card_ref) - self.month_total(card_ref, month)
        return left if left.fils > 0 else Money.zero(left.currency)

    def authorizations(self, card_ref: str) -> list[CardAuthorization]:
        """Every approved, held authorization on the card, in order (never a dry-run one)."""
        self._cap(card_ref)
        return list(self._holds[card_ref])

    def _cap(self, card_ref: str) -> Money:
        if card_ref not in self.caps:
            raise KeyError(f"unknown card {card_ref!r}; issued: {sorted(self.caps)}")
        return self.caps[card_ref]
