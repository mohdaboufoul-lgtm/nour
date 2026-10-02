"""nour/core/clock.py (DESIGN §3.2, §7.4): FakeClock + freezegun lockstep, IdGenerator, process clock.

Proves (MODULES.md "core"): FakeClock and freezegun agree (``datetime.now(tz=UTC)`` inside the
frozen period equals ``clock.now()``) and ``advance``/``set`` move both; ``IdGenerator(seed)`` is
reproducible and sortable; the process clock feeds ``process_now``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from ulid import ULID

from nour.core.clock import (
    DUBAI,
    Clock,
    FakeClock,
    IdGenerator,
    SystemClock,
    process_clock,
    process_now,
    set_process_clock,
)

START = datetime(2026, 10, 5, 7, 0, tzinfo=DUBAI)  # Monday 07:00 Dubai (DESIGN §7)
START_UTC = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)
CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    with FakeClock(START) as fake:
        yield fake


@pytest.fixture
def restore_process_clock() -> Iterator[None]:
    before = process_clock()
    try:
        yield
    finally:
        set_process_clock(before)


# --------------------------------------------------------------------------- Dubai + SystemClock


def test_dubai_zone_is_utc_plus_four_all_year() -> None:
    assert DUBAI.key == "Asia/Dubai"
    assert isinstance(DUBAI, ZoneInfo)
    for month in (1, 7):
        assert datetime(2026, month, 1, tzinfo=DUBAI).utcoffset() == timedelta(hours=4)


def test_system_clock_is_aware_utc_and_dubai_local() -> None:
    sys_clock = SystemClock()
    assert isinstance(sys_clock, Clock)
    now = sys_clock.now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    local = sys_clock.local()
    assert local.utcoffset() == timedelta(hours=4)
    assert abs(local - now) < timedelta(seconds=5)
    assert sys_clock.today_dubai() == sys_clock.local().date()


# --------------------------------------------------------------------------- FakeClock


def test_fake_clock_and_freezegun_agree(clock: FakeClock) -> None:
    assert isinstance(clock, Clock)
    assert clock.now() == START == START_UTC
    assert clock.now().tzinfo is UTC
    assert datetime.now(tz=UTC) == clock.now()
    assert datetime.now(DUBAI) == clock.local()
    assert clock.local().utcoffset() == timedelta(hours=4)
    assert clock.local().hour == 7
    assert clock.today_dubai() == date(2026, 10, 5)
    assert time.time() == clock.now().timestamp()
    assert datetime.now(tz=UTC).timestamp() == START_UTC.timestamp()


def test_advance_moves_both_clocks(clock: FakeClock) -> None:
    before_wall = time.time()
    returned = clock.advance(timedelta(minutes=5))
    assert returned == clock.now() == START_UTC + timedelta(minutes=5)
    assert datetime.now(tz=UTC) == clock.now()
    assert time.time() - before_wall == pytest.approx(300.0)
    clock.advance(timedelta(hours=48))
    assert datetime.now(tz=UTC) == clock.now() == START_UTC + timedelta(hours=48, minutes=5)
    assert clock.today_dubai() == date(2026, 10, 7)


def test_set_moves_both_clocks_forwards_and_backwards(clock: FakeClock) -> None:
    late = datetime(2026, 10, 5, 23, 10, tzinfo=DUBAI)  # quiet hours (SPEC §12)
    clock.set(late)
    assert clock.now() == late and datetime.now(tz=UTC) == late
    assert clock.local().hour == 23
    earlier = datetime(2026, 10, 4, 22, 30, tzinfo=UTC)
    clock.set(earlier)
    assert clock.now() == earlier and datetime.now(tz=UTC) == earlier
    assert clock.now() < late


def test_today_dubai_follows_the_dubai_day_not_utc(clock: FakeClock) -> None:
    clock.set(datetime(2026, 10, 5, 22, 30, tzinfo=UTC))  # 02:30 next day in Dubai
    assert clock.now().date() == date(2026, 10, 5)
    assert clock.today_dubai() == date(2026, 10, 6)


def test_naive_datetimes_are_refused(clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime(2026, 10, 5, 7, 0))  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.set(datetime(2026, 10, 5, 8, 0))  # noqa: DTZ001 - naive on purpose


def test_close_restores_real_time_and_is_idempotent() -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    fake = FakeClock(old)
    assert datetime.now(tz=UTC) == old
    fake.close()
    assert datetime.now(tz=UTC) > old + timedelta(days=365)
    fake.close()  # idempotent
    with pytest.raises(RuntimeError):
        fake.advance(timedelta(seconds=1))
    with pytest.raises(RuntimeError):
        fake.set(old)


def test_nested_fake_clocks_unwind_in_order(clock: FakeClock) -> None:
    inner_start = datetime(2027, 1, 1, tzinfo=UTC)
    with FakeClock(inner_start) as inner:
        assert datetime.now(tz=UTC) == inner.now() == inner_start
        inner.advance(timedelta(days=1))
        assert datetime.now(tz=UTC) == inner_start + timedelta(days=1)
    assert datetime.now(tz=UTC) == clock.now() == START_UTC


def test_tick_true_really_advances() -> None:
    with FakeClock(START, tick=True) as ticking:
        assert ticking.tick is True
        first = ticking.now()
        assert first >= START_UTC
        time.sleep(0.02)
        second = ticking.now()
        assert second > first
        assert second - first < timedelta(seconds=5)
        assert datetime.now(tz=UTC) >= second
        moved = ticking.advance(timedelta(hours=1))
        assert moved >= second + timedelta(hours=1)
        assert datetime.now(tz=UTC) >= moved


# --------------------------------------------------------------------------- IdGenerator


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def test_ids_are_crockford_ulids_stamped_with_the_clock(clock: FakeClock) -> None:
    uid = IdGenerator(clock, seed=1).new()
    assert isinstance(uid, str) and len(uid) == 26
    assert set(uid) <= CROCKFORD
    parsed = ULID.from_str(uid)
    assert parsed.milliseconds == _ms(clock.now())
    assert parsed.datetime == clock.now()


def test_seeded_generator_is_reproducible(clock: FakeClock) -> None:
    first = IdGenerator(clock, seed=7)
    second = IdGenerator(clock, seed=7)
    script = [0, 0, 1, 0, 2, 0]  # seconds to advance before each id
    a: list[str] = []
    for delta in script:
        clock.advance(timedelta(seconds=delta))
        a.append(first.new())
    clock.set(START)
    b: list[str] = []
    for delta in script:
        clock.advance(timedelta(seconds=delta))
        b.append(second.new())
    assert a == b
    assert len(set(a)) == len(a)
    other = IdGenerator(clock, seed=8)
    clock.set(START)
    assert other.new() != a[0]


def test_ids_sort_in_generation_order(clock: FakeClock) -> None:
    gen = IdGenerator(clock, seed=3)
    same_ms = [gen.new() for _ in range(50)]  # frozen clock: all in one millisecond
    assert same_ms == sorted(same_ms)
    assert len(set(same_ms)) == 50
    assert {ULID.from_str(u).milliseconds for u in same_ms} == {_ms(clock.now())}
    # Monotonic within the millisecond: the randomness part increments by one.
    randomness = [int.from_bytes(ULID.from_str(u).bytes[6:], "big") for u in same_ms]
    assert all(b - a == 1 for a, b in zip(randomness[:-1], randomness[1:], strict=True))
    across: list[str] = []
    for _ in range(20):
        clock.advance(timedelta(milliseconds=1))
        across.append(gen.new())
    assert across == sorted(across)
    assert across[0] > same_ms[-1]
    clock.advance(timedelta(hours=1))
    assert gen.new() > across[-1]


def test_fresh_entropy_after_the_clock_moves(clock: FakeClock) -> None:
    gen = IdGenerator(clock, seed=5)
    first = gen.new()
    clock.advance(timedelta(milliseconds=1))
    second = gen.new()
    first_random = int.from_bytes(ULID.from_str(first).bytes[6:], "big")
    second_random = int.from_bytes(ULID.from_str(second).bytes[6:], "big")
    assert second_random != first_random + 1


def test_unseeded_generator_is_unique_and_valid(clock: FakeClock) -> None:
    gen = IdGenerator(clock)
    ids = [gen.new() for _ in range(500)]
    assert len(set(ids)) == 500
    assert ids == sorted(ids)
    assert all(len(u) == 26 and set(u) <= CROCKFORD for u in ids)
    assert ULID.from_str(ids[0]).milliseconds == _ms(clock.now())


def test_ids_stay_unique_when_the_clock_goes_backwards(clock: FakeClock) -> None:
    gen = IdGenerator(clock, seed=11)
    before = [gen.new() for _ in range(3)]
    clock.set(START - timedelta(days=1))
    after = [gen.new() for _ in range(3)]
    assert len(set(before + after)) == 6
    assert all(u < before[0] for u in after)  # stamped with the (earlier) clock


def test_generator_follows_any_clock() -> None:
    uid = IdGenerator(SystemClock(), seed=1).new()
    assert abs(ULID.from_str(uid).datetime - SystemClock().now()) < timedelta(seconds=5)


# --------------------------------------------------------------------------- process clock


def test_process_clock_defaults_to_the_system_clock(restore_process_clock: None) -> None:
    assert isinstance(process_clock(), SystemClock)
    now = process_now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)
    assert abs(now - SystemClock().now()) < timedelta(seconds=5)


def test_set_process_clock_feeds_process_now(restore_process_clock: None, clock: FakeClock) -> None:
    set_process_clock(clock)
    assert process_clock() is clock
    assert process_now() == clock.now() == START_UTC
    clock.advance(timedelta(minutes=30))
    assert process_now() == START_UTC + timedelta(minutes=30)
    set_process_clock(SystemClock())
    assert process_now() == datetime.now(tz=UTC)  # still frozen by the fixture's freezegun
