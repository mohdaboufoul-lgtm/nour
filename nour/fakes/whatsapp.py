"""``FakeWhatsApp``: the in-memory WhatsApp Business provider (DESIGN §3.9 §6; SPEC §4 §9 §12 §16).

Mirrors the Cloud API shapes in ``docs/adapters/whatsapp.md``: ``deliver`` builds an
``InboundWhatsApp`` exactly as the webhook parser would, ``webhook_payload`` renders the Meta
envelope (``entry[].changes[].value.messages[]``, ``wamid.`` ids, ``phone_number_id`` as the
line) with an ``X-Hub-Signature-256`` header computed over the *raw bytes* (``verify_signature``
compares in constant time), ``parse_webhook`` reads it back, ``send`` answers ``accepted`` with a
synthetic ``wamid.`` and counts business sends per line per Dubai day (SPEC §9 daily cap), and
``refuse_next`` scripts the provider's error replies (``131047`` and friends) for the watchdog's
failed-sends rule (SPEC §12).

Addresses are canonical E.164 (``+`` and digits only) everywhere the fake stores or compares a
number (THREAT_REVIEW top-10 #2: every address is normalised before lookup or comparison):
``deliver`` canonicalises the sender exactly as ``parse_webhook`` does, so one number in four
spellings is one sender for ``inbound_count`` and ``sent_to``.

Identity never comes from here: ``sender_display`` is the user-chosen profile name and is data;
the owner check happens server-side in the authenticator (SPEC §6; DESIGN §4d). ``signature_valid``
on a delivered message is what the real webhook verification would have produced, so a test can
replay a spoof (DESIGN §7.1 case 5). The signature is always computed with the secret of the fake
that verifies it: ``signed_webhook_payload`` signs with this instance's ``app_secret``; the
static ``webhook_payload`` takes the secret as a keyword (default: the class ``APP_SECRET``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from nour.core.clock import DUBAI, Clock
from nour.core.ports import CallLog, InboundWhatsApp, OutboundWhatsApp, PortCall, SendReceipt
from nour.fakes import FakePort

OWNER_LINE = "owner"
"""The line id of the owner thread; every coat line is keyed by the coat slug."""

AUDIO_MARKER = b"OggS\x00nour-fake-audio:"
"""``fetch_media(ref)`` returns ``AUDIO_MARKER + ref`` unless bytes were attached at ``deliver``;
``FakeStt`` reads the ref back out of these bytes to find its scripted transcript."""

WINDOW_CLOSED = "131047 Re-engagement message: more than 24 hours since the user last replied"
"""The provider error a free-form message gets outside the customer-service window."""

MEDIA_KINDS: frozenset[str] = frozenset({"image", "document", "video", "sticker"})
"""Message kinds that carry an optional ``caption``; without one there is nothing to read."""


def _digits(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


def canonical_number(number: str) -> str:
    """``number`` as ``+<digits>``: spaces, dashes, dots and a missing ``+`` are spelling, not
    identity (THREAT_REVIEW top-10 #2). ``ValueError`` when there is no digit at all."""
    if not isinstance(number, str):
        raise TypeError("a WhatsApp number is a str")
    digits = _digits(number)
    if not digits:
        raise ValueError(f"not a phone number: {number!r}")
    return "+" + digits


def same_number(a: str, b: str) -> bool:
    """Whether two spellings name one number (``False`` when either has no digits)."""
    return _digits(a) != "" and _digits(a) == _digits(b)


class FakeWhatsApp(FakePort):
    """WhatsAppPort fake: ``deliver`` fills the inbound queue, ``send`` records (or dry-runs).

    ``lines`` maps line id → the line's own number (``"owner"`` → Nour's owner-thread number,
    ``"buzz-avenue"`` → the coat's business line). ``owner_number`` is the owner's number
    (stored canonical), used when ``deliver`` is called with ``sender=None``. ``app_secret``
    is what ``verify_signature`` checks against; ``signed_webhook_payload`` signs with it.
    """

    port_name: str = "whatsapp"
    APP_SECRET: ClassVar[bytes] = b"nour-fake-whatsapp-app-secret"

    def __init__(
        self,
        call_log: CallLog | None,
        clock: Clock | None,
        lines: Mapping[str, str],
        *,
        owner_number: str | None = None,
        app_secret: bytes | None = None,
    ) -> None:
        super().__init__(call_log, clock)
        if not lines:
            raise ValueError("FakeWhatsApp needs at least one line (line_id → number)")
        if app_secret is not None and not isinstance(app_secret, bytes | bytearray):
            raise TypeError("app_secret is bytes")
        self.lines: dict[str, str] = dict(lines)
        self.owner_number = canonical_number(owner_number) if owner_number is not None else None
        self.app_secret: bytes = bytes(app_secret) if app_secret is not None else self.APP_SECRET
        self.sent: list[OutboundWhatsApp] = []
        self.dry_run_sends: list[OutboundWhatsApp] = []
        self.receipts: list[SendReceipt] = []
        self.delivered: list[InboundWhatsApp] = []
        self.media: dict[str, bytes] = {}
        self._queue: list[InboundWhatsApp] = []
        self._refusals: list[str] = []
        self._send_days: list[tuple[OutboundWhatsApp, date]] = []
        self._inbound_seq = 0
        self._outbound_seq = 0

    # ----- the world talks to Nour

    def deliver(
        self,
        line_id: str,
        sender: str | None,
        text: str | None = None,
        *,
        audio_ref: str | None = None,
        audio: bytes | None = None,
        sender_display: str | None = None,
        provider_msg_id: str | None = None,
        signature_valid: bool = True,
    ) -> InboundWhatsApp:
        """Queue one inbound message on ``line_id`` from ``sender`` (``None`` → the owner's number).

        Exactly as the webhook parser would build it: the sender canonical (``+digits``, a
        number with no digits is a ``ValueError``), ``at`` from the clock, a ``wamid.`` id unless
        ``provider_msg_id`` is given (replay a duplicate by passing the same id twice, SPEC §13),
        ``signature_valid`` as the real signature check would have reported. A voice note is
        ``audio_ref`` (optionally with its bytes, else ``fetch_media`` returns the marker form).
        """
        if line_id not in self.lines:
            raise KeyError(f"unknown WhatsApp line {line_id!r}; known: {sorted(self.lines)}")
        if sender is None:
            if self.owner_number is None:
                raise ValueError("deliver(sender=None) needs an owner_number on the fake")
            sender = self.owner_number
        sender = canonical_number(sender)
        if text is None and audio_ref is None:
            raise ValueError("an inbound WhatsApp message needs text or an audio_ref")
        self._inbound_seq += 1
        if provider_msg_id is None:
            provider_msg_id = self._wamid("in", self._inbound_seq)
        if audio_ref is not None and audio is not None:
            self.media[audio_ref] = bytes(audio)
        msg = InboundWhatsApp(
            provider_msg_id=provider_msg_id,
            line_id=line_id,
            sender=sender,
            sender_display=sender_display,
            text=text,
            audio_ref=audio_ref,
            at=self.clock.now(),
            signature_valid=signature_valid,
        )
        self._queue.append(msg)
        self.delivered.append(msg)
        return msg

    def pull_inbound(self) -> list[InboundWhatsApp]:
        """Drain the inbound queue (what ``InboundAdapters.poll`` calls)."""
        out, self._queue = self._queue, []
        return out

    @property
    def pending(self) -> int:
        return len(self._queue)

    def fetch_media(self, audio_ref: str) -> bytes:
        """The bytes behind a voice note: what was attached at ``deliver``, else the marker form."""
        if audio_ref in self.media:
            return self.media[audio_ref]
        return AUDIO_MARKER + audio_ref.encode("utf-8")

    # ----- webhook shape (docs/adapters/whatsapp.md §1.3–1.4)

    @staticmethod
    def webhook_payload(
        msg: InboundWhatsApp, *, app_secret: bytes = APP_SECRET
    ) -> tuple[bytes, str]:
        """``(raw_body, signature_header)`` for ``msg``: the Meta envelope as the webhook would
        POST it, signed with ``app_secret`` over the raw bytes when ``msg.signature_valid`` and
        with a deliberately wrong digest otherwise (so ``verify_signature`` fails). A fake built
        with its own secret signs through :meth:`signed_webhook_payload`."""
        message: dict[str, Any] = {
            "from": _digits(msg.sender),
            "id": msg.provider_msg_id,
            "timestamp": str(int(msg.at.timestamp())),
        }
        if msg.audio_ref is not None:
            message["type"] = "audio"
            message["audio"] = {
                "mime_type": "audio/ogg; codecs=opus",
                "sha256": base64.b64encode(
                    hashlib.sha256(msg.audio_ref.encode()).digest()
                ).decode(),
                "id": msg.audio_ref,
                "voice": True,
            }
            if msg.text is not None:
                message["caption"] = msg.text
        else:
            message["type"] = "text"
            message["text"] = {"body": msg.text}
        contacts = [
            {
                "profile": {"name": msg.sender_display if msg.sender_display is not None else ""},
                "wa_id": _digits(msg.sender),
            }
        ]
        envelope = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA-FAKE",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "",
                                    "phone_number_id": msg.line_id,
                                },
                                "contacts": contacts,
                                "messages": [message],
                            },
                        }
                    ],
                }
            ],
        }
        raw = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if msg.signature_valid:
            header = "sha256=" + hmac.new(bytes(app_secret), raw, hashlib.sha256).hexdigest()
        else:
            header = "sha256=" + "0" * 64
        return raw, header

    def signed_webhook_payload(self, msg: InboundWhatsApp) -> tuple[bytes, str]:
        """:meth:`webhook_payload` signed with *this* fake's ``app_secret``: what the provider
        configured with that secret would POST, so ``verify_signature`` round-trips whatever
        secret the fake was built with (and a ``signature_valid=False`` message still fails)."""
        return self.webhook_payload(msg, app_secret=self.app_secret)

    def verify_signature(self, raw_body: bytes, header: str) -> bool:
        """``X-Hub-Signature-256`` check: HMAC-SHA256 of the raw body with the app secret,
        compared in constant time (docs/adapters/whatsapp.md §1.2)."""
        if not isinstance(raw_body, bytes | bytearray) or not isinstance(header, str):
            return False
        expected = (
            "sha256=" + hmac.new(self.app_secret, bytes(raw_body), hashlib.sha256).hexdigest()
        )
        return hmac.compare_digest(expected, header)

    def parse_webhook(
        self, payload: dict[str, Any], signature_valid: bool
    ) -> list[InboundWhatsApp]:
        """Read every message out of a Meta envelope (statuses are ignored); ``signature_valid`` is
        what ``verify_signature`` said and is stamped on each message. Unknown lines raise.

        Mirrors the adapter's rules (docs/adapters/whatsapp.md §1.3): a message without a usable
        ``from`` (no digits) is dropped, never surfaced with a bogus address; ``text`` and
        ``audio`` are read; an image/document/video/sticker is surfaced only when it carries a
        caption; every other kind (reactions, ``unsupported`` errors, statuses) is ignored.
        """
        out: list[InboundWhatsApp] = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                line_id = str(value.get("metadata", {}).get("phone_number_id", ""))
                if line_id not in self.lines:
                    raise KeyError(f"webhook for unknown line {line_id!r}")
                names = {
                    str(c.get("wa_id", "")): c.get("profile", {}).get("name")
                    for c in value.get("contacts", [])
                }
                for message in value.get("messages", []):
                    raw_from = str(message.get("from", "") or "")
                    if not _digits(raw_from):
                        continue  # no sender: the adapter never surfaces it
                    sender = canonical_number(raw_from)
                    kind = message.get("type")
                    text: str | None = None
                    audio_ref: str | None = None
                    if kind == "text":
                        text = str(message.get("text", {}).get("body", ""))
                    elif kind == "audio":
                        audio_ref = str(message.get("audio", {}).get("id", ""))
                        caption = message.get("caption")
                        text = str(caption) if caption is not None else None
                    elif kind in MEDIA_KINDS:
                        caption = message.get(kind, {}).get("caption") or message.get("caption")
                        if not caption:
                            continue  # media without a caption: nothing to read
                        text = str(caption)
                    else:
                        continue  # reactions, unsupported, statuses: ignored
                    display = names.get(raw_from) or names.get(_digits(raw_from))
                    out.append(
                        InboundWhatsApp(
                            provider_msg_id=str(message.get("id", "")),
                            line_id=line_id,
                            sender=sender,
                            sender_display=display or None,
                            text=text,
                            audio_ref=audio_ref,
                            at=datetime.fromtimestamp(int(message.get("timestamp", "0")), tz=UTC),
                            signature_valid=signature_valid,
                        )
                    )
        return out

    # ----- Nour talks to the world

    def refuse_next(self, n: int, error: str = WINDOW_CLOSED) -> None:
        """Make the next ``n`` sends come back refused (``accepted=False``, ``error``) without
        raising: the provider's error reply, which the watchdog counts as a failed send."""
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("refuse_next takes a non-negative count")
        self._refusals.extend([error] * n)

    def send(self, call: PortCall, msg: OutboundWhatsApp) -> SendReceipt:
        """Record the send; append to ``.sent`` (or ``.dry_run_sends`` when ``call.dry_run``)."""
        if not isinstance(msg, OutboundWhatsApp):
            raise TypeError("send takes an OutboundWhatsApp")
        if msg.line_id not in self.lines:
            raise KeyError(f"unknown WhatsApp line {msg.line_id!r}")
        self._record("send", call, msg=msg)
        self._maybe_fail("send")
        if call.dry_run:
            self.dry_run_sends.append(msg)
            receipt = SendReceipt(provider_msg_id=None, accepted=True, dry_run=True)
        elif self._refusals:
            error = self._refusals.pop(0)
            receipt = SendReceipt(provider_msg_id=None, accepted=False, error=error)
        else:
            self._outbound_seq += 1
            self.sent.append(msg)
            self._send_days.append((msg, self.clock.today_dubai()))
            receipt = SendReceipt(
                provider_msg_id=self._wamid("out", self._outbound_seq), accepted=True
            )
        self.receipts.append(receipt)
        return receipt

    # ----- counts (SPEC §9 caps; THREAT_REVIEW top-10 #7 ingress rate limits)

    def daily_count(self, line_id: str, day: date | None = None) -> int:
        """Accepted sends on ``line_id`` on the Dubai day ``day`` (default: today). Dry-run and
        refused sends never count: nothing left."""
        day = day if day is not None else self.clock.today_dubai()
        return sum(
            1 for msg, sent_on in self._send_days if msg.line_id == line_id and sent_on == day
        )

    def inbound_count(
        self, sender: str, day: date | None = None, *, line_id: str | None = None
    ) -> int:
        """Messages delivered from ``sender`` (any spelling of the number) on the Dubai day
        ``day`` (default: today), on every line or on ``line_id`` only."""
        day = day if day is not None else self.clock.today_dubai()
        return sum(
            1
            for msg in self.delivered
            if same_number(msg.sender, sender)
            and (line_id is None or msg.line_id == line_id)
            and msg.at.astimezone(DUBAI).date() == day
        )

    def sent_to(self, to: str) -> list[OutboundWhatsApp]:
        """Every accepted send to ``to`` (any spelling of the number)."""
        return [msg for msg in self.sent if same_number(msg.to, to)]

    # ----- internals

    def _wamid(self, direction: str, seq: int) -> str:
        token = base64.b64encode(f"nour-fake:{direction}:{seq:08d}".encode()).decode("ascii")
        return f"wamid.{token}"
