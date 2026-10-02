"""``FakeObjectStorage``: the UAE bucket (DESIGN §3.9 §6; SPEC §11 §13 §14).

Vault blobs, message bodies and rendered documents live here, never in a prompt or a row
(SPEC §11). ``put``/``get``/``delete`` are a dict; ``signed_link`` records ``(key, recipient,
ttl)`` because every share link is recipient-bound and expiring (SPEC §11). Blob contents are
never hashed into the ``CallLog`` (a rendered invoice carries an IBAN and every Tier 2 hash must
be keyed, SPEC §10): the log sees the key, the size and the content type.

A put is bookkeeping, not an outbound effect, so it is kept under dry run; a signed link under dry
run is recorded in ``dry_run_links`` and no shareable link exists.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from nour.core.clock import Clock
from nour.core.ports import CallLog, PortCall
from nour.fakes import FakePort


class FakeObjectStorage(FakePort):
    """ObjectStoragePort fake: ``objects`` dict; ``links`` list of ``(key, recipient, ttl)``."""

    port_name: str = "objects"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.links: list[tuple[str, str, timedelta]] = []
        self.dry_run_links: list[tuple[str, str, timedelta]] = []
        self.deleted: list[str] = []
        self.dry_run_deletes: list[str] = []

    def put(self, call: PortCall, key: str, data: bytes, *, content_type: str) -> str:
        """Store ``data`` under ``key`` and return the storage ref (the key)."""
        if not isinstance(key, str) or not key:
            raise ValueError("an object key is a non-empty str")
        if isinstance(data, str) or not isinstance(data, bytes | bytearray):
            raise TypeError("put takes bytes, never text")
        self._record("put", call, key=key, size=len(data), content_type=content_type)
        self._maybe_fail("put")
        self.objects[key] = bytes(data)
        self.content_types[key] = content_type
        return key

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise KeyError(f"no object at {key!r}")
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, call: PortCall, key: str) -> None:
        self._record("delete", call, key=key)
        self._maybe_fail("delete")
        if key not in self.objects:
            raise KeyError(f"no object at {key!r}")
        if call.dry_run:
            self.dry_run_deletes.append(key)
            return
        del self.objects[key]
        del self.content_types[key]
        self.deleted.append(key)

    def signed_link(self, call: PortCall, key: str, ttl: timedelta, recipient: str) -> str:
        """A recipient-bound, expiring link (SPEC §11); recorded as ``(key, recipient, ttl)``."""
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("a signed link needs a positive ttl")
        if not recipient:
            raise ValueError("a signed link is bound to a recipient")
        self._record(
            "signed_link", call, key=key, recipient=recipient, ttl_s=int(ttl.total_seconds())
        )
        self._maybe_fail("signed_link")
        if key not in self.objects:
            raise KeyError(f"no object at {key!r}")
        expires = int((self.clock.now() + ttl).timestamp())
        if call.dry_run:
            self.dry_run_links.append((key, recipient, ttl))
            return f"https://objects.fake.example/dry-run/{key}?for={recipient}&exp={expires}"
        self.links.append((key, recipient, ttl))
        return f"https://objects.fake.example/{key}?for={recipient}&exp={expires}"

    def text_sinks(self) -> Iterator[tuple[str, str]]:
        """``(key, text)`` for every object decoded as UTF-8 (lossy): what a leak scan reads."""
        for key, data in self.objects.items():
            yield key, data.decode("utf-8", "replace")
