"""Tests for tools/stt_bakeoff.py: Arabic normalisation, WER/CER alignment,
custom-vocabulary recall and the end-to-end CLI (SPEC section 9, section 16)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "stt_bakeoff.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("stt_bakeoff", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("stt_bakeoff", module)
    spec.loader.exec_module(module)
    return module


bakeoff = _load_tool()
normalize_arabic = bakeoff.normalize_arabic
tokenize = bakeoff.tokenize
align = bakeoff.align
word_error_rate = bakeoff.word_error_rate
character_error_rate = bakeoff.character_error_rate
levenshtein_distance = bakeoff.levenshtein_distance
vocabulary_recall = bakeoff.vocabulary_recall


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


class TestNormalizeArabic:
    def test_strips_tashkeel_diacritics(self) -> None:
        assert normalize_arabic("مُحَمَّد") == "محمد"
        # fathatan, shadda, sukun, superscript alef and a Quranic mark
        assert normalize_arabic("شُكْرًا هٰذا الْحَمْدُ") == "شكرا هذا الحمد"

    def test_normalises_alef_forms(self) -> None:
        assert normalize_arabic("أحمد إبراهيم آمنة ٱلله") == "احمد ابراهيم امنه الله"

    def test_normalises_hamza_carriers_but_keeps_standalone_hamza(self) -> None:
        assert normalize_arabic("مسؤول رئيس") == "مسوول رييس"
        assert normalize_arabic("شيء") == "شيء"

    def test_taa_marbuta_and_alef_maqsura(self) -> None:
        assert normalize_arabic("شركة") == "شركه"
        assert normalize_arabic("على مصطفى") == "علي مصطفي"
        assert normalize_arabic("شركة", taa_marbuta=False) == "شركة"
        assert normalize_arabic("على", alef_maqsura=False) == "على"

    def test_removes_tatweel_and_bidi_marks(self) -> None:
        assert normalize_arabic("بـــزز") == "بزز"
        assert normalize_arabic("‏نور‎ ﻿أفنيو") == "نور افنيو"

    def test_unifies_digit_forms(self) -> None:
        assert normalize_arabic("٣٠٠٠ درهم") == "3000 درهم"
        assert normalize_arabic("۴ كيلو") == "4 كيلو"  # Extended Arabic-Indic
        assert normalize_arabic("3000 درهم") == normalize_arabic("٣٠٠٠ درهم")
        assert normalize_arabic("٥٬٠٠٠ ريال") == "5000 ريال"  # Arabic thousands separator

    def test_digits_can_be_kept(self) -> None:
        assert normalize_arabic("٣", digits=False) == "٣"

    def test_punctuation_becomes_space_and_digit_separators_vanish(self) -> None:
        assert normalize_arabic("مرحبا، كيفك؟ شو الأخبار!") == "مرحبا كيفك شو الاخبار"
        assert normalize_arabic("5,000 AED and 3.5 kg, don't") == "5000 aed and 35 kg dont"
        assert normalize_arabic("(بز) - أفنيو") == "بز افنيو"

    def test_latin_casefolded_and_arabizi_preserved(self) -> None:
        assert normalize_arabic("Buzz Avenue – sho el 2akhbar, 3ala tool") == (
            "buzz avenue sho el 2akhbar 3ala tool"
        )
        assert normalize_arabic("Nour", lowercase=False) == "Nour"

    def test_presentation_forms_folded(self) -> None:
        assert normalize_arabic("ﻣﺮﺣﺒﺎ") == "مرحبا"

    def test_whitespace_collapsed(self) -> None:
        assert normalize_arabic("  a   b\tc\n d ") == "a b c d"
        assert normalize_arabic("") == ""
        assert normalize_arabic("   ") == ""

    @pytest.mark.parametrize(
        "text",
        [
            "مُحَمَّد",
            "أحمد إبراهيم آمنة",
            "شركة بز أفنيو ٣٠٠٠ درهم، don't",
            "sho el 2akhbar",
        ],
    )
    def test_idempotent(self, text: str) -> None:
        once = normalize_arabic(text)
        assert normalize_arabic(once) == once

    def test_tokenize(self) -> None:
        assert tokenize(normalize_arabic("بدي  أحجز\tموعد")) == ["بدي", "احجز", "موعد"]


# --------------------------------------------------------------------------- #
# Alignment, WER and CER
# --------------------------------------------------------------------------- #


class TestAlignment:
    def test_identical(self) -> None:
        counts, ops = align("a b c".split(), "a b c".split())
        assert counts.rate() == 0.0
        assert (counts.hits, counts.substitutions, counts.deletions, counts.insertions) == (
            3,
            0,
            0,
            0,
        )
        assert [k for k, _, _ in ops] == ["hit", "hit", "hit"]

    def test_substitution_and_insertion(self) -> None:
        counts, ops = align("a b c d".split(), "a x c d e".split())
        assert (counts.hits, counts.substitutions, counts.deletions, counts.insertions) == (
            3,
            1,
            0,
            1,
        )
        assert counts.rate() == pytest.approx(0.5)
        assert ops == [
            ("hit", 0, 0),
            ("sub", 1, 1),
            ("hit", 2, 2),
            ("hit", 3, 3),
            ("ins", None, 4),
        ]

    def test_deletion(self) -> None:
        counts, ops = align("a b c".split(), "a c".split())
        assert counts.deletions == 1 and counts.hits == 2
        assert ("del", 1, None) in ops

    def test_empty_hypothesis_is_all_deletions(self) -> None:
        counts, _ = align("a b c".split(), [])
        assert counts.deletions == 3
        assert counts.rate() == 1.0

    def test_empty_reference_has_undefined_rate(self) -> None:
        counts, _ = align([], "a b".split())
        assert counts.insertions == 2
        assert counts.rate() is None

    def test_wer_can_exceed_one(self) -> None:
        counts = word_error_rate(["a"], "x y z".split())
        assert counts.rate() == 3.0

    def test_counts_addition(self) -> None:
        a = word_error_rate("a b".split(), "a x".split())
        b = word_error_rate("c d e".split(), "c d".split())
        total = a + b
        assert total.ref_len == 5
        assert total.errors == 2
        assert total.rate() == pytest.approx(0.4)


class TestArabicWer:
    def test_diacritics_only_difference_is_zero_wer_after_normalisation(self) -> None:
        ref = "بَدّي أحجز موعد بكرا"
        hyp = "بدي احجز موعد بكرا"
        raw = word_error_rate(tokenize(ref), tokenize(hyp))
        norm = word_error_rate(tokenize(normalize_arabic(ref)), tokenize(normalize_arabic(hyp)))
        assert raw.rate() == 0.5
        assert norm.rate() == 0.0

    def test_digit_form_difference_is_zero_wer_after_normalisation(self) -> None:
        ref = normalize_arabic("حول ٣٠٠٠ درهم لشركة بز أفنيو")
        hyp = normalize_arabic("حوّل 3000 درهم لشركه بز افنيو")
        assert word_error_rate(tokenize(ref), tokenize(hyp)).rate() == 0.0

    def test_one_wrong_word_in_four(self) -> None:
        ref = normalize_arabic("بدي أحجز موعد بكرا")
        hyp = normalize_arabic("بدي أحجز موعد اليوم")
        counts = word_error_rate(tokenize(ref), tokenize(hyp))
        assert counts.substitutions == 1
        assert counts.rate() == pytest.approx(0.25)


class TestCer:
    def test_levenshtein_distance(self) -> None:
        assert levenshtein_distance("", "") == 0
        assert levenshtein_distance("abc", "") == 3
        assert levenshtein_distance("", "ab") == 2
        assert levenshtein_distance("kitten", "sitting") == 3
        assert levenshtein_distance("كتاب", "كتب") == 1

    def test_cer_on_arabic(self) -> None:
        counts = character_error_rate("كتاب", "كتب")
        assert counts.ref_len == 4
        assert counts.errors == 1
        assert counts.rate() == pytest.approx(0.25)

    def test_cer_ignores_segmentation(self) -> None:
        ref = normalize_arabic("ما بدي")
        hyp = normalize_arabic("مابدي")
        assert word_error_rate(tokenize(ref), tokenize(hyp)).rate() == 1.0
        assert character_error_rate(ref, hyp).rate() == 0.0

    def test_cer_empty_reference(self) -> None:
        assert character_error_rate("", "x").rate() is None


# --------------------------------------------------------------------------- #
# Custom vocabulary recall
# --------------------------------------------------------------------------- #


class TestVocabularyRecall:
    def test_multiword_and_single_terms(self) -> None:
        ref = tokenize(normalize_arabic("شركة Buzz Avenue حكت مع نور وبعدين buzz avenue تاني"))
        hyp = tokenize(normalize_arabic("شركه بز افنيو حكت مع نور وبعدين Buzz Avenue تاني"))
        _, ops = align(ref, hyp)
        vocab = [tokenize(normalize_arabic(t)) for t in ("Buzz Avenue", "نور", "رامي")]
        recall = vocabulary_recall(ref, ops, vocab)
        assert recall["buzz avenue"] == (1, 2)
        assert recall["نور"] == (1, 1)
        assert recall["رامي"] == (0, 0)  # never in the reference -> not counted

    def test_partial_hit_of_multiword_term_is_not_recalled(self) -> None:
        ref = "buzz avenue".split()
        hyp = "buzz avenu".split()
        _, ops = align(ref, hyp)
        assert vocabulary_recall(ref, ops, [["buzz", "avenue"]]) == {"buzz avenue": (0, 1)}

    def test_empty_terms_ignored(self) -> None:
        ref = ["a"]
        _, ops = align(ref, ref)
        assert vocabulary_recall(ref, ops, [[]]) == {}


# --------------------------------------------------------------------------- #
# Corpus scoring, ranking and CLI
# --------------------------------------------------------------------------- #

REFS = {
    "note_001": "بدي أحجز موعد بكرا الساعة ٣ مع شركة Buzz Avenue",
    "note_002": "حوّل ٣٠٠٠ درهم لحساب رامي من بز أفنيو",
    "note_003": "sho el 2akhbar? بعتلي التقرير",
}
GOOD = {
    "note_001": "بدي احجز موعد بكرا الساعه 3 مع شركه buzz avenue",
    "note_002": "حول 3000 درهم لحساب رامي من بز افنيو",
    "note_003": "sho el 2akhbar بعتلي التقرير",
}
WEAK = {
    "note_001": "بدي احجز موعد بكرا الساعه 3 مع شركه بز افنيو",
    "note_002": "حول 300 درهم لحساب سامي من بز افنيو",
    # note_003 deliberately missing
}


def _write_corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    refs = tmp_path / "refs"
    good = tmp_path / "out" / "good"
    weak = tmp_path / "out" / "weak"
    for d in (refs, good, weak):
        d.mkdir(parents=True)
    for file_id, text in REFS.items():
        (refs / f"{file_id}.ref.txt").write_text(text, encoding="utf-8")
    for file_id, text in GOOD.items():
        (good / f"{file_id}.hyp.txt").write_text(text, encoding="utf-8")
    for file_id, text in WEAK.items():
        (weak / f"{file_id}.hyp.txt").write_text(text, encoding="utf-8")
    return refs, good, weak


class TestScoreEngine:
    def test_perfect_engine_after_normalisation(self) -> None:
        report = bakeoff.score_engine("good", REFS, GOOD)
        assert report.corpus_wer == 0.0
        assert report.corpus_cer == 0.0
        assert report.files_missing == []

    def test_missing_file_scored_as_deletions(self) -> None:
        report = bakeoff.score_engine("weak", REFS, WEAK)
        assert report.files_missing == ["note_003"]
        per_file = {f.file_id: f for f in report.files}
        assert per_file["note_003"].missing is True
        assert per_file["note_003"].words.rate() == 1.0
        # note_001: "buzz avenue" -> "بز افنيو" = 2 substitutions
        assert per_file["note_001"].words.substitutions == 2
        # note_002: 300 vs 3000 and سامي vs رامي = 2 substitutions
        assert per_file["note_002"].words.substitutions == 2

    def test_skip_missing(self) -> None:
        report = bakeoff.score_engine("weak", REFS, WEAK, skip_missing=True)
        assert report.files_missing == ["note_003"]
        assert [f.file_id for f in report.files] == ["note_001", "note_002"]

    def test_ranking_orders_by_corpus_wer(self) -> None:
        good = bakeoff.score_engine("good", REFS, GOOD)
        weak = bakeoff.score_engine("weak", REFS, WEAK)
        ranked = bakeoff.rank_engines([weak, good])
        assert [r.name for r in ranked] == ["good", "weak"]


class TestVocabularyLoading:
    def test_txt_vocabulary(self, tmp_path: Path) -> None:
        vocab = tmp_path / "vocab.txt"
        vocab.write_text("# names\nBuzz Avenue\nنور\n\nرامي\nbuzz avenue\n", encoding="utf-8")
        assert bakeoff.load_vocabulary(vocab) == [["buzz", "avenue"], ["نور"], ["رامي"]]

    def test_yaml_vocabulary_mapping(self, tmp_path: Path) -> None:
        vocab = tmp_path / "vocab.yaml"
        vocab.write_text(
            "companies:\n  - Buzz Avenue\nstaff:\n  - رامي\nplaces: دبي\n", encoding="utf-8"
        )
        assert bakeoff.load_vocabulary(vocab) == [["buzz", "avenue"], ["رامي"], ["دبي"]]

    def test_yaml_vocabulary_list(self, tmp_path: Path) -> None:
        vocab = tmp_path / "vocab.yml"
        vocab.write_text("- نور\n- Buzz Avenue\n", encoding="utf-8")
        assert bakeoff.load_vocabulary(vocab) == [["نور"], ["buzz", "avenue"]]


class TestCli:
    def test_end_to_end_report_and_table(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        refs, good, weak = _write_corpus(tmp_path)
        vocab = tmp_path / "vocab.txt"
        vocab.write_text("Buzz Avenue\nرامي\nنور\n", encoding="utf-8")
        report_path = tmp_path / "reports" / "bakeoff.json"

        rc = bakeoff.main(
            [
                "--refs",
                str(refs),
                "--hyps",
                str(good),
                f"weak_engine={weak}",
                "--custom-vocab",
                str(vocab),
                "--report",
                str(report_path),
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "good" in out and "weak_engine" in out
        assert "missing hypotheses for note_003" in out

        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["report_version"] == "1"
        assert "generated_at" not in report
        assert [r["engine"] for r in report["ranking"]] == ["good", "weak_engine"]
        assert report["files"] == ["note_001", "note_002", "note_003"]

        good_json = report["engines"]["good"]
        assert good_json["corpus_wer"] == 0.0
        assert good_json["vocab"]["recall"] == 1.0
        assert good_json["vocab"]["terms"]["buzz avenue"] == {"recalled": 1, "total": 1}
        assert good_json["vocab"]["terms"]["نور"] == {"recalled": 0, "total": 0}

        weak_json = report["engines"]["weak_engine"]
        assert weak_json["files_missing"] == ["note_003"]
        assert weak_json["per_file"]["note_003"]["missing"] is True
        assert weak_json["per_file"]["note_003"]["wer"] == 1.0
        # Buzz Avenue transliterated and رامي mis-heard: 0 of 2 vocabulary hits
        assert weak_json["vocab"] == {
            "recall": 0.0,
            "recalled": 0,
            "total": 2,
            "terms": {
                "buzz avenue": {"recalled": 0, "total": 1},
                "رامي": {"recalled": 0, "total": 1},
                "نور": {"recalled": 0, "total": 0},
            },
        }
        assert weak_json["corpus_wer"] > good_json["corpus_wer"]
        assert weak_json["ref_words"] == good_json["ref_words"]

    def test_report_is_deterministic(self, tmp_path: Path) -> None:
        refs, good, weak = _write_corpus(tmp_path)
        outputs = []
        for i in range(2):
            path = tmp_path / f"r{i}.json"
            rc = bakeoff.main(
                [
                    "--refs",
                    str(refs),
                    "--hyps",
                    str(good),
                    str(weak),
                    "--report",
                    str(path),
                    "--quiet",
                ]
            )
            assert rc == 0
            outputs.append(path.read_bytes())
        assert outputs[0] == outputs[1]

    def test_show_alignment(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        refs, good, weak = _write_corpus(tmp_path)
        rc = bakeoff.main(
            ["--refs", str(refs), "--hyps", str(weak), "--show-alignment", "note_002", "--quiet"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "== weak / note_002" in out
        assert "sub " in out and "رامي" in out and "سامي" in out

    def test_errors_for_bad_inputs(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        refs, good, _ = _write_corpus(tmp_path)
        assert bakeoff.main(["--refs", str(tmp_path / "nope"), "--hyps", str(good)]) == 2
        assert bakeoff.main(["--refs", str(refs), "--hyps", str(tmp_path / "nope")]) == 2
        assert bakeoff.main(["--refs", str(refs), "--hyps", str(good), str(good)]) == 2
        assert (
            bakeoff.main(
                ["--refs", str(refs), "--hyps", str(good), "--show-alignment", "zzz", "--quiet"]
            )
            == 2
        )
        empty = tmp_path / "empty"
        empty.mkdir()
        assert bakeoff.main(["--refs", str(empty), "--hyps", str(good)]) == 2
        assert "error" in capsys.readouterr().err

    def test_no_normalize_flag_counts_diacritics(self, tmp_path: Path) -> None:
        refs, good, _ = _write_corpus(tmp_path)
        raw = tmp_path / "raw.json"
        norm = tmp_path / "norm.json"
        assert (
            bakeoff.main(
                [
                    "--refs",
                    str(refs),
                    "--hyps",
                    str(good),
                    "--report",
                    str(raw),
                    "--no-normalize",
                    "--quiet",
                ]
            )
            == 0
        )
        assert (
            bakeoff.main(
                ["--refs", str(refs), "--hyps", str(good), "--report", str(norm), "--quiet"]
            )
            == 0
        )
        raw_wer = json.loads(raw.read_text())["engines"]["good"]["corpus_wer"]
        norm_wer = json.loads(norm.read_text())["engines"]["good"]["corpus_wer"]
        assert norm_wer == 0.0
        assert raw_wer > 0.0
