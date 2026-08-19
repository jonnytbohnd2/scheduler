# 🪟 Offline Smart HUD

A frameless, glassmorphic desktop overlay that keeps today's schedule on screen
and puts a local LLM one tab away. **Everything runs on your machine** —
SQLite for storage, APScheduler for alarms, llama.cpp for inference. There is
no network code anywhere in this project, so it works unchanged on an
air-gapped PC.

```
┌─────────────────────────────────────────┐
│ ● Offline Smart HUD    16:11  🔓 📌 ─ ✕ │
├─────────────────────────────────────────┤
│ [ 📅 오늘의 일정 ] [ 🤖 AI 어시스턴트 ]  │
│ ┌─────────────────────────────┐ ┌─┐┌──┐ │
│ │ 매주 월요일 10시 주간 회의   │ │🗓││추가│ │
│ └─────────────────────────────┘ └─┘└──┘ │
│ 열린 일정 5건      다음: 주간 회의 · 42분 후 │
│ ○ 주간 팀 회의            [매주 (월)]   │
│   오늘 16:53  42분 6초 후          ✎ 🗑 │
│ ○ 분기 보고서 제출           [1회]      │
│   오늘 21:06  4시간 55분 후        ✎ 🗑 │
└─────────────────────────────────────────┘
```

---

## Features

**Glass overlay**
- Frameless, translucent, always-on-top; sits at the top-right corner by default
- Rests at ~45 % opacity and fades to ~97 % when you hover — configurable from
  20 % to 100 % via the right-click menu
- Drag by the title bar (or Ctrl+drag anywhere); 🔒 locks the position,
  📌 toggles always-on-top; both persist across restarts
- On an alarm: the border pulses with an accent glow, the panel jumps to full
  opacity, a chime plays and a notification card slides in

**Recurring to-dos**
- Ticking off a recurring item advances it to the next cycle instead of
  finishing it forever — `08/12 → 09/12`, with a toast saying so
- Works when completed early, and skips stale slots when long overdue
- A `↻` badge marks recurring rows at a glance

**Awaited email reminders (Outlook)**
- `'특약OS이월' 메일 오면 '결재 시스템 승인' 리마인드해줘` registers an inbox watch
- When the mail lands the HUD wakes, glows, chimes and shows the follow-up
  checklist attached to the rule
- Attach-only COM: it never launches Outlook, and reports
  `Outlook 미실행 (열면 자동 연결됩니다)` instead of failing
- Zero COM traffic when no rules are registered

**Chat tool calling**
- `매월 12일 특약OS이월 추가해줘` → writes to the DB, replies with a confirmation
- `내일 오전 10시 회의` → **no creation verb needed**; any phrase the heuristic
  calls *definite* is saved as a schedule instead of being answered with prose
- `오늘 일정 알려줘` / `이번주 할일` → formatted listing
- `완료된 일정 정리해줘`, `치과 예약 삭제해줘` → housekeeping
- `특약 메일 알림 삭제해줘` → removes an awaited-email rule
- Rule-based detection runs *before* generation, so actions are instant and
  cannot be hallucinated; anything else falls through to normal chat

The bare-phrase rule is applied **after** the query matchers, not before:
`오늘 일정 알려줘` and `내일 스케줄 뭐 있지?` are also "definite", and matching on
definiteness first would turn those questions into junk schedules named 일정.

Two guards keep the verb-less path from over-firing:

- **Single-syllable weekdays need context.** 수/일/목/금 are ordinary words
  (`할 수 있다`, `일 처리`, `목이 아파`). A bare syllable only reads as a weekday
  when it carries the full form (`수요일`), follows a week modifier (`매주 수`),
  sits in a list (`화,목`), or is immediately followed by a clock time (`수 10시`).
  Without this, `너가 할 수 있는게 뭐야?` booked a Wednesday meeting.
- **Questions never book.** Text ending in `?` or `~을까/인가요/나요` skips the
  fallback unless an explicit creation verb is present.

**📅 Schedule tab**
- Natural-language quick add — `매주 월요일 10시 주간 회의`,
  `내일 오후 3시 치과`, `30분 뒤 휴식`, `tomorrow 3pm dentist`
- Live countdowns that recolour as the deadline approaches
  (blue → amber under an hour → red once overdue)
- Recurrence chips: `1회` / `매일` / `매주 (월)` / `매월 (25일)`
- Check off, edit or delete inline; snooze or complete straight from the alarm card

**🤖 AI Assistant tab**
- ChatGPT-style bubbles with **real-time token streaming**
- Runs on a dedicated `QThread` — the UI never blocks, and a 중지 button
  cancels generation mid-sentence
- Conversation history is persisted and replayed as context

**Reliability**
- Single-instance lock (a second launch can't double your alarms)
- Rotating log at `hud.log`
- Graceful degradation at every layer: no model → offline parser; no
  QtMultimedia → system beep; no tray → window-only

---

## Architecture

| File | Responsibility |
|---|---|
| `main.py` | App bootstrap, frameless translucent window, drag/lock/opacity, tray icon, all signal wiring |
| `outlook_service.py` | Outlook COM inbox poller on its own thread (awaited-email rules) |
| `ui_components.py` | Custom widgets: glass panel, title bar, schedule rows, chat bubbles, notification card, manual dialog, generated icon & chime |
| `llm_engine.py` | Heuristic NL date parser + `llama-cpp-python` worker on a background `QThread` (chat streaming and JSON schedule extraction) |
| `db_manager.py` | SQLite CRUD for both databases, thread-local connections, recurrence maths |
| `scheduler_service.py` | APScheduler job polling the DB every 5 s, firing Qt signals |
| `build.py` | PyInstaller `--onedir` packaging |
| `models/` | Your GGUF file (never bundled into the exe) |

### Threads

```
GUI thread ─────────── widgets, animations, UI-initiated DB writes
LlmThread ──────────── llama.cpp load + token generation
APScheduler thread ─── 5-second alarm poll
OutlookThread ──────── 10-second inbox poll (COM apartment lives here)
```

`pythoncom.CoInitialize()` runs on `OutlookThread` itself — a COM apartment
belongs to the thread that initialises it, so doing it on the GUI thread would
force every call to marshal.

All cross-thread traffic is Qt signals (auto-queued), so no worker ever touches
a widget. Each thread gets its own SQLite connection via `threading.local()`.

### How schedule parsing works

```
user text
   │
   ├─► HeuristicParser (regex, ~0 ms)
   │
   │   any concrete date or clock token?          ("내일", "10시", "30분 뒤",
   │      │                                        "8월 15일", "다음주 수요일")
   │      ├─ YES ─────────────────────────────►  save immediately.  NO LLM.
   │      │
   │      └─ only a vague word ("점심") ───────►  confirm dialog, pre-filled
   │
   └─ nothing temporal at all
          ├─ model available ─► LLM JSON ─► cross-validate ─► confirm dialog
          └─ no model ────────────────────────►  confirm dialog, pre-filled
```

**The heuristic is authoritative.** Anything with a real date or time is
handled locally and deterministically — the LLM is never consulted, so it
cannot invent a time or a recurrence. Measured against Qwen3-1.7B on the same
inputs, the model got `다음주 수요일 오후 2시 반` and `담달 첫째주 금요일` wrong
in both date *and* recurrence, which is why it sits behind this gate.

The LLM only sees text with no temporal information at all, and even then its
answer is cross-validated (invented recurrence dropped, past/out-of-range times
rejected) and shown in the confirm dialog rather than saved.

### Database schema

`schedules.db`

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `title` | TEXT | |
| `target_time` | TEXT | `YYYY-MM-DD HH:MM:SS` |
| `repeat_type` | TEXT | `none` \| `daily` \| `weekly` \| `monthly` |
| `repeat_detail` | TEXT | weekly → `월`…`일`; monthly → day-of-month anchor |
| `notified` | INTEGER | 0 pending, 1 fired |
| `is_done` | INTEGER | 0 open, 1 completed |
| `created_at` | TEXT | |

`chat_history.db`

| column | type |
|---|---|
| `id` | INTEGER PK |
| `sender` | TEXT (`user` \| `ai`) |
| `message` | TEXT |
| `timestamp` | TEXT |

**Recurrence rule.** When `target_time <= now` and `notified = 0`, the service
fires the alarm and then: one-shot items get `notified = 1`; recurring items
get the next occurrence computed and `notified` stays `0`. Month-end is
clamped intelligently — a "31st of every month" schedule falls back to Feb 28
and returns to the 31st in March, because the anchor day is stored in
`repeat_detail` rather than derived from the last fire time.

---

## Setup

### 1. Dependencies (on a machine with internet)

```bash
py -m pip install PySide6 APScheduler

# Optional - needed only for the AI chat tab.
# There is no Windows wheel on PyPI, so use the maintainer's index:
py -m pip install llama-cpp-python \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

### 2. Model

Drop any instruct-tuned `*.gguf` into `models/`. When several are present the
**most recently added** one is used; pin a specific file in 설정 → AI.

CPU speed matters more than size here — measured on this machine:

| Model | Size | Feel |
|---|---|---|
| Qwen3-1.7B-Q4_K_M | 1.3 GB | fast (a few seconds), honours `/no_think` |
| Qwen3.5-2B-Q4_K_M | 1.4 GB | usable |
| Qwen3.5-4B-Q4_K_M | 3.0 GB | ~2 min/answer, and reasons out loud regardless |

A warning is shown once when a model ≥ 2 GB is loaded.

**The app runs fine without any model** — only the chat tab is disabled;
schedule entry is regex-based and unaffected.

### 3. Run

```bash
py main.py            # add --debug for verbose logging
```

---

## Building a portable app

```bash
py build.py                          # -> dist/OfflineSmartHUD/
py build.py --model path\to\X.gguf   # bundle one specific model
py build.py --with-model             # bundle every models/*.gguf
py build.py --console                # keep a console window for debugging
py build.py --outdir D:\out          # build off a OneDrive/Dropbox-synced folder
```

`--onedir` is deliberate: `--onefile` would unpack ~200 MB of Qt to a temp
folder on every launch and break the relative `./models` path.

Deploying to the air-gapped machine:

1. Copy the whole `dist/OfflineSmartHUD/` folder across
2. Drop the `.gguf` file into `OfflineSmartHUD/models/`
3. Run `OfflineSmartHUD.exe`

### Upgrading an existing install

The databases live **next to the executable**, so replacing the folder wholesale
destroys the user's schedules and mail rules. Every build ships an
`upgrade.ps1` that does it safely — run it from the *new* folder:

```powershell
.\upgrade.ps1 -Target "C:\path\to\installed\OfflineSmartHUD"
```

It stops the app, snapshots the data to `backups/pre-upgrade-<ts>/`, replaces
`OfflineSmartHUD.exe` and `_internal/` (deleting the old `_internal` first so
stale files from the previous version cannot linger), and leaves
`schedules.db`, `chat_history.db`, `config.json`, `logs/` and `models/` alone.

As a second line of defence the app snapshots both databases into
`backups/<date>/` on every start (10 days retained, `backup_on_start` in
config.json).

The build generates its own icon and notification chime from code, so there
are no binary art assets to ship.

---

## Self-tests

Each module is runnable on its own and has no external test dependencies:

```bash
py db_manager.py         # recurrence maths + CRUD round-trip
py llm_engine.py         # 13 natural-language parse cases (no model needed)
py scheduler_service.py  # live 6-second alarm/rollover test
py ui_components.py      # opens a widget gallery
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Chat answers are odd / repeat old nonsense | History is replayed as context — `Ctrl+Shift+C` purges it |
| Model invents facts about a term | Expected of a 1–4B model; use it for drafting/summarising, not lookups |
| Panel is invisible | It rests at 45 % opacity over a dark wallpaper — hover to wake it, or right-click → 기본 투명도 → 100 % |
| Panel opens on the wrong monitor | It snaps to the screen under the cursor on first run; drag it and the position is remembered |
| Chat tab disabled | No GGUF in `models/`, or `llama-cpp-python` isn't installed — the status line in the tab says which |
| `ERROR: No matching distribution found for llama-cpp-python` | PyPI has no Windows wheel; use the `--extra-index-url` above |
| No sound on alarm | QtMultimedia missing → falls back to the system beep. `assets/notify.wav` is generated on first use |
| "이미 실행 중입니다" | Single-instance lock. Check the tray icon; if the app crashed, delete `OfflineSmartHUD.lock` |
| Alarms didn't fire while asleep | On wake the service fires at most 5 stale alarms, then rolls the rest forward silently |

---

## Files created at runtime

```
schedules.db  chat_history.db  hud.log  assets/notify.wav  OfflineSmartHUD.lock
```

Window position, lock state, opacity and last tab are stored in
`HKCU\Software\OfflineSmartHUD` (Qt `QSettings`).
