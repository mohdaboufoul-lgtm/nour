"""Tier 2 values that are not strings (DESIGN §3.3 ``nour/core/tier2.py``; SPEC §6 §10 §11).

SPEC §2: "Nothing from Tier 2 (vault) is ever written into her memory, logs or prompts; only
references and hashes." SPEC §10: "No chat, prompt or memory ever carries the full value, so an
injected instruction cannot exfiltrate it." This module is the *primary* control for that rule,
by type (DESIGN §4b); ``nour/core/leakguard.py`` is the value-based defence in depth.

* :class:`SecretRef` is what the model, briefs, memory and logs may hold: a ``vault://`` uri, the
  last four characters and a keyed hash of the content (never the content).
* :class:`Tier2Value` is the decrypted value in flight. It is **not** a ``str``: every dunder that
  could turn it into text or copy it (``__str__``, ``__repr__``, ``__format__``, ``__eq__``,
  ``__hash__``, ``__iter__``, ``__len__``, pickling, copying, pydantic schema generation, JSON
  hooks) raises :class:`~nour.core.errors.Tier2LeakError`. ``nour/core/hashing.py`` refuses it
  through the ``__tier2__`` class marker. The only egress is :meth:`Tier2Value.write_into`, which
  decrypts straight into a :class:`RenderSink` under a :class:`RenderWitness` and returns the
  number of characters written, never the text.
* :class:`RenderWitness` is the proof that a reveal happens inside the document renderer (or the
  phase-3 private model path).

Seals, for the engineers who match them
---------------------------------------
Two module-private sentinels, compared by identity (``is``), gate the two constructors:

* ``_VALUE_SEAL`` — the ``Tier2Value`` constructor: ``ref, ciphertext, decrypt, _seal=_VALUE_SEAL``.
  ``nour/vault/store.py`` imports it (``from nour.core.tier2 import _VALUE_SEAL``) and is the
  only module that calls the constructor (``tests/unit/test_walls.py``).
* ``_WITNESS_SEAL`` — the ``RenderWitness`` constructor: ``purpose, approval_id, sink_id,
  _seal=_WITNESS_SEAL``. ``nour/vault/renderer.py`` imports it and is the only module that
  calls the constructor.

A wrong seal raises ``Tier2LeakError`` before anything is stored. Both classes are final
(``__init_subclass__`` refuses subclasses, so no subclass can re-enable ``__str__`` or skip the
seal), slotted, immutable and unpicklable. Every witness the sealed constructor builds is also
recorded in a module-private ``weakref.WeakSet``, and ``write_into`` accepts a witness only if it
is in that set: an object conjured with ``object.__new__(RenderWitness)`` plus
``object.__setattr__`` on the four slots has the right type and populated slots but was never
minted, so it opens nothing. (The ``__weakref__`` slot exists for that registry.) Tests that need
a genuine witness call the constructor through an alias with ``_WITNESS_SEAL``.

What Python cannot seal (DESIGN §10)
------------------------------------
A ``Tier2Value`` must hold its ciphertext and its decrypt callable somewhere, and CPython lets any
caller read a slot (``value._decrypt(value._ciphertext)``, ``object.__getattribute__``,
``gc.get_referents``). Nothing in-process can stop a hostile engineer; the threat model is the
model and the code paths downstream of it (SPEC §13). The compensating control is the AST wall:
``tests/unit/test_walls.py`` confines every ``._decrypt(`` call and every ``_decrypt`` /
``_ciphertext`` attribute reference to this file, so no module in ``nour/`` can grow such a path.
"""

from __future__ import annotations

import re
import secrets
import weakref
from collections.abc import Callable
from typing import Any, ClassVar, Protocol, final, runtime_checkable

from pydantic import BaseModel, field_validator

from nour.core.errors import Tier2LeakError
from nour.core.types import CoatId, Hash, Ulid

_VALUE_SEAL: object = object()
"""Module-private seal for ``Tier2Value``: only ``nour/vault/store.py`` passes it."""

_WITNESS_SEAL: object = object()
"""Module-private seal for ``RenderWitness``: only ``nour/vault/renderer.py`` passes it."""

OWNER_ENTITY = "owner"
"""SPEC §11: the vault has one folder per entity, the owner personally and each company; the
owner's folder is not a coat, so ``SecretRef.coat_id`` is ``None`` for it."""

_URI_RE = re.compile(
    r"^vault://(?P<entity>[a-z0-9][a-z0-9-]*)/(?P<path>[A-Za-z0-9_\-/]+)#(?P<field>[A-Za-z0-9_]+)$"
)
_BANK_PATH = "banking/receiving"
_HMAC_PREFIX = "hmac:"
_HEX_DIGEST_LEN = 64
_LAST4_MAX = 4


# --------------------------------------------------------------------------- SecretRef


class SecretRef(BaseModel, frozen=True):
    """§10: what the model, briefs, memory and logs may hold instead of a Tier 2 value.

    ``uri`` is ``vault://<entity>/<path>#<field>`` (``vault://buzz-avenue/banking/receiving#iban``),
    ``last4`` the last characters SPEC §10 allows briefs and logs to show, ``content_fp`` the
    *keyed* hash (``"hmac:<hex>"``, ``hashing.keyed_hash`` with the LeakGuard key) that audit rows
    store; a plain ``sha256:`` hash is refused because the IBAN space is brute-forceable (§10).
    """

    uri: str
    last4: str
    content_fp: Hash

    @field_validator("uri")
    @classmethod
    def _uri_shape(cls, value: str) -> str:
        if not _URI_RE.match(value):
            raise ValueError("SecretRef.uri must look like vault://<entity>/<path>#<field>")
        return value

    @field_validator("last4")
    @classmethod
    def _last4_shape(cls, value: str) -> str:
        if not value or len(value) > _LAST4_MAX or not value.isalnum():
            raise ValueError("SecretRef.last4 is 1 to 4 alphanumeric characters")
        return value

    @field_validator("content_fp")
    @classmethod
    def _keyed_only(cls, value: str) -> Hash:
        digest = value[len(_HMAC_PREFIX) :]
        if (
            not value.startswith(_HMAC_PREFIX)
            or len(digest) != _HEX_DIGEST_LEN
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise ValueError(
                "SecretRef.content_fp must be a keyed hash: 'hmac:<64 hex>' (SPEC §10)"
            )
        return Hash(value)

    @property
    def entity(self) -> str:
        """The vault folder: a coat slug or ``"owner"``."""
        match = _URI_RE.match(self.uri)
        assert match is not None  # validated at construction
        return match.group("entity")

    @property
    def coat_id(self) -> CoatId | None:
        """The coat this value belongs to; ``None`` for the owner's personal folder."""
        entity = self.entity
        return None if entity == OWNER_ENTITY else CoatId(entity)

    @property
    def path(self) -> str:
        """``"banking/receiving#iban"``: everything after the entity."""
        match = _URI_RE.match(self.uri)
        assert match is not None
        return f"{match.group('path')}#{match.group('field')}"

    @property
    def field(self) -> str:
        """The fragment: ``"iban"``."""
        return self.uri.rsplit("#", 1)[1]

    def placeholder(self) -> str:
        """§10 IBAN templating: ``{{bank.<coat>.<field>}}`` for receiving-bank fields
        (``banking/receiving#<field>``); any other secret renders as
        ``{{vault.<entity>.<path segments joined by dots>}}`` (``nour/vault/placeholders.py``
        parses both grammars back to a uri)."""
        match = _URI_RE.match(self.uri)
        assert match is not None
        entity, path, field = match.group("entity"), match.group("path"), match.group("field")
        if path == _BANK_PATH:
            return f"{{{{bank.{entity}.{field}}}}}"
        dotted = ".".join(path.split("/"))
        return f"{{{{vault.{entity}.{dotted}.{field}}}}}"

    def __str__(self) -> str:
        """``vault://buzz-avenue/banking/receiving#iban (…3456)``: safe for briefs and logs."""
        return f"{self.uri} (…{self.last4})"


# --------------------------------------------------------------------------- RenderSink / RenderWitness


@runtime_checkable
class RenderSink(Protocol):
    """Where :meth:`Tier2Value.write_into` writes: the renderer's buffer, never a str it returns."""

    def write(self, chunk: str) -> None: ...


@final
class RenderWitness:
    """§10 §11: proof that a reveal happens inside the document renderer or the phase-3 private model path.
    Constructor takes a module-private seal; tests/unit/test_walls.py asserts the constructor call appears
    only in nour/vault/renderer.py.

    ``purpose`` names the rendering ("invoice:INV-0042"), ``approval_id`` the approval that
    released a non-bank Tier 2 fill (``None`` for the receiving-bank exception of SPEC §10),
    ``sink_id`` the object-storage key the output goes to, ``nonce`` a fresh random token so two
    witnesses are never equal. Immutable, final, unpicklable, and registered at construction so
    ``Tier2Value.write_into`` can tell a minted witness from a look-alike.
    """

    __slots__ = ("purpose", "approval_id", "sink_id", "nonce", "__weakref__")
    purpose: str
    approval_id: Ulid | None
    sink_id: str
    nonce: str

    def __init__(
        self, purpose: str, approval_id: Ulid | None, sink_id: str, *, _seal: object
    ) -> None:
        if _seal is not _WITNESS_SEAL:
            raise Tier2LeakError(
                "RenderWitness may only be constructed by nour.vault.renderer.DocumentRenderer"
            )
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("RenderWitness.purpose must name the rendering")
        if not isinstance(sink_id, str) or not sink_id.strip():
            raise ValueError("RenderWitness.sink_id must name the sink")
        if approval_id is not None and not isinstance(approval_id, str):
            raise TypeError("RenderWitness.approval_id is a Ulid or None")
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "approval_id", approval_id)
        object.__setattr__(self, "sink_id", sink_id)
        object.__setattr__(self, "nonce", secrets.token_hex(16))
        _MINTED_WITNESSES.add(self)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("RenderWitness is final: a subclass could skip the seal")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("RenderWitness is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("RenderWitness is immutable")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("RenderWitness cannot be pickled")

    def __copy__(self) -> RenderWitness:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> RenderWitness:
        return self

    def __repr__(self) -> str:
        return (
            f"<RenderWitness purpose={self.purpose!r} sink_id={self.sink_id!r} "
            f"approval_id={self.approval_id!r}>"
        )


_MINTED_WITNESSES: weakref.WeakSet[RenderWitness] = weakref.WeakSet()
"""Every witness the sealed constructor built and that is still alive (see the module docstring)."""


def _is_complete_witness(witness: object) -> bool:
    """Exact type (the class is final), every slot populated, and minted by the constructor: a
    witness assembled with ``object.__new__`` has the first two properties and not the third."""
    if type(witness) is not RenderWitness:
        return False
    if not all(hasattr(witness, slot) for slot in ("purpose", "approval_id", "sink_id", "nonce")):
        return False
    return witness in _MINTED_WITNESSES


# --------------------------------------------------------------------------- Tier2Value


def _refuse(what: str) -> Tier2LeakError:
    return Tier2LeakError(
        f"Tier2Value cannot be {what}: a Tier 2 value never becomes text outside "
        "Tier2Value.write_into(RenderWitness, RenderSink) (SPEC §2 §11)"
    )


@final
class Tier2Value:
    """§6 §11: NOT a str. __str__, __repr__, __format__, __eq__, __hash__, __iter__, __len__, __reduce__,
    __getstate__ and pydantic schema generation all raise Tier2LeakError. The only egress is write_into().

    Holds the :class:`SecretRef`, the ciphertext bytes and the decrypt callable the vault's
    ``FieldCipher`` bound to this value; the plaintext exists only inside :meth:`write_into`,
    between the decrypt call and the sink's ``write``. ``__tier2__`` is the class marker
    ``nour/core/hashing.py`` checks so ``canonical_json`` refuses it wherever it sits.
    """

    __slots__ = ("_ref", "_ciphertext", "_decrypt")
    __tier2__: ClassVar[bool] = True
    _ref: SecretRef
    _ciphertext: bytes
    _decrypt: Callable[[bytes], str]

    def __init__(
        self,
        ref: SecretRef,
        ciphertext: bytes,
        decrypt: Callable[[bytes], str],
        *,
        _seal: object,
    ) -> None:
        if _seal is not _VALUE_SEAL:
            raise Tier2LeakError(
                "Tier2Value may only be constructed by nour.vault.store.VaultStore"
            )
        if not isinstance(ref, SecretRef):
            raise TypeError("Tier2Value.ref must be a SecretRef")
        if isinstance(ciphertext, str) or not isinstance(ciphertext, bytes | bytearray):
            raise TypeError("Tier2Value takes ciphertext bytes, never text")
        if not ciphertext:
            raise ValueError("Tier2Value ciphertext is empty")
        if not callable(decrypt):
            raise TypeError("Tier2Value.decrypt must be callable")
        object.__setattr__(self, "_ref", ref)
        object.__setattr__(self, "_ciphertext", bytes(ciphertext))
        object.__setattr__(self, "_decrypt", decrypt)

    # ----- what may be known about the value

    @property
    def ref(self) -> SecretRef:
        return self._ref

    @property
    def last4(self) -> str:
        return self._ref.last4

    # ----- the single egress

    def write_into(self, witness: RenderWitness, sink: RenderSink) -> int:
        """Decrypt into ``sink`` under ``witness``; returns the number of characters written.

        Raises ``Tier2LeakError`` unless ``witness`` is a complete :class:`RenderWitness` (exact
        type, every slot set, minted by the sealed constructor) and ``TypeError`` unless ``sink``
        provides ``write(chunk)``. The plaintext is dropped as soon as the sink has it; it is
        never returned.
        """
        if not _is_complete_witness(witness):
            raise Tier2LeakError(
                "Tier2Value.write_into needs a RenderWitness constructed by the document renderer"
            )
        if not isinstance(sink, RenderSink):
            raise TypeError("Tier2Value.write_into needs a RenderSink with write(chunk: str)")
        plaintext = self._decrypt(self._ciphertext)
        if not isinstance(plaintext, str):
            raise TypeError("decrypt must return str")
        try:
            sink.write(plaintext)
            return len(plaintext)
        finally:
            del plaintext

    # ----- structural refusals: nothing below ever produces text or a copy

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("Tier2Value is final: a subclass could re-enable __str__")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Tier2Value is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Tier2Value is immutable")

    def __str__(self) -> str:
        raise _refuse("rendered with str()")

    def __repr__(self) -> str:
        raise _refuse("rendered with repr()")

    def __format__(self, format_spec: str) -> str:
        raise _refuse("formatted (f-string, format(), %)")

    def __bytes__(self) -> bytes:
        raise _refuse("converted to bytes")

    def __eq__(self, other: object) -> bool:
        raise _refuse("compared")

    def __ne__(self, other: object) -> bool:
        raise _refuse("compared")

    def __hash__(self) -> int:
        raise _refuse("hashed")

    def __iter__(self) -> Any:
        raise _refuse("iterated")

    def __len__(self) -> int:
        raise _refuse("measured")

    def __contains__(self, item: object) -> bool:
        raise _refuse("searched")

    def __getitem__(self, item: object) -> Any:
        raise _refuse("indexed")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise _refuse("pickled")

    def __reduce_ex__(self, protocol: object) -> str | tuple[object, ...]:
        raise _refuse("pickled")

    def __getstate__(self) -> object:
        raise _refuse("pickled")

    def __setstate__(self, state: object) -> None:
        raise _refuse("unpickled")

    def __copy__(self) -> Tier2Value:
        raise _refuse("copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Tier2Value:
        raise _refuse("copied")

    def __json__(self) -> object:
        raise _refuse("serialised to JSON")

    def for_json(self) -> object:
        raise _refuse("serialised to JSON")

    def to_json(self) -> object:
        raise _refuse("serialised to JSON")

    def __html__(self) -> str:
        raise _refuse("rendered as HTML")

    def __fspath__(self) -> str:
        raise _refuse("used as a path")

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        """A pydantic model that declares a Tier2Value field fails at class creation."""
        raise _refuse("a pydantic field")
