# CLAUDE.md — ManuAI

Offline-first, **grounded** voice copilot for factory operators: an operator speaks a fault,
ManuAI speaks back the right SOP procedure and shows it on screen **cited to the exact SOP** —
or **refuses + escalates** if nothing approved matches. 100% local on Apple Silicon; the
headline is that it works **wifi-off**.

This file is a **map, not a manual** — open the right resource for the task:

| If you're… | Read / run |
|---|---|
| Setting up from a fresh clone | the **`first-setup`** skill |
| Developing — the loop, the verify gates, conventions, task recipes | the **`dev-setup`** skill |
| Needing the architecture, contracts, data flows, or the **gap register (G1–G16)** | **`docs/ARCHITECTURE.md`** — the source of truth |
| Working with a specific technology | the matching skill in `.claude/skills/` (`moss`, `ollama`, `qwen`, `mlx`, `livekit-agents`, `whisper-stt`, `local-tts`, `unsiloed`) |
| After the vision + the locked decisions (D1–D10) | `docs/PRD.md` (kept local) |
| Checking build status / what's left | `docs/TODO.md` · per-phase goals in `docs/phases/` |

## Watch-outs (the traps that cost hours — full detail in the ARCHITECTURE gap register)
- **WebRTC can't go offline** → the wifi-off path is `src/offline_demo.py` (no WebRTC); LiveKit / `operator.html` is the wifi-ON path only. *(G16)*
- **Moss is cloud-anchored** (`load_index` is a network call) → the **offline brain is the stub** (`CosineRetriever`), not Moss *(G14)*. Moss `.score` is per-query normalized, so the threshold gate is **stub-only**; on the Moss path, refusal comes from the task-match **few-shot in `src/core.py`** *(G15)*.
- **Whisper** mis-detects Chinese unless `language="en"` is pinned; `.env` `WHISPER_MODEL` needs the `-mlx` suffix; keep `HF_TOKEN` blank.
- The **`screen_state`** and **chunk-schema** contracts are shared by both retrievers and both UIs — **don't fork them** (ARCHITECTURE §3b).
- Verify with the gates the `dev-setup` skill lists; **don't push to `main` without explicit OK.**

Repo: `src/` code · `web/` UI · `data/` SOP corpus · `docs/` · `attic/` (superseded). Working branch: `build/demo-mvp`.
