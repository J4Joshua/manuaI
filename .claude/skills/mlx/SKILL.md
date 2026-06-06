---
name: mlx
description: Run LLMs, Whisper, and embeddings fast on Apple Silicon with Apple MLX (mlx-lm, mlx-whisper, mlx-embeddings), including streaming generation and 4-bit/8-bit quantization. Use when optimizing ManuAI's local on-device inference latency on the MacBook edge box, or deciding MLX vs Ollama for the offline voice loop.
---

# Apple MLX for fast on-device inference

**Purpose.** MLX is Apple's array/ML framework for Apple Silicon (M-series). It uses unified memory + Metal so the same tensors run on CPU and GPU with no copies. For ManuAI it is the latency play that makes Qwen (LLM), Whisper (STT), and the embedding model fast enough for a conversational, offline voice loop on a single MacBook.

The three ecosystem packages you care about:
- **`mlx-lm`** — run/quantize/serve LLMs (Qwen2.5-Instruct), with streaming generation.
- **`mlx-whisper`** — OpenAI Whisper STT, Metal-accelerated.
- **`mlx-embeddings`** — BERT/RoBERTa/ModernBERT/Qwen3 text embeddings for retrieval (must match the Moss index model).

## When to use this skill

- Hosting the **local LLM** (Qwen2.5-Instruct) and you need **streaming** tokens to feed sentence-by-sentence TTS within the ≤1.5s first-word budget.
- Running **Whisper STT** locally for the ~200–400ms transcription step.
- Producing **query-time embeddings** locally that must be byte-for-byte the same model as the one used to build the **Moss** index (embedding-parity rule).
- Picking **quantization** (4-bit vs 8-bit) to fit unified memory while hitting the first-token latency target.
- Deciding **MLX vs Ollama** for any leg of the offline pipeline.
- Converting a Hugging Face model to MLX format / caching it for **wifi-off** operation.

## Quickstart

### Install
```bash
# Requires Apple Silicon (M1/M2/M3/M4) + native (arm64) Python 3.9+, macOS 13.5+.
# Some large-model / long-context optimizations want macOS 15+.
pip install mlx-lm mlx-whisper mlx-embeddings
# ffmpeg is needed by mlx-whisper for non-wav audio:
#   brew install ffmpeg
```
All three pull pre-converted models from the `mlx-community` org on Hugging Face on first use and cache them under `~/.cache/huggingface`.

### Run Qwen2.5-Instruct with mlx-lm

CLI (one-shot generate, and interactive chat):
```bash
# 7B for quality, 3B for speed — both 4-bit quantized:
mlx_lm.generate \
  --model mlx-community/Qwen2.5-7B-Instruct-4bit \
  --prompt "First step to clear a labeler jam, error E-42?" \
  --max-tokens 256 --temp 0.2

mlx_lm.chat --model mlx-community/Qwen2.5-3B-Instruct-4bit   # REPL
```

Python — **streaming** (this is the one ManuAI uses, to stream into TTS):
```python
from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

model, tokenizer = load("mlx-community/Qwen2.5-7B-Instruct-4bit")

messages = [
    {"role": "system", "content": "Answer from the SOP. Cite the source or say you don't know."},
    {"role": "user", "content": "The labeler on line 3 jammed and threw error E-42."},
]
prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True)

sampler = make_sampler(temp=0.2, top_p=0.9)   # temp=0.0 -> greedy/deterministic

# stream_generate yields response objects; .text is the new delta each step.
for resp in stream_generate(model, tokenizer, prompt, max_tokens=256, sampler=sampler):
    print(resp.text, end="", flush=True)   # buffer to sentence boundary -> send to TTS
print()
```
Non-streaming one-shot uses `generate(model, tokenizer, prompt=..., max_tokens=..., verbose=True)`.

> Sampling note: `generate`/`stream_generate` do NOT take `temperature`/`top_p` directly. Build a callable with `make_sampler(temp=, top_p=, top_k=, min_p=, ...)` (defaults: `temp=0.0`, all others 0) and pass it as `sampler=`. Defaults give greedy decoding.

### Transcribe with mlx-whisper

CLI:
```bash
mlx_whisper audio.wav --model mlx-community/whisper-large-v3-turbo
```
Python:
```python
import mlx_whisper

result = mlx_whisper.transcribe(
    "audio.wav",
    path_or_hf_repo="mlx-community/whisper-large-v3-turbo",  # default is whisper-tiny
    language="en",            # skip auto-detect to save latency
    word_timestamps=False,
)
text = result["text"]
# result["segments"] holds per-segment (and per-word if word_timestamps=True) timing.
```
`large-v3-turbo` runs comfortably on a 16 GB M-series and is the usual speed/quality pick. For ManuAI's loud-room push-to-talk, `distil-whisper`/`small` is the faster fallback.

### Produce an embedding (must match the Moss index)

```python
import mlx.core as mx
from mlx_embeddings.utils import load

# 384-dim, mirrors sentence-transformers/all-MiniLM-L6-v2:
model, tokenizer = load("mlx-community/all-MiniLM-L6-v2-bf16")

def embed(texts):
    enc = tokenizer.batch_encode_plus(
        texts, return_tensors="mlx", padding=True, truncation=True, max_length=512
    )
    out = model(enc["input_ids"], attention_mask=enc["attention_mask"])
    return out.text_embeds   # mean-pooled AND L2-normalized -> cosine = dot product

q = embed(["error E-42 labeler jam"])   # shape (1, 384)
```
- `out.text_embeds` = pooled + normalized sentence vector (use this).
- `out.last_hidden_state[:, 0, :]` = raw CLS token (only if you need it).
- `mlx-embeddings` supports BERT / RoBERTa / XLM-RoBERTa / **ModernBERT** / **Qwen3** embedding architectures, so most `mlx-community/*` embedding repos load the same way.

## ManuAI guidance

**MLX as the latency play.** On Apple Silicon, MLX uses the GPU/Neural Engine via Metal and a unified-memory pool (no host↔device copies). Against the PRD budget (≤1.5s to first word): STT ~200–400ms, embed+Moss <50ms, **LLM first token ~300–600ms**, TTS first audio ~150–300ms. MLX's low per-token overhead and `stream_generate` are what let you start speaking before the full answer is generated.

**Choosing quantization (fit memory, hit first-token budget).** Unified memory is shared with everything else on the Mac, so footprint matters.
- **4-bit** (`*-4bit`): smallest + fastest first token. Qwen2.5-7B-4bit ≈ ~4–5 GB; Qwen2.5-3B-4bit ≈ ~2 GB. Default choice for the demo.
- **8-bit** (`*-8bit`): higher fidelity, ~2× the memory of 4-bit, slightly slower. Use only if 4-bit answers degrade on your SOPs.
- Prefer **Qwen2.5-3B-4bit** if you're missing the first-token budget; step up to **7B-4bit** for answer quality once latency is comfortable.
- Bound the KV cache for long conversations with `--max-kv-size N` (CLI) to cap memory growth.

**Streaming generation → sentence-by-sentence TTS.** Iterate `stream_generate`, accumulate `resp.text` into a buffer, and flush each completed sentence to the TTS engine. This overlaps LLM decoding with speech synthesis and is the single biggest win against the latency budget. Keep answers short/extractive (PRD principle) so total tokens stay low.

**Embedding parity (critical).** Moss retrieval silently degrades if the **query-time** embedding model differs from the **index-time** one. Rules:
- Use the **same model id and dimension** for ingestion (build the index) and runtime (query). Pin the exact repo, e.g. `mlx-community/all-MiniLM-L6-v2-bf16` (384-dim).
- Use the **same pooling/normalization** path (`out.text_embeds`) on both sides.
- The PRD names `bge-small` / `nomic-embed-text` as candidates. If you ingest with one of those (e.g. via Ollama/sentence-transformers), then **either** query with the identical model **or** ingest with the MLX equivalent so index and query match. MLX equivalents in `mlx-community`: `all-MiniLM-L6-v2` (384-dim) and `nomicai-modernbert-embed-base` (Nomic/ModernBERT). Confirm the exact dim of whatever you pick and keep it constant.
- Practical hackathon move: pick **one** embedding model, embed both index and query with it, and never change it.

**MLX vs Ollama — when to pick which.**
| | MLX (`mlx-lm`/`mlx-whisper`/`mlx-embeddings`) | Ollama |
|---|---|---|
| Platform | Apple Silicon only | Cross-platform |
| Speed on Mac | Typically fastest tokens/sec + lowest first-token on M-series | Good, but a layer above llama.cpp |
| Setup | `pip install`, Python-native, easy to embed in the agent | `ollama pull`, daemon + REST |
| Streaming | `stream_generate` (Python) or `mlx_lm.server` OpenAI API | `/api/generate` + OpenAI-compat endpoint |
| Whisper/embeddings | First-class (`mlx-whisper`, `mlx-embeddings`) | LLM-focused; embeddings yes, STT no |
| Offline | Cache models, run wifi-off | Cache models, run wifi-off |

Pick **MLX** when you want the lowest latency on the MacBook, an all-in-one Python pipeline (LLM + STT + embeddings in one process/runtime), and tight control over streaming/quantization. Pick **Ollama** for a quick drop-in REST server, cross-machine portability, or if your embedding model is only set up there. They can coexist — e.g. Ollama for a quick start, MLX once you're squeezing latency. (See the `ollama` skill.)

**OpenAI-compatible server (optional).** If LiveKit/your app wants an HTTP endpoint instead of in-process calls:
```bash
mlx_lm.server --model mlx-community/Qwen2.5-7B-Instruct-4bit --host 127.0.0.1 --port 8080
# POST http://127.0.0.1:8080/v1/chat/completions  (OpenAI chat schema)
```
In-process `stream_generate` is lower-overhead for the latency budget; use the server only if an integration needs HTTP.

## Key reference

| Task | Command / call |
|---|---|
| Generate (CLI) | `mlx_lm.generate --model REPO --prompt "..." --max-tokens N --temp 0.2` |
| Chat REPL | `mlx_lm.chat --model REPO` |
| Serve OpenAI API | `mlx_lm.server --model REPO --host 127.0.0.1 --port 8080` |
| Load model (Py) | `from mlx_lm import load; model, tok = load("mlx-community/...")` |
| One-shot (Py) | `from mlx_lm import generate; generate(model, tok, prompt=p, max_tokens=N, verbose=True)` |
| Stream (Py) | `from mlx_lm import stream_generate; for r in stream_generate(model, tok, prompt, max_tokens=N, sampler=s): r.text` |
| Sampler (Py) | `from mlx_lm.sample_utils import make_sampler; make_sampler(temp=0.2, top_p=0.9, top_k=0, min_p=0.0)` |
| Cap KV cache | add `--max-kv-size N` (CLI) for long sessions |
| Transcribe (CLI) | `mlx_whisper audio.wav --model mlx-community/whisper-large-v3-turbo` |
| Transcribe (Py) | `mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-large-v3-turbo", language="en")` |
| Embed (Py) | `from mlx_embeddings.utils import load; model, tok = load("mlx-community/all-MiniLM-L6-v2-bf16"); model(ids, attention_mask=mask).text_embeds` |
| Convert HF→MLX | `mlx_lm.convert --hf-path HF/MODEL --mlx-path ./out` |
| Convert + quantize | `mlx_lm.convert --hf-path HF/MODEL --mlx-path ./out -q --q-bits 4 --q-group-size 64` |
| Upload converted | add `--upload-repo your-org/model-name` |

**Model naming.** Pre-converted models live under `mlx-community/<name>-<precision>` on Hugging Face, e.g. `mlx-community/Qwen2.5-7B-Instruct-4bit`, `mlx-community/Qwen2.5-3B-Instruct-4bit`, `mlx-community/whisper-large-v3-turbo`, `mlx-community/all-MiniLM-L6-v2-bf16`. `--q-bits` is 4 or 8; `--q-group-size` is typically 32 or 64.

## Gotchas

- **Apple Silicon + native Python only.** MLX needs an arm64 Python; an x86/Rosetta interpreter will fail or run slow. Check `python -c "import platform; print(platform.machine())"` → `arm64`.
- **Conversion/format.** MLX needs MLX-format weights. Use the pre-converted `mlx-community/*` repos, or run `mlx_lm.convert` once (from a safetensors/HF model) — you can't point `load()` at an arbitrary unconverted HF checkpoint.
- **Unified-memory pressure.** GPU and CPU share one RAM pool. A 7B-4bit model + Whisper + embeddings + the OS + Chrome can trigger Metal "out of memory". Close heavy apps; prefer 4-bit; drop to Qwen-3B if needed; bound `--max-kv-size`.
- **First-call warmup.** The first generation/transcription is slower (model load, Metal kernel compile/cache, weights into memory). **Warm up each model once at startup** with a tiny dummy call so the live demo's first real query is fast.
- **Embedding index↔query parity.** Same embed model + dim + pooling/normalization at index time and query time, every time. Changing it silently wrecks Moss retrieval. (See ManuAI guidance above.)
- **Offline / wifi-off caching.** Models download from Hugging Face on first use into `~/.cache/huggingface`. **Pull every model while you still have wifi** (Qwen, Whisper, embed). Then for the wifi-off run set `export HF_HUB_OFFLINE=1` so nothing tries to reach the network. Verify by toggling wifi off and running the full loop before the demo.
- **`make_sampler` defaults to greedy** (`temp=0.0`). If you expected sampling, set `temp` explicitly.

## Related skills

`qwen` (the LLM you run via mlx-lm), `ollama` (the cross-platform alternative — see decision table above), `whisper-stt` (STT details/tuning), `moss` (the retrieval index that dictates embedding parity), `local-tts` (consumes the streamed tokens), `livekit-agents` (orchestrates STT→LLM→TTS), `unsiloed` (produces the chunks you embed at ingest).

---

**Docs & sources**
- MLX core: https://github.com/ml-explore/mlx · https://ml-explore.github.io/mlx/
- mlx-lm: https://github.com/ml-explore/mlx-lm · https://pypi.org/project/mlx-lm/ · server: https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md
- mlx-whisper: https://pypi.org/project/mlx-whisper/ · (source under https://github.com/ml-explore/mlx-examples)
- mlx-embeddings: https://github.com/Blaizzy/mlx-embeddings
- Models: https://huggingface.co/mlx-community

**Verified on 2026-06-06** (mlx-lm v0.31.x, mlx-whisper v0.4.3, mlx-embeddings v0.1.0).

**Unverified / confirm before relying on:**
- The exact **embeddings package** for ManuAI: `mlx-embeddings` (Blaizzy) is the actively maintained one used above; an alternative `mlx-embedding-models` (taylorai) also exists with a different API — pick one and standardize. Package names/APIs in this space churn; confirm the import path and `text_embeds` attribute against the installed version.
- Whether your chosen **Moss index embedding model** has an exact MLX equivalent — verify the model id AND dimension match before trusting retrieval (e.g. `bge-small-en-v1.5` is 384-dim; confirm the specific MLX repo and dim you use).
- Latency numbers in the PRD are targets, not measured — benchmark on the actual MacBook.
- `--q-group-size` valid values and `--max-kv-size` behavior — confirm against your installed `mlx-lm` (`mlx_lm.convert -h`, `mlx_lm.generate -h`).
