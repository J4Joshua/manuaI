---
name: first-setup
description: First-time setup of the ManuAI repo, from a fresh clone to a verified, running demo. Installs Ollama + models, the Python venv, livekit-server, builds the local index, and runs the verification gates. Use for initial project setup, or to diagnose install / dependency / model-download errors.
---

# First-time setup — ManuAI

Take a fresh clone to a **verified, running demo**. Do the steps in order; **STOP and fix if a verification fails.** Read `CLAUDE.md` first for the mental model. Target: macOS / Apple Silicon, Python 3.10–3.13.

## 0. Prereqs
- `python3 --version` (3.10–3.13), `git --version`, `brew --version`. You should be in the repo root (it has `src/`, `requirements.txt`).

## 1. Ollama (local LLM + embeddings) — INSTALL THE APP, NOT THE FORMULA
- ⚠ `brew install ollama` (the **formula**) ships WITHOUT the `llama-server` runner and cannot run models (`llama-server binary not found`). Use the cask:
  ```bash
  brew install --cask ollama && open -a Ollama   # starts the server on :11434
  ollama pull qwen2.5:3b && ollama pull nomic-embed-text
  ```
- Verify: `curl -s localhost:11434/api/tags` lists both models.

## 2. Python environment
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Pulls livekit-agents, mlx-whisper, kokoro-onnx, inferedge-moss, etc. (a few minutes).

## 3. livekit-server (only for the wifi-ON browser demo)
```bash
brew install livekit          # provides `livekit-server`
```

## 4. `.env`
```bash
cp .env.example .env
```
Gotchas that will bite:
- `WHISPER_MODEL` must end in `-mlx` → `mlx-community/whisper-small-mlx` (the bare name 404s).
- Leave `HF_TOKEN` **blank** (an empty value sends a broken auth header → 401 even on public models; the code pops it).
- `MOSS_PROJECT_ID/KEY` (from moss.dev) and `UNSILOED_API_KEY` are **optional** — the demo runs fully on the local stub without them.

## 5. Build the local index
```bash
.venv/bin/python src/ingest_local.py     # -> index.json (21 chunks from data/)
```

## 6. Voice models (download once, then offline)
Whisper auto-downloads on first STT run. Kokoro needs `models/kokoro-v1.0.onnx` + `models/voices-v1.0.bin` (the voice scripts fetch them on first run from the kokoro-onnx `model-files-v1.0` release; needs wifi once).

## 7. VERIFY — all must pass before declaring setup done
```bash
.venv/bin/python src/test_beats.py            # -> ALL BEATS PASS   (offline, no mic)
.venv/bin/python src/offline_demo.py --selftest   # -> SELFTEST: PASS (downloads voice models first run)
.venv/bin/python src/agent.py check           # -> PASS  (LiveKit wiring)
```

## 8. Run a demo
- **Wifi-off:** `.venv/bin/python src/offline_demo.py` → open `http://localhost:8000`, press Enter, speak.
- **Wifi-on:** `livekit-server --config livekit.offline.yaml` · `.venv/bin/python src/agent.py dev` · `.venv/bin/python src/server.py` → open `/operator.html`.

If something fails, the **"Hard-won gotchas"** in `CLAUDE.md` cover the known traps; deep detail is in `docs/ARCHITECTURE.md`. Next: run `/dev-setup` for the development workflow.
