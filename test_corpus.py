# -*- coding: utf-8 -*-
"""A corpus of things a Korean reinsurance office would actually type.

Written because the user kept finding breakage my own tests never covered:
"8/24", "1pm on August 27th", a fabricated "등록 완료". Those all came from
real use, not from my imagination -- so this file tries to imagine harder.

NOW is a Friday, deliberately: business-day and "next week" logic behaves
differently at the end of a week, and Friday is when the weekly report is due.
"""
import sys, io, os, tempfile
from datetime import datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["OFFLINESMARTHUD_DATA"] = tempfile.mkdtemp(prefix="hud_corpus_")

from llm_engine import (TOOL_ADD, TOOL_LIST, TOOL_NONE, TOOL_CLEAR, TOOL_DELETE,
                        TOOL_REPORT, TOOL_ADD_EMAIL, TOOL_LIST_EMAIL,
                        TOOL_DELETE_EMAIL, detect_tool_intent, HeuristicParser)

NOW = datetime(2026, 8, 21, 10, 0)          # Friday
P = HeuristicParser()

# (input, expected tool, expected title or None, expected "MM/DD HH:MM" or None)
# None means "do not check". Titles are exact; dates are exact.
CASES = [
    # ---------- 1. Korean date shorthand, the way it gets typed -------------
    ("8/24 경영전략 엑셀 제출", TOOL_ADD, "경영전략 엑셀 제출", "08/24 09:00"),
    ("8/24 14시 경영전략 엑셀 제출", TOOL_ADD, "경영전략 엑셀 제출", "08/24 14:00"),
    ("08/24 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    ("8월 24일 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    ("8.24 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    # "8-24" is left unsupported on purpose: a dash reads as a range
    # ("8-24일") at least as often as a date, and guessing wrong books
    # the wrong day silently.
    ("8-24 결재 상신", TOOL_NONE, None, None),
    ("2026-08-24 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    ("2026.08.24 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    ("24일 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    ("내일 결재 상신", TOOL_ADD, "결재 상신", "08/22 09:00"),
    ("다음주 월요일 결재 상신", TOOL_ADD, "결재 상신", "08/24 09:00"),
    ("이번주 금요일 마감", TOOL_ADD, "마감", "08/21 09:00"),
    ("말일 정산", TOOL_ADD, "정산", None),
    ("8월 말일 정산 마감", TOOL_ADD, "정산 마감", "08/31 09:00"),

    # ---------- 2. Times ----------------------------------------------------
    ("오늘 3시 팀 미팅", TOOL_ADD, "팀 미팅", "08/21 15:00"),
    ("오늘 오전 9시 반 조회", TOOL_ADD, "조회", "08/21 09:30"),
    ("오늘 14:30 요율 검토", TOOL_ADD, "요율 검토", "08/21 14:30"),
    ("오늘 저녁 7시 회식", TOOL_ADD, "회식", "08/21 19:00"),
    ("30분 뒤 콜백", TOOL_ADD, "콜백", "08/21 10:30"),
    ("2시간 뒤 자료 취합", TOOL_ADD, "자료 취합", "08/21 12:00"),
    ("오늘 정오 점심 약속", TOOL_ADD, "점심 약속", None),

    # ---------- 3. Business days (reinsurance speaks this way) --------------
    ("3영업일 뒤 서류 제출", TOOL_ADD, "서류 제출", "08/26 09:00"),
    ("1영업일 뒤 회신", TOOL_ADD, "회신", "08/24 09:00"),
    ("다음 영업일 결재 확인", TOOL_ADD, "결재 확인", "08/24 09:00"),
    ("5영업일 뒤 최종 보고", TOOL_ADD, "최종 보고", "08/28 09:00"),

    # ---------- 4. English / bilingual broker mail --------------------------
    ("1pm on August 27th   JB BODA 미팅", TOOL_ADD, "JB BODA 미팅", "08/27 13:00"),
    ("Aug 27 2pm broker call", TOOL_ADD, "broker call", "08/27 14:00"),
    ("27 August 3pm renewal meeting", TOOL_ADD, "renewal meeting", "08/27 15:00"),
    ("Dec 1, 2026 treaty renewal", TOOL_ADD, "treaty renewal", "12/01 09:00"),
    ("meeting on Sep 3rd at 10am", TOOL_ADD, "meeting", "09/03 10:00"),
    ("September 30 quarterly close", TOOL_ADD, "quarterly close", "09/30 09:00"),
    ("hands on training Sep 3rd 2pm", TOOL_ADD, "hands on training", "09/03 14:00"),
    ("tomorrow 3pm dentist", TOOL_ADD, "dentist", "08/22 15:00"),
    ("next monday 9am kickoff", TOOL_ADD, "kickoff", "08/24 09:00"),
    ("Oct 1 10:30 XOL placement call", TOOL_ADD, "XOL placement call", "10/01 10:30"),

    # ---------- 5. Recurrence ----------------------------------------------
    ("매월 12일 특약OS이월", TOOL_ADD, "특약OS이월", None),
    ("매주 월요일 9시 주간회의", TOOL_ADD, "주간회의", None),
    ("매주 화목 7시 헬스", TOOL_ADD, "헬스", None),
    ("매일 아침 8시 메일 확인", TOOL_ADD, "메일 확인", None),
    ("매월 말일 결산 마감", TOOL_ADD, "결산 마감", None),
    ("격주 수요일 3시 부서 미팅", TOOL_ADD, None, None),

    # ---------- 6. Multi-item ----------------------------------------------
    ("8/24 엑셀 제출 / 8/31 ppt 제출", TOOL_ADD, "엑셀 제출", "08/24 09:00"),
    ("8/24 엑셀 제출, 8/31 ppt 제출 추가", TOOL_ADD, "엑셀 제출", "08/24 09:00"),
    ("내일 회의 그리고 모레 보고서 등록", TOOL_ADD, "회의", "08/22 09:00"),

    # ---------- 7. Real titles from this office -----------------------------
    ("9월 9일까지 TCPL KYC 서류 확보", TOOL_ADD, "TCPL KYC 서류 확보", "09/09 09:00"),
    ("2026.7월 프론팅계약 bdx 8월18일 할일로 등록해줘", TOOL_ADD, "2026.7월 프론팅계약 bdx", None),
    ("김보성 db 카피 요청 3시간 후 알림 설정해줘", TOOL_ADD, "김보성 db 카피 요청", "08/21 13:00"),
    ("내일 아레나 계산서 처리 마무리하기 오전 11시", TOOL_ADD, "아레나 계산서 처리 마무리하기", "08/22 11:00"),
    ("8/25 10시 재보험 요율 검토 회의", TOOL_ADD, "재보험 요율 검토 회의", "08/25 10:00"),
    ("9/1 특약 갱신 자료 송부", TOOL_ADD, "특약 갱신 자료 송부", "09/01 09:00"),

    # ---------- 8. Queries, which must never create anything ----------------
    ("오늘 일정 알려줘", TOOL_LIST, None, None),
    ("내일 일정 뭐 있지", TOOL_LIST, None, None),
    ("이번주 일정 보여줘", TOOL_LIST, None, None),
    ("이번달 일정", TOOL_LIST, None, None),
    ("완료된 일정 보여줘", TOOL_LIST, None, None),
    ("남은 일정 몇 개야", TOOL_LIST, None, None),

    # ---------- 9. Reports --------------------------------------------------
    ("이번주 한 일", TOOL_REPORT, None, None),
    ("지난주 한 일 알려줘", TOOL_REPORT, None, None),
    ("이번달 완료한 거", TOOL_REPORT, None, None),
    ("주간보고 뽑아줘", TOOL_REPORT, None, None),
    ("업무보고 정리해줘", TOOL_REPORT, None, None),
    # ...but the same words as a meeting title are a schedule
    ("매주 월요일 9시 주간보고", TOOL_ADD, "주간보고", None),
    ("내일 10시 업무보고 등록", TOOL_ADD, "업무보고", "08/22 10:00"),

    # ---------- 10. Delete / clear -----------------------------------------
    ("치과 예약 삭제해줘", TOOL_DELETE, None, None),
    ("완료된 일정 정리해줘", TOOL_CLEAR, None, None),

    # ---------- 11. Awaited email ------------------------------------------
    ("'특약OS이월' 메일 오면 '결재 시스템 승인' 리마인드해줘", TOOL_ADD_EMAIL, None, None),
    ("메일 알림 목록 보여줘", TOOL_LIST_EMAIL, None, None),
    ("특약 메일 알림 삭제해줘", TOOL_DELETE_EMAIL, None, None),

    # ---------- 12. Must NOT become a schedule ------------------------------
    ("내일 시간 괜찮을까?", TOOL_NONE, None, None),
    ("회의 언제가 좋을까?", TOOL_NONE, None, None),
    ("너가 할 수 있는게 뭐야?", TOOL_NONE, None, None),
    ("3/4 정도만 끝냈어", TOOL_NONE, None, None),
    ("진행률이 2/3쯤 돼", TOOL_NONE, None, None),
    ("일 처리 좀 도와줘", TOOL_NONE, None, None),
    ("목이 아파서 병원 가야하나", TOOL_NONE, None, None),
    ("안녕", TOOL_NONE, None, None),
    ("고마워", TOOL_NONE, None, None),
    ("재보험이 뭐야?", TOOL_NONE, None, None),
    ("보고서 어떻게 쓰지", TOOL_NONE, None, None),
    ("이거 어떻게 해?", TOOL_NONE, None, None),
    ("2시간 걸렸어", TOOL_NONE, None, None),
    ("어제 회의 어땠어?", TOOL_NONE, None, None),

    # ---------- 13. Messy real input ---------------------------------------
    ("8/24  경영전략   엑셀   제출", TOOL_ADD, "경영전략 엑셀 제출", "08/24 09:00"),
    ("8/24 경영전략 엑셀 제출!!", TOOL_ADD, "경영전략 엑셀 제출", "08/24 09:00"),
    ("  8/24 경영전략 엑셀 제출  ", TOOL_ADD, "경영전략 엑셀 제출", "08/24 09:00"),
    ("8/24 [긴급] 경영전략 엑셀 제출", TOOL_ADD, None, "08/24 09:00"),
    ("8/24 경영전략 엑셀 제출 (팀장님 지시)", TOOL_ADD, None, "08/24 09:00"),
    ("RE: 8/24 경영전략 엑셀 제출", TOOL_ADD, None, "08/24 09:00"),
    ("8/24\n경영전략 엑셀 제출", TOOL_ADD, None, "08/24 09:00"),

    # ---------- 13b. Numbers and codes inside titles ------------------------
    ("8/24 2026년 1분기 실적 보고", TOOL_ADD, None, "08/24 09:00"),
    ("9/1 XOL 100억 담보 검토", TOOL_ADD, None, "09/01 09:00"),
    ("8/25 A-1 등급 심사", TOOL_ADD, None, "08/25 09:00"),
    ("8/26 3층 회의실 예약", TOOL_ADD, None, "08/26 09:00"),
    ("내일 Q3 마감 점검", TOOL_ADD, "Q3 마감 점검", "08/22 09:00"),

    # ---------- 13c. Words that merely start with a time syllable -----------
    # 시무식 / 시행 / 시장 all begin with 시; none is a clock reading. This is
    # what turned "1/2 시무식" into a 14:00 alarm titled "무식".
    ("8/24 시행 계획 수립", TOOL_ADD, "시행 계획 수립", "08/24 09:00"),
    ("8/24 시장 동향 조사", TOOL_ADD, "시장 동향 조사", "08/24 09:00"),
    ("3일 걸렸어", TOOL_NONE, None, None),
    ("어제 2시간 회의했어", TOOL_NONE, None, None),
    ("보고서 다 썼어", TOOL_NONE, None, None),

    # ---------- 13d. Deletion phrasings -------------------------------------
    ("주간회의 지워줘", TOOL_DELETE, None, None),

    # ---------- 13e. Awaited-email phrasings --------------------------------
    ("BDX 메일 오면 정산 자료 확인 리마인드해줘", TOOL_ADD_EMAIL, None, None),
    ("메일 규칙 보여줘", TOOL_LIST_EMAIL, None, None),

    # ---------- 14. Boundaries ---------------------------------------------
    ("12/31 종무식", TOOL_ADD, "종무식", "12/31 09:00"),
    ("1/2 시무식", TOOL_ADD, "시무식", "01/02 09:00"),      # rolls to next year
    # 2027 has no 29 February; rather than silently pick another day the
    # parser declines, and the quick-add box falls back to the date dialog.
    ("2/29 윤년 확인", TOOL_NONE, None, None),
    ("13/1 잘못된 날짜", TOOL_NONE, None, None),
    ("8/32 잘못된 날짜", TOOL_NONE, None, None),
    ("0/0", TOOL_NONE, None, None),
    ("", TOOL_NONE, None, None),
    ("   ", TOOL_NONE, None, None),
    ("8/24", TOOL_NONE, None, None),                        # a date with no task
]


def run() -> int:
    fails = []
    for text, tool, title, when in CASES:
        try:
            intent = detect_tool_intent(text, P, NOW)
        except Exception as exc:                            # noqa: BLE001
            fails.append((text, f"예외 {type(exc).__name__}: {exc}"))
            continue
        if intent.tool != tool:
            fails.append((text, f"tool={intent.tool} (기대 {tool})"))
            continue
        if tool != TOOL_ADD:
            continue
        s = intent.schedule
        if s is None:
            fails.append((text, "schedule 없음"))
            continue
        if title is not None and s.title != title:
            fails.append((text, f"title={s.title!r} (기대 {title!r})"))
            continue
        if when is not None and s.target_time.strftime("%m/%d %H:%M") != when:
            fails.append((text,
                          f"when={s.target_time.strftime('%m/%d %H:%M')} (기대 {when})"))
    print(f"\n{len(CASES) - len(fails)}/{len(CASES)} 통과\n")
    for text, why in fails:
        print(f"  [FAIL] {text!r}")
        print(f"         {why}")
    return len(fails)


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
