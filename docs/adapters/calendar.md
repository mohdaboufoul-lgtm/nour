# Calendar adapter: holidays, Ramadan and prayer times

Backs `config/calendar.yaml` (SPEC §9 cadences, §12 daily rhythm and quiet hours, §14 UAE
rails). Dubai time (`Asia/Dubai`, UTC+4, no DST) is the system clock. Last verified 2026-10-02.

## 1. Holiday sources and how dates were set

UAE public holidays are the same for the public and private sectors and are fixed by Cabinet
Resolution No. 27 of 2024, published on the official portal:
<https://u.ae/en/information-and-services/public-holidays-and-religious-affairs/public-holidays>
(page updated 2026-07-02). The resolution defines each holiday, not a yearly Gregorian list:

| Holiday | Rule | Gregorian basis |
|---|---|---|
| New Year | 1 January | fixed |
| Eid Al Fitr | 1 to 3 Shawwal; 30 Ramadan added if Ramadan runs 30 days | lunar |
| Arafah Day + Eid Al Adha | 9 to 12 Dhu al-Hijjah | lunar |
| Hijri New Year | 1 Muharram | lunar |
| Prophet Mohammed's Birthday | 12 Rabi' Al Awwal | lunar |
| Eid Al Etihad (National Day) | 2 and 3 December | fixed |

Commemoration Day (30 November) is observed with a flag ceremony but is not a public holiday
under the 2024 resolution; the earlier config entry that bundled it with National Day was
wrong and is now "Eid Al Etihad (National Day)" only. A holiday that lands on a weekend is not
transferred. Exact Gregorian days for lunar holidays are announced by the Cabinet, MOHRE and
FAHR only after the crescent is sighted, usually a few days before.

Remaining 2026: Eid Al Etihad 2026-12-02 to 03 (fixed, `confirm: false`). The 2026 Hijri New
Year (06-16) and Prophet's Birthday (08-25) have already passed.

2027 lunar holidays were not yet announced on 2026-10-02, so each carries `confirm: true` with
an expected date and a `basis`. Expected dates come from two agreeing sources:

- Umm al-Qura tables (via the `hijridate` package), which matched all five UAE-announced lunar
  holidays in 2026 (Eid Al Fitr 03-20, Arafah 05-26, Hijri New Year 06-16, Mawlid 08-25).
- UAE Astronomy Centre expectations reported by Khaleej Times on 2026-08-11/22:
  <https://www.khaleejtimes.com/uae/uae-holidays-2027-possible-6-day-break-multiple-long-weekends-expected>
  and <https://www.khaleejtimes.com/uae/ramadan-eid-al-fitr-eid-al-adha-2027-likely-dates>.

Set values: Eid Al Fitr 2027-03-09 to 03-12 (covers both the 29- and 30-day Ramadan cases),
Arafah + Eid Al Adha 2027-05-15 to 05-18 (may slip to 05-16 to 05-19), Hijri New Year
2027-06-06, Prophet's Birthday 2027-08-14 (a Saturday), Eid Al Etihad 2027-12-02 to 03 and New
Year 2027-01-01 (both fixed, `confirm: false`).

## 2. Ramadan 1448 and the working-hours adjustment

Ramadan is expected to start Monday 2027-02-08 (Umm al-Qura; UAE Astronomy Centre agrees
because the crescent is not visible from the UAE on 02-06, so Sha'ban completes 30 days) and
end 03-08 (29 days) or 03-09 (30 days). `ramadan.dates` holds 2027-02-08 to 2027-03-09 with
`confirm: true`; the last day overlaps the Eid block, which takes precedence.

The spec requires Ramadan hours to be adjusted. The legal basis: UAE Labour Law (Federal
Decree-Law 33 of 2021 and its regulations) cuts the working day by two hours for every private
sector employee, Muslim or not, without a pay cut; MOHRE restates this each year
(<https://mohre.gov.ae/en/media-center/news/13/3/2023/private-sector-working-hours-to-be-reduced-by-2-hours-during-ramadan>,
<https://u.ae/en/information-and-services/public-holidays-and-religious-affairs/ramadan>).
Business contacts are therefore reachable roughly 09:00 to 15:00 and fast until Maghrib
(about 18:10 in February), so `ramadan.outreach_window` narrows to 10:00 to 16:00. Evening
outreach after Iftar is deliberately not enabled; the owner can widen the window if a coat's
customers are consumer-facing and active at night.

## 3. Prayer-time method

The project computes times offline from coordinates (Dubai 25.2048 N, 55.2708 E); no API is
called at runtime. The General Authority of Islamic Affairs, Endowments and Zakat (Awqaf) and
Dubai's IACAD publish timetables but not their formula, so the method was chosen by matching
their published output.

Verified choice: the "Dubai" method documented by Batoul Apps
(<https://cdn.jsdelivr.net/npm/adhan@4.4.4/METHODS.md>; aladhan.com lists it as method 16,
"Dubai (unofficial)"): Fajr angle 18.2 deg, Isha angle 18.2 deg (angle-based, not a fixed
interval), Shafi Asr, method adjustments sunrise -3, Dhuhr +3, Asr +3, Maghrib +3 minutes.
For 2026-10-02 it returns Fajr 04:54, Sunrise 06:08, Dhuhr 12:11, Asr 15:35, Maghrib 18:08,
Isha 19:22; the Awqaf timetable republished at <https://www.khaleejtimes.com/prayer-time-uae>
shows 04:54, 06:08, 12:11, 15:33, 18:08, 19:22. Umm al-Qura (Fajr 18.5 deg, Isha = Maghrib +
90 min) was 3 minutes off on most prayers and 13 minutes late on Isha, so the task's starting
assumption of "Isha 90 minutes after Maghrib" does not hold for Dubai; no Ramadan +30 minute
Isha offset applies either. Expect residual differences of 0 to 2 minutes; the 20-minute
buffer in `outreach_window.prayer_times.buffer_minutes` absorbs them.

Library: `adhanpy` 1.0.5 (<https://pypi.org/project/adhanpy/>, MIT, pure Python, no
dependencies, Python 3.9+; port of adhan-java with `CalculationMethod.DUBAI` built in). It is
not yet in `pyproject.toml`; add `adhanpy>=1.0.5` to `dependencies` (and `hijridate` to
regenerate expected lunar dates each year). Rejected: `adhan` 0.1.1 (2015, LGPL, Python 2.7 to
3.4, unmaintained) and `prayer-times-calculator-offline` 1.0.3 (Home Assistant drop-in, no
Dubai method). Minimal use:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod

pt = PrayerTimes(
    (25.2048, 55.2708),
    datetime(2026, 10, 2),
    CalculationMethod.DUBAI,
    time_zone=ZoneInfo("Asia/Dubai"),
)
pt.fajr, pt.sunrise, pt.dhuhr, pt.asr, pt.maghrib, pt.isha  # aware datetimes
```

`prayer_times.method_adjustments_minutes` in the YAML mirrors what `DUBAI` applies, so a
future swap of library can reproduce the same output from the config alone.

## 4. How the cadence engine consumes `calendar.yaml`

Evaluate a candidate send time `t` (Dubai local) in this order; the first match wins.

1. Quiet hours (`quiet_hours`): 22:00 to 07:00 only emergency categories pass.
2. Blocked dates (`outreach_window.blocked_dates`): any entry whose `date` or `start`..`end`
   (inclusive) contains `t.date()` blocks all outreach. While `confirm: true`, treat the entry
   as provisional: also block the day after the range, and surface the entry in the morning
   brief seven days ahead so the owner confirms against the MOHRE announcement, then flips
   `confirm` to `false` and adjusts the dates. Replies to inbound messages are not outreach
   and are not blocked, but keep them to the daytime window.
3. Weekend and Friday: Saturday and Sunday are the UAE weekend for government; most private
   firms work Saturday. Keep weekends open unless a coat's playbook says otherwise, and apply
   `blocked_weekday_slots` (Friday 11:30 to 13:30 covers Jumu'ah; Dhuhr in Dubai ranges 12:08
   to 12:36 across the year).
4. Ramadan (`outreach_window.ramadan`): when `adjust_hours` is true and `t.date()` falls inside
   any `dates` entry, replace `start`/`end` with `ramadan.outreach_window`.
5. Daily window (`outreach_window.start`/`end`, 09:00 to 20:00): outside it, defer to the next
   open slot.
6. Prayer times: compute the five daily times with the `prayer_times` block (cache per day;
   recompute at midnight Dubai time) and block `[time - buffer, time + buffer]` with
   `buffer_minutes`. Fajr falls before 09:00 and Isha after 20:00 for most of the year, so in
   practice Dhuhr, Asr and Maghrib shape the day. If `source` is `table`, read the owner's
   override table instead of computing.

A blocked `t` moves to the next free minute, re-running the checks; a cadence step that moves
past midnight keeps its day count (day 0, 3, 7, 14 are calendar days, not sends). Log the rule
that moved each send so the weekly review can show how much the calendar costs in delay.

## 5. Open items for the owner

- Confirm each `confirm: true` entry when MOHRE announces it (Ramadan start early Feb 2027;
  Eid Al Fitr early Mar; Eid Al Adha mid May; Hijri New Year early Jun; Mawlid mid Aug).
- Watch for a Cabinet decision moving any holiday; none was announced for 2026 or 2027.
- Decide whether consumer-facing coats may send after Iftar during Ramadan.
- Add `adhanpy` to `pyproject.toml`; re-check computed times against Awqaf every quarter.
