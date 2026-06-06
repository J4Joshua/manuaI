# CLAUDE.md — working on ManuAI

Offline-first, **grounded** voice copilot for factory operators: an operator speaks a fault
("labeler on line 3 jammed, error E-42"), ManuAI speaks back the right SOP procedure and shows
it on screen **cited to the exact SOP** — or **refuses + escalates** if no approved procedure
matches. Runs 100% locally on Apple Silicon; the headline is that it works **wifi-off**.

## Mental model (read this first)
- **One brain:** `src/core.py` → `async answer(question, machine_id, retriever) -> screen_state`.
  Everything funnels through it: retrieve → **threshold gate** → Qwen (Ollama, forced-JSON) →
  cite-or-refuse → `screen_state`.
- **One retriever seam** (`src/retriever.py`), two impls — both `async search(q, machine_id, k) -> [record]`
  with a class attr `threshold`:
  - `CosineRetriever` — local nomic + cosine over `index.json`. **Offline-bulletproof. gate = 0.70.**
  - `MossRetriever` — Moss (sponsor tech), sub-10ms. **gate = None** (see gotchas).
- **Two delivery modes, same brain + same screen renderer:**
  - `src/offline_demo.py` — **WebRTC-free, the WIFI-OFF demo** (mic→whisper→core→kokoro + screen over localhost HTTP).
  - `src/agent.py` (LiveKit) + `web/operator.html` — browser push-to-talk, **WIFI-ON only**.
- **One screen contract:** the `screen_state` dict, rendered by `web/screen.html`'s `applyState()`
  (reused verbatim by `operator.html` over the LiveKit data channel).

## Run & verify (from repo root; venv at `.venv`; Ollama running with `qwen2.5:3b` + `nomic-embed-text`)
- Build index: `.venv/bin/python src/ingest_local.py`  (→ `index.json` at repo root, from `data/`).
- Wifi-off demo: `.venv/bin/python src/offline_demo.py` → open `http://localhost:8000`, press Enter, speak.
- Wifi-on demo: `livekit-server --config livekit.offline.yaml` · `src/agent.py dev` · `src/server.py` → `/operator.html`.
- **REGRESSION GATE — run after ANY corpus / threshold / prompt change:**
  `.venv/bin/python src/test_beats.py` → must print `ALL BEATS PASS`.
  Voice/pipeline checks (no mic needed): `src/offline_demo.py --selftest`, `src/voice_smoke.py`, `src/agent.py check`.

## Layout & conventions
- `src/` = all Python; imports stay **flat** (`import core`, `from retriever import …`) — works because
  `sys.path[0]` = `src/` when you run `python src/<x>.py`.
- **`src/paths.py` is the ONLY source of repo-root asset paths** (`REPO/DATA/WEB/MODELS/INDEX_JSON/ENV_FILE`).
  Use it; never hardcode `Path(__file__).parent` for assets (they live at repo root, not in `src/`).
- `web/` HTML+static · `docs/` PRD/ARCHITECTURE/TODO/phases · `data/` SOP corpus · `attic/` superseded.
- Stack: **Ollama** (Qwen2.5-3B + nomic-embed) · **mlx-whisper** STT · **kokoro-onnx** TTS · **Moss**/stub retrieval · **Unsiloed** ingest · MLX/Apple Silicon.
- Gitignored: `.env`, `.venv/`, `models/`, `docs/PRD.md`. Working branch: `build/demo-mvp`; commit per verified step.

## Hard-won gotchas (do NOT re-learn these the hard way)
- **WebRTC can't run offline.** LiveKit's peer connection fails wifi-off even with `node_ip` pinned to
  loopback. The wifi-off path is `offline_demo.py` (no WebRTC). operator.html/LiveKit = wifi-ON only. *(G16)*
- **Moss is cloud-anchored.** `load_index` is a network fetch; only per-query is local. The **offline brain
  is the stub (CosineRetriever), not Moss.** *(G14)*
- **Moss `.score` is per-query normalized** (top ≈ 1.0) → no absolute threshold works → the gate is
  **stub-only**. On the Moss path, refusal comes from the **task-match few-shot in `core.SYSTEM`** —
  it is load-bearing; keep it. *(G15)*
- **Whisper:** pin `language="en"` or it mis-detects Chinese (`对ising…`). `.env` `WHISPER_MODEL` needs the
  `-mlx` suffix (`whisper-small-mlx`); an empty `HF_TOKEN` breaks anonymous HF downloads (the code pops it).
- **The 0.70 threshold is a window**, not a magic number: must pass jam/cobot, reject the off-domain (servo),
  and let the bypass-interlock query through to a *cited policy refusal*. Re-tune + re-run `test_beats.py` after corpus changes.
- `data/manifest.json → intentional_gaps`: queries you must **never** add docs for — their absence is what
  drives the refusal/escalation beat.

## Contracts (don't break these — full detail in docs/ARCHITECTURE.md)
- **`screen_state`** (§3b): `{question, machine_id, status:"answered"|"escalated", answer,
  citations:[{sop_id,section,page,procedure_title}], steps_source, steps[], safety_warnings[],
  safety_flag, top_score, threshold, source_excerpt}`. escalated ⇒ all the list/source fields empty.
- **chunk schema** (`src/corpus.py`): `{id, sop_id, section, page, machine_id, doc_type, procedure_title,
  error_codes[], safety_flag, fault_codes, text}` — the stub index and the Moss index hold IDENTICAL chunks.

## Deep docs
- **`docs/ARCHITECTURE.md`** — the source of truth: contracts, data flows, the **gap register (G1–G16)**, and the diagram.
- `docs/TODO.md` — build status/log · `docs/PRD.md` (local) — vision + decisions D1–D10 · `docs/phases/` — per-phase goals + tests.
