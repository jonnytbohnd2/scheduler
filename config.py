"""
config.py
=========
User settings + the visual theme derived from them.

Settings live in a plain, human-readable ``config.json`` next to the executable
so they survive re-installs, can be edited by hand, diffed, or copied to another
air-gapped machine. Nothing is stored in the registry.

Two objects matter:

* :class:`Config`  -- the persisted values (appearance / window / behavior / llm)
* :class:`Style`   -- colours and pixel metrics computed *from* a Config

``Style`` is the single source of truth for every colour and size in the UI.
Widgets read it through :func:`style` at build and paint time, so changing a
setting and calling :func:`set_style` restyles the whole app.

Readability rule (learned the hard way): **text colours are always fully
opaque.** Only backgrounds and borders carry alpha. A translucent panel over a
bright wallpaper must never swallow its own text.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtGui import QColor

log = logging.getLogger(__name__)

CONFIG_VERSION = 1
CONFIG_FILENAME = "config.json"


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULTS: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "appearance": {
        "theme": "dark",            # dark | midnight | slate | light | contrast
        "accent": "sky",            # sky | violet | emerald | amber | rose | custom
        "accent_custom": "#38BDF8",
        "font_scale": 1.0,          # 0.80 – 1.40
        "density": "compact",       # compact | normal | roomy
        "radius": "medium",         # sharp | small | medium | round
        "panel_alpha": 0.88,        # panel fill opacity when active
        "idle_panel_alpha": 0.42,   # panel fill opacity when idle  (see-through)
        "idle_opacity": 0.72,       # whole-window opacity when idle
        "hover_opacity": 1.0,       # whole-window opacity when hovered/focused
        "show_seconds": True,
        "show_summary": True,
        "show_countdown": True,
        "shadow": True,
    },
    "window": {
        "width": 290,
        "height": 360,
        "x": None,
        "y": None,
        "corner": "top-right",      # top-right | bottom-right | top-left | bottom-left
        "locked": False,
        "always_on_top": True,
        "start_minimized": False,
        "remember_position": True,
        "last_tab": 0,
        "collapsed": False,
        "schedule_filter": "all",   # all | today | recurring
    },
    "behavior": {
        "poll_seconds": 5,          # 1 – 60
        "snooze_minutes": 5,
        "sound_enabled": True,
        "sound_volume": 0.45,
        "tray_balloon": True,
        "notification_seconds": 25,
        "alert_pulses": 4,
        "flash_on_alert": True,
        "confirm_delete": False,
        "hide_completed": False,
        "autostart_hint_shown": False,
        # Awaited-email monitoring (Outlook COM)
        "outlook_enabled": True,
        "outlook_poll_seconds": 10,
    },
    "llm": {
        "model_path": "",           # empty -> auto-discover in models/
        "n_ctx": 4096,
        "n_threads": 0,             # 0 = auto (half the cores)
        "max_tokens": 512,
        "temperature": 0.6,         # matches llm_engine.TEMP_CHAT
        "preload": True,
        "thinking": "hide",         # off | hide | show
        "use_llm_for_parsing": True,
        "history_turns": 12,
    },
}


# --------------------------------------------------------------------------- #
# Palettes
# --------------------------------------------------------------------------- #
# Each palette lists RGB triples. Alpha is applied later so that a single
# palette can serve both the solid (hover) and see-through (idle) states.

THEMES: dict[str, dict[str, Any]] = {
    "dark": {
        "label": "다크 (기본)",
        "base": (15, 23, 42),        # #0F172A slate-900  -- panel
        "soft": (30, 41, 59),        # #1E293B slate-800  -- cards
        "softer": (51, 65, 85),      # #334155 slate-700  -- inputs
        "line": (148, 163, 184),     # #94A3B8 hairlines
        "text": (248, 250, 252),     # #F8FAFC  primary   (always opaque)
        "text_dim": (148, 163, 184), # #94A3B8  secondary (always opaque)
        "is_light": False,
    },
    "midnight": {
        "label": "미드나잇 (더 어둡게)",
        "base": (8, 10, 16),
        "soft": (20, 24, 34),
        "softer": (36, 42, 56),
        "line": (130, 143, 166),
        "text": (240, 244, 250),
        "text_dim": (158, 172, 192),
        "is_light": False,
    },
    "slate": {
        "label": "슬레이트 (부드럽게)",
        "base": (28, 33, 44),
        "soft": (44, 51, 66),
        "softer": (64, 74, 94),
        "line": (160, 172, 192),
        "text": (238, 242, 248),
        "text_dim": (172, 184, 202),
        "is_light": False,
    },
    "light": {
        "label": "라이트",
        "base": (248, 250, 252),
        "soft": (241, 245, 249),
        "softer": (226, 232, 240),
        "line": (100, 116, 139),
        "text": (15, 23, 42),
        "text_dim": (71, 85, 105),
        "is_light": True,
    },
    "contrast": {
        "label": "고대비 (가독성 우선)",
        "base": (0, 0, 0),
        "soft": (18, 18, 18),
        "softer": (38, 38, 38),
        "line": (200, 200, 200),
        "text": (255, 255, 255),
        "text_dim": (215, 215, 215),
        "is_light": False,
    },
}

ACCENTS: dict[str, tuple[str, str]] = {
    #  key      label          hex
    "sky":     ("스카이 블루", "#38BDF8"),
    "violet":  ("바이올렛",    "#A78BFA"),
    "emerald": ("에메랄드",    "#34D399"),
    "amber":   ("앰버",        "#FBBF24"),
    "rose":    ("로즈",        "#FB7185"),
    "custom":  ("직접 지정",   "#38BDF8"),
}

DENSITIES: dict[str, dict[str, int]] = {
    #             pad  gap  row  ctl  title  card
    # "compact" is the default and is tuned for zero wasted vertical space:
    # the header carries the tab strip inline, so `title` is the entire chrome.
    "compact": {"pad": 4,  "gap": 3, "row": 32, "ctl": 24, "title": 26, "card": 4},
    "normal":  {"pad": 8,  "gap": 5, "row": 44, "ctl": 28, "title": 29, "card": 7},
    "roomy":   {"pad": 13, "gap": 8, "row": 56, "ctl": 33, "title": 33, "card": 10},
}

RADII: dict[str, tuple[int, int]] = {
    #          panel, card      (modern glass look lives around 14 / 12)
    "sharp":  (6, 4),
    "small":  (10, 8),
    "medium": (14, 12),
    "round":  (22, 16),
}

# Semantic colours are theme-independent (they must stay recognisable), but the
# light theme needs darker variants to keep contrast on a pale background.
SEMANTIC_DARK = {
    "success": (52, 211, 153),
    "warn":    (251, 191, 36),
    "danger":  (248, 113, 113),
    "info":    (56, 189, 248),
}
SEMANTIC_LIGHT = {
    "success": (4, 120, 87),
    "warn":    (180, 83, 9),
    "danger":  (190, 18, 60),
    "info":    (2, 132, 199),
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively overlay ``override`` on a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return fallback


class Config:
    """Loads / saves ``config.json`` and validates every value it hands out.

    Unknown keys are preserved on save (forward compatibility), and any value
    that fails validation silently reverts to its default rather than raising --
    a corrupt settings file must never stop the app from starting.
    """

    def __init__(self, base_dir: str, data: Optional[dict] = None) -> None:
        self.base_dir = base_dir
        self.path = os.path.join(base_dir, CONFIG_FILENAME)
        self.data = _deep_merge(DEFAULTS, data or {})
        self._sanitise()

    # ---------------- persistence ---------------- #

    @classmethod
    def load(cls, base_dir: str) -> "Config":
        path = os.path.join(base_dir, CONFIG_FILENAME)
        raw: dict = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    raw = loaded
                else:
                    log.warning("config.json is not an object; using defaults")
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Could not read config.json (%s); using defaults", exc)
                cls._backup_broken(path)
        return cls(base_dir, raw)

    @staticmethod
    def _backup_broken(path: str) -> None:
        """Keep a corrupt file around instead of overwriting it silently."""
        try:
            backup = path + ".broken"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
            log.warning("Corrupt config saved as %s", os.path.basename(backup))
        except OSError:
            pass

    def save(self) -> bool:
        """Atomic write. Returns False (and logs) instead of raising."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            log.error("Could not save config.json: %s", exc)
            return False

    def reset(self) -> None:
        self.data = copy.deepcopy(DEFAULTS)

    # ---------------- access ---------------- #

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value: Any) -> None:
        self.data.setdefault(section, {})[key] = value

    # Shorthand section accessors used all over the app.
    @property
    def appearance(self) -> dict:
        return self.data["appearance"]

    @property
    def window(self) -> dict:
        return self.data["window"]

    @property
    def behavior(self) -> dict:
        return self.data["behavior"]

    @property
    def llm(self) -> dict:
        return self.data["llm"]

    # ---------------- validation ---------------- #

    def _sanitise(self) -> None:
        """Force every value into a sane range. Never raises."""
        a, w, b, m = (self.data["appearance"], self.data["window"],
                      self.data["behavior"], self.data["llm"])

        if a.get("theme") not in THEMES:
            a["theme"] = "dark"
        if a.get("accent") not in ACCENTS:
            a["accent"] = "sky"
        if a.get("density") not in DENSITIES:
            a["density"] = "compact"
        if a.get("radius") not in RADII:
            a["radius"] = "medium"
        if not QColor(str(a.get("accent_custom", ""))).isValid():
            a["accent_custom"] = "#38BDF8"
        a["font_scale"] = round(_clamp(a.get("font_scale"), 0.80, 1.40, 1.0), 2)
        a["panel_alpha"] = round(_clamp(a.get("panel_alpha"), 0.20, 1.0, 0.88), 2)
        a["idle_panel_alpha"] = round(_clamp(a.get("idle_panel_alpha"), 0.0, 1.0, 0.42), 2)
        # 0.35 floor: below this, even pure white text stops being readable.
        a["idle_opacity"] = round(_clamp(a.get("idle_opacity"), 0.35, 1.0, 0.72), 2)
        a["hover_opacity"] = round(_clamp(a.get("hover_opacity"), 0.60, 1.0, 1.0), 2)
        for flag in ("show_seconds", "show_summary", "show_countdown", "shadow"):
            a[flag] = bool(a.get(flag, True))

        w["width"] = int(_clamp(w.get("width"), 240, 900, 290))
        w["height"] = int(_clamp(w.get("height"), 150, 1400, 360))
        if w.get("corner") not in ("top-right", "bottom-right", "top-left", "bottom-left"):
            w["corner"] = "top-right"
        for flag in ("locked", "always_on_top", "start_minimized",
                     "remember_position", "collapsed"):
            w[flag] = bool(w.get(flag, False))
        w["last_tab"] = 1 if w.get("last_tab") == 1 else 0
        if w.get("schedule_filter") not in ("all", "today", "recurring"):
            w["schedule_filter"] = "all"
        for axis in ("x", "y"):
            try:
                w[axis] = None if w.get(axis) is None else int(w[axis])
            except (TypeError, ValueError):
                w[axis] = None

        b["poll_seconds"] = int(_clamp(b.get("poll_seconds"), 1, 60, 5))
        b["snooze_minutes"] = int(_clamp(b.get("snooze_minutes"), 1, 240, 5))
        b["sound_volume"] = round(_clamp(b.get("sound_volume"), 0.0, 1.0, 0.45), 2)
        b["notification_seconds"] = int(_clamp(b.get("notification_seconds"), 0, 300, 25))
        b["alert_pulses"] = int(_clamp(b.get("alert_pulses"), 0, 12, 4))
        b["outlook_poll_seconds"] = int(_clamp(b.get("outlook_poll_seconds"), 3, 600, 10))
        for flag in ("sound_enabled", "tray_balloon", "flash_on_alert",
                     "confirm_delete", "hide_completed", "autostart_hint_shown"):
            b[flag] = bool(b.get(flag, False))
        b["outlook_enabled"] = bool(b.get("outlook_enabled", True))

        m["n_ctx"] = int(_clamp(m.get("n_ctx"), 512, 32768, 4096))
        m["n_threads"] = int(_clamp(m.get("n_threads"), 0, 128, 0))
        m["max_tokens"] = int(_clamp(m.get("max_tokens"), 64, 8192, 512))
        m["temperature"] = round(_clamp(m.get("temperature"), 0.0, 2.0, 0.6), 2)
        m["history_turns"] = int(_clamp(m.get("history_turns"), 0, 50, 12))
        if m.get("thinking") not in ("off", "hide", "show"):
            m["thinking"] = "hide"
        m["preload"] = bool(m.get("preload", True))
        m["use_llm_for_parsing"] = bool(m.get("use_llm_for_parsing", True))
        m["model_path"] = str(m.get("model_path") or "")

        self.data["version"] = CONFIG_VERSION


# --------------------------------------------------------------------------- #
# Style
# --------------------------------------------------------------------------- #

@dataclass
class Style:
    """Every colour and pixel metric the UI needs, derived from a Config."""

    # colours -------------------------------------------------------------
    bg: QColor = field(default_factory=lambda: QColor(15, 23, 42, 224))
    bg_idle: QColor = field(default_factory=lambda: QColor(15, 23, 42, 107))
    soft: QColor = field(default_factory=lambda: QColor(30, 41, 59, 150))
    softer: QColor = field(default_factory=lambda: QColor(51, 65, 85, 110))
    line: QColor = field(default_factory=lambda: QColor(148, 163, 184, 70))
    line_strong: QColor = field(default_factory=lambda: QColor(148, 163, 184, 120))
    text: QColor = field(default_factory=lambda: QColor(237, 242, 249))
    text_dim: QColor = field(default_factory=lambda: QColor(163, 179, 199))
    accent: QColor = field(default_factory=lambda: QColor(56, 189, 248))
    success: QColor = field(default_factory=lambda: QColor(52, 211, 153))
    warn: QColor = field(default_factory=lambda: QColor(251, 191, 36))
    danger: QColor = field(default_factory=lambda: QColor(248, 113, 113))
    on_accent: QColor = field(default_factory=lambda: QColor(8, 14, 24))

    # metrics -------------------------------------------------------------
    pad: int = 7
    gap: int = 4
    row_h: int = 40
    ctl_h: int = 26
    title_h: int = 26
    card_pad: int = 6
    radius: int = 13
    card_radius: int = 9
    glow_margin: int = 7

    # fonts (pt-like px sizes, already scaled)
    f_xs: int = 10
    f_sm: int = 11
    f_md: int = 12
    f_lg: int = 14

    # flags ---------------------------------------------------------------
    is_light: bool = False
    shadow: bool = True
    show_seconds: bool = True
    show_summary: bool = True
    show_countdown: bool = True

    # ---------------- helpers ---------------- #

    def alpha(self, color: QColor, alpha: int) -> QColor:
        """Same colour, explicit alpha (0-255)."""
        out = QColor(color)
        out.setAlpha(max(0, min(255, int(alpha))))
        return out

    def css(self, color: QColor, alpha: Optional[int] = None) -> str:
        """QColor -> ``rgba(r, g, b, a)`` for stylesheets."""
        a = color.alpha() if alpha is None else max(0, min(255, int(alpha)))
        return f"rgba({color.red()}, {color.green()}, {color.blue()}, {a / 255:.3f})"


def build_style(config: Config) -> Style:
    """Compute a :class:`Style` from the appearance section of ``config``."""
    a = config.appearance
    theme = THEMES.get(a["theme"], THEMES["dark"])
    density = DENSITIES.get(a["density"], DENSITIES["compact"])
    panel_r, card_r = RADII.get(a["radius"], RADII["medium"])
    scale = float(a["font_scale"])

    accent_hex = (a["accent_custom"] if a["accent"] == "custom"
                  else ACCENTS.get(a["accent"], ACCENTS["sky"])[1])
    accent = QColor(accent_hex)
    if not accent.isValid():
        accent = QColor("#38BDF8")

    semantic = SEMANTIC_LIGHT if theme["is_light"] else SEMANTIC_DARK
    panel_alpha = int(255 * float(a["panel_alpha"]))
    idle_alpha = int(255 * float(a["idle_panel_alpha"]))

    def rgb(key: str, alpha_: int = 255) -> QColor:
        r, g, b = theme[key]
        return QColor(r, g, b, alpha_)

    # Text on top of the accent colour: pick black or white by luminance so
    # buttons stay readable with any accent, including custom ones.
    luminance = (0.299 * accent.red() + 0.587 * accent.green() + 0.114 * accent.blue())
    on_accent = QColor(10, 15, 25) if luminance > 150 else QColor(255, 255, 255)

    def px(base: float) -> int:
        return max(8, int(round(base * scale)))

    return Style(
        bg=rgb("base", panel_alpha),
        bg_idle=rgb("base", idle_alpha),
        soft=rgb("soft", int(panel_alpha * 0.62)),
        softer=rgb("softer", int(panel_alpha * 0.48)),
        line=rgb("line", 66 if not theme["is_light"] else 90),
        line_strong=rgb("line", 120 if not theme["is_light"] else 140),
        text=rgb("text"),                     # always opaque
        text_dim=rgb("text_dim"),             # always opaque
        accent=accent,
        success=QColor(*semantic["success"]),
        warn=QColor(*semantic["warn"]),
        danger=QColor(*semantic["danger"]),
        on_accent=on_accent,
        pad=density["pad"],
        gap=density["gap"],
        row_h=px(density["row"]),
        ctl_h=px(density["ctl"]),
        title_h=px(density["title"]),
        card_pad=density["card"],
        radius=panel_r,
        card_radius=card_r,
        # Halo reserve *inside* the widget bounds, so it costs real window
        # space on all four sides. 5 px still reads as a glow while giving
        # 4 px back to content in each dimension.
        glow_margin=5,
        f_xs=px(10),
        f_sm=px(11),
        f_md=px(12),
        f_lg=px(14),
        is_light=theme["is_light"],
        shadow=bool(a["shadow"]),
        show_seconds=bool(a["show_seconds"]),
        show_summary=bool(a["show_summary"]),
        show_countdown=bool(a["show_countdown"]),
    )


# --------------------------------------------------------------------------- #
# Global style singleton
# --------------------------------------------------------------------------- #

_STYLE = Style()


def style() -> Style:
    """The style every widget paints with."""
    return _STYLE


def set_style(new_style: Style) -> Style:
    """Swap the active style (call before rebuilding / restyling widgets)."""
    global _STYLE
    _STYLE = new_style
    return _STYLE


# --------------------------------------------------------------------------- #
# Self-test: ``py config.py``
# --------------------------------------------------------------------------- #

def _selftest() -> None:  # pragma: no cover
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    tmp = tempfile.mkdtemp(prefix="hud_cfg_")

    cfg = Config.load(tmp)
    assert cfg.appearance["theme"] == "dark"
    assert cfg.window["width"] == 290 and cfg.window["height"] == 360
    assert cfg.save() and os.path.isfile(cfg.path)

    # Out-of-range and garbage values must be clamped, not crash.
    hostile = Config(tmp, {
        "appearance": {"theme": "neon", "font_scale": 99, "idle_opacity": 0.0,
                       "accent": "chartreuse", "accent_custom": "not-a-color"},
        "window": {"width": -5, "last_tab": 77, "x": "abc"},
        "behavior": {"poll_seconds": 0},
        "llm": {"n_ctx": 10, "thinking": "maybe", "temperature": "hot"},
    })
    assert hostile.appearance["theme"] == "dark"
    assert hostile.appearance["font_scale"] == 1.40
    assert hostile.appearance["idle_opacity"] == 0.35          # readability floor
    assert hostile.appearance["accent"] == "sky"
    assert hostile.appearance["accent_custom"] == "#38BDF8"
    assert hostile.window["width"] == 240 and hostile.window["x"] is None
    assert hostile.window["last_tab"] == 0
    assert hostile.behavior["poll_seconds"] == 1
    assert hostile.llm["n_ctx"] == 512 and hostile.llm["thinking"] == "hide"
    assert hostile.llm["temperature"] == 0.6

    # A corrupt file must not stop startup.
    with open(os.path.join(tmp, CONFIG_FILENAME), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json ")
    recovered = Config.load(tmp)
    assert recovered.appearance["theme"] == "dark"
    assert os.path.isfile(os.path.join(tmp, CONFIG_FILENAME + ".broken"))

    # Styles for every theme/density/accent combination must build cleanly and
    # keep text fully opaque.
    count = 0
    for theme in THEMES:
        for density in DENSITIES:
            for accent in ACCENTS:
                c = Config(tmp, {"appearance": {"theme": theme, "density": density,
                                                "accent": accent, "font_scale": 1.2}})
                s = build_style(c)
                assert s.text.alpha() == 255 and s.text_dim.alpha() == 255, (theme, accent)
                assert s.row_h > 20 and s.f_md >= 8
                count += 1
    print(f"config self-test OK  ({count} style combinations, {tmp})")


if __name__ == "__main__":  # pragma: no cover
    _selftest()
