"""
crash_handler.py
================
"It must never just disappear."

Everything here exists so that a failure produces an *explanation* -- on screen
and on disk -- instead of a window that vanishes.

Layers, from softest to hardest failure:

1. :func:`guard` / :func:`guarded` -- decorator for Qt slots. A raising slot is
   logged and reported to the UI, and the app keeps running.
2. :func:`install` -> ``sys.excepthook`` -- an unhandled Python exception on the
   main thread writes ``logs/crash_*.txt`` and shows a dialog with the reason
   and the log path. The user chooses whether to continue or quit.
3. ``threading.excepthook`` -- same treatment for worker threads (the app keeps
   running; the thread is already dead).
4. ``faulthandler`` -- a native crash inside llama.cpp cannot be caught by
   Python, so the C-level traceback is streamed to ``logs/faulthandler.log``
   while the process is still alive. That file is the only evidence such a
   crash leaves behind.
5. Qt's own message handler is routed into the logging system, so Qt warnings
   land in the same file as everything else.

All log files live in ``logs/`` next to the executable.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import platform
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Optional

log = logging.getLogger("crash")

_STATE: dict[str, Any] = {
    "log_dir": "",
    "app_name": "OfflineSmartHUD",
    "reporter": None,       # optional callable(title, detail) -> None (UI toast)
    "dialog": True,
    "fault_file": None,
    "crash_count": 0,
}

MAX_DIALOGS = 3             # after this many, log silently instead of nagging


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

def log_dir(base_dir: str) -> str:
    """``<base>/logs``, created if possible; falls back to ``<base>``."""
    target = os.path.join(base_dir, "logs")
    try:
        os.makedirs(target, exist_ok=True)
        return target
    except OSError:
        return base_dir


def setup_logging(base_dir: str, verbose: bool = False) -> str:
    """Configure console + rotating file logging. Returns the log directory."""
    directory = log_dir(base_dir)
    _STATE["log_dir"] = directory

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s")

    # A frozen --windowed build has no stdout; guard against that.
    if sys.stdout is not None:
        try:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(fmt)
            root.addHandler(console)
        except Exception:                              # noqa: BLE001
            pass

    try:
        handler = RotatingFileHandler(
            os.path.join(directory, "hud.log"),
            maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError as exc:
        root.warning("File logging unavailable: %s", exc)

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    return directory


def install(base_dir: str, app_name: str = "OfflineSmartHUD",
            show_dialog: bool = True, reporter: Optional[Callable[[str, str], None]] = None) -> None:
    """Install every safety net. Safe to call once at startup."""
    _STATE["log_dir"] = _STATE["log_dir"] or log_dir(base_dir)
    _STATE["app_name"] = app_name
    _STATE["dialog"] = show_dialog
    _STATE["reporter"] = reporter

    sys.excepthook = _excepthook
    try:
        threading.excepthook = _thread_excepthook
    except Exception:                                  # noqa: BLE001
        pass

    _enable_faulthandler()
    _install_qt_message_handler()
    log.info("Crash handlers installed (logs: %s)", _STATE["log_dir"])


def set_reporter(reporter: Optional[Callable[[str, str], None]]) -> None:
    """Register a UI callback used to surface non-fatal errors."""
    _STATE["reporter"] = reporter


def _enable_faulthandler() -> None:
    """Stream native (C-level) crashes to a file.

    llama.cpp runs as compiled code; if it segfaults, Python never sees an
    exception and the process dies instantly. faulthandler writes the C stack
    to this file *as it happens*, which is the only forensic trail available.
    """
    try:
        path = os.path.join(_STATE["log_dir"], "faulthandler.log")
        handle = open(path, "a", encoding="utf-8", buffering=1)      # noqa: SIM115
        handle.write(f"\n=== session {datetime.now():%Y-%m-%d %H:%M:%S} "
                     f"pid={os.getpid()} ===\n")
        faulthandler.enable(file=handle, all_threads=True)
        _STATE["fault_file"] = handle                                # keep it open
    except Exception as exc:                            # noqa: BLE001
        log.debug("faulthandler unavailable: %s", exc)


def _install_qt_message_handler() -> None:
    """Route Qt's own warnings into the Python log instead of stderr."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        qt_log = logging.getLogger("qt")
        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def handler(mode, context, message):            # noqa: ANN001
            qt_log.log(levels.get(mode, logging.INFO), "%s", message)

        qInstallMessageHandler(handler)
    except Exception as exc:                            # noqa: BLE001
        log.debug("Qt message handler not installed: %s", exc)


# --------------------------------------------------------------------------- #
# Crash reports
# --------------------------------------------------------------------------- #

def _environment() -> str:
    try:
        from PySide6 import __version__ as pyside_version
    except Exception:                                   # noqa: BLE001
        pyside_version = "?"
    try:
        import llama_cpp

        llama_version = getattr(llama_cpp, "__version__", "?")
    except Exception:                                   # noqa: BLE001
        llama_version = "미설치"
    return (
        f"시각      : {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"앱        : {_STATE['app_name']}\n"
        f"실행 형태 : {'빌드(exe)' if getattr(sys, 'frozen', False) else '소스'}\n"
        f"Python    : {sys.version.split()[0]}\n"
        f"PySide6   : {pyside_version}\n"
        f"llama_cpp : {llama_version}\n"
        f"OS        : {platform.platform()}\n"
        f"경로      : {os.path.abspath(os.getcwd())}\n"
    )


def write_crash_report(exc_type, exc_value, exc_tb, context: str = "") -> str:
    """Write ``logs/crash_<timestamp>.txt``. Returns the path (or "")."""
    try:
        directory = _STATE["log_dir"] or log_dir(os.getcwd())
        os.makedirs(directory, exist_ok=True)
        # Milliseconds: two failures inside one second must not overwrite
        # each other -- the second report is often the more informative one.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_") + f"{datetime.now().microsecond // 1000:03d}"
        path = os.path.join(directory, f"crash_{stamp}.txt")
        body = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"{_STATE['app_name']} 오류 보고서\n")
            handle.write("=" * 60 + "\n\n")
            if context:
                handle.write(f"상황: {context}\n\n")
            handle.write(_environment())
            handle.write("\n" + "-" * 60 + "\n오류 내용\n" + "-" * 60 + "\n")
            handle.write(body)
            handle.write("\n" + "-" * 60 + "\n실행 중인 스레드\n" + "-" * 60 + "\n")
            for thread in threading.enumerate():
                handle.write(f"  {thread.name} (alive={thread.is_alive()})\n")
        log.error("Crash report written: %s", path)
        return path
    except Exception:                                   # noqa: BLE001
        log.exception("Could not write crash report")
        return ""


def _short_reason(exc_value: BaseException) -> str:
    text = str(exc_value).strip() or exc_value.__class__.__name__
    return text if len(text) <= 300 else text[:297] + "…"


def _show_dialog(title: str, reason: str, report_path: str, fatal: bool) -> bool:
    """Show the failure dialog. Returns True if the user wants to quit.

    Falls back to console output when Qt is unusable (which is exactly when
    things are worst), so an explanation is always produced.
    """
    # Staying alive is always the default. The app only exits when the user
    # explicitly picks "종료" in the dialog below; a suppressed or unavailable
    # dialog must never turn into a silent disappearance.
    if not _STATE["dialog"]:
        return False
    _STATE["crash_count"] += 1
    if _STATE["crash_count"] > MAX_DIALOGS:
        log.warning("Suppressing further crash dialogs (%d shown)", MAX_DIALOGS)
        return False

    detail = (
        f"{reason}\n\n"
        f"기록 위치:\n{report_path or _STATE['log_dir']}\n\n"
        "이 파일을 첨부하시면 원인 파악에 도움이 됩니다."
    )
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            raise RuntimeError("no QApplication")

        box = QMessageBox()
        box.setWindowTitle(f"{_STATE['app_name']} - 오류")
        box.setIcon(QMessageBox.Critical if fatal else QMessageBox.Warning)
        box.setText(title)
        box.setInformativeText(detail)
        if report_path:
            box.setDetailedText(_tail(report_path))
        if fatal:
            quit_btn = box.addButton("종료", QMessageBox.AcceptRole)
            box.addButton("계속 시도", QMessageBox.RejectRole)
            box.exec()
            return box.clickedButton() is quit_btn
        box.addButton("확인", QMessageBox.AcceptRole)
        open_btn = box.addButton("폴더 열기", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is open_btn:
            _open_folder(report_path or _STATE["log_dir"])
        return False
    except Exception:                                   # noqa: BLE001
        # Qt is gone -- say it on the console and in the log at least.
        message = f"[{_STATE['app_name']}] {title}\n{detail}"
        try:
            print(message, file=sys.stderr, flush=True)
        except Exception:                               # noqa: BLE001
            pass
        log.critical(message)
        return False                                    # keep running regardless


def _tail(path: str, limit: int = 2500) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()[-limit:]
    except OSError:
        return ""


def _open_folder(path: str) -> None:
    try:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        os.startfile(target)                            # noqa: S606
    except Exception:                                   # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #

def _excepthook(exc_type, exc_value, exc_tb) -> None:
    """Unhandled exception on the main thread."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
    path = write_crash_report(exc_type, exc_value, exc_tb, "메인 스레드 예외")
    should_quit = _show_dialog(
        "예기치 못한 오류가 발생했습니다.", _short_reason(exc_value), path, fatal=True)
    if should_quit:
        _quit_app()


def _thread_excepthook(args) -> None:                   # noqa: ANN001
    """Unhandled exception on a worker thread -- report, keep the app alive."""
    if issubclass(args.exc_type, SystemExit):
        return
    name = getattr(args.thread, "name", "?")
    log.critical("Unhandled exception in thread %s", name,
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    path = write_crash_report(args.exc_type, args.exc_value, args.exc_traceback,
                              f"백그라운드 스레드 예외 ({name})")
    report = _STATE.get("reporter")
    if callable(report):
        try:
            report("백그라운드 오류", _short_reason(args.exc_value))
        except Exception:                               # noqa: BLE001
            pass
    else:
        _show_dialog("백그라운드 작업에서 오류가 발생했습니다.",
                     _short_reason(args.exc_value), path, fatal=False)


def _quit_app() -> None:
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.quit()
            return
    except Exception:                                   # noqa: BLE001
        pass
    os._exit(1)


# --------------------------------------------------------------------------- #
# Slot guard
# --------------------------------------------------------------------------- #

def guard(context: str = "", toast: bool = True, default: Any = None) -> Callable:
    """Decorator: log and surface exceptions instead of letting them escape.

    Used on every Qt slot that touches the database, the model or the file
    system -- one bad row must not take the window down with it.

        @guard("일정 저장")
        def save(self): ...
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as exc:                    # noqa: BLE001
                label = context or getattr(func, "__name__", "작업")
                logging.getLogger(func.__module__).exception("Error in %s", label)
                write_crash_report(type(exc), exc, exc.__traceback__, f"{label} 처리 중")
                reporter = _STATE.get("reporter")
                if toast and callable(reporter):
                    try:
                        reporter(label, _short_reason(exc))
                    except Exception:                   # noqa: BLE001
                        pass
                return default
        wrapper.__name__ = getattr(func, "__name__", "guarded")
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator


def guarded(func: Callable) -> Callable:
    """``@guarded`` -- :func:`guard` with default settings."""
    return guard()(func)


def shutdown() -> None:
    """Close the faulthandler file cleanly on exit."""
    handle = _STATE.get("fault_file")
    if handle is not None:
        try:
            faulthandler.disable()
            handle.close()
        except Exception:                               # noqa: BLE001
            pass
        _STATE["fault_file"] = None


# --------------------------------------------------------------------------- #
# Self-test: ``py crash_handler.py``
# --------------------------------------------------------------------------- #

def _selftest() -> None:  # pragma: no cover
    import glob
    import tempfile

    tmp = tempfile.mkdtemp(prefix="hud_crash_")
    setup_logging(tmp, verbose=False)
    seen: list[tuple[str, str]] = []
    install(tmp, show_dialog=False, reporter=lambda t, d: seen.append((t, d)))

    # 1. guard keeps the caller alive and returns the default
    @guard("테스트 작업", default="fallback")
    def boom():
        raise ValueError("의도된 오류")

    assert boom() == "fallback"
    assert seen and seen[0][0] == "테스트 작업", seen
    print("  [ok] guard swallowed the exception and reported it")

    # 2. a crash report was written and contains the traceback
    reports = glob.glob(os.path.join(tmp, "logs", "crash_*.txt"))
    assert reports, "no crash report written"
    body = open(reports[0], encoding="utf-8").read()
    assert "의도된 오류" in body and "ValueError" in body
    assert "Python" in body and "PySide6" in body
    print(f"  [ok] crash report has traceback + environment ({os.path.basename(reports[0])})")

    # 3. thread exceptions are captured
    def explode():
        raise RuntimeError("스레드 오류")

    thread = threading.Thread(target=explode, name="TestThread")
    thread.start()
    thread.join()
    assert any("스레드 오류" in d for _, d in seen), seen
    print("  [ok] thread excepthook reported")

    # 4. main-thread hook writes a report and does NOT kill the process
    try:
        raise KeyError("메인 오류")
    except KeyError:
        _excepthook(*sys.exc_info())
    assert len(glob.glob(os.path.join(tmp, "logs", "crash_*.txt"))) >= 3
    print("  [ok] main excepthook wrote a report and the process survived")

    # 5. faulthandler is live
    assert faulthandler.is_enabled()
    assert os.path.isfile(os.path.join(tmp, "logs", "faulthandler.log"))
    print("  [ok] faulthandler armed for native crashes")

    shutdown()
    print(f"crash_handler self-test OK  ({tmp})")


if __name__ == "__main__":  # pragma: no cover
    _selftest()
