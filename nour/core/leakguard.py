"""Value-based leak guard and the ``SafeStr`` mint (DESIGN §3.3 ``nour/core/leakguard.py``;
SPEC §2 §6 §10 §11 §13).

SPEC §2: "Nothing from Tier 2 (vault) is ever written into her memory, logs or prompts; only
references and hashes." SPEC §13 (vault exfiltration): "Tier 2 content never enters prompts,
memory or logs." The primary control is the ``Tier2Value`` type (``nour/core/tier2.py``); this
module is the defence in depth that also catches the passphrase in **any** position of **any**
text (DESIGN §4d: ``register_plaintext_once(passphrase)``).

What the guard holds
--------------------
Only *fingerprints*: ``HMAC-SHA256(key, normalise(value))`` for every vault value, every card
PAN and the passphrase, each with a label (``"vault:buzz-avenue/banking/receiving#iban"``,
``"passphrase"``, ``"card:operator"``) and the *length* of the normalised value. Never the
plaintext. The key is the ``governance/leakguard-key`` secret, so the fingerprints cannot be
brute-forced offline (SPEC §10: the IBAN space is small). The length is not a secret and is what
lets the scan run in linear time (below); a caller that re-registers a stored fingerprint
(``owner.passphrase_fp``, vault rows) stores and passes the length alongside it.

Normalisation (:func:`normalise`) is applied character by character: NFKC, every Unicode decimal
digit to ASCII, every punctuation, symbol, separator and control/format character dropped
(general categories ``P*``, ``S*``, ``Z*``, ``C*``), upper-cased. So ``AE07 0331 2345``,
``ae07-0331-2345``, ``AE07.0331.2345``, ``AE07/0331/2345``, ``AE07,0331,2345``,
``AE07_0331_2345``, ``AE07\\u200b0331`` and ``ＡＥ０７ ٠٣٣١`` are all the same value, and a
passphrase survives commas, slashes, quotes, emoji and line breaks between its words.

How a text is scanned
---------------------
:meth:`LeakGuard.scan` builds the canonical form of the whole text once, keeping for every
canonical character the span of the original character it came from, and slides a window of each
registered length over it; every window is fingerprinted (verdicts are cached per distinct
window, so repetitive text costs one HMAC per distinct window) and compared through the digest
index with ``hmac.compare_digest`` (constant time). A hit's span is the original text from the
first to the last matched character, so ``(AE07 0331 … 456).`` yields the bare value and a value
split across lines yields both lines' halves. The same scan runs a second time over the
percent-decoded text when it contains ``%XX`` sequences (URL query strings), with spans mapped
back to the encoded original. Cost is linear in the text length times the number of distinct
registered lengths: a 10,000-character prompt scans in tens of milliseconds, a pasted list of
numbers no slower than prose.

Because the window slides over every position, a registered value is found glued to other
characters (``IBANAE07…``, ``xAE07…y``, the value with digits appended); that is deliberate — the
model is the adversary (SPEC §13) and appending a character must not hide a value. The price is
that short values would hit by chance inside unrelated codes, so :data:`MIN_VALUE_CHARS` is six
normalised characters (a six-character alphanumeric value appears by accident once in ~2 billion
positions).

Value-based, never shape-based (DESIGN §10 decision): a supplier's legitimate IBAN in observed
mail is not registered, so it is shown to the model as data rather than blocked. The regex
:func:`shape_hits` exists for the auditor and the injection scanner and never blocks anything.

The ``SafeStr`` mint
--------------------
``nour/core/types.py`` defines ``SafeStr`` and the private sentinel ``_MINT``; this module imports
``_MINT`` and is the **only** module that calls the ``SafeStr`` constructor (``tests/unit/test_walls.py``).
:meth:`LeakGuard.safe` raises ``Tier2LeakError`` on any hit and mints otherwise;
:meth:`LeakGuard.redact` replaces hits with ``[<label> …<last4>]`` and mints the result (the
passphrase is redacted without its last characters: it is a credential, not a reference).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import hmac
import re
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, field_validator

from nour.core.errors import Tier2LeakError
from nour.core.hashing import keyed_hash
from nour.core.types import _MINT, Hash, Money, SafeStr

MIN_VALUE_CHARS = 6
"""Shortest value (after normalisation) that may be registered. The scan matches a value in any
position, even inside a longer run, so anything shorter would hit by chance in ordinary text."""

MAX_VALUE_CHARS = 128
"""Longest value (after normalisation) that may be registered: bounds the window the scan slides."""

LAST4_MIN_VALUE_CHARS = 8
"""A redaction shows the last four characters only when the value is at least this long,
otherwise ``…last4`` would reveal most of it."""

NO_LAST4_LABELS = frozenset({"passphrase"})
"""Labels (exact, or as a ``<label>:`` prefix) redacted without ``…last4``."""

_DIGEST_LEN = hashlib.sha256().digest_size
_UNSEEN: Any = object()
_DROPPED_CATEGORIES = ("P", "S", "Z", "C")  # punctuation, symbols, separators, control/format

_PERCENT_RUN_RE = re.compile(r"(?:%[0-9A-Fa-f]{2})+")

_IBAN_SHAPE_RE = re.compile(
    r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ \-]?[A-Z0-9]{4}){2,7}(?:[ \-]?[A-Z0-9]{1,4})?(?![A-Z0-9])"
)
_PAN_SHAPE_RE = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")
_PASSPORT_SHAPE_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{1,2}\d{6,8}(?![A-Z0-9])")

_SCALARS: tuple[type, ...] = (
    bool,
    int,
    float,
    Decimal,
    dt.datetime,
    dt.date,
    dt.time,
    uuid.UUID,
    Enum,
)


# --------------------------------------------------------------------------- hits


class LeakHit(BaseModel, frozen=True):
    """A registered value found in a text: its label and ``(start, end)`` in the original."""

    label: str
    span: tuple[int, int]

    @field_validator("span")
    @classmethod
    def _ordered(cls, value: tuple[int, int]) -> tuple[int, int]:
        start, end = value
        if start < 0 or end < start:
            raise ValueError("span must be 0 <= start <= end")
        return value


class ShapeHit(BaseModel, frozen=True):
    """An advisory shape match (``iban`` / ``pan`` / ``passport``); never blocks (DESIGN §4b)."""

    kind: Literal["iban", "pan", "passport"]
    span: tuple[int, int]

    @field_validator("span")
    @classmethod
    def _ordered(cls, value: tuple[int, int]) -> tuple[int, int]:
        start, end = value
        if start < 0 or end < start:
            raise ValueError("span must be 0 <= start <= end")
        return value


# --------------------------------------------------------------------------- normalisation

_canon_cache: dict[str, str] = {}


def _canon_char(ch: str) -> str:
    """The canonical form of one character (``""`` when it is dropped); memoised."""
    piece = _canon_cache.get(ch)
    if piece is None:
        out: list[str] = []
        for part in unicodedata.normalize("NFKC", ch):
            if unicodedata.category(part)[0] in _DROPPED_CATEGORIES:
                continue
            digit = unicodedata.decimal(part, None)
            out.append(str(digit) if digit is not None else part)
        piece = "".join(out).upper()
        if len(_canon_cache) < 65536:
            _canon_cache[ch] = piece
    return piece


def normalise(value: str) -> str:
    """The canonical form both sides are fingerprinted in: per character, NFKC, Unicode decimal
    digits to ASCII, punctuation / symbols / separators / control and format characters removed,
    upper-cased. Letters, marks and digits are all that survive."""
    return "".join(_canon_char(ch) for ch in value)


def _is_tier2(obj: object) -> bool:
    try:
        return bool(getattr(type(obj), "__tier2__", False))
    except Exception:  # noqa: BLE001 - a Tier2Value may raise on any probe
        return True


def _no_last4(label: str) -> bool:
    return any(label == bare or label.startswith(bare + ":") for bare in NO_LAST4_LABELS)


@dataclasses.dataclass(frozen=True, slots=True)
class _View:
    """A canonical rendering of a text: ``canon[i]`` came from ``text[starts[i]:ends[i]]``."""

    canon: str
    starts: list[int]
    ends: list[int]


def _raw_view(text: str) -> _View:
    canon: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, ch in enumerate(text):
        piece = _canon_char(ch)
        for part in piece:
            canon.append(part)
            starts.append(index)
            ends.append(index + 1)
    return _View("".join(canon), starts, ends)


def _decoded_view(text: str) -> _View | None:
    """The percent-decoded text (``%41%45%30%37`` → ``AE07``), each decoded character mapped
    back to the encoded run it came from; ``None`` when the text has no ``%XX`` sequence."""
    if not _PERCENT_RUN_RE.search(text):
        return None
    canon: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for match in _PERCENT_RUN_RE.finditer(text):
        for index in range(pos, match.start()):
            for part in _canon_char(text[index]):
                canon.append(part)
                starts.append(index)
                ends.append(index + 1)
        decoded = bytes.fromhex(match.group(0).replace("%", "")).decode("utf-8", "replace")
        for ch in decoded:
            for part in _canon_char(ch):
                canon.append(part)
                starts.append(match.start())
                ends.append(match.end())
        pos = match.end()
    for index in range(pos, len(text)):
        for part in _canon_char(text[index]):
            canon.append(part)
            starts.append(index)
            ends.append(index + 1)
    return _View("".join(canon), starts, ends)


# --------------------------------------------------------------------------- the guard


class LeakGuard:
    """§2 §6 §10 §11: value-based scrubber. Holds keyed HMAC fingerprints of every Tier 2 value, every card PAN
    and the passphrase; never plaintext. Primary control is Tier2Value's type (3.3b); this is defence in depth
    that also catches the passphrase in ANY position of any text."""

    def __init__(self, hmac_key: bytes) -> None:
        if not isinstance(hmac_key, bytes | bytearray) or not hmac_key:
            raise ValueError(
                "LeakGuard needs a non-empty HMAC key (secret governance/leakguard-key)"
            )
        self._key = bytes(hmac_key)
        self._fingerprints: dict[bytes, tuple[bytes, str]] = {}
        self._lengths: set[int] = set()

    def __repr__(self) -> str:
        return f"LeakGuard(labels={sorted(self.labels())!r})"

    # ----- fingerprints

    def fingerprint(self, value: str) -> bytes:
        """``HMAC-SHA256(key, normalise(value))`` as raw digest bytes."""
        if not isinstance(value, str):
            raise TypeError("fingerprint takes a str")
        return hmac.digest(self._key, normalise(value).encode("utf-8"), "sha256")

    def content_fp(self, value: str) -> Hash:
        """``"hmac:<hex>"`` over the raw value with the guard's key: what ``SecretRef.content_fp``
        and audit rows store for a Tier 2 value (DESIGN §3.3; SPEC §10)."""
        if not isinstance(value, str):
            raise TypeError("content_fp takes a str")
        return keyed_hash(self._key, value)

    def register_fingerprint(self, fp: bytes, label: str, *, length: int) -> None:
        """Register a fingerprint computed earlier (``owner.passphrase_fp``, vault rows) together
        with ``length``, the number of characters of the normalised value (``len(normalise(v))``
        at the time it was fingerprinted; stored next to the fingerprint, never derivable from
        it). The length is what the scan slides its window by, so a wrong length never matches."""
        if not isinstance(fp, bytes | bytearray) or len(fp) != _DIGEST_LEN:
            raise ValueError(f"a fingerprint is {_DIGEST_LEN} raw HMAC-SHA256 bytes")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("a fingerprint needs a label")
        if isinstance(length, bool) or not isinstance(length, int):
            raise TypeError("length is the normalised value's character count (int)")
        if not MIN_VALUE_CHARS <= length <= MAX_VALUE_CHARS:
            raise ValueError(
                f"a registered value is {MIN_VALUE_CHARS}..{MAX_VALUE_CHARS} normalised "
                f"characters long, got {length}"
            )
        self._fingerprints[bytes(fp)] = (bytes(fp), label)
        self._lengths.add(length)

    def register_plaintext_once(self, value: str, label: str) -> bytes:
        """Fingerprint ``value`` immediately, register it under ``label`` and return the
        fingerprint; the plaintext is not retained (DESIGN §4d). The normalised length is
        recorded for the scan; a caller that persists the fingerprint persists
        ``len(normalise(value))`` with it for :meth:`register_fingerprint`."""
        if not isinstance(value, str):
            raise TypeError("register_plaintext_once takes a str")
        length = len(normalise(value))
        if not MIN_VALUE_CHARS <= length <= MAX_VALUE_CHARS:
            raise ValueError(
                f"a registered value needs {MIN_VALUE_CHARS}..{MAX_VALUE_CHARS} characters "
                f"after normalisation, got {length}"
            )
        fp = self.fingerprint(value)
        self.register_fingerprint(fp, label, length=length)
        return fp

    def labels(self) -> frozenset[str]:
        return frozenset(label for _, label in self._fingerprints.values())

    def lengths(self) -> frozenset[int]:
        """The distinct normalised lengths registered (what the scan slides its windows by)."""
        return frozenset(self._lengths)

    def _label_for(self, digest: bytes) -> str | None:
        """Look the digest up and confirm the match in constant time."""
        entry = self._fingerprints.get(digest)
        if entry is None:
            return None
        registered, label = entry
        return label if hmac.compare_digest(digest, registered) else None

    # ----- scanning

    def _hits_in(
        self, view: _View, verdicts: dict[str, str | None]
    ) -> Iterator[tuple[int, int, str]]:
        """``(start, end, label)`` for every window of a registered length that fingerprints to a
        registered value; ``verdicts`` caches the answer per distinct window text."""
        canon = view.canon
        key = self._key
        digest = hmac.digest
        label_for = self._label_for
        # One character is one byte when the canonical text is ASCII (the common case), so the
        # windows can be sliced from the encoded bytes without an encode per window.
        encoded: bytes | None = canon.encode("ascii") if canon.isascii() else None
        for length in self._lengths:
            last = len(canon) - length
            for index in range(last + 1):
                window = canon[index : index + length]
                label = verdicts.get(window, _UNSEEN)
                if label is _UNSEEN:
                    data = (
                        encoded[index : index + length]
                        if encoded is not None
                        else window.encode("utf-8")
                    )
                    label = label_for(digest(key, data, "sha256"))
                    verdicts[window] = label
                if label is not None:
                    yield view.starts[index], view.ends[index + length - 1], label

    def scan(self, text: str) -> list[LeakHit]:
        """Every registered value in ``text``, as minimal non-nested spans sorted by start."""
        if _is_tier2(text):
            raise Tier2LeakError("a Tier2Value is not text; it cannot be scanned, only written")
        if not isinstance(text, str):
            raise TypeError(f"scan takes a str, got {type(text).__name__}")
        if not self._fingerprints or not text:
            return []
        verdicts: dict[str, str | None] = {}
        hits: dict[tuple[int, int], str] = {}
        views = [_raw_view(text)]
        decoded = _decoded_view(text)
        if decoded is not None:
            views.append(decoded)
        for view in views:
            for start, end, label in self._hits_in(view, verdicts):
                if end > start:
                    hits[(start, end)] = label
        minimal = [
            LeakHit(label=label, span=span)
            for span, label in hits.items()
            if not any(
                other != span and other[0] >= span[0] and other[1] <= span[1] and lbl == label
                for other, lbl in hits.items()
            )
        ]
        return sorted(minimal, key=lambda hit: (hit.span[0], hit.span[1], hit.label))

    def safe(self, text: str) -> SafeStr:
        """Mint a ``SafeStr`` for a sink Nour writes; raises ``Tier2LeakError`` on any hit.

        The error names the labels, never the text.
        """
        hits = self.scan(text)
        if hits:
            labels = sorted({hit.label for hit in hits})
            raise Tier2LeakError(f"text contains registered Tier 2 value(s): {', '.join(labels)}")
        return SafeStr(text, _minted_by=_MINT)

    def redact(self, text: str) -> tuple[SafeStr, list[LeakHit]]:
        """Replace every hit with ``[<label> …<last4>]`` and mint the result — for observed text
        shown to the model (SPEC §10: last four characters only). Overlapping hits of different
        labels are replaced together."""
        hits = self.scan(text)
        if not hits:
            return SafeStr(text, _minted_by=_MINT), []
        groups: list[list[LeakHit]] = []
        for hit in hits:
            if groups and hit.span[0] < max(h.span[1] for h in groups[-1]):
                groups[-1].append(hit)
            else:
                groups.append([hit])
        out = text
        for group in reversed(groups):
            start = group[0].span[0]
            end = max(hit.span[1] for hit in group)
            replacement = "".join(self._mask(text, hit) for hit in _distinct_labels(group))
            out = out[:start] + replacement + out[end:]
        return SafeStr(out, _minted_by=_MINT), hits

    @staticmethod
    def _mask(text: str, hit: LeakHit) -> str:
        canon = normalise(text[hit.span[0] : hit.span[1]])
        if _no_last4(hit.label) or len(canon) < LAST4_MIN_VALUE_CHARS:
            return f"[{hit.label}]"
        return f"[{hit.label} …{canon[-4:]}]"

    # ----- structured objects

    def safe_mapping(self, obj: Mapping[str, Any]) -> dict[str, Any]:
        """Recursive ``safe`` over a mapping: every ``str`` (keys included) becomes a ``SafeStr``,
        ``dict``/``list``/``tuple``/``set`` recurse, pydantic models and dataclasses become dicts
        of their fields (``Money`` passes through, so ``canonical_json`` hashes it the same),
        ``bytes`` are scanned as UTF-8, UTF-16 and Latin-1 text and kept, the scalars
        ``canonical_json`` accepts pass through, and anything else — an object whose ``str()``
        could carry anything — raises ``TypeError``; a ``Tier2Value`` anywhere raises
        ``Tier2LeakError``. Raises ``Tier2LeakError`` on any hit."""
        if not isinstance(obj, Mapping):
            raise TypeError("safe_mapping takes a mapping")
        result = self._safe_any(obj, "$")
        assert isinstance(result, dict)
        return result

    def _safe_any(self, value: Any, path: str) -> Any:  # noqa: PLR0911 - one branch per shape
        if _is_tier2(value):
            raise Tier2LeakError(f"a Tier2Value sits at {path}; hash its SecretRef instead")
        if isinstance(value, str):
            return self.safe(value)
        if isinstance(value, bytes | bytearray | memoryview):
            self._scan_bytes(bytes(value))
            return value
        if value is None or isinstance(value, (Money, *_SCALARS)):
            return value
        if isinstance(value, BaseModel):
            return {
                name: self._safe_any(getattr(value, name), f"{path}.{name}")
                for name in type(value).model_fields
            }
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                f.name: self._safe_any(getattr(value, f.name), f"{path}.{f.name}")
                for f in dataclasses.fields(value)
            }
        if isinstance(value, Mapping):
            return {
                self._safe_key(key): self._safe_any(item, f"{path}.{key}")
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._safe_any(item, f"{path}[{i}]") for i, item in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(self._safe_any(item, f"{path}[{i}]") for i, item in enumerate(value))
        if isinstance(value, set | frozenset):
            return type(value)(self._safe_any(item, f"{path}{{}}") for item in value)
        raise TypeError(
            f"safe_mapping cannot scrub an object of type {type(value).__name__} at {path}; "
            "pass its fields, never an opaque object"
        )

    def _scan_bytes(self, data: bytes) -> None:
        """Scan the text a byte string could be: UTF-8, both UTF-16 byte orders (when the length
        allows) and Latin-1 (every byte string decodes)."""
        candidates = [data.decode("utf-8", "ignore"), data.decode("latin-1")]
        if len(data) % 2 == 0:
            candidates.append(data.decode("utf-16-le", "ignore"))
            candidates.append(data.decode("utf-16-be", "ignore"))
        for text in candidates:
            self.safe(text)

    def _safe_key(self, key: Any) -> Any:
        return self.safe(key) if isinstance(key, str) else key


def _distinct_labels(group: list[LeakHit]) -> list[LeakHit]:
    """One mask per label in an overlapping group (a value found at several overlapping windows
    is still one value)."""
    seen: dict[str, LeakHit] = {}
    for hit in group:
        seen.setdefault(hit.label, hit)
    return list(seen.values())


# --------------------------------------------------------------------------- advisory shapes


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def shape_hits(text: str) -> list[ShapeHit]:
    """Advisory shape detection (§13 auditor flag, InjectionScanner `bank_details` pattern).

    ``iban``: two letters, two digits, 15–34 alphanumerics in optional groups of four;
    ``pan``: 13–19 digits with optional separators that pass the Luhn check (every real card
    number does); ``passport``: one or two letters followed by 6–8 digits. Sorted by position.
    Observed text is never blocked on shapes. For text Nour writes herself the blocking set is
    :func:`blocking_shape_hits` (iban + pan only): the passport shape also matches ordinary
    order references such as ``SO2026001``, and passport numbers held in the vault are caught
    by value through the registered fingerprints.
    """
    if not isinstance(text, str):
        raise TypeError("shape_hits takes a str")
    hits: list[ShapeHit] = []
    for m in _IBAN_SHAPE_RE.finditer(text):
        hits.append(ShapeHit(kind="iban", span=(m.start(), m.end())))
    for m in _PAN_SHAPE_RE.finditer(text):
        digits = "".join(ch for ch in m.group(0) if ch.isdigit())
        if _luhn_ok(digits):
            hits.append(ShapeHit(kind="pan", span=(m.start(), m.end())))
    for m in _PASSPORT_SHAPE_RE.finditer(text):
        hits.append(ShapeHit(kind="passport", span=(m.start(), m.end())))
    return sorted(hits, key=lambda hit: (hit.span[0], hit.span[1], hit.kind))


def blocking_shape_hits(text: str) -> list[ShapeHit]:
    """The shapes that make Nour-authored text a leak (THREAT_REVIEW 2.1): IBAN-shaped and
    Luhn-valid card-PAN-shaped tokens. Nour never needs to type either (she uses placeholders
    and references, §10), so a hit in a draft, brief, memory row, handoff or reason is refused
    with ``RefusalCode.LEAK``. Passport shapes are advisory only (see :func:`shape_hits`)."""
    return [hit for hit in shape_hits(text) if hit.kind in ("iban", "pan")]
