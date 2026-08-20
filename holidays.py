# -*- coding: utf-8 -*-
"""Korean public holidays and business-day arithmetic, fully offline.

Reinsurance deadlines are spoken in business days -- "3영업일 뒤 서류 제출",
"다음 영업일 결재" -- so "3일 뒤" lands on a Saturday often enough to matter.

There is no network here to ask a calendar service, and the lunar holidays
(설날 · 추석 · 부처님오신날) cannot be derived from the Gregorian date, so the
dates have to be shipped as a table. That table is the weak point of this
module: I can state 2026-2027 with confidence and the later years are best
treated as provisional.

So the file below is only a *default*. The real source of truth is
``holidays.txt`` in the data folder, which the user owns:

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

#: Confident: 2026-2027. Later years are carried for convenience and flagged
#: by :func:`coverage_note` so nobody trusts them silently.
CONFIDENT_THROUGH = 2027

_DEFAULTS: dict[str, str] = {
    # ---- 2026 ----
    "2026-01-01": "신정",
    "2026-02-16": "설날 연휴",
    "2026-02-17": "설날",
    "2026-02-18": "설날 연휴",
    "2026-03-01": "삼일절",
    "2026-03-02": "삼일절 대체공휴일",
    "2026-05-05": "어린이날",
    "2026-05-24": "부처님오신날",
    "2026-05-25": "부처님오신날 대체공휴일",
    "2026-06-06": "현충일",
    "2026-08-15": "광복절",
    "2026-08-17": "광복절 대체공휴일",
    "2026-09-24": "추석 연휴",
    "2026-09-25": "추석",
    "2026-09-26": "추석 연휴",
    "2026-10-03": "개천절",
    "2026-10-05": "개천절 대체공휴일",
    "2026-10-09": "한글날",
    "2026-12-25": "성탄절",
    # ---- 2027 ----
    "2027-01-01": "신정",
    "2027-02-06": "설날 연휴",
    "2027-02-07": "설날",
    "2027-02-08": "설날 연휴",
    "2027-02-09": "설날 대체공휴일",
    "2027-03-01": "삼일절",
    "2027-05-05": "어린이날",
    "2027-05-13": "부처님오신날",
    "2027-06-06": "현충일",
    "2027-06-07": "현충일 대체공휴일",
    "2027-08-15": "광복절",
    "2027-08-16": "광복절 대체공휴일",
    "2027-09-14": "추석 연휴",
    "2027-09-15": "추석",
    "2027-09-16": "추석 연휴",
    "2027-10-03": "개천절",
    "2027-10-04": "개천절 대체공휴일",
    "2027-10-09": "한글날",
    "2027-10-11": "한글날 대체공휴일",
    "2027-12-25": "성탄절",
}

# Fixed-date holidays, used to keep *something* sensible past the table. The
# lunar ones are deliberately absent -- a wrong 추석 is worse than none.
_FIXED = {(1, 1): "신정", (3, 1): "삼일절", (5, 5): "어린이날",
          (6, 6): "현충일", (8, 15): "광복절", (10, 3): "개천절",
          (10, 9): "한글날", (12, 25): "성탄절"}


class HolidayCalendar:
    """Public holidays plus whatever the user added, with business-day maths."""

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self._map: dict[date, str] = {}
        self._custom_path: Optional[str] = None
        self._load_defaults()
        if data_dir:
            self.load_user_file(os.path.join(data_dir, HOLIDAYS_FILENAME))

    # -- loading --------------------------------------------------------- #
    def _load_defaults(self) -> None:
        for text, name in _DEFAULTS.items():
            try:
                self._map[date.fromisoformat(text)] = name
            except ValueError:
                log.warning("Bad built-in holiday date %r", text)

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
                    if remove:
                        self._map.pop(day, None)
                    else:
                        self._map[day] = parts[1].strip() if len(parts) > 1 else "휴일"
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
        if day in self._map:
            return self._map[day]
        # Past the shipped table, still honour the fixed-date holidays.
        if day.year > max(d.year for d in self._map):
            return _FIXED.get((day.month, day.day))
        return None

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
        last = max(self._map) if self._map else None
        note = (f"기본 공휴일 {CONFIDENT_THROUGH}년까지 확인됨"
                f" (표는 {last.year if last else '-'}년까지)")
        if self._custom_path and os.path.isfile(self._custom_path):
            note += " · holidays.txt 반영됨"
        return note

    def upcoming(self, start: date, days: int = 90) -> list[tuple[date, str]]:
        if isinstance(start, datetime):
            start = start.date()
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

    # beyond the table we still know the fixed holidays, and never crash
    check("표 이후 고정 공휴일", not cal.is_business_day(date(2031, 1, 1)))
    check("표 이후 평일", cal.is_business_day(date(2031, 1, 2)))

    ups = cal.upcoming(date(2026, 9, 1), 40)
    check("다가오는 휴일", any(n.startswith("추석") for _, n in ups), str(ups))

    print(f"holidays self-test {'OK' if not failures else f'{failures} FAILURE(S)'}")
    print(" ", cal.coverage_note())
    sys.exit(1 if failures else 0)
