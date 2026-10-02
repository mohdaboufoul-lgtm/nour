"""``FakeSecondChannel``: the owner's second channel (DESIGN §3.9 §6; SPEC §6 §12 §13).

Constitution changes, kill-switch release and deputy activation need the passphrase *plus* a
confirmation on a second channel (SPEC §6); any authentication failure alerts the owner there
(SPEC §13). The fake records every ``send_confirmation`` as ``(purpose, token, summary)`` in
``requests`` and every ``send_alert`` in ``alerts``; a test answers with ``reply(text, token=…)``
(``token=None`` → the latest *delivered* challenge; a dry-run challenge never left, so it can
only be replayed by passing its token explicitly), which queues a ``SecondChannelMessage`` that
``InboundAdapters.poll`` turns into a ``SECOND_CHANNEL`` inbox event. ``kill()`` queues one of
the constitution's kill phrases: the second kill path (SPEC §12), for when WhatsApp itself is
compromised.

``address`` is what the channel is bound to: an e-mail address or the desktop app (SPEC §6 allows
either; any non-empty handle is accepted). It is never one of the mailboxes Nour reads
(``default_fakes`` refuses that): a confirmation must come from a channel she does not hold
(THREAT_REVIEW top-10 #10). Tokens are never written to the ``CallLog``.
"""

from __future__ import annotations

from collections.abc import Iterable

from nour.core.clock import Clock
from nour.core.ports import CallLog, PortCall, SecondChannelMessage
from nour.core.types import SafeStr
from nour.fakes import FakePort

DEFAULT_KILL_PHRASES: tuple[str, ...] = ("توقفي نور",)
"""The first bullet of ``config/constitution.md`` "Kill switch"; ``default_fakes`` passes the
real set from the loaded constitution."""


def _phrases(kill_phrases: Iterable[str]) -> tuple[str, ...]:
    """The kill phrases, de-duplicated; an unordered input (``set``/``frozenset``, which is what
    ``Constitution.kill_phrases()`` returns) is sorted so ``kill()`` is reproducible across
    processes whatever ``PYTHONHASHSEED`` is."""
    cleaned = [p for p in kill_phrases if isinstance(p, str) and p.strip()]
    if isinstance(kill_phrases, set | frozenset):
        cleaned = sorted(cleaned)
    return tuple(dict.fromkeys(cleaned))


class FakeSecondChannel(FakePort):
    """SecondChannelPort fake: ``requests``, ``alerts``, ``reply``, ``kill``."""

    port_name: str = "second"

    def __init__(
        self,
        call_log: CallLog | None = None,
        clock: Clock | None = None,
        *,
        address: str = "owner@second-channel.example",
        kill_phrases: Iterable[str] = DEFAULT_KILL_PHRASES,
    ) -> None:
        super().__init__(call_log, clock)
        if not isinstance(address, str) or not address.strip():
            raise ValueError("the second channel is bound to a non-empty address or app handle")
        phrases = _phrases(kill_phrases)
        if not phrases:
            raise ValueError("FakeSecondChannel needs at least one kill phrase")
        self.address = address.strip()
        self.kill_phrases = phrases
        self.requests: list[tuple[str, str, SafeStr]] = []
        self.dry_run_requests: list[tuple[str, str, SafeStr]] = []
        self.alerts: list[SafeStr] = []
        self.dry_run_alerts: list[SafeStr] = []
        self.messages: list[SecondChannelMessage] = []
        self._queue: list[SecondChannelMessage] = []
        self._tokens: list[str] = []
        self._dry_run_tokens: list[str] = []

    @property
    def kill_phrase(self) -> str:
        return self.kill_phrases[0]

    # ----- Nour → owner

    def send_confirmation(self, call: PortCall, purpose: str, token: str, summary: SafeStr) -> None:
        """Send a challenge bound to ``purpose``; recorded as ``(purpose, token, summary)``
        (in ``dry_run_requests`` when ``call.dry_run``: the challenge never left)."""
        if not isinstance(summary, SafeStr):
            raise TypeError("send_confirmation takes a SafeStr summary")
        if not token:
            raise ValueError("a confirmation carries a token")
        self._record("send_confirmation", call, purpose=purpose, summary=summary)
        self._maybe_fail("send_confirmation")
        if call.dry_run:
            self._dry_run_tokens.append(token)
            self.dry_run_requests.append((purpose, token, summary))
        else:
            self._tokens.append(token)
            self.requests.append((purpose, token, summary))

    def send_alert(self, call: PortCall, text: SafeStr) -> None:
        """An alert to the owner on the second channel (SPEC §13 passphrase failure)."""
        if not isinstance(text, SafeStr):
            raise TypeError("send_alert takes a SafeStr")
        self._record("send_alert", call, text=text)
        self._maybe_fail("send_alert")
        if call.dry_run:
            self.dry_run_alerts.append(text)
        else:
            self.alerts.append(text)

    def latest_token(self) -> str | None:
        """The token of the most recent *delivered* challenge, or ``None``; a dry-run challenge
        is never offered (the owner never received it)."""
        return self._tokens[-1] if self._tokens else None

    def latest_dry_run_token(self) -> str | None:
        """The token of the most recent challenge held back by dry run, or ``None``."""
        return self._dry_run_tokens[-1] if self._dry_run_tokens else None

    # ----- owner → Nour

    def reply(
        self, text: str, *, token: str | None = None, sender: str | None = None
    ) -> SecondChannelMessage:
        """Queue the owner's reply (``token=None`` → the latest delivered challenge's token;
        ``sender=None`` → the bound address). Returned for inspection; ``pull_messages`` hands it
        to the bus. Pass ``token=`` explicitly to replay a dry-run or stale token on purpose."""
        if token is None:
            token = self.latest_token()
        return self._queue_message(text, token, sender)

    def kill(self) -> SecondChannelMessage:
        """The second kill path (SPEC §12): queue a kill phrase from the bound address, bound to
        no challenge (a kill is never an answer to anything)."""
        return self._queue_message(self.kill_phrase, None, None)

    def _queue_message(
        self, text: str, token: str | None, sender: str | None
    ) -> SecondChannelMessage:
        message = SecondChannelMessage(
            sender=sender if sender is not None else self.address,
            text=text,
            token=token,
            at=self.clock.now(),
        )
        self._queue.append(message)
        self.messages.append(message)
        return message

    def pull_messages(self) -> list[SecondChannelMessage]:
        """Drain the queue: confirmations and kill phrases alike."""
        out, self._queue = self._queue, []
        return out

    @property
    def pending(self) -> int:
        return len(self._queue)
