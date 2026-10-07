"""nour/moona/wallet.py (docs/MOONA.md §2): the three doors and the invariant.

Proves: the seed lands once; ``authorize`` declines ``funds``, ``limit``, ``frozen`` and ``dead``
in that order and records every decision; ``debit`` takes only an authorization the wallet
issued (a duck-typed object, a dict, a foreign or reused or dry-run reference are refused);
``credit`` takes only a ``PaymentReceipt`` and books a ``ref`` once; ``charge`` clamps at zero and
records the shortfall; for any sequence of operations the balance is never negative and equals
seed + income − spend − costs (hypothesis); the database refuses UPDATE/DELETE on the entries.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from nour.core.clock import DUBAI, FakeClock, IdGenerator, process_clock, set_process_clock
from nour.core.errors import CurrencyMismatch
from nour.core.ports import PortCall
from nour.core.types import Desk, Hash, Money, Ulid
from nour.moona.life import DeathCause, Life
from nour.moona.money import money
from nour.moona.ports import PaymentReceipt
from nour.moona.store import open_store
from nour.moona.wallet import (
    DRY_RUN_PREFIX,
    DeclineReason,
    EntryKind,
    Wallet,
    WalletAuthorization,
    WalletDeclined,
)

CONFIG_HASH = Hash("sha256:" + "0" * 64)
START = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)


def usd(amount: str) -> Money:
    return money(amount, "USD")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Engine]:
    engine = open_store(f"sqlite+pysqlite:///{tmp_path / 'moona.sqlite3'}")
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def life(store: Engine, clock: FakeClock, idgen: IdGenerator) -> Life:
    return Life.birth(
        store, name="Moona", seed=usd("50"), config_hash=CONFIG_HASH, clock=clock, idgen=idgen
    )


@pytest.fixture
def wallet(store: Engine, life: Life, clock: FakeClock, idgen: IdGenerator) -> Wallet:
    wallet = Wallet(store, life, clock, idgen, max_single_spend_fraction=Decimal("0.5"))
    wallet.seed(usd("50"), audit_id=idgen.new())
    return wallet


@pytest.fixture
def call(idgen: IdGenerator) -> PortCall:
    return PortCall(audit_id=idgen.new(), desk=Desk.OPERATOR, coat_id=None)


def receipt(ref: str, amount: str, clock: FakeClock, payer: str = "client-01") -> PaymentReceipt:
    return PaymentReceipt(
        ref=ref,
        request_ref=f"inv-{ref}",
        job_ref="req-1",
        payer=payer,
        amount=usd(amount),
        settled_at=clock.now(),
    )


# --------------------------------------------------------------------------- the seed


def test_seed_lands_once(store: Engine, life: Life, clock: FakeClock, idgen: IdGenerator) -> None:
    wallet = Wallet(store, life, clock, idgen)
    assert wallet.balance() == usd("0")
    wallet.seed(usd("50"), audit_id=idgen.new())
    assert wallet.balance() == usd("50")
    with pytest.raises(WalletDeclined, match="already seeded"):
        wallet.seed(usd("50"), audit_id=idgen.new())
    with pytest.raises(CurrencyMismatch):
        Wallet(store, life, clock, idgen).seed(Money.aed(50), audit_id=idgen.new())
    summary = wallet.summary()
    assert summary.seed == usd("50") and summary.entries == 1 and summary.balance == usd("50")


# --------------------------------------------------------------------------- out, on purpose


def test_authorize_then_debit_spends_once(
    wallet: Wallet, call: PortCall, idgen: IdGenerator
) -> None:
    decision = wallet.authorize(call, usd("12"), "Namecheap")
    assert decision.approved and decision.authorization is not None
    assert decision.balance == usd("50") and decision.decline_reason is None
    row = wallet.debit(decision.authorization, audit_id=idgen.new(), purpose="a domain")
    assert (
        row.kind == EntryKind.SPEND.value
        and row.amount == -1200
        and row.ref == decision.authorization.auth_ref
    )
    assert wallet.balance() == usd("38")
    with pytest.raises(WalletDeclined, match="already spent"):
        wallet.debit(decision.authorization, audit_id=idgen.new(), purpose="again")
    assert wallet.balance() == usd("38")
    auths = wallet.authorizations()
    assert len(auths) == 1 and auths[0].approved and auths[0].balance_before == 5000


def test_authorize_declines_in_order(
    wallet: Wallet, life: Life, call: PortCall, idgen: IdGenerator
) -> None:
    limit = wallet.authorize(call, usd("30"), "Shop")  # above half the balance
    assert not limit.approved and limit.decline_reason == DeclineReason.LIMIT.value
    funds = wallet.authorize(call, usd("60"), "Shop")
    assert not funds.approved and funds.decline_reason == DeclineReason.FUNDS.value
    life.freeze()
    frozen = wallet.authorize(call, usd("1"), "Shop")
    assert not frozen.approved and frozen.decline_reason == DeclineReason.FROZEN.value
    life.thaw()
    life.die(DeathCause.KILLED)
    dead = wallet.authorize(call, usd("1"), "Shop")
    assert not dead.approved and dead.decline_reason == DeclineReason.DEAD.value
    assert [row.decline_reason for row in wallet.authorizations()] == [
        "limit",
        "funds",
        "frozen",
        "dead",
    ]
    assert wallet.balance() == usd("50")


def test_debit_takes_only_what_the_wallet_issued(
    wallet: Wallet, call: PortCall, clock: FakeClock, idgen: IdGenerator
) -> None:
    hand_built = SimpleNamespace(
        auth_ref="wa-forged", life_id=wallet.life.id, amount=usd("5"), merchant="x", at=clock.now()
    )
    with pytest.raises(ValidationError):
        wallet.debit(hand_built, audit_id=idgen.new(), purpose="forged")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        wallet.debit({"auth_ref": "x", "amount": 500}, audit_id=idgen.new(), purpose="forged")  # type: ignore[arg-type]
    forged = WalletAuthorization(
        auth_ref="wa-never-issued",
        life_id=wallet.life.id,
        amount=usd("5"),
        merchant="x",
        at=clock.now(),
    )
    with pytest.raises(WalletDeclined, match="not issued"):
        wallet.debit(forged, audit_id=idgen.new(), purpose="forged")
    issued = wallet.authorize(call, usd("5"), "x").authorization
    assert issued is not None
    bigger = WalletAuthorization(
        auth_ref=issued.auth_ref,
        life_id=issued.life_id,
        amount=usd("25"),
        merchant="x",
        at=issued.at,
    )
    with pytest.raises(WalletDeclined, match="not issued"):
        wallet.debit(bigger, audit_id=idgen.new(), purpose="inflated")
    other_life = WalletAuthorization(
        auth_ref=issued.auth_ref,
        life_id=Ulid("0" * 26),
        amount=usd("5"),
        merchant="x",
        at=issued.at,
    )
    with pytest.raises(WalletDeclined, match="not issued"):
        wallet.debit(other_life, audit_id=idgen.new(), purpose="other life")
    assert wallet.balance() == usd("50")


def test_dry_run_authorizations_are_never_debited(wallet: Wallet, idgen: IdGenerator) -> None:
    dry = PortCall(audit_id=idgen.new(), desk=Desk.OPERATOR, coat_id=None, dry_run=True)
    decision = wallet.authorize(dry, usd("5"), "x")
    assert decision.approved and decision.authorization is not None
    assert decision.authorization.auth_ref.startswith(DRY_RUN_PREFIX)
    assert wallet.authorizations() == []  # nothing recorded under dry run
    with pytest.raises(WalletDeclined, match="dry run"):
        wallet.debit(decision.authorization, audit_id=idgen.new(), purpose="x")
    declined = wallet.authorize(dry, usd("40"), "x")  # the cap is still exercised
    assert not declined.approved and declined.decline_reason == DeclineReason.LIMIT.value


def test_debit_rechecks_the_balance(wallet: Wallet, call: PortCall, idgen: IdGenerator) -> None:
    first = wallet.authorize(call, usd("25"), "a").authorization
    second = wallet.authorize(call, usd("25"), "b").authorization
    assert first is not None and second is not None
    wallet.debit(first, audit_id=idgen.new(), purpose="a")
    wallet.charge(EntryKind.UPKEEP, usd("1"), audit_id=None, detail="upkeep")
    with pytest.raises(WalletDeclined, match="funds"):
        wallet.debit(second, audit_id=idgen.new(), purpose="b")
    assert wallet.balance() == usd("24")


# --------------------------------------------------------------------------- in


def test_credit_takes_a_receipt_once(wallet: Wallet, clock: FakeClock, idgen: IdGenerator) -> None:
    paid = receipt("pay-1", "8", clock)
    row = wallet.credit(paid, audit_id=idgen.new())
    assert row is not None and row.kind == EntryKind.INCOME.value and row.amount == 800
    assert wallet.credit(paid, audit_id=idgen.new()) is None
    assert wallet.has_ref("pay-1") and not wallet.has_ref("pay-2")
    assert wallet.balance() == usd("58")
    with pytest.raises(ValidationError):
        wallet.credit({"ref": "pay-2", "amount": 800}, audit_id=idgen.new())  # type: ignore[arg-type]
    with pytest.raises(CurrencyMismatch):
        wallet.credit(
            PaymentReceipt(
                ref="pay-3",
                request_ref="i",
                job_ref="j",
                payer="p",
                amount=Money.aed(5),
                settled_at=clock.now(),
            ),
            audit_id=idgen.new(),
        )
    with pytest.raises(ValidationError):
        PaymentReceipt(
            ref="pay-4",
            request_ref="i",
            job_ref="j",
            payer="p",
            amount=usd("0"),
            settled_at=clock.now(),
        )
    assert wallet.summary().income == usd("8")


# --------------------------------------------------------------------------- the cost of being alive


def test_charge_clamps_at_zero_and_records_the_shortfall(wallet: Wallet) -> None:
    zero = wallet.charge(EntryKind.MODEL_COST, usd("0"), audit_id=None, detail="free")
    assert zero.charged == usd("0") and zero.shortfall == usd("0") and not zero.starved
    assert wallet.summary().entries == 1  # a zero charge writes nothing
    small = wallet.charge(EntryKind.MODEL_COST, usd("0.07"), audit_id=None, detail="tick 1")
    assert small.charged == usd("0.07") and small.balance_after == usd("49.93")
    big = wallet.charge(EntryKind.UPKEEP, usd("60"), audit_id=None, detail="upkeep")
    assert big.charged == usd("49.93") and big.shortfall == usd("10.07")
    assert big.balance_after == usd("0") and big.starved
    assert wallet.balance() == usd("0")
    assert wallet.entries()[-1].shortfall == 1007
    with pytest.raises(ValueError, match="spends go through authorize"):
        wallet.charge(EntryKind.SPEND, usd("1"), audit_id=None, detail="x")
    with pytest.raises(ValueError, match="not negative"):
        wallet.charge(EntryKind.UPKEEP, Money(fils=-1, currency="USD"), audit_id=None, detail="x")


# --------------------------------------------------------------------------- the invariant

Op = tuple[str, int]


def _ops() -> st.SearchStrategy[list[Op]]:
    return st.lists(
        st.one_of(
            st.tuples(st.just("spend"), st.integers(1, 6000)),
            st.tuples(st.just("credit"), st.integers(1, 3000)),
            st.tuples(st.just("charge"), st.integers(0, 2000)),
        ),
        min_size=1,
        max_size=25,
    )


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=_ops(), fraction=st.sampled_from([None, Decimal("0.5"), Decimal("1")]))
def test_balance_is_never_negative_and_always_adds_up(
    tmp_path_factory: pytest.TempPathFactory, ops: list[Op], fraction: Decimal | None
) -> None:
    directory = tmp_path_factory.mktemp("wallet")
    engine = open_store(f"sqlite+pysqlite:///{directory / 'moona.sqlite3'}")
    previous = process_clock()
    with FakeClock(START) as clock:
        set_process_clock(clock)
        try:
            idgen = IdGenerator(clock, seed=0)
            life = Life.birth(
                engine,
                name="Moona",
                seed=usd("50"),
                config_hash=CONFIG_HASH,
                clock=clock,
                idgen=idgen,
            )
            wallet = Wallet(engine, life, clock, idgen, max_single_spend_fraction=fraction)
            wallet.seed(usd("50"), audit_id=idgen.new())
            call = PortCall(audit_id=idgen.new(), desk=Desk.OPERATOR, coat_id=None)
            income = spent = costs = 0
            for index, (op, fils) in enumerate(ops):
                clock.advance(timedelta(minutes=1))
                before = wallet.balance()
                if op == "spend":
                    decision = wallet.authorize(call, Money(fils=fils, currency="USD"), f"m{index}")
                    assert decision.balance == before
                    if decision.approved:
                        assert fils <= before.fils
                        assert decision.authorization is not None
                        wallet.debit(decision.authorization, audit_id=idgen.new(), purpose="p")
                        spent += fils
                    else:
                        assert decision.decline_reason in {"funds", "limit"}
                        assert wallet.balance() == before
                elif op == "credit":
                    row = wallet.credit(
                        receipt(f"pay-{index}", str(Decimal(fils) / 100), clock),
                        audit_id=idgen.new(),
                    )
                    assert row is not None
                    income += fils
                else:
                    charge = wallet.charge(
                        EntryKind.MODEL_COST,
                        Money(fils=fils, currency="USD"),
                        audit_id=None,
                        detail="c",
                    )
                    assert charge.charged.fils + charge.shortfall.fils == fils
                    assert charge.charged.fils <= before.fils
                    costs += charge.charged.fils
                balance = wallet.balance()
                assert balance.fils >= 0
                assert balance.fils == 5000 + income - spent - costs
            summary = wallet.summary()
            assert summary.balance == wallet.balance()
            assert summary.income.fils == income and summary.spent.fils == spent
            assert summary.model_cost.fils == costs
        finally:
            set_process_clock(previous)
            engine.dispose()


# --------------------------------------------------------------------------- the database


@pytest.mark.parametrize("table", ["moona_wallet_entry", "moona_wallet_auth"])
def test_entries_and_authorizations_are_append_only_at_the_database(
    store: Engine, wallet: Wallet, call: PortCall, idgen: IdGenerator, table: str
) -> None:
    decision = wallet.authorize(call, usd("5"), "x")
    assert decision.authorization is not None
    wallet.debit(decision.authorization, audit_id=idgen.new(), purpose="p")
    for statement in (f"UPDATE {table} SET amount = 1", f"DELETE FROM {table}"):
        with store.begin() as connection, pytest.raises(IntegrityError, match="append-only"):
            connection.execute(text(statement))
    assert wallet.balance() == usd("45")


def test_sign_follows_kind_at_the_database(store: Engine, wallet: Wallet) -> None:
    with store.begin() as connection, pytest.raises(IntegrityError):
        connection.execute(
            text(
                "INSERT INTO moona_wallet_entry (id, life_id, kind, amount, currency, shortfall, at, created_at) "
                "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZZ', :life, 'income', -100, 'USD', 0, '2026-10-05 03:00:00', '2026-10-05 03:00:00')"
            ),
            {"life": wallet.life.id},
        )
