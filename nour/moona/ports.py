"""Moona's two external systems as ports (docs/MOONA.md §3; DESIGN §3.4 for the shape).

A marketplace where work is requested, bid for and delivered, and a payment rail that settles
what clients pay. Both follow the house rules for ports: a ``typing.Protocol`` with frozen DTOs;
every side-effecting method takes a ``PortCall`` first (SPEC §12 §16: an unlogged side effect
is a type error); text Moona emits is ``SafeStr`` minted by ``LeakGuard`` (SPEC §2 §11); text
that arrives from the world is plain ``str``, data and never authority (SPEC §2); every
timestamp is ``AwareDatetime``. They live here rather than in ``nour/core/ports.py`` because
they are a sub-agent's body, not a desk's (the desk ports and ``PortSet`` are closed sets pinned
by ``tests/unit/test_ports_shapes.py``).

The one object a rail *mints* is :class:`PaymentReceipt`: ``Wallet.credit`` accepts nothing
else, so the only way money ever enters the wallet is a payment the rail says has settled
(SPEC §10 "money in is automated"; DESIGN §4f in miniature: the credit lives in the rail, never
in Moona's judgement and never in a tool argument).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, field_validator

from nour.core.ports import PortCall
from nour.core.types import Money, SafeStr

# --------------------------------------------------------------------------- marketplace DTOs


class JobRequest(BaseModel, frozen=True):
    """One open request for work on the marketplace: observed content (``title`` and ``brief``
    are a stranger's words and reach the model only inside an ``<observed>`` fence)."""

    id: str
    client: str
    title: str
    brief: str
    budget: Money
    posted_at: AwareDatetime
    expires_at: AwareDatetime | None = None

    @field_validator("id", "client")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class ClientMessage(BaseModel, frozen=True):
    """A message from a client about a request (or none): observed content."""

    id: str
    request_id: str | None
    client: str
    text: str
    at: AwareDatetime


class BidReceipt(BaseModel, frozen=True):
    """What the marketplace answered to a bid; ``accepted`` means the job is Moona's at ``price``."""

    request_id: str
    accepted: bool
    price: Money
    note: str | None = None
    dry_run: bool = False


class DeliveryReceipt(BaseModel, frozen=True):
    """What the marketplace answered to a delivery."""

    request_id: str
    accepted: bool
    note: str | None = None
    dry_run: bool = False


@runtime_checkable
class MarketplacePort(Protocol):
    """Where Moona finds work. ``open_requests`` and ``pull_messages`` are reads (no call);
    ``bid`` and ``deliver`` are outbound and take the ``PortCall`` of their journal span."""

    def open_requests(self) -> list[JobRequest]: ...
    def pull_messages(self) -> list[ClientMessage]: ...
    def bid(
        self, call: PortCall, request_id: str, price: Money, message: SafeStr
    ) -> BidReceipt: ...
    def deliver(self, call: PortCall, request_id: str, content: SafeStr) -> DeliveryReceipt: ...


# --------------------------------------------------------------------------- payment DTOs


class PaymentRequest(BaseModel, frozen=True):
    """An invoice the rail issued for a job; nothing has moved yet."""

    ref: str
    job_ref: str
    payer: str
    amount: Money
    memo: str
    requested_at: AwareDatetime
    dry_run: bool = False


class PaymentReceipt(BaseModel, frozen=True):
    """Minted only by a ``PaymentPort`` implementation when a payment has settled;
    ``Wallet.credit`` requires one (docs/MOONA.md §2)."""

    ref: str
    request_ref: str
    job_ref: str
    payer: str
    amount: Money
    settled_at: AwareDatetime

    @field_validator("amount")
    @classmethod
    def _positive(cls, value: Money) -> Money:
        if value.fils <= 0:
            raise ValueError("a settled payment is positive")
        return value


@runtime_checkable
class PaymentPort(Protocol):
    """The rail that collects what clients owe. ``request`` is outbound (an invoice leaves);
    ``settled`` is a read of what has arrived since ``since``."""

    def request(
        self, call: PortCall, *, payer: str, amount: Money, memo: SafeStr, job_ref: str
    ) -> PaymentRequest: ...
    def settled(self, since: AwareDatetime) -> list[PaymentReceipt]: ...
