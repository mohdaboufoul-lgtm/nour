#!/usr/bin/env python3
"""Arabic speech-to-text bake-off scorer (SPEC section 9 "Arabic speech pipeline",
section 16 week-one checklist "Arabic speech bake-off on 50 owner voice notes").

Scores one or more recogniser outputs against human reference transcripts and
ranks the engines by word error rate (WER). Character error rate (CER) and a
custom-vocabulary recall score (company, product, staff and customer names) are
reported alongside.

Layout expected on disk::

    refs/            <id>.ref.txt   one UTF-8 reference transcript per voice note
    out/azure_lb/    <id>.hyp.txt   one hypothesis per voice note, per engine
    out/google/      <id>.hyp.txt
    out/scribe/      <id>.hyp.txt
    out/whisper/     <id>.hyp.txt

Usage::

    python tools/stt_bakeoff.py --refs refs --hyps out/azure_lb out/google out/scribe out/whisper \\
        --custom-vocab config/speech_vocab.txt --report reports/bakeoff.json

    python tools/stt_bakeoff.py --refs refs --hyps azure=out/azure_lb --show-alignment note_017

Design notes
------------
* Standard library only; PyYAML is imported lazily and only if a ``.yaml``
  vocabulary file is given.
* ``normalize_arabic``, ``tokenize``, ``align``, ``levenshtein_distance``,
  ``word_error_rate``, ``character_error_rate`` and ``vocabulary_recall`` are
  pure functions so they can be unit-tested and reused by the runtime adapter.
* Everything is deterministic: files are processed in sorted order, ties in
  the alignment are broken in a fixed order, and the JSON report carries no
  timestamps unless ``--stamp`` is passed.
* An engine that produced no output for a reference id is scored as if it had
  returned an empty transcript (100 percent deletions) and the id is listed under
  ``files_missing``; pass ``--skip-missing`` to exclude those ids instead.

Normalisation (applied identically to references and hypotheses)
---------------------------------------------------------------
1. Unicode NFKC (folds Arabic presentation forms such as U+FE70..U+FEFF).
2. Alef variants (U+0622, U+0623, U+0625, U+0671) -> bare alef U+0627.
3. Hamza carriers: U+0624 (waw with hamza) -> U+0648, U+0626 (yeh with hamza) -> U+064A.
   Standalone hamza U+0621 is kept.
4. Taa marbuta U+0629 -> haa U+0647; alef maqsura U+0649 -> yeh U+064A.
5. Tatweel U+0640 and zero-width / bidi control characters removed.
6. All combining marks (Unicode category Mn) removed: fatha, damma, kasra,
   tanween, shadda, sukun, superscript alef, Quranic annotation marks.
7. Arabic-Indic (U+0660..U+0669) and Extended Arabic-Indic (U+06F0..U+06F9)
   digits -> ASCII digits.
8. Punctuation and symbols -> space, except separators between two digits
   (``5,000`` -> ``5000``, ``٣٫٥`` -> ``35``) and apostrophes inside Latin
   words (``don't`` -> ``dont``), which are dropped.
9. Latin text case-folded (code-switching and Arabizi tokens such as
   ``3ala`` or ``sho el 2akhbar`` are kept verbatim; Arabizi -> Arabic
   transliteration is out of scope here).
10. Whitespace collapsed to single spaces.

CER is computed over the characters of the normalised text with whitespace
removed, so it is insensitive to word-segmentation disagreements
(``ما بدي`` vs ``مابدي``) that WER penalises.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "EditCounts",
    "EngineReport",
    "FileScore",
    "Op",
    "align",
    "character_error_rate",
    "levenshtein_distance",
    "load_vocabulary",
    "main",
    "normalize_arabic",
    "score_engine",
    "tokenize",
    "vocabulary_recall",
    "word_error_rate",
]

REPORT_VERSION = "1"

# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_ALEF = "ا"
_ALEF_VARIANTS = {
    0x0622: _ALEF,  # alef with madda above
    0x0623: _ALEF,  # alef with hamza above
    0x0625: _ALEF,  # alef with hamza below
    0x0671: _ALEF,  # alef wasla
}
_HAMZA_CARRIERS = {
    0x0624: "و",  # waw with hamza above -> waw
    0x0626: "ي",  # yeh with hamza above -> yeh
}
_TAA_MARBUTA = {0x0629: "ه"}  # taa marbuta -> haa
_ALEF_MAQSURA = {0x0649: "ي"}  # alef maqsura -> yeh
_REMOVED = {
    0x0640: None,  # tatweel / kashida
    0x200B: None,  # zero width space
    0x200C: None,  # zero width non-joiner
    0x200D: None,  # zero width joiner
    0x200E: None,  # left-to-right mark
    0x200F: None,  # right-to-left mark
    0x202A: None,
    0x202B: None,
    0x202C: None,
    0x202D: None,
    0x202E: None,
    0x2066: None,
    0x2067: None,
    0x2068: None,
    0x2069: None,
    0xFEFF: None,  # byte order mark
}
_DIGITS = {}
for _i in range(10):
    _DIGITS[0x0660 + _i] = str(_i)  # Arabic-Indic
    _DIGITS[0x06F0 + _i] = str(_i)  # Extended Arabic-Indic (Persian/Urdu)

_DIGIT_SEPARATORS = frozenset(",.٫٬’'")
_WORD_APOSTROPHES = frozenset("'’")


def normalize_arabic(
    text: str,
    *,
    taa_marbuta: bool = True,
    alef_maqsura: bool = True,
    hamza: bool = True,
    digits: bool = True,
    lowercase: bool = True,
) -> str:
    """Return ``text`` normalised for fair WER comparison (see module docstring).

    The function is idempotent: ``normalize_arabic(normalize_arabic(t)) ==
    normalize_arabic(t)``.
    """
    text = unicodedata.normalize("NFKC", text)

    table: dict[int, str | None] = {}
    table.update(_ALEF_VARIANTS)
    table.update(_REMOVED)
    if hamza:
        table.update(_HAMZA_CARRIERS)
    if taa_marbuta:
        table.update(_TAA_MARBUTA)
    if alef_maqsura:
        table.update(_ALEF_MAQSURA)
    if digits:
        table.update(_DIGITS)
    text = text.translate(table)

    out: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat == "Mn":  # combining marks: tashkeel, Quranic annotations, accents
            continue
        if cat[0] in "PS":  # punctuation and symbols
            prev_ch = text[i - 1] if i > 0 else ""
            next_ch = text[i + 1] if i + 1 < n else ""
            if ch in _DIGIT_SEPARATORS and prev_ch.isdigit() and next_ch.isdigit():
                continue  # 5,000 -> 5000
            if ch in _WORD_APOSTROPHES and prev_ch.isalpha() and next_ch.isalpha():
                continue  # don't -> dont
            out.append(" ")
            continue
        if cat[0] == "Z" or cat == "Cc":  # separators and control characters
            out.append(" ")
            continue
        out.append(ch)
    text = "".join(out)
    if lowercase:
        text = text.casefold()
    return " ".join(text.split())


def tokenize(text: str) -> list[str]:
    """Split normalised text into word tokens (whitespace separated)."""
    return text.split()


# --------------------------------------------------------------------------- #
# Alignment and error rates
# --------------------------------------------------------------------------- #

Op = tuple[str, int | None, int | None]
"""One alignment step: (kind, ref_index, hyp_index) with kind in
{"hit", "sub", "del", "ins"}. ``ref_index`` is None for insertions and
``hyp_index`` is None for deletions."""


@dataclass(frozen=True)
class EditCounts:
    """Edit statistics of a hypothesis against a reference."""

    hits: int
    substitutions: int
    deletions: int
    insertions: int
    ref_len: int
    hyp_len: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    def rate(self) -> float | None:
        """Error rate (errors / ref_len); None when the reference is empty."""
        if self.ref_len == 0:
            return None
        return self.errors / self.ref_len

    def __add__(self, other: EditCounts) -> EditCounts:
        return EditCounts(
            self.hits + other.hits,
            self.substitutions + other.substitutions,
            self.deletions + other.deletions,
            self.insertions + other.insertions,
            self.ref_len + other.ref_len,
            self.hyp_len + other.hyp_len,
        )


_ZERO = EditCounts(0, 0, 0, 0, 0, 0)


def align(ref: Sequence[str], hyp: Sequence[str]) -> tuple[EditCounts, list[Op]]:
    """Levenshtein-align ``hyp`` to ``ref`` (unit costs) and return counts plus
    the alignment operations in reference order.

    Ties are broken deterministically: diagonal (hit/substitution) first, then
    deletion, then insertion.
    """
    n, m = len(ref), len(hyp)
    # dist[i][j] = edit distance between ref[:i] and hyp[:j]
    dist = [[0] * (m + 1) for _ in range(n + 1)]
    back = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dist[i][0] = i
        back[i][0] = "del"
    for j in range(1, m + 1):
        dist[0][j] = j
        back[0][j] = "ins"
    for i in range(1, n + 1):
        ri = ref[i - 1]
        row, prev = dist[i], dist[i - 1]
        brow = back[i]
        for j in range(1, m + 1):
            if ri == hyp[j - 1]:
                row[j] = prev[j - 1]
                brow[j] = "hit"
                continue
            diag = prev[j - 1] + 1
            up = prev[j] + 1
            left = row[j - 1] + 1
            best = min(diag, up, left)
            row[j] = best
            if best == diag:
                brow[j] = "sub"
            elif best == up:
                brow[j] = "del"
            else:
                brow[j] = "ins"

    ops: list[Op] = []
    i, j = n, m
    hits = subs = dels = ins = 0
    while i > 0 or j > 0:
        kind = back[i][j]
        if kind == "hit":
            ops.append(("hit", i - 1, j - 1))
            hits += 1
            i, j = i - 1, j - 1
        elif kind == "sub":
            ops.append(("sub", i - 1, j - 1))
            subs += 1
            i, j = i - 1, j - 1
        elif kind == "del":
            ops.append(("del", i - 1, None))
            dels += 1
            i -= 1
        else:
            ops.append(("ins", None, j - 1))
            ins += 1
            j -= 1
    ops.reverse()
    return EditCounts(hits, subs, dels, ins, n, m), ops


def levenshtein_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Plain edit distance (two-row DP, no backtrace); used for CER."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def word_error_rate(ref_tokens: Sequence[str], hyp_tokens: Sequence[str]) -> EditCounts:
    """Word-level edit counts; ``counts.rate()`` is the WER."""
    counts, _ = align(ref_tokens, hyp_tokens)
    return counts


def character_error_rate(ref_norm: str, hyp_norm: str) -> EditCounts:
    """Character-level edit counts over normalised text with whitespace removed.

    Only the total number of edits is known (no backtrace), so it is reported as
    substitutions; ``counts.rate()`` is the CER.
    """
    ref_chars = "".join(ref_norm.split())
    hyp_chars = "".join(hyp_norm.split())
    edits = levenshtein_distance(ref_chars, hyp_chars)
    return EditCounts(
        hits=max(len(ref_chars) - edits, 0),
        substitutions=edits,
        deletions=0,
        insertions=0,
        ref_len=len(ref_chars),
        hyp_len=len(hyp_chars),
    )


def vocabulary_recall(
    ref_tokens: Sequence[str],
    ops: Sequence[Op],
    vocabulary: Iterable[Sequence[str]],
) -> dict[str, tuple[int, int]]:
    """Per-term (recalled, total) counts of vocabulary occurrences in ``ref_tokens``.

    A vocabulary term (one or more normalised tokens) occurring at reference
    positions ``i..i+k-1`` counts as recalled only if every one of those
    positions is an exact hit in the alignment ``ops`` (from :func:`align`).
    Terms that never occur in the reference get ``(0, 0)``.
    """
    hits = {r for kind, r, _ in ops if kind == "hit" and r is not None}
    result: dict[str, tuple[int, int]] = {}
    for term in vocabulary:
        term = list(term)
        key = " ".join(term)
        k = len(term)
        if k == 0:
            continue
        recalled = total = 0
        for i in range(len(ref_tokens) - k + 1):
            if list(ref_tokens[i : i + k]) == term:
                total += 1
                if all((i + offset) in hits for offset in range(k)):
                    recalled += 1
        prev_r, prev_t = result.get(key, (0, 0))
        result[key] = (prev_r + recalled, prev_t + total)
    return result


# --------------------------------------------------------------------------- #
# Scoring a corpus
# --------------------------------------------------------------------------- #


@dataclass
class FileScore:
    file_id: str
    words: EditCounts
    chars: EditCounts
    missing: bool
    vocab: dict[str, tuple[int, int]] = field(default_factory=dict)
    ops: list[Op] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "wer": self.words.rate(),
            "cer": self.chars.rate(),
            "ref_words": self.words.ref_len,
            "hyp_words": self.words.hyp_len,
            "hits": self.words.hits,
            "substitutions": self.words.substitutions,
            "deletions": self.words.deletions,
            "insertions": self.words.insertions,
            "ref_chars": self.chars.ref_len,
            "char_edits": self.chars.errors,
            "missing": self.missing,
        }


@dataclass
class EngineReport:
    name: str
    files: list[FileScore]
    files_missing: list[str]

    @property
    def words(self) -> EditCounts:
        return sum((f.words for f in self.files), _ZERO)

    @property
    def chars(self) -> EditCounts:
        return sum((f.chars for f in self.files), _ZERO)

    @property
    def corpus_wer(self) -> float | None:
        return self.words.rate()

    @property
    def corpus_cer(self) -> float | None:
        return self.chars.rate()

    @property
    def file_wers(self) -> list[float]:
        return [r for f in self.files if (r := f.words.rate()) is not None]

    @property
    def vocab_totals(self) -> dict[str, tuple[int, int]]:
        totals: dict[str, tuple[int, int]] = {}
        for f in self.files:
            for term, (r, t) in f.vocab.items():
                pr, pt = totals.get(term, (0, 0))
                totals[term] = (pr + r, pt + t)
        return totals

    @property
    def vocab_recall(self) -> float | None:
        totals = self.vocab_totals
        total = sum(t for _, t in totals.values())
        if total == 0:
            return None
        return sum(r for r, _ in totals.values()) / total

    def to_json(self) -> dict[str, object]:
        w, c = self.words, self.chars
        wers = self.file_wers
        totals = self.vocab_totals
        vocab_total = sum(t for _, t in totals.values())
        return {
            "corpus_wer": self.corpus_wer,
            "corpus_cer": self.corpus_cer,
            "mean_file_wer": statistics.fmean(wers) if wers else None,
            "median_file_wer": statistics.median(wers) if wers else None,
            "ref_words": w.ref_len,
            "hyp_words": w.hyp_len,
            "hits": w.hits,
            "substitutions": w.substitutions,
            "deletions": w.deletions,
            "insertions": w.insertions,
            "ref_chars": c.ref_len,
            "char_edits": c.errors,
            "files_scored": len(self.files),
            "files_missing": list(self.files_missing),
            "vocab": {
                "recall": self.vocab_recall,
                "recalled": sum(r for r, _ in totals.values()),
                "total": vocab_total,
                "terms": {
                    term: {"recalled": r, "total": t} for term, (r, t) in sorted(totals.items())
                },
            },
            "per_file": {f.file_id: f.to_json() for f in self.files},
        }


def score_pair(
    file_id: str,
    ref_text: str,
    hyp_text: str | None,
    vocabulary: Sequence[Sequence[str]],
    *,
    normalize: bool = True,
) -> FileScore:
    """Score one hypothesis against one reference. ``hyp_text`` None means the
    engine produced no output for this id."""
    ref_norm = normalize_arabic(ref_text) if normalize else " ".join(ref_text.split())
    hyp_raw = hyp_text or ""
    hyp_norm = normalize_arabic(hyp_raw) if normalize else " ".join(hyp_raw.split())
    ref_tokens, hyp_tokens = tokenize(ref_norm), tokenize(hyp_norm)
    words, ops = align(ref_tokens, hyp_tokens)
    chars = character_error_rate(ref_norm, hyp_norm)
    vocab = vocabulary_recall(ref_tokens, ops, vocabulary) if vocabulary else {}
    return FileScore(file_id, words, chars, hyp_text is None, vocab, ops)


def score_engine(
    name: str,
    refs: dict[str, str],
    hyps: dict[str, str],
    vocabulary: Sequence[Sequence[str]] = (),
    *,
    skip_missing: bool = False,
    normalize: bool = True,
) -> EngineReport:
    """Score an engine's hypotheses (``id -> text``) against references."""
    files: list[FileScore] = []
    missing: list[str] = []
    for file_id in sorted(refs):
        hyp = hyps.get(file_id)
        if hyp is None:
            missing.append(file_id)
            if skip_missing:
                continue
        files.append(score_pair(file_id, refs[file_id], hyp, vocabulary, normalize=normalize))
    return EngineReport(name, files, missing)


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #


def read_transcripts(directory: Path, suffix: str) -> dict[str, str]:
    """Map ``<id>`` -> text for every ``<id><suffix>`` file in ``directory``."""
    if not directory.is_dir():
        raise FileNotFoundError(f"not a directory: {directory}")
    out: dict[str, str] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name.endswith(suffix) and len(path.name) > len(suffix):
            out[path.name[: -len(suffix)]] = path.read_text(encoding="utf-8")
    return out


def load_vocabulary(path: Path, *, normalize: bool = True) -> list[list[str]]:
    """Load custom-vocabulary terms (one per line in ``.txt``; a list or a
    mapping of category -> list in ``.yaml``/``.yml``) as normalised token lists,
    de-duplicated and in a stable order."""
    raw_terms: list[str] = []
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml  # PyYAML, imported lazily so .txt vocabularies need no dependency

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(data, dict):
            for _category, items in data.items():
                if isinstance(items, str):
                    raw_terms.append(items)
                elif items:
                    raw_terms.extend(str(x) for x in items)
        elif isinstance(data, list):
            raw_terms.extend(str(x) for x in data)
        else:
            raise ValueError(f"unsupported vocabulary YAML shape in {path}")
    else:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw_terms.append(line)

    seen: set[str] = set()
    terms: list[list[str]] = []
    for raw in raw_terms:
        norm = normalize_arabic(raw) if normalize else " ".join(raw.split())
        if norm and norm not in seen:
            seen.add(norm)
            terms.append(tokenize(norm))
    return terms


def parse_engine_arg(arg: str) -> tuple[str, Path]:
    """``name=path`` or ``path`` (name = directory basename)."""
    if "=" in arg:
        name, _, raw = arg.partition("=")
        return name.strip(), Path(raw)
    path = Path(arg)
    return path.name or str(path), path


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def _pct(value: float | None) -> str:
    return "   n/a" if value is None else f"{100 * value:6.2f}"


def rank_engines(reports: Sequence[EngineReport]) -> list[EngineReport]:
    """Lowest corpus WER first; ties broken by CER, then vocab recall (higher is
    better), then name. Engines with no scorable words sort last."""

    def key(r: EngineReport) -> tuple[int, float, float, float, str]:
        wer = r.corpus_wer
        cer = r.corpus_cer
        recall = r.vocab_recall
        return (
            0 if wer is not None else 1,
            wer if wer is not None else 0.0,
            cer if cer is not None else 0.0,
            -(recall if recall is not None else 0.0),
            r.name,
        )

    return sorted(reports, key=key)


def format_table(ranked: Sequence[EngineReport], with_vocab: bool) -> str:
    name_w = max([len(r.name) for r in ranked] + [6])
    header = (
        f"{'#':>2}  {'engine':<{name_w}}  {'WER%':>6}  {'CER%':>6}  {'meanWER%':>8}  "
        f"{'words':>6}  {'S':>5}  {'D':>5}  {'I':>5}  {'files':>5}  {'miss':>4}"
    )
    if with_vocab:
        header += f"  {'vocab%':>6}  {'vocab':>7}"
    lines = [header, "-" * len(header)]
    for pos, r in enumerate(ranked, start=1):
        w = r.words
        wers = r.file_wers
        mean = statistics.fmean(wers) if wers else None
        line = (
            f"{pos:>2}  {r.name:<{name_w}}  {_pct(r.corpus_wer)}  {_pct(r.corpus_cer)}  "
            f"{_pct(mean):>8}  {w.ref_len:>6}  {w.substitutions:>5}  {w.deletions:>5}  "
            f"{w.insertions:>5}  {len(r.files):>5}  {len(r.files_missing):>4}"
        )
        if with_vocab:
            totals = r.vocab_totals
            rec = sum(x for x, _ in totals.values())
            tot = sum(x for _, x in totals.values())
            line += f"  {_pct(r.vocab_recall)}  {rec:>3}/{tot:<3}"
        lines.append(line)
    return "\n".join(lines)


def format_alignment(score: FileScore, ref_tokens: Sequence[str], hyp_tokens: Sequence[str]) -> str:
    rows = []
    for kind, r, h in score.ops:
        ref_tok = ref_tokens[r] if r is not None else "-"
        hyp_tok = hyp_tokens[h] if h is not None else "-"
        rows.append(f"{kind:<4} {ref_tok:<24} {hyp_tok}")
    return "\n".join(rows)


def build_report(
    ranked: Sequence[EngineReport],
    *,
    refs_dir: Path,
    file_ids: Sequence[str],
    vocab_path: Path | None,
    vocabulary: Sequence[Sequence[str]],
    normalize: bool,
    skip_missing: bool,
    stamp: str | None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "tool": "stt_bakeoff.py",
        "report_version": REPORT_VERSION,
        "refs_dir": str(refs_dir),
        "files": list(file_ids),
        "normalization": {
            "enabled": normalize,
            "steps": [
                "NFKC",
                "alef variants -> alef",
                "hamza on waw/yeh -> waw/yeh",
                "taa marbuta -> haa",
                "alef maqsura -> yeh",
                "tatweel and zero-width marks removed",
                "combining marks (tashkeel) removed",
                "Arabic-Indic digits -> ASCII",
                "punctuation -> space (digit separators dropped)",
                "Latin casefold",
                "whitespace collapsed",
            ],
            "cer_ignores_whitespace": True,
        },
        "skip_missing": skip_missing,
        "custom_vocab": {
            "path": str(vocab_path) if vocab_path else None,
            "terms": [" ".join(t) for t in vocabulary],
        },
        "ranking": [
            {"rank": i, "engine": r.name, "corpus_wer": r.corpus_wer, "corpus_cer": r.corpus_cer}
            for i, r in enumerate(ranked, start=1)
        ],
        "engines": {r.name: r.to_json() for r in ranked},
    }
    if stamp:
        report["generated_at"] = stamp
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stt_bakeoff.py",
        description="Score Arabic STT engine outputs against reference transcripts "
        "(WER, CER, custom-vocabulary recall) and rank the engines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Engines are given as directories; prefix with 'name=' to override the\n"
            "name derived from the directory basename, e.g. azure_lb=out/run3/azure."
        ),
    )
    p.add_argument("--refs", required=True, type=Path, help="directory of <id>.ref.txt files")
    p.add_argument(
        "--hyps",
        required=True,
        nargs="+",
        metavar="ENGINE_DIR",
        help="one or more directories of <id>.hyp.txt files (optionally name=path)",
    )
    p.add_argument("--ref-suffix", default=".ref.txt", help="reference filename suffix")
    p.add_argument("--hyp-suffix", default=".hyp.txt", help="hypothesis filename suffix")
    p.add_argument(
        "--custom-vocab",
        type=Path,
        default=None,
        help="vocabulary terms (.txt one per line, or .yaml list / category mapping) "
        "for the recall score",
    )
    p.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    p.add_argument(
        "--skip-missing",
        action="store_true",
        help="exclude ids an engine did not transcribe instead of scoring them as deletions",
    )
    p.add_argument(
        "--no-normalize",
        action="store_true",
        help="compare raw text (whitespace-collapsed only); for debugging",
    )
    p.add_argument(
        "--show-alignment",
        metavar="ID",
        default=None,
        help="print the word alignment of this id for every engine",
    )
    p.add_argument(
        "--stamp",
        metavar="ISO8601",
        default=None,
        help="record this generation timestamp in the report (omitted by default "
        "to keep reports byte-for-byte reproducible)",
    )
    p.add_argument("--quiet", action="store_true", help="do not print the table")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    normalize = not args.no_normalize

    try:
        refs = read_transcripts(args.refs, args.ref_suffix)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not refs:
        print(f"error: no *{args.ref_suffix} files in {args.refs}", file=sys.stderr)
        return 2

    vocabulary: list[list[str]] = []
    if args.custom_vocab is not None:
        if not args.custom_vocab.is_file():
            print(f"error: vocabulary file not found: {args.custom_vocab}", file=sys.stderr)
            return 2
        vocabulary = load_vocabulary(args.custom_vocab, normalize=normalize)

    reports: list[EngineReport] = []
    engine_hyps: dict[str, dict[str, str]] = {}
    for arg in args.hyps:
        name, path = parse_engine_arg(arg)
        if name in engine_hyps:
            print(f"error: duplicate engine name '{name}' (use name=path)", file=sys.stderr)
            return 2
        try:
            hyps = read_transcripts(path, args.hyp_suffix)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        engine_hyps[name] = hyps
        extra = sorted(set(hyps) - set(refs))
        if extra:
            print(
                f"warning: {name}: {len(extra)} hypothesis file(s) without a reference "
                f"ignored (e.g. {extra[0]})",
                file=sys.stderr,
            )
        reports.append(
            score_engine(
                name,
                refs,
                hyps,
                vocabulary,
                skip_missing=args.skip_missing,
                normalize=normalize,
            )
        )

    ranked = rank_engines(reports)

    if not args.quiet:
        print(format_table(ranked, with_vocab=bool(vocabulary)))
        for r in ranked:
            if r.files_missing:
                shown = ", ".join(r.files_missing[:5])
                more = "" if len(r.files_missing) <= 5 else f" (+{len(r.files_missing) - 5} more)"
                print(f"note: {r.name}: missing hypotheses for {shown}{more}")

    if args.show_alignment is not None:
        file_id = args.show_alignment
        if file_id not in refs:
            print(f"error: unknown id '{file_id}'", file=sys.stderr)
            return 2
        ref_norm = normalize_arabic(refs[file_id]) if normalize else refs[file_id]
        ref_tokens = tokenize(ref_norm)
        for r in ranked:
            score = next((f for f in r.files if f.file_id == file_id), None)
            print(f"\n== {r.name} / {file_id}")
            if score is None:
                print("(not scored)")
                continue
            hyp_text = engine_hyps[r.name].get(file_id, "")
            hyp_tokens = tokenize(normalize_arabic(hyp_text) if normalize else hyp_text)
            print(format_alignment(score, ref_tokens, hyp_tokens))

    if args.report is not None:
        report = build_report(
            ranked,
            refs_dir=args.refs,
            file_ids=sorted(refs),
            vocab_path=args.custom_vocab,
            vocabulary=vocabulary,
            normalize=normalize,
            skip_missing=args.skip_missing,
            stamp=args.stamp,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not args.quiet:
            print(f"report written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
