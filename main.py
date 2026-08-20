"""
main.py
=======
Offline Smart HUD -- entry point.

A small, frameless, translucent desktop panel with two tabs:

    [일정]  오늘의 일정       -- natural-language entry, countdowns, recurrence
    [AI]    AI 어시스턴트     -- streaming chat with a local GGUF model

Everything runs on this machine: SQLite for storage, APScheduler for alarms,
llama.cpp for inference. There is no network code in this project.

Unobtrusive by design
---------------------
At rest the panel drops to a low window opacity *and* an almost fully
transparent background, so it reads as a faint overlay. Move the pointer over
it (or focus it) and both snap back to solid. Text colour never changes -- that
was the earlier bug where translucent labels on a translucent panel became
invisible. Both opacity settings have floors that keep text legible, and every
value is adjustable in 설정 → 모양.

Threads
-------
    GUI thread         : widgets, animations, UI-initiated DB writes
    LlmThread          : llama.cpp load + token generation
    APScheduler thread : alarm poll

All cross-thread traffic is Qt signals (auto-queued), so no worker ever touches
a widget. Every slot that can fail is wrapped in ``@guard`` -- the window must
never vanish without an explanation.

Run:    py main.py [--debug] [--reset-config] [--safe]
Build:  py build.py --model <path-to.gguf>
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCursor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSizeGrip,
    QSizePolicy,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import crash_handler
from config import (
    Config,
    build_style,
    migrate_legacy_data,
    resolve_data_dir,
    set_style,
    style,
)
from crash_handler import guard
from db_manager import REPEAT_NONE, WEEKDAY_NAMES_KO, DatabaseManager, Schedule
from llm_engine import (
    set_data_dir,
    TOOL_ADD,
    TOOL_ADD_EMAIL,
    TOOL_CLEAR,
    TOOL_DELETE,
    TOOL_DELETE_EMAIL,
    TOOL_LIST,
    TOOL_LIST_EMAIL,
    TOOL_REPORT,
    LlmController,
    ParseResult,
    ToolIntent,
    app_dir,
    backend_info,
    build_chat_context,
    correct_false_action_claim,
    ensure_models_dir,
    detect_tool_intent,
    find_model_path,
    is_volatile_model,
    models_dir,
)
from holidays import HOLIDAYS_FILENAME, calendar as holiday_calendar
from outlook_service import OutlookMonitorController
from scheduler_service import (QuietHours, SchedulerService,
                               _parse_hhmm as _hhmm, humanize_countdown)
from ui_components import (
    FILTER_ALL,
    ChatInput,
    ChatView,
    ElidedLabel,
    EmailActionCard,
    GlassPanel,
    ManualScheduleDialog,
    NotificationCard,
    NotificationSound,
    QuickAddBar,
    ScheduleFilterBar,
    ScheduleListView,
    SettingsDialog,
    TitleBar,
    Toast,
    WorkReportDialog,
    build_stylesheet,
    filter_schedules,
    make_app_icon,
    restyle_tree,
)

log = logging.getLogger("hud")

APP_NAME = "OfflineSmartHUD"
ORG_NAME = "OfflineSmartHUD"

OPACITY_FADE_MS = 200
HOVER_POLL_MS = 180
ALERT_HOLD_MS = 6000
SCREEN_MARGIN = 18

#: A heuristic parse below this confidence is never saved silently.
MIN_COMMIT_CONFIDENCE = 0.70


class HudWindow(QWidget):
    """The glass panel: chrome, tabs, settings, and all the wiring."""

    def __init__(self, db: DatabaseManager, config: Config) -> None:
        super().__init__()
        self.db = db
        self.config = config

        self._drag_origin: Optional[QPoint] = None
        self._drag_window_pos: Optional[QPoint] = None
        self._hovered = False
        self._alert_until: Optional[datetime] = None
        self._pending_parse: dict[int, str] = {}
        self._chat_request_id: Optional[int] = None
        self._chat_db_row: Optional[int] = None
        self._active_alarm: Optional[Schedule] = None
        self._first_hide_notice = True
        self._cached_schedules: list[Schedule] = []
        self._expanded_height = int(config.window["height"])
        self._outlook_status: tuple[bool, str] = (False, "확인 중")
        self.tray: Optional[QSystemTrayIcon] = None

        self.sound = NotificationSound(volume=config.behavior["sound_volume"])
        self.llm = LlmController(dict(config.llm))
        self.quiet = QuietHours()
        self._sync_quiet_hours()
        self.scheduler = SchedulerService(
            db, interval_seconds=config.behavior["poll_seconds"],
            nag_minutes=int(config.behavior.get("nag_minutes", 10)),
            nag_max=int(config.behavior.get("nag_max_count", 3)),
            quiet=self.quiet)
        self.outlook = OutlookMonitorController(
            db, interval_seconds=int(config.behavior.get("outlook_poll_seconds", 10)),
            calendar_enabled=bool(config.behavior.get("calendar_enabled", True)))

        self._build_window()
        self._build_ui()
        self._connect_signals()
        self._install_shortcuts()
        self._restore_geometry()

        self.refresh_schedules()
        # Drop empty/corrupt rows before they are ever replayed as context.
        removed = self.db.sanitize_chat()
        if removed:
            log.info("Removed %d malformed chat message(s) at startup", removed)
        self.chat_view.load_history(self.db.recent_messages(60))

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(HOVER_POLL_MS)
        self._hover_timer.timeout.connect(self._poll_hover)
        self._hover_timer.start()

        if config.window["collapsed"]:
            self.set_collapsed(True)
        self._apply_opacity(immediate=True)
        self._on_tick()

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    def _build_window(self) -> None:
        self.setWindowTitle("Offline Smart HUD")
        self.setWindowIcon(make_app_icon())
        flags = Qt.FramelessWindowHint | Qt.Tool      # Tool = no taskbar entry
        if self.config.window["always_on_top"]:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMinimumSize(240, 120)
        self.resize(self.config.window["width"], self.config.window["height"])

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.setDuration(OPACITY_FADE_MS)

    def _build_ui(self) -> None:
        s = style()
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.panel = GlassPanel(self)
        shell.addWidget(self.panel)

        m = self.panel.content_margin()
        self.root = QVBoxLayout(self.panel)
        self.root.setContentsMargins(s.pad + m, max(2, s.pad + m - 4), s.pad + m, s.pad + m)
        self.root.setSpacing(s.gap)

        self.title_bar = TitleBar()
        self.root.addWidget(self.title_bar)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_schedule_tab(), "일정")
        self.tabs.addTab(self._build_chat_tab(), "AI")
        # The tab strip lives in the header row; the native one would just be a
        # second 26 px band of chrome saying the same thing.
        self.tabs.tabBar().hide()
        self.tabs.setCurrentIndex(int(self.config.window["last_tab"]))
        self.title_bar.set_tab_index(self.tabs.currentIndex())
        self.root.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(4)
        self.toast = Toast(self.panel)
        footer.addWidget(self.toast, 1)
        self.grip = QSizeGrip(self.panel)
        self.grip.setFixedSize(12, 12)
        footer.addWidget(self.grip, 0, Qt.AlignBottom | Qt.AlignRight)
        self.root.addLayout(footer)

        self.notification = NotificationCard(self.panel)
        self.notification.hide()

        self.email_card = EmailActionCard(self.panel)
        self.email_card.hide()

    def _build_schedule_tab(self) -> QWidget:
        s = style()
        page = QWidget()
        page.setAttribute(Qt.WA_TranslucentBackground, True)
        self.schedule_layout = QVBoxLayout(page)
        self.schedule_layout.setContentsMargins(0, s.gap, 0, 0)
        self.schedule_layout.setSpacing(s.gap)

        self.quick_add = QuickAddBar()
        self.schedule_layout.addWidget(self.quick_add)

        self.summary_row = QWidget()
        self.summary_row.setAttribute(Qt.WA_TranslucentBackground, True)
        strip = QHBoxLayout(self.summary_row)
        strip.setContentsMargins(2, 0, 2, 0)
        strip.setSpacing(6)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        self.summary_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        strip.addWidget(self.summary_label)
        strip.addStretch(1)
        # "다음 …"는 제목 길이에 따라 얼마든지 길어진다 -- 남는 폭에 맞춰 줄인다.
        self.next_label = ElidedLabel("")
        strip.addWidget(self.next_label, 1)
        self.schedule_layout.addWidget(self.summary_row)

        # Outlook's calendar, read-only. Meetings live in Outlook and always
        # will; duplicating them into our DB would just create two sources of
        # truth. One line saying what is on today is the useful part.
        self.meeting_label = ElidedLabel("")
        self.meeting_label.setObjectName("muted")
        self.meeting_label.setVisible(False)
        self.schedule_layout.addWidget(self.meeting_label)

        self.filter_bar = ScheduleFilterBar()
        self.filter_bar.set_current(
            str(self.config.window.get("schedule_filter", FILTER_ALL)), notify=False)
        self.schedule_layout.addWidget(self.filter_bar)

        self.schedule_list = ScheduleListView()
        self.schedule_layout.addWidget(self.schedule_list, 1)
        return page

    def _build_chat_tab(self) -> QWidget:
        s = style()
        page = QWidget()
        page.setAttribute(Qt.WA_TranslucentBackground, True)
        self.chat_layout = QVBoxLayout(page)
        self.chat_layout.setContentsMargins(0, s.gap, 0, 0)
        self.chat_layout.setSpacing(s.gap)

        self.chat_view = ChatView()
        self.chat_layout.addWidget(self.chat_view, 1)

        self.model_label = QLabel("")
        self.model_label.setObjectName("muted")
        self.model_label.setWordWrap(True)
        self.chat_layout.addWidget(self.model_label)

        self.chat_input = ChatInput()
        self.chat_layout.addWidget(self.chat_input)
        return page

    def _connect_signals(self) -> None:
        self.title_bar.drag_started.connect(self._drag_begin)
        self.title_bar.drag_moved.connect(self._drag_move)
        self.title_bar.drag_finished.connect(self._drag_end)
        self.title_bar.collapse_toggled.connect(self.set_collapsed)
        self.title_bar.settings_requested.connect(self.open_settings)
        self.title_bar.minimize_requested.connect(self.hide_to_tray)
        self.title_bar.close_requested.connect(self.hide_to_tray)
        self.title_bar.tab_selected.connect(self.tabs.setCurrentIndex)
        self.title_bar.lock_toggled.connect(
            lambda v: self._toggle_window_option("locked", v, "위치 잠금"))
        self.title_bar.pin_toggled.connect(
            lambda v: self._toggle_window_option("always_on_top", v, "항상 위에 표시"))
        self.title_bar.set_locked(self.config.window["locked"])
        self.title_bar.set_pinned(self.config.window["always_on_top"])

        self.quick_add.submitted.connect(self.handle_quick_add)
        self.quick_add.manual_requested.connect(lambda: self.open_manual_dialog())
        self.filter_bar.changed.connect(self.on_filter_changed)
        self.schedule_list.toggled.connect(self.on_schedule_toggled)
        self.schedule_list.deleted.connect(self.on_schedule_deleted)
        self.schedule_list.edit_requested.connect(self.on_schedule_edit)

        self.chat_input.submitted.connect(self.handle_chat_submit)
        self.chat_input.stop_requested.connect(self.llm.cancel)
        self.chat_input.clear_requested.connect(self.clear_chat)

        self.llm.model_state.connect(self.on_model_state)
        self.llm.model_note.connect(self._on_model_note)
        self.on_model_state("idle", "")          # sensible label before loading
        self.llm.parse_finished.connect(self.on_parse_finished)
        self.llm.chat_token.connect(self.on_chat_token)
        self.llm.chat_thinking.connect(self.on_chat_thinking)
        self.llm.chat_finished.connect(self.on_chat_finished)
        self.llm.chat_error.connect(self.on_chat_error)

        self.scheduler.schedule_due.connect(self.on_schedule_due)
        self.scheduler.schedule_missed.connect(self.on_schedule_missed)
        self.scheduler.schedules_changed.connect(self.refresh_schedules)
        self.scheduler.error.connect(lambda m: self.toast.show_text(m, "error"))

        self.notification.completed.connect(self._complete_active_alarm)
        self.notification.snoozed.connect(self._snooze_active_alarm)
        # Closing the card means "I saw it"; timing out does not, so only the
        # explicit close stops the re-reminders.
        self.notification.dismissed.connect(self._acknowledge_active_alarm)

        self.outlook.email_matched.connect(self.on_awaited_email_matched)
        self.outlook.service_status.connect(self.on_outlook_status)
        self.outlook.meetings_updated.connect(self.on_meetings_updated)

        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, index: int) -> None:
        """Keep the header pills in step with programmatic tab changes."""
        self.config.set("window", "last_tab", int(index))
        self.title_bar.set_tab_index(int(index))

    def _install_shortcuts(self) -> None:
        for keys, handler in (
            ("Ctrl+N", lambda: self.open_manual_dialog()),
            ("Ctrl+,", self.open_settings),
            ("Ctrl+Tab", lambda: self.tabs.setCurrentIndex(1 - self.tabs.currentIndex())),
            ("Esc", self.hide_to_tray),
            ("Ctrl+L", lambda: self.quick_add.input.setFocus()),
            ("Ctrl+Shift+C", self.clear_chat),
        ):
            QShortcut(QKeySequence(keys), self, activated=handler)

    #: Delay before the model is loaded, so the first paint always wins the race.
    PRELOAD_DELAY_MS = 1200

    def start_services(self) -> None:
        """Start background threads once the window is up.

        Model loading is deferred on purpose. ``Llama(...)`` runs on the worker
        thread but makes thousands of small ctypes calls while parsing GGUF
        metadata, and that contention visibly stalls the GUI thread for several
        seconds on a multi-GB model. Letting the window paint and settle first
        turns a "frozen at launch" window into a responsive one that simply
        shows ⏳ 모델 로딩 중 for a moment.
        """
        if not self.scheduler.start():
            self.toast.show_text("알람 스케줄러를 시작하지 못했습니다 (로그 확인)", "error", 8000)

        preload = bool(self.config.llm["preload"])
        self.llm.start(preload=False)                 # spin the thread only
        if preload:
            QTimer.singleShot(self.PRELOAD_DELAY_MS, self._preload_model)

        if self.config.behavior.get("outlook_enabled", True):
            self.outlook.start()
        else:
            log.info("Outlook monitoring disabled by settings")

    @guard("모델 미리 로드", toast=False)
    def _preload_model(self) -> None:
        if self.llm.current_state in ("idle", "missing"):
            log.info("Preloading model after UI settled")
            self.llm.reload_model()

    # ------------------------------------------------------------------ #
    # styling
    # ------------------------------------------------------------------ #

    @guard("화면 갱신")
    def apply_theme(self) -> None:
        """Rebuild the style from config and restyle every widget in place."""
        set_style(build_style(self.config))
        s = style()
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(s))
            app.setWindowIcon(make_app_icon())

        m = self.panel.content_margin()
        self.root.setContentsMargins(s.pad + m, max(2, s.pad + m - 4), s.pad + m, s.pad + m)
        self.root.setSpacing(s.gap)
        for layout in (self.schedule_layout, self.chat_layout):
            layout.setContentsMargins(0, s.gap, 0, 0)
            layout.setSpacing(s.gap)
        self.next_label.setStyleSheet(
            f"color: {s.css(s.accent)}; font-size: {s.f_xs}px; font-weight: 700;")
        self.summary_row.setVisible(s.show_summary)

        restyle_tree(self)
        if self.tray is not None:
            self.tray.setIcon(make_app_icon())
        self._position_notification()
        self._position_email_card()
        self._apply_opacity(immediate=True)

    @guard("설정 적용")
    def apply_settings(self, restyle: bool = True) -> None:
        """Push config values into every subsystem that caches them."""
        cfg = self.config
        if restyle:
            self.apply_theme()

        # setWindowFlag() re-creates the native window and hides it, so the
        # visibility check has to happen BEFORE the call. Testing it afterwards
        # always saw False and never re-showed -- toggling 항상 위에 표시 made
        # the HUD vanish until the tray icon was clicked.
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(cfg.window["always_on_top"]))
        if was_visible and not self.isVisible():
            self.show()

        self.sound.set_volume(float(cfg.behavior["sound_volume"]))
        self.scheduler.set_interval(int(cfg.behavior["poll_seconds"]))
        self._sync_quiet_hours()
        self.outlook.set_calendar_enabled(
            bool(cfg.behavior.get("calendar_enabled", True)))
        self.scheduler.nag_minutes = max(1, int(cfg.behavior.get("nag_minutes", 10)))
        self.scheduler.nag_max = max(1, int(cfg.behavior.get("nag_max_count", 3)))
        self.llm.apply_options(dict(cfg.llm))
        self.refresh_schedules()
        cfg.save()

    @guard("설정 창")
    def open_settings(self) -> None:
        snapshot = {k: dict(v) if isinstance(v, dict) else v
                    for k, v in self.config.data.items()}
        dialog = SettingsDialog(self.config, self, backend=backend_info(), db=self.db)
        dialog.preview_requested.connect(lambda: self.apply_settings(restyle=True))
        dialog.reset_requested.connect(self._reset_settings)
        dialog.center_on(self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        if not accepted:
            self.config.data = snapshot          # Cancel restores the snapshot
            self.config._sanitise()
            self.apply_settings(restyle=True)
        else:
            self.config.save()
            self.toast.show_text("설정이 저장되었습니다", "success", 2000)

    def _reset_settings(self) -> None:
        self.config.reset()
        self.apply_settings(restyle=True)
        self.toast.show_text("기본값으로 되돌렸습니다", "info")

    # ------------------------------------------------------------------ #
    # geometry, opacity, collapse
    # ------------------------------------------------------------------ #

    def _restore_geometry(self) -> None:
        w = self.config.window
        if w["remember_position"] and w["x"] is not None and w["y"] is not None:
            self.move(int(w["x"]), int(w["y"]))
            if self._on_a_screen():
                return
        self.snap_to_corner(w["corner"])

    def _on_a_screen(self) -> bool:
        centre = self.frameGeometry().center()
        return any(s.availableGeometry().contains(centre) for s in QGuiApplication.screens())

    def snap_to_corner(self, corner: str = "top-right") -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = area.right() - w - SCREEN_MARGIN if "right" in corner else area.left() + SCREEN_MARGIN
        y = area.bottom() - h - SCREEN_MARGIN if "bottom" in corner else area.top() + SCREEN_MARGIN
        self.move(int(x), int(y))

    def save_state(self) -> None:
        w = self.config.window
        w["x"], w["y"] = self.x(), self.y()
        w["width"] = self.width()
        w["height"] = self._expanded_height if w["collapsed"] else self.height()
        w["last_tab"] = self.tabs.currentIndex()
        self.config.save()

    @guard("접기/펼치기")
    def set_collapsed(self, collapsed: bool) -> None:
        """Shrink to just the title bar -- the smallest possible footprint."""
        collapsed = bool(collapsed)
        self.config.set("window", "collapsed", collapsed)
        self.title_bar.set_collapsed(collapsed)
        if collapsed:
            self._expanded_height = max(self.height(), 160)
            self.tabs.hide()
            self.toast.hide()
            self.grip.hide()
            self.notification.hide()
            self.email_card.hide()
            s = style()
            chrome = self.panel.content_margin() * 2 + s.pad * 2 + s.title_h
            self.setFixedHeight(chrome)
        else:
            self.tabs.show()
            self.grip.show()
            self.setMinimumHeight(160)
            self.setMaximumHeight(16777215)
            self.resize(self.width(), self._expanded_height)
        self.config.save()

    def _drag_begin(self, global_pos: QPoint) -> None:
        if self.config.window["locked"]:
            self.toast.show_text("위치가 잠겨 있습니다 (설정 → 동작)", "warn", 1800)
            return
        self._drag_origin = global_pos
        self._drag_window_pos = self.pos()

    def _drag_move(self, global_pos: QPoint) -> None:
        if self._drag_origin is not None and self._drag_window_pos is not None:
            self.move(self._drag_window_pos + (global_pos - self._drag_origin))

    def _drag_end(self) -> None:
        if self._drag_origin is not None:
            self._drag_origin = None
            self._drag_window_pos = None
            self.save_state()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
            self._drag_begin(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_origin is not None:
            self._drag_move(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_end()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_notification()
        self._position_email_card()

    def _position_notification(self) -> None:
        """Float the alarm card below the quick-add bar, never over it."""
        if not hasattr(self, "notification"):
            return
        m = self.panel.content_margin()
        self.notification.setFixedWidth(max(180, self.panel.width() - 2 * (m + 10)))
        self.notification.adjustSize()
        top = m + style().title_h + 8
        if self.quick_add.isVisible() and self.tabs.isVisible():
            anchor = self.quick_add.mapTo(self.panel, QPoint(0, self.quick_add.height()))
            top = anchor.y() + (22 if style().show_summary else 6)
        self.notification.move(m + 10, top)
        self.notification.raise_()

    def _poll_hover(self) -> None:
        """Polling avoids the enter/leave flicker caused by child widgets."""
        inside = self.isVisible() and self.frameGeometry().contains(QCursor.pos())
        if inside != self._hovered:
            self._hovered = inside
            self._apply_opacity()

    def _engaged(self) -> bool:
        return bool(self._hovered or self.isActiveWindow()
                    or (self._alert_until and datetime.now() < self._alert_until))

    def _apply_opacity(self, immediate: bool = False) -> None:
        a = self.config.appearance
        if self._alert_until and datetime.now() < self._alert_until:
            target = 1.0
        elif self._hovered or self.isActiveWindow():
            target = float(a["hover_opacity"])
        else:
            target = float(a["idle_opacity"])

        self.panel.fade_to(self._engaged())
        if immediate:
            self._opacity_anim.stop()
            self.setWindowOpacity(target)
            return
        if abs(self.windowOpacity() - target) < 0.01:
            return
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(target)
        self._opacity_anim.start()

    def event(self, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.WindowActivate, QEvent.WindowDeactivate):
            self._apply_opacity()
        return super().event(event)

    # ------------------------------------------------------------------ #
    # context menu
    # ------------------------------------------------------------------ #

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        w = self.config.window
        menu = QMenu(self)

        lock = QAction("위치 잠금", self, checkable=True, checked=w["locked"])
        lock.toggled.connect(lambda v: self._toggle_window_option("locked", v, "위치 잠금"))
        menu.addAction(lock)

        pin = QAction("항상 위에 표시", self, checkable=True, checked=w["always_on_top"])
        pin.toggled.connect(
            lambda v: self._toggle_window_option("always_on_top", v, "항상 위에 표시"))
        menu.addAction(pin)

        collapse = QAction("접기", self, checkable=True, checked=w["collapsed"])
        collapse.toggled.connect(self.set_collapsed)
        menu.addAction(collapse)

        corners = menu.addMenu("위치")
        for label, key in (("우측 상단", "top-right"), ("우측 하단", "bottom-right"),
                           ("좌측 상단", "top-left"), ("좌측 하단", "bottom-left")):
            corners.addAction(label, lambda k=key: (self.snap_to_corner(k), self.save_state()))

        opacity = menu.addMenu("평소 투명도")
        group = QActionGroup(self)
        group.setExclusive(True)
        for percent in (35, 50, 65, 80, 100):
            action = QAction(f"{percent}%", self, checkable=True)
            action.setChecked(abs(self.config.appearance["idle_opacity"] - percent / 100) < 0.03)
            action.triggered.connect(lambda _=False, p=percent: self._set_idle_opacity(p / 100))
            group.addAction(action)
            opacity.addAction(action)

        menu.addSeparator()
        menu.addAction("설정…", self.open_settings)
        menu.addAction("일정 직접 추가", lambda: self.open_manual_dialog())
        menu.addAction("업무 보고 만들기…", self.open_work_report)
        menu.addAction("완료된 일정 정리", self.clear_completed)
        menu.addAction("대화 기록 지우기  (Ctrl+Shift+C)", self.clear_chat)
        menu.addSeparator()
        menu.addAction("트레이로 숨기기", self.hide_to_tray)
        menu.addAction("종료", QApplication.quit)
        menu.exec(event.globalPos())

    def _toggle_window_option(self, key: str, value: bool, label: str) -> None:
        self.config.set("window", key, bool(value))
        # Header buttons and the context menu drive the same two settings, so
        # whichever was used has to update the other.
        self.title_bar.set_locked(self.config.window["locked"])
        self.title_bar.set_pinned(self.config.window["always_on_top"])
        self.apply_settings(restyle=False)
        self.toast.show_text(f"{label} {'켜짐' if value else '꺼짐'}", "info", 1500)

    def _set_idle_opacity(self, value: float) -> None:
        self.config.set("appearance", "idle_opacity", value)
        self.config._sanitise()
        self._apply_opacity(immediate=True)
        self.config.save()
        self.toast.show_text(
            f"평소 투명도 {int(self.config.appearance['idle_opacity'] * 100)}%", "info", 1500)

    # ------------------------------------------------------------------ #
    # schedules
    # ------------------------------------------------------------------ #

    @guard("일정 목록 불러오기")
    def refresh_schedules(self) -> None:
        """Reload from SQLite, then narrow to the selected filter.

        The summary line and overdue badge always describe the *whole* list, not
        the filtered view -- hiding items must never hide the fact that
        something is overdue.
        """
        schedules = self.db.list_schedules(
            include_done=not self.config.behavior["hide_completed"], limit=300)
        self._cached_schedules = schedules

        key = self.filter_bar.current()
        visible = filter_schedules(schedules, key)
        self.schedule_list.set_schedules(visible)
        self.filter_bar.set_count(
            "" if key == FILTER_ALL else f"{len(visible)}/{len(schedules)}")
        self._update_summary(schedules)

    @guard("필터 변경")
    def on_filter_changed(self, key: str) -> None:
        self.config.set("window", "schedule_filter", key)
        self.config.save()
        self.refresh_schedules()

    def _update_summary(self, schedules: list[Schedule]) -> None:
        now = datetime.now()
        open_items = [s for s in schedules if not s.is_done]
        overdue = sum(1 for s in open_items if s.target_time <= now)
        self.summary_label.setText(
            f"열린 일정 {len(open_items)}" + (f" · 지남 {overdue}" if overdue else ""))
        self.title_bar.set_badge(str(overdue) if overdue else "")

        upcoming = [s for s in open_items if s.target_time > now]
        if upcoming:
            nxt = min(upcoming, key=lambda s: s.target_time)
            # No character-count trim: ElidedLabel cuts to the real pixel width
            # and keeps the whole line in a tooltip.
            self.next_label.setText(
                f"다음 {nxt.title} · {humanize_countdown(nxt.seconds_left(now))}")
        else:
            self.next_label.setText("")

    @guard("시계 갱신", toast=False)
    def _on_tick(self) -> None:
        now = datetime.now()
        fmt = "%H:%M:%S" if self.config.appearance["show_seconds"] else "%H:%M"
        self.title_bar.set_clock(now.strftime(fmt))
        self.schedule_list.refresh_countdowns()
        self._update_summary(self._cached_schedules)
        if self._alert_until and now >= self._alert_until:
            self._alert_until = None
            self._apply_opacity()

    # -- quick add ----------------------------------------------------------- #

    @guard("일정 추가")
    def handle_quick_add(self, text: str) -> None:
        now = datetime.now().replace(second=0, microsecond=0)
        request_id, immediate = self.llm.parse_schedule(text, now)
        if immediate is not None:
            self._apply_parse_result(immediate, text)
            return
        self._pending_parse[request_id] = text
        self.quick_add.set_busy(True)
        self.toast.show_text("AI가 분석하는 중…", "info", 2500)

    @guard("일정 분석 결과")
    def on_parse_finished(self, request_id: int, result: object) -> None:
        text = self._pending_parse.pop(request_id, "")
        if not self._pending_parse:
            self.quick_add.set_busy(False)
        if isinstance(result, ParseResult):
            self._apply_parse_result(result, text or result.raw_text)

    def _apply_parse_result(self, result: ParseResult, raw_text: str) -> None:
        confident = (result.usable and not result.needs_confirm
                     and result.confidence >= MIN_COMMIT_CONFIDENCE)
        if not confident:
            reason = {
                "model_unavailable": "AI 모델을 사용할 수 없습니다.",
                "json_parse_failed": "AI 응답을 이해하지 못했습니다.",
                "past_time": "이미 지난 시각으로 해석되었습니다.",
                "out_of_range": "너무 먼 미래로 해석되었습니다.",
            }.get(result.error,
                  "시간 정보를 확인해주세요." if result.usable else "시간 정보를 찾지 못했습니다.")
            self.open_manual_dialog(
                title=result.title or raw_text,
                when=result.target_time,
                repeat_type=result.repeat_type,
                repeat_detail=result.repeat_detail,
                note=f"‘{raw_text}’\n{reason}",
            )
            return

        self.db.add_schedule(result.title, result.target_time,
                             result.repeat_type, result.repeat_detail)
        self.quick_add.clear()
        self.refresh_schedules()
        self.toast.show_text(
            f"{result.title} · {result.target_time.strftime('%m/%d %H:%M')} 등록", "success")
        if self.config.behavior["flash_on_alert"]:
            self.panel.flash_alert(style().success, pulses=1, duration_ms=420)

    @guard("일정 입력 창")
    def open_manual_dialog(self, title: str = "", when: Optional[datetime] = None,
                           repeat_type: str = REPEAT_NONE, repeat_detail: str = "",
                           note: str = "", schedule_id: Optional[int] = None) -> None:
        dialog = ManualScheduleDialog(
            self, title=title, when=when, repeat_type=repeat_type,
            repeat_detail=repeat_detail,
            heading="일정 수정" if schedule_id else "일정 확인", note=note)
        dialog.center_on(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if schedule_id is None:
            self.db.add_schedule(**values)
            message = "일정이 추가되었습니다"
        else:
            self.db.update_schedule(schedule_id, notified=0, **values)
            message = "일정이 수정되었습니다"
        self.quick_add.clear()
        self.refresh_schedules()
        self.toast.show_text(message, "success")

    # -- row actions ---------------------------------------------------------- #

    @guard("완료 처리")
    def on_schedule_toggled(self, schedule_id: int, done: bool) -> None:
        """Tick a row off. Recurring chores advance instead of finishing."""
        schedule = self.db.get_schedule(schedule_id)
        title = schedule.title if schedule else ""
        action, nxt = self.db.complete_schedule(schedule_id, done)
        self.refresh_schedules()

        if action == "rolled" and nxt is not None:
            self.toast.show_text(
                f"{title} 완료! 다음 일정: {nxt.strftime('%m/%d %H:%M')} 이월됨",
                "success", 4200)
            if self.config.behavior["flash_on_alert"]:
                self.panel.flash_alert(style().success, pulses=1, duration_ms=420)
        elif action == "done":
            self.toast.show_text(f"{title} 완료", "success", 2000)

    @guard("일정 삭제")
    def on_schedule_deleted(self, schedule_id: int) -> None:
        schedule = self.db.get_schedule(schedule_id)
        if schedule is None:
            return
        if self.config.behavior["confirm_delete"]:
            answer = QMessageBox.question(
                self, "삭제 확인", f"‘{schedule.title}’ 일정을 삭제할까요?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.db.delete_schedule(schedule_id)
        self.refresh_schedules()
        self.toast.show_text(f"‘{schedule.title}’ 삭제됨", "info")

    @guard("일정 수정")
    def on_schedule_edit(self, schedule_id: int) -> None:
        schedule = self.db.get_schedule(schedule_id)
        if schedule is None:
            return
        self.open_manual_dialog(
            title=schedule.title, when=schedule.target_time,
            repeat_type=schedule.repeat_type, repeat_detail=schedule.repeat_detail,
            schedule_id=schedule_id)

    @guard("Outlook 일정 표시", toast=False)
    def on_meetings_updated(self, meetings: list) -> None:
        """One line under the summary: what Outlook says is on today."""
        now = datetime.now()
        upcoming = [m for m in meetings
                    if (m.get("end") or m["start"]) >= now or m.get("all_day")]
        if not upcoming:
            self.meeting_label.setText("")
            self.meeting_label.setVisible(False)
            return
        nxt = upcoming[0]
        when = "종일" if nxt.get("all_day") else nxt["start"].strftime("%H:%M")
        where = f" · {nxt['location']}" if nxt.get("location") else ""
        more = f"  (+{len(upcoming) - 1})" if len(upcoming) > 1 else ""
        self.meeting_label.setText(f"▤ {when}  {nxt['subject']}{where}{more}")
        self.meeting_label.setToolTip("\n".join(
            ("종일" if m.get("all_day") else m["start"].strftime("%H:%M"))
            + f"  {m['subject']}" + (f" · {m['location']}" if m.get("location") else "")
            for m in meetings))
        self.meeting_label.setVisible(True)

    def _sync_quiet_hours(self) -> None:
        """Push the working-hours settings into the live QuietHours object."""
        b = self.config.behavior
        self.quiet.enabled = bool(b.get("quiet_enabled", False))
        self.quiet.start = _hhmm(b.get("work_start", "09:00"), 9, 0)
        self.quiet.end = _hhmm(b.get("work_end", "18:00"), 18, 0)
        self.quiet.lunch_start = _hhmm(b.get("lunch_start", "12:00"), 12, 0)
        self.quiet.lunch_end = _hhmm(b.get("lunch_end", "13:00"), 13, 0)
        self.quiet.skip_lunch = bool(b.get("quiet_skip_lunch", False))
        self.quiet.skip_holidays = bool(b.get("quiet_skip_holidays", True))

    @guard("업무 보고 만들기")
    def open_work_report(self) -> None:
        """The Friday paragraph, assembled from what was actually completed."""
        WorkReportDialog(self.db, self).exec()

    @guard("완료 일정 정리")
    def clear_completed(self) -> None:
        removed = self.db.clear_completed()
        self.refresh_schedules()
        self.toast.show_text(f"완료된 일정 {removed}건 정리", "success")

    # -- alarms ---------------------------------------------------------------- #

    @guard("알람 처리")
    def on_schedule_due(self, schedule: object) -> None:
        if not isinstance(schedule, Schedule):
            return
        b = self.config.behavior
        log.info("Alarm fired: #%s %s", schedule.id, schedule.title)
        self._active_alarm = schedule

        if b["sound_enabled"]:
            self.sound.play()

        self._alert_until = datetime.now() + timedelta(milliseconds=ALERT_HOLD_MS)
        self._apply_opacity()
        if b["flash_on_alert"]:
            self.panel.flash_alert(style().accent, pulses=int(b["alert_pulses"]))

        if not self.isVisible():
            self.show_hud()
        if self.config.window["collapsed"]:
            self.set_collapsed(False)
        self.tabs.setCurrentIndex(0)
        self._position_notification()
        self.notification.show_alarm(
            schedule,
            auto_hide_ms=int(b["notification_seconds"]) * 1000,
            snooze_minutes=int(b["snooze_minutes"]))
        self.schedule_list.highlight(schedule.id)
        # `schedule` is the row as it fired, so this is the occurrence the user
        # will have missed even after a recurring row rolls forward.
        self._arm_nag(schedule.id, missed=schedule.target_time)

        if b["tray_balloon"] and self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(
                "일정 알림", f"{schedule.title}\n{schedule.target_time.strftime('%H:%M')}",
                make_app_icon(), 8000)

    @guard("놓친 알람 재알림")
    def on_schedule_missed(self, schedule: object, count: int) -> None:
        """Re-reminder for an alarm that was never acknowledged."""
        if not isinstance(schedule, Schedule):
            return
        b = self.config.behavior
        log.info("Missed-alarm nudge #%d: %s", count, schedule.title)
        self._active_alarm = schedule

        if b["sound_enabled"]:
            self.sound.play()
        self._alert_until = datetime.now() + timedelta(milliseconds=ALERT_HOLD_MS)
        if not self.isVisible():
            self.show_hud()
        if self.config.window["collapsed"]:
            self.set_collapsed(False)
        self._apply_opacity(immediate=True)
        if b["flash_on_alert"]:
            self.panel.flash_alert(style().warn, pulses=int(b["alert_pulses"]))

        self.tabs.setCurrentIndex(0)
        self._position_notification()
        self.notification.show_alarm(
            schedule,
            auto_hide_ms=int(b["notification_seconds"]) * 1000,
            snooze_minutes=int(b["snooze_minutes"]),
            missed_count=count)
        self.schedule_list.highlight(schedule.id)

        if b["tray_balloon"] and self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(
                f"놓친 알림 · {count}번째",
                f"{schedule.title}\n{schedule.missed_time.strftime('%H:%M')} 예정",
                make_app_icon(), 8000)

    def _arm_nag(self, schedule_id: int, missed: Optional[datetime] = None) -> None:
        """Start the re-reminder clock for a freshly fired alarm."""
        b = self.config.behavior
        if not b.get("nag_enabled", True):
            return
        minutes = int(b.get("nag_minutes", 10))
        self.db.arm_nag(schedule_id, datetime.now() + timedelta(minutes=minutes),
                        0, origin=missed)

    @guard("알람 확인 처리", toast=False)
    def _acknowledge_active_alarm(self) -> None:
        """Card closed by hand -- the user has seen it, stop nudging."""
        if self._active_alarm is not None:
            self.db.clear_nag(self._active_alarm.id)

    @guard("알람 완료")
    def _complete_active_alarm(self) -> None:
        """The card's 완료 button follows the same recurring-to-do rule."""
        if self._active_alarm is None:
            return
        title = self._active_alarm.title
        self.db.clear_nag(self._active_alarm.id)
        action, nxt = self.db.complete_schedule(self._active_alarm.id, True)
        self.refresh_schedules()
        if action == "rolled" and nxt is not None:
            self.toast.show_text(
                f"{title} 완료! 다음 일정: {nxt.strftime('%m/%d %H:%M')} 이월됨",
                "success", 4200)
        else:
            self.toast.show_text(f"{title} 완료", "success")
        self._active_alarm = None

    @guard("알람 미루기")
    def _snooze_active_alarm(self, minutes: int) -> None:
        schedule = self._active_alarm
        if schedule is None:
            return
        when = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=minutes)
        self.db.clear_nag(schedule.id)      # snoozing IS acknowledging
        if schedule.is_recurring:
            # The series already rolled forward; add a one-off so the
            # recurrence itself stays untouched.
            self.db.add_schedule(f"{schedule.title} (미룸)", when)
        else:
            self.db.update_schedule(schedule.id, target_time=when, notified=0, is_done=0)
        self.refresh_schedules()
        self.toast.show_text(f"{minutes}분 뒤 다시 알림", "info")
        self._active_alarm = None

    # ------------------------------------------------------------------ #
    # chat
    # ------------------------------------------------------------------ #

    @guard("모델 상태", toast=False)
    def on_model_state(self, state: str, message: str) -> None:
        self.title_bar.set_model_state(state, f"{state}: {message}")
        usable = self.llm.can_chat
        texts = {
            "idle": "○ 대기 중 · 첫 메시지를 보내면 모델을 불러옵니다",
            "loading": f"◌ 모델 로딩 중… ({message})",
            "ready": f"● 준비 완료 · {message}",
            "missing": f"○ {message}",
            "unavailable": f"● {message}",
            "error": f"● {message}",
            "oom": f"● {message}",
        }
        self.model_label.setText(texts.get(state, message))
        self.chat_input.set_enabled_state(
            usable,
            "메시지…  (Enter 전송 · Shift+Enter 줄바꿈)" if usable
            else "AI 모델을 사용할 수 없어 채팅이 비활성화되었습니다.")
        if state in ("missing", "unavailable", "error", "oom"):
            log.warning("Model state=%s: %s", state, message)

    # ---- awaited email ------------------------------------------------- #

    @guard("메일 도착 알림")
    def on_awaited_email_matched(self, rule_id: int, subject: str,
                                 sender_name: str, reminder_action: str) -> None:
        """An awaited mail arrived: wake the HUD and show the follow-up steps."""
        b = self.config.behavior
        log.info("Awaited email matched rule #%s: %r", rule_id, subject[:60])

        if b["sound_enabled"]:
            self.sound.play()

        self._alert_until = datetime.now() + timedelta(milliseconds=ALERT_HOLD_MS)
        if not self.isVisible():
            self.show_hud()
        if self.config.window["collapsed"]:
            self.set_collapsed(False)
        self._apply_opacity(immediate=True)          # alert opacity, right away
        if b["flash_on_alert"]:
            self.panel.flash_alert(style().accent, pulses=int(b["alert_pulses"]))

        self._position_email_card()
        self.email_card.show_email(rule_id, subject, sender_name, reminder_action)
        self.toast.show_text(f"메일 도착: {subject[:24]}", "success", 5000)

        if b["tray_balloon"] and self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(
                "기다리던 메일 도착",
                f"{subject}\n{sender_name}\n\n{reminder_action}"[:250],
                make_app_icon(), 10000)

    def on_outlook_status(self, available: bool, message: str) -> None:
        """Outlook connectivity changes are informational, never fatal."""
        log.info("Outlook status: available=%s (%s)", available, message)
        self._outlook_status = (available, message)
        # Only surface a problem if the user actually has rules waiting.
        try:
            waiting = self.db.get_active_awaited_emails()
        except Exception:                                # noqa: BLE001
            waiting = []
        if waiting and not available:
            self.toast.show_text(f"⚠ {message}", "warn", 5000)

    def _position_email_card(self) -> None:
        """Same slot as the alarm card: under the quick-add row."""
        if not hasattr(self, "email_card"):
            return
        m = self.panel.content_margin()
        self.email_card.setFixedWidth(max(190, self.panel.width() - 2 * (m + 10)))
        self.email_card.adjustSize()
        top = m + style().title_h + 8
        if self.quick_add.isVisible() and self.tabs.isVisible():
            anchor = self.quick_add.mapTo(self.panel, QPoint(0, self.quick_add.height()))
            top = anchor.y() + (22 if style().show_summary else 6)
        self.email_card.move(m + 10, top)
        self.email_card.raise_()

    # ---- chat tool calling -------------------------------------------- #

    @guard("채팅 명령 실행", default=None)
    def run_tool(self, intent: ToolIntent) -> Optional[str]:
        """Execute a detected chat instruction. Returns the reply, or None.

        Returning None falls through to normal LLM chat, so a misfired
        detection degrades into a conversation rather than a dead end.
        """
        handlers = {
            TOOL_ADD: self._tool_add,
            TOOL_LIST: self._tool_list,
            TOOL_CLEAR: self._tool_clear,
            TOOL_DELETE: self._tool_delete,
            TOOL_ADD_EMAIL: self._tool_add_email,
            TOOL_LIST_EMAIL: self._tool_list_email,
            TOOL_DELETE_EMAIL: self._tool_delete_email,
            TOOL_REPORT: self._tool_report,
        }
        handler = handlers.get(intent.tool)
        if handler is None:
            return None
        log.info("Chat tool: %s (%r)", intent.tool, intent.raw_text[:60])
        return handler(intent)

    def _tool_add(self, intent: ToolIntent) -> Optional[str]:
        items = [r for r in (intent.schedules or [intent.schedule])
                 if r is not None and r.usable]
        if not items:
            return None

        lines = []
        for result in items:
            self.db.add_schedule(result.title, result.target_time,
                                 result.repeat_type, result.repeat_detail)
            when = result.target_time.strftime("%m/%d %H:%M")
            repeat = ""
            if result.repeat_type != REPEAT_NONE:
                sample = Schedule(0, result.title, result.target_time,
                                  result.repeat_type, result.repeat_detail)
                repeat = f" ({sample.repeat_label})"
            lines.append(f"'{result.title}' — {when}{repeat}")

        self.refresh_schedules()
        if len(items) == 1:
            self.toast.show_text(f"‘{items[0].title}’ 등록됨", "success")
            return f"✅ {lines[0]} 으로 등록되었습니다."
        self.toast.show_text(f"일정 {len(items)}건 등록됨", "success")
        body = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
        return f"✅ 일정 {len(items)}건 등록 완료:\n{body}"

    def _tool_report(self, intent: ToolIntent) -> str:
        """"이번주 한 일" in the chat tab -- same text the dialog produces."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if intent.scope == "month":
            start = today.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1)
        else:
            monday = today - timedelta(days=today.weekday())
            if intent.scope == "last_week":
                monday -= timedelta(weeks=1)
            start, end = monday, monday + timedelta(days=7)
        body = self.db.work_report(start, end, include_open=False)
        return f"{body}\n\n(우클릭 → 업무 보고 만들기 에서 복사할 수 있습니다)"

    def _tool_list(self, intent: ToolIntent) -> str:
        now = datetime.now()
        rows = self.db.list_schedules(include_done=True, limit=300)
        scope = intent.scope

        if scope == "done":
            rows = [s for s in rows if s.is_done]
            header = "완료된 일정"
        else:
            rows = [s for s in rows if not s.is_done]
            if scope == "today":
                end = datetime.combine(now.date(), datetime.max.time())
                rows = [s for s in rows if s.target_time <= end]
                header = "오늘 일정"
            elif scope == "tomorrow":
                day = (now + timedelta(days=1)).date()
                rows = [s for s in rows if s.target_time.date() == day]
                header = "내일 일정"
            elif scope == "week":
                end = now + timedelta(days=7)
                rows = [s for s in rows if s.target_time <= end]
                header = "이번 주 일정"
            elif scope == "month":
                end = now + timedelta(days=31)
                rows = [s for s in rows if s.target_time <= end]
                header = "이번 달 일정"
            else:
                header = "전체 일정"

        if not rows:
            return f"☰ {header}이(가) 없습니다."

        rows.sort(key=lambda s: s.target_time)
        lines = [f"☰ {header} {len(rows)}건"]
        for schedule in rows[:12]:
            mark = "✔" if schedule.is_done else "•"
            weekday = WEEKDAY_NAMES_KO[schedule.target_time.weekday()]
            when = f"{schedule.target_time.strftime('%m/%d')}({weekday}) " \
                   f"{schedule.target_time.strftime('%H:%M')}"
            tag = f" [{schedule.repeat_label}]" if schedule.is_recurring else ""
            left = "" if schedule.is_done else f" · {humanize_countdown(schedule.seconds_left(now))}"
            lines.append(f"{mark} {when}  {schedule.title}{tag}{left}")
        if len(rows) > 12:
            lines.append(f"… 외 {len(rows) - 12}건")
        return "\n".join(lines)

    def _tool_clear(self, intent: ToolIntent) -> str:
        removed = self.db.clear_completed()
        self.refresh_schedules()
        if removed:
            self.toast.show_text(f"완료된 일정 {removed}건 정리", "success")
            return f"✔ 완료된 일정 {removed}건을 정리했습니다."
        return "✔ 정리할 완료 일정이 없습니다."

    def _tool_delete(self, intent: ToolIntent) -> Optional[str]:
        query = intent.query.strip()
        matches = [s for s in self.db.list_schedules(include_done=True, limit=300)
                   if query in s.title]
        if not matches:
            return None                      # let the model answer instead
        if len(matches) > 1:
            names = ", ".join(f"‘{s.title}’" for s in matches[:5])
            return (f"⚠ '{query}' 와(과) 일치하는 일정이 {len(matches)}건입니다: {names}\n"
                    "정확한 제목으로 다시 말씀해주세요.")
        target = matches[0]
        self.db.delete_schedule(target.id)
        self.refresh_schedules()
        self.toast.show_text(f"‘{target.title}’ 삭제됨", "info")
        return f"✕ '{target.title}' 일정을 삭제했습니다."

    def _tool_add_email(self, intent: ToolIntent) -> Optional[str]:
        keywords, action = intent.keywords.strip(), intent.action.strip()
        if len(keywords) < 2 or len(action) < 2:
            return None
        self.db.add_awaited_email(keywords, action)
        self.toast.show_text(f"메일 감지 등록: {keywords}", "success")
        note = ""
        if not getattr(self, "_outlook_status", (True, ""))[0]:
            # Be honest: the rule is saved, but nothing will fire until Outlook
            # is reachable.
            note = f"\n⚠ {self._outlook_status[1]} — Outlook 실행 후 감시가 시작됩니다."
        return (f"★ '{keywords}' 메일 수신 감지 알림이 등록되었습니다.\n"
                f"● 후속 업무: {action}{note}")

    def _tool_list_email(self, intent: ToolIntent) -> str:
        rules = self.db.list_awaited_emails(include_triggered=True)
        if not rules:
            return "☰ 등록된 메일 감지 알림이 없습니다."
        waiting = [r for r in rules if not r["is_triggered"] and r["is_active"]]
        lines = [f"☰ 메일 감지 알림 {len(rules)}건 (대기 중 {len(waiting)}건)"]
        for rule in rules[:12]:
            if rule["is_triggered"]:
                mark, state = "✔", " (수신 완료)"
            elif not rule["is_active"]:
                mark, state = "○", " (중지됨)"
            else:
                mark, state = "●", ""
            action = (rule["reminder_action"] or "").replace("\n", " / ")
            sender = f" · 발신자 {rule['sender_filter']}" if rule["sender_filter"] else ""
            lines.append(f"{mark} {rule['keywords']}{sender}{state}\n    → {action}")
        if len(rules) > 12:
            lines.append(f"… 외 {len(rules) - 12}건")
        available, message = getattr(self, "_outlook_status", (False, "확인 중"))
        lines.append(f"\nOutlook: {'연결됨' if available else message}")
        return "\n".join(lines)

    def _tool_delete_email(self, intent: ToolIntent) -> Optional[str]:
        rules = self.db.list_awaited_emails(include_triggered=True)
        if not rules:
            return "☰ 등록된 메일 감지 알림이 없습니다."

        query = intent.query.strip()
        if query:
            matches = [r for r in rules if query.lower() in r["keywords"].lower()]
        elif len(rules) == 1:
            matches = rules            # "메일 알림 삭제" with exactly one rule
        else:
            names = ", ".join(f"‘{r['keywords']}’" for r in rules[:5])
            return (f"⚠ 등록된 메일 감지 알림이 {len(rules)}건입니다: {names}\n"
                    "지울 규칙의 키워드를 함께 말씀해주세요. 예) '특약 메일 알림 삭제'")

        if not matches:
            return f"⚠ '{query}' 와(과) 일치하는 메일 감지 알림이 없습니다."
        if len(matches) > 1:
            names = ", ".join(f"‘{r['keywords']}’" for r in matches[:5])
            return (f"⚠ '{query}' 와(과) 일치하는 규칙이 {len(matches)}건입니다: {names}\n"
                    "정확한 키워드로 다시 말씀해주세요.")

        target = matches[0]
        self.db.delete_awaited_email(int(target["id"]))
        self.toast.show_text(f"메일 감지 삭제: {target['keywords']}", "info")
        return f"✕ '{target['keywords']}' 메일 감지 알림 규칙이 삭제되었습니다."

    def _on_model_note(self, message: str) -> None:
        """Advisory from the worker -- shown once per session, not per message."""
        if message in getattr(self, "_seen_notes", set()):
            return
        self._seen_notes = getattr(self, "_seen_notes", set()) | {message}
        self.toast.show_text(message, "warn", 9000)

    @guard("메시지 전송")
    def handle_chat_submit(self, text: str) -> None:
        if self._chat_request_id is not None:
            self.toast.show_text("이미 응답을 생성하는 중입니다", "warn", 1800)
            return

        self.chat_view.add_message("user", text)
        self.db.add_message("user", text)

        # Tool calling first: an instruction ("매월 12일 특약OS이월 추가해줘") is
        # executed against the database instead of being answered with prose.
        # This runs *before* the model checks on purpose -- adding or listing a
        # schedule is pure DB work and must keep working with no GGUF installed.
        intent = detect_tool_intent(text, self.llm.heuristic)
        if intent:
            reply = self.run_tool(intent)
            if reply:
                self.chat_view.add_message("ai", reply)
                self.db.add_message("ai", reply)
                self.chat_input.set_generating(False)
                return

        # Not an instruction -> a real conversation, which does need the model.
        if not self.llm.can_chat:
            notice = "AI 모델을 사용할 수 없어 대화는 어렵습니다. 일정 명령은 그대로 쓸 수 있어요. (설정 → AI)"
            self.chat_view.add_message("ai", notice)
            self.db.add_message("ai", notice)
            self.chat_input.set_generating(False)
            return
        if not self.llm.model_ready:
            # Lazy load: the worker loads the model before generating.
            self.toast.show_text("모델을 불러오는 중입니다… 잠시만 기다려주세요", "info", 4000)

        history = build_chat_context(
            self.db.recent_messages(40), max_turns=int(self.config.llm["history_turns"]))
        self.chat_view.start_stream()
        self.chat_input.set_generating(True)
        # Insert the AI row up-front so a crash mid-stream still leaves a record.
        self._chat_db_row = self.db.add_message("ai", "")
        self._chat_request_id = self.llm.send_chat(history)

    def on_chat_token(self, request_id: int, chunk: str) -> None:
        if request_id == self._chat_request_id:
            self.chat_view.append_token(chunk)

    def on_chat_thinking(self, request_id: int, thinking: bool) -> None:
        if request_id == self._chat_request_id:
            self.chat_view.set_thinking(thinking)

    @guard("응답 저장")
    def on_chat_finished(self, request_id: int, full_text: str) -> None:
        if request_id != self._chat_request_id:
            return
        # Getting here means no tool ran this turn, so the model cannot have
        # saved anything -- if it says it did, show the truth instead.
        corrected = correct_false_action_claim(full_text)
        if corrected is not full_text:
            log.warning("Model claimed a DB action that never happened: %r",
                        full_text[:200])
        text = self.chat_view.end_stream(corrected)
        self.chat_input.set_generating(False)
        self._chat_request_id = None
        if self._chat_db_row is not None:
            self.db.update_message(self._chat_db_row, text)
        self._chat_db_row = None

    @guard("응답 오류 처리", toast=False)
    def on_chat_error(self, request_id: int, message: str) -> None:
        if self._chat_request_id is not None and request_id != self._chat_request_id:
            return
        self.chat_view.end_stream(f"⚠ {message}")
        self.chat_input.set_generating(False)
        self._chat_request_id = None
        self._chat_db_row = None
        self.toast.show_text(message, "error", 5000)

    @guard("대화 기록 삭제")
    def clear_chat(self) -> None:
        """Purge chat history and reset the in-flight generation state.

        Old turns are replayed to the model as context, so a single bad answer
        keeps steering later ones. Clearing has to reset the live stream too,
        otherwise a finishing reply would write itself straight back in.
        """
        self.llm.cancel()
        self._chat_request_id = None
        self._chat_db_row = None
        removed = self.db.clear_chat()
        self.chat_view.clear()
        self.chat_input.set_generating(False)
        self.toast.show_text(f"대화 기록 {removed}건을 지웠습니다", "success")

    # ------------------------------------------------------------------ #
    # visibility / shutdown
    # ------------------------------------------------------------------ #

    def show_hud(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self._apply_opacity(immediate=True)

    def toggle_hud(self) -> None:
        self.hide_to_tray() if self.isVisible() else self.show_hud()

    def hide_to_tray(self) -> None:
        self.save_state()
        self.hide()
        if self.tray is not None and self._first_hide_notice:
            self._first_hide_notice = False
            self.tray.showMessage(
                "Offline Smart HUD",
                "트레이에서 계속 실행됩니다. 아이콘을 클릭하면 다시 열립니다.",
                make_app_icon(), 4000)

    def closeEvent(self, event) -> None:  # noqa: N802
        event.ignore()
        self.hide_to_tray()

    def report_error(self, title: str, detail: str) -> None:
        """Callback handed to crash_handler so background errors reach the UI."""
        try:
            self.toast.show_text(f"{title}: {detail}"[:120], "error", 6000)
        except Exception:                                # noqa: BLE001
            pass

    def shutdown(self) -> None:
        """Ordered teardown; every step is independently protected."""
        log.info("Shutting down…")
        for step, action in (
            ("save", self.save_state),
            ("timers", lambda: (self._tick_timer.stop(), self._hover_timer.stop())),
            ("scheduler", lambda: self.scheduler.stop(wait=False)),
            ("outlook", self.outlook.shutdown),
            ("llm", self.llm.shutdown),
            ("db", self.db.close),
        ):
            try:
                action()
            except Exception:                            # noqa: BLE001
                log.exception("Shutdown step %s failed", step)
        crash_handler.shutdown()
        log.info("Shutdown complete")


# --------------------------------------------------------------------------- #
# Tray
# --------------------------------------------------------------------------- #

def build_tray(window: HudWindow, app: QApplication) -> Optional[QSystemTrayIcon]:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.warning("System tray unavailable")
        return None
    tray = QSystemTrayIcon(make_app_icon(), app)
    tray.setToolTip("Offline Smart HUD")

    menu = QMenu()
    menu.addAction("표시 / 숨기기", window.toggle_hud)
    menu.addAction("일정 직접 추가",
                   lambda: (window.show_hud(), window.open_manual_dialog()))
    menu.addAction("설정…", lambda: (window.show_hud(), window.open_settings()))
    menu.addAction("대화 기록 지우기", window.clear_chat)
    menu.addSeparator()
    menu.addAction("기본 위치로", lambda: window.snap_to_corner(
        window.config.window["corner"]))
    menu.addSeparator()
    menu.addAction("종료", app.quit)
    tray.setContextMenu(menu)

    tray.activated.connect(
        lambda reason: window.toggle_hud()
        if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.messageClicked.connect(window.show_hud)
    tray.show()
    return tray


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def _fatal(message: str, detail: str = "") -> int:
    """Report a startup failure the user can actually read, then exit."""
    log.critical("%s %s", message, detail)
    try:
        QMessageBox.critical(None, f"{APP_NAME} - 시작 실패",
                             f"{message}\n\n{detail}\n\n자세한 내용은 logs 폴더를 확인해주세요.")
    except Exception:                                    # noqa: BLE001
        print(f"[{APP_NAME}] {message}\n{detail}", file=sys.stderr)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    base = app_dir()

    # llama.cpp drives a very high rate of small ctypes calls while loading and
    # generating. The default 5 ms GIL switch interval lets it monopolise the
    # interpreter and the UI stutters; 2 ms keeps the GUI thread responsive at
    # a negligible cost to throughput.
    sys.setswitchinterval(0.002)

    # User data lives OUTSIDE the program folder so the whole program folder
    # can be replaced to upgrade. On a locked-down machine the user cannot run
    # an upgrade script, so "delete folder, paste new folder" has to be safe.
    data = resolve_data_dir(base, argv)
    migrated = migrate_legacy_data(base, data)
    set_data_dir(data)          # models/ is looked up here first, then beside the exe

    # Business-day arithmetic. The shipped table only reaches so far and knows
    # nothing about company shutdown days, so holidays.txt in the data folder
    # overrides it -- write the commented template on first run.
    try:
        holiday_cal = holiday_calendar(data)
        holiday_cal.write_template(os.path.join(data, HOLIDAYS_FILENAME))
    except Exception:                                    # noqa: BLE001
        log.exception("Holiday calendar unavailable; business days may be off")

    # The GGUF lives in the data folder, so replacing the program folder to
    # upgrade never costs a 1 GB re-copy.
    ensure_models_dir()

    crash_handler.setup_logging(data, verbose="--debug" in argv)
    crash_handler.install(data, APP_NAME)
    log.info("=" * 58)
    log.info("Offline Smart HUD starting (python %s, frozen=%s)",
             sys.version.split()[0], getattr(sys, "frozen", False))
    log.info("Program folder: %s", base)
    log.info("Data folder   : %s%s", data, "  (same as program folder)"
             if os.path.abspath(data) == os.path.abspath(base) else "")
    if migrated:
        log.info("Migrated from the old layout: %s", ", ".join(migrated))

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setQuitOnLastWindowClosed(False)     # keep living in the tray
    # Fusion is the only style that does not paint native 3-D bevels and grey
    # frames underneath our QSS; without it the panel looks like a 90s dialog.
    try:
        app.setStyle("Fusion")
    except Exception:                        # noqa: BLE001
        log.debug("Fusion style unavailable", exc_info=True)

    # ---- config ---- #
    try:
        config = Config.load(data)
        if "--reset-config" in argv:
            config.reset()
            config.save()
            log.info("Config reset by command line")
        if "--safe" in argv:
            # Safe mode: opaque window, no preload -- for recovering from a
            # setting that made the HUD unusable.
            config.set("appearance", "idle_opacity", 1.0)
            config.set("appearance", "idle_panel_alpha", 1.0)
            config.set("appearance", "panel_alpha", 1.0)
            config.set("llm", "preload", False)
            config._sanitise()
            log.info("Safe mode enabled")
    except Exception:                                    # noqa: BLE001
        log.exception("Config load failed; using defaults")
        config = Config(data)

    set_style(build_style(config))
    app.setStyleSheet(build_stylesheet())
    app.setWindowIcon(make_app_icon())

    # ---- single instance ---- #
    from PySide6.QtCore import QLockFile

    # Lock in the data dir: two copies of the program folder must still count
    # as one instance, since they share the same database.
    lock = QLockFile(os.path.join(data, f"{APP_NAME}.lock"))
    lock.setStaleLockTime(30_000)
    if not lock.tryLock(150):
        QMessageBox.information(
            None, APP_NAME,
            "Offline Smart HUD가 이미 실행 중입니다.\n트레이 아이콘을 확인해주세요.")
        return 0

    # ---- database ---- #
    try:
        db = DatabaseManager(data)
    except Exception as exc:                             # noqa: BLE001
        return _fatal("데이터베이스를 열 수 없습니다.",
                      f"{exc}\n\n위치: {data}\n폴더 쓰기 권한을 확인해주세요.")

    # Daily snapshot before anything touches the data. Schedules and mail rules
    # live next to the executable, so a careless folder-replace upgrade would
    # otherwise take them with it. Cheap (a few hundred KB) and never fatal.
    if config.behavior.get("backup_on_start", True):
        db.backup_to(os.path.join(data, "backups"),
                     keep=int(config.behavior.get("backup_keep_days", 10)))

    # ---- window ---- #
    try:
        window = HudWindow(db, config)
    except Exception as exc:                             # noqa: BLE001
        log.exception("Window construction failed")
        crash_handler.write_crash_report(type(exc), exc, exc.__traceback__, "창 생성")
        return _fatal("창을 만들지 못했습니다.",
                      f"{exc}\n\n'--reset-config' 옵션으로 다시 실행하면 "
                      "설정을 초기화할 수 있습니다.")

    crash_handler.set_reporter(window.report_error)
    window.tray = build_tray(window, app)

    if config.window["start_minimized"] and window.tray is not None:
        window.hide()
        log.info("Started minimised to tray")
    else:
        window.show_hud()

    window.start_services()

    model = find_model_path(config.llm["model_path"])
    log.info("Model path: %s", model or "(none)")
    if model is None:
        window.toast.show_text(
            f"AI 모델이 없어 채팅은 비활성화 상태입니다 (일정 기능은 정상)\n"
            f"모델을 넣을 곳: {models_dir()}", "warn", 8000)
    elif is_volatile_model(model):
        # It works, but the next folder swap deletes it. Say so once, with the
        # destination spelled out -- AppData is hidden, so "move it" is not
        # actionable without the path.
        log.warning("Model sits in the program folder and will be lost on "
                    "upgrade: %s", model)
        window.toast.show_text(
            f"모델이 프로그램 폴더에 있습니다. 업그레이드 시 사라집니다.\n"
            f"이곳으로 옮기세요: {models_dir()}", "warn", 12000)

    app.aboutToQuit.connect(window.shutdown)
    log.info("Event loop starting")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
