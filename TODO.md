# ManuAI — TODO to a demo-ready product

**End goal:** a rehearsed, wifi-off, voice + screen demo for the labeler scenario, grounded in the real SOP corpus, with the cite-or-refuse beats reliable.

**How this runs:** worked top-down on branch `build/demo-mvp`, **one commit per verified step** (local only — not pushed; review with `git log --oneline` and merge to `main` when happy). `🔒 needs-you` items require hardware / API keys / decisions and are left for you.

Legend: ✅ done · ⏳ in progress · ☐ todo · 🔒 needs you

---

## Foundation (done before tonight)
- ✅ M1 offline loop (stub): retrieve → threshold gate → Qwen JSON → cite-or-refuse — validated (3 beats + paraphrase)
- ✅ Moss de-risked: smoke test PASS; real corpus indexed (21 chunks); 4 demo queries pass. Findings G7/G14/G15 in `ARCHITECTURE.md §12`
- ✅ Planning: `PRD.md`, `ARCHITECTURE.md` (contracts + 15-gap register), `phases/`

## Tonight — autonomous (verifiable; commit each)
1. ☐ **Baseline commit** of all current work on `build/demo-mvp`
2. ✅ **Phase 1.5 refactor** — `core.answer(question, machine, retriever) → screen_state`; `retriever.py` holds **both** `CosineRetriever` (stub) and `MossRetriever` behind one `search()` seam; `render.py` (terminal); `ask.py` becomes a thin CLI. Unify the system prompt (with the few-shot task-match example). *Check: the beats still pass on the stub.*
3. ✅ **`test_beats.py`** — regression over the canonical beats (jam→answer+cite; bypass→escalate; servo→escalate; cobot→answer+cite). *Check: all pass; run after every corpus/threshold change.*
4. ✅ **Real-corpus stub** — `ingest_local.py` builds `index.json` from `data/machines/*/sops/*.md` via local nomic (same chunker as Moss). *Check: beats pass on the real corpus, wifi-offable.*
5. ✅ **Unify Moss through `core.answer`** — `RETRIEVER=stub|moss` switch so both paths run the same loop. *Check: Moss beats pass via core.*
6. ✅ **Phase 2 screen** — `server.py` (stdlib http.server + SSE) + `screen.html` rendering `screen_state` (transcript · answer · steps · citation · ⚠ safety · escalation). *Check: server serves the page and streams a screen_state; typed-input box (the R2 fallback, gap G3).*
7. ✅ **Phase 3 voice WIRED** *(deps + livekit-server 1.12 installed; voice_smoke PASS; agent.py worker registers; live mic test = yours — see 🔒)* — `agent.py` (LiveKit: push-to-talk → STT → `core.answer` → TTS + data-channel push). Code + run-notes; not hardware-tested.
8. ✅ **Scaffold Phase 4** *(code written + syntax-checked; needs Unsiloed API key to run — see 🔒)* — `unsiloed_ingest.py` (PDF → Unsiloed Parse/Extract → chunk → Moss). Code + schema mapping; not run (needs API key).
9. ✅ Update `ARCHITECTURE.md` (§13 build status + gap deltas) + this TODO as items land.

## 🔒 Needs you (when you wake)
- 🔒 **Moss office-hours** (4pm): offline cold-load/persist in Python + token-expiry — protects the wifi-off demo (`ARCHITECTURE.md §12e`)
- 🔒 **Voice — LIVE MIC TEST only** (pipeline built + verified mic-free): deps + `livekit-server` 1.12 installed; `voice_smoke.py` PASS (TTS→STT→core→TTS); `agent.py` worker registers. **You do:** `livekit-server --dev` + `.venv/bin/python agent.py dev` + connect `screen.html` / a token to room `manuai` → hold push-to-talk, speak, release; then **redo with wifi OFF** (closes G1). First press garbled → tune `commit_user_turn` flush / VAD silence (see `agent.py`).
- 🔒 **Pre-pull + verify offline**: Whisper-small-mlx + Kokoro + Silero weights are DOWNLOADED (in `models/` + HF cache); still set `HF_HUB_OFFLINE=1` on the demo box and confirm a wifi-off `voice_smoke.py` run (gap G6)
- 🔒 **Unsiloed API key** in `.env` → run Phase 4 ingest on the real PDFs
- 🔒 **Rehearse the Moss wifi-off sequence** with `scripts/moss_offline_test.py` on the demo box (load online → keep process alive → wifi off)
- 🔒 **Record the backup wifi-off video** (stub path = bulletproof offline)
- 🔒 **Harden**: corpus to ~5–10 SOPs, re-tune, 5× dry-run, freeze (Phase 5)

## Progress log
- `66bdb7b` baseline: M1 stub + Moss integration + planning docs
- Phase 1.5 refactor (items 2/4/5): `core.answer → screen_state` over the Retriever seam
  (`CosineRetriever` stub gate 0.70 + `MossRetriever` gate None), `render.py`, thin `ask.py`,
  shared `corpus.py` chunker, `ingest_local.py`. Verified: 4 stub beats (ANSWERED/ESCALATED/
  ESCALATED/ANSWERED) + Moss path answers & cites SOP-1187. On the real 21-chunk corpus the
  stub gate now catches BOTH bypass (0.645) and servo (0.680) deterministically.
- `b659466` test_beats.py regression gate (item 3) — all 4 beats PASS.
- Phase 2 screen (item 6): `server.py` (stdlib, /state + /ask + typed-input R2 fallback,
  inline-no-CDN) + `screen.html` (single applyState renderer). Verified: /ask jam→answered
  +SOP-1187, bypass→escalated, / serves HTML.
- Phase 3/4 scaffolds (items 7,8): `agent.py` + `unsiloed_ingest.py` written, syntax OK.
- Phase 3 voice WIRED + verified mic-free: installed deps + livekit-server 1.12; `voice_smoke.py`
  (Kokoro TTS→mlx-whisper STT→core.answer→TTS) PASS (jam→answered+SOP-1187, bypass→escalated);
  `agent.py` rebuilt vs real livekit-agents 1.5.17 (in-process custom STT/TTS, core.answer via
  llm_node, push-to-talk RPC, screen_state over data channel) — `agent.py check` PASS + worker
  registers with livekit-server. Live mic round-trip = user's test. Fixed .env: WHISPER_MODEL
  needs `-mlx` suffix; empty HF_TOKEN breaks downloads (handled in code).
- Voice LIVE-verified (console): mic → STT → core.answer → spoken answer works. Fixed Whisper
  mis-detecting English as Chinese by pinning `language="en"` (was stored, never passed).
- Unified operator frontend (Phase 2↔3 integration): `operator.html` (reuses screen.html
  `applyState` byte-identical + hold-to-talk button + status pill + debug log) + bundled
  `static/livekit-client.umd.min.js` (2.19.1, no CDN) + `static/operator.js` (token → connect →
  push-to-talk RPC → play agent audio → render `screen_state` from the LiveKit data channel) +
  `server.py` routes `/operator.html` `/static` `/token`. Headless-verified (token+dispatch,
  serves, JS syntax, 17 API paths resolve, contract matches agent.py). Caught+fixed: `@rtc_session
  (agent_name)` disables auto-dispatch → `/token` now embeds a RoomConfiguration agent dispatch.
  **Live browser talk-test = user's.**
