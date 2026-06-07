---
name: dev-setup
description: The continuous-development workflow for ManuAI — a start-of-session health check, the edit→verify→commit loop, the verification gates to run after each kind of change, the conventions (paths.py, flat imports, the screen_state + retriever contracts), and recipes for common tasks (add an SOP, change the prompt, tweak the screen, add a retriever). Use when starting a dev session or before changing code. Assumes first-setup is done.
---

# Dev workflow — ManuAI

Assumes setup is done (run `/first-setup` if not). `AGENTS.md` has the mental model.

## Health check (start of session)
```bash
curl -s localhost:11434/api/tags        # Ollama up, qwen2.5:3b + nomic-embed-text present
```

## The loop: edit → verify → commit
1. Edit in `src/`. Imports stay **flat** (`import core`, `from retriever import …`). Use **`src/paths.py`** for any repo-root asset path — never hardcode `Path(__file__).parent` (assets live at repo root, not in `src/`).
2. Run the gate that matches the change, then **commit per verified step** (working branch `build/demo-mvp`):
   | change | gate |
   |---|---|
   | voice pipeline (STT/TTS/core) | `src/offline_demo.py --selftest` · `src/voice_smoke.py` |
   | LiveKit agent | `src/agent.py check` |
   | screen / server | `src/server.py` then curl `/`, `/operator.html`, `/state` |
3. Commit messages end with the `Co-Authored-By` trailer. Commit only verified steps; don't push to `main` without explicit OK.

## Recipes (common tasks)
- **Add / edit an SOP:** drop a `.md` (with the SOP frontmatter — see `data/machines/*/sops/*.md`) under `data/machines/<id>/sops/`, then `src/ingest_local.py` (and `src/moss_ingest.py` for the Moss index). **Never** add a doc covering a `data/manifest.json → intentional_gaps` query (it kills the refusal behavior: pass jam/cobot, reject off-domain (servo), and let the bypass query through to a *cited policy refusal*).
- **Change answer / refuse behavior:** edit `SYSTEM` in `src/core.py`. The off-domain refusal depends on the **task-match few-shot** there — keep it.
- **Tweak the screen:** edit `web/screen.html`'s `applyState()` — it's reused VERBATIM by `operator.html` over the LiveKit data channel, so one edit updates **both** modes. Keep logic out of the UI; it only renders `screen_state`.
- **Add a retriever:** implement the seam in `src/retriever.py` — `async search(question, machine_id, k) -> [record]` + class attr `threshold`. Moss `.score` is per-query normalized → `threshold=None` (refusal via the LLM few-shot); raw cosine (stub) → a real numeric gate.
- **Offline guarantees:** `WHISPER_MODEL` needs the `-mlx` suffix; keep `HF_TOKEN` blank; export `HF_HUB_OFFLINE=1` for a guaranteed-offline run once models are cached.

## Respect these (don't fork the contracts — full detail in `docs/ARCHITECTURE.md` gap register)
- **`screen_state`** and the **chunk schema** are shared by both retrievers and both UIs.
- WebRTC can't go offline → `offline_demo.py` is the wifi-off path (G16). Moss is cloud-anchored → the **stub** is the offline brain (G14). The Moss gate is disabled by design (G15).

## Map
`src/` code · `web/` UI · `data/` SOP corpus · **`docs/ARCHITECTURE.md`** (contracts + gap register) · `docs/TODO.md` (status) · `docs/phases/` (per-phase goals + tests) · `AGENTS.md` (always-loaded summary).
