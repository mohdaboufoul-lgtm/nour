"""Arabic speech and script handling (DESIGN §3.10 ``nour/language/speech.py``; SPEC §9).

SPEC §9 "Arabic speech pipeline": a dialect-capable recogniser chosen by a bake-off on the
owner's own voice notes (lowest word-error rate wins, re-tested quarterly); custom vocabulary
(company, product, staff and customer names) loaded into the recogniser; "code-switching and
Arabizi ('3ala', 'sho el 2akhbar') parsed as Arabic"; the read-back rule for voice commands.

Four things live here:

* :func:`normalise_arabizi` — the typed-text side of SPEC §9: Lebanese Arabizi (Latin letters plus
  the digit-letters 2 3 5 6 7 8 9) is rewritten in Arabic script, by lexicon first and by a
  rule-based transliteration for the rest, while English words of a code-switched message, pure
  numbers, codes and anything already in Arabic script are left alone. Best effort by nature:
  the prompt shows it *next to* the owner's verbatim text, never instead of it.
* :func:`apply_vocabulary` — replaces near-matches of vocabulary terms in a transcript with their
  canonical spelling (the recogniser's output for "Buzz Avenue" or "مطبعة النور" varies).
* :class:`ArabicSTT` — the agent-facing recogniser behind one or more ``SttPort`` engines: the
  primary engine transcribes with the vocabulary and locale, :meth:`ArabicSTT.bake_off` scores
  every engine by corpus WER on reference samples, :meth:`ArabicSTT.best_engine` picks the lowest.
* :func:`word_error_rate` and :func:`normalise_arabic` — the scorer of ``tools/stt_bakeoff.py``
  mirrored (same normalisation steps, same Levenshtein alignment) so the runtime and the week-one
  bake-off report agree to the digit.

Nothing here reads a clock, a database or the network; every engine is injected.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from nour.core.ports import SttPort, Transcript

ARABIZI_DIGITS = frozenset("2356789")
"""The digit-letters of Lebanese Arabizi: 2 ء, 3 ع, 5 خ, 6 ط, 7 ح, 8 غ, 9 ق."""

READBACK_CONFIDENCE = 0.80
"""Below this utterance confidence the agent reads back even a Tier A command (docs/adapters/speech.md §9)."""

# --------------------------------------------------------------------------- Arabizi → Arabic

# Lexicon of Lebanese Arabizi tokens (case-folded) → Arabic script. Strong markers prove a text
# is Arabizi; weak ones (el, la, w, ya …) are too short or too ambiguous to decide on their own
# and are converted only once the text is known to be Arabizi. A token that is also an English
# word or name (eh, mesh, kel, shi, merci, dirham, bass, Hal, Abel …) is never strong: "the mesh
# fabric samples" and "send 500 dirham to Ahmed" are English sentences.
_STRONG_LEXICON: dict[str, str] = {
    # greetings, particles, pronouns
    "marhaba": "مرحبا",
    "sho": "شو",
    "shu": "شو",
    "shou": "شو",
    "kif": "كيف",
    "kifak": "كيفك",
    "kifek": "كيفك",
    "yalla": "يلا",
    "yala": "يلا",
    "la2": "لا",
    "khalas": "خلص",
    "khallas": "خلص",
    "tamem": "تمام",
    "tamam": "تمام",
    "mnih": "منيح",
    "mni7": "منيح",
    "ro7": "روح",
    "ktir": "كتير",
    "kteer": "كتير",
    "kil": "كل",
    "hayda": "هيدا",
    "hayde": "هيدي",
    "halla2": "هلق",
    "hala2": "هلق",
    "lesh": "ليش",
    "wein": "وين",
    "enta": "إنت",
    "ente": "إنتي",
    "huwe": "هو",
    "hiye": "هي",
    "eno": "إنو",
    "enno": "إنو",
    "mish": "مش",
    "fik": "فيك",
    "fike": "فيكي",
    "fiki": "فيكي",
    "momken": "ممكن",
    "mumkin": "ممكن",
    # time
    "bokra": "بكرا",
    "bukra": "بكرا",
    "mbare7": "مبارح",
    "sob7": "صبح",
    "sobe7": "صبح",
    "el yom": "اليوم",
    "shahr": "شهر",
    "jeye": "جاي",
    "jaye": "جاي",
    "tali3": "طالع",
    "2abel": "قبل",
    "ba3d": "بعد",
    "ba3den": "بعدين",
    "daroure": "ضروري",
    "daroura": "ضروري",
    "msta3jil": "مستعجل",
    "msta3jel": "مستعجل",
    "bsor3a": "بسرعة",
    # money and business
    "derhem": "درهم",
    "derham": "درهم",
    "alf": "ألف",
    "fatoura": "فاتورة",
    "fatura": "فاتورة",
    "matba3et": "مطبعة",
    "matba3a": "مطبعة",
    "matb3a": "مطبعة",
    "shirke": "شركة",
    "sherke": "شركة",
    "kamen": "كمان",
    "kaman": "كمان",
    "mablagh": "مبلغ",
    "masari": "مصاري",
    "7seb": "حساب",
    "7esab": "حساب",
    "7sab": "حساب",
    "se3r": "سعر",
    "rkhis": "رخيص",
    "jdid": "جديد",
    "jdide": "جديدة",
    "2adim": "قديم",
    "2adime": "قديمة",
    "esmo": "اسمو",
    "esma": "اسما",
    "ra2em": "رقم",
    "rakam": "رقم",
    "wa2et": "وقت",
    "maw3ed": "موعد",
    "ma3": "مع",
    "3an": "عن",
    "3ala": "على",
    "3al": "عال",
    "3a": "ع",
    "3ande": "عندي",
    "3andi": "عندي",
    "3ando": "عندو",
    "lal": "لل",
    "bel": "بال",
    "bl": "بال",
    "taba3": "تبع",
    "ta3": "تبع",
    "tab3o": "تبعو",
    "taba3na": "تبعنا",
    "bade": "بدي",
    "badde": "بدي",
    "bado": "بدو",
    "baddo": "بدو",
    "badna": "بدنا",
    "7odour": "حضور",
    "ghalat": "غلط",
    "sa7": "صح",
    "akid": "أكيد",
    # verbs the owner uses with Nour
    "edfa3e": "ادفعي",
    "edfa3": "ادفع",
    "tdaf3e": "تدفعي",
    "7awle": "حولي",
    "7awli": "حولي",
    "7awel": "حول",
    "7awlili": "حوليلي",
    "7adrile": "حضريلي",
    "7adre": "حضري",
    "7adrili": "حضريلي",
    "7otte": "حطي",
    "7otti": "حطي",
    "7ott": "حط",
    "ba3te": "بعتي",
    "b3ate": "بعتي",
    "ba3tile": "بعتيلي",
    "b3atile": "بعتيلي",
    "ba3atlek": "بعتلك",
    "ba3atlak": "بعتلك",
    "b3atlo": "بعتلو",
    "eb3ato": "ابعتو",
    "nb3ata": "نبعتا",
    "ba3atne": "بعتني",
    "jawbe": "جاوبي",
    "jawbi": "جاوبي",
    "2ello": "قلو",
    "2ella": "قلا",
    "2ellon": "قلون",
    "2al": "قال",
    "2alek": "قالك",
    "2allek": "قالك",
    "byo2ellik": "بيقلك",
    "byo2elik": "بيقلك",
    "jibile": "جيبيلي",
    "jibe": "جيبي",
    "dife": "ضيفي",
    "difi": "ضيفي",
    "zide": "زيدي",
    "zakrine": "ذكريني",
    "zakrini": "ذكريني",
    "emsa7e": "امسحي",
    "emsa7i": "امسحي",
    "wa2fe": "وقفي",
    "wa2fi": "وقفي",
    "wa2ef": "وقف",
    "sajle": "سجلي",
    "sajli": "سجلي",
    "ghayre": "غيري",
    "8ayre": "غيري",
    "tjahale": "تجاهلي",
    "tjahali": "تجاهلي",
    "2abli": "اقبلي",
    "rajje3": "رجعي",
    "rajje3e": "رجعي",
    "7afzeti": "حفظتي",
    "7afzetih": "حفظتيه",
    "7afze": "حفظي",
    "khallasna": "خلصنا",
    "jehzin": "جاهزين",
    "jahzin": "جاهزين",
    "mne2bal": "منقبل",
    "ne2bal": "نقبل",
    "3ard": "عرض",
    "sou2": "سوق",
    "sa2": "ساق",
    "yeshteghel": "يشتغل",
    "byakhod": "بياخد",
    "tnatre": "تنطري",
    "yetkhallas": "يتخلص",
    "wafa2": "وافق",
    "da3e": "داعي",
    "tes2al": "تسأل",
    "tes2alih": "تسأليه",
    "tes2ale": "تسألي",
    "t8ayar": "تغير",
    "sakar": "سكر",
    "ma2dye": "ماضية",
    "3am": "عم",
    "3amel": "عامل",
    "3melet": "عملت",
    "2adde": "قديش",
    "2addesh": "قديش",
    "sorna": "صرنا",
    "sa3a": "ساعة",
    "sa3tein": "ساعتين",
    "walad": "ولد",
    "aswad": "أسود",
    "abyad": "أبيض",
    "b2esme": "باسمي",
    "b2esmi": "باسمي",
    "3ende": "عندي",
    "3end": "عند",
    "2a7ad": "أحد",
    "el 2a7ad": "الأحد",
    "tnen": "تنين",
    "tlete": "تلاتة",
    "khamis": "خميس",
    "jem3a": "جمعة",
    "sabt": "سبت",
    "2akhbar": "أخبار",
    "akhbar": "أخبار",
    "2akhbarak": "أخبارك",
}

_WEAK_LEXICON: dict[str, str] = {
    # Also English words or names (men, she, bass, Ana, Hal, Abel, mesh, kel, shi, eh, merci,
    # dirham …): converted only once a strong word or two digit-letter tokens have shown the
    # text is Arabizi (:func:`_evidence`).
    "men": "من",
    "min": "من",
    "ma": "ما",
    "fi": "في",
    "fii": "في",
    "bas": "بس",
    "bass": "بس",
    "ana": "أنا",
    "hal": "هال",
    "abel": "قبل",
    "jay": "جاي",
    "jib": "جيب",
    "wen": "وين",
    "el": "ال",
    "il": "ال",
    "l": "ل",
    "la": "ل",
    "w": "و",
    "ya": "يا",
    "nour": "نور",
    "ra7": "رح",
    "mn": "من",
    "lel": "لل",
    "eh": "إيه",
    "merci": "ميرسي",
    "kel": "كل",
    "shi": "شي",
    "mesh": "مش",
    "dirham": "درهم",
    "yom": "يوم",
    "leh": "ليه",
    "halla": "هلق",
    "ghali": "غالي",
}

_LEXICON: dict[str, str] = {**_STRONG_LEXICON, **_WEAK_LEXICON}

_ARABIC_PREFIXES = frozenset({"ال", "ل", "و", "بال", "لل", "عال", "ع"})
"""Converted particles that Arabic writes attached to the next word (``el 2akhbar`` → ``الأخبار``)."""
_PREFIX_JOIN_RE = re.compile(
    r"(?<![\w\u0600-\u06FF])("
    + "|".join(sorted(_ARABIC_PREFIXES, key=len, reverse=True))
    + r") (?=[\u0600-\u06FF])"
)

# Tokens with digits that are not Arabizi: times, units, codes, product names with a trailing
# digit (win7, gta5, iphone7). Real Arabizi words ending in a digit-letter are short (la2, sa7,
# ma3, ro7, mni7) and live in the lexicon, which is consulted first.
_NOT_ARABIZI_RE = re.compile(
    r"^\d+(?:am|pm|l|kg|g|km|ml|cm|mm|k|m|h|hr|hrs|min|x|st|nd|rd|th|gb|mb|tb|pc|pcs)$"
    r"|^[a-z]{1,2}\d+$"  # A5, Q4, DN5520
    r"|^[a-z]{3,}\d+$"  # win7, gta5, iphone7
)
_CODE_STOPLIST = frozenset({"b2b", "b2c", "p2p", "c2c", "2fa", "3d", "4k", "5g", "mp3", "h2o"})
_ARABIZI_SHAPE_RE = re.compile(r"^[a-z]*[2356789][a-z]+$|^[a-z]+[2356789]+[a-z]*$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9'’]+")

_DIGRAPHS: tuple[tuple[str, str], ...] = (
    ("sh", "ش"),
    ("kh", "خ"),
    ("gh", "غ"),
    ("th", "ث"),
    ("dh", "ذ"),
    ("ch", "ش"),
    ("ph", "ف"),
    ("ou", "و"),
    ("oo", "و"),
    ("ee", "ي"),
    ("ii", "ي"),
    ("aa", "ا"),
    ("ay", "اي"),
    ("ai", "اي"),
    ("ei", "ي"),
    ("ie", "ي"),
)
_CONSONANTS: dict[str, str] = {
    "3": "ع",
    "5": "خ",
    "6": "ط",
    "7": "ح",
    "8": "غ",
    "9": "ق",
    "b": "ب",
    "t": "ت",
    "j": "ج",
    "d": "د",
    "r": "ر",
    "z": "ز",
    "s": "س",
    "c": "ك",
    "k": "ك",
    "q": "ق",
    "l": "ل",
    "m": "م",
    "n": "ن",
    "h": "ه",
    "w": "و",
    "y": "ي",
    "f": "ف",
    "g": "غ",
    "v": "ف",
    "p": "ب",
    "x": "كس",
}
_VOWELS = frozenset("aeiou")


def _transliterate(token: str) -> str:
    """Rule-based Arabizi → Arabic for a token the lexicon does not know (best effort)."""
    out: list[str] = []
    i = 0
    n = len(token)
    while i < n:
        pair = token[i : i + 2]
        matched = False
        for digraph, arabic in _DIGRAPHS:
            if pair == digraph:
                out.append(arabic)
                i += 2
                matched = True
                break
        if matched:
            continue
        ch = token[i]
        if ch == "2":
            out.append("أ" if i == 0 else "ء")
        elif ch in _CONSONANTS:
            out.append(_CONSONANTS[ch])
        elif ch in _VOWELS:
            if i == 0:
                out.append("ا" if ch in "aou" else "إ")
            elif i == n - 1:
                out.append({"a": "ا", "e": "ي", "i": "ي", "o": "و", "u": "و"}[ch])
            # short vowels in the middle of a word are not written in Arabic
        elif ch.isdigit():
            out.append(ch)  # a stray 0/1/4 stays
        i += 1
    return "".join(out)


def _is_arabizi_token(token: str) -> bool:
    """A Latin token carrying a digit-letter next to letters (``7awli``, ``la2``), not a
    time, unit or code (``3pm``, ``40l``, ``A5``, ``b2b``)."""
    lowered = token.casefold().replace("’", "'")
    if lowered in _CODE_STOPLIST or _NOT_ARABIZI_RE.match(lowered):
        return False
    return bool(_ARABIZI_SHAPE_RE.match(lowered)) and any(ch in ARABIZI_DIGITS for ch in lowered)


def _evidence(text: str) -> tuple[int, int, bool]:
    """``(strong_words, digit_letter_tokens, all_lexicon)`` found in ``text``: ``all_lexicon`` is
    true when every Latin word is a lexicon word (strong or weak), the reading of a short reply
    such as ``eh`` or ``bass`` that an English sentence ("eh, book a 30 min call") never gets."""
    strong = 0
    digits = 0
    words = 0
    lexicon_words = 0
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        lowered = token.casefold().replace("’", "'")
        if lowered in _STRONG_LEXICON and _STRONG_LEXICON[lowered] != lowered:
            strong += 1
        elif _is_arabizi_token(token):
            digits += 1
        if lowered.isalpha():
            words += 1
            if lowered in _LEXICON:
                lexicon_words += 1
    return strong, digits, words >= 1 and lexicon_words == words


def looks_arabizi(text: str) -> bool:
    """True when ``text`` carries at least one Arabizi token: a digit-letter token or a strong
    lexicon word, or consists of lexicon words only (``eh``, ``bass``). A plain English sentence
    (even one with "mesh", "dirham", "win7" or "30 min" in it), a number or Arabic script never
    qualifies."""
    strong, digits, all_lexicon = _evidence(text)
    return strong >= 1 or digits >= 1 or all_lexicon


def _converts_weak_words(text: str) -> bool:
    """Weak lexicon words (men, el, w, fi, bass, eh …) are converted only once a strong word,
    two digit-letter tokens or an all-lexicon message prove the text is Arabizi; one unknown
    digit-letter token alone is transliterated on its own."""
    strong, digits, all_lexicon = _evidence(text)
    return strong >= 1 or digits >= 2 or all_lexicon


def normalise_arabizi(text: str) -> str:
    """§9: "3ala", "sho el 2akhbar" → Arabic script.

    Converts a text only when :func:`looks_arabizi` says it is Arabizi (so English messages and
    English words inside a code-switched message are untouched); then every lexicon token and
    every digit-letter token is rewritten in Arabic script, pure numbers, times, units, codes
    (``3pm``, ``A5``, ``win7``), ``min`` right after a number and Arabic text stay as they are.
    Idempotent: Arabic script never looks like Arabizi.
    """
    if not isinstance(text, str):
        raise TypeError("normalise_arabizi takes a str")
    if not looks_arabizi(text):
        return text
    weak_ok = _converts_weak_words(text)

    def convert(match: re.Match[str]) -> str:
        token = match.group(0)
        lowered = token.casefold().replace("’", "'")
        if lowered.isdigit():
            return token
        known = _STRONG_LEXICON.get(lowered)
        if known is None and weak_ok:
            known = _WEAK_LEXICON.get(lowered)
            if lowered == "min" and text[: match.start()].rstrip()[-1:].isdigit():
                known = None  # "30 min" is minutes, not "from"
        if known is not None:
            return known
        if _is_arabizi_token(token):
            return _transliterate(lowered)
        return token

    converted = _TOKEN_RE.sub(convert, text)
    return _PREFIX_JOIN_RE.sub(lambda m: m.group(1), converted)


# --------------------------------------------------------------------------- bake-off normalisation (mirror of tools/stt_bakeoff.py)

_ALEF = "ا"
_NORMALISE_TABLE: dict[int, str | None] = {
    0x0622: _ALEF,  # alef with madda above
    0x0623: _ALEF,  # alef with hamza above
    0x0625: _ALEF,  # alef with hamza below
    0x0671: _ALEF,  # alef wasla
    0x0624: "و",  # waw with hamza above -> waw
    0x0626: "ي",  # yeh with hamza above -> yeh
    0x0629: "ه",  # taa marbuta -> haa
    0x0649: "ي",  # alef maqsura -> yeh
    0x0640: None,  # tatweel
    0x200B: None,
    0x200C: None,
    0x200D: None,
    0x200E: None,
    0x200F: None,
    0x202A: None,
    0x202B: None,
    0x202C: None,
    0x202D: None,
    0x202E: None,
    0x2066: None,
    0x2067: None,
    0x2068: None,
    0x2069: None,
    0xFEFF: None,
}
for _i in range(10):
    _NORMALISE_TABLE[0x0660 + _i] = str(_i)  # Arabic-Indic
    _NORMALISE_TABLE[0x06F0 + _i] = str(_i)  # Extended Arabic-Indic

_DIGIT_SEPARATORS = frozenset(",.٫٬’'")
_WORD_APOSTROPHES = frozenset("'’")


def normalise_arabic(text: str) -> str:
    """The bake-off scorer's normalisation (``tools/stt_bakeoff.normalize_arabic`` with its
    defaults), mirrored so runtime WER equals the report's: NFKC; alef variants → alef; hamza
    on waw/yeh → waw/yeh; taa marbuta → haa; alef maqsura → yeh; tatweel and bidi marks removed;
    combining marks (tashkeel) removed; Arabic-Indic digits → ASCII; punctuation → space except
    separators inside numbers and apostrophes inside Latin words; Latin case-folded; whitespace
    collapsed. Arabizi tokens are kept verbatim (transliteration is :func:`normalise_arabizi`'s job)."""
    text = unicodedata.normalize("NFKC", text).translate(_NORMALISE_TABLE)
    out: list[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        cat = unicodedata.category(ch)
        if cat == "Mn":
            continue
        if cat[0] in "PS":
            prev_ch = text[i - 1] if i > 0 else ""
            next_ch = text[i + 1] if i + 1 < n else ""
            if ch in _DIGIT_SEPARATORS and prev_ch.isdigit() and next_ch.isdigit():
                continue
            if ch in _WORD_APOSTROPHES and prev_ch.isalpha() and next_ch.isalpha():
                continue
            out.append(" ")
            continue
        if cat[0] == "Z" or cat == "Cc":
            out.append(" ")
            continue
        out.append(ch)
    return " ".join("".join(out).casefold().split())


def tokenize(text: str) -> list[str]:
    """Whitespace tokens of a normalised text."""
    return text.split()


def _levenshtein(a: Sequence[str], b: Sequence[str]) -> int:
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


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    """``(errors, reference_words)`` after normalisation: the two numbers a corpus WER sums."""
    ref = tokenize(normalise_arabic(reference))
    hyp = tokenize(normalise_arabic(hypothesis))
    return _levenshtein(ref, hyp), len(ref)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """§9: word error rate of ``hypothesis`` against ``reference`` (both normalised like the
    bake-off scorer); ``0.0`` for a perfect match, may exceed ``1.0`` with many insertions.
    An empty reference counts every hypothesis word as an error."""
    errors, ref_len = word_errors(reference, hypothesis)
    return errors / max(ref_len, 1)


# --------------------------------------------------------------------------- custom vocabulary


def apply_vocabulary(text: str, vocabulary: Sequence[str]) -> str:
    """§9 custom vocabulary: every run of words in ``text`` that normalises to a vocabulary term
    is replaced by the term's canonical spelling (``"buzz avenue"`` → ``"Buzz Avenue"``,
    ``"buzz-avenue"`` → ``"Buzz Avenue"``, ``"مطبعه النور"`` → ``"مطبعة النور"``). Matching runs
    over the normalised word stream, so a hyphenated token can match a two-word term; longer
    terms win over shorter ones; punctuation glued to the first and the last word is kept; text
    without a match is returned unchanged."""
    if not isinstance(text, str):
        raise TypeError("apply_vocabulary takes a str")
    terms = [term for term in vocabulary if isinstance(term, str) and term.strip()]
    if not terms or not text.strip():
        return text
    tokens = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"\S+", text)]
    # the flattened normalised stream: (word, index of the text token it came from)
    stream: list[tuple[str, int]] = []
    for index, (_, _, token) in enumerate(tokens):
        stream.extend((word, index) for word in tokenize(normalise_arabic(token)))
    canonical: list[tuple[list[str], str]] = []
    for term in terms:
        norm = tokenize(normalise_arabic(term))
        if norm:
            canonical.append((norm, term))
    canonical.sort(key=lambda item: -len(item[0]))
    replacements: list[tuple[int, int, str]] = []
    taken = [False] * len(tokens)
    for norm, term in canonical:
        k = len(norm)
        i = 0
        while i + k <= len(stream):
            words = [word for word, _ in stream[i : i + k]]
            first_tok = stream[i][1]
            last_tok = stream[i + k - 1][1]
            whole = (i == 0 or stream[i - 1][1] != first_tok) and (
                i + k == len(stream) or stream[i + k][1] != last_tok
            )
            if words == norm and whole and not any(taken[first_tok : last_tok + 1]):
                head = _leading_punctuation(tokens[first_tok][2])
                tail = _trailing_punctuation(tokens[last_tok][2])
                replacements.append((tokens[first_tok][0], tokens[last_tok][1], head + term + tail))
                for j in range(first_tok, last_tok + 1):
                    taken[j] = True
                i += k
            else:
                i += 1
    if not replacements:
        return text
    out = text
    for start, end, replacement in sorted(replacements, reverse=True):
        out = out[:start] + replacement + out[end:]
    return out


def _trailing_punctuation(token: str) -> str:
    end = len(token)
    while end > 0 and unicodedata.category(token[end - 1])[0] in "PS":
        end -= 1
    return token[end:]


def _leading_punctuation(token: str) -> str:
    start = 0
    while start < len(token) and unicodedata.category(token[start])[0] in "PS":
        start += 1
    return token[:start]


# --------------------------------------------------------------------------- the recogniser


def needs_readback(transcript: Transcript, threshold: float = READBACK_CONFIDENCE) -> bool:
    """True when recognition is shaky enough that the agent reads back before acting even on a
    Tier A command (SPEC §9 read-back rule; docs/adapters/speech.md §9)."""
    return transcript.confidence < threshold


class ArabicSTT:
    """§9: dialect-capable recognition behind SttPort; custom vocabulary from coats + owner names; bake-off by WER."""

    def __init__(
        self,
        engines: Mapping[str, SttPort],
        primary: str,
        vocabulary: Sequence[str],
        language: str = "ar-LB",
    ) -> None:
        if not engines:
            raise ValueError("ArabicSTT needs at least one engine")
        if primary not in engines:
            raise ValueError(f"primary engine {primary!r} is not one of {sorted(engines)}")
        self._engines: dict[str, SttPort] = dict(engines)
        self._primary = primary
        self._vocabulary: tuple[str, ...] = tuple(
            term for term in vocabulary if isinstance(term, str) and term.strip()
        )
        self._language = language
        self._last_failures: dict[str, int] = {}

    @property
    def primary(self) -> str:
        return self._primary

    @property
    def engines(self) -> Mapping[str, SttPort]:
        return dict(self._engines)

    @property
    def vocabulary(self) -> tuple[str, ...]:
        return self._vocabulary

    @property
    def language(self) -> str:
        return self._language

    def transcribe(self, audio: bytes, audio_ref: str) -> Transcript:
        """Transcribe ``audio`` with the primary engine, the locale and the custom vocabulary;
        the returned text has vocabulary terms in their canonical spelling. ``audio_ref`` names
        the object-storage key in error messages only (audio is deleted after 7 days, SPEC §9;
        the transcript is what the log keeps)."""
        if not isinstance(audio, bytes | bytearray) or not audio:
            raise ValueError(f"no audio to transcribe for {audio_ref!r}")
        engine = self._engines[self._primary]
        raw = engine.transcribe(bytes(audio), language=self._language, vocabulary=self._vocabulary)
        return Transcript(
            text=apply_vocabulary(raw.text, self._vocabulary),
            language=raw.language or self._language,
            confidence=raw.confidence,
            engine=raw.engine,
        )

    def bake_off(self, samples: Sequence[tuple[bytes, str]]) -> dict[str, float]:
        """§9 selection by test: corpus WER per engine over ``(audio, reference_text)`` samples
        (total word errors / total reference words, like ``tools/stt_bakeoff.py``). An engine
        whose call fails operationally (``OSError`` — timeouts and connection errors included —
        or ``RuntimeError``, the vendor error base) is scored as if it returned nothing for that
        sample (every word deleted) and the failure is counted in :attr:`last_failures`; a
        programming error (wrong signature, missing attribute …) propagates, so a mis-wired
        engine fails the bake-off loudly instead of ranking last."""
        if not samples:
            raise ValueError("bake_off needs at least one (audio, reference) sample")
        table: dict[str, float] = {}
        failures: dict[str, int] = {}
        for name, engine in self._engines.items():
            errors = 0
            words = 0
            failures[name] = 0
            for audio, reference in samples:
                try:
                    hypothesis = engine.transcribe(
                        bytes(audio), language=self._language, vocabulary=self._vocabulary
                    ).text
                except (OSError, RuntimeError):
                    hypothesis = ""
                    failures[name] += 1
                sample_errors, ref_len = word_errors(reference, hypothesis)
                errors += sample_errors
                words += ref_len
            table[name] = errors / max(words, 1)
        self._last_failures = failures
        return table

    @property
    def last_failures(self) -> dict[str, int]:
        """Per-engine count of samples that failed operationally in the last :meth:`bake_off`
        (``{}`` before any bake-off): the report shows "scribe: 4/4 samples failed"."""
        return dict(self._last_failures)

    def best_engine(self, samples: Sequence[tuple[bytes, str]]) -> str:
        """The engine with the lowest corpus WER (ties: the primary, then the name)."""
        table = self.bake_off(samples)
        return min(table, key=lambda name: (table[name], name != self._primary, name))
