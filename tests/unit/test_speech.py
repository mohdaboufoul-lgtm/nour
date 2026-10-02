"""nour/language/speech.py (DESIGN §3.10; SPEC §9): Arabizi, vocabulary, WER, the recogniser.

Proves (MODULES.md "language"): Arabizi and code-switched samples of
``tests/fixtures/arabic_commands.yaml`` normalise (digit-letter tokens become Arabic script,
English words and numbers survive, Arabic script and English messages are untouched, the
function is idempotent); ``apply_vocabulary`` canonicalises names; ``normalise_arabic`` and
``word_error_rate`` agree with ``tools/stt_bakeoff.py``; ``ArabicSTT.transcribe`` passes the
locale and vocabulary to the engine; the bake-off picks the lowest-WER engine from the engines'
``wer_table``, counts operational failures and lets programming errors propagate; English
sentences with Arabizi homographs ("mesh", "dirham", "eh", "win7", "30 min") stay English.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from nour.core.ports import SttPort, Transcript
from nour.language.speech import (
    ArabicSTT,
    apply_vocabulary,
    looks_arabizi,
    needs_readback,
    normalise_arabic,
    normalise_arabizi,
    word_error_rate,
    word_errors,
)

REPO = Path(__file__).resolve().parents[2]
CORPUS: dict[str, Any] = yaml.safe_load(
    (REPO / "tests" / "fixtures" / "arabic_commands.yaml").read_text(encoding="utf-8")
)
COMMANDS = CORPUS["commands"]
ARABIZI = [pytest.param(c, id=c["id"]) for c in COMMANDS if c["script"] == "arabizi"]
MIXED = [pytest.param(c, id=c["id"]) for c in COMMANDS if c["script"] == "mixed"]
ENGLISH = [pytest.param(c, id=c["id"]) for c in COMMANDS if c["script"] == "english"]
ARABIC = [pytest.param(c, id=c["id"]) for c in COMMANDS if c["script"] == "arabic"]

ARABIZI_TOKEN = re.compile(r"[A-Za-z]*[2356789][A-Za-z]+|[A-Za-z]+[2356789]+[A-Za-z]*")
ARABIC_LETTERS = re.compile(r"[؀-ۿ]")


def _load_bakeoff_tool() -> Any:
    spec = importlib.util.spec_from_file_location("stt_bakeoff", REPO / "tools" / "stt_bakeoff.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("stt_bakeoff", module)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- Arabizi


def test_spec_examples() -> None:
    assert normalise_arabizi("3ala") == "على"
    assert normalise_arabizi("sho el 2akhbar") == "شو الأخبار"
    assert normalise_arabizi("ya nour 7awli 200 derhem") == "يا نور حولي 200 درهم"
    assert normalise_arabizi("msta3jil") == "مستعجل"
    assert normalise_arabizi("sho el 2akhbar? kel shi tamem?") == "شو الأخبار? كل شي تمام?"


@pytest.mark.parametrize("command", ARABIZI)
def test_arabizi_commands_normalise_to_arabic_script(command: dict[str, Any]) -> None:
    text = command["text"]
    out = normalise_arabizi(text)
    assert out != text
    assert looks_arabizi(text)
    leftover = [tok for tok in re.findall(r"[A-Za-z0-9]+", out) if ARABIZI_TOKEN.fullmatch(tok)]
    assert leftover == [], leftover  # every digit-letter token became Arabic
    assert ARABIC_LETTERS.search(out)
    for number in re.findall(r"(?<![A-Za-z0-9])\d+(?![A-Za-z0-9])", text):
        assert number in out  # amounts and references survive
    for token in ARABIC_LETTERS.findall(text):
        assert token in out
    assert "<PASSPHRASE>" not in text or "<PASSPHRASE>" in out
    assert normalise_arabizi(out) == out  # idempotent


@pytest.mark.parametrize("command", ENGLISH)
def test_english_commands_are_untouched(command: dict[str, Any]) -> None:
    assert normalise_arabizi(command["text"]) == command["text"]
    assert not looks_arabizi(command["text"])


@pytest.mark.parametrize("command", ARABIC)
def test_arabic_script_commands_are_untouched(command: dict[str, Any]) -> None:
    assert normalise_arabizi(command["text"]) == command["text"]


@pytest.mark.parametrize("command", MIXED)
def test_mixed_commands_keep_their_english_and_are_idempotent(command: dict[str, Any]) -> None:
    text = command["text"]
    out = normalise_arabizi(text)
    english = [
        tok
        for tok in re.findall(r"[A-Za-z]+", text)
        if tok.lower()
        in {
            "pay",
            "the",
            "bill",
            "from",
            "card",
            "please",
            "invoice",
            "and",
            "log",
            "it",
            "subscribe",
            "use",
            "share",
            "with",
            "book",
            "call",
            "client",
            "email",
            "reply",
            "cancel",
            "tomorrow",
            "driving",
            "trade",
            "licence",
            "expire",
            "flight",
            "friday",
            "evening",
            "emirates",
            "office",
            "lease",
            "renew",
            "samples",
            "arrive",
            "tuesday",
        }
    ]
    for token in english:
        assert token in out, (token, out)
    assert normalise_arabizi(out) == out


def test_code_switched_sample_converts_only_the_arabizi() -> None:
    out = normalise_arabizi(
        "yalla pay Al Noor Printing alf derhem من الكرت for invoice 4410 and log it"
    )
    assert out == "يلا pay Al Noor Printing ألف درهم من الكرت for invoice 4410 and log it"
    out2 = normalise_arabizi(
        "book a call with the Riyadh client Sunday 3pm their time, w 7otte el link bel invite"
    )
    assert "3pm" in out2 and "Riyadh client Sunday" in out2 and "حطي" in out2


def test_numbers_times_units_and_codes_are_not_arabizi() -> None:
    for token in ("3pm", "10am", "40L", "A5", "Q4", "B2B", "2FA", "mp3", "3321", "7781", "4k"):
        assert not looks_arabizi(token), token
        assert normalise_arabizi(token) == token
    assert (
        normalise_arabizi("the Atlas 40L backpack, 2,000 units")
        == "the Atlas 40L backpack, 2,000 units"
    )


@pytest.mark.parametrize(
    "text",
    [
        "eh, book a 30 min call w the client and send the invoice",
        "merci, send it in 5 min",
        "I was in Shi Fu restaurant; fi there?",
        "the mesh fabric samples arrive Tuesday",
        "send 500 dirham to Ahmed and ask Hal to confirm",
        "pay 200 dirham w note it",
        "wi-fi router is 300 dirham",
        "Kel and Bass are coming at 3pm",
        "install win7 driver and the x9 firmware",
        "men at work, bass guitar lesson at 5",
    ],
)
def test_english_sentences_with_arabizi_homographs_stay_english(text: str) -> None:
    assert not looks_arabizi(text)
    assert normalise_arabizi(text) == text


def test_short_lexicon_replies_and_mixed_messages_still_convert() -> None:
    assert normalise_arabizi("eh") == "إيه" and normalise_arabizi("bass") == "بس"
    assert normalise_arabizi("merci ktir") == "ميرسي كتير"
    assert normalise_arabizi("eh eh ماشي ابعتيه") == "إيه إيه ماشي ابعتيه"
    out = normalise_arabizi("sho el 7al? install win7 driver men el site")
    assert out.startswith("شو الحل?") and "install win7 driver" in out and "من ال site" in out
    assert (
        normalise_arabizi("ya nour 7awli 900 derhem b3d 30 min")
        == "يا نور حولي 900 درهم بعد 30 min"
    )
    assert normalise_arabizi("7awli 500 derhem min Ahmad") == "حولي 500 درهم من Ahmad"
    one_token = normalise_arabizi("meet me at the 3ammet place, el usual")
    assert not ARABIZI_TOKEN.search(one_token) and "ع" in one_token
    assert "el usual" in one_token  # one unknown digit token converts weak words nowhere


def test_unknown_arabizi_tokens_are_transliterated_best_effort() -> None:
    out = normalise_arabizi("ya nour 3ammet el 9adiye t3ab")
    assert not ARABIZI_TOKEN.search(out)
    assert out.startswith("يا نور ")
    assert "ق" in out and "ع" in out


def test_normalise_arabizi_rejects_non_text() -> None:
    with pytest.raises(TypeError):
        normalise_arabizi(None)  # type: ignore[arg-type]
    assert normalise_arabizi("") == ""


# --------------------------------------------------------------------------- vocabulary


def test_apply_vocabulary_canonicalises_names() -> None:
    vocabulary = ["Buzz Avenue", "مطبعة النور", "Gulf Packaging", "Rami"]
    text = "hi buzz avenue, مطبعه النور sent the gulf packaging quote to rami."
    out = apply_vocabulary(text, vocabulary)
    assert out == "hi Buzz Avenue, مطبعة النور sent the Gulf Packaging quote to Rami."
    assert apply_vocabulary("(buzz avenue) sent it", ["Buzz Avenue"]) == "(Buzz Avenue) sent it"
    assert apply_vocabulary("“buzz avenue” sent it", ["Buzz Avenue"]) == "“Buzz Avenue” sent it"
    assert apply_vocabulary("buzz-avenue, hi", ["Buzz Avenue"]) == "Buzz Avenue, hi"
    assert (
        apply_vocabulary("the buzz avenue-events team", ["Buzz Avenue"])
        == "the buzz avenue-events team"
    )
    assert apply_vocabulary("pay rami now", ["rami now", "Rami"]) == "pay rami now"


def test_apply_vocabulary_prefers_longer_terms_and_keeps_text_without_a_match() -> None:
    assert (
        apply_vocabulary("buzz avenue events", ["Buzz", "Buzz Avenue Events"])
        == "Buzz Avenue Events"
    )
    assert apply_vocabulary("nothing to see", ["Buzz Avenue"]) == "nothing to see"
    assert apply_vocabulary("nothing", []) == "nothing"
    assert apply_vocabulary("", ["Buzz Avenue"]) == ""
    with pytest.raises(TypeError):
        apply_vocabulary(None, ["x"])  # type: ignore[arg-type]


def test_apply_vocabulary_handles_tashkeel_and_hamza_variants() -> None:
    assert (
        apply_vocabulary("بَدّي أحجز مع أحمد إبراهيم", ["احمد ابراهيم"]) == "بَدّي أحجز مع احمد ابراهيم"
    )


# --------------------------------------------------------------------------- normalisation parity with tools/stt_bakeoff.py


SAMPLES = [
    "مُحَمَّد",
    "أحمد إبراهيم آمنة ٱلله",
    "مسؤول رئيس شيء",
    "شركة على مصطفى",
    "بـــزز ‏نور‎ ﻿أفنيو",
    "٣٠٠٠ درهم ۴ كيلو ٥٬٠٠٠ ريال",
    "مرحبا، كيفك؟ شو الأخبار! 5,000 AED and 3.5 kg, don't",
    "Buzz Avenue – sho el 2akhbar, 3ala tool",
    "ﻣﺮﺣﺒﺎ   a   b\tc\n d ",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_normalise_arabic_matches_the_bakeoff_tool(text: str) -> None:
    tool = _load_bakeoff_tool()
    assert normalise_arabic(text) == tool.normalize_arabic(text)
    assert normalise_arabic(normalise_arabic(text)) == normalise_arabic(text)


# --------------------------------------------------------------------------- WER


def test_word_error_rate() -> None:
    assert word_error_rate("بدي أحجز موعد بكرا", "بدي أحجز موعد بكرا") == 0.0
    assert word_error_rate("بَدّي أحجز موعد بكرا", "بدي احجز موعد بكرا") == 0.0  # diacritics only
    assert word_error_rate("بدي أحجز موعد بكرا", "بدي أحجز موعد اليوم") == pytest.approx(0.25)
    assert word_error_rate("a b c", "") == 1.0
    assert word_error_rate("a", "x y z") == 3.0
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "x y") == 2.0
    assert word_errors("حول ٣٠٠٠ درهم لشركة بز أفنيو", "حوّل 3000 درهم لشركه بز افنيو") == (0, 6)


def test_word_error_rate_agrees_with_the_bakeoff_tool() -> None:
    tool = _load_bakeoff_tool()
    ref, hyp = "بدي أحجز موعد بكرا الساعة ٤", "بدي احجز موعد اليوم الساعة 4 بالمكتب"
    counts = tool.word_error_rate(
        tool.tokenize(tool.normalize_arabic(ref)), tool.tokenize(tool.normalize_arabic(hyp))
    )
    assert word_error_rate(ref, hyp) == pytest.approx(counts.rate())


# --------------------------------------------------------------------------- the recogniser


class RecordingEngine:
    """A local ``SttPort``: returns the reference text hidden in the audio bytes, corrupted at
    the word error rate ``wer_table`` assigns to it; records every call."""

    def __init__(self, engine: str, wer: float = 0.0, *, fail: bool = False) -> None:
        self.engine = engine
        self.wer_table: dict[str, float] = {engine: wer}
        self.fail = fail
        self.calls: list[tuple[bytes, str, tuple[str, ...]]] = []

    def transcribe(self, audio: bytes, *, language: str, vocabulary: Sequence[str]) -> Transcript:
        self.calls.append((audio, language, tuple(vocabulary)))
        if self.fail:
            raise RuntimeError(f"{self.engine} is down")
        words = audio.decode("utf-8").split()
        wer = self.wer_table[self.engine]
        wrong = round(len(words) * wer)
        for index in range(wrong):
            words[index] = "xxx"
        return Transcript(
            text=" ".join(words), language=language, confidence=0.93, engine=self.engine
        )


@pytest.fixture
def samples() -> list[tuple[bytes, str]]:
    references = [
        "بدي أحجز موعد بكرا الساعة أربعة بالمكتب مع خالد",
        "ادفعي فاتورة أرامكس مئتين درهم من كرت Buzz Avenue رقمها سبعة",
        "حوّلي ألف درهم لشركة الخليج للتغليف عن فاتورة الشهر",
        "ذكريني بكرا الصبح ادفع الفيزا تبع الولد قبل الظهر",
    ]
    return [(ref.encode("utf-8"), ref) for ref in references]


def test_engines_satisfy_the_port() -> None:
    assert isinstance(RecordingEngine("azure_ar_lb"), SttPort)


def test_constructor_validates_engines() -> None:
    with pytest.raises(ValueError):
        ArabicSTT({}, "azure", [])
    with pytest.raises(ValueError):
        ArabicSTT({"azure": RecordingEngine("azure")}, "google", [])
    stt = ArabicSTT({"azure": RecordingEngine("azure")}, "azure", ["Buzz Avenue", " ", ""])
    assert stt.primary == "azure" and stt.language == "ar-LB"
    assert stt.vocabulary == ("Buzz Avenue",)


def test_transcribe_passes_locale_and_vocabulary_and_canonicalises() -> None:
    engine = RecordingEngine("azure_ar_lb")
    stt = ArabicSTT(
        {"azure_ar_lb": engine}, "azure_ar_lb", ["Buzz Avenue", "مطبعة النور"], language="ar-LB"
    )
    audio = "ادفعي مئتين درهم لمطبعه النور من كرت buzz avenue".encode()
    transcript = stt.transcribe(audio, "whatsapp/audio/wamid.1")
    assert engine.calls == [(audio, "ar-LB", ("Buzz Avenue", "مطبعة النور"))]
    assert transcript.text == "ادفعي مئتين درهم لمطبعه النور من كرت Buzz Avenue"
    assert transcript.engine == "azure_ar_lb" and transcript.language == "ar-LB"
    assert transcript.confidence == pytest.approx(0.93)
    assert not needs_readback(transcript)
    assert needs_readback(Transcript(text="x", language="ar-LB", confidence=0.5, engine="e"))


def test_transcribe_refuses_empty_audio() -> None:
    stt = ArabicSTT({"azure": RecordingEngine("azure")}, "azure", [])
    with pytest.raises(ValueError, match="wamid.7"):
        stt.transcribe(b"", "whatsapp/audio/wamid.7")


def test_bake_off_scores_every_engine_by_corpus_wer(samples: list[tuple[bytes, str]]) -> None:
    engines = {
        "whisper": RecordingEngine("whisper", 0.5),
        "azure_ar_lb": RecordingEngine("azure_ar_lb", 0.1),
        "google_ar_lb": RecordingEngine("google_ar_lb", 0.3),
        "scribe": RecordingEngine("scribe", 0.0, fail=True),
    }
    stt = ArabicSTT(engines, "whisper", ["Buzz Avenue"])
    table = stt.bake_off(samples)
    assert set(table) == set(engines)
    assert table["azure_ar_lb"] < table["google_ar_lb"] < table["whisper"]
    assert table["azure_ar_lb"] == pytest.approx(0.1, abs=0.06)
    assert table["scribe"] == 1.0  # a failing engine scores every word as deleted
    assert stt.last_failures == {"whisper": 0, "azure_ar_lb": 0, "google_ar_lb": 0, "scribe": 4}
    for engine in engines.values():
        assert len(engine.calls) == len(samples)
        assert all(call[1] == "ar-LB" and call[2] == ("Buzz Avenue",) for call in engine.calls)
    assert stt.best_engine(samples) == "azure_ar_lb"


class MisWiredEngine:
    """An engine whose call is a programming error, not an operational failure."""

    engine = "broken"

    def transcribe(self, audio: bytes, *, language: str, vocabulary: Sequence[str]) -> Transcript:
        raise TypeError("transcribe() got an unexpected keyword argument 'vocabulary'")


class TimingOutEngine:
    engine = "slow"

    def transcribe(self, audio: bytes, *, language: str, vocabulary: Sequence[str]) -> Transcript:
        raise TimeoutError("vendor did not answer")


def test_bake_off_counts_operational_failures_and_propagates_programming_errors(
    samples: list[tuple[bytes, str]],
) -> None:
    stt = ArabicSTT({"slow": TimingOutEngine(), "ok": RecordingEngine("ok")}, "ok", [])
    assert stt.last_failures == {}
    table = stt.bake_off(samples)
    assert table == {"slow": 1.0, "ok": 0.0} and stt.last_failures == {"slow": 4, "ok": 0}
    broken = ArabicSTT({"broken": MisWiredEngine(), "ok": RecordingEngine("ok")}, "ok", [])
    with pytest.raises(TypeError, match="vocabulary"):
        broken.bake_off(samples)


def test_bake_off_tie_prefers_the_primary(samples: list[tuple[bytes, str]]) -> None:
    engines = {"b": RecordingEngine("b", 0.0), "a": RecordingEngine("a", 0.0)}
    assert ArabicSTT(engines, "b", []).best_engine(samples) == "b"
    assert ArabicSTT(engines, "a", []).best_engine(samples) == "a"
    with pytest.raises(ValueError):
        ArabicSTT(engines, "a", []).bake_off([])
