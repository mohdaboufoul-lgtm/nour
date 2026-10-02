"""nour/fakes (DESIGN §3.9, §6, §7.3; SPEC §16 §18): one fake per port.

Proves (MODULES.md "fakes"): every fake satisfies its Protocol (runtime ``isinstance`` against
the ``runtime_checkable`` ports), records every side effect into the shared ``CallLog`` with the
``PortCall`` it was given, honours ``dry_run`` (no ``.sent`` entry, a ``.dry_run_sends`` entry,
``SendReceipt.dry_run``), ``fail_next`` raises exactly n times; ``FakeCardIssuer`` declines above
the cap and when frozen and ``month_total`` follows the clock; ``FakeSecrets.scoped`` narrows
only and ``revoke_all`` makes ``get`` raise; ``FakeSecondChannel.reply/kill`` produce
``SecondChannelMessage``s; per-sender counts; token usage; the second channel's address;
``default_fakes`` wiring from the real config; the passphrase is never stored; addresses are
compared canonically (E.164 numbers, lower-cased IDNA e-mail); the inbound mail queue is the
read cursor; protective effects (card freeze, secrets revocation) apply under dry run; a desk
port cannot revoke another desk's secrets; an unaudited call is recorded and refused.

Tokens come from the ``tokens`` fixture. The owner-mailbox protocol is reached through the
module namespace, as ``tests/unit/test_ports_shapes.py`` does.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from nour.config.schema import ModelEndpoint, NourConfig
from nour.core import ports
from nour.core.clock import DUBAI, FakeClock, IdGenerator
from nour.core.errors import (
    CurrencyMismatch,
    ModelUnavailable,
    Revoked,
    ScopeViolation,
)
from nour.core.hashing import content_hash
from nour.core.leakguard import LeakGuard, shape_hits
from nour.core.ports import (
    AdPlatformPort,
    Attachment,
    BankFeedPort,
    BankLine,
    CalendarEvent,
    CalendarPort,
    CallLog,
    CardAuthorization,
    CardIssuerPort,
    CoatMailboxPort,
    DraftEmail,
    IndexableText,
    MailboxPort,
    ModelMessage,
    ModelPort,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelUsage,
    ObjectStoragePort,
    OutboundWhatsApp,
    PhoneBodyPort,
    PortCall,
    PortSet,
    PrivateModelRequest,
    SandboxPort,
    SandboxResult,
    SecondChannelMessage,
    SecondChannelPort,
    SecretsPort,
    SttPort,
    TelephonyPort,
    Tier2ModelPort,
    Transcript,
    TtsPort,
    VectorIndexPort,
    WhatsAppPort,
)
from nour.core.tier2 import SecretRef
from nour.core.tokens import AnyToken
from nour.core.types import BudgetHolder, CoatId, DataTier, Desk, Money, SafeStr
from nour.fakes import (
    DEFAULT_OWNER_MAILBOXES,
    FAKE_VENDOR_A,
    FAKE_VENDOR_B,
    FAKE_VENDOR_PRIVATE,
    FakePort,
    FakeSet,
    default_fakes,
)
from nour.fakes.card import DeclineReason, FakeCardIssuer
from nour.fakes.mailbox import (
    ALL_SCOPES,
    FakeCoatMailbox,
    FakeMailbox,
    FakeOwnerMailbox,
    canonical_address,
)
from nour.fakes.model import NO_USAGE, FakePrivateModel, ScriptedModel, usage_estimate
from nour.fakes.policies import SanePolicy, response, tool_call
from nour.fakes.reserved import FakeAds, FakeCalendar, FakeSandbox, FakeTelephony
from nour.fakes.second import FakeSecondChannel
from nour.fakes.secrets import DEFAULT_SECRET_NAMES, FakeSecrets, seed_value
from nour.fakes.stt import AUDIO_MARKER, FakeStt, FakeTts
from nour.fakes.vector import FakeVectorIndex
from nour.fakes.whatsapp import OWNER_LINE, FakeWhatsApp, canonical_number, same_number

OWNER = "+971500000001"
STRANGER = "+971500000002"
PASSPHRASE = "correct-horse-battery"
COAT = CoatId("buzz-avenue")
OPERATOR = BudgetHolder("operator")
START = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)


# --------------------------------------------------------------------------- helpers and fixtures


@pytest.fixture
def fakes(clock: FakeClock, cfg: NourConfig, leakguard: LeakGuard) -> FakeSet:
    return default_fakes(clock, cfg, owner_number=OWNER, passphrase=PASSPHRASE, guard=leakguard)


@pytest.fixture
def call(idgen: IdGenerator) -> PortCall:
    return PortCall(audit_id=idgen.new(), desk=Desk.OPERATOR, coat_id=COAT)


@pytest.fixture
def dry(idgen: IdGenerator) -> PortCall:
    return PortCall(audit_id=idgen.new(), desk=Desk.OPERATOR, coat_id=COAT, dry_run=True)


@pytest.fixture
def safe(leakguard: LeakGuard) -> SafeStr:
    return leakguard.safe("Thank you for your order; it ships on Monday.")


def _last(log: CallLog) -> ports.RecordedCall:
    assert log.calls, "nothing was recorded"
    return log.calls[-1]


def _strings_in(obj: Any, depth: int = 0) -> Iterator[str]:
    """Every str (and decoded bytes) reachable from ``obj`` through containers, models and
    object ``__dict__``s: what a leak scan of a fake sees."""
    if depth > 8:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, bytes | bytearray):
        yield bytes(obj).decode("utf-8", "replace")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from _strings_in(key, depth + 1)
            yield from _strings_in(value, depth + 1)
    elif isinstance(obj, list | tuple | set | frozenset):
        for item in obj:
            yield from _strings_in(item, depth + 1)
    elif isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from _strings_in(getattr(obj, name), depth + 1)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for field in dataclasses.fields(obj):
            yield from _strings_in(getattr(obj, field.name), depth + 1)
    elif hasattr(obj, "__dict__") and not callable(obj):
        yield from _strings_in(vars(obj), depth + 1)


# --------------------------------------------------------------------------- protocols


def _port_pairs(fakes: FakeSet) -> list[tuple[type, object]]:
    owner_proto = getattr(ports, "Owner" + "MailboxPort")
    pairs: list[tuple[type, object]] = [
        (WhatsAppPort, fakes.whatsapp),
        (MailboxPort, fakes.coat_mail),
        (CoatMailboxPort, fakes.coat_mail),
        (MailboxPort, fakes.owner_mail),
        (owner_proto, fakes.owner_mail),
        (PhoneBodyPort, fakes.phone),
        (CardIssuerPort, fakes.card),
        (SttPort, fakes.stt),
        (TtsPort, fakes.tts),
        (Tier2ModelPort, fakes.private_model),
        (SecretsPort, fakes.secrets),
        (ObjectStoragePort, fakes.objects),
        (BankFeedPort, fakes.bank),
        (SecondChannelPort, fakes.second),
        (VectorIndexPort, fakes.vector),
        (CalendarPort, fakes.calendar),
        (TelephonyPort, fakes.telephony),
        (SandboxPort, fakes.sandbox),
        (AdPlatformPort, fakes.ads),
    ]
    pairs.extend((ModelPort, model) for model in fakes.models.values())
    return pairs


def test_every_fake_satisfies_its_runtime_checkable_port(fakes: FakeSet) -> None:
    pairs = _port_pairs(fakes)
    assert len(pairs) == 19 + len(ModelRole)
    for proto, fake in pairs:
        assert isinstance(fake, proto), (proto.__name__, type(fake).__name__)
        for name in (
            n for n in dir(proto) if not n.startswith("_") and callable(getattr(proto, n))
        ):
            assert callable(getattr(fake, name)), f"{type(fake).__name__}.{name}"
    assert fakes.coat_mail.kind == "coat" and fakes.owner_mail.kind == "owner"
    assert isinstance(fakes, PortSet)


def test_every_fake_is_a_fake_port_sharing_one_call_log_and_clock(
    fakes: FakeSet, clock: FakeClock
) -> None:
    members = fakes.fakes()
    assert set(members) >= {"whatsapp", "coat_mail", "owner_mail", "card", "secrets", "second"}
    assert {f"models[{role.value}]" for role in ModelRole} <= set(members)
    for name, fake in members.items():
        assert isinstance(fake, FakePort), name
        assert fake.call_log is fakes.call_log, name
        assert fake.clock is clock, name
        assert fake.failures_pending == 0
        fake.fail_next(0)
        with pytest.raises(ValueError):
            fake.fail_next(-1)
        with pytest.raises(TypeError):
            fake.fail_next(1, "RuntimeError")  # type: ignore[arg-type]
    assert fakes.call_log.calls == []


# --------------------------------------------------------------------------- default_fakes wiring


def test_default_fakes_issues_cards_from_the_real_caps(fakes: FakeSet, cfg: NourConfig) -> None:
    cap = cfg.spend_tiers.monthly_cap[OPERATOR]
    assert cap is not None
    card_ref = fakes.card.card_for(OPERATOR)
    assert fakes.card.caps[card_ref] == cap and fakes.card.is_frozen(card_ref) is False
    for holder, holder_cap in cfg.spend_tiers.monthly_cap.items():
        if holder_cap is None:
            with pytest.raises(KeyError):
                fakes.card.card_for(holder)


def test_default_fakes_registers_lines_mailboxes_secrets_and_models(
    fakes: FakeSet, cfg: NourConfig
) -> None:
    assert OWNER_LINE in fakes.whatsapp.lines
    for coat in cfg.coats.values():
        assert fakes.whatsapp.lines[str(coat.slug)] == coat.identity.whatsapp_line
        assert coat.identity.email in fakes.coat_mail.mailboxes
    assert fakes.whatsapp.owner_number == OWNER
    assert fakes.owner_mail.mailboxes == DEFAULT_OWNER_MAILBOXES
    assert fakes.secrets.prefix == ""
    for name in DEFAULT_SECRET_NAMES:
        assert fakes.secrets.get(name) == seed_value(name)
    prefixes = {name.split("/", 1)[0] + "/" for name in DEFAULT_SECRET_NAMES}
    assert prefixes == {"governance/", "operator/", "assistant/", "auditor/"}
    primary, fallback = fakes.models[ModelRole.PRIMARY], fakes.models[ModelRole.FALLBACK]
    assert primary.vendor == FAKE_VENDOR_A and fallback.vendor == FAKE_VENDOR_B
    assert fakes.models[ModelRole.CRITIC].vendor == FAKE_VENDOR_A
    assert fakes.models[ModelRole.AUDITOR].vendor == FAKE_VENDOR_B
    assert fakes.models[ModelRole.AUDITOR].vendor != primary.vendor != fallback.vendor
    assert primary.model == cfg.models.primary.model
    assert ModelRole.PRIVATE_TIER2 in fakes.models
    for vendor in (FAKE_VENDOR_A, FAKE_VENDOR_B, FAKE_VENDOR_PRIVATE):
        assert ModelEndpoint(vendor=vendor, model="x").vendor == vendor  # the config validator
    assert fakes.second.kill_phrases == tuple(sorted(cfg.constitution.kill_phrases()))
    assert set(fakes.second.kill_phrases) == set(cfg.constitution.kill_phrases())


def test_second_channel_is_never_a_mailbox_nour_reads(
    fakes: FakeSet, clock: FakeClock, cfg: NourConfig
) -> None:
    read_by_nour = {*fakes.coat_mail.mailboxes, *fakes.owner_mail.mailboxes}
    assert fakes.second.address not in read_by_nour
    for address in read_by_nour:
        with pytest.raises(ValueError, match="second channel"):
            default_fakes(
                clock,
                cfg,
                owner_number=OWNER,
                passphrase=PASSPHRASE,
                second_channel_address=address,
            )


def test_default_fakes_never_stores_the_passphrase(fakes: FakeSet, leakguard: LeakGuard) -> None:
    assert "passphrase" in leakguard.labels()
    assert [hit.label for hit in leakguard.scan(f"x {PASSPHRASE} y")] == ["passphrase"]
    for name, fake in fakes.fakes().items():
        for text in _strings_in(fake):
            assert PASSPHRASE not in text, name
    for name in fakes.secrets.names():
        assert PASSPHRASE.encode() not in fakes.secrets.get(name)


def test_default_fakes_builds_its_own_guard_when_none_is_given(
    clock: FakeClock, cfg: NourConfig
) -> None:
    built = default_fakes(clock, cfg, owner_number=OWNER, passphrase=PASSPHRASE)
    assert "passphrase" in built.private_model.guard.labels()
    with pytest.raises(ValueError):
        default_fakes(clock, cfg, owner_number="", passphrase=PASSPHRASE)
    with pytest.raises(ValueError):
        default_fakes(clock, cfg, owner_number=OWNER, passphrase="  ")


def test_for_desk_narrows_the_fake_set_like_any_port_set(
    fakes: FakeSet, tokens: dict[str, AnyToken]
) -> None:
    operator = fakes.for_desk(tokens["operator"])  # type: ignore[arg-type]
    assert operator.owner_mail is None and operator.private_model is None
    assert ModelRole.PRIVATE_TIER2 not in operator.models
    assert operator.secrets.prefix == "operator/"
    assert operator.secrets.get("operator/stamp-key") == seed_value("operator/stamp-key")
    with pytest.raises(ScopeViolation):
        operator.secrets.get("assistant/vault/field-key")
    with pytest.raises(ScopeViolation):
        operator.secrets.scoped("assistant/")
    assert operator.whatsapp is fakes.whatsapp and operator.card is fakes.card
    assert isinstance(operator, FakeSet)
    narrowed = operator.fakes()
    assert "owner_mail" not in narrowed and "private_model" not in narrowed
    assert f"models[{ModelRole.PRIVATE_TIER2.value}]" not in narrowed
    assert all(isinstance(member, FakePort) for member in narrowed.values())
    for member in narrowed.values():
        member.fail_next(0)  # never a None in the map
    assistant = fakes.for_desk(tokens["assistant"])  # type: ignore[arg-type]
    assert assistant.owner_mail is fakes.owner_mail
    assert assistant.secrets.get("assistant/vault/field-key") == seed_value(
        "assistant/vault/field-key"
    )
    assert fakes.owner_mail is not None and fakes.secrets.prefix == ""


# --------------------------------------------------------------------------- WhatsApp


def test_whatsapp_deliver_queues_an_inbound_message_from_the_clock(
    fakes: FakeSet, clock: FakeClock
) -> None:
    wa = fakes.whatsapp
    msg = wa.deliver(COAT, STRANGER, "Does it come in blue?", sender_display="Sheena")
    assert msg.line_id == COAT and msg.sender == STRANGER and msg.text == "Does it come in blue?"
    assert msg.at == clock.now() and msg.signature_valid is True and msg.audio_ref is None
    assert msg.provider_msg_id.startswith("wamid.") and msg.sender_display == "Sheena"
    assert wa.pending == 1 and wa.pull_inbound() == [msg] and wa.pull_inbound() == []
    assert wa.delivered == [msg]
    owner = wa.deliver(OWNER_LINE, None, "مرحبا")
    assert owner.sender == OWNER and owner.line_id == OWNER_LINE
    replayed = wa.deliver(COAT, STRANGER, "again", provider_msg_id=msg.provider_msg_id)
    assert replayed.provider_msg_id == msg.provider_msg_id  # the bus dedups, not the provider
    spaced = wa.deliver(COAT, "+971 50 000 0002", "spelled with spaces")
    assert spaced.sender == STRANGER  # canonical E.164, exactly as parse_webhook builds it
    assert wa.deliver(COAT, "971-50-000-0002", "x").sender == STRANGER
    assert canonical_number("00971 50 000 0002") == "+00971500000002"
    assert same_number("+971 50 000 0002", STRANGER) and not same_number("abc", "def")
    with pytest.raises(ValueError):
        wa.deliver(COAT, "no digits here", "x")
    with pytest.raises(KeyError):
        wa.deliver("no-such-line", STRANGER, "x")
    with pytest.raises(ValueError):
        wa.deliver(COAT, STRANGER)
    assert wa.call_log.calls == []  # inbound is not a side effect of Nour's


def test_whatsapp_webhook_payload_round_trips_and_is_signed_over_the_raw_bytes(
    fakes: FakeSet,
) -> None:
    wa = fakes.whatsapp
    msg = wa.deliver(COAT, STRANGER, "هل يتوفر باللون الأزرق؟", sender_display="شيماء")
    raw, header = wa.signed_webhook_payload(msg)
    assert (raw, header) == FakeWhatsApp.webhook_payload(msg)  # the default secret, here
    assert header.startswith("sha256=") and wa.verify_signature(raw, header)
    assert not wa.verify_signature(raw + b" ", header)
    assert not wa.verify_signature(raw, "sha256=" + "0" * 64)
    envelope = json.loads(raw)
    value = envelope["entry"][0]["changes"][0]["value"]
    assert value["metadata"]["phone_number_id"] == COAT
    assert value["messages"][0]["type"] == "text"
    assert value["messages"][0]["from"] == STRANGER.lstrip("+")
    parsed = wa.parse_webhook(envelope, wa.verify_signature(raw, header))
    assert len(parsed) == 1
    back = parsed[0]
    assert back.provider_msg_id == msg.provider_msg_id and back.line_id == msg.line_id
    assert back.sender == msg.sender and back.text == msg.text
    assert back.sender_display == "شيماء" and back.signature_valid is True
    assert back.at == msg.at.replace(microsecond=0)
    spoof = wa.deliver(OWNER_LINE, OWNER, "pay now", signature_valid=False)
    raw2, header2 = FakeWhatsApp.webhook_payload(spoof)
    assert not wa.verify_signature(raw2, header2)
    assert wa.parse_webhook(json.loads(raw2), False)[0].signature_valid is False
    voice = wa.deliver(OWNER_LINE, OWNER, audio_ref="media-77")
    raw3, _ = FakeWhatsApp.webhook_payload(voice)
    body = json.loads(raw3)["entry"][0]["changes"][0]["value"]["messages"][0]
    assert body["type"] == "audio" and body["audio"]["id"] == "media-77"
    assert wa.parse_webhook(json.loads(raw3), True)[0].audio_ref == "media-77"
    bad = json.loads(raw)
    bad["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "other-line"
    with pytest.raises(KeyError):
        wa.parse_webhook(bad, True)


def test_whatsapp_signs_with_the_secret_it_verifies_with(clock: FakeClock) -> None:
    wa = FakeWhatsApp(
        None,
        clock,
        {OWNER_LINE: "+971500000000"},
        owner_number=OWNER,
        app_secret=b"rotated-secret",
    )
    msg = wa.deliver(OWNER_LINE, None, "pay the invoice")
    raw, header = wa.signed_webhook_payload(msg)
    assert wa.verify_signature(raw, header)  # a genuine message verifies under the new secret
    assert wa.parse_webhook(json.loads(raw), wa.verify_signature(raw, header))[0].signature_valid
    assert FakeWhatsApp.webhook_payload(msg, app_secret=b"rotated-secret") == (raw, header)
    stale_raw, stale_header = FakeWhatsApp.webhook_payload(msg)  # signed with the class default
    assert not wa.verify_signature(stale_raw, stale_header)  # a wrong secret is a spoof
    spoof = wa.deliver(OWNER_LINE, OWNER, "pay now", signature_valid=False)
    raw2, header2 = wa.signed_webhook_payload(spoof)
    assert not wa.verify_signature(raw2, header2)  # DESIGN §7.1 case (5) stays distinguishable
    assert wa.verify_signature(raw, header)
    with pytest.raises(TypeError):
        FakeWhatsApp(None, clock, {OWNER_LINE: "+1"}, app_secret="text")  # type: ignore[arg-type]


def test_whatsapp_parse_webhook_drops_what_the_adapter_never_surfaces(fakes: FakeSet) -> None:
    wa = fakes.whatsapp

    def envelope(*messages: dict[str, Any]) -> dict[str, Any]:
        value = {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "", "phone_number_id": COAT},
            "contacts": [{"profile": {"name": "Sheena"}, "wa_id": STRANGER.lstrip("+")}],
            "messages": list(messages),
        }
        return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}]}]}

    no_from = {"id": "wamid.1", "type": "image", "timestamp": "0", "image": {"id": "m1"}}
    captioned = {
        "from": STRANGER.lstrip("+"),
        "id": "wamid.2",
        "type": "image",
        "timestamp": "0",
        "image": {"id": "m2", "caption": "my receipt"},
    }
    mute_image = {
        "from": STRANGER.lstrip("+"),
        "id": "wamid.3",
        "type": "image",
        "timestamp": "0",
        "image": {"id": "m3"},
    }
    reaction = {
        "from": STRANGER.lstrip("+"),
        "id": "wamid.4",
        "type": "reaction",
        "timestamp": "0",
        "reaction": {"message_id": "wamid.2", "emoji": "👍"},
    }
    unsupported = {
        "from": STRANGER.lstrip("+"),
        "id": "wamid.5",
        "type": "unsupported",
        "timestamp": "0",
        "errors": [{"type": "unsupported"}],
    }
    text = {
        "from": "971 50 000 0002",
        "id": "wamid.6",
        "type": "text",
        "timestamp": "0",
        "text": {"body": "hi"},
    }
    parsed = wa.parse_webhook(
        envelope(no_from, captioned, mute_image, reaction, unsupported, text), True
    )
    assert [m.provider_msg_id for m in parsed] == ["wamid.2", "wamid.6"]
    assert parsed[0].text == "my receipt" and parsed[0].audio_ref is None
    assert parsed[0].sender == STRANGER and parsed[0].sender_display == "Sheena"
    assert parsed[1].sender == STRANGER and parsed[1].text == "hi"  # canonical, spaces or not
    assert all(m.sender != "+" for m in parsed)


def test_whatsapp_send_records_counts_and_dry_runs(
    fakes: FakeSet, call: PortCall, dry: PortCall, safe: SafeStr, clock: FakeClock
) -> None:
    wa = fakes.whatsapp
    msg = OutboundWhatsApp(line_id=COAT, to=STRANGER, text=safe)
    receipt = wa.send(call, msg)
    assert receipt.accepted and receipt.provider_msg_id is not None and not receipt.dry_run
    assert receipt.provider_msg_id.startswith("wamid.")
    assert wa.sent == [msg] and wa.dry_run_sends == []
    entry = _last(wa.call_log)
    assert (entry.port, entry.method, entry.audit_id) == ("whatsapp", "send", call.audit_id)
    assert entry.args_hash == content_hash({"msg": msg}) and entry.dry_run is False
    assert entry.at == clock.now()
    assert wa.daily_count(COAT) == 1 and wa.daily_count(OWNER_LINE) == 0
    dry_receipt = wa.send(dry, msg)
    assert dry_receipt.dry_run and dry_receipt.accepted and dry_receipt.provider_msg_id is None
    assert wa.sent == [msg] and wa.dry_run_sends == [msg]
    assert _last(wa.call_log).dry_run is True and _last(wa.call_log).audit_id == dry.audit_id
    assert wa.daily_count(COAT) == 1
    assert wa.sent_to(STRANGER) == [msg]
    today = clock.today_dubai()
    clock.advance(timedelta(days=1))
    assert wa.daily_count(COAT) == 0 and wa.daily_count(COAT, today) == 1
    with pytest.raises(KeyError):
        wa.send(call, OutboundWhatsApp(line_id="nope", to=STRANGER, text=safe))
    with pytest.raises(TypeError):
        wa.send(call, "text")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        wa.send({"audit_id": "x"}, msg)  # type: ignore[arg-type]


def test_whatsapp_fail_next_raises_exactly_n_times_and_refuse_next_returns_errors(
    fakes: FakeSet, call: PortCall, safe: SafeStr
) -> None:
    wa = fakes.whatsapp
    msg = OutboundWhatsApp(line_id=COAT, to=STRANGER, text=safe)
    wa.fail_next(2, ConnectionError)
    assert wa.failures_pending == 2
    for _ in range(2):
        with pytest.raises(ConnectionError, match="scripted failure"):
            wa.send(call, msg)
    assert wa.send(call, msg).accepted
    assert len(wa.call_log.calls) == 3 and wa.sent == [msg]  # failed attempts are logged too
    wa.refuse_next(1)
    refused = wa.send(call, msg)
    assert refused.accepted is False and refused.error is not None and "131047" in refused.error
    assert refused.provider_msg_id is None and wa.sent == [msg]
    assert wa.send(call, msg).accepted and wa.daily_count(COAT) == 2


def test_whatsapp_counts_inbound_per_sender_for_ingress_rate_limits(
    fakes: FakeSet, clock: FakeClock
) -> None:
    wa = fakes.whatsapp
    for i in range(3):
        wa.deliver(COAT, STRANGER, f"hi {i}")
    wa.deliver(OWNER_LINE, STRANGER, "wrong line")
    wa.deliver(COAT, "+971500000003", "other")
    assert wa.inbound_count(STRANGER) == 4
    assert wa.inbound_count(STRANGER, line_id=COAT) == 3
    for spelling in ("+971 50 000 0002", "971500000002", "+971-50-000-0002"):
        wa.deliver(COAT, spelling, "same number, another spelling")
    assert wa.inbound_count(STRANGER) == 7  # one sender, however the number is written
    assert wa.inbound_count("971 50 000 0002", line_id=COAT) == 6
    assert wa.inbound_count("+971500000003") == 1 and wa.inbound_count("+971500000009") == 0
    today = clock.today_dubai()
    clock.advance(timedelta(days=1))
    assert wa.inbound_count(STRANGER) == 0 and wa.inbound_count(STRANGER, today) == 7


def test_whatsapp_sent_to_and_daily_count_use_canonical_numbers_and_dubai_days(
    fakes: FakeSet, call: PortCall, safe: SafeStr, clock: FakeClock
) -> None:
    wa = fakes.whatsapp
    clock.set(datetime(2026, 10, 31, 21, 30, tzinfo=UTC))  # 01:30 on 1 November in Dubai
    assert clock.today_dubai() == date(2026, 11, 1)
    msg = OutboundWhatsApp(line_id=COAT, to="+971 50 000 0002", text=safe)
    assert wa.send(call, msg).accepted
    assert wa.sent_to(STRANGER) == [msg] and wa.sent_to("971500000002") == [msg]
    assert wa.daily_count(COAT) == 1 and wa.daily_count(COAT, date(2026, 11, 1)) == 1
    assert wa.daily_count(COAT, date(2026, 10, 31)) == 0  # the Dubai day, not the UTC one


def test_whatsapp_media_bytes_carry_the_ref_for_the_stt_fake(fakes: FakeSet) -> None:
    wa = fakes.whatsapp
    assert wa.fetch_media("media-1") == AUDIO_MARKER + b"media-1"
    wa.deliver(OWNER_LINE, OWNER, audio_ref="media-2", audio=b"OggS real bytes")
    assert wa.fetch_media("media-2") == b"OggS real bytes"


# --------------------------------------------------------------------------- mailbox


def test_mailbox_deliver_builds_a_realistic_inbound_email(fakes: FakeSet, clock: FakeClock) -> None:
    box = fakes.coat_mail.mailboxes[0]
    pdf = Attachment(ref="att-1", name="invoice.pdf", mime="application/pdf", size=10, text="pay")
    mail = fakes.coat_mail.deliver(
        mailbox=box,
        sender="Supplier Accounts <Supplier@ACME.example>",
        subject="Invoice 1177",
        body="Please find…",
        attachments=[pdf],
    )
    assert mail.mailbox == box and mail.to == (box,) and mail.sender == "supplier@acme.example"
    assert mail.subject == "Invoice 1177" and mail.body_text == "Please find…"
    assert mail.attachments == (pdf,) and mail.at == clock.now() and mail.dkim_pass is True
    assert "dkim=pass" in mail.headers["Authentication-Results"]
    assert "header.d=acme.example" in mail.headers["Authentication-Results"]
    assert mail.headers["From"] == "Supplier Accounts <Supplier@ACME.example>"  # raw, for flags
    assert mail.headers["To"] == box
    assert mail.headers["Message-ID"].startswith("<") and mail.provider_msg_id.startswith("coat-")
    forged = fakes.coat_mail.deliver(
        mailbox=box,
        sender="owner@personal.example",
        subject="pay",
        body="…",
        dkim_pass=False,
        headers={"Reply-To": "attacker@evil.example"},
    )
    assert forged.dkim_pass is False and "dkim=fail" in forged.headers["Authentication-Results"]
    assert forged.headers["Reply-To"] == "attacker@evil.example"
    with pytest.raises(KeyError):
        fakes.coat_mail.deliver(mailbox="nobody@nowhere.example", sender="s", subject="x", body="y")
    with pytest.raises(KeyError):
        fakes.owner_mail.deliver(
            mailbox=box, sender="s", subject="x", body="y"
        )  # wrong credentials


def test_mailbox_pull_inbound_drains_the_queue_whatever_the_cursor(
    fakes: FakeSet, clock: FakeClock
) -> None:
    box = fakes.owner_mail.mailboxes[0]
    first = fakes.owner_mail.deliver(mailbox=box, sender="a@x.example", subject="1", body="")
    clock.advance(timedelta(minutes=10))
    second = fakes.owner_mail.deliver(mailbox=box, sender="b@x.example", subject="2", body="")
    clock.advance(timedelta(minutes=5))
    assert fakes.owner_mail.pending(box) == 2
    # a poller passing since=now (DESIGN §7.4: deliver → advance → poll) loses nothing
    assert fakes.owner_mail.pull_inbound(box, clock.now()) == [first, second]
    assert fakes.owner_mail.pending() == 0 and fakes.owner_mail.pull_inbound(box, clock.now()) == []
    third = fakes.owner_mail.deliver(mailbox=box, sender="c@x.example", subject="3", body="")
    clock.advance(timedelta(hours=1))
    future = clock.now() + timedelta(days=1)
    assert fakes.owner_mail.pull_inbound(box, future) == [third]  # returned exactly once
    assert fakes.owner_mail.pull_inbound(box, datetime(2000, 1, 1, tzinfo=UTC)) == []
    with pytest.raises(ValueError):
        fakes.owner_mail.pull_inbound(box, datetime(2026, 10, 5, 7, 0))  # noqa: DTZ001 - naive on purpose
    assert fakes.owner_mail.pending(box.upper()) == 0  # mailboxes are found by canonical form
    assert fakes.owner_mail.inbound_count("a@x.example") == 1
    assert fakes.owner_mail.inbound_count("a@x.example", mailbox=box) == 1
    with pytest.raises(KeyError):
        fakes.owner_mail.inbound_count("a@x.example", mailbox="other@nowhere.example")


def test_mailbox_counts_inbound_per_canonical_sender(fakes: FakeSet) -> None:
    box = fakes.coat_mail.mailboxes[0]
    for spelling in ("A@X.example", "a@x.example", "Ahmed <a@x.example>", " a@X.EXAMPLE "):
        fakes.coat_mail.deliver(mailbox=box, sender=spelling, subject="s", body="b")
    assert fakes.coat_mail.inbound_count("a@x.example") == 4  # one sender, four spellings
    assert fakes.coat_mail.inbound_count("Ahmed <A@x.example>") == 4
    assert {mail.sender for mail in fakes.coat_mail.delivered} == {"a@x.example"}
    latin = fakes.coat_mail.deliver(mailbox=box, sender="ahmed@buzz-avenue.ae", subject="", body="")
    cyrillic = fakes.coat_mail.deliver(
        mailbox=box, sender="ahmed@buzz-avenuе.ae", subject="", body=""
    )  # Cyrillic е
    assert latin.sender == "ahmed@buzz-avenue.ae" and cyrillic.sender.startswith("ahmed@xn--")
    assert latin.sender != cyrillic.sender  # a homoglyph domain never compares equal
    assert fakes.coat_mail.inbound_count("ahmed@buzz-avenue.ae") == 1
    assert canonical_address("Ahmed <A@X.example>") == "a@x.example"
    assert canonical_address("not an address") == "not an address"
    with pytest.raises(TypeError):
        canonical_address(5)  # type: ignore[arg-type]


def test_mailbox_drafts_sends_records_and_dry_runs(
    fakes: FakeSet, call: PortCall, dry: PortCall, leakguard: LeakGuard, clock: FakeClock
) -> None:
    box = fakes.coat_mail.mailboxes[0]
    draft = DraftEmail(
        mailbox=box,
        to=("customer@x.example",),
        subject=leakguard.safe("Quote"),
        body=leakguard.safe("Please find the quote attached."),
    )
    draft_id = fakes.coat_mail.create_draft(call, draft)
    assert fakes.coat_mail.drafts == {draft_id: draft}
    entry = _last(fakes.call_log)
    assert (entry.port, entry.method, entry.audit_id) == (
        "coat_mail",
        "create_draft",
        call.audit_id,
    )
    dry_receipt = fakes.coat_mail.send(dry, draft_id)
    assert dry_receipt.dry_run and dry_receipt.accepted and fakes.coat_mail.sent == []
    assert fakes.coat_mail.dry_run_sends == [draft] and draft_id in fakes.coat_mail.drafts
    assert _last(fakes.call_log).dry_run is True
    receipt = fakes.coat_mail.send(call, draft_id)
    assert receipt.accepted and receipt.provider_msg_id and not receipt.dry_run
    assert fakes.coat_mail.sent == [draft] and draft_id not in fakes.coat_mail.drafts
    assert (_last(fakes.call_log).method, _last(fakes.call_log).audit_id) == ("send", call.audit_id)
    assert fakes.coat_mail.daily_count(box) == 1 and fakes.coat_mail.sent_to(
        "customer@x.example"
    ) == [draft]
    assert fakes.coat_mail.sent_to("Customer <CUSTOMER@x.example>") == [draft]
    assert fakes.coat_mail.daily_count(box.upper()) == 1
    with pytest.raises(KeyError):
        fakes.coat_mail.send(call, "draft-missing")
    with pytest.raises(KeyError):
        fakes.coat_mail.create_draft(
            call,
            DraftEmail(mailbox="x@y.example", to=("t",), subject=draft.subject, body=draft.body),
        )
    with pytest.raises(ValidationError):
        DraftEmail(mailbox=box, to=("t",), subject="plain", body=draft.body)  # type: ignore[arg-type]
    fakes.coat_mail.fail_next(1)
    with pytest.raises(RuntimeError):
        fakes.coat_mail.create_draft(call, draft)
    assert fakes.coat_mail.create_draft(call, draft).startswith("draft-coat-")


def test_mailbox_scopes_are_a_subset_of_read_draft_send(
    clock: FakeClock, call: PortCall, leakguard: LeakGuard
) -> None:
    assert (
        FakeMailbox(None, clock, "coat", ["nour@x.example"]).scopes("nour@x.example") == ALL_SCOPES
    )
    draft_only = FakeCoatMailbox(
        None, clock, ["nour@x.example"], granted_scopes=frozenset({"read", "draft"})
    )
    draft = DraftEmail(
        mailbox="nour@x.example",
        to=("t@x.example",),
        subject=leakguard.safe("s"),
        body=leakguard.safe("b"),
    )
    draft_id = draft_only.create_draft(call, draft)
    with pytest.raises(ScopeViolation):
        draft_only.send(call, draft_id)
    read_only = FakeOwnerMailbox(None, clock, ["o@x.example"], granted_scopes=frozenset({"read"}))
    with pytest.raises(ScopeViolation):
        read_only.create_draft(
            call,
            DraftEmail(mailbox="o@x.example", to=("t",), subject=draft.subject, body=draft.body),
        )
    with pytest.raises(ScopeViolation):
        FakeMailbox(
            None, clock, "owner", ["o@x.example"], granted_scopes=frozenset({"read", "delete"})
        )
    with pytest.raises(ValueError):
        FakeMailbox(None, clock, "vault", ["o@x.example"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FakeMailbox(None, clock, "coat", [])
    assert FakeCoatMailbox(None, clock, ["a@x.example"]).kind == "coat"
    assert FakeOwnerMailbox(None, clock, ["a@x.example"]).kind == "owner"
    assert FakeCoatMailbox(None, clock, ["a@x.example"]).port_name == "coat_mail"
    assert FakeOwnerMailbox(None, clock, ["a@x.example"]).port_name == "owner_mail"


# --------------------------------------------------------------------------- phone


def test_phone_notifications_online_flag_and_wipe(
    fakes: FakeSet, call: PortCall, dry: PortCall, clock: FakeClock
) -> None:
    phone = fakes.phone
    note = phone.notify("bank", "Payment", "owner says send AED 900")
    assert note.at == clock.now() and note.device_id == phone.device_id and note.app == "bank"
    assert phone.is_online() and phone.pending == 1
    phone.online = False
    assert phone.pull_notifications() == [] and phone.pending == 1
    phone.online = True
    assert phone.pull_notifications() == [note] and phone.pull_notifications() == []
    phone.wipe(dry)
    assert phone.wiped is False and phone.dry_run_wipes == 1 and _last(fakes.call_log).dry_run
    phone.notify("sms", "x", "y")
    phone.wipe(call)
    assert phone.wiped is True and phone.is_online() is False and phone.pending == 0
    assert (_last(fakes.call_log).port, _last(fakes.call_log).method) == ("phone", "wipe")
    assert _last(fakes.call_log).audit_id == call.audit_id


# --------------------------------------------------------------------------- card issuer


def test_card_authorizes_within_the_cap_and_records_every_decision(
    fakes: FakeSet, call: PortCall, clock: FakeClock
) -> None:
    card = fakes.card
    ref = card.card_for(OPERATOR)
    assert ref == "card-operator"
    decision = card.authorize(call, ref, Money.aed(1500), "Ad Platform")
    assert decision.approved and decision.decline_reason is None
    auth = decision.authorization
    assert isinstance(auth, CardAuthorization)
    assert auth.card_ref == ref and auth.holder == OPERATOR and auth.amount == Money.aed(1500)
    assert (
        auth.merchant == "Ad Platform"
        and auth.at == clock.now()
        and auth.auth_ref.startswith("auth-")
    )
    assert decision.remaining == Money.aed(1500) and card.month_total(ref) == Money.aed(1500)
    entry = _last(fakes.call_log)
    assert (entry.port, entry.method, entry.audit_id) == ("card", "authorize", call.audit_id)
    assert entry.args_hash == content_hash(
        {"card_ref": ref, "amount": Money.aed(1500), "merchant": "Ad Platform"}
    )
    assert card.auths == [decision] and card.authorizations(ref) == [auth]
    for text in _strings_in(card):
        assert not [hit for hit in shape_hits(text) if hit.kind == "pan"]  # no PAN anywhere


def test_card_declines_above_the_cap_whatever_the_approval(
    fakes: FakeSet, call: PortCall, cfg: NourConfig
) -> None:
    card = fakes.card
    ref = card.card_for(OPERATOR)
    cap = cfg.spend_tiers.monthly_cap[OPERATOR]
    assert cap == Money.aed(3000)
    assert card.authorize(call, ref, Money.aed(1500), "a").approved
    assert card.authorize(call, ref, Money.aed(1400), "b").approved
    third = card.authorize(call, ref, Money.aed(150), "c")
    assert third.approved is False and third.authorization is None
    assert third.decline_reason == DeclineReason.LIMIT.value == "limit"
    assert third.remaining == Money.aed(100) and card.month_total(ref) == Money.aed(2900)
    assert card.auths[-1].approved is False
    approved_later = card.authorize(call, ref, Money.aed(500), "d")  # "cap beats approval"
    assert approved_later.approved is False and approved_later.decline_reason == "limit"
    assert card.authorize(call, ref, Money.aed(100), "e").approved  # exactly to the cap
    assert card.month_total(ref) == cap and card.remaining(ref) == Money.zero()
    assert card.authorize(call, ref, Money.aed(1), "f").decline_reason == "limit"
    assert len(fakes.call_log.calls) == 6 and fakes.call_log.without_audit() == []


def test_card_declines_when_frozen_and_month_total_follows_the_clock(
    fakes: FakeSet, call: PortCall, dry: PortCall, clock: FakeClock
) -> None:
    card = fakes.card
    ref = card.card_for(OPERATOR)
    card.freeze(dry, ref)  # protective: a kill switch in dry-run mode still freezes the card
    assert card.is_frozen(ref) is True and card.dry_run_ops == [("freeze", ref)]
    assert _last(fakes.call_log).dry_run is True
    assert card.authorize(dry, ref, Money.aed(10), "m").decline_reason == "frozen"
    card.unfreeze(dry, ref)
    assert card.is_frozen(ref) is False and card.dry_run_ops == [("freeze", ref), ("unfreeze", ref)]
    card.freeze(call, ref)
    assert card.is_frozen(ref) and card.frozen == {ref: True}
    frozen = card.authorize(call, ref, Money.aed(10), "m")
    assert frozen.approved is False and frozen.decline_reason == DeclineReason.FROZEN.value
    assert (_last(fakes.call_log).method, _last(fakes.call_log).audit_id) == (
        "authorize",
        call.audit_id,
    )
    card.unfreeze(call, ref)
    assert not card.is_frozen(ref)
    assert card.authorize(call, ref, Money.aed(100), "m").approved
    october = clock.today_dubai()
    clock.advance(timedelta(days=31))
    assert clock.today_dubai().month == 11
    assert card.month_total(ref) == Money.zero() and card.month_total(ref, october) == Money.aed(
        100
    )
    assert card.remaining(ref) == Money.aed(3000)
    assert card.authorize(call, ref, Money.aed(3000), "nov").approved
    assert card.month_total(ref) == Money.aed(3000) and card.month_total(
        ref, date(2026, 10, 1)
    ) == Money.aed(100)


def test_card_month_total_uses_the_dubai_calendar_at_the_utc_boundary(
    fakes: FakeSet, call: PortCall, clock: FakeClock
) -> None:
    card = fakes.card
    ref = card.card_for(OPERATOR)
    clock.set(datetime(2026, 10, 31, 21, 30, tzinfo=UTC))  # 01:30 on 1 November in Dubai
    assert card.authorize(call, ref, Money.aed(100), "m").approved
    assert card.month_total(ref) == Money.aed(100)  # the clock's month is November
    assert card.month_total(ref, date(2026, 11, 1)) == Money.aed(100)
    assert card.month_total(ref, date(2026, 10, 31)) == Money.zero()  # not October (UTC)


def test_card_remaining_is_never_negative_after_a_lower_reissue(
    fakes: FakeSet, call: PortCall
) -> None:
    card = fakes.card
    ref = card.card_for(OPERATOR)
    assert card.authorize(call, ref, Money.aed(2900), "a").approved
    assert card.issue(OPERATOR, Money.aed(1000)) == ref  # the cap drops under the month's spend
    declined = card.authorize(call, ref, Money.aed(10), "b")
    assert declined.approved is False and declined.decline_reason == "limit"
    assert declined.remaining == Money.zero() == card.remaining(ref)  # one answer, floored
    assert card.month_total(ref) == Money.aed(2900)


def test_card_dry_run_computes_the_decision_but_holds_nothing(
    fakes: FakeSet, call: PortCall, dry: PortCall
) -> None:
    card = fakes.card
    ref = card.card_for(OPERATOR)
    decision = card.authorize(dry, ref, Money.aed(200), "m")
    assert decision.approved and decision.authorization is not None
    assert decision.authorization.auth_ref.startswith("dryrun-")
    assert FakeCardIssuer.is_dry_run_ref(decision.authorization.auth_ref)
    assert card.auths == [] and card.dry_run_auths == [decision]
    assert card.authorizations(ref) == []  # a dry-run authorization is never held
    assert card.month_total(ref) == Money.zero() and _last(fakes.call_log).dry_run is True
    assert card.authorize(dry, ref, Money.aed(3001), "m").decline_reason == "limit"
    assert card.auths == []
    with pytest.raises(KeyError):
        card.authorize(call, "card-nobody", Money.aed(1), "m")
    with pytest.raises(ValueError):
        card.authorize(call, ref, Money.aed(0), "m")
    with pytest.raises(CurrencyMismatch):
        card.authorize(call, ref, Money(fils=100, currency="USD"), "m")
    with pytest.raises(TypeError):
        card.authorize(call, ref, 100, "m")  # type: ignore[arg-type]
    card.fail_next(1, ConnectionError)
    with pytest.raises(ConnectionError):
        card.authorize(call, ref, Money.aed(1), "m")
    real = card.authorize(call, ref, Money.aed(1), "m")
    assert real.approved and real.authorization is not None
    assert not FakeCardIssuer.is_dry_run_ref(real.authorization.auth_ref)
    assert not any(FakeCardIssuer.is_dry_run_ref(a.auth_ref) for a in card.authorizations(ref))
    assert card.issue(OPERATOR, Money.aed(5000)) == ref and card.caps[ref] == Money.aed(5000)
    assert card.month_total(ref) == Money.aed(1)  # a re-issue never resets the spend


class _StillClock:
    """A Clock that never moves, for the property test (no fixtures inside ``@given``)."""

    def now(self) -> datetime:
        return datetime(2026, 10, 5, 3, 0, tzinfo=UTC)

    def local(self) -> datetime:
        return self.now().astimezone(DUBAI)

    def today_dubai(self) -> date:
        return self.local().date()


@settings(deadline=None, max_examples=60)
@given(amounts=st.lists(st.integers(min_value=1, max_value=2000), max_size=25))
def test_card_cap_invariant_holds_for_any_sequence(amounts: list[int]) -> None:
    clock = _StillClock()
    card = FakeCardIssuer(CallLog(clock), clock)
    cap = Money.aed(3000)
    ref = card.issue(OPERATOR, cap)
    pc = PortCall(audit_id="01ARZ3NDEKTSV4RRFFQ69G5FAV", desk=Desk.OPERATOR, coat_id=COAT)  # type: ignore[arg-type]
    for amount in amounts:
        before = card.month_total(ref)
        decision = card.authorize(pc, ref, Money.aed(amount), "m")
        assert decision.approved == (before + Money.aed(amount) <= cap)
        assert card.month_total(ref) <= cap
    assert len(card.auths) == len(amounts)


# --------------------------------------------------------------------------- speech


def test_stt_returns_the_scripted_transcript_for_a_voice_note(fakes: FakeSet) -> None:
    stt, wa = fakes.stt, fakes.whatsapp
    scripted = stt.script_ref("media-9", "حوّل خمسمية درهم لشركة الأمل", confidence=0.91)
    note = wa.deliver(OWNER_LINE, OWNER, audio_ref="media-9")
    assert note.audio_ref is not None
    out = stt.transcribe(wa.fetch_media(note.audio_ref), language="ar-LB", vocabulary=["الأمل"])
    assert out == Transcript(
        text=scripted.text, language="ar-LB", confidence=0.91, engine=stt.engine
    )
    assert stt.calls[-1] == (wa.fetch_media("media-9"), "ar-LB", ("الأمل",))
    unscripted = stt.transcribe(wa.fetch_media("media-unknown"), language="ar-LB", vocabulary=())
    assert unscripted.text == "" and unscripted.confidence == 0.0
    assert stt.transcribe(b"", language="ar-LB", vocabulary=()).confidence == 0.0
    by_digest = Transcript(text="by bytes", language="en", confidence=0.8, engine="x")
    import hashlib

    stt.script[hashlib.sha256(b"raw audio").hexdigest()] = by_digest
    assert stt.transcribe(b"raw audio", language="en", vocabulary=()).text == "by bytes"
    assert min(stt.wer_table, key=stt.wer_table.__getitem__) == stt.engine
    stt.fail_next(1, TimeoutError)
    with pytest.raises(TimeoutError):
        stt.transcribe(b"x", language="ar-LB", vocabulary=())
    with pytest.raises(TypeError):
        stt.transcribe("text", language="ar-LB", vocabulary=())  # type: ignore[arg-type]
    assert isinstance(FakeStt(engine="other"), SttPort)


def test_tts_synthesises_deterministic_bytes_and_bills_characters(
    fakes: FakeSet, call: PortCall, safe: SafeStr
) -> None:
    audio = fakes.tts.synthesize(call, safe)
    assert audio == b"TTS:" + safe.encode()
    assert fakes.tts.synthesized == [safe] and fakes.tts.chars_billed == len(safe)
    assert (_last(fakes.call_log).port, _last(fakes.call_log).method) == ("tts", "synthesize")
    with pytest.raises(TypeError):
        fakes.tts.synthesize(call, "plain text")  # type: ignore[arg-type]
    assert isinstance(FakeTts(), TtsPort)


# --------------------------------------------------------------------------- models


def _request(leakguard: LeakGuard, text: str = "hello", desk: Desk = Desk.OPERATOR) -> ModelRequest:
    return ModelRequest(
        role=ModelRole.PRIMARY,
        desk=desk,
        system=leakguard.safe("You are Nour."),
        messages=[ModelMessage(role="user", content=leakguard.safe(text))],
        tools=[],
    )


def test_scripted_model_fifo_then_policy_records_requests_and_usage(
    leakguard: LeakGuard,
) -> None:
    model = ScriptedModel(FAKE_VENDOR_A, "a-1")
    assert isinstance(model, ModelPort) and isinstance(model.policy, SanePolicy)
    canned = response([tool_call("owner.reply", reason="Canned.", text="hi")], "canned")
    model.enqueue(canned)
    first = model.complete(_request(leakguard, "one"))
    assert first.text == "canned" and first.tool_calls == canned.tool_calls
    assert first.vendor == FAKE_VENDOR_A == "fake_a" and first.model == "a-1"
    assert first.usage != NO_USAGE and first.usage.cost_fils > 0
    assert first.usage == usage_estimate(FAKE_VENDOR_A, model.requests[0], canned)
    second = model.complete(_request(leakguard, "two", Desk.ASSISTANT))
    assert second.text == "Noted." and second.vendor == FAKE_VENDOR_A
    assert len(model.requests) == 2 and [r.messages[0].content for r in model.requests] == [
        "one",
        "two",
    ]
    assert model.usage == [first.usage, second.usage]
    total = model.usage_total()
    assert total.input_tokens == first.usage.input_tokens + second.usage.input_tokens
    assert model.usage_total(Desk.ASSISTANT) == second.usage
    assert model.usage_total(Desk.GOVERNANCE) == NO_USAGE
    assert model.trace == [(model.requests[0], first), (model.requests[1], second)]
    explicit = ModelResponse(
        text="x",
        tool_calls=[],
        vendor="v",
        model="m",
        usage=ModelUsage(input_tokens=5, output_tokens=6, cost_fils=7),
    )
    model.enqueue(explicit)
    assert model.complete(_request(leakguard)).usage == explicit.usage
    with pytest.raises(TypeError):
        model.complete("not a request")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        model.enqueue("x")  # type: ignore[arg-type]


def test_scripted_model_fail_next_raises_model_unavailable_exactly_n_times(
    leakguard: LeakGuard,
) -> None:
    model = ScriptedModel(FAKE_VENDOR_B, "b-1", SanePolicy())
    model.fail_next(2)
    for _ in range(2):
        with pytest.raises(ModelUnavailable):
            model.complete(_request(leakguard, "down"))
    ok = model.complete(_request(leakguard, "up"))
    assert ok.vendor == FAKE_VENDOR_B == "fake_b"
    assert model.call_log.calls == []  # a model call is not a side-effecting port method
    assert len(model.requests) == 3 and len(model.responses) == 1  # outages are still recorded
    assert model.trace == [(model.requests[2], ok)] and model.usage_total() == ok.usage
    with pytest.raises(ValueError):
        ScriptedModel("", "m")


def test_private_model_needs_the_renderers_witness_and_a_guard(leakguard: LeakGuard) -> None:
    private = FakePrivateModel(leakguard)
    assert isinstance(private, Tier2ModelPort) and private.guard is leakguard
    ref = SecretRef(
        uri="vault://buzz-avenue/banking/receiving#iban",
        last4="3456",
        content_fp=leakguard.content_fp("v"),
    )
    req = PrivateModelRequest(refs=(ref,), instruction=leakguard.safe("Summarise."))
    with pytest.raises(TypeError):
        private.complete_private(req, object())  # type: ignore[arg-type]
    assert private.calls == []
    with pytest.raises(TypeError):
        FakePrivateModel("not a guard")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- secrets


def test_secrets_scoped_narrows_only(fakes: FakeSet) -> None:
    root = fakes.secrets
    operator = root.scoped("operator/")
    assert operator.prefix == "operator/" and isinstance(operator, SecretsPort)
    assert operator.get("operator/whatsapp-token") == seed_value("operator/whatsapp-token")
    assert operator.names() == sorted(n for n in DEFAULT_SECRET_NAMES if n.startswith("operator/"))
    with pytest.raises(ScopeViolation):
        operator.get("assistant/vault/field-key")
    with pytest.raises(ScopeViolation):
        operator.scoped("assistant/")
    with pytest.raises(ScopeViolation):
        operator.scoped("")
    with pytest.raises(ScopeViolation):
        operator.put("assistant/x", b"y")
    with pytest.raises(ValueError):
        operator.scoped("operator/stt")  # would also cover "operator/sttX/…": a prefix ends in "/"
    with pytest.raises(ValueError):
        FakeSecrets(prefix="operator")
    narrower = operator.scoped("operator/stt/")
    assert narrower.prefix == "operator/stt/" and narrower.names() == []
    with pytest.raises(ScopeViolation):
        narrower.get("operator/stt-key")
    with pytest.raises(ScopeViolation):
        narrower.get("operator/whatsapp-token")
    with pytest.raises(KeyError):
        root.get("governance/nope")
    with pytest.raises(TypeError):
        root.put("governance/x", "text")  # type: ignore[arg-type]


def test_secrets_revoke_all_makes_get_raise_everywhere(
    fakes: FakeSet, call: PortCall, dry: PortCall
) -> None:
    root = fakes.secrets
    operator = root.scoped("operator/")
    assert root.revoke_all(dry, ["auditor/"]) == ["auditor/"]
    assert root.revoked == ["auditor/"] and _last(fakes.call_log).dry_run is True  # protective
    with pytest.raises(Revoked):
        root.get("auditor/whatsapp-token")
    assert operator.get("operator/stamp-key")
    assert root.revoke_all(call, ["operator/", "assistant/", "operator/"]) == [
        "operator/",
        "assistant/",
        "operator/",
    ]
    assert root.revoked == ["auditor/", "operator/", "assistant/"]
    assert operator.revoked is root.revoked
    entry = _last(fakes.call_log)
    assert (entry.port, entry.method, entry.audit_id) == ("secrets", "revoke_all", call.audit_id)
    for name in DEFAULT_SECRET_NAMES:
        if name.startswith(("operator/", "assistant/", "auditor/")):
            with pytest.raises(Revoked):
                root.get(name)
            assert root.is_revoked(name)
        else:
            assert root.get(name) == seed_value(name)
    with pytest.raises(Revoked):
        operator.get("operator/whatsapp-token")
    assert root.scoped("governance/").get("governance/leakguard-key")
    root.fail_next(1)
    with pytest.raises(RuntimeError):
        root.scoped("governance/").rotate(call, "governance/leakguard-key")  # one queue per store
    assert root.failures_pending == 0 and root.scoped("governance/").failures_pending == 0
    root.fail_next(1)
    with pytest.raises(RuntimeError):
        root.revoke_all(call, ["governance/"])
    assert "governance/" not in root.revoked
    with pytest.raises(TypeError):
        root.revoke_all(call, "governance/")  # type: ignore[arg-type]  # a bare str, not per char
    assert "governance/" not in root.revoked and not root.is_revoked("governance/leakguard-key")


def test_secrets_desk_scoped_port_cannot_revoke_another_scope(
    fakes: FakeSet, call: PortCall, tokens: dict[str, AnyToken]
) -> None:
    root = fakes.secrets
    operator = fakes.for_desk(tokens["operator"]).secrets  # type: ignore[arg-type]
    assert operator.prefix == "operator/"
    for prefixes in (["governance/"], ["assistant/"], [""], ["operator/", "auditor/"]):
        with pytest.raises(ScopeViolation):
            operator.revoke_all(call, prefixes)
        assert root.revoked == []
        assert _last(fakes.call_log).method == "revoke_all"  # the attempt is on the record
    assert root.get("governance/leakguard-key") == seed_value("governance/leakguard-key")
    assert operator.revoke_all(call, ["operator/"]) == ["operator/"]  # its own scope: allowed
    assert root.revoked == ["operator/"]
    governance = fakes.for_desk(tokens["governance"]).secrets  # type: ignore[arg-type]
    assert governance.prefix == "governance/"
    assert governance.revoke_all(call, ["assistant/"]) == ["assistant/"]  # the kill switch
    assert root.revoked == ["operator/", "assistant/"]
    with pytest.raises(Revoked):
        root.get("assistant/vault/field-key")
    assert governance.get("governance/leakguard-key")  # governance/ untouched (DESIGN §6)


def test_secrets_rotate_bumps_the_version_and_changes_the_bytes(
    fakes: FakeSet, call: PortCall, dry: PortCall
) -> None:
    name = "assistant/vault/field-key"
    root = fakes.secrets
    before = root.get(name)
    assert len(before) == 32 and root.version(name) == 1
    root.rotate(dry, name)
    assert root.get(name) == before and root.version(name) == 1
    root.rotate(call, name)
    assert root.version(name) == 2 and root.get(name) != before and len(root.get(name)) == 32
    assert (_last(fakes.call_log).method, _last(fakes.call_log).audit_id) == (
        "rotate",
        call.audit_id,
    )
    with pytest.raises(ScopeViolation):
        root.scoped("operator/").rotate(call, name)
    with pytest.raises(KeyError):
        root.rotate(call, "assistant/unknown")
    fresh = FakeSecrets()
    fresh.put("a/b", b"x")
    assert fresh.get("a/b") == b"x" and fresh.version("a/b") == 1


# --------------------------------------------------------------------------- object storage


def test_objects_store_and_recipient_bound_links(
    fakes: FakeSet, call: PortCall, dry: PortCall
) -> None:
    objects = fakes.objects
    key = objects.put(
        call, "vault/buzz-avenue/licence.pdf", b"%PDF-1.4 fake", content_type="application/pdf"
    )
    assert key == "vault/buzz-avenue/licence.pdf" and objects.get(key) == b"%PDF-1.4 fake"
    entry = _last(fakes.call_log)
    assert (entry.port, entry.method, entry.audit_id) == ("objects", "put", call.audit_id)
    assert entry.args_hash == content_hash(
        {"key": key, "size": 13, "content_type": "application/pdf"}
    )
    link = objects.signed_link(call, key, timedelta(hours=1), "lawyer@firm.example")
    assert "lawyer@firm.example" in link and key in link and "dry-run" not in link
    assert objects.links == [(key, "lawyer@firm.example", timedelta(hours=1))]
    dry_link = objects.signed_link(dry, key, timedelta(hours=1), "x@y.example")
    assert "dry-run" in dry_link and objects.dry_run_links == [
        (key, "x@y.example", timedelta(hours=1))
    ]
    assert len(objects.links) == 1
    with pytest.raises(ValueError):
        objects.signed_link(call, key, timedelta(0), "x@y.example")
    with pytest.raises(KeyError):
        objects.signed_link(call, "missing", timedelta(hours=1), "x@y.example")
    with pytest.raises(TypeError):
        objects.put(call, "k", "text", content_type="text/plain")  # type: ignore[arg-type]
    assert list(objects.text_sinks()) == [(key, "%PDF-1.4 fake")]
    objects.delete(dry, key)
    assert objects.exists(key) and objects.dry_run_deletes == [key]
    objects.delete(call, key)
    assert not objects.exists(key) and objects.deleted == [key]
    with pytest.raises(KeyError):
        objects.get(key)


# --------------------------------------------------------------------------- bank feed


def test_bank_feed_serves_seeded_lines_and_balances(fakes: FakeSet, clock: FakeClock) -> None:
    bank = fakes.bank
    assert bank.balance(COAT) == Money.zero() and bank.lines(COAT, clock.now()) == []
    first = bank.line(COAT, Money.aed(1200), counterpart_last4="4567", memo="INV-1177")
    clock.advance(timedelta(hours=2))
    cutoff = clock.now()
    second = bank.line(COAT, Money.aed(-300), counterpart_last4="9999", memo="refund")
    assert first.at < second.at and first.account_ref == f"acct-{COAT}"
    assert bank.lines(COAT, cutoff) == [second]
    assert bank.lines(COAT, cutoff - timedelta(days=1)) == [first, second]
    bank.set_balance(COAT, Money.aed(10_000))
    assert bank.balance(COAT) == Money.aed(10_000)
    seeded = BankLine(
        ref="x",
        account_ref="a",
        at=clock.now(),
        amount=Money.aed(1),
        counterpart_last4="1111",
        memo="m",
    )
    bank.seed(CoatId("other"), [seeded])
    assert bank.lines(CoatId("other"), cutoff) == [seeded] and bank.lines(COAT, cutoff) == [second]
    with pytest.raises(ValueError):
        bank.lines(COAT, datetime(2026, 1, 1))  # noqa: DTZ001 - naive on purpose
    with pytest.raises(TypeError):
        bank.set_balance(COAT, 5)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- second channel


def test_second_channel_requests_alerts_reply_and_kill(
    fakes: FakeSet, call: PortCall, dry: PortCall, leakguard: LeakGuard, clock: FakeClock
) -> None:
    second = fakes.second
    summary = leakguard.safe("Release the kill switch?")
    second.send_confirmation(call, "kill_switch_release", "tok-1", summary)
    assert second.requests == [("kill_switch_release", "tok-1", summary)]
    entry = _last(fakes.call_log)
    assert (entry.port, entry.method, entry.audit_id) == (
        "second",
        "send_confirmation",
        call.audit_id,
    )
    assert entry.args_hash == content_hash(
        {"purpose": "kill_switch_release", "summary": summary}
    )  # no token
    second.send_confirmation(dry, "constitution:abc", "tok-2", summary)
    assert (
        second.dry_run_requests == [("constitution:abc", "tok-2", summary)]
        and len(second.requests) == 1
    )
    assert second.latest_token() == "tok-1"  # a dry-run challenge never reached the owner
    assert second.latest_dry_run_token() == "tok-2"
    reply = second.reply("yes")
    assert isinstance(reply, SecondChannelMessage)
    assert reply == SecondChannelMessage(
        sender=second.address, text="yes", token="tok-1", at=clock.now()
    )
    explicit = second.reply("yes", token="tok-2", sender="someone@else.example")  # on purpose
    assert explicit.token == "tok-2" and explicit.sender == "someone@else.example"
    killed = second.kill()
    assert (
        killed.text in second.kill_phrases
        and killed.token is None
        and killed.sender == second.address
    )
    assert second.pending == 3 and second.pull_messages() == [reply, explicit, killed]
    assert second.pull_messages() == [] and second.messages == [reply, explicit, killed]
    alert = leakguard.safe("A wrong passphrase was entered on the owner thread.")
    second.send_alert(call, alert)
    assert second.alerts == [alert] and len(second.alerts) == 1
    second.send_alert(dry, alert)
    assert second.dry_run_alerts == [alert] and len(second.alerts) == 1
    with pytest.raises(TypeError):
        second.send_alert(call, "plain")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        second.send_confirmation(call, "p", "t", "plain")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        second.send_confirmation(call, "p", "", summary)
    with pytest.raises(ValueError):
        FakeSecondChannel(address="  ")
    with pytest.raises(ValueError):
        FakeSecondChannel(kill_phrases=[])
    app = FakeSecondChannel(address="desktop-app://owner-device")  # SPEC §6: the desktop app
    assert app.address == "desktop-app://owner-device"
    assert isinstance(FakeSecondChannel(), SecondChannelPort)
    unordered = FakeSecondChannel(kill_phrases=frozenset({"توقفي نور", "stop nour", "halt"}))
    assert unordered.kill_phrases == ("halt", "stop nour", "توقفي نور")  # hash-seed independent
    assert unordered.kill().text == "halt"
    ordered = FakeSecondChannel(kill_phrases=["توقفي نور", "halt", "halt"])
    assert ordered.kill_phrases == ("توقفي نور", "halt") and ordered.kill_phrase == "توقفي نور"


# --------------------------------------------------------------------------- vector index


def test_vector_index_scores_overlap_per_namespace_and_tracks_touched(
    fakes: FakeSet, leakguard: LeakGuard
) -> None:
    index = fakes.vector
    for ns, item_id, text in (
        ("operator", "o1", "Ahmed asked about delivery times for the blue chairs"),
        ("operator", "o2", "Supplier invoice 1177 paid"),
        ("assistant", "a1", "The owner's daughter has school on Sunday"),
    ):
        index.upsert(
            IndexableText(
                id=item_id,
                namespace=ns,
                text=leakguard.safe(text),
                tier=DataTier.T0,
                meta={"kind": "episodic"},
            )
        )
    assert index.touched == {"operator", "assistant"} and index.size("operator") == 2
    hits = index.search("operator", "delivery times", k=5)
    assert (
        [h.id for h in hits] == ["o1"]
        and 0 < hits[0].score <= 1
        and hits[0].meta == {"kind": "episodic"}
    )
    assert index.search("operator", "owner's daughter school", k=5) == []  # another namespace
    assert (
        index.search("operator", "", k=5) == [] and index.search("operator", "invoice", k=0) == []
    )
    both = index.search("operator", "Ahmed invoice", k=5)
    assert {h.id for h in both} == {"o1", "o2"}
    assert (
        index.search("operator", "ahmed invoice", k=1)[0].id
        == max(both, key=lambda h: (h.score, -ord(h.id[1]))).id
    )
    index.delete("operator", "o2")
    assert index.size("operator") == 1 and index.search("operator", "invoice", k=5) == []
    index.delete("operator", "missing")
    fresh = FakeVectorIndex()
    assert (
        fresh.touched == set()
        and fresh.search("nobody", "x", 3) == []
        and fresh.touched == {"nobody"}
    )
    with pytest.raises(ValidationError):
        IndexableText(
            id="t2",
            namespace="assistant",
            text=leakguard.safe("secret"),
            tier=DataTier.T2,  # type: ignore[arg-type]
            meta={},
        )
    with pytest.raises(TypeError):
        index.upsert("text")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- reserved ports


def test_reserved_fakes_record_only(
    fakes: FakeSet, call: PortCall, dry: PortCall, leakguard: LeakGuard, clock: FakeClock
) -> None:
    now = clock.now()
    event = CalendarEvent(
        id="e1",
        title=leakguard.safe("Supplier call"),
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=2),
    )
    later = CalendarEvent(
        id="e2",
        title=leakguard.safe("Dinner"),
        start=now + timedelta(days=1),
        end=now + timedelta(days=1, hours=1),
    )
    fakes.calendar.seed("work", [later, event])
    assert fakes.calendar.list_events("work", now, now + timedelta(hours=3)) == [event]
    assert fakes.calendar.list_events("work", now, now + timedelta(days=2)) == [event, later]
    assert fakes.calendar.list_events("other", now, now + timedelta(days=2)) == []
    assert fakes.calendar.queries[-1] == ("other", now, now + timedelta(days=2))

    script = leakguard.safe("Hello, this is Nour from Buzz Avenue.")
    assert fakes.telephony.place_call(call, COAT, STRANGER, script).startswith("call-")
    assert fakes.telephony.calls == [(COAT, STRANGER, script)]
    assert (_last(fakes.call_log).port, _last(fakes.call_log).audit_id) == (
        "telephony",
        call.audit_id,
    )
    assert fakes.telephony.place_call(dry, COAT, STRANGER, script).startswith("dryrun-call-")
    assert (
        fakes.telephony.dry_run_calls == [(COAT, STRANGER, script)]
        and len(fakes.telephony.calls) == 1
    )

    fakes.sandbox.enqueue(SandboxResult(ok=False, stdout="boom", artifacts=()))
    assert fakes.sandbox.run(call, "print(1)", 5).ok is False
    assert fakes.sandbox.run(call, "print(2)", 5) == SandboxResult(ok=True, stdout="", artifacts=())
    assert fakes.sandbox.runs == [("print(1)", 5), ("print(2)", 5)]
    assert _last(fakes.call_log).args_hash == content_hash({"code_len": 8, "timeout_s": 5})
    with pytest.raises(ValueError):
        fakes.sandbox.run(call, "x", 0)

    fakes.ads.set_budget(call, "camp-1", Money.aed(50))
    assert fakes.ads.budgets == {"camp-1": Money.aed(50)}
    fakes.ads.set_budget(dry, "camp-2", Money.aed(70))
    assert (
        fakes.ads.dry_run_budgets == [("camp-2", Money.aed(70))]
        and "camp-2" not in fakes.ads.budgets
    )
    with pytest.raises(ValueError):
        fakes.ads.set_budget(call, "camp-3", Money.aed(-1))
    for fake, proto in (
        (FakeCalendar(), CalendarPort),
        (FakeTelephony(), TelephonyPort),
        (FakeSandbox(), SandboxPort),
        (FakeAds(), AdPlatformPort),
    ):
        assert isinstance(fake, proto)


# --------------------------------------------------------------------------- the coverage assertion


def test_every_side_effect_lands_in_the_shared_call_log_with_its_audit_id(
    fakes: FakeSet, idgen: IdGenerator, leakguard: LeakGuard, safe: SafeStr
) -> None:
    calls = [PortCall(audit_id=idgen.new(), desk=Desk.OPERATOR, coat_id=COAT) for _ in range(12)]
    box = fakes.coat_mail.mailboxes[0]
    fakes.whatsapp.send(calls[0], OutboundWhatsApp(line_id=COAT, to=STRANGER, text=safe))
    draft_id = fakes.coat_mail.create_draft(
        calls[1], DraftEmail(mailbox=box, to=("t@x.example",), subject=safe, body=safe)
    )
    fakes.coat_mail.send(calls[2], draft_id)
    fakes.phone.wipe(calls[3])
    fakes.card.authorize(calls[4], fakes.card.card_for(OPERATOR), Money.aed(5), "m")
    fakes.tts.synthesize(calls[5], safe)
    fakes.secrets.rotate(calls[6], "operator/stt-key")
    fakes.objects.put(calls[7], "k", b"v", content_type="text/plain")
    fakes.second.send_alert(calls[8], safe)
    fakes.telephony.place_call(calls[9], COAT, STRANGER, safe)
    fakes.sandbox.run(calls[10], "x", 1)
    fakes.ads.set_budget(calls[11], "c", Money.aed(1))
    log = fakes.call_log
    assert len(log) == 12 and log.without_audit() == []
    assert [entry.audit_id for entry in log.calls] == [c.audit_id for c in calls]
    assert set(log.by_audit_id()) == {c.audit_id for c in calls}
    assert [entry.port for entry in log.calls] == [
        "whatsapp",
        "coat_mail",
        "coat_mail",
        "phone",
        "card",
        "tts",
        "secrets",
        "objects",
        "second",
        "telephony",
        "sandbox",
        "ads",
    ]


def test_an_unaudited_call_is_recorded_and_refused_before_any_effect(
    fakes: FakeSet, safe: SafeStr
) -> None:
    msg = OutboundWhatsApp(line_id=COAT, to=STRANGER, text=safe)
    with pytest.raises(TypeError, match="takes a PortCall"):
        fakes.whatsapp.send(None, msg)  # type: ignore[arg-type]
    assert fakes.whatsapp.sent == [] and fakes.whatsapp.dry_run_sends == []
    ref = fakes.card.card_for(OPERATOR)
    with pytest.raises(TypeError, match="takes a PortCall"):
        fakes.card.authorize(None, ref, Money.aed(5), "m")  # type: ignore[arg-type]
    assert fakes.card.auths == [] and fakes.card.month_total(ref) == Money.zero()
    with pytest.raises(TypeError, match="takes a PortCall"):
        fakes.card.freeze(None, ref)  # type: ignore[arg-type]
    assert not fakes.card.is_frozen(ref)
    unaudited = fakes.call_log.without_audit()
    assert [(e.port, e.method) for e in unaudited] == [
        ("whatsapp", "send"),
        ("card", "authorize"),
        ("card", "freeze"),
    ]  # the coverage report can name every attempt
    assert len(fakes.call_log) == 3
    with pytest.raises(TypeError):
        fakes.whatsapp.send("not a call", msg)  # type: ignore[arg-type]
    assert len(fakes.call_log) == 3  # a wrong type is refused without being recorded
