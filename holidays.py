# -*- coding: utf-8 -*-
"""Korean public holidays and business-day arithmetic, fully offline.

Reinsurance deadlines are spoken in business days -- "3영업일 뒤 서류 제출",
"다음 영업일 결재" -- so "3일 뒤" lands on a Saturday often enough to matter.

There is no network here to ask a calendar service, so the dates come from
two places:

* **Computed** -- the solar holidays (신정 · 삼일절 · 어린이날 · 현충일 ·
  광복절 · 개천절 · 한글날 · 성탄절) and every substitute day. The substitute
  rule is written in law and deterministic, so these are correct for *any*
  year, past or future, with no table to maintain.
* **Tabulated** -- 설날 · 추석 · 부처님오신날 only. These follow the lunar
  calendar and cannot be derived from a Gregorian date. ``_LUNAR`` carries
  them through 2030; 2026-2027 are verified and the rest are provisional.
  :meth:`HolidayCalendar.coverage_note` says so out loud, and only warns once
  the user is actually living past the table.

Whatever is in the table, the real source of truth is ``holidays.txt`` in the
data folder, which the user owns:

    2026-08-15  광복절
    2026-12-24  창립기념일        <- company days off belong here too
    -2026-10-09                   <- a leading '-' cancels a default entry

That matters more than a perfect government table: a company shutdown day is
a non-working day for this user, and no public source would know it.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

log = logging.getLogger("holidays")

HOLIDAYS_FILENAME = "holidays.txt"

#: Solar-calendar holidays and their substitute-day eligibility.
#: 신정 and 현충일 are the two that never get a substitute day.
_SOLAR = (
    ((1, 1), "신정", False),
    ((3, 1), "삼일절", True),
    ((5, 5), "어린이날", True),
    ((6, 6), "현충일", False),
    ((8, 15), "광복절", True),
    ((10, 3), "개천절", True),
    ((10, 9), "한글날", True),
    ((12, 25), "성탄절", True),
)

#: Lunar-derived dates, which cannot be computed from the Gregorian calendar.
#: 설날 and 추석 are the middle day; the day either side is a holiday too.
#: {year: (설날, 추석, 부처님오신날)}
_LUNAR = {
    2026: ((2, 17), (9, 25), (5, 24)),
    2027: ((2, 7), (9, 15), (5, 13)),
    2028: ((1, 27), (10, 3), (5, 2)),
    2029: ((2, 13), (9, 22), (5, 20)),
    2030: ((2, 3), (9, 12), (5, 9)),
}

#: Lunar dates up to here have been checked; the rest are carried so the app
#: keeps working, and flagged by :func:`coverage_note` so nobody trusts them
#: silently. Solar holidays and substitute days are *computed*, so they are
#: correct for any year regardless of this.
CONFIDENT_THROUGH = 2027

def holidays_for_year(year: int) -> dict[date, str]:
    """Every public holiday in `year`, substitute days included.

    The substitute rule (공휴일에 관한 법률 시행령) is deterministic, so it is
    applied rather than tabulated:

    * 설날 · 추석 연휴 -- a substitute if any of the three days is a Sunday
    * 삼일절 · 어린이날 · 부처님오신날 · 광복절 · 개천절 · 한글날 · 성탄절 --
      a substitute if the day is a Saturday or Sunday
    * 신정 · 현충일 -- never substituted

    The substitute is the next day that is not already a holiday or a weekend.
    """
    days: dict[date, str] = {}
    substitutable: list[tuple[date, str]] = []

    for (month, day), name, subs in _SOLAR:
        try:
            when = date(year, month, day)
        except ValueError:
            continue
        days[when] = name
        if subs:
            substitutable.append((when, name))

    lunar = _LUNAR.get(year)
    if lunar:
        (sm, sd), (cm, cd), (bm, bd) = lunar
        for month, day, label in ((sm, sd, "설날"), (cm, cd, "추석")):
            try:
                middle = date(year, month, day)
            except ValueError:
                continue
            block = [middle - timedelta(days=1), middle, middle + timedelta(days=1)]
            # A day of the block already taken means it lands on another
            # public holiday -- 2028 추석 (10/2-10/4) covers 개천절 on 10/3.
            collides = any(d in days for d in block)
            for offset, when in enumerate(block):
                days.setdefault(when, label if offset == 1 else f"{label} 연휴")
            # One substitute per block: a Sunday inside it, or an overlap with
            # another holiday. Both are grounds under 공휴일에 관한 법률 시행령.
            if collides or any(d.weekday() == 6 for d in block):
                substitutable.append((block[-1], f"{label} 대체공휴일"))
        try:
            buddha = date(year, bm, bd)
            days[buddha] = "부처님오신날"
            substitutable.append((buddha, "부처님오신날"))
        except ValueError:
            pass

    for when, name in substitutable:
        if name.endswith("대체공휴일"):
            needs = True                       # 설날/추석 block, decided above
        else:
            needs = when.weekday() >= 5        # Saturday or Sunday
        if not needs:
            continue
        probe = when + timedelta(days=1)
        for _ in range(10):
            if probe not in days and probe.weekday() < 5:
                base = name.replace(" 대체공휴일", "")
                days[probe] = f"{base} 대체공휴일"
                break
            probe += timedelta(days=1)
    return days


class HolidayCalendar:
    """Public holidays plus whatever the user added, with business-day maths."""

    #: Years generated up front. Anything outside is produced on demand, so a
    #: date far in the future never silently reports "not a holiday".
    SPAN_BEFORE, SPAN_AFTER = 2, 6

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self._map: dict[date, str] = {}
        self._generated: set[int] = set()
        self._custom: dict[date, Optional[str]] = {}
        self._custom_path: Optional[str] = None
        this_year = date.today().year
        for year in range(this_year - self.SPAN_BEFORE, this_year + self.SPAN_AFTER):
            self._ensure_year(year)
        if data_dir:
            self.load_user_file(os.path.join(data_dir, HOLIDAYS_FILENAME))

    # -- loading --------------------------------------------------------- #
    def _ensure_year(self, year: int) -> None:
        """Generate a year on first use, then re-apply the user's overrides.

        The overrides have to win: a generated year arriving later must not
        resurrect a public holiday the user cancelled with '-'.
        """
        if year in self._generated:
            return
        self._generated.add(year)
        try:
            self._map.update(holidays_for_year(year))
        except Exception:                                # noqa: BLE001
            log.exception("Could not build holidays for %d", year)
        for day, name in self._custom.items():
            if day.year != year:
                continue
            if name is None:
                self._map.pop(day, None)
            else:
                self._map[day] = name

    def load_user_file(self, path: str) -> int:
        """Merge ``holidays.txt``. Returns how many lines were applied."""
        self._custom_path = path
        if not os.path.isfile(path):
            return 0
        applied = 0
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                for lineno, raw in enumerate(fh, 1):
                    line = raw.split("#", 1)[0].strip()
                    if not line:
                        continue
                    remove = line.startswith("-")
                    if remove:
                        line = line[1:].strip()
                    parts = line.split(None, 1)
                    try:
                        day = date.fromisoformat(parts[0])
                    except ValueError:
                        log.warning("%s:%d 날짜 형식이 아닙니다: %r", path, lineno, raw.strip())
                        continue
                    self._ensure_year(day.year)
                    if remove:
                        self._custom[day] = None
                        self._map.pop(day, None)
                    else:
                        label = parts[1].strip() if len(parts) > 1 else "휴일"
                        self._custom[day] = label
                        self._map[day] = label
                    applied += 1
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
        return applied

    def write_template(self, path: str) -> bool:
        """Drop a commented starter file so the format is self-explaining."""
        if os.path.exists(path):
            return False
        try:
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(
                    "# 휴일 목록 - 이 파일을 고치면 영업일 계산에 바로 반영됩니다.\n"
                    "# (앱을 다시 켜야 적용됩니다)\n"
                    "#\n"
                    "# 형식:  YYYY-MM-DD  이름\n"
                    "#   2026-12-24  창립기념일\n"
                    "#\n"
                    "# 맨 앞에 '-' 를 붙이면 기본 공휴일을 취소합니다.\n"
                    "#   -2026-10-09\n"
                    "#\n"
                    f"# 기본 공휴일은 {CONFIDENT_THROUGH}년까지 확인된 값입니다.\n"
                    "# 그 이후 연도와 회사 휴무일은 여기에 직접 적어주세요.\n")
            return True
        except OSError as exc:
            log.warning("Could not write %s: %s", path, exc)
            return False

    # -- queries --------------------------------------------------------- #
    def holiday_name(self, day: date) -> Optional[str]:
        if isinstance(day, datetime):
            day = day.date()
        self._ensure_year(day.year)
        return self._map.get(day)

    def is_holiday(self, day: date) -> bool:
        return self.holiday_name(day) is not None

    def is_business_day(self, day: date) -> bool:
        if isinstance(day, datetime):
            day = day.date()
        return day.weekday() < 5 and not self.is_holiday(day)

    def next_business_day(self, day: date, include_today: bool = False) -> date:
        if isinstance(day, datetime):
            day = day.date()
        candidate = day if include_today else day + timedelta(days=1)
        for _ in range(400):
            if self.is_business_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate                       # pathological: give something back

    def previous_business_day(self, day: date, include_today: bool = False) -> date:
        if isinstance(day, datetime):
            day = day.date()
        candidate = day if include_today else day - timedelta(days=1)
        for _ in range(400):
            if self.is_business_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        return candidate

    def add_business_days(self, day: date, count: int) -> date:
        """`count` business days from `day`, not counting `day` itself.

        "3영업일 뒤" on a Friday is the following Wednesday, not Monday.
        """
        if isinstance(day, datetime):
            day = day.date()
        if count == 0:
            return self.next_business_day(day, include_today=True)
        step = 1 if count > 0 else -1
        remaining = abs(int(count))
        candidate = day
        for _ in range(4000):
            candidate += timedelta(days=step)
            if self.is_business_day(candidate):
                remaining -= 1
                if remaining == 0:
                    return candidate
        return candidate

    def business_days_between(self, start: date, end: date) -> int:
        """Business days in [start, end) -- negative if end precedes start."""
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()
        if end == start:
            return 0
        step = 1 if end > start else -1
        count = 0
        candidate = start
        while candidate != end:
            candidate += timedelta(days=step)
            if self.is_business_day(candidate):
                count += step
        return count

    # -- reporting ------------------------------------------------------- #
    def coverage_note(self) -> str:
        """What the calendar actually knows, stated plainly.

        Solar holidays and substitute days are computed and hold for any year.
        Only the lunar ones need a table, so that is the part worth warning
        about -- and only once the user is actually living in those years.
        """
        year = date.today().year
        parts = ["양력 공휴일·대체공휴일은 자동 계산"]
        lunar_last = max(_LUNAR) if _LUNAR else 0
        if year > lunar_last:
            parts.append(f"⚠ 음력 공휴일(설날·추석·부처님오신날) {lunar_last}년까지만 "
                         f"수록 — holidays.txt 에 추가 필요")
        elif year > CONFIDENT_THROUGH:
            parts.append(f"음력 공휴일 {lunar_last}년까지 수록 "
                         f"({CONFIDENT_THROUGH}년까지 검증)")
        else:
            parts.append(f"음력 공휴일 {lunar_last}년까지 수록")
        if self._custom_path and os.path.isfile(self._custom_path):
            parts.append("holidays.txt 반영됨")
        return " · ".join(parts)

    def lunar_years(self) -> tuple[int, int]:
        """(verified through, tabulated through) for the lunar holidays."""
        return CONFIDENT_THROUGH, (max(_LUNAR) if _LUNAR else 0)

    def upcoming(self, start: date, days: int = 90) -> list[tuple[date, str]]:
        if isinstance(start, datetime):
            start = start.date()
        self._ensure_year(start.year)
        self._ensure_year((start + timedelta(days=days)).year)
        out = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            name = self.holiday_name(day)
            if name:
                out.append((day, name))
        return out


_CALENDAR: Optional[HolidayCalendar] = None


def calendar(data_dir: Optional[str] = None) -> HolidayCalendar:
    """Process-wide calendar. Pass `data_dir` once at startup."""
    global _CALENDAR
    if _CALENDAR is None or data_dir is not None:
        _CALENDAR = HolidayCalendar(data_dir)
    return _CALENDAR


def reset_calendar() -> None:
    """Test hook."""
    global _CALENDAR
    _CALENDAR = None


if __name__ == "__main__":                                       # self-test
    import sys, io, tempfile
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    failures = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        global failures
        if not cond:
            failures += 1
            print(f"FAIL {label}  {detail}")

    cal = HolidayCalendar()

    # weekends and holidays are not business days
    check("토요일", not cal.is_business_day(date(2026, 8, 22)))
    check("일요일", not cal.is_business_day(date(2026, 8, 23)))
    check("광복절", not cal.is_business_day(date(2026, 8, 15)))
    check("평일", cal.is_business_day(date(2026, 8, 20)))

    # Friday + 3 business days = Wednesday
    check("금요일 +3영업일",
          cal.add_business_days(date(2026, 8, 21), 3) == date(2026, 8, 26),
          str(cal.add_business_days(date(2026, 8, 21), 3)))
    # across the 추석 block (9/24-26 Thu-Sat, so 9/23 Wed +1 -> 9/28 Mon)
    check("추석 건너뛰기",
          cal.add_business_days(date(2026, 9, 23), 1) == date(2026, 9, 28),
          str(cal.add_business_days(date(2026, 9, 23), 1)))
    check("다음 영업일(금->월)",
          cal.next_business_day(date(2026, 8, 21)) == date(2026, 8, 24))
    check("이전 영업일(월->금)",
          cal.previous_business_day(date(2026, 8, 24)) == date(2026, 8, 21))
    check("영업일 0 = 오늘이 영업일이면 오늘",
          cal.add_business_days(date(2026, 8, 20), 0) == date(2026, 8, 20))
    check("영업일 0 = 휴일이면 다음 영업일",
          cal.add_business_days(date(2026, 8, 22), 0) == date(2026, 8, 24))
    check("음수 영업일",
          cal.add_business_days(date(2026, 8, 24), -1) == date(2026, 8, 21))
    check("사이 영업일 수",
          cal.business_days_between(date(2026, 8, 17), date(2026, 8, 24)) == 5,
          str(cal.business_days_between(date(2026, 8, 17), date(2026, 8, 24))))

    # user file: add a company day off, cancel a public one
    tmp = tempfile.mkdtemp(prefix="hol_")
    path = os.path.join(tmp, HOLIDAYS_FILENAME)
    check("템플릿 생성", cal.write_template(path))
    check("이미 있으면 덮어쓰지 않음", not cal.write_template(path))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("2026-12-24  창립기념일\n")
        fh.write("-2026-10-09\n")
        fh.write("쓰레기 줄\n")             # must be skipped, not fatal
    cal2 = HolidayCalendar(tmp)
    check("회사 휴무일 반영", not cal2.is_business_day(date(2026, 12, 24)))
    check("기본 공휴일 취소", cal2.is_business_day(date(2026, 10, 9)))
    check("잘못된 줄은 무시", cal2.is_business_day(date(2026, 8, 20)))
    check("커버리지 안내", "holidays.txt" in cal2.coverage_note(), cal2.coverage_note())

    # --- computed substitute days ------------------------------------------ #
    # 2026: 삼일절 Sun -> Mon 3/2, 광복절 Sat -> Mon 8/17,
    #       개천절 Sat -> Mon 10/5, 부처님오신날 Sun -> Mon 5/25
    for when, label in ((date(2026, 3, 2), "삼일절"),
                        (date(2026, 8, 17), "광복절"),
                        (date(2026, 10, 5), "개천절"),
                        (date(2026, 5, 25), "부처님오신날")):
        got = cal.holiday_name(when)
        check(f"2026 {label} 대체", got is not None and "대체" in got, f"{when} -> {got}")
    # 한글날 2026-10-09 is a Friday: a weekday, so no substitute is due.
    check("평일 공휴일엔 대체 없음", cal.holiday_name(date(2026, 10, 12)) is None,
          str(cal.holiday_name(date(2026, 10, 12))))
    # 신정과 현충일은 주말에 걸려도 대체가 없다. 2027-01-01 is a Friday;
    # 2026-06-06 현충일 is a Saturday -> the following Monday stays a workday.
    check("현충일은 대체 없음", cal.is_business_day(date(2026, 6, 8)),
          str(cal.holiday_name(date(2026, 6, 8))))

    # 2028: the 추석 block (10/2-10/4) swallows 개천절 on 10/3, which earns a
    # substitute even though no Sunday is involved.
    y2028 = holidays_for_year(2028)
    check("연휴가 다른 공휴일과 겹치면 대체",
          any("추석 대체" in n for n in y2028.values()),
          str(sorted((str(d), n) for d, n in y2028.items() if d.month == 10)))

    # A year with no lunar data still yields the solar holidays and never
    # invents a 추석 -- a wrong lunar date is worse than a missing one.
    far = holidays_for_year(2035)
    check("표 밖 연도: 양력 공휴일 계산됨", far.get(date(2035, 3, 1)) == "삼일절")
    check("표 밖 연도: 음력 공휴일 없음",
          not any("추석" in n or "설날" in n for n in far.values()))
    check("표 밖 연도에도 안 죽음", not cal.is_business_day(date(2035, 1, 1)))
    check("표 밖 평일", cal.is_business_day(date(2035, 1, 2)))

    # A cancelled holiday must stay cancelled when its year is generated late.
    tmp2 = tempfile.mkdtemp(prefix="hol2_")
    with open(os.path.join(tmp2, HOLIDAYS_FILENAME), "w", encoding="utf-8") as fh:
        fh.write("-2033-03-01\n2033-07-07  창립기념일\n")
    late = HolidayCalendar(tmp2)
    check("나중에 생성된 연도에도 취소 유지", late.is_business_day(date(2033, 3, 1)),
          str(late.holiday_name(date(2033, 3, 1))))
    check("나중에 생성된 연도에도 추가 유지", not late.is_business_day(date(2033, 7, 7)))

    ups = cal.upcoming(date(2026, 9, 1), 40)
    check("다가오는 휴일", any(n.startswith("추석") for _, n in ups), str(ups))

    print(f"holidays self-test {'OK' if not failures else f'{failures} FAILURE(S)'}")
    print(" ", cal.coverage_note())
    sys.exit(1 if failures else 0)
