"""
db_manager.py
=============
SQLite persistence layer for the Offline Smart HUD.

Design notes
------------
* Two physical databases are used, exactly as specified:
    - ``schedules.db``     -> ``schedules`` table
    - ``chat_history.db``  -> ``chat_messages`` table
* Every connection is stored in a ``threading.local()`` slot, so the same
  manager instance can safely be shared between the Qt GUI thread, the
  APScheduler worker thread and the LLM QThread (sqlite3 objects are *not*
  thread-safe and must never cross thread boundaries).
* All writes go through a re-entrant lock; SQLite is put in WAL mode so a
  reader (the scheduler poll) never blocks a writer (the GUI).
* No network, no ORM, no external services -> fully air-gapped.

Public API
----------
    db = DatabaseManager()                      # uses ./  as base dir
    db.add_schedule(title, target_time, ...)    # -> row id
    db.list_schedules(include_done=False)       # -> list[Schedule]
    db.due_schedules(now)                       # -> list[Schedule]
    db.mark_notified(id)  /  db.delete_schedule(id)
    db.complete_schedule(id, done)              # user tick-off  -> (action, next)
    db.roll_forward(schedule, now)              # alarm fired    -> next time
    db.add_message('user', 'hi') / db.recent_messages(50)
    db.sanitize_chat() / db.clear_chat()

Two ways a recurrence advances
------------------------------
* :meth:`DatabaseManager.roll_forward` -- the *alarm* fired; the scheduler moves
  the row on so it can ring again next cycle.
* :meth:`DatabaseManager.complete_schedule` -- the *user* ticked it off. A
  recurring chore is never "done forever": the row stays open and jumps to its
  next slot. Only one-shot items get ``is_done = 1``.

The recurrence helpers (:func:`compute_next_trigger`) are pure functions and
therefore trivially unit-testable without a database.
"""

from __future__ import annotations

import calendar
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

TIME_FMT = "%Y-%m-%d %H:%M:%S"

REPEAT_NONE = "none"
REPEAT_DAILY = "daily"
REPEAT_WEEKLY = "weekly"
REPEAT_MONTHLY = "monthly"
VALID_REPEATS = (REPEAT_NONE, REPEAT_DAILY, REPEAT_WEEKLY, REPEAT_MONTHLY)

# Human labels used by the UI chips (Korean + English so the HUD reads well
# for the primary user while staying understandable in screenshots).
REPEAT_LABELS = {
    REPEAT_NONE: "1회",
    REPEAT_DAILY: "매일",
    REPEAT_WEEKLY: "매주",
    REPEAT_MONTHLY: "매월",
}

# Monday == 0 to match ``datetime.weekday()``.
WEEKDAY_TOKENS = {
    "mon": 0, "monday": 0, "월": 0, "월요일": 0,
    "tue": 1, "tues": 1, "tuesday": 1, "화": 1, "화요일": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "수": 2, "수요일": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "목": 3, "목요일": 3,
    "fri": 4, "friday": 4, "금": 4, "금요일": 4,
    "sat": 5, "saturday": 5, "토": 5, "토요일": 5,
    "sun": 6, "sunday": 6, "일": 6, "일요일": 6,
}
WEEKDAY_NAMES_KO = ["월", "화", "수", "목", "금", "토", "일"]


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Schedule:
    """A single row of the ``schedules`` table, with parsed datetime."""

    id: int
    title: str
    target_time: datetime
    repeat_type: str = REPEAT_NONE
    repeat_detail: str = ""
    notified: int = 0
    is_done: int = 0
    created_at: str = ""

    # -- convenience ------------------------------------------------------- #
    @property
    def is_recurring(self) -> bool:
        return self.repeat_type in (REPEAT_DAILY, REPEAT_WEEKLY, REPEAT_MONTHLY)

    @property
    def repeat_label(self) -> str:
        """Chip text, e.g. ``매주 (월)``."""
        base = REPEAT_LABELS.get(self.repeat_type, REPEAT_LABELS[REPEAT_NONE])
        if self.repeat_type == REPEAT_WEEKLY:
            days = parse_weekdays(self.repeat_detail) or [self.target_time.weekday()]
            return f"{base} ({format_weekdays(days)})"
        if self.repeat_type == REPEAT_MONTHLY:
            day = parse_month_day(self.repeat_detail) or self.target_time.day
            return f"{base} ({day}일)"
        return base

    def seconds_left(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now()
        return (self.target_time - now).total_seconds()

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Schedule":
        return Schedule(
            id=row["id"],
            title=row["title"],
            target_time=parse_time(row["target_time"]) or datetime.now(),
            repeat_type=(row["repeat_type"] or REPEAT_NONE),
            repeat_detail=(row["repeat_detail"] or ""),
            notified=int(row["notified"] or 0),
            is_done=int(row["is_done"] or 0),
            created_at=(row["created_at"] or ""),
        )


@dataclass(slots=True)
class ChatMessage:
    """A single row of the ``chat_messages`` table."""

    id: int
    sender: str          # 'user' | 'ai'
    message: str
    timestamp: str = field(default="")

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ChatMessage":
        return ChatMessage(
            id=row["id"],
            sender=row["sender"],
            message=row["message"] or "",
            timestamp=row["timestamp"] or "",
        )


# --------------------------------------------------------------------------- #
# Pure helpers - time parsing / formatting
# --------------------------------------------------------------------------- #

def fmt_time(dt: datetime) -> str:
    """``datetime`` -> ``'YYYY-MM-DD HH:MM:SS'`` (the on-disk format)."""
    return dt.strftime(TIME_FMT)


def parse_time(text: str | datetime | None) -> Optional[datetime]:
    """Lenient parser for the stored/LLM-produced timestamp strings.

    Accepts the canonical ``YYYY-MM-DD HH:MM:SS`` plus a handful of shapes the
    LLM occasionally emits (``T`` separator, missing seconds, slashes).
    Returns ``None`` when nothing sensible can be extracted.
    """
    if isinstance(text, datetime):
        return text
    if not text:
        return None
    raw = str(text).strip().replace("T", " ").replace("/", "-")
    # Drop trailing timezone markers / fractional seconds the model may add.
    raw = raw.split(".")[0].replace("Z", "").strip()
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d %H", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    log.debug("parse_time: unrecognised timestamp %r", text)
    return None


def parse_weekday(detail: str | int | None) -> Optional[int]:
    """``'월요일'`` / ``'mon'`` / ``'0'`` -> ``0`` (Monday). ``None`` if unknown.

    For details naming several days (``'화,목'``) the first one is returned;
    use :func:`parse_weekdays` when you need the whole set.
    """
    days = parse_weekdays(detail)
    return days[0] if days else None


def parse_weekdays(detail: str | int | None) -> list[int]:
    """Parse one *or more* weekdays out of ``repeat_detail``.

    Handles ``'화'``, ``'화요일'``, ``'화,목'``, ``'화요일, 목요일'``,
    ``'월수금'``, ``'mon,thu'`` and bare indices. Returns a sorted, de-duplicated
    list of ``datetime.weekday()`` values (Monday = 0); empty when nothing
    recognisable is present.
    """
    if detail is None or detail == "":
        return []
    if isinstance(detail, int):
        return [detail % 7]

    text = str(detail).strip().lower()
    found: set[int] = set()

    # 1) explicit separators: "화,목" / "mon, thu" / "화요일 목요일"
    for piece in re.split(r"[,\s/&+·|]+|and|그리고|이랑|하고|랑", text):
        piece = piece.strip()
        if not piece:
            continue
        if piece in WEEKDAY_TOKENS:
            found.add(WEEKDAY_TOKENS[piece])
        elif piece.isdigit() and 0 <= int(piece) <= 6:
            found.add(int(piece))
    if found:
        return sorted(found)

    # 2) run-on Korean form: "월수금", "화목"
    korean = re.findall(r"[월화수목금토일]", text)
    if korean:
        for ch in korean:
            found.add(WEEKDAY_TOKENS[ch])
        return sorted(found)

    # 3) last resort: substring scan for English names
    for key, value in WEEKDAY_TOKENS.items():
        if len(key) > 2 and key in text:
            found.add(value)
    return sorted(found)


def format_weekdays(weekdays: list[int]) -> str:
    """``[1, 3]`` -> ``'화,목'`` (the canonical ``repeat_detail`` form)."""
    return ",".join(WEEKDAY_NAMES_KO[d] for d in sorted(set(weekdays)) if 0 <= d <= 6)


def split_keywords(keywords: str) -> list[str]:
    """``"특약OS이월, 특약이월"`` -> ``['특약OS이월', '특약이월']``.

    Commas separate alternatives; a bare space-separated string is treated as
    one phrase *and* as its individual words, so "특약 이월" matches a subject
    containing the whole phrase or either word. Duplicates and 1-character
    fragments are dropped -- a single letter would match almost any mail.
    """
    if not keywords:
        return []
    out: list[str] = []
    for chunk in re.split(r"[,;|/·]+", str(keywords)):
        phrase = chunk.strip()
        if len(phrase) >= 2:
            out.append(phrase)
        if " " in phrase:
            out.extend(word for word in phrase.split() if len(word) >= 2)
    seen: set[str] = set()
    unique: list[str] = []
    for word in out:
        key = word.lower()
        if key not in seen:
            seen.add(key)
            unique.append(word)
    return unique


def parse_month_day(detail: str | int | None) -> Optional[int]:
    """Extract a 1-31 day-of-month anchor from ``repeat_detail``."""
    if detail is None or detail == "":
        return None
    if isinstance(detail, int):
        return detail if 1 <= detail <= 31 else None
    digits = ""
    for ch in str(detail):
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if digits:
        value = int(digits)
        if 1 <= value <= 31:
            return value
    return None


def _add_months(dt: datetime, months: int, anchor_day: Optional[int] = None) -> datetime:
    """Month arithmetic with end-of-month clamping.

    ``anchor_day`` lets a "31st of every month" schedule survive February:
    the anchor is remembered, February clamps to the 28th/29th, and March
    returns to the 31st.
    """
    day = anchor_day or dt.day
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def compute_next_trigger(
    current: datetime,
    repeat_type: str,
    repeat_detail: str = "",
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Return the next fire time strictly greater than ``now``.

    ``None`` is returned for non-recurring schedules (nothing to advance to).
    The function always makes progress -- it steps forward until the result is
    in the future -- so a HUD that was closed for a month still lands on a
    sensible next occurrence instead of firing a burst of stale alarms.
    """
    now = now or datetime.now()
    repeat_type = (repeat_type or REPEAT_NONE).lower()
    if repeat_type not in (REPEAT_DAILY, REPEAT_WEEKLY, REPEAT_MONTHLY):
        return None

    nxt = current
    guard = 0                      # hard stop against pathological input
    MAX_STEPS = 5000

    if repeat_type == REPEAT_DAILY:
        # Jump most of the gap in one go, then step day-by-day.
        if nxt <= now:
            gap_days = (now.date() - nxt.date()).days
            if gap_days > 0:
                nxt += timedelta(days=gap_days)
        while nxt <= now and guard < MAX_STEPS:
            nxt += timedelta(days=1)
            guard += 1
        return nxt

    if repeat_type == REPEAT_WEEKLY:
        # One or more weekdays ("매주 화,목"). Walk forward day by day and take
        # the first matching weekday strictly after `now`, keeping the time.
        weekdays = parse_weekdays(repeat_detail) or [current.weekday()]
        time_of_day = current.time()
        start = current.date() if current > now else now.date()
        for offset in range(0, 15):                # 15 days covers any weekly set
            day = start + timedelta(days=offset)
            if day.weekday() in weekdays:
                candidate = datetime.combine(day, time_of_day)
                if candidate > now:
                    return candidate
        return current + timedelta(days=7)         # unreachable safety net

    # monthly
    anchor = parse_month_day(repeat_detail) or current.day
    while nxt <= now and guard < MAX_STEPS:
        nxt = _add_months(nxt, 1, anchor)
        guard += 1
    return nxt


def normalise_repeat(repeat_type: str | None) -> str:
    """Coerce arbitrary model output into one of the four allowed values."""
    value = (repeat_type or REPEAT_NONE).strip().lower()
    aliases = {
        "": REPEAT_NONE, "once": REPEAT_NONE, "single": REPEAT_NONE,
        "1회": REPEAT_NONE, "없음": REPEAT_NONE, "null": REPEAT_NONE,
        "day": REPEAT_DAILY, "everyday": REPEAT_DAILY, "매일": REPEAT_DAILY,
        "week": REPEAT_WEEKLY, "매주": REPEAT_WEEKLY,
        "month": REPEAT_MONTHLY, "매월": REPEAT_MONTHLY, "매달": REPEAT_MONTHLY,
        "yearly": REPEAT_NONE,   # unsupported -> treat as one-shot
    }
    value = aliases.get(value, value)
    return value if value in VALID_REPEATS else REPEAT_NONE


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

_SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    target_time   TEXT    NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS'
    repeat_type   TEXT    NOT NULL DEFAULT 'none',-- none|daily|weekly|monthly
    repeat_detail TEXT    DEFAULT '',
    notified      INTEGER NOT NULL DEFAULT 0,     -- 0 = pending, 1 = fired
    is_done       INTEGER NOT NULL DEFAULT 0,     -- 0 = open,    1 = completed
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules (notified, is_done, target_time);

-- "Tell me when the 특약OS이월 mail lands, and remind me what to do next."
CREATE TABLE IF NOT EXISTS awaited_emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keywords        TEXT    NOT NULL,               -- "특약OS이월, 특약이월"
    sender_filter   TEXT    DEFAULT '',             -- "팀장" or "kim@koreanre.co.kr"
    reminder_action TEXT    NOT NULL,               -- follow-up steps, may be multi-line
    is_triggered    INTEGER NOT NULL DEFAULT 0,     -- 0 = waiting, 1 = fired
    is_active       INTEGER NOT NULL DEFAULT 1,     -- 1 = enabled
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_awaited_active
    ON awaited_emails (is_active, is_triggered);
"""

_CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sender    TEXT NOT NULL,                      -- 'user' | 'ai'
    message   TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_chat_time ON chat_messages (id);
"""


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

class DatabaseManager:
    """Thread-safe facade over the two SQLite files."""

    def __init__(self, base_dir: str | os.PathLike | None = None) -> None:
        self.base_dir = os.path.abspath(base_dir or os.getcwd())
        os.makedirs(self.base_dir, exist_ok=True)
        self.schedule_path = os.path.join(self.base_dir, "schedules.db")
        self.chat_path = os.path.join(self.base_dir, "chat_history.db")

        self._local = threading.local()
        self._lock = threading.RLock()
        self._init_schema()

    # ---------------- connection plumbing ---------------- #

    def _connect(self, path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL + NORMAL sync: readers never block the GUI, still crash-safe.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:            # e.g. read-only media
            log.warning("PRAGMA setup failed for %s: %s", path, exc)
        return conn

    def _sched(self) -> sqlite3.Connection:
        if getattr(self._local, "sched", None) is None:
            self._local.sched = self._connect(self.schedule_path)
        return self._local.sched

    def _chat(self) -> sqlite3.Connection:
        if getattr(self._local, "chat", None) is None:
            self._local.chat = self._connect(self.chat_path)
        return self._local.chat

    def _init_schema(self) -> None:
        with self._lock:
            self._sched().executescript(_SCHEDULE_SCHEMA)
            self._chat().executescript(_CHAT_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns that older installs may be missing (idempotent)."""
        cols = {r["name"] for r in self._sched().execute("PRAGMA table_info(schedules)")}
        if "created_at" not in cols:
            try:
                self._sched().execute(
                    "ALTER TABLE schedules ADD COLUMN created_at TEXT "
                    "NOT NULL DEFAULT ''"
                )
                log.info("Migrated schedules: added created_at")
            except sqlite3.Error as exc:
                log.warning("Migration failed: %s", exc)

    def close(self) -> None:
        """Close this thread's connections (call from each worker on shutdown)."""
        for attr in ("sched", "chat"):
            conn = getattr(self._local, attr, None)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                setattr(self._local, attr, None)

    # ------------------------------------------------------------------ #
    # schedules - CRUD
    # ------------------------------------------------------------------ #

    def add_schedule(
        self,
        title: str,
        target_time: datetime | str,
        repeat_type: str = REPEAT_NONE,
        repeat_detail: str = "",
    ) -> int:
        """Insert a schedule and return its new row id.

        ``target_time`` may be a ``datetime`` or a string; invalid strings raise
        ``ValueError`` so the caller can fall back to the manual dialog.
        """
        title = (title or "").strip() or "(제목 없음)"
        dt = parse_time(target_time)
        if dt is None:
            raise ValueError(f"Unparseable target_time: {target_time!r}")
        repeat_type = normalise_repeat(repeat_type)

        # Derive a stable anchor so recurrence survives month-end clamping and
        # weekday re-alignment even when the model left repeat_detail empty.
        detail = (repeat_detail or "").strip()
        if repeat_type == REPEAT_WEEKLY:
            # Canonicalise to "화,목" so the label and recurrence agree.
            detail = format_weekdays(parse_weekdays(detail) or [dt.weekday()])
        elif repeat_type == REPEAT_MONTHLY and parse_month_day(detail) is None:
            detail = str(dt.day)

        with self._lock:
            cur = self._sched().execute(
                "INSERT INTO schedules "
                "(title, target_time, repeat_type, repeat_detail, notified, is_done, created_at) "
                "VALUES (?,?,?,?,0,0,?)",
                (title, fmt_time(dt), repeat_type, detail, fmt_time(datetime.now())),
            )
            new_id = int(cur.lastrowid)
        log.info("Added schedule #%d %r at %s (%s)", new_id, title, fmt_time(dt), repeat_type)
        return new_id

    def update_schedule(self, schedule_id: int, **fields) -> None:
        """Partial update. Only known columns are accepted (SQL-injection safe)."""
        allowed = {
            "title", "target_time", "repeat_type",
            "repeat_detail", "notified", "is_done",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise KeyError(f"Unknown schedule column: {key}")
            if key == "target_time":
                dt = parse_time(value)
                if dt is None:
                    raise ValueError(f"Unparseable target_time: {value!r}")
                value = fmt_time(dt)
            elif key == "repeat_type":
                value = normalise_repeat(value)
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return
        values.append(int(schedule_id))
        with self._lock:
            self._sched().execute(
                f"UPDATE schedules SET {', '.join(sets)} WHERE id = ?", values
            )

    def get_schedule(self, schedule_id: int) -> Optional[Schedule]:
        row = self._sched().execute(
            "SELECT * FROM schedules WHERE id = ?", (int(schedule_id),)
        ).fetchone()
        return Schedule.from_row(row) if row else None

    def list_schedules(
        self,
        include_done: bool = True,
        limit: int = 500,
    ) -> list[Schedule]:
        """All schedules ordered by fire time (open items first when hiding done)."""
        sql = "SELECT * FROM schedules"
        if not include_done:
            sql += " WHERE is_done = 0"
        sql += " ORDER BY is_done ASC, target_time ASC, id ASC LIMIT ?"
        rows = self._sched().execute(sql, (int(limit),)).fetchall()
        return [Schedule.from_row(r) for r in rows]

    def today_schedules(self, now: Optional[datetime] = None) -> list[Schedule]:
        """Items firing today plus anything already overdue and still open."""
        now = now or datetime.now()
        end = datetime.combine(now.date(), datetime.max.time())
        rows = self._sched().execute(
            "SELECT * FROM schedules WHERE is_done = 0 AND target_time <= ? "
            "ORDER BY target_time ASC",
            (fmt_time(end),),
        ).fetchall()
        return [Schedule.from_row(r) for r in rows]

    def due_schedules(self, now: Optional[datetime] = None) -> list[Schedule]:
        """Open, un-notified rows whose ``target_time`` has arrived."""
        now = now or datetime.now()
        rows = self._sched().execute(
            "SELECT * FROM schedules "
            "WHERE notified = 0 AND is_done = 0 AND target_time <= ? "
            "ORDER BY target_time ASC",
            (fmt_time(now),),
        ).fetchall()
        return [Schedule.from_row(r) for r in rows]

    def mark_notified(self, schedule_id: int, notified: int = 1) -> None:
        self.update_schedule(schedule_id, notified=int(notified))

    def set_done(self, schedule_id: int, done: bool | int = True) -> None:
        """Raw completion flag. Completing also silences pending alarms.

        Prefer :meth:`complete_schedule` for user-driven check-offs -- it knows
        what to do with a recurring item.
        """
        done = 1 if done else 0
        fields = {"is_done": done}
        if done:
            fields["notified"] = 1
        self.update_schedule(schedule_id, **fields)

    def complete_schedule(
        self,
        schedule_id: int,
        done: bool = True,
        now: Optional[datetime] = None,
    ) -> tuple[str, Optional[datetime]]:
        """Tick a schedule off the way a to-do list should behave.

        A recurring chore is never "finished" -- completing this month's copy
        just means the next one is due. So:

        * ``repeat_type == 'none'`` -> ``is_done = 1`` (classic one-shot)
        * recurring                 -> ``target_time`` advances to the next
          cycle, ``notified`` and ``is_done`` reset to 0, and the row stays
          live in the list.

        Un-checking (``done=False``) always just clears the flag; it never
        rewinds a recurrence, because the previous slot has already passed.

        Returns ``(action, next_time)`` where action is one of
        ``'rolled' | 'done' | 'reopened'``.
        """
        now = now or datetime.now()
        schedule = self.get_schedule(schedule_id)
        if schedule is None:
            return "missing", None

        if not done:
            self.update_schedule(schedule_id, is_done=0)
            return "reopened", schedule.target_time

        if not schedule.is_recurring:
            self.set_done(schedule_id, True)
            return "done", None

        # Anchor at the later of "now" and the current slot. Ticking a chore off
        # early ("이번 달 것 미리 했다") must still advance a full cycle -- with a
        # plain `now` anchor the next trigger would resolve back to the slot the
        # user just completed and the row would never move.
        anchor = max(now, schedule.target_time)
        nxt = compute_next_trigger(
            schedule.target_time, schedule.repeat_type, schedule.repeat_detail, anchor)
        if nxt is None:                       # defensive: treat as one-shot
            self.set_done(schedule_id, True)
            return "done", None

        self.update_schedule(schedule_id, target_time=fmt_time(nxt), notified=0, is_done=0)
        log.info("Schedule #%d %r completed -> next cycle %s",
                 schedule_id, schedule.title, fmt_time(nxt))
        return "rolled", nxt

    def delete_schedule(self, schedule_id: int) -> None:
        with self._lock:
            self._sched().execute("DELETE FROM schedules WHERE id = ?", (int(schedule_id),))

    def clear_completed(self) -> int:
        """Delete every finished, non-recurring item. Returns rows removed."""
        with self._lock:
            cur = self._sched().execute(
                "DELETE FROM schedules WHERE is_done = 1 AND repeat_type = 'none'"
            )
            return cur.rowcount or 0

    # ------------------------------------------------------------------ #
    # schedules - recurrence
    # ------------------------------------------------------------------ #

    def roll_forward(
        self,
        schedule: Schedule,
        now: Optional[datetime] = None,
    ) -> Optional[datetime]:
        """Advance a fired schedule to its next occurrence.

        * one-shot  -> ``notified = 1`` (stays visible until the user ticks it)
        * recurring -> ``target_time`` moves to the next slot, ``notified = 0``

        Returns the new ``target_time``, or ``None`` for one-shot items.
        """
        now = now or datetime.now()
        if not schedule.is_recurring:
            self.mark_notified(schedule.id, 1)
            return None

        nxt = compute_next_trigger(
            schedule.target_time, schedule.repeat_type, schedule.repeat_detail, now
        )
        if nxt is None:                      # defensive: treat as one-shot
            self.mark_notified(schedule.id, 1)
            return None

        self.update_schedule(schedule.id, target_time=fmt_time(nxt), notified=0)
        log.info("Schedule #%d rolled forward to %s", schedule.id, fmt_time(nxt))
        return nxt

    # ------------------------------------------------------------------ #
    # awaited emails
    # ------------------------------------------------------------------ #

    def add_awaited_email(self, keywords: str, reminder_action: str,
                          sender_filter: str = "") -> int:
        """Register "when a mail matching X arrives, remind me to do Y".

        ``keywords`` is stored as the user typed it (comma/space separated);
        :func:`split_keywords` does the splitting at match time so an existing
        row never needs migrating when the parsing rules change.
        """
        keywords = (keywords or "").strip()
        reminder_action = (reminder_action or "").strip()
        if not keywords:
            raise ValueError("keywords must not be empty")
        if not reminder_action:
            raise ValueError("reminder_action must not be empty")
        with self._lock:
            cur = self._sched().execute(
                "INSERT INTO awaited_emails "
                "(keywords, sender_filter, reminder_action, is_triggered, is_active, created_at) "
                "VALUES (?,?,?,0,1,?)",
                (keywords, (sender_filter or "").strip(), reminder_action,
                 fmt_time(datetime.now())),
            )
            new_id = int(cur.lastrowid)
        log.info("Awaited-email rule #%d added: %r (sender=%r)",
                 new_id, keywords, sender_filter)
        return new_id

    def list_awaited_emails(self, include_triggered: bool = False) -> list[dict]:
        """All rules, newest first. Returns plain dicts for easy formatting."""
        sql = "SELECT * FROM awaited_emails"
        if not include_triggered:
            sql += " WHERE is_triggered = 0"
        sql += " ORDER BY is_active DESC, id DESC"
        rows = self._sched().execute(sql).fetchall()
        return [dict(row) for row in rows]

    def get_active_awaited_emails(self) -> list[dict]:
        """Rules the Outlook poller should currently watch for."""
        rows = self._sched().execute(
            "SELECT * FROM awaited_emails WHERE is_active = 1 AND is_triggered = 0 "
            "ORDER BY id ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_email_triggered(self, rule_id: int, triggered: bool = True) -> None:
        """Flip a rule to fired so it stops matching every poll."""
        with self._lock:
            self._sched().execute(
                "UPDATE awaited_emails SET is_triggered = ? WHERE id = ?",
                (1 if triggered else 0, int(rule_id)),
            )

    def set_awaited_active(self, rule_id: int, active: bool = True) -> None:
        with self._lock:
            self._sched().execute(
                "UPDATE awaited_emails SET is_active = ? WHERE id = ?",
                (1 if active else 0, int(rule_id)),
            )

    def delete_awaited_email(self, rule_id: int) -> None:
        with self._lock:
            self._sched().execute(
                "DELETE FROM awaited_emails WHERE id = ?", (int(rule_id),))

    def get_awaited_email(self, rule_id: int) -> Optional[dict]:
        row = self._sched().execute(
            "SELECT * FROM awaited_emails WHERE id = ?", (int(rule_id),)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # chat history
    # ------------------------------------------------------------------ #

    def add_message(self, sender: str, message: str) -> int:
        sender = "user" if str(sender).lower().startswith("u") else "ai"
        with self._lock:
            cur = self._chat().execute(
                "INSERT INTO chat_messages (sender, message, timestamp) VALUES (?,?,?)",
                (sender, message or "", fmt_time(datetime.now())),
            )
            return int(cur.lastrowid)

    def update_message(self, message_id: int, message: str) -> None:
        """Used to persist the final text of a streamed AI reply."""
        with self._lock:
            self._chat().execute(
                "UPDATE chat_messages SET message = ? WHERE id = ?",
                (message or "", int(message_id)),
            )

    def recent_messages(self, limit: int = 60) -> list[ChatMessage]:
        """Last ``limit`` messages in chronological order."""
        rows = self._chat().execute(
            "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [ChatMessage.from_row(r) for r in reversed(rows)]

    def clear_chat(self) -> int:
        """Purge the whole conversation. Returns the number of rows removed.

        The chat history is replayed into the model as context, so one bad
        answer keeps poisoning every later turn. This is the escape hatch.
        """
        with self._lock:
            cur = self._chat().execute("DELETE FROM chat_messages")
            removed = cur.rowcount or 0
            try:
                self._chat().execute("VACUUM")      # reclaim the space too
            except sqlite3.Error:
                pass
        log.info("Chat history cleared (%d messages)", removed)
        return removed

    def sanitize_chat(self, max_chars: int = 20000) -> int:
        """Drop rows that would poison the context. Returns rows removed.

        Runs at startup. Four kinds of junk accumulate:

        * empty/whitespace messages -- the placeholder row inserted before a
          stream that never produced tokens (crash, cancel, model failure);
        * rows with an unknown ``sender``, which would map to the wrong role;
        * absurdly long messages, usually a run-away generation, which would
          eat the entire context window on the next turn;
        * **hallucinated monologues** -- assistant turns that leaked raw ChatML
          markers or an untagged "Thinking Process:" ramble. Replaying those
          teaches the model that talking to itself is the expected format, so
          one bad answer breeds more.

        Only ``ai`` rows are eligible for the monologue check: whatever the
        user typed is theirs and is never second-guessed.
        """
        removed = 0
        with self._lock:
            conn = self._chat()
            try:
                cur = conn.execute(
                    "DELETE FROM chat_messages "
                    "WHERE message IS NULL OR TRIM(message) = '' "
                    "   OR sender NOT IN ('user','ai') "
                    "   OR LENGTH(message) > ?",
                    (int(max_chars),),
                )
                removed = cur.rowcount or 0
                removed += self._purge_monologues(conn)
                # Leftover control characters break the prompt encoding.
                conn.execute(
                    "UPDATE chat_messages SET message = REPLACE(message, char(0), '') "
                    "WHERE instr(message, char(0)) > 0"
                )
            except sqlite3.Error as exc:
                log.warning("Chat sanitise failed: %s", exc)
                return 0
        if removed:
            log.info("Sanitised chat history: removed %d malformed message(s)", removed)
        return removed

    #: Fragments that only ever appear in a broken assistant turn.
    _BROKEN_AI_MARKERS = (
        "<|im_start|>", "<|im_end|>", "<|endoftext|>",   # raw ChatML leaked
        "<think>", "</think>",                            # unstripped reasoning
    )
    #: Untagged reasoning preambles (English scaffolding the model wrote to
    #: itself). Matched only at the very start of an assistant turn.
    _MONOLOGUE_PREFIXES = (
        "thinking process", "thought process", "let me think", "okay, the user",
        "okay, so the user", "first, i need to", "the user is asking",
        "the user wants", "analysis:", "reasoning:", "step 1:",
    )

    def _purge_monologues(self, conn: sqlite3.Connection) -> int:
        """Delete assistant turns that are scratch work rather than answers."""
        rows = conn.execute(
            "SELECT id, message FROM chat_messages WHERE sender = 'ai'").fetchall()
        doomed: list[int] = []
        for row in rows:
            text = (row["message"] or "").strip()
            low = text.lower()
            if any(marker in text for marker in self._BROKEN_AI_MARKERS):
                doomed.append(row["id"])
            elif low.startswith(self._MONOLOGUE_PREFIXES):
                doomed.append(row["id"])
        for rule_id in doomed:
            conn.execute("DELETE FROM chat_messages WHERE id = ?", (rule_id,))
        if doomed:
            log.info("Removed %d hallucinated assistant turn(s)", len(doomed))
        return len(doomed)

    # ------------------------------------------------------------------ #
    # misc
    # ------------------------------------------------------------------ #

    def stats(self) -> dict[str, int]:
        """Small summary used by the HUD header."""
        row = self._sched().execute(
            "SELECT "
            " COUNT(*) AS total, "
            " SUM(CASE WHEN is_done = 1 THEN 1 ELSE 0 END) AS done, "
            " SUM(CASE WHEN is_done = 0 THEN 1 ELSE 0 END) AS open "
            "FROM schedules"
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "done": int(row["done"] or 0),
            "open": int(row["open"] or 0),
        }


# --------------------------------------------------------------------------- #
# Self-test: ``py db_manager.py``
# --------------------------------------------------------------------------- #

def _selftest() -> None:  # pragma: no cover - manual smoke test
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    tmp = tempfile.mkdtemp(prefix="hud_db_")
    db = DatabaseManager(tmp)

    now = datetime(2026, 8, 10, 9, 0, 0)      # a Monday

    # --- recurrence maths ------------------------------------------------ #
    weekly = compute_next_trigger(datetime(2026, 8, 10, 10, 0), "weekly", "월", now)
    assert weekly == datetime(2026, 8, 10, 10, 0), weekly
    weekly2 = compute_next_trigger(datetime(2026, 8, 10, 8, 0), "weekly", "월", now)
    assert weekly2 == datetime(2026, 8, 17, 8, 0), weekly2
    daily = compute_next_trigger(datetime(2026, 8, 1, 8, 30), "daily", "", now)
    assert daily == datetime(2026, 8, 10, 8, 30) + timedelta(days=1), daily
    # 31st -> February clamps to 28th, March returns to the 31st.
    m1 = compute_next_trigger(datetime(2027, 1, 31, 9, 0), "monthly", "31",
                              datetime(2027, 2, 1))
    assert m1 == datetime(2027, 2, 28, 9, 0), m1
    m2 = compute_next_trigger(m1, "monthly", "31", datetime(2027, 3, 1))
    assert m2 == datetime(2027, 3, 31, 9, 0), m2
    assert compute_next_trigger(now, "none") is None

    # --- multi-weekday recurrence ---------------------------------------- #
    assert parse_weekdays("화,목") == [1, 3]
    assert parse_weekdays("화요일, 목요일") == [1, 3]
    assert parse_weekdays("월수금") == [0, 2, 4]
    assert parse_weekdays("mon, thu") == [0, 3]
    assert parse_weekdays("화요일이랑 목요일") == [1, 3]
    assert parse_weekdays("") == [] and parse_weekdays(None) == []
    assert format_weekdays([3, 1, 1]) == "화,목"
    # Monday 09:00 "매주 화,목 07:00" -> Tuesday, then Thursday, then next Tuesday
    t1 = compute_next_trigger(datetime(2026, 8, 10, 7, 0), "weekly", "화,목", now)
    assert t1 == datetime(2026, 8, 11, 7, 0), t1
    t2 = compute_next_trigger(t1, "weekly", "화,목", datetime(2026, 8, 11, 7, 1))
    assert t2 == datetime(2026, 8, 13, 7, 0), t2
    t3 = compute_next_trigger(t2, "weekly", "화,목", datetime(2026, 8, 13, 7, 1))
    assert t3 == datetime(2026, 8, 18, 7, 0), t3

    # --- CRUD ------------------------------------------------------------ #
    sid = db.add_schedule("주간 회의", datetime(2026, 8, 10, 10, 0), "weekly", "")
    s = db.get_schedule(sid)
    assert s and s.repeat_detail == "월" and s.repeat_label == "매주 (월)", s
    assert len(db.due_schedules(datetime(2026, 8, 10, 10, 0, 1))) == 1
    db.roll_forward(s, datetime(2026, 8, 10, 10, 0, 1))
    assert db.get_schedule(sid).target_time == datetime(2026, 8, 17, 10, 0)

    one = db.add_schedule("치과 예약", "2026-08-11 15:30")
    db.roll_forward(db.get_schedule(one), datetime(2026, 8, 11, 15, 31))
    assert db.get_schedule(one).notified == 1
    db.set_done(one, True)
    assert db.get_schedule(one).is_done == 1
    assert db.stats()["open"] == 1

    # --- recurring to-do: completing advances instead of finishing --------- #
    monthly = db.add_schedule("특약OS이월", datetime(2026, 8, 12, 9, 0), "monthly", "12")
    action, nxt = db.complete_schedule(monthly, True, datetime(2026, 8, 12, 9, 5))
    assert action == "rolled", action
    assert nxt == datetime(2026, 9, 12, 9, 0), nxt
    row = db.get_schedule(monthly)
    assert row.is_done == 0 and row.notified == 0, row      # still a live to-do
    assert row.target_time == datetime(2026, 9, 12, 9, 0)
    # ...and again next cycle
    action, nxt = db.complete_schedule(monthly, True, datetime(2026, 9, 12, 9, 5))
    assert (action, nxt) == ("rolled", datetime(2026, 10, 12, 9, 0)), (action, nxt)

    weekly_todo = db.add_schedule("주간 보고", datetime(2026, 8, 10, 17, 0), "weekly", "월")
    action, nxt = db.complete_schedule(weekly_todo, True, datetime(2026, 8, 10, 17, 1))
    assert (action, nxt) == ("rolled", datetime(2026, 8, 17, 17, 0)), (action, nxt)

    # Completed EARLY (before the due time) -- must still advance a full cycle.
    early = db.add_schedule("사전 처리", datetime(2026, 8, 12, 9, 0), "monthly", "12")
    action, nxt = db.complete_schedule(early, True, datetime(2026, 8, 11, 15, 46))
    assert (action, nxt) == ("rolled", datetime(2026, 9, 12, 9, 0)), (action, nxt)
    early_weekly = db.add_schedule("사전 주간", datetime(2026, 8, 17, 9, 0), "weekly", "월")
    action, nxt = db.complete_schedule(early_weekly, True, datetime(2026, 8, 11, 10, 0))
    assert (action, nxt) == ("rolled", datetime(2026, 8, 24, 9, 0)), (action, nxt)
    # Long-overdue recurring item lands in the future, not on a stale slot.
    stale = db.add_schedule("밀린 일", datetime(2026, 1, 5, 9, 0), "daily")
    action, nxt = db.complete_schedule(stale, True, datetime(2026, 8, 11, 15, 46))
    assert action == "rolled" and nxt > datetime(2026, 8, 11, 15, 46), (action, nxt)

    # one-shot still behaves classically
    shot = db.add_schedule("서류 제출", datetime(2026, 8, 20, 10, 0))
    assert db.complete_schedule(shot, True) == ("done", None)
    assert db.get_schedule(shot).is_done == 1
    # un-checking never rewinds a recurrence
    action, _ = db.complete_schedule(shot, False)
    assert action == "reopened" and db.get_schedule(shot).is_done == 0
    assert db.complete_schedule(999999, True)[0] == "missing"

    # --- awaited emails --------------------------------------------------- #
    assert split_keywords("특약OS이월, 특약이월") == ["특약OS이월", "특약이월"]
    assert split_keywords("특약 이월") == ["특약 이월", "특약", "이월"]
    assert split_keywords("a, 결재") == ["결재"]          # 1-char noise dropped
    assert split_keywords("") == [] and split_keywords(None) == []

    rule = db.add_awaited_email("특약OS이월, 특약이월",
                                "1. 결재 시스템 승인\n2. 담당자 이메일 공유",
                                sender_filter="팀장")
    active = db.get_active_awaited_emails()
    assert len(active) == 1 and active[0]["id"] == rule
    assert active[0]["keywords"] == "특약OS이월, 특약이월"
    assert active[0]["sender_filter"] == "팀장"
    assert active[0]["is_active"] == 1 and active[0]["is_triggered"] == 0
    assert "결재 시스템 승인" in active[0]["reminder_action"]

    second = db.add_awaited_email("월마감", "마감 자료 취합")
    assert len(db.get_active_awaited_emails()) == 2
    assert len(db.list_awaited_emails()) == 2

    db.mark_email_triggered(rule)
    assert [r["id"] for r in db.get_active_awaited_emails()] == [second]
    assert len(db.list_awaited_emails(include_triggered=False)) == 1
    assert len(db.list_awaited_emails(include_triggered=True)) == 2

    db.set_awaited_active(second, False)
    assert db.get_active_awaited_emails() == []
    db.set_awaited_active(second, True)
    assert len(db.get_active_awaited_emails()) == 1

    assert db.get_awaited_email(rule)["is_triggered"] == 1
    db.delete_awaited_email(rule)
    assert db.get_awaited_email(rule) is None
    assert len(db.list_awaited_emails(include_triggered=True)) == 1

    for bad in ("", "   "):
        try:
            db.add_awaited_email(bad, "action")
            raise AssertionError("empty keywords must be rejected")
        except ValueError:
            pass
    try:
        db.add_awaited_email("키워드", "  ")
        raise AssertionError("empty action must be rejected")
    except ValueError:
        pass

    # --- chat ------------------------------------------------------------ #
    db.add_message("user", "안녕")
    mid = db.add_message("ai", "")
    db.update_message(mid, "안녕하세요!")
    msgs = db.recent_messages()
    assert [m.sender for m in msgs] == ["user", "ai"]
    assert msgs[-1].message == "안녕하세요!"

    # --- chat sanitising / purge ------------------------------------------ #
    db.add_message("ai", "")                     # abandoned stream placeholder
    db.add_message("user", "   ")                # whitespace only
    db.add_message("ai", "x" * 30000)            # run-away generation
    with db._lock:                               # a row with a bogus sender
        db._chat().execute(
            "INSERT INTO chat_messages (sender, message, timestamp) VALUES (?,?,?)",
            ("system", "poisoned", fmt_time(datetime.now())))
    # hallucinated assistant turns
    db.add_message("ai", "Thinking Process:\n1. Analyze the request\n2. Draft")
    db.add_message("ai", "답변입니다<|im_end|>\n<|im_start|>user\n또 뭐야")
    db.add_message("ai", "<think>몰래 생각</think>")
    db.add_message("user", "Thinking Process 라는 게 뭐야?")   # user text is sacred
    assert len(db.recent_messages(50)) == 10
    removed = db.sanitize_chat()
    assert removed == 7, removed
    survivors = [m.message for m in db.recent_messages(50)]
    assert survivors == ["안녕", "안녕하세요!", "Thinking Process 라는 게 뭐야?"], survivors
    assert db.sanitize_chat() == 0                # idempotent
    assert db.clear_chat() == 3
    assert db.recent_messages() == []

    db.close()
    print(f"db_manager self-test OK  ({tmp})")


if __name__ == "__main__":  # pragma: no cover
    _selftest()
