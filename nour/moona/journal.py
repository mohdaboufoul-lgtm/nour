"""His audit log (docs/MOONA.md §7; SPEC §12 "every action … is appended to the audit log with
input and output hashes", §16 "audit log covers 100% of actions"; DESIGN §3.11 ``AuditLog.span``
in miniature).

Two rows per action: ``opened`` is committed *before* the side effect and mints the ``PortCall``
every port method must be given, ``closed`` is written when the body returns or raises (the
status then comes from the exception: a refusal, a wallet decline, a freeze, a failure). Rows are
append-only and hash-chained: ``entry_hash = chain_hash(prev_hash, canonical_json(row))``, and
:meth:`Journal.verify_chain` recomputes the whole chain. One-row events (upkeep, a settlement,
death, an owner report) go through :meth:`Journal.append` with their own invocation id.

Text he wrote himself (``reason``) passes ``LeakGuard.safe`` (a registered value is refused);
text that may derive from observed content (``detail``, ``counterpart``) passes
``LeakGuard.redact`` (a registered value becomes ``[label …last4]``); objects are hashed through
``LeakGuard.safe_mapping``, never stored (SPEC §2: only references and hashes), so callers hand
in observed text redacted.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from nour.core.clock import Clock, IdGenerator
from nour.core.errors import FrozenError, NourError, Refusal
from nour.core.hashing import GENESIS_HASH, canonical_json, chain_hash, content_hash
from nour.core.leakguard import LeakGuard
from nour.core.ports import PortCall
from nour.core.types import ActionStatus, Actor, Desk, Hash, Money, Reason, Ulid
from nour.moona.life import NotAlive
from nour.moona.store import JournalRow, reading, unit_of_work
from nour.moona.wallet import WalletDeclined

_HASHED_FIELDS: tuple[str, ...] = (
    "id",
    "life_id",
    "at",
    "tick",
    "actor",
    "action",
    "phase",
    "status",
    "invocation_id",
    "amount",
    "currency",
    "counterpart",
    "reason",
    "detail",
    "input_hash",
    "output_hash",
    "config_hash",
    "dry_run",
)


def _entry_bytes(row: JournalRow) -> bytes:
    return canonical_json({name: getattr(row, name) for name in _HASHED_FIELDS})


def status_for(exc: BaseException) -> ActionStatus:
    """The ``closed`` status an exception in a span body means (DESIGN §3.11)."""
    if isinstance(exc, Refusal | NotAlive):
        return ActionStatus.REFUSED
    if isinstance(exc, WalletDeclined):
        return ActionStatus.DECLINED
    if isinstance(exc, FrozenError):
        return ActionStatus.FROZEN
    return ActionStatus.FAILED


class JournalSpan:
    """The open action: its id is the audit id on every port call it makes."""

    def __init__(self, journal: Journal, invocation_id: Ulid, call: PortCall) -> None:
        self._journal = journal
        self.id = invocation_id
        self.call = call
        self.status: ActionStatus = ActionStatus.EXECUTED
        self.output: Mapping[str, Any] = {}
        self.detail: str | None = None
        self.amount: Money | None = None
        self.counterpart: str | None = None

    def set_status(self, status: ActionStatus) -> None:
        self.status = ActionStatus(status)

    def set_output(self, output: Mapping[str, Any]) -> None:
        self.output = dict(output)

    def set_detail(self, detail: str | None) -> None:
        self.detail = detail

    def set_amount(self, amount: Money | None, counterpart: str | None = None) -> None:
        self.amount = amount
        if counterpart is not None:
            self.counterpart = counterpart


class Journal:
    """§12 §16: the append-only, hash-chained record of everything that happens to him."""

    def __init__(
        self,
        engine: Engine,
        life_id: Ulid,
        clock: Clock,
        idgen: IdGenerator,
        guard: LeakGuard,
        *,
        config_hash: Hash,
        dry_run: bool,
    ) -> None:
        self._engine = engine
        self._life_id = life_id
        self._clock = clock
        self._idgen = idgen
        self._guard = guard
        self._config_hash = str(config_hash)
        self._dry_run = bool(dry_run)

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    # ----- writing

    def _write(
        self,
        *,
        invocation_id: Ulid,
        phase: str,
        tick: int,
        actor: Actor,
        action: str,
        status: ActionStatus,
        reason: Reason,
        input_obj: Mapping[str, Any],
        output_obj: Mapping[str, Any] | None,
        amount: Money | None,
        counterpart: str | None,
        detail: str | None,
    ) -> Ulid:
        reason_text = str(self._guard.safe(str(Reason(reason))))
        detail_text = str(self._guard.redact(detail)[0]) if detail is not None else None
        counterpart_text = (
            str(self._guard.redact(counterpart)[0])[:200] if counterpart is not None else None
        )
        input_hash = content_hash(self._guard.safe_mapping(dict(input_obj)))
        output_hash = (
            content_hash(self._guard.safe_mapping(dict(output_obj)))
            if output_obj is not None
            else None
        )
        row_id = self._idgen.new()
        with unit_of_work(self._engine) as session:
            prev = session.scalar(
                select(JournalRow.entry_hash).order_by(JournalRow.seq.desc()).limit(1)
            )
            row = JournalRow(
                id=row_id,
                life_id=self._life_id,
                at=self._clock.now(),
                tick=tick,
                actor=Actor(actor).value,
                action=action,
                phase=phase,
                status=ActionStatus(status).value,
                invocation_id=invocation_id,
                amount=amount.fils if amount is not None else None,
                currency=amount.currency if amount is not None else None,
                counterpart=counterpart_text,
                reason=reason_text,
                detail=detail_text,
                input_hash=str(input_hash),
                output_hash=str(output_hash) if output_hash is not None else None,
                config_hash=self._config_hash,
                dry_run=self._dry_run,
                prev_hash=prev if prev is not None else GENESIS_HASH,
                entry_hash="",
            )
            row.entry_hash = chain_hash(row.prev_hash, _entry_bytes(row))
            session.add(row)
        return row_id

    def append(
        self,
        *,
        action: str,
        status: ActionStatus,
        reason: Reason,
        tick: int,
        input_obj: Mapping[str, Any] | None = None,
        output_obj: Mapping[str, Any] | None = None,
        actor: Actor = Actor.SUBAGENT,
        amount: Money | None = None,
        counterpart: str | None = None,
        detail: str | None = None,
    ) -> Ulid:
        """One ``closed`` row with its own invocation id (events that have no body)."""
        invocation_id = self._idgen.new()
        return self._write(
            invocation_id=invocation_id,
            phase="closed",
            tick=tick,
            actor=actor,
            action=action,
            status=status,
            reason=reason,
            input_obj=input_obj or {},
            output_obj=output_obj,
            amount=amount,
            counterpart=counterpart,
            detail=detail,
        )

    @contextmanager
    def span(
        self,
        *,
        action: str,
        reason: Reason,
        tick: int,
        input_obj: Mapping[str, Any],
        actor: Actor = Actor.SUBAGENT,
        amount: Money | None = None,
        counterpart: str | None = None,
    ) -> Iterator[JournalSpan]:
        """``opened`` row committed first (minting the ``PortCall``), the body, then exactly one
        ``closed`` row with the body's status or the exception's; the exception is re-raised."""
        invocation_id = self._idgen.new()
        self._write(
            invocation_id=invocation_id,
            phase="opened",
            tick=tick,
            actor=actor,
            action=action,
            status=ActionStatus.OPENED,
            reason=reason,
            input_obj=input_obj,
            output_obj=None,
            amount=amount,
            counterpart=counterpart,
            detail=None,
        )
        call = PortCall(
            audit_id=invocation_id, desk=Desk.OPERATOR, coat_id=None, dry_run=self._dry_run
        )
        span = JournalSpan(self, invocation_id, call)
        span.amount = amount
        span.counterpart = counterpart
        try:
            yield span
        except BaseException as exc:
            closing_status = status_for(exc)
            why = str(exc) if isinstance(exc, NourError) else type(exc).__name__
            self._write(
                invocation_id=invocation_id,
                phase="closed",
                tick=tick,
                actor=actor,
                action=action,
                status=closing_status,
                reason=reason,
                input_obj=input_obj,
                output_obj={"error": why},
                amount=span.amount,
                counterpart=span.counterpart,
                detail=why[:500],
            )
            raise
        self._write(
            invocation_id=invocation_id,
            phase="closed",
            tick=tick,
            actor=actor,
            action=action,
            status=span.status,
            reason=reason,
            input_obj=input_obj,
            output_obj=span.output,
            amount=span.amount,
            counterpart=span.counterpart,
            detail=span.detail,
        )

    # ----- reading

    def rows(
        self,
        *,
        action: str | None = None,
        status: ActionStatus | None = None,
        phase: str | None = None,
        tick: int | None = None,
        actor: Actor | None = None,
    ) -> list[JournalRow]:
        stmt = select(JournalRow).where(JournalRow.life_id == self._life_id)
        if action is not None:
            stmt = stmt.where(JournalRow.action == action)
        if status is not None:
            stmt = stmt.where(JournalRow.status == ActionStatus(status).value)
        if phase is not None:
            stmt = stmt.where(JournalRow.phase == phase)
        if tick is not None:
            stmt = stmt.where(JournalRow.tick == tick)
        if actor is not None:
            stmt = stmt.where(JournalRow.actor == Actor(actor).value)
        with reading(self._engine) as session:
            rows = list(session.scalars(stmt.order_by(JournalRow.seq)).all())
            session.expunge_all()
            return rows

    def head(self) -> str:
        with reading(self._engine) as session:
            last = session.scalar(
                select(JournalRow.entry_hash).order_by(JournalRow.seq.desc()).limit(1)
            )
            return last if last is not None else GENESIS_HASH

    def verify_chain(self) -> bool:
        """Recompute every ``entry_hash`` from the genesis constant; ``False`` on any break."""
        with reading(self._engine) as session:
            rows = session.scalars(select(JournalRow).order_by(JournalRow.seq)).all()
            prev = GENESIS_HASH
            for row in rows:
                if row.prev_hash != prev:
                    return False
                if chain_hash(prev, _entry_bytes(row)) != row.entry_hash:
                    return False
                prev = row.entry_hash
            return True

    def unclosed(self) -> list[JournalRow]:
        """``opened`` rows with no ``closed`` row: must be empty after every tick (SPEC §16)."""
        rows = self.rows()
        closed = {row.invocation_id for row in rows if row.phase == "closed"}
        return [row for row in rows if row.phase == "opened" and row.invocation_id not in closed]
