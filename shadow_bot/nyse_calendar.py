"""NYSE trading calendar, hard-coded through 2028 (as the gamma collector does).

Two layers, which tests/test_calendar.py requires to agree:
  * RULES      - a holiday generator (fixed dates with weekend observance, nth weekdays, Good Friday via the
                 Easter algorithm, plus the one-off closures). Checked against the SPY trading dates the study used
                 (1998-2026): every day SPY traded is a trading day here and every other weekday is a holiday.
  * EXPLICIT   - literal holiday and early-close tables for 2026-2028, copied from the published NYSE schedule
                 rules; a test asserts the generator reproduces them.

Outside [CALENDAR_START, CALENDAR_END] every function raises CalendarRange instead of guessing: a silently wrong
calendar would put a shadow entry on a closed day. When 2029 approaches, extend EXPLICIT and CALENDAR_END.

Early closes (1:00 pm ET) cannot be verified from a price calendar. The 2026-2028 set is the published one:
day after Thanksgiving, 24 Dec on a Mon-Thu, and 3 Jul on a Mon-Thu when 4 Jul is a weekday.
"""
from __future__ import annotations

import bisect
import datetime as dt
from functools import lru_cache

CALENDAR_START = dt.date(1998, 1, 1)
CALENDAR_END = dt.date(2028, 12, 31)


class CalendarRange(ValueError):
    """The date is outside the years this module has verified."""


# One-off closures (market closed on a weekday that no rule explains).
SPECIAL_CLOSURES = frozenset({
    dt.date(2001, 9, 11), dt.date(2001, 9, 12), dt.date(2001, 9, 13), dt.date(2001, 9, 14),   # 9/11
    dt.date(2004, 6, 11),                                                                      # Reagan mourning
    dt.date(2007, 1, 2),                                                                       # Ford mourning
    dt.date(2012, 10, 29), dt.date(2012, 10, 30),                                              # Hurricane Sandy
    dt.date(2018, 12, 5),                                                                      # Bush mourning
    dt.date(2025, 1, 9),                                                                       # Carter mourning
})

# Literal tables for the years the bot will run in. Dates are ISO strings so a diff against NYSE's page is easy.
EXPLICIT_HOLIDAYS = {
    2026: ("2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
           "2026-09-07", "2026-11-26", "2026-12-25"),
    2027: ("2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
           "2027-11-25", "2027-12-24"),                       # 1 Jan 2027 is a Friday: closed too (below)
    2028: ("2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04", "2028-09-04",
           "2028-11-23", "2028-12-25"),                       # 1 Jan 2028 is a Saturday: NOT observed on 31 Dec 2027
}
EXPLICIT_HOLIDAYS[2027] = ("2027-01-01",) + EXPLICIT_HOLIDAYS[2027]
EXPLICIT_EARLY_CLOSES = {
    2026: ("2026-11-27", "2026-12-24"),
    2027: ("2027-11-26",),
    2028: ("2028-07-03", "2028-11-24"),
}


# ----------------------------------------------------------------------------- rule generator
def _easter(year: int) -> dt.date:
    """Anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: dt.date) -> dt.date:
    """Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def rule_holidays(year: int) -> frozenset:
    out = set()
    jan1 = dt.date(year, 1, 1)
    if jan1.weekday() != 5:                       # a Saturday 1 Jan is not observed on the Friday before
        out.add(_observed(jan1))
    if year >= 1998:
        out.add(_nth_weekday(year, 1, 0, 3))      # Martin Luther King Jr. Day
    out.add(_nth_weekday(year, 2, 0, 3))          # Washington's Birthday
    out.add(_easter(year) - dt.timedelta(days=2))  # Good Friday
    out.add(_last_weekday(year, 5, 0))            # Memorial Day
    if year >= 2022:
        out.add(_observed(dt.date(year, 6, 19)))  # Juneteenth
    out.add(_observed(dt.date(year, 7, 4)))       # Independence Day
    out.add(_nth_weekday(year, 9, 0, 1))          # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))         # Thanksgiving
    out.add(_observed(dt.date(year, 12, 25)))     # Christmas
    out |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return frozenset(d for d in out if d.year == year)


def rule_early_closes(year: int) -> frozenset:
    out = set()
    out.add(_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1))                 # day after Thanksgiving
    dec24 = dt.date(year, 12, 24)
    if dec24.weekday() <= 3:                                                      # Mon-Thu (Fri 24th is the observed holiday)
        out.add(dec24)
    jul3, jul4 = dt.date(year, 7, 3), dt.date(year, 7, 4)
    if jul3.weekday() <= 3 and jul4.weekday() <= 4 and year >= 1998:              # Jul 4 on Tue-Fri -> Jul 3 closes at 1 pm
        out.add(jul3)
    return frozenset(out)


# ----------------------------------------------------------------------------- calendar tables
@lru_cache(maxsize=1)
def _tables():
    holidays = set()
    for y in range(CALENDAR_START.year, CALENDAR_END.year + 1):
        holidays |= rule_holidays(y)
    for y, lst in EXPLICIT_HOLIDAYS.items():           # the literal tables win over the generator
        holidays -= {d for d in holidays if d.year == y}
        holidays |= {dt.date.fromisoformat(s) for s in lst}
    early = set()
    for y in range(CALENDAR_START.year, CALENDAR_END.year + 1):
        early |= rule_early_closes(y)
    for y, lst in EXPLICIT_EARLY_CLOSES.items():
        early -= {d for d in early if d.year == y}
        early |= {dt.date.fromisoformat(s) for s in lst}
    days, d = [], CALENDAR_START
    while d <= CALENDAR_END:
        if d.weekday() < 5 and d not in holidays:
            days.append(d)
        d += dt.timedelta(days=1)
    return frozenset(holidays), frozenset(early & set(days)), days, {x: i for i, x in enumerate(days)}


def _check(d: dt.date) -> None:
    if not (CALENDAR_START <= d <= CALENDAR_END):
        raise CalendarRange(f"{d} is outside the verified NYSE calendar {CALENDAR_START}..{CALENDAR_END}; "
                            f"extend nyse_calendar.py before running past this date")


def holidays() -> frozenset:
    return _tables()[0]


def trading_days() -> list:
    """All trading days in range, ascending (a copy)."""
    return list(_tables()[2])


def is_trading_day(d: dt.date) -> bool:
    _check(d)
    return d in _tables()[3]


def is_early_close(d: dt.date) -> bool:
    _check(d)
    return d in _tables()[1]


def next_td(d: dt.date, n: int = 1) -> dt.date:
    """The n-th trading day strictly after d (n >= 1), or strictly before d (n <= -1). d need not be a trading day."""
    _check(d)
    if n == 0:
        raise ValueError("n must be non-zero")
    days = _tables()[2]
    if n > 0:
        i = bisect.bisect_right(days, d) + n - 1
    else:
        i = bisect.bisect_left(days, d) + n
    if not (0 <= i < len(days)):
        raise CalendarRange(f"next_td({d}, {n}) falls outside the verified calendar")
    return days[i]


def prev_td(d: dt.date, n: int = 1) -> dt.date:
    return next_td(d, -n)


def td_offset(d: dt.date, k: int) -> dt.date:
    """k trading days from a trading day d (k may be 0 or negative). d must be a trading day."""
    _check(d)
    idx = _tables()[3]
    if d not in idx:
        raise ValueError(f"{d} is not a trading day")
    days = _tables()[2]
    i = idx[d] + k
    if not (0 <= i < len(days)):
        raise CalendarRange(f"td_offset({d}, {k}) falls outside the verified calendar")
    return days[i]


def last_session_on_or_before(d: dt.date) -> dt.date:
    _check(d)
    days = _tables()[2]
    return days[bisect.bisect_right(days, d) - 1]


def trading_days_between(a: dt.date, b: dt.date) -> list:
    """Trading days d with a < d <= b."""
    _check(a), _check(b)
    days = _tables()[2]
    return days[bisect.bisect_right(days, a):bisect.bisect_right(days, b)]


def is_first_td_of_month(d: dt.date) -> bool:
    return is_trading_day(d) and prev_td(d).month != d.month


def is_first_td_of_week(d: dt.date) -> bool:
    """First trading day of its ISO week (a Monday unless it is a holiday)."""
    if not is_trading_day(d):
        return False
    p = prev_td(d)
    return tuple(p.isocalendar())[:2] != tuple(d.isocalendar())[:2]


def is_last_td_of_week(d: dt.date) -> bool:
    if not is_trading_day(d):
        return False
    n = next_td(d)
    return tuple(n.isocalendar())[:2] != tuple(d.isocalendar())[:2]
