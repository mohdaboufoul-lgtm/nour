"""Canonical JSON, SHA-256, HMAC and the audit chain (DESIGN §3.2 ``nour/core/hashing.py``;
SPEC §10 §12).

``input_hash`` / ``output_hash`` on every audit row (SPEC §12) are content hashes over
:func:`canonical_json`, so two processes, two dialects and a replay (DESIGN §7.4) hash the same
object to the same bytes. Every hash of a Tier 2 value is a *keyed* HMAC (:func:`keyed_hash`),
never a plain digest, because the IBAN space is small enough to brute-force (SPEC §10).

Tier 2 marker contract (so this module does not import ``nour.core.tier2``)
--------------------------------------------------------------------------
Any object whose class or instance exposes a truthy attribute ``__tier2__`` is refused by
:func:`canonical_json` with ``Tier2LeakError`` wherever it sits in the object graph.
``nour/core/tier2.py`` sets ``__tier2__ = True`` on ``Tier2Value``; anything else that must never
be hashed in the clear can do the same.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping, Sequence, Set
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from nour.core.errors import Tier2LeakError
from nour.core.types import Hash, Money

GENESIS_HASH = "0" * 64
"""``prev_hash`` of the first audit row (SPEC §12: the chain starts from a known constant)."""

_CHAIN_DOMAIN = b"nour-audit-chain-v1"


def _is_tier2(obj: object) -> bool:
    try:
        return bool(getattr(type(obj), "__tier2__", False)) or bool(
            getattr(obj, "__tier2__", False)
        )
    except Exception:  # noqa: BLE001 - a Tier2Value may raise on any attribute access
        return True


def _key(key: object) -> str:
    if isinstance(key, Enum):
        key = key.value
    if isinstance(key, str):
        return str(key)
    if key is None or isinstance(key, bool | int | float | Decimal):
        return str(key)
    raise TypeError(f"mapping key of type {type(key).__name__} cannot be canonicalised")


def _normalise(obj: Any) -> Any:  # noqa: PLR0911 - one branch per supported type
    if _is_tier2(obj):
        raise Tier2LeakError("a Tier 2 value cannot be hashed in the clear; hash its SecretRef")
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, Money):
        return obj.fils
    if isinstance(obj, Enum):
        return _normalise(obj.value)
    if isinstance(obj, str):
        return str(obj)  # SafeStr, Reason, NewTypes → plain text
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            raise ValueError("NaN and infinity cannot be canonicalised")
        return obj
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.tzinfo.utcoffset(obj) is None:
            raise ValueError("naive datetime cannot be canonicalised; every timestamp is tz-aware")
        return obj.astimezone(UTC).isoformat()
    if isinstance(obj, date | time):
        return obj.isoformat()
    if isinstance(obj, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, BaseModel):
        return {name: _normalise(getattr(obj, name)) for name in type(obj).model_fields}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _normalise(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {_key(k): _normalise(v) for k, v in obj.items()}
    if isinstance(obj, Set):
        return sorted((_normalise(item) for item in obj), key=_sort_key)
    if isinstance(obj, Sequence):
        return [_normalise(item) for item in obj]
    raise TypeError(f"object of type {type(obj).__name__} cannot be canonicalised")


def _sort_key(item: Any) -> bytes:
    return _dumps(item)


def _dumps(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def canonical_json(obj: Any) -> bytes:
    """Deterministic UTF-8 JSON: sorted keys, no whitespace; Money→fils, Decimal→str,
    datetime→UTC isoformat, date→isoformat, SafeStr/Reason→str, Enum→value, bytes→base64,
    sets→sorted lists, pydantic models and dataclasses→field dicts; a ``__tier2__``-marked object
    anywhere raises ``Tier2LeakError``; an unknown type raises ``TypeError`` rather than falling
    back to ``repr`` (a repr could carry anything)."""
    return _dumps(_normalise(obj))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_hex(key: bytes, data: bytes) -> str:
    """HMAC-SHA256; the key must be non-empty (an empty key is a plain hash in disguise)."""
    if not key:
        raise ValueError("hmac key must not be empty")
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def content_hash(obj: Any) -> Hash:
    """``"sha256:<hex>"`` over :func:`canonical_json` (audit ``input_hash`` / ``output_hash``)."""
    return Hash("sha256:" + sha256_hex(canonical_json(obj)))


def keyed_hash(key: bytes, value: str) -> Hash:
    """``"hmac:<hex>"`` — used for every Tier 2 hash (§10: IBAN space is brute-forceable)."""
    return Hash("hmac:" + hmac_hex(key, value.encode("utf-8")))


def chain_hash(prev: str, entry: bytes) -> str:
    """§12 audit chain: ``sha256(domain ‖ len(prev) ‖ prev ‖ entry)`` as hex.

    ``prev`` is the previous row's ``entry_hash`` (or :data:`GENESIS_HASH`); the length prefix
    keeps the framing unambiguous whatever ``entry`` contains.
    """
    prev_bytes = prev.encode("utf-8")
    framed = _CHAIN_DOMAIN + len(prev_bytes).to_bytes(4, "big") + prev_bytes + entry
    return sha256_hex(framed)
