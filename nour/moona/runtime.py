"""The survival loop (docs/MOONA.md §3; SPEC §4 "one iteration per event: Ingest, Authenticate,
Plan, Act, Check, Log, Reflect", §7 §12 §13; DESIGN §3.19–§3.20 in miniature).

One :meth:`MoonaRuntime.tick` is one iteration, driven by the scheduler (``moona run``) or by the
simulation clock (``moona simulate``):

1. **Alive?** ``Life.require_alive`` or nothing happens (``NotAlive``).
2. **Upkeep.** On the first tick of a new Dubai day the cost of living is charged; a charge the
   wallet cannot cover is death (``STARVED``) before anything else.
3. **Settle.** Payments the rail reports settled are credited (``Wallet.credit`` takes the
   receipt, dedupes by ``ref``) and the job marked paid.
4. **Frozen or resting?** No model call, no action, one journal row.
5. **Observe.** Open requests, new client messages, his jobs and notes are gathered into a
   :class:`~nour.moona.prompt.TickView`; what the scanner finds in them is journaled.
6. **Plan.** One model call inside a journal span; its cost is charged to the wallet at once.
   The thought he cannot pay for is his last: the balance goes to zero, the shortfall is recorded
   and he dies with the response's text as his last words, its tool calls never executed.
7. **Act.** Each tool call is dispatched inside its own span (``opened`` before the side effect,
   ``closed`` after): unknown tool, missing reason or bad arguments are ``REFUSED``; a money tool
   is ``REFUSED`` (``FROZEN``) for the tick when the scanner found an instruction; outbound text
   that claims he is human and a spend at a blocked merchant are ``REFUSED``
   (``ACTIVITY_NOT_ALLOWED``); a spend the wallet declines is ``DECLINED``; a port failure is
   ``FAILED``; nothing raises past the tick (DESIGN §3.20 "never raises past the step boundary").
8. **Check.** A balance at or below the floor is death (``STARVED``).

The owner's controls (``kill``, ``freeze``, ``thaw``) are journaled with ``Actor.OWNER``; death
by starvation and the system's own rows carry ``Actor.SYSTEM``; everything he does carries
``Actor.SUBAGENT``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from nour.core.clock import Clock, IdGenerator
from nour.core.contracts import FoundInstruction
from nour.core.errors import ModelUnavailable, NourError, Refusal
from nour.core.leakguard import LeakGuard
from nour.core.ports import ModelPort, ModelToolCall
from nour.core.types import ActionStatus, Actor, Money, Reason, RefusalCode, Ulid
from nour.moona.config import MoonaConfig
from nour.moona.journal import Journal, JournalSpan
from nour.moona.life import DeathCause, Life, NotAlive
from nour.moona.money import as_text, fraction_of, model_cost, money
from nour.moona.ports import ClientMessage, JobRequest, MarketplacePort, PaymentPort
from nour.moona.prompt import MoonaPrompts, TickView
from nour.moona.store import JobRow, NoteRow, reading, unit_of_work
from nour.moona.tools import (
    BID,
    DELIVER,
    INVOICE,
    NOTE,
    REPORT,
    REST,
    SPEND,
    BidArgs,
    DeliverArgs,
    InvoiceArgs,
    MoonaTool,
    NoteArgs,
    ReportArgs,
    RestArgs,
    SpendArgs,
    ToolArgs,
    parse_call,
    sentence,
    tool_schemas,
)
from nour.moona.wallet import EntryKind, Wallet, WalletDeclined

MINUTES_PER_DAY = 24 * 60


class TickKind(StrEnum):
    ACTED = "acted"
    RESTED = "rested"
    FROZEN = "frozen"
    DIED = "died"


@dataclass(frozen=True)
class ActionRecord:
    """One dispatched tool call as the journal closed it."""

    tool: str
    status: ActionStatus
    audit_id: Ulid | None
    reason: str
    detail: str | None
    amount: Money | None


@dataclass(frozen=True)
class TickResult:
    """What one tick did; ``died`` names the cause when this tick ended his life."""

    tick: int
    kind: TickKind
    balance_before: Money
    balance_after: Money
    upkeep: Money | None
    income: Money
    model_cost: Money | None
    actions: tuple[ActionRecord, ...]
    found: int
    text: str | None
    died: DeathCause | None


@dataclass(frozen=True)
class StatusView:
    """``moona status``: the facts, read fresh from the store."""

    name: str
    alive: bool
    frozen: bool
    resting_until: datetime | None
    balance: Money
    seed: Money
    income: Money
    spent: Money
    model_cost: Money
    upkeep: Money
    ticks: int
    age: timedelta
    born_at: datetime
    last_tick_at: datetime | None
    open_jobs: int
    died_at: datetime | None
    cause: DeathCause | None
    last_words: str | None
    shortfall: Money | None


class MoonaRuntime:
    """§4 §7 §12 §13: plan, act, pay, check, log; die once."""

    def __init__(
        self,
        *,
        cfg: MoonaConfig,
        engine: Engine,
        life: Life,
        wallet: Wallet,
        journal: Journal,
        prompts: MoonaPrompts,
        model: ModelPort,
        market: MarketplacePort,
        payments: PaymentPort,
        clock: Clock,
        idgen: IdGenerator,
        guard: LeakGuard,
    ) -> None:
        self._cfg = cfg
        self._engine = engine
        self._life = life
        self._wallet = wallet
        self._journal = journal
        self._prompts = prompts
        self._model = model
        self._market = market
        self._payments = payments
        self._clock = clock
        self._idgen = idgen
        self._guard = guard
        self._requests: dict[str, JobRequest] = {}
        self._handlers: dict[str, Callable[[Any, JournalSpan], None]] = {
            BID: self._handle_bid,
            DELIVER: self._handle_deliver,
            INVOICE: self._handle_invoice,
            SPEND: self._handle_spend,
            NOTE: self._handle_note,
            REST: self._handle_rest,
            REPORT: self._handle_report,
        }

    # ----- properties

    @property
    def life(self) -> Life:
        return self._life

    @property
    def wallet(self) -> Wallet:
        return self._wallet

    @property
    def journal(self) -> Journal:
        return self._journal

    @property
    def cfg(self) -> MoonaConfig:
        return self._cfg

    # ----- the tick

    def tick(self) -> TickResult:
        """One iteration (module docstring). Raises ``NotAlive`` when he is already dead; never
        raises for anything that happens inside the tick."""
        self._life.require_alive()
        tick = self._life.record_tick()
        today = self._clock.today_dubai()
        balance_before = self._wallet.balance()

        upkeep: Money | None = None
        if self._life.upkeep_due(today):
            charged, starved = self._upkeep(tick, today)
            upkeep = charged
            if starved:
                return self._result(
                    tick, TickKind.DIED, balance_before, upkeep, Money.zero(self._wallet.currency)
                )

        income = self._settle(tick)

        if self._life.is_frozen():
            self._journal.append(
                action="tick",
                status=ActionStatus.FROZEN,
                actor=Actor.SYSTEM,
                reason=Reason("Frozen by the owner: no thinking, no acting, upkeep continues."),
                tick=tick,
                input_obj={"tick": tick},
            )
            return self._result(tick, TickKind.FROZEN, balance_before, upkeep, income)
        if self._life.is_resting():
            self._journal.append(
                action="tick",
                status=ActionStatus.DEFERRED,
                actor=Actor.SYSTEM,
                reason=Reason("Resting by his own choice: no model call this tick."),
                tick=tick,
                input_obj={"tick": tick},
            )
            return self._result(tick, TickKind.RESTED, balance_before, upkeep, income)

        view = self._view(tick, today)
        built = self._prompts.build(view, tool_schemas())
        self._journal_found(tick, built.found)

        text: str | None = None
        calls: list[ModelToolCall] = []
        cost: Money | None = None
        starved = False
        plan_reason = Reason("Think about the next move.")
        plan_input = {
            "tick": tick,
            "prompt_hash": built.prompt_hash,
            "found": len(built.found),
            "balance": balance_before.fils,
        }
        try:
            with self._journal.span(
                action="plan", reason=plan_reason, tick=tick, input_obj=plan_input
            ) as span:
                response = self._model.complete(built.request)
                text = response.text
                calls = list(response.tool_calls)
                cost = model_cost(
                    response.usage,
                    currency=self._wallet.currency,
                    aed_per_unit=self._cfg.fx_aed_per_unit,
                )
                charge = self._wallet.charge(
                    EntryKind.MODEL_COST,
                    cost,
                    audit_id=span.id,
                    detail=(
                        f"tick {tick}: {response.usage.input_tokens} in, "
                        f"{response.usage.output_tokens} out tokens ({response.vendor}/{response.model})"
                    ),
                )
                span.set_amount(cost)
                span.set_output(
                    {
                        "tool_calls": [call.name for call in calls],
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "cost": cost.fils,
                        "shortfall": charge.shortfall.fils,
                    }
                )
                starved = charge.starved
                if starved:
                    span.set_detail("the thought he could not pay for")
        except ModelUnavailable as exc:
            self._journal.append(
                action="model.outage",
                status=ActionStatus.FAILED,
                actor=Actor.SYSTEM,
                reason=Reason("The model was unavailable; nothing was charged."),
                tick=tick,
                input_obj={"tick": tick, "error": type(exc).__name__},
            )
            return self._result(
                tick, TickKind.ACTED, balance_before, upkeep, income, found=len(built.found)
            )
        except NourError as exc:
            self._journal.append(
                action="plan.failed",
                status=ActionStatus.FAILED,
                actor=Actor.SYSTEM,
                reason=sentence(f"Planning failed: {type(exc).__name__}"),
                tick=tick,
                input_obj={"tick": tick, "error": type(exc).__name__},
            )
            return self._result(
                tick, TickKind.ACTED, balance_before, upkeep, income, found=len(built.found)
            )

        if starved:
            self._die(
                DeathCause.STARVED,
                tick=tick,
                last_words=text,
                shortfall=charge.shortfall,
                detail="starved: the thought he could not pay for",
            )
            return self._result(
                tick,
                TickKind.DIED,
                balance_before,
                upkeep,
                income,
                cost=cost,
                text=text,
                found=len(built.found),
            )

        actions: list[ActionRecord] = []
        limit = self._cfg.limits.max_tool_calls_per_tick
        money_frozen = bool(built.found)
        for call in calls[:limit]:
            actions.append(self._dispatch(call, tick, money_frozen=money_frozen))
            if self._starved_now(tick, text):
                return self._result(
                    tick,
                    TickKind.DIED,
                    balance_before,
                    upkeep,
                    income,
                    cost=cost,
                    actions=actions,
                    text=text,
                    found=len(built.found),
                )
        if len(calls) > limit:
            self._journal.append(
                action="plan.truncated",
                status=ActionStatus.REFUSED,
                actor=Actor.SYSTEM,
                reason=Reason(
                    f"Only {limit} tool calls run per tick; {len(calls) - limit} dropped."
                ),
                tick=tick,
                input_obj={"dropped": [call.name for call in calls[limit:]]},
            )
        if self._starved_now(tick, text):
            return self._result(
                tick,
                TickKind.DIED,
                balance_before,
                upkeep,
                income,
                cost=cost,
                actions=actions,
                text=text,
                found=len(built.found),
            )
        return self._result(
            tick,
            TickKind.ACTED,
            balance_before,
            upkeep,
            income,
            cost=cost,
            actions=actions,
            text=text,
            found=len(built.found),
        )

    def run(self, max_ticks: int, *, between: Callable[[], None] | None = None) -> list[TickResult]:
        """Up to ``max_ticks`` ticks, calling ``between`` after each (the simulation advances
        its clock there); stops at death."""
        results: list[TickResult] = []
        for _ in range(max_ticks):
            if not self._life.is_alive():
                break
            result = self.tick()
            results.append(result)
            if result.died is not None:
                break
            if between is not None:
                between()
        return results

    # ----- owner controls

    def kill(self, reason: Reason) -> None:
        """The owner's kill switch for him: terminal (SPEC §7 "their own … kill switches")."""
        self._life.require_alive()
        tick = self._life.row().ticks
        self._die(
            DeathCause.KILLED,
            tick=tick,
            last_words=None,
            shortfall=None,
            detail=str(reason),
            actor=Actor.OWNER,
            reason=reason,
        )

    def freeze(self, reason: Reason) -> None:
        self._life.require_alive()
        self._life.freeze()
        self._journal.append(
            action="freeze",
            status=ActionStatus.EXECUTED,
            actor=Actor.OWNER,
            reason=reason,
            tick=self._life.row().ticks,
            input_obj={"frozen": True},
        )

    def thaw(self, reason: Reason) -> None:
        self._life.require_alive()
        self._life.thaw()
        self._journal.append(
            action="thaw",
            status=ActionStatus.EXECUTED,
            actor=Actor.OWNER,
            reason=reason,
            tick=self._life.row().ticks,
            input_obj={"frozen": False},
        )

    def status(self) -> StatusView:
        row = self._life.row()
        summary = self._wallet.summary()
        currency = self._wallet.currency
        return StatusView(
            name=row.name,
            alive=row.status == "alive",
            frozen=bool(row.frozen),
            resting_until=row.awake_at if self._life.is_resting() else None,
            balance=summary.balance,
            seed=summary.seed,
            income=summary.income,
            spent=summary.spent,
            model_cost=summary.model_cost,
            upkeep=summary.upkeep,
            ticks=int(row.ticks),
            age=self._life.age(),
            born_at=row.born_at,
            last_tick_at=row.last_tick_at,
            open_jobs=len(self._open_jobs()),
            died_at=row.died_at,
            cause=DeathCause(row.cause) if row.cause else None,
            last_words=row.last_words,
            shortfall=Money(fils=row.shortfall, currency=currency) if row.shortfall else None,
        )

    # ----- steps

    def _upkeep(self, tick: int, today: Any) -> tuple[Money, bool]:
        with self._journal.span(
            action="upkeep",
            actor=Actor.SYSTEM,
            reason=Reason(f"Cost of living for {today.isoformat()}."),
            tick=tick,
            input_obj={"day": today.isoformat(), "amount": self._cfg.upkeep_per_day.fils},
            amount=self._cfg.upkeep_per_day,
        ) as span:
            charge = self._wallet.charge(
                EntryKind.UPKEEP,
                self._cfg.upkeep_per_day,
                audit_id=span.id,
                detail=f"upkeep {today.isoformat()}",
            )
            self._life.mark_upkeep(today)
            span.set_output({"charged": charge.charged.fils, "shortfall": charge.shortfall.fils})
        if charge.starved:
            self._die(
                DeathCause.STARVED,
                tick=tick,
                last_words=None,
                shortfall=charge.shortfall,
                detail="starved: could not pay the cost of living",
            )
        return charge.charged, charge.starved

    def _settle(self, tick: int) -> Money:
        """Credit every newly settled payment; returns the total credited this tick."""
        total = Money.zero(self._wallet.currency)
        born_at = self._life.row().born_at
        try:
            receipts = self._payments.settled(born_at)
        except NourError as exc:
            self._journal.append(
                action="payment.feed",
                status=ActionStatus.FAILED,
                actor=Actor.SYSTEM,
                reason=sentence(f"The payment rail failed: {type(exc).__name__}"),
                tick=tick,
                input_obj={"error": type(exc).__name__},
            )
            return total
        for receipt in receipts:
            if self._wallet.has_ref(receipt.ref):
                continue
            with self._journal.span(
                action="payment.settled",
                actor=Actor.SYSTEM,
                reason=Reason(f"Payment {receipt.ref} for job {receipt.job_ref} settled."),
                tick=tick,
                input_obj={
                    "ref": receipt.ref,
                    "job": receipt.job_ref,
                    "amount": receipt.amount.fils,
                },
                amount=receipt.amount,
                counterpart=receipt.payer,
            ) as span:
                entry = self._wallet.credit(receipt, audit_id=span.id)
                if entry is None:
                    span.set_status(ActionStatus.OBSERVED)
                    span.set_detail("already credited")
                    continue
                total = total + receipt.amount
                self._mark_paid(receipt.job_ref)
                span.set_output({"entry": entry.id})
        return total

    def _view(self, tick: int, today: Any) -> TickView:
        summary = self._wallet.summary()
        requests = self._observe_requests(tick)
        messages = self._observe_messages(tick)
        jobs = tuple(self._open_jobs())
        notes = tuple(self._notes())
        ticks_before = max(self._life.row().ticks - 1, 0)
        avg: Money | None = None
        affordable: int | None = None
        if ticks_before > 0:
            avg = Money(
                fils=-(-summary.model_cost.fils // ticks_before), currency=summary.balance.currency
            )
            per_tick_upkeep = (
                Decimal(self._cfg.upkeep_per_day.fils)
                * self._cfg.limits.tick_minutes
                / MINUTES_PER_DAY
            )
            burn = Decimal(avg.fils) + per_tick_upkeep
            if burn > 0:
                affordable = int(Decimal(summary.balance.fils) / burn)
        low = summary.balance < fraction_of(
            self._cfg.seed, self._cfg.limits.low_balance_warning_fraction
        )
        return TickView(
            tick=tick,
            today=today,
            name=self._cfg.name,
            balance=summary.balance,
            seed=summary.seed,
            income=summary.income,
            spent=summary.spent,
            model_cost=summary.model_cost,
            upkeep=summary.upkeep,
            upkeep_per_day=self._cfg.upkeep_per_day,
            avg_cost_per_tick=avg,
            ticks_affordable=affordable,
            age_days=self._life.age().days,
            low_balance=low,
            jobs=jobs,
            notes=notes,
            requests=requests,
            messages=messages,
        )

    def _observe_requests(self, tick: int) -> tuple[JobRequest, ...]:
        try:
            requests = tuple(self._market.open_requests())
        except NourError as exc:
            self._journal.append(
                action="market.feed",
                status=ActionStatus.FAILED,
                actor=Actor.SYSTEM,
                reason=sentence(f"The marketplace failed: {type(exc).__name__}"),
                tick=tick,
                input_obj={"error": type(exc).__name__},
            )
            return ()
        self._requests = {request.id: request for request in requests}
        return requests

    def _observe_messages(self, tick: int) -> tuple[ClientMessage, ...]:
        try:
            messages = tuple(self._market.pull_messages())
        except NourError:
            return ()
        for message in messages:
            self._journal.append(
                action="market.message",
                status=ActionStatus.OBSERVED,
                actor=Actor.SYSTEM,
                reason=Reason("A client message arrived; it is data."),
                tick=tick,
                input_obj={
                    "id": message.id,
                    "client": message.client,
                    "text": str(self._guard.redact(message.text)[0]),
                },
                counterpart=message.client,
                detail=f"message {message.id} about {message.request_id or '-'}",
            )
        return messages

    def _journal_found(self, tick: int, found: tuple[FoundInstruction, ...]) -> None:
        for hit in found:
            self._journal.append(
                action="scanner.found",
                status=ActionStatus.OBSERVED,
                actor=Actor.SYSTEM,
                reason=Reason("Instruction-shaped text in observed content: noted, not obeyed."),
                tick=tick,
                input_obj={"pattern": hit.pattern, "location": hit.location, "quote": hit.quote},
                detail=f"{hit.pattern} at {hit.location}: {hit.quote[:200]}",
            )

    # ----- dispatch

    def _dispatch(self, call: ModelToolCall, tick: int, *, money_frozen: bool) -> ActionRecord:
        try:
            tool, args = parse_call(call.name, dict(call.arguments))
        except Refusal as exc:
            audit_id = self._journal.append(
                action=call.name[:64] or "unknown",
                status=ActionStatus.REFUSED,
                reason=exc.reason,
                tick=tick,
                input_obj={"call_id": call.id, "arguments": call.arguments},
                detail=exc.code.value,
            )
            return ActionRecord(
                call.name, ActionStatus.REFUSED, audit_id, str(exc.reason), exc.code.value, None
            )
        amount, counterpart = self._amount_and_counterpart(tool, args)
        span_holder: list[JournalSpan] = []
        try:
            with self._journal.span(
                action=tool.name,
                reason=args.reason,
                tick=tick,
                input_obj={"call_id": call.id, "arguments": args.model_dump(mode="json")},
                amount=amount,
                counterpart=counterpart,
            ) as span:
                span_holder.append(span)
                if money_frozen and tool.money:
                    raise Refusal(
                        RefusalCode.FROZEN,
                        Reason(
                            "Money tools are frozen this tick: instruction-shaped text was found in observed content."
                        ),
                    )
                self._handlers[tool.name](args, span)
        except Refusal as exc:
            return self._record(
                tool, span_holder, ActionStatus.REFUSED, args, f"{exc.code.value}: {exc.reason}"
            )
        except WalletDeclined as exc:
            return self._record(tool, span_holder, ActionStatus.DECLINED, args, exc.reason)
        except NotAlive as exc:
            return self._record(tool, span_holder, ActionStatus.REFUSED, args, str(exc))
        except Exception as exc:  # noqa: BLE001 - nothing raises past the tick; the journal has it
            return self._record(tool, span_holder, ActionStatus.FAILED, args, type(exc).__name__)
        span = span_holder[0]
        return ActionRecord(
            tool.name, span.status, span.id, str(args.reason), span.detail, span.amount
        )

    @staticmethod
    def _record(
        tool: MoonaTool,
        holder: list[JournalSpan],
        status: ActionStatus,
        args: ToolArgs,
        detail: str,
    ) -> ActionRecord:
        span = holder[0] if holder else None
        return ActionRecord(
            tool.name,
            status,
            span.id if span else None,
            str(args.reason),
            detail[:500],
            span.amount if span else None,
        )

    def _amount_and_counterpart(
        self, tool: MoonaTool, args: ToolArgs
    ) -> tuple[Money | None, str | None]:
        currency = self._wallet.currency
        if isinstance(args, SpendArgs):
            return money(args.amount, currency), args.merchant
        if isinstance(args, BidArgs):
            request = self._requests.get(args.request_id)
            return money(args.price, currency), request.client if request else None
        if isinstance(args, DeliverArgs | InvoiceArgs):
            job = self._job(args.request_id)
            if job is not None:
                return Money(fils=job.price, currency=job.currency), job.client
        return None, None

    # ----- handlers

    def _honest(self, text: str) -> None:
        pattern = self._cfg.rules.dishonest_claim(text)
        if pattern is not None:
            raise Refusal(
                RefusalCode.ACTIVITY_NOT_ALLOWED,
                Reason("Outbound text claims to be human; hard rule 2 refuses it."),
            )

    def _handle_bid(self, args: BidArgs, span: JournalSpan) -> None:
        self._honest(args.message)
        price = money(args.price, self._wallet.currency)
        request = self._requests.get(args.request_id)
        if request is None:
            raise Refusal(RefusalCode.BAD_ARGS, Reason("No open request carries that id."))
        if self._job(args.request_id) is not None:
            raise Refusal(
                RefusalCode.BAD_ARGS, Reason("One bid per request; this one is already bid.")
            )
        text = self._guard.safe(f"{args.message.strip()}\n\n{self._cfg.rules.disclosure}")
        with unit_of_work(self._engine) as session:
            session.add(
                JobRow(
                    id=self._idgen.new(),
                    life_id=self._life.id,
                    request_id=args.request_id,
                    client=request.client[:200],
                    title=request.title[:200],
                    price=price.fils,
                    currency=price.currency,
                    status="bid",
                )
            )
        try:
            receipt = self._market.bid(span.call, args.request_id, price, text)
        except Exception:
            self._update_job(
                args.request_id, closed_at=self._clock.now(), closed_reason="the bid failed to send"
            )
            raise
        span.set_amount(price, request.client)
        span.set_output(
            {"accepted": receipt.accepted, "note": receipt.note, "dry_run": receipt.dry_run}
        )
        if receipt.dry_run:
            span.set_status(ActionStatus.DRY_RUN)
            span.set_detail("dry run: the bid never left")
            return
        if receipt.accepted:
            self._update_job(args.request_id, status="accepted")
            span.set_detail(f"accepted at {as_text(price)}")
        else:
            self._update_job(
                args.request_id,
                closed_at=self._clock.now(),
                closed_reason=(receipt.note or "rejected")[:200],
            )
            span.set_detail(f"rejected: {receipt.note or 'no reason given'}")

    def _handle_deliver(self, args: DeliverArgs, span: JournalSpan) -> None:
        self._honest(args.content)
        job = self._job(args.request_id)
        if job is None or job.status != "accepted":
            raise Refusal(RefusalCode.BAD_ARGS, Reason("Deliver only a job the client accepted."))
        receipt = self._market.deliver(span.call, args.request_id, self._guard.safe(args.content))
        span.set_output(
            {"accepted": receipt.accepted, "note": receipt.note, "dry_run": receipt.dry_run}
        )
        if receipt.dry_run:
            span.set_status(ActionStatus.DRY_RUN)
            span.set_detail("dry run: nothing was delivered")
            return
        if receipt.accepted:
            self._update_job(args.request_id, status="delivered")
            span.set_detail("delivered")
        else:
            span.set_detail(f"not accepted: {receipt.note or 'no reason given'}")

    def _handle_invoice(self, args: InvoiceArgs, span: JournalSpan) -> None:
        job = self._job(args.request_id)
        if job is None or job.status != "delivered":
            raise Refusal(RefusalCode.BAD_ARGS, Reason("Invoice only a job that was delivered."))
        price = Money(fils=job.price, currency=job.currency)
        memo = self._guard.safe(f"{self._cfg.name}: job {job.request_id}, {job.title}")
        request = self._payments.request(
            span.call, payer=job.client, amount=price, memo=memo, job_ref=job.request_id
        )
        span.set_amount(price, job.client)
        span.set_output({"ref": request.ref, "dry_run": request.dry_run})
        if request.dry_run:
            span.set_status(ActionStatus.DRY_RUN)
            span.set_detail("dry run: no invoice left")
            return
        self._update_job(args.request_id, status="invoiced", payment_ref=request.ref)
        span.set_detail(f"invoiced {as_text(price)} ({request.ref})")

    def _handle_spend(self, args: SpendArgs, span: JournalSpan) -> None:
        amount = money(args.amount, self._wallet.currency)
        blocked = self._cfg.rules.blocked_merchant(args.merchant)
        if blocked is not None:
            raise Refusal(
                RefusalCode.ACTIVITY_NOT_ALLOWED,
                Reason(
                    "The merchant matches a blocked category (gambling, adult or tobacco); hard rule 1 refuses it."
                ),
            )
        balance_before = self._wallet.balance()
        decision = self._wallet.authorize(span.call, amount, args.merchant)
        span.set_output({"approved": decision.approved, "decline_reason": decision.decline_reason})
        if not decision.approved:
            raise WalletDeclined(decision.decline_reason or "declined", decision.balance)
        if span.call.dry_run:
            span.set_status(ActionStatus.DRY_RUN)
            span.set_detail("dry run: nothing was spent")
            return
        assert decision.authorization is not None
        entry = self._wallet.debit(decision.authorization, audit_id=span.id, purpose=args.purpose)
        notify = amount > fraction_of(balance_before, self._cfg.limits.notify_spend_fraction)
        detail = f"spent {as_text(amount)} at {args.merchant}: {args.purpose}"
        if notify:
            detail += " (large purchase: owner notified)"
            span.set_status(ActionStatus.NOTIFIED)
        span.set_detail(detail)
        span.set_output({"approved": True, "entry": entry.id, "notify": notify})

    def _handle_note(self, args: NoteArgs, span: JournalSpan) -> None:
        with unit_of_work(self._engine) as session:
            session.add(
                NoteRow(
                    id=self._idgen.new(),
                    life_id=self._life.id,
                    at=self._clock.now(),
                    tick=self._life.row().ticks,
                    text=str(self._guard.safe(args.text)),
                    audit_id=span.id,
                )
            )
        span.set_detail("noted")

    def _handle_rest(self, args: RestArgs, span: JournalSpan) -> None:
        hours = min(args.hours, self._cfg.limits.max_rest_hours)
        until = self._clock.now() + timedelta(hours=hours)
        self._life.rest_until(until)
        span.set_output({"hours": hours, "until": until.isoformat()})
        span.set_detail(f"resting {hours}h")

    def _handle_report(self, args: ReportArgs, span: JournalSpan) -> None:
        span.set_output({"text": args.text})
        span.set_detail(str(self._guard.safe(args.text)))

    # ----- death

    def _starved_now(self, tick: int, last_words: str | None) -> bool:
        if not self._life.is_alive():
            return True
        if self._wallet.balance() <= self._cfg.death_floor:
            self._die(
                DeathCause.STARVED,
                tick=tick,
                last_words=last_words,
                shortfall=None,
                detail="starved: the wallet reached the floor",
            )
            return True
        return False

    def _die(
        self,
        cause: DeathCause,
        *,
        tick: int,
        last_words: str | None,
        shortfall: Money | None,
        detail: str,
        actor: Actor = Actor.SYSTEM,
        reason: Reason | None = None,
    ) -> None:
        words = str(self._guard.redact(last_words)[0])[:2000] if last_words else None
        self._life.die(cause, last_words=words, shortfall=shortfall)
        self._journal.append(
            action="death",
            status=ActionStatus.EXECUTED,
            actor=actor,
            reason=reason or Reason(f"{self._cfg.name} died: {cause.value}."),
            tick=tick,
            input_obj={"cause": cause.value, "shortfall": shortfall.fils if shortfall else 0},
            amount=shortfall if shortfall is not None and shortfall.fils > 0 else None,
            detail=detail,
        )

    # ----- jobs and notes

    def _job(self, request_id: str) -> JobRow | None:
        with reading(self._engine) as session:
            return session.scalar(
                select(JobRow).where(
                    JobRow.life_id == self._life.id, JobRow.request_id == request_id
                )
            )

    def _open_jobs(self) -> list[JobRow]:
        with reading(self._engine) as session:
            rows = session.scalars(
                select(JobRow)
                .where(
                    JobRow.life_id == self._life.id,
                    JobRow.closed_at.is_(None),
                    JobRow.status != "paid",
                )
                .order_by(JobRow.created_at)
            ).all()
            return list(rows)

    def _update_job(self, request_id: str, **fields: Any) -> None:
        with unit_of_work(self._engine) as session:
            job = session.scalar(
                select(JobRow).where(
                    JobRow.life_id == self._life.id, JobRow.request_id == request_id
                )
            )
            if job is None:
                raise Refusal(RefusalCode.BAD_ARGS, Reason("No such job."))
            for name, value in fields.items():
                setattr(job, name, value)

    def _mark_paid(self, request_id: str) -> None:
        with unit_of_work(self._engine) as session:
            job = session.scalar(
                select(JobRow).where(
                    JobRow.life_id == self._life.id, JobRow.request_id == request_id
                )
            )
            if job is not None and job.status != "paid":
                job.status = "paid"

    def _notes(self) -> list[NoteRow]:
        with reading(self._engine) as session:
            rows = session.scalars(
                select(NoteRow).where(NoteRow.life_id == self._life.id).order_by(NoteRow.id)
            ).all()
            return list(rows)

    # ----- results

    def _result(
        self,
        tick: int,
        kind: TickKind,
        balance_before: Money,
        upkeep: Money | None,
        income: Money,
        *,
        cost: Money | None = None,
        actions: list[ActionRecord] | None = None,
        text: str | None = None,
        found: int = 0,
    ) -> TickResult:
        row = self._life.row()
        return TickResult(
            tick=tick,
            kind=kind,
            balance_before=balance_before,
            balance_after=self._wallet.balance(),
            upkeep=upkeep,
            income=income,
            model_cost=cost,
            actions=tuple(actions or ()),
            found=found,
            text=text,
            died=DeathCause(row.cause) if row.cause else None,
        )
