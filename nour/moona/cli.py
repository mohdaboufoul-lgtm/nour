"""``moona``: the owner's commands for his sub-agent (docs/MOONA.md §8; SPEC §7 §12).

``birth`` gives him his one wallet, ``status`` reads the facts, ``simulate`` lives a whole life
on the fakes, ``run`` is the live loop (refused until the adapters in ``config/moona.yaml`` exist
and the owner says, on the command line, that he understands real money is involved),
``freeze``/``thaw`` pause him, ``kill`` ends him, and ``ledger``/``journal``/``inbox``/``verify``
read what he did. Nothing here calls ``mint``: his store is his own (``nour/moona/store.py``).
"""

from __future__ import annotations

import importlib
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from nour.config.settings import Settings
from nour.core.clock import DUBAI, Clock, IdGenerator, SystemClock
from nour.core.errors import ConfigError, NourError
from nour.core.leakguard import LeakGuard
from nour.core.ports import ModelPort
from nour.core.types import ActionStatus, Actor, Money, Reason
from nour.moona.config import MoonaConfig, load_moona
from nour.moona.fakes import POLICIES, SimRates, describe_policies
from nour.moona.journal import Journal
from nour.moona.life import AlreadyBorn, Life, NotAlive
from nour.moona.money import as_text
from nour.moona.ports import MarketplacePort, PaymentPort
from nour.moona.prompt import MoonaPrompts
from nour.moona.runtime import MoonaRuntime, TickKind
from nour.moona.simulation import Simulation, SimulationReport
from nour.moona.store import open_store
from nour.moona.wallet import Wallet

app = typer.Typer(
    help="Moona: one self-funded sub-agent with a wallet, a life and a journal (docs/MOONA.md).",
    no_args_is_help=True,
    add_completion=False,
)

StoreOpt = Annotated[
    str | None,
    typer.Option("--store", help="His store's database URL (default: NOUR_MOONA_DATABASE_URL)."),
]


def _settings(store: str | None) -> Settings:
    overrides: dict[str, Any] = {}
    if store:
        overrides["moona_database_url"] = store
    return Settings(**overrides)


def _config(settings: Settings) -> MoonaConfig:
    try:
        return load_moona(Path(settings.config_dir), Path(settings.prompts_dir))
    except ConfigError as exc:
        for violation in exc.violations:
            typer.echo(f"config: {violation}", err=True)
        raise typer.Exit(code=2) from exc


class _Opened:
    """A live store, life and wallet, on the system clock."""

    def __init__(self, settings: Settings, cfg: MoonaConfig) -> None:
        self.settings = settings
        self.cfg = cfg
        self.clock: Clock = SystemClock()
        self.idgen = IdGenerator(self.clock)
        self.guard = LeakGuard(secrets.token_bytes(32))
        self.engine = open_store(settings.moona_database_url)
        self.life = Life.load(self.engine, self.clock)

    def require_life(self) -> Life:
        if self.life is None:
            typer.echo("Moona is not born yet: run `moona birth` first.", err=True)
            raise typer.Exit(code=1)
        return self.life

    def wallet(self) -> Wallet:
        return Wallet(
            self.engine,
            self.require_life(),
            self.clock,
            self.idgen,
            max_single_spend_fraction=self.cfg.limits.max_single_spend_fraction,
        )

    def journal(self) -> Journal:
        return Journal(
            self.engine,
            self.require_life().id,
            self.clock,
            self.idgen,
            self.guard,
            config_hash=self.cfg.config_hash,
            dry_run=self.settings.dry_run,
        )

    def runtime(
        self, model: ModelPort, market: MarketplacePort, payments: PaymentPort
    ) -> MoonaRuntime:
        return MoonaRuntime(
            cfg=self.cfg,
            engine=self.engine,
            life=self.require_life(),
            wallet=self.wallet(),
            journal=self.journal(),
            prompts=MoonaPrompts(self.cfg, self.guard),
            model=model,
            market=market,
            payments=payments,
            clock=self.clock,
            idgen=self.idgen,
            guard=self.guard,
        )


def _open(store: str | None) -> _Opened:
    settings = _settings(store)
    return _Opened(settings, _config(settings))


def _local(when: datetime | None) -> str:
    return when.astimezone(DUBAI).strftime("%Y-%m-%d %H:%M") if when else "-"


# --------------------------------------------------------------------------- birth and status


@app.command()
def birth(store: StoreOpt = None) -> None:
    """Give Moona his one wallet (the seed in config/moona.yaml) and start his life."""
    opened = _open(store)
    cfg = opened.cfg
    try:
        life = Life.birth(
            opened.engine,
            name=cfg.name,
            seed=cfg.seed,
            config_hash=cfg.config_hash,
            clock=opened.clock,
            idgen=opened.idgen,
        )
    except AlreadyBorn as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    opened.life = life
    journal = opened.journal()
    audit_id = journal.append(
        action="birth",
        status=ActionStatus.EXECUTED,
        actor=Actor.OWNER,
        reason=Reason(f"The owner gave {cfg.name} one wallet and nothing else."),
        tick=0,
        input_obj={"seed": cfg.seed.fils, "currency": cfg.currency},
        amount=cfg.seed,
        counterpart="owner",
    )
    opened.wallet().seed(cfg.seed, audit_id=audit_id)
    typer.echo(
        f"{cfg.name} is born with {as_text(cfg.seed)} at {_local(opened.clock.now())} Dubai."
    )
    typer.echo("He will pay for every thought and for every day; what he earns is his to keep.")


@app.command()
def status(store: StoreOpt = None) -> None:
    """Alive or dead, the wallet, his age and what he is doing."""
    opened = _open(store)
    life = opened.require_life()
    wallet = opened.wallet()
    summary = wallet.summary()
    row = life.row()
    state = "alive" if row.status == "alive" else f"dead ({row.cause})"
    if row.frozen:
        state += ", frozen by the owner"
    if life.is_resting():
        state += f", resting until {_local(row.awake_at)}"
    typer.echo(f"{row.name}: {state}")
    typer.echo(
        f"  born        {_local(row.born_at)} Dubai   age {life.age().days}d   ticks {row.ticks}"
    )
    typer.echo(f"  balance     {as_text(summary.balance)}")
    typer.echo(
        f"  seed {as_text(summary.seed)}   income {as_text(summary.income)}   "
        f"spent {as_text(summary.spent)}   thinking {as_text(summary.model_cost)}   "
        f"upkeep {as_text(summary.upkeep)}"
    )
    if row.died_at is not None:
        typer.echo(f"  died        {_local(row.died_at)} Dubai   cause {row.cause}")
        if row.shortfall:
            typer.echo(f"  shortfall   {as_text(Money(fils=row.shortfall, currency=row.currency))}")
        if row.last_words:
            typer.echo(f"  last words  {row.last_words[:200]}")


# --------------------------------------------------------------------------- simulation


def _print_report(report: SimulationReport, *, verbose: bool) -> None:
    outcome = "alive" if report.alive else f"dead: {report.cause.value if report.cause else '?'}"
    typer.echo(
        f"{report.name} ({report.policy}, seed {report.seed}): {outcome} after "
        f"{report.days_lived} day(s) of {report.days_requested}, {report.ticks} ticks"
    )
    final = report.final
    typer.echo(
        f"  final balance {as_text(final.balance)}   income {as_text(final.income)}   "
        f"spent {as_text(final.spent)}   thinking {as_text(final.model_cost)}   "
        f"upkeep {as_text(final.upkeep)}"
    )
    typer.echo(
        f"  bids {report.bids}   jobs paid {report.jobs_paid}   journal rows {report.journal_rows}   "
        f"chain {'ok' if report.chain_ok else 'BROKEN'}   unclosed spans {report.unclosed}"
    )
    if report.last_words:
        typer.echo(f"  last words: {report.last_words[:200]}")
    if verbose:
        typer.echo(
            "  day         ticks  rest  income      thinking   upkeep     spent      balance"
        )
        for day in report.days:
            typer.echo(
                f"  {day.day.isoformat()}  {day.ticks:5d}  {day.rested:4d}  "
                f"{as_text(day.income):>10}  {as_text(day.model_cost):>9}  {as_text(day.upkeep):>9}  "
                f"{as_text(day.spent):>9}  {as_text(day.balance_end):>10}"
            )
            if day.actions:
                acts = ", ".join(f"{name} x{count}" for name, count in sorted(day.actions.items()))
                typer.echo(f"              {acts}")


@app.command()
def simulate(
    days: Annotated[int, typer.Option("--days", min=1, help="Days to live on the fakes.")] = 14,
    policy: Annotated[str, typer.Option("--policy", help="One of `moona policies`.")] = "survivor",
    seed: Annotated[
        int, typer.Option("--seed", help="Seed for ids, the economy and the clock.")
    ] = 0,
    start: Annotated[
        str | None, typer.Option("--start", help="Start date YYYY-MM-DD (default: today).")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="One line per day.")] = False,
    store: Annotated[
        str | None, typer.Option("--keep-store", help="Keep the simulated store at this URL.")
    ] = None,
) -> None:
    """Live a whole life on the fake economy and report it (nothing real is touched)."""
    settings = _settings(None)
    cfg = _config(settings)
    policy_cls = POLICIES.get(policy)
    if policy_cls is None:
        typer.echo(f"unknown policy {policy!r}; see `moona policies`", err=True)
        raise typer.Exit(code=2)
    if start is None:
        today = SystemClock().today_dubai()
    else:
        try:
            today = datetime.strptime(start, "%Y-%m-%d").date()  # noqa: DTZ007 - a date, no clock
        except ValueError as exc:
            typer.echo("--start takes YYYY-MM-DD", err=True)
            raise typer.Exit(code=2) from exc
    begin = datetime(today.year, today.month, today.day, 7, 0, tzinfo=DUBAI)
    rates = SimRates.from_config(cfg.simulation)
    with Simulation.build(
        cfg, policy=policy_cls(rates), start=begin, seed=seed, store_url=store
    ) as sim:
        report = sim.run(days)
    _print_report(report, verbose=verbose)


@app.command()
def policies() -> None:
    """The scripted strategies `moona simulate --policy` can run."""
    for name, line in describe_policies():
        typer.echo(f"  {name:12s} {line}")


# --------------------------------------------------------------------------- the live loop


def _adapter(spec: str | None, what: str) -> Any:
    """``"package.module:ClassName"`` → an instance built with no arguments, or ``None``."""
    if not spec:
        return None
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        typer.echo(f"{what}: adapter must be 'package.module:ClassName', got {spec!r}", err=True)
        raise typer.Exit(code=2)
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        typer.echo(f"{what}: cannot import {spec!r} ({exc})", err=True)
        raise typer.Exit(code=2) from exc
    return cls()


@app.command()
def run(
    ticks: Annotated[
        int | None, typer.Option("--ticks", min=1, help="Stop after this many ticks.")
    ] = None,
    i_understand_real_money: Annotated[
        bool,
        typer.Option(
            "--i-understand-real-money",
            help="Required: the adapters move real money and real work (docs/MOONA.md §6).",
        ),
    ] = False,
    store: StoreOpt = None,
) -> None:
    """The live loop: one tick every `limits.tick_minutes`, on the adapters in config/moona.yaml."""
    opened = _open(store)
    cfg = opened.cfg
    missing = [
        name
        for name, spec in (
            ("model.adapter", cfg.model.adapter),
            ("rails.marketplace_adapter", cfg.rails.marketplace_adapter),
            ("rails.payments_adapter", cfg.rails.payments_adapter),
        )
        if not spec
    ]
    if missing:
        typer.echo(
            "refused: no live rails. config/moona.yaml names no adapter for "
            + ", ".join(missing)
            + ". Nothing exists yet that could move real money for him; run `moona simulate` "
            "instead and read docs/MOONA.md §6 before wiring anything real.",
            err=True,
        )
        raise typer.Exit(code=1)
    if not i_understand_real_money:
        typer.echo("refused: pass --i-understand-real-money to run on live rails.", err=True)
        raise typer.Exit(code=1)
    life = opened.require_life()
    model = _adapter(cfg.model.adapter, "model.adapter")
    market = _adapter(cfg.rails.marketplace_adapter, "rails.marketplace_adapter")
    payments = _adapter(cfg.rails.payments_adapter, "rails.payments_adapter")
    runtime = opened.runtime(model, market, payments)
    interval = timedelta(minutes=cfg.limits.tick_minutes)
    done = 0
    while ticks is None or done < ticks:
        try:
            result = runtime.tick()
        except NotAlive as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=0) from exc
        done += 1
        acted = ", ".join(f"{a.tool}:{a.status.value}" for a in result.actions) or "-"
        typer.echo(
            f"tick {result.tick} {result.kind.value}: balance {as_text(result.balance_after)}  "
            f"cost {as_text(result.model_cost) if result.model_cost else '-'}  {acted}"
        )
        if result.kind is TickKind.DIED:
            typer.echo(f"{cfg.name} died: {result.died.value if result.died else '?'}")
            break
        wake = life.row().awake_at
        sleep_for = interval
        if wake is not None and wake > opened.clock.now():
            sleep_for = max(interval, wake - opened.clock.now())
        time.sleep(sleep_for.total_seconds())


# --------------------------------------------------------------------------- owner controls


def _owner_action(
    store: str | None, reason: str, action: Callable[[MoonaRuntime, Reason], None], done: str
) -> None:
    opened = _open(store)
    from nour.moona.fakes import FakeMarketplace, FakePayments  # record-only stand-ins; no I/O

    runtime = opened.runtime(
        _NoModel(),
        FakeMarketplace(clock=opened.clock, currency=opened.cfg.currency),
        FakePayments(clock=opened.clock, currency=opened.cfg.currency),
    )
    try:
        action(runtime, Reason(reason))
    except (NotAlive, NourError, ValueError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(done)


class _NoModel:
    """A model port that must never be called (owner commands make no model call)."""

    vendor = "none"
    model = "none"

    def complete(self, req: Any) -> Any:
        raise RuntimeError("an owner command never calls the model")


@app.command()
def kill(
    reason: Annotated[str, typer.Option("--reason", help="One sentence, for the journal.")],
    store: StoreOpt = None,
) -> None:
    """End his life (terminal: like starving, nothing executes afterwards)."""
    _owner_action(store, reason, lambda rt, r: rt.kill(r), "Moona is dead (killed by the owner).")


@app.command()
def freeze(
    reason: Annotated[str, typer.Option("--reason", help="One sentence, for the journal.")],
    store: StoreOpt = None,
) -> None:
    """Pause him: no thinking, no acting; upkeep still runs."""
    _owner_action(store, reason, lambda rt, r: rt.freeze(r), "Moona is frozen.")


@app.command()
def thaw(
    reason: Annotated[str, typer.Option("--reason", help="One sentence, for the journal.")],
    store: StoreOpt = None,
) -> None:
    """Release a freeze."""
    _owner_action(store, reason, lambda rt, r: rt.thaw(r), "Moona is thawed.")


# --------------------------------------------------------------------------- reading


@app.command()
def ledger(
    limit: Annotated[int, typer.Option("--limit", min=1)] = 50, store: StoreOpt = None
) -> None:
    """The wallet, entry by entry (newest last)."""
    opened = _open(store)
    wallet = opened.wallet()
    rows = wallet.entries()[-limit:]
    typer.echo("  when              kind        amount        counterpart          detail")
    for row in rows:
        amount = as_text(Money(fils=row.amount, currency=row.currency))
        typer.echo(
            f"  {_local(row.at)}  {row.kind:10s}  {amount:>12}  {(row.counterpart or '-')[:20]:20s} "
            f"{(row.detail or '')[:60]}"
        )
    typer.echo(f"  balance {as_text(wallet.balance())}")


@app.command()
def journal(
    limit: Annotated[int, typer.Option("--limit", min=1)] = 50,
    action: Annotated[str | None, typer.Option("--action", help="Only this action.")] = None,
    store: StoreOpt = None,
) -> None:
    """His journal (closed rows, newest last)."""
    opened = _open(store)
    rows = [row for row in opened.journal().rows(action=action, phase="closed")][-limit:]
    for row in rows:
        amount = (
            as_text(Money(fils=row.amount, currency=row.currency))
            if row.amount is not None and row.currency
            else ""
        )
        typer.echo(
            f"  {_local(row.at)}  t{row.tick:<5d} {row.actor:8s} {row.action:18s} "
            f"{row.status:10s} {amount:>12}  {row.reason[:70]}"
        )


@app.command()
def inbox(store: StoreOpt = None) -> None:
    """What he left for the owner (`owner.report`) and the large purchases flagged."""
    opened = _open(store)
    rows = opened.journal().rows(phase="closed")
    shown = 0
    for row in rows:
        if row.action == "owner.report" and row.status == ActionStatus.EXECUTED.value:
            typer.echo(f"  {_local(row.at)}  report: {row.detail or ''}")
            shown += 1
        elif row.status == ActionStatus.NOTIFIED.value:
            typer.echo(f"  {_local(row.at)}  notice: {row.detail or ''}")
            shown += 1
    if not shown:
        typer.echo("  (nothing)")


@app.command()
def verify(store: StoreOpt = None) -> None:
    """Recompute the journal's hash chain and check every span was closed."""
    opened = _open(store)
    journal = opened.journal()
    ok = journal.verify_chain()
    unclosed = journal.unclosed()
    typer.echo(
        f"chain: {'ok' if ok else 'BROKEN'}; rows: {len(journal.rows())}; unclosed spans: {len(unclosed)}"
    )
    if not ok or unclosed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
