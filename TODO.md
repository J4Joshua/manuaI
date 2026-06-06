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
2. ☐ **Phase 1.5 refactor** — `core.answer(question, machine, retriever) → screen_state`; `retriever.py` holds **both** `CosineRetriever` (stub) and `MossRetriever` behind one `search()` seam; `render.py` (terminal); `ask.py` becomes a thin CLI. Unify the system prompt (with the few-shot task-match example). *Check: the beats still pass on the stub.*
3. ☐ **`test_beats.py`** — regression over the canonical beats (jam→answer+cite; bypass→escalate; servo→escalate; cobot→answer+cite). *Check: all pass; run after every corpus/threshold change.*
4. ☐ **Real-corpus stub** — `ingest_local.py` builds `index.json` from `data/machines/*/sops/*.md` via local nomic (same chunker as Moss). *Check: beats pass on the real corpus, wifi-offable.*
5. ☐ **Unify Moss through `core.answer`** — `RETRIEVER=stub|moss` switch so both paths run the same loop. *Check: Moss beats pass via core.*
6. ☐ **Phase 2 screen** — `server.py` (stdlib http.server + SSE) + `screen.html` rendering `screen_state` (transcript · answer · steps · citation · ⚠ safety · escalation). *Check: server serves the page and streams a screen_state; typed-input box (the R2 fallback, gap G3).*
7. ☐ **Scaffold Phase 3** — `agent.py` (LiveKit: push-to-talk → STT → `core.answer` → TTS + data-channel push). Code + run-notes; not hardware-tested.
8. ☐ **Scaffold Phase 4** — `unsiloed_ingest.py` (PDF → Unsiloed Parse/Extract → chunk → Moss). Code + schema mapping; not run (needs API key).
9. ☐ Update `ARCHITECTURE.md` / `phases/` statuses + this TODO as items land.

## 🔒 Needs you (when you wake)
- 🔒 **Moss office-hours** (4pm): offline cold-load/persist in Python + token-expiry — protects the wifi-off demo (`ARCHITECTURE.md §12e`)
- 🔒 **LiveKit**: `pip install -r requirements.txt`, run `livekit-server --dev`, run `agent.py`, test push-to-talk + TTS on a real mic; **verify a wifi-off round-trip** (gap G1)
- 🔒 **Pre-pull + verify offline**: `mlx-whisper` (whisper-small), `kokoro-onnx`, `mlx-lm`; set `HF_HUB_OFFLINE=1` (gap G6)
- 🔒 **Unsiloed API key** in `.env` → run Phase 4 ingest on the real PDFs
- 🔒 **Rehearse the Moss wifi-off sequence** with `scripts/moss_offline_test.py` on the demo box (load online → keep process alive → wifi off)
- 🔒 **Record the backup wifi-off video** (stub path = bulletproof offline)
- 🔒 **Harden**: corpus to ~5–10 SOPs, re-tune, 5× dry-run, freeze (Phase 5)

## Progress log
- (commits will appear here / in `git log` as steps land)
