"""
ui_components.py
================
Every custom widget in the HUD. Nothing here touches the database, scheduler or
LLM -- widgets expose signals and setters; ``main.py`` wires them together.

Two rules the whole file obeys
------------------------------
1. **Everything is driven by** :func:`config.style`. Colours, paddings, row
   heights and font sizes all come from the active :class:`~config.Style`, so a
   settings change restyles the app without a restart (see :func:`restyle_tree`).
2. **Text is never translucent.** Only panels, borders and chip fills carry
   alpha. Earlier builds tinted labels with alpha colours *and* lowered the
   window opacity; the two multiplied and the text vanished over bright
   wallpapers. Idle subtlety now comes from the panel fill and the window
   opacity alone, both of which have readability floors.

Preview the whole kit with ``py ui_components.py``.
"""

from __future__ import annotations

import math
import os
import struct
import wave
from datetime import datetime, timedelta
from typing import Iterable, Optional

from PySide6.QtCore import (
    QTime,
    Property,
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QIcon,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTimeEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import ACCENTS, THEMES, Config, Style, build_style, set_style, style
from db_manager import (
    REPEAT_DAILY,
    REPEAT_MONTHLY,
    REPEAT_NONE,
    REPEAT_WEEKLY,
    WEEKDAY_NAMES_KO,
    Schedule,
    format_weekdays,
    parse_month_day,
    parse_weekdays,
)
from scheduler_service import humanize_countdown


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Idle ghost fade
# --------------------------------------------------------------------------- #
# Lowering only the window opacity was not enough: the inner cards keep their
# own slate-800 fill, so at rest the HUD read as a column of dark blocks
# floating over a bright desktop. The fills have to fade *with* the panel.
#
# `_IDLE_FADE` runs 0..1 (0 = fully idle/ghost, 1 = engaged) and is driven by
# the same animation that fades the panel background, so everything moves
# together. Text is never faded -- only fills and hairlines.

_IDLE_FADE = 1.0


def idle_fade() -> float:
    return _IDLE_FADE


def set_idle_fade(value: float) -> bool:
    """Set the engagement level. Returns True when it actually changed."""
    global _IDLE_FADE
    value = max(0.0, min(1.0, float(value)))
    if abs(value - _IDLE_FADE) < 0.02:
        return False
    _IDLE_FADE = value
    return True


def faded(color: QColor, floor: float = 0.0) -> QColor:
    """Scale a fill/border alpha by the current engagement level.

    ``floor`` keeps a minimum fraction of the alpha so an element never becomes
    completely invisible (used for borders, which give the cards their shape).
    """
    out = QColor(color)
    scale = floor + (1.0 - floor) * _IDLE_FADE
    out.setAlpha(max(0, min(255, int(out.alpha() * scale))))
    return out


def repaint_faded(root: QWidget) -> None:
    """Ask every fade-aware widget under ``root`` to redraw."""
    for widget in root.findChildren(QWidget):
        if isinstance(widget, (SoftCard, ChatBubble)):
            widget.update()


def chip_color(repeat_type: str) -> QColor:
    s = style()
    return {
        REPEAT_NONE: s.text_dim,
        REPEAT_DAILY: s.success,
        REPEAT_WEEKLY: s.accent,
        REPEAT_MONTHLY: QColor(167, 139, 250) if not s.is_light else QColor(109, 40, 217),
    }.get(repeat_type, s.text_dim)


def restyle_tree(root: QWidget) -> None:
    """Re-apply the active style to ``root`` and every descendant that knows how."""
    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        handler = getattr(widget, "apply_style", None)
        if callable(handler):
            try:
                handler()
            except Exception:                       # noqa: BLE001 - never break restyling
                pass
    root.update()


def build_stylesheet(s: Optional[Style] = None) -> str:
    """Global QSS derived from the active style.

    Every native control is fully repainted here. Qt's Windows style otherwise
    draws 3-D bevels, grey frames and 16-px scrollbars that make the panel look
    like a 1990s dialog sitting on top of a glass card. Pair this with
    ``QApplication.setStyle("Fusion")`` so no platform theme paints underneath.

    Contract: backgrounds and borders carry alpha, **text never does**.
    """
    s = s or style()
    css = s.css
    r_card = s.card_radius                      # 12 by default
    r_ctl = max(8, s.card_radius - 2)
    r_pill = s.ctl_h // 2                       # fully rounded inputs
    focus_bg = s.alpha(s.softer, min(255, s.softer.alpha() + 45))
    hover_bg = s.alpha(s.softer, min(255, s.softer.alpha() + 30))
    return f"""
    /* ===== base ============================================== */
    /* 'Segoe UI Emoji'/'Segoe UI Symbol' must stay in the stack: Malgun Gothic
       has no pictographs, so without them every emoji renders as a .notdef
       box (📅 일정 showed up as "■ 일정"). Qt falls through the list per glyph. */
    * {{
        font-family: 'Malgun Gothic', 'Segoe UI Variable', 'Segoe UI',
                     'Segoe UI Emoji', 'Segoe UI Symbol', 'Noto Sans KR', sans-serif;
        color: {css(s.text)};
        outline: 0;
        border: 0;
    }}
    QWidget {{ background: transparent; }}
    QDialog, QMainWindow {{ background: transparent; }}

    QToolTip {{
        background: {css(s.bg, 250)};
        color: {css(s.text)};
        border: 1px solid {css(s.line_strong)};
        border-radius: 8px;
        padding: 5px 9px;
        font-size: {s.f_sm}px;
    }}

    /* ===== tabs (flat, underline indicator) ================== */
    QTabWidget::pane {{ border: 0; background: transparent; }}
    QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
    QTabBar::tab {{
        background: transparent;
        color: {css(s.text_dim)};
        border: 0;
        border-bottom: 2px solid transparent;
        padding: {max(4, s.gap + 1)}px {s.pad + 4}px;
        margin-right: 2px;
        font-size: {s.f_sm}px;
        font-weight: 600;
    }}
    QTabBar::tab:hover {{ color: {css(s.text)}; }}
    QTabBar::tab:selected {{
        color: {css(s.accent)};
        border-bottom: 2px solid {css(s.accent)};
    }}
    QTabBar::tab:focus {{ border: 0; border-bottom: 2px solid {css(s.accent)}; }}

    /* ===== inputs (flat, rounded, accent focus ring) ========= */
    QLineEdit, QPlainTextEdit, QTextEdit, QDateTimeEdit, QComboBox, QSpinBox {{
        background: {css(s.softer)};
        border: 1px solid {css(s.line)};
        border-radius: {r_ctl}px;
        padding: {max(2, s.gap - 1)}px {s.pad + 2}px;
        selection-background-color: {css(s.accent, 160)};
        selection-color: {css(s.on_accent)};
        font-size: {s.f_md}px;
    }}
    QLineEdit:hover, QPlainTextEdit:hover, QDateTimeEdit:hover,
    QComboBox:hover, QSpinBox:hover {{ border-color: {css(s.line_strong)}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QDateTimeEdit:focus,
    QComboBox:focus, QSpinBox:focus {{
        border: 1px solid {css(s.accent, 200)};
        background: {css(focus_bg)};
    }}
    QLineEdit:disabled, QPlainTextEdit:disabled {{
        color: {css(s.text_dim)}; background: {css(s.softer, 70)};
    }}
    QLineEdit#quickadd {{
        border-radius: {r_pill}px;
        padding-left: {s.pad + 6}px;
    }}

    /* combo / spin: no native arrows, no bevels */
    QComboBox::drop-down, QComboBox::down-arrow {{
        border: 0; background: transparent; width: 16px;
    }}
    QComboBox QAbstractItemView {{
        background: {css(s.bg, 252)};
        border: 1px solid {css(s.line_strong)};
        border-radius: {r_ctl}px;
        selection-background-color: {css(s.accent, 120)};
        selection-color: {css(s.on_accent)};
        padding: 3px;
        outline: 0;
    }}
    QSpinBox::up-button, QSpinBox::down-button,
    QDateTimeEdit::up-button, QDateTimeEdit::down-button {{
        width: 14px; border: 0; background: transparent;
    }}
    QSpinBox::up-arrow, QSpinBox::down-arrow,
    QDateTimeEdit::up-arrow, QDateTimeEdit::down-arrow {{
        width: 7px; height: 7px; background: {css(s.text_dim)};
        border-radius: 1px;
    }}

    /* calendar popup */
    QCalendarWidget QWidget {{
        alternate-background-color: {css(s.soft, 230)};
        background: {css(s.bg, 252)};
    }}
    QCalendarWidget QAbstractItemView:enabled {{
        background: {css(s.bg, 252)};
        color: {css(s.text)};
        selection-background-color: {css(s.accent, 170)};
        selection-color: {css(s.on_accent)};
        outline: 0;
    }}
    QCalendarWidget QAbstractItemView:disabled {{ color: {css(s.text_dim, 110)}; }}
    QCalendarWidget QToolButton {{
        color: {css(s.text)}; background: transparent;
        border: 0; border-radius: 6px; padding: 3px 8px;
    }}
    QCalendarWidget QToolButton:hover {{ background: {css(s.accent, 60)}; }}
    QCalendarWidget QSpinBox {{ background: {css(s.softer)}; }}

    /* ===== buttons =========================================== */
    QPushButton {{
        background: {css(s.softer)};
        border: 1px solid {css(s.line)};
        border-radius: {r_ctl}px;
        padding: {max(3, s.gap)}px {s.pad + 4}px;
        font-size: {s.f_sm}px;
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {css(hover_bg)}; border-color: {css(s.accent, 130)}; }}
    QPushButton:pressed {{ background: {css(s.accent, 90)}; }}
    QPushButton:disabled {{ color: {css(s.text_dim, 130)}; background: {css(s.softer, 60)}; }}
    QPushButton#primary {{
        background: {css(s.accent, 215)};
        border: 1px solid {css(s.accent)};
        color: {css(s.on_accent)};
    }}
    QPushButton#primary:hover {{ background: {css(s.accent)}; }}
    QPushButton#primary:pressed {{ background: {css(s.accent.darker(115))}; }}
    QPushButton#danger {{ color: {css(s.danger)}; border-color: {css(s.danger, 120)}; }}
    QPushButton#danger:hover {{ background: {css(s.danger, 80)}; color: #FFFFFF; }}
    QPushButton#ghost {{ background: transparent; border: 1px solid {css(s.line)}; }}
    QPushButton#ghost:hover {{ background: {css(s.accent, 45)}; }}

    /* ===== checkbox / slider ================================= */
    QCheckBox {{ font-size: {s.f_sm}px; spacing: 7px; background: transparent; }}
    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border: 1px solid {css(s.line_strong)};
        border-radius: 5px;
        background: {css(s.softer)};
    }}
    QCheckBox::indicator:hover {{ border-color: {css(s.accent)}; }}
    QCheckBox::indicator:checked {{
        background: {css(s.accent)}; border-color: {css(s.accent)};
    }}

    QSlider {{ background: transparent; }}
    QSlider::groove:horizontal {{
        height: 4px; background: {css(s.line, 90)}; border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{ background: {css(s.accent)}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        width: 13px; height: 13px; margin: -5px 0;
        background: {css(s.accent)}; border: 0; border-radius: 7px;
    }}
    QSlider::handle:horizontal:hover {{ background: {css(s.accent.lighter(115))}; }}

    /* ===== scrollbars (thin, floating) ======================= */
    QScrollArea, QAbstractScrollArea {{ background: transparent; border: 0; }}
    QAbstractScrollArea::corner {{ background: transparent; border: 0; }}
    QScrollBar:vertical {{
        background: transparent; width: 7px; margin: 2px 1px 2px 0; border: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {css(s.line, 105)}; border-radius: 3px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {css(s.accent, 190)}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0; width: 0; border: 0; background: transparent;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QScrollBar:horizontal {{ height: 0px; background: transparent; }}

    /* ===== menus ============================================= */
    QMenu {{
        background: {css(s.bg, 252)};
        border: 1px solid {css(s.line_strong)};
        border-radius: {r_card}px;
        padding: 5px;
    }}
    QMenu::item {{
        padding: 6px 20px 6px 12px;
        border-radius: 7px;
        font-size: {s.f_sm}px;
        color: {css(s.text)};
    }}
    QMenu::item:selected {{ background: {css(s.accent, 110)}; color: {css(s.on_accent)}; }}
    QMenu::item:disabled {{ color: {css(s.text_dim, 120)}; }}
    QMenu::separator {{ height: 1px; background: {css(s.line, 70)}; margin: 5px 8px; }}
    QMenu::indicator {{ width: 13px; height: 13px; left: 7px; }}

    /* ===== message boxes (native grey otherwise) ============= */
    QMessageBox {{ background: {css(s.bg, 252)}; }}
    QMessageBox QLabel {{ color: {css(s.text)}; font-size: {s.f_md}px; }}
    QMessageBox QPushButton {{ min-width: 68px; }}

    /* ===== misc ============================================== */
    QLabel {{ background: transparent; }}
    QLabel#muted   {{ color: {css(s.text_dim)}; font-size: {s.f_xs}px; }}
    QLabel#heading {{ color: {css(s.text)}; font-size: {s.f_lg}px; font-weight: 700; }}
    QLabel#section {{ color: {css(s.accent)}; font-size: {s.f_xs}px; font-weight: 700; }}
    QSizeGrip {{ background: transparent; image: none; width: 12px; height: 12px; }}
    """


# --------------------------------------------------------------------------- #
# Glass chrome
# --------------------------------------------------------------------------- #

class GlassPanel(QFrame):
    """Frosted root panel: rounded fill, alarm glow, and an idle/active fade.

    Two animatable properties:

    * ``glow``   0..1 -- accent halo used for alarms
    * ``active`` 0..1 -- 0 = idle (very see-through), 1 = engaged (solid)
    """

    def __init__(self, parent: Optional[QWidget] = None, radius: Optional[int] = None,
                 margin: Optional[int] = None) -> None:
        super().__init__(parent)
        s = style()
        self._radius = radius if radius is not None else s.radius
        self._explicit_radius = radius
        self._margin = s.glow_margin if margin is None else margin
        self._glow = 0.0
        self._active = 1.0
        self._glow_color = QColor(s.accent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._glow_anim = QPropertyAnimation(self, b"glow", self)
        self._glow_anim.setEasingCurve(QEasingCurve.InOutSine)
        self._fade_anim = QPropertyAnimation(self, b"active", self)
        self._fade_anim.setDuration(180)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

    # -- properties -------------------------------------------------------- #

    def get_glow(self) -> float:
        return self._glow

    def set_glow(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._glow) > 0.004:
            self._glow = value
            self.update()

    glow = Property(float, get_glow, set_glow)

    def get_active(self) -> float:
        return self._active

    def set_active_value(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._active) > 0.004:
            self._active = value
            # Inner cards fade on the same curve as the panel fill, so the HUD
            # dissolves as one object rather than leaving dark blocks behind.
            floor = style().idle_card_fade
            if set_idle_fade(floor + (1.0 - floor) * value):
                repaint_faded(self)
            self.update()

    active = Property(float, get_active, set_active_value)

    # -- api ---------------------------------------------------------------- #

    def apply_style(self) -> None:
        s = style()
        if self._explicit_radius is None:
            self._radius = s.radius
        self._margin = s.glow_margin
        self.update()

    def content_margin(self) -> int:
        return self._margin

    def fade_to(self, active: bool) -> None:
        """Animate between the idle and engaged background fills."""
        target = 1.0 if active else 0.0
        if abs(self._active - target) < 0.01:
            return
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self._active)
        self._fade_anim.setEndValue(target)
        self._fade_anim.start()

    def flash_alert(self, color: Optional[QColor] = None, pulses: int = 3,
                    duration_ms: int = 620) -> None:
        if pulses <= 0:
            return
        self._glow_color = QColor(color or style().accent)
        self._glow_anim.stop()
        self._glow_anim.setDuration(duration_ms)
        self._glow_anim.setStartValue(0.0)
        self._glow_anim.setKeyValueAt(0.5, 1.0)
        self._glow_anim.setEndValue(0.0)
        self._glow_anim.setLoopCount(pulses)
        self._glow_anim.start()

    def stop_alert(self) -> None:
        self._glow_anim.stop()
        self.set_glow(0.0)

    # -- painting ------------------------------------------------------------ #

    def paintEvent(self, event) -> None:  # noqa: N802
        s = style()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        m = self._margin
        rect = QRectF(self.rect()).adjusted(m, m, -m, -m)
        if rect.width() <= 1 or rect.height() <= 1:
            return

        if self._glow > 0.01:
            for i in range(4, 0, -1):
                alpha = int(46 * self._glow / i)
                if alpha <= 0:
                    continue
                pen = QColor(self._glow_color)
                pen.setAlpha(alpha)
                painter.setPen(QPen(pen, i * 2.2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(-i * 1.3, -i * 1.3, i * 1.3, i * 1.3),
                                        self._radius + i, self._radius + i)

        # Fill lerps between the idle and active alphas.
        t = self._active
        fill = QColor(s.bg)
        fill.setAlpha(int(s.bg_idle.alpha() + (s.bg.alpha() - s.bg_idle.alpha()) * t))
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, self._radius, self._radius)

        base = QColor(s.line_strong)
        g = self._glow
        border = QColor(
            int(base.red() + (self._glow_color.red() - base.red()) * g),
            int(base.green() + (self._glow_color.green() - base.green()) * g),
            int(base.blue() + (self._glow_color.blue() - base.blue()) * g),
            int(base.alpha() * (0.55 + 0.45 * t) + (255 - base.alpha()) * g),
        )
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(border, 1.0 + g))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), self._radius, self._radius)


class SoftCard(QFrame):
    """Lighter inner card used for rows, bubbles and the notification."""

    def __init__(self, parent: Optional[QWidget] = None, radius: Optional[int] = None,
                 fill: Optional[QColor] = None, border: Optional[QColor] = None) -> None:
        super().__init__(parent)
        self._radius = radius
        self._fill = QColor(fill) if fill else None
        self._border = QColor(border) if border else None
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_colors(self, fill: Optional[QColor] = None, border: Optional[QColor] = None) -> None:
        self._fill = QColor(fill) if fill is not None else None
        self._border = QColor(border) if border is not None else None
        self.update()

    def apply_style(self) -> None:
        self.update()

    #: Cards fade with the panel, but keep a sliver of border so shapes stay
    #: legible at rest instead of dissolving into a text-only smear.
    FADE_FILL_FLOOR = 0.12
    FADE_BORDER_FLOOR = 0.30

    def paintEvent(self, event) -> None:  # noqa: N802
        s = style()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self._radius if self._radius is not None else s.card_radius
        painter.setPen(QPen(faded(self._border or s.line, self.FADE_BORDER_FLOOR), 1.0))
        painter.setBrush(faded(self._fill or s.soft, self.FADE_FILL_FLOOR))
        painter.drawRoundedRect(rect, radius, radius)


def _holiday_note() -> str:
    """Coverage of the bundled holiday table, for the settings hint."""
    try:
        from holidays import calendar as _cal
        return _cal().coverage_note()
    except Exception:                                # noqa: BLE001
        return ""


class IconButton(QPushButton):
    """Small glyph button sized from the active density."""

    def __init__(self, glyph: str, tooltip: str = "", parent: Optional[QWidget] = None,
                 role: str = "normal", checkable: bool = False,
                 size: Optional[int] = None) -> None:
        super().__init__(glyph, parent)
        self._role = role
        self._size_override = size
        self.setCheckable(checkable)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setFocusPolicy(Qt.NoFocus)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        size = self._size_override or max(18, s.ctl_h - 4)
        hover = s.danger if self._role == "danger" else s.accent
        self.setFixedSize(size, size)
        # 0.62, not 0.52: at compact density the button is only 20 px, and a
        # 10 px pictograph turns into an unreadable smudge. Bumping the ratio
        # costs no layout space because the glyph is centred in a fixed box.
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent; border: none;
                border-radius: {size // 2}px;
                color: {s.css(s.text_dim)};
                font-size: {max(11, int(size * 0.62))}px;
                padding: 0;
            }}
            QPushButton:hover {{
                background: {s.css(hover, 55)}; color: {s.css(hover)};
            }}
            QPushButton:pressed {{ background: {s.css(hover, 95)}; }}
            QPushButton:checked {{ background: {s.css(hover, 65)}; color: {s.css(hover)}; }}
        """)


class TitleBar(QWidget):
    """The entire window chrome, on one row.

    ``[● 15:04] [일정 | AI] ────────── [⚿ ⚑ ⚙ ▴ — ✕]``

    The tab strip lives *inside* the header instead of under it. A separate
    QTabBar underneath cost ~26 px of pure chrome for two words; inlining it
    makes the header the only non-content row in the window.
    """

    drag_started = Signal(QPoint)
    drag_moved = Signal(QPoint)
    drag_finished = Signal()
    collapse_toggled = Signal(bool)
    settings_requested = Signal()
    minimize_requested = Signal()
    close_requested = Signal()
    tab_selected = Signal(int)
    lock_toggled = Signal(bool)
    pin_toggled = Signal(bool)

    TABS = ("일정", "AI")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pressed = False
        self._collapsed = False

        self.layout_ = QHBoxLayout(self)
        self.layout_.setSpacing(2)

        self.dot = QLabel("●")
        self.dot.setToolTip("로컬 모델 상태")
        self.layout_.addWidget(self.dot)

        self.clock = QLabel("--:--")
        self.layout_.addWidget(self.clock)

        self.badge = QLabel("")
        self.badge.setVisible(False)
        self.layout_.addWidget(self.badge)

        # Inline tab strip
        self.tab_buttons: list[QPushButton] = []
        for index, label in enumerate(self.TABS):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, i=index: self.select_tab(i))
            self.tab_buttons.append(btn)
            self.layout_.addWidget(btn)
        self.tab_buttons[0].setChecked(True)
        self.layout_.addStretch(1)

        # ⊘ rather than a padlock: no BMP padlock exists, and ⚿ (key) collapses
        # into an unreadable blob at 12 px. A circle-slash stays crisp, and the
        # checked highlight carries the on/off state anyway.
        self.lock_btn = IconButton("⊘", "위치 잠금", checkable=True)
        self.pin_btn = IconButton("⚑", "항상 위에 표시", checkable=True)
        self.collapse_btn = IconButton("▴", "접기 / 펼치기", checkable=True)
        self.settings_btn = IconButton("⚙", "설정")
        self.min_btn = IconButton("—", "트레이로 숨기기")
        self.close_btn = IconButton("✕", "닫기 (트레이로 이동)", role="danger")
        for btn in (self.lock_btn, self.pin_btn, self.collapse_btn,
                    self.settings_btn, self.min_btn, self.close_btn):
            self.layout_.addWidget(btn)

        self.lock_btn.toggled.connect(self.lock_toggled)
        self.pin_btn.toggled.connect(self.pin_toggled)
        self.collapse_btn.toggled.connect(self._on_collapse)
        self.settings_btn.clicked.connect(self.settings_requested)
        self.min_btn.clicked.connect(self.minimize_requested)
        self.close_btn.clicked.connect(self.close_requested)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.setFixedHeight(s.title_h)
        self.layout_.setContentsMargins(2, 0, 0, 0)
        self.layout_.setSpacing(2)
        self.dot.setStyleSheet(f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px;")
        self.clock.setStyleSheet(
            f"color: {s.css(s.text)}; font-size: {s.f_sm}px; font-weight: 700;")
        self.badge.setStyleSheet(
            f"color: {s.css(s.on_accent)}; background: {s.css(s.accent)};"
            f"border-radius: {max(5, s.f_xs // 2 + 2)}px; padding: 0px 4px;"
            f"font-size: {s.f_xs}px; font-weight: 700;")
        height = max(15, s.title_h - 8)
        for btn in self.tab_buttons:
            btn.setFixedHeight(height)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; border: 0;
                    border-radius: {height // 2}px;
                    padding: 0px {max(5, s.pad + 2)}px; margin-left: 2px;
                    font-size: {s.f_sm}px; font-weight: 700;
                    color: {s.css(s.text_dim)};
                }}
                QPushButton:hover {{ color: {s.css(s.text)};
                                     background: {s.css(s.softer, 90)}; }}
                QPushButton:checked {{
                    background: {s.css(s.accent, 58)};
                    color: {s.css(s.accent)};
                }}
            """)

    # -- tabs ---------------------------------------------------------------- #

    def select_tab(self, index: int) -> None:
        self.set_tab_index(index)
        self.tab_selected.emit(index)

    def set_tab_index(self, index: int) -> None:
        for i, btn in enumerate(self.tab_buttons):
            btn.blockSignals(True)
            btn.setChecked(i == index)
            btn.blockSignals(False)

    def set_locked(self, locked: bool) -> None:
        self.lock_btn.blockSignals(True)
        self.lock_btn.setChecked(bool(locked))
        self.lock_btn.blockSignals(False)

    def set_pinned(self, pinned: bool) -> None:
        self.pin_btn.blockSignals(True)
        self.pin_btn.setChecked(bool(pinned))
        self.pin_btn.blockSignals(False)

    # -- state ------------------------------------------------------------- #

    def _on_collapse(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.collapse_btn.setText("▾" if collapsed else "▴")
        self.collapse_toggled.emit(collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapse_btn.blockSignals(True)
        self.collapse_btn.setChecked(collapsed)
        self.collapse_btn.setText("▾" if collapsed else "▴")
        self._collapsed = collapsed
        self.collapse_btn.blockSignals(False)

    def set_model_state(self, state: str, tooltip: str = "") -> None:
        s = style()
        color = {
            "ready": s.success, "loading": s.warn, "missing": s.text_dim,
            "unavailable": s.danger, "error": s.danger, "oom": s.danger,
        }.get(state, s.text_dim)
        self.dot.setStyleSheet(f"color: {s.css(color)}; font-size: {s.f_xs}px;")
        self.dot.setToolTip(tooltip or state)

    def set_clock(self, text: str) -> None:
        self.clock.setText(text)
        # The title bar is a single tight row; with "초 단위 시계" on, its
        # natural width exceeds the default 290 px window and the layout takes
        # the space back out of the clock, chopping the seconds off. Pinning
        # the clock to what it needs makes the stretch give way instead.
        self.clock.setMinimumWidth(
            QFontMetrics(self.clock.font()).horizontalAdvance(text or "00:00"))

    def set_badge(self, text: str) -> None:
        """Small count pill (e.g. overdue items) shown next to the clock."""
        self.badge.setText(text)
        self.badge.setVisible(bool(text))

    # -- dragging ----------------------------------------------------------- #

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._pressed = True
            self.drag_started.emit(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self.drag_moved.emit(event.globalPosition().toPoint())
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self._pressed = False
            self.drag_finished.emit()
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.collapse_btn.toggle()
        event.accept()


# --------------------------------------------------------------------------- #
# Small parts
# --------------------------------------------------------------------------- #

class ElidedLabel(QLabel):
    """QLabel that elides instead of forcing the layout wider."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        metrics = QFontMetrics(self.font())
        shown = metrics.elidedText(self._full, Qt.ElideRight, max(20, self.width()))
        super().setText(shown)
        # A tooltip only earns its keep when something is actually hidden --
        # otherwise every label in the panel sprouts a redundant popup.
        self.setToolTip(self._full if shown != self._full else "")


class TagChip(QLabel):
    """Pill label for recurrence tags. Opaque text, tinted background."""

    def __init__(self, text: str, color: Optional[QColor] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._color = QColor(color or style().accent)
        self.setAlignment(Qt.AlignCenter)
        self.apply_style()

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        # On light themes a 15%-alpha tint plus coloured text is too faint, so
        # darken the text instead of relying on the fill.
        text_color = self._color.darker(150) if s.is_light else self._color
        self.setStyleSheet(f"""
            background: {s.css(self._color, 46)};
            color: {s.css(text_color, 255)};
            border: 1px solid {s.css(self._color, 105)};
            border-radius: {max(4, s.f_xs // 2 + 2)}px;
            padding: 0px 5px;
            font-size: {s.f_xs}px;
            font-weight: 700;
        """)


class CheckCircle(QAbstractButton):
    """Hand-painted round checkbox -- crisper than a QSS indicator."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._hovered = False
        self.apply_style()

    def apply_style(self) -> None:
        size = max(14, style().f_md + 5)
        self.setFixedSize(size, size)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.size()

    def paintEvent(self, event) -> None:  # noqa: N802
        s = style()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.2, 1.2, -1.2, -1.2)
        if self.isChecked():
            painter.setPen(QPen(s.success, 1.3))
            painter.setBrush(s.alpha(s.success, 80))
            painter.drawEllipse(rect)
            tick = QPainterPath()
            tick.moveTo(rect.left() + rect.width() * 0.26, rect.top() + rect.height() * 0.52)
            tick.lineTo(rect.left() + rect.width() * 0.44, rect.top() + rect.height() * 0.70)
            tick.lineTo(rect.left() + rect.width() * 0.76, rect.top() + rect.height() * 0.30)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(s.success, 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPath(tick)
        else:
            painter.setPen(QPen(s.accent if self._hovered else s.line_strong, 1.3))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(rect)


class BusyDots(QWidget):
    """Three-dot activity indicator; animates only while shown."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._phase = 0.0
        self.setFixedSize(26, 12)
        self._timer = QTimer(self)
        self._timer.setInterval(95)
        self._timer.timeout.connect(self._advance)

    def _advance(self) -> None:
        self._phase = (self._phase + 0.24) % (2 * math.pi)
        self.update()

    def start(self) -> None:
        self.show()
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        accent = style().accent
        for i in range(3):
            wave = (math.sin(self._phase - i * 0.9) + 1) / 2
            color = QColor(accent)
            color.setAlpha(int(90 + 165 * wave))
            radius = 1.9 + wave * 1.2
            cx = 5 + i * 8
            painter.setBrush(color)
            painter.drawEllipse(QRectF(cx - radius, 6 - radius, radius * 2, radius * 2))


# --------------------------------------------------------------------------- #
# Tab 1 -- schedules
# --------------------------------------------------------------------------- #

class QuickAddBar(QWidget):
    """Natural-language input row."""

    submitted = Signal(str)
    manual_requested = Signal()

    PLACEHOLDER = "매주 월요일 10시 회의 · 내일 3시 치과 · 30분 뒤 휴식"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)

        self.input = QLineEdit()
        self.input.setObjectName("quickadd")      # pill styling in the QSS
        self.input.setPlaceholderText(self.PLACEHOLDER)
        self.input.setClearButtonEnabled(True)
        self.input.returnPressed.connect(self._submit)
        self.row.addWidget(self.input, 1)

        self.busy = BusyDots()
        self.busy.hide()
        self.row.addWidget(self.busy)

        self.manual_btn = IconButton("▤", "직접 입력 (Ctrl+N)")
        self.manual_btn.clicked.connect(self.manual_requested)
        self.row.addWidget(self.manual_btn)

        self.add_btn = IconButton("＋", "추가 (Enter)")
        self.add_btn.clicked.connect(self._submit)
        self.row.addWidget(self.add_btn)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.row.setSpacing(max(2, s.gap - 1))
        self.input.setFixedHeight(s.ctl_h)
        self.add_btn.setStyleSheet(self.add_btn.styleSheet() + f"""
            QPushButton {{ color: {s.css(s.accent)}; font-weight: 700; }}
        """)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if text:
            self.submitted.emit(text)

    def set_busy(self, busy: bool) -> None:
        self.add_btn.setEnabled(not busy)
        self.input.setEnabled(not busy)
        self.busy.start() if busy else self.busy.stop()
        if not busy:
            self.input.setFocus()

    def clear(self) -> None:
        self.input.clear()

    def text(self) -> str:
        return self.input.text().strip()


#: Filter keys for :class:`ScheduleFilterBar` / ``HudWindow.refresh_schedules``.
FILTER_ALL = "all"
FILTER_TODAY = "today"
FILTER_RECURRING = "recurring"


class ScheduleFilterBar(QWidget):
    """Compact segmented pills above the list: 전체 / 오늘 / 반복 업무 ↻."""

    changed = Signal(str)

    OPTIONS = (
        ("전체", FILTER_ALL, "모든 일정"),
        ("오늘", FILTER_TODAY, "오늘 안에 예정된 일정과 지난 일정"),
        ("반복 ↻", FILTER_RECURRING, "매일·매주·매월 반복되는 업무만"),
    )

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._current = FILTER_ALL
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)

        self.buttons: dict[str, QPushButton] = {}
        for label, key, tip in self.OPTIONS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, k=key: self.set_current(k))
            self.buttons[key] = btn
            self.row.addWidget(btn)
        self.row.addStretch(1)

        self.count_label = QLabel("")
        self.row.addWidget(self.count_label)

        self.buttons[FILTER_ALL].setChecked(True)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.row.setSpacing(3)
        radius = max(7, s.card_radius - 3)
        for btn in self.buttons.values():
            btn.setFixedHeight(max(17, s.ctl_h - 8))
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    border: 1px solid {s.css(s.line, 70)};
                    border-radius: {radius}px;
                    padding: 0px {max(6, s.pad)}px;
                    font-size: {s.f_xs}px; font-weight: 600;
                    color: {s.css(s.text_dim)};
                }}
                QPushButton:hover {{ border-color: {s.css(s.accent, 140)};
                                     color: {s.css(s.text)}; }}
                QPushButton:checked {{
                    background: {s.css(s.accent, 55)};
                    border-color: {s.css(s.accent, 150)};
                    color: {s.css(s.accent)};
                }}
            """)
        self.count_label.setStyleSheet(
            f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px;")

    # -- api ---------------------------------------------------------------- #

    def current(self) -> str:
        return self._current

    def set_current(self, key: str, notify: bool = True) -> None:
        if key not in self.buttons:
            key = FILTER_ALL
        self._current = key
        for name, btn in self.buttons.items():
            btn.blockSignals(True)
            btn.setChecked(name == key)
            btn.blockSignals(False)
        if notify:
            self.changed.emit(key)

    def set_count(self, text: str) -> None:
        self.count_label.setText(text)


def filter_schedules(schedules: Iterable[Schedule], key: str,
                     now: Optional[datetime] = None) -> list[Schedule]:
    """Apply a :class:`ScheduleFilterBar` selection to a schedule list.

    Pure function so the filtering rules are testable without a UI.
    ``today`` keeps overdue items too -- something you should have done this
    morning is still today's problem.
    """
    now = now or datetime.now()
    rows = list(schedules)
    if key == FILTER_TODAY:
        end = datetime.combine(now.date(), datetime.max.time())
        return [s for s in rows if s.target_time <= end]
    if key == FILTER_RECURRING:
        return [s for s in rows if s.is_recurring]
    return rows


class ScheduleItem(SoftCard):
    """One compact row. Edit/delete buttons appear on hover to save width."""

    toggled = Signal(int, bool)
    deleted = Signal(int)
    edit_requested = Signal(int)

    def __init__(self, schedule: Schedule, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.schedule = schedule
        self._hovered = False

        self.outer = QHBoxLayout(self)
        self.check = CheckCircle()
        self.check.setChecked(bool(schedule.is_done))
        self.check.toggled.connect(lambda done: self.toggled.emit(self.schedule.id, done))
        self.outer.addWidget(self.check, 0, Qt.AlignVCenter)

        self.column = QVBoxLayout()
        self.column.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        # A repeating chore reads differently from a one-off alarm, so it gets a
        # glyph in front of the title as well as the chip on the right -- the
        # chip can be elided away on a narrow window, the glyph never is.
        self.repeat_icon = QLabel("↻")
        self.repeat_icon.setToolTip("반복 일정 · 완료하면 다음 주기로 이월됩니다")
        top.addWidget(self.repeat_icon, 0)
        self.title = ElidedLabel(schedule.title)
        top.addWidget(self.title, 1)
        self.chip = TagChip(schedule.repeat_label, chip_color(schedule.repeat_type))
        top.addWidget(self.chip, 0)
        self.column.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(5)
        self.when = QLabel(self._format_when(schedule))
        bottom.addWidget(self.when)
        self.countdown = QLabel("")
        bottom.addWidget(self.countdown)
        bottom.addStretch(1)
        self.column.addLayout(bottom)
        self.outer.addLayout(self.column, 1)

        self.edit_btn = IconButton("✎", "수정")
        self.edit_btn.clicked.connect(lambda: self.edit_requested.emit(self.schedule.id))
        self.del_btn = IconButton("✕", "삭제", role="danger")
        self.del_btn.clicked.connect(lambda: self.deleted.emit(self.schedule.id))
        for btn in (self.edit_btn, self.del_btn):
            btn.setVisible(False)
            self.outer.addWidget(btn, 0, Qt.AlignVCenter)

        self.apply_style()

    # -- style / hover ------------------------------------------------------ #

    def apply_style(self) -> None:
        s = style()
        self.outer.setContentsMargins(s.card_pad, max(2, s.card_pad - 2),
                                      max(2, s.card_pad - 3), max(2, s.card_pad - 2))
        self.outer.setSpacing(max(4, s.gap + 1))
        self.column.setSpacing(1)
        self.setMinimumHeight(s.row_h)
        self.when.setStyleSheet(f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px;")
        self.chip.set_color(chip_color(self.schedule.repeat_type))
        self._sync_repeat_badge()
        self.refresh_countdown()

    def _sync_repeat_badge(self) -> None:
        """Show the ↻ glyph only for recurring rows, tinted to match the chip."""
        s = style()
        recurring = self.schedule.is_recurring
        self.repeat_icon.setVisible(recurring)
        if recurring:
            color = chip_color(self.schedule.repeat_type)
            self.repeat_icon.setStyleSheet(
                f"color: {s.css(color)}; font-size: {max(9, s.f_xs - 1)}px;")
            self.repeat_icon.setToolTip(
                f"{self.schedule.repeat_label} 반복 · 완료하면 다음 주기로 이월됩니다")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.edit_btn.setVisible(True)
        self.del_btn.setVisible(True)
        self.refresh_countdown()          # brighten the card while hovered

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.edit_btn.setVisible(False)
        self.del_btn.setVisible(False)
        self.refresh_countdown()

    # -- content ------------------------------------------------------------ #

    @staticmethod
    def _format_when(schedule: Schedule) -> str:
        dt = schedule.target_time
        today = datetime.now().date()
        weekday = WEEKDAY_NAMES_KO[dt.weekday()]
        if dt.date() == today:
            return f"오늘 {dt.strftime('%H:%M')}"
        if dt.date() == today + timedelta(days=1):
            return f"내일 {dt.strftime('%H:%M')}"
        if dt.year == datetime.now().year:
            return f"{dt.strftime('%m/%d')}({weekday}) {dt.strftime('%H:%M')}"
        return f"{dt.strftime('%y/%m/%d')} {dt.strftime('%H:%M')}"

    def refresh_countdown(self, now: Optional[datetime] = None) -> None:
        s = style()
        now = now or datetime.now()
        seconds = self.schedule.seconds_left(now)

        if self.schedule.is_done:
            self.countdown.setText("완료")
            self.countdown.setStyleSheet(
                f"color: {s.css(s.success)}; font-size: {s.f_xs}px; font-weight: 700;")
            self.title.setStyleSheet(
                f"color: {s.css(s.text_dim)}; font-size: {s.f_md}px; font-weight: 600;"
                f"text-decoration: line-through;")
            self.set_colors(s.alpha(s.soft, max(30, s.soft.alpha() - 60)),
                            s.alpha(s.line, 40))
            return

        self.title.setStyleSheet(
            f"color: {s.css(s.text)}; font-size: {s.f_md}px; font-weight: 600;")
        self.countdown.setText(humanize_countdown(seconds) if s.show_countdown else "")
        if seconds < 0:
            color, fill, border = s.danger, s.alpha(s.danger, 34), s.alpha(s.danger, 95)
        elif seconds < 3600:
            color, fill, border = s.warn, s.alpha(s.warn, 30), s.alpha(s.warn, 85)
        else:
            color, fill, border = s.accent, s.soft, s.line
        if self._hovered:
            # Subtle lift on hover, the way a modern list row behaves.
            fill = s.alpha(fill, min(255, fill.alpha() + 38))
            border = s.alpha(border, min(255, border.alpha() + 55))
        self.countdown.setStyleSheet(
            f"color: {s.css(color)}; font-size: {s.f_xs}px; font-weight: 700;")
        self.set_colors(fill, border)

    def update_schedule(self, schedule: Schedule) -> None:
        """Re-bind to a fresh row.

        Called on every refresh, so a recurrence that rolled to its next cycle
        updates its time, label, badge and countdown together -- no stale text
        left over from the previous occurrence.
        """
        self.schedule = schedule
        self.title.setText(schedule.title)
        self.when.setText(self._format_when(schedule))
        self.chip.setText(schedule.repeat_label)
        self.chip.set_color(chip_color(schedule.repeat_type))
        self._sync_repeat_badge()
        self.check.blockSignals(True)
        self.check.setChecked(bool(schedule.is_done))
        self.check.blockSignals(False)
        self.refresh_countdown()


class ScheduleListView(QScrollArea):
    """Scrollable list of :class:`ScheduleItem` rows with an empty state."""

    toggled = Signal(int, bool)
    deleted = Signal(int)
    edit_requested = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.viewport().setAutoFillBackground(False)

        self._host = QWidget()
        self._host.setAttribute(Qt.WA_TranslucentBackground, True)
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(0, 0, 3, 0)
        self._layout.addStretch(1)
        self.setWidget(self._host)

        self._empty = QLabel("등록된 일정이 없습니다.\n위 입력창에 자연어로 적어보세요.")
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setWordWrap(True)
        self._layout.insertWidget(0, self._empty)

        self._items: dict[int, ScheduleItem] = {}
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self._layout.setContentsMargins(0, 0, 3, 0)
        self._layout.setSpacing(max(2, s.gap - 1))
        self._empty.setStyleSheet(
            f"color: {s.css(s.text_dim)}; font-size: {s.f_sm}px; padding: 12px 8px;")

    def set_schedules(self, schedules: Iterable[Schedule]) -> None:
        """Diff-update so rows keep their hover state and scroll position."""
        schedules = list(schedules)
        incoming = {s.id: s for s in schedules}
        for sid in list(self._items):
            if sid not in incoming:
                widget = self._items.pop(sid)
                self._layout.removeWidget(widget)
                widget.deleteLater()
        for index, schedule in enumerate(schedules):
            item = self._items.get(schedule.id)
            if item is None:
                item = ScheduleItem(schedule)
                item.toggled.connect(self.toggled)
                item.deleted.connect(self.deleted)
                item.edit_requested.connect(self.edit_requested)
                self._items[schedule.id] = item
            else:
                item.update_schedule(schedule)
            self._layout.insertWidget(index + 1, item)
        self._empty.setVisible(not schedules)

    def refresh_countdowns(self) -> None:
        now = datetime.now()
        for item in self._items.values():
            item.refresh_countdown(now)

    def highlight(self, schedule_id: int) -> None:
        item = self._items.get(schedule_id)
        if item is None:
            return
        s = style()
        item.set_colors(s.alpha(s.accent, 70), s.alpha(s.accent, 170))
        QTimer.singleShot(2600, item.refresh_countdown)


# --------------------------------------------------------------------------- #
# Tab 2 -- chat
# --------------------------------------------------------------------------- #

class ChatBubble(QFrame):
    """One chat message; shrink-wraps its text up to a max width."""

    def __init__(self, sender: str, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.sender = "user" if sender == "user" else "ai"
        self._text = text
        self._max_width = 300
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)

        self.box = QVBoxLayout(self)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.PlainText)      # never render model markup
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.box.addWidget(self.label)
        self.apply_style()

    FADE_FILL_FLOOR = 0.12
    FADE_BORDER_FLOOR = 0.30

    def apply_style(self) -> None:
        s = style()
        pad_v, pad_h = max(4, s.card_pad), max(6, s.card_pad + 3)
        self.box.setContentsMargins(pad_h, pad_v, pad_h, pad_v)
        self.box.setSpacing(0)
        # Only the text is styled here. The bubble background is painted in
        # paintEvent so it can fade with the panel -- a QSS background is a
        # fixed colour and would stay a solid dark block while idle.
        self.setStyleSheet(
            f"QLabel {{ color: {s.css(s.text)}; background: transparent;"
            f" border: 0; font-size: {s.f_md}px; }}")
        self._fit()

    def paintEvent(self, event) -> None:  # noqa: N802
        s = style()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = max(12, s.card_radius + 2)
        tail = 4.0

        if self.sender == "user":
            fill, border = s.alpha(s.accent, 70), s.alpha(s.accent, 145)
        else:
            fill = s.alpha(s.softer, min(255, s.softer.alpha() + 48))
            border = s.alpha(s.line, 80)

        # Rounded box with one squared-off corner pointing at the speaker.
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        path.addRoundedRect(rect, radius, radius)
        if self.sender == "user":
            corner = QRectF(rect.right() - radius, rect.bottom() - radius, radius, radius)
        else:
            corner = QRectF(rect.left(), rect.bottom() - radius, radius, radius)
        path.addRoundedRect(corner, tail, tail)

        painter.setPen(QPen(faded(border, self.FADE_BORDER_FLOOR), 1.0))
        painter.setBrush(faded(fill, self.FADE_FILL_FLOOR))
        painter.drawPath(path.simplified())

    # -- sizing -------------------------------------------------------------- #

    def set_max_width(self, max_width: int) -> None:
        self._max_width = max(110, int(max_width))
        self._fit()

    def _fit(self) -> None:
        """Shrink-wrap: a word-wrapped QLabel in a stretch layout otherwise
        collapses to a narrow column and wraps mid-sentence."""
        metrics = QFontMetrics(self.label.font())
        longest = max((metrics.horizontalAdvance(line)
                       for line in (self._text or " ").split("\n")), default=0)
        padding = self.box.contentsMargins().left() + self.box.contentsMargins().right() + 4
        self.setFixedWidth(min(self._max_width, longest + padding))

    # -- content -------------------------------------------------------------- #

    def set_text(self, text: str) -> None:
        self._text = text
        self.label.setText(text)
        self._fit()

    def append_text(self, chunk: str) -> None:
        self._text += chunk
        self.label.setText(self._text)
        self._fit()

    def text(self) -> str:
        return self._text


class ChatView(QScrollArea):
    """Bubble transcript with sticky auto-scroll and a thinking indicator."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.viewport().setAutoFillBackground(False)

        self._host = QWidget()
        self._host.setAttribute(Qt.WA_TranslucentBackground, True)
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(1, 1, 4, 1)
        self._layout.addStretch(1)
        self.setWidget(self._host)

        self._streaming: Optional[ChatBubble] = None
        self._rows: list[QWidget] = []

        self._hint = QLabel("◈ 오프라인 AI 어시스턴트\n질문 · 메일 초안 · 메모 요약을 맡겨보세요.")
        self._hint.setAlignment(Qt.AlignCenter)
        self._hint.setWordWrap(True)
        self._layout.insertWidget(0, self._hint)

        self._thinking = QLabel("◌ 생각하는 중…")
        self._thinking.setVisible(False)
        self._layout.insertWidget(1, self._thinking)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self._layout.setContentsMargins(1, 0, 4, 0)
        self._layout.setSpacing(max(3, s.gap))
        self._hint.setStyleSheet(
            f"color: {s.css(s.text_dim)}; font-size: {s.f_sm}px; padding: 10px 8px;")
        self._thinking.setStyleSheet(
            f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px; padding: 1px 3px;")
        self._reflow()

    # -- layout --------------------------------------------------------------- #

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        max_width = max(140, int(self.viewport().width() * 0.82))
        for row in self._rows:
            bubble = row.findChild(ChatBubble)
            if bubble is not None:
                bubble.set_max_width(max_width)

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 40

    def _scroll_to_bottom(self) -> None:
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(
            self.verticalScrollBar().maximum()))

    # -- content -------------------------------------------------------------- #

    def add_message(self, sender: str, text: str, scroll: bool = True) -> ChatBubble:
        stick = self._at_bottom()
        self._hint.hide()
        row = QWidget()
        row.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        bubble = ChatBubble(sender, text)
        if sender == "user":
            layout.addStretch(1)
            layout.addWidget(bubble)
        else:
            layout.addWidget(bubble)
            layout.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._rows.append(row)
        bubble.set_max_width(max(140, int(self.viewport().width() * 0.82)))
        if scroll and stick:
            self._scroll_to_bottom()
        return bubble

    def set_thinking(self, thinking: bool) -> None:
        """Show a placeholder while the model is inside a ``<think>`` block."""
        self._thinking.setVisible(thinking)
        if thinking:
            # Keep it at the end of the transcript.
            self._layout.removeWidget(self._thinking)
            self._layout.insertWidget(self._layout.count() - 1, self._thinking)
            self._scroll_to_bottom()

    def start_stream(self) -> ChatBubble:
        self._streaming = self.add_message("ai", "")
        return self._streaming

    def append_token(self, chunk: str) -> None:
        if self._streaming is None:
            self.start_stream()
        stick = self._at_bottom()
        self._streaming.append_text(chunk)
        if stick:
            self._scroll_to_bottom()

    def end_stream(self, final_text: Optional[str] = None) -> str:
        self.set_thinking(False)
        text = ""
        if self._streaming is not None:
            if final_text is not None and final_text.strip():
                self._streaming.set_text(final_text)
            text = self._streaming.text()
            if not text.strip():
                self._streaming.set_text("(응답이 비어 있습니다)")
            self._streaming = None
            self._scroll_to_bottom()
        return text

    def load_history(self, messages: Iterable) -> None:
        self.clear()
        items = list(messages)
        for msg in items:
            self.add_message(msg.sender, msg.message, scroll=False)
        if items:
            self._hint.hide()
            self._scroll_to_bottom()

    def clear(self) -> None:
        for row in self._rows:
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        self._streaming = None
        self.set_thinking(False)
        self._hint.show()


class ChatInput(QWidget):
    """Auto-growing composer. Enter sends, Shift+Enter inserts a newline."""

    submitted = Signal(str)
    stop_requested = Signal()
    clear_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)

        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("메시지…  (Enter 전송 · Shift+Enter 줄바꿈)")
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self._autosize)
        self.row.addWidget(self.edit, 1)

        # One click to drop a poisoned context, instead of hunting through the
        # right-click menu for it.
        self.clear_btn = IconButton("⌫", "대화 기록 지우기  (Ctrl+Shift+C)")
        self.clear_btn.clicked.connect(self.clear_requested)
        self.row.addWidget(self.clear_btn)

        self.send_btn = IconButton("➤", "전송")
        self.send_btn.clicked.connect(self._submit)
        self.row.addWidget(self.send_btn)

        self.stop_btn = IconButton("■", "중지", role="danger")
        self.stop_btn.clicked.connect(self.stop_requested)
        self.stop_btn.hide()
        self.row.addWidget(self.stop_btn)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.row.setSpacing(max(2, s.gap - 1))
        self._min_h = s.ctl_h
        self._max_h = s.ctl_h * 4
        self.edit.setFixedHeight(self._min_h)
        self.send_btn.setStyleSheet(self.send_btn.styleSheet() + f"""
            QPushButton {{ color: {s.css(s.accent)}; }}
        """)
        self._autosize()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.edit and event.type() == QEvent.KeyPress:
            key: QKeyEvent = event
            if key.key() in (Qt.Key_Return, Qt.Key_Enter):
                if key.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier):
                    return False
                self._submit()
                return True
        return super().eventFilter(obj, event)

    def _autosize(self) -> None:
        height = int(min(self._max_h,
                         max(self._min_h, self.edit.document().size().height() + 12)))
        if height != self.edit.height():
            self.edit.setFixedHeight(height)

    def _submit(self) -> None:
        text = self.edit.toPlainText().strip()
        if text and self.send_btn.isVisible() and self.send_btn.isEnabled():
            self.edit.clear()
            self.submitted.emit(text)

    def set_generating(self, generating: bool) -> None:
        self.send_btn.setVisible(not generating)
        self.stop_btn.setVisible(generating)
        self.edit.setReadOnly(generating)
        if not generating:
            self.edit.setFocus()

    def set_enabled_state(self, enabled: bool, placeholder: Optional[str] = None) -> None:
        self.edit.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        if placeholder is not None:
            self.edit.setPlaceholderText(placeholder)


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #

class NotificationCard(SoftCard):
    """In-window alarm popup with snooze / done."""

    #: The user actively closed the card -- they saw it.
    dismissed = Signal()
    #: The card auto-hid with no interaction -- they probably did not.
    timed_out = Signal()
    snoozed = Signal(int)
    completed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setVisible(False)
        self._schedule_id: Optional[int] = None
        self._snooze_minutes = 5

        self.box = QVBoxLayout(self)
        header = QHBoxLayout()
        header.setSpacing(4)
        self.kicker = QLabel("★ 일정 알림")
        header.addWidget(self.kicker)
        header.addStretch(1)
        self.close_btn = IconButton("✕", "닫기")
        self.close_btn.clicked.connect(self.hide_card)
        header.addWidget(self.close_btn)
        self.box.addLayout(header)

        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.box.addWidget(self.title)

        self.subtitle = QLabel("")
        self.subtitle.setWordWrap(True)
        self.box.addWidget(self.subtitle)

        buttons = QHBoxLayout()
        buttons.setSpacing(3)
        self.done_btn = QPushButton("완료")
        self.done_btn.setObjectName("primary")
        self.done_btn.setCursor(Qt.PointingHandCursor)
        self.done_btn.clicked.connect(self._on_done)
        buttons.addWidget(self.done_btn)

        # Quick snooze row. One fixed "5분 후" was never the right amount often
        # enough; these three cover almost every real deferral.
        self.snooze_buttons: list[QPushButton] = []
        for label, minutes in (("5분", 5), ("15분", 15), ("1시간", 60)):
            btn = QPushButton(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"{label} 후 다시 알림")
            btn.clicked.connect(lambda _=False, m=minutes: self._on_snooze(m))
            self.snooze_buttons.append(btn)
            buttons.addWidget(btn)
        buttons.addStretch(1)
        self.box.addLayout(buttons)

        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self._on_timeout)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.box.setContentsMargins(s.pad + 2, s.pad, s.pad, s.pad)
        self.box.setSpacing(max(3, s.gap))
        self.kicker.setStyleSheet(
            f"color: {s.css(s.accent)}; font-size: {s.f_xs}px; font-weight: 700;")
        self.title.setStyleSheet(
            f"color: {s.css(s.text)}; font-size: {s.f_md + 1}px; font-weight: 700;")
        self.subtitle.setStyleSheet(f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px;")
        self.set_colors(s.alpha(s.bg, 250), s.alpha(s.accent, 180))
        if s.shadow and self.graphicsEffect() is None:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(26)
            shadow.setOffset(0, 4)
            shadow.setColor(QColor(0, 0, 0, 190))
            self.setGraphicsEffect(shadow)
        elif not s.shadow:
            self.setGraphicsEffect(None)

    # -- api ------------------------------------------------------------------ #

    def show_alarm(self, schedule: Schedule, auto_hide_ms: int = 25000,
                   snooze_minutes: int = 5, missed_count: int = 0) -> None:
        """``missed_count`` > 0 marks this as a nudge for an ignored alarm."""
        s = style()
        self._schedule_id = schedule.id
        self._snooze_minutes = max(1, snooze_minutes)
        accent = s.warn if missed_count else s.accent
        self.kicker.setText(
            f"★ 놓친 알림 · {missed_count}번째" if missed_count else "★ 일정 알림")
        self.kicker.setStyleSheet(
            f"color: {s.css(accent)}; font-size: {s.f_xs}px; font-weight: 700;")
        self.title.setText(schedule.title)
        repeat = f" · {schedule.repeat_label}" if schedule.is_recurring else ""
        if missed_count:
            # Report the occurrence that was missed, not the next one: a
            # recurring row has already moved on by the time we nudge.
            missed_at = schedule.missed_time
            late = humanize_countdown((missed_at - datetime.now()).total_seconds())
            self.subtitle.setText(
                f"{missed_at.strftime('%m/%d %H:%M')} 예정 · {late}{repeat}")
        else:
            self.subtitle.setText(
                f"{schedule.target_time.strftime('%H:%M')} 예정{repeat}")
        self.subtitle.setVisible(True)
        for btn in self.snooze_buttons:
            btn.setVisible(True)
        self.done_btn.setVisible(True)
        self.set_colors(s.alpha(s.bg, 250), s.alpha(accent, 190))
        self._reveal(auto_hide_ms)

    def show_message(self, kicker: str, title: str, body: str = "",
                     accent: Optional[QColor] = None, auto_hide_ms: int = 4200) -> None:
        s = style()
        accent = accent or s.accent
        self._schedule_id = None
        self.kicker.setText(kicker)
        self.kicker.setStyleSheet(
            f"color: {s.css(accent)}; font-size: {s.f_xs}px; font-weight: 700;")
        self.title.setText(title)
        self.subtitle.setText(body)
        self.subtitle.setVisible(bool(body))
        for btn in self.snooze_buttons:
            btn.setVisible(False)
        self.done_btn.setVisible(False)
        self.set_colors(s.alpha(s.bg, 250), s.alpha(accent, 180))
        self._reveal(auto_hide_ms)

    @property
    def schedule_id(self) -> Optional[int]:
        return self._schedule_id

    def _reveal(self, auto_hide_ms: int) -> None:
        self.show()
        self.raise_()
        self._auto_hide.stop()
        if auto_hide_ms > 0:
            self._auto_hide.start(auto_hide_ms)

    def hide_card(self) -> None:
        """Explicit close (✕). Counts as "I saw this"."""
        self._auto_hide.stop()
        self.hide()
        self.dismissed.emit()

    def _on_timeout(self) -> None:
        """Auto-hide. Deliberately does NOT count as acknowledgement."""
        self.hide()
        self.timed_out.emit()

    def _on_done(self) -> None:
        self.completed.emit()
        self.hide_card()

    def _on_snooze(self, minutes: int) -> None:
        self.snoozed.emit(minutes)
        self.hide_card()


class EmailActionCard(SoftCard):
    """Floating alert for an awaited mail, with the follow-up steps attached.

    Deliberately taller than :class:`NotificationCard`: the whole point is that
    the mail arriving is only the trigger, and what matters is the checklist of
    what to do next.

    Note on symbols: the spec's 📩/📌 are non-BMP and render as .notdef boxes in
    Malgun Gothic (the bug fixed earlier), so the equivalent BMP marks ★ and ●
    carry the same meaning here.
    """

    dismissed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setVisible(False)
        self._rule_id: Optional[int] = None

        self.box = QVBoxLayout(self)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.kicker = QLabel("★ [기다리던 메일 도착!]")
        header.addWidget(self.kicker)
        header.addStretch(1)
        self.close_btn = IconButton("✕", "닫기")
        self.close_btn.clicked.connect(self.hide_card)
        header.addWidget(self.close_btn)
        self.box.addLayout(header)

        self.subject = QLabel("")
        self.subject.setWordWrap(True)
        self.box.addWidget(self.subject)

        self.sender = QLabel("")
        self.sender.setWordWrap(True)
        self.box.addWidget(self.sender)

        # The action checklist gets its own inset panel so it reads as content
        # rather than as more header text.
        self.action_box = QFrame()
        action_layout = QVBoxLayout(self.action_box)
        self.action_title = QLabel("● [후속 진행 업무]")
        action_layout.addWidget(self.action_title)
        self.action_text = QLabel("")
        self.action_text.setWordWrap(True)
        self.action_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        action_layout.addWidget(self.action_text)
        self.action_layout = action_layout
        self.box.addWidget(self.action_box)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.ok_btn = QPushButton("확인 / 닫기")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.setCursor(Qt.PointingHandCursor)
        self.ok_btn.clicked.connect(self.hide_card)
        buttons.addWidget(self.ok_btn)
        self.box.addLayout(buttons)

        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.box.setContentsMargins(s.pad + 2, s.pad, s.pad, s.pad)
        self.box.setSpacing(max(3, s.gap))
        self.kicker.setStyleSheet(
            f"color: {s.css(s.accent)}; font-size: {s.f_sm}px; font-weight: 700;")
        self.subject.setStyleSheet(
            f"color: {s.css(s.text)}; font-size: {s.f_md}px; font-weight: 700;")
        self.sender.setStyleSheet(f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px;")
        self.action_layout.setContentsMargins(s.pad, max(4, s.gap + 1),
                                              s.pad, max(4, s.gap + 1))
        self.action_layout.setSpacing(2)
        self.action_box.setStyleSheet(f"""
            QFrame {{
                background: {s.css(s.accent, 38)};
                border: 1px solid {s.css(s.accent, 105)};
                border-radius: {s.card_radius}px;
            }}
        """)
        self.action_title.setStyleSheet(
            f"color: {s.css(s.accent)}; font-size: {s.f_xs}px; font-weight: 700;"
            f"background: transparent; border: 0;")
        self.action_text.setStyleSheet(
            f"color: {s.css(s.text)}; font-size: {s.f_sm}px;"
            f"background: transparent; border: 0;")
        self.set_colors(s.alpha(s.bg, 250), s.alpha(s.accent, 190))
        if s.shadow and self.graphicsEffect() is None:
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(28)
            shadow.setOffset(0, 5)
            shadow.setColor(QColor(0, 0, 0, 200))
            self.setGraphicsEffect(shadow)
        elif not s.shadow:
            self.setGraphicsEffect(None)

    # -- api ------------------------------------------------------------------ #

    def show_email(self, rule_id: int, subject: str, sender_name: str,
                   reminder_action: str) -> None:
        self._rule_id = rule_id
        self.subject.setText(f"제목: {subject}")
        self.sender.setText(f"발신자: {sender_name}")
        self.action_text.setText((reminder_action or "").strip() or "(등록된 후속 업무 없음)")
        self.adjustSize()
        self.show()
        self.raise_()

    @property
    def rule_id(self) -> Optional[int]:
        return self._rule_id

    def hide_card(self) -> None:
        self.hide()
        self.dismissed.emit()


class Toast(QLabel):
    """Transient status line pinned to the bottom of the panel."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setVisible(False)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def apply_style(self) -> None:
        pass                    # colours are set per message in show_text

    def show_text(self, text: str, level: str = "info", msec: int = 3200) -> None:
        s = style()
        color = {"info": s.accent, "success": s.success,
                 "warn": s.warn, "error": s.danger}.get(level, s.accent)
        self.setStyleSheet(f"""
            background: {s.css(color, 44)};
            color: {s.css(color if not s.is_light else color.darker(140), 255)};
            border: 1px solid {s.css(color, 115)};
            border-radius: {max(8, s.card_radius - 2)}px;
            padding: 4px 9px;
            font-size: {s.f_xs}px;
            font-weight: 700;
        """)
        self.setText(text)
        self.setVisible(True)
        self._timer.start(max(600, msec))


class NotificationSound:
    """Plays a short chime, generating the WAV on first use.

    Generating the asset keeps the repo and the build binary-free while staying
    100 % offline. Falls back to the platform beep if QtMultimedia is missing.
    """

    #: Cap on rebuild attempts, so a permanently broken audio stack does not
    #: retry on every single alarm.
    MAX_REBUILDS = 5

    def __init__(self, path: Optional[str] = None, volume: float = 0.45) -> None:
        base = os.path.dirname(os.path.abspath(__file__))
        self.path = path or os.path.join(base, "assets", "notify.wav")
        self._effect = None
        self._volume = volume
        self._rebuilds = 0
        self._build_effect()

    def _build_effect(self) -> bool:
        """(Re)create the QSoundEffect. Returns True on success."""
        try:
            self._ensure_file()
            from PySide6.QtMultimedia import QSoundEffect

            effect = QSoundEffect()
            effect.setSource(QUrl.fromLocalFile(self.path))
            effect.setVolume(max(0.0, min(1.0, self._volume)))
            self._effect = effect
            return True
        except Exception:                                # noqa: BLE001
            self._effect = None
            return False

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, float(volume)))
        if self._effect is not None:
            try:
                self._effect.setVolume(self._volume)
            except Exception:                            # noqa: BLE001
                pass

    def _ensure_file(self) -> None:
        if os.path.isfile(self.path):
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._write_chime(self.path)

    @staticmethod
    def _write_chime(path: str, sample_rate: int = 44100) -> None:
        """Two-note bell (E6 -> B6) with exponential decay."""
        frames = bytearray()
        notes = ((1318.51, 0.0, 0.30), (1975.53, 0.14, 0.34))
        total = 0.50
        for i in range(int(sample_rate * total)):
            t = i / sample_rate
            sample = 0.0
            for freq, start, length in notes:
                if start <= t < start + length:
                    local = t - start
                    envelope = math.exp(-4.2 * local / length)
                    sample += 0.42 * envelope * math.sin(2 * math.pi * freq * local)
                    sample += 0.10 * envelope * math.sin(4 * math.pi * freq * local)
            if t > total - 0.02:
                sample *= max(0.0, (total - t) / 0.02)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(frames))

    def play(self) -> None:
        """Play the chime, recovering from a lost audio device.

        This app stays open for days. Sleep/undock/headphone changes invalidate
        the WASAPI client (``AUDCLNT_E_DEVICE_INVALIDATED`` in the wild), and
        the QSoundEffect never recovers on its own -- every later alarm would be
        silent with no visible sign. So a dead effect is rebuilt once per
        failure before falling back to the system beep.
        """
        try:
            if self._effect is not None and self._effect.status() != self._effect.Status.Error:
                self._effect.play()
                return
        except Exception:                                # noqa: BLE001
            pass

        # Effect is dead (or was never built): try once to rebuild it.
        if self._rebuilds < self.MAX_REBUILDS:
            self._rebuilds += 1
            if self._build_effect():
                try:
                    self._effect.play()
                    return
                except Exception:                        # noqa: BLE001
                    pass

        try:
            QApplication.beep()                          # always audible fallback
        except Exception:                                # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Dialog base
# --------------------------------------------------------------------------- #

class GlassDialog(QDialog):
    """Frameless, draggable dialog sharing the panel look."""

    def __init__(self, parent: Optional[QWidget] = None, title: str = "",
                 width: int = 320) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setMinimumWidth(width)
        self._drag_offset: Optional[QPoint] = None

        s = style()
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self.panel = GlassPanel(self, margin=6)
        shell.addWidget(self.panel)

        self.body = QVBoxLayout(self.panel)
        m = self.panel.content_margin()
        self.body.setContentsMargins(s.pad + m + 3, s.pad + m, s.pad + m + 3, s.pad + m)
        self.body.setSpacing(max(4, s.gap + 1))

        header = QHBoxLayout()
        self.heading = QLabel(title)
        self.heading.setObjectName("heading")
        header.addWidget(self.heading)
        header.addStretch(1)
        close = IconButton("✕", "닫기", role="danger")
        close.clicked.connect(self.reject)
        header.addWidget(close)
        self.body.addLayout(header)

    # frameless dragging
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None

    def center_on(self, anchor: Optional[QWidget]) -> None:
        """Place the dialog over ``anchor``, clamped to that screen."""
        self.adjustSize()
        if anchor is None:
            return
        centre = anchor.frameGeometry().center()
        x, y = centre.x() - self.width() // 2, centre.y() - self.height() // 2
        screen = anchor.screen() or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
            y = max(area.top() + 8, min(y, area.bottom() - self.height() - 8))
        self.move(int(x), int(y))


class WeekdayPicker(QWidget):
    """Seven toggle buttons for '매주 화,목' style recurrence."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.buttons: list[QPushButton] = []
        for index, name in enumerate(WEEKDAY_NAMES_KO):
            btn = QPushButton(name)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.toggled.connect(lambda *_: self.changed.emit())
            self.buttons.append(btn)
            self.row.addWidget(btn)
        self.apply_style()

    def apply_style(self) -> None:
        s = style()
        self.row.setSpacing(2)
        for btn in self.buttons:
            btn.setFixedHeight(s.ctl_h)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {s.css(s.softer)};
                    border: 1px solid {s.css(s.line)};
                    border-radius: {s.card_radius - 2}px;
                    padding: 0px; font-size: {s.f_sm}px; font-weight: 600;
                    color: {s.css(s.text_dim)};
                }}
                QPushButton:hover {{ border-color: {s.css(s.accent, 150)}; }}
                QPushButton:checked {{
                    background: {s.css(s.accent, 200)};
                    border-color: {s.css(s.accent)};
                    color: {s.css(s.on_accent)};
                }}
            """)

    def selected(self) -> list[int]:
        return [i for i, btn in enumerate(self.buttons) if btn.isChecked()]

    def set_selected(self, weekdays: Iterable[int]) -> None:
        wanted = set(weekdays)
        for i, btn in enumerate(self.buttons):
            btn.blockSignals(True)
            btn.setChecked(i in wanted)
            btn.blockSignals(False)


class WorkReportDialog(GlassDialog):
    """"이번 주 한 일" -- the paragraph everyone has to write on Friday.

    Assembled from the completion log, so every line is something that was
    actually ticked off. The text is editable before copying: the point is to
    save the recalling, not to dictate the wording.
    """

    PERIODS = (("이번 주", 0), ("지난 주", -1), ("이번 달", "month"))

    def __init__(self, db, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, "업무 보고 · 완료 내역", width=372)
        self.db = db
        s = style()

        row = QHBoxLayout()
        row.setSpacing(s.gap)
        self._buttons: list[QPushButton] = []
        for label, key in self.PERIODS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            self._buttons.append(btn)
            row.addWidget(btn)
        row.addStretch(1)
        self.include_open = QCheckBox("예정 항목도 포함")
        self.include_open.setChecked(True)
        self.include_open.toggled.connect(lambda _: self._rebuild())
        row.addWidget(self.include_open)
        self.body.addLayout(row)

        self.text = QPlainTextEdit()
        self.text.setMinimumHeight(224)
        self.text.setStyleSheet(
            f"font-size: {s.f_sm}px; line-height: 150%;")
        self.body.addWidget(self.text)

        hint = QLabel("복사한 뒤 보고서에 그대로 붙여넣으세요. 수정해도 됩니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.body.addWidget(hint)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.copy_btn = QPushButton("복사")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.clicked.connect(self._copy)
        close = QPushButton("닫기")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        actions.addWidget(self.copy_btn)
        self.body.addLayout(actions)

        self._period = 0
        self._select(0)

    # -- period maths ---------------------------------------------------- #
    def _range(self) -> tuple[datetime, datetime]:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if self._period == "month":
            start = today.replace(day=1)
            nxt = (start + timedelta(days=32)).replace(day=1)
            return start, nxt
        monday = today - timedelta(days=today.weekday())
        monday += timedelta(weeks=int(self._period))
        return monday, monday + timedelta(days=7)

    def _select(self, key) -> None:
        self._period = key
        for btn, (_, k) in zip(self._buttons, self.PERIODS):
            btn.setChecked(k == key)
        self._rebuild()

    def _rebuild(self) -> None:
        start, end = self._range()
        try:
            text = self.db.work_report(start, end,
                                       include_open=self.include_open.isChecked())
        except Exception as exc:                      # noqa: BLE001
            text = f"보고서를 만들지 못했습니다: {exc}"
        self.text.setPlainText(text)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())
        self.copy_btn.setText("복사됨 ✔")
        QTimer.singleShot(1400, lambda: self.copy_btn.setText("복사"))


class ManualScheduleDialog(GlassDialog):
    """Manual date/time picker -- the fallback and the edit dialog."""

    def __init__(self, parent: Optional[QWidget] = None, title: str = "",
                 when: Optional[datetime] = None, repeat_type: str = REPEAT_NONE,
                 repeat_detail: str = "", heading: str = "일정 추가", note: str = "") -> None:
        super().__init__(parent, heading, width=308)
        s = style()

        if note:
            hint = QLabel(note)
            hint.setObjectName("muted")
            hint.setWordWrap(True)
            self.body.addWidget(hint)

        self.body.addWidget(self._label("제목"))
        self.title_edit = QLineEdit(title)
        self.title_edit.setPlaceholderText("예) 주간 회의")
        self.title_edit.setFixedHeight(s.ctl_h)
        self.body.addWidget(self.title_edit)

        self.body.addWidget(self._label("날짜 / 시간"))
        default = when or (datetime.now() + timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0)
        self.when_edit = QDateTimeEdit(default)
        self.when_edit.setDisplayFormat("yyyy-MM-dd  HH:mm")
        self.when_edit.setCalendarPopup(True)
        self.when_edit.setFixedHeight(s.ctl_h)
        self.body.addWidget(self.when_edit)

        quick = QHBoxLayout()
        quick.setSpacing(3)
        for label, delta in (("+10분", timedelta(minutes=10)), ("+1시간", timedelta(hours=1)),
                             ("내일", timedelta(days=1)), ("다음주", timedelta(days=7))):
            btn = QPushButton(label)
            btn.setObjectName("ghost")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(max(18, s.ctl_h - 6))
            btn.clicked.connect(lambda _=False, d=delta: self._nudge(d))
            quick.addWidget(btn)
        self.body.addLayout(quick)

        self.body.addWidget(self._label("반복"))
        self.repeat_box = QComboBox()
        for label, value in (("반복 없음", REPEAT_NONE), ("매일", REPEAT_DAILY),
                             ("매주", REPEAT_WEEKLY), ("매월", REPEAT_MONTHLY)):
            self.repeat_box.addItem(label, value)
        self.repeat_box.setCurrentIndex(max(0, self.repeat_box.findData(repeat_type)))
        self.repeat_box.setFixedHeight(s.ctl_h)
        self.repeat_box.currentIndexChanged.connect(self._sync_detail)
        self.body.addWidget(self.repeat_box)

        self.weekdays = WeekdayPicker()
        self.body.addWidget(self.weekdays)

        self.month_row = QWidget()
        month_layout = QHBoxLayout(self.month_row)
        month_layout.setContentsMargins(0, 0, 0, 0)
        month_layout.addWidget(QLabel("매월"))
        self.month_day = QSpinBox()
        self.month_day.setRange(1, 31)
        self.month_day.setSuffix("일")
        self.month_day.setFixedHeight(s.ctl_h)
        month_layout.addWidget(self.month_day)
        month_layout.addStretch(1)
        self.body.addWidget(self.month_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("저장")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        self.body.addLayout(buttons)

        self._preset_detail = repeat_detail
        self._sync_detail()
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        return label

    def _nudge(self, delta: timedelta) -> None:
        current = self.when_edit.dateTime().toPython()
        self.when_edit.setDateTime(current + delta)

    def _sync_detail(self, *_args) -> None:
        repeat = self.repeat_box.currentData()
        when = self.when_edit.dateTime().toPython()
        self.weekdays.setVisible(repeat == REPEAT_WEEKLY)
        self.month_row.setVisible(repeat == REPEAT_MONTHLY)
        if repeat == REPEAT_WEEKLY:
            days = parse_weekdays(self._preset_detail) or [when.weekday()]
            self.weekdays.set_selected(days)
        elif repeat == REPEAT_MONTHLY:
            self.month_day.setValue(parse_month_day(self._preset_detail) or when.day)
        self.adjustSize()

    def _on_save(self) -> None:
        s = style()
        if not self.title_edit.text().strip():
            self.title_edit.setPlaceholderText("제목을 입력해주세요")
            self.title_edit.setStyleSheet(f"border: 1px solid {s.css(s.danger, 190)};")
            self.title_edit.setFocus()
            return
        if self.repeat_box.currentData() == REPEAT_WEEKLY and not self.weekdays.selected():
            self.weekdays.set_selected([self.when_edit.dateTime().toPython().weekday()])
        self.accept()

    def values(self) -> dict:
        repeat = self.repeat_box.currentData()
        when = self.when_edit.dateTime().toPython().replace(second=0, microsecond=0)
        if repeat == REPEAT_WEEKLY:
            detail = format_weekdays(self.weekdays.selected() or [when.weekday()])
        elif repeat == REPEAT_MONTHLY:
            detail = str(self.month_day.value())
        else:
            detail = ""
        return {"title": self.title_edit.text().strip(), "target_time": when,
                "repeat_type": repeat, "repeat_detail": detail}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

class AwaitedEmailDialog(GlassDialog):
    """Add / edit one awaited-email rule."""

    def __init__(self, parent: Optional[QWidget] = None, keywords: str = "",
                 action: str = "", sender: str = "") -> None:
        super().__init__(parent, "메일 감지 규칙", width=320)
        s = style()

        hint = QLabel("받은 메일의 제목·본문에 키워드가 있으면 알리고, 아래 후속 업무를 띄웁니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        self.body.addWidget(hint)

        self.body.addWidget(self._label("키워드  (쉼표로 여러 개)"))
        self.kw_edit = QLineEdit(keywords)
        self.kw_edit.setPlaceholderText("특약OS이월, 특약이월")
        self.kw_edit.setFixedHeight(s.ctl_h)
        self.body.addWidget(self.kw_edit)

        self.body.addWidget(self._label("후속 업무  (여러 줄 가능)"))
        self.action_edit = QPlainTextEdit(action)
        self.action_edit.setPlaceholderText("1. 결재 시스템 승인\n2. 담당자 이메일 공유")
        self.action_edit.setFixedHeight(s.ctl_h * 3)
        self.body.addWidget(self.action_edit)

        self.body.addWidget(self._label("발신자 필터  (선택)"))
        self.sender_edit = QLineEdit(sender)
        self.sender_edit.setPlaceholderText("팀장  또는  @koreanre.co.kr")
        self.sender_edit.setFixedHeight(s.ctl_h)
        self.body.addWidget(self.sender_edit)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("취소")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("저장")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        buttons.addWidget(save)
        self.body.addLayout(buttons)
        self.kw_edit.setFocus()

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        return label

    def _on_save(self) -> None:
        s = style()
        if len(self.kw_edit.text().strip()) < 2:
            self.kw_edit.setPlaceholderText("키워드를 2자 이상 입력해주세요")
            self.kw_edit.setStyleSheet(f"border: 1px solid {s.css(s.danger, 190)};")
            self.kw_edit.setFocus()
            return
        if len(self.action_edit.toPlainText().strip()) < 2:
            self.action_edit.setStyleSheet(f"border: 1px solid {s.css(s.danger, 190)};")
            self.action_edit.setFocus()
            return
        self.accept()

    def values(self) -> dict:
        return {
            "keywords": self.kw_edit.text().strip(),
            "reminder_action": self.action_edit.toPlainText().strip(),
            "sender_filter": self.sender_edit.text().strip(),
        }


class AwaitedEmailRow(SoftCard):
    """One rule in the settings list: ON/OFF, text, delete."""

    toggled = Signal(int, bool)
    deleted = Signal(int)

    def __init__(self, rule: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.rule = rule
        s = style()

        outer = QHBoxLayout(self)
        outer.setContentsMargins(s.card_pad + 2, s.card_pad, s.card_pad, s.card_pad)
        outer.setSpacing(max(4, s.gap + 1))

        self.enabled = QCheckBox()
        self.enabled.setChecked(bool(rule.get("is_active", 1)))
        self.enabled.setToolTip("감시 켜기 / 끄기")
        self.enabled.toggled.connect(lambda v: self.toggled.emit(int(rule["id"]), v))
        outer.addWidget(self.enabled, 0, Qt.AlignTop)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        title = ElidedLabel(rule.get("keywords", ""))
        title.setStyleSheet(
            f"color: {s.css(s.text)}; font-size: {s.f_md}px; font-weight: 600;")
        top.addWidget(title, 1)
        triggered = bool(rule.get("is_triggered"))
        chip = TagChip("수신됨" if triggered else "대기 중",
                       s.success if triggered else s.accent)
        top.addWidget(chip, 0)
        column.addLayout(top)

        detail = (rule.get("reminder_action") or "").replace("\n", " / ")
        if rule.get("sender_filter"):
            detail = f"[{rule['sender_filter']}] {detail}"
        body = QLabel(detail)
        body.setWordWrap(True)
        body.setStyleSheet(f"color: {s.css(s.text_dim)}; font-size: {s.f_xs}px;")
        column.addWidget(body)
        outer.addLayout(column, 1)

        remove = IconButton("✕", "규칙 삭제", role="danger")
        remove.clicked.connect(lambda: self.deleted.emit(int(rule["id"])))
        outer.addWidget(remove, 0, Qt.AlignTop)


class SettingsDialog(GlassDialog):
    """Live-previewing settings. Every change applies immediately;
    Cancel restores the snapshot taken when the dialog opened."""

    preview_requested = Signal()        # config changed -> restyle now
    reset_requested = Signal()

    def __init__(self, config: Config, parent: Optional[QWidget] = None,
                 backend: Optional[dict] = None, db: object = None) -> None:
        super().__init__(parent, "⚙  설정", width=372)
        self.config = config
        self.backend = backend or {}
        self.db = db
        self._loading = True

        self.tabs = QTabWidget()
        self.tabs.addTab(self._appearance_tab(), "모양")
        self.tabs.addTab(self._behavior_tab(), "동작")
        self.tabs.addTab(self._ai_tab(), "AI")
        if db is not None:
            self.tabs.addTab(self._email_tab(), "이메일 감지")
        self.tabs.addTab(self._about_tab(), "정보")
        self.body.addWidget(self.tabs)

        buttons = QHBoxLayout()
        reset = QPushButton("기본값 복원")
        reset.clicked.connect(self._on_reset)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        close = QPushButton("닫기")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        self.body.addLayout(buttons)

        self._loading = False

    # ---- builders ---------------------------------------------------------- #

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QVBoxLayout(page)
        s = style()
        layout.setContentsMargins(1, s.gap + 2, 1, 1)
        layout.setSpacing(max(3, s.gap))
        return page, layout

    @staticmethod
    def _row(label: str, widget: QWidget) -> QWidget:
        holder = QWidget()
        holder.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        text = QLabel(label)
        text.setObjectName("muted")
        text.setMinimumWidth(78)
        layout.addWidget(text)
        layout.addWidget(widget, 1)
        return holder

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _slider(self, low: int, high: int, value: int, suffix: str, on_change) -> QWidget:
        holder = QWidget()
        holder.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        readout = QLabel(f"{value}{suffix}")
        readout.setObjectName("muted")
        readout.setMinimumWidth(38)
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        def handler(v: int) -> None:
            readout.setText(f"{v}{suffix}")
            if not self._loading:
                on_change(v)

        slider.valueChanged.connect(handler)
        layout.addWidget(slider, 1)
        layout.addWidget(readout)
        return holder

    def _combo(self, options: list[tuple[str, str]], current: str, on_change) -> QComboBox:
        combo = QComboBox()
        combo.setFixedHeight(style().ctl_h)
        for label, value in options:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        combo.currentIndexChanged.connect(
            lambda _=0: None if self._loading else on_change(combo.currentData()))
        return combo

    def _check(self, text: str, checked: bool, on_change) -> QCheckBox:
        box = QCheckBox(text)
        box.setChecked(bool(checked))
        box.toggled.connect(lambda v: None if self._loading else on_change(v))
        return box

    def _spin(self, low: int, high: int, value: int, suffix: str, on_change) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setValue(int(value))
        spin.setSuffix(suffix)
        spin.setFixedHeight(style().ctl_h)
        spin.valueChanged.connect(lambda v: None if self._loading else on_change(v))
        return spin

    def _time_range(self, start: str, end: str, on_start, on_end) -> QWidget:
        """Two HH:MM pickers with a dash, for "09:00 ~ 18:00"."""
        box = QWidget()
        box.setAttribute(Qt.WA_TranslucentBackground, True)
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for value, handler in ((start, on_start), (end, on_end)):
            edit = QTimeEdit()
            edit.setDisplayFormat("HH:mm")
            edit.setTime(QTime.fromString(str(value), "HH:mm"))
            edit.setFixedHeight(style().ctl_h)
            edit.timeChanged.connect(
                lambda t, h=handler: None if self._loading else h(t.toString("HH:mm")))
            row.addWidget(edit)
            if handler is on_start:
                dash = QLabel("~")
                dash.setObjectName("muted")
                row.addWidget(dash)
        return box

    def _apply(self, section: str, key: str, value) -> None:
        self.config.set(section, key, value)
        self.config._sanitise()
        self.preview_requested.emit()

    # ---- tabs --------------------------------------------------------------- #

    def _appearance_tab(self) -> QWidget:
        page, layout = self._page()
        a = self.config.appearance

        layout.addWidget(self._section("테마"))
        layout.addWidget(self._row("테마", self._combo(
            [(v["label"], k) for k, v in THEMES.items()], a["theme"],
            lambda v: self._apply("appearance", "theme", v))))
        layout.addWidget(self._row("강조색", self._accent_row()))

        layout.addWidget(self._section("글자 · 간격"))
        layout.addWidget(self._row("글자 크기", self._slider(
            80, 140, int(a["font_scale"] * 100), "%",
            lambda v: self._apply("appearance", "font_scale", v / 100))))
        layout.addWidget(self._row("간격", self._combo(
            [("좁게", "compact"), ("보통", "normal"), ("넓게", "roomy")], a["density"],
            lambda v: self._apply("appearance", "density", v))))
        layout.addWidget(self._row("모서리", self._combo(
            [("각지게", "sharp"), ("조금", "small"), ("보통", "medium"), ("둥글게", "round")],
            a["radius"], lambda v: self._apply("appearance", "radius", v))))

        layout.addWidget(self._section("투명도  (마우스를 올리면 또렷해집니다)"))
        layout.addWidget(self._row("평소 창", self._slider(
            35, 100, int(a["idle_opacity"] * 100), "%",
            lambda v: self._apply("appearance", "idle_opacity", v / 100))))
        layout.addWidget(self._row("평소 배경", self._slider(
            0, 100, int(a["idle_panel_alpha"] * 100), "%",
            lambda v: self._apply("appearance", "idle_panel_alpha", v / 100))))
        layout.addWidget(self._row("사용 중 배경", self._slider(
            20, 100, int(a["panel_alpha"] * 100), "%",
            lambda v: self._apply("appearance", "panel_alpha", v / 100))))
        layout.addWidget(self._row("평소 카드", self._slider(
            0, 100, int(a["idle_card_fade"] * 100), "%",
            lambda v: self._apply("appearance", "idle_card_fade", v / 100))))
        note = QLabel("※ 글자는 항상 불투명하게 유지되어 배경만 투명해집니다.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addWidget(self._section("표시 항목"))
        layout.addWidget(self._check("초 단위 시계", a["show_seconds"],
                                     lambda v: self._apply("appearance", "show_seconds", v)))
        layout.addWidget(self._check("요약 줄 표시", a["show_summary"],
                                     lambda v: self._apply("appearance", "show_summary", v)))
        layout.addWidget(self._check("남은 시간 표시", a["show_countdown"],
                                     lambda v: self._apply("appearance", "show_countdown", v)))
        layout.addWidget(self._check("그림자 효과", a["shadow"],
                                     lambda v: self._apply("appearance", "shadow", v)))
        layout.addStretch(1)
        return page

    def _accent_row(self) -> QWidget:
        holder = QWidget()
        holder.setAttribute(Qt.WA_TranslucentBackground, True)
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        combo = self._combo([(label, key) for key, (label, _) in ACCENTS.items()],
                            self.config.appearance["accent"],
                            lambda v: self._apply("appearance", "accent", v))
        layout.addWidget(combo, 1)
        pick = QPushButton("색상…")
        pick.setFixedHeight(style().ctl_h)
        pick.clicked.connect(self._pick_accent)
        layout.addWidget(pick)
        return holder

    def _pick_accent(self) -> None:
        current = QColor(self.config.appearance["accent_custom"])
        chosen = QColorDialog.getColor(current, self, "강조색 선택")
        if chosen.isValid():
            self.config.set("appearance", "accent_custom", chosen.name())
            self.config.set("appearance", "accent", "custom")
            self.config._sanitise()
            self.preview_requested.emit()

    def _behavior_tab(self) -> QWidget:
        page, layout = self._page()
        b, w = self.config.behavior, self.config.window

        layout.addWidget(self._section("창"))
        layout.addWidget(self._check("항상 위에 표시", w["always_on_top"],
                                     lambda v: self._apply("window", "always_on_top", v)))
        layout.addWidget(self._check("위치 잠금", w["locked"],
                                     lambda v: self._apply("window", "locked", v)))
        layout.addWidget(self._check("위치 기억", w["remember_position"],
                                     lambda v: self._apply("window", "remember_position", v)))
        layout.addWidget(self._check("시작 시 트레이로", w["start_minimized"],
                                     lambda v: self._apply("window", "start_minimized", v)))
        layout.addWidget(self._row("기본 위치", self._combo(
            [("우측 상단", "top-right"), ("우측 하단", "bottom-right"),
             ("좌측 상단", "top-left"), ("좌측 하단", "bottom-left")], w["corner"],
            lambda v: self._apply("window", "corner", v))))

        layout.addWidget(self._section("알림"))
        layout.addWidget(self._check("알림음", b["sound_enabled"],
                                     lambda v: self._apply("behavior", "sound_enabled", v)))
        layout.addWidget(self._row("음량", self._slider(
            0, 100, int(b["sound_volume"] * 100), "%",
            lambda v: self._apply("behavior", "sound_volume", v / 100))))
        layout.addWidget(self._check("트레이 풍선 알림", b["tray_balloon"],
                                     lambda v: self._apply("behavior", "tray_balloon", v)))
        layout.addWidget(self._check("알림 시 테두리 반짝임", b["flash_on_alert"],
                                     lambda v: self._apply("behavior", "flash_on_alert", v)))
        layout.addWidget(self._row("알림 유지", self._spin(
            0, 300, b["notification_seconds"], "초",
            lambda v: self._apply("behavior", "notification_seconds", v))))
        layout.addWidget(self._row("미루기", self._spin(
            1, 240, b["snooze_minutes"], "분",
            lambda v: self._apply("behavior", "snooze_minutes", v))))
        layout.addWidget(self._check("놓친 알림 다시 알리기", b["nag_enabled"],
                                     lambda v: self._apply("behavior", "nag_enabled", v)))
        layout.addWidget(self._row("다시 알림", self._spin(
            1, 180, b["nag_minutes"], "분 뒤",
            lambda v: self._apply("behavior", "nag_minutes", v))))
        layout.addWidget(self._row("최대 횟수", self._spin(
            1, 20, b["nag_max_count"], "회",
            lambda v: self._apply("behavior", "nag_max_count", v))))
        nag_hint = QLabel("완료·미루기·닫기 중 아무것도 안 하면 다시 알립니다.")
        nag_hint.setObjectName("muted")
        nag_hint.setWordWrap(True)
        layout.addWidget(nag_hint)

        layout.addWidget(self._section("근무 시간"))
        layout.addWidget(self._check(
            "근무 시간에만 알리기", b["quiet_enabled"],
            lambda v: self._apply("behavior", "quiet_enabled", v)))
        layout.addWidget(self._row("근무", self._time_range(
            b["work_start"], b["work_end"],
            lambda v: self._apply("behavior", "work_start", v),
            lambda v: self._apply("behavior", "work_end", v))))
        layout.addWidget(self._check(
            "점심시간 제외", b["quiet_skip_lunch"],
            lambda v: self._apply("behavior", "quiet_skip_lunch", v)))
        layout.addWidget(self._row("점심", self._time_range(
            b["lunch_start"], b["lunch_end"],
            lambda v: self._apply("behavior", "lunch_start", v),
            lambda v: self._apply("behavior", "lunch_end", v))))
        layout.addWidget(self._check(
            "주말·공휴일 제외", b["quiet_skip_holidays"],
            lambda v: self._apply("behavior", "quiet_skip_holidays", v)))
        layout.addWidget(self._check(
            "Outlook 오늘 일정 표시", b["calendar_enabled"],
            lambda v: self._apply("behavior", "calendar_enabled", v)))

        quiet_hint = QLabel(
            "근무 시간 밖의 알림은 사라지지 않고 다음 근무 시간에 울립니다.\n"
            + _holiday_note())
        quiet_hint.setObjectName("muted")
        quiet_hint.setWordWrap(True)
        layout.addWidget(quiet_hint)

        layout.addWidget(self._section("일정"))
        layout.addWidget(self._row("확인 주기", self._spin(
            1, 60, b["poll_seconds"], "초",
            lambda v: self._apply("behavior", "poll_seconds", v))))
        layout.addWidget(self._check("완료된 일정 숨기기", b["hide_completed"],
                                     lambda v: self._apply("behavior", "hide_completed", v)))
        layout.addWidget(self._check("삭제 전 확인", b["confirm_delete"],
                                     lambda v: self._apply("behavior", "confirm_delete", v)))
        layout.addStretch(1)
        return page

    def _ai_tab(self) -> QWidget:
        page, layout = self._page()
        m = self.config.llm

        layout.addWidget(self._section("모델"))
        self.model_label = QLabel(self._model_text())
        self.model_label.setObjectName("muted")
        self.model_label.setWordWrap(True)
        layout.addWidget(self.model_label)

        row = QHBoxLayout()
        row.setSpacing(4)
        browse = QPushButton("GGUF 파일 선택…")
        browse.clicked.connect(self._pick_model)
        row.addWidget(browse)
        clear = QPushButton("자동 탐색")
        clear.clicked.connect(lambda: self._set_model(""))
        row.addWidget(clear)
        layout.addLayout(row)

        layout.addWidget(self._section("생성"))
        layout.addWidget(self._row("사고 과정", self._combo(
            [("끄기 (빠름)", "off"), ("숨기기", "hide"), ("표시", "show")], m["thinking"],
            lambda v: self._apply("llm", "thinking", v))))
        layout.addWidget(self._row("응답 길이", self._spin(
            64, 4096, m["max_tokens"], " 토큰",
            lambda v: self._apply("llm", "max_tokens", v))))
        layout.addWidget(self._row("창의성", self._slider(
            0, 150, int(m["temperature"] * 100), "",
            lambda v: self._apply("llm", "temperature", v / 100))))
        layout.addWidget(self._row("기억할 대화", self._spin(
            0, 50, m["history_turns"], " 턴",
            lambda v: self._apply("llm", "history_turns", v))))

        layout.addWidget(self._section("성능  (변경 시 모델 재로드)"))
        layout.addWidget(self._row("컨텍스트", self._spin(
            512, 32768, m["n_ctx"], "",
            lambda v: self._apply("llm", "n_ctx", v))))
        layout.addWidget(self._row("스레드", self._spin(
            0, 64, m["n_threads"], " (0=자동)",
            lambda v: self._apply("llm", "n_threads", v))))
        layout.addWidget(self._check("시작 시 모델 미리 로드", m["preload"],
                                     lambda v: self._apply("llm", "preload", v)))
        layout.addWidget(self._check("일정 분석에 AI 사용 (실패 시에만)",
                                     m["use_llm_for_parsing"],
                                     lambda v: self._apply("llm", "use_llm_for_parsing", v)))
        hint = QLabel("※ 일정 인식은 내장 분석기가 먼저 처리합니다. AI는 인식 실패 시에만 "
                      "사용되며, 결과는 항상 확인 창을 거칩니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _email_tab(self) -> QWidget:
        """Visual management for awaited-email rules (previously chat-only)."""
        page, layout = self._page()

        header = QHBoxLayout()
        header.setSpacing(4)
        self.email_count = QLabel("")
        self.email_count.setObjectName("muted")
        header.addWidget(self.email_count)
        header.addStretch(1)
        add = QPushButton("＋ 규칙 추가")
        add.setObjectName("primary")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self._add_email_rule)
        header.addWidget(add)
        layout.addLayout(header)

        self.email_scroll = QScrollArea()
        self.email_scroll.setWidgetResizable(True)
        self.email_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.email_scroll.setFrameShape(QFrame.NoFrame)
        self.email_scroll.viewport().setAutoFillBackground(False)
        self.email_scroll.setMinimumHeight(150)

        host = QWidget()
        host.setAttribute(Qt.WA_TranslucentBackground, True)
        self.email_list = QVBoxLayout(host)
        self.email_list.setContentsMargins(0, 0, 3, 0)
        self.email_list.setSpacing(max(3, style().gap))
        self.email_list.addStretch(1)
        self.email_scroll.setWidget(host)
        layout.addWidget(self.email_scroll, 1)

        note = QLabel("체크를 끄면 감시만 멈추고 규칙은 남습니다. "
                      "채팅에서 \"'키워드' 메일 오면 '할 일' 리마인드해줘\" 로도 등록됩니다.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._reload_email_rules()
        return page

    def _reload_email_rules(self) -> None:
        while self.email_list.count() > 1:                # keep the stretch
            item = self.email_list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        try:
            rules = self.db.list_awaited_emails(include_triggered=True)
        except Exception:                                 # noqa: BLE001
            rules = []
        for index, rule in enumerate(rules):
            row = AwaitedEmailRow(rule)
            row.toggled.connect(self._toggle_email_rule)
            row.deleted.connect(self._delete_email_rule)
            self.email_list.insertWidget(index, row)
        if not rules:
            empty = QLabel("등록된 메일 감지 규칙이 없습니다.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setObjectName("muted")
            self.email_list.insertWidget(0, empty)
        waiting = sum(1 for r in rules if not r["is_triggered"] and r["is_active"])
        self.email_count.setText(f"규칙 {len(rules)}건 · 대기 중 {waiting}건")

    def _add_email_rule(self) -> None:
        dialog = AwaitedEmailDialog(self)
        dialog.center_on(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self.db.add_awaited_email(values["keywords"], values["reminder_action"],
                                      values["sender_filter"])
        except Exception:                                 # noqa: BLE001
            return
        self._reload_email_rules()

    def _toggle_email_rule(self, rule_id: int, active: bool) -> None:
        try:
            self.db.set_awaited_active(rule_id, active)
        except Exception:                                 # noqa: BLE001
            pass

    def _delete_email_rule(self, rule_id: int) -> None:
        try:
            self.db.delete_awaited_email(rule_id)
        except Exception:                                 # noqa: BLE001
            pass
        self._reload_email_rules()

    def _about_tab(self) -> QWidget:
        page, layout = self._page()
        backend = self.backend
        model = backend.get("model") or {}
        lines = [
            "**Offline Smart HUD**",
            "완전 오프라인 데스크톱 일정 · AI 도우미",
            "",
            f"llama-cpp-python: {backend.get('version') or '미설치'}"
            + (f"  ({backend.get('error')})" if backend.get("error") else ""),
            f"모델: {model.get('name', '없음')}"
            + (f"  ({model.get('size_mb')} MB)" if model.get("size_mb") else ""),
            f"설정 파일: {os.path.basename(self.config.path)}",
        ]
        info = QLabel("\n".join(lines).replace("**", ""))
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(info)

        row = QHBoxLayout()
        row.setSpacing(4)
        for label, target in (("사용 설명서", "USER_MANUAL.md"), ("폴더 열기", "."),
                              ("로그 열기", "logs")):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=target: self._open_path(t))
            row.addWidget(btn)
        layout.addLayout(row)

        shortcuts = QLabel(
            "단축키\n"
            "  Enter          일정 추가 / 메시지 전송\n"
            "  Shift+Enter    줄바꿈\n"
            "  Ctrl+N         직접 입력 창\n"
            "  Ctrl+,         설정\n"
            "  Ctrl+Shift+C   대화 기록 지우기\n"
            "  Ctrl+Tab       탭 전환\n"
            "  Esc            트레이로 숨기기\n"
            "  Ctrl+드래그    창 이동 (제목 표시줄 밖에서)\n"
            "  더블클릭       제목 표시줄에서 접기/펼치기"
        )
        shortcuts.setObjectName("muted")
        layout.addWidget(shortcuts)
        layout.addStretch(1)
        return page

    # ---- helpers ------------------------------------------------------------ #

    def _model_text(self) -> str:
        from llm_engine import find_model_path, is_volatile_model, models_dir
        configured = self.config.llm["model_path"]
        head = f"지정됨: {configured}" if configured else "자동 탐색"
        lines = [head, f"모델 폴더: {models_dir()}"]
        try:
            active = find_model_path(configured)
        except Exception:                                # noqa: BLE001
            active = None
        if active is None:
            lines.append("현재 사용 중인 모델 없음 (AI 대화 비활성화)")
        elif is_volatile_model(active):
            # Worth the extra line: the file is too big to want to re-copy,
            # and AppData is hidden so "move it" needs the path spelled out.
            lines.append(f"⚠ 모델이 프로그램 폴더에 있습니다 — 업그레이드 시 사라집니다.\n"
                         f"    {active}\n    위 '모델 폴더'로 옮겨주세요.")
        else:
            lines.append(f"사용 중: {os.path.basename(active)}")
        return "\n".join(lines)

    def _pick_model(self) -> None:
        from llm_engine import models_dir
        start = (os.path.dirname(self.config.llm["model_path"])
                 or models_dir() or self.config.base_dir)
        path, _ = QFileDialog.getOpenFileName(self, "GGUF 모델 선택", start,
                                              "GGUF 모델 (*.gguf);;모든 파일 (*.*)")
        if path:
            self._set_model(path)

    def _set_model(self, path: str) -> None:
        self.config.set("llm", "model_path", path)
        self.config._sanitise()
        self.model_label.setText(self._model_text())
        self.preview_requested.emit()

    def _open_path(self, relative: str) -> None:
        target = os.path.join(self.config.base_dir, relative)
        try:
            if not os.path.exists(target):
                target = self.config.base_dir
            os.startfile(target)                          # noqa: S606 - Windows shell open
        except Exception:                                 # noqa: BLE001
            pass

    def _on_reset(self) -> None:
        self.reset_requested.emit()
        self.accept()


# --------------------------------------------------------------------------- #
# Icon
# --------------------------------------------------------------------------- #

def make_app_icon(size: int = 64, accent: Optional[QColor] = None) -> QIcon:
    """Draw the tray/app icon: rounded square with a clock glyph."""
    s = style()
    accent = accent or s.accent
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)

    rect = QRectF(2, 2, size - 4, size - 4)
    painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 215), 2))
    painter.setBrush(QColor(15, 23, 42, 240))
    painter.drawRoundedRect(rect, size * 0.28, size * 0.28)

    centre = QRectF(size * 0.24, size * 0.24, size * 0.52, size * 0.52)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(accent, max(2.0, size * 0.045)))
    painter.drawEllipse(centre)

    painter.setPen(QPen(QColor(235, 240, 248), max(2.0, size * 0.05), Qt.SolidLine, Qt.RoundCap))
    cx, cy = centre.center().x(), centre.center().y()
    painter.drawLine(int(cx), int(cy), int(cx), int(cy - centre.height() * 0.30))
    painter.drawLine(int(cx), int(cy), int(cx + centre.width() * 0.26), int(cy))
    painter.end()
    return QIcon(pixmap)


# --------------------------------------------------------------------------- #
# Gallery: ``py ui_components.py``
# --------------------------------------------------------------------------- #

def _gallery() -> None:  # pragma: no cover
    import sys
    import tempfile

    app = QApplication(sys.argv)
    cfg = Config(tempfile.mkdtemp(prefix="hud_gallery_"))
    set_style(build_style(cfg))
    app.setStyleSheet(build_stylesheet())

    window = QWidget()
    window.setWindowTitle("ui gallery")
    window.setAttribute(Qt.WA_TranslucentBackground, True)
    window.resize(cfg.window["width"], cfg.window["height"] + 120)

    shell = QVBoxLayout(window)
    shell.setContentsMargins(0, 0, 0, 0)
    panel = GlassPanel(window)
    shell.addWidget(panel)

    s = style()
    layout = QVBoxLayout(panel)
    m = panel.content_margin()
    layout.setContentsMargins(s.pad + m, s.pad + m - 3, s.pad + m, s.pad + m)
    layout.setSpacing(s.gap + 1)

    bar = TitleBar()
    bar.set_model_state("ready", "demo")
    bar.set_clock("15:04")
    bar.set_badge("2")
    bar.close_requested.connect(app.quit)
    layout.addWidget(bar)

    quick = QuickAddBar()
    layout.addWidget(quick)

    filters = ScheduleFilterBar()
    filters.set_count("5/5")
    filters.changed.connect(lambda k: print("filter:", k))
    layout.addWidget(filters)

    now = datetime.now()
    listview = ScheduleListView()
    listview.set_schedules([
        Schedule(1, "주간 회의", now + timedelta(minutes=42), REPEAT_WEEKLY, "월"),
        Schedule(2, "치과 예약", now + timedelta(days=1, hours=3)),
        Schedule(3, "아침 운동", now - timedelta(minutes=12), REPEAT_DAILY),
        Schedule(4, "헬스", now + timedelta(days=1), REPEAT_WEEKLY, "화,목"),
        Schedule(5, "월급 확인", now + timedelta(days=14), REPEAT_MONTHLY, "25", is_done=1),
    ])
    layout.addWidget(listview, 1)

    chat = ChatView()
    chat.add_message("user", "회의 안내 메일 초안 좀")
    chat.add_message("ai", "네, 아래 초안을 확인해 주세요.\n\n제목: 주간 회의 일정 안내")
    layout.addWidget(chat, 1)
    layout.addWidget(ChatInput())

    toast = Toast(panel)
    layout.addWidget(toast)
    toast.show_text("갤러리 미리보기", "success", 8000)

    card = NotificationCard(panel)
    card.setFixedWidth(panel.width() - 2 * (m + 10))
    card.move(m + 10, m + 70)
    card.show_alarm(Schedule(1, "주간 회의", now, REPEAT_WEEKLY, "월"), auto_hide_ms=0)
    card.snoozed.connect(lambda minutes: print("snooze:", minutes))

    mail = EmailActionCard(panel)
    mail.setFixedWidth(panel.width() - 2 * (m + 10))
    mail.move(m + 10, m + 70 + card.height() + 8)
    mail.show_email(1, "[공지] 특약OS이월 처리 안내", "김팀장",
                    "1. 결재 시스템 승인\n2. 담당자 이메일 공유")
    QTimer.singleShot(600, lambda: panel.flash_alert(pulses=3))

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":  # pragma: no cover
    _gallery()
