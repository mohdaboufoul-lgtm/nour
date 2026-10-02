"""Clock, test clock, id generator and the process clock (DESIGN §3.2 ``nour/core/clock.py``;
SPEC §12 §14).

Every timestamp in Nour comes from an injected :class:`Clock` and every id from an
:class:`IdGenerator`; this is the only module that may call ``datetime.now(`` (ruff ``DTZ`` is
switched off for this file alone and ``tests/unit/test_walls.py`` greps the rest of ``nour/``).
Dubai time is the system clock (SPEC §14): ``now()`` is tz-aware UTC for storage and hashing,
``local()`` and ``today_dubai()`` give the Asia/Dubai view the daily rhythm (SPEC §12) runs on.

:class:`FakeClock` wraps ``freezegun.freeze_time`` so ``datetime.now()`` inside any library agrees
with the injected clock, and :func:`set_process_clock` feeds the ORM ``created_at``/``updated_at``
defaults (``nour/db/base.py`` calls :func:`process_now`), so a 48-hour simulation never reads the
wall clock (DESIGN §7.4).
"""

from __future__ import annotations

import os
import random
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from ulid import ULID

from nour.core.types import Ulid

DUBAI = ZoneInfo("Asia/Dubai")
"""§14: Dubai time is the system clock."""

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = timedelta(milliseconds=1)
_RANDOMNESS_BYTES = 10
_MAX_RANDOMNESS = (1 << (8 * _RANDOMNESS_BYTES)) - 1


def _require_aware(when: datetime, what: str) -> datetime:
    if when.tzinfo is None or when.tzinfo.utcoffset(when) is None:
        raise ValueError(f"{what} must be timezone-aware (every Nour timestamp is)")
    return when.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """The time source every service takes by injection (SPEC §12 §14)."""

    def now(self) -> datetime:
        """tz-aware UTC."""
        ...

    def local(self) -> datetime:
        """Asia/Dubai."""
        ...

    def today_dubai(self) -> date: ...


class SystemClock:
    """Production clock: the wall clock, read once per call."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def local(self) -> datetime:
        return self.now().astimezone(DUBAI)

    def today_dubai(self) -> date:
        return self.local().date()


class FakeClock:
    """Test clock. Enters freezegun.freeze_time(start, tick=tick) so library datetime.now() agrees;
    tick=True only for the one @wallclock kill-switch test, where perf_counter must really advance.

    ``start`` must be tz-aware (a Dubai-aware datetime is fine; it is stored as UTC). ``advance``
    and ``set`` move the fake and freezegun together, so ``datetime.now(tz=UTC)``, ``time.time()``
    and ``clock.now()`` never disagree inside the frozen period. ``close()`` (or leaving the
    ``with`` block) stops the freeze; nested FakeClocks must be closed in reverse order, as
    freezegun keeps a stack.
    """

    def __init__(self, start: datetime, *, tick: bool = False) -> None:
        start_utc = _require_aware(start, "FakeClock start")
        self.tick = tick
        self._freezer = freeze_time(start_utc, tick=tick)
        self._factory = self._freezer.start()
        self._closed = False

    def now(self) -> datetime:
        """tz-aware UTC; read from the freezegun factory so the two can never drift."""
        return self._factory().replace(tzinfo=UTC)

    def local(self) -> datetime:
        return self.now().astimezone(DUBAI)

    def today_dubai(self) -> date:
        return self.local().date()

    def advance(self, delta: timedelta) -> datetime:
        """Move both clocks forward by ``delta`` (negative deltas are allowed) and return ``now()``.

        Relative to the *current* time, which matters under ``tick=True`` (freezegun's own
        ``tick()`` is relative to the time first frozen, so it would drop the real time elapsed).
        """
        self._assert_open()
        self._factory.move_to(self._factory() + delta)
        return self.now()

    def set(self, when: datetime) -> None:
        """Move both clocks to the tz-aware instant ``when`` (forwards or backwards)."""
        self._assert_open()
        self._factory.move_to(_require_aware(when, "FakeClock.set"))

    def close(self) -> None:
        """Stop freezegun; idempotent."""
        if not self._closed:
            self._closed = True
            self._freezer.stop()

    def __enter__(self) -> FakeClock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("FakeClock is closed")


class IdGenerator:
    """ULIDs from the clock's milliseconds plus seeded entropy (seed=None → os.urandom). Seeded in tests so
    Replayer reproduces ids.

    Within one millisecond the randomness part is incremented (ULID monotonicity), so ids sort in
    generation order under a frozen clock as well as a moving one. Two generators built with the
    same seed over the same clock yield the same sequence.
    """

    def __init__(self, clock: Clock, seed: int | None = None) -> None:
        self._clock = clock
        self._rng: random.Random | None = random.Random(seed) if seed is not None else None
        self._last_ms = -1
        self._last_randomness = 0

    def _ms(self) -> int:
        return (self._clock.now().astimezone(UTC) - _EPOCH) // _ONE_MS

    def _entropy(self) -> int:
        if self._rng is not None:
            return self._rng.getrandbits(8 * _RANDOMNESS_BYTES)
        return int.from_bytes(os.urandom(_RANDOMNESS_BYTES), "big")

    def new(self) -> Ulid:
        ms = self._ms()
        if ms == self._last_ms:
            if self._last_randomness >= _MAX_RANDOMNESS:
                raise OverflowError("ULID randomness exhausted within one millisecond")
            randomness = self._last_randomness + 1
        else:
            randomness = self._entropy()
        self._last_ms = ms
        self._last_randomness = randomness
        raw = ms.to_bytes(6, "big") + randomness.to_bytes(_RANDOMNESS_BYTES, "big")
        return Ulid(str(ULID.from_bytes(raw)))


_process_clock: Clock = SystemClock()


def set_process_clock(clock: Clock) -> None:
    """ORM created_at/updated_at defaults read this (DESIGN §3.7 ``RecordMixin``)."""
    global _process_clock
    _process_clock = clock


def process_clock() -> Clock:
    """The clock :func:`process_now` reads; tests save and restore it around ``set_process_clock``."""
    return _process_clock


def process_now() -> datetime:
    """tz-aware UTC from the process clock: the only datetime.now() outside tests (ruff DTZ + AST test)."""
    return _process_clock.now()
