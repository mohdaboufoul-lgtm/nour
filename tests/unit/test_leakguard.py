"""nour/core/leakguard.py (DESIGN §3.3, §4b, §4d, §7.3; SPEC §2 §6 §10 §11 §13): fingerprints and the SafeStr mint.

Proves (MODULES.md "core"): ``LeakGuard`` finds a registered value in any position (middle of a
sentence, with dashes, spaces, commas, slashes or underscores, glued to other characters, inside a
URL, percent-encoded, in Arabic surrounding text, across a line break, in Arabic-Indic digits,
with invisible characters, one character per token, longer than any token window), ``redact``
masks it, ``safe`` raises, shape hits never block; the scan is linear in the text; ``safe_mapping``
recurses and refuses opaque objects; ``SafeStr`` cannot be built outside ``LeakGuard``; the guard
never holds plaintext.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from nour.core import types as core_types
from nour.core.errors import Tier2LeakError
from nour.core.hashing import canonical_json, content_hash, keyed_hash
from nour.core.leakguard import (
    LAST4_MIN_VALUE_CHARS,
    MAX_VALUE_CHARS,
    MIN_VALUE_CHARS,
    LeakGuard,
    LeakHit,
    ShapeHit,
    blocking_shape_hits,
    normalise,
    shape_hits,
)
from nour.core.types import Money, Reason, SafeStr

_safe_ctor = SafeStr  # the constructor call is confined to nour/core/leakguard.py (AST walls)
_MINT = core_types._MINT

KEY = b"governance/leakguard-key: 32 bytes!!"
IBAN = "AE07 0331 2345 6789 0123 456"
IBAN_FLAT = "AE070331234567890123456"
IBAN_LABEL = "vault:buzz-avenue/banking/receiving#iban"
PAN = "4111 1111 1111 1111"  # Luhn-valid test number
PAN_LABEL = "card:operator"
PASSPHRASE = "open sesame 42"
OTHER_IBAN = "AE07 9999 2345 6789 0123 456"  # legitimate supplier IBAN, never registered


@pytest.fixture
def guard() -> LeakGuard:
    g = LeakGuard(KEY)
    g.register_plaintext_once(IBAN, IBAN_LABEL)
    g.register_plaintext_once(PAN, PAN_LABEL)
    g.register_plaintext_once(PASSPHRASE, "passphrase")
    return g


# --------------------------------------------------------------------------- SafeStr mint


def test_safe_str_cannot_be_built_outside_leakguard() -> None:
    with pytest.raises(TypeError):
        _safe_ctor("x")  # type: ignore[call-arg]
    with pytest.raises(Tier2LeakError):
        _safe_ctor("x", _minted_by=object())
    with pytest.raises(Tier2LeakError):
        _safe_ctor("x", _minted_by=None)
    with pytest.raises(Tier2LeakError):
        _safe_ctor("x", _minted_by="_MINT")
    minted = _safe_ctor("x", _minted_by=_MINT)  # the mechanism leakguard.py uses
    assert isinstance(minted, SafeStr) and minted == "x"


def test_safe_mints_a_safe_str(guard: LeakGuard) -> None:
    text = "Please find the invoice attached; the IBAN is on the letterhead."
    safe = guard.safe(text)
    assert isinstance(safe, SafeStr) and isinstance(safe, str)
    assert safe == text
    assert type(guard.safe(safe)) is SafeStr  # re-scanning a SafeStr is fine
    assert isinstance(guard.safe(Reason("Customer asked for a quote.")), SafeStr)
    assert isinstance(guard.safe(""), SafeStr)
    with pytest.raises(TypeError):
        guard.safe(b"bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        guard.safe(None)  # type: ignore[arg-type]


def test_safe_str_fields_accept_only_minted_text(guard: LeakGuard) -> None:
    class Sink(BaseModel, frozen=True):
        text: SafeStr

    assert Sink(text=guard.safe("hello")).text == "hello"
    with pytest.raises(ValidationError):
        Sink(text="hello")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Sink.model_validate_json('{"text": "hello"}')


# --------------------------------------------------------------------------- fingerprints and normalisation


def test_normalise_keeps_letters_marks_and_digits_only() -> None:
    assert normalise("AE07 0331 2345") == "AE0703312345"
    assert normalise("ae07-0331-2345") == "AE0703312345"
    assert normalise("AE07.0331.2345") == "AE0703312345"
    assert normalise("AE07,0331,2345") == "AE0703312345"
    assert normalise("AE07/0331/2345") == "AE0703312345"
    assert normalise("AE07_0331_2345") == "AE0703312345"
    assert normalise("AE07:0331;2345") == "AE0703312345"
    assert normalise("AE07‑0331—2345") == "AE0703312345"  # unicode dashes
    assert normalise("AE07​0331‏2345") == "AE0703312345"  # zero-width / RTL marks
    assert normalise("AE07 ٠٣٣١ ٢٣٤٥") == "AE0703312345"
    assert normalise("AE07 ۰۳۳۱ ۲۳۴۵") == "AE0703312345"  # Persian digits
    assert normalise("ＡＥ０７") == "AE07"  # full-width (NFKC)
    assert normalise("open sesame 42\n") == "OPENSESAME42"
    assert normalise('"open" 🐪 sesame/42!') == "OPENSESAME42"
    assert normalise("كلمة السر") == "كلمةالسر"
    assert normalise("- . , / _ ( ) [ ] 🌅 \t\r\n") == ""


def test_fingerprint_is_keyed_normalised_and_deterministic() -> None:
    a, b = LeakGuard(KEY), LeakGuard(KEY)
    assert a.fingerprint(IBAN) == b.fingerprint(IBAN)
    assert a.fingerprint(IBAN) == a.fingerprint("ae07-0331-2345-6789-0123-456")
    assert a.fingerprint(IBAN) == a.fingerprint("AE07,0331/2345_6789 0123.456")
    assert a.fingerprint(IBAN) != a.fingerprint(OTHER_IBAN)
    assert len(a.fingerprint(IBAN)) == 32
    assert LeakGuard(b"another key").fingerprint(IBAN) != a.fingerprint(IBAN)
    assert a.content_fp("x") == keyed_hash(KEY, "x") and a.content_fp("x").startswith("hmac:")
    with pytest.raises(ValueError):
        LeakGuard(b"")
    with pytest.raises(ValueError):
        LeakGuard("not bytes")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        a.fingerprint(1234)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        a.content_fp(1234)  # type: ignore[arg-type]


def test_registration_and_labels() -> None:
    g = LeakGuard(KEY)
    assert g.labels() == frozenset() and g.lengths() == frozenset()
    fp = g.register_plaintext_once(IBAN, IBAN_LABEL)
    assert fp == g.fingerprint(IBAN)
    assert g.labels() == frozenset({IBAN_LABEL}) and g.lengths() == frozenset({23})
    # a fingerprint persisted earlier is re-registered with the normalised length it was taken at
    g.register_fingerprint(LeakGuard(KEY).fingerprint(PAN), PAN_LABEL, length=len(normalise(PAN)))
    assert g.labels() == frozenset({IBAN_LABEL, PAN_LABEL}) and g.lengths() == frozenset({23, 16})
    assert g.scan(f"card {PAN} charged") and g.scan(f"iban {IBAN} paid")
    g.register_fingerprint(fp, "vault:renamed", length=23)  # same fingerprint: label replaced
    assert g.labels() == frozenset({"vault:renamed", PAN_LABEL})
    with pytest.raises(ValueError):
        g.register_fingerprint(b"short", "x", length=8)
    with pytest.raises(ValueError):
        g.register_fingerprint(fp, "   ", length=8)
    with pytest.raises(ValueError):
        g.register_fingerprint(fp, "x", length=MIN_VALUE_CHARS - 1)
    with pytest.raises(ValueError):
        g.register_fingerprint(fp, "x", length=MAX_VALUE_CHARS + 1)
    with pytest.raises(TypeError):
        g.register_fingerprint(fp, "x", length=True)
    with pytest.raises(TypeError):
        g.register_fingerprint(fp, "x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        g.register_plaintext_once("ab123", "too-short")
    with pytest.raises(ValueError):
        g.register_plaintext_once(" - . ", "empty-after-normalisation")
    with pytest.raises(ValueError):
        g.register_plaintext_once("x" * (MAX_VALUE_CHARS + 1), "too-long")
    with pytest.raises(TypeError):
        g.register_plaintext_once(b"bytes", "x")  # type: ignore[arg-type]
    assert MIN_VALUE_CHARS == 6 and MAX_VALUE_CHARS == 128


def test_a_wrong_length_never_matches() -> None:
    """The length is part of the registration: a fingerprint re-registered with the wrong
    length is dead weight, never a false hit."""
    g = LeakGuard(KEY)
    g.register_fingerprint(g.fingerprint(IBAN), IBAN_LABEL, length=24)
    assert g.scan(f"pay {IBAN} now") == []


def test_guard_never_holds_plaintext(guard: LeakGuard) -> None:
    state = repr(vars(guard)) + repr(guard)
    for secret in (IBAN, normalise(IBAN), PAN, normalise(PAN), PASSPHRASE, normalise(PASSPHRASE)):
        assert secret not in state
    assert repr(guard) == f"LeakGuard(labels={sorted([IBAN_LABEL, PAN_LABEL, 'passphrase'])!r})"
    assert KEY not in repr(guard).encode()
    assert guard.lengths() == frozenset({23, 16, 12})  # lengths, never values


# --------------------------------------------------------------------------- scan: any position


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (IBAN, IBAN_LABEL),
        (IBAN_FLAT, IBAN_LABEL),
        (f"please pay {IBAN} by Thursday", IBAN_LABEL),
        ("please pay AE07-0331-2345-6789-0123-456 by Thursday", IBAN_LABEL),
        ("please pay ae07 0331 2345 6789 0123 456 by Thursday", IBAN_LABEL),
        (f"iban:{IBAN_FLAT},thanks", IBAN_LABEL),
        (f"ref-{IBAN_FLAT}-x", IBAN_LABEL),
        (f"(IBAN: {IBAN}).", IBAN_LABEL),
        (f'"{IBAN}"', IBAN_LABEL),
        (f"`{IBAN_FLAT}`", IBAN_LABEL),
        (f'{{"iban": "{IBAN_FLAT}"}}', IBAN_LABEL),
        (f"الآيبان {IBAN} للتحويل", IBAN_LABEL),
        ("الآيبان AE07 ٠٣٣١ ٢٣٤٥ ٦٧٨٩ ٠١٢٣ ٤٥٦ شكراً", IBAN_LABEL),
        ("AE۰۷۰۳۳۱۲۳۴۵۶۷۸۹۰۱۲۳۴۵۶", IBAN_LABEL),  # Persian digits
        ("ＡＥ０７０３３１２３４５６７８９０１２３４５６", IBAN_LABEL),  # full-width
        ("AE07​0331​2345​6789​0123​456", IBAN_LABEL),  # zero-width spaces
        ("AE07\xad0331\xad2345\xad6789\xad0123\xad456", IBAN_LABEL),  # soft hyphens
        ("AE07 0331\n2345 6789\n0123 456", IBAN_LABEL),
        (f"IBAN {IBAN_FLAT[:10]}\n\n{IBAN_FLAT[10:]} thanks", IBAN_LABEL),  # mid-group split
        (f"{IBAN} send it", IBAN_LABEL),
        ("line one\nline two with AE07 0331 2345 6789 0123 456 inside\nline three", IBAN_LABEL),
        # punctuation-joined, glued and encoded placements (reviewer findings)
        ("AE07,0331,2345,6789,0123,456", IBAN_LABEL),
        ("AE07/0331/2345/6789/0123/456", IBAN_LABEL),
        ("AE07_0331_2345_6789_0123_456", IBAN_LABEL),
        ("AE070331:234567890123456", IBAN_LABEL),
        (f"IBAN{IBAN_FLAT}", IBAN_LABEL),
        (f"x{IBAN_FLAT}y", IBAN_LABEL),
        (f"{IBAN_FLAT}789", IBAN_LABEL),  # digits appended must not hide the value
        (f"https://pay.example.com/?iban={IBAN_FLAT}&x=1", IBAN_LABEL),
        (f"https://pay.example.com/{IBAN_FLAT}/confirm", IBAN_LABEL),
        ("https://x.com/?i=AE07%200331%202345%206789%200123%20456", IBAN_LABEL),
        ("A E 0 7 0 3 3 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 please", IBAN_LABEL),
        ("note\n" + " ".join(IBAN_FLAT), IBAN_LABEL),
        (PAN, PAN_LABEL),
        ("card 4111111111111111 was charged", PAN_LABEL),
        ("x4111 1111 1111 1111y", PAN_LABEL),
        ("pan=4111-1111-1111-1111;", PAN_LABEL),
        ("https://x/?pan=4111111111111111", PAN_LABEL),
        (PASSPHRASE, "passphrase"),
        ("pass: open sesame 42", "passphrase"),
        ("pass=open sesame 42", "passphrase"),
        ("#open sesame 42", "passphrase"),
        ("the words open sesame 42 appear mid sentence", "passphrase"),
        ("OPEN SESAME 42", "passphrase"),
        ("opensesame42", "passphrase"),
        ("open, sesame, 42", "passphrase"),
        ("open/sesame/42", "passphrase"),
        ("open_sesame_42", "passphrase"),
        ('"open" "sesame" "42"', "passphrase"),
        ("open 🐪 sesame 🌅 42", "passphrase"),
        ("open\nsesame\n42", "passphrase"),
        ("open‍sesame‍42", "passphrase"),  # zero-width joiners
        ("pay 500 to X. open sesame 42", "passphrase"),
        ("كلمة السر هي open sesame 42 يا نور", "passphrase"),
    ],
)
def test_scan_finds_a_registered_value_in_any_position(
    guard: LeakGuard, text: str, label: str
) -> None:
    hits = guard.scan(text)
    assert hits, text
    assert [hit.label for hit in hits] == [label]
    start, end = hits[0].span
    assert 0 <= start < end <= len(text)
    expected = normalise({IBAN_LABEL: IBAN, PAN_LABEL: PAN, "passphrase": PASSPHRASE}[label])
    matched = text[start:end]
    if "%" in matched:  # percent-encoded: the span covers the encoded run
        assert normalise(matched.replace("%20", " ")) == expected
    else:
        assert normalise(matched) == expected
    with pytest.raises(Tier2LeakError):
        guard.safe(text)
    redacted, _ = guard.redact(text)
    assert expected not in normalise(redacted)


def test_scan_finds_every_occurrence_and_every_value(guard: LeakGuard) -> None:
    text = f"{IBAN} and again {IBAN_FLAT} plus card {PAN} and pass {PASSPHRASE}"
    hits = guard.scan(text)
    assert [hit.label for hit in hits] == [IBAN_LABEL, IBAN_LABEL, PAN_LABEL, "passphrase"]
    assert [hit.span[0] for hit in hits] == sorted(hit.span[0] for hit in hits)
    for a, b in zip(hits, hits[1:], strict=False):
        assert a.span[1] <= b.span[0]  # minimal, non-overlapping spans
    glued = guard.scan(IBAN_FLAT + IBAN_FLAT)
    assert [hit.span for hit in glued] == [(0, 23), (23, 46)]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a plain customer question about delivery times",
        OTHER_IBAN,  # a legitimate, unregistered IBAN is data, not a hit (value-based)
        f"please pay {OTHER_IBAN}",
        "AE07 0331 2345 6789 0123 457",  # one digit off
        "4111 1111 1111 1112",
        "open sesame 43",
        "open sesame",
        "AE07 0331 2345",  # a prefix of the value
        IBAN_FLAT[2:],  # the digits without the country code are another value
        # reversed, homoglyph and base64 forms are not the value: value-based detection is the
        # defence in depth, the Tier2Value type is the primary control (DESIGN §4b)
        IBAN_FLAT[::-1],
        "AEO7O33123456789O123456",
        "QUUwNzAzMzEyMzQ1Njc4OTAxMjM0NTY=",
    ],
)
def test_scan_has_no_false_positives(guard: LeakGuard, text: str) -> None:
    assert guard.scan(text) == []
    assert isinstance(guard.safe(text), SafeStr)


def test_scan_is_empty_without_registrations() -> None:
    assert LeakGuard(KEY).scan(IBAN) == []
    assert isinstance(LeakGuard(KEY).safe(IBAN), SafeStr)


def test_scan_spans_are_minimal_and_hits_are_frozen(guard: LeakGuard) -> None:
    text = f"({IBAN})."
    hits = guard.scan(text)
    assert len(hits) == 1
    assert text[hits[0].span[0] : hits[0].span[1]] == IBAN  # not the brackets
    split = "AE07 0331\n2345 6789\n0123 456"
    (hit,) = guard.scan(f"x\n{split}\ny")
    assert f"x\n{split}\ny"[hit.span[0] : hit.span[1]] == split
    with pytest.raises(ValidationError):
        hits[0].span = (0, 1)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LeakHit(label="x", span=(5, 2))


def test_a_value_longer_than_any_token_window_is_found_mid_sentence() -> None:
    """DESIGN §4d: the passphrase is caught in ANY position; there is no token limit."""
    g = LeakGuard(KEY)
    words = [f"word{i}" for i in range(12)]
    phrase = " ".join(words)
    g.register_plaintext_once(phrase, "passphrase")
    for text in (
        f"hello {phrase} bye",
        f"pay 500 now, {phrase}",
        f"{phrase} bye",
        f"hello {phrase}",
        f"transfer 500 to the supplier\npass: {phrase}\nthanks",
        f"transfer 500 to the supplier\nكلمة السر: {phrase}.\nthanks",
        "hello " + ", ".join(words) + " bye",
        "hello " + "".join(words) + " bye",
    ):
        hits = g.scan(text)
        assert [hit.label for hit in hits] == ["passphrase"], text
        with pytest.raises(Tier2LeakError):
            g.safe(text)
        redacted, _ = g.redact(text)
        assert "word0" not in redacted and "[passphrase]" in redacted


def test_scan_is_linear_on_digit_heavy_text(guard: LeakGuard) -> None:
    """A pasted list of numbers (bank statement, phone list) must not stall the desk loop."""
    for text in ("1 " * 5000, "12345 " * 3000, "1" * 5000, "a" * 20000):
        started = time.perf_counter()
        assert guard.scan(text) == []
        assert time.perf_counter() - started < 0.2, len(text)
    prose = " ".join(f"tok{i}" for i in range(2000))
    started = time.perf_counter()
    assert guard.scan(prose) == []
    assert time.perf_counter() - started < 0.5


SEPARATORS = st.sampled_from([" ", ",", "/", "_", "-", ".", "\n", " 🐪 ", '" "', "%20", "​"])


@settings(max_examples=60, deadline=None)
@given(
    value=st.text(alphabet=st.characters(categories=("Lu", "Nd")), min_size=6, max_size=24),
    before=st.text(max_size=20),
    after=st.text(max_size=20),
    sep=SEPARATORS,
)
def test_hypothesis_any_surrounding_text(value: str, before: str, after: str, sep: str) -> None:
    g = LeakGuard(KEY)
    g.register_plaintext_once(value, "v")
    text = f"{before} {value[:3]}{sep}{value[3:]} {after}"
    hits = g.scan(text)
    assert hits
    assert any(
        normalise(text[s:e].replace("%20", " ")) == normalise(value)
        for (s, e) in (h.span for h in hits)
    )
    with pytest.raises(Tier2LeakError):
        g.safe(text)
    redacted, _ = g.redact(text)
    assert normalise(value) not in normalise(redacted)


# --------------------------------------------------------------------------- safe vs redact


def test_safe_raises_naming_the_label_never_the_value(guard: LeakGuard) -> None:
    with pytest.raises(Tier2LeakError) as info:
        guard.safe(f"pay to {IBAN} now")
    message = str(info.value)
    assert IBAN_LABEL in message
    assert IBAN not in message and normalise(IBAN) not in message and "3456" not in message
    with pytest.raises(Tier2LeakError) as info:
        guard.safe(f"{PASSPHRASE} {PAN}")
    assert "passphrase" in str(info.value) and PAN_LABEL in str(info.value)
    assert PASSPHRASE not in str(info.value)


def test_redact_masks_with_label_and_last4(guard: LeakGuard) -> None:
    text = "Supplier says: pay AE07-0331-2345-6789-0123-456 by Monday."
    redacted, hits = guard.redact(text)
    assert isinstance(redacted, SafeStr)
    assert redacted == f"Supplier says: pay [{IBAN_LABEL} …3456] by Monday."
    assert [hit.label for hit in hits] == [IBAN_LABEL]
    assert hits == guard.scan(text)
    assert guard.scan(redacted) == []
    assert isinstance(guard.safe(redacted), SafeStr)
    assert guard.redact(f"x{IBAN_FLAT}y")[0] == f"x[{IBAN_LABEL} …3456]y"
    assert guard.redact("AE07,0331,2345,6789,0123,456")[0] == f"[{IBAN_LABEL} …3456]"


def test_redact_handles_every_occurrence_and_mixed_values(guard: LeakGuard) -> None:
    text = f"{IBAN} / {PAN} / {IBAN}"
    redacted, hits = guard.redact(text)
    assert redacted == f"[{IBAN_LABEL} …3456] / [{PAN_LABEL} …1111] / [{IBAN_LABEL} …3456]"
    assert len(hits) == 3
    assert guard.scan(redacted) == []


def test_redact_masks_overlapping_labels_together() -> None:
    g = LeakGuard(KEY)
    g.register_plaintext_once(IBAN_FLAT, "iban")
    g.register_plaintext_once(IBAN_FLAT[4:], "acct")
    redacted, hits = g.redact(f"pay {IBAN_FLAT} now")
    assert redacted == "pay [iban …3456][acct …3456] now"
    assert {hit.label for hit in hits} == {"iban", "acct"}
    assert g.scan(redacted) == []


def test_redact_never_shows_passphrase_characters(guard: LeakGuard) -> None:
    redacted, hits = guard.redact(f"pass: {PASSPHRASE} please")
    assert redacted == "pass: [passphrase] please"
    assert hits[0].label == "passphrase"
    for piece in ("42", "me42", "sesame", "open"):
        assert piece not in redacted
    g = LeakGuard(KEY)
    g.register_plaintext_once("hunter2-secret", "passphrase:owner")
    assert g.redact("x hunter2-secret y")[0] == "x [passphrase:owner] y"


def test_redact_shows_no_last4_for_short_values() -> None:
    g = LeakGuard(KEY)
    g.register_plaintext_once("abc123", "card:short")
    assert len(normalise("abc123")) < LAST4_MIN_VALUE_CHARS
    assert g.redact("code abc123 here")[0] == "code [card:short] here"
    g.register_plaintext_once("abcd1234", "card:long")
    assert g.redact("code abcd1234 here")[0] == "code [card:long …1234] here"


def test_redact_of_clean_text_is_identity(guard: LeakGuard) -> None:
    redacted, hits = guard.redact("nothing to see")
    assert redacted == "nothing to see" and hits == [] and isinstance(redacted, SafeStr)


def test_redact_arabic_context_keeps_the_rest(guard: LeakGuard) -> None:
    redacted, _ = guard.redact(f"حوّل المبلغ إلى {IBAN} قبل الخميس")
    assert redacted == f"حوّل المبلغ إلى [{IBAN_LABEL} …3456] قبل الخميس"


# --------------------------------------------------------------------------- shape hits are advisory


def test_shape_hits_find_iban_pan_and_passport_shapes() -> None:
    text = (
        f"IBAN {OTHER_IBAN}; card {PAN}; passport A12345678; phone 0501234567; order 1234567890123"
    )
    hits = shape_hits(text)
    kinds = [(hit.kind, text[hit.span[0] : hit.span[1]]) for hit in hits]
    assert ("iban", OTHER_IBAN) in kinds
    assert ("pan", PAN) in kinds
    assert ("passport", "A12345678") in kinds
    assert all(
        kind != "pan" or text[s:e] == PAN for kind, (s, e) in ((h.kind, h.span) for h in hits)
    )
    assert [hit.span[0] for hit in hits] == sorted(hit.span[0] for hit in hits)
    assert shape_hits("1234 5678 9012 3456") == []  # fails Luhn: not a card number
    assert shape_hits("no shapes here, just prose and a 2026 date") == []
    with pytest.raises(ValidationError):
        ShapeHit(kind="phone", span=(0, 1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        shape_hits(None)  # type: ignore[arg-type]


def test_shape_hits_never_block(guard: LeakGuard) -> None:
    observed = f"Our new account: {OTHER_IBAN}. Card on file {PAN[:-1]}2."
    assert shape_hits(observed)  # a shape is there
    assert guard.scan(observed) == []  # but no registered value is
    assert isinstance(guard.safe(observed), SafeStr)
    assert guard.redact(observed)[0] == observed


# --------------------------------------------------------------------------- safe_mapping


class Nested(BaseModel, frozen=True):
    note: str
    amount: Money
    tags: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Row:
    name: str
    count: int


def test_safe_mapping_recurses_and_mints_every_str(guard: LeakGuard) -> None:
    amount = Money.aed("12.50")
    obj: dict[str, Any] = {
        "tool": "reply_whatsapp",
        "args": {"text": "hello", "to": ["+971500000001", "+971500000002"], "pair": ("a", "b")},
        "model": Nested(note="n", amount=amount, tags=("x",)),
        "row": Row(name="r", count=2),
        "amount": amount,
        "n": 3,
        "flag": True,
        "none": None,
        "blob": b"\x00\xff\xfe binary",
        "set": frozenset({"s1", "s2"}),
    }
    out = guard.safe_mapping(obj)
    assert isinstance(out, dict)
    assert type(out["tool"]) is SafeStr
    assert all(type(key) is SafeStr for key in out)
    assert all(type(item) is SafeStr for item in out["args"]["to"])
    assert isinstance(out["args"]["to"], list) and isinstance(out["args"]["pair"], tuple)
    assert out["model"] == {"note": "n", "amount": amount, "tags": ("x",)}
    assert type(out["model"]["note"]) is SafeStr and out["model"]["amount"] is amount
    assert out["row"] == {"name": "r", "count": 2} and type(out["row"]["name"]) is SafeStr
    assert out["amount"] is amount and out["n"] == 3 and out["flag"] is True and out["none"] is None
    assert out["blob"] == obj["blob"]
    assert isinstance(out["set"], frozenset) and all(type(s) is SafeStr for s in out["set"])
    assert canonical_json(out) == canonical_json(obj)  # hashing is unchanged by scrubbing
    assert content_hash(out) == content_hash(obj)


@pytest.mark.parametrize(
    "obj",
    [
        {"text": IBAN},
        {"nested": {"deeper": [1, {"here": f"pay {IBAN}"}]}},
        {"tuple": ("x", PASSPHRASE)},
        {"model": Nested(note=PAN, amount=Money.zero())},
        {"row": Row(name=f"iban {IBAN}", count=1)},
        {IBAN: "the key leaks"},
        {"bytes": IBAN.encode("utf-8")},
        {"bytes-utf16": IBAN.encode("utf-16")},
        {"bytes-utf16-be": IBAN_FLAT.encode("utf-16-be")},
        {"bytes-latin1": f"pay {IBAN}".encode("latin-1")},
        {"set": {"a", PASSPHRASE}},
    ],
)
def test_safe_mapping_raises_on_a_hit_anywhere(guard: LeakGuard, obj: dict[str, Any]) -> None:
    with pytest.raises(Tier2LeakError):
        guard.safe_mapping(obj)


def test_safe_mapping_refuses_opaque_objects(guard: LeakGuard) -> None:
    """An object whose str() could carry anything is refused, never passed through unscanned."""

    class Opaque:
        def __str__(self) -> str:
            return IBAN

    with pytest.raises(TypeError, match="Opaque"):
        guard.safe_mapping({"o": Opaque()})
    with pytest.raises(TypeError):
        guard.safe_mapping({"nested": [object()]})


def test_safe_mapping_takes_a_mapping(guard: LeakGuard) -> None:
    with pytest.raises(TypeError):
        guard.safe_mapping(["not", "a", "mapping"])  # type: ignore[arg-type]
    assert guard.safe_mapping({}) == {}


def test_blocking_shape_hits_exclude_passport_shapes() -> None:
    """Order references like SO2026001 share the passport shape; Nour-authored text blocks on
    IBAN and card shapes only (lead decision recorded in nour/core/leakguard.py)."""
    text = f"order SO2026001 and PO1234567; IBAN {OTHER_IBAN}; card {PAN}; passport A12345678"
    kinds = {hit.kind for hit in blocking_shape_hits(text)}
    assert kinds == {"iban", "pan"}
    assert blocking_shape_hits("Your order SO2026001 ships Tuesday.") == []
    assert {hit.kind for hit in shape_hits(text)} == {"iban", "pan", "passport"}
