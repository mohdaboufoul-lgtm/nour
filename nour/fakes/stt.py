"""``FakeStt`` and ``FakeTts``: Arabic speech in and out (DESIGN §3.9 §6; SPEC §3 §9 §16).

``FakeStt`` returns the scripted ``Transcript`` for a voice note. The port takes bytes, not a
reference, so the script is keyed by the ``audio_ref`` that ``FakeWhatsApp.fetch_media`` encodes
into its marker bytes (``AUDIO_MARKER + ref``) or by the SHA-256 of arbitrary bytes; an unscripted
note yields an empty, zero-confidence transcript, which is what forces a read-back (SPEC §9).
``wer_table`` holds the per-engine word-error rates the bake-off (``ArabicSTT.bake_off``, SPEC §9
"selection by test") ranks. ``calls`` records every request so a test can assert the custom
vocabulary was passed and that no Tier 2 audio reached a non-in-region engine.

``FakeTts`` synthesises ``b"TTS:" + text`` for the one locked voice (SPEC §3): deterministic bytes,
every call recorded with its ``PortCall`` and its character count (the AI-model budget line).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from nour.core.clock import Clock
from nour.core.ports import CallLog, PortCall, Transcript
from nour.core.types import SafeStr
from nour.fakes import FakePort
from nour.fakes.whatsapp import AUDIO_MARKER

DEFAULT_ENGINE = "fake-stt"
DEFAULT_WER_TABLE: Mapping[str, float] = {
    "fake-stt": 0.12,
    "azure-ar-lb": 0.18,
    "google-ar-lb": 0.21,
    "whisper-large-v3": 0.30,
}
"""Illustrative corpus WERs (docs/adapters/speech.md §4): the fake engine wins the bake-off."""


def audio_ref_of(audio: bytes) -> str | None:
    """The ``audio_ref`` behind marker bytes from ``FakeWhatsApp.fetch_media``; ``None`` otherwise."""
    if audio.startswith(AUDIO_MARKER):
        return audio[len(AUDIO_MARKER) :].decode("utf-8", "replace")
    return None


class FakeStt(FakePort):
    """SttPort fake: ``script`` (by audio_ref or sha256 hex) → ``Transcript``."""

    port_name: str = "stt"

    def __init__(
        self,
        call_log: CallLog | None = None,
        clock: Clock | None = None,
        *,
        engine: str = DEFAULT_ENGINE,
        script: Mapping[str, Transcript] | None = None,
        wer_table: Mapping[str, float] | None = None,
    ) -> None:
        super().__init__(call_log, clock)
        self.engine = engine
        self.script: dict[str, Transcript] = dict(script or {})
        self.wer_table: dict[str, float] = dict(
            wer_table if wer_table is not None else DEFAULT_WER_TABLE
        )
        self.calls: list[tuple[bytes, str, tuple[str, ...]]] = []

    def script_ref(
        self,
        audio_ref: str,
        text: str,
        *,
        language: str = "ar-LB",
        confidence: float = 0.93,
    ) -> Transcript:
        """Script the transcript of the voice note ``audio_ref`` (the owner's voice, SPEC §9)."""
        transcript = Transcript(
            text=text, language=language, confidence=confidence, engine=self.engine
        )
        self.script[audio_ref] = transcript
        return transcript

    def transcribe(self, audio: bytes, *, language: str, vocabulary: Sequence[str]) -> Transcript:
        """The scripted transcript for ``audio`` (by marker ref, then by sha256), else an empty
        zero-confidence one. Records ``(audio, language, vocabulary)``; ``vocabulary`` is a
        sequence of terms (a bare ``str`` would be recorded per character: ``TypeError``)."""
        if not isinstance(audio, bytes | bytearray):
            raise TypeError("transcribe takes audio bytes")
        if isinstance(vocabulary, str | bytes):
            raise TypeError("transcribe takes a sequence of vocabulary terms, not a bare str")
        audio = bytes(audio)
        self.calls.append((audio, language, tuple(vocabulary)))
        self._maybe_fail("transcribe")
        if not audio:
            return Transcript(text="", language=language, confidence=0.0, engine=self.engine)
        ref = audio_ref_of(audio)
        hit = self.script.get(ref) if ref is not None else None
        if hit is None:
            hit = self.script.get(hashlib.sha256(audio).hexdigest())
        if hit is None:
            return Transcript(text="", language=language, confidence=0.0, engine=self.engine)
        return Transcript(
            text=hit.text, language=hit.language, confidence=hit.confidence, engine=self.engine
        )


class FakeTts(FakePort):
    """TtsPort fake: ``synthesize`` → ``b"TTS:" + text``; records every call."""

    port_name: str = "tts"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.synthesized: list[SafeStr] = []
        self.chars_billed = 0

    def synthesize(self, call: PortCall, text: SafeStr) -> bytes:
        if not isinstance(text, SafeStr):
            raise TypeError("synthesize takes a SafeStr (text Nour emits is scrubbed first)")
        self._record("synthesize", call, text=text)
        self._maybe_fail("synthesize")
        self.synthesized.append(text)
        self.chars_billed += len(text)
        return b"TTS:" + text.encode("utf-8")
