---
name: whisper-stt
description: Transcribe push-to-talk speech locally on Apple Silicon with faster-whisper / mlx-whisper, sized for low latency. Use when implementing or tuning ManuAI's offline STT step (push-to-talk -> live transcript -> embed/retrieve/LLM).
---

# Whisper STT (local, Apple Silicon) for ManuAI

Purpose: turn an operator's push-to-talk utterance into text **on the box, offline**, fast enough to fit the ManuAI latency budget (STT ~200-400ms of a <=1.5s end-to-end to-first-word target). Two viable local backends on a MacBook (Apple Silicon):

- **mlx-whisper** - runs on the Apple GPU via MLX. *Recommended default for ManuAI* because faster-whisper has no Metal acceleration on Mac (see Gotchas).
- **faster-whisper** (CTranslate2) - mature, great API (built-in VAD, batching), but **CPU-only on Apple Silicon**. Solid fallback / portable choice.

## When to use this skill

- Implementing the **STT step** of ManuAI's voice loop (the `Whisper STT (local)` box in the PRD pipeline).
- Choosing/sizing a Whisper model against the **<=400ms STT budget** vs accuracy in a loud factory room.
- Wiring Whisper into **LiveKit Agents** as a local (self-hosted) STT plugin with **push-to-talk** (manual turn) capture.
- Showing a **live transcript on screen** as the operator speaks.
- Debugging offline model caching, `compute_type`, or first-call warmup latency.

## Quickstart

Both download model weights from the Hugging Face Hub on first use, then cache locally (see offline caching in Gotchas). Audio decoding needs `ffmpeg` (`brew install ffmpeg`).

### mlx-whisper (recommended on Apple Silicon)

```bash
pip install mlx-whisper
brew install ffmpeg
```

```python
import mlx_whisper

# Short push-to-talk clip. Pin an MLX-converted repo from mlx-community.
result = mlx_whisper.transcribe(
    "utterance.wav",
    path_or_hf_repo="mlx-community/whisper-large-v3-turbo",  # or .../distil-whisper-large-v3, .../whisper-small
    language="en",          # skip auto-detect: faster + fewer errors in noise
    word_timestamps=False,  # not needed for a single short utterance
)
print(result["text"])
```

CLI sanity check: `mlx_whisper utterance.wav --model mlx-community/whisper-large-v3-turbo`

mlx-whisper takes a path/array; for a captured **buffer**, write a temp WAV (16 kHz mono) or pass a NumPy float32 array of samples as `audio`.

### faster-whisper (CPU on Mac; CUDA elsewhere)

```bash
pip install faster-whisper
brew install ffmpeg
```

```python
from faster_whisper import WhisperModel

# On Apple Silicon: device="cpu". int8 is fastest/lightest on CPU.
model = WhisperModel("small", device="cpu", compute_type="int8")

segments, info = model.transcribe(
    "utterance.wav",
    language="en",     # pin language; avoids per-call detection
    beam_size=1,       # greedy = lowest latency for short utterances (vs default 5)
    vad_filter=True,   # built-in Silero VAD trims leading/trailing silence
)
text = " ".join(seg.text for seg in segments)  # generator is lazy; iterate to run
print(text)
```

`distil-large-v3` needs two extra args for good results:
```python
model = WhisperModel("distil-large-v3", device="cpu", compute_type="int8")
segments, info = model.transcribe("utterance.wav", language="en",
                                  condition_on_previous_text=False, beam_size=1)
```

## ManuAI guidance

**Model choice for the <=400ms STT budget (loud room, push-to-talk, en):**

- Start with **`small`** (mlx-community/whisper-small). Best latency/accuracy balance for short utterances; small models run ~22-34x real-time on M-series, so a 3-5s clip is well under budget. This is the safe demo default.
- If `small` accuracy on factory terms / error codes ("E-42") is weak, step up to **`large-v3-turbo`** (`mlx-community/whisper-large-v3-turbo`, 809M params) - near-large accuracy at a fraction of the cost (~14-18x real-time on recent M-series). Good accuracy-per-ms sweet spot.
- **`distil-large-v3`** is English-only, fast, near-large WER - a fine alternative to turbo. Use `condition_on_previous_text=False`.
- Avoid full **`large-v3`** for the live path: ~5-14x real-time, can blow the budget on short clips and adds warmup cost. Keep it only as an offline "re-transcribe for the audit log" option.
- `tiny`/`base` are fast but error-prone in noise - not recommended for safety-relevant transcripts where "E-42" vs "E-40" matters.

**Push-to-talk, not continuous VAD.** ManuAI deliberately uses push-to-talk to dodge wake-word/VAD fragility in a loud room. Capture **one utterance at a time**: start recording on button-down, stop on button-up, then transcribe the whole buffer in a single call. Do **not** run an always-on VAD listener. (faster-whisper's `vad_filter=True` is still useful *within* the captured clip to trim dead air.)

**Live partial transcript for the screen.** Whisper is non-streaming (it transcribes a complete buffer), so true word-by-word streaming isn't native. For ManuAI's glanceable screen, two pragmatic options:
1. Show "Listening..." while the button is held, then render the full transcript the instant STT returns (simplest; STT is ~200-400ms so it feels immediate).
2. Pseudo-streaming: while the button is held, re-transcribe the growing buffer every ~0.5-1s with the smallest fast model and overwrite the on-screen text; replace with the final pass on button-up. More work; only do it if the empty-while-talking gap feels bad on stage.

**Wiring as a LiveKit local STT plugin.** Whisper is a **non-streaming** STT, so LiveKit wraps it with VAD via `StreamAdapter`, or you bypass turn detection entirely with **manual turn detection** (the right model for push-to-talk):

```python
from livekit.agents import AgentSession
from livekit.agents.voice import TurnHandlingOptions
from livekit import rtc

session = AgentSession(
    stt=my_whisper_stt,  # custom STT wrapping mlx_whisper / faster_whisper (see below)
    turn_handling=TurnHandlingOptions(turn_detection="manual"),
    # llm=..., tts=...
)
session.input.set_audio_enabled(False)  # mic off until the operator holds the button

@ctx.room.local_participant.register_rpc_method("start_turn")
async def start_turn(data: rtc.RpcInvocationData):
    session.interrupt()
    session.clear_user_turn()
    session.input.set_audio_enabled(True)   # button down

@ctx.room.local_participant.register_rpc_method("end_turn")
async def end_turn(data: rtc.RpcInvocationData):
    session.input.set_audio_enabled(False)  # button up
    session.commit_user_turn()              # finalize -> STT -> LLM
```

There is no official faster-whisper/mlx LiveKit plugin shipped in `livekit-plugins-*`; implement a custom `stt.STT` subclass (a community example, `taresh18/livekit-whisper`, exposes a `WhisperSTT(model=..., language="en", device=..., compute_type=...)` over the faster-whisper backend and is passed straight into `AgentSession(stt=...)`). For ManuAI, a thin custom STT that calls `mlx_whisper.transcribe(...)` on the committed utterance buffer is the cleanest local path.

**Loud-room tips.** Headset/boom mic close to the mouth (biggest single SNR win). Keep `language="en"` pinned. Push-to-talk already excludes background chatter between turns; `vad_filter` (faster-whisper) trims silence at the edges. **Keep a typed-input fallback wired** for the stage in case STT mis-hears on the demo floor (PRD risk mitigation).

## Key reference

| Model (size key / mlx-community repo) | Params | Rough speed (M-series) | Notes |
|---|---|---|---|
| `tiny` | 39M | ~48-60x RT | Fast, noisy-room errors; avoid for safety text |
| `base` | 74M | very fast | Same caveat as tiny |
| `small` | 244M | ~22-34x RT | **ManuAI default** - good balance |
| `medium` | 769M | moderate | Usually skip; turbo is better value |
| `distil-large-v3` | ~756M | fast, near-large WER | **English-only**; set `condition_on_previous_text=False` |
| `large-v3-turbo` (`whisper-large-v3-turbo`) | 809M | ~14-18x RT | **Best accuracy/ms** if `small` underperforms |
| `large-v3` | 1550M | ~5-14x RT | Highest accuracy; too slow for live, OK for audit re-pass |

Key params:
- **`language="en"`** - pin it; skips per-utterance language detection (faster, fewer errors).
- **`beam_size`** (faster-whisper) - `1` (greedy) = lowest latency for short clips; default `5` is slightly more accurate but slower.
- **`vad_filter=True`** (faster-whisper) - Silero VAD trims silence; conservative default removes silence > ~2s.
- **`condition_on_previous_text=False`** - required for `distil-*`; also avoids drift on single short utterances.
- **device / compute_type (faster-whisper on Mac):** `device="cpu"`, `compute_type="int8"` (fastest/lightest). `int8_float16` / `float16` are GPU/CUDA-oriented; **on Apple Silicon CTranslate2 effectively runs float32 on CPU** (no Metal).
- **mlx-whisper:** no device flag - it uses the Apple GPU automatically; pick precision by choosing the repo (e.g. `whisper-large-v3-turbo` fp16 vs `-q4` / `-8bit` quantized variants).

mlx-community quantized turbo variants: `whisper-large-v3-turbo`, `whisper-large-v3-turbo-fp16`, `whisper-large-v3-turbo-q4`, `whisper-large-v3-turbo-8bit`.

## Gotchas

- **Offline model caching (critical for ManuAI).** Weights download from HF Hub on first run and cache in `~/.cache/huggingface/hub`. **Pre-pull every model during setup while wifi is on** (PRD: kick off all downloads in the first 30 min). At query time the demo runs wifi-off; set `HF_HUB_OFFLINE=1` to guarantee no network call, or point to a local model dir (`path_or_hf_repo="/path/to/model"` for mlx, a local CTranslate2 dir for faster-whisper). A missing cached model = silent demo failure.
- **`compute_type` on Apple Silicon.** faster-whisper relies on CTranslate2, which has **no Metal/GPU backend** - it's CPU-only on Mac and tends to fall back to float32 even if you ask for int8/float16. So faster-whisper does NOT benefit from the M-series GPU. If you need the GPU, use **mlx-whisper**. (On CPU, still prefer `compute_type="int8"`.)
- **First-call warmup latency.** The first `transcribe()` after load is much slower (model load, MLX graph/Metal kernel compile, lazy weight init). **Warm up at startup** with a ~1s dummy clip so the operator's first real utterance hits the steady-state ~200-400ms, not a cold path.
- **mlx vs faster-whisper - genuine tradeoff.** mlx-whisper uses the GPU and is generally the fastest *offline single-utterance* path on current M-series, and is the native fit for an MLX box. But results vary by chip/model and some older benchmarks (early MLX on M1) found faster-whisper competitive or better. **Benchmark both on the actual demo MacBook** with the actual clip length and chosen model before locking it in. faster-whisper has a richer API (built-in VAD, `BatchedInferencePipeline`, word timestamps) if you need it.
- **Non-streaming.** Both transcribe a complete buffer, not a token stream - design the UI around utterance-at-a-time (see Live partial transcript above).
- **Audio format.** Whisper expects 16 kHz mono; pass a path (ffmpeg decodes) or a 16 kHz float32 mono array. Mismatched sample rate degrades accuracy.

## Related skills

- **livekit-agents** - voice orchestration; where this STT plugs in (AgentSession, manual turn detection).
- **mlx** - the Apple-Silicon runtime mlx-whisper builds on; shared model-caching/offline concerns.
- **qwen** - downstream LLM that consumes the transcript.
- **ollama** - alt local LLM/embedding runtime.
- **local-tts** - the speak-back step after the LLM (Kokoro/Piper).
- **moss** - local retrieval the transcript is embedded into.
- **unsiloed** - doc parsing for the ingestion side.

---

Doc links:
- faster-whisper (SYSTRAN): https://github.com/SYSTRAN/faster-whisper
- mlx-whisper (PyPI): https://pypi.org/project/mlx-whisper/ - and source in https://github.com/ml-explore/mlx-examples/tree/main/whisper
- MLX whisper models: https://huggingface.co/mlx-community (e.g. whisper-large-v3-turbo, distil-whisper-large-v3, whisper-small)
- LiveKit STT overview: https://docs.livekit.io/agents/models/stt/
- LiveKit turns / manual turn detection: https://docs.livekit.io/agents/build/turns/

**Verified on 2026-06-06.**

Unverified / flagged:
- **mlx-whisper vs faster-whisper "which is fastest on M-series"** - no single authoritative head-to-head for short utterances; sources conflict (older M1 MLX benchmark favored faster-whisper). Treat the "mlx is faster" recommendation as a strong prior to **confirm on the actual demo machine**, not a settled fact.
- **Real-time-factor / latency numbers** (e.g. small ~200ms, turbo ~400-600ms) are from third-party Apple-Silicon benchmark blogs, not primary docs; they vary by chip generation (M1-M5).
- **faster-whisper version** observed at 1.2.x with `large-v3-turbo` + `BatchedInferencePipeline` support; confirm the exact current version with `pip show faster-whisper`. mlx-whisper observed at 0.4.3.
- **No official LiveKit faster-whisper/mlx plugin** in `livekit-plugins-*` as of verification; custom STT subclass or community plugin required. The `StreamAdapter`+VAD path is documented but the exact import path/signature was not fully quotable from primary docs - verify against the installed `livekit-agents` version.
