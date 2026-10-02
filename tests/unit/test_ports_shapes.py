"""nour/core/ports.py (DESIGN §3.4, §4a, §4h, §6; SPEC §4 §9 §10 §13 §16): ports, DTOs, PortCall, CallLog, PortSet.

Proves (MODULES.md "core"): every DTO is frozen; every side-effecting port method takes a
``PortCall`` first; the protocols are runtime-checkable (a stub satisfies one, a stub missing a
method does not); ``CallLog`` records with the injected clock and hashes arguments;
``PortSet.for_desk`` narrows the Operator (no owner mailbox, no private model, ``operator/``
secrets) and the Assistant (``assistant/`` secrets) and refuses the auditor; outbound text
fields accept ``SafeStr`` only.

Tokens come from the ``tokens`` fixture (``tests/conftest.py`` is the one minting site). The
owner-mailbox protocol is not named in this file (DESIGN §4a confines its references); it is
reached through the module namespace. The ``Tier2Value`` seal is read here only to prove the
``CallLog`` refuses to hash a Tier 2 value; ``tests/unit/test_walls.py`` allows that here.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Iterator, Sequence
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ValidationError

from nour.core import ports
from nour.core import tier2 as tier2_module
from nour.core.clock import DUBAI, FakeClock, process_clock, set_process_clock
from nour.core.errors import AuthError, ScopeViolation, Tier2LeakError
from nour.core.hashing import content_hash, keyed_hash
from nour.core.leakguard import LeakGuard
from nour.core.ports import (
    DESK_SECRET_PREFIX,
    AdPlatformPort,
    Attachment,
    BankFeedPort,
    BankLine,
    CalendarEvent,
    CalendarPort,
    CallLog,
    CardAuthorization,
    CardDecision,
    CardIssuerPort,
    CoatMailboxPort,
    DraftEmail,
    InboundEmail,
    InboundWhatsApp,
    IndexableText,
    MailboxPort,
    ModelMessage,
    ModelPort,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelToolCall,
    ModelUsage,
    ObjectStoragePort,
    OutboundWhatsApp,
    PhoneBodyPort,
    PhoneNotification,
    PortCall,
    PortSet,
    PrivateModelRequest,
    PrivateModelResult,
    RecordedCall,
    SandboxPort,
    SandboxResult,
    SecondChannelMessage,
    SecondChannelPort,
    SecretsPort,
    SendReceipt,
    SttPort,
    TelephonyPort,
    Tier2ModelPort,
    ToolSchema,
    Transcript,
    TtsPort,
    VectorHit,
    VectorIndexPort,
    WhatsAppPort,
)
from nour.core.tier2 import RenderWitness, SecretRef, Tier2Value
from nour.core.tokens import AnyToken, AssistantToken, AuditorToken, DeskToken
from nour.core.types import BudgetHolder, CoatId, DataTier, Desk, Money, SafeStr, Ulid

START = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)
AT = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
NAIVE = datetime(2026, 10, 5, 7, 0)  # noqa: DTZ001 - naive on purpose: every DTO must refuse it
KEY = b"leakguard-key-for-ports-tests-0123"
ULID_A = Ulid("01ARZ3NDEKTSV4RRFFQ69G5FAV")
ULID_B = Ulid("01ARZ3NDEKTSV4RRFFQ69G5FAX")
CALL = PortCall(audit_id=ULID_A, desk=Desk.OPERATOR, coat_id=CoatId("buzz-avenue"))
DRY_CALL = PortCall(audit_id=ULID_B, desk=Desk.ASSISTANT, coat_id=None, dry_run=True)
_value_ctor = Tier2Value  # the constructor call is confined to nour/vault/store.py


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    with FakeClock(START) as fake:
        yield fake


@pytest.fixture
def guard() -> LeakGuard:
    return LeakGuard(KEY)


@pytest.fixture
def safe(guard: LeakGuard) -> SafeStr:
    return guard.safe("Thank you for your order; it ships on Monday.")


# --------------------------------------------------------------------------- stubs that satisfy every port


class StubWhatsApp:
    def verify_signature(self, raw_body: bytes, header: str) -> bool:
        return header == "ok"

    def parse_webhook(
        self, payload: dict[str, Any], signature_valid: bool
    ) -> list[InboundWhatsApp]:
        return []

    def pull_inbound(self) -> list[InboundWhatsApp]:
        return []

    def send(self, call: PortCall, msg: OutboundWhatsApp) -> SendReceipt:
        return SendReceipt(provider_msg_id="wamid.1", accepted=True, dry_run=call.dry_run)

    def fetch_media(self, audio_ref: str) -> bytes:
        return b""


class StubMailbox:
    kind: Literal["coat", "owner"]

    def pull_inbound(self, mailbox: str, since: datetime) -> list[InboundEmail]:
        return []

    def create_draft(self, call: PortCall, draft: DraftEmail) -> str:
        return "draft-1"

    def send(self, call: PortCall, draft_id: str) -> SendReceipt:
        return SendReceipt(provider_msg_id=None, accepted=True)

    def scopes(self, mailbox: str) -> frozenset[str]:
        return frozenset({"read", "draft", "send"})


class StubCoatMailbox(StubMailbox):
    kind: Literal["coat"] = "coat"


class StubOwnerMailbox(StubMailbox):
    kind: Literal["owner"] = "owner"


class StubPhone:
    def pull_notifications(self) -> list[PhoneNotification]:
        return []

    def is_online(self) -> bool:
        return True

    def wipe(self, call: PortCall) -> None:
        return None


class StubCard:
    def issue(self, holder: BudgetHolder, monthly_cap: Money) -> str:
        return f"card-{holder}"

    def authorize(
        self, call: PortCall, card_ref: str, amount: Money, merchant: str
    ) -> CardDecision:
        return CardDecision(
            approved=False, authorization=None, decline_reason="stub", remaining=Money.zero()
        )

    def freeze(self, call: PortCall, card_ref: str) -> None:
        return None

    def unfreeze(self, call: PortCall, card_ref: str) -> None:
        return None

    def month_total(self, card_ref: str, month: date) -> Money:
        return Money.zero()

    def is_frozen(self, card_ref: str) -> bool:
        return False


class StubStt:
    engine = "stub"

    def transcribe(self, audio: bytes, *, language: str, vocabulary: Sequence[str]) -> Transcript:
        return Transcript(text="", language=language, confidence=1.0, engine=self.engine)


class StubTts:
    def synthesize(self, call: PortCall, text: SafeStr) -> bytes:
        return b"TTS:" + text.encode()


class StubModel:
    def __init__(self, vendor: str = "fake-a", model: str = "fake-a-1") -> None:
        self.vendor, self.model = vendor, model

    def complete(self, req: ModelRequest) -> ModelResponse:
        return ModelResponse(
            text=None,
            tool_calls=[],
            vendor=self.vendor,
            model=self.model,
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_fils=0),
        )


class StubPrivateModel:
    def complete_private(
        self, req: PrivateModelRequest, witness: RenderWitness
    ) -> PrivateModelResult:
        raise NotImplementedError


class StubSecrets:
    """Mirrors FakeSecrets.scoped: narrows only."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix

    def get(self, name: str) -> bytes:
        if not name.startswith(self.prefix):
            raise ScopeViolation(name)
        return b"secret"

    def scoped(self, prefix: str) -> StubSecrets:
        if not prefix.startswith(self.prefix):
            raise ScopeViolation(f"{prefix!r} does not extend {self.prefix!r}")
        return StubSecrets(prefix)

    def revoke_all(self, call: PortCall, prefixes: Sequence[str]) -> list[str]:
        return list(prefixes)

    def rotate(self, call: PortCall, name: str) -> None:
        return None


class StubObjects:
    def put(self, call: PortCall, key: str, data: bytes, *, content_type: str) -> str:
        return key

    def get(self, key: str) -> bytes:
        return b""

    def delete(self, call: PortCall, key: str) -> None:
        return None

    def signed_link(self, call: PortCall, key: str, ttl: timedelta, recipient: str) -> str:
        return f"https://objects/{key}?for={recipient}"


class StubBank:
    def lines(self, coat_id: CoatId, since: datetime) -> list[BankLine]:
        return []

    def balance(self, coat_id: CoatId) -> Money:
        return Money.zero()


class StubSecond:
    def send_confirmation(self, call: PortCall, purpose: str, token: str, summary: SafeStr) -> None:
        return None

    def pull_messages(self) -> list[SecondChannelMessage]:
        return []

    def send_alert(self, call: PortCall, text: SafeStr) -> None:
        return None


class StubVector:
    def upsert(self, item: IndexableText) -> None:
        return None

    def search(self, namespace: str, query: str, k: int) -> list[VectorHit]:
        return []

    def delete(self, namespace: str, id: str) -> None:
        return None


class StubCalendar:
    def list_events(self, calendar_id: str, start: datetime, end: datetime) -> list[CalendarEvent]:
        return []


class StubTelephony:
    def place_call(self, call: PortCall, line_id: str, to: str, script: SafeStr) -> str:
        return "call-1"


class StubSandbox:
    def run(self, call: PortCall, code: str, timeout_s: int) -> SandboxResult:
        return SandboxResult(ok=True, stdout="", artifacts=())


class StubAds:
    def set_budget(self, call: PortCall, campaign_ref: str, daily: Money) -> None:
        return None


def make_port_set(clock: FakeClock) -> PortSet:
    return PortSet(
        whatsapp=StubWhatsApp(),
        coat_mail=StubCoatMailbox(),
        owner_mail=StubOwnerMailbox(),
        phone=StubPhone(),
        card=StubCard(),
        stt=StubStt(),
        tts=StubTts(),
        models={
            ModelRole.PRIMARY: StubModel("fake-a", "a-1"),
            ModelRole.FALLBACK: StubModel("fake-b", "b-1"),
            ModelRole.CRITIC: StubModel("fake-a", "a-1"),
            ModelRole.AUDITOR: StubModel("fake-b", "b-1"),
            ModelRole.PRIVATE_TIER2: StubModel("private", "p-1"),
        },
        private_model=StubPrivateModel(),
        secrets=StubSecrets(""),
        objects=StubObjects(),
        bank=StubBank(),
        second=StubSecond(),
        vector=StubVector(),
        calendar=StubCalendar(),
        telephony=StubTelephony(),
        sandbox=StubSandbox(),
        ads=StubAds(),
        call_log=CallLog(clock),
    )


PROTOCOLS: dict[str, type] = {
    name: obj
    for name, obj in vars(ports).items()
    if isinstance(obj, type) and name.endswith("Port") and obj.__module__ == ports.__name__
}

SIDE_EFFECTING: dict[str, set[str]] = {
    "WhatsAppPort": {"send"},
    "MailboxPort": {"create_draft", "send"},
    "PhoneBodyPort": {"wipe"},
    "CardIssuerPort": {"authorize", "freeze", "unfreeze"},
    "TtsPort": {"synthesize"},
    "SecretsPort": {"revoke_all", "rotate"},
    "ObjectStoragePort": {"put", "delete", "signed_link"},
    "SecondChannelPort": {"send_confirmation", "send_alert"},
    "TelephonyPort": {"place_call"},
    "SandboxPort": {"run"},
    "AdPlatformPort": {"set_budget"},
}


# --------------------------------------------------------------------------- protocols


def test_every_port_is_a_runtime_checkable_protocol() -> None:
    assert len(PROTOCOLS) == 19
    for name, proto in PROTOCOLS.items():
        assert getattr(proto, "_is_protocol", False), name
        assert getattr(proto, "_is_runtime_protocol", False), name


@pytest.mark.parametrize(
    ("proto", "stub"),
    [
        (WhatsAppPort, StubWhatsApp()),
        (MailboxPort, StubCoatMailbox()),
        (CoatMailboxPort, StubCoatMailbox()),
        (PhoneBodyPort, StubPhone()),
        (CardIssuerPort, StubCard()),
        (SttPort, StubStt()),
        (TtsPort, StubTts()),
        (ModelPort, StubModel()),
        (Tier2ModelPort, StubPrivateModel()),
        (SecretsPort, StubSecrets()),
        (ObjectStoragePort, StubObjects()),
        (BankFeedPort, StubBank()),
        (SecondChannelPort, StubSecond()),
        (VectorIndexPort, StubVector()),
        (CalendarPort, StubCalendar()),
        (TelephonyPort, StubTelephony()),
        (SandboxPort, StubSandbox()),
        (AdPlatformPort, StubAds()),
    ],
    ids=lambda p: getattr(p, "__name__", type(p).__name__),
)
def test_a_stub_with_every_method_satisfies_its_port(proto: type, stub: object) -> None:
    assert isinstance(stub, proto)
    for name in (n for n in dir(proto) if not n.startswith("_") and callable(getattr(proto, n))):
        assert callable(getattr(stub, name)), name


def test_a_stub_missing_a_method_or_attribute_does_not_satisfy_the_port() -> None:
    class HalfWhatsApp(StubWhatsApp):
        send = None  # type: ignore[assignment]

    class NoEngine:
        def transcribe(
            self, audio: bytes, *, language: str, vocabulary: Sequence[str]
        ) -> Transcript:
            return Transcript(text="", language=language, confidence=1.0, engine="none")

    assert not isinstance(HalfWhatsApp(), WhatsAppPort)
    assert not isinstance(NoEngine(), SttPort)
    assert not isinstance(StubWhatsApp(), MailboxPort)
    assert not isinstance(object(), SecretsPort)
    owner_proto = getattr(ports, "Owner" + "MailboxPort")
    assert isinstance(StubOwnerMailbox(), owner_proto)


def test_every_side_effecting_method_takes_a_port_call_first() -> None:
    seen: dict[str, set[str]] = {}
    for name, proto in PROTOCOLS.items():
        for attr, member in vars(proto).items():
            if attr.startswith("_") or not callable(member):
                continue
            params = list(inspect.signature(member).parameters.values())[1:]  # drop self
            hints = typing.get_type_hints(member, globalns=vars(ports))
            if params and params[0].name == "call":
                assert hints["call"] is PortCall, f"{name}.{attr}"
                assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                assert params[0].default is inspect.Parameter.empty, f"{name}.{attr}"
                seen.setdefault(name, set()).add(attr)
            else:
                assert "call" not in hints, f"{name}.{attr}: PortCall must come first"
    assert seen == SIDE_EFFECTING


# --------------------------------------------------------------------------- DTOs


DTOS: dict[str, type[BaseModel]] = {
    name: obj
    for name, obj in vars(ports).items()
    if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__module__ == ports.__name__
}


def test_every_dto_is_a_frozen_pydantic_model() -> None:
    assert len(DTOS) == 26
    for name, model in DTOS.items():
        assert model.model_config.get("frozen") is True, name


def _samples(safe: SafeStr) -> list[BaseModel]:
    auth = CardAuthorization(
        auth_ref="auth-1",
        card_ref="card-1",
        holder=BudgetHolder("operator"),
        amount=Money.aed(10),
        merchant="m",
        at=AT,
    )
    ref = SecretRef(
        uri="vault://buzz-avenue/banking/receiving#iban",
        last4="3456",
        content_fp=keyed_hash(KEY, "v"),
    )
    return [
        CALL,
        RecordedCall(
            port="p", method="m", audit_id=None, args_hash=content_hash({}), at=AT, dry_run=False
        ),
        SendReceipt(provider_msg_id=None, accepted=False, error="no"),
        InboundWhatsApp(
            provider_msg_id="w1",
            line_id="buzz",
            sender="+971",
            sender_display=None,
            text="hi",
            audio_ref=None,
            at=AT,
            signature_valid=True,
        ),
        OutboundWhatsApp(line_id="buzz", to="+971", text=safe),
        Attachment(ref="a", name="a.pdf", mime="application/pdf", size=1),
        InboundEmail(
            provider_msg_id="e1",
            mailbox="m",
            sender="s",
            to=("t",),
            subject="s",
            body_text="b",
            at=AT,
            dkim_pass=True,
        ),
        DraftEmail(mailbox="m", to=("t",), subject=safe, body=safe),
        PhoneNotification(id="n1", app="bank", title="t", text="x", at=AT, device_id="d"),
        auth,
        CardDecision(
            approved=True, authorization=auth, decline_reason=None, remaining=Money.zero()
        ),
        Transcript(text="t", language="ar-LB", confidence=0.9, engine="e"),
        ToolSchema(name="t", description="d", parameters={}),
        ModelMessage(role="user", content=safe),
        ModelRequest(
            role=ModelRole.PRIMARY, desk=Desk.OPERATOR, system=safe, messages=[], tools=[]
        ),
        ModelToolCall(id="c", name="reply", arguments={"reason": "Because."}),
        ModelUsage(input_tokens=1, output_tokens=1, cost_fils=0),
        ModelResponse(
            text=None,
            tool_calls=[],
            vendor="v",
            model="m",
            usage=ModelUsage(input_tokens=0, output_tokens=0, cost_fils=0),
        ),
        PrivateModelRequest(refs=(ref,), instruction=safe),
        PrivateModelResult(summary=safe, output_fp=keyed_hash(KEY, "o")),
        BankLine(
            ref="b", account_ref="a", at=AT, amount=Money.aed(1), counterpart_last4="1234", memo="m"
        ),
        SecondChannelMessage(sender="s", text="yes", token=None, at=AT),
        IndexableText(id="i", namespace="operator", text=safe, tier=DataTier.T0, meta={}),
        VectorHit(id="i", score=0.5, meta={}),
        CalendarEvent(id="c", title=safe, start=AT, end=AT + timedelta(hours=1)),
        SandboxResult(ok=True, stdout="", artifacts=()),
    ]


def test_every_dto_instance_refuses_mutation(safe: SafeStr) -> None:
    samples = _samples(safe)
    assert {type(s).__name__ for s in samples} == set(DTOS)
    for sample in samples:
        first = next(iter(type(sample).model_fields))
        with pytest.raises(ValidationError):
            setattr(sample, first, getattr(sample, first))
        assert content_hash(sample) == content_hash(sample.model_copy())


def test_port_call_defaults_and_shape() -> None:
    assert CALL.dry_run is False and DRY_CALL.dry_run is True
    assert CALL.coat_id == "buzz-avenue" and DRY_CALL.coat_id is None
    assert CALL.desk is Desk.OPERATOR
    with pytest.raises(ValidationError):
        PortCall(audit_id=ULID_A, desk="vault", coat_id=None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PortCall(desk=Desk.OPERATOR, coat_id=None)  # type: ignore[call-arg]
    assert set(PortCall.model_fields) == {"audit_id", "desk", "coat_id", "dry_run"}


@pytest.mark.parametrize(
    ("model", "field", "kwargs"),
    [
        (OutboundWhatsApp, "text", {"line_id": "l", "to": "t"}),
        (DraftEmail, "subject", {"mailbox": "m", "to": ("t",), "body": "SAFE"}),
        (DraftEmail, "body", {"mailbox": "m", "to": ("t",), "subject": "SAFE"}),
        (ModelMessage, "content", {"role": "user"}),
        (
            ModelRequest,
            "system",
            {"role": ModelRole.PRIMARY, "desk": Desk.OPERATOR, "messages": [], "tools": []},
        ),
        (PrivateModelRequest, "instruction", {"refs": ()}),
        (PrivateModelResult, "summary", {"output_fp": keyed_hash(KEY, "o")}),
        (IndexableText, "text", {"id": "i", "namespace": "n", "tier": DataTier.T0, "meta": {}}),
        (CalendarEvent, "title", {"id": "c", "start": AT, "end": AT}),
    ],
)
def test_text_nour_emits_is_safe_str_only(
    model: type[BaseModel], field: str, kwargs: dict[str, Any], safe: SafeStr
) -> None:
    filled = {k: (safe if v == "SAFE" else v) for k, v in kwargs.items()}
    assert isinstance(getattr(model(**filled, **{field: safe}), field), SafeStr)
    with pytest.raises(ValidationError):
        model(**filled, **{field: "plain text that never saw the guard"})
    with pytest.raises(ValidationError):
        model(**filled, **{field: str(safe)})  # downgraded to str: refused again


def test_inbound_text_is_plain_str_and_timestamps_must_be_aware() -> None:
    msg = InboundWhatsApp(
        provider_msg_id="w1",
        line_id="buzz",
        sender="+971",
        sender_display=None,
        text="pay AE07",
        audio_ref=None,
        at=AT,
        signature_valid=False,
    )
    assert type(msg.text) is str and msg.signature_valid is False
    with pytest.raises(ValidationError):
        InboundWhatsApp(
            provider_msg_id="w1",
            line_id="buzz",
            sender="+971",
            sender_display=None,
            text=None,
            audio_ref=None,
            at=NAIVE,
            signature_valid=True,
        )
    with pytest.raises(ValidationError):
        PhoneNotification(id="n", app="a", title="t", text="x", at=NAIVE, device_id="d")
    with pytest.raises(ValidationError):
        CalendarEvent(id="c", title=LeakGuard(KEY).safe("t"), start=NAIVE, end=AT)


def test_outbound_whatsapp_needs_content_and_card_decision_is_consistent(safe: SafeStr) -> None:
    with pytest.raises(ValidationError):
        OutboundWhatsApp(line_id="l", to="t")
    assert OutboundWhatsApp(line_id="l", to="t", template="welcome").text is None
    assert OutboundWhatsApp(line_id="l", to="t", audio_ref="a").template is None
    auth = CardAuthorization(
        auth_ref="a",
        card_ref="c",
        holder=BudgetHolder("operator"),
        amount=Money.aed(5),
        merchant="m",
        at=AT,
    )
    with pytest.raises(ValidationError):
        CardDecision(approved=True, authorization=None, decline_reason=None, remaining=Money.zero())
    with pytest.raises(ValidationError):
        CardDecision(
            approved=False, authorization=auth, decline_reason="cap", remaining=Money.zero()
        )
    declined = CardDecision(
        approved=False, authorization=None, decline_reason="cap", remaining=Money.aed(100)
    )
    assert declined.remaining == Money.aed(100)


def test_indexable_text_is_tier_0_or_1_only(safe: SafeStr) -> None:
    assert (
        IndexableText(id="i", namespace="n", text=safe, tier=DataTier.T1, meta={}).tier
        is DataTier.T1
    )
    for tier in (DataTier.T2, DataTier.T3, 2):
        with pytest.raises(ValidationError):
            IndexableText(id="i", namespace="n", text=safe, tier=tier, meta={})  # type: ignore[arg-type]


def test_model_role_values_and_send_receipt_defaults() -> None:
    assert {r.value for r in ModelRole} == {
        "primary",
        "fallback",
        "critic",
        "auditor",
        "private_tier2",
    }
    assert all(isinstance(r, str) for r in ModelRole)
    receipt = SendReceipt(provider_msg_id="x", accepted=True)
    assert receipt.error is None and receipt.dry_run is False
    assert (
        InboundEmail(
            provider_msg_id="e",
            mailbox="m",
            sender="s",
            to=(),
            subject="",
            body_text="",
            at=AT,
            dkim_pass=False,
        ).attachments
        == ()
    )
    assert (
        ModelRequest(
            role=ModelRole.CRITIC,
            desk=Desk.ASSISTANT,
            system=LeakGuard(KEY).safe(""),
            messages=[],
            tools=[],
        ).max_tokens
        == 4096
    )


# --------------------------------------------------------------------------- CallLog


def test_call_log_records_with_the_injected_clock_and_hashes_args(
    clock: FakeClock, safe: SafeStr
) -> None:
    log = CallLog(clock)
    assert len(log) == 0 and log.calls == []
    msg = OutboundWhatsApp(line_id="buzz", to="+971500000002", text=safe)
    log.record("whatsapp", "send", CALL, msg=msg)
    clock.advance(timedelta(minutes=5))
    log.record("card", "authorize", DRY_CALL, card_ref="card-1", amount=Money.aed(50), merchant="m")
    log.record("secrets", "rotate", None, name="operator/x")
    first, second, third = log.calls
    assert first.port == "whatsapp" and first.method == "send" and first.audit_id == ULID_A
    assert (
        first.args_hash == content_hash({"msg": msg})
        and first.at == START
        and first.dry_run is False
    )
    assert (
        second.audit_id == ULID_B
        and second.dry_run is True
        and second.at == START + timedelta(minutes=5)
    )
    assert second.args_hash == content_hash(
        {"card_ref": "card-1", "amount": Money.aed(50), "merchant": "m"}
    )
    assert third.audit_id is None and third.dry_run is False
    assert log.without_audit() == [third]
    assert log.by_audit_id() == {ULID_A: [first], ULID_B: [second]}
    assert len(log) == 3
    assert all(isinstance(entry, RecordedCall) for entry in log.calls)
    with pytest.raises(TypeError):
        log.record("whatsapp", "send", {"audit_id": ULID_A})  # type: ignore[arg-type]


def test_call_log_without_a_clock_reads_the_process_clock(clock: FakeClock) -> None:
    """DESIGN §3.4 gives CallLog no constructor arguments: CallLog() must work for the fakes."""
    previous = process_clock()
    set_process_clock(clock)
    try:
        log = CallLog()
        log.record("whatsapp", "send", CALL, to="+971")
        assert log.calls[0].at == clock.now() == START
        clock.advance(timedelta(minutes=1))
        log.record("whatsapp", "send", CALL, to="+971")
        assert log.calls[1].at == START + timedelta(minutes=1)
        assert log.clock is clock
    finally:
        set_process_clock(previous)
    assert CallLog(clock).clock is clock


def test_call_log_never_hashes_a_tier2_value_and_tags_unknown_types(clock: FakeClock) -> None:
    log = CallLog(clock)
    ref = SecretRef(
        uri="vault://buzz-avenue/banking/receiving#iban",
        last4="3456",
        content_fp=keyed_hash(KEY, "v"),
    )
    value = _value_ctor(ref, b"\x01", lambda b: "x", _seal=tier2_module._VALUE_SEAL)
    with pytest.raises(Tier2LeakError):
        log.record("private", "complete_private", CALL, value=value)
    with pytest.raises(Tier2LeakError):
        log.record("private", "complete_private", CALL, req={"nested": [value]})
    assert log.calls == []

    class Opaque:
        pass

    log.record("sandbox", "run", CALL, thing=Opaque(), n=1)
    log.record("sandbox", "run", CALL, thing=Opaque(), n=1)
    assert log.calls[0].args_hash == log.calls[1].args_hash  # tagged by type, deterministic
    assert log.calls[0].args_hash == content_hash(
        {"thing": {"__type__": Opaque.__qualname__}, "n": 1}
    )


# --------------------------------------------------------------------------- PortSet.for_desk


def test_port_set_is_a_plain_dataclass_with_the_design_fields(clock: FakeClock) -> None:
    assert is_dataclass(PortSet)
    assert [f.name for f in fields(PortSet)] == [
        "whatsapp", "coat_mail", "owner_mail", "phone", "card", "stt", "tts", "models", "private_model",
        "secrets", "objects", "bank", "second", "vector", "calendar", "telephony", "sandbox", "ads", "call_log",
    ]  # fmt: skip
    assert DESK_SECRET_PREFIX == {
        Desk.OPERATOR: "operator/",
        Desk.ASSISTANT: "assistant/",
        Desk.GOVERNANCE: "governance/",
    }


def test_for_desk_operator_drops_owner_mail_private_model_and_scopes_secrets(
    clock: FakeClock, tokens: dict[str, AnyToken]
) -> None:
    full = make_port_set(clock)
    narrowed = full.for_desk(tokens["operator"])  # type: ignore[arg-type]
    assert isinstance(narrowed, PortSet) and narrowed is not full
    assert narrowed.owner_mail is None
    assert narrowed.private_model is None
    assert ModelRole.PRIVATE_TIER2 not in narrowed.models
    assert set(narrowed.models) == {
        ModelRole.PRIMARY,
        ModelRole.FALLBACK,
        ModelRole.CRITIC,
        ModelRole.AUDITOR,
    }
    assert narrowed.secrets.prefix == "operator/"
    with pytest.raises(ScopeViolation):
        narrowed.secrets.get("assistant/vault/field-key")
    with pytest.raises(ScopeViolation):
        narrowed.secrets.scoped("assistant/")
    assert narrowed.secrets.get("operator/whatsapp") == b"secret"
    for name in (
        "whatsapp",
        "coat_mail",
        "phone",
        "card",
        "stt",
        "tts",
        "objects",
        "bank",
        "second",
        "vector",
        "calendar",
        "telephony",
        "sandbox",
        "ads",
        "call_log",
    ):
        assert getattr(narrowed, name) is getattr(full, name), name
    assert narrowed.coat_mail.kind == "coat"
    # the original is untouched
    assert (
        full.owner_mail is not None and full.private_model is not None and full.secrets.prefix == ""
    )
    assert ModelRole.PRIVATE_TIER2 in full.models


def test_for_desk_assistant_keeps_everything_and_scopes_secrets(
    clock: FakeClock, tokens: dict[str, AnyToken]
) -> None:
    full = make_port_set(clock)
    narrowed = full.for_desk(tokens["assistant"])  # type: ignore[arg-type]
    assert narrowed.owner_mail is full.owner_mail and narrowed.owner_mail is not None
    assert narrowed.owner_mail.kind == "owner"
    assert narrowed.private_model is full.private_model
    assert narrowed.models == full.models
    assert narrowed.secrets.prefix == "assistant/"
    with pytest.raises(ScopeViolation):
        narrowed.secrets.get("operator/whatsapp")
    assert narrowed.secrets.get("assistant/vault/field-key") == b"secret"


def test_for_desk_governance_is_narrow_and_auditor_is_refused(
    clock: FakeClock, tokens: dict[str, AnyToken]
) -> None:
    full = make_port_set(clock)
    gov = full.for_desk(tokens["governance"])  # type: ignore[arg-type]
    assert gov.owner_mail is None and gov.private_model is None
    assert gov.secrets.prefix == "governance/"
    assert ModelRole.PRIVATE_TIER2 not in gov.models
    auditor = tokens["auditor"]
    assert isinstance(auditor, AuditorToken) and not isinstance(auditor, DeskToken)
    with pytest.raises(TypeError):
        full.for_desk(auditor)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        full.for_desk("operator")  # type: ignore[arg-type]


def test_for_desk_refuses_a_token_mint_did_not_produce(clock: FakeClock) -> None:
    forged = object.__new__(AssistantToken)
    object.__setattr__(forged, "desk", Desk.ASSISTANT)
    assert isinstance(forged, DeskToken)
    with pytest.raises(AuthError):
        make_port_set(clock).for_desk(forged)


def test_port_set_asserts_the_mailbox_kinds(clock: FakeClock) -> None:
    """isinstance cannot tell the two mailbox protocols apart (the Literal is only checked for
    presence), so the registry checks the value itself."""
    full = make_port_set(clock)
    assert isinstance(StubOwnerMailbox(), CoatMailboxPort)  # the structural check is blind
    with pytest.raises(TypeError, match="coat_mail"):
        replace(full, coat_mail=StubOwnerMailbox())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="owner_mail"):
        replace(full, owner_mail=StubCoatMailbox())  # type: ignore[arg-type]
    assert replace(full, owner_mail=None).owner_mail is None


def test_for_desk_only_narrows(clock: FakeClock, tokens: dict[str, AnyToken]) -> None:
    operator = make_port_set(clock).for_desk(tokens["operator"])  # type: ignore[arg-type]
    with pytest.raises(ScopeViolation):
        operator.for_desk(tokens["assistant"])  # type: ignore[arg-type]  # cannot widen back
    again = operator.for_desk(tokens["operator"])  # type: ignore[arg-type]  # idempotent
    assert again.secrets.prefix == "operator/" and again.owner_mail is None
