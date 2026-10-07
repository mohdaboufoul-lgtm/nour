"""``moona simulate``: a whole life on the fakes and a fake clock (docs/MOONA.md §5; SPEC §8
"any new playbook runs in simulation … before going live", §16 the 48-hour dry run).

Everything a live run would wire (``nour/moona/cli.py``) is wired here on the fakes instead:
the store on a temporary SQLite file, ``FakeClock`` driving the ORM defaults through
``set_process_clock``, a ``ScriptedModel`` answering from one of the policies in
``nour.moona.fakes``, the fake marketplace fed by the ``GigGenerator``, the fake payment rail.
The clock advances ``tick_minutes`` between ticks and the generator posts each day's requests at
the start of the day. The report is what the owner reads before deciding whether Moona should
ever touch a real rail.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine

from nour.core.clock import FakeClock, IdGenerator, process_clock, set_process_clock
from nour.core.leakguard import LeakGuard
from nour.core.ports import CallLog
from nour.core.types import ActionStatus, Actor, Money, Reason
from nour.fakes.model import ScriptedModel
from nour.moona.config import MoonaConfig
from nour.moona.fakes import FakeMarketplace, FakePayments, GigGenerator, MoonaPolicy
from nour.moona.journal import Journal
from nour.moona.life import DeathCause, Life
from nour.moona.prompt import MoonaPrompts
from nour.moona.runtime import MINUTES_PER_DAY, MoonaRuntime, TickKind, TickResult
from nour.moona.store import JobRow, open_store, reading
from nour.moona.wallet import Wallet, WalletSummary

SIM_VENDOR = "moona_sim"
"""The scripted model's vendor id in a simulation (``^[a-z][a-z0-9_]+$``, as the config wants)."""


@dataclass
class DaySummary:
    day: date
    ticks: int = 0
    rested: int = 0
    income: Money = field(default_factory=Money.zero)
    model_cost: Money = field(default_factory=Money.zero)
    upkeep: Money = field(default_factory=Money.zero)
    spent: Money = field(default_factory=Money.zero)
    actions: Counter[str] = field(default_factory=Counter)
    found: int = 0
    balance_end: Money = field(default_factory=Money.zero)


@dataclass(frozen=True)
class SimulationReport:
    """A whole simulated life, day by day. ``days_lived`` counts 24-hour periods lived, rounded
    up (a life that ends mid-day counts the day); ``days`` is the calendar table."""

    name: str
    policy: str
    seed: int
    days_requested: int
    days_lived: int
    ticks: int
    born_at: datetime
    died_at: datetime | None
    cause: DeathCause | None
    last_words: str | None
    final: WalletSummary
    days: tuple[DaySummary, ...]
    chain_ok: bool
    unclosed: int
    journal_rows: int
    bids: int
    jobs_paid: int

    @property
    def alive(self) -> bool:
        return self.cause is None


class Simulation:
    """One simulated life: ``build`` wires it, ``run`` lives it, ``close`` releases the clock."""

    def __init__(
        self,
        *,
        cfg: MoonaConfig,
        policy: MoonaPolicy,
        clock: FakeClock,
        previous_clock: object,
        engine_dir: Path | None,
        engine: Engine,
        runtime: MoonaRuntime,
        market: FakeMarketplace,
        payments: FakePayments,
        generator: GigGenerator,
        model: ScriptedModel,
        call_log: CallLog,
        seed: int,
    ) -> None:
        self.cfg = cfg
        self.policy = policy
        self.clock = clock
        self._previous_clock = previous_clock
        self._engine_dir = engine_dir
        self.engine = engine
        self.runtime = runtime
        self.market = market
        self.payments = payments
        self.generator = generator
        self.model = model
        self.call_log = call_log
        self.seed = seed
        self.results: list[TickResult] = []
        self.result_days: list[date] = []
        self._closed = False

    @classmethod
    def build(
        cls,
        cfg: MoonaConfig,
        *,
        policy: MoonaPolicy,
        start: datetime,
        seed: int = 0,
        store_url: str | None = None,
        dry_run: bool = False,
    ) -> Simulation:
        """Wire a life at ``start`` (tz-aware) on the fakes. ``store_url`` defaults to a fresh
        temporary SQLite file; the ``LeakGuard`` key derives from the seed (no secret is
        registered in a simulation, so the key only has to exist)."""
        previous = process_clock()
        clock = FakeClock(start)
        set_process_clock(clock)
        engine_dir: Path | None = None
        try:
            idgen = IdGenerator(clock, seed=seed)
            guard = LeakGuard(hashlib.sha256(f"moona-sim-{seed}".encode()).digest())
            if store_url is None:
                engine_dir = Path(tempfile.mkdtemp(prefix="moona-sim-"))
                store_url = f"sqlite+pysqlite:///{engine_dir / 'moona.sqlite3'}"
            engine = open_store(store_url)
            life = Life.birth(
                engine,
                name=cfg.name,
                seed=cfg.seed,
                config_hash=cfg.config_hash,
                clock=clock,
                idgen=idgen,
            )
            journal = Journal(
                engine, life.id, clock, idgen, guard, config_hash=cfg.config_hash, dry_run=dry_run
            )
            wallet = Wallet(
                engine,
                life,
                clock,
                idgen,
                max_single_spend_fraction=cfg.limits.max_single_spend_fraction,
            )
            birth_id = journal.append(
                action="birth",
                status=ActionStatus.EXECUTED,
                actor=Actor.OWNER,
                reason=Reason(f"The owner gave {cfg.name} one wallet and nothing else."),
                tick=0,
                input_obj={"seed": cfg.seed.fils, "currency": cfg.currency, "simulation": True},
                amount=cfg.seed,
                counterpart="owner",
            )
            wallet.seed(cfg.seed, audit_id=birth_id)
            call_log = CallLog(clock)
            market = FakeMarketplace(call_log, clock, currency=cfg.currency)
            payments = FakePayments(call_log, clock, market=market, currency=cfg.currency)
            generator = GigGenerator(cfg.simulation, clock, currency=cfg.currency, seed=seed)
            model = ScriptedModel(SIM_VENDOR, "scripted", policy, call_log=call_log, clock=clock)
            runtime = MoonaRuntime(
                cfg=cfg,
                engine=engine,
                life=life,
                wallet=wallet,
                journal=journal,
                prompts=MoonaPrompts(cfg, guard),
                model=model,
                market=market,
                payments=payments,
                clock=clock,
                idgen=idgen,
                guard=guard,
            )
        except BaseException:
            set_process_clock(previous)
            clock.close()
            if engine_dir is not None:
                shutil.rmtree(engine_dir, ignore_errors=True)
            raise
        return cls(
            cfg=cfg,
            policy=policy,
            clock=clock,
            previous_clock=previous,
            engine_dir=engine_dir,
            engine=engine,
            runtime=runtime,
            market=market,
            payments=payments,
            generator=generator,
            model=model,
            call_log=call_log,
            seed=seed,
        )

    @property
    def ticks_per_day(self) -> int:
        return max(1, MINUTES_PER_DAY // self.cfg.limits.tick_minutes)

    def step(self) -> TickResult:
        """One tick: post the day's requests if new, tick, advance the clock."""
        today = self.clock.today_dubai()
        self.generator.generate_day(today, self.market)
        result = self.runtime.tick()
        self.results.append(result)
        self.result_days.append(today)
        self.clock.advance(timedelta(minutes=self.cfg.limits.tick_minutes))
        return result

    def run(self, days: int) -> SimulationReport:
        """Live ``days`` days (or until death) and report."""
        if days < 1:
            raise ValueError("a simulation runs at least one day")
        life = self.runtime.life
        for _ in range(days * self.ticks_per_day):
            if not life.is_alive():
                break
            self.step()
        return self.report(days)

    def report(self, days_requested: int) -> SimulationReport:
        life = self.runtime.life
        row = life.row()
        currency = self.cfg.currency
        by_day: dict[date, DaySummary] = {}
        zero = Money.zero(currency)
        for day, result in zip(self.result_days, self.results, strict=True):
            summary = by_day.setdefault(
                day,
                DaySummary(
                    day=day,
                    income=zero,
                    model_cost=zero,
                    upkeep=zero,
                    spent=zero,
                    balance_end=zero,
                ),
            )
            summary.ticks += 1
            if result.kind in (TickKind.RESTED, TickKind.FROZEN):
                summary.rested += 1
            summary.income = summary.income + result.income
            if result.model_cost is not None:
                summary.model_cost = summary.model_cost + result.model_cost
            if result.upkeep is not None:
                summary.upkeep = summary.upkeep + result.upkeep
            for action in result.actions:
                summary.actions[f"{action.tool}:{action.status.value}"] += 1
                if action.tool == "wallet.spend" and action.status in (
                    ActionStatus.EXECUTED,
                    ActionStatus.NOTIFIED,
                ):
                    summary.spent = summary.spent + (action.amount or zero)
            summary.found += result.found
            summary.balance_end = result.balance_after
        journal = self.runtime.journal
        rows = journal.rows()
        paid = sum(1 for job in self._jobs() if job.status == "paid")
        return SimulationReport(
            name=row.name,
            policy=getattr(self.policy, "name", type(self.policy).__name__),
            seed=self.seed,
            days_requested=days_requested,
            days_lived=-(-(len(self.results) * self.cfg.limits.tick_minutes) // MINUTES_PER_DAY),
            ticks=len(self.results),
            born_at=row.born_at,
            died_at=row.died_at,
            cause=DeathCause(row.cause) if row.cause else None,
            last_words=row.last_words,
            final=self.runtime.wallet.summary(),
            days=tuple(by_day[day] for day in sorted(by_day)),
            chain_ok=journal.verify_chain(),
            unclosed=len(journal.unclosed()),
            journal_rows=len(rows),
            bids=len(self.market.bids),
            jobs_paid=paid,
        )

    def _jobs(self) -> list[JobRow]:
        with reading(self.engine) as session:
            return list(session.scalars(select(JobRow)).all())

    def close(self) -> None:
        """Restore the process clock, stop freezegun, drop the temporary store."""
        if self._closed:
            return
        self._closed = True
        set_process_clock(self._previous_clock)  # type: ignore[arg-type]
        self.clock.close()
        self.engine.dispose()
        if self._engine_dir is not None:
            shutil.rmtree(self._engine_dir, ignore_errors=True)

    def __enter__(self) -> Simulation:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
