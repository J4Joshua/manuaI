---
name: qwen
description: Run Qwen2.5-Instruct locally on Apple Silicon (Ollama/MLX) with token streaming and a cite-or-refuse grounded-RAG prompt. Use when implementing or tuning ManuAI's local answer-composition LLM (the step that turns retrieved SOP chunks into a spoken, cited answer).
---

# Qwen2.5-Instruct — local grounded answer composer (ManuAI)

**Purpose.** Qwen2.5-Instruct is the local LLM that COMPOSES ManuAI's spoken answer from retrieved SOP chunks. It runs offline (open weights) via Ollama or MLX on one Apple-Silicon MacBook, streams tokens so TTS can speak sentence-by-sentence, and is constrained to **cite a retrieved source or refuse and escalate** — never hallucinate on safety-critical equipment.

This skill is the model layer only. Retrieval (Moss), STT (Whisper), TTS (Kokoro/Piper), and orchestration (LiveKit Agents) are separate skills.

## When to use this skill

- Implementing or tuning ManuAI's "Qwen LLM → cite-or-refuse" step (PRD §M1, the offline brain).
- Choosing/pulling the right model tag (7B for quality vs 3B for speed) for the ≤1.5s latency budget.
- Wiring streaming generation into the LiveKit/TTS pipeline (sentence-by-sentence).
- Writing or hardening the cite-or-refuse / safety-first system prompt (see `reference.md`).
- Debugging prompt-format, hallucination, offline-caching, or first-token-latency issues.
- NOT for: retrieval/embeddings (Moss/unsiloed), audio (whisper-stt/local-tts), or the voice loop (livekit-agents).

## Verified model tags (2026-06-06)

**Ollama** (`ollama pull <tag>`):

| Use | Tag | Size | Notes |
|---|---|---|---|
| 7B default (quality) | `qwen2.5:7b-instruct` | 4.7 GB | Q4_K_M under the hood; `qwen2.5:latest` is the same 7B |
| 7B higher fidelity | `qwen2.5:7b-instruct-q8_0` | 8.1 GB | use if 16GB+ headroom and quality matters |
| 3B default (speed) | `qwen2.5:3b-instruct` | 1.9 GB | Q4_K_M; fastest first token |
| 3B max quality | `qwen2.5:3b-instruct-q8_0` | 3.3 GB | |

Full quant ladder exists (`q2_K`…`fp16`); `q4_K_M` is the sweet spot for M-series.

**MLX** (`mlx-community`, loaded by name, auto-downloaded + cached):

| Use | Repo | ~Size |
|---|---|---|
| 7B 4-bit (recommended) | `mlx-community/Qwen2.5-7B-Instruct-4bit` | 4.3 GB |
| 7B 8-bit | `mlx-community/Qwen2.5-7B-Instruct-8bit` | ~8 GB |
| 3B 4-bit (recommended) | `mlx-community/Qwen2.5-3B-Instruct-4bit` | ~1.7 GB |
| 7B long-context | `mlx-community/Qwen2.5-7B-Instruct-1M-4bit` | 4-bit, 1M ctx |

> NOTE: `Qwen/Qwen2.5-7B-Instruct-MLX` (Qwen's own repo, used in their docs) also exists; the `mlx-community` 4-bit repos are smaller and the de-facto default. **Use `*-Instruct-*`, never the base (non-Instruct) or `-VL` (vision) repos** for this task.

## Quickstart

### Ollama

```bash
ollama pull qwen2.5:7b-instruct   # quality
ollama pull qwen2.5:3b-instruct   # speed
ollama run qwen2.5:3b-instruct "test"   # warm + sanity check
```

Streaming via the OpenAI-compatible API (Ollama applies the ChatML template for you — pass plain messages):

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")

stream = client.chat.completions.create(
    model="qwen2.5:3b-instruct",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},   # see reference.md
        {"role": "user", "content": grounded_user_msg}, # chunks + question
    ],
    temperature=0.0, max_tokens=300, stream=True,
)
for chunk in stream:
    tok = chunk.choices[0].delta.content or ""
    print(tok, end="", flush=True)   # feed tok into the sentence segmenter -> TTS
```

(Or use the native `ollama` Python client / `POST /api/chat` with `"stream": true`.)

### MLX (mlx-lm)

```bash
pip install mlx-lm        # latest verified: v0.31.3 (2026-04-22)
# one-off CLI sanity check:
mlx_lm.generate --model mlx-community/Qwen2.5-3B-Instruct-4bit --prompt "test"
```

Streaming generate in Python — **you must apply the chat template yourself**:

```python
from mlx_lm import load, stream_generate
from mlx_lm.sample_utils import make_sampler

model, tokenizer = load("mlx-community/Qwen2.5-7B-Instruct-4bit")

messages = [
    {"role": "system", "content": SYSTEM_PROMPT},     # see reference.md
    {"role": "user",   "content": grounded_user_msg}, # chunks + question
]
prompt = tokenizer.apply_chat_template(
    messages, add_generation_prompt=True   # tokenize=False is the default here
)

sampler = make_sampler(temp=0.0)   # greedy/deterministic for grounded extraction
for resp in stream_generate(model, tokenizer, prompt, max_tokens=300, sampler=sampler):
    print(resp.text, end="", flush=True)   # resp.text is the incremental piece
```

`stream_generate` yields response objects with a `.text` attribute (the new token piece). Non-streaming equivalent: `generate(model, tokenizer, prompt=..., verbose=True, max_tokens=...)`.

## ManuAI guidance

### Cite-or-refuse system prompt (the core of ManuAI's trust beat)

Use the full template in **`reference.md`**. It enforces, in priority order:
1. **Safety first** — chunks tagged `safety_flag` (LOTO/PPE/interlocks) are surfaced first, in a leading line.
2. **Answer ONLY from provided chunks** — no outside knowledge, no inference beyond the text.
3. **Cite every claim** with the chunk's source tag, e.g. `(SOP-1187 §4.2)`.
4. **Refuse + escalate** when chunks are missing/insufficient/off-topic — emit the fixed refusal string and do not guess.

Shape of the user turn (retrieval builds this; never put chunks in the system prompt so the prompt stays cacheable):

```
[CHUNK 1] source: SOP-1187 §4.2  safety_flag: LOTO
<chunk text>
[CHUNK 2] source: SOP-1187 §4.3
<chunk text>

QUESTION: How do I clear a jam on press line 3?
```

Keep answers **short and extractive** (the PRD wants brevity; long generations blow the latency budget and tire TTS). `max_tokens ≈ 250–350` is plenty.

### Stream sentence-by-sentence into TTS

Don't wait for the full generation. Accumulate streamed token pieces in a buffer; whenever the buffer ends a sentence, flush that sentence to TTS and keep going. Minimal segmenter:

```python
import re
buf = ""
SENT_END = re.compile(r"(.+?[.!?])(\s|$)")   # flush on . ! ?
def feed(piece, speak):
    global buf
    buf += piece
    while (m := SENT_END.match(buf)):
        speak(m.group(1).strip()); buf = buf[m.end():]
def finish(speak):
    global buf
    if buf.strip(): speak(buf.strip()); buf = ""
```

This makes TTS start speaking the first sentence while the LLM is still generating, which is what keeps perceived latency under the 1.5s budget. Guard the citation tag `(SOP-… §…)` so the segmenter doesn't split on a `.` inside a section number if your TTS reads tags aloud (or strip tags from the spoken stream and show them only on screen).

### 7B vs 3B for the latency budget

PRD budget: **LLM first token ~300–600ms** inside ≤1.5s total; stream to TTS per sentence.

- **3B** (`qwen2.5:3b-instruct` / `mlx-community/Qwen2.5-3B-Instruct-4bit`): lowest first-token latency, highest tokens/sec — default for the live demo and any battery/thermal-constrained run. Fully capable of extractive cite-or-refuse on clean chunks.
- **7B** (`qwen2.5:7b-instruct` / `mlx-community/Qwen2.5-7B-Instruct-4bit`): better at refusing correctly on ambiguous/adversarial questions (the "bypass the interlock?" safety beat) and at not over-citing. Use when the box has headroom (16GB+).
- Practical pick: **start on 3B to hit latency, validate the refusal path on 7B**, and make the model a single config value so you can A/B during tuning.

## Key reference (verified)

- **Chat template:** ChatML. Each turn is `<|im_start|>{role}\n{content}<|im_end|>`; generation begins with `<|im_start|>assistant\n`. EOS / stop token is `<|im_end|>`. Roles: `system`, `user`, `assistant` (+ `tool`). **Ollama and the OpenAI API apply this for you; with raw mlx-lm/transformers you call `apply_chat_template`.**
- **Default system prompt** (if you omit one): `You are Qwen, created by Alibaba Cloud. You are a helpful assistant.` — ManuAI always overrides this with the cite-or-refuse prompt.
- **Context window:** native config `max_position_embeddings` = 32,768 tokens; up to 131,072 with YaRN `rope_scaling` (only enable for genuinely long inputs — it slightly degrades short-prompt quality). Max generation ~8,192 tokens. ManuAI prompts (a few chunks + question) sit far under 32k; do NOT enable YaRN.
- **Params:** for grounded extraction use **`temperature=0.0`** (deterministic, minimizes drift from chunks). Qwen's chat-default sampling is `temp=0.7, top_p=0.8, repetition_penalty=1.05` — only relevant if you want some fluency; keep it low for safety. `max_tokens` 250–350.
- **Quantization / memory on M-series:** 4-bit (`q4_K_M` / `-4bit`) is the default — 7B ≈ 4.3–4.7 GB, 3B ≈ 1.7–1.9 GB of weights (plus KV cache, modest at these context sizes). 8-bit roughly doubles that for marginal quality gain. On a 16GB Mac, 7B-4bit alongside Whisper + embeddings + TTS is tight but workable; on 8GB prefer 3B-4bit.

## Gotchas

- **Don't hand-build ChatML when using Ollama/OpenAI/transformers chat APIs** — they template for you; double-templating (literal `<|im_start|>` in your content) breaks the model. Only raw `mlx_lm.generate(prompt=...)` / `model.generate` need a pre-templated string.
- **Hallucination control is the prompt + temp, not the model.** A 7B/3B model *will* invent plausible SOP steps if the prompt doesn't force cite-or-refuse and `temperature` isn't ~0. Test the refusal path explicitly (empty/irrelevant chunks → fixed refusal string).
- **Citations must come from chunk metadata, not the model's memory.** Have retrieval inject the exact `source` tag per chunk and instruct the model to copy it verbatim. Optionally validate post-hoc that every emitted `(SOP-… §…)` exists in the supplied chunks; if not, treat as a refusal.
- **Offline caching:** pre-download everything before going offline. Ollama caches in `~/.ollama/models`; MLX/HF caches in `~/.cache/huggingface/hub`. Set `HF_HUB_OFFLINE=1` for MLX runs so it never tries the network at query time. Pull both 3B and 7B in the first 30 min (PRD warns multi-GB pulls over hackathon wifi are the #1 time-killer).
- **Streaming + sentence segmentation:** a naive split on `.` mis-fires on `§4.2`, `No.`, decimals. Strip/replace citation tags before the segmenter (show them on screen, not in speech) or use a smarter boundary check.
- **Latency:** first call after load is slow (warm-up/compile). Send a tiny warm-up generation at startup. Keep the system prompt fixed (so Ollama can reuse the KV cache) and put the variable chunks in the user turn. Short `max_tokens` + streaming is what actually meets the budget — not a bigger/smaller model alone.
- **First-token vs throughput:** 3B wins both on M-series; if 7B first-token > ~600ms, drop to 3B or a smaller quant before optimizing anything else.

## Ollama vs MLX, and the Qwen3 upgrade path

- **Ollama** — easiest path, built-in templating, OpenAI-compatible streaming server, trivial model management. Best for fast iteration and the LiveKit integration. See the **ollama** skill.
- **MLX (mlx-lm)** — native Apple-Silicon performance, finest control over sampling/streaming, smaller 4-bit repos. Best when squeezing latency or embedding generation in-process. See the **mlx** skill.
- For ManuAI: prototype on Ollama, switch the hot path to MLX if you need the last few ms. Keep the model id behind one config flag.

**Qwen3 upgrade path (note, do not switch by default).** As of 2026-06, Qwen3 (Apr 2025, Apache-2.0) and later (Qwen3-Next, Qwen3.5) exist and are current. Qwen3 dense sizes are 0.6/1.7/4B/8B/14B/32B, and Qwen states **Qwen3-4B ≈ Qwen2.5-7B** and **Qwen3-1.7B ≈ Qwen2.5-3B** in quality — so `qwen3:4b` / `qwen3:1.7b` could give equal quality at lower latency, with a hybrid thinking/non-thinking mode. **PRD pins Qwen2.5-Instruct, so keep it as primary.** If you later evaluate Qwen3: (a) use **non-thinking mode** (thinking traces add latency and pollute the spoken stream — disable via `/no_think` or the chat-template flag), (b) re-validate the cite-or-refuse + safety behavior from scratch, (c) keep the swap behind the same config flag.

## Related skills

`ollama` · `mlx` · `moss` (retrieval that feeds the chunks) · `livekit-agents` (voice orchestration) · `whisper-stt` (upstream STT) · `local-tts` (downstream speech) · `unsiloed` (SOP ingestion/chunking)

---

**Doc links**
- Qwen2.5 docs (MLX-LM): https://qwen.readthedocs.io/en/latest/run_locally/mlx-lm.html
- Qwen2.5 docs (Ollama): https://qwen.readthedocs.io/en/latest/run_locally/ollama.html
- Model card: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct
- Ollama tags: https://ollama.com/library/qwen2.5/tags
- MLX repos: https://huggingface.co/mlx-community/Qwen2.5-7B-Instruct-4bit · https://huggingface.co/mlx-community/Qwen2.5-3B-Instruct-4bit
- mlx-lm: https://github.com/ml-explore/mlx-lm
- Qwen3 blog (upgrade reference): https://qwenlm.github.io/blog/qwen3/

**Verified on 2026-06-06.**

**Unverified / assumed (test on the actual box):** exact first-token/tokens-per-sec latency on the specific MacBook (model-, RAM-, thermal-dependent); whether `mlx-community` 8-bit 3B exists (4-bit confirmed; 8-bit assumed); LiveKit's exact streaming hook (see livekit-agents skill); real-world citation accuracy and refusal-rate on the ManuAI SOP corpus (must be measured); KV-cache reuse behavior across Ollama versions; and the Qwen3 quality-parity claims (Qwen's own benchmarks, not independently verified for this RAG use case).
