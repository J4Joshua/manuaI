---
name: dev-setup
description: The continuous-development workflow for ManuAI — a start-of-session health check, the edit→verify→commit loop, the verification gates to run after each kind of change, the conventions (paths.py, flat imports, the screen_state + retriever contracts), and recipes for common tasks (add an SOP, change the prompt, tweak the screen, add a retriever). Use when starting a dev session or before changing code. Assumes first-setup is done.
---

# Dev workflow — ManuAI

Assumes setup is done (run `/first-setup` if not). `CLAUDE.md` has the mental model.

## Health check (start of session)
```bash
curl -s localhost:11434/api/tags        # Ollama up, qwen2.5:3b present
.venv/bin/python src/test_beats.py      # -> ALL BEATS PASS   (if red, fix BEFORE changing anything)
```

## Start the app (dev)
**The dev app is the glasses bridge** — one process that serves the operator UI, runs the
Moss swarm, and accepts glasses audio, **fully offline**:
```bash
.venv/bin/python src/glasses_bridge.py
```
- **UI:** open `http://localhost:8000` → redirects to the operator console
  `operator.html?poll=1` (chat + SOP card + Moss context bubble), polling `/state` every ~600 ms.
- **One process, two ports:** audio WebSocket on `:8766`, screen HTTP on `:8000`.
- **Drive it without glasses** (the normal dev case): run the laptop-mic demo
  `.venv/bin/python src/offline_demo.py`, or push a synthetic utterance through the bridge with
  `_synth_fixture(...)` + `loopback_stream(...)` (see `glasses_bridge.py`). Verify with
  `--selftest-wire` (zero models) / `--selftest` (full).

**iOS app — ONLY when `/dev-setup` is invoked with `--ios`.** The Ray-Ban glasses input is
*not needed* for normal development; skip it unless the `--ios` arg is present. When it is:
```bash
ios/configure_and_launch.sh    # writes this Mac's LAN IP into the app, opens Xcode
```
then in Xcode set the **Signing Team** → Run on the iPhone → tap **Start hands-free (audio only)**.
The phone must share the Mac's Wi-Fi/hotspot; re-run the script whenever the IP changes.

## The loop: edit → verify → commit
1. Edit in `src/`. Imports stay **flat** (`import core`, `from retriever import …`). Use **`src/paths.py`** for any repo-root asset path — never hardcode `Path(__file__).parent` (assets live at repo root, not in `src/`).
2. Run the gate that matches the change, then **commit per verified step** (working branch `build/demo-mvp`):
   | change | gate |
   |---|---|
   | logic / corpus / prompt / threshold | `.venv/bin/python src/test_beats.py` → `ALL BEATS PASS` |
   | voice pipeline (STT/TTS/core) | `src/offline_demo.py --selftest` · `src/voice_smoke.py` |
   | glasses bridge / operator UI | `src/glasses_bridge.py --selftest-wire` (zero models) · `--selftest` (full) |
   | LiveKit agent | `src/agent.py check` |
   | screen / server | `src/server.py` then curl `/`, `/operator.html`, `/state` |
3. Commit messages end with the `Co-Authored-By` trailer. Commit only verified steps; don't push to `main` without explicit OK.

## Recipes (common tasks)
- **Add / edit an SOP:** drop a `.md` (with the SOP frontmatter — see `data/machines/*/sops/*.md`) under `data/machines/<id>/sops/`, then `.venv/bin/python src/moss_ingest.py` (rebuilds `data/moss_index.json`). Re-run `test_beats.py`. **Never** add a doc covering a `data/manifest.json → intentional_gaps` query (it kills the refusal beat).
- **Change answer / refuse behavior:** edit `SYSTEM` in `src/core.py`. The off-domain refusal depends on the **task-match few-shot** there — keep it. Re-run `test_beats.py`.
- **Tweak the screen:** edit `web/screen.html`'s `applyState()` — it's reused VERBATIM by `operator.html` over the LiveKit data channel, so one edit updates **both** modes. Keep logic out of the UI; it only renders `screen_state`.
- **Add a retriever:** implement the seam in `src/retriever.py` — `async search(question, machine_id, k) -> [record]` + class attr `threshold`. Moss offline path uses `threshold=None` (refusal via the LLM few-shot).
- **Offline guarantees:** `WHISPER_MODEL` needs the `-mlx` suffix; keep `HF_TOKEN` blank; export `HF_HUB_OFFLINE=1` for a guaranteed-offline run once models are cached.

## Respect these (don't fork the contracts — full detail in `docs/ARCHITECTURE.md` gap register)
- **`screen_state`** and the **chunk schema** are shared by retriever and both UIs.
- WebRTC can't go offline → `offline_demo.py` is the wifi-off path (G16). Moss embed + retrieve is fully local via `data/moss_index.json` (G15 refusal = LLM few-shot).

## Map
`src/` code · `web/` UI · `data/` SOP corpus · **`docs/ARCHITECTURE.md`** (contracts + gap register) · `docs/TODO.md` (status) · `docs/phases/` (per-phase goals + tests) · `CLAUDE.md` (always-loaded summary).
