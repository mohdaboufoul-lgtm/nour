"""The wallet: his whole fortune, his only cap (docs/MOONA.md §2; SPEC §10 "caps live in the
card and the config, never in her judgment"; DESIGN §4f "cap beats approval" in miniature).

Money moves through exactly three doors, each a type the model cannot forge:

* **Out, on purpose** — :meth:`Wallet.debit` takes a :class:`WalletAuthorization` that only
  :meth:`Wallet.authorize` issues, after reading the life row (dead → ``dead``, frozen →
  ``frozen``) and the balance (``amount > balance`` → ``funds``; above the configured fraction of
  the balance → ``limit``). A hand-built object fails pydantic validation, an authorization the
  wallet never issued or one issued under dry run is declined, and a ``ref`` is UNIQUE in the
  entry table so the same authorization can never be debited twice.
* **In** — :meth:`Wallet.credit` takes a :class:`~nour.moona.ports.PaymentReceipt` that only a
  ``PaymentPort`` mints (a settled payment); the receipt's ``ref`` is UNIQUE, so a settlement
  reported twice is credited once.
* **Out, as the cost of being alive** — :meth:`Wallet.charge` (a model call, the daily upkeep)
  takes what it can: a charge that exceeds the balance takes the balance to zero and records the
  rest as the entry's ``shortfall``. The wallet never goes negative; the runtime reads a zero
  balance as death (``Life.die(STARVED)``).

Balance is ``SUM(amount)`` over the append-only entries, computed in the same ``BEGIN
IMMEDIATE`` transaction as the write that depends on it, so two writers cannot both see the
same money.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, model_validator
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from nour.core.clock import Clock, IdGenerator
from nour.core.errors import NourError
from nour.core.ports import PortCall
from nour.core.types import Money, Ulid
from nour.moona.life import Life, LifeStatus
from nour.moona.money import fraction_of, same_currency
from nour.moona.ports import PaymentReceipt
from nour.moona.store import LifeRow, WalletAuthRow, WalletEntryRow, reading, unit_of_work

DRY_RUN_PREFIX = "dryrun-"
"""``auth_ref`` prefix of an authorization computed under ``call.dry_run``: never debitable."""


class EntryKind(StrEnum):
    SEED = "seed"
    INCOME = "income"
    SPEND = "spend"
    MODEL_COST = "model_cost"
    UPKEEP = "upkeep"


COST_KINDS: frozenset[EntryKind] = frozenset({EntryKind.MODEL_COST, EntryKind.UPKEEP})


class DeclineReason(StrEnum):
    """Why ``authorize`` said no (the wallet's ``DeclineReason``, mirroring the card's)."""

    FUNDS = "funds"
    LIMIT = "limit"
    FROZEN = "frozen"
    DEAD = "dead"


class WalletAuthorization(BaseModel, frozen=True):
    """Issued only by :meth:`Wallet.authorize`; :meth:`Wallet.debit` requires one."""

    auth_ref: str
    life_id: Ulid
    amount: Money
    merchant: str
    at: AwareDatetime


class WalletDecision(BaseModel, frozen=True):
    """The wallet's answer; ``approved`` and ``authorization`` agree (validated)."""

    approved: bool
    authorization: WalletAuthorization | None
    decline_reason: str | None
    balance: Money

    @model_validator(mode="after")
    def _consistent(self) -> WalletDecision:
        if self.approved and self.authorization is None:
            raise ValueError("an approved WalletDecision carries a WalletAuthorization")
        if not self.approved and self.authorization is not None:
            raise ValueError("a declined WalletDecision carries no WalletAuthorization")
        if not self.approved and not self.decline_reason:
            raise ValueError("a declined WalletDecision names its reason")
        return self


class Charge(BaseModel, frozen=True):
    """What :meth:`Wallet.charge` took: ``charged + shortfall == asked``."""

    kind: EntryKind
    asked: Money
    charged: Money
    shortfall: Money
    balance_after: Money

    @property
    def starved(self) -> bool:
        """True when the charge emptied the wallet (whether or not something was left unpaid)."""
        return self.balance_after.fils == 0


class WalletSummary(BaseModel, frozen=True):
    """Totals per kind for the status line and the prompt."""

    balance: Money
    seed: Money
    income: Money
    spent: Money
    model_cost: Money
    upkeep: Money
    entries: int


class WalletDeclined(NourError):  # noqa: N818 - DESIGN §3.2 style (``CardDeclined``)
    """§10: the wallet declined; the cap lives here, not in judgement."""

    reason: str
    balance: Money

    def __init__(self, reason: str, balance: Money) -> None:
        self.reason = reason
        self.balance = balance
        super().__init__(f"wallet declined: {reason} (balance {balance})")


class Wallet:
    """One life's wallet. ``max_single_spend_fraction`` is ``config/moona.yaml:
    limits.max_single_spend_fraction`` (``None`` disables the limit, for tests of the other
    doors)."""

    def __init__(
        self,
        engine: Engine,
        life: Life,
        clock: Clock,
        idgen: IdGenerator,
        *,
        max_single_spend_fraction: Decimal | None = None,
    ) -> None:
        if max_single_spend_fraction is not None and not (
            Decimal("0") < max_single_spend_fraction <= Decimal("1")
        ):
            raise ValueError("max_single_spend_fraction is in (0, 1]")
        self._engine = engine
        self._life = life
        self._clock = clock
        self._idgen = idgen
        self._fraction = max_single_spend_fraction
        self._currency = life.currency

    @property
    def currency(self) -> str:
        return self._currency

    @property
    def life(self) -> Life:
        return self._life

    # ----- reading

    def balance(self) -> Money:
        with reading(self._engine) as session:
            return self._balance(session)

    def _balance(self, session: Session) -> Money:
        total = session.scalar(
            select(func.coalesce(func.sum(WalletEntryRow.amount), 0)).where(
                WalletEntryRow.life_id == self._life.id
            )
        )
        return Money(fils=int(total or 0), currency=self._currency)

    def entries(self) -> list[WalletEntryRow]:
        with reading(self._engine) as session:
            rows = session.scalars(
                select(WalletEntryRow)
                .where(WalletEntryRow.life_id == self._life.id)
                .order_by(WalletEntryRow.id)
            ).all()
            session.expunge_all()
            return list(rows)

    def authorizations(self) -> list[WalletAuthRow]:
        with reading(self._engine) as session:
            rows = session.scalars(
                select(WalletAuthRow)
                .where(WalletAuthRow.life_id == self._life.id)
                .order_by(WalletAuthRow.id)
            ).all()
            session.expunge_all()
            return list(rows)

    def has_ref(self, ref: str) -> bool:
        """Whether an authorization or a receipt ``ref`` is already booked."""
        with reading(self._engine) as session:
            count = session.scalar(
                select(func.count()).select_from(WalletEntryRow).where(WalletEntryRow.ref == ref)
            )
            return bool(count)

    def summary(self) -> WalletSummary:
        with reading(self._engine) as session:
            totals = {
                kind: int(amount or 0)
                for kind, amount in session.execute(
                    select(WalletEntryRow.kind, func.sum(WalletEntryRow.amount))
                    .where(WalletEntryRow.life_id == self._life.id)
                    .group_by(WalletEntryRow.kind)
                ).all()
            }
            count = session.scalar(
                select(func.count())
                .select_from(WalletEntryRow)
                .where(WalletEntryRow.life_id == self._life.id)
            )
            balance = self._balance(session)

        def money(kind: EntryKind, *, sign: int = 1) -> Money:
            return Money(fils=sign * totals.get(kind.value, 0), currency=self._currency)

        return WalletSummary(
            balance=balance,
            seed=money(EntryKind.SEED),
            income=money(EntryKind.INCOME),
            spent=money(EntryKind.SPEND, sign=-1),
            model_cost=money(EntryKind.MODEL_COST, sign=-1),
            upkeep=money(EntryKind.UPKEEP, sign=-1),
            entries=int(count or 0),
        )

    # ----- the seed (once)

    def seed(self, amount: Money, *, audit_id: Ulid) -> WalletEntryRow:
        """The one credit that is not a payment: the owner's seed, at birth, exactly once."""
        same_currency(amount, self._currency, "Wallet.seed")
        if amount.fils <= 0:
            raise ValueError("the seed is positive")
        with unit_of_work(self._engine) as session:
            existing = session.scalar(
                select(func.count())
                .select_from(WalletEntryRow)
                .where(WalletEntryRow.life_id == self._life.id)
            )
            if existing:
                raise WalletDeclined("already seeded", self._balance(session))
            row = self._entry(
                session,
                kind=EntryKind.SEED,
                amount=amount.fils,
                ref=None,
                counterpart="owner",
                detail="seed",
                audit_id=audit_id,
            )
            session.flush()
            return row

    # ----- out, on purpose

    def authorize(self, call: PortCall, amount: Money, merchant: str) -> WalletDecision:
        """The wallet's answer to "may I spend ``amount`` at ``merchant``?". Declines ``dead``,
        then ``frozen``, then ``funds`` (``amount > balance``), then ``limit`` (above the single
        spend fraction); otherwise records the authorization and returns it. Under
        ``call.dry_run`` the same decision is computed but nothing is recorded and the
        ``auth_ref`` starts with ``dryrun-`` (never debitable)."""
        if not isinstance(call, PortCall):
            raise TypeError("authorize takes the PortCall of a journal span")
        same_currency(amount, self._currency, "Wallet.authorize")
        if amount.fils <= 0:
            raise ValueError("an authorization amount is positive")
        if not merchant.strip():
            raise ValueError("an authorization names its merchant")
        with unit_of_work(self._engine) as session:
            life_row = session.get(LifeRow, self._life.id)
            balance = self._balance(session)
            reason: DeclineReason | None = None
            if life_row is None or life_row.status != LifeStatus.ALIVE.value:
                reason = DeclineReason.DEAD
            elif life_row.frozen:
                reason = DeclineReason.FROZEN
            elif amount > balance:
                reason = DeclineReason.FUNDS
            elif self._fraction is not None and amount > fraction_of(balance, self._fraction):
                reason = DeclineReason.LIMIT
            now = self._clock.now()
            prefix = DRY_RUN_PREFIX if call.dry_run else "wa-"
            auth_ref = f"{prefix}{self._idgen.new()}"
            authorization = (
                WalletAuthorization(
                    auth_ref=auth_ref,
                    life_id=self._life.id,
                    amount=amount,
                    merchant=merchant.strip(),
                    at=now,
                )
                if reason is None
                else None
            )
            if not call.dry_run:
                session.add(
                    WalletAuthRow(
                        id=self._idgen.new(),
                        life_id=self._life.id,
                        auth_ref=auth_ref,
                        amount=amount.fils,
                        currency=amount.currency,
                        merchant=merchant.strip(),
                        approved=reason is None,
                        decline_reason=reason.value if reason is not None else None,
                        balance_before=balance.fils,
                        audit_id=call.audit_id,
                        at=now,
                    )
                )
            return WalletDecision(
                approved=reason is None,
                authorization=authorization,
                decline_reason=reason.value if reason is not None else None,
                balance=balance,
            )

    def debit(
        self, authorization: WalletAuthorization, *, audit_id: Ulid, purpose: str
    ) -> WalletEntryRow:
        """Spend what ``authorize`` approved. Refuses (``WalletDeclined``) a dry-run reference,
        a reference the wallet never issued or issued for another life, a reused reference and an
        amount the balance no longer covers; a duck-typed object is a ``ValidationError``."""
        auth = WalletAuthorization.model_validate(authorization)
        same_currency(auth.amount, self._currency, "Wallet.debit")
        with unit_of_work(self._engine) as session:
            balance = self._balance(session)
            if auth.auth_ref.startswith(DRY_RUN_PREFIX):
                raise WalletDeclined("dry run authorization", balance)
            issued = session.scalar(
                select(WalletAuthRow).where(WalletAuthRow.auth_ref == auth.auth_ref)
            )
            if (
                issued is None
                or not issued.approved
                or issued.life_id != self._life.id
                or issued.life_id != auth.life_id
                or issued.amount != auth.amount.fils
                or issued.currency != auth.amount.currency
                or issued.merchant != auth.merchant
                or issued.at != auth.at
            ):
                raise WalletDeclined("authorization not issued by this wallet", balance)
            reused = session.scalar(
                select(func.count())
                .select_from(WalletEntryRow)
                .where(WalletEntryRow.ref == auth.auth_ref)
            )
            if reused:
                raise WalletDeclined("authorization already spent", balance)
            if auth.amount > balance:
                raise WalletDeclined(DeclineReason.FUNDS.value, balance)
            row = self._entry(
                session,
                kind=EntryKind.SPEND,
                amount=-auth.amount.fils,
                ref=auth.auth_ref,
                counterpart=auth.merchant,
                detail=purpose,
                audit_id=audit_id,
            )
            session.flush()
            return row

    # ----- in

    def credit(self, receipt: PaymentReceipt, *, audit_id: Ulid) -> WalletEntryRow | None:
        """Book a settled payment. A receipt already credited (same ``ref``) returns ``None``:
        settlement feeds are re-read, money is not re-counted."""
        receipt = PaymentReceipt.model_validate(receipt)
        same_currency(receipt.amount, self._currency, "Wallet.credit")
        with unit_of_work(self._engine) as session:
            seen = session.scalar(
                select(func.count())
                .select_from(WalletEntryRow)
                .where(WalletEntryRow.ref == receipt.ref)
            )
            if seen:
                return None
            row = self._entry(
                session,
                kind=EntryKind.INCOME,
                amount=receipt.amount.fils,
                ref=receipt.ref,
                counterpart=receipt.payer,
                detail=f"payment {receipt.request_ref} for job {receipt.job_ref}",
                audit_id=audit_id,
            )
            session.flush()
            return row

    # ----- out, as the cost of being alive

    def charge(
        self, kind: EntryKind, amount: Money, *, audit_id: Ulid | None, detail: str
    ) -> Charge:
        """Take ``amount`` for a model call or the daily upkeep, as far as the balance goes.
        Zero charges write nothing; a charge beyond the balance empties the wallet and records
        the unpaid rest as the entry's ``shortfall``."""
        kind = EntryKind(kind)
        if kind not in COST_KINDS:
            raise ValueError("charge takes MODEL_COST or UPKEEP; spends go through authorize")
        same_currency(amount, self._currency, "Wallet.charge")
        if amount.fils < 0:
            raise ValueError("a charge is not negative")
        with unit_of_work(self._engine) as session:
            balance = self._balance(session)
            if amount.fils == 0:
                zero = Money.zero(self._currency)
                return Charge(
                    kind=kind, asked=amount, charged=zero, shortfall=zero, balance_after=balance
                )
            charged = amount if amount <= balance else balance
            shortfall = amount - charged
            if charged.fils > 0:
                self._entry(
                    session,
                    kind=kind,
                    amount=-charged.fils,
                    ref=None,
                    counterpart=None,
                    detail=detail,
                    audit_id=audit_id,
                    shortfall=shortfall.fils,
                )
                session.flush()
            return Charge(
                kind=kind,
                asked=amount,
                charged=charged,
                shortfall=shortfall,
                balance_after=balance - charged,
            )

    # ----- internals

    def _entry(
        self,
        session: Session,
        *,
        kind: EntryKind,
        amount: int,
        ref: str | None,
        counterpart: str | None,
        detail: str | None,
        audit_id: Ulid | None,
        shortfall: int = 0,
    ) -> WalletEntryRow:
        balance = self._balance(session)
        if balance.fils + amount < 0:
            raise WalletDeclined(DeclineReason.FUNDS.value, balance)  # never negative, ever
        row = WalletEntryRow(
            id=self._idgen.new(),
            life_id=self._life.id,
            kind=kind.value,
            amount=amount,
            currency=self._currency,
            ref=ref,
            counterpart=counterpart,
            detail=detail,
            shortfall=shortfall,
            audit_id=audit_id,
            at=self._clock.now(),
        )
        session.add(row)
        return row


def entry_time(row: WalletEntryRow) -> datetime:
    """The entry's own timestamp (``at``), for tables and tests."""
    return row.at
