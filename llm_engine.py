"""
llm_engine.py
=============
Local-only language understanding, in three layers:

1. :class:`HeuristicParser` -- a dependency-free Korean/English date parser.
   It is the **primary** schedule extractor: instant, deterministic, and
   measurably more accurate on Korean relative dates than a 1.7B model.
2. :class:`LlmWorker` -- owns ``llama_cpp.Llama`` on a background ``QThread``.
   Handles chat streaming, Qwen3 ``<think>`` blocks, and JSON extraction.
3. :class:`LlmController` -- the GUI-thread facade.

Why the heuristic leads
-----------------------
Measured on Qwen3-1.7B-Q4_K_M with grammar-constrained JSON:

    "다음주 수요일 오후 2시 반에 치과"  -> 2026-08-17, repeat=weekly   (both wrong)
    "담달 첫째주 금요일에 회식"          -> 2026-08-14, repeat=weekly   (both wrong)

A hallucinated *recurring* alarm is the worst failure this app can produce, so
the routing rule is absolute:

* **Any** concrete date or clock token -> :attr:`ParseResult.definite` -> the
  heuristic result is final. No LLM job is queued at all.
  ("내일 오전 10시 돌스냅 촬영" -> tomorrow 10:00, title "돌스냅 촬영".)
* Only a vague part-of-day word ("점심 뭐 먹지") -> pre-filled confirm dialog.
* Nothing temporal whatsoever -> the LLM may try, and its answer is
  cross-validated (:func:`validate_llm_result`) *and* confirmed by the user.

Qwen3 specifics
---------------
* ``<think>...</think>`` reasoning blocks precede the answer. ``/no_think``
  suppresses them (10.7 s -> 1.6 s for a one-line reply) and
  :class:`ThinkFilter` strips whatever still arrives, including tags split
  across streaming chunks.
* The chat template is ChatML. :data:`CHATML_STOP` must be passed on every
  call: llama.cpp otherwise stops only on EOS, and the model runs past its own
  ``<|im_end|>`` to hallucinate a fresh turn.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from holidays import calendar as holiday_calendar
from db_manager import (
    REPEAT_DAILY,
    REPEAT_MONTHLY,
    REPEAT_NONE,
    REPEAT_WEEKLY,
    WEEKDAY_NAMES_KO,
    compute_next_trigger,
    fmt_time,
    format_weekdays,
    normalise_repeat,
    parse_month_day,
    parse_time,
    parse_weekdays,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Paths / model discovery
# --------------------------------------------------------------------------- #

DEFAULT_MODEL_NAME = "Qwen3-1.7B-Q4_K_M.gguf"


def app_dir() -> str:
    """Directory the app runs from -- correct for source *and* PyInstaller onedir."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


#: Set by main() once the data directory is known, so a multi-GB model can live
#: outside the program folder and survive a folder-replace upgrade.
_DATA_DIR: Optional[str] = None


def set_data_dir(path: str) -> None:
    global _DATA_DIR
    _DATA_DIR = path or None


def models_dir() -> str:
    """Preferred models folder: the data dir if one is set, else beside the exe."""
    return os.path.join(_DATA_DIR or app_dir(), "models")


def model_search_dirs() -> list[str]:
    """Every folder searched for weights, in priority order.

    The data dir wins, but the program folder is still searched so an existing
    install (and any build that ships a model inside it) keeps working.
    """
    dirs = []
    for candidate in (models_dir(), os.path.join(app_dir(), "models")):
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def is_volatile_model(path: str) -> bool:
    """True when the weights sit in the program folder rather than the data one.

    Upgrading means replacing the program folder wholesale, so a model kept
    there disappears -- and it is the one file too big to want to copy back.
    Worth saying out loud rather than letting it vanish quietly.
    """
    if not path or _DATA_DIR is None:
        return False
    try:
        resolved = os.path.abspath(path)
        data_models = os.path.abspath(models_dir())
        if os.path.commonpath([resolved, data_models]) == data_models:
            return False
        program = os.path.abspath(app_dir())
        return os.path.commonpath([resolved, program]) == program
    except (ValueError, OSError):
        return False                 # different drives, or an odd path


def ensure_models_dir() -> str:
    """Create the data-folder models/ and leave a note explaining the choice."""
    target = models_dir()
    try:
        os.makedirs(target, exist_ok=True)
        note = os.path.join(target, "여기에_GGUF_모델을_넣으세요.txt")
        if not os.path.exists(note):
            with open(note, "w", encoding="utf-8-sig") as handle:
                handle.write(
                    "이 폴더에 GGUF 모델 파일을 넣으세요.\n\n"
                    "여러 개를 넣으면 가장 최근에 넣은 파일이 선택됩니다.\n"
                    "특정 파일을 고정하려면 설정 > AI 에서 지정하세요.\n\n"
                    "이 폴더는 데이터 폴더라 프로그램을 새 버전으로 덮어써도\n"
                    "지워지지 않습니다. 모델을 다시 넣을 필요가 없습니다.\n")
    except OSError as exc:
        log.warning("Could not prepare %s: %s", target, exc)
    return target


def list_models() -> list[str]:
    """Every ``*.gguf`` found, best candidate first.

    Folder priority beats file age: the data folder is where the model is
    supposed to live, and the program folder is only still searched so an
    older install keeps working. Sorting the whole set by mtime let a stale
    copy beside the exe win simply because it had been touched more recently
    -- and that copy is the one an upgrade deletes.

    Within a folder the newest file wins, because dropping a newer model in is
    exactly how a swap is performed.
    """
    found: list[str] = []
    for directory in model_search_dirs():
        if not os.path.isdir(directory):
            continue
        here = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.lower().endswith(".gguf")
        ]
        try:
            here.sort(key=os.path.getmtime, reverse=True)
        except OSError:
            here.sort()
        found += here
    return found


def find_model_path(configured: str = "") -> Optional[str]:
    """Locate a GGUF file.

    Order: the explicitly configured path, then the **most recently added**
    ``*.gguf`` in ``models/``.

    Newest-first (rather than a hard-coded filename) is deliberate: dropping a
    newer model into the folder is exactly how a swap is performed, and pinning
    one name meant a stale model kept winning while the user believed the new
    one was running. 설정 → AI lets you pin a specific file when the folder
    holds several.
    """
    if configured:
        roots = [""] if os.path.isabs(configured) else [_DATA_DIR or "", app_dir()]
        for root in roots:
            candidate = configured if not root else os.path.join(root, configured)
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        log.warning("Configured model_path not found, falling back: %s", configured)

    found = list_models()
    if not found:
        return None
    if len(found) > 1:
        log.info("%d models present; using newest: %s (others: %s)", len(found),
                 os.path.basename(found[0]),
                 ", ".join(os.path.basename(p) for p in found[1:]))
    return found[0]


def backend_info() -> dict[str, Any]:
    """Diagnostics for the settings / about dialog (never raises)."""
    info: dict[str, Any] = {"available": False, "version": "", "error": "", "model": None}
    try:
        import importlib.util

        spec = importlib.util.find_spec("llama_cpp")
        if spec is None:
            info["error"] = "llama-cpp-python 미설치"
        else:
            info["available"] = True
            try:
                import llama_cpp

                info["version"] = getattr(llama_cpp, "__version__", "?")
            except Exception as exc:                    # noqa: BLE001
                info["available"] = False
                info["error"] = f"로드 실패: {exc}"
    except Exception as exc:                            # noqa: BLE001
        info["error"] = str(exc)

    path = find_model_path()
    if path:
        try:
            info["model"] = {
                "path": path,
                "name": os.path.basename(path),
                "size_mb": round(os.path.getsize(path) / (1024 * 1024), 1),
            }
        except OSError:
            info["model"] = {"path": path, "name": os.path.basename(path), "size_mb": 0}
    return info


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class ParseResult:
    """Outcome of a natural-language schedule extraction."""

    is_schedule: bool = False
    title: str = ""
    target_time: Optional[datetime] = None
    repeat_type: str = REPEAT_NONE
    repeat_detail: str = ""
    confidence: float = 0.0          # 0.0 - 1.0
    source: str = "heuristic"        # heuristic | llm | manual
    raw_text: str = ""
    error: str = ""
    needs_confirm: bool = False      # show the dialog even when usable

    #: A concrete date token was matched ("내일", "8월 15일", "다음주 수요일", …).
    explicit_date: bool = False
    #: A concrete clock token was matched ("10시", "14:30", "3pm", "30분 뒤").
    explicit_time: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.is_schedule and self.target_time and self.title)

    @property
    def definite(self) -> bool:
        """True when the text contained a real date or clock token.

        This is the gate for the heuristic-first guarantee: anything definite
        is saved directly and the LLM is never consulted. Vague part-of-day
        words alone ("점심 뭐 먹지") are deliberately *not* definite -- they
        would otherwise turn idle chatter into alarms.
        """
        if not (self.usable and (self.explicit_date or self.explicit_time)):
            return False
        # Pasted material is not a command. An email asking for a reply draft
        # was filed as a schedule titled with its own 652-character body,
        # because "Sent: Monday" parsed as a date -- and the reply the user
        # actually asked for never happened.
        raw = self.raw_text or ""
        if len(self.title) > MAX_TITLE_CHARS or looks_pasted(raw):
            return False
        if _COMPOSE_RE.search(raw):
            return False
        # A date on its own is not a task. "8/24" left nothing behind but the
        # filler word, and filing an item called "일정" helps no one -- send it
        # to the confirm dialog instead, where a title can be typed.
        if self.title in _EMPTY_TITLES:
            return False
        # Past tense is a statement, not a request: "3일 걸렸어" is someone
        # reporting how long something took, not booking the 3rd.
        if _PAST_TENSE_RE.search(self.raw_text or ""):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target_time"] = fmt_time(self.target_time) if self.target_time else None
        return data

    @staticmethod
    def from_dict(data: dict[str, Any], raw_text: str = "", source: str = "llm") -> "ParseResult":
        dt = parse_time(data.get("target_time"))
        return ParseResult(
            is_schedule=bool(data.get("is_schedule")) and dt is not None,
            title=str(data.get("title") or "").strip(),
            target_time=dt,
            repeat_type=normalise_repeat(data.get("repeat_type")),
            repeat_detail=str(data.get("repeat_detail") or "").strip(),
            confidence=0.6,
            source=source,
            raw_text=raw_text,
        )


# --------------------------------------------------------------------------- #
# Heuristic natural-language parser
# --------------------------------------------------------------------------- #

#: A task name is short. Anything longer is pasted content that happens to
#: contain a date -- an email body registered itself as a 652-character
#: schedule because "Sent: Monday" was in the headers.
MAX_TITLE_CHARS = 80

#: Two or more of these means the text was pasted, not typed as a command.
_PASTE_MARKERS = (
    "from:", "sent:", "to:", "cc:", "bcc:", "subject:", "reply-to:",
    "보낸사람:", "받는사람:", "받는 사람:", "수신:", "발신:", "참조:", "제목:",
    "보낸 날짜:", "회신:", "best regards", "kind regards", "감사합니다\n",
)

#: "이 메일 답장 써줘", "write me a response to below email" -- a request to
#: compose something, which must reach the model rather than the database.
_COMPOSE_RE = re.compile(
    r"(답장|회신|답변)\s*(을|를)?\s*(써|작성|보내|드래프트|초안)"
    r"|(써|작성)\s*(줘|주세요|해줘|해\s*주)"
    r"|write\s+(me\s+)?(a\s+)?(reply|response|email|draft)"
    r"|draft\s+(a\s+)?(reply|response|email)"
    r"|reply\s+to\s+(this|the|below)",
    re.IGNORECASE,
)


#: Fragments that mean nothing once the date they qualified has been removed.
_HOLLOW = re.compile(
    r"^[\s,./·~\-–—:;]*"
    r"(?:(?:월|화|수|목|금|토|일)요일|[월화수목금토일]|전|까지|부터|이전|무렵|"
    r"쯤|경|안|내|중|마감|기한|예정|due|by|until)?"
    r"[\s,./·~\-–—:;]*$")


def _drop_hollow_brackets(text: str) -> str:
    """Remove bracket groups left hollow by cutting the date out of them.

    "영업계수 마감 결과 송부 요청(8/24(월) 퇴근 전까지)" kept its brackets after
    the date was blanked, and filed itself as "…송부 요청( (월) 전 )". A group
    holding real content -- "(긴급)", "(김과장)" -- has to survive, so only
    groups whose remainder is punctuation or a stranded date particle go.
    """
    pattern = re.compile(r"([(\[（【])([^()\[\]（）【】]*)([)\]）】])")
    for _ in range(3):                      # nested groups need a second pass
        new = pattern.sub(
            lambda m: "" if _HOLLOW.match(m.group(2)) else m.group(0), text)
        if new == text:
            break
        text = new
    # An opener whose partner was inside the removed span, or vice versa.
    if text.count("(") != text.count(")"):
        text = text.replace("(", " ").replace(")", " ")
    if text.count("[") != text.count("]"):
        text = text.replace("[", " ").replace("]", " ")
    return text


def looks_pasted(text: str) -> bool:
    """True when the text is quoted material rather than an instruction."""
    if not text:
        return False
    low = text.lower()
    hits = sum(1 for marker in _PASTE_MARKERS if marker in low)
    if hits >= 2:
        return True
    # An address plus any header line is enough on its own.
    return hits >= 1 and bool(re.search(r"[\w.+-]+@[\w.-]+\.\w{2,}", low))


#: What is left when the text was a bare date. Not worth a schedule row.
_EMPTY_TITLES = frozenset(
    ("", "일정", "스케줄", "할일", "할 일", "todo", "건", "알람", "알림"))

#: Past-tense endings. "3일 걸렸어" reports how long something took; without
#: this it booked the 3rd of the month.
_PAST_TENSE_RE = re.compile(
    r"(?:했|였|았|었|봤|왔|갔|줬|썼|끝냈|마쳤|받았|보냈|걸렸|됐|되었)"
    r"\s*(?:어|다|음|네|지|는데|고|으며|습니다|어요|아요)?\s*[.!]?\s*$")

_NUM_KO = {
    "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7,
    "여덟": 8, "아홉": 9, "열": 10, "열한": 11, "열두": 12,
}
_ORDINAL_KO = {
    "첫": 1, "첫째": 1, "1째": 1, "1번째": 1, "둘째": 2, "두번째": 2, "2째": 2, "2번째": 2,
    "셋째": 3, "세번째": 3, "3째": 3, "3번째": 3, "넷째": 4, "네번째": 4, "4째": 4, "4번째": 4,
    "다섯째": 5, "5번째": 5,
}

#: Always removed from a title -- these are instructions, never event names.
_FILLER = (
    "정각", "쯤", "경에", "즈음", "까지", "부터", "추가", "등록",
    "해줘", "해 줘", "잡아줘", "잡아 줘", "설정", "있다고", "알려줘", "말해줘",
    "please", "remind me to", "remind me", "add", "schedule", "set",
)

#: Removed only when something else survives. "오전 3시 알람" has to keep
#: "알람" as its title, but "내일 3시 회의 알람 등록" should not.
#:
#: The container words come from real usage: "매월 12일에 [할일에] 특약OS이월
#: 넣어줘" and "…8월18일 [할일로] 등록해줘" were filing tasks named
#: "할일에 특약OS이월" and "…bdx 할일". The user is naming the list to put the
#: item in, not the item.
_FILLER_SOFT = (
    "리마인드", "리마인더", "알람", "알림", "reminder", "alarm",
    "할일에", "할 일에", "할일로", "할 일로", "할일", "할 일",
    "일정에", "일정으로", "스케줄에", "투두", "todo",
    # Bare "일정"/"스케줄" trail the item just as often -- "8/31 ppt 1차 제출
    # 일정 등록" names the list, not the task. Soft, so "내일 일정" survives.
    "일정", "스케줄", "건",
    # Recurrence and part-of-day words the date/repeat matchers leave behind:
    # "격주 수요일 3시 부서 미팅" kept 격주, "매주 마지막 금요일 …" kept 마지막,
    # "매일 아침 오전 11시 …" kept 아침. None of them names the task.
    "격주", "마지막", "첫째", "둘째", "셋째", "넷째",
    "아침", "점심", "저녁", "새벽", "밤", "오전", "오후",
)

# --------------------------------------------------------------------------- #
# Weekday tokens
# --------------------------------------------------------------------------- #
# Single-syllable weekday names are also ordinary Korean words. 수 is the
# dependent noun in "할 수 있다", 일 means "work"/"day", 목 is "neck", 금 is
# "gold". Matching them bare turned "너가 할 수 있는게 뭐야?" into a Wednesday
# appointment titled "너가 할 있는게 뭐야".
#
# So a bare syllable is only a weekday with corroborating context:
#   a) the full form  -> 수요일
#   b) a week modifier in front -> 매주 수 / 이번주 수 / 다음주 수
#   c) a list or run  -> 화,목 / 월수금        (only after a repeat keyword)
#   d) an explicit clock time right after -> 수 10시 / 수 오후 2시
# Everything else is treated as ordinary prose.

#: Unambiguous: "수요일".
_WD_FULL = r"[월화수목금토일]요일"

#: Bare syllable, not glued to other Hangul ("지금" never yields 금).
_WD_BARE = r"(?<![가-힣])[월화수목금토일](?![가-힣])"

#: Grammar words that immediately follow the dependent noun 수 and friends.
#: "수 있는", "수가 없다", "일도", "목은" -- never weekdays.
_WD_PARTICLE = (r"(?!\s*(?:있|없|많|적|밖에|조차|마저|뿐|대로|같|처럼)"
                r"|\s*[가-힣]*(?:이|가|은|는|을|를|도|만|의|와|과|로|에게|께|한테)\b)")

#: A clock time immediately after the syllable is strong enough on its own.
_WD_CLOCK_AHEAD = (r"(?=\s*(?:오전|오후|아침|저녁|밤|새벽)?\s*"
                   r"(?:\d{1,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열두|열한|열)\s*시)")

#: Bare syllable that earned its weekday reading via an explicit time.
_WD_BARE_TIMED = rf"{_WD_BARE}{_WD_CLOCK_AHEAD}"

#: Used inside repeat/list contexts, where a bare syllable is already qualified
#: by the surrounding "매주"/separator and is safe.
_WD_RE = rf"(?:{_WD_FULL}|{_WD_BARE}{_WD_PARTICLE})"

#: Standalone weekday reference in free text: full form, or bare + clock time.
_WD_STANDALONE = rf"(?:{_WD_FULL}|{_WD_BARE_TIMED})"
_WD_SEP = r"(?:\s*(?:,|·|/|와|과|이랑|랑|하고|및|그리고|and)\s*)"
_WD_LIST_RE = rf"{_WD_RE}(?:{_WD_SEP}{_WD_RE})*"

_EN_WD = (r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|"
          r"fri(?:day)?|sat(?:urday)?|sun(?:day)?")

# English dates. Reinsurance mail is bilingual -- "1pm on August 27th JB BODA
# 미팅" is a line pasted straight out of a broker's message, and before this
# the month name was invisible to the parser: the time survived, the date did
# not, and "on August 27th" ended up inside the title.
_EN_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_EN_MON_RE = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
              r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|"
              r"nov(?:ember)?|dec(?:ember)?")
_ORD = r"(?:st|nd|rd|th)?"

#: "August 27th", "Aug 27, 2026". The day must not be a clock reading:
#: in "27 August 16:45" this pattern happily took "August 16" as the date and
#: swallowed the hour, leaving "27" stranded in the title.
_EN_DATE_MD_RE = re.compile(
    rf"\b(?P<mon>{_EN_MON_RE})\.?\s+(?P<day>\d{{1,2}}){_ORD}(?!\s*:)"
    rf"(?:\s*,?\s*(?P<year>20\d{{2}}))?\b", re.I)
#: "27 August", "27th of Aug 2026". The day must not be the tail of a clock:
#: "14:30 Nov 11" otherwise matched "30 Nov", and "09:00 Dec 1" matched
#: "00 Dec" -- a day of zero, which then failed validation and took the real
#: date down with it.
_EN_DATE_DM_RE = re.compile(
    rf"\b(?<![:\d])(?P<day>\d{{1,2}}){_ORD}\s+(?:of\s+)?(?P<mon>{_EN_MON_RE})\.?"
    rf"(?:\s*,?\s*(?P<year>20\d{{2}}))?\b", re.I)


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _shift_month(anchor: date, months: int) -> tuple[int, int]:
    index = anchor.month - 1 + months
    return anchor.year + index // 12, index % 12 + 1


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> Optional[date]:
    """The ``nth`` occurrence of ``weekday`` in a month (nth<=0 means last)."""
    days = [
        d for d in range(1, _last_day_of_month(year, month) + 1)
        if date(year, month, d).weekday() == weekday
    ]
    if not days:
        return None
    index = -1 if nth <= 0 else nth - 1
    if index >= len(days):
        return None
    return date(year, month, days[index])


class HeuristicParser:
    """Regex-driven Korean + English scheduling phrase parser.

    Confidence bands:

    * ``>= 0.75`` -- explicit time found; safe to save without asking.
    * ``0.4 – 0.75`` -- date but no clock time, or a vague part-of-day; the
      caller should confirm.
    * ``< 0.4`` -- no temporal information; escalate to the LLM.
    """

    DEFAULT_HOUR = 9

    # ---- public ---------------------------------------------------------- #

    def parse(self, text: str, now: Optional[datetime] = None) -> ParseResult:
        now = (now or datetime.now()).replace(microsecond=0)
        raw = (text or "").strip()
        result = ParseResult(raw_text=raw, source="heuristic")
        if not raw:
            return result

        spans: list[tuple[int, int]] = []
        repeat_type, repeat_detail, spans_r = self._match_repeat(raw)
        spans += spans_r

        rel_dt, spans_rel, rel_is_day = self._match_relative(raw, now)
        if rel_dt is not None:
            spans += spans_rel
            result.explicit_date = result.explicit_time = True
            # "3일 뒤 오후 2시" -> take the day from the offset and the clock
            # time from the explicit expression, not from `now`.
            if rel_is_day:
                clock, _conf, spans_t, meridiem, explicit = self._match_time(raw)
                if clock:
                    spans += spans_t
                    result.explicit_time = explicit
                    hour = self._afternoon(clock[0], meridiem)
                    rel_dt = rel_dt.replace(hour=hour, minute=clock[1])
                else:
                    rel_dt = rel_dt.replace(hour=self.DEFAULT_HOUR, minute=0)
            result.target_time = rel_dt.replace(second=0, microsecond=0)
            result.confidence = 0.9
        else:
            date_part, date_conf, spans_d, date_explicit = self._match_date(
                raw, now, repeat_type, repeat_detail)
            time_part, time_conf, spans_t, meridiem, time_explicit = self._match_time(raw)
            spans += spans_d + spans_t
            result.explicit_date = bool(date_part) and date_explicit
            result.explicit_time = bool(time_part) and time_explicit

            if time_part is None and date_part is None:
                result.title = self._extract_title(raw, spans)
                return result

            base = date_part or now.date()
            hour, minute = time_part if time_part else (self.DEFAULT_HOUR, 0)
            if time_part:
                hour = self._afternoon(hour, meridiem)
            candidate = datetime(base.year, base.month, base.day, hour, minute)
            if date_part is None and candidate <= now:
                candidate += timedelta(days=1)
            result.target_time = candidate
            result.confidence = max(date_conf, time_conf) if time_part else min(date_conf, 0.65)

        # A recurring rule must start at a slot that actually matches it.
        if repeat_type != REPEAT_NONE and result.target_time:
            aligned = self._align_to_rule(result.target_time, repeat_type, repeat_detail, now)
            if aligned:
                result.target_time = aligned

        result.repeat_type = repeat_type
        result.repeat_detail = repeat_detail
        result.title = self._extract_title(raw, spans)
        result.is_schedule = result.target_time is not None
        if not result.title:
            result.title = "일정"
            result.confidence = min(result.confidence, 0.6)
        return result

    @staticmethod
    def _align_to_rule(target: datetime, repeat_type: str, detail: str,
                       now: datetime) -> Optional[datetime]:
        """Snap the first occurrence onto the recurrence rule."""
        if repeat_type == REPEAT_WEEKLY:
            days = parse_weekdays(detail)
            if days and target.weekday() not in days:
                for offset in range(1, 8):
                    candidate = target + timedelta(days=offset)
                    if candidate.weekday() in days:
                        return candidate
        elif repeat_type == REPEAT_MONTHLY:
            day = parse_month_day(detail)
            if day:
                year, month = target.year, target.month
                day = min(day, _last_day_of_month(year, month))
                candidate = target.replace(year=year, month=month, day=day)
                if candidate <= now:
                    year, month = _shift_month(candidate.date(), 1)
                    candidate = candidate.replace(
                        year=year, month=month,
                        day=min(parse_month_day(detail) or day, _last_day_of_month(year, month)))
                return candidate
        return None

    # ---- repeat ---------------------------------------------------------- #

    def _match_repeat(self, text: str) -> tuple[str, str, list[tuple[int, int]]]:
        spans: list[tuple[int, int]] = []
        low = text.lower()

        # weekly, possibly with several days: "매주 화요일이랑 목요일", "매주 월수금"
        m = re.search(
            rf"(매주|주마다|매\s*주|every\s+week(?:\s+on)?|weekly)\s*"
            rf"({_WD_LIST_RE}|(?<![가-힣])[월화수목금토일]{{2,5}}(?![가-힣])|"
            rf"(?:{_EN_WD})(?:\s*(?:,|and|&)\s*(?:{_EN_WD}))*)?",
            low,
        )
        if m:
            spans.append(m.span())
            days = parse_weekdays(m.group(2) or "")
            return REPEAT_WEEKLY, format_weekdays(days), spans

        # "every monday" / "월요일마다"
        m = re.search(rf"every\s+({_EN_WD})s?", low)
        if m:
            spans.append(m.span())
            return REPEAT_WEEKLY, format_weekdays(parse_weekdays(m.group(1))), spans
        m = re.search(rf"({_WD_LIST_RE})\s*마다", low)
        if m:
            spans.append(m.span())
            return REPEAT_WEEKLY, format_weekdays(parse_weekdays(m.group(1))), spans

        # monthly: "매월 25일", "매달", "달마다"
        m = re.search(r"(매월|매달|달마다|every\s+month|monthly)\s*(\d{1,2})?\s*일?", low)
        if m:
            spans.append(m.span())
            return REPEAT_MONTHLY, (m.group(2) or ""), spans

        # daily
        m = re.search(r"(매일|날마다|하루에\s*한\s*번|every\s*day|daily)", low)
        if m:
            spans.append(m.span())
            return REPEAT_DAILY, "", spans

        return REPEAT_NONE, "", spans

    # ---- relative offsets ------------------------------------------------ #

    @staticmethod
    def _afternoon(hour: int, meridiem: bool) -> int:
        """Korean scheduling convention: an unqualified 1~6시 is the afternoon.

        "2시 반 치과" is 14:30, never 02:30. 7~12시 are left alone because
        "7시 회의" / "10시 회의" really are morning meetings.

        This lives in one place because it did not used to: the day-offset
        branch of :meth:`parse` had its own copy that forgot the rule, so
        "오늘 5시" was 17:00 while "5일 후 5시" was 05:00.
        """
        return hour + 12 if (not meridiem and 1 <= hour <= 6) else hour

    def _match_relative(self, text: str, now: datetime):
        """-> (datetime | None, spans, is_day_offset)

        ``is_day_offset`` is True for day/week/month offsets, where the caller
        should still look for an explicit clock time ("3일 뒤 오후 2시").
        """
        low_text = text.lower()

        # Business days first: "3영업일 뒤" must not be read as "3일 뒤".
        # Deadlines in this office are quoted in working days, and a plain
        # day-count lands on a Saturday roughly two times in seven.
        m = re.search(r"(\d{1,3})\s*영업일\s*(뒤|후|이내|안|째|만에)?", low_text)
        if m:
            cal = holiday_calendar()
            target = cal.add_business_days(now.date(), int(m.group(1)))
            return (datetime.combine(target, now.time().replace(microsecond=0)),
                    [m.span()], True)
        m = re.search(r"(다음|담|익)\s*영업일", low_text)
        if m:
            target = holiday_calendar().next_business_day(now.date())
            return (datetime.combine(target, now.time().replace(microsecond=0)),
                    [m.span()], True)

        m = re.search(
            r"(\d{1,4})\s*(분|시간|일|주|개월|달|minutes?|mins?|hours?|hrs?|days?|weeks?|months?)"
            r"\s*(뒤|후|이따|이후|later|from\s+now)",
            low_text,
        )
        if not m:
            return None, [], False
        qty, unit = int(m.group(1)), m.group(2)
        if unit.startswith(("분", "min")):
            return now + timedelta(minutes=qty), [m.span()], False
        if unit.startswith(("시간", "hour", "hr")):
            return now + timedelta(hours=qty), [m.span()], False
        if unit.startswith(("주", "week")):
            return now + timedelta(weeks=qty), [m.span()], True
        if unit.startswith(("개월", "달", "month")):
            year, month = _shift_month(now.date(), qty)
            day = min(now.day, _last_day_of_month(year, month))
            return now.replace(year=year, month=month, day=day), [m.span()], True
        return now + timedelta(days=qty), [m.span()], True

    # ---- dates ----------------------------------------------------------- #

    def _match_date(self, text: str, now: datetime, repeat_type: str, repeat_detail: str):
        """-> (date | None, confidence, spans)"""
        low = text.lower()
        today = now.date()

        # A weekly rule already pins the weekday(s).
        if repeat_type == REPEAT_WEEKLY:
            days = parse_weekdays(repeat_detail)
            if days:
                for offset in range(0, 8):
                    candidate = today + timedelta(days=offset)
                    if candidate.weekday() in days:
                        return candidate, 0.85, [], True
        if repeat_type == REPEAT_MONTHLY:
            day = parse_month_day(repeat_detail)
            if day:
                clamped = min(day, _last_day_of_month(today.year, today.month))
                return today.replace(day=clamped), 0.85, [], True

        # --- absolute forms ------------------------------------------------
        m = re.search(r"(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})", low)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), 0.92, [m.span()], True
            except ValueError:
                pass

        m = re.search(r"(\d{1,2})\s*월\s*(?:(\d{1,2})\s*일|(말일|말))", low)
        if m:
            month = int(m.group(1))
            try:
                year = now.year
                if m.group(3):                      # "8월 말일"
                    day = _last_day_of_month(year, month)
                else:
                    day = int(m.group(2))
                candidate = date(year, month, day)
                if candidate < today:
                    candidate = date(year + 1, month,
                                     min(day, _last_day_of_month(year + 1, month)))
                return candidate, 0.9, [m.span()], True
            except ValueError:
                pass

        # "8/24" -- office shorthand, and the form people actually type when
        # copying a date out of a mail. Year-qualified variants ("2026/8/24")
        # are already gone by now, so a bare pair is month/day. Requiring the
        # neighbours to be non-numeric keeps us off "8/24/2026" tails and off
        # ratios written without spaces.
        # A trailing quantity word means it was a fraction, not a date:
        # "3/4 정도만 끝냈어", "진행률이 2/3쯤 돼".
        m = re.search(
            r"(?<![\d/.\-])(\d{1,2})\s*/\s*(\d{1,2})"
            r"(?![\d/.\-])(?!\s*(?:정도|쯤|가량|만큼|수준|밖에|이상|이하|짜리|배))",
            low)
        if not m:
            # "8.24" is the other shorthand people type. A dot is also the
            # decimal point, so this form only counts when a space or the end
            # of the line follows -- that keeps "1.5시간" and "3.5B 모델" out,
            # and the lookbehind keeps it from biting into "2026.7월".
            m = re.search(r"(?<![\d.\-])(\d{1,2})\.(\d{1,2})(?![\d.])(?=\s|$)", low)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                try:
                    year = now.year
                    candidate = date(year, month, day)
                    if candidate < today:               # "1/5" typed in December
                        candidate = date(year + 1, month, day)
                    return candidate, 0.88, [m.span()], True
                except ValueError:
                    pass

        # English month names, both orders: "August 27th", "27 Aug",
        # "Aug 27, 2026". Reinsurance correspondence is bilingual, so a line
        # pasted out of a London broker's mail has to work.
        # Day-first is tried first: "27 August" is unambiguous, while the
        # month-first pattern would otherwise match its tail as "August ...".
        # Both are tried, because a match that fails validation must not stop
        # the other pattern from finding the real date.
        for pattern in (_EN_DATE_DM_RE, _EN_DATE_MD_RE):
            m = pattern.search(low)
            if not m:
                continue
            groups = m.groupdict()
            month = _EN_MONTHS.get((groups.get("mon") or "")[:3])
            try:
                day = int(groups.get("day") or 0)
            except (TypeError, ValueError):
                day = 0
            if not (month and 1 <= day <= 31):
                continue
            year = int(groups["year"]) if groups.get("year") else now.year
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if not groups.get("year") and candidate < today:
                candidate = date(year + 1, month, day)
            return candidate, 0.9, [m.span()], True

        # --- month-relative words -----------------------------------------
        month_shift, shift_span = self._match_month_word(low)

        # "N째주 X요일" (optionally inside 다음달/이번달)
        m = re.search(
            rf"(마지막|막|첫|첫째|둘째|셋째|넷째|다섯째|두번째|세번째|네번째|\d)\s*(?:째|번째)?\s*주\s*"
            rf"({_WD_RE})",
            low,
        )
        if m:
            token = m.group(1)
            nth = 0 if token in ("마지막", "막") else (
                int(token) if token.isdigit() else _ORDINAL_KO.get(token, 1))
            days = parse_weekdays(m.group(2))
            if days:
                year, month = _shift_month(today, month_shift or 0)
                found = _nth_weekday(year, month, days[0], nth)
                if found and found < today and month_shift is None:
                    year, month = _shift_month(today, 1)
                    found = _nth_weekday(year, month, days[0], nth)
                if found:
                    return found, 0.85, [m.span()] + ([shift_span] if shift_span else []), True

        # "말일" / "월말" with an optional 다음달 prefix
        m = re.search(r"(말일|월말|달\s*말|이달\s*말)", low)
        if m:
            year, month = _shift_month(today, month_shift or 0)
            candidate = date(year, month, _last_day_of_month(year, month))
            if candidate < today:
                year, month = _shift_month(today, 1)
                candidate = date(year, month, _last_day_of_month(year, month))
            return candidate, 0.85, [m.span()] + ([shift_span] if shift_span else []), True

        # "다음달 15일" / "이번달 3일"
        if month_shift is not None:
            m = re.search(r"(?<!\d)(\d{1,2})\s*일(?!\s*(뒤|후|간))", low)
            year, month = _shift_month(today, month_shift)
            if m:
                day = min(int(m.group(1)), _last_day_of_month(year, month))
                return date(year, month, day), 0.85, [m.span(), shift_span], True
            day = min(today.day, _last_day_of_month(year, month))
            return date(year, month, day), 0.55, [shift_span], True

        # bare "15일"
        m = re.search(r"(?<!\d)(\d{1,2})\s*일(?!\s*(뒤|후|간))", low)
        if m:
            day = int(m.group(1))
            if 1 <= day <= 31:
                year, month = today.year, today.month
                if day > _last_day_of_month(year, month) or day < today.day:
                    year, month = _shift_month(today, 1)
                    day = min(day, _last_day_of_month(year, month))
                return date(year, month, day), 0.75, [m.span()], True

        # --- day words -----------------------------------------------------
        # Order matters: 낼모레/내일모레 must be tested before 내일/낼, and
        # 그저께 before 어제, otherwise the shorter word wins the prefix.
        for pattern, offset in (
            (r"(낼모레|내일모레|모레)", 2), (r"(글피)", 3),
            (r"(그저께|그제)", -2), (r"(어제|yesterday)", -1),
            (r"(오늘|today|금일)", 0), (r"(내일|tomorrow|명일|낼)", 1),
        ):
            m = re.search(pattern, low)
            if m:
                return today + timedelta(days=offset), 0.8, [m.span()], True

        # 주말 -> the coming Saturday (next week's if "다음주 주말")
        m = re.search(r"(이번\s*주\s*|이번\s*|다음\s*주\s*|담주\s*|차주\s*)?주말", low)
        if m:
            offset = (5 - today.weekday()) % 7 or 7
            prefix = (m.group(1) or "").replace(" ", "")
            if prefix in ("다음주", "담주", "차주"):
                offset += 7
            return today + timedelta(days=offset), 0.7, [m.span()], True

        # A week modifier licenses even a bare syllable: "다음주 수".
        m = re.search(
            rf"(이번\s*주|금주|다음\s*주|담주|차주|next\s+week)\s*({_WD_RE}|{_EN_WD})", low)
        if not m:
            # Standalone: only the full form, or a bare syllable backed by a
            # clock time. "할 수 있는" must never reach this.
            m = re.search(rf"()({_WD_STANDALONE}|{_EN_WD})", low)
        if m:
            days = parse_weekdays(m.group(2))
            if days:
                prefix = (m.group(1) or "").replace(" ", "")
                if prefix:
                    # "다음주 월요일" means the Monday of next *calendar* week,
                    # not "the coming Monday, plus seven". On a Friday those
                    # differ by a week: the coming Monday is already next
                    # week's, so adding seven overshot to the week after.
                    week_start = today - timedelta(days=today.weekday())
                    if prefix in ("다음주", "담주", "차주", "nextweek"):
                        week_start += timedelta(days=7)
                    target = week_start + timedelta(days=days[0])
                    # "이번주 수요일" said on Thursday means the coming one --
                    # nobody schedules into the past.
                    if target < today:
                        target += timedelta(days=7)
                    return target, 0.8, [m.span()], True
                offset = (days[0] - today.weekday()) % 7
                return today + timedelta(days=offset), 0.8, [m.span()], True

        m = re.search(r"(다음\s*주|담주|차주|next\s+week)", low)
        if m:
            return today + timedelta(days=7), 0.6, [m.span()], True

        if month_shift:
            year, month = _shift_month(today, month_shift)
            return date(year, month, min(today.day, _last_day_of_month(year, month))), 0.5, [shift_span], True

        return None, 0.0, [], False

    @staticmethod
    def _match_month_word(low: str):
        """-> (month offset | None, span | None) for 이번달 / 다음달 / 담달."""
        m = re.search(r"(다음\s*달|담달|내달|다음\s*월|next\s+month)", low)
        if m:
            return 1, m.span()
        m = re.search(r"(이번\s*달|금월|이달|this\s+month)", low)
        if m:
            return 0, m.span()
        return None, None

    # ---- clock times ------------------------------------------------------ #

    def _match_time(self, text: str):
        """-> ((hour, minute) | None, confidence, spans, had_meridiem, explicit)

        ``explicit`` distinguishes a real clock reading ("10시", "14:30") from a
        vague part-of-day word ("점심"), which must not be enough on its own to
        create an alarm.
        """
        low = text.lower()

        def shift(tag: str, hour: int) -> int:
            tag = (tag or "").strip()
            if tag in ("오후", "저녁", "저녁때", "밤", "pm", "p.m."):
                return hour + 12 if hour < 12 else hour
            if tag in ("오전", "아침", "새벽", "am", "a.m."):
                return 0 if hour == 12 else hour
            return hour

        # HH:MM, with a meridiem on either side. English puts it after
        # ("10:30am"); leaving it out of the match both dropped the pm shift
        # and left a stray "am" sitting in the title.
        m = re.search(r"(오전|오후|아침|저녁|밤|새벽|am|pm)?\s*(\d{1,2})\s*:\s*(\d{2})"
                      r"\s*(am|pm|a\.m\.|p\.m\.)?", low)
        if m:
            hour, minute = int(m.group(2)), int(m.group(3))
            hour = shift(m.group(1) or m.group(4), hour)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute), 0.95, [m.span()], True, True

        # N시 (반 | N분), digits or Korean numerals.
        #
        # 시 has to be the whole syllable, not the head of another word:
        # "1/2 시무식" was read as 2 o'clock and filed as "무식", and
        # "2시간 걸렸어" became a 14:00 alarm titled "간 걸렸어". So 시 must be
        # followed by a non-Hangul character, or by one of the few particles
        # that really do trail a clock reading.
        m = re.search(
            r"(오전|오후|아침|저녁|밤|새벽)?\s*"
            r"(\d{1,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열두|열한|열)\s*시"
            r"(?:(?![가-힣])|(?=\s*(?:반|정각|분|경|쯤|까지|부터|에|께|이후|이전)))"
            r"\s*(반|\d{1,2}\s*분|정각)?",
            low,
        )
        if m:
            token = m.group(2)
            hour = int(token) if token.isdigit() else _NUM_KO.get(token, 0)
            tail = (m.group(3) or "").strip()
            if tail == "반":
                minute = 30
            elif tail and tail != "정각":
                minute = int(re.sub(r"\D", "", tail) or 0)
            else:
                minute = 0
            hour = shift(m.group(1), hour)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute), 0.9, [m.span()], bool(m.group(1)), True

        # "at 3pm" / "3 pm"
        m = re.search(r"(?:at\s+)?(\d{1,2})\s*(am|pm)", low)
        if m:
            hour = shift(m.group(2), int(m.group(1)))
            if 0 <= hour <= 23:
                return (hour, 0), 0.9, [m.span()], True, True

        # Vague parts of the day -- usable as a time, but never definite.
        # The trailing 전/까지/쯤 is swallowed with the word: matching only
        # "퇴근" out of "퇴근 전까지" left a bare "전" stranded in the title
        # ("8/24 퇴근 전까지 보고서 제출" -> "전 보고서 제출").
        tail = r"(?:\s*(?:전|이전|무렵|쯤|경)?\s*(?:까지|전까지|까지는|안에|내로|내)?)"
        for word, hm, conf in (
            ("자정", (0, 0), 0.8), ("정오", (12, 0), 0.8),
            ("점심때", (12, 0), 0.6), ("점심", (12, 0), 0.55),
            ("아침", (8, 0), 0.5), ("저녁때", (18, 0), 0.6), ("저녁", (18, 0), 0.55),
            ("밤", (21, 0), 0.5), ("새벽", (5, 0), 0.5), ("퇴근", (18, 0), 0.5),
        ):
            m = re.search(word + tail, low)
            if m:
                definite = word in ("자정", "정오")
                return hm, conf, [m.span()], True, definite

        return None, 0.0, [], False, False

    # ---- title ------------------------------------------------------------ #

    def _extract_title(self, raw: str, spans: Iterable[tuple[int, int]]) -> str:
        chars = list(raw)
        for start, end in spans:
            for i in range(max(0, start), min(len(chars), end)):
                chars[i] = " "
        base = "".join(chars)
        for word in _FILLER:
            base = re.sub(re.escape(word), " ", base, flags=re.IGNORECASE)

        def tidy(text: str) -> str:
            text = _drop_hollow_brackets(text)
            text = re.sub(r"\b(에|에서|부터|까지|의|은|는|이|가|을|를)\b", " ", text)
            text = re.sub(r"\s+", " ", text).strip(" ,.!?~-·|")
            text = re.sub(r"(에|에서|으로|로|때)$", "", text).strip()
            # Cutting an English date out of a sentence leaves its preposition
            # behind: "1pm on August 27th JB BODA 미팅" -> "on JB BODA 미팅".
            # Only trim at the edges -- "hands on training" must survive.
            # A space is required after the word, not just a boundary: with
            # \b alone the "a" in "A-1 등급 심사" counted as the article and
            # the title became "1 등급 심사".
            text = re.sub(r"^(?:(?:on|at|by|from|in|of|the|a|next|this|coming|"
                          r"last)\s+)+", "", text, flags=re.I)
            text = re.sub(r"(?:\s*\b(?:on|at|by|from|in|of|the|a)\b)+$", "",
                          text, flags=re.I)
            return text.strip(" ,.!?~-·|")

        # Prefer the version without the soft fillers, but keep them rather
        # than end up with no title at all.
        trimmed = base
        for word in _FILLER_SOFT:
            trimmed = re.sub(re.escape(word), " ", trimmed, flags=re.IGNORECASE)
        return tidy(trimmed) or tidy(base)


# --------------------------------------------------------------------------- #
# Chat tool calling
# --------------------------------------------------------------------------- #
# The chat tab is not only a conversation: "매월 12일 특약OS이월 추가해줘" is an
# instruction, and answering it with prose would be useless. Intent detection
# runs *before* generation and is deliberately rule-based -- a tool call that
# writes to the database must be predictable, instant, and immune to the
# hallucinations a 2B model produces.

TOOL_NONE = "none"
TOOL_ADD = "add"
TOOL_LIST = "list"
TOOL_CLEAR = "clear"
TOOL_DELETE = "delete"
TOOL_ADD_EMAIL = "add_email_reminder"
TOOL_LIST_EMAIL = "list_email_reminders"
TOOL_DELETE_EMAIL = "delete_email_reminder"
TOOL_REPORT = "work_report"
TOOL_LIST_MAIL = "list_recent_mail"
TOOL_REPLY_MAIL = "reply_to_mail"


@dataclass(slots=True)
class ToolIntent:
    """A detected action the chat message is asking the app to perform."""

    tool: str = TOOL_NONE
    schedule: Optional[ParseResult] = None      # for ADD (first, if several)
    #: ADD may cover several items -- "8/24 엑셀 제출 / 8/31 ppt 제출". Always
    #: holds every item including the first; `schedule` stays for callers that
    #: only ever expect one.
    schedules: list[ParseResult] = field(default_factory=list)
    scope: str = "all"                          # for LIST: today | week | all | done
    query: str = ""                             # for DELETE: title fragment
    keywords: str = ""                          # for ADD_EMAIL: mail keywords
    action: str = ""                            # for ADD_EMAIL: follow-up steps
    raw_text: str = ""

    def __bool__(self) -> bool:
        return self.tool != TOOL_NONE

    def as_dict(self) -> dict[str, Any]:
        """Flat view of the intent (handy for logging and tests)."""
        return {"tool": self.tool, "scope": self.scope, "query": self.query,
                "keywords": self.keywords, "action": self.action}


# "받은 메일 뭐 있지", "최근 메일 보여줘" -- what is available to reply to.
_RECENT_MAIL_LIST_RE = re.compile(
    r"(?:최근|받은|온|도착한)\s*(?:메일|이메일)\s*"
    r"(?:목록|리스트|뭐|무엇|보여|알려|확인|있)"
    r"|(?:메일|이메일)\s*(?:목록|리스트)\s*(?:보여|알려|확인)")

# "요율 검토 메일 답장 써줘", "이거 답장 써줘 - 수락한다고",
# "reply to the Marsh email saying well received"
_REPLY_MAIL_RE = re.compile(
    r"^\s*(?P<subject>.*?)\s*(?:메일|이메일|mail|email)?\s*"
    r"(?:에\s*)?(?:답장|회신|답변|reply|respond)\s*"
    r"(?:을|를|은|는)?\s*(?:써|작성|보내|draft|write)?\s*"
    r"(?:줘|주세요|해줘|해\s*주세요|해라|it|to\s+it)?\s*"
    r"(?:[-–—:,]\s*|하고\s*|라고\s*|으로\s*|saying\s*|해서\s*)?"
    r"(?P<intent>.*)$",
    re.IGNORECASE | re.DOTALL)

# "이번주 한 일", "주간보고", "지난주 뭐 했지" -- the Friday report. Needs a
# completed-work sense, so a bare "이번주 일정" stays a plain listing.
_REPORT_RE = re.compile(
    r"(주간\s*보고|업무\s*보고|주보|월간\s*보고)"
    r"|(?:이번\s*주|지난\s*주|저번\s*주|이번\s*달|금월|한\s*달)\s*"
    r"(?:내가\s*)?(?:한\s*일|완료|끝낸|처리한|마친|한\s*것|한거)")

# "주간보고" is also one of the most common *titles* in a Korean office, so
# "매주 월요일 9시 주간보고" is a recurring appointment, not a request for the
# report. A clock time or a recurrence word settles it, and so do the strong
# creation verbs -- but not "만들어줘", which reads naturally for both.
_REPORT_NOT_RE = re.compile(
    r"매\s*(주|일|월|달)|격주|\d{1,2}\s*시|:\d{2}"
    r"|추가|등록|잡아\s*줘|잡아줘|넣어\s*줘|넣어줘")

# Separators between several items in one breath: "8/24 엑셀 제출 / 8/31 ppt
# 제출". The slash must be surrounded by space -- an unspaced one belongs to a
# date ("8/24"), which is exactly how this went wrong in production.
#
# The comma must not be the one inside "Dec 1, 2026": splitting there gave
# "…보고 Dec 1" and "2026 새벽 6시", both of which look dated enough to pass,
# so a single English date silently became two schedules.
_ITEM_SPLIT_RE = re.compile(
    r"\s+/\s+|\s*[;\n]\s*|\s*,(?!\s*20\d{2}\b)\s*|\s+그리고\s+|\s+및\s+")

# "…2개 별도로", "…각각 등록" -- says how to file the items, not what they are.
_COUNT_TAIL_RE = re.compile(
    r"(?:\d+\s*개\s*)?(?:별도로|별도|각각|따로따로|따로|개별로|개별)\s*$")


def split_items(text: str) -> list[str]:
    """Break one message into candidate schedule phrases.

    Conservative on purpose: the caller only accepts the split when *every*
    piece independently parses as a dated schedule, so an over-eager split of
    "회의, 점심 약속" simply loses to the single-item path.
    """
    parts = [_COUNT_TAIL_RE.sub("", p).strip(" .·-") for p in _ITEM_SPLIT_RE.split(text)]
    return [p for p in parts if len(p) >= 2]


def _parse_items(text: str, parser: "HeuristicParser",
                 now: Optional[datetime] = None) -> list["ParseResult"]:
    """Several dated items in one message, or [] if it is not that shape."""
    segments = split_items(text)
    if len(segments) < 2:
        return []
    results = []
    for segment in segments:
        parsed = parser.parse(segment, now)
        if not parsed.definite:
            return []                    # one vague piece -> treat as one item
        # A piece whose whole title is a number is not a task -- it is the
        # other half of something we should not have cut, like the year in
        # "Dec 1, 2026".
        if parsed.title.isdigit():
            return []
        results.append(parsed)
    # Distinct times, or the user wrote one thing that merely looks like two.
    if len({r.target_time for r in results}) < len(results):
        return []
    return results


# "추가/등록/잡아줘/알림 설정" -- an explicit request to create something.
_ADD_RE = re.compile(
    r"(추가|등록|잡아\s*줘|잡아줘|넣어\s*줘|넣어줘|만들어\s*줘|만들어줘|"
    r"알려\s*줘.*(?:하게|하도록)|설정해\s*줘|리마인드|알람\s*(?:설정|맞춰)|"
    r"\badd\b|\bcreate\b|\bschedule\b|remind\s+me)"
)

# "오늘 일정 알려줘", "이번주 할일", "뭐 있지" -- a read-only query.
_LIST_RE = re.compile(
    r"(일정|스케줄|할\s*일|할일|todo|to-do|약속|알람|남은\s*거|뭐\s*있|"
    r"schedule|task)s?\s*"
    r"(?:이|가|은|는|을|를|좀)?\s*"
    r"(뭐|무엇|어떻게\s*되|알려|보여|말해|리스트|목록|확인|조회|정리해서|"
    r"있(?:어|나|니|는지)|남았|list|show|what)"
)
_LIST_RE_ALT = re.compile(
    r"(오늘|내일|이번\s*주|금주|이번\s*달|전체|모든|남은|밀린|안\s*끝난)\s*"
    r"(일정|스케줄|할\s*일|할일|todo|약속|task)"
)

# "완료된 일정 정리해줘" -- housekeeping.
_CLEAR_RE = re.compile(
    r"(완료|끝난|done|지난|finished)\s*(된|한)?\s*"
    r"(일정|것|거|항목|task|schedule)s?\s*"
    # tolerate particles and quantifiers: "끝난 일정 다 지워줘"
    r"(?:은|는|을|를|이|가)?\s*(?:다|모두|전부|싹|all)?\s*"
    r"(정리|삭제|지워|치워|비워|clean|clear|remove|delete)"
    r"|(정리|비워)\s*해\s*줘"
)

_DELETE_RE = re.compile(
    # "지워줘" is 지워 + 줘, not 지워 + 해줘 -- requiring the 해 meant the most
    # natural way to say it fell through to chat.
    r"(?P<title>.+?)\s*(일정|약속|알람)?\s*"
    r"(삭제|지워|취소|없애|빼|remove|delete|cancel)\s*"
    r"(?:(?:해\s*)?(?:줘|주세요|주라|줄래))?$"
)

# "'특약OS이월' 메일 오면 '결재 승인' 리마인드해줘"
#   group kw     -> the keyword(s) to watch the inbox for
#   group action -> what to do once it lands
_ADD_EMAIL_RE = re.compile(
    r"^\s*[\"'‘’“”\[]?\s*(?P<kw>[^\"'‘’“”\[\]]{2,40}?)\s*[\"'‘’“”\]]?\s*"
    r"(?:관련\s*)?(?:메일|이메일|mail|email)\s*(?:이|가|을|를)?\s*"
    r"(?:오면|도착하면|수신되면|받으면|들어오면|오거든|come[s]?|arrive[s]?)\s*"
    r"[,:]?\s*(?P<action>.+?)\s*$",
    re.IGNORECASE,
)

# "특약 메일 알림 삭제해줘", "월마감 메일 감지 지워줘", "메일 알림 삭제"
# Checked before the list/add matchers, which the same wording also satisfies.
_DELETE_EMAIL_RE = re.compile(
    r"^\s*(?P<kw>.*?)\s*(?:관련\s*)?(?:메일|이메일|mail|email)\s*"
    r"(?:수신\s*)?(?:알림|감지|리마인더|알람|규칙|rule)?\s*"
    r"(?:을|를|은|는)?\s*"
    r"(?:삭제|지워|취소|해제|제거|끄기|없애|delete|remove|cancel)\s*"
    r"(?:해\s*줘|해줘|해주세요|줘|주세요)?\s*$",
    re.IGNORECASE,
)

# "기다리는 메일 목록", "메일 알림 리스트", "메일 리마인더"
_LIST_EMAIL_RE = re.compile(
    r"(기다리(?:는|던)\s*(?:메일|이메일)"
    r"|(?:메일|이메일)\s*(?:알림|리마인더|대기|감지|규칙)\s*(?:목록|리스트|현황|규칙)?"
    r"|(?:대기|등록)\s*(?:중인?)?\s*(?:메일|이메일)"
    r"|(?:메일|이메일)\s*(?:목록|리스트)"
    r"|email\s*reminder)",
    re.IGNORECASE,
)

# Trailing instruction verbs that belong to the request, not to the action text.
_ACTION_TAIL_RE = re.compile(
    r"\s*(?:라고|하라고|하도록)?\s*"
    r"(?:리마인드|리마인더|알림|알려|기억|메모|등록|추가|설정)?\s*"
    r"(?:해\s*줘|해줘|해주세요|해라|해|줘|주세요)?\s*$"
)

# A question is a question, never a booking. "내일 시간 있어?" asks something;
# it does not ask for an entry in the calendar. Only gates the verb-less
# fallback -- "내일 3시 회의 추가해줘?" still adds, because the verb is explicit.
_QUESTION_RE = re.compile(
    r"[?？]\s*$"
    r"|(?:을까|ㄹ까|일까|할까|런가|인가요|나요|가요|ᆯ까요)\s*$"
)

_SCOPE_PATTERNS = (
    ("today", re.compile(r"오늘|today|금일")),
    ("tomorrow", re.compile(r"내일|tomorrow")),
    ("week", re.compile(r"이번\s*주|금주|this\s+week|한\s*주")),
    ("month", re.compile(r"이번\s*달|이달|금월|this\s+month")),
    ("done", re.compile(r"완료|끝난|done")),
)


def _clean_fragment(text: str) -> str:
    """Trim quotes, brackets and stray particles off an extracted fragment."""
    out = (text or "").strip().strip("'\"‘’“”[]()<>")
    out = re.sub(r"^(그|저|이)\s+", "", out)
    return out.strip(" ,.!?~-·|:;")


def detect_tool_intent(
    text: str,
    parser: Optional["HeuristicParser"] = None,
    now: Optional[datetime] = None,
) -> ToolIntent:
    """Classify a chat message as an action request, or ``TOOL_NONE``.

    Ordering matters: CLEAR is checked before LIST ("완료된 일정 정리해줘"
    contains "일정"), and ADD requires both an explicit creation verb *and* a
    parseable date, so "회의 언제가 좋을까?" stays a conversation.
    """
    raw = (text or "").strip()
    intent = ToolIntent(raw_text=raw)
    if not raw:
        return intent
    low = raw.lower()
    now = now or datetime.now()

    # Parse once, up front: both the verb-driven ADD branch and the bare
    # noun-phrase fallback at the end need the heuristic's verdict.
    parser = parser or HeuristicParser()
    quick = parser.parse(raw, now)

    # 0) Awaited-email rules go first: "…메일 오면 …리마인드해줘" also contains
    #    the words that would otherwise trip the schedule ADD/LIST matchers.
    delete_email = _DELETE_EMAIL_RE.match(raw)
    if delete_email:
        intent.tool = TOOL_DELETE_EMAIL
        intent.query = _clean_fragment(delete_email.group("kw"))
        return intent

    if _LIST_EMAIL_RE.search(low) and not _ADD_EMAIL_RE.match(raw):
        intent.tool = TOOL_LIST_EMAIL
        return intent

    match = _ADD_EMAIL_RE.match(raw)
    if match:
        keywords = _clean_fragment(match.group("kw"))
        # Clean, drop the trailing instruction verb, then clean again -- the
        # closing quote of "'결재 승인' 리마인드해줘" only becomes a trailing
        # character once the verb behind it is gone.
        action = _clean_fragment(
            _ACTION_TAIL_RE.sub("", _clean_fragment(match.group("action"))))
        if len(keywords) >= 2 and len(action) >= 2:
            intent.tool = TOOL_ADD_EMAIL
            intent.keywords = keywords
            intent.action = action
            return intent

    # 0.4) Mail. "요율 검토 메일 답장 써줘" must be caught before the compose
    #      guard sends it to plain chat with no mail attached.
    if _RECENT_MAIL_LIST_RE.search(low):
        intent.tool = TOOL_LIST_MAIL
        return intent
    reply = _REPLY_MAIL_RE.search(raw)
    if reply:
        intent.tool = TOOL_REPLY_MAIL
        # Whatever was said before the verb is the subject hint; empty means
        # "the most recent one".
        intent.query = _clean_fragment(reply.group("subject") or "")
        intent.action = _clean_fragment(reply.group("intent") or "")
        return intent

    # 0.5) Work report. "이번주 한 일" reads like a LIST query, so it has to be
    #      settled before the list matchers get hold of it.
    report = _REPORT_RE.search(low) and not _REPORT_NOT_RE.search(low)
    if report:
        intent.tool = TOOL_REPORT
        intent.scope = ("last_week" if re.search(r"지난\s*주|저번\s*주", low)
                        else "month" if re.search(r"이번\s*달|금월|한\s*달", low)
                        else "week")
        return intent

    # 1) CLEAR -- most specific
    if _CLEAR_RE.search(low) and re.search(r"완료|끝난|done|정리|비워", low):
        if re.search(r"일정|것|거|항목|task|schedule|전체|모두|다", low) or "정리" in low:
            intent.tool = TOOL_CLEAR
            return intent

    # 2) ADD with an explicit creation verb
    if _ADD_RE.search(low):
        # Strip the instruction verb so it never lands in the title.
        stripped = _ADD_RE.sub(" ", raw)
        several = _parse_items(stripped, parser, now)
        if several:
            intent.tool = TOOL_ADD
            intent.schedules = several
            intent.schedule = several[0]
            return intent
        result = parser.parse(stripped, now)
        if result.definite:
            intent.tool = TOOL_ADD
            intent.schedule = result
            intent.schedules = [result]
            return intent
        # An add request we could not time -- fall through to LIST/chat rather
        # than guessing a time the user never gave.

    # 3) LIST
    if _LIST_RE.search(low) or _LIST_RE_ALT.search(low):
        intent.tool = TOOL_LIST
        intent.scope = "all"
        for scope, pattern in _SCOPE_PATTERNS:
            if pattern.search(low):
                intent.scope = scope
                break
        return intent

    # 4) DELETE -- explicit, and only with a title to match on
    match = _DELETE_RE.match(raw)
    if match and not _ADD_RE.search(low):
        title = (match.group("title") or "").strip(" '\"‘’“”")
        title = re.sub(r"^(그|저|이)\s+", "", title)
        if 1 < len(title) <= 40:
            intent.tool = TOOL_DELETE
            intent.query = title
            return intent

    # 5) A bare schedule phrase with no verb at all: "내일 오전 10시 회의".
    #    People drop the "추가해줘" constantly, and answering a plain schedule
    #    with chat prose ("정확히는 모르겠습니다") is useless. Anything the
    #    heuristic calls *definite* is a schedule, so save it.
    #
    #    This deliberately runs last, not first: "오늘 일정 알려줘" and
    #    "내일 스케줄 뭐 있지?" are also `definite`, and classifying on
    #    definiteness up front would turn those queries into junk schedules
    #    titled "일정". The query matchers above get first refusal.
    #
    #    A question never books anything either -- without an explicit creation
    #    verb, "내일 시간 괜찮을까?" goes to chat, not the database.
    if quick.definite and not _QUESTION_RE.search(raw):
        intent.tool = TOOL_ADD
        several = _parse_items(raw, parser, now)
        intent.schedules = several or [quick]
        intent.schedule = intent.schedules[0]
        return intent

    return intent


# --------------------------------------------------------------------------- #
# Fabricated confirmations
# --------------------------------------------------------------------------- #
# Observed in production: the user typed "8/24 경영전략 엑셀 제출 / 8/31 ppt
# 1차 제출 일정 등록 2개 별도로", the date format was not understood, no tool
# ran -- and the 2B model answered "✅ 일정 등록 완료:" with two items and
# invented times. Nothing was in the database. The user believed it was.
#
# A tool reply never reaches the model (handle_chat_submit returns first), so
# by the time we are looking at generated text, *nothing was written*. Any
# claim to the contrary is false and has to be replaced rather than shown.

_CLAIM_RE = re.compile(
    r"(등록|추가|저장|삭제|입력|반영)\s*(?:을|를|이|가)?\s*"
    r"(?:모두\s*|전부\s*|정상적으로\s*)?"
    r"(?:완료|되었|됐|하였|했|해\s*두었|해\s*놓았)")

#: "등록하려면 …", "추가하시면 …" -- explaining how, not claiming to have done it.
_INSTRUCTIONAL_RE = re.compile(r"(하려면|하시려면|하시면|하는\s*방법|할\s*수\s*있)")

FALSE_CLAIM_NOTICE = (
    "⚠ 방금 답변에 '등록했다'는 내용이 있었지만 **실제로 저장되지 않았습니다.**\n"
    "AI가 지어낸 문구라 그대로 두면 안 되기에 지웠습니다.\n\n"
    "날짜를 붙여 다시 말해주시면 바로 등록됩니다.\n"
    "  · 8/24 경영전략 엑셀 제출 추가해줘\n"
    "  · 8/24 엑셀 제출 / 8/31 ppt 제출 등록  (여러 건도 한 번에)\n\n"
    "위쪽 입력창에 넣으셔도 됩니다."
)


def correct_false_action_claim(text: str) -> str:
    """Replace a fabricated "saved it" reply with the truth.

    Only ever called on model-generated text, i.e. on a turn where no tool
    ran, so a completed-action claim is always wrong.
    """
    if not text or _INSTRUCTIONAL_RE.search(text):
        return text
    if not _CLAIM_RE.search(text):
        return text
    return FALSE_CLAIM_NOTICE


# --------------------------------------------------------------------------- #
# Qwen3 <think> handling
# --------------------------------------------------------------------------- #

#: Some models (Qwen3.5) ignore ``/no_think`` and never emit ``<think>`` tags;
#: they just start their answer with an untagged English reasoning monologue.
#: Recognising it lets the UI show "생각하는 중…" instead of streaming the
#: model's scratch work into the chat bubble.
#: The closing tag, used on its own by templates that open the block for
#: the model. Defined before the helpers that need it.
CLOSE_THINK = "</think>"

PREAMBLE_RE = re.compile(
    r"^\s*(?:"
    r"(?:thinking\s+process|thought\s+process|reasoning|let\s+me\s+think|"
    r"analysis|first,?\s+i\s+need\s+to|okay,?\s+the\s+user)\b[:\s]"
    # Plain narration, which is what these models actually produce:
    # "The user is asking for help writing an email in English."
    r"|the\s+user\s+(?:is|was|wants|asks|asked|needs|seems)\b"
    r"|i\s+(?:should|need\s+to|will)\s+(?:provide|explain|respond|answer|keep)\b"
    # ...and the Korean equivalent: "사용자가 AI 비서의 능력을 묻는 질문입니다."
    r"|사용자(?:가|는|의|께서)\s"
    r"|(?:먼저|우선)\s+\S{0,12}(?:해야|확인|파악)"
    r")",
    re.IGNORECASE,
)

#: Markers a model uses to hand over from its reasoning to the real answer.
_ANSWER_MARKERS = (
    "**final answer**", "final answer:", "**answer**", "answer:",
    "**최종 답변**", "최종 답변:", "답변:", "**response**", "response:",
)


def strip_reasoning_preamble(text: str) -> str:
    """Extract the answer from an untagged reasoning monologue.

    Conservative by design: if no answer can be located the original text is
    returned unchanged. Losing the model's only output would be worse than
    showing its scratch work.
    """
    if not text:
        return text

    # An explicit end-of-reasoning tag beats every heuristic below. Qwen3's
    # template opens the block in the prompt, so the only tag in the output is
    # the closing one -- everything before it is scratch work, by definition.
    if CLOSE_THINK in text:
        return text.rsplit(CLOSE_THINK, 1)[1].strip()

    if not PREAMBLE_RE.match(text):
        return text

    low = text.lower()
    for marker in _ANSWER_MARKERS:
        index = low.rfind(marker)
        if index != -1:
            answer = text[index + len(marker):].strip(" *:\n")
            if answer:
                return answer

    # No explicit marker: fall back to the trailing prose paragraph(s) -- the
    # scratch work is numbered/bulleted, the answer usually is not.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for paragraph in reversed(paragraphs):
        if re.match(r"^(\d+[.)]|[*\-#]|\s*\*\s)", paragraph):
            continue
        if PREAMBLE_RE.match(paragraph):
            continue
        if len(paragraph) >= 15:
            return paragraph
    return text


class ThinkFilter:
    """Removes model reasoning from a *streamed* token sequence.

    Handles both shapes:

    * ``<think>…</think>`` tags (Qwen3) -- stripped inline, including tags that
      straddle chunk boundaries.
    * an untagged "Thinking Process:" monologue (Qwen3.5) -- detected from the
      first tokens, buffered silently while the UI shows a thinking indicator,
      then reduced to the answer by :func:`strip_reasoning_preamble` on flush.

    ``mode='show'`` disables filtering entirely.
    """

    OPEN, CLOSE = "<think>", "</think>"
    #: How much leading text to inspect before deciding it is a monologue.
    SNIFF_CHARS = 64

    def __init__(self, mode: str = "hide") -> None:
        self.mode = mode
        self.buffer = ""
        self.in_think = False
        self.thoughts = ""
        self.in_preamble = False
        self.used_preamble = False      # sticky: survives flush(), for warnings
        self._sniffed = False
        self._seen = ""
        #: True once a literal "<think>" has been seen in the output. Until
        #: then a lone "</think>" means the template opened the block for us.
        self.saw_open = False
        #: Reasoning that reached the UI before we knew it was reasoning.
        self.retracted = False
        #: Everything handed out as answer text -- the authoritative result,
        #: because a retraction has to be able to take earlier output back.
        self.emitted = ""

    @staticmethod
    def _partial_tail(text: str, tag: str) -> int:
        """Length of the longest suffix of ``text`` that prefixes ``tag``."""
        limit = min(len(text), len(tag) - 1)
        for size in range(limit, 0, -1):
            if tag.startswith(text[-size:]):
                return size
        return 0

    def feed(self, chunk: str) -> str:
        """Consume a chunk, return the text that should be displayed."""
        if self.mode == "show":
            return chunk

        # Untagged monologue: decide once, from the opening tokens.
        if not self._sniffed:
            self._seen += chunk
            if len(self._seen) < self.SNIFF_CHARS and "\n" not in self._seen:
                if not PREAMBLE_RE.match(self._seen):
                    return ""                     # still undecided - hold back
            self._sniffed = True
            if PREAMBLE_RE.match(self._seen):
                self.in_preamble = self.used_preamble = True
                self.thoughts += self._seen
                return ""
            chunk, self.buffer = self._seen, ""   # release what we held back

        if self.in_preamble:
            self.thoughts += chunk
            return ""

        self.buffer += chunk
        out: list[str] = []
        while True:
            if not self.in_think:
                index = self.buffer.find(self.OPEN)
                # A closing tag with no opening one: Qwen3's chat template puts
                # "<think>" into the *prompt*, so the model generates only the
                # reasoning and the closing tag. Nothing here ever sees an OPEN,
                # and the monologue was being shown to the user verbatim.
                # Everything up to that CLOSE was reasoning -- retract it.
                if not self.saw_open:
                    close_at = self.buffer.find(self.CLOSE)
                    if close_at != -1 and (index == -1 or close_at < index):
                        self.thoughts += self.emitted + "".join(out)
                        self.thoughts += self.buffer[:close_at]
                        self.used_preamble = True
                        self.retracted = True
                        self.emitted = ""
                        out = []
                        self.buffer = self.buffer[close_at + len(self.CLOSE):]
                        continue
                if index == -1:
                    keep = max(self._partial_tail(self.buffer, self.OPEN),
                               0 if self.saw_open
                               else self._partial_tail(self.buffer, self.CLOSE))
                    if keep:
                        out.append(self.buffer[:-keep])
                        self.buffer = self.buffer[-keep:]
                    else:
                        out.append(self.buffer)
                        self.buffer = ""
                    break
                out.append(self.buffer[:index])
                self.buffer = self.buffer[index + len(self.OPEN):]
                self.in_think = True
                self.saw_open = True
            else:
                index = self.buffer.find(self.CLOSE)
                if index == -1:
                    keep = self._partial_tail(self.buffer, self.CLOSE)
                    self.thoughts += self.buffer[:len(self.buffer) - keep] if keep else self.buffer
                    self.buffer = self.buffer[-keep:] if keep else ""
                    break
                self.thoughts += self.buffer[:index]
                self.buffer = self.buffer[index + len(self.CLOSE):]
                self.in_think = False
        shown = "".join(out)
        self.emitted += shown
        return shown

    def flush(self) -> str:
        """Release anything held back once the stream ends."""
        if self.mode == "show":
            return ""
        if self.in_preamble:
            # Nothing was displayed while the model reasoned; hand back just
            # the answer (or everything, if none could be identified).
            answer = strip_reasoning_preamble(self.thoughts).strip()
            self.in_preamble = False
            self.thoughts = ""
            self.emitted += answer
            return answer
        if not self._sniffed and self._seen:      # very short reply
            # Held back entirely, so it never went through the tag scanner --
            # run it now, or a brief answer keeps its <think> block.
            self._sniffed = True
            tail, self._seen = self._seen, ""
            tail = strip_think(tail, self.mode)
            self.emitted += tail
            return tail
        tail = "" if self.in_think else self.buffer
        self.buffer = ""
        self.emitted += tail
        return tail

    @property
    def thinking(self) -> bool:
        return (self.in_think or self.in_preamble) and self.mode != "show"

    @property
    def had_untagged_reasoning(self) -> bool:
        """True when the model wrote its scratch work without ``<think>`` tags."""
        return self.used_preamble


def strip_think(text: str, mode: str = "hide") -> str:
    """Non-streaming equivalent of :class:`ThinkFilter`."""
    if mode == "show" or not text:
        return text or ""
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    cleaned = re.sub(r"^.*?</think>", "", cleaned, flags=re.DOTALL) if "</think>" in cleaned else cleaned
    return cleaned.strip()


#: Drafting a reply is the one job a small local model is reliably good at:
#: it only has to reshape text it was handed, never recall a fact. Measured on
#: Qwen3.8-4B, the three cases that broke the 1.7B (accept / decline / no
#: information) all came out sendable, and the "no information" one stopped
#: inventing a status. The budget is generous because the model narrates its
#: plan first when prompted in English -- the filter removes that, but it has
#: to be allowed to finish.
REPLY_MAX_TOKENS = 900

REPLY_SYSTEM_PROMPT = (
    "당신은 한국 재보험사 직원의 이메일 답장 작성 보조입니다.\n"
    "받은 메일과 사용자의 답변 의도를 보고, 그대로 보낼 수 있는 답장 초안만 "
    "출력하세요.\n"
    "\n"
    "규칙:\n"
    "- 설명하지 말고 인사말부터 바로 시작하세요. 계획을 늘어놓지 마세요.\n"
    "- 받은 메일이 영어면 영어로, 한국어면 한국어로 씁니다.\n"
    "- 사용자가 알려준 사실만 씁니다. 날짜·금액·담당자·첨부파일을 지어내지 "
    "마세요. 모르는 것은 [   ] 로 비워 둡니다.\n"
    "- 3~6문장. 인사 - 본문 - 맺음말.\n"
    "- 보낸 사람의 이름을 알면 호칭에 씁니다. 모르면 [이름] 으로 둡니다.\n"
)


def build_reply_prompt(mail_text: str, wish: str) -> str:
    """The user turn for a reply draft."""
    return (f"[받은 메일]\n{mail_text.strip()}\n\n"
            f"[내 답변 의도]\n{wish.strip() or '적절히 회신'}\n\n"
            f"위 메일에 대한 답장 초안을 작성해줘.")


SYSTEM_PROMPT_FILENAME = "system_prompt.txt"
_PROMPT_CACHE: dict[str, Any] = {"path": None, "mtime": None, "text": None}


def system_prompt_path() -> str:
    """Where the editable copy of the chat system prompt lives."""
    return os.path.join(_DATA_DIR or app_dir(), SYSTEM_PROMPT_FILENAME)


def write_system_prompt_template(path: Optional[str] = None) -> str:
    """Drop the built-in prompt into the data folder so it can be edited.

    Kept as a plain file rather than a settings text box: it is long, it wants
    a real editor, and having it on disk means a bad edit can be fixed with
    Notepad instead of by reinstalling. Deleting the file restores the default.
    """
    target = path or system_prompt_path()
    if os.path.exists(target):
        return target
    try:
        with open(target, "w", encoding="utf-8-sig") as handle:
            handle.write(
                "# AI 대화 탭의 시스템 프롬프트입니다.\n"
                "# 이 파일을 고치면 다음 대화부터 바로 적용됩니다 (재시작 불필요).\n"
                "# 파일을 지우면 기본값으로 돌아갑니다.\n"
                "# '#' 로 시작하는 줄은 무시됩니다.\n"
                "#\n"
                "# 주의: 아래 '정확성 규칙' 은 실측으로 정해진 문구입니다.\n"
                "#   모르는 용어를 지어내는 비율 50% -> 9% 로 줄인 조합이며,\n"
                "#   특히 마지막 '일반 상식은 평소대로 설명해도 된다' 줄을 지우면\n"
                "#   아는 것까지 모른다고 답하기 시작합니다.\n"
                "\n" + CHAT_SYSTEM_PROMPT + "\n")
    except OSError as exc:
        log.warning("Could not write %s: %s", target, exc)
    return target


def load_system_prompt() -> str:
    """The active prompt: the user's file if present, else the built-in one."""
    path = system_prompt_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return CHAT_SYSTEM_PROMPT
    if _PROMPT_CACHE["path"] == path and _PROMPT_CACHE["mtime"] == mtime:
        return _PROMPT_CACHE["text"] or CHAT_SYSTEM_PROMPT
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            body = "\n".join(line for line in handle.read().splitlines()
                             if not line.lstrip().startswith("#")).strip()
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return CHAT_SYSTEM_PROMPT
    if len(body) < 20:          # emptied by accident -- fall back, do not ship a blank
        log.warning("%s is empty; using the built-in prompt", path)
        body = CHAT_SYSTEM_PROMPT
    _PROMPT_CACHE.update(path=path, mtime=mtime, text=body)
    return body


NO_THINK = "/no_think"

#: ChatML terminators. Qwen's GGUF chat template wraps every turn in
#: ``<|im_start|>role … <|im_end|>``. Without these stop strings the model
#: happily rolls past the end of its answer and starts inventing a *new* turn --
#: which is where "DolSnap is an Apple Vision Pro feature"-style nonsense comes
#: from. llama.cpp only stops on the single EOS token by default, so both
#: terminators are passed explicitly on every call.
CHATML_STOP = ["<|im_end|>", "<|endoftext|>", "<|im_start|>"]

#: Low temperature for structured extraction, moderate for conversation.
TEMP_JSON = 0.2
TEMP_CHAT = 0.6

#: Models at or above this size are slow enough on CPU to warrant a warning.
LARGE_MODEL_GB = 2.0


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

CHAT_SYSTEM_PROMPT = (
    "너는 오프라인 사내 일정 및 업무 보조 AI 비서이다. "
    "한국어로 정중하고 간결하게 답변해라.\n"
    "\n"
    "형식 규칙:\n"
    # A hard "always use bullets" rule was here to stop the 1.7B rambling. A
    # model that follows instructions properly then answered "오늘 힘들었어"
    # with a three-item checklist, which reads like a machine. Bullets are for
    # lists; a remark deserves a sentence.
    "- 나열할 항목이 둘 이상일 때만 불릿을 쓴다. 그 외에는 평범한 문장으로 답한다.\n"
    "- 전체 3~4문장 이내로 짧게 끝낸다. 서론과 맺음말은 쓰지 않는다.\n"
    "- 사용자가 영어로 물으면 영어로 답한다.\n"
    "\n"
    "정확성 규칙:\n"
    # Measured, not guessed. On three terms the model cannot know (돌스냅,
    # 특약OS이월, BDX) the original wording hedged 6/12 of the time -- the rest
    # were confident inventions, including "특약OS이월은 삼성이 2022년에
    # 출시한 안드로이드 OS". The wording below took that to 11/12 while still
    # answering all four control questions (재보험 / VLOOKUP / 손해율 / 파이썬),
    # which is what the final clause protects: a rule that refuses everything
    # scores perfectly and is useless.
    "- 사내 용어, 사내 시스템 이름, 사내 약자는 너의 학습 데이터에 없다. "
    "사용자가 쓰는 회사 고유의 표현일 가능성이 매우 높다.\n"
    "- 어떤 용어의 뜻을 물었을 때, 그 용어를 확실히 아는 경우가 아니면 "
    "첫 문장을 반드시 \"정확히는 모르겠습니다.\"로 시작하고 되물어라.\n"
    "- 특히 영어 약자(BDX, KYC, XOL 등)는 회사마다 뜻이 다르므로 "
    "풀어서 추측하지 마라. 아는 척하는 답변 하나가 모르는 답변 열 개보다 나쁘다.\n"
    "- 일반 상식(재보험, 엑셀 함수, 프로그래밍 등)은 평소대로 설명해도 된다. "
    "이 규칙은 고유명사와 약자에만 적용된다.\n"
    "- 인터넷·실시간 정보는 사용할 수 없으므로 필요하면 그렇게 말한다.\n"
    "- 생각 과정을 출력하지 않는다. 답변만 한 번 하고 멈춘다.\n"
    "- 사용자 차례를 이어서 쓰거나 흉내내지 않는다.\n"
    "\n"
    "일정 관련:\n"
    "- 사용자가 일정이나 할 일을 말씀하시면, 상단 일정 입력창을 이용하시거나 "
    "'추가해줘'를 붙여 말씀해 달라고 안내해라. 날짜와 시간을 임의로 지어내지 않는다.\n"
    "\n"
    # Without this the model answers as a generic chatbot: asked about Outlook
    # it replied "No, I cannot access your Outlook or any personal accounts",
    # which is wrong -- the app watches the inbox and the user was told so.
    # It must not deny what the program around it actually does.
    "이 프로그램이 할 수 있는 일 (너 자신은 못 하더라도 앱은 한다):\n"
    "- 일정 등록·조회·삭제, 반복 일정, 알림과 놓친 알림 재알림\n"
    "- Outlook 받은편지함 감시: 특정 키워드의 메일이 오면 알림을 띄운다. "
    "(\"'특약OS이월' 메일 오면 '결재 승인' 리마인드해줘\" 처럼 등록)\n"
    "- Outlook 오늘 일정 표시, 영업일 계산, 주간 업무 보고 만들기\n"
    "- 사용자가 이런 기능을 물으면 \"할 수 없다\"고 하지 말고 어떻게 쓰는지 안내해라.\n"
    "- 다만 네가 직접 메일을 열거나 보낼 수는 없다. 메일 내용을 붙여넣어 주시면 "
    "읽고 답장 초안을 써 줄 수 있다고 안내해라."
)

SCHEDULE_SYSTEM_PROMPT = """You turn a message into ONE JSON object. Output JSON only.

is_schedule   true only if the user is asking to create a reminder/appointment
title         short event name, no date or time words
target_time   "YYYY-MM-DD HH:MM:SS", the next occurrence, strictly after current_time
repeat_type   "none" unless the user EXPLICITLY says it repeats
              (매일->daily, 매주->weekly, 매월/매달->monthly)
repeat_detail weekly -> Korean weekday such as "수"; monthly -> day number "25"; else ""

Rules:
- Default repeat_type is "none". One future date is NOT a repeat.
- No clock time -> 09:00:00.
- Resolve 오늘/내일/모레/이번주/다음주 against current_time.

Examples (current_time 2026-08-10 09:00:00 월):
"내일 오후 3시 치과" -> {"is_schedule":true,"title":"치과","target_time":"2026-08-11 15:00:00","repeat_type":"none","repeat_detail":""}
"매주 금요일 6시 회식" -> {"is_schedule":true,"title":"회식","target_time":"2026-08-14 18:00:00","repeat_type":"weekly","repeat_detail":"금"}
"파이썬이 뭐야?" -> {"is_schedule":false,"title":"","target_time":"","repeat_type":"none","repeat_detail":""}"""

_SCHEDULE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "is_schedule": {"type": "boolean"},
        "title": {"type": "string"},
        "target_time": {"type": "string"},
        "repeat_type": {"type": "string", "enum": ["none", "daily", "weekly", "monthly"]},
        "repeat_detail": {"type": "string"},
    },
    "required": ["is_schedule", "title", "target_time", "repeat_type", "repeat_detail"],
}


def extract_json_object(text: str) -> Optional[dict]:
    """Pull the first balanced ``{...}`` object out of raw model output."""
    if not text:
        return None
    cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", strip_think(text).strip()).strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    depth, start = 0, -1
    for i, ch in enumerate(cleaned):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = json.loads(cleaned[start:i + 1])
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    start = -1
    return None


# --------------------------------------------------------------------------- #
# LLM output validation
# --------------------------------------------------------------------------- #

MAX_HORIZON_DAYS = 366 * 3

#: Explicit recurrence words. If none appear in the user's text, the model is
#: not allowed to invent a repeating schedule.
_REPEAT_WORDS = re.compile(
    r"매일|매주|매월|매달|날마다|주마다|달마다|마다|격주|every\s*(day|week|month)|daily|weekly|monthly",
    re.IGNORECASE,
)


def validate_llm_result(
    result: ParseResult,
    text: str,
    now: datetime,
    heuristic: Optional[ParseResult] = None,
) -> ParseResult:
    """Repair or reject an LLM extraction.

    Guards, in order of importance:

    1. **No invented recurrence.** Unless the user actually wrote a recurrence
       word, ``repeat_type`` is forced to ``none``. This is the single most
       damaging hallucination the model makes.
    2. Recurrence details must be parseable; otherwise they are derived from
       the target time.
    3. The time must be in the future and inside a sane horizon.
    4. When the heuristic also found a time, it wins — it is measurably more
       accurate on Korean relative dates.
    """
    if not result.is_schedule:
        return result

    user_repeats = bool(_REPEAT_WORDS.search(text or ""))
    heur_repeat = heuristic.repeat_type if heuristic else REPEAT_NONE

    # 1 + 2: recurrence sanity
    if not user_repeats:
        if result.repeat_type != REPEAT_NONE:
            log.info("Dropped invented repeat_type=%s for %r", result.repeat_type, text)
        result.repeat_type, result.repeat_detail = REPEAT_NONE, ""
    elif heur_repeat != REPEAT_NONE:
        # The heuristic saw the recurrence word too - trust its reading.
        result.repeat_type = heur_repeat
        result.repeat_detail = heuristic.repeat_detail if heuristic else ""

    if result.repeat_type == REPEAT_WEEKLY:
        days = parse_weekdays(result.repeat_detail)
        result.repeat_detail = format_weekdays(
            days or [(result.target_time or now).weekday()])
    elif result.repeat_type == REPEAT_MONTHLY:
        day = parse_month_day(result.repeat_detail)
        result.repeat_detail = str(day or (result.target_time or now).day)
    else:
        result.repeat_detail = ""

    # 4: prefer the heuristic's timing when it found any
    if heuristic and heuristic.target_time and heuristic.confidence >= 0.5:
        if result.target_time != heuristic.target_time:
            log.info("Preferring heuristic time %s over LLM %s for %r",
                     heuristic.target_time, result.target_time, text)
        result.target_time = heuristic.target_time

    # 3: horizon / past checks
    if result.target_time is None:
        result.is_schedule = False
        result.error = "no_time"
        return result
    if result.target_time <= now:
        bumped = None
        if result.repeat_type != REPEAT_NONE:
            bumped = compute_next_trigger(
                result.target_time, result.repeat_type, result.repeat_detail, now)
        elif (now - result.target_time) < timedelta(hours=24):
            bumped = result.target_time + timedelta(days=1)
        if bumped is None:
            result.is_schedule = False
            result.error = "past_time"
            return result
        result.target_time = bumped
    if result.target_time > now + timedelta(days=MAX_HORIZON_DAYS):
        result.is_schedule = False
        result.error = "out_of_range"
        return result

    if not result.title:
        result.title = (heuristic.title if heuristic and heuristic.title
                        else (text or "").strip()[:40] or "일정")

    # LLM answers are never saved silently.
    result.needs_confirm = True
    return result


# --------------------------------------------------------------------------- #
# Worker (background QThread)
# --------------------------------------------------------------------------- #

class LlmWorker(QObject):
    """Owns the llama.cpp context. Every slot here runs off the GUI thread."""

    # state: missing | loading | ready | error | unavailable | oom
    model_state = Signal(str, str)
    model_note = Signal(str)            # advisory shown once in the UI
    chat_started = Signal(int)
    chat_token = Signal(int, str)
    chat_thinking = Signal(int, bool)
    chat_finished = Signal(int, str)
    chat_error = Signal(int, str)
    parse_finished = Signal(int, object)

    def __init__(self, options: Optional[dict] = None) -> None:
        super().__init__()
        self.options = dict(options or {})
        self._llm: Any = None
        self._cancel = threading.Event()
        self._state = "idle"
        self.model_path: Optional[str] = None

    # ---- options ---------------------------------------------------------- #

    def _opt(self, key: str, default: Any) -> Any:
        value = self.options.get(key, default)
        return default if value in (None, "") else value

    @Slot(object)
    def set_options(self, options: object) -> None:
        """Apply new settings; reload the model if a load-time option changed."""
        new = dict(options or {})
        reload_keys = ("model_path", "n_ctx", "n_threads")
        needs_reload = any(new.get(k) != self.options.get(k) for k in reload_keys)
        self.options = new
        if needs_reload and self._llm is not None:
            log.info("LLM options changed -> reloading model")
            self.unload_model()
            self.load_model()

    # ---- lifecycle -------------------------------------------------------- #

    def request_cancel(self) -> None:
        """Thread-safe stop flag, called *directly* from the GUI thread.

        A queued slot would sit behind the running generation and arrive far
        too late, so this deliberately bypasses the signal system.
        """
        self._cancel.set()

    @Slot()
    def load_model(self) -> None:
        """Import llama_cpp and build the context. Always emits ``model_state``."""
        if self._llm is not None:
            self.model_state.emit("ready", os.path.basename(self.model_path or ""))
            return

        path = find_model_path(str(self._opt("model_path", "")))
        if not path:
            self._state = "missing"
            self.model_state.emit(
                "missing",
                "models 폴더에 GGUF 모델 파일이 없습니다. "
                "설정 → AI 에서 모델 파일을 지정하거나 models 폴더에 넣어주세요.",
            )
            return
        self.model_path = path

        try:
            from llama_cpp import Llama
        except Exception as exc:                        # noqa: BLE001
            self._state = "unavailable"
            log.exception("llama-cpp-python import failed")
            self.model_state.emit(
                "unavailable",
                f"llama-cpp-python을 불러오지 못했습니다: {exc}. "
                "pip install llama-cpp-python --extra-index-url "
                "https://abetlen.github.io/llama-cpp-python/whl/cpu",
            )
            return

        threads = int(self._opt("n_threads", 0)) or max(2, (os.cpu_count() or 4) // 2)
        n_ctx = int(self._opt("n_ctx", 4096))
        self._state = "loading"
        self.model_state.emit("loading", os.path.basename(path))
        try:
            self._llm = Llama(
                model_path=path,
                n_ctx=n_ctx,
                n_threads=threads,
                n_batch=256,
                use_mlock=False,
                verbose=False,
            )
            self._state = "ready"
            log.info("Model loaded: %s (n_ctx=%d, threads=%d)", path, n_ctx, threads)
            self.model_state.emit("ready", os.path.basename(path))

            # CPU inference scales badly with size: ~3 s/answer at 1.5 GB versus
            # ~2 min at 3 GB. Say so rather than letting the chat look hung.
            try:
                size_gb = os.path.getsize(path) / (1024 ** 3)
                others = [p for p in list_models() if p != path]
                if size_gb >= LARGE_MODEL_GB and others:
                    self.model_note.emit(
                        f"{os.path.basename(path)} 은(는) {size_gb:.1f} GB로 큽니다. "
                        f"CPU에서는 응답이 수십 초~수 분 걸릴 수 있어요. "
                        f"설정 → AI 에서 더 작은 모델을 선택하면 훨씬 빨라집니다.")
            except OSError:
                pass
        except MemoryError:
            self._llm = None
            self._state = "oom"
            log.exception("Model load ran out of memory")
            self.model_state.emit(
                "oom", "메모리가 부족합니다. 설정에서 컨텍스트 크기(n_ctx)를 줄여보세요.")
        except Exception as exc:                        # noqa: BLE001
            self._llm = None
            self._state = "error"
            log.exception("Model load failed")
            self.model_state.emit("error", f"모델 로드 실패: {exc}")

    @Slot()
    def unload_model(self) -> None:
        self._cancel.set()
        llm, self._llm = self._llm, None
        if llm is not None:
            try:
                close = getattr(llm, "close", None)
                if callable(close):
                    close()
            except Exception:                            # noqa: BLE001
                log.debug("Model close raised", exc_info=True)
        self._state = "idle"

    # ---- chat -------------------------------------------------------------- #

    @Slot(int, object)
    def handle_chat(self, request_id: int, messages: object) -> None:
        """Stream a chat completion, emitting one signal per visible token."""
        self._cancel.clear()
        try:
            if self._llm is None:
                self.load_model()
            if self._llm is None:
                self.chat_error.emit(
                    request_id, "로컬 모델이 준비되지 않았습니다. 설정 → AI 에서 모델을 확인해주세요.")
                return

            think_mode = str(self._opt("thinking", "hide"))
            override_system, override_tokens = None, None
            if isinstance(messages, dict):
                override_system = messages.get("system")
                override_tokens = messages.get("max_tokens")
                messages = messages.get("messages") or []
            # Re-read each turn: editing system_prompt.txt takes effect
            # on the next message, with no restart.
            payload = [{"role": "system",
                        "content": override_system or load_system_prompt()}]
            payload += [dict(m) for m in (messages or [])]
            if think_mode == "off" and payload:
                # Qwen3 reads /no_think from the latest user turn.
                for message in reversed(payload):
                    if message.get("role") == "user":
                        message["content"] = f"{message['content']} {NO_THINK}"
                        break

            self.chat_started.emit(request_id)
            think = ThinkFilter("show" if think_mode == "show" else "hide")
            visible: list[str] = []
            was_thinking = False

            stream = self._llm.create_chat_completion(
                messages=payload,
                max_tokens=int(override_tokens
                               or self._opt("max_tokens", 512)),
                temperature=float(self._opt("temperature", TEMP_CHAT)),
                top_p=0.9,
                repeat_penalty=1.1,
                stop=CHATML_STOP,          # keep the model inside its own turn
                stream=True,
            )
            for chunk in stream:
                if self._cancel.is_set():
                    log.info("Chat #%d cancelled", request_id)
                    break
                try:
                    delta = chunk["choices"][0].get("delta", {})
                except (KeyError, IndexError, TypeError):
                    continue
                piece = delta.get("content")
                if not piece:
                    continue
                shown = think.feed(piece)
                if think.thinking != was_thinking:
                    was_thinking = think.thinking
                    self.chat_thinking.emit(request_id, was_thinking)
                if shown:
                    visible.append(shown)
                    self.chat_token.emit(request_id, shown)

            tail = think.flush()
            if tail:
                visible.append(tail)
                self.chat_token.emit(request_id, tail)
            if was_thinking:
                self.chat_thinking.emit(request_id, False)

            # `emitted` rather than the streamed list: a late "</think>"
            # retracts reasoning that was already sent to the UI, and
            # end_stream() replaces the bubble with this value.
            answer = (think.emitted if think.retracted
                      else "".join(visible)).strip()
            if think.had_untagged_reasoning:
                # The model ignored /no_think and wrote its scratch work in the
                # open. Say so once instead of leaving the user to wonder.
                log.warning("Model emitted untagged reasoning; answer extracted "
                            "from %d chars of monologue", len(think.thoughts))
                self.model_note.emit(
                    "이 모델은 사고 과정을 그대로 출력합니다. 응답만 추려서 표시했습니다. "
                    "설정 → AI 에서 더 작은 모델을 선택하면 빠르고 깔끔합니다.")
            self.chat_finished.emit(request_id, answer)
        except MemoryError:
            log.exception("Chat OOM")
            self.chat_error.emit(request_id, "메모리가 부족합니다. 대화 기록을 지우거나 컨텍스트를 줄여보세요.")
        except Exception as exc:                          # noqa: BLE001
            log.exception("Chat generation failed")
            self.chat_error.emit(request_id, f"생성 중 오류가 발생했습니다: {exc}")

    # ---- schedule extraction ----------------------------------------------- #

    @Slot(int, str, str)
    def handle_parse(self, request_id: int, text: str, now_str: str) -> None:
        self._cancel.clear()
        now = parse_time(now_str) or datetime.now()
        heuristic = HeuristicParser().parse(text, now)
        try:
            if self._llm is None:
                self.load_model()
            if self._llm is None:
                heuristic.error = "model_unavailable"
                heuristic.needs_confirm = True
                self.parse_finished.emit(request_id, heuristic)
                return

            prompt = (f"current_time: {fmt_time(now)} ({WEEKDAY_NAMES_KO[now.weekday()]})\n"
                      f"{text} {NO_THINK}")
            raw = self._complete_json([
                {"role": "system", "content": SCHEDULE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ])
            data = extract_json_object(raw)
            if not data:
                log.warning("Schedule JSON unparseable: %r", (raw or "")[:300])
                heuristic.error = "json_parse_failed"
                heuristic.needs_confirm = True
                self.parse_finished.emit(request_id, heuristic)
                return

            result = validate_llm_result(
                ParseResult.from_dict(data, raw_text=text, source="llm"), text, now, heuristic)
            self.parse_finished.emit(request_id, result)
        except Exception as exc:                          # noqa: BLE001
            log.exception("Schedule extraction failed")
            heuristic.error = str(exc)
            heuristic.needs_confirm = True
            self.parse_finished.emit(request_id, heuristic)

    def _complete_json(self, messages: list[dict]) -> str:
        """Grammar-constrained JSON, with a graceful unconstrained fallback."""
        kwargs = dict(messages=messages, max_tokens=320, temperature=TEMP_JSON,
                      stop=CHATML_STOP, stream=False)
        try:
            out = self._llm.create_chat_completion(
                response_format={"type": "json_object", "schema": _SCHEDULE_JSON_SCHEMA}, **kwargs)
        except Exception as exc:                          # noqa: BLE001
            log.info("Constrained JSON unsupported (%s); retrying unconstrained", exc)
            try:
                out = self._llm.create_chat_completion(**kwargs)
            except Exception:                             # noqa: BLE001
                log.exception("Unconstrained JSON also failed")
                return ""
        try:
            return out["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""


# --------------------------------------------------------------------------- #
# Controller (GUI-thread facade)
# --------------------------------------------------------------------------- #

class LlmController(QObject):
    """Owns the worker thread and re-emits its signals for the UI."""

    model_state = Signal(str, str)
    model_note = Signal(str)
    chat_started = Signal(int)
    chat_token = Signal(int, str)
    chat_thinking = Signal(int, bool)
    chat_finished = Signal(int, str)
    chat_error = Signal(int, str)
    parse_finished = Signal(int, object)

    _do_load = Signal()
    _do_unload = Signal()
    _do_chat = Signal(int, object)
    _do_parse = Signal(int, str, str)
    _do_options = Signal(object)

    #: Heuristic results at or above this confidence are saved without asking.
    HEURISTIC_TRUST = 0.75
    #: Below this, there is no usable time at all -> ask the LLM.
    HEURISTIC_FLOOR = 0.40

    def __init__(self, options: Optional[dict] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.heuristic = HeuristicParser()
        self.options = dict(options or {})
        self._request_seq = 0
        self._model_state = "idle"
        self._model_message = ""

        self.thread = QThread()
        self.thread.setObjectName("LlmThread")
        self.worker = LlmWorker(self.options)
        self.worker.moveToThread(self.thread)

        self.worker.model_state.connect(self._on_model_state)
        self.worker.model_note.connect(self.model_note)
        self.worker.chat_started.connect(self.chat_started)
        self.worker.chat_token.connect(self.chat_token)
        self.worker.chat_thinking.connect(self.chat_thinking)
        self.worker.chat_finished.connect(self.chat_finished)
        self.worker.chat_error.connect(self.chat_error)
        self.worker.parse_finished.connect(self.parse_finished)

        self._do_load.connect(self.worker.load_model)
        self._do_unload.connect(self.worker.unload_model)
        self._do_chat.connect(self.worker.handle_chat)
        self._do_parse.connect(self.worker.handle_parse)
        self._do_options.connect(self.worker.set_options)

    # ---- lifecycle -------------------------------------------------------- #

    def start(self, preload: bool = True) -> None:
        if not self.thread.isRunning():
            self.thread.start()
        if preload:
            self._do_load.emit()

    def apply_options(self, options: dict) -> None:
        self.options = dict(options or {})
        self._do_options.emit(self.options)

    def reload_model(self) -> None:
        self._do_unload.emit()
        self._do_load.emit()

    def shutdown(self, timeout_ms: int = 8000) -> None:
        try:
            self.worker.request_cancel()
            if self.thread.isRunning():
                self._do_unload.emit()
                self.thread.quit()
                if not self.thread.wait(timeout_ms):
                    log.warning("LLM thread did not stop in %d ms; terminating", timeout_ms)
                    self.thread.terminate()
                    self.thread.wait(2000)
        except RuntimeError:
            pass

    @property
    def model_ready(self) -> bool:
        return self._model_state == "ready"

    @property
    def can_chat(self) -> bool:
        """True unless the model is known to be unusable.

        ``idle`` counts as usable: with preload disabled nothing has been tried
        yet, and the worker loads on demand when the first message arrives.
        """
        return self._model_state not in ("missing", "unavailable", "error", "oom")

    @property
    def current_state(self) -> str:
        return self._model_state

    @property
    def state_message(self) -> str:
        return self._model_message

    def _on_model_state(self, state: str, message: str) -> None:
        self._model_state = state
        self._model_message = message
        self.model_state.emit(state, message)

    # ---- requests --------------------------------------------------------- #

    def _next_id(self) -> int:
        self._request_seq += 1
        return self._request_seq

    def send_chat(self, messages: list[dict], system: Optional[str] = None,
                  max_tokens: Optional[int] = None) -> int:
        """`system` / `max_tokens` override the defaults for this call only.

        A reply draft needs its own instructions and a bigger budget than a
        chat turn, without disturbing either for the next message.
        """
        request_id = self._next_id()
        if not self.thread.isRunning():
            self.start(preload=False)
        payload = {"messages": messages, "system": system,
                   "max_tokens": max_tokens}
        self._do_chat.emit(request_id, payload)
        return request_id

    def cancel(self) -> None:
        self.worker.request_cancel()

    def parse_schedule(
        self, text: str, now: Optional[datetime] = None
    ) -> tuple[int, Optional[ParseResult]]:
        """Hybrid parse.

        Returns ``(request_id, immediate_result)``. When ``immediate_result``
        is not ``None`` the answer is final and no signal follows; otherwise
        wait for :attr:`parse_finished` with the returned id.
        """
        now = now or datetime.now()
        quick = self.heuristic.parse(text, now)

        # ---- heuristic-first guarantee ------------------------------------ #
        # Any concrete date or clock token ("내일", "오전 10시", "30분 뒤",
        # "8월 15일", "다음주 수요일" …) is answered here and now. No LLM job is
        # queued, no confirmation dialog, no chance of a hallucinated time or
        # an invented weekly repeat. This path is 100 % deterministic.
        if quick.definite:
            quick.needs_confirm = False
            log.debug("Heuristic final (%.2f, date=%s time=%s): %s @ %s",
                      quick.confidence, quick.explicit_date, quick.explicit_time,
                      quick.title, quick.target_time)
            return 0, quick

        # Only a vague part-of-day word ("점심 뭐 먹지") -- usable but far too
        # weak to become an alarm on its own, so confirm it instead.
        if quick.usable:
            quick.needs_confirm = True
            return 0, quick

        use_llm = bool(self.options.get("use_llm_for_parsing", True))
        can_ask = use_llm and self._model_state in ("ready", "loading", "idle")
        if not can_ask:
            quick.needs_confirm = True
            if not self.model_ready:
                quick.error = quick.error or "model_unavailable"
            return 0, quick

        # Nothing temporal at all -> the model is the only chance before the
        # user has to type it by hand.
        request_id = self._next_id()
        if not self.thread.isRunning():
            self.start(preload=True)
        self._do_parse.emit(request_id, text, fmt_time(now))
        return request_id, None


# --------------------------------------------------------------------------- #
# Chat history helper
# --------------------------------------------------------------------------- #

def build_chat_context(messages: Iterable[Any], max_turns: int = 12,
                       max_chars: int = 6000) -> list[dict]:
    """``ChatMessage`` rows -> OpenAI-style message list, newest kept first."""
    items = list(messages)[-max(1, max_turns) * 2:]
    payload: list[dict] = []
    budget = max_chars
    for msg in reversed(items):
        text = (getattr(msg, "message", "") or "").strip()
        if not text:
            continue
        if budget - len(text) < 0:
            break
        budget -= len(text)
        role = "user" if getattr(msg, "sender", "user") == "user" else "assistant"
        payload.append({"role": role, "content": text})
    payload.reverse()
    return payload


# --------------------------------------------------------------------------- #
# Self-test: ``py llm_engine.py``  (no model required)
# --------------------------------------------------------------------------- #

def _selftest() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.WARNING)
    parser = HeuristicParser()
    now = datetime(2026, 8, 10, 9, 0, 0)     # Monday
    failures = 0

    cases = [
        # text, repeat, when, title-substring
        ("매주 월요일 10시 주간 회의", "weekly", "2026-08-10 10:00:00", "주간 회의"),
        ("내일 오후 3시 치과 예약", "none", "2026-08-11 15:00:00", "치과 예약"),
        ("매일 아침 7시 운동", "daily", "2026-08-11 07:00:00", "운동"),
        ("8월 15일 14:30 팀 미팅", "none", "2026-08-15 14:30:00", "팀 미팅"),
        ("30분 뒤 휴식", "none", "2026-08-10 09:30:00", "휴식"),
        ("매월 25일 월급 확인", "monthly", "2026-08-25 09:00:00", "월급 확인"),
        ("금요일 저녁 7시 회식", "none", "2026-08-14 19:00:00", "회식"),
        ("2026-12-25 09:00 크리스마스", "none", "2026-12-25 09:00:00", "크리스마스"),
        ("every monday 10am standup", "weekly", "2026-08-10 10:00:00", "standup"),
        ("tomorrow 3pm dentist", "none", "2026-08-11 15:00:00", "dentist"),
        # --- cases the 1.7B model got wrong ---
        ("다음주 수요일 오후 2시 반에 치과 예약", "none", "2026-08-19 14:30:00", "치과 예약"),
        ("낼모레 점심때 김부장님이랑 미팅", "none", "2026-08-12 12:00:00", "김부장님"),
        ("담달 첫째주 금요일 회식", "none", "2026-09-04 09:00:00", "회식"),
        ("매주 화요일이랑 목요일 아침 7시 헬스", "weekly", "2026-08-11 07:00:00", "헬스"),
        ("8월 말일 오후 6시 분기 마감", "none", "2026-08-31 18:00:00", "마감"),
        ("다음달 15일 오전 10시 정기점검", "none", "2026-09-15 10:00:00", "정기점검"),
        ("매주 월수금 오후 8시 스터디", "weekly", "2026-08-10 20:00:00", "스터디"),
        ("3일 뒤 오후 2시 서류 제출", "none", "2026-08-13 14:00:00", "서류 제출"),
        ("이번주 주말 브런치", "none", "2026-08-15 09:00:00", "브런치"),
        ("2시간 뒤 회의실 예약", "none", "2026-08-10 11:00:00", "회의실"),
        # --- reported hallucination case: must stay 100 % local ---
        ("내일 오전 10시 돌스냅 촬영", "none", "2026-08-11 10:00:00", "돌스냅 촬영"),
        ("모레 오후 4시 스튜디오 미팅", "none", "2026-08-12 16:00:00", "스튜디오 미팅"),
        # unqualified 1~6시 means the afternoon; an explicit 오전 overrides it
        ("3시 커피", "none", "2026-08-10 15:00:00", "커피"),
        ("오전 3시 알람", "none", "2026-08-11 03:00:00", "알람"),
        ("다음주 수요일 2시 반 치과", "none", "2026-08-19 14:30:00", "치과"),
    ]
    print("--- heuristic ---")
    for text, repeat, when, title in cases:
        r = parser.parse(text, now)
        got = fmt_time(r.target_time) if r.target_time else None
        ok = (r.repeat_type == repeat) and (got == when) and (title in r.title)
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text!r:34} -> {got} | {r.repeat_type:7} | "
              f"{r.title!r} ({r.confidence:.2f})")

    print("\n--- non-schedule ---")
    for text in ("파이썬 데코레이터가 뭐야?", "회의록 요약해줘", "hello there", "고마워"):
        r = parser.parse(text, now)
        ok = not r.definite
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text!r} -> definite={r.definite} "
              f"conf={r.confidence:.2f}")

    print("\n--- heuristic-first guarantee (no LLM for clear input) ---")
    definite_inputs = [
        "내일 오전 10시 돌스냅 촬영", "모레 오후 3시 미팅", "30분 뒤 커피",
        "8월 15일 14:30 팀 미팅", "다음주 수요일 2시 반 치과", "매주 월요일 10시 회의",
        "오늘 저녁 7시 약속", "매월 25일 월급 확인", "3일 뒤 서류 제출",
    ]
    for text in definite_inputs:
        r = parser.parse(text, now)
        ok = r.definite and not r.needs_confirm
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text!r:30} definite={r.definite} "
              f"(date={r.explicit_date} time={r.explicit_time}) -> {r.target_time}")

    vague_inputs = ["점심 뭐 먹지", "저녁에 뭐하지", "아침에 생각해보자"]
    for text in vague_inputs:
        r = parser.parse(text, now)
        ok = not r.definite
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} vague {text!r:24} definite={r.definite} "
              f"(would ask the user)")

    print("\n--- chat tool intent ---")
    tool_cases = [
        # text, expected tool, extra check
        ("매월 12일 특약OS이월 추가해줘", TOOL_ADD, "특약OS이월"),
        ("내일 오전 10시 돌스냅 촬영 등록해줘", TOOL_ADD, "돌스냅 촬영"),
        ("매주 월요일 9시 주간회의 잡아줘", TOOL_ADD, "주간회의"),
        ("오늘 일정 알려줘", TOOL_LIST, "today"),
        ("이번주 할일 보여줘", TOOL_LIST, "week"),
        ("내일 스케줄 뭐 있지?", TOOL_LIST, "tomorrow"),
        ("할일 목록", TOOL_LIST, "all"),
        ("완료된 일정 정리해줘", TOOL_CLEAR, ""),
        ("끝난 일정 다 지워줘", TOOL_CLEAR, ""),
        ("치과 예약 삭제해줘", TOOL_DELETE, "치과 예약"),
        ("기다리는 메일 목록", TOOL_LIST_EMAIL, ""),
        ("메일 알림 리스트", TOOL_LIST_EMAIL, ""),
        ("메일 리마인더", TOOL_LIST_EMAIL, ""),
        ("등록된 메일 감지 현황 보여줘", TOOL_LIST_EMAIL, ""),
        # --- schedule phrases with NO creation verb (the reported bug) ---
        ("내일 오전 10시 회의", TOOL_ADD, "회의"),
        ("8월 15일 3시 치과", TOOL_ADD, "치과"),
        ("30분 뒤 커피", TOOL_ADD, "커피"),
        ("매주 월요일 9시 주간보고", TOOL_ADD, "주간보고"),
        ("모레 오후 2시 스튜디오 미팅", TOOL_ADD, "스튜디오 미팅"),
        # --- email rule deletion ---
        ("특약 메일 알림 삭제해줘", TOOL_DELETE_EMAIL, "특약"),
        ("월마감 메일 감지 지워줘", TOOL_DELETE_EMAIL, "월마감"),
        ("메일 알림 삭제", TOOL_DELETE_EMAIL, ""),
        # --- single-syllable weekdays only count with real context ---
        ("수요일 10시 회의", TOOL_ADD, "회의"),
        ("매주 수 10시 스터디", TOOL_ADD, "스터디"),
        ("다음주 수 3시 외근", TOOL_ADD, "외근"),
        # --- from the production log: the to-do list is the container, not
        #     part of the task name ---
        ("매월 12일에 할일에 특약OS이월 넣어줘", TOOL_ADD, "특약OS이월"),
        ("2026.7월 프론팅계약 bdx 8월18일 할일로 등록해줘", TOOL_ADD, "2026.7월 프론팅계약 bdx"),
        ("9월 9일까지 TCPL KYC 서류 확보", TOOL_ADD, "TCPL KYC 서류 확보"),
        # Business days: deadlines here are quoted in working days.
        ("3영업일 뒤 서류 제출 등록", TOOL_ADD, "서류 제출"),
        ("다음 영업일 결재 확인 추가", TOOL_ADD, "결재 확인"),
        # English dates -- reinsurance mail is bilingual. Reported from real
        # use: "1pm on August 27th   JB BODA 미팅" kept the time, lost the
        # date entirely, and filed "on August 27th JB BODA 미팅" as the title.
        ("1pm on August 27th   JB BODA 미팅", TOOL_ADD, "JB BODA 미팅"),
        ("Aug 27 2pm broker call", TOOL_ADD, "broker call"),
        ("27 August 3pm renewal meeting", TOOL_ADD, "renewal meeting"),
        ("Dec 1, 2026 treaty renewal", TOOL_ADD, "treaty renewal"),
        ("meeting on Sep 3rd at 10am", TOOL_ADD, "meeting"),
        # ...but a stray preposition inside a title is not a leftover.
        ("hands on training Sep 3rd 2pm", TOOL_ADD, "hands on training"),
        # "주간보고" is a report command *and* a very common meeting title.
        ("매주 월요일 9시 주간보고", TOOL_ADD, "주간보고"),
        ("내일 10시 업무보고 등록", TOOL_ADD, "업무보고"),
        ("이번주 한 일 알려줘", TOOL_REPORT, ""),
        ("주간보고 뽑아줘", TOOL_REPORT, ""),
        ("김보성 db 카피 요청 3시간 후 알림 설정해줘", TOOL_ADD, "김보성 db 카피 요청"),
        ("내일 아레나 계산서 처리 마무리하기 오전 11시", TOOL_ADD, "아레나 계산서 처리 마무리하기"),
        # "8/24" -- office shorthand the parser used to ignore entirely, which
        # sent the whole message to chat and got a fabricated confirmation.
        ("8/24 경영전략 엑셀 제출 추가해줘", TOOL_ADD, "경영전략 엑셀 제출"),
        ("9/3 부서 회식 등록", TOOL_ADD, "부서 회식"),
        # …but a fraction is not a date.
        ("3/4 정도만 끝냈어", TOOL_NONE, ""),
        ("진행률이 2/3쯤 돼", TOOL_NONE, ""),
    ]
    for text, expect, extra in tool_cases:
        intent = detect_tool_intent(text, parser, now)
        ok = intent.tool == expect
        if ok and expect == TOOL_ADD:
            ok = intent.schedule is not None and extra in intent.schedule.title
        elif ok and expect == TOOL_LIST:
            ok = intent.scope == extra
        elif ok and expect in (TOOL_DELETE, TOOL_DELETE_EMAIL):
            ok = intent.query == extra
        failures += 0 if ok else 1
        detail = (intent.schedule.title if intent.schedule else
                  intent.scope if expect == TOOL_LIST else intent.query)
        if ok and expect == TOOL_ADD and intent.schedule:
            # A bare phrase must still land on the right moment, and the title
            # must be exactly the task -- no container words leaking in.
            ok = (intent.schedule.target_time is not None
                  and intent.schedule.title == extra)
            detail = f"{intent.schedule.title!r} @ {intent.schedule.target_time}"
        print(f"{'OK  ' if ok else 'FAIL'} {text!r:34} -> {intent.tool:6} {detail!r}")

    # --- several items in one message -------------------------------------- #
    # The 2026-08-20 report: two schedules asked for, none saved, and the model
    # answered "일정 등록 완료" with times it made up.
    multi_cases = [
        ("8/24 경영전략 엑셀 제출 / 8/31 ppt 1차 제출 일정 등록 2개 별도로",
         [("경영전략 엑셀 제출", (8, 24)), ("ppt 1차 제출", (8, 31))]),
        ("8/24 엑셀 제출, 8/31 ppt 제출 추가해줘",
         [("엑셀 제출", (8, 24)), ("ppt 제출", (8, 31))]),
        ("내일 회의 그리고 모레 보고서 추가해줘",
         [("회의", (8, 11)), ("보고서", (8, 12))]),
        # One vague piece means we cannot trust the split at all. The leftover
        # text stays in the title on purpose: trimming it to a tidy "엑셀 제출"
        # would quietly swallow the half of the request we could not schedule,
        # and the user would never know the ppt part went nowhere.
        ("8/24 엑셀 제출, 그리고 나중에 ppt도 추가해줘",
         [("엑셀 제출, 그리고 나중에 ppt도", (8, 24))]),
    ]
    for text, expected in multi_cases:
        intent = detect_tool_intent(text, parser, now)
        got = [(s.title, (s.target_time.month, s.target_time.day))
               for s in intent.schedules]
        ok = intent.tool == TOOL_ADD and got == expected
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text[:38]!r:40} -> {got}")

    # --- a fabricated confirmation must never reach the user ---------------- #
    fabricated = ("✅ 일정 등록 완료:\n1. 8/24 경영전략 엑셀 제출 (09:00)\n"
                  "2. 8/31 PPT 1 차 제출 (15:00)")
    claim_cases = [
        (fabricated, True), ("일정을 추가했습니다.", True),
        ("삭제되었습니다.", True), ("모두 등록 완료했습니다", True),
        ("일정을 등록하려면 날짜를 함께 알려주세요.", False),
        ("위쪽 입력창에 넣으시면 등록할 수 있습니다.", False),
        ("회의는 보통 오전에 하는 것이 좋습니다.", False),
    ]
    for text, should_replace in claim_cases:
        replaced = correct_false_action_claim(text) == FALSE_CLAIM_NOTICE
        ok = replaced == should_replace
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} claim {text[:30]!r:34} "
              f"replaced={replaced}")

    print("\n--- awaited-email intents ---")
    email_cases = [
        ("'특약OS이월' 메일 오면 '결재 시스템 승인' 리마인드해줘",
         "특약OS이월", "결재 시스템 승인"),
        ("특약OS이월 메일 오면 담당자 이메일 공유해줘",
         "특약OS이월", "담당자 이메일 공유"),
        ("월마감 이메일 오면 마감 자료 취합",
         "월마감", "마감 자료 취합"),
        ("결재승인 메일이 도착하면 팀장님께 보고",
         "결재승인", "팀장님께 보고"),
        ("계약서 관련 메일 받으면 스캔본 저장해줘",
         "계약서", "스캔본 저장"),
    ]
    for text, kw, action in email_cases:
        intent = detect_tool_intent(text, parser, now)
        ok = (intent.tool == TOOL_ADD_EMAIL and intent.keywords == kw
              and intent.action == action)
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text!r:44}\n"
              f"      -> {intent.tool} kw={intent.keywords!r} action={intent.action!r}")

    print("\n--- single-syllable weekday must not hijack prose ---")
    # 수 = the dependent noun in "할 수 있다"; 일 = work/day; 목 = neck.
    prose = [
        "너가 할 수 있는게 뭐야?", "나랑 가위바위보할까?", "볼 수 있어?",
        "수가 없네", "일 처리 좀 도와줘", "목이 아파", "지금 금액이 얼마야?",
        "이거 할 수 있을까요?", "화 내지 마",
    ]
    for text in prose:
        r = parser.parse(text, now)
        intent = detect_tool_intent(text, parser, now)
        ok = intent.tool == TOOL_NONE and not r.definite
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {text!r:26} tool={intent.tool:6} "
              f"definite={r.definite} title={r.title!r}")

    # Plain conversation must NOT trigger a tool call.
    for text in ("회의 언제가 좋을까?", "파이썬 데코레이터가 뭐야?",
                 "안내 메일 초안 좀 써줘", "고마워", "일정 관리 팁 알려주는 책 추천"):
        intent = detect_tool_intent(text, parser, now)
        ok = intent.tool in (TOOL_NONE, TOOL_LIST) if "일정" in text else intent.tool == TOOL_NONE
        # the book question mentions 일정 but must not become a DB action
        if text.startswith("일정 관리 팁"):
            ok = intent.tool == TOOL_NONE
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} chat {text!r:30} -> {intent.tool}")

    print("\n--- ChatML stop tokens ---")
    ok = ("<|im_end|>" in CHATML_STOP and "<|endoftext|>" in CHATML_STOP
          and TEMP_JSON == 0.2 and TEMP_CHAT == 0.6)
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} stop={CHATML_STOP} "
          f"temp(json)={TEMP_JSON} temp(chat)={TEMP_CHAT}")

    print("\n--- <think> filter ---")
    filt = ThinkFilter("hide")
    stream = ["<th", "ink>\nreason", "ing here\n</thi", "nk>\n\n안녕", "하세요!"]
    shown = "".join(filt.feed(part) for part in stream) + filt.flush()
    ok = shown.strip() == "안녕하세요!" and "reasoning here" in filt.thoughts
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} split tags -> {shown.strip()!r}")

    filt2 = ThinkFilter("hide")
    shown2 = filt2.feed("<think>\n\n</think>\n\n답변") + filt2.flush()
    ok = shown2.strip() == "답변"
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} empty think -> {shown2.strip()!r}")

    plain = ThinkFilter("hide")
    ok = (plain.feed("no tags at all, just a normal sentence") + plain.flush()
          ) == "no tags at all, just a normal sentence"
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} passthrough")
    ok = strip_think("<think>abc</think>  결과") == "결과"
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} strip_think")

    # Qwen3.5 style: untagged monologue, answer recovered on flush
    monologue = ThinkFilter("hide")
    shown = "".join(monologue.feed(p) for p in (
        "Thinking Pro", "cess:\n\n1.  **Analyze the Request:**\n",
        "    *   Topic: 돌스냅\n    *   Language: Korean\n\n",
        "2.  **Draft:**\n    *   short answer\n\n",
        "**Final Answer**\n돌스냅은 아기 돌잔치 기념 사진 촬영입니다."))
    ok = shown == "" and monologue.thinking and monologue.had_untagged_reasoning
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} untagged monologue suppressed while streaming")
    answer = monologue.flush()
    ok = answer == "돌스냅은 아기 돌잔치 기념 사진 촬영입니다."
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} answer recovered -> {answer!r}")

    # No recoverable answer -> keep everything rather than show nothing
    lossy = ThinkFilter("hide")
    lossy.feed("Thinking Process:\n1. step one\n2. step two")
    recovered = lossy.flush()
    ok = "step one" in recovered
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} unrecoverable monologue is not silently dropped")

    ok = strip_reasoning_preamble("일반 답변입니다.") == "일반 답변입니다."
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} normal answers untouched by preamble stripper")

    print("\n--- LLM output validation ---")
    checks = [
        # (text, llm payload, expected repeat, note)
        ("다음주 수요일 오후 2시 반에 치과",
         {"is_schedule": True, "title": "치과", "target_time": "2026-08-17T14:30:00",
          "repeat_type": "weekly", "repeat_detail": "수"}, "none", "invented repeat dropped"),
        ("매주 금요일 6시 회식",
         {"is_schedule": True, "title": "회식", "target_time": "2026-08-14 18:00:00",
          "repeat_type": "weekly", "repeat_detail": "garbage"}, "weekly", "detail repaired"),
        ("회의",
         {"is_schedule": True, "title": "회의", "target_time": "2020-01-01 09:00:00",
          "repeat_type": "none", "repeat_detail": ""}, "none", "far past rejected"),
    ]
    for text, payload, expect_repeat, note in checks:
        heur = parser.parse(text, now)
        res = validate_llm_result(ParseResult.from_dict(payload, text), text, now, heur)
        ok = res.repeat_type == expect_repeat
        if note.startswith("far past"):
            ok = ok and not res.is_schedule and res.error == "past_time"
        else:
            ok = ok and res.needs_confirm and res.target_time > now
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {note:26} -> repeat={res.repeat_type} "
              f"time={res.target_time} confirm={res.needs_confirm} err={res.error!r}")

    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('<think>hmm</think>{"b": 2}') == {"b": 2}
    assert extract_json_object("nope") is None

    # --- where the weights live -------------------------------------------- #
    # The model must resolve out of the *data* folder, so replacing the
    # program folder to upgrade never costs a 1 GB re-copy.
    import tempfile as _tempfile
    _saved_data_dir = _DATA_DIR
    try:
        data_root = _tempfile.mkdtemp(prefix="hud_models_data_")
        prog_root = os.path.join(data_root, "_program")
        os.makedirs(os.path.join(prog_root, "models"), exist_ok=True)
        set_data_dir(data_root)

        created = ensure_models_dir()
        if not os.path.isdir(created):
            failures += 1
            print("FAIL 데이터 폴더에 models/ 가 안 만들어짐")
        if not os.listdir(created):
            failures += 1
            print("FAIL models/ 안내 파일이 없음")

        data_gguf = os.path.join(created, "data-side.gguf")
        with open(data_gguf, "wb") as fh:
            fh.write(b"0")
        if is_volatile_model(data_gguf):
            failures += 1
            print("FAIL 데이터 폴더 모델을 휘발성으로 판단함")
        # A path outside both folders (a user-pinned absolute path) is not
        # ours to warn about either.
        if is_volatile_model(os.path.join(data_root, "elsewhere.gguf")):
            failures += 1
            print("FAIL 데이터 폴더 밖 임의 경로를 휘발성으로 판단함")
        # ...but one sitting beside the exe is exactly the case to flag.
        _real_app_dir = globals()["app_dir"]
        globals()["app_dir"] = lambda: prog_root
        try:
            prog_gguf = os.path.join(prog_root, "models", "prog-side.gguf")
            with open(prog_gguf, "wb") as fh:
                fh.write(b"0")
            if not is_volatile_model(prog_gguf):
                failures += 1
                print("FAIL 프로그램 폴더 모델을 경고하지 않음")
            # Both present -> the data-folder copy must win.
            chosen = find_model_path("")
            if chosen is None or os.path.abspath(chosen) != os.path.abspath(data_gguf):
                failures += 1
                print(f"FAIL 데이터 폴더 모델이 우선되지 않음: {chosen}")
        finally:
            globals()["app_dir"] = _real_app_dir
    finally:
        set_data_dir(_saved_data_dir or "")

    # --- reasoning that arrives without its opening tag ---------------------- #
    # Reported from real use with Qwen3.8-4B. The chat template writes
    # "<think>" into the *prompt*, so the completion is reasoning followed by a
    # bare "</think>" -- the filter waited for an OPEN that never came and put
    # the monologue on screen.
    REAL = ("사용자가 AI 비서의 능력을 묻는 질문입니다. 역할 설정에 따라 업무 보조, "
            "일정 관리, 정보 제공 등의 기능을 안내해야 합니다.\n</think>\n\n"
            "- 업무 관련 문의나 요청을 도와드릴 수 있습니다.\n"
            "- 일정 입력 및 알림 기능도 지원합니다.")
    REAL_EN = ("The user is asking for help writing an email in English. I should "
               "provide a polite and concise response.\n</think>\n\n"
               "- Of course! What would you like to write?")

    for label, raw in (("ko", REAL), ("en", REAL_EN)):
        for chunk_size in (len(raw), 7, 1):        # whole, chunked, token-wise
            f = ThinkFilter("hide")
            for i in range(0, len(raw), chunk_size):
                f.feed(raw[i:i + chunk_size])
            f.flush()
            shown = f.emitted.strip()
            if "</think>" in shown or "사용자가" in shown or "The user is" in shown:
                failures += 1
                print(f"FAIL 사고 과정이 새어나감 ({label}, chunk={chunk_size}): "
                      f"{shown[:70]!r}")
            if "도와드릴 수 있습니다" not in shown and "Of course" not in shown:
                failures += 1
                print(f"FAIL 실제 답변이 사라짐 ({label}, chunk={chunk_size}): {shown[:70]!r}")

    # A normal answer with no reasoning at all must pass through untouched.
    plain = "내일 오전 10시로 등록했습니다. 다른 도움이 필요하시면 말씀해 주세요."
    f = ThinkFilter("hide")
    for ch in plain:
        f.feed(ch)
    f.flush()
    if f.emitted.strip() != plain:
        failures += 1
        print(f"FAIL 평범한 답변이 변형됨: {f.emitted.strip()[:70]!r}")

    # Properly tagged reasoning still works.
    tagged = "<think>hmm, let me see</think>답변입니다."
    f = ThinkFilter("hide")
    f.feed(tagged)
    f.flush()
    if f.emitted.strip() != "답변입니다.":
        failures += 1
        print(f"FAIL 태그된 사고 과정 처리 실패: {f.emitted.strip()!r}")

    # mode='show' keeps everything, including the tags.
    f = ThinkFilter("show")
    if f.feed(REAL) != REAL:
        failures += 1
        print("FAIL show 모드가 내용을 바꿈")

    info = backend_info()
    print(f"\nbackend: llama_cpp={info['available']} v{info['version'] or '-'} "
          f"model={(info['model'] or {}).get('name', '(none)')}")
    print("llm_engine self-test:", "OK" if failures == 0 else f"{failures} FAILURE(S)")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    _selftest()
