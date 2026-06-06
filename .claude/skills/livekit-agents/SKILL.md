---
name: livekit-agents
description: Build a self-hosted LiveKit voice agent wiring local STT/LLM/TTS with push-to-talk and sentence-level streaming. Use when implementing or debugging ManuAI's offline voice pipeline (Whisper -> Moss -> Qwen -> local TTS) and its on-screen transcript/SOP/safety UI.
---

# LiveKit Agents (self-hosted, offline) for ManuAI

Voice orchestration for ManuAI. LiveKit Agents runs the realtime STT -> LLM -> TTS
pipeline. Everything here is wired to run **fully offline on one Apple-Silicon MacBook**:
a self-hosted `livekit-server` plus **local-inference plugins** (no cloud STT/LLM/TTS).
LiveKit handles audio transport, turn-taking, interruption, and sentence-by-sentence
streaming; ManuAI's own skills (whisper-stt, moss, qwen) supply the actual inference.

**Verified on 2026-06-06 against `livekit-agents==1.5.17` (latest stable; 1.6.0rc2 in
pre-release).** See the "Version churn" gotcha — the current docs/`main` API differs
from the older v1.0 `WorkerOptions` examples still floating around.

## When to use this skill

- Standing up or debugging the **voice loop**: headset push-to-talk -> Whisper STT ->
  embed/Moss retrieve -> Qwen LLM -> local TTS -> spoken answer.
- Wiring **local plugins** (Whisper STT, Ollama/MLX LLM, Kokoro/Piper TTS) instead of
  cloud ones, with **wifi OFF**.
- Implementing **push-to-talk** (disabling automatic VAD turn detection).
- Hitting the **<=1.5s end-of-speech -> first word** budget via **sentence-level
  LLM -> TTS streaming**.
- Pushing **live transcript + SOP card + citation + safety banner + escalation state**
  to the operator screen.
- Self-hosting `livekit-server` offline (no LiveKit Cloud).

For the actual model inference, defer to the sibling skills (see Related skills). This
skill is the orchestration glue only.

---

## Quickstart

### 1. Install the SDK + plugins

```bash
# Core agents runtime
pip install "livekit-agents~=1.5"
# OpenAI-compatible plugin (used to reach local Ollama/Whisper OpenAI-style servers)
# and Silero VAD (local, ONNX, no network)
pip install "livekit-plugins-openai" "livekit-plugins-silero"
# The LiveKit server + CLI (macOS, offline-capable)
brew update && brew install livekit
```

`livekit-agents` requires Python >=3.10, <3.15. Plugins are separate packages so you
only pull what runs locally — do **not** install cloud plugins (deepgram, cartesia,
elevenlabs) for the offline build.

### 2. Run a self-hosted server, fully offline

Dev mode is the fastest path. It needs no internet and ships a fixed key/secret pair:

```bash
livekit-server --dev
#  API key:    devkey
#  API secret: secret
#  binds:      ws://127.0.0.1:7880
```

The agent and a local browser frontend both connect to `ws://127.0.0.1:7880`. For a
pinned, reproducible setup use a config file instead of `--dev`:

```yaml
# config.yaml
port: 7880
log_level: info
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: false        # loopback / LAN only; keep false for an air-gapped box
keys:
  devkey: secret                # api_key: api_secret
```

```bash
livekit-server --config config.yaml
# add --bind 0.0.0.0 only if the screen UI runs on a different LAN device
```

Generate a join token for the frontend **offline** (signed locally with the secret):

```bash
lk token create \
  --api-key devkey --api-secret secret \
  --join --room manuai --identity operator-1 \
  --valid-for 24h
```

Set these for the agent process:

```bash
export LIVEKIT_URL="ws://127.0.0.1:7880"
export LIVEKIT_API_KEY="devkey"
export LIVEKIT_API_SECRET="secret"
```

### 3. Minimal working voice agent (current API)

This is the **current (1.5.x)** entrypoint style using `AgentServer` + `@server.rtc_session()`.
(See the version gotcha for the older `WorkerOptions` form.)

```python
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent
from livekit.plugins import openai, silero

server = AgentServer()


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions="You are ManuAI, a factory-floor voice copilot. "
                         "Answer only from provided SOP context. Be concise."
        )


@server.rtc_session(agent_name="manuai")
async def entrypoint(ctx: agents.JobContext):
    session = AgentSession(
        stt=openai.STT(base_url="http://127.0.0.1:9000/v1", model="whisper-1"),
        llm=openai.LLM.with_ollama(model="qwen2.5:7b-instruct",
                                   base_url="http://127.0.0.1:11434/v1"),
        tts=openai.TTS(base_url="http://127.0.0.1:8880/v1", model="kokoro"),
        vad=silero.VAD.load(),
    )
    await session.start(room=ctx.room, agent=Assistant())
    await session.generate_reply(instructions="Greet the operator briefly.")


if __name__ == "__main__":
    agents.cli.run_app(server)
```

```bash
python agent.py dev        # connects to LIVEKIT_URL, joins rooms as the worker
```

> The `base_url` values above point at **local OpenAI-compatible servers** you run for
> Whisper, Ollama, and Kokoro/Piper. That keeps everything offline while reusing the
> battle-tested `openai` plugin. The custom-`*_node` approach (below) is the alternative
> when you'd rather call MLX / faster-whisper in-process with no local HTTP server.

---

## ManuAI guidance

### (a) Fully self-hosted / offline pipeline

Two ways to plug in local inference. Pick one per component; they can be mixed.

**Option A — local OpenAI-compatible servers (simplest, recommended start).**
Run each model behind a localhost OpenAI-style endpoint and point the `openai` plugin's
`base_url` at it. No cloud, all loopback:

| Component | Local server | LiveKit wiring |
|-----------|-------------|----------------|
| STT (Whisper) | faster-whisper / whisper.cpp server exposing `/v1/audio/transcriptions` | `openai.STT(base_url="http://127.0.0.1:9000/v1", model="whisper-1")` |
| LLM (Qwen) | Ollama (`ollama serve`) | `openai.LLM.with_ollama(model="qwen2.5:7b-instruct", base_url="http://127.0.0.1:11434/v1")` |
| TTS (Kokoro/Piper) | Kokoro-FastAPI / Piper OpenAI shim | `openai.TTS(base_url="http://127.0.0.1:8880/v1", model="kokoro")` |
| VAD | Silero (in-process ONNX) | `silero.VAD.load()` |

`openai.LLM.with_ollama(...)` is the documented helper (default `base_url`
`http://localhost:11434/v1`, no API key needed). There is **no** `with_faster_whisper`
helper in 1.5.x — use `openai.STT(base_url=..., model=...)` against your local Whisper
server, or Option B.

**Option B — custom in-process plugins via pipeline nodes (no local HTTP servers).**
Override the node methods on your `Agent` to call MLX / faster-whisper / Kokoro directly
in Python. This is the lowest-overhead path for the MacBook and gives you full control:

```python
from typing import AsyncIterable, Optional
from livekit import rtc
from livekit.agents import Agent, ModelSettings, stt, llm

class Assistant(Agent):
    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame],
                       model_settings: ModelSettings
                       ) -> Optional[AsyncIterable[stt.SpeechEvent]]:
        # call local faster-whisper / mlx-whisper here, yield stt.SpeechEvent(...)
        ...

    async def llm_node(self, chat_ctx: llm.ChatContext, tools, model_settings):
        # call local Qwen (mlx-lm / Ollama) and yield text or llm.ChatChunk
        ...

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings
                       ) -> AsyncIterable[rtc.AudioFrame]:
        # synthesize with local Kokoro/Piper, yield rtc.AudioFrame
        ...
```

Each node can `async for ... in Agent.default.<node>(self, ...)` to reuse default
behavior and only customize part of it. See `reference.md` for full node bodies.

**Moss retrieve** is not a LiveKit component — inject retrieved SOP context into the
chat just before the LLM runs (see escalation/RAG below).

### (b) Push-to-talk pattern

Operators use a headset PTT button, so **disable automatic VAD turn detection** and
drive turns manually. Set `turn_detection="manual"` and gate audio + commit turns from
the frontend over RPC:

```python
from livekit.agents import AgentSession
from livekit import rtc

# Confirmed in examples/voice_agents/push_to_talk.py (main, 2026-06-06):
# turn_detection is a direct AgentSession kwarg.
# (TurnHandlingOptions(turn_detection="manual") is the alternative wrapper form.)
session = AgentSession(
    stt=..., llm=..., tts=..., vad=silero.VAD.load(),
    turn_detection="manual",
)
# Don't listen until the operator presses PTT
session.input.set_audio_enabled(False)

@ctx.room.local_participant.register_rpc_method("start_turn")
async def start_turn(data: rtc.RpcInvocationData):
    session.interrupt()            # barge-in: stop any agent speech
    session.clear_user_turn()      # drop stale buffered audio
    session.input.set_audio_enabled(True)

@ctx.room.local_participant.register_rpc_method("end_turn")
async def end_turn(data: rtc.RpcInvocationData):
    session.input.set_audio_enabled(False)
    # confirmed signature in push_to_talk.py: awaited, with these kwargs
    await session.commit_user_turn(transcript_timeout=5.0, stt_flush_duration=2.0)  # finalize -> STT -> LLM -> TTS

@ctx.room.local_participant.register_rpc_method("cancel_turn")
async def cancel_turn(data: rtc.RpcInvocationData):
    session.input.set_audio_enabled(False)
    session.clear_user_turn()      # discard, no response
```

Frontend calls `start_turn` on button-down, `end_turn` on button-up. You still pass a
`vad=` for endpointing inside the captured window, but with `turn_detection="manual"`
LiveKit will not auto-end the turn — the button does.

### (c) Sentence-level LLM -> TTS streaming (the latency hook)

To hit **<=1.5s end-of-speech -> first spoken word**, audio must start before the LLM
finishes. LiveKit streams the `llm_node` text output into the `tts_node` token-by-token
and synthesizes **sentence by sentence**:

- If your TTS **supports streaming natively**, this is automatic — text chunks flow
  straight to the TTS stream.
- If your TTS is **non-streaming** (most local Kokoro/Piper setups), wrap it in
  `StreamAdapter`, which uses a `SentenceTokenizer` to cut the LLM stream into sentences
  and synthesize each as soon as it's complete:

```python
from livekit.agents import tts, tokenize
from livekit.plugins import openai  # or your custom local TTS

base_tts = openai.TTS(base_url="http://127.0.0.1:8880/v1", model="kokoro")

streaming_tts = tts.StreamAdapter(
    tts=base_tts,
    sentence_tokenizer=tokenize.basic.SentenceTokenizer(),
)
# pass streaming_tts to AgentSession(tts=streaming_tts)
```

`StreamAdapter.__init__(self, *, tts, sentence_tokenizer=NOT_GIVEN, text_pacing=False)`.
It reports `streaming=True` and emits the first audio after the **first complete
sentence** from the LLM, not after the whole reply. Keep the LLM prompt producing short
sentences (Qwen skill covers this) so the first sentence lands fast.

### Passing transcript / SOP card / safety banner / escalation to the screen

The screen is a LiveKit room participant. Two channels:

1. **State (low-frequency): participant attributes.** Use for SOP card, citation,
   safety banner, escalation state — anything that changes a few times per turn.
   AgentSession already maintains `lk.agent.state` (`initializing|listening|thinking|
   speaking`) automatically; add your own keys:

   ```python
   await ctx.room.local_participant.set_attributes({
       "sop_card_id": "SOP-204",
       "citation": "Manual p.12 §3.1",
       "safety": "warn",            # frontend renders the ⚠ banner
       "escalation": "none",        # none|requested|active
   })
   ```
   Attributes are **not** for >~once/few-seconds updates.

2. **Live transcript (high-frequency).** LiveKit forwards realtime transcriptions over
   the transcription protocol automatically — the frontend's `useVoiceAssistant` /
   transcript components render them. Mirror them server-side for logging via events:

   ```python
   @session.on("user_input_transcribed")
   def _t(ev):  # ev.transcript, ev.is_final, ev.language, ev.speaker_id
       ...
   @session.on("conversation_item_added")
   def _c(ev):  # ev.item.role, ev.item.text_content, ev.item.interrupted
       ...
   ```

For escalation, drive a custom attribute (above) and/or `perform_rpc(...)` to the screen
so it can flip into an "escalating to supervisor" view. Push the retrieved SOP card from
the Moss step at the same point you inject context (below).

### Wiring Moss retrieval into the turn

Inject retrieved SOP chunks right after the user turn finalizes, before the LLM runs,
using the `on_user_turn_completed` hook on your `Agent`:

```python
class Assistant(Agent):
    async def on_user_turn_completed(self, turn_ctx, new_message):
        hits = moss_retrieve(new_message.text_content)          # your Moss skill
        turn_ctx.add_message(role="assistant",
            content=f"SOP context for the next answer:\n{hits.text}")
        await self.session._room_io...  # or set_attributes() to show the SOP card
```

This avoids tool-call round-trips and keeps the answer grounded + citable.

---

## Key API reference

| Symbol | Purpose |
|--------|---------|
| `AgentServer()` + `@server.rtc_session(agent_name=...)` | Current (1.5.x) worker entrypoint; run with `agents.cli.run_app(server)`. |
| `AgentSession(stt=, llm=, tts=, vad=, turn_handling=)` | The orchestrator. Glues media + STT/LLM/TTS + turn detection + interruptions. |
| `session.start(room=, agent=)` | Begin the session in a room. |
| `session.generate_reply(instructions=)` | Make the agent speak (greeting / proactive). |
| `session.say(text)` | Speak fixed text without the LLM. |
| `TurnHandlingOptions(turn_detection="manual")` | Disable auto VAD turn-taking for PTT. |
| `session.input.set_audio_enabled(bool)` | Gate the mic (PTT). |
| `session.interrupt()` / `clear_user_turn()` / `commit_user_turn()` | Manual turn control. |
| `Agent.stt_node / llm_node / tts_node / transcription_node` | Override points for custom local inference + post-processing. |
| `Agent.on_user_turn_completed(turn_ctx, new_message)` | Hook to inject RAG/Moss context before the LLM. |
| `tts.StreamAdapter(tts=, sentence_tokenizer=)` | Sentence-level streaming for non-streaming TTS. |
| `tokenize.basic.SentenceTokenizer()` | Splits LLM text into sentences. |
| `openai.LLM.with_ollama(model=, base_url=)` | Local Ollama LLM. |
| `openai.STT(base_url=, model=)` / `openai.TTS(base_url=, model=)` | Reach local OpenAI-compatible Whisper / TTS servers. |
| `silero.VAD.load()` | Local in-process VAD (offline). |
| Events: `user_input_transcribed`, `conversation_item_added`, `agent_state_changed`, `user_state_changed` | Register with `@session.on("...")`. |
| `room.local_participant.set_attributes({...})` | Push SOP/safety/escalation state to the screen. |

Node signatures (custom local plugins):
```python
async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings) -> Optional[AsyncIterable[stt.SpeechEvent]]
async def llm_node(self, chat_ctx: llm.ChatContext, tools, model_settings) -> AsyncIterable[llm.ChatChunk]
async def tts_node(self, text: AsyncIterable[str], model_settings) -> AsyncIterable[rtc.AudioFrame]
async def transcription_node(self, text: AsyncIterable[str], model_settings) -> AsyncIterable[str]
```

---

## Gotchas

- **Version churn (important).** As of 2026-06-06 the latest stable is
  `livekit-agents==1.5.17`. The **current** entrypoint API — **CONFIRMED** against the
  `agents/start/voice-ai` quickstart and `examples/voice_agents/push_to_talk.py` on
  `main` — uses `AgentServer` + `@server.rtc_session(agent_name=...)` +
  `agents.cli.run_app(server)`, and does **not** call `await ctx.connect()` in the
  STT-LLM-TTS example. Many older tutorials (v1.0, April 2025) use the legacy form
  `cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))` with a plain
  `async def entrypoint(ctx)` and `await ctx.connect()` — that form is superseded; use
  `AgentServer` for new code (still worth a quick `pip show livekit-agents` sanity check
  on the box). The framework previously moved from `VoicePipelineAgent` (v0.x) to
  `AgentSession` (v1.0+); `VoicePipelineAgent`/`voice_assistant` examples are obsolete —
  do not use them.
- **Offline server keys.** `--dev` hardcodes `devkey`/`secret`. For a pinned box, use a
  `config.yaml` `keys:` map. The agent reads `LIVEKIT_URL/API_KEY/API_SECRET` from env;
  the frontend needs a token minted with the **same** secret (`lk token create`, fully
  offline). Keep `use_external_ip: false` on an air-gapped machine or ICE will stall
  trying to discover a public IP.
- **No cloud plugins.** Installing `livekit-plugins-deepgram` etc. is harmless but those
  paths phone home. For wifi-off, only `openai` (pointed at localhost), `silero`, and
  your custom nodes should be in the audio path. Do **not** use
  `openai.realtime.RealtimeModel` (the quickstart's default) — it requires the OpenAI
  cloud Realtime API and will fail offline.
- **No `with_faster_whisper`.** Not in 1.5.x's `openai` plugin (only `with_azure`,
  `with_ovhcloud`). Use `openai.STT(base_url=...)` against a local Whisper HTTP server,
  or a custom `stt_node`.
- **Whisper doesn't stream.** Whisper STT is non-streaming; it needs VAD to know when an
  utterance ends. With PTT that's the button (`commit_user_turn`), but still pass a
  `vad=` so endpointing works inside the captured window.
- **Streaming pitfall.** If you pass a non-streaming TTS directly, the framework may wrap
  it for you, but to control sentence segmentation (and thus first-word latency) wrap it
  explicitly in `StreamAdapter`. Conversely, a **streaming-capable** TTS that's wrongly
  wrapped loses native streaming — only wrap non-streaming engines.
- **Attributes vs data.** `set_attributes` is throttled server-side; don't use it for
  per-token transcript. Let LiveKit's transcription protocol carry the live transcript;
  use attributes only for the SOP card / safety / escalation state.
- **`agent_name` and dispatch.** With `@server.rtc_session(agent_name="manuai")` the
  agent only joins rooms it's dispatched to; for a single-operator kiosk, dispatch via
  the token's room config or omit `agent_name` to auto-join all rooms.

---

## Office-hours answers

**(a) Best path for a fully self-hosted pipeline with local STT/LLM/TTS?**
Run `livekit-server` locally (`--dev` or a pinned `config.yaml`, `use_external_ip:false`).
Use **local-inference plugins only**: `silero.VAD.load()` in-process; for STT/LLM/TTS
either point the `openai` plugin's `base_url` at localhost servers
(faster-whisper + `with_ollama` Qwen + Kokoro/Piper shim) — simplest — or override
`stt_node`/`llm_node`/`tts_node` to call MLX/faster-whisper/Kokoro directly in-process
(lowest overhead). Avoid `RealtimeModel` and all cloud plugins. Defer model details to
the whisper-stt, qwen, ollama/mlx, and local-tts skills.

**(b) Push-to-talk pattern?**
`AgentSession(turn_handling=TurnHandlingOptions(turn_detection="manual"))`, start with
`session.input.set_audio_enabled(False)`, and register `start_turn`/`end_turn`/
`cancel_turn` RPC methods that call `interrupt()` + `clear_user_turn()` +
`set_audio_enabled(True)` on press, and `set_audio_enabled(False)` + `commit_user_turn()`
on release. The button replaces automatic VAD turn-ending.

**(c) Sentence-level LLM -> TTS streaming hook?**
LiveKit streams `llm_node` output into `tts_node` automatically. For a non-streaming
local TTS, wrap it in `tts.StreamAdapter(tts=..., sentence_tokenizer=
tokenize.basic.SentenceTokenizer())` so the first sentence is synthesized and spoken
before the LLM finishes — the key to the <=1.5s budget.

---

## Related skills

- **whisper-stt** — local Whisper/faster-whisper/mlx-whisper for the `stt_node` or local STT server.
- **qwen** — local Qwen LLM + cite-or-refuse grounded-RAG prompt for `llm_node`.
- **ollama** — serving Qwen/embeddings via Ollama (`with_ollama` target).
- **mlx** — Apple-Silicon MLX inference (alternative in-process LLM/Whisper backend).
- **local-tts** — Kokoro/Piper for the `tts_node` / local TTS server.
- **moss** — local semantic retrieval to inject SOP context in `on_user_turn_completed`.
- **unsiloed** — one-time PDF->Markdown/JSON ingestion that feeds the Moss index (cloud, wifi-on).

---

## Docs + verification

- Agents intro: https://docs.livekit.io/agents/
- Agent sessions: https://docs.livekit.io/agents/build/session/
- Turns / push-to-talk: https://docs.livekit.io/agents/build/turns/
- Pipeline nodes: https://docs.livekit.io/agents/logic/nodes/
- Events: https://docs.livekit.io/agents/build/events/
- External data / RAG: https://docs.livekit.io/agents/build/external-data/
- Ollama plugin: https://docs.livekit.io/agents/models/llm/plugins/ollama/
- Self-hosting (local): https://docs.livekit.io/home/self-hosting/local/
- Frontend starter (React/Next.js): https://github.com/livekit-examples/agent-starter-react
- Examples (basic_agent.py, push_to_talk.py): https://github.com/livekit/agents/tree/main/examples/voice_agents
- StreamAdapter source: https://github.com/livekit/agents/blob/main/livekit-agents/livekit/agents/tts/stream_adapter.py

**Verified on 2026-06-06** against `livekit-agents==1.5.17` (latest stable; 1.6.0rc2 pre-release).

**Confirmed 2026-06-06** (voice-ai quickstart + `push_to_talk.py` on `main`):
`AgentServer` + `@server.rtc_session(agent_name=...)` + `agents.cli.run_app(server)` is the
current entrypoint; `turn_detection="manual"` is a direct `AgentSession` kwarg;
`await session.commit_user_turn(transcript_timeout=, stt_flush_duration=)` is the commit call.

**Unverified / re-confirm against your installed version:**
- Whether `agent_name` is *required* on `@server.rtc_session` for a single-operator kiosk,
  or whether you can omit it to auto-join all rooms.
- `TurnHandlingOptions` import path (the wrapper alternative to the direct kwarg) — confirm
  with `python -c "from livekit.agents import TurnHandlingOptions"` if you use it.
- `tokenize.basic.SentenceTokenizer` exact module path (could be `tokenize.blingfire`);
  the framework default wrap uses a blingfire tokenizer.
- `set_attributes` exact async signature and the SOP-card/escalation key conventions
  (these are ManuAI-defined, not LiveKit-defined).
- That your local Whisper/Kokoro servers expose OpenAI-compatible `/v1` routes the
  `openai` plugin expects; otherwise use the custom-node path.
- `session.say(...)` availability/signature in 1.5.17.
