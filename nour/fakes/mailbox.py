"""``FakeMailbox``: the in-memory coat / owner mailbox (DESIGN §3.9 §6; SPEC §5 §9 §11 §13).

One instance per credential set: ``kind="coat"`` is the ``CoatMailboxPort`` the Operator may hold
(``nour@<company-domain>``, one address per coat), ``kind="owner"`` the ``OwnerMailboxPort`` only
the Assistant process is built with (DESIGN §4a; ``PortSet.for_desk(OperatorToken)`` drops it).
The port exposes read, draft and send and nothing else; ``scopes()`` is always a subset of
``{"read", "draft", "send"}`` (SPEC §9) and a draft or send outside the granted scopes raises
``ScopeViolation`` (docs/adapters/mailbox.md §6).

``deliver`` builds an ``InboundEmail`` as the Gmail/Graph adapter would: a provider id, the
``to`` tuple, the attachments with their extracted text (observed content, SPEC §13), and
realistic headers (``Message-ID``, ``From`` as the sender wrote it, ``To``, ``Date``,
``Authentication-Results`` with ``dkim=pass|fail``) that the spoof-flag parser of a later wave
can read. ``dkim_pass`` is the provider's verdict, never a decision.

Addresses are canonical wherever the fake stores or compares one (THREAT_REVIEW top-10 #2):
:func:`canonical_address` strips a display name (``Ahmed <a@x.example>``), lower-cases, and
IDNA-encodes the domain, so a homoglyph domain (``buzz-avenuе.ae`` with a Cyrillic ``е``) is
stored as its punycode form and can never compare equal to the Latin one. ``InboundEmail.sender``
is the canonical address; ``headers["From"]`` keeps the raw form for the spoof-flag parser.

The inbound queue *is* the read cursor: ``pull_inbound`` drains everything delivered so far and a
mail is returned exactly once, whatever ``since`` the poller passes (a real adapter maps ``since``
to the provider's query; the fake only validates it). Nothing is ever stranded.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date
from email.utils import format_datetime, parseaddr
from typing import Literal

from pydantic import AwareDatetime

from nour.core.clock import DUBAI, Clock
from nour.core.errors import ScopeViolation
from nour.core.ports import Attachment, CallLog, DraftEmail, InboundEmail, PortCall, SendReceipt
from nour.fakes import FakePort

ALL_SCOPES: frozenset[str] = frozenset({"read", "draft", "send"})
"""SPEC §9: the only scopes a mailbox port may hold."""


def canonical_address(address: str) -> str:
    """``address`` as the fake stores and compares it (THREAT_REVIEW top-10 #2).

    A display name is stripped (``email.utils.parseaddr``), the whole address is NFKC-normalised,
    stripped and lower-cased, and the domain is IDNA-encoded (punycode for any non-ASCII label,
    so a homoglyph domain never equals the Latin one). Text with no ``@`` is lower-cased as is.
    """
    if not isinstance(address, str):
        raise TypeError("an e-mail address is a str")
    _, addr = parseaddr(address)
    raw = addr.strip() if "@" in addr else address.strip()
    raw = unicodedata.normalize("NFKC", raw).strip().lower()
    if "@" not in raw:
        return raw
    local, _, domain = raw.rpartition("@")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # a label IDNA cannot encode stays as typed (still lower-case NFKC)
    return f"{local}@{domain}"


def same_address(a: str, b: str) -> bool:
    return canonical_address(a) == canonical_address(b)


class FakeMailbox(FakePort):
    """MailboxPort fake for one credential set (``kind`` "coat" or "owner").

    ``mailboxes`` are the addresses this credential set reads (kept as given; looked up by
    canonical form); delivering to or drafting from another address raises ``KeyError`` (the
    adapter has no credentials for it). ``granted_scopes`` narrows the port for the phase-1
    draft-only gate (SPEC §16).
    """

    port_name: str = "mailbox"  # set per instance to "coat_mail" / "owner_mail"

    def __init__(
        self,
        call_log: CallLog | None,
        clock: Clock | None,
        kind: Literal["coat", "owner"],
        mailboxes: Sequence[str],
        *,
        granted_scopes: frozenset[str] = ALL_SCOPES,
    ) -> None:
        super().__init__(call_log, clock)
        if kind not in ("coat", "owner"):
            raise ValueError("FakeMailbox kind is 'coat' or 'owner'")
        if not mailboxes:
            raise ValueError("FakeMailbox needs at least one mailbox address")
        if not frozenset(granted_scopes) <= ALL_SCOPES:
            raise ScopeViolation(
                f"a mailbox port may hold only {sorted(ALL_SCOPES)}, got {sorted(granted_scopes)}"
            )
        self.kind: Literal["coat", "owner"] = kind
        self.port_name = f"{kind}_mail"
        self.mailboxes: tuple[str, ...] = tuple(mailboxes)
        self.granted_scopes: frozenset[str] = frozenset(granted_scopes)
        self.drafts: dict[str, DraftEmail] = {}
        self.sent: list[DraftEmail] = []
        self.dry_run_sends: list[DraftEmail] = []
        self.receipts: list[SendReceipt] = []
        self.delivered: list[InboundEmail] = []
        self._by_canonical: dict[str, str] = {canonical_address(box): box for box in self.mailboxes}
        self._queue: dict[str, list[InboundEmail]] = {box: [] for box in self.mailboxes}
        self._send_days: list[tuple[DraftEmail, date]] = []
        self._seq = 0

    # ----- the world talks to Nour

    def deliver(
        self,
        *,
        mailbox: str,
        sender: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment] = (),
        dkim_pass: bool = True,
        headers: Mapping[str, str] | None = None,
        provider_msg_id: str | None = None,
    ) -> InboundEmail:
        """Queue one inbound e-mail in ``mailbox`` (must be one this credential set reads).

        ``sender`` may carry a display name and any casing; ``InboundEmail.sender`` is its
        canonical form and ``headers["From"]`` the raw one. Headers default to what Gmail/Graph
        would surface (``Message-ID``, ``From``, ``To``, ``Subject``, ``Date``,
        ``Authentication-Results`` reflecting ``dkim_pass``); explicit ``headers`` are merged on
        top, so a spoof test can forge any of them.
        """
        box = self._box(mailbox)
        canonical = canonical_address(sender)
        self._seq += 1
        at = self.clock.now()
        if provider_msg_id is None:
            provider_msg_id = f"{self.kind}-{self._seq:016x}"
        verdict = "pass" if dkim_pass else "fail"
        domain = canonical.rsplit("@", 1)[-1] if "@" in canonical else "unknown.example"
        default_headers = {
            "Message-ID": f"<{provider_msg_id}@fake.{self.kind}.example>",
            "From": sender,
            "To": box,
            "Subject": subject,
            "Date": format_datetime(at),
            "Authentication-Results": (
                f"mx.fake.example; spf={verdict} smtp.mailfrom={domain}; "
                f"dkim={verdict} header.d={domain}; dmarc={verdict} header.from={domain}"
            ),
        }
        if headers:
            default_headers.update(dict(headers))
        mail = InboundEmail(
            provider_msg_id=provider_msg_id,
            mailbox=box,
            sender=canonical,
            to=(box,),
            subject=subject,
            body_text=body,
            attachments=tuple(attachments),
            headers=default_headers,
            at=at,
            dkim_pass=dkim_pass,
        )
        self._queue[box].append(mail)
        self.delivered.append(mail)
        return mail

    def pull_inbound(self, mailbox: str, since: AwareDatetime) -> list[InboundEmail]:
        """Drain ``mailbox``'s queue: every mail delivered so far, oldest first, each returned
        exactly once. The queue is the cursor; ``since`` must be timezone-aware (``ValueError``
        otherwise) and is not used to filter, so a poller that passes ``since=now`` never loses
        a mail delivered a moment earlier (DESIGN §7.4: deliver → advance → poll)."""
        box = self._box(mailbox)
        if since.tzinfo is None or since.tzinfo.utcoffset(since) is None:
            raise ValueError("pull_inbound takes a timezone-aware 'since'")
        due, self._queue[box] = self._queue[box], []
        return due

    def pending(self, mailbox: str | None = None) -> int:
        if mailbox is None:
            return sum(len(queue) for queue in self._queue.values())
        return len(self._queue[self._box(mailbox)])

    # ----- Nour talks to the world

    def scopes(self, mailbox: str) -> frozenset[str]:
        """The granted scopes for ``mailbox``: always ⊆ {read, draft, send} (SPEC §9)."""
        self._box(mailbox)
        return self.granted_scopes

    def create_draft(self, call: PortCall, draft: DraftEmail) -> str:
        """Store the draft and return its id (a draft is bookkeeping, so it is kept under dry run
        too; only ``send`` is held back)."""
        if not isinstance(draft, DraftEmail):
            raise TypeError("create_draft takes a DraftEmail")
        self._box(draft.mailbox)
        self._record("create_draft", call, draft=draft)
        self._maybe_fail("create_draft")
        if "draft" not in self.granted_scopes:
            raise ScopeViolation(f"{self.kind} mailbox port has no 'draft' scope")
        self._seq += 1
        draft_id = f"draft-{self.kind}-{self._seq:06d}"
        self.drafts[draft_id] = draft
        return draft_id

    def send(self, call: PortCall, draft_id: str) -> SendReceipt:
        """Send a stored draft: moves it from ``drafts`` to ``sent`` (or to ``dry_run_sends``,
        leaving the draft in place, when ``call.dry_run``)."""
        self._record("send", call, draft_id=draft_id)
        self._maybe_fail("send")
        if draft_id not in self.drafts:
            raise KeyError(f"unknown draft {draft_id!r}")
        if "send" not in self.granted_scopes:
            raise ScopeViolation(f"{self.kind} mailbox port has no 'send' scope")
        draft = self.drafts[draft_id]
        if call.dry_run:
            self.dry_run_sends.append(draft)
            receipt = SendReceipt(provider_msg_id=None, accepted=True, dry_run=True)
        else:
            del self.drafts[draft_id]
            self.sent.append(draft)
            self._send_days.append((draft, self.clock.today_dubai()))
            self._seq += 1
            receipt = SendReceipt(
                provider_msg_id=f"sent-{self.kind}-{self._seq:06d}", accepted=True
            )
        self.receipts.append(receipt)
        return receipt

    # ----- counts (SPEC §9 cold caps; THREAT_REVIEW top-10 #7 ingress rate limits)

    def daily_count(self, mailbox: str, day: date | None = None) -> int:
        """Accepted sends from ``mailbox`` on the Dubai day ``day`` (default: today)."""
        day = day if day is not None else self.clock.today_dubai()
        return sum(
            1
            for draft, sent_on in self._send_days
            if same_address(draft.mailbox, mailbox) and sent_on == day
        )

    def inbound_count(
        self, sender: str, day: date | None = None, *, mailbox: str | None = None
    ) -> int:
        """Mails delivered from ``sender`` (any spelling: display name, case, IDNA) on the Dubai
        day ``day`` (default: today), across every mailbox or in ``mailbox`` only (``KeyError``
        for a mailbox this credential set does not read)."""
        day = day if day is not None else self.clock.today_dubai()
        box = self._box(mailbox) if mailbox is not None else None
        wanted = canonical_address(sender)
        return sum(
            1
            for mail in self.delivered
            if mail.sender == wanted
            and (box is None or mail.mailbox == box)
            and mail.at.astimezone(DUBAI).date() == day
        )

    def sent_to(self, address: str) -> list[DraftEmail]:
        """Every accepted send with ``address`` (any spelling) among its recipients."""
        wanted = canonical_address(address)
        return [
            draft for draft in self.sent if any(canonical_address(to) == wanted for to in draft.to)
        ]

    # ----- internals

    def _box(self, mailbox: str) -> str:
        """The stored mailbox key for ``mailbox`` (looked up by canonical form)."""
        if not isinstance(mailbox, str):
            raise TypeError("a mailbox is an address (str)")
        key = self._by_canonical.get(canonical_address(mailbox))
        if key is None:
            raise KeyError(
                f"{self.kind} mailbox port has no credentials for {mailbox!r}; "
                f"known: {list(self.mailboxes)}"
            )
        return key

    def _known(self, mailbox: str) -> None:
        self._box(mailbox)


class FakeCoatMailbox(FakeMailbox):
    """The ``CoatMailboxPort`` twin (``kind == "coat"``): the only mailbox port the Operator holds."""

    kind: Literal["coat"] = "coat"

    def __init__(
        self,
        call_log: CallLog | None,
        clock: Clock | None,
        mailboxes: Sequence[str],
        *,
        granted_scopes: frozenset[str] = ALL_SCOPES,
    ) -> None:
        super().__init__(call_log, clock, "coat", mailboxes, granted_scopes=granted_scopes)


class FakeOwnerMailbox(FakeMailbox):
    """The owner-mailbox twin (``kind == "owner"``): built only into the Assistant's ``PortSet``
    (DESIGN §4a); ``PortSet.for_desk(OperatorToken)`` drops it."""

    kind: Literal["owner"] = "owner"

    def __init__(
        self,
        call_log: CallLog | None,
        clock: Clock | None,
        mailboxes: Sequence[str],
        *,
        granted_scopes: frozenset[str] = ALL_SCOPES,
    ) -> None:
        super().__init__(call_log, clock, "owner", mailboxes, granted_scopes=granted_scopes)
