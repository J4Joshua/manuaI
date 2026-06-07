---
name: ollama
description: Serve local LLMs and embedding models offline on Apple Silicon with Ollama, including streaming chat and the embeddings API. Use when running ManuAI's local Qwen LLM or generating embeddings for the Moss index.
---

# Ollama — Local LLM + Embedding Runtime (Offline, Apple Silicon)

Run quantized LLMs (Qwen2.5) and embedding models entirely on-device via a local HTTP server (`localhost:11434`), using Apple Metal on M-series chips. For ManuAI, Ollama is a candidate runtime for BOTH (a) the Qwen2.5-Instruct answer-composition LLM and (b) the single embedding model used at ingestion-time and query-time for the Moss vector index.

The whole point for ManuAI: **everything must work with wifi OFF**. After a one-time `ollama pull`, models live on local disk and the server makes no network calls at query time. Pull every model up front, verify offline, and pin exact tags so index-time and query-time stay byte-identical.

## When to use this skill

- Standing up the local **Qwen2.5** LLM for cite-or-refuse answer composition with streaming tokens → TTS.
- Generating **embeddings** for the Moss index at ingestion-time and at query-time (must be the same model — see parity rule).
- Wiring the latency-sensitive path: embed+retrieve <50ms, LLM first token ~300–600ms, ≤1.5s total.
- Guaranteeing **offline** operation: pre-pulling models, choosing the storage path, keeping models resident in memory.
- Deciding between Ollama and MLX as the on-device runtime (see "Ollama vs MLX").

If you need raw MLX-level control or maximum throughput, see the `mlx` skill. For embedding model selection and index wiring, see the `moss` skill.

## Quickstart

### 1. Install (macOS, Apple Silicon)

Download `Ollama.dmg` from https://ollama.com/download, mount it, and drag **Ollama** to `/Applications`. On first launch it offers to symlink the `ollama` CLI into `/usr/local/bin`. Apple M-series uses the Metal GPU automatically (unified memory acts as VRAM). Homebrew (`brew install ollama`) also works but the `.app` is the documented path.

The desktop app starts the background server. To run the server manually (e.g. headless / scripted):

```bash
ollama serve        # serves the REST API on http://localhost:11434
```

### 2. Pull the models (do this ONCE, while online)

```bash
ollama pull qwen2.5:7b-instruct        # LLM — pin an exact tag (see parity note for embeds)
ollama pull nomic-embed-text:v1.5      # embedding model — 768-dim, pin the version tag
ollama list                            # verify both are on disk
```

`ollama pull` is the **only** step that touches the network. Everything after works offline.

### 3. (a) Streaming chat — REST + Python

REST (`/api/chat`, streaming is the default; each line is one JSON object):

```bash
curl http://localhost:11434/api/chat -d '{
  "model": "qwen2.5:7b-instruct",
  "messages": [{"role": "user", "content": "Summarize the safety procedure."}],
  "stream": true,
  "keep_alive": "30m"
}'
```

Each streamed chunk looks like:
```json
{"model":"qwen2.5:7b-instruct","message":{"role":"assistant","content":"The"},"done":false}
```
The final object has `"done": true` plus timing fields (`total_duration`, `eval_count`, etc.).

Python (official `ollama` package — `pip install ollama`):

```python
import ollama

stream = ollama.chat(
    model="qwen2.5:7b-instruct",
    messages=[{"role": "user", "content": "Summarize the safety procedure."}],
    stream=True,
    keep_alive="30m",
)
buf = ""
for chunk in stream:
    tok = chunk["message"]["content"]
    buf += tok
    # flush sentence-by-sentence to TTS (see ManuAI guidance below)
    if tok.endswith((".", "!", "?", "\n")):
        speak(buf); buf = ""
if buf:
    speak(buf)
```

### 3. (b) Embeddings — REST + Python

REST — **current endpoint is `/api/embed`** (note: singular `/api/embeddings` is legacy). `input` takes a single string OR an array for batching; the response key is `embeddings` (a list of vectors):

```bash
curl http://localhost:11434/api/embed -d '{
  "model": "nomic-embed-text:v1.5",
  "input": ["chunk one text", "chunk two text"]
}'
```

Response:
```json
{
  "model": "nomic-embed-text:v1.5",
  "embeddings": [[0.0100, -0.0017, 0.0500, ...], [-0.0098, 0.0604, 0.0252, ...]]
}
```
Vectors are L2-normalized (unit length). For `nomic-embed-text` each vector is **768-dim**.

Python (official package — note `ollama.embed` → `embeddings`, plural):

```python
import ollama

resp = ollama.embed(
    model="nomic-embed-text:v1.5",
    input=["chunk one text", "chunk two text"],
)
vectors = resp["embeddings"]          # list[list[float]], each len 768
```

Python (plain `requests`, no extra deps):

```python
import requests

r = requests.post("http://localhost:11434/api/embed", json={
    "model": "nomic-embed-text:v1.5",
    "input": "query text",
})
vector = r.json()["embeddings"][0]    # 768 floats
```

## ManuAI guidance

### Embedding-model parity (CRITICAL — the silent-failure rule)

The PRD warns: the embedding model **must be identical at index-time and query-time**, or Moss retrieval silently degrades (vectors land in a different geometry, similarity scores become meaningless, no error is raised). Enforce it:

- **Pin the exact tag everywhere**, including the version: `nomic-embed-text:v1.5`, never bare `nomic-embed-text` (the floating `:latest` can move between a future `pull` and your build). Store the tag in ONE config constant used by both the ingestion job and the query path.
- Record the embedding model tag + dimension in the Moss index metadata at build time. At query-time, assert the runtime model tag and vector length match the index's recorded values before searching; refuse/rebuild on mismatch.
- If you ever change the embedding model, you must **re-index everything**. There is no partial migration.

### Guaranteeing OFFLINE operation

- **Pre-pull all models** while online: `ollama pull qwen2.5:7b-instruct` and `ollama pull nomic-embed-text:v1.5`. `ollama pull` is the only networked operation; generation and embedding never call out.
- **Storage path:** models live in `~/.ollama/models` (`manifests/` = tiny JSON name→blob maps; `blobs/` = SHA256-named GGUF files). Override with `OLLAMA_MODELS=/path` if you want them on an external/dedicated volume. Back up this directory so a clean machine can run offline without re-pulling.
- **Verify with wifi OFF:** turn off networking, then `ollama list`, run a chat completion, and run an `/api/embed` call. All three must succeed. Make this a pre-deploy checklist item.
- Optionally disable prompt history logging: `OLLAMA_KEEP_HISTORY=false` (history is otherwise plain-text at `~/.ollama/history`).

### Streaming LLM tokens for sentence-by-sentence TTS

Use `stream: true` (default) so first tokens arrive in ~300–600ms instead of waiting for the full answer. Accumulate tokens and flush to TTS on sentence boundaries (`. ! ? \n`) as shown in the chat example — this keeps perceived latency low and lets the cite-or-refuse answer be spoken incrementally. See `local-tts` and `livekit-agents` for the downstream audio pipeline.

### Choosing the embedding model + dimension to match Moss

Pick once, then never drift. Match the Moss index's configured dimension exactly.

| Model (pin a version tag) | Dim | Disk | Context | Notes |
|---|---|---|---|---|
| `nomic-embed-text:v1.5` | **768** | ~274 MB | 2K | Default ManuAI pick; fast, small, strong general retrieval. |
| `mxbai-embed-large:v1` | 1024 | ~670 MB | 512 | Higher quality, larger; needs 1024-dim Moss index. |
| `bge-m3` | 1024 | ~1.2 GB | 8192 | Long-context, 100+ languages, dense+sparse+ColBERT. |
| `all-minilm` | 384 | ~46 MB | — | Tiny/fastest; lower quality, 384-dim. |

Default recommendation for ManuAI: **`nomic-embed-text:v1.5` (768-dim)** — best size/latency/quality balance for the <50ms embed+retrieve budget. Whatever you choose, configure Moss for that exact dimension and lock the tag (parity rule above).

## Key reference

### Core CLI

```bash
ollama pull <model:tag>     # download once (only networked command)
ollama run  <model:tag>     # interactive REPL / one-shot
ollama serve                # start the REST server on :11434
ollama list                 # list local models (verify offline-ready)
ollama ps                   # show models currently loaded in memory
ollama show  <model:tag>    # model details (params, context, etc.)
ollama rm    <model:tag>    # delete a local model
```

### `/api/chat` (streaming)

`POST http://localhost:11434/api/chat`
```json
{
  "model": "qwen2.5:7b-instruct",
  "messages": [{"role": "user", "content": "..."}],
  "stream": true,
  "keep_alive": "30m",
  "options": { "temperature": 0.2, "num_ctx": 4096, "top_p": 0.9, "seed": 42 }
}
```
Streamed chunks: `{"message":{"role":"assistant","content":"..."},"done":false}`; final chunk `"done":true` + timings. `/api/generate` is the single-prompt (non-chat) analog with a `"response"` field instead of `"message"`.

### Embeddings endpoint

- **Current:** `POST /api/embed` — `input` = string or array; response key `embeddings` (list of vectors); L2-normalized. **Use this.**
- **Legacy:** `POST /api/embeddings` — `prompt` = single string only; response key `embedding` (singular). Avoid; returns float64 and lacks batching. Some versions return empty results — migrate off it.

### OpenAI-compatible endpoint

Drop-in for OpenAI SDK clients (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`):

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:11434/v1/", api_key="ollama")  # key ignored

# chat (streaming)
for c in client.chat.completions.create(
        model="qwen2.5:7b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        stream=True):
    print(c.choices[0].delta.content or "", end="")

# embeddings (supports optional `dimensions=` for Matryoshka truncation)
e = client.embeddings.create(model="nomic-embed-text:v1.5", input="text")
vec = e.data[0].embedding
```

## Gotchas

- **Embedding parity (the big one):** different model OR version at index vs query → silent retrieval rot, no error. Pin `model:version` in one shared constant; record tag+dim in the index; assert on load.
- **Endpoint naming / version drift:** use `/api/embed` (plural response `embeddings`). The older `/api/embeddings` (singular `embedding`, single prompt) is legacy and has produced empty-vector bugs. Don't mix the two response shapes in code.
- **Keep models loaded for latency:** a cold model reload adds seconds. Set `keep_alive` per request (`"30m"`, or a negative number / `-1` to pin forever) or the server-wide `OLLAMA_KEEP_ALIVE` env var. `keep_alive: 0` unloads immediately (don't use on the hot path). Pre-warm both the LLM and the embed model at startup with a dummy call. Check residency with `ollama ps`.
- **Offline verification:** test with wifi OFF before trusting it. A missing model only surfaces as a runtime error when first requested.
- **Memory on M-series:** unified memory is shared between OS, LLM, and embed model. Qwen2.5-7B (Q4) is ~4–5 GB; running it alongside an embed model and TTS/STT can pressure an 8–16 GB machine. Right-size the quant (e.g. `7b-instruct` Q4_K_M) and confirm both models stay resident under load without swapping.
- **Setting env vars for the macOS app:** the desktop app does NOT read your shell `.zshrc`. Use `launchctl setenv OLLAMA_KEEP_ALIVE -1` (and similar), then restart Ollama; for persistence across reboots use a LaunchAgent plist. Shell-launched `ollama serve` does read the shell environment.

## Ollama vs MLX (tradeoff)

- **Ollama:** simplest path — one binary, an HTTP server, pull-and-run, OpenAI-compatible API, easy keep-alive and batching. Slightly more abstraction over the metal; quantization choices are per-model-tag.
- **MLX:** Apple's native ML framework — finer control over quantization/memory, can be faster/leaner for a fixed model, but more wiring and no built-in server. See the `mlx` skill.

For ManuAI's "ship it offline on one MacBook" goal, Ollama is the low-friction default; reach for MLX if you need to squeeze latency/memory beyond what Ollama gives.

## Related skills

`qwen` · `mlx` · `moss` · `whisper-stt` · `local-tts` · `livekit-agents` · `unsiloed`

---

**Docs:** https://docs.ollama.com · API reference https://github.com/ollama/ollama/blob/main/docs/api.md · OpenAI compat https://docs.ollama.com/openai · Embeddings https://docs.ollama.com/capabilities/embeddings · macOS https://docs.ollama.com/macos · nomic-embed-text https://ollama.com/library/nomic-embed-text

**Verified on 2026-06-06.** Confirmed against primary sources: current embeddings endpoint is `/api/embed` (legacy `/api/embeddings`); `nomic-embed-text:v1.5` = 768-dim, ~274 MB, 2K ctx; `mxbai-embed-large` / `bge-m3` = 1024-dim; OpenAI base URL `http://localhost:11434/v1/`; default store `~/.ollama/models`; `keep_alive` default 5m; `OLLAMA_KEEP_ALIVE` env var.

**Unverified / confirm at build time:**
- Whether `nomic-embed-text` requires task prefixes (e.g. `search_document:` / `search_query:`) in your Ollama version — the upstream Nomic model uses them, but the Ollama library page did not document them. Test before relying on prefixing.
- Exact Qwen2.5-Instruct tag/quant best for your machine's RAM (e.g. `qwen2.5:7b-instruct` Q4_K_M vs a 3B); measure first-token latency against the ≤600ms budget on the target MacBook.
- Real end-to-end latency (embed+retrieve <50ms, total ≤1.5s) on the actual hardware with both models kept warm — benchmark, don't assume.
- Precise current default disk sizes/tags can shift between Ollama releases; reconfirm with `ollama show` after pulling.
