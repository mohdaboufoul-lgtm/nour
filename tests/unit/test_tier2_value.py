"""nour/core/tier2.py (DESIGN §3.3, §4b, §7.3; SPEC §2 §6 §10 §11): SecretRef, Tier2Value, RenderWitness.

Proves (MODULES.md "core"): ``Tier2Value`` cannot become a string by any dunder, by pydantic,
``json.dumps``, pickle, copy, f-string or %-format; ``write_into`` with a non-witness raises;
the sealed constructors refuse a wrong seal; ``SecretRef`` carries references only.

The ``Tier2Value`` and ``RenderWitness`` constructor calls are confined by ``tests/unit/test_walls.py``
to ``nour/vault/store.py`` and ``nour/vault/renderer.py``; this file reaches them through aliases
with the module-private seals (to prove the seals), which the walls allow here and nowhere else.
A witness assembled with ``object.__new__`` is built here only to prove it is refused.
"""

from __future__ import annotations

import copy
import io
import json
import os
import pickle
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from nour.core import tier2
from nour.core.errors import NourError, Tier2LeakError
from nour.core.hashing import canonical_json, content_hash, keyed_hash
from nour.core.leakguard import LeakGuard
from nour.core.tier2 import RenderSink, RenderWitness, SecretRef, Tier2Value
from nour.core.types import CoatId, Hash, Ulid

_value_ctor = Tier2Value  # the constructor call is confined to nour/vault/store.py (AST walls)
_witness_ctor = RenderWitness  # the constructor call is confined to nour/vault/renderer.py
_VALUE_SEAL = tier2._VALUE_SEAL
_WITNESS_SEAL = tier2._WITNESS_SEAL

KEY = b"leakguard-key-for-tests-0123456789"
IBAN = "AE070331234567890123456"
REF = SecretRef(
    uri="vault://buzz-avenue/banking/receiving#iban",
    last4=IBAN[-4:],
    content_fp=keyed_hash(KEY, IBAN),
)


def _toy_cipher(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)


def _decrypt(blob: bytes) -> str:
    return _toy_cipher(blob).decode("utf-8")


def make_value(plain: str = IBAN, ref: SecretRef = REF) -> Tier2Value:
    return _value_ctor(ref, _toy_cipher(plain.encode("utf-8")), _decrypt, _seal=_VALUE_SEAL)


def make_witness(
    purpose: str = "invoice:INV-0042",
    approval_id: Ulid | None = None,
    sink_id: str = "objects/buzz-avenue/INV-0042.pdf",
) -> RenderWitness:
    """A genuine witness: the sealed constructor through its alias (the renderer's path)."""
    return _witness_ctor(purpose, approval_id, sink_id, _seal=_WITNESS_SEAL)


def forge_witness(source: RenderWitness | None = None) -> RenderWitness:
    """What an attacker can build without the seal: the right type with every slot populated
    (copied from a real witness when given), never registered as minted."""
    witness = object.__new__(RenderWitness)
    for slot in ("purpose", "approval_id", "sink_id", "nonce"):
        value = (
            getattr(source, slot) if source is not None else {"approval_id": None}.get(slot, "x")
        )
        object.__setattr__(witness, slot, value)
    return witness


class Buffer:
    """A RenderSink that keeps what was written, so the test can see the plaintext arrived."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, chunk: str) -> None:
        self.chunks.append(chunk)


# --------------------------------------------------------------------------- construction


def test_value_needs_the_seal() -> None:
    blob = _toy_cipher(IBAN.encode())
    with pytest.raises(TypeError):
        _value_ctor(REF, blob, _decrypt)  # type: ignore[call-arg]
    with pytest.raises(Tier2LeakError):
        _value_ctor(REF, blob, _decrypt, _seal=object())
    with pytest.raises(Tier2LeakError):
        _value_ctor(REF, blob, _decrypt, _seal=_WITNESS_SEAL)  # the other seal is not this seal
    with pytest.raises(Tier2LeakError):
        _value_ctor(REF, blob, _decrypt, _seal=None)
    assert issubclass(Tier2LeakError, NourError)


def test_value_validates_its_parts() -> None:
    with pytest.raises(TypeError):
        _value_ctor("vault://x/y#z", b"\x01", _decrypt, _seal=_VALUE_SEAL)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _value_ctor(REF, IBAN, _decrypt, _seal=_VALUE_SEAL)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _value_ctor(REF, b"", _decrypt, _seal=_VALUE_SEAL)
    with pytest.raises(TypeError):
        _value_ctor(REF, b"\x01", "not callable", _seal=_VALUE_SEAL)  # type: ignore[arg-type]


def test_value_is_not_a_str_and_is_marked_for_hashing() -> None:
    value = make_value()
    assert not isinstance(value, str)
    assert not isinstance(value, bytes)
    assert Tier2Value.__tier2__ is True
    assert value.ref is REF
    assert value.last4 == "3456"
    with pytest.raises(Tier2LeakError):
        canonical_json(value)
    with pytest.raises(Tier2LeakError):
        content_hash({"iban": value})
    with pytest.raises(Tier2LeakError):
        content_hash([1, [2, [value]]])


def test_value_is_immutable_slotted_and_final() -> None:
    value = make_value()
    with pytest.raises(AttributeError):
        value.extra = 1
    with pytest.raises(AttributeError):
        value._ref = REF
    with pytest.raises(AttributeError):
        del value._ref
    with pytest.raises(AttributeError):
        _ = value.__dict__
    assert Tier2Value.__slots__ == ("_ref", "_ciphertext", "_decrypt")
    with pytest.raises(TypeError):
        type("Leaky", (Tier2Value,), {"__str__": lambda self: "x"})


# --------------------------------------------------------------------------- every text path raises


def _print(value: object) -> None:
    print(value, file=io.StringIO())


DUNDER_PATHS: dict[str, Callable[[Tier2Value], Any]] = {
    "str()": str,
    "repr()": repr,
    "f-string": lambda v: f"{v}",
    "%-format": lambda v: "%s" % v,  # noqa: UP031 - the %-path is the point
    "%r-format": lambda v: "%r" % v,  # noqa: UP031
    "str.format": lambda v: "{}".format(v),  # noqa: UP032 - the .format path is the point
    "format()": format,
    "print()": _print,
    "== str": lambda v: v == "x",
    "!= str": lambda v: v != "x",
    "== self": lambda v: v == v,
    "hash()": hash,
    "iter()": iter,
    "len()": len,
    "in": lambda v: "A" in v,
    "index": lambda v: v[0],
    "bytes()": bytes,
    "copy.copy": copy.copy,
    "copy.deepcopy": copy.deepcopy,
    "pickle.dumps": pickle.dumps,
    "__getstate__": lambda v: v.__getstate__(),
    "__reduce__": lambda v: v.__reduce__(),
    "__reduce_ex__": lambda v: v.__reduce_ex__(2),
    "__json__": lambda v: v.__json__(),
    "for_json": lambda v: v.for_json(),
    "to_json": lambda v: v.to_json(),
    "__html__": lambda v: v.__html__(),
    "os.fspath": os.fspath,
    "list()": list,
    "set membership": lambda v: v in {1, 2},
}


@pytest.mark.parametrize("path", sorted(DUNDER_PATHS))
def test_every_dunder_raises_tier2_leak_error(path: str) -> None:
    value = make_value()
    with pytest.raises(Tier2LeakError):
        DUNDER_PATHS[path](value)


OTHER_PATHS: dict[str, Callable[[Tier2Value], Any]] = {
    "json.dumps": json.dumps,
    "json.dumps(default=str)": lambda v: json.dumps(v, default=str),
    "json.dumps in dict": lambda v: json.dumps({"iban": v}),
    "str.join": lambda v: "".join([v]),  # type: ignore[list-item]
    "str concat": lambda v: "iban " + v,  # type: ignore[operator]
    "int()": lambda v: int(v),  # type: ignore[call-overload]
    "sorted()": lambda v: sorted([v, v]),
    "TypeAdapter": lambda v: TypeAdapter(Tier2Value).validate_python(v),
    "TypeAdapter(Any).dump_json": lambda v: TypeAdapter(Any).dump_json(v),  # type: ignore[arg-type]
}


@pytest.mark.parametrize("path", sorted(OTHER_PATHS))
def test_every_serialiser_raises(path: str) -> None:
    value = make_value()
    with pytest.raises(Exception):  # noqa: B017 - any failure is fine; a string is not
        OTHER_PATHS[path](value)


def test_pydantic_cannot_declare_or_serialise_the_value() -> None:
    with pytest.raises(Tier2LeakError):

        class Holder(BaseModel, frozen=True, arbitrary_types_allowed=True):
            iban: Tier2Value

    class Loose(BaseModel, frozen=True):
        payload: dict[str, Any]

    value = make_value()
    loose = Loose(payload={"iban": value})
    assert loose.payload["iban"] is value  # held as the object, never as text
    dumped = loose.model_dump()
    assert dumped["payload"]["iban"] is value
    with pytest.raises(Exception):  # noqa: B017 - pydantic has no serialiser for it
        loose.model_dump_json()
    with pytest.raises(Exception):  # noqa: B017
        loose.model_dump(mode="json")


def test_exception_messages_never_carry_the_value() -> None:
    value = make_value()
    for path, action in DUNDER_PATHS.items():
        try:
            action(value)
        except Tier2LeakError as exc:
            assert IBAN not in str(exc), path
            assert "3456" not in str(exc), path


# --------------------------------------------------------------------------- the single egress


def test_write_into_decrypts_into_the_sink_and_returns_the_count() -> None:
    value = make_value()
    sink = Buffer()
    witness = make_witness()
    assert isinstance(sink, RenderSink)
    written = value.write_into(witness, sink)
    assert written == len(IBAN)
    assert sink.chunks == [IBAN]
    assert isinstance(written, int)  # never the text
    # the value is reusable: a second render with a second witness works the same way
    again = Buffer()
    assert value.write_into(make_witness(sink_id="objects/other.pdf"), again) == len(IBAN)
    assert again.chunks == [IBAN]


class LooksLikeAWitness:
    """Duck-typed impostor with the same four attributes."""

    purpose = "invoice:INV-0042"
    approval_id = None
    sink_id = "objects/x.pdf"
    nonce = "nonce"


@pytest.mark.parametrize(
    "impostor",
    [
        object(),
        None,
        "RenderWitness",
        LooksLikeAWitness(),
        {"purpose": "p", "approval_id": None, "sink_id": "s", "nonce": "n"},
        object.__new__(RenderWitness),  # half-built: slots never populated
        forge_witness(),  # right type, every slot set, never minted
    ],
    ids=["object", "None", "str", "duck", "dict", "half-built", "forged"],
)
def test_write_into_without_a_witness_raises(impostor: object) -> None:
    value = make_value()
    sink = Buffer()
    with pytest.raises(Tier2LeakError):
        value.write_into(impostor, sink)  # type: ignore[arg-type]
    assert sink.chunks == []


def test_a_witness_assembled_without_the_constructor_is_refused() -> None:
    """object.__new__ + object.__setattr__ passes the type and slot checks; the minted registry
    does not, even when every slot (nonce included) is copied from a genuine witness."""
    value = make_value()
    genuine = make_witness()
    assert value.write_into(genuine, Buffer()) == len(IBAN)
    clone = forge_witness(genuine)
    assert type(clone) is RenderWitness and clone.nonce == genuine.nonce
    sink = Buffer()
    with pytest.raises(Tier2LeakError):
        value.write_into(clone, sink)
    assert sink.chunks == []
    assert tier2._is_complete_witness(genuine) and not tier2._is_complete_witness(clone)


def test_write_into_needs_a_render_sink() -> None:
    value = make_value()

    class NotASink:
        pass

    with pytest.raises(TypeError):
        value.write_into(make_witness(), NotASink())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        value.write_into(make_witness(), io.BytesIO())  # type: ignore[arg-type]


def test_write_into_refuses_a_decrypt_that_returns_bytes() -> None:
    value = _value_ctor(REF, b"\x01\x02", lambda blob: blob, _seal=_VALUE_SEAL)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError):
        value.write_into(make_witness(), Buffer())


def test_leakguard_refuses_the_value_as_text() -> None:
    guard = LeakGuard(KEY)
    value = make_value()
    with pytest.raises(Tier2LeakError):
        guard.safe(value)  # type: ignore[arg-type]
    with pytest.raises(Tier2LeakError):
        guard.scan(value)  # type: ignore[arg-type]
    with pytest.raises(Tier2LeakError):
        guard.safe_mapping({"iban": value})


# --------------------------------------------------------------------------- RenderWitness


def test_witness_needs_the_seal() -> None:
    with pytest.raises(TypeError):
        _witness_ctor("invoice", None, "sink")  # type: ignore[call-arg]
    with pytest.raises(Tier2LeakError):
        _witness_ctor("invoice", None, "sink", _seal=object())
    with pytest.raises(Tier2LeakError):
        _witness_ctor("invoice", None, "sink", _seal=_VALUE_SEAL)


def test_witness_with_the_real_seal() -> None:
    first = _witness_ctor("invoice:INV-1", None, "objects/inv-1.pdf", _seal=_WITNESS_SEAL)
    second = _witness_ctor(
        "invoice:INV-1",
        Ulid("01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        "objects/inv-1.pdf",
        _seal=_WITNESS_SEAL,
    )
    assert first.purpose == "invoice:INV-1" and first.sink_id == "objects/inv-1.pdf"
    assert first.approval_id is None and second.approval_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert first.nonce != second.nonce and len(first.nonce) == 32
    assert first.nonce not in repr(first)
    assert repr(first).startswith("<RenderWitness purpose='invoice:INV-1'")
    assert make_value().write_into(first, Buffer()) == len(IBAN)
    with pytest.raises(ValueError):
        _witness_ctor("  ", None, "sink", _seal=_WITNESS_SEAL)
    with pytest.raises(ValueError):
        _witness_ctor("invoice", None, "", _seal=_WITNESS_SEAL)
    with pytest.raises(TypeError):
        _witness_ctor("invoice", 12, "sink", _seal=_WITNESS_SEAL)  # type: ignore[arg-type]


def test_witness_is_immutable_unpicklable_and_final() -> None:
    witness = make_witness()
    with pytest.raises(AttributeError):
        witness.purpose = "other"
    with pytest.raises(AttributeError):
        del witness.nonce
    with pytest.raises(AttributeError):
        _ = witness.__dict__
    with pytest.raises(TypeError):
        pickle.dumps(witness)
    assert copy.copy(witness) is witness
    assert copy.deepcopy(witness) is witness
    assert RenderWitness.__slots__ == ("purpose", "approval_id", "sink_id", "nonce", "__weakref__")
    with pytest.raises(TypeError):
        type("ForgedWitness", (RenderWitness,), {})


# --------------------------------------------------------------------------- SecretRef


def test_secret_ref_carries_references_only() -> None:
    assert REF.coat_id == CoatId("buzz-avenue")
    assert REF.entity == "buzz-avenue"
    assert REF.path == "banking/receiving#iban"
    assert REF.field == "iban"
    assert REF.placeholder() == "{{bank.buzz-avenue.iban}}"
    assert str(REF) == "vault://buzz-avenue/banking/receiving#iban (…3456)"
    assert IBAN not in repr(REF) and IBAN not in str(REF)
    assert REF.content_fp.startswith("hmac:")
    assert content_hash(REF) == content_hash(REF.model_copy())
    with pytest.raises(ValidationError):
        REF.last4 = "0000"  # type: ignore[misc]


def test_secret_ref_owner_folder_and_general_placeholder() -> None:
    passport = SecretRef(
        uri="vault://owner/identity/passport#number", last4="A123", content_fp=keyed_hash(KEY, "x")
    )
    assert passport.coat_id is None
    assert passport.entity == "owner"
    assert passport.path == "identity/passport#number"
    assert passport.placeholder() == "{{vault.owner.identity.passport.number}}"
    swift = SecretRef(
        uri="vault://buzz-avenue/banking/receiving#swift",
        last4="XXX",
        content_fp=keyed_hash(KEY, "y"),
    )
    assert swift.placeholder() == "{{bank.buzz-avenue.swift}}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uri", "https://buzz-avenue/banking/receiving#iban"),
        ("uri", "vault://buzz-avenue/banking/receiving"),  # no field fragment
        ("uri", "vault://Buzz-Avenue/banking/receiving#iban"),  # slugs are lower-case
        ("uri", "vault://buzz-avenue#iban"),
        ("uri", "vault:///banking#iban"),
        ("last4", ""),
        ("last4", "12345"),
        ("last4", "34 6"),
        ("content_fp", "sha256:" + "0" * 64),  # plain digest: brute-forceable (SPEC §10)
        ("content_fp", "hmac:" + "0" * 63),
        ("content_fp", "hmac:" + "g" * 64),
        ("content_fp", "0" * 64),
    ],
)
def test_secret_ref_rejects_bad_parts(field: str, value: str) -> None:
    good: dict[str, Any] = {"uri": REF.uri, "last4": REF.last4, "content_fp": REF.content_fp}
    good[field] = value
    with pytest.raises(ValidationError):
        SecretRef(**good)


def test_secret_ref_accepts_a_hash_newtype() -> None:
    ref = SecretRef(uri=REF.uri, last4="3456", content_fp=Hash("hmac:" + "ab" * 32))
    assert ref.content_fp == "hmac:" + "ab" * 32
