"""nour/core/contracts.py (DESIGN §3.5, §4a, §4c, §4d; SPEC §4 §5 §6 §12): the wave-crossing types.

Proves (MODULES.md "core"): every contract model is frozen; ``PAYLOAD_MODELS`` covers every
``SourceKind`` and ``RawInbound`` validates its payload per kind; ``AuthStamp`` cannot claim
passphrase without owner; ``Event.flags`` drops the signature; ``ReleaseToken`` is immutable and
hashable; ``TaskHandoff`` rejects each forbidden pattern; ``ExecContext`` takes a structural
``CoatConfig`` and a real ``DeskToken``; ``ToolSpec`` keeps its structural rules; the upward
protocols are runtime-checkable.

Tokens come from the ``tokens`` fixture (``tests/conftest.py`` is the one minting site).
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from nour.core import contracts
from nour.core.contracts import (
    ASSISTANT_ID_PREFIX,
    PAYLOAD_MODELS,
    ActionOutcome,
    ActionProposal,
    ApprovalDecisionPayload,
    AuditEntryIn,
    AuditSink,
    AuthFailureSink,
    AuthFlags,
    AuthStamp,
    CounterpartLookup,
    CriticLike,
    CriticScore,
    EmailPayload,
    Event,
    ExecContext,
    FoundInstruction,
    FreezeLike,
    HandoffPayload,
    IncidentSink,
    NotifierLike,
    ObservedText,
    PassphraseAttempt,
    PhoneNotificationPayload,
    RawInbound,
    ReadbackPayload,
    RedactorLike,
    ReleasedAction,
    ReleaseToken,
    ResolvedAction,
    RoutedInbound,
    SecondChannelPayload,
    TaskHandoff,
    Tier0Ref,
    TierDecision,
    TimerPayload,
    ToolHandler,
    ToolResult,
    ToolSpec,
    WhatsAppPayload,
    handoff_violations,
)
from nour.core.errors import AuthError
from nour.core.hashing import content_hash
from nour.core.leakguard import LeakGuard
from nour.core.ports import PortCall, Transcript
from nour.core.tokens import AnyToken, AssistantToken
from nour.core.types import (
    OWNER_WHATSAPP,
    ActionCategory,
    ActionStatus,
    ActionTier,
    Actor,
    Authority,
    CoatId,
    DataTier,
    Desk,
    DeskScope,
    EventKind,
    FreezeScope,
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

AT = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
NAIVE = datetime(2026, 10, 5, 7, 0)  # noqa: DTZ001 - naive on purpose: every timestamp must refuse it
KEY = b"leakguard-key-for-contracts-tests-01"
U1 = Ulid("01ARZ3NDEKTSV4RRFFQ69G5FAV")
U2 = Ulid("01ARZ3NDEKTSV4RRFFQ69G5FAX")
U3 = Ulid("01ARZ3NDEKTSV4RRFFQ69G5FAZ")
COAT = CoatId("buzz-avenue")
GUARD = LeakGuard(KEY)
CALL = PortCall(audit_id=U3, desk=Desk.ASSISTANT, coat_id=COAT)


def safe(text: str) -> SafeStr:
    return GUARD.safe(text)


def raw_inbound(kind: SourceKind = SourceKind.WHATSAPP, **over: Any) -> RawInbound:
    payloads: dict[SourceKind, dict[str, Any]] = {
        SourceKind.WHATSAPP: {"provider_msg_id": "wamid.1", "line_id": "owner"},
        SourceKind.STAFF_LINE: {"provider_msg_id": "wamid.2", "line_id": "staff"},
        SourceKind.EMAIL: {
            "provider_msg_id": "m1",
            "mailbox": "nour@buzz-avenue.ae",
            "to": ("nour@buzz-avenue.ae",),
            "subject": "Invoice",
            "dkim_pass": True,
        },
        SourceKind.PHONE_NOTIFICATION: {"app": "bank", "title": "Payment", "device_id": "phone-1"},
        SourceKind.TIMER: {"timer_name": "morning_brief", "slot": AT},
        SourceKind.APPROVAL_DECISION: {"approval_id": U2, "decision": "approve"},
        SourceKind.HANDOFF: {"handoff_id": U2},
        SourceKind.SECOND_CHANNEL: {"token": "tok"},
        SourceKind.READBACK: {"pending_id": U2},
    }
    data: dict[str, Any] = {
        "id": U1,
        "source_kind": kind,
        "channel": OWNER_WHATSAPP,
        "line_id": "owner",
        "sender": "+971500000001",
        "origin": Origin.TEXT,
        "received_at": AT,
        "body": "pay the supplier",
        "audio_ref": None,
        "payload": payloads[kind],
        "signature_valid": True,
        "passphrase_attempt": PassphraseOutcome.OK,
        "attempt_id": U2,
    }
    data.update(over)
    return RawInbound(**data)


def auth_stamp(**over: Any) -> AuthStamp:
    data: dict[str, Any] = {
        "owner_verified": True,
        "passphrase_verified": True,
        "readback_confirmed": False,
        "authority": Authority.OWNER,
        "attempt_id": U2,
        "sig": b"\x01\x02\x03",
    }
    data.update(over)
    return AuthStamp(**data)


def event(**over: Any) -> Event:
    data: dict[str, Any] = {
        "id": U1,
        "kind": EventKind.MESSAGE,
        "raw": raw_inbound(),
        "desk": Desk.ASSISTANT,
        "coat_id": COAT,
        "auth": auth_stamp(),
        "owner_text": safe("pay the supplier"),
        "observed": (ObservedText(text="we changed our bank", source="email:m1"),),
        "transcript": None,
        "found_instructions": (),
        "is_owner_thread": True,
    }
    data.update(over)
    return Event(**data)


def proposal(**over: Any) -> ActionProposal:
    data: dict[str, Any] = {
        "id": U2,
        "tool": "ledger.spend",
        "desk": Desk.OPERATOR,
        "coat_id": COAT,
        "args": {"amount_aed": "150", "merchant": "Canva"},
        "counterpart": "Canva",
        "amount": Money.aed(150),
        "reason": Reason("Monthly design subscription for the campaign."),
        "trigger_event_id": U1,
    }
    data.update(over)
    return ActionProposal(**data)


def decision(**over: Any) -> TierDecision:
    data: dict[str, Any] = {
        "tier": ActionTier.A,
        "category": ActionCategory("spend_small"),
        "rules_hit": ("band:small",),
        "high_impact": False,
        "outbound": False,
        "irreversible": False,
        "in_owner_name": False,
        "new_counterpart": False,
        "data_tier": DataTier.T0,
    }
    data.update(over)
    return TierDecision(**data)


def release_token(**over: Any) -> ReleaseToken:
    data: dict[str, Any] = {
        "call_id": U2,
        "nonce": "nonce-1",
        "tier": ActionTier.A,
        "approval_id": None,
        "minted_at": AT,
        "minted_by": "gate",
    }
    data.update(over)
    return ReleaseToken(**data)


class CoatStandIn(BaseModel, frozen=True):
    """Structurally a CoatConfig (DESIGN §3.6 fields the handlers read)."""

    name: str = "Buzz Avenue"
    slug: CoatId = COAT
    legal_entity: str = "Buzz Avenue FZ-LLC"
    desks_allowed: tuple[Desk, ...] = (Desk.OPERATOR, Desk.ASSISTANT)
    identity: dict[str, str] = {"whatsapp_line": "+971500000009"}
    mandate: dict[str, Any] = {}
    approval_rules: dict[str, Any] = {}
    allowed_activities: tuple[str, ...] = ("sell",)


class SpendArgs(BaseModel, frozen=True):
    amount_aed: str
    merchant: str


def handler(p: ActionProposal, ctx: ExecContext) -> ToolResult:
    return ToolResult(ok=True, output={"merchant": p.args["merchant"]})


def tool_spec(**over: Any) -> ToolSpec:
    data: dict[str, Any] = {
        "name": "ledger.spend",
        "desk": DeskScope.BOTH,
        "category": ActionCategory("spend_small"),
        "default_tier": ActionTier.A,
        "data_tier_max": DataTier.T0,
        "outbound": False,
        "spends": True,
        "high_impact": False,
        "irreversible": True,
        "in_owner_name": False,
        "side_effect": True,
        "phase": 0,
        "description": "Spend from the desk card through the issuer.",
        "args_model": SpendArgs,
        "handler": handler,
        "counterpart_arg": "merchant",
        "amount_arg": "amount_aed",
    }
    data.update(over)
    return ToolSpec(**data)


# --------------------------------------------------------------------------- everything is frozen


MODELS: dict[str, type[BaseModel]] = {
    name: obj
    for name, obj in vars(contracts).items()
    if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == contracts.__name__
}


def test_every_contract_model_is_frozen() -> None:
    assert len(MODELS) == 28
    for name, model in MODELS.items():
        assert model.model_config.get("frozen") is True, name
    for sample in (raw_inbound(), auth_stamp(), event(), proposal(), decision(), tool_spec()):
        first = next(iter(type(sample).model_fields))
        with pytest.raises(ValidationError):
            setattr(sample, first, getattr(sample, first))


# --------------------------------------------------------------------------- inbound events


def test_payload_models_cover_every_source_kind_and_are_read_only() -> None:
    assert set(PAYLOAD_MODELS) == set(SourceKind)
    assert isinstance(PAYLOAD_MODELS, MappingProxyType)
    with pytest.raises(TypeError):
        PAYLOAD_MODELS[SourceKind.TIMER] = TimerPayload  # type: ignore[index]
    assert PAYLOAD_MODELS[SourceKind.WHATSAPP] is WhatsAppPayload
    assert PAYLOAD_MODELS[SourceKind.STAFF_LINE] is WhatsAppPayload
    assert PAYLOAD_MODELS[SourceKind.EMAIL] is EmailPayload
    assert PAYLOAD_MODELS[SourceKind.PHONE_NOTIFICATION] is PhoneNotificationPayload
    assert PAYLOAD_MODELS[SourceKind.TIMER] is TimerPayload
    assert PAYLOAD_MODELS[SourceKind.APPROVAL_DECISION] is ApprovalDecisionPayload
    assert PAYLOAD_MODELS[SourceKind.HANDOFF] is HandoffPayload
    assert PAYLOAD_MODELS[SourceKind.SECOND_CHANNEL] is SecondChannelPayload
    assert PAYLOAD_MODELS[SourceKind.READBACK] is ReadbackPayload
    for model in PAYLOAD_MODELS.values():
        assert model.model_config.get("frozen") is True


@pytest.mark.parametrize("kind", list(SourceKind))
def test_raw_inbound_validates_its_payload_per_kind(kind: SourceKind) -> None:
    raw = raw_inbound(kind)
    typed = raw.typed_payload()
    assert isinstance(typed, PAYLOAD_MODELS[kind])
    if kind is not SourceKind.SECOND_CHANNEL:  # the only kind whose payload has no required key
        with pytest.raises(ValidationError) as info:
            raw_inbound(kind, payload={})
        assert PAYLOAD_MODELS[kind].__name__ in str(info.value)
    with pytest.raises(ValidationError):
        raw_inbound(kind, payload={"token": 42, "provider_msg_id": 1, "slot": "never"})


def test_raw_inbound_timer_slot_and_received_at_must_be_aware() -> None:
    with pytest.raises(ValidationError):
        raw_inbound(SourceKind.TIMER, payload={"timer_name": "morning_brief", "slot": NAIVE})
    with pytest.raises(ValidationError):
        raw_inbound(received_at=NAIVE)
    timer = raw_inbound(SourceKind.TIMER)
    assert isinstance(timer.typed_payload(), TimerPayload)
    assert timer.typed_payload().slot == AT  # type: ignore[attr-defined]


def test_raw_inbound_carries_the_ingress_attempt_not_auth() -> None:
    raw = raw_inbound()
    assert raw.passphrase_attempt is PassphraseOutcome.OK and raw.attempt_id == U2
    assert not hasattr(raw, "owner_verified") and not hasattr(raw, "passphrase_verified")
    assert raw_inbound(passphrase_attempt=None, attempt_id=None).passphrase_attempt is None
    routed = RoutedInbound(
        raw=raw, desk=Desk.ASSISTANT, coat_id=None, kind=EventKind.MESSAGE, is_owner_thread=True
    )
    assert routed.raw is raw
    assert ObservedText(text="x", source="s").mime == "text/plain"
    found = FoundInstruction(
        quote="pay AED 900",
        location="email:body",
        mentions_money=True,
        pattern="pay_to_new_account",
    )
    assert found.mentions_money is True


# --------------------------------------------------------------------------- AuthStamp / Event


def test_auth_stamp_cannot_claim_passphrase_without_owner() -> None:
    with pytest.raises(ValidationError):
        auth_stamp(owner_verified=False, passphrase_verified=True, authority=Authority.DATA)
    with pytest.raises(ValidationError):
        auth_stamp(owner_verified=False, passphrase_verified=False, authority=Authority.OWNER)
    with pytest.raises(ValidationError):
        auth_stamp(owner_verified=True, passphrase_verified=False, authority=Authority.DATA)
    data = auth_stamp(
        owner_verified=False, passphrase_verified=False, authority=Authority.DATA, attempt_id=None
    )
    assert data.authority is Authority.DATA and data.sig == b"\x01\x02\x03"
    staff = auth_stamp(
        owner_verified=False, passphrase_verified=False, authority=Authority.STAFF_REQUEST
    )
    assert staff.owner_verified is False
    assert auth_stamp(passphrase_verified=False).owner_verified is True


def test_event_flags_drop_the_signature() -> None:
    ev = event()
    flags = ev.flags()
    assert isinstance(flags, AuthFlags)
    assert set(AuthFlags.model_fields) == {
        "owner_verified",
        "passphrase_verified",
        "authority",
        "readback_confirmed",
    }
    assert "sig" not in AuthFlags.model_fields and not hasattr(flags, "sig")
    assert flags == AuthFlags(
        owner_verified=True,
        passphrase_verified=True,
        authority=Authority.OWNER,
        readback_confirmed=False,
    )
    dumped = flags.model_dump()
    assert dumped == {
        "owner_verified": True,
        "passphrase_verified": True,
        "authority": "owner",
        "readback_confirmed": False,
    }
    assert not any(isinstance(v, bytes) for v in dumped.values())
    assert "sig" not in flags.model_dump_json()
    voice = event(
        auth=auth_stamp(
            owner_verified=False,
            passphrase_verified=False,
            authority=Authority.DATA,
            readback_confirmed=True,
        )
    )
    assert voice.flags().readback_confirmed is True and voice.flags().authority is Authority.DATA


def test_event_shape() -> None:
    ev = event(transcript=Transcript(text="حوّل 500", language="ar-LB", confidence=0.8, engine="e"))
    assert ev.transcript is not None and ev.transcript.text == "حوّل 500"
    assert isinstance(ev.owner_text, SafeStr)
    with pytest.raises(ValidationError):
        event(owner_text="plain owner text")
    with pytest.raises(ValidationError):
        event(observed=("not an ObservedText",))
    assert content_hash(ev) == content_hash(ev.model_copy())


# --------------------------------------------------------------------------- proposals and decisions


def test_action_proposal_defaults_keep_model_claims_separate() -> None:
    p = proposal()
    assert p.model_claimed_tier is None and p.model_claimed_data_tier is None
    assert p.readback_confirmed is False
    claimed = proposal(model_claimed_tier=ActionTier.A, model_claimed_data_tier=DataTier.T0)
    assert claimed.model_claimed_tier is ActionTier.A
    assert isinstance(p.reason, Reason)
    assert proposal(reason=None).reason is None
    with pytest.raises(ValidationError):
        proposal(reason="Two sentences. Not allowed.")
    assert proposal(reason="Coerced from a plain string.").reason == "Coerced from a plain string."
    assert "tier" not in ActionProposal.model_fields  # nothing the model emits routes
    # extra="forbid": a parser that copies a model-emitted tier/high_impact kwarg fails loudly
    for extra in (
        {"tier": "A"},
        {"high_impact": False},
        {"data_tier": 0},
        {"owner_verified": True},
    ):
        with pytest.raises(ValidationError, match="extra"):
            proposal(**extra)
    with pytest.raises(ValidationError, match="extra"):
        decision(claimed_tier="A")
    with pytest.raises(ValidationError, match="extra"):
        tool_spec(exempt_from_kil=True)  # a misspelt flag never defaults to the permissive value
    assert ActionProposal.model_config.get("extra") == "forbid"
    assert TierDecision.model_config.get("extra") == "forbid"
    assert ToolSpec.model_config.get("extra") == "forbid"


def test_readback_confirmed_on_a_proposal_is_informational() -> None:
    """SPEC §9 / DESIGN §4d: the proof of a read-back is the signed stamp on the event, which
    the resolver reads; the proposal flag only records that the ledger re-issued the proposal."""
    confirmed = proposal(readback_confirmed=True)
    assert confirmed.readback_confirmed is True
    unconfirmed_event = event(auth=auth_stamp(readback_confirmed=False))
    assert unconfirmed_event.flags().readback_confirmed is False
    assert "signed" in (ActionProposal.__doc__ or "") and "readback" in (
        ActionProposal.__doc__ or ""
    )


def test_tier_decision_and_resolved_action() -> None:
    d = decision()
    assert d.refusal is None and d.readback_required is False and d.deferred_until is None
    refused = decision(tier=ActionTier.K, refusal=RefusalCode.NO_COAT, rules_hit=("coat_required",))
    assert refused.refusal is RefusalCode.NO_COAT
    deferred = decision(deferred_until=AT + timedelta(hours=6))
    assert deferred.deferred_until == AT + timedelta(hours=6)
    with pytest.raises(ValidationError):
        decision(deferred_until=NAIVE)
    resolved = ResolvedAction(
        proposal=proposal(), decision=d, desk=Desk.OPERATOR, coat_id=COAT, resolved_at=AT
    )
    assert resolved.decision.tier is ActionTier.A
    with pytest.raises(ValidationError):
        ResolvedAction(
            proposal=proposal(), decision=d, desk=Desk.OPERATOR, coat_id=COAT, resolved_at=NAIVE
        )


def test_release_token_is_an_immutable_hashable_slotted_dataclass() -> None:
    token = release_token()
    assert dataclasses.is_dataclass(ReleaseToken)
    assert ReleaseToken.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert hasattr(ReleaseToken, "__slots__") and "nonce" in ReleaseToken.__slots__
    assert not hasattr(token, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.nonce = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        del token.nonce
    assert hash(token) == hash(release_token())
    assert token == release_token() and token != release_token(nonce="nonce-2")
    assert len({token, release_token(), release_token(nonce="nonce-2")}) == 2
    assert token.tier is ActionTier.A and token.minted_by == "gate" and token.approval_id is None
    assert release_token(tier="K", minted_by="approval", approval_id=U3).tier is ActionTier.K
    for bad in (
        {"nonce": ""},
        {"call_id": ""},
        {"minted_by": "model"},
        {"minted_at": NAIVE},
        {"tier": "X"},
    ):
        with pytest.raises(ValueError):
            release_token(**bad)


def test_released_action_outcome_and_tool_result() -> None:
    resolved = ResolvedAction(
        proposal=proposal(),
        decision=decision(tier=ActionTier.K),
        desk=Desk.OPERATOR,
        coat_id=COAT,
        resolved_at=AT,
    )
    token = release_token(tier=ActionTier.K, minted_by="approval", approval_id=U3)
    released = ReleasedAction(
        action=resolved,
        approval_id=U3,
        passphrase_verified=True,
        second_channel_confirmed=False,
        decided_by_event_id=U1,
        release=token,
    )
    assert released.release is token
    result = ToolResult(ok=True, output={"sent": 1})
    assert result.outbound_sent is False and result.error is None and result.dry_run is False
    outcome = ActionOutcome(
        call_id=U2,
        status=ActionStatus.QUEUED,
        decision=resolved.decision,
        audit_id=U3,
        approval_id=U3,
        result_hash=content_hash(result),
    )
    assert outcome.detail is None and outcome.status is ActionStatus.QUEUED
    with pytest.raises(ValidationError):
        ActionOutcome(
            call_id=U2,
            status=ActionStatus.QUEUED,
            decision=resolved.decision,
            audit_id=U3,
            approval_id=None,
            result_hash=content_hash(result),
            detail="plain",  # type: ignore[arg-type]
        )
    assert (
        ActionOutcome(
            call_id=U2,
            status=ActionStatus.EXECUTED,
            decision=resolved.decision,
            audit_id=U3,
            approval_id=None,
            result_hash=content_hash(result),
            detail=safe("ok"),
        ).detail
        == "ok"
    )


# --------------------------------------------------------------------------- ExecContext


def test_exec_context_takes_a_structural_coat_and_a_real_token(
    tokens: dict[str, AnyToken],
) -> None:
    token = tokens["operator"]
    coat = CoatStandIn()
    ctx = ExecContext(
        event=event(),
        coat=coat,  # type: ignore[arg-type]
        release=release_token(),
        call=CALL,
        now=AT,
        token=token,  # type: ignore[arg-type]
    )
    assert ctx.coat is coat and ctx.token is token and ctx.call is CALL
    assert ctx.release == release_token()
    assert (
        ExecContext(
            event=event(), coat=None, release=release_token(), call=CALL, now=AT, token=token
        ).coat
        is None
    )
    assert (
        ExecContext.model_config.get("frozen") is True
        and ExecContext.model_config.get("arbitrary_types_allowed") is True
    )
    assert ExecContext.__pydantic_complete__ is True
    hints = ExecContext.__annotations__
    assert hints["coat"] == "CoatConfig | None"  # typed on the real config class for mypy


@pytest.mark.parametrize(
    "bad",
    [
        {"coat": {"name": "Buzz Avenue", "slug": "buzz-avenue"}},
        {"coat": "buzz-avenue"},
        {"coat": CoatStandIn().model_dump()},
        {"token": "auditor-token"},  # replaced by the auditor token inside the test
        {"token": "operator"},
        {"token": Desk.OPERATOR},
        {"release": {"call_id": U2, "nonce": "n"}},
        {"release": "nonce"},
        {"call": {"audit_id": U3}},
        {"now": NAIVE},
        {"event": raw_inbound()},
    ],
    ids=lambda bad: next(iter(bad)) + ":" + str(next(iter(bad.values())))[:12],
)
def test_exec_context_rejects_the_wrong_shapes(
    bad: dict[str, Any], tokens: dict[str, AnyToken]
) -> None:
    data: dict[str, Any] = {
        "event": event(),
        "coat": CoatStandIn(),
        "release": release_token(),
        "call": CALL,
        "now": AT,
        "token": tokens["assistant"],
    }
    if bad.get("token") == "auditor-token":
        bad = {"token": tokens["auditor"]}
    data.update(bad)
    with pytest.raises(ValidationError):
        ExecContext(**data)


def test_exec_context_refuses_a_token_mint_did_not_produce(tokens: dict[str, AnyToken]) -> None:
    forged = object.__new__(AssistantToken)
    object.__setattr__(forged, "desk", Desk.ASSISTANT)
    assert isinstance(forged, AssistantToken)
    with pytest.raises(AuthError):
        ExecContext(
            event=event(), coat=None, release=release_token(), call=CALL, now=AT, token=forged
        )
    ok = ExecContext(
        event=event(),
        coat=None,
        release=release_token(),
        call=CALL,
        now=AT,
        token=tokens["assistant"],  # type: ignore[arg-type]
    )
    assert ok.token is tokens["assistant"]


def test_exec_context_coat_stand_in_needs_every_key_attribute(tokens: dict[str, AnyToken]) -> None:
    class Partial(BaseModel, frozen=True):
        name: str = "x"
        slug: str = "x"

    with pytest.raises(ValidationError):
        ExecContext(
            event=event(),
            coat=Partial(),  # type: ignore[arg-type]
            release=release_token(),
            call=CALL,
            now=AT,
            token=tokens["operator"],  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- ToolSpec


def test_tool_spec_shape_and_defaults(tokens: dict[str, AnyToken]) -> None:
    spec = tool_spec()
    assert spec.exempt_from_kill is False and spec.coat_from_args is True
    assert spec.args_model is SpendArgs and spec.handler is handler
    assert spec.handler is not None
    assert spec.handler(
        proposal(),
        ExecContext(
            event=event(),
            coat=None,
            release=release_token(),
            call=CALL,
            now=AT,
            token=tokens["operator"],  # type: ignore[arg-type]
        ),
    ).ok
    stub = tool_spec(
        name="phone.open_app",
        spends=False,
        amount_arg=None,
        counterpart_arg=None,
        handler=None,
        phase=3,
    )
    assert stub.handler is None and stub.phase == 3
    assert inspect.signature(handler).return_annotation == "ToolResult"
    assert ToolHandler.__args__[-1] is ToolResult  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        tool_spec(args_model=dict)
    with pytest.raises(ValidationError):
        tool_spec(handler="not callable")
    with pytest.raises(ValidationError):
        tool_spec(name="  ")
    with pytest.raises(ValidationError):
        tool_spec(phase=-1)
    with pytest.raises(ValidationError):
        tool_spec(desk="vault")
    with pytest.raises(ValidationError):
        tool_spec(default_tier="X")


def test_tool_spec_structural_rules() -> None:
    reply = tool_spec(
        name="owner.reply", spends=False, high_impact=False, irreversible=False, in_owner_name=False,
        outbound=True, amount_arg=None, counterpart_arg=None, exempt_from_kill=True,
    )  # fmt: skip
    assert reply.exempt_from_kill is True
    for flag in ("spends", "high_impact", "irreversible", "in_owner_name"):
        with pytest.raises(ValidationError, match="exempt_from_kill"):
            tool_spec(
                **{
                    **{
                        "spends": False,
                        "high_impact": False,
                        "irreversible": False,
                        "in_owner_name": False,
                        "amount_arg": None,
                        "counterpart_arg": None,
                        "exempt_from_kill": True,
                    },
                    flag: True,
                    **({"amount_arg": "amount_aed"} if flag == "spends" else {}),
                }
            )
    with pytest.raises(ValidationError, match="amount_arg"):
        tool_spec(spends=True, amount_arg=None)
    with pytest.raises(ValidationError, match="not a field"):
        tool_spec(amount_arg="amount")
    with pytest.raises(ValidationError, match="not a field"):
        tool_spec(counterpart_arg="vendor")


# --------------------------------------------------------------------------- one-way gate


def handoff(**over: Any) -> TaskHandoff:
    data: dict[str, Any] = {
        "id": U1,
        "coat_id": COAT,
        "title": safe("Chase the Al Noor invoice"),
        "brief": safe(
            "Invoice INV-0042 is 10 days overdue; send a polite reminder under the Buzz Avenue coat."
        ),
        "facts": (
            Tier0Ref(kind="knowledge_pack", ref="payment_terms"),
            Tier0Ref(kind="contact", ref="01ARZ3NDEKTSV4RRFFQ69G5FB0"),
        ),
        "due": AT + timedelta(days=1),
        "source_event_id": U2,
    }
    data.update(over)
    return TaskHandoff(**data)


def test_task_handoff_accepts_references_only() -> None:
    h = handoff()
    assert isinstance(h.title, SafeStr) and isinstance(h.brief, SafeStr)
    assert h.facts[0].kind == "knowledge_pack" and h.facts[1].ref.startswith("01ARZ")
    assert handoff(facts=(), due=None).facts == ()
    with pytest.raises(ValidationError):
        handoff(title="plain str title")
    with pytest.raises(ValidationError):
        handoff(brief=str(safe("downgraded")))
    with pytest.raises(ValidationError):
        handoff(facts=("free text fact",))
    with pytest.raises(ValidationError):
        Tier0Ref(kind="memory", ref="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Tier0Ref(kind="task", ref="  ")
    with pytest.raises(ValidationError):
        handoff(due=NAIVE)


FORBIDDEN = [
    ("see vault://buzz-avenue/banking/receiving#iban", "vault uri"),
    ("VAULT://owner/identity/passport#number", "vault uri, upper-case"),
    ("use {{bank.buzz-avenue.iban}} on the invoice", "placeholder"),
    ("open {{", "bare placeholder opener"),
    ("mail ahmed@example.com", "e-mail address"),
    ("mail Ahmed <ahmed.k+x@sub.example.ae>", "e-mail in brackets"),
    ("write to owner@example.com.", "e-mail with trailing dot"),
    ("ask asst_01ARZ3NDEKTSV4RRFFQ69G5FAV", "assistant id"),
    ("ask ASST_01ARZ3NDEKTSV4RRFFQ69G5FAV", "assistant id, upper-case"),
    ("ｖａｕｌｔ://buzz-avenue/x#y", "full-width vault uri"),
]


@pytest.mark.parametrize(("text", "what"), FORBIDDEN, ids=[what for _, what in FORBIDDEN])
def test_task_handoff_rejects_each_forbidden_pattern(text: str, what: str) -> None:
    assert handoff_violations(text)
    with pytest.raises(ValidationError, match="title"):
        handoff(title=safe(text))
    with pytest.raises(ValidationError, match="brief"):
        handoff(brief=safe(text))
    with pytest.raises(ValidationError, match="facts"):
        handoff(facts=(Tier0Ref(kind="config", ref=text),))


def test_handoff_violations_names_what_crosses() -> None:
    assert handoff_violations("plain brief about an overdue invoice") == []
    assert handoff_violations("nour@buzz-avenue.ae and vault://x/y#z and {{a}} and asst_1") == [
        "a vault uri",
        "a placeholder",
        "an Assistant-partition id",
        "an e-mail address",
    ]
    assert ASSISTANT_ID_PREFIX == "asst_"
    assert handoff_violations("price 12.50 @ 3 units") == []  # an @ is not an address


# --------------------------------------------------------------------------- audit


def test_audit_entry_in_shape() -> None:
    entry = AuditEntryIn(
        ts=AT,
        desk=Desk.OPERATOR,
        coat_id=COAT,
        actor=Actor.NOUR,
        action="ledger.spend",
        category=ActionCategory("spend_small"),
        tier=ActionTier.A,
        status=ActionStatus.OPENED,
        counterpart=safe("Canva"),
        amount=Money.aed(150),
        approval_id=None,
        data_tier=DataTier.T0,
        reason=Reason("Monthly design subscription for the campaign."),
        event_id=U1,
        invocation_id=U2,
        input_obj={"merchant": safe("Canva")},
        output_obj={},
        phase="opened",
    )
    assert isinstance(entry.reason, Reason) and entry.phase == "opened"
    base = entry.model_dump()
    with pytest.raises(ValidationError):
        AuditEntryIn(**{**base, "reason": "Two sentences. Refused."})
    with pytest.raises(ValidationError):
        AuditEntryIn(**{**base, "counterpart": "plain"})
    with pytest.raises(ValidationError):
        AuditEntryIn(**{**base, "phase": "middle"})
    with pytest.raises(ValidationError):
        AuditEntryIn(**{**base, "ts": NAIVE})
    with pytest.raises(ValidationError):
        AuditEntryIn(**{**base, "status": "done"})
    attempt = PassphraseAttempt(
        id=U1, event_id=None, sender="+971", outcome=PassphraseOutcome.WRONG, at=AT
    )
    assert attempt.outcome is PassphraseOutcome.WRONG
    assert set(PassphraseAttempt.model_fields) == {
        "id",
        "event_id",
        "sender",
        "outcome",
        "at",
    }  # no body, no candidate


# --------------------------------------------------------------------------- upward protocols


class Upward:
    """One object that satisfies every upward protocol."""

    def append(self, entry: AuditEntryIn) -> Ulid:
        return U3

    def process(
        self,
        *,
        body: str | None,
        origin: Origin,
        channel: str,
        sender: str,
        signature_valid: bool,
        provider_msg_id: str | None,
    ) -> tuple[str | None, PassphraseAttempt | None]:
        return body, None

    def notify(
        self, text: SafeStr, kind: str, *, call: PortCall, emergency: bool = False
    ) -> Ulid | None:
        return None

    def alert_second_channel(self, text: SafeStr, *, call: PortCall) -> None:
        return None

    def scope(self) -> frozenset[FreezeScope]:
        return frozenset()

    def on_auth_failure(self, attempt: PassphraseAttempt) -> None:
        return None

    def open(
        self,
        type: IncidentType,
        detected_by: Actor,
        first_response: SafeStr,
        frozen_scope: FreezeScope | None,
        event_id: Ulid | None,
    ) -> Ulid:
        return U3

    def is_known(self, coat_id: CoatId, desk: Desk, counterpart: str) -> bool:
        return False

    def score(self, draft: SafeStr, coat: Any, counterpart_register: str) -> CriticScore:
        return CriticScore(
            tone=1, claims=1, compliance=1, register=1, passed=True, notes=safe("fine")
        )


@pytest.mark.parametrize(
    "proto",
    [
        AuditSink,
        RedactorLike,
        NotifierLike,
        FreezeLike,
        AuthFailureSink,
        IncidentSink,
        CounterpartLookup,
        CriticLike,
    ],
    ids=lambda p: p.__name__,
)
def test_upward_protocols_are_runtime_checkable(proto: type) -> None:
    assert getattr(proto, "_is_runtime_protocol", False)
    assert isinstance(Upward(), proto)
    assert not isinstance(object(), proto)


def test_critic_score_is_frozen_and_keeps_its_register_field() -> None:
    score = CriticScore(
        tone=0.9, claims=0.8, compliance=1.0, register=0.7, passed=True, notes=safe("ok")
    )
    assert score.register == 0.7 and score.passed is True
    assert score.model_dump()["register"] == 0.7
    with pytest.raises(ValidationError):
        CriticScore(tone=0.9, claims=0.8, compliance=1.0, register=0.7, passed=True, notes="plain")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        score.passed = False  # type: ignore[misc]
