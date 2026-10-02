"""In-memory fakes, one per port (DESIGN §3.9, §6; SPEC §16 §18).

Every external system Nour talks to is a ``typing.Protocol`` in ``nour/core/ports.py``; this
package holds the one in-memory twin of each, plus the scripted model and the adversarial model
policies that the phase-0 gate (SPEC §16) and the §18 scenarios drive. Nothing here performs I/O.

What every fake shares (:class:`FakePort`): the injected ``Clock`` (every timestamp, SPEC §12 §14),
the shared :class:`~nour.core.ports.CallLog` (every side-effecting method records itself with the
``PortCall`` it was given, so ``CallLog.without_audit() == []`` is the §16 coverage assertion; a
call made without a ``PortCall`` is recorded as unaudited and then refused with ``TypeError``, so
the world stays untouched and the coverage report can still name it), the §8 dry-run rule
(``call.dry_run`` records the call and holds back every send and spend: a ``.dry_run_*`` list
instead of ``.sent`` / ``.auths`` / ``.requests``; protective effects — a card freeze, a secrets
revocation — are not sends or spends and apply under dry run too, see ``card.py`` and
``secrets.py``) and ``fail_next(n, exc)``, which makes the next *n* calls raise so outage paths
(SPEC §12 incident table) are testable.

:func:`default_fakes` wires all of them in one call from the real ``NourConfig``: the owner line
and one WhatsApp line per coat, the coat and owner mailboxes, one card per budget holder that has
a ``spend_tiers.monthly_cap``, a ``ScriptedModel`` per model role (vendors ``fake_a`` for primary
and critic, ``fake_b`` for fallback and auditor, so the different-vendor rule of SPEC §4 §12
holds; the ids satisfy the config vendor validator ``^[a-z][a-z0-9_]+$``), and a secret under
every desk prefix (SPEC §13).

Text rules (SPEC §2 §11): a fake never *creates* a ``SafeStr``. Text that arrives from the world
(``deliver``, ``notify``, ``reply``) is plain ``str``; text Nour emits reaches a fake already
minted by ``LeakGuard`` and is stored as it came. The one fake that answers with text of its own,
``FakePrivateModel``, is handed a ``LeakGuard`` to mint it.

The base class is defined before the submodule imports because the submodules import it from
this package (``from nour.fakes import FakePort``); Python resolves that from the partially
initialised package, so the imports below must stay after the class.
"""

# ruff: noqa: E402  (the shared base class must precede the submodule imports; see the docstring)

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nour.core.clock import Clock
from nour.core.ports import CallLog, PortCall


class FakePort:
    """What every fake shares: the ``CallLog``, the ``Clock``, ``fail_next`` and ``_record``.

    ``call_log=None`` builds a private log on the given clock (a fake used alone in a unit test);
    ``clock=None`` reads the log's clock (which falls back to the process clock set by
    ``set_process_clock``). ``port_name`` is the ``RecordedCall.port`` label.
    """

    port_name: str = "port"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        if call_log is not None and not isinstance(call_log, CallLog):
            raise TypeError(f"{type(self).__name__} takes a CallLog, got {type(call_log).__name__}")
        if call_log is None:
            call_log = CallLog(clock)
        self._call_log = call_log
        self._clock: Clock = clock if clock is not None else call_log.clock
        self._failures: list[type[BaseException]] = []

    @property
    def call_log(self) -> CallLog:
        """The shared log this fake records into."""
        return self._call_log

    @property
    def clock(self) -> Clock:
        """The clock every timestamp this fake produces comes from (SPEC §12)."""
        return self._clock

    # ----- scripted failures

    def fail_next(self, n: int, exc: type[BaseException] = RuntimeError) -> None:
        """Make the next ``n`` side-effecting calls raise ``exc`` (SPEC §12: provider outage paths).

        Exactly ``n`` calls raise, then the fake behaves normally again; ``exc`` must accept one
        message argument. ``n == 0`` is a no-op.
        """
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("fail_next takes a non-negative count")
        if not (isinstance(exc, type) and issubclass(exc, BaseException)):
            raise TypeError("fail_next takes an exception class")
        self._failures.extend([exc] * n)

    @property
    def failures_pending(self) -> int:
        """How many scripted failures are still queued."""
        return len(self._failures)

    def _maybe_fail(self, method: str) -> None:
        if self._failures:
            exc = self._failures.pop(0)
            raise exc(f"{type(self).__name__}.{method}: scripted failure (fail_next)")

    # ----- recording

    def _record(self, method: str, call: PortCall | None, **args: Any) -> None:
        """Append one ``RecordedCall`` for the ``PortCall`` a side-effecting method took.

        ``call=None`` is the unaudited case: the attempt is recorded (so
        ``CallLog.without_audit()`` can name it) and then refused with ``TypeError`` before any
        effect happens; a wrong type is refused without being recorded (SPEC §12 §16: a port
        call without an audit id is a type error).
        """
        if call is not None and not isinstance(call, PortCall):
            raise TypeError(
                f"{type(self).__name__}.{method} takes a PortCall, got {type(call).__name__}"
            )
        self._call_log.record(self.port_name, method, call, **args)
        if call is None:
            raise TypeError(
                f"{type(self).__name__}.{method} takes a PortCall; the unaudited attempt was "
                "recorded and refused (SPEC §16: every side effect carries an audit id)"
            )


from nour.config.schema import NourConfig
from nour.core.leakguard import LeakGuard
from nour.core.ports import ModelRole, PortSet
from nour.fakes.bank import FakeBankFeed
from nour.fakes.card import FakeCardIssuer
from nour.fakes.mailbox import FakeCoatMailbox, FakeMailbox, FakeOwnerMailbox, canonical_address
from nour.fakes.model import FakePrivateModel, ModelPolicy, ScriptedModel
from nour.fakes.objects import FakeObjectStorage
from nour.fakes.phone import FakePhoneBody
from nour.fakes.policies import SanePolicy
from nour.fakes.reserved import FakeAds, FakeCalendar, FakeSandbox, FakeTelephony
from nour.fakes.second import FakeSecondChannel
from nour.fakes.secrets import DEFAULT_SECRET_NAMES, FakeSecrets
from nour.fakes.stt import FakeStt, FakeTts
from nour.fakes.vector import FakeVectorIndex
from nour.fakes.whatsapp import OWNER_LINE, FakeWhatsApp

OWNER_LINE_NUMBER = "+971500000000"
"""Nour's own number on the owner thread (the ``"owner"`` line); the owner's number is the sender."""

DEFAULT_OWNER_MAILBOXES: tuple[str, ...] = ("owner@personal.example", "owner@company.example")
"""The owner's personal and company mailboxes the Assistant reads through ``OwnerMailboxPort``."""

DEFAULT_SECOND_CHANNEL_ADDRESS = "owner@second-channel.example"
"""Where the second channel is bound (SPEC §6: an e-mail reply or the desktop app). It is never
one of the mailboxes Nour reads: a confirmation must come from a channel she does not hold."""

FAKE_VENDOR_A = "fake_a"
FAKE_VENDOR_B = "fake_b"
FAKE_VENDOR_PRIVATE = "fake_private"
"""Fake vendor ids: adapter-registry keys (``^[a-z][a-z0-9_]+$``, ``nour/config/schema.py``), so a
``ModelEndpoint`` or a ``model_cost`` row typed against the config accepts them (DESIGN §6 names
them ``fake-a``/``fake-b``; the hyphen never passes the validator, so the underscore form landed)."""


@dataclass
class FakeSet(PortSet):
    """``PortSet`` with every field narrowed to its fake, for test access (DESIGN §3.9).

    The field order and ``__post_init__`` (mailbox kinds) are ``PortSet``'s; ``for_desk`` returns
    the usual narrowed copy (``owner_mail=None`` and ``private_model=None`` for the Operator and
    the governance process), typed as ``PortSet``; the two fields are optional here for that
    reason, exactly as on ``PortSet``.
    """

    whatsapp: FakeWhatsApp
    coat_mail: FakeCoatMailbox
    owner_mail: FakeOwnerMailbox | None
    phone: FakePhoneBody
    card: FakeCardIssuer
    stt: FakeStt
    tts: FakeTts
    models: dict[ModelRole, ScriptedModel]
    private_model: FakePrivateModel | None
    secrets: FakeSecrets
    objects: FakeObjectStorage
    bank: FakeBankFeed
    second: FakeSecondChannel
    vector: FakeVectorIndex
    calendar: FakeCalendar
    telephony: FakeTelephony
    sandbox: FakeSandbox
    ads: FakeAds
    call_log: CallLog

    def fakes(self) -> dict[str, FakePort]:
        """Every ``FakePort`` in the set by field name (the model map expanded by role); a
        member a ``for_desk`` narrowing dropped (``None``) is left out."""
        out: dict[str, FakePort] = {}
        for name in (
            "whatsapp",
            "coat_mail",
            "owner_mail",
            "phone",
            "card",
            "stt",
            "tts",
            "private_model",
            "secrets",
            "objects",
            "bank",
            "second",
            "vector",
            "calendar",
            "telephony",
            "sandbox",
            "ads",
        ):
            member = getattr(self, name)
            if isinstance(member, FakePort):
                out[name] = member
        for role, model in self.models.items():
            out[f"models[{role.value}]"] = model
        return out


def default_fakes(
    clock: Clock,
    cfg: NourConfig,
    *,
    policy: ModelPolicy | None = None,
    owner_number: str,
    passphrase: str,
    guard: LeakGuard | None = None,
    owner_line: str = OWNER_LINE_NUMBER,
    owner_mailboxes: Sequence[str] = DEFAULT_OWNER_MAILBOXES,
    second_channel_address: str = DEFAULT_SECOND_CHANNEL_ADDRESS,
) -> FakeSet:
    """Wire every fake in one call (DESIGN §3.9 §6).

    Issues one card per budget holder with a ``spend_tiers.monthly_cap`` (the ``null`` holders
    get no card, so every spend for them is refused: DESIGN §10); registers the ``"owner"`` line
    and one WhatsApp line per coat (``coat.identity.whatsapp_line``), the coat mailboxes
    (``coat.identity.email``) and the owner mailboxes; seeds a secret under every desk prefix
    (SPEC §13); binds the second channel to ``second_channel_address``, which must differ from
    every mailbox Nour reads (``ValueError`` otherwise).

    ``passphrase`` is **never stored** (SPEC §6 §13; DESIGN §4d): when a ``guard`` is given its
    fingerprint is registered there, so every fake sink can be scanned for it; without a guard
    the argument is only validated. ``guard`` also mints the private model's summaries; when it
    is ``None`` one is built on the seeded ``governance/leakguard-key`` secret.
    """
    if not isinstance(owner_number, str) or not owner_number.strip():
        raise ValueError("default_fakes needs the owner's number")
    if not isinstance(passphrase, str) or not passphrase.strip():
        raise ValueError("default_fakes needs a passphrase (it is fingerprinted, never stored)")
    call_log = CallLog(clock)

    lines: dict[str, str] = {OWNER_LINE: owner_line}
    coat_mailboxes: list[str] = []
    for coat in cfg.coats.values():
        lines[str(coat.slug)] = coat.identity.whatsapp_line
        coat_mailboxes.append(coat.identity.email)
    owner_boxes = list(owner_mailboxes)
    read_by_nour = {canonical_address(box) for box in (*coat_mailboxes, *owner_boxes)}
    if canonical_address(second_channel_address) in read_by_nour:
        raise ValueError(
            "the second channel must not be a mailbox Nour reads (SPEC §6: a second party)"
        )

    secrets = FakeSecrets(call_log, "")
    for name in DEFAULT_SECRET_NAMES:
        secrets.seed(name)
    if guard is None:
        guard = LeakGuard(secrets.get("governance/leakguard-key"))
    guard.register_plaintext_once(passphrase, "passphrase")

    card = FakeCardIssuer(call_log, clock)
    for holder, cap in cfg.spend_tiers.monthly_cap.items():
        if cap is not None:
            card.issue(holder, cap)

    policy = policy if policy is not None else SanePolicy()
    models: dict[ModelRole, ScriptedModel] = {
        ModelRole.PRIMARY: ScriptedModel(
            FAKE_VENDOR_A, cfg.models.primary.model, policy, call_log=call_log, clock=clock
        ),
        ModelRole.FALLBACK: ScriptedModel(
            FAKE_VENDOR_B, cfg.models.fallback.model, policy, call_log=call_log, clock=clock
        ),
        ModelRole.CRITIC: ScriptedModel(
            FAKE_VENDOR_A, cfg.models.critic.model, policy, call_log=call_log, clock=clock
        ),
        ModelRole.AUDITOR: ScriptedModel(
            FAKE_VENDOR_B, cfg.models.auditor.model, policy, call_log=call_log, clock=clock
        ),
        ModelRole.PRIVATE_TIER2: ScriptedModel(
            FAKE_VENDOR_PRIVATE, "private-1", policy, call_log=call_log, clock=clock
        ),
    }

    return FakeSet(
        whatsapp=FakeWhatsApp(call_log, clock, lines, owner_number=owner_number),
        coat_mail=FakeCoatMailbox(call_log, clock, coat_mailboxes),
        owner_mail=FakeOwnerMailbox(call_log, clock, owner_boxes),
        phone=FakePhoneBody(call_log, clock),
        card=card,
        stt=FakeStt(call_log, clock),
        tts=FakeTts(call_log, clock),
        models=models,
        private_model=FakePrivateModel(guard, call_log, clock),
        secrets=secrets,
        objects=FakeObjectStorage(call_log, clock),
        bank=FakeBankFeed(call_log, clock),
        second=FakeSecondChannel(
            call_log,
            clock,
            address=second_channel_address,
            kill_phrases=cfg.constitution.kill_phrases(),
        ),
        vector=FakeVectorIndex(call_log, clock),
        calendar=FakeCalendar(call_log, clock),
        telephony=FakeTelephony(call_log, clock),
        sandbox=FakeSandbox(call_log, clock),
        ads=FakeAds(call_log, clock),
        call_log=call_log,
    )


__all__ = [
    "DEFAULT_OWNER_MAILBOXES",
    "DEFAULT_SECOND_CHANNEL_ADDRESS",
    "FAKE_VENDOR_A",
    "FAKE_VENDOR_B",
    "FAKE_VENDOR_PRIVATE",
    "OWNER_LINE_NUMBER",
    "FakeAds",
    "FakeBankFeed",
    "FakeCalendar",
    "FakeCardIssuer",
    "FakeCoatMailbox",
    "FakeMailbox",
    "FakeObjectStorage",
    "FakeOwnerMailbox",
    "FakePhoneBody",
    "FakePort",
    "FakePrivateModel",
    "FakeSandbox",
    "FakeSecondChannel",
    "FakeSecrets",
    "FakeSet",
    "FakeStt",
    "FakeTelephony",
    "FakeTts",
    "FakeVectorIndex",
    "FakeWhatsApp",
    "ModelPolicy",
    "ScriptedModel",
    "default_fakes",
]
