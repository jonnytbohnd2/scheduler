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
import shutil
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

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
    nag_at: Optional[datetime] = None
    nag_count: int = 0
    #: The occurrence that was missed (recurring rows have moved on since).
    nag_origin: Optional[datetime] = None
    #: The occurrence whose alarm most recently fired, unacknowledged so far.
    last_fired: Optional[datetime] = None

    @property
    def missed_time(self) -> datetime:
        """When the ignored alarm was actually due."""
        return self.nag_origin or self.target_time

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
            nag_at=parse_time(row["nag_at"]) if "nag_at" in row.keys() else None,
            nag_count=int((row["nag_count"] if "nag_count" in row.keys() else 0) or 0),
            nag_origin=(parse_time(row["nag_origin"])
                        if "nag_origin" in row.keys() else None),
            last_fired=(parse_time(row["last_fired"])
                        if "last_fired" in row.keys() else None),
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
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    -- Re-reminder for an alarm the user never reacted to. nag_at is when to
    -- nudge again; NULL means nothing pending (acknowledged, or nagged out).
    nag_at        TEXT,
    nag_count     INTEGER NOT NULL DEFAULT 0,
    -- The occurrence whose alarm most recently fired. Firing already advances
    -- a recurring row, so ticking that alarm off must not advance it again.
    last_fired    TEXT,
    -- The occurrence that was actually missed. A recurring row has already
    -- rolled forward by nag time, so without this the nudge would show the
    -- *next* due date instead of the one the user walked past.
    nag_origin    TEXT
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

-- Work log. A recurring chore rolls to its next cycle when you tick it off,
-- so the schedules table keeps no trace that this month's 특약OS이월 was
-- actually done -- and that is precisely what a weekly report is made of.
-- Rows are kept even if the schedule is later deleted: the work still happened.
CREATE TABLE IF NOT EXISTS completions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id  INTEGER,
    title        TEXT    NOT NULL,
    completed_at TEXT    NOT NULL,            -- when the box was ticked
    due_at       TEXT,                        -- the slot it was due for
    repeat_type  TEXT    NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_completions_when
    ON completions (completed_at);
"""

def _columns_of(schema: str, table: str) -> list[tuple[str, str]]:
    """(name, declaration) for every column of `table` that ALTER can add.

    Feeds `_migrate`, so the declarations are normalised to what SQLite will
    accept in `ADD COLUMN`: no PRIMARY KEY, and no computed DEFAULT -- for an
    existing row "when it was created" is unknowable anyway, so those become a
    constant and the row reads as empty rather than as today.
    """
    body = re.search(
        rf"CREATE TABLE (?:IF NOT EXISTS )?{table}\s*\((.*?)\n\);",
        schema, re.S)
    if not body:
        return []
    out: list[tuple[str, str]] = []
    for line in body.group(1).splitlines():
        line = re.sub(r"--.*$", "", line).strip().rstrip(",").strip()
        if not line or line.upper().startswith(("PRIMARY KEY", "UNIQUE",
                                                "FOREIGN KEY", "CHECK")):
            continue
        name, _, decl = line.partition(" ")
        decl = decl.strip()
        if not decl or "PRIMARY KEY" in decl.upper():
            continue                                   # id column
        # Greedy to the last ")": the default may itself contain parentheses,
        # e.g. DEFAULT (datetime('now','localtime')).
        decl = re.sub(r"DEFAULT\s*\(.*\)\s*$",
                      "DEFAULT 0" if "INTEGER" in decl.upper() else "DEFAULT ''",
                      decl, flags=re.I)
        if "NOT NULL" in decl.upper() and "DEFAULT" not in decl.upper():
            decl += " DEFAULT 0" if "INTEGER" in decl.upper() else " DEFAULT ''"
        out.append((name, decl))
    return out


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
        """Add columns that older installs are missing (idempotent).

        `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it is,
        so a DB from an older build keeps its old column set while the code
        reads the new one -- and a missing column is a hard crash on open, i.e.
        the user's data looks lost. The wanted list is derived from the schema
        above rather than hand-maintained, so adding a column there is enough.
        """
        for table, ddl in (("schedules", _SCHEDULE_SCHEMA),
                           ("awaited_emails", _SCHEDULE_SCHEMA)):
            have = {r["name"] for r in
                    self._sched().execute(f"PRAGMA table_info({table})")}
            if not have:                               # brand-new DB, nothing to do
                continue
            for name, decl in _columns_of(ddl, table):
                if name in have:
                    continue
                try:
                    self._sched().execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    log.info("Migrated %s: added %s", table, name)
                except sqlite3.Error as exc:
                    log.warning("Migration failed for %s.%s: %s", table, name, exc)

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
            "repeat_detail", "notified", "is_done", "last_fired",
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

    # ---------------- missed-alarm re-reminders ---------------- #

    def arm_nag(self, schedule_id: int, when: datetime, count: int = 0,
                origin: Optional[datetime] = None) -> None:
        """Schedule a nudge for an alarm the user has not reacted to yet.

        ``origin`` is the occurrence that was missed; pass it on the first arm
        so a recurring row (already rolled forward) still reports the right
        time. Later re-arms keep whatever origin is already stored.
        """
        with self._lock:
            if origin is not None:
                self._sched().execute(
                    "UPDATE schedules SET nag_at = ?, nag_count = ?, nag_origin = ? "
                    "WHERE id = ?",
                    (fmt_time(when), int(count), fmt_time(origin), int(schedule_id)),
                )
            else:
                self._sched().execute(
                    "UPDATE schedules SET nag_at = ?, nag_count = ? WHERE id = ?",
                    (fmt_time(when), int(count), int(schedule_id)),
                )

    def clear_nag(self, schedule_id: int) -> None:
        """The user acted (완료 / 미루기 / 카드 닫기) -- stop nudging."""
        with self._lock:
            self._sched().execute(
                "UPDATE schedules SET nag_at = NULL, nag_count = 0, nag_origin = NULL "
                "WHERE id = ?",
                (int(schedule_id),),
            )

    def due_nags(self, now: Optional[datetime] = None) -> list[Schedule]:
        """Open items whose re-reminder time has arrived."""
        now = now or datetime.now()
        rows = self._sched().execute(
            "SELECT * FROM schedules "
            "WHERE is_done = 0 AND nag_at IS NOT NULL AND nag_at <= ? "
            "ORDER BY nag_at ASC",
            (fmt_time(now),),
        ).fetchall()
        return [Schedule.from_row(r) for r in rows]

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
            self._unlog_completion(schedule_id)
            return "reopened", schedule.target_time

        if not schedule.is_recurring:
            self.set_done(schedule_id, True)
            self._log_completion(schedule, now)
            return "done", None

        # The alarm for this cycle already fired, which advanced the row. The
        # user is ticking off *that* occurrence, so the row is already where it
        # should be -- advancing again would silently eat a whole cycle.
        # (Reported: 8/12 alarm fired -> row moved to 9/12 -> pressing 완료 on
        #  the card jumped it to 10/12 and September was never scheduled.)
        fired = schedule.last_fired
        if fired is not None and fired < schedule.target_time and now < schedule.target_time:
            self.update_schedule(schedule_id, last_fired=None, notified=0)
            self.clear_nag(schedule_id)
            self._log_completion(schedule, now, due_at=fired)
            log.info("Schedule #%d %r: acknowledged the %s alarm; next stays %s",
                     schedule_id, schedule.title, fmt_time(fired),
                     fmt_time(schedule.target_time))
            return "acknowledged", schedule.target_time

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

        self.update_schedule(schedule_id, target_time=fmt_time(nxt), notified=0,
                             is_done=0, last_fired=None)
        self._log_completion(schedule, now)
        log.info("Schedule #%d %r completed -> next cycle %s",
                 schedule_id, schedule.title, fmt_time(nxt))
        return "rolled", nxt

    # ------------------------------------------------------------------ #
    # work log
    # ------------------------------------------------------------------ #
    def _log_completion(self, schedule: Schedule, when: datetime,
                        due_at: Optional[datetime] = None) -> None:
        """Record that a piece of work actually got done.

        `due_at` overrides the row's current slot: after an alarm has rolled a
        recurring row on, the occurrence that was completed is the one that
        fired, not the one the row now points at.
        """
        try:
            with self._lock:
                self._sched().execute(
                    "INSERT INTO completions "
                    "(schedule_id, title, completed_at, due_at, repeat_type) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (int(schedule.id), schedule.title, fmt_time(when),
                     fmt_time(due_at or schedule.target_time),
                     schedule.repeat_type))
        except sqlite3.Error:
            # The tick itself already succeeded; losing a log row must not
            # turn into a failed completion.
            log.exception("Could not log completion of #%s", schedule.id)

    def _unlog_completion(self, schedule_id: int) -> None:
        """Un-ticking a box takes the newest log row back out."""
        try:
            with self._lock:
                self._sched().execute(
                    "DELETE FROM completions WHERE id = "
                    "(SELECT id FROM completions WHERE schedule_id = ? "
                    " ORDER BY completed_at DESC, id DESC LIMIT 1)",
                    (int(schedule_id),))
        except sqlite3.Error:
            log.exception("Could not un-log completion of #%s", schedule_id)

    def completions_between(self, start: datetime,
                            end: datetime) -> list[dict[str, Any]]:
        """Everything ticked off in a window, oldest first."""
        with self._lock:
            rows = self._sched().execute(
                "SELECT title, completed_at, due_at, repeat_type FROM completions "
                "WHERE completed_at >= ? AND completed_at < ? "
                "ORDER BY completed_at ASC, id ASC",
                (fmt_time(start), fmt_time(end))).fetchall()
        out = []
        for r in rows:
            out.append({
                "title": r["title"],
                "completed_at": parse_time(r["completed_at"]),
                "due_at": parse_time(r["due_at"]) if r["due_at"] else None,
                "repeat_type": r["repeat_type"] or REPEAT_NONE,
            })
        return out

    def work_report(self, start: datetime, end: datetime,
                    include_open: bool = True) -> str:
        """The 주간보고 paragraph, ready to paste.

        Built purely from the database -- no model involved, so nothing in it
        can be invented. Anything not actually ticked off simply is not here.
        """
        done = self.completions_between(start, end)
        lines = [f"[{start.strftime('%m/%d')} ~ "
                 f"{(end - timedelta(days=1)).strftime('%m/%d')}] 완료한 업무"]
        if done:
            seen: set[str] = set()
            for row in done:
                when = row["completed_at"]
                stamp = f"{when.strftime('%m/%d')}({WEEKDAY_NAMES_KO[when.weekday()]})"
                mark = " ↻" if row["repeat_type"] != REPEAT_NONE else ""
                key = f"{row['title']}|{stamp}"
                if key in seen:                 # same chore twice in a day
                    continue
                seen.add(key)
                lines.append(f"- {row['title']}{mark}  ({stamp})")
        else:
            lines.append("- (완료 처리한 항목이 없습니다)")

        if include_open:
            now = datetime.now()
            pending = [s for s in self.list_schedules(include_done=False)
                       if s.target_time < end + timedelta(days=7)]
            pending.sort(key=lambda s: s.target_time)
            if pending:
                lines.append("")
                lines.append("[예정 · 진행 중]")
                for s in pending[:12]:
                    stamp = (f"{s.target_time.strftime('%m/%d')}"
                             f"({WEEKDAY_NAMES_KO[s.target_time.weekday()]})")
                    late = " ⚠지남" if s.target_time < now else ""
                    lines.append(f"- {s.title}  ({stamp}){late}")
        return "\n".join(lines)

    def delete_schedule(self, schedule_id: int) -> None:
        with self._lock:
            self._sched().execute("DELETE FROM schedules WHERE id = ?", (int(schedule_id),))

    def list_backups(self, directory: Optional[str] = None) -> list[dict[str, Any]]:
        """Available snapshots, newest first, with row counts.

        The counts matter: after the incident that prompted this, the useful
        question was not "which backups exist" but "which one still has my
        일정 in it". A folder listing cannot answer that; opening each snapshot
        read-only can.
        """
        root = directory or os.path.join(self.base_dir, "backups")
        if not os.path.isdir(root):
            return []
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(root), reverse=True):
            folder = os.path.join(root, name)
            snapshot = os.path.join(folder, "schedules.db")
            if not os.path.isfile(snapshot):
                continue
            entry = {"name": name, "path": folder, "schedules": None,
                     "messages": None,
                     "size": os.path.getsize(snapshot),
                     "when": datetime.fromtimestamp(os.path.getmtime(snapshot))}
            try:
                conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
                try:
                    entry["schedules"] = conn.execute(
                        "SELECT COUNT(*) FROM schedules").fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error:
                pass
            chat = os.path.join(folder, "chat_history.db")
            if os.path.isfile(chat):
                try:
                    conn = sqlite3.connect(f"file:{chat}?mode=ro", uri=True)
                    try:
                        entry["messages"] = conn.execute(
                            "SELECT COUNT(*) FROM chat_messages").fetchone()[0]
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass
            out.append(entry)
        return out

    def restore_from(self, folder: str, include_chat: bool = True) -> list[str]:
        """Replace the live databases with a snapshot. Returns what was restored.

        The current files are copied aside first, into ``backups/replaced-<ts>``:
        a restore is itself destructive, and the thing being overwritten might
        turn out to have been the good copy.

        Callers must close every other connection first -- see
        :meth:`DatabaseManager.close`.
        """
        names = ["schedules.db"] + (["chat_history.db"] if include_chat else [])
        available = [n for n in names if os.path.isfile(os.path.join(folder, n))]
        if not available:
            raise FileNotFoundError(f"백업에 DB 파일이 없습니다: {folder}")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        aside = os.path.join(self.base_dir, "backups", f"replaced-{stamp}")
        os.makedirs(aside, exist_ok=True)

        self.close()
        restored: list[str] = []
        for name in available:
            live = os.path.join(self.base_dir, name)
            if os.path.isfile(live):
                shutil.copy2(live, os.path.join(aside, name))
            # A stale -wal/-shm belongs to the file being replaced. SQLite
            # copes, but leaving them is asking for a confusing recovery.
            for suffix in ("-wal", "-shm"):
                stale = live + suffix
                if os.path.isfile(stale):
                    try:
                        os.replace(stale, os.path.join(aside, name + suffix))
                    except OSError:
                        pass
            shutil.copy2(os.path.join(folder, name), live)
            restored.append(name)
        log.info("Restored %s from %s (previous copies in %s)",
                 ", ".join(restored), folder, aside)
        return restored

    def sweep_completed(self, older_than_hours: int = 24,
                        now: Optional[datetime] = None) -> int:
        """Retire one-shot items finished more than `older_than_hours` ago.

        A ticked-off item is worth seeing for the rest of the day -- it is the
        proof you did it -- but a list that only grows stops being glanceable.
        The completion log keeps the record either way, so the weekly report is
        unaffected by this.
        """
        now = now or datetime.now()
        cutoff = now - timedelta(hours=max(1, int(older_than_hours)))
        with self._lock:
            rows = self._sched().execute(
                "SELECT id, title FROM schedules "
                "WHERE is_done = 1 AND repeat_type = 'none'").fetchall()
        stale: list[int] = []
        for row in rows:
            done_at = self._completed_at(int(row["id"]))
            if done_at is not None and done_at < cutoff:
                stale.append(int(row["id"]))
        if not stale:
            return 0
        with self._lock:
            self._sched().execute(
                f"DELETE FROM schedules WHERE id IN "
                f"({','.join('?' * len(stale))})", stale)
        log.info("Swept %d completed item(s) older than %dh", len(stale),
                 older_than_hours)
        return len(stale)

    def _completed_at(self, schedule_id: int) -> Optional[datetime]:
        """When this row was last ticked off, from the work log."""
        with self._lock:
            row = self._sched().execute(
                "SELECT completed_at FROM completions WHERE schedule_id = ? "
                "ORDER BY completed_at DESC, id DESC LIMIT 1",
                (int(schedule_id),)).fetchone()
        return parse_time(row["completed_at"]) if row else None

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

        # Remember which occurrence this was. Completing an alarm you just
        # received must not advance the row a second time -- the firing has
        # already moved it on, and a second hop silently eats a whole cycle.
        self.update_schedule(schedule.id, target_time=fmt_time(nxt), notified=0,
                             last_fired=fmt_time(schedule.target_time))
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
    # backup
    # ------------------------------------------------------------------ #

    def backup_to(self, directory: str, keep: int = 10) -> Optional[str]:
        """Snapshot both databases into ``directory/<date>/``.

        Uses SQLite's online backup API rather than copying the file: in WAL
        mode the ``.db`` on disk can lag behind the ``-wal`` sidecar, so a
        plain copy is not guaranteed to be consistent.

        One snapshot per calendar day (re-running the same day overwrites it),
        pruned to the newest ``keep`` days. Returns the folder written, or
        ``None`` if it could not be written -- a failed backup must never stop
        the app from starting.
        """
        try:
            stamp = datetime.now().strftime("%Y%m%d")
            target = os.path.join(directory, stamp)
            os.makedirs(target, exist_ok=True)

            for name, conn in (("schedules.db", self._sched()),
                               ("chat_history.db", self._chat())):
                path = os.path.join(target, name)
                with self._lock:
                    dest = sqlite3.connect(path)
                    try:
                        conn.backup(dest)
                    finally:
                        dest.close()

            self._prune_backups(directory, keep)
            log.info("Database backup written to %s", target)
            return target
        except Exception as exc:                       # noqa: BLE001
            log.warning("Database backup failed: %s", exc)
            return None

    @staticmethod
    def _prune_backups(directory: str, keep: int) -> None:
        try:
            days = sorted(
                name for name in os.listdir(directory)
                if len(name) == 8 and name.isdigit()
                and os.path.isdir(os.path.join(directory, name))
            )
            for stale in days[:-max(1, keep)]:
                for root, _dirs, files in os.walk(os.path.join(directory, stale),
                                                  topdown=False):
                    for file in files:
                        os.remove(os.path.join(root, file))
                    os.rmdir(root)
        except OSError as exc:
            log.debug("Backup prune skipped: %s", exc)

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

    # --- missed-alarm nagging ----------------------------------------------- #
    nagged = db.add_schedule("놓칠 일정", datetime(2026, 8, 12, 9, 0))
    assert db.due_nags(datetime(2026, 8, 12, 9, 5)) == []      # nothing armed yet
    db.arm_nag(nagged, datetime(2026, 8, 12, 9, 10), 0)
    assert db.due_nags(datetime(2026, 8, 12, 9, 9)) == []      # not yet
    pending = db.due_nags(datetime(2026, 8, 12, 9, 11))
    assert [s.id for s in pending] == [nagged], pending
    assert pending[0].nag_count == 0 and pending[0].nag_at is not None
    db.arm_nag(nagged, datetime(2026, 8, 12, 9, 20), 1)
    assert db.due_nags(datetime(2026, 8, 12, 9, 21))[0].nag_count == 1
    db.clear_nag(nagged)
    assert db.due_nags(datetime(2026, 8, 12, 23, 0)) == [], "cleared nag still fires"
    # a completed item never nags
    db.arm_nag(nagged, datetime(2026, 8, 12, 9, 10), 0)
    db.set_done(nagged, True)
    assert db.due_nags(datetime(2026, 8, 12, 23, 0)) == [], "done item nagged"
    db.delete_schedule(nagged)

    # --- backup ------------------------------------------------------------ #
    db.add_schedule("백업 대상", datetime(2026, 9, 1, 9, 0))
    backup_root = os.path.join(tmp, "backups")
    written = db.backup_to(backup_root, keep=3)
    assert written and os.path.isdir(written), written
    for name in ("schedules.db", "chat_history.db"):
        assert os.path.isfile(os.path.join(written, name)), name
    # the snapshot must be a real, readable database with our row in it
    snap = DatabaseManager(written)
    assert any(s.title == "백업 대상" for s in snap.list_schedules()), "backup empty"
    snap.close()
    # pruning keeps the newest N day-folders
    for day in ("20250101", "20250102", "20250103", "20250104"):
        os.makedirs(os.path.join(backup_root, day), exist_ok=True)
    db._prune_backups(backup_root, keep=3)
    remaining = sorted(d for d in os.listdir(backup_root) if d.isdigit())
    assert len(remaining) == 3, remaining
    # a bad path is reported, not raised
    assert db.backup_to("\0invalid") is None

    # --- work log / weekly report ------------------------------------------- #
    mon = datetime(2026, 8, 17, 9, 0)          # a Monday
    one_off = db.add_schedule("경영전략 엑셀 제출", datetime(2026, 8, 20, 9, 0))
    chore = db.add_schedule("월간 요율표 갱신", datetime(2026, 8, 19, 9, 0), "monthly")
    db.complete_schedule(one_off, now=datetime(2026, 8, 20, 14, 30))
    db.complete_schedule(chore, now=datetime(2026, 8, 19, 11, 0))

    logged = db.completions_between(mon, mon + timedelta(days=5))
    titles = [r["title"] for r in logged]
    assert titles == ["월간 요율표 갱신", "경영전략 엑셀 제출"], titles
    # The recurring row has already rolled forward, yet the work is on record.
    assert db.get_schedule(chore).target_time > datetime(2026, 8, 19, 9, 0)
    assert not db.get_schedule(chore).is_done, "recurring chore should stay live"

    report = db.work_report(mon, mon + timedelta(days=5))
    assert "월간 요율표 갱신 ↻" in report, report
    assert "경영전략 엑셀 제출" in report, report
    assert "08/20(목)" in report, report
    # Nothing outside the window leaks in. (Earlier cases in this self-test
    # completed their own items, so check by title rather than emptiness.)
    earlier = [r["title"] for r in db.completions_between(mon - timedelta(days=7), mon)]
    assert "월간 요율표 갱신" not in earlier and "경영전략 엑셀 제출" not in earlier, earlier

    # Un-ticking takes the entry back out again.
    db.complete_schedule(one_off, done=False, now=datetime(2026, 8, 21, 9, 0))
    assert [r["title"] for r in
            db.completions_between(mon, mon + timedelta(days=5))] == ["월간 요율표 갱신"]
    db.delete_schedule(one_off)
    db.delete_schedule(chore)

    # --- an alarm you tick off must not eat a whole cycle -------------------- #
    # Reported from real use: 매월 12일 특약OS이월. The 8/12 alarm fired, which
    # rolled the row to 9/12; pressing 완료 on that card rolled it again to
    # 10/12 and September was never scheduled.
    ack_now = datetime(2026, 8, 21, 10, 0)
    fired = db.add_schedule("특약OS이월", datetime(2026, 8, 12, 9, 0), "monthly", "12")
    db.roll_forward(db.get_schedule(fired), ack_now)
    assert db.get_schedule(fired).target_time == datetime(2026, 9, 12, 9, 0)
    action, nxt = db.complete_schedule(fired, now=ack_now)
    assert action == "acknowledged", action
    assert nxt == datetime(2026, 9, 12, 9, 0), nxt
    assert db.get_schedule(fired).target_time == datetime(2026, 9, 12, 9, 0), "9월을 건너뜀"
    logged = db.completions_between(datetime(2026, 8, 1), datetime(2026, 9, 1))
    assert any(r["due_at"] == datetime(2026, 8, 12, 9, 0) for r in logged), logged
    # A second tick is a genuine early completion and does advance.
    assert db.complete_schedule(fired, now=ack_now)[1] == datetime(2026, 10, 12, 9, 0)
    # ...and completing without an alarm behaves as before.
    plain = db.add_schedule("월간 정산", datetime(2026, 8, 12, 9, 0), "monthly", "12")
    assert db.complete_schedule(plain, now=ack_now)[1] == datetime(2026, 9, 12, 9, 0)
    early = db.add_schedule("미리 처리", datetime(2026, 9, 12, 9, 0), "monthly", "12")
    assert db.complete_schedule(early, now=ack_now)[1] == datetime(2026, 10, 12, 9, 0)
    for sid in (fired, plain, early):
        db.delete_schedule(sid)

    # --- restoring from a backup --------------------------------------------- #
    # Prompted by a real recovery: the live schedules.db had been replaced by
    # an empty one and the only good copy was a snapshot in backups/. Hand
    # copying database files is not something a user should have to do.
    db.add_schedule("복원 대상", datetime(2026, 8, 20, 9, 0))
    snap = db.backup_to(os.path.join(tmp, "backups"), keep=10)
    listing = db.list_backups(os.path.join(tmp, "backups"))
    assert listing, "백업 목록이 비어 있음"
    newest = listing[0]
    assert newest["schedules"] and newest["schedules"] > 0, newest
    assert newest["messages"] is not None, newest

    db.delete_schedule([s for s in db.list_schedules(include_done=True)
                        if s.title == "복원 대상"][0].id)
    assert not any(s.title == "복원 대상" for s in db.list_schedules(include_done=True))
    restored_names = db.restore_from(snap)
    assert "schedules.db" in restored_names, restored_names
    reopened = DatabaseManager(tmp)
    assert any(s.title == "복원 대상"
               for s in reopened.list_schedules(include_done=True)), "복원 실패"
    # the copy that was overwritten is kept, in case the restore was a mistake
    replaced = [d for d in os.listdir(os.path.join(tmp, "backups"))
                if d.startswith("replaced-")]
    assert replaced, "덮어쓴 원본을 보관하지 않음"
    reopened.close()
    db = DatabaseManager(tmp)                    # our own handle was closed

    # --- completed one-shots retire after a day ------------------------------ #
    sweep_now = datetime(2026, 8, 21, 18, 0)
    old = db.add_schedule("어제 끝낸 일", datetime(2026, 8, 19, 9, 0))
    db.complete_schedule(old, now=datetime(2026, 8, 20, 9, 0))     # 33h ago
    recent = db.add_schedule("아까 끝낸 일", datetime(2026, 8, 21, 9, 0))
    db.complete_schedule(recent, now=datetime(2026, 8, 21, 15, 0))  # 3h ago
    keep = db.add_schedule("아직 안 한 일", datetime(2026, 8, 22, 9, 0))
    chore = db.add_schedule("반복 업무", datetime(2026, 8, 21, 9, 0), "monthly", "21")
    db.complete_schedule(chore, now=sweep_now)
    assert db.sweep_completed(24, sweep_now) == 1, "하루 지난 완료 항목만 정리해야 함"
    remaining = {s.title for s in db.list_schedules(include_done=True)}
    assert "어제 끝낸 일" not in remaining, remaining
    assert {"아까 끝낸 일", "아직 안 한 일", "반복 업무"} <= remaining, remaining
    # the work log survives the sweep -- the weekly report must not lose it
    swept_log = [r["title"] for r in
                 db.completions_between(datetime(2026, 8, 19), datetime(2026, 8, 22))]
    assert "어제 끝낸 일" in swept_log, swept_log
    for sid in (recent, keep, chore):
        db.delete_schedule(sid)

    # --- opening a database from an older build ----------------------------- #
    # A missing column is a crash on open, which to the user looks like their
    # data is gone. Simulate the oldest schema we ever shipped.
    legacy_dir = os.path.join(tmp, "legacy")
    os.makedirs(legacy_dir, exist_ok=True)
    legacy = sqlite3.connect(os.path.join(legacy_dir, "schedules.db"))
    legacy.executescript(
        "CREATE TABLE schedules ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  title TEXT NOT NULL,"
        "  target_time TEXT NOT NULL,"
        "  repeat_type TEXT NOT NULL DEFAULT 'none',"
        "  notified INTEGER NOT NULL DEFAULT 0,"
        "  is_done INTEGER NOT NULL DEFAULT 0);"
        "INSERT INTO schedules (title, target_time, repeat_type)"
        "  VALUES ('묵은 일정', '2026-08-12 09:00:00', 'monthly');")
    legacy.commit()
    legacy.close()
    old_db = DatabaseManager(legacy_dir)
    kept = old_db.list_schedules(include_done=True)
    assert len(kept) == 1 and kept[0].title == "묵은 일정", kept
    assert kept[0].repeat_type == "monthly", "repeat lost in migration"
    assert kept[0].nag_at is None and kept[0].nag_count == 0, kept[0]
    old_db.arm_nag(kept[0].id, kept[0].target_time, 0, origin=kept[0].target_time)
    assert old_db.due_nags(kept[0].target_time), "nag broken on a migrated row"
    old_db.close()

    db.close()
    print(f"db_manager self-test OK  ({tmp})")


if __name__ == "__main__":  # pragma: no cover
    _selftest()
