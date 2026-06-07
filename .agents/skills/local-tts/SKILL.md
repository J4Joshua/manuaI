---
name: local-tts
description: Synthesize speech locally on Apple Silicon with Kokoro (quality+fast) or Piper (ultra-fast), streamed sentence-by-sentence. Use when implementing or tuning ManuAI's offline TTS step (the box that speaks the LLM's grounded answer back to the operator).
---

# Local TTS (Kokoro / Piper, Apple Silicon) for ManuAI

Purpose: speak the LLM's grounded answer back to the operator **on the box, offline**, fast enough that first audio lands while Qwen is still streaming the rest of the sentence (TTS first-audio ~150-300ms inside a <=1.5s end-to-end budget). Two viable local engines on a MacBook (Apple Silicon):

- **Kokoro** (82M, StyleTTS2/ISTFTNet) - the most natural-sounding open TTS at this size; still fast on M-series. *Lead with this for the demo* (see Recommendation). 24 kHz output.
- **Piper** (VITS) - smaller, **ultra-fast**, lower-but-clear quality; the safety fallback if Kokoro's first-audio latency misses budget on the demo machine. 16 kHz (low) or 22.05 kHz (medium/high) output.

The win for live feel is **sentence-by-sentence streaming**: synthesize and play each completed sentence from the token stream so the operator hears audio almost immediately instead of waiting for the whole answer.

## When to use this skill

- Implementing the **TTS step** of ManuAI's voice loop (the `Kokoro/Piper (local)` box in the PRD pipeline) - the speak-back after Qwen composes the cited answer.
- Wiring **sentence-at-a-time streaming** from the LLM token stream into TTS to minimize first-audio latency.
- Choosing **Kokoro (quality) vs Piper (speed)** against the 150-300ms first-audio budget on the actual demo MacBook.
- Wiring TTS into **LiveKit Agents** as a local (self-hosted) plugin / custom adapter.
- Matching **sample rate** between the TTS output and the rest of the audio pipeline (LiveKit, playback device).
- Debugging offline model/voice caching, warmup latency, espeak-ng/phonemizer setup, or sample-rate mismatches.

## Quickstart

### Kokoro - recommended path: `kokoro-onnx` (offline, no PyTorch, has streaming)

For ManuAI prefer **`kokoro-onnx`** over the PyTorch `kokoro` package: it runs on ONNX Runtime (no PyTorch/transformers), is the fast near-real-time path on M-series, ships an async `create_stream()`, and on macOS does **not** require a manual espeak-ng/phonemizer setup the way the PyTorch package does (see Gotchas).

```bash
pip install -U kokoro-onnx soundfile sounddevice
# Download the two model files ONCE (wifi on) and keep them on the box:
#   kokoro-v1.0.onnx   (~310MB; quantized variant ~80MB)
#   voices-v1.0.bin
# from: github.com/thewh1teagle/kokoro-onnx releases (model-files-v1.0)
```

Synthesize one sentence and save it:

```python
import soundfile as sf
from kokoro_onnx import Kokoro

kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
samples, sample_rate = kokoro.create(
    "Lockout-tagout is required before clearing the jam.",
    voice="af_heart", speed=1.0, lang="en-us",
)
sf.write("answer.wav", samples, sample_rate)   # sample_rate == 24000
```

Stream sentence-by-sentence (each yielded chunk is one synth segment, ready to play as it arrives):

```python
import asyncio, sounddevice as sd
from kokoro_onnx import Kokoro

async def main():
    kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
    stream = kokoro.create_stream(long_text, voice="af_heart", speed=1.0, lang="en-us")
    async for samples, sample_rate in stream:   # first chunk arrives early
        sd.play(samples, sample_rate); sd.wait()

asyncio.run(main())
```

(Alt: the PyTorch package `pip install "kokoro>=0.9.4" soundfile` exposes `KPipeline`, which yields `(graphemes, phonemes, audio)` tuples at 24 kHz - higher fidelity G2P via `misaki`, but needs espeak-ng wired up on macOS. Use `kokoro-onnx` unless you specifically need `KPipeline`.)

### Piper - ultra-fast fallback

```bash
pip install piper-tts          # ships a prebuilt macOS 11+ arm64 wheel with espeak-ng embedded
python -m piper.download_voices en_US-lessac-medium   # downloads .onnx + .onnx.json (~60-100MB)
```

Synthesize one sentence to a WAV:

```python
import wave
from piper import PiperVoice

voice = PiperVoice.load("en_US-lessac-medium.onnx")   # finds the matching .onnx.json
with wave.open("answer.wav", "wb") as wav:
    voice.synthesize_wav("Lockout-tagout is required before clearing the jam.", wav)
```

Stream raw audio chunks (play as they're produced):

```python
import sounddevice as sd
from piper import PiperVoice

voice = PiperVoice.load("en_US-lessac-medium.onnx")
stream = None
for chunk in voice.synthesize("Lockout-tagout is required before clearing the jam."):
    if stream is None:                       # open once, from the first chunk's format
        stream = sd.RawOutputStream(samplerate=chunk.sample_rate,
                                    channels=chunk.sample_channels, dtype="int16")
        stream.start()
    stream.write(chunk.audio_int16_bytes)    # 16-bit PCM bytes; or chunk.audio_float_array
```

CLI sanity check: `echo "test one two three" | piper -m en_US-lessac-medium.onnx -f out.wav`

## ManuAI guidance

**Sentence-by-sentence streaming from the LLM token stream (the key latency move).** Don't wait for Qwen to finish. Accumulate tokens, cut at sentence boundaries, and hand each finished sentence to TTS immediately so the first sentence is *spoken* while later ones are still being *generated*. A minimal sentence splitter over the token stream:

```python
import re
_BOUNDARY = re.compile(r'(?<=[.!?])\s+')      # split after . ! ? + whitespace

async def speak_sentences(token_stream, synth):   # synth(sentence) -> plays audio
    buf = ""
    async for tok in token_stream:                # Qwen tokens (see qwen skill)
        buf += tok
        while (m := _BOUNDARY.search(buf)):
            sentence, buf = buf[:m.end()].strip(), buf[m.end():]
            if sentence:
                synth(sentence)                   # synth+play this sentence now
    if buf.strip():
        synth(buf.strip())                        # flush the trailing fragment
```

This puts first-audio latency ~= (time to Qwen's first sentence) + (TTS first-chunk for one short sentence), not the whole answer. Keep the **first** sentence short (the LLM prompt can encourage a brief lead clause) so the very first synth call returns fast. Inside LiveKit, you get this segmentation for free - it chunks the LLM stream into sentences before calling TTS (see wiring below); the snippet above is for a standalone (non-LiveKit) loop.

**Kokoro (quality) vs Piper (speed) against 150-300ms first-audio.** Both are well under real-time on M-series for a short sentence, so the budget is usually met by *either* once you stream per-sentence and **warm up at startup** (see Gotchas). Piper has the smaller model and lower first-chunk latency (often double-digit-to-~100ms TTFB on capable hardware), so it's the safer pick if the demo machine is weak or the budget is tight. Kokoro costs a bit more compute for clearly more natural prosody. Decision rule: **start on Kokoro; if measured first-audio on the actual MacBook misses ~300ms after warmup + per-sentence streaming, switch the voice loop to Piper** - it's a drop-in swap behind the same `synth(sentence)` interface. Measure on the real machine, not from these notes.

**Wiring as a LiveKit local TTS plugin.** There is **no official Kokoro/Piper plugin** in `livekit-plugins-*`. Two local paths:

1. **Custom `tts_node`** (simplest, fully local, no server). Override the node on your Agent and yield `rtc.AudioFrame`s from your local synth. LiveKit already chunks the LLM output into sentences before calling this node, so you mostly map synth chunks -> frames:

```python
from typing import AsyncIterable
from livekit import rtc
from livekit.agents import Agent, ModelSettings

class ManuAIAgent(Agent):
    async def tts_node(self, text: AsyncIterable[str],
                       model_settings: ModelSettings) -> AsyncIterable[rtc.AudioFrame]:
        async for sentence in text:                       # one sentence per item
            samples, sr = kokoro.create(sentence, voice="af_heart", lang="en-us")
            pcm16 = (samples * 32767).astype("int16").tobytes()  # float32 -> s16
            yield rtc.AudioFrame(data=pcm16, sample_rate=sr,
                                 num_channels=1, samples_per_channel=len(samples))
```

2. **Custom `tts.TTS` subclass** (reusable plugin you pass as `AgentSession(tts=...)`). Implement a `tts.TTS` with `TTSCapabilities(streaming=...)` and a `ChunkedStream`/`SynthesizeStream` that emits `SynthesizedAudio`. Community references: `taresh18/livekit-kokoro` and `nay-cat/LiveKit-PiperTTS-Plugin` show the pattern (some route through a local Kokoro-FastAPI server with OpenAI-compatible endpoints; for a single offline box, in-process synth like option 1 avoids the extra server hop). The `livekit-plugins-piper-tts` PyPI package wraps a self-hosted Piper server if you prefer that split.

**Matching sample rate to the audio pipeline.** Kokoro is **24000 Hz**; Piper is **16000 Hz** (x_low/low) or **22050 Hz** (medium/high). Take the rate from the synth output (`sample_rate` / `chunk.sample_rate`) and pass it straight to the playback device or `rtc.AudioFrame` - don't hardcode 44100/48000. If the output device or LiveKit track is fixed at another rate, resample once (e.g. `soxr`/`librosa`) rather than letting the device guess. Mismatched rate = chipmunk/slow-motion audio.

## Key reference

| | Kokoro | Piper |
|---|---|---|
| Engine | StyleTTS2 + ISTFTNet, 82M | VITS |
| Quality | Higher / more natural | Good, clearly synthetic at low/medium |
| Speed | Fast (more than RT on M-series) | **Ultra-fast**, smaller model |
| Sample rate | **24000 Hz** (fixed) | **16000** (x_low/low) / **22050** (medium/high) |
| Output | float32 array | int16 PCM bytes / float array |
| Streaming | `create_stream()` (kokoro-onnx, async) | `synthesize()` generator of `AudioChunk` |
| Offline path | `kokoro-onnx` (ONNX, no PyTorch) | `piper-tts` (arm64 wheel, espeak-ng embedded) |
| License | Apache-2.0 | MIT model code / GPL voices (piper1-gpl) |

**Kokoro voices** (v1.0, 54 voices / 8 langs). American English (`a`): female `af_heart` (A, top), `af_bella` (A-), `af_nicole`, `af_sky`, `af_aoede`, `af_alloy`, `af_jessica`, `af_kore`, `af_nova`, `af_river`, `af_sarah`; male `am_michael`, `am_adam`, `am_echo`, `am_eric`, `am_fenrir`, `am_liam`, `am_onyx`, `am_puck`, `am_santa`. British English (`b`): `bf_emma`, `bf_isabella`, `bf_alice`, `bf_lily`, `bm_george`, `bm_lewis`, `bm_daniel`, `bm_fable`. Other langs (lang_code): Spanish `e`, French `f`, Hindi `h`, Italian `i`, Japanese `j`, Brazilian Portuguese `p`, Mandarin `z`. **ManuAI: `af_heart` (highest-rated) or `af_bella`, `lang="en-us"`.** Quality is best on ~100-200-token chunks - another reason to feed it one sentence at a time.

**Kokoro params:** `voice` (name or blended tensor), `speed` (1.0 default; <1 slower/clearer for noisy floor), `lang`/`lang_code` (must match the voice's language). kokoro-onnx `create()` returns `(samples float32, sample_rate)`; `create_stream()` is async-iterable of the same tuples.

**Piper voices:** named `<lang>_<REGION>-<name>-<quality>`, e.g. `en_US-lessac-medium`, `en_US-amy-medium`, `en_US-ryan-high`, `en_GB-alba-medium`. Quality tiers `x_low` / `low` (16 kHz) and `medium` / `high` (22.05 kHz) - higher = better + slower + larger. **ManuAI: a `-medium` US voice** (22.05 kHz) is the quality/speed sweet spot; drop to `low` only if you need every millisecond.

**Piper params (`SynthesisConfig`):** `length_scale` (>1 slower speech, <1 faster), `volume`, `noise_scale` / `noise_w_scale` (variation), `normalize_audio`. `AudioChunk` fields: `audio_int16_bytes`, `audio_float_array`, `sample_rate`, `sample_width`, `sample_channels`.

## Gotchas

- **Offline model/voice caching (critical for ManuAI).** Both engines need their model files local before the demo runs wifi-off. **kokoro-onnx:** download `kokoro-v1.0.onnx` + `voices-v1.0.bin` once and pass explicit paths - no network at runtime. **Piper:** `python -m piper.download_voices <name>` pulls `.onnx` + `.onnx.json` from HF; do it during setup and load by local path. The PyTorch `kokoro` package also pulls weights from HF Hub on first use - pre-pull and set `HF_HUB_OFFLINE=1`. A missing file = silent demo failure.
- **espeak-ng / phonemizer (the macOS footgun).** The **PyTorch `kokoro` package** needs system espeak-ng for G2P fallback: `brew install espeak-ng`, and on Apple Silicon you often must point phonemizer at the dylib before importing it (`EspeakWrapper.set_library('/opt/homebrew/Cellar/espeak-ng/<ver>/lib/libespeak-ng.1.dylib')`). **`kokoro-onnx` avoids this** (it handles phonemization without the manual brew/path dance) - another reason it's the recommended path. **Piper** embeds espeak-ng in its bundled arm64 wheel, so `pip install piper-tts` works without a separate brew install (if you instead build from source, espeak-ng headers must be present).
- **Warmup latency.** The first synth after load is much slower (model load + ONNX graph / Metal kernel init). **Warm up at startup** with a one-word dummy synth so the operator's first real sentence hits steady-state ~150-300ms, not a cold path. Do this for whichever engine you pick.
- **Sample-rate mismatch.** See "Matching sample rate" - use the rate the synth reports, don't assume 44.1/48 kHz. Wrong rate = pitch/speed-distorted speech. Kokoro 24 kHz, Piper 16/22.05 kHz.
- **Sentence segmentation.** Naive `.`-splitting breaks on "14.5", "Fig. 3", "No. 2", abbreviations, and decimals - it'll cut mid-number and the operator hears a fragment. The regex above is a starting point; guard against splitting inside digits/known abbreviations, and don't synth empty/whitespace-only fragments. In LiveKit the built-in sentence tokenizer handles most of this for you.
- **Chunk too small = choppy / overhead-bound; too big = late first audio.** One sentence per chunk is the sweet spot. For a very long sentence, Kokoro/Piper internally chunk further; for streaming playback, queue chunks to a single output stream rather than `play(); wait()` serially (the `wait()` in the simple examples blocks - in the real loop, feed a continuous `OutputStream` so playback is gapless).
- **`onnxruntime` provider.** kokoro-onnx/Piper run on ONNX Runtime; on Apple Silicon the default CPU provider is already near-real-time. CoreML EP exists but can be slower to init/finicky - benchmark before assuming it helps; CPU is the safe demo default.
- **Voice licensing.** Piper *model code* is permissive but many **voices are GPL** (piper1-gpl) and some are CC; Kokoro is Apache-2.0 weights. Fine for an internal demo - note it if anything ships.

## Recommendation (for the live demo)

**Lead with Kokoro (`kokoro-onnx`, voice `af_heart`, `lang="en-us"`, 24 kHz), with Piper (`en_US-*-medium`) wired as a one-line fallback.** Kokoro is clearly the more natural voice at this size and is still comfortably faster than real-time on Apple Silicon, so with **per-sentence streaming + startup warmup** it should hit the 150-300ms first-audio target while sounding like a real assistant - which matters for a demo where a human is listening. Keep Piper behind the same `synth(sentence)` interface: if measured first-audio on the actual demo MacBook misses budget after warmup, flip to Piper (ultra-fast, smaller) for guaranteed latency at some quality cost. The deciding number must come from a measurement on the real machine, not from this doc.

## Related skills

- **livekit-agents** - voice orchestration; where this TTS plugs in (`tts_node` / custom `tts.TTS`, AgentSession).
- **qwen** - the LLM whose **token stream** feeds the sentence-by-sentence synth loop here.
- **whisper-stt** - the upstream STT step (push-to-talk -> transcript) that starts the loop this skill ends.
- **mlx** - Apple-Silicon runtime; shared model-caching/offline/warmup concerns (and the MLX Kokoro variant if you go that route).
- **ollama** - alt local LLM/embedding runtime feeding the answer.
- **moss** - local retrieval feeding the grounded answer that gets spoken.
- **unsiloed** - doc parsing on the ingestion side.

---

Doc links:
- Kokoro (PyTorch, KPipeline): https://github.com/hexgrad/kokoro
- Kokoro model card + VOICES/EVAL: https://huggingface.co/hexgrad/Kokoro-82M
- kokoro-onnx (recommended offline path): https://github.com/thewh1teagle/kokoro-onnx
- Piper (current home): https://github.com/OHF-Voice/piper1-gpl - Python API: docs/API_PYTHON.md, voices: docs/VOICES.md
- Piper voice samples + sample rates: https://rhasspy.github.io/piper-samples/
- LiveKit TTS overview: https://docs.livekit.io/agents/models/tts/ - custom nodes: https://docs.livekit.io/agents/build/nodes/
- Community LiveKit plugins: https://github.com/taresh18/livekit-kokoro , https://github.com/nay-cat/LiveKit-PiperTTS-Plugin , https://pypi.org/project/livekit-plugins-piper-tts/

**Verified on 2026-06-06.**

Unverified / flagged:
- **First-audio latency numbers** (Kokoro/Piper "150-300ms", Piper "~80ms TTFB", "more than RT on M-series") are from third-party blogs/community plugins, not primary benchmarks, and vary by chip (M1-M5), model/quantization, and sentence length. **Measure on the actual demo MacBook** before locking the engine choice - treat the Kokoro-first recommendation as a strong prior, not a settled fact.
- **No official LiveKit Kokoro/Piper plugin** in `livekit-plugins-*` as of verification; the `tts_node` override and `tts.TTS` subclass interfaces are from LiveKit docs but the exact `ChunkedStream`/`SynthesizedAudio`/`TTSCapabilities` signatures were not fully quotable from primary docs - verify against the installed `livekit-agents` version. Community plugins (`taresh18/livekit-kokoro`, `nay-cat/LiveKit-PiperTTS-Plugin`) are unofficial.
- **kokoro-onnx vs PyTorch `kokoro` on macOS espeak-ng:** kokoro-onnx is reported to avoid the manual espeak-ng/phonemizer setup; confirm on your install (some misaki/G2P paths may still want espeak-ng for edge text). The PyTorch package's `brew install espeak-ng` + `set_library` dylib path requirement is well-documented.
- **Package versions** not pinned here (kokoro-onnx, piper-tts, kokoro, livekit-agents move fast) - check `pip show <pkg>` for the installed version and re-verify API names (`create_stream`, `synthesize`, `download_voices`) against it.
- **kokoro-onnx `create()`/`create_stream()` exact kwarg names** (`voice`, `speed`, `lang`) confirmed from repo examples; the streaming chunk granularity (per-sentence vs per-N-tokens) is implementation-defined - confirm chunk sizes empirically.
