"""
scheduler_service.py
====================
Background alarm engine.

An APScheduler ``BackgroundScheduler`` polls ``schedules.db`` every few seconds
looking for rows where ``target_time <= now AND notified = 0 AND is_done = 0``.
For each hit it:

1. emits :attr:`SchedulerService.schedule_due` (Qt signal -> queued onto the GUI
   thread, so slots may safely touch widgets, play sounds and animate);
2. advances the row -- one-shot items get ``notified = 1``, recurring items get
   a freshly computed ``target_time`` and stay ``notified = 0``;
3. emits :attr:`SchedulerService.schedules_changed` so the list view refreshes.

The poll job never touches Qt widgets itself and only uses this thread's own
SQLite connection (see ``db_manager``'s thread-local pooling), which keeps the
whole thing lock-free from the GUI's point of view.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QObject, Signal

from db_manager import DatabaseManager, Schedule, fmt_time

log = logging.getLogger(__name__)


class QuietHours:
    """When the user is not at their desk, so an alarm would just be noise.

    Holding rather than dropping: the schedule row is left completely alone,
    so an alarm that came due at 22:40 simply fires at 09:00 -- and still
    fires if the PC was switched off in between.
    """

    def __init__(self, start: str = "09:00", end: str = "18:00",
                 lunch_start: str = "12:00", lunch_end: str = "13:00",
                 skip_lunch: bool = False, skip_holidays: bool = True,
                 enabled: bool = False,
                 calendar: Optional[object] = None) -> None:
        self.enabled = bool(enabled)
        self.start = _parse_hhmm(start, 9, 0)
        self.end = _parse_hhmm(end, 18, 0)
        self.lunch_start = _parse_hhmm(lunch_start, 12, 0)
        self.lunch_end = _parse_hhmm(lunch_end, 13, 0)
        self.skip_lunch = bool(skip_lunch)
        self.skip_holidays = bool(skip_holidays)
        self._calendar = calendar

    # -- helpers --------------------------------------------------------- #
    def _is_workday(self, moment: datetime) -> bool:
        if not self.skip_holidays:
            return True
        cal = self._calendar
        if cal is None:
            from holidays import calendar as _cal
            cal = _cal()
        return bool(cal.is_business_day(moment.date()))

    def _within_workhours(self, moment: datetime) -> bool:
        minutes = moment.hour * 60 + moment.minute
        start, end = self.start, self.end
        if start <= end:
            inside = start <= minutes < end
        else:                                   # a shift that crosses midnight
            inside = minutes >= start or minutes < end
        if inside and self.skip_lunch:
            if self.lunch_start <= minutes < self.lunch_end:
                return False
        return inside

    # -- API ------------------------------------------------------------- #
    def is_quiet(self, moment: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        moment = moment or datetime.now()
        return not (self._is_workday(moment) and self._within_workhours(moment))

    def next_active(self, moment: Optional[datetime] = None) -> datetime:
        """First moment from now that is not quiet (probe by the quarter hour)."""
        moment = (moment or datetime.now()).replace(second=0, microsecond=0)
        if not self.is_quiet(moment):
            return moment
        probe = moment
        for _ in range(4 * 24 * 40):            # up to ~40 days of holidays
            probe += timedelta(minutes=15)
            if not self.is_quiet(probe):
                return probe.replace(minute=(probe.minute // 15) * 15)
        return moment


def _parse_hhmm(text: str, default_h: int, default_m: int) -> int:
    """'09:30' -> minutes past midnight. Bad input falls back, never raises."""
    try:
        hours, _, mins = str(text).partition(":")
        h, m = int(hours), int(mins or 0)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (TypeError, ValueError):
        pass
    return default_h * 60 + default_m


class SchedulerService(QObject):
    """Qt-friendly wrapper around an APScheduler polling job."""

    #: A schedule has just come due. Payload: the :class:`Schedule` **as it was
    #: when it fired** (before any recurrence roll-forward).
    schedule_due = Signal(object)
    #: A previously fired alarm the user never reacted to. (schedule, nth time)
    schedule_missed = Signal(object, int)
    #: Emitted whenever rows were modified by the service (refresh your views).
    schedules_changed = Signal()
    #: Heartbeat, once per poll -- handy for "next alarm in ..." headers.
    tick = Signal()
    #: Non-fatal problem worth surfacing in the status bar.
    error = Signal(str)

    #: Never fire more than this many alarms in a single poll; a laptop waking
    #: from a week of sleep should not machine-gun the user with popups.
    MAX_BURST = 5

    #: A nudge replaces whatever card is on screen, so firing several at once
    #: would burn their retry budget on cards nobody ever saw. One per minute;
    #: the rest stay due and come round on a later poll.
    NAG_SPACING_SECONDS = 60

    def __init__(
        self,
        db: DatabaseManager,
        interval_seconds: int = 5,
        parent: Optional[QObject] = None,
        nag_minutes: int = 10,
        nag_max: int = 3,
        quiet: Optional["QuietHours"] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        #: When set, alarms are held outside working hours.
        self.quiet = quiet
        self._quiet_since: Optional[datetime] = None
        self.interval_seconds = max(1, int(interval_seconds))
        #: Gap between re-reminders, and how many times to try.
        self.nag_minutes = max(1, int(nag_minutes))
        self.nag_max = max(1, int(nag_max))
        self._scheduler = None
        self._lock = threading.Lock()
        self._running = False
        self._last_nag: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> bool:
        """Start polling. Returns ``False`` if APScheduler is unavailable."""
        if self._running:
            return True
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
        except Exception as exc:                      # noqa: BLE001
            log.exception("APScheduler unavailable")
            self.error.emit(f"APScheduler를 불러올 수 없습니다: {exc}")
            return False

        try:
            self._scheduler = BackgroundScheduler(
                daemon=True,
                job_defaults={
                    "coalesce": True,        # collapse missed runs into one
                    # Overlap is prevented by our own non-blocking lock in
                    # _poll(); allowing 2 here means a one-off slow poll (cold
                    # disk, AV scan on first launch) is silently skipped by us
                    # rather than logged as an APScheduler "max instances" error.
                    "max_instances": 2,
                    "misfire_grace_time": 30,
                },
            )
            self._scheduler.add_job(
                self._poll,
                trigger=IntervalTrigger(seconds=self.interval_seconds),
                id="hud_poll",
                replace_existing=True,
                next_run_time=datetime.now() + timedelta(seconds=1),
            )
            self._scheduler.start()
            self._running = True
            log.info("SchedulerService started (every %ds)", self.interval_seconds)
            return True
        except Exception as exc:                      # noqa: BLE001
            log.exception("Scheduler start failed")
            self.error.emit(f"스케줄러 시작 실패: {exc}")
            self._scheduler = None
            return False

    def stop(self, wait: bool = False) -> None:
        """Shut the poller down (called from ``aboutToQuit``)."""
        self._running = False
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=wait)
                log.info("SchedulerService stopped")
            except Exception:                          # noqa: BLE001
                log.debug("Scheduler shutdown raised", exc_info=True)

    @property
    def running(self) -> bool:
        return self._running

    def set_interval(self, seconds: int) -> None:
        """Change the poll period at runtime (settings → 확인 주기)."""
        seconds = max(1, int(seconds))
        if seconds == self.interval_seconds:
            return
        self.interval_seconds = seconds
        if not self._running or self._scheduler is None:
            return
        try:
            from apscheduler.triggers.interval import IntervalTrigger

            self._scheduler.reschedule_job("hud_poll", trigger=IntervalTrigger(seconds=seconds))
            log.info("Poll interval changed to %ds", seconds)
        except Exception as exc:                       # noqa: BLE001
            log.warning("Could not reschedule poll job: %s", exc)

    # ------------------------------------------------------------------ #
    # polling
    # ------------------------------------------------------------------ #

    def _poll(self) -> None:
        """Runs on the APScheduler worker thread -- signals only, no widgets."""
        if not self._lock.acquire(blocking=False):
            return                                    # previous poll still busy
        try:
            now = datetime.now().replace(microsecond=0)
            if self.quiet is not None and self.quiet.is_quiet(now):
                # Outside working hours the alarm is noise, not a reminder:
                # nobody is going to act on 특약OS이월 at 22:40. Hold everything
                # and let the next poll after work resumes deliver it -- the
                # rows are untouched, so nothing is lost if the PC is off.
                if self._quiet_since is None:
                    self._quiet_since = now
                    log.info("Quiet hours: holding alarms until %s",
                             self.quiet.next_active(now).strftime("%m-%d %H:%M"))
                self.tick.emit()
                return
            if self._quiet_since is not None:
                log.info("Quiet hours over (held since %s)",
                         self._quiet_since.strftime("%m-%d %H:%M"))
                self._quiet_since = None

            due = self.db.due_schedules(now)
            if due:
                self._fire(due, now)
            self._fire_nags(now)
            self.tick.emit()
        except Exception as exc:                       # noqa: BLE001
            # A crash here would silently kill the job, so swallow + report.
            log.exception("Scheduler poll failed")
            self.error.emit(f"알람 확인 중 오류: {exc}")
        finally:
            self._lock.release()

    def _fire(self, due: list[Schedule], now: datetime) -> None:
        fired = 0
        for schedule in due:
            try:
                if fired < self.MAX_BURST:
                    log.info(
                        "ALARM #%d %r (due %s)",
                        schedule.id, schedule.title, fmt_time(schedule.target_time),
                    )
                    self.schedule_due.emit(schedule)
                    fired += 1
                else:
                    # Over the burst cap: still advance the row so it does not
                    # pile up, but stay silent.
                    log.info("Suppressed burst alarm #%d %r", schedule.id, schedule.title)

                # Recurrence / notified bookkeeping (rule 6 in the spec).
                self.db.roll_forward(schedule, now)
            except Exception:                          # noqa: BLE001
                log.exception("Failed to process schedule #%s", schedule.id)
        self.schedules_changed.emit()

    def _fire_nags(self, now: datetime) -> None:
        """Nudge again for alarms that were never acknowledged.

        An alarm the user walked past just turns red and sits there, which is
        exactly when a reminder is most needed. The nag repeats until they
        complete it, snooze it, or dismiss the card -- capped so a forgotten
        item cannot pester forever.
        """
        try:
            pending = self.db.due_nags(now)
        except Exception:                              # noqa: BLE001
            log.exception("Nag lookup failed")
            return
        if not pending:
            return
        if (self._last_nag is not None
                and (now - self._last_nag).total_seconds() < self.NAG_SPACING_SECONDS):
            return

        # Oldest miss first, one per pass -- see NAG_SPACING_SECONDS.
        schedule = min(pending, key=lambda s: s.nag_at or now)
        count = int(schedule.nag_count) + 1
        try:
            if count >= self.nag_max:
                self.db.clear_nag(schedule.id)         # gave it our best shot
            else:
                self.db.arm_nag(
                    schedule.id, now + timedelta(minutes=self.nag_minutes), count)
        except Exception:                              # noqa: BLE001
            log.exception("Could not nudge schedule #%s", schedule.id)
            return
        self._last_nag = now
        log.info("Missed-alarm nudge #%d for #%d %r (%d pending)",
                 count, schedule.id, schedule.title, len(pending))
        self.schedule_missed.emit(schedule, count)
        self.schedules_changed.emit()

    # ------------------------------------------------------------------ #
    # helpers used by the UI
    # ------------------------------------------------------------------ #

    def check_now(self) -> None:
        """Force an immediate poll (used after the user adds a schedule)."""
        if self._running:
            self._poll()

    def next_alarm(self, now: Optional[datetime] = None) -> Optional[Schedule]:
        """The soonest pending alarm, or ``None``."""
        now = now or datetime.now()
        upcoming = [
            s for s in self.db.list_schedules(include_done=False)
            if s.notified == 0 and s.target_time > now
        ]
        return min(upcoming, key=lambda s: s.target_time) if upcoming else None


# --------------------------------------------------------------------------- #
# Countdown formatting (shared by the list rows and the notification card)
# --------------------------------------------------------------------------- #

def humanize_countdown(seconds: float) -> str:
    """``4530`` -> ``'1시간 15분 후'``; negatives read as ``'... 지남'``."""
    past = seconds < 0
    seconds = abs(int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        text = f"{days}일 {hours}시간"
    elif hours:
        text = f"{hours}시간 {minutes}분"
    elif minutes:
        text = f"{minutes}분 {secs}초"
    else:
        text = f"{secs}초"
    return f"{text} 지남" if past else f"{text} 후"


# --------------------------------------------------------------------------- #
# Self-test: ``py scheduler_service.py``
# --------------------------------------------------------------------------- #

def _selftest() -> None:  # pragma: no cover - manual smoke test
    import tempfile

    from PySide6.QtCore import QCoreApplication, QTimer

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = QCoreApplication([])
    db = DatabaseManager(tempfile.mkdtemp(prefix="hud_sched_"))

    now = datetime.now().replace(microsecond=0)
    one_shot = db.add_schedule("한 번만", now + timedelta(seconds=2))
    repeating = db.add_schedule("매일 반복", now + timedelta(seconds=2), "daily")

    service = SchedulerService(db, interval_seconds=1)
    heard: list[str] = []
    service.schedule_due.connect(lambda s: heard.append(s.title))
    service.error.connect(lambda m: print("ERROR:", m))
    assert service.start()

    QTimer.singleShot(6000, app.quit)
    app.exec()
    service.stop(wait=True)

    assert sorted(heard) == ["매일 반복", "한 번만"], heard
    assert db.get_schedule(one_shot).notified == 1
    rolled = db.get_schedule(repeating)
    assert rolled.notified == 0 and rolled.target_time > now + timedelta(hours=23), rolled

    assert humanize_countdown(4530).startswith("1시간 15분")
    assert humanize_countdown(-90).endswith("지남")

    # --- quiet hours -------------------------------------------------------- #
    from holidays import HolidayCalendar
    cal = HolidayCalendar()
    q = QuietHours(enabled=True, calendar=cal)                # 09:00-18:00
    THU = datetime(2026, 8, 20, 10, 0)          # ordinary working Thursday
    assert not q.is_quiet(THU), "근무 시간인데 조용히 함"
    assert q.is_quiet(THU.replace(hour=7)), "출근 전인데 알림"
    assert q.is_quiet(THU.replace(hour=22)), "퇴근 후인데 알림"
    assert q.is_quiet(datetime(2026, 8, 22, 10, 0)), "토요일인데 알림"
    assert q.is_quiet(datetime(2026, 8, 15, 10, 0)), "광복절인데 알림"
    # 22:40 Thursday -> next active is Friday 09:00
    nxt = q.next_active(THU.replace(hour=22, minute=40))
    assert (nxt.day, nxt.hour) == (21, 9), nxt
    # Friday evening -> skips the weekend to Monday
    nxt = q.next_active(datetime(2026, 8, 21, 19, 0))
    assert (nxt.day, nxt.hour) == (24, 9), nxt
    # lunch is only excluded when asked for
    assert not q.is_quiet(THU.replace(hour=12, minute=30))
    q.skip_lunch = True
    assert q.is_quiet(THU.replace(hour=12, minute=30)), "점심 제외인데 알림"
    q.skip_lunch = False
    # disabled means never quiet, whatever the clock says
    q.enabled = False
    assert not q.is_quiet(THU.replace(hour=3)), "꺼져 있는데 조용히 함"
    # a night shift that wraps midnight
    night = QuietHours(start="22:00", end="06:00", enabled=True,
                       skip_holidays=False, calendar=cal)
    assert not night.is_quiet(datetime(2026, 8, 20, 23, 0))
    assert not night.is_quiet(datetime(2026, 8, 20, 2, 0))
    assert night.is_quiet(datetime(2026, 8, 20, 12, 0))
    # garbage settings fall back instead of raising
    broken = QuietHours(start="사팔", end="", enabled=True, calendar=cal)
    assert broken.start == 9 * 60 and broken.end == 18 * 60

    # the poller holds everything while quiet, then delivers
    db2 = DatabaseManager(tempfile.mkdtemp(prefix="hud_quiet_"))
    quiet_now = QuietHours(enabled=True, calendar=cal)
    svc2 = SchedulerService(db2, interval_seconds=1, quiet=quiet_now)
    seen: list[str] = []
    svc2.schedule_due.connect(lambda s: seen.append(s.title))
    db2.add_schedule("야간 알람", datetime.now() - timedelta(minutes=1))
    quiet_now.start, quiet_now.end = 0, 1        # "work" is 00:00-00:01 -> quiet
    svc2._poll()
    assert seen == [], f"조용한 시간에 울림: {seen}"
    quiet_now.enabled = False
    svc2._poll()
    assert seen == ["야간 알람"], f"조용한 시간이 끝났는데 안 울림: {seen}"
    db2.close()

    print("scheduler_service self-test OK ->", heard)


if __name__ == "__main__":  # pragma: no cover
    _selftest()
