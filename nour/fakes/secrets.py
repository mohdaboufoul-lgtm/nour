"""``FakeSecrets``: the secrets manager (DESIGN §3.9 §4a §6; SPEC §12 §13).

One credential set per desk (SPEC §13): a process holds a port bound to its prefix
(``operator/``, ``assistant/``, ``governance/``, ``auditor/``) and ``scoped`` can only narrow, so
``PortSet.for_desk(OperatorToken)`` yields a port from which the vault field key
(``assistant/vault/field-key``) is unreachable (``ScopeViolation``). A prefix is ``""`` (the root)
or ends in ``/``: ``scoped("operator")`` would also cover ``operatorX/…`` and is refused.

The kill switch's ``revoke_all`` (SPEC §12) is scope-restricted like everything else, with one
deliberate exception: the root port and the ``governance/`` port (the process that runs the kill
switch) may revoke any prefix, because the governance process must be able to revoke the desks'
credentials; a desk-scoped port may only revoke prefixes under its own scope, so a compromised
Operator process can never kill the governance LeakGuard key, the stamp keys or the auditor's
credentials (THREAT_REVIEW: availability of the safety processes). After a revocation ``get``
raises ``Revoked`` for every name under a revoked prefix. Revocation is protective, not a spend
or a send, so it applies under ``call.dry_run`` too (SPEC §8 dry run holds back sends and spends);
``rotate`` replaces live credentials and *is* held back under dry run. ``rotate`` bumps a version
and replaces the bytes (SPEC §13 quarterly rotation).

Every scoped view shares one store: values, versions, the revocation list and the ``fail_next``
queue, so a scripted outage on ``fakes.secrets`` reaches the desk views ``for_desk`` hands out.

Values are deterministic bytes derived from the name (``seed``), never anything the owner typed:
the passphrase is never persisted anywhere, this store included (SPEC §6; DESIGN §4d).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from nour.core.clock import Clock
from nour.core.errors import Revoked, ScopeViolation
from nour.core.ports import CallLog, PortCall
from nour.fakes import FakePort

DEFAULT_SECRET_NAMES: tuple[str, ...] = (
    "governance/leakguard-key",
    "governance/webhook-secret",
    "governance/passphrase-fingerprint-key",
    "governance/second-channel-token",
    "operator/whatsapp-token",
    "operator/coat-mailbox-token",
    "operator/card-token",
    "operator/stt-key",
    "operator/stamp-key",
    "assistant/owner-mailbox-oauth",
    "assistant/vault/field-key",
    "assistant/card-token",
    "assistant/stt-key",
    "assistant/stamp-key",
    "auditor/whatsapp-token",
    "auditor/second-channel-token",
)
"""One secret under every prefix of DESIGN §2.2 (what ``default_fakes`` seeds)."""

REVOKE_ANYWHERE_PREFIXES: frozenset[str] = frozenset({"", "governance/"})
"""The ports allowed to revoke outside their own scope: the root and the kill-switch process."""

_SEED_DOMAIN = b"nour-fake-secret:v1:"


def seed_value(name: str, version: int = 1) -> bytes:
    """The deterministic 32-byte value of a seeded secret (AES-256-sized, so the vault field
    key works as is)."""
    return hashlib.sha256(_SEED_DOMAIN + f"{name}#{version}".encode()).digest()


def _check_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise TypeError("a secrets prefix is a str")
    if prefix != "" and not prefix.endswith("/"):
        raise ValueError(f"a secrets prefix is '' or ends with '/', got {prefix!r}")
    return prefix


class _Store:
    """The backing store every scoped view shares (so a revocation or an outage is visible to all)."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.versions: dict[str, int] = {}
        self.revoked: list[str] = []
        self.failures: list[type[BaseException]] = []


class FakeSecrets(FakePort):
    """SecretsPort fake bound to ``prefix``; ``scoped`` narrows only; ``revoke_all`` → ``Revoked``."""

    port_name: str = "secrets"

    def __init__(
        self,
        call_log: CallLog | None = None,
        prefix: str = "",
        *,
        clock: Clock | None = None,
        _store: _Store | None = None,
    ) -> None:
        super().__init__(call_log, clock)
        self.prefix = _check_prefix(prefix)
        self._store = _store if _store is not None else _Store()
        self._failures = self._store.failures  # one fail_next queue for every view of the store

    # ----- inspection

    @property
    def revoked(self) -> list[str]:
        """Every prefix revoked so far, in order (shared by every scoped view)."""
        return self._store.revoked

    def names(self) -> list[str]:
        """The names within this port's scope."""
        return sorted(name for name in self._store.values if name.startswith(self.prefix))

    def version(self, name: str) -> int:
        self._in_scope(name)
        if name not in self._store.values:
            raise KeyError(name)
        return self._store.versions[name]

    def is_revoked(self, name: str) -> bool:
        return any(name.startswith(prefix) for prefix in self._store.revoked)

    # ----- seeding (test setup, not a port method)

    def seed(self, name: str) -> bytes:
        """Store the deterministic value for ``name`` (within scope) and return it."""
        value = seed_value(name)
        self.put(name, value)
        return value

    def put(self, name: str, value: bytes) -> None:
        """Store ``value`` under ``name`` (within scope); version 1, or kept on overwrite."""
        self._in_scope(name)
        if isinstance(value, str) or not isinstance(value, bytes | bytearray):
            raise TypeError("a secret is bytes, never text")
        self._store.values[name] = bytes(value)
        self._store.versions.setdefault(name, 1)

    # ----- the port

    def get(self, name: str) -> bytes:
        """The secret ``name``: ``ScopeViolation`` outside the prefix, ``Revoked`` after a
        revocation of any prefix of it, ``KeyError`` when it was never stored."""
        self._in_scope(name)
        if self.is_revoked(name):
            raise Revoked(f"secret {name!r} was revoked (kill switch)")
        if name not in self._store.values:
            raise KeyError(f"no secret named {name!r}")
        return self._store.values[name]

    def scoped(self, prefix: str) -> FakeSecrets:
        """A view narrowed to ``prefix``; it must extend this port's prefix (never widen) and end
        with ``/`` (``ValueError`` otherwise, so ``operator`` cannot shadow ``operatorX/``)."""
        if not isinstance(prefix, str) or not prefix.startswith(self.prefix):
            raise ScopeViolation(f"cannot widen secrets scope {self.prefix!r} to {prefix!r}")
        _check_prefix(prefix)
        return FakeSecrets(self._call_log, prefix, clock=self._clock, _store=self._store)

    def revoke_all(self, call: PortCall, prefixes: Sequence[str]) -> list[str]:
        """Revoke every secret under each prefix (SPEC §12 kill switch). Returns the prefixes
        revoked. From the root or the ``governance/`` port any prefix may be named; from a
        desk-scoped port every prefix must lie under its own scope (``ScopeViolation``, after
        the attempt is recorded). ``prefixes`` is a sequence, never a bare ``str``. Revocation
        is protective and applies under ``call.dry_run`` too."""
        if isinstance(prefixes, str | bytes):
            raise TypeError("revoke_all takes a sequence of prefixes, not a bare str")
        wanted = [str(p) for p in prefixes]
        self._record("revoke_all", call, prefixes=wanted)
        self._maybe_fail("revoke_all")
        if self.prefix not in REVOKE_ANYWHERE_PREFIXES:
            outside = [p for p in wanted if not p.startswith(self.prefix)]
            if outside:
                raise ScopeViolation(
                    f"secrets scope {self.prefix!r} cannot revoke {outside!r}: only the root or "
                    "the governance/ port revokes across scopes (SPEC §12 kill switch)"
                )
        for prefix in wanted:
            if prefix not in self._store.revoked:
                self._store.revoked.append(prefix)
        return wanted

    def rotate(self, call: PortCall, name: str) -> None:
        """Replace the bytes of ``name`` (within scope) and bump its version; held back under
        ``call.dry_run`` (it replaces live credentials)."""
        self._record("rotate", call, name=name)
        self._maybe_fail("rotate")
        self._in_scope(name)
        if name not in self._store.values:
            raise KeyError(f"no secret named {name!r}")
        if call.dry_run:
            return
        version = self._store.versions[name] + 1
        self._store.versions[name] = version
        self._store.values[name] = seed_value(name, version)

    def _in_scope(self, name: str) -> None:
        if not isinstance(name, str) or not name.startswith(self.prefix):
            raise ScopeViolation(f"{name!r} is outside the secrets scope {self.prefix!r}")
