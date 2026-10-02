"""Record-only fakes for the phase 1–3 ports (DESIGN §3.9 §6; SPEC §7 §16).

``CalendarPort``, ``TelephonyPort``, ``SandboxPort`` and ``AdPlatformPort`` have a shape now and a
body later (SPEC §16 phases 1–3; the capability definition-of-done test asserts every later-phase
row has a stub). Each fake records its one side-effecting call with the ``PortCall`` it was
given, honours dry run, and returns a canned result; ``FakeCalendar`` serves seeded events.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import AwareDatetime

from nour.core.clock import Clock
from nour.core.ports import CalendarEvent, CallLog, PortCall, SandboxResult
from nour.core.types import Money, SafeStr
from nour.fakes import FakePort


class FakeCalendar(FakePort):
    """CalendarPort fake: ``seed(calendar_id, events)``; ``list_events`` returns overlaps."""

    port_name: str = "calendar"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.events: dict[str, list[CalendarEvent]] = {}
        self.queries: list[tuple[str, AwareDatetime, AwareDatetime]] = []

    def seed(self, calendar_id: str, events: Sequence[CalendarEvent]) -> None:
        for event in events:
            if not isinstance(event, CalendarEvent):
                raise TypeError("seed takes CalendarEvent rows")
        self.events.setdefault(calendar_id, []).extend(events)

    def list_events(
        self, calendar_id: str, start: AwareDatetime, end: AwareDatetime
    ) -> list[CalendarEvent]:
        """Events of ``calendar_id`` overlapping ``[start, end)``, by start time."""
        self.queries.append((calendar_id, start, end))
        self._maybe_fail("list_events")
        rows = [
            event
            for event in self.events.get(calendar_id, [])
            if event.start < end and event.end > start
        ]
        return sorted(rows, key=lambda event: (event.start, event.id))


class FakeTelephony(FakePort):
    """TelephonyPort fake (phase 2 AI voice line): records ``(line_id, to, script)``."""

    port_name: str = "telephony"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.calls: list[tuple[str, str, SafeStr]] = []
        self.dry_run_calls: list[tuple[str, str, SafeStr]] = []
        self._seq = 0

    def place_call(self, call: PortCall, line_id: str, to: str, script: SafeStr) -> str:
        if not isinstance(script, SafeStr):
            raise TypeError("place_call takes a SafeStr script")
        self._record("place_call", call, line_id=line_id, to=to, script=script)
        self._maybe_fail("place_call")
        self._seq += 1
        if call.dry_run:
            self.dry_run_calls.append((line_id, to, script))
            return f"dryrun-call-{self._seq:06d}"
        self.calls.append((line_id, to, script))
        return f"call-{self._seq:06d}"


class FakeSandbox(FakePort):
    """SandboxPort fake (phase 3 code sandbox): records runs; results are scripted or canned."""

    port_name: str = "sandbox"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.runs: list[tuple[str, int]] = []
        self.script: list[SandboxResult] = []

    def enqueue(self, result: SandboxResult) -> None:
        self.script.append(result)

    def run(self, call: PortCall, code: str, timeout_s: int) -> SandboxResult:
        if timeout_s <= 0:
            raise ValueError("timeout_s is positive")
        self._record("run", call, code_len=len(code), timeout_s=timeout_s)
        self._maybe_fail("run")
        self.runs.append((code, timeout_s))
        if self.script:
            return self.script.pop(0)
        return SandboxResult(ok=True, stdout="", artifacts=())


class FakeAds(FakePort):
    """AdPlatformPort fake (phase 3 ad spend): ``budgets[campaign_ref] = daily``."""

    port_name: str = "ads"

    def __init__(self, call_log: CallLog | None = None, clock: Clock | None = None) -> None:
        super().__init__(call_log, clock)
        self.budgets: dict[str, Money] = {}
        self.dry_run_budgets: list[tuple[str, Money]] = []

    def set_budget(self, call: PortCall, campaign_ref: str, daily: Money) -> None:
        if not isinstance(daily, Money):
            raise TypeError("set_budget takes Money")
        if daily.fils < 0:
            raise ValueError("a daily budget is not negative")
        self._record("set_budget", call, campaign_ref=campaign_ref, daily=daily)
        self._maybe_fail("set_budget")
        if call.dry_run:
            self.dry_run_budgets.append((campaign_ref, daily))
            return
        self.budgets[campaign_ref] = daily
