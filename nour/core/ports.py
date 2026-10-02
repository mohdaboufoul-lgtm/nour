"""Ports, DTOs, ``PortCall``, ``CallLog`` and ``PortSet`` (DESIGN §3.4 ``nour/core/ports.py``;
SPEC §4 §9 §10 §16).

Every external system is a ``typing.Protocol`` here with one in-memory fake in ``nour/fakes/``
(DESIGN §6). Every side-effecting method takes a :class:`PortCall` first (SPEC §12: every action
is logged; DESIGN §4h: "an unlogged side effect is a type error"), so ``CallLog.without_audit()
== []`` is a type-backed assertion and dry run (SPEC §8) is a port-level flag rather than a
prompt instruction. Read-only methods (``pull_inbound``, ``month_total``, ``search`` ...) take
no call.

Every DTO is a frozen pydantic model; text a port *emits on Nour's behalf* (``OutboundWhatsApp.text``,
``DraftEmail.subject/body``, ``ModelRequest.system``, ``ModelMessage.content``,
``IndexableText.text``, second-channel summaries and alerts) is typed ``SafeStr`` and so can only
come out of ``LeakGuard`` (SPEC §2 §11). Text that *arrives* from the world (``InboundWhatsApp.text``,
``InboundEmail.body_text`` ...) is plain ``str``: it is data, never authority (SPEC §2), and is
redacted before any model sees it. Every timestamp field is ``AwareDatetime``: naive datetimes
are refused at the boundary because every clock in Nour is tz-aware UTC (DESIGN §5).

The protocols are ``@runtime_checkable`` so ``tests/unit/test_fakes.py`` can assert each fake
satisfies its port with ``isinstance``.

``PortSet.for_desk(token)`` is how the Operator process is built without the owner mailbox, the
private model, the vault key or any secret outside ``operator/`` (SPEC §5 one-way gate; DESIGN
§4a): the narrowing is a copy, keyed on the token's desk, and ``SecretsPort.scoped`` can only
narrow.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, model_validator

from nour.core.clock import Clock, process_clock
from nour.core.errors import Tier2LeakError
from nour.core.hashing import content_hash
from nour.core.tier2 import RenderWitness, SecretRef
from nour.core.tokens import DeskToken, require_minted
from nour.core.types import BudgetHolder, CoatId, DataTier, Desk, Hash, Money, SafeStr, Ulid

# --------------------------------------------------------------------------- the audit capability


class PortCall(BaseModel, frozen=True):
    """§12 §16: the audit id as a capability at the port boundary. Minted by AuditLog.span (opened row) and
    carried in ExecContext; a port call without one is a type error. dry_run is §8 dry-run mode."""

    audit_id: Ulid
    desk: Desk
    coat_id: CoatId | None
    dry_run: bool = False


class RecordedCall(BaseModel, frozen=True):
    """One side-effecting call as the :class:`CallLog` saw it (args hashed, never stored)."""

    port: str
    method: str
    audit_id: Ulid | None
    args_hash: Hash
    at: AwareDatetime
    dry_run: bool


def _args_hash(args: Mapping[str, Any]) -> Hash:
    """``content_hash`` over the call arguments; objects ``canonical_json`` cannot represent are
    replaced by their type name, a ``Tier2Value`` anywhere raises (it must never be hashed in the
    clear)."""
    try:
        return content_hash(args)
    except Tier2LeakError:
        raise
    except TypeError:
        return content_hash(_hashable(args))


def _hashable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _hashable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_hashable(item) for item in value]
    try:
        content_hash(value)
    except Tier2LeakError:
        raise
    except TypeError:
        return {"__type__": type(value).__qualname__}
    return value


class CallLog:
    """Shared by every fake (and by real adapters in staging): one list of every side-effecting call.

    Timestamps come from the injected ``Clock`` (SPEC §12; DESIGN §7.4). DESIGN §3.4 gives the
    class no constructor arguments, so ``CallLog()`` works and reads the process clock
    (``set_process_clock`` points it at the test ``FakeClock``); the harness and
    ``default_fakes`` pass their clock explicitly, which wins.
    """

    calls: list[RecordedCall]

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock
        self.calls = []

    @property
    def clock(self) -> Clock:
        return self._clock if self._clock is not None else process_clock()

    def record(self, port: str, method: str, call: PortCall | None, **args: Any) -> None:
        """Append one entry. ``call=None`` records an *unaudited* side effect: allowed so the
        coverage report can name it, never silently dropped."""
        if call is not None and not isinstance(call, PortCall):
            raise TypeError("CallLog.record takes a PortCall (or None for an unaudited call)")
        self.calls.append(
            RecordedCall(
                port=port,
                method=method,
                audit_id=call.audit_id if call is not None else None,
                args_hash=_args_hash(args),
                at=self.clock.now(),
                dry_run=call.dry_run if call is not None else False,
            )
        )

    def without_audit(self) -> list[RecordedCall]:
        """Every side effect that happened without an audit id (§16: must be empty)."""
        return [entry for entry in self.calls if entry.audit_id is None]

    def by_audit_id(self) -> dict[Ulid, list[RecordedCall]]:
        grouped: dict[Ulid, list[RecordedCall]] = {}
        for entry in self.calls:
            if entry.audit_id is not None:
                grouped.setdefault(entry.audit_id, []).append(entry)
        return grouped

    def __len__(self) -> int:
        return len(self.calls)


class SendReceipt(BaseModel, frozen=True):
    """What a send returns: the provider id, or why it was refused; ``dry_run`` when nothing left."""

    provider_msg_id: str | None
    accepted: bool
    error: str | None = None
    dry_run: bool = False


# --------------------------------------------------------------------------- WhatsApp (§9 owner thread + company lines)


class InboundWhatsApp(BaseModel, frozen=True):
    provider_msg_id: str
    line_id: str
    sender: str
    sender_display: str | None
    text: str | None
    audio_ref: str | None
    at: AwareDatetime
    signature_valid: bool


class OutboundWhatsApp(BaseModel, frozen=True):
    """A message Nour sends: text is ``SafeStr`` only; at least one of text/audio/template."""

    line_id: str
    to: str
    text: SafeStr | None = None
    audio_ref: str | None = None
    template: str | None = None

    @model_validator(mode="after")
    def _has_content(self) -> OutboundWhatsApp:
        if self.text is None and self.audio_ref is None and self.template is None:
            raise ValueError("an outbound WhatsApp message needs text, an audio_ref or a template")
        return self


@runtime_checkable
class WhatsAppPort(Protocol):
    def verify_signature(self, raw_body: bytes, header: str) -> bool: ...
    def parse_webhook(
        self, payload: dict[str, Any], signature_valid: bool
    ) -> list[InboundWhatsApp]: ...
    def pull_inbound(self) -> list[InboundWhatsApp]: ...  # polling adapters and fakes
    def send(self, call: PortCall, msg: OutboundWhatsApp) -> SendReceipt: ...
    def fetch_media(self, audio_ref: str) -> bytes: ...


# --------------------------------------------------------------------------- Mailboxes (§5 §9): two ports, two credential sets


class Attachment(BaseModel, frozen=True):
    ref: str
    name: str
    mime: str
    size: int
    text: str | None = None  # text extracted by the adapter (PDF/plain)


class InboundEmail(BaseModel, frozen=True):
    provider_msg_id: str
    mailbox: str
    sender: str
    to: tuple[str, ...]
    subject: str
    body_text: str
    attachments: tuple[Attachment, ...] = ()
    headers: Mapping[str, str] = {}
    at: AwareDatetime
    dkim_pass: bool


class DraftEmail(BaseModel, frozen=True):
    """A draft Nour writes: subject and body are ``SafeStr`` only."""

    mailbox: str
    to: tuple[str, ...]
    subject: SafeStr
    body: SafeStr
    in_reply_to: str | None = None
    attachment_refs: tuple[str, ...] = ()


@runtime_checkable
class MailboxPort(Protocol):
    kind: Literal["coat", "owner"]

    def pull_inbound(self, mailbox: str, since: AwareDatetime) -> list[InboundEmail]: ...
    def create_draft(self, call: PortCall, draft: DraftEmail) -> str: ...
    def send(self, call: PortCall, draft_id: str) -> SendReceipt: ...
    def scopes(self, mailbox: str) -> frozenset[str]: ...  # §9: must be ⊆ {"read","draft","send"}


@runtime_checkable
class CoatMailboxPort(MailboxPort, Protocol):
    """The company mailbox port: the only mailbox port the Operator process can construct."""

    kind: Literal["coat"]


@runtime_checkable
class OwnerMailboxPort(MailboxPort, Protocol):
    """Built only from ``assistant/`` credentials; ``PortSet.for_desk(OperatorToken)`` drops it."""

    kind: Literal["owner"]


# --------------------------------------------------------------------------- Phone body (§4 §13; phase 0 = interface + fake)


class PhoneNotification(BaseModel, frozen=True):
    id: str
    app: str
    title: str
    text: str
    at: AwareDatetime
    device_id: str


@runtime_checkable
class PhoneBodyPort(Protocol):
    def pull_notifications(self) -> list[PhoneNotification]: ...
    def is_online(self) -> bool: ...
    def wipe(self, call: PortCall) -> None: ...


# --------------------------------------------------------------------------- Card issuer (§10): the cap lives in the issuer


class CardAuthorization(BaseModel, frozen=True):
    """Minted only by a CardIssuerPort implementation; Ledger.record_spend requires one."""

    auth_ref: str
    card_ref: str
    holder: BudgetHolder
    amount: Money
    merchant: str
    at: AwareDatetime


class CardDecision(BaseModel, frozen=True):
    """The issuer's answer; ``approved`` and ``authorization`` agree (validated)."""

    approved: bool
    authorization: CardAuthorization | None
    decline_reason: str | None
    remaining: Money

    @model_validator(mode="after")
    def _consistent(self) -> CardDecision:
        if self.approved and self.authorization is None:
            raise ValueError("an approved CardDecision carries a CardAuthorization")
        if not self.approved and self.authorization is not None:
            raise ValueError("a declined CardDecision carries no CardAuthorization")
        return self


@runtime_checkable
class CardIssuerPort(Protocol):
    def issue(self, holder: BudgetHolder, monthly_cap: Money) -> str: ...  # card_ref
    def authorize(
        self, call: PortCall, card_ref: str, amount: Money, merchant: str
    ) -> CardDecision: ...
    def freeze(self, call: PortCall, card_ref: str) -> None: ...
    def unfreeze(self, call: PortCall, card_ref: str) -> None: ...
    def month_total(self, card_ref: str, month: date) -> Money: ...
    def is_frozen(self, card_ref: str) -> bool: ...


# --------------------------------------------------------------------------- Speech (§9)


class Transcript(BaseModel, frozen=True):
    text: str
    language: str
    confidence: float
    engine: str


@runtime_checkable
class SttPort(Protocol):
    engine: str

    def transcribe(
        self, audio: bytes, *, language: str, vocabulary: Sequence[str]
    ) -> Transcript: ...


@runtime_checkable
class TtsPort(Protocol):
    def synthesize(self, call: PortCall, text: SafeStr) -> bytes: ...


# --------------------------------------------------------------------------- Models (§4 §12 §14)


class ModelRole(StrEnum):
    """One port per role from ``config/models.yaml``; fallback and auditor are a different vendor."""

    PRIMARY = "primary"
    FALLBACK = "fallback"
    CRITIC = "critic"
    AUDITOR = "auditor"
    PRIVATE_TIER2 = "private_tier2"


class ToolSchema(BaseModel, frozen=True):
    name: str
    description: str
    parameters: dict[str, Any]


class ModelMessage(BaseModel, frozen=True):
    role: Literal["user", "assistant", "tool"]
    content: SafeStr
    tool_call_id: str | None = None


class ModelRequest(BaseModel, frozen=True):
    """Everything the model sees; ``system`` and every message are ``SafeStr`` (SPEC §2 §11)."""

    role: ModelRole
    desk: Desk
    system: SafeStr
    messages: list[ModelMessage]
    tools: list[ToolSchema]
    max_tokens: int = 4096
    metadata: dict[str, str] = {}


class ModelToolCall(BaseModel, frozen=True):
    """The model tool-call JSON contract (§18 output contract), fixed here for the prompt, parser, fakes and
    replayer: `arguments` MUST carry `reason` (one sentence) and MAY carry `coat`, `counterpart`, `amount_aed`,
    `tier`, `data_tier_touched` plus the tool's own args. `tier`/`data_tier_touched` are logged, never routed on."""

    id: str
    name: str
    arguments: dict[str, Any]


class ModelUsage(BaseModel, frozen=True):
    input_tokens: int
    output_tokens: int
    cost_fils: int


class ModelResponse(BaseModel, frozen=True):
    text: str | None
    tool_calls: list[ModelToolCall]
    vendor: str
    model: str
    usage: ModelUsage


@runtime_checkable
class ModelPort(Protocol):
    vendor: str
    model: str

    def complete(self, req: ModelRequest) -> ModelResponse: ...


class PrivateModelRequest(BaseModel, frozen=True):
    """§11 phase 3: refs in, never values; the adapter reveals them under the witness in region."""

    refs: tuple[SecretRef, ...]
    instruction: SafeStr
    max_tokens: int = 2048


class PrivateModelResult(BaseModel, frozen=True):
    summary: SafeStr
    output_fp: Hash


@runtime_checkable
class Tier2ModelPort(Protocol):
    def complete_private(
        self, req: PrivateModelRequest, witness: RenderWitness
    ) -> PrivateModelResult: ...


# --------------------------------------------------------------------------- Secrets (§13): a desk gets a port bound to its prefix


@runtime_checkable
class SecretsPort(Protocol):
    prefix: str

    def get(self, name: str) -> bytes: ...  # name must start with prefix → else ScopeViolation
    def scoped(
        self, prefix: str
    ) -> SecretsPort: ...  # narrows only (prefix must extend self.prefix)
    def revoke_all(self, call: PortCall, prefixes: Sequence[str]) -> list[str]: ...
    def rotate(self, call: PortCall, name: str) -> None: ...


# --------------------------------------------------------------------------- Object storage (§11)


@runtime_checkable
class ObjectStoragePort(Protocol):
    def put(self, call: PortCall, key: str, data: bytes, *, content_type: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, call: PortCall, key: str) -> None: ...
    def signed_link(
        self, call: PortCall, key: str, ttl: timedelta, recipient: str
    ) -> str: ...  # §11 recipient-bound


# --------------------------------------------------------------------------- Bank feed (§10, phase 2 body; shape now)


class BankLine(BaseModel, frozen=True):
    ref: str
    account_ref: str
    at: AwareDatetime
    amount: Money
    counterpart_last4: str
    memo: str


@runtime_checkable
class BankFeedPort(Protocol):
    def lines(self, coat_id: CoatId, since: AwareDatetime) -> list[BankLine]: ...
    def balance(self, coat_id: CoatId) -> Money: ...


# --------------------------------------------------------------------------- Second channel (§6 §12)


class SecondChannelMessage(BaseModel, frozen=True):
    sender: str
    text: str
    token: str | None
    at: AwareDatetime


@runtime_checkable
class SecondChannelPort(Protocol):
    def send_confirmation(
        self, call: PortCall, purpose: str, token: str, summary: SafeStr
    ) -> None: ...
    def pull_messages(
        self,
    ) -> list[SecondChannelMessage]: ...  # confirmations AND the second kill path
    def send_alert(self, call: PortCall, text: SafeStr) -> None: ...


# --------------------------------------------------------------------------- Vector index (§8)


class IndexableText(BaseModel, frozen=True):
    """Only Tier 0/1 text is ever indexed (SPEC §11: Tier 2 is indexed by metadata only)."""

    id: str
    namespace: str
    text: SafeStr
    tier: Literal[DataTier.T0, DataTier.T1]
    meta: dict[str, str]


class VectorHit(BaseModel, frozen=True):
    id: str
    score: float
    meta: dict[str, str]


@runtime_checkable
class VectorIndexPort(Protocol):
    def upsert(self, item: IndexableText) -> None: ...
    def search(self, namespace: str, query: str, k: int) -> list[VectorHit]: ...
    def delete(self, namespace: str, id: str) -> None: ...


# --------------------------------------------------------------------------- Reserved phase 1–3 ports (one method each, with fakes)


class CalendarEvent(BaseModel, frozen=True):
    id: str
    title: SafeStr
    start: AwareDatetime
    end: AwareDatetime


@runtime_checkable
class CalendarPort(Protocol):
    def list_events(
        self, calendar_id: str, start: AwareDatetime, end: AwareDatetime
    ) -> list[CalendarEvent]: ...


@runtime_checkable
class TelephonyPort(Protocol):
    def place_call(self, call: PortCall, line_id: str, to: str, script: SafeStr) -> str: ...


class SandboxResult(BaseModel, frozen=True):
    ok: bool
    stdout: str
    artifacts: tuple[str, ...]


@runtime_checkable
class SandboxPort(Protocol):
    def run(self, call: PortCall, code: str, timeout_s: int) -> SandboxResult: ...


@runtime_checkable
class AdPlatformPort(Protocol):
    def set_budget(self, call: PortCall, campaign_ref: str, daily: Money) -> None: ...


# --------------------------------------------------------------------------- the registry one process holds


DESK_SECRET_PREFIX: Mapping[Desk, str] = {
    Desk.OPERATOR: "operator/",
    Desk.ASSISTANT: "assistant/",
    Desk.GOVERNANCE: "governance/",
}
"""SPEC §13 one credential set per desk: the secrets prefix each token's process is scoped to."""


@dataclass
class PortSet:
    """The port registry one process holds. `for_desk` is how the Operator process is built without the
    owner mailbox, the private model or the vault key (§5)."""

    whatsapp: WhatsAppPort
    coat_mail: CoatMailboxPort
    owner_mail: OwnerMailboxPort | None
    phone: PhoneBodyPort
    card: CardIssuerPort
    stt: SttPort
    tts: TtsPort
    models: Mapping[ModelRole, ModelPort]
    private_model: Tier2ModelPort | None
    secrets: SecretsPort
    objects: ObjectStoragePort
    bank: BankFeedPort
    second: SecondChannelPort
    vector: VectorIndexPort
    calendar: CalendarPort
    telephony: TelephonyPort
    sandbox: SandboxPort
    ads: AdPlatformPort
    call_log: CallLog

    def __post_init__(self) -> None:
        """``CoatMailboxPort`` and ``OwnerMailboxPort`` differ only in the ``kind`` literal, which
        ``isinstance`` cannot see (a ``runtime_checkable`` Protocol checks that the member exists,
        not its value), so the registry asserts the values itself: an owner-mailbox object can
        never sit in ``coat_mail`` and vice versa (SPEC §5 §9)."""
        if getattr(self.coat_mail, "kind", None) != "coat":
            raise TypeError("PortSet.coat_mail must be a CoatMailboxPort (kind == 'coat')")
        if self.owner_mail is not None and getattr(self.owner_mail, "kind", None) != "owner":
            raise TypeError("PortSet.owner_mail must be an OwnerMailboxPort (kind == 'owner')")

    def for_desk(self, token: DeskToken) -> PortSet:
        """A narrowed copy for the process ``token`` belongs to (the original is untouched).

        ``OperatorToken``: ``owner_mail=None``, ``private_model=None``, the ``PRIVATE_TIER2`` model
        role dropped, ``secrets=secrets.scoped("operator/")``. ``AssistantToken``: everything,
        ``secrets.scoped("assistant/")``. ``GovernanceToken`` (ingress, scheduler, CLI): like the
        Operator narrowing with ``secrets.scoped("governance/")``. An ``AuditorToken`` is not a
        ``DeskToken`` and is refused: the auditor process is wired with its own credentials. The
        token must be one ``mint`` produced (``AuthError`` otherwise).
        """
        if not isinstance(token, DeskToken):
            raise TypeError("PortSet.for_desk takes a DeskToken; the auditor has its own PortSet")
        require_minted(token, "PortSet.for_desk")
        desk = Desk(token.desk)
        secrets = self.secrets.scoped(DESK_SECRET_PREFIX[desk])
        if desk is Desk.ASSISTANT:
            return replace(self, secrets=secrets)
        models: dict[ModelRole, ModelPort] = {
            role: port for role, port in self.models.items() if role != ModelRole.PRIVATE_TIER2
        }
        return replace(self, owner_mail=None, private_model=None, models=models, secrets=secrets)
