"""nour/core/hashing.py (DESIGN §3.2; SPEC §10 §12): canonical JSON, digests, keyed hashes, chain.

Proves (MODULES.md "core"): canonical_json determinism and the Tier 2 marker raise; chain_hash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from typing import Any

import pytest
from pydantic import BaseModel

from nour.core.clock import DUBAI
from nour.core.errors import Tier2LeakError
from nour.core.hashing import (
    GENESIS_HASH,
    canonical_json,
    chain_hash,
    content_hash,
    hmac_hex,
    keyed_hash,
    sha256_hex,
)
from nour.core.leakguard import LeakGuard
from nour.core.types import ActionTier, DataTier, Desk, Money, Reason, SafeStr

DUBAI_0700 = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)
_GUARD = LeakGuard(b"hashing-test-key-0123456789abcdef")


def _minted(text: str) -> SafeStr:
    """A SafeStr the way every sink gets one: through LeakGuard (the mint stays in leakguard.py)."""
    return _GUARD.safe(text)


class Marked:
    """What ``nour/core/tier2.py`` does on ``Tier2Value``: a class-level ``__tier2__`` marker."""

    __tier2__ = True


class Hostile:
    """A value that raises on any attribute probe (as every Tier2Value dunder does)."""

    def __getattr__(self, name: str) -> Any:
        raise Tier2LeakError("no attribute access")


class Colour(StrEnum):
    RED = "red"


class Level(IntEnum):
    LOW = 1


class Plain(Enum):
    X = "x-value"


class Entry(BaseModel, frozen=True):
    amount: Money
    reason: Reason
    when: datetime
    tier: ActionTier
    notes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Row:
    name: str
    amount: Money


# --------------------------------------------------------------------------- canonical_json


def test_sorted_keys_compact_separators_utf8() -> None:
    assert (
        canonical_json({"b": 1, "a": [1, 2, {"d": 0, "c": None}]})
        == b'{"a":[1,2,{"c":null,"d":0}],"b":1}'
    )
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})
    assert canonical_json("مرحبا") == '"مرحبا"'.encode()
    assert canonical_json(True) == b"true" and canonical_json(None) == b"null"
    assert canonical_json(1.5) == b"1.5"
    assert canonical_json((1, "two")) == b'[1,"two"]'
    assert canonical_json([]) == b"[]" and canonical_json({}) == b"{}"


def test_determinism_across_calls_and_equal_objects() -> None:
    obj = {"when": DUBAI_0700, "amount": Money.aed("12.34"), "tags": {"b", "a"}}
    again = {"tags": {"a", "b"}, "amount": Money(fils=1234), "when": DUBAI_0700.astimezone(UTC)}
    assert canonical_json(obj) == canonical_json(again)
    assert content_hash(obj) == content_hash(again)


def test_money_is_fils_decimal_is_str() -> None:
    assert canonical_json(Money.aed("1.50")) == b"150"
    assert canonical_json({"amount": Money.aed(3000)}) == b'{"amount":300000}'
    assert canonical_json(Decimal("1.50")) == b'"1.50"'
    assert canonical_json({"d": Decimal("-0.5")}) == b'{"d":"-0.5"}'


def test_datetimes_become_utc_isoformat_dates_isoformat() -> None:
    assert canonical_json(DUBAI_0700) == b'"2026-10-05T03:00:00+00:00"'
    assert canonical_json(DUBAI_0700) == canonical_json(datetime(2026, 10, 5, 3, 0, tzinfo=UTC))
    assert canonical_json(date(2026, 10, 5)) == b'"2026-10-05"'
    assert canonical_json(time(7, 30)) == b'"07:30:00"'
    with pytest.raises(ValueError, match="naive"):
        canonical_json(datetime(2026, 10, 5, 7, 0))  # noqa: DTZ001 - naive on purpose


def test_safe_str_and_reason_hash_as_plain_text() -> None:
    assert canonical_json(_minted("hello")) == canonical_json("hello") == b'"hello"'
    assert canonical_json(Reason("Paid the supplier.")) == b'"Paid the supplier."'
    assert canonical_json({"text": _minted("x")}) == b'{"text":"x"}'


def test_enums_become_values() -> None:
    assert canonical_json(Desk.OPERATOR) == b'"operator"'
    assert canonical_json(ActionTier.K) == b'"K"'
    assert canonical_json(DataTier.T2) == b"2"
    assert canonical_json(Colour.RED) == b'"red"'
    assert canonical_json(Level.LOW) == b"1"
    assert canonical_json(Plain.X) == b'"x-value"'
    assert (
        canonical_json({Desk.ASSISTANT: 1, Level.LOW: 2, 3: "x"})
        == b'{"1":2,"3":"x","assistant":1}'
    )


def test_bytes_base64_sets_sorted_uuid_str() -> None:
    assert canonical_json(b"\x00\x01") == b'"AAE="'
    assert canonical_json(bytearray(b"abc")) == b'"YWJj"'
    assert canonical_json(memoryview(b"abc")) == b'"YWJj"'
    assert canonical_json({"c", "a", "b"}) == b'["a","b","c"]'
    assert canonical_json(frozenset({3, 1, 2})) == b"[1,2,3]"
    assert canonical_json({(1, 2), (0, 9)}) == b"[[0,9],[1,2]]"
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert canonical_json(uid) == b'"12345678-1234-5678-1234-567812345678"'


def test_pydantic_models_and_dataclasses_walk_fields_keeping_money_rule() -> None:
    entry = Entry(
        amount=Money.aed("12.34"),
        reason=Reason("Paid the supplier."),
        when=DUBAI_0700,
        tier=ActionTier.N,
        notes=("a", "b"),
    )
    assert json.loads(canonical_json(entry)) == {
        "amount": 1234,
        "reason": "Paid the supplier.",
        "when": "2026-10-05T03:00:00+00:00",
        "tier": "N",
        "notes": ["a", "b"],
    }
    assert canonical_json(Row(name="n", amount=Money.aed(1))) == b'{"amount":100,"name":"n"}'
    assert canonical_json([entry, Row("n", Money.zero())]).startswith(b"[{")


@pytest.mark.parametrize(
    "obj",
    [
        Marked(),
        {"nested": Marked()},
        [1, [2, [Marked()]]],
        (Marked(),),
        {
            "model": Entry(
                amount=Money.zero(), reason=Reason("Fine."), when=DUBAI_0700, tier=ActionTier.A
            ),
            "m": Marked(),
        },
    ],
)
def test_tier2_marker_anywhere_raises(obj: Any) -> None:
    with pytest.raises(Tier2LeakError):
        canonical_json(obj)
    with pytest.raises(Tier2LeakError):
        content_hash(obj)


def test_object_that_raises_on_attribute_probe_is_refused() -> None:
    # Built inside the test: pytest's id generation would trip the probe at collection time.
    with pytest.raises(Tier2LeakError):
        canonical_json(Hostile())
    with pytest.raises(Tier2LeakError):
        canonical_json({"ciphertext": [Hostile()]})


def test_instance_level_marker_is_honoured_too() -> None:
    class Plain:
        pass

    carrier = Plain()
    carrier.__tier2__ = True  # type: ignore[attr-defined]
    with pytest.raises(Tier2LeakError):
        canonical_json({"x": carrier})


def test_unknown_objects_and_non_finite_floats_are_refused() -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "AE07 0331 2345 6789 0123 456"  # a repr fallback would leak this

    with pytest.raises(TypeError):
        canonical_json(Opaque())
    with pytest.raises(TypeError):
        canonical_json({"k": object()})
    with pytest.raises(TypeError):
        canonical_json({object(): 1})
    with pytest.raises(ValueError):
        canonical_json(float("nan"))
    with pytest.raises(ValueError):
        canonical_json([float("inf")])


# --------------------------------------------------------------------------- digests


def test_sha256_known_vectors() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_hmac_known_vector_and_empty_key_refused() -> None:
    key, msg = b"key", b"The quick brown fox jumps over the lazy dog"
    assert hmac_hex(key, msg) == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    assert hmac_hex(key, msg) == hmac.new(key, msg, hashlib.sha256).hexdigest()
    with pytest.raises(ValueError):
        hmac_hex(b"", msg)


def test_content_hash_is_prefixed_sha256_of_canonical_json() -> None:
    obj = {"a": Money.aed(1), "b": [DUBAI_0700]}
    digest = content_hash(obj)
    assert digest == "sha256:" + sha256_hex(canonical_json(obj))
    assert digest == "sha256:" + hashlib.sha256(canonical_json(obj)).hexdigest()
    assert isinstance(digest, str) and len(digest) == len("sha256:") + 64
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_keyed_hash_is_prefixed_hmac_and_never_a_plain_digest() -> None:
    key, value = b"leakguard-key", "AE070331234567890123456"
    digest = keyed_hash(key, value)
    assert digest == "hmac:" + hmac_hex(key, value.encode("utf-8"))
    assert digest.startswith("hmac:") and len(digest) == len("hmac:") + 64
    assert keyed_hash(b"other-key", value) != digest
    assert keyed_hash(key, value + "1") != digest
    assert digest.removeprefix("hmac:") != sha256_hex(value.encode())
    assert digest.removeprefix("hmac:") != content_hash(value).removeprefix("sha256:")
    with pytest.raises(ValueError):
        keyed_hash(b"", value)


# --------------------------------------------------------------------------- chain


def test_chain_hash_shape_and_sensitivity() -> None:
    assert GENESIS_HASH == "0" * 64
    first = chain_hash(GENESIS_HASH, b"entry-1")
    assert len(first) == 64 and int(first, 16) >= 0
    assert first == chain_hash(GENESIS_HASH, b"entry-1")
    assert first != chain_hash(GENESIS_HASH, b"entry-2")
    assert first != chain_hash("1" * 64, b"entry-1")
    assert first != sha256_hex(b"entry-1")
    assert first != sha256_hex(GENESIS_HASH.encode() + b"entry-1")
    # Framing is unambiguous: moving bytes between prev and entry changes the hash.
    assert chain_hash("ab", b"cd") != chain_hash("abc", b"d")


def test_chain_links_rows_and_detects_tampering() -> None:
    rows = [{"seq": i, "action": f"a{i}", "amount": Money.aed(i)} for i in range(1, 4)]

    def build(entries: list[dict[str, Any]]) -> list[str]:
        prev, hashes = GENESIS_HASH, []
        for row in entries:
            prev = chain_hash(prev, canonical_json(row))
            hashes.append(prev)
        return hashes

    original = build(rows)
    assert len(set(original)) == 3
    assert build(rows) == original  # verify_chain recomputes and matches
    tampered = [dict(row) for row in rows]
    tampered[1]["amount"] = Money.aed(2000)
    recomputed = build(tampered)
    assert recomputed[0] == original[0]
    assert recomputed[1] != original[1] and recomputed[2] != original[2]
    reordered = build([rows[0], rows[2], rows[1]])
    assert reordered[-1] != original[-1]


def test_hashes_do_not_depend_on_time_zone_of_input() -> None:
    utc_row = {"ts": datetime(2026, 10, 5, 3, 0, tzinfo=UTC)}
    dubai_row = {"ts": DUBAI_0700}
    plus_one = {"ts": DUBAI_0700 + timedelta(seconds=1)}
    assert content_hash(utc_row) == content_hash(dubai_row) != content_hash(plus_one)
