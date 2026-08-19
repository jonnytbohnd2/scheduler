"""
outlook_service.py
==================
"Tell me when that mail arrives, and remind me what to do next."

Polls the local Outlook inbox over COM and fires a Qt signal when a message
matches one of the user's ``awaited_emails`` rules.

Design notes
------------
* **COM lives entirely on its own thread.** ``pythoncom.CoInitialize()`` is
  called inside the worker's own ``run``-equivalent slot, not in ``__init__``:
  the apartment belongs to the thread that initialises it, so doing this on the
  GUI thread would make every later Dispatch call marshal (or fail).
* **Nothing here blocks the GUI.** Outlook can take seconds to answer -- or
  hang behind a modal dialog of its own -- so all of it happens on
  ``OutlookThread`` and only signals cross back.
* **Absence is the normal case.** No pywin32, no Outlook installed, Outlook
  closed, a security prompt, a COM server that went away mid-poll: each is
  reported once through :attr:`service_status` and retried later. The app must
  behave exactly as before when Outlook is simply not there.
* **The matcher is pure.** :func:`match_rule` takes plain strings, so the
  matching rules are unit-tested without Outlook, COM, or Windows.
* **Two-step scan, to stay silent.** Outlook's object-model guard pops
  "전자 메일 주소 정보에 액세스하려는 프로그램이 있습니다" when a program reads
  ``SenderName``/``SenderEmailAddress``. ``Subject`` and ``Body`` are exempt.
  So the 10-second pass reads only subject/body (:func:`match_keywords`), and
  the sender is resolved lazily for a single matched item
  (:meth:`OutlookMonitorWorker._sender_of`) via MAPI property tags.

Run ``py outlook_service.py`` for a self-test (matching logic always; a live
one-shot inbox scan only if Outlook happens to be running).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, QTimer, Signal, Slot

from db_manager import split_keywords

log = logging.getLogger(__name__)

#: ``olFolderInbox`` from the Outlook object model.
OL_FOLDER_INBOX = 6

DEFAULT_INTERVAL_S = 10
#: How many of the newest inbox items to inspect per poll. Reading properties
#: is a cross-process COM call each, so this stays small on purpose.
DEFAULT_SCAN_LIMIT = 30
#: Body text is expensive to fetch and can be enormous; only this much is used.
BODY_SCAN_CHARS = 4000


# --------------------------------------------------------------------------- #
# Pure matching logic (no COM, no Qt)
# --------------------------------------------------------------------------- #

def match_keywords(rule: dict, subject: str, body: str = "") -> bool:
    """Keyword half of a rule -- checked against subject/body only.

    Split out from the sender check on purpose: ``Subject`` and ``Body`` are
    safe to read for every message, while touching sender fields pops Outlook's
    security dialog. See :meth:`OutlookMonitorWorker._poll_once`.
    """
    keywords = split_keywords(rule.get("keywords", ""))
    if not keywords:
        return False
    haystack = f"{subject or ''}\n{(body or '')[:BODY_SCAN_CHARS]}".lower()
    return any(word.lower() in haystack for word in keywords)


def match_sender(rule: dict, sender_name: str = "", sender_email: str = "") -> bool:
    """Sender half of a rule. No filter configured -> always passes.

    Case-insensitive substring, so "팀장" matches "김팀장" and a bare domain
    matches any address inside it.
    """
    sender_filter = (rule.get("sender_filter") or "").strip().lower()
    if not sender_filter:
        return True
    return sender_filter in f"{sender_name or ''}\n{sender_email or ''}".lower()


def match_rule(
    rule: dict,
    subject: str,
    body: str = "",
    sender_name: str = "",
    sender_email: str = "",
) -> bool:
    """Full match (keywords **and** sender). Convenience for tests/callers that
    already hold every field."""
    return (match_keywords(rule, subject, body)
            and match_sender(rule, sender_name, sender_email))


# --------------------------------------------------------------------------- #
# Worker (runs on OutlookThread)
# --------------------------------------------------------------------------- #

class OutlookMonitorWorker(QObject):
    """Owns the COM connection. Every slot here executes off the GUI thread."""

    #: (rule_id, subject, sender_name, reminder_action)
    email_matched = Signal(int, str, str, str)
    #: (available, human-readable reason)
    service_status = Signal(bool, str)

    def __init__(self, db, interval_seconds: int = DEFAULT_INTERVAL_S,
                 scan_limit: int = DEFAULT_SCAN_LIMIT,
                 allow_launch: bool = False) -> None:
        super().__init__()
        self.db = db
        self.interval_seconds = max(3, int(interval_seconds))
        self.scan_limit = max(1, int(scan_limit))
        #: Start Outlook if it is closed. Off by default -- see :meth:`_connect`.
        self.allow_launch = bool(allow_launch)

        self._timer: Optional[QTimer] = None
        self._com_ready = False
        self._outlook: Any = None
        self._inbox: Any = None
        self._available = False
        self._last_status: Optional[tuple[bool, str]] = None
        self._busy = threading.Event()
        self._stopping = False
        #: EntryIDs already examined, so a rule added later does not re-fire on
        #: mail that was already sitting in the inbox... and so an unread mail
        #: that stays unread is not re-matched every 10 seconds.
        self._seen: set[str] = set()
        self._primed = False

    # ---- lifecycle -------------------------------------------------------- #

    @Slot()
    def start_polling(self) -> None:
        """Initialise COM on *this* thread and begin the poll timer."""
        if not self._init_com():
            # Still poll: Outlook may be started later, and the retry path
            # re-attempts the connection each tick.
            log.info("Outlook unavailable at startup; will keep retrying")

        self._timer = QTimer()
        self._timer.setInterval(self.interval_seconds * 1000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        QTimer.singleShot(1500, self._poll)          # first look, shortly after boot

    @Slot()
    def stop_polling(self) -> None:
        """Stop the timer and release COM from the thread that initialised it."""
        self._stopping = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._release_com()

    def _init_com(self) -> bool:
        """``CoInitialize`` + import pywin32. Safe to call repeatedly."""
        if self._com_ready:
            return True
        try:
            import pythoncom                                    # noqa: PLC0415
        except ImportError as exc:
            self._emit_status(False, "pywin32 미설치 (Outlook 연동 불가)")
            log.warning("Outlook COM unavailable: %s", exc)
            return False
        try:
            pythoncom.CoInitialize()
            self._com_ready = True
            return True
        except Exception as exc:                                # noqa: BLE001
            log.warning("CoInitialize failed: %s", exc)
            self._emit_status(False, "COM 초기화 실패")
            return False

    def _release_com(self) -> None:
        self._outlook = None
        self._inbox = None
        if not self._com_ready:
            return
        try:
            import pythoncom                                    # noqa: PLC0415

            pythoncom.CoUninitialize()
        except Exception:                                       # noqa: BLE001
            log.debug("CoUninitialize raised", exc_info=True)
        finally:
            self._com_ready = False

    # ---- connection ------------------------------------------------------- #

    def _connect(self) -> bool:
        """Get (or re-get) the MAPI inbox. Returns False when unavailable."""
        if self._inbox is not None:
            return True
        if not self._init_com():
            return False
        try:
            import win32com.client                              # noqa: PLC0415
        except ImportError as exc:
            self._emit_status(False, "pywin32 미설치 (Outlook 연동 불가)")
            log.warning("Outlook COM unavailable: %s", exc)
            return False

        try:
            if self.allow_launch:
                # Dispatch attaches to a running Outlook or starts a new one.
                outlook = win32com.client.Dispatch("Outlook.Application")
            else:
                # Attach only. A background poller must never start Outlook on
                # the user's machine -- Dispatch() silently launches it, which
                # is a surprising side effect for a status checker. When
                # Outlook is closed we simply report that and retry later.
                outlook = win32com.client.GetActiveObject("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")
            inbox = namespace.GetDefaultFolder(OL_FOLDER_INBOX)
            _ = inbox.Name                                       # force a real call
        except Exception as exc:                                 # noqa: BLE001
            self._outlook = self._inbox = None
            self._emit_status(False, self._explain(exc))
            log.debug("Outlook COM unavailable: %s", exc)
            return False

        self._outlook, self._inbox = outlook, inbox
        self._emit_status(True, "Outlook 연결됨")
        log.info("Connected to Outlook inbox")
        return True

    #: HRESULT -> message. pywin32 puts the code in ``exc.args[0]`` as a signed
    #: int and localises ``str(exc)``, so matching on text is unreliable on a
    #: Korean Windows install -- match on the number.
    _COM_ERRORS = {
        -2147221021: "Outlook 미실행 (열면 자동 연결됩니다)",   # MK_E_UNAVAILABLE
        -2147221164: "Outlook이 설치되어 있지 않습니다",        # REGDB_E_CLASSNOTREG
        -2147221005: "Outlook이 설치되어 있지 않습니다",        # CO_E_CLASSSTRING
        -2147023174: "Outlook 연결이 끊어졌습니다 (재시도 중)",  # RPC_S_SERVER_UNAVAILABLE
        -2147417846: "Outlook이 응답하지 않습니다 (대화상자 확인)",  # RPC_E_SERVERCALL_RETRYLATER
        -2146959355: "Outlook 실행 권한 문제 (관리자 권한 차이)",   # CO_E_SERVER_EXEC_FAILURE
        -2147352567: "Outlook이 요청을 거부했습니다 (보안 설정 확인)",  # DISP_E_EXCEPTION
    }

    @classmethod
    def _explain(cls, exc: Exception) -> str:
        """Turn a COM failure into something a user can act on."""
        code = None
        args = getattr(exc, "args", ())
        if args and isinstance(args[0], int):
            code = args[0]
        if code in cls._COM_ERRORS:
            return cls._COM_ERRORS[code]
        text = str(exc)
        if "Invalid class string" in text:
            return "Outlook이 설치되어 있지 않습니다"
        if "RPC" in text.upper():
            return "Outlook 연결이 끊어졌습니다 (재시도 중)"
        return "Outlook 미연결"

    def _emit_status(self, available: bool, reason: str) -> None:
        """Emit only on change -- this runs every 10 s forever."""
        state = (available, reason)
        if state != self._last_status:
            self._last_status = state
            self._available = available
            self.service_status.emit(available, reason)

    # ---- polling ----------------------------------------------------------- #

    @Slot()
    def _poll(self) -> None:
        # A slow Outlook can make a poll outlast its own interval; skip rather
        # than stack up overlapping COM traffic.
        if self._stopping or self._busy.is_set():
            return
        self._busy.set()
        try:
            self._poll_once()
        except Exception as exc:                                 # noqa: BLE001
            # A poll must never kill the timer.
            log.exception("Outlook poll failed")
            self._inbox = None                                   # force reconnect
            self._emit_status(False, self._explain(exc))
        finally:
            self._busy.clear()

    def _poll_once(self) -> None:
        try:
            rules = self.db.get_active_awaited_emails()
        except Exception:                                        # noqa: BLE001
            log.exception("Could not read awaited-email rules")
            return
        if not rules:
            return                       # nothing to watch: skip the COM work
        if not self._connect():
            return

        for item in self._recent_items():
            entry_id = self._safe(item, "EntryID", "")
            if entry_id and entry_id in self._seen:
                continue
            if entry_id:
                self._seen.add(entry_id)

            # First run only fills the "already seen" set: a rule created today
            # must not fire on last week's mail still sitting in the inbox.
            if not self._primed:
                continue

            # Step 1 -- cheap, silent scan. Subject and Body never trigger the
            # Outlook security dialog; SenderName / SenderEmailAddress do, and
            # asking for them on all 30 items every 10 seconds is exactly what
            # made the prompt appear. So the routine pass reads neither.
            subject = self._safe(item, "Subject", "")
            body = self._safe(item, "Body", "")[:BODY_SCAN_CHARS]

            candidates = [r for r in rules if match_keywords(r, subject, body)]
            if not candidates:
                continue

            # Step 2 -- a keyword hit. Now (and only now, for this one item)
            # resolve the sender, preferring MAPI property tags which are not
            # gated by the object-model guard.
            sender_name, sender_email = self._sender_of(item)

            for rule in candidates:
                if not match_sender(rule, sender_name, sender_email):
                    continue
                rule_id = int(rule["id"])
                log.info("Awaited mail matched rule #%d: %r from %r",
                         rule_id, subject[:60], sender_name)
                try:
                    self.db.mark_email_triggered(rule_id)
                except Exception:                                # noqa: BLE001
                    log.exception("Could not mark rule #%d triggered", rule_id)
                self.email_matched.emit(
                    rule_id, subject or "(제목 없음)", sender_name or "(발신자 미상)",
                    rule.get("reminder_action", ""))
                rules = [r for r in rules if int(r["id"]) != rule_id]
                break
            if not rules:
                break

        if not self._primed:
            self._primed = True
            log.info("Outlook baseline captured (%d existing items ignored)",
                     len(self._seen))

        # Keep the seen-set from growing without bound over a long session.
        if len(self._seen) > 4000:
            self._seen = set(list(self._seen)[-2000:])

    def _recent_items(self) -> list:
        """Newest ``scan_limit`` inbox items, unread first.

        Sorting and slicing happen inside Outlook; pulling the whole folder
        into Python would be far slower and can trip mailbox limits.
        """
        try:
            items = self._inbox.Items
            items.Sort("[ReceivedTime]", True)                   # newest first
        except Exception as exc:                                 # noqa: BLE001
            log.warning("Could not read inbox items: %s", exc)
            self._inbox = None
            self._emit_status(False, self._explain(exc))
            return []

        collected: list = []
        try:
            # Restrict to unread when possible -- far fewer COM round-trips.
            unread = items.Restrict("[Unread] = True")
            for index in range(1, min(unread.Count, self.scan_limit) + 1):
                collected.append(unread.Item(index))
        except Exception:                                        # noqa: BLE001
            log.debug("Unread restrict unsupported; falling back", exc_info=True)

        if len(collected) < self.scan_limit:
            try:
                for index in range(1, min(items.Count, self.scan_limit) + 1):
                    collected.append(items.Item(index))
            except Exception as exc:                             # noqa: BLE001
                log.warning("Inbox enumeration failed: %s", exc)
        return collected

    #: MAPI property tags read through PropertyAccessor. These are not covered
    #: by the Outlook object-model guard that protects SenderName /
    #: SenderEmailAddress, so they resolve the sender without a prompt.
    PROP_SENDER_NAME = "http://schemas.microsoft.com/mapi/proptag/0x0C1A001E"
    PROP_SENDER_EMAIL = "http://schemas.microsoft.com/mapi/proptag/0x0C1F001E"

    def _sender_of(self, item: Any) -> tuple[str, str]:
        """Resolve (name, email) for one matched item, quietly if possible.

        PropertyAccessor first; if it raises (older Outlook, protected item,
        or a policy that blocks it too) fall back to the plain attributes. The
        fallback may show the security dialog, but by then it happens once for
        a real hit rather than 30 times a poll.
        """
        name = email = ""
        accessor = None
        try:
            accessor = item.PropertyAccessor
        except Exception:                                        # noqa: BLE001
            accessor = None

        if accessor is not None:
            for tag, slot in ((self.PROP_SENDER_NAME, "name"),
                              (self.PROP_SENDER_EMAIL, "email")):
                try:
                    value = accessor.GetProperty(tag)
                    if value:
                        if slot == "name":
                            name = str(value)
                        else:
                            email = str(value)
                except Exception:                                # noqa: BLE001
                    log.debug("PropertyAccessor %s unavailable", slot, exc_info=True)

        if not name:
            name = self._safe(item, "SenderName", "")
        if not email:
            email = self._safe(item, "SenderEmailAddress", "")
        return name, email

    @staticmethod
    def _safe(item: Any, attribute: str, default: str = "") -> str:
        """Read a COM property defensively.

        Any property can raise: a meeting request has no ``SenderName``, a
        digitally-protected mail refuses ``Body``, and an item deleted between
        the sort and the read raises outright.
        """
        try:
            value = getattr(item, attribute, None)
            return default if value is None else str(value)
        except Exception:                                        # noqa: BLE001
            return default


# --------------------------------------------------------------------------- #
# Controller (GUI-thread facade)
# --------------------------------------------------------------------------- #

class OutlookMonitorController(QObject):
    """Owns ``OutlookThread`` and re-emits the worker's signals."""

    email_matched = Signal(int, str, str, str)
    service_status = Signal(bool, str)

    _do_start = Signal()
    _do_stop = Signal()

    def __init__(self, db, interval_seconds: int = DEFAULT_INTERVAL_S,
                 scan_limit: int = DEFAULT_SCAN_LIMIT,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._available = False
        self._status_message = "확인 중"

        self.thread = QThread()
        self.thread.setObjectName("OutlookThread")
        self.worker = OutlookMonitorWorker(db, interval_seconds, scan_limit)
        self.worker.moveToThread(self.thread)

        self.worker.email_matched.connect(self.email_matched)
        self.worker.service_status.connect(self._on_status)
        self._do_start.connect(self.worker.start_polling)
        self._do_stop.connect(self.worker.stop_polling)

    def start(self) -> None:
        if not self.thread.isRunning():
            self.thread.start()
        self._do_start.emit()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        try:
            if self.thread.isRunning():
                # Blocking, not a plain emit: the poll QTimer belongs to the
                # worker thread and must be stopped *there*. Firing a queued
                # signal and immediately calling quit() races the event loop --
                # the timer then gets torn down from the GUI thread and Qt
                # complains "Timers cannot be stopped from another thread".
                QMetaObject.invokeMethod(
                    self.worker, "stop_polling", Qt.BlockingQueuedConnection)
                self.thread.quit()
                if not self.thread.wait(timeout_ms):
                    log.warning("Outlook thread did not stop in %d ms; terminating",
                                timeout_ms)
                    self.thread.terminate()
                    self.thread.wait(1500)
        except RuntimeError:
            pass

    @property
    def available(self) -> bool:
        return self._available

    @property
    def status_message(self) -> str:
        return self._status_message

    def _on_status(self, available: bool, message: str) -> None:
        self._available = available
        self._status_message = message
        self.service_status.emit(available, message)


# --------------------------------------------------------------------------- #
# Self-test: ``py outlook_service.py``
# --------------------------------------------------------------------------- #

def _selftest() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    failures = 0

    def check(cond: bool, msg: str) -> None:
        nonlocal failures
        print(("  [ok] " if cond else "  [FAIL] ") + msg)
        if not cond:
            failures += 1

    print("--- rule matching (no COM required) ---")
    rule = {"id": 1, "keywords": "특약OS이월, 특약이월",
            "sender_filter": "", "reminder_action": "결재"}
    check(match_rule(rule, "[공지] 특약OS이월 안내"), "keyword in subject")
    check(match_rule(rule, "무관한 제목", "본문에 특약이월 포함"), "keyword in body")
    check(not match_rule(rule, "무관한 제목", "무관한 본문"), "no keyword -> no match")
    check(match_rule(rule, "특약os이월 처리"), "case-insensitive")
    check(not match_rule({"id": 2, "keywords": ""}, "특약OS이월"), "empty keywords never match")

    sender_rule = {"id": 3, "keywords": "결재", "sender_filter": "팀장",
                   "reminder_action": "확인"}
    check(match_rule(sender_rule, "결재 요청", "", "김팀장"), "sender name matches")
    check(not match_rule(sender_rule, "결재 요청", "", "이대리"), "wrong sender filtered out")
    check(match_rule(sender_rule, "결재 요청", "", "", "boss@koreanre.co.kr")
          is False, "sender filter checks address too")
    domain_rule = {"id": 4, "keywords": "정산", "sender_filter": "koreanre.co.kr",
                   "reminder_action": "x"}
    check(match_rule(domain_rule, "정산 자료", "", "", "kim@koreanre.co.kr"),
          "domain filter matches address")

    long_body = "x" * (BODY_SCAN_CHARS + 500) + "특약OS이월"
    check(not match_rule(rule, "제목", long_body), "body scan is bounded")

    print("\n--- 2-step scan: no sender access without a keyword hit ---")

    class GuardedMail:
        """Mimics Outlook's object-model guard: reading sender fields raises,
        as if the user had clicked 'deny' on the security dialog."""

        def __init__(self, entry, subject, body="", name="", email=""):
            self.EntryID = entry
            self.Subject = subject
            self.Body = body
            self._name, self._email = name, email
            self.touched: list[str] = []

        def __getattr__(self, attribute):
            if attribute in ("SenderName", "SenderEmailAddress"):
                object.__getattribute__(self, "touched").append(attribute)
                raise RuntimeError("security dialog / access denied")
            raise AttributeError(attribute)

        @property
        def PropertyAccessor(self):                              # noqa: N802
            outer = self

            class Accessor:
                def GetProperty(self, tag):                      # noqa: N802
                    outer.touched.append("PropertyAccessor")
                    if tag.endswith("0x0C1A001E"):
                        return outer._name
                    if tag.endswith("0x0C1F001E"):
                        return outer._email
                    raise RuntimeError("unknown tag")
            return Accessor()

    worker = OutlookMonitorWorker(type("Db", (), {
        "get_active_awaited_emails": lambda self: [dict(rule)],
        "mark_email_triggered": lambda self, rid: None,
    })(), interval_seconds=3)
    worker._connect = lambda: True
    worker._primed = True

    class Items:
        def __init__(self, mails):
            self._m = mails
        Count = property(lambda self: len(self._m))
        def Sort(self, *a): pass
        def Restrict(self, q): raise RuntimeError("unsupported")
        def Item(self, i): return self._m[i - 1]

    boring = GuardedMail("N1", "점심 메뉴 안내", "관계 없는 본문", "김대리", "kim@x.com")
    worker._inbox = type("Inbox", (), {"Items": Items([boring])})()
    fired: list = []
    worker.email_matched.connect(lambda *a: fired.append(a))
    worker._poll_once()
    check(boring.touched == [], f"non-matching mail: sender untouched ({boring.touched})")
    check(not fired, "non-matching mail did not fire")

    hit = GuardedMail("N2", "[공지] 특약OS이월 처리", "본문", "김팀장", "boss@koreanre.co.kr")
    worker._inbox = type("Inbox", (), {"Items": Items([hit])})()
    worker._poll_once()
    check("PropertyAccessor" in hit.touched, "matching mail: sender read via PropertyAccessor")
    check("SenderName" not in hit.touched,
          f"guarded attribute never used when PropertyAccessor works ({hit.touched})")
    check(len(fired) == 1 and fired[0][2] == "김팀장", f"sender resolved: {fired and fired[0][2]}")

    print("\n--- controller lifecycle (no Outlook needed) ---")
    from PySide6.QtCore import QCoreApplication

    class FakeDb:
        def __init__(self) -> None:
            self.triggered: list[int] = []

        def get_active_awaited_emails(self) -> list[dict]:
            return [dict(rule)]

        def mark_email_triggered(self, rule_id: int) -> None:
            self.triggered.append(rule_id)

    app = QCoreApplication.instance() or QCoreApplication([])
    statuses: list[tuple[bool, str]] = []
    controller = OutlookMonitorController(FakeDb(), interval_seconds=3)
    controller.service_status.connect(lambda ok, msg: statuses.append((ok, msg)))
    controller.start()

    QTimer.singleShot(4000, app.quit)
    app.exec()
    controller.shutdown()

    check(not controller.thread.isRunning(), "thread joined cleanly")
    if statuses:
        ok, message = statuses[-1]
        print(f"       Outlook status: available={ok} ({message})")
        check(True, "status reported without crashing")
    else:
        check(True, "no status change (Outlook state unchanged)")

    print("\noutlook_service self-test:", "OK" if failures == 0 else f"{failures} FAILURE(S)")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    _selftest()
