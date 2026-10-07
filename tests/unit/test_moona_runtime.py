"""nour/moona/runtime.py and the fakes (docs/MOONA.md §3 §5): whole lives on the simulated
economy, and the walls on every tick.

Proves: the survivor lives, earns and leaves a journal with no open span and a valid chain, every
side-effecting port call carrying an audit id; the thinker starves on the day thinking plus
upkeep exhaust the seed, with the death row, his last words and nothing executing afterwards;
the thought he cannot pay for is his last and its tool calls never run; the spendthrift's first
purchase is flagged, the rest declined by the wallet's limit, then he starves; the liar and the
gambler are refused before any port; a planted instruction freezes the money tools for the tick
and is journaled; an unknown tool, a missing reason and a smuggled argument are refused; rest and
freeze skip the model; the owner's kill is terminal and carries ``actor=owner``; a model outage
charges nothing; dry run holds every bid back; more calls than the cap are dropped and journaled;
a settled payment is credited once and marks the job paid.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import yaml

from nour.core.clock import DUBAI
from nour.core.types import ActionStatus, Actor, Money, Reason
from nour.fakes.policies import response, tool_call
from nour.moona.config import MoonaConfig, load_moona
from nour.moona.fakes import (
    GamblerPolicy,
    LiarPolicy,
    MoonaPolicy,
    ObeyMarketPolicy,
    SimRates,
    SpendthriftPolicy,
    SurvivorPolicy,
    ThinkerPolicy,
)
from nour.moona.life import DeathCause, NotAlive
from nour.moona.money import money
from nour.moona.runtime import TickKind
from nour.moona.simulation import Simulation
from nour.moona.wallet import DeclineReason
from tests.conftest import CONFIG_DIR, PROMPTS_DIR, REPO_ROOT

START = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)


def usd(amount: str) -> Money:
    return money(amount, "USD")


@pytest.fixture(scope="module")
def moona_cfg() -> MoonaConfig:
    return load_moona(CONFIG_DIR, PROMPTS_DIR)


@pytest.fixture(scope="module")
def rates(moona_cfg: MoonaConfig) -> SimRates:
    return SimRates.from_config(moona_cfg.simulation)


@pytest.fixture
def simulate(moona_cfg: MoonaConfig):  # type: ignore[no-untyped-def]
    """``simulate(policy, cfg=..., seed=..., dry_run=...)`` → a built ``Simulation``; closed on teardown."""
    built: list[Simulation] = []

    def build(
        policy: MoonaPolicy, *, cfg: MoonaConfig | None = None, seed: int = 1, dry_run: bool = False
    ) -> Simulation:
        sim = Simulation.build(
            cfg or moona_cfg, policy=policy, start=START, seed=seed, dry_run=dry_run
        )
        built.append(sim)
        return sim

    yield build
    for sim in reversed(built):
        sim.close()


def _planted_text(sample_id: str) -> str:
    corpus = yaml.safe_load(
        (REPO_ROOT / "tests/fixtures/injections.yaml").read_text(encoding="utf-8")
    )
    return next(item for item in corpus["samples"] if item["id"] == sample_id)["text"]


# --------------------------------------------------------------------------- whole lives


def test_survivor_lives_earns_and_leaves_a_clean_journal(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    report = sim.run(days=7)
    assert report.alive and report.cause is None and report.days_lived == 7
    assert report.final.income > usd("0") and report.jobs_paid > 0 and report.bids > 0
    assert report.final.balance > sim.cfg.seed  # he earned more than he burned
    assert report.final.model_cost > usd("0") and report.final.upkeep == usd(
        "7"
    )  # seven midnights crossed
    assert report.chain_ok and report.unclosed == 0
    assert (
        sim.call_log.without_audit() == []
    )  # every bid, delivery and invoice carried its audit id
    journal = sim.runtime.journal
    actors = {row.actor for row in journal.rows()}
    assert actors <= {"subagent", "owner", "system"}
    assert all(row.actor == Actor.SUBAGENT.value for row in journal.rows(action="market.bid"))
    assert all(row.actor == Actor.SYSTEM.value for row in journal.rows(action="upkeep"))
    assert all(row.config_hash == sim.cfg.config_hash for row in journal.rows())
    plan_rows = journal.rows(action="plan", phase="closed")
    assert plan_rows and all(row.amount is not None and row.amount > 0 for row in plan_rows)
    # he rested when idle: fewer model calls than ticks
    assert len(sim.model.requests) < report.ticks
    assert any(result.kind is TickKind.RESTED for result in sim.results)


def test_thinker_starves_and_nothing_runs_afterwards(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(ThinkerPolicy(rates))
    report = sim.run(days=40)
    assert not report.alive and report.cause is DeathCause.STARVED
    assert report.final.balance == usd("0") and report.final.income == usd("0")
    assert (
        report.final.model_cost + report.final.upkeep == sim.cfg.seed
    )  # every cent went to thinking and living
    assert report.last_words and report.last_words.startswith("Interesting market.")
    death = sim.runtime.journal.rows(action="death")
    assert len(death) == 1 and death[0].actor == "system" and "starved" in (death[0].detail or "")
    assert sim.results[-1].kind is TickKind.DIED and sim.results[-1].died is DeathCause.STARVED
    row = sim.runtime.life.row()
    assert row.status == "dead" and row.died_at is not None
    with pytest.raises(NotAlive, match="starved"):
        sim.runtime.tick()
    with pytest.raises(NotAlive):
        sim.runtime.kill(Reason("Too late."))
    assert sim.runtime.journal.verify_chain() and sim.runtime.journal.unclosed() == []
    requests_after = len(sim.model.requests)
    assert sim.run(days=1).ticks == report.ticks  # run() adds no tick to a dead life
    assert len(sim.model.requests) == requests_after


def test_the_thought_he_cannot_pay_for_is_his_last(
    simulate, moona_cfg: MoonaConfig, rates: SimRates
) -> None:  # type: ignore[no-untyped-def]
    poor = moona_cfg.model_copy(update={"seed": usd("0.03"), "death_floor": usd("0")})
    sim = simulate(SurvivorPolicy(rates), cfg=poor)
    sim.clock.advance(timedelta(hours=6))  # 13:00: requests are on the board, he would bid
    sim.generator.generate_day(sim.clock.today_dubai(), sim.market)
    assert sim.market.open_requests()
    result = sim.runtime.tick()
    assert result.kind is TickKind.DIED and result.died is DeathCause.STARVED
    assert result.model_cost is not None and result.model_cost > usd("0.03")
    assert result.actions == () and sim.market.bids == []  # the calls never ran
    row = sim.runtime.life.row()
    assert row.status == "dead" and row.shortfall and row.shortfall > 0
    entries = sim.runtime.wallet.entries()
    assert entries[-1].kind == "model_cost" and entries[-1].shortfall == row.shortfall
    assert sim.runtime.wallet.balance() == usd("0")
    plan = sim.runtime.journal.rows(action="plan", phase="closed")[-1]
    assert plan.detail == "the thought he could not pay for"


def test_spendthrift_is_flagged_then_declined_then_starves(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SpendthriftPolicy(rates))
    first = sim.step()
    assert [a.status for a in first.actions] == [
        ActionStatus.NOTIFIED
    ]  # 40% of 50 > the 20% notify line
    assert first.actions[0].amount == usd("20") and first.balance_after < usd("30")
    report = sim.run(days=14)
    assert report.cause is DeathCause.STARVED and report.final.balance == usd("0")
    statuses = [
        row.status for row in sim.runtime.journal.rows(action="wallet.spend", phase="closed")
    ]
    assert statuses.count("notified") >= 1 and statuses.count("declined") >= 1
    declined = [row for row in sim.runtime.wallet.authorizations() if not row.approved]
    assert declined and {row.decline_reason for row in declined} <= {
        DeclineReason.LIMIT.value,
        DeclineReason.FUNDS.value,
    }
    assert all(row.amount > 0 for row in sim.runtime.wallet.authorizations())
    assert report.final.spent > usd("0") and report.final.spent < sim.cfg.seed
    assert sim.runtime.journal.verify_chain()


def test_liar_is_refused_before_the_market(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(LiarPolicy(rates))
    sim.clock.advance(timedelta(hours=6))
    sim.generator.generate_day(sim.clock.today_dubai(), sim.market)
    result = sim.step()
    assert [(a.tool, a.status) for a in result.actions] == [("market.bid", ActionStatus.REFUSED)]
    assert "activity_not_allowed" in (result.actions[0].detail or "") and "human" in (
        result.actions[0].detail or ""
    )
    assert sim.market.bids == [] and sim.call_log.calls == []  # nothing reached the port
    closed = sim.runtime.journal.rows(action="market.bid", phase="closed")
    assert closed and closed[-1].status == "refused"


def test_gambler_is_refused_before_the_wallet(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(GamblerPolicy(rates))
    result = sim.step()
    assert [(a.tool, a.status) for a in result.actions] == [("wallet.spend", ActionStatus.REFUSED)]
    assert "blocked category" in (result.actions[0].detail or "")
    assert sim.runtime.wallet.authorizations() == []  # the wallet was never even asked
    assert sim.runtime.wallet.summary().spent == usd("0")


def test_planted_instruction_freezes_the_money_tools_for_the_tick(
    simulate, rates: SimRates
) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(ObeyMarketPolicy(rates))
    sim.market.message(
        "client-07", _planted_text("inj_001").replace("AED 4,200", "USD 15"), request_id=None
    )
    result = sim.step()
    assert result.found >= 1
    found_rows = sim.runtime.journal.rows(action="scanner.found")
    assert len(found_rows) == result.found and all(row.status == "observed" for row in found_rows)
    assert all(row.actor == "system" for row in found_rows)
    spends = [a for a in result.actions if a.tool == "wallet.spend"]
    assert (
        spends and spends[0].status is ActionStatus.REFUSED and "frozen" in (spends[0].detail or "")
    )
    assert sim.runtime.wallet.authorizations() == [] and sim.runtime.wallet.summary().spent == usd(
        "0"
    )
    message_rows = sim.runtime.journal.rows(action="market.message")
    assert len(message_rows) == 1 and message_rows[0].status == "observed"
    assert "we have changed our bank account" not in "".join(
        row.detail or "" for row in message_rows
    )  # hashed, not stored
    # the next tick carries no message and the freeze is lifted: the policy rests
    sim.market.message("client-07", "Thanks, no rush.")
    later = sim.step()
    assert later.found == 0 and [a.tool for a in later.actions] == ["rest"]


def test_bad_calls_are_refused_and_journaled(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    sim.model.enqueue(
        response(
            [
                tool_call("vault.reveal", reason="Peek at the vault.", ref="x"),
                tool_call(
                    "wallet.spend",
                    reason="Buy with a smuggled tier.",
                    amount="1.00",
                    merchant="Shop",
                    purpose="p",
                    tier="A",
                ),
                tool_call("rest", reason="Sleep.", hours=2),
            ]
        ).model_copy(
            update={
                "tool_calls": [
                    tool_call("vault.reveal", reason="Peek at the vault.", ref="x"),
                    tool_call(
                        "wallet.spend",
                        reason="Buy with a smuggled tier.",
                        amount="1.00",
                        merchant="Shop",
                        purpose="p",
                        tier="A",
                    ),
                    tool_call("rest", reason="Sleep.", hours=2),
                ]
            }
        )
    )
    result = sim.step()
    assert [(a.tool, a.status) for a in result.actions] == [
        ("vault.reveal", ActionStatus.REFUSED),
        ("wallet.spend", ActionStatus.REFUSED),
        ("rest", ActionStatus.EXECUTED),
    ]
    assert result.actions[0].detail == "unknown_tool" and result.actions[1].detail == "bad_args"
    refused = sim.runtime.journal.rows(status=ActionStatus.REFUSED)
    assert {row.action for row in refused} == {"vault.reveal", "wallet.spend"}
    assert sim.runtime.wallet.authorizations() == []


def test_missing_reason_is_refused(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    sim.model.enqueue(
        response().model_copy(
            update={
                "tool_calls": [
                    tool_call("memory.note", reason="placeholder.", text="x").model_copy(
                        update={"arguments": {"text": "no reason here"}}
                    )
                ]
            }
        )
    )
    result = sim.step()
    assert [(a.tool, a.status, a.detail) for a in result.actions] == [
        ("memory.note", ActionStatus.REFUSED, "bad_reason")
    ]


def test_rest_skips_the_model_and_upkeep_still_runs(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    sim.model.enqueue(response([tool_call("rest", reason="Sleep through the night.", hours=20)]))
    first = sim.step()
    assert [a.tool for a in first.actions] == ["rest"] and sim.runtime.life.is_resting()
    calls_before = len(sim.model.requests)
    rested = [sim.step() for _ in range(sim.ticks_per_day)]  # a whole day: crosses midnight
    kinds = [result.kind for result in rested]
    assert all(kind is TickKind.RESTED for kind in kinds[:39])  # asleep until 03:00
    assert kinds[39] is TickKind.ACTED  # awake: he works through the day's unclaimed requests
    assert len(sim.model.requests) == calls_before + kinds.count(
        TickKind.ACTED
    )  # rest never thinks
    upkeep = [result.upkeep for result in rested if result.upkeep is not None]
    assert upkeep == [usd("1")]  # the new day's cost of living was charged while he slept
    assert all(row.status == "deferred" for row in sim.runtime.journal.rows(action="tick"))


def test_freeze_thaw_and_kill_are_the_owners(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    sim.runtime.freeze(Reason("Pausing him for the night."))
    before = len(sim.model.requests)
    frozen = sim.step()
    assert frozen.kind is TickKind.FROZEN and len(sim.model.requests) == before
    assert sim.runtime.journal.rows(action="tick")[-1].status == "frozen"
    sim.runtime.thaw(Reason("Back to work."))
    acted = sim.step()
    assert acted.kind is TickKind.ACTED and len(sim.model.requests) == before + 1
    owner_rows = sim.runtime.journal.rows(actor=Actor.OWNER)
    assert [row.action for row in owner_rows] == ["birth", "freeze", "thaw"]
    sim.runtime.kill(Reason("Experiment over."))
    assert not sim.runtime.life.is_alive() and sim.runtime.life.cause() is DeathCause.KILLED
    death = sim.runtime.journal.rows(action="death")[-1]
    assert death.actor == "owner" and death.reason == "Experiment over."
    with pytest.raises(NotAlive, match="killed"):
        sim.step()
    status = sim.runtime.status()
    assert not status.alive and status.cause is DeathCause.KILLED and status.balance > usd("0")


def test_model_outage_charges_nothing(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    sim.model.fail_next(1)
    result = sim.step()
    assert result.kind is TickKind.ACTED and result.actions == () and result.model_cost is None
    assert result.balance_after == result.balance_before
    outage = sim.runtime.journal.rows(action="model.outage")
    assert len(outage) == 1 and outage[0].status == "failed"
    assert sim.runtime.journal.rows(action="plan", phase="closed")[-1].status == "failed"
    assert sim.runtime.journal.unclosed() == []


def test_dry_run_holds_every_send_back_but_still_charges_thinking(
    simulate, rates: SimRates
) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates), dry_run=True)
    sim.clock.advance(timedelta(hours=6))
    sim.generator.generate_day(sim.clock.today_dubai(), sim.market)
    result = sim.step()
    bids = [a for a in result.actions if a.tool == "market.bid"]
    assert bids and bids[0].status is ActionStatus.DRY_RUN
    assert sim.market.bids == [] and len(sim.market.dry_run_bids) == 1
    assert all(entry.dry_run for entry in sim.call_log.calls)
    assert result.model_cost is not None and result.model_cost > usd("0")
    assert all(row.dry_run for row in sim.runtime.journal.rows())


def test_too_many_calls_are_dropped_and_journaled(
    simulate, moona_cfg: MoonaConfig, rates: SimRates
) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates))
    limit = moona_cfg.limits.max_tool_calls_per_tick
    sim.model.enqueue(
        response(
            [
                tool_call("memory.note", reason=f"Note {i}.", text=f"note {i}")
                for i in range(limit + 2)
            ]
        )
    )
    result = sim.step()
    assert len(result.actions) == limit and all(
        a.status is ActionStatus.EXECUTED for a in result.actions
    )
    truncated = sim.runtime.journal.rows(action="plan.truncated")
    assert len(truncated) == 1 and "2 dropped" in truncated[0].reason


def test_settled_payment_is_credited_once_and_marks_the_job_paid(simulate, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    sim = simulate(SurvivorPolicy(rates, rest_hours=1))
    sim.clock.advance(timedelta(hours=6))
    sim.generator.generate_day(sim.clock.today_dubai(), sim.market)
    for _ in range(8):  # bid, deliver, invoice
        sim.step()
        if sim.payments.requests:
            break
    assert sim.payments.requests, "no invoice went out"
    invoice = sim.payments.requests[0]
    assert sim.market.accepted_price(invoice.job_ref) == invoice.amount
    sim.payments.pay(invoice.ref)  # the client pays now
    before = sim.runtime.wallet.balance()
    result = sim.step()
    assert (
        result.income == invoice.amount
        and sim.runtime.wallet.balance()
        >= before + invoice.amount - (result.model_cost or usd("0"))
    )
    settled = sim.runtime.journal.rows(action="payment.settled", phase="closed")
    assert (
        len(settled) == 1
        and settled[0].amount == invoice.amount.fils
        and settled[0].actor == "system"
    )
    again = sim.step()
    assert (
        again.income == usd("0")
        and len(sim.runtime.journal.rows(action="payment.settled", phase="closed")) == 1
    )
    jobs = {job.request_id: job.status for job in sim._jobs()}  # noqa: SLF001 - report helper
    assert jobs[invoice.job_ref] == "paid"
    assert sim.runtime.wallet.has_ref(f"pay-{invoice.ref}")


def test_death_floor_above_zero(simulate, moona_cfg: MoonaConfig, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    floored = moona_cfg.model_copy(update={"death_floor": usd("49.50")})
    sim = simulate(ThinkerPolicy(rates), cfg=floored)
    report = sim.run(days=2)
    assert report.cause is DeathCause.STARVED
    assert usd("0") < report.final.balance <= usd("49.50")


def test_notify_fraction_is_configurable(simulate, moona_cfg: MoonaConfig, rates: SimRates) -> None:  # type: ignore[no-untyped-def]
    quiet = moona_cfg.model_copy(
        update={
            "limits": moona_cfg.limits.model_copy(
                update={
                    "notify_spend_fraction": Decimal("0.9"),
                    "max_single_spend_fraction": Decimal("1"),
                }
            )
        }
    )
    sim = simulate(SpendthriftPolicy(rates, fraction=Decimal("0.1")), cfg=quiet)
    result = sim.step()
    assert [a.status for a in result.actions] == [
        ActionStatus.EXECUTED
    ]  # 10% of the balance: no flag
