"""Every type that crosses a wave boundary (DESIGN §3.5 ``nour/core/contracts.py``; SPEC §4 §5 §6 §12).

Inbound events (SPEC §4 Ingest/Authenticate), the signed :class:`AuthStamp` (DESIGN §4d: owner
and passphrase verification are set server-side before any prompt exists), action proposals and
tier decisions (SPEC §6; nothing the model emits lowers a tier), the one-shot :class:`ReleaseToken`
(DESIGN §4c), tool specs (SPEC §7), the one-way gate's :class:`TaskHandoff` (SPEC §5), the audit
entry (SPEC §12) and the *upward* protocols that earlier waves depend on and later waves
implement (so siblings never import each other, DESIGN §8).

Every model is frozen. Text that Nour writes (``Event.owner_text``, ``TaskHandoff.title/brief``,
``AuditEntryIn.counterpart``, ``ActionOutcome.detail``, ``CriticScore.notes``) is ``SafeStr``.
Every timestamp is ``AwareDatetime``.

``CoatConfig`` lives in ``nour/config/schema.py``, a package above ``nour.core`` in the import
graph. It is imported here only under ``TYPE_CHECKING``; at runtime the name is bound to a
``runtime_checkable`` Protocol with the same key attributes, so ``ExecContext.coat`` and
``CriticLike.score`` are typed on the real class for mypy while pydantic validates the field
structurally (a real ``CoatConfig`` passes; a dict or a string does not) without any import
from ``nour.config``.
"""

from __future__ import annotations

import re
import unicodedata
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ValidationError, field_validator, model_validator

from nour.core.ports import Attachment, PortCall, Transcript
from nour.core.tokens import DeskToken, require_minted
from nour.core.types import (
    ActionCategory,
    ActionStatus,
    ActionTier,
    Actor,
    Authority,
    Channel,
    CoatId,
    DataTier,
    Desk,
    DeskScope,
    EventKind,
    FreezeScope,
    Hash,
    IncidentType,
    Money,
    Origin,
    PassphraseOutcome,
    Reason,
    RefusalCode,
    SafeStr,
    SourceKind,
    Ulid,
)

if TYPE_CHECKING:
    from nour.config.schema import CoatConfig
else:

    @runtime_checkable
    class CoatConfig(Protocol):
        """Runtime stand-in for ``nour.config.schema.CoatConfig`` (SPEC §5 coat bundle): the
        attributes a handler or critic reads. Structural, so no import from ``nour.config``."""

        name: str
        slug: str
        legal_entity: str
        desks_allowed: Any
        identity: Any
        mandate: Any
        approval_rules: Any
        allowed_activities: Any


# --------------------------------------------------------------------------- inbound events (§4 Ingest/Authenticate)


class WhatsAppPayload(BaseModel, frozen=True):
    provider_msg_id: str
    line_id: str
    sender_display: str | None = None
    audio_ref: str | None = None


class EmailPayload(BaseModel, frozen=True):
    provider_msg_id: str
    mailbox: str
    to: tuple[str, ...]
    subject: str
    attachments: tuple[Attachment, ...] = ()
    headers: Mapping[str, str] = {}
    dkim_pass: bool


class PhoneNotificationPayload(BaseModel, frozen=True):
    app: str
    title: str
    device_id: str


class TimerPayload(BaseModel, frozen=True):
    timer_name: str
    slot: AwareDatetime


class ApprovalDecisionPayload(BaseModel, frozen=True):
    approval_id: Ulid
    decision: Literal["approve", "reject"]
    reason: str | None = None


class HandoffPayload(BaseModel, frozen=True):
    handoff_id: Ulid


class SecondChannelPayload(BaseModel, frozen=True):
    token: str | None = None


class ReadbackPayload(BaseModel, frozen=True):
    pending_id: Ulid


PAYLOAD_MODELS: Mapping[SourceKind, type[BaseModel]] = MappingProxyType(
    {
        SourceKind.WHATSAPP: WhatsAppPayload,
        SourceKind.EMAIL: EmailPayload,
        SourceKind.PHONE_NOTIFICATION: PhoneNotificationPayload,
        SourceKind.TIMER: TimerPayload,
        SourceKind.APPROVAL_DECISION: ApprovalDecisionPayload,
        SourceKind.HANDOFF: HandoffPayload,
        SourceKind.SECOND_CHANNEL: SecondChannelPayload,
        SourceKind.STAFF_LINE: WhatsAppPayload,
        SourceKind.READBACK: ReadbackPayload,
    }
)
"""kind + validated JSON payload instead of one nullable column per kind (DESIGN §5.2 ``inbox_event``)."""


class RawInbound(BaseModel, frozen=True):
    """One inbox_event row. `body` is ALREADY redacted by IngressRedactor; the attempt outcome rides along.

    ``payload`` is validated against ``PAYLOAD_MODELS[source_kind]`` at construction, so a row
    whose payload does not fit its kind never exists.
    """

    id: Ulid
    source_kind: SourceKind
    channel: Channel
    line_id: str | None
    sender: str
    origin: Origin
    received_at: AwareDatetime
    body: str | None
    audio_ref: str | None
    payload: dict[str, Any]
    signature_valid: bool
    passphrase_attempt: PassphraseOutcome | None
    attempt_id: Ulid | None

    @model_validator(mode="after")
    def _payload_fits_kind(self) -> RawInbound:
        model = PAYLOAD_MODELS[SourceKind(self.source_kind)]
        try:
            model.model_validate(self.payload)
        except ValidationError as exc:
            where = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
            raise ValueError(
                f"payload does not fit {model.__name__} for {self.source_kind}: {', '.join(where)}"
            ) from None
        return self

    def typed_payload(self) -> BaseModel:
        """The payload as its kind's model."""
        return PAYLOAD_MODELS[SourceKind(self.source_kind)].model_validate(self.payload)


class RoutedInbound(BaseModel, frozen=True):
    raw: RawInbound
    desk: Desk
    coat_id: CoatId | None
    kind: EventKind
    is_owner_thread: bool


class ObservedText(BaseModel, frozen=True):
    """Third-party content. Never carries authority; rendered inside <observed source=…> fences after LeakGuard.redact."""

    text: str
    source: str
    mime: str = "text/plain"


class FoundInstruction(BaseModel, frozen=True):
    """SPEC §12: an instruction found inside observed content; quoted, ignored, logged."""

    quote: str
    location: str
    mentions_money: bool
    pattern: str


class AuthStamp(BaseModel, frozen=True):
    """§4: set server-side before the model sees the event; signed so no other producer can mint an Event.

    Structural rules (DESIGN §3.12): ``passphrase_verified`` implies ``owner_verified``, and
    ``authority == OWNER`` exactly when ``owner_verified``; a stamp that breaks them is refused
    at construction, before ``Authenticator.verify`` ever sees it.
    """

    owner_verified: bool
    passphrase_verified: bool
    readback_confirmed: bool
    authority: Authority
    attempt_id: Ulid | None
    sig: bytes

    @model_validator(mode="after")
    def _monotone(self) -> AuthStamp:
        if self.passphrase_verified and not self.owner_verified:
            raise ValueError("passphrase_verified requires owner_verified (SPEC §6)")
        if (self.authority == Authority.OWNER) != self.owner_verified:
            raise ValueError("authority is OWNER exactly when owner_verified (SPEC §2)")
        return self


class AuthFlags(BaseModel, frozen=True):
    """What the prompt sees (no signature bytes)."""

    owner_verified: bool
    passphrase_verified: bool
    authority: Authority
    readback_confirmed: bool


class Event(BaseModel, frozen=True):
    """The authenticated event one loop iteration runs on; produced only by ``Authenticator.stamp``."""

    id: Ulid
    kind: EventKind
    raw: RawInbound
    desk: Desk
    coat_id: CoatId | None
    auth: AuthStamp
    owner_text: SafeStr | None
    observed: tuple[ObservedText, ...]
    transcript: Transcript | None
    found_instructions: tuple[FoundInstruction, ...]
    is_owner_thread: bool

    def flags(self) -> AuthFlags:
        """The stamp without its signature: what the prompt renders."""
        return AuthFlags(
            owner_verified=self.auth.owner_verified,
            passphrase_verified=self.auth.passphrase_verified,
            authority=self.auth.authority,
            readback_confirmed=self.auth.readback_confirmed,
        )


# --------------------------------------------------------------------------- actions (§4 Act, §6)


class ActionProposal(BaseModel, frozen=True, extra="forbid"):
    """Parsed from a ModelToolCall by DeskRuntime. Nothing here lowers a tier: every tier-relevant attribute
    is recomputed by TierResolver from ToolSpec, config and lookups.

    ``extra="forbid"``: a parser that copies a model-emitted ``tier=`` / ``high_impact=`` kwarg into
    the constructor gets a ``ValidationError``, never a silently dropped field. The model's claims
    live only in ``model_claimed_*`` and are logged, never routed on (DESIGN §4c).

    ``readback_confirmed`` is *informational*: it records that ``ReadBackLedger`` re-issued this
    proposal after a confirmation. The proof is the signed stamp — ``TierResolver`` reads
    ``event.auth.readback_confirmed`` (``AuthStamp``, HMAC-signed by ``Authenticator``) and never
    this flag, so a constructor argument cannot skip a read-back (SPEC §9; DESIGN §4d).
    """

    id: Ulid
    tool: str
    desk: Desk
    coat_id: CoatId | None
    args: dict[str, Any]
    counterpart: str | None
    amount: Money | None
    reason: Reason | None
    trigger_event_id: Ulid
    model_claimed_tier: ActionTier | None = None
    model_claimed_data_tier: DataTier | None = None
    readback_confirmed: bool = False


class TierDecision(BaseModel, frozen=True, extra="forbid"):
    """``TierResolver.resolve`` output: the tier, why, and every server-derived attribute.
    ``extra="forbid"`` so a misspelt rule attribute is an error, not an ignored kwarg."""

    tier: ActionTier
    category: ActionCategory
    rules_hit: tuple[str, ...]
    refusal: RefusalCode | None = None
    readback_required: bool = False
    deferred_until: AwareDatetime | None = None
    high_impact: bool
    outbound: bool
    irreversible: bool
    in_owner_name: bool
    new_counterpart: bool
    data_tier: DataTier


class ResolvedAction(BaseModel, frozen=True):
    proposal: ActionProposal
    decision: TierDecision
    desk: Desk
    coat_id: CoatId | None
    resolved_at: AwareDatetime


@dataclass(frozen=True, slots=True)
class ReleaseToken:
    """§6: one-shot capability; the nonce is a row in `releases`, burnt by ToolExecutor.execute."""

    call_id: Ulid
    nonce: str
    tier: ActionTier
    approval_id: Ulid | None
    minted_at: AwareDatetime
    minted_by: Literal["gate", "approval"]

    def __post_init__(self) -> None:
        if not self.call_id or not self.nonce:
            raise ValueError("ReleaseToken needs a call_id and a nonce")
        if self.minted_by not in ("gate", "approval"):
            raise ValueError("ReleaseToken.minted_by is 'gate' or 'approval'")
        if self.minted_at.tzinfo is None or self.minted_at.tzinfo.utcoffset(self.minted_at) is None:
            raise ValueError("ReleaseToken.minted_at must be timezone-aware")
        object.__setattr__(self, "tier", ActionTier(self.tier))


class ReleasedAction(BaseModel, frozen=True):
    """Minted only by ApprovalsQueue.decide; the flags are derived from the signed decision Event and the
    second_channel_challenge table, never from a constructor argument."""

    action: ResolvedAction
    approval_id: Ulid
    passphrase_verified: bool
    second_channel_confirmed: bool
    decided_by_event_id: Ulid
    release: ReleaseToken


class ToolResult(BaseModel, frozen=True):
    ok: bool
    output: dict[str, Any]
    outbound_sent: bool = False
    error: str | None = None
    dry_run: bool = False


class ActionOutcome(BaseModel, frozen=True):
    call_id: Ulid
    status: ActionStatus
    decision: TierDecision
    audit_id: Ulid
    approval_id: Ulid | None
    result_hash: Hash
    detail: SafeStr | None = None


class ExecContext(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What a tool handler runs under: the event, the coat, the burnt release, the ``PortCall``
    every port method must be given, the clock reading and the desk token (which must be one
    ``mint`` produced, not merely an ``isinstance`` look-alike)."""

    event: Event
    coat: CoatConfig | None
    release: ReleaseToken
    call: PortCall
    now: AwareDatetime
    token: DeskToken

    @field_validator("token")
    @classmethod
    def _minted(cls, value: DeskToken) -> DeskToken:
        require_minted(value, "ExecContext")
        return value


ToolHandler = Callable[[ActionProposal, ExecContext], ToolResult]


class ToolSpec(BaseModel, frozen=True, arbitrary_types_allowed=True, extra="forbid"):
    """§7 capability register entry at the tool level. Every tier-relevant attribute lives here, not in the model's output.

    Structural rules: ``exempt_from_kill`` (SPEC §12 post-kill: owner-thread replies and
    second-channel alerts only) is refused for a tool that spends, is high impact, is
    irreversible or acts in the owner's name; a spending tool names its ``amount_arg``;
    ``counterpart_arg`` / ``amount_arg`` must be fields of ``args_model``; ``phase >= 0``;
    an unknown keyword (``extra="forbid"``) is a ``ValidationError``, so a misspelt flag can
    never silently default to the permissive value.
    """

    name: str
    desk: DeskScope
    category: ActionCategory
    default_tier: ActionTier
    data_tier_max: DataTier
    outbound: bool
    spends: bool
    high_impact: bool
    irreversible: bool
    in_owner_name: bool
    side_effect: bool
    exempt_from_kill: bool = False
    phase: int
    description: str
    args_model: type[BaseModel]
    handler: ToolHandler | None
    counterpart_arg: str | None = None
    amount_arg: str | None = None
    coat_from_args: bool = True

    @field_validator("name")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ToolSpec.name is required")
        return value

    @field_validator("phase")
    @classmethod
    def _phase(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ToolSpec.phase is 0 or later")
        return value

    @model_validator(mode="after")
    def _structural(self) -> ToolSpec:
        if self.exempt_from_kill and (
            self.spends or self.high_impact or self.irreversible or self.in_owner_name
        ):
            raise ValueError(
                "exempt_from_kill is only for owner-thread replies and alerts: never for a tool "
                "that spends, is high impact, is irreversible or acts in the owner's name"
            )
        if self.spends and self.amount_arg is None:
            raise ValueError("a spending tool names its amount_arg")
        fields = self.args_model.model_fields
        for label, arg in (
            ("counterpart_arg", self.counterpart_arg),
            ("amount_arg", self.amount_arg),
        ):
            if arg is not None and arg not in fields:
                raise ValueError(f"{label} {arg!r} is not a field of {self.args_model.__name__}")
        return self


# --------------------------------------------------------------------------- one-way gate (§5)


class Tier0Ref(BaseModel, frozen=True):
    """A fact is a reference to a record the Operator can already see — never free text."""

    kind: Literal["knowledge_pack", "contact", "config", "task"]
    ref: str

    @field_validator("ref")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Tier0Ref.ref is required")
        return value


ASSISTANT_ID_PREFIX = "asst_"
"""Defence-in-depth substring rule for the one-way gate (SPEC §5; DESIGN §4a).

Ids are 26-character ULIDs from ``IdGenerator`` with no desk prefix, so no record Nour produces
carries ``asst_``; the rule exists so that a convention or an external reference that *does*
label Assistant-side ids this way can never be smuggled through ``title``/``brief``/``facts``.
The enforcement point for ids is ``HandoffQueue.push`` (``nour/policy/handoff.py``), which
resolves every ``Tier0Ref.ref`` against Operator-visible records (knowledge-pack keys,
Operator-partition contacts, config paths, Operator tasks) and refuses anything else; a bare
Assistant ULID fails that lookup even though it passes this substring check."""

_EMAIL_RE = re.compile(r"[^\s@<>()\[\]:;,\"']+@[^\s@<>()\[\]:;,\"']+\.[^\W\d_]{2,}")
_FORBIDDEN_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    ("vault://", "a vault uri"),
    ("{{", "a placeholder"),
    (ASSISTANT_ID_PREFIX, "an Assistant-partition id"),
)


def handoff_violations(text: str) -> list[str]:
    """What in ``text`` may not cross the one-way gate (SPEC §5; DESIGN §4a): a ``vault://`` uri,
    a ``{{`` placeholder, an e-mail address or an Assistant-partition id. Case- and
    width-insensitive."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    found = [what for needle, what in _FORBIDDEN_SUBSTRINGS if needle in folded]
    if _EMAIL_RE.search(folded):
        found.append("an e-mail address")
    return found


class TaskHandoff(BaseModel, frozen=True):
    """The one-way gate's typed task (SPEC §5): Tier 0 references only, never free facts."""

    id: Ulid
    coat_id: CoatId
    title: SafeStr
    brief: SafeStr
    facts: tuple[Tier0Ref, ...] = ()
    due: AwareDatetime | None = None
    source_event_id: Ulid

    @model_validator(mode="after")
    def _nothing_crosses(self) -> TaskHandoff:
        for name, text in (("title", self.title), ("brief", self.brief)):
            found = handoff_violations(text)
            if found:
                raise ValueError(f"TaskHandoff.{name} carries {', '.join(found)}")
        for i, fact in enumerate(self.facts):
            found = handoff_violations(fact.ref)
            if found:
                raise ValueError(f"TaskHandoff.facts[{i}] carries {', '.join(found)}")
        return self


# --------------------------------------------------------------------------- audit (§12)


class AuditEntryIn(BaseModel, frozen=True):
    """One audit row as the log receives it (SPEC §12 columns); objects are hashed, never stored."""

    ts: AwareDatetime
    desk: Desk
    coat_id: CoatId | None
    actor: Actor
    action: str
    category: ActionCategory | None
    tier: ActionTier | None
    status: ActionStatus
    counterpart: SafeStr | None
    amount: Money | None
    approval_id: Ulid | None
    data_tier: DataTier
    reason: Reason
    event_id: Ulid | None
    invocation_id: Ulid
    input_obj: Mapping[str, Any]
    output_obj: Mapping[str, Any]
    phase: Literal["opened", "closed"]


class PassphraseAttempt(BaseModel, frozen=True):
    """One ``passphrase_attempt`` row: the outcome only; no body, no candidate, ever (SPEC §6 §13)."""

    id: Ulid
    event_id: Ulid | None
    sender: str
    outcome: PassphraseOutcome
    at: AwareDatetime


# --------------------------------------------------------------------------- upward protocols


@runtime_checkable
class AuditSink(Protocol):
    """Implemented by ``nour.audit.log.AuditLog``."""

    def append(self, entry: AuditEntryIn) -> Ulid: ...


@runtime_checkable
class RedactorLike(Protocol):
    """Implemented by nour.auth.passphrase.IngressRedactor; consumed by nour.events.adapters (same wave, no import)."""

    def process(
        self,
        *,
        body: str | None,
        origin: Origin,
        channel: Channel,
        sender: str,
        signature_valid: bool,
        provider_msg_id: str | None,
    ) -> tuple[str | None, PassphraseAttempt | None]: ...


@runtime_checkable
class NotifierLike(Protocol):
    """Implemented by ``nour.governance.owner_channel.OwnerChannel``."""

    def notify(
        self, text: SafeStr, kind: str, *, call: PortCall, emergency: bool = False
    ) -> Ulid | None: ...
    def alert_second_channel(self, text: SafeStr, *, call: PortCall) -> None: ...


@runtime_checkable
class FreezeLike(Protocol):
    """Implemented by ``nour.governance.freeze.FreezeGuard``; read by the tier resolver (rule 11)."""

    def scope(self) -> frozenset[FreezeScope]: ...


@runtime_checkable
class AuthFailureSink(Protocol):
    """Implemented by ``nour.governance.watchdog.Watchdog``: any wrong passphrase freezes high impact."""

    def on_auth_failure(self, attempt: PassphraseAttempt) -> None: ...


@runtime_checkable
class IncidentSink(Protocol):
    """Implemented by ``nour.governance.incidents.IncidentService``."""

    def open(
        self,
        type: IncidentType,
        detected_by: Actor,
        first_response: SafeStr,
        frozen_scope: FreezeScope | None,
        event_id: Ulid | None,
    ) -> Ulid: ...


@runtime_checkable
class CounterpartLookup(Protocol):
    """Implemented by the CRM and the beneficiary registry (rule 5: new counterpart → K)."""

    def is_known(self, coat_id: CoatId, desk: Desk, counterpart: str) -> bool: ...


with warnings.catch_warnings():
    # DESIGN §3.5 fixes the field name ``register`` (the language register of the draft);
    # pydantic 2.13 has a ``BaseModel.register`` helper and warns about the shadow. The instance
    # attribute is the float; nothing in Nour calls the class-level helper.
    warnings.filterwarnings("ignore", message='Field name "register"', category=UserWarning)

    class CriticScore(BaseModel, frozen=True):
        """SPEC §8 self-critic verdict on an outbound draft; ``passed=False`` blocks execution."""

        tone: float
        claims: float
        compliance: float
        register: float
        passed: bool
        notes: SafeStr


@runtime_checkable
class CriticLike(Protocol):
    """Implemented by ``nour.language.critic.Critic``."""

    def score(self, draft: SafeStr, coat: CoatConfig, counterpart_register: str) -> CriticScore: ...
