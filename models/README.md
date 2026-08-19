# models/

Put your GGUF model here. **Nothing in this folder is bundled into the
executable** — it is loaded at runtime from the relative path
`./models/<file>.gguf`, so you can swap models without rebuilding.

## Which file is used

When several `*.gguf` files are present the **most recently added** one wins.
The selected filename is always shown under the chat tab. To pin a specific
one, use 설정 → AI → GGUF 파일 선택.

## Speed on this machine (CPU)

| Model | Size | Feel |
|---|---|---|
| Qwen3-1.7B-Q4_K_M | 1.3 GB | fast (a few seconds), honours `/no_think` |
| Qwen3.5-2B-Q4_K_M | 1.4 GB | usable |
| Qwen3.5-4B-Q4_K_M | 3.0 GB | ~2 min/answer, and reasons out loud regardless |

A one-time warning appears when a model ≥ 2 GB is loaded.

## Running with this folder empty

The app is fully usable without any model:

| Feature | Without a model |
|---|---|
| Natural-language schedule entry | ✅ built-in offline parser (regex, Korean + English) |
| Chat commands (add / list / clear / delete, mail rules) | ✅ rule-based, no model needed |
| Recurrence, alarms, notifications, Outlook mail watch | ✅ unaffected |
| Free-form AI conversation | ❌ disabled, with an on-screen explanation |

The status dot in the title bar shows the model state:
○ missing · ◌ loading · ● ready (green) · ● error (red).

> A built copy of `Qwen3-1.7B-Q4_K_M.gguf` also lives in the packaged app at
> `dist/OfflineSmartHUD/models/`. Copy it back here if you want the source
> checkout to run chat too.
