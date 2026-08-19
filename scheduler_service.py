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
    ) -> None:
        super().__init__(parent)
        self.db = db
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
    print("scheduler_service self-test OK ->", heard)


if __name__ == "__main__":  # pragma: no cover
    _selftest()
