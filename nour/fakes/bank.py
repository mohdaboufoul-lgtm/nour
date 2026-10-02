"""``FakeBankFeed``: the read-only bank feed (DESIGN §3.9 §6; SPEC §10, phase 2 body).

Read-only access per company for reconciliation, invoice chasing and the cash position in the
morning brief (SPEC §10): ``seed(coat_id, lines)`` and ``set_balance`` are the test's side,
``lines``/``balance`` the port's. Nothing here moves money; ``BankLine.counterpart_last4`` is all
a brief may show (SPEC §10).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import AwareDatetime

from nour.core.clock import Clock
from nour.core.ports import BankLine, CallLog
from nour.core.types import CoatId, Money
from nour.fakes import FakePort


class FakeBankFeed(FakePort):
    """BankFeedPort fake: ``seed(coat_id, lines)``, ``set_balance(coat_id, money)``."""

    port_name: str = "bank"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self._lines: dict[CoatId, list[BankLine]] = {}
        self._balances: dict[CoatId, Money] = {}
        self._seq = 0

    def seed(self, coat_id: CoatId, lines: Sequence[BankLine]) -> None:
        """Append statement lines for ``coat_id``."""
        for line in lines:
            if not isinstance(line, BankLine):
                raise TypeError("seed takes BankLine rows")
        self._lines.setdefault(coat_id, []).extend(lines)

    def line(
        self,
        coat_id: CoatId,
        amount: Money,
        *,
        counterpart_last4: str,
        memo: str,
        account_ref: str | None = None,
    ) -> BankLine:
        """Build and seed one line dated now (a helper for tests)."""
        self._seq += 1
        row = BankLine(
            ref=f"bank-{self._seq:06d}",
            account_ref=account_ref if account_ref is not None else f"acct-{coat_id}",
            at=self.clock.now(),
            amount=amount,
            counterpart_last4=counterpart_last4,
            memo=memo,
        )
        self.seed(coat_id, [row])
        return row

    def set_balance(self, coat_id: CoatId, money: Money) -> None:
        if not isinstance(money, Money):
            raise TypeError("set_balance takes Money")
        self._balances[coat_id] = money

    def lines(self, coat_id: CoatId, since: AwareDatetime) -> list[BankLine]:
        """Lines for ``coat_id`` at or after ``since``, oldest first."""
        if since.tzinfo is None or since.tzinfo.utcoffset(since) is None:
            raise ValueError("lines takes a timezone-aware 'since'")
        self._maybe_fail("lines")
        rows = [row for row in self._lines.get(coat_id, []) if row.at >= since]
        return sorted(rows, key=lambda row: (row.at, row.ref))

    def balance(self, coat_id: CoatId) -> Money:
        """The seeded balance, zero AED when none was set."""
        self._maybe_fail("balance")
        return self._balances.get(coat_id, Money.zero())
