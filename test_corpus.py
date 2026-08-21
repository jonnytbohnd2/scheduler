# -*- coding: utf-8 -*-
"""~1000 phrases a Korean reinsurance office would actually type.

Why this file exists
--------------------
Every parser bug that reached the user came from a sentence I had not
imagined: ``8/24``, ``1pm on August 27th``, ``1/2 시무식``, ``다음주 월요일``.
My hand-written cases kept testing my own assumptions back at me. So the bulk
of this corpus is *generated* from component lists -- date expressions, clock
expressions, real task titles -- recombined by templates.

The ground truth is independent of the parser. A case is built from parts whose
meaning is known by construction ("8/24" comes from a generator that knows it
means 24 August), and the expectation is computed with plain calendar
arithmetic here in this file. Nothing asks ``llm_engine`` what the right answer
is, so a bug in the parser cannot make the test agree with it.

``NOW`` is a **Friday** on purpose: business-day maths, "다음주", and "the
coming <weekday>" all behave differently at the end of a week, and a
Monday-based version of this corpus missed a bug that shipped.

Run::

    py test_corpus.py           # summary + failures
    py test_corpus.py -v        # every case
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("OFFLINESMARTHUD_DATA", tempfile.mkdtemp(prefix="hud_corpus_"))

from llm_engine import (TOOL_ADD, TOOL_ADD_EMAIL, TOOL_CLEAR, TOOL_DELETE,
                        TOOL_DELETE_EMAIL, TOOL_LIST, TOOL_LIST_EMAIL,
                        TOOL_NONE, TOOL_REPORT, HeuristicParser,
                        detect_tool_intent)

NOW = datetime(2026, 8, 21, 10, 0)          # Friday
TODAY = NOW.date()
P = HeuristicParser()


# --------------------------------------------------------------------------- #
# Independent calendar arithmetic (deliberately not shared with the parser)
# --------------------------------------------------------------------------- #

def md(month: int, day: int) -> date:
    """The next occurrence of a month/day, counting today."""
    candidate = date(TODAY.year, month, day)
    return candidate if candidate >= TODAY else date(TODAY.year + 1, month, day)


def dom(day: int) -> date:
    """A bare day-of-month: this month if still ahead, else next month."""
    if day >= TODAY.day:
        return date(TODAY.year, TODAY.month, day)
    month, year = TODAY.month + 1, TODAY.year
    if month > 12:
        month, year = 1, year + 1
    return date(year, month, day)


def coming(weekday: int) -> date:
    """The next occurrence of a weekday, today counting as itself."""
    return TODAY + timedelta(days=(weekday - TODAY.weekday()) % 7)


def week_of(weekday: int, offset_weeks: int) -> date:
    """Monday-based calendar week: 'this week's Tuesday', 'next week's Monday'."""
    start = TODAY - timedelta(days=TODAY.weekday()) + timedelta(weeks=offset_weeks)
    target = start + timedelta(days=weekday)
    return target + timedelta(days=7) if target < TODAY else target


def plus_business(n: int) -> date:
    """Weekday-only arithmetic.

    The window used below carries no public holiday (Korea's next one after
    2026-08-17 is 추석 on 09-24), so this stays an independent check rather
    than a second copy of holidays.py.
    """
    candidate, remaining = TODAY, n
    while remaining > 0:
        candidate += timedelta(days=1)
        if candidate.weekday() < 5:
            remaining -= 1
    return candidate


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #

DATES: list[tuple[str, date]] = [
    ("오늘", TODAY), ("금일", TODAY),
    ("내일", TODAY + timedelta(days=1)), ("명일", TODAY + timedelta(days=1)),
    ("낼", TODAY + timedelta(days=1)),
    ("모레", TODAY + timedelta(days=2)), ("내일모레", TODAY + timedelta(days=2)),
    ("낼모레", TODAY + timedelta(days=2)), ("글피", TODAY + timedelta(days=3)),
    # numeric shorthands
    ("8/24", md(8, 24)), ("08/24", md(8, 24)), ("8.24", md(8, 24)),
    ("9/1", md(9, 1)), ("12/31", md(12, 31)), ("1/2", md(1, 2)),
    ("8월 24일", md(8, 24)), ("8월24일", md(8, 24)), ("9월 9일", md(9, 9)),
    ("2026-08-24", date(2026, 8, 24)), ("2026.08.24", date(2026, 8, 24)),
    ("2026/08/24", date(2026, 8, 24)),
    ("24일", dom(24)), ("28일", dom(28)),
    # weekdays
    ("월요일", coming(0)), ("화요일", coming(1)), ("수요일", coming(2)),
    ("목요일", coming(3)), ("금요일", coming(4)),
    ("다음주 월요일", week_of(0, 1)), ("다음주 화요일", week_of(1, 1)),
    ("다음주 수요일", week_of(2, 1)), ("다음주 목요일", week_of(3, 1)),
    ("다음주 금요일", week_of(4, 1)),
    ("담주 월요일", week_of(0, 1)), ("차주 수요일", week_of(2, 1)),
    ("이번주 월요일", week_of(0, 0)), ("이번주 금요일", week_of(4, 0)),
    # offsets
    ("3일 뒤", TODAY + timedelta(days=3)), ("5일 후", TODAY + timedelta(days=5)),
    ("10일 뒤", TODAY + timedelta(days=10)),
    ("1주 뒤", TODAY + timedelta(days=7)), ("2주 뒤", TODAY + timedelta(days=14)),
    # business days -- how deadlines are actually quoted here
    ("1영업일 뒤", plus_business(1)), ("2영업일 뒤", plus_business(2)),
    ("3영업일 뒤", plus_business(3)), ("5영업일 뒤", plus_business(5)),
    ("다음 영업일", plus_business(1)),
    # English
    ("August 27th", md(8, 27)), ("Aug 27", md(8, 27)), ("27 August", md(8, 27)),
    ("Sep 3rd", md(9, 3)), ("Sept 3", md(9, 3)), ("September 3", md(9, 3)),
    ("Oct 1", md(10, 1)), ("Nov 11", md(11, 11)), ("Dec 1", md(12, 1)),
    ("Dec 1, 2026", date(2026, 12, 1)), ("Jan 5", md(1, 5)),
    ("tomorrow", TODAY + timedelta(days=1)),
]

TIMES: list[tuple[str, tuple[int, int]]] = [
    ("오전 9시", (9, 0)), ("오전 11시", (11, 0)), ("오후 2시", (14, 0)),
    ("오후 5시", (17, 0)), ("아침 8시", (8, 0)), ("저녁 7시", (19, 0)),
    ("밤 9시", (21, 0)), ("새벽 6시", (6, 0)),
    ("9시", (9, 0)), ("10시", (10, 0)), ("11시", (11, 0)),
    # 1~6시 with no meridiem reads as afternoon -- the documented rule
    ("2시", (14, 0)), ("3시", (15, 0)), ("5시", (17, 0)),
    ("3시 반", (15, 30)), ("3시반", (15, 30)), ("오전 9시 30분", (9, 30)),
    ("14:30", (14, 30)), ("09:00", (9, 0)), ("16:45", (16, 45)),
    ("2pm", (14, 0)), ("9am", (9, 0)), ("10:30am", (10, 30)),
]

TITLES = [
    "경영전략 엑셀 제출", "특약OS이월", "결재 상신", "요율 검토 회의",
    "TCPL KYC 서류 확보", "프론팅계약 검토", "BDX 정산", "재보험 계약 갱신",
    "손해율 분석", "주간 팀미팅", "분기 실적 보고", "사업계획 초안 검토",
    "아레나 계산서 처리", "원수사 미팅", "재무제표 검토", "내부통제 점검",
    "감사 자료 준비", "계리 검증", "지급여력비율 산출", "재보험료 정산",
    "시무식", "종무식", "시행 계획 수립", "시장 동향 조사",
    "broker call", "renewal meeting", "treaty renewal", "XOL placement call",
    "quarterly close", "JB BODA 미팅",
    # titles carrying digits, codes and punctuation -- these are where a
    # date/clock matcher is most likely to bite into the task name
    "2026년 1분기 실적 보고", "XOL 100억 담보 검토", "A-1 등급 심사",
    "3층 회의실 예약", "Q3 마감 점검", "IFRS17 대응 회의", "2차 검토 회의",
    "5개년 계획 수립", "1:1 면담", "K-ICS 비율 점검",
]

VERBS = ["", " 추가해줘", " 등록", " 등록해줘", " 잡아줘", " 넣어줘"]

Case = tuple


# --------------------------------------------------------------------------- #
# Generated cases
# --------------------------------------------------------------------------- #

def generated() -> list[Case]:
    cases: list[Case] = []
    n_dates, n_times, n_titles = len(DATES), len(TIMES), len(TITLES)
    n_verbs = len(VERBS)

    # 1. date + clock + title, rotating all three with co-prime strides so no
    #    component is only ever seen next to the same partner.
    for i in range(n_dates * 7):
        d_text, d_val = DATES[i % n_dates]
        t_text, (hh, mm) = TIMES[(i * 7) % n_times]
        title = TITLES[(i * 11) % n_titles]
        verb = VERBS[i % n_verbs]
        cases.append((f"{d_text} {t_text} {title}{verb}", TOOL_ADD, title,
                      f"{d_val.strftime('%m/%d')} {hh:02d}:{mm:02d}"))

    # 1b. clock before date -- "오후 2시 8/24 회의" is written that way too
    for i in range(n_dates * 2):
        d_text, d_val = DATES[i % n_dates]
        t_text, (hh, mm) = TIMES[(i * 5) % n_times]
        title = TITLES[(i * 13) % n_titles]
        cases.append((f"{t_text} {d_text} {title}", TOOL_ADD, title,
                      f"{d_val.strftime('%m/%d')} {hh:02d}:{mm:02d}"))

    # 2. date + title, no clock -> the 09:00 default
    for i, (d_text, d_val) in enumerate(DATES):
        for step in (0, 7, 13):
            title = TITLES[(i + step) % n_titles]
            verb = VERBS[(i + step) % n_verbs]
            cases.append((f"{d_text} {title}{verb}", TOOL_ADD, title,
                          f"{d_val.strftime('%m/%d')} 09:00"))

    # 3. title first, date after -- people write it both ways round
    for i, (d_text, d_val) in enumerate(DATES):
        t_text, (hh, mm) = TIMES[(i * 3) % n_times]
        title = TITLES[(i * 5) % n_titles]
        cases.append((f"{title} {d_text} {t_text} 추가해줘", TOOL_ADD, title,
                      f"{d_val.strftime('%m/%d')} {hh:02d}:{mm:02d}"))

    # 4. every clock form against two fixed dates, so a time bug cannot hide
    #    behind a date that happens to work
    for i, (t_text, (hh, mm)) in enumerate(TIMES):
        title = TITLES[i % n_titles]
        cases.append((f"8/24 {t_text} {title}", TOOL_ADD, title,
                      f"{md(8, 24).strftime('%m/%d')} {hh:02d}:{mm:02d}"))
        cases.append((f"내일 {t_text} {title}", TOOL_ADD, title,
                      f"{(TODAY + timedelta(days=1)).strftime('%m/%d')} "
                      f"{hh:02d}:{mm:02d}"))

    # 5. every date form against a fixed clock, likewise
    for i, (d_text, d_val) in enumerate(DATES):
        title = TITLES[(i + 3) % n_titles]
        cases.append((f"{d_text} 오후 2시 {title}", TOOL_ADD, title,
                      f"{d_val.strftime('%m/%d')} 14:00"))

    # 6. whitespace, punctuation and mail-subject noise around a good phrase
    noisy = [
        "8/24  {t}", "  8/24 {t}  ", "8/24\t{t}", "8/24 {t}!!", "8/24 {t}...",
        "8/24 [긴급] {t}", "8/24 ({t})", "RE: 8/24 {t}", "FW: 8/24 {t}",
        "8/24 {t} (팀장님 지시)", "8/24\n{t}", "★ 8/24 {t}", "- 8/24 {t}",
        "1. 8/24 {t}", "8/24 · {t}", "[공지] 8/24 {t}", "8/24 {t}~",
    ]
    for i, shape in enumerate(noisy):
        title = TITLES[i % n_titles]
        cases.append((shape.format(t=title), TOOL_ADD, None,
                      f"{md(8, 24).strftime('%m/%d')} 09:00"))

    # 7. recurrence. Dates for repeats are covered by db_manager's own tests,
    #    so only the title and the ADD verdict are pinned here.
    repeats = ["매일", "매주 월요일", "매주 화요일", "매주 수요일", "매주 목요일",
               "매주 금요일", "매주 화목", "매주 월수금", "매월 12일",
               "매월 25일", "매월 말일", "매달 1일", "격주 수요일",
               "매일 아침", "매주 마지막 금요일"]
    for i, rep in enumerate(repeats):
        for step in (0, 5, 11):
            title = TITLES[(i + step) % n_titles]
            t_text = TIMES[(i + step) % n_times][0]
            cases.append((f"{rep} {t_text} {title}", TOOL_ADD, title, None))

    return cases


# --------------------------------------------------------------------------- #
# Curated cases: real reports, and everything that must NOT book
# --------------------------------------------------------------------------- #

def curated() -> list[Case]:
    tomorrow = (TODAY + timedelta(days=1)).strftime("%m/%d")
    return [
        # ---- reported from real use --------------------------------------- #
        ("8/24 경영전략 엑셀 제출 / 8/31 ppt 1차 제출 일정 등록 2개 별도로",
         TOOL_ADD, "경영전략 엑셀 제출", f"{md(8, 24).strftime('%m/%d')} 09:00"),
        ("1pm on August 27th   JB BODA 미팅", TOOL_ADD, "JB BODA 미팅",
         f"{md(8, 27).strftime('%m/%d')} 13:00"),
        ("2026.7월 프론팅계약 bdx 8월18일 할일로 등록해줘",
         TOOL_ADD, "2026.7월 프론팅계약 bdx", None),
        ("김보성 db 카피 요청 3시간 후 알림 설정해줘", TOOL_ADD,
         "김보성 db 카피 요청", "08/21 13:00"),
        ("내일 아레나 계산서 처리 마무리하기 오전 11시", TOOL_ADD,
         "아레나 계산서 처리 마무리하기", f"{tomorrow} 11:00"),
        ("9월 9일까지 TCPL KYC 서류 확보", TOOL_ADD, "TCPL KYC 서류 확보",
         f"{md(9, 9).strftime('%m/%d')} 09:00"),
        ("1/2 시무식", TOOL_ADD, "시무식", f"{md(1, 2).strftime('%m/%d')} 09:00"),
        ("hands on training Sep 3rd 2pm", TOOL_ADD, "hands on training",
         f"{md(9, 3).strftime('%m/%d')} 14:00"),
        ("next monday 9am kickoff", TOOL_ADD, "kickoff",
         f"{week_of(0, 1).strftime('%m/%d')} 09:00"),
        ("meeting on Sep 3rd at 10am", TOOL_ADD, "meeting",
         f"{md(9, 3).strftime('%m/%d')} 10:00"),
        ("30분 뒤 콜백", TOOL_ADD, "콜백", "08/21 10:30"),
        ("2시간 뒤 자료 취합", TOOL_ADD, "자료 취합", "08/21 12:00"),
        ("8월 말일 정산 마감", TOOL_ADD, "정산 마감",
         f"{md(8, 31).strftime('%m/%d')} 09:00"),

        # ---- a date inside brackets, and 퇴근 전까지 ------------------------ #
        # Reported: the date was cut out of the parenthetical and the shell was
        # kept -- "…송부 요청( (월) 전 )". Brackets holding real content must
        # still survive.
        ("영업계수 마감 결과 송부 요청(8/24(월) 퇴근 전까지)", TOOL_ADD,
         "영업계수 마감 결과 송부 요청", f"{md(8, 24).strftime('%m/%d')} 18:00"),
        ("분기보고서 제출[8/24 까지]", TOOL_ADD, "분기보고서 제출",
         f"{md(8, 24).strftime('%m/%d')} 09:00"),
        ("8/24 퇴근 전까지 보고서 제출", TOOL_ADD, "보고서 제출",
         f"{md(8, 24).strftime('%m/%d')} 18:00"),
        ("요율 검토 (긴급) 8/25 10시", TOOL_ADD, "요율 검토 (긴급)",
         f"{md(8, 25).strftime('%m/%d')} 10:00"),
        ("계약 검토 (김과장) 내일 3시", TOOL_ADD, "계약 검토 (김과장)",
         f"{tomorrow} 15:00"),

        # ---- pasted email must never book ---------------------------------- #
        # A 652-character schedule was created from an email whose header said
        # "Sent: Monday"; the reply the user asked for never happened.
        ("ok let's move on. write me a reponse to below email saying well "
         "received. From: Gosling, Thomas 01 <Thomas.Gosling01@marsh.com> "
         "Sent: Monday, To: sjhuh@koreanre.co.kr Subject: RE: Korean Re / "
         "Marsh Leading Edge Presentation Hi All Thanks once again for your "
         "time today. Best regards, Tom", TOOL_NONE, None, None),
        ("이 메일 답장 써줘\n보낸사람: 김영수 과장\n제목: 회의 일정 문의\n"
         "다음 주 화요일 오후 2시에 진행하려 합니다.", TOOL_NONE, None, None),

        # ---- multi-item ---------------------------------------------------- #
        ("8/24 엑셀 제출 / 8/31 ppt 제출", TOOL_ADD, "엑셀 제출",
         f"{md(8, 24).strftime('%m/%d')} 09:00"),
        ("8/24 엑셀 제출, 8/31 ppt 제출 추가", TOOL_ADD, "엑셀 제출",
         f"{md(8, 24).strftime('%m/%d')} 09:00"),
        ("내일 회의 그리고 모레 보고서 등록", TOOL_ADD, "회의", f"{tomorrow} 09:00"),

        # ---- listing -------------------------------------------------------- #
        *[(t, TOOL_LIST, None, None) for t in (
            "오늘 일정 알려줘", "내일 일정 뭐 있지", "이번주 일정 보여줘",
            "이번달 일정", "완료된 일정 보여줘", "남은 일정 몇 개야",
            "전체 일정 보여줘", "오늘 할일", "내일 스케줄", "일정 목록",
            "밀린 일정 알려줘", "이번주 할일 리스트")],

        # ---- reports --------------------------------------------------------- #
        *[(t, TOOL_REPORT, None, None) for t in (
            "이번주 한 일", "지난주 한 일 알려줘", "이번달 완료한 거",
            "주간보고 뽑아줘", "업무보고 정리해줘", "저번주 처리한 일",
            "이번주 끝낸 일", "월간보고")],
        # ...the same words with a clock or a repeat are a meeting, not a report
        ("매주 월요일 9시 주간보고", TOOL_ADD, "주간보고", None),
        ("내일 10시 업무보고 등록", TOOL_ADD, "업무보고", f"{tomorrow} 10:00"),

        # ---- housekeeping ---------------------------------------------------- #
        *[(t, TOOL_DELETE, None, None) for t in (
            "치과 예약 삭제해줘", "주간회의 지워줘", "요율 검토 회의 취소해줘",
            "특약OS이월 일정 삭제", "BDX 정산 없애줘")],
        *[(t, TOOL_CLEAR, None, None) for t in (
            "완료된 일정 정리해줘", "끝난 일정 다 지워줘")],

        # ---- awaited email ---------------------------------------------------- #
        *[(t, TOOL_ADD_EMAIL, None, None) for t in (
            "'특약OS이월' 메일 오면 '결재 시스템 승인' 리마인드해줘",
            "BDX 메일 오면 정산 자료 확인 리마인드해줘",
            "'요율표' 메일 오면 '검토 후 회신' 알려줘")],
        *[(t, TOOL_LIST_EMAIL, None, None) for t in (
            "메일 알림 목록 보여줘", "메일 규칙 보여줘", "기다리는 메일 뭐 있지")],
        ("특약 메일 알림 삭제해줘", TOOL_DELETE_EMAIL, None, None),

        # ---- must never become a schedule -------------------------------------- #
        *[(t, TOOL_NONE, None, None) for t in (
            # questions
            "내일 시간 괜찮을까?", "회의 언제가 좋을까?", "너가 할 수 있는게 뭐야?",
            "재보험이 뭐야?", "이거 어떻게 해?", "보고서 어떻게 쓰지",
            "특약이랑 임의재보험 차이가 뭐야?", "손해율 계산 어떻게 해?",
            "내일 날씨 어때?", "지금 몇 시야?", "이번주 바빠?",
            "출재랑 수재 차이 설명해줘", "이 계약 조건 어떻게 생각해?",
            # statements about the past
            "2시간 걸렸어", "3일 걸렸어", "어제 2시간 회의했어", "보고서 다 썼어",
            "어제 회의 어땠어?", "지난주에 제출했어", "방금 결재 올렸어",
            "오전에 미팅 끝냈어", "계산서 처리 완료했다", "메일 보냈어",
            "자료 받았어", "검토 마쳤습니다",
            # fractions, ratios, versions, quantities
            "3/4 정도만 끝냈어", "진행률이 2/3쯤 돼", "1.5시간 걸려",
            "손해율이 65.3% 나왔어", "3.5B 모델이 더 크네", "2/3 수준이야",
            "버전 2.1 배포됐어", "5.5억 규모야",
            # chit-chat
            "안녕", "고마워", "ㅇㅇ", "그래", "알겠어", "수고하셨습니다",
            "점심 뭐 먹지", "커피 마실래?", "퇴근하고 싶다", "화이팅",
            # single-syllable weekday traps
            "일 처리 좀 도와줘", "목이 아파서 병원 가야하나", "볼 수 있어?",
            "수가 없네", "금 시세 어떻게 됐어", "화가 나네", "할 수 있을까",
            # empty / bare
            "", "   ", "8/24", "내일", "오후 2시에",
            # invalid dates
            "13/1 잘못된 날짜", "8/32 잘못된 날짜", "0/0", "2/29 윤년 확인",
            # deliberately unsupported: a dash reads as a range at least as
            # often as a date, and guessing wrong books the wrong day silently
            "8-24 결재 상신",
        )],
    ]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def all_cases() -> list[Case]:
    seen: set[str] = set()
    cases: list[Case] = []
    for case in generated() + curated():
        if case[0] in seen:
            continue
        seen.add(case[0])
        cases.append(case)
    return cases


def run(verbose: bool = False) -> int:
    cases = all_cases()
    fails: list[tuple[str, str]] = []
    for text, tool, title, when in cases:
        try:
            intent = detect_tool_intent(text, P, NOW)
        except Exception as exc:                            # noqa: BLE001
            fails.append((text, f"예외 {type(exc).__name__}: {exc}"))
            continue
        if intent.tool != tool:
            fails.append((text, f"tool={intent.tool} (기대 {tool})"))
            continue
        if tool != TOOL_ADD:
            if verbose:
                print(f"  ok  {text!r} -> {tool}")
            continue
        s = intent.schedule
        if s is None:
            fails.append((text, "schedule 없음"))
            continue
        if title is not None and s.title != title:
            fails.append((text, f"title={s.title!r} (기대 {title!r})"))
            continue
        got = s.target_time.strftime("%m/%d %H:%M")
        if when is not None and got != when:
            fails.append((text, f"when={got} (기대 {when})"))
            continue
        if verbose:
            print(f"  ok  {text!r} -> {got} {s.title!r}")

    print(f"\n{len(cases) - len(fails)}/{len(cases)} 통과")
    if fails:
        print()
        for text, why in fails:
            print(f"  [FAIL] {text!r}\n         {why}")
    return len(fails)


if __name__ == "__main__":
    sys.exit(1 if run("-v" in sys.argv) else 0)
