"""``FakeVectorIndex``: the per-namespace vector index (DESIGN §3.9 §4a §6; SPEC §8 §11).

One namespace per desk (``MemoryStore`` derives it from the token, DESIGN §4a) and only Tier 0/1
text is ever indexed (``IndexableText.tier``, SPEC §11: Tier 2 by metadata only). Search is
token-overlap scoring (cosine over word sets) so recall tests are deterministic. ``touched``
records every namespace any method reached: the one-way-gate test asserts the Operator never
touches ``assistant`` (DESIGN §6).
"""

from __future__ import annotations

import math
import re
import unicodedata

from nour.core.clock import Clock
from nour.core.ports import CallLog, IndexableText, VectorHit
from nour.fakes import FakePort

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TASHKEEL_RE = re.compile(r"[ً-ْٰـ]")


def tokenize(text: str) -> frozenset[str]:
    """Casefolded NFKC word tokens with Arabic tashkeel and tatweel removed."""
    cleaned = _TASHKEEL_RE.sub("", unicodedata.normalize("NFKC", text)).casefold()
    return frozenset(_TOKEN_RE.findall(cleaned))


class FakeVectorIndex(FakePort):
    """VectorIndexPort fake: ``namespaces[ns][id] → IndexableText``; ``touched`` namespaces."""

    port_name: str = "vector"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.namespaces: dict[str, dict[str, IndexableText]] = {}
        self.touched: set[str] = set()

    def upsert(self, item: IndexableText) -> None:
        if not isinstance(item, IndexableText):
            raise TypeError("upsert takes an IndexableText (Tier 0/1 SafeStr text)")
        self.touched.add(item.namespace)
        self._maybe_fail("upsert")
        self.namespaces.setdefault(item.namespace, {})[item.id] = item

    def search(self, namespace: str, query: str, k: int) -> list[VectorHit]:
        """Top-``k`` items of ``namespace`` by token overlap with ``query`` (score in (0, 1]),
        ties broken by id; nothing from any other namespace."""
        self.touched.add(namespace)
        self._maybe_fail("search")
        if k <= 0:
            return []
        wanted = tokenize(query)
        if not wanted:
            return []
        hits: list[VectorHit] = []
        for item_id, item in self.namespaces.get(namespace, {}).items():
            have = tokenize(item.text)
            overlap = len(wanted & have)
            if overlap == 0:
                continue
            score = overlap / math.sqrt(len(wanted) * len(have))
            hits.append(VectorHit(id=item_id, score=round(score, 6), meta=dict(item.meta)))
        hits.sort(key=lambda hit: (-hit.score, hit.id))
        return hits[:k]

    def delete(self, namespace: str, id: str) -> None:  # noqa: A002 - the port names it `id`
        self.touched.add(namespace)
        self._maybe_fail("delete")
        self.namespaces.get(namespace, {}).pop(id, None)

    def size(self, namespace: str) -> int:
        return len(self.namespaces.get(namespace, {}))
