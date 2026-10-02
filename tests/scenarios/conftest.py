"""Shared fixtures and helpers for the seven SPEC §18 weekly regression scenarios.

Sources: SPEC §13 (weekly behavioural regression on fixed scenarios), SPEC §18 (the scenario
list: each has an expected tier, an expected log entry and an expected owner message), DESIGN
§3.21 (the `Harness` API every scenario drives), DESIGN §7.2 (the expected triple per scenario)
and MODULES.md "acceptance" (the scenarios consume `Harness` only).

Collection guard. `nour.testing.harness` lands in wave 6. Every scenario module calls
`pytest.importorskip("nour.testing.harness")` at module level, which pytest turns into a clean
module-level skip. This conftest must NOT raise `Skipped` while it is being imported: when
`tests/scenarios` is the command-line argument (what `make scenarios` passes) pytest loads this
file as an *initial* conftest, and a `Skipped` raised there aborts the whole session with exit 1
(verified on pytest 9.1). The guard therefore lives in the `harness` fixture, and the wave 1-6
imports the helpers need are resolved lazily inside the helper bodies.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from sqlalchemy import func, select

from nour.core.clock import DUBAI
from nour.core.types import ActionStatus, ActionTier, CoatId, Desk, Ulid

if TYPE_CHECKING:
    # ruff sorts not-yet-built nour.* packages as third-party; the final order is kept instead.
    # isort: off
    from nour.core.contracts import ActionOutcome
    from nour.core.ports import ModelResponse, ModelToolCall, OutboundWhatsApp
    from nour.db.models import AuditEventRow, PendingOwnerMessageRow
    from nour.governance.owner_channel import OwnerMessageKind
    from nour.runtime.loop import StepResult
    from nour.testing.harness import Harness
    # isort: on

HARNESS_MODULE = "nour.testing.harness"
BUZZ = CoatId("buzz-avenue")
CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "arabic_commands.yaml"

# ASSUMPTION: DESIGN gives no owner-mailbox address for phase 0 (owner mailboxes arrive by OAuth
# in phase 2); the fake registers some address at build time. `owner_mailbox()` reads it from the
# fake when it is exposed and otherwise falls back to the address `Harness.build` already uses
# for the owner's second channel.
OWNER_MAILBOX_FALLBACK = "owner@example.com"


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Callable[..., Harness]]:
    """Factory fixture: `h = harness(policy=..., start=...)` runs `Harness.build(<dir>, **kw)`.

    One SQLite file per build (DESIGN §7: "SQLite file per test, fakes, FakeClock at Monday
    2026-10-05 07:00 Dubai"); every harness built through the factory is closed at teardown.
    Skips (instead of erroring) until wave 6 lands.
    """
    harness_mod = pytest.importorskip(HARNESS_MODULE)
    built: list[Harness] = []

    def build(**kwargs: Any) -> Harness:
        root = tmp_path / f"harness-{len(built)}"
        root.mkdir()
        h = harness_mod.Harness.build(root, **kwargs)
        built.append(h)
        return h

    yield build
    for h in reversed(built):
        h.close()


# --------------------------------------------------------------------------- #
# The §18 triple and the per-scenario integrity check
# --------------------------------------------------------------------------- #


def expect_triple(
    h: Harness,
    event_id: Ulid,
    *,
    tier: ActionTier | None,
    status: ActionStatus,
    owner_message_contains: str | None,
    rules_hit: Sequence[str] = (),
    tool: str | None = None,
    owner_message_kind: OwnerMessageKind | None = None,
) -> None:
    """SPEC §18: expected tier, expected log entry, expected owner message — via `Harness.expect`.

    `tool` disambiguates when one event produced several outcomes (DESIGN §3.21); `rules_hit`
    is the subset of `TierDecision.rules_hit` the scenario names (DESIGN §7.2).
    """
    h.expect(
        event_id,
        tier=tier,
        status=status,
        tool=tool,
        rules_hit=tuple(rules_hit),
        owner_message_contains=owner_message_contains,
        owner_message_kind=owner_message_kind,
    )


def assert_day_integrity(h: Harness, day: date | None = None) -> None:
    """DESIGN §7.2: "Each scenario also asserts `verify_chain()` and replays its own day"."""
    day = day or h.clock.today_dubai()
    assert h.reader.verify_chain(), "audit hash chain does not verify"
    report = h.replay(day)
    assert report.first_divergence is None, (
        f"replay of {day} diverged at audit row {report.first_divergence}: "
        f"{report.original_tail_hash} != {report.replayed_tail_hash}"
    )


# --------------------------------------------------------------------------- #
# Clock helpers (Asia/Dubai is the system clock, SPEC §14)
# --------------------------------------------------------------------------- #


def dubai(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=DUBAI)


def local(when: datetime) -> datetime:
    # ASSUMPTION: every stored timestamp is UTC (DESIGN §5 conventions); SQLite hands
    # `DateTime(timezone=True)` columns back naive, so a naive value is read as UTC.
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(DUBAI)


def advance_to(h: Harness, when: datetime) -> list[StepResult]:
    """`Harness.advance` up to an absolute Dubai time (five-minute steps, timers and sweeps)."""
    delta = when - h.clock.now()
    assert delta > timedelta(0), f"{when} is not after the clock ({h.clock.now()})"
    return h.advance(delta)


# --------------------------------------------------------------------------- #
# Config and corpus
# --------------------------------------------------------------------------- #


def coat_mailbox(h: Harness, coat: CoatId = BUZZ) -> str:
    """The coat's own address (`coats/<slug>.yaml` identity.email), never a literal."""
    return h.cfg.coat(coat).identity.email


def owner_mailbox(h: Harness) -> str:
    boxes = getattr(h.fakes.owner_mail, "mailboxes", None)
    return str(boxes[0]) if boxes else OWNER_MAILBOX_FALLBACK


@lru_cache(maxsize=1)
def _corpus() -> dict[str, Any]:
    return yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))


def corpus_command(cmd_id: str) -> dict[str, Any]:
    """One entry of tests/fixtures/arabic_commands.yaml (realistic owner Arabic, see its README)."""
    for cmd in _corpus()["commands"]:
        if cmd["id"] == cmd_id:
            return cmd
    raise KeyError(cmd_id)


# --------------------------------------------------------------------------- #
# Scripting the model (DESIGN §3.9: ScriptedModel FIFO, then the policy)
# --------------------------------------------------------------------------- #


def scripted_response(*calls: ModelToolCall, text: str | None = None) -> ModelResponse:
    from nour.core.ports import ModelResponse, ModelUsage

    return ModelResponse(
        text=text,
        tool_calls=list(calls),
        vendor="fake-a",
        model="scripted-scenario",
        usage=ModelUsage(input_tokens=0, output_tokens=0, cost_fils=0),
    )


def enqueue_model(h: Harness, *calls: ModelToolCall, text: str | None = None) -> None:
    """Pin the PRIMARY model's next answer so the assertion is about the gate, not a heuristic."""
    from nour.core.ports import ModelRole

    h.fakes.models[ModelRole.PRIMARY].enqueue(scripted_response(*calls, text=text))


def call(name: str, *, reason: str, **args: Any) -> ModelToolCall:
    """`nour.fakes.policies.tool_call` — the §18 output contract (`reason` mandatory)."""
    from nour.fakes.policies import tool_call

    return tool_call(name, reason=reason, **args)


def spend_call(
    amount: Decimal | int,
    counterpart: str,
    *,
    reason: str,
    coat: CoatId = BUZZ,
    category: str | None = None,
) -> ModelToolCall:
    """A `ledger.spend` proposal at `amount` AED.

    ASSUMPTION: the spend handler takes its coat, counterpart and amount from the contract keys
    `coat` / `counterpart` / `amount_aed` (DESIGN §3.4 ModelToolCall) and the always-K kinds
    (customer_refund …) are selected with a `category` argument, since §7.2 says "ledger.spend
    50 with category customer_refund".
    """
    args: dict[str, Any] = {"coat": str(coat), "counterpart": counterpart, "amount_aed": amount}
    if category is not None:
        args["category"] = category
    return call("ledger.spend", reason=reason, **args)


# --------------------------------------------------------------------------- #
# Audit rows and outcomes
# --------------------------------------------------------------------------- #


def audit_rows_for(
    h: Harness, event_id: Ulid | None = None, *, tool: str | None = None, phase: str | None = None
) -> list[AuditEventRow]:
    rows = list(h.audit_rows())
    if event_id is not None:
        rows = [r for r in rows if r.event_id == event_id]
    if tool is not None:
        rows = [r for r in rows if r.action == tool]
    if phase is not None:
        rows = [r for r in rows if r.phase == phase]
    return sorted(rows, key=lambda r: r.seq)


def closed_rows(h: Harness, event_id: Ulid | None = None, *, tool: str | None = None) -> list[Any]:
    return audit_rows_for(h, event_id, tool=tool, phase="closed")


def closed_row(h: Harness, event_id: Ulid, tool: str) -> AuditEventRow:
    rows = closed_rows(h, event_id, tool=tool)
    assert len(rows) == 1, f"expected one closed {tool} row for {event_id}, got {len(rows)}"
    return rows[0]


def outcomes_for(results: Iterable[StepResult], event_id: Ulid) -> list[ActionOutcome]:
    return [o for r in results if r.event_id == event_id for o in r.outcomes]


def outcome_for(
    h: Harness, results: Iterable[StepResult], event_id: Ulid, tool: str
) -> ActionOutcome:
    """The `ActionOutcome` of `tool` for `event_id` (carries the TierDecision: rules_hit,
    refusal code, deferred_until — none of which are audit columns)."""
    outcomes = outcomes_for(results, event_id)
    if len(outcomes) == 1:
        return outcomes[0]
    rows = audit_rows_for(h, event_id, tool=tool)
    # ASSUMPTION: ActionOutcome.audit_id is the id of the span's `opened` row or its
    # invocation_id (DESIGN §3.11: the span mints the PortCall from the opened row).
    ids = {r.id for r in rows} | {r.invocation_id for r in rows}
    matches = [o for o in outcomes if o.audit_id in ids]
    assert len(matches) == 1, f"could not match an outcome for {tool} on {event_id}: {ids}"
    return matches[0]


# --------------------------------------------------------------------------- #
# Owner messages and outbound fakes
# --------------------------------------------------------------------------- #


def owner_sends(h: Harness) -> list[OutboundWhatsApp]:
    """WhatsApp messages the fakes delivered to the owner's number."""
    return [m for m in h.fakes.whatsapp.sent if m.to == h.OWNER_NUMBER]


def message_ids(h: Harness, kind: OwnerMessageKind | None = None) -> set[Ulid]:
    return {m.id for m in h.owner_messages(kind)}


def parked_messages(h: Harness) -> list[PendingOwnerMessageRow]:
    """pending_owner_message rows waiting for the end of quiet hours (DESIGN §2.6)."""
    return [m for m in h.owner_messages() if m.parked_until is not None and m.sent_at is None]


def mentions_any(text: str | None, needles: Iterable[str]) -> bool:
    lowered = (text or "").lower()
    return any(needle.lower() in lowered for needle in needles)


def vault_calls(h: Harness) -> list[Any]:
    """CallLog entries that would mean the vault was reached: anything named after it, or a
    recipient-bound link minted on object storage (DESIGN §3.4 ObjectStoragePort.signed_link)."""
    return [
        c
        for c in h.call_log.calls
        if "vault" in c.port.lower()
        or "vault" in c.method.lower()
        or (c.port == "objects" and c.method == "signed_link")
    ]


# --------------------------------------------------------------------------- #
# Direct table reads the Harness does not wrap
# --------------------------------------------------------------------------- #


def found_instructions(h: Harness, event_id: Ulid) -> list[dict[str, Any]]:
    """found_instruction rows for an event (SHARED scope: readable through any desk session)."""
    from nour.db.models import FoundInstructionRow

    with h.desks[Desk.ASSISTANT].sf.session() as session:
        rows = session.scalars(
            select(FoundInstructionRow).where(FoundInstructionRow.event_id == event_id)
        ).all()
        return [
            {
                "quote": r.quote,
                "location": r.location,
                "mentions_money": r.mentions_money,
                "pattern": r.pattern,
            }
            for r in rows
        ]


def passphrase_attempt_outcomes(h: Harness) -> list[str]:
    """Outcomes of every passphrase_attempt row, oldest first (no body is ever stored there)."""
    from nour.db.models import PassphraseAttemptRow

    with h.desks[Desk.ASSISTANT].sf.session() as session:
        rows = session.scalars(select(PassphraseAttemptRow).order_by(PassphraseAttemptRow.at)).all()
        return [str(r.outcome) for r in rows]


def release_count(h: Harness, call_id: Ulid) -> int:
    """How many `release` rows (DESIGN §5.2) were minted for a proposal id."""
    from nour.db.models import ReleaseRow

    with h.desks[Desk.ASSISTANT].sf.session() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(ReleaseRow).where(ReleaseRow.call_id == call_id)
            )
            or 0
        )


# --------------------------------------------------------------------------- #
# Seeding records the scenarios need to exist beforehand
# --------------------------------------------------------------------------- #


def seed_operator_contact(h: Harness, *, name: str, address: str, coat: CoatId = BUZZ) -> Ulid:
    """Make `name` a known counterpart of the Operator desk so TierResolver rule 5
    (new_counterpart → K) does not fire on in-band spends.

    ASSUMPTION: the Harness has no seeding helper, so the row is written through
    `CrmStore.upsert_contact` on the Operator desk's own session factory (the wall still holds);
    the `audit_id` it requires is a fresh ULID — scenario tests do not run the coverage proof.
    """
    from nour.records.crm import ContactIn, CrmStore

    crm = CrmStore(h.desks[Desk.OPERATOR].sf, h.clock, h.idgen)
    contact = ContactIn(
        name=h.guard.safe(name),
        org=h.guard.safe(name),
        role="supplier",
        channels={"email": address},
        language="en",
        register="formal",
        consent_status="business",
        source="scenario_seed",
    )
    return crm.upsert_contact(coat, contact, h.idgen.new()).id


def beneficiary_snapshot(h: Harness, name: str, coat: CoatId = BUZZ) -> dict[str, Any]:
    """The columns of one beneficiary row that a registry change would touch (DESIGN §5.1)."""
    from nour.db.models import BeneficiaryRow

    with h.desks[Desk.ASSISTANT].sf.session() as session:
        row = session.scalars(
            select(BeneficiaryRow).where(
                BeneficiaryRow.coat_id == coat, BeneficiaryRow.name == name
            )
        ).one()
        return {
            "id": row.id,
            "bank_details_ref": row.bank_details_ref,
            "bank_last4": row.bank_last4,
            "bank_fp": row.bank_fp,
            "verified_at": row.verified_at,
            "verified_by": row.verified_by,
            "verification_method": row.verification_method,
            "change_history": row.change_history,
            "status": row.status,
            "updated_at": row.updated_at,
        }


def seed_beneficiary(h: Harness, *, name: str, iban: str, coat: CoatId = BUZZ) -> dict[str, Any]:
    """A verified supplier in the coat's beneficiary registry; returns its snapshot.

    ASSUMPTION: phase 0 has no API that creates a beneficiary (registry writes take a
    `ReleasedAction` and are phase 2), so the scenario inserts the row directly through the
    Assistant desk's session factory with the §5.1 columns: the IBAN encrypted by `FieldCipher`
    (AAD = table.column.id), `bank_fp` as the keyed hash every Tier 2 value uses, and the value
    registered in the LeakGuard so `assert_no_leaks()` also covers this supplier's IBAN.
    """
    # isort: off
    from nour.core.hashing import keyed_hash
    from nour.db.models import BeneficiaryRow
    from nour.vault.crypto import FieldCipher
    # isort: on

    cipher = FieldCipher(h.fakes.secrets)
    row_id = h.idgen.new()
    slug = name.lower().replace(" ", "-")
    leak_key = h.fakes.secrets.get(h.settings.leakguard_key_name)
    row = BeneficiaryRow(
        id=row_id,
        coat_id=coat,
        name=name,
        bank_details_ref=f"vault://{coat}/beneficiaries/{slug}#iban",
        bank_details_ct=cipher.encrypt(iban, f"beneficiary.bank_details_ct.{row_id}".encode()),
        bank_last4=iban[-4:],
        bank_fp=keyed_hash(leak_key, iban),
        verified_at=h.clock.now() - timedelta(days=90),
        verified_by="owner",
        verification_method="callback",
        change_history=[],
        status="verified",
    )
    with h.desks[Desk.ASSISTANT].sf.write() as session:
        session.add(row)
        session.commit()
    h.guard.register_plaintext_once(iban, f"beneficiary:{coat}/{slug}")
    return beneficiary_snapshot(h, name, coat)
