# LiveKit Agents — ManuAI reference (deep detail)

Companion to `SKILL.md`. Verified on 2026-06-06 against `livekit-agents==1.5.17`.
This file holds (1) a fuller end-to-end offline agent, (2) custom-node bodies for
in-process local inference, and (3) the older-vs-current entrypoint forms.

---

## 1. Full offline ManuAI agent (OpenAI-compatible local servers)

Assumes you run, locally and offline:
- faster-whisper / whisper.cpp server at `http://127.0.0.1:9000/v1`
- `ollama serve` at `http://127.0.0.1:11434/v1` with `qwen2.5:7b-instruct` pulled
- Kokoro-FastAPI (or Piper OpenAI shim) at `http://127.0.0.1:8880/v1`
- `livekit-server --dev` (or a pinned config) at `ws://127.0.0.1:7880`

```python
import json
from livekit import agents, rtc
from livekit.agents import (
    AgentServer, AgentSession, Agent, TurnHandlingOptions,
    UserInputTranscribedEvent, ConversationItemAddedEvent,
    AgentStateChangedEvent,
)
from livekit.agents import tts as tts_mod, tokenize
from livekit.plugins import openai, silero

server = AgentServer()


# ---- ManuAI agent: grounded, cites SOPs, escalates on safety ------------------
class ManuAIAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are ManuAI, an offline voice copilot for factory operators. "
                "Answer ONLY from the SOP context provided in the conversation. "
                "If the context does not contain the answer, say you don't know and "
                "suggest escalation. Cite the SOP section. Keep sentences short for "
                "fast text-to-speech. Never invent safety procedures."
            )
        )

    async def on_user_turn_completed(self, turn_ctx, new_message):
        """Inject Moss-retrieved SOP context before the LLM runs, and push the SOP
        card / safety banner to the screen."""
        query = new_message.text_content or ""
        hits = moss_retrieve(query)            # <-- your Moss skill; returns chunks+meta
        if hits:
            turn_ctx.add_message(
                role="assistant",
                content=("SOP context for the next answer (cite the section):\n"
                         + hits.text),
            )
            await _push_screen(
                sop_card_id=hits.sop_id,
                citation=hits.citation,
                safety=("warn" if hits.is_safety_critical else "none"),
                escalation="none",
            )
        else:
            await _push_screen(sop_card_id="", citation="", safety="none",
                               escalation="suggested")


# ---- helper: low-frequency state to the screen via participant attributes -----
async def _push_screen(*, sop_card_id, citation, safety, escalation):
    room = agents.get_job_context().room
    await room.local_participant.set_attributes({
        "sop_card_id": sop_card_id,
        "citation": citation,
        "safety": safety,            # none | warn | stop  -> frontend ⚠ banner
        "escalation": escalation,    # none | suggested | requested | active
    })


@server.rtc_session(agent_name="manuai")
async def entrypoint(ctx: agents.JobContext):
    # Non-streaming local TTS -> wrap for sentence-by-sentence synthesis
    base_tts = openai.TTS(base_url="http://127.0.0.1:8880/v1", model="kokoro",
                          voice="af_heart")
    streaming_tts = tts_mod.StreamAdapter(
        tts=base_tts,
        sentence_tokenizer=tokenize.basic.SentenceTokenizer(),
    )

    session = AgentSession(
        stt=openai.STT(base_url="http://127.0.0.1:9000/v1", model="whisper-1",
                       language="en"),
        llm=openai.LLM.with_ollama(model="qwen2.5:7b-instruct",
                                   base_url="http://127.0.0.1:11434/v1"),
        tts=streaming_tts,
        vad=silero.VAD.load(min_silence_duration=0.4),
        turn_handling=TurnHandlingOptions(turn_detection="manual"),  # push-to-talk
    )

    # --- observability: mirror transcript + agent state -----------------------
    @session.on("user_input_transcribed")
    def _on_user(ev: UserInputTranscribedEvent):
        print(f"[user] {ev.transcript!r} final={ev.is_final}")

    @session.on("conversation_item_added")
    def _on_item(ev: ConversationItemAddedEvent):
        print(f"[{ev.item.role}] {ev.item.text_content}")

    @session.on("agent_state_changed")
    def _on_state(ev: AgentStateChangedEvent):
        # lk.agent.state attribute is updated automatically for the frontend
        print(f"[state] {ev.old_state} -> {ev.new_state}")

    await session.start(room=ctx.room, agent=ManuAIAgent())

    # Push-to-talk: stay silent until the operator presses the headset button
    session.input.set_audio_enabled(False)

    @ctx.room.local_participant.register_rpc_method("start_turn")
    async def start_turn(data: rtc.RpcInvocationData):
        session.interrupt()              # barge-in: cut off any current answer
        session.clear_user_turn()        # drop stale buffered audio
        session.input.set_audio_enabled(True)
        return "listening"

    @ctx.room.local_participant.register_rpc_method("end_turn")
    async def end_turn(data: rtc.RpcInvocationData):
        session.input.set_audio_enabled(False)
        session.commit_user_turn()       # finalize -> STT -> Moss -> LLM -> TTS
        return "thinking"

    @ctx.room.local_participant.register_rpc_method("cancel_turn")
    async def cancel_turn(data: rtc.RpcInvocationData):
        session.input.set_audio_enabled(False)
        session.clear_user_turn()        # discard, no response
        return "cancelled"

    @ctx.room.local_participant.register_rpc_method("escalate")
    async def escalate(data: rtc.RpcInvocationData):
        await _push_screen(sop_card_id="", citation="", safety="stop",
                           escalation="active")
        return "escalated"


if __name__ == "__main__":
    agents.cli.run_app(server)
```

Run:
```bash
export LIVEKIT_URL="ws://127.0.0.1:7880" LIVEKIT_API_KEY="devkey" LIVEKIT_API_SECRET="secret"
python agent.py dev
```

---

## 2. Custom in-process nodes (no local HTTP servers)

When you'd rather call MLX / faster-whisper / Kokoro **in the agent process** (lower
latency, no extra servers), override the node methods. Each yields the same types the
default produces, so the rest of the pipeline is unchanged.

```python
from typing import AsyncIterable, Optional
from livekit import rtc
from livekit.agents import Agent, ModelSettings, stt, llm

class LocalManuAIAgent(Agent):

    async def stt_node(
        self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings
    ) -> Optional[AsyncIterable[stt.SpeechEvent]]:
        # Buffer frames for the captured (push-to-talk) window, run faster-whisper /
        # mlx-whisper on the utterance, then emit a final transcript event.
        # Pseudocode:
        #   pcm = await collect(audio)
        #   text = whisper_transcribe(pcm)            # whisper-stt skill
        #   yield stt.SpeechEvent(
        #       type=stt.SpeechEventType.FINAL_TRANSCRIPT,
        #       alternatives=[stt.SpeechData(text=text, language="en")])
        ...

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools, model_settings: ModelSettings
    ) -> AsyncIterable[llm.ChatChunk]:
        # Stream tokens from local Qwen (mlx-lm or Ollama). Yield str for plain text,
        # or llm.ChatChunk for text + optional tool calls. Yield as tokens arrive so
        # the tts_node can begin on the first sentence.
        #   async for tok in qwen_stream(chat_ctx):   # qwen skill
        #       yield tok
        ...

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[rtc.AudioFrame]:
        # Accumulate text into sentences, synthesize each with Kokoro/Piper as soon as
        # it completes, and yield rtc.AudioFrame. To reuse framework sentence handling,
        # you can instead set a StreamAdapter-wrapped TTS on the session and NOT override
        # this node. If overriding, drive your own SentenceTokenizer:
        #   tok = tokenize.basic.SentenceTokenizer()
        #   ... feed text, on each sentence -> kokoro_synthesize -> yield frames
        ...

    async def transcription_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        # Optional: clean up agent transcript text before it's forwarded to the screen
        # (e.g. strip markdown). Delegate to default if you only tweak part:
        async for chunk in Agent.default.transcription_node(self, text, model_settings):
            yield chunk
```

Mix freely: e.g. custom `stt_node` + `tts_node` for in-process Whisper/Kokoro, but keep
`llm=openai.LLM.with_ollama(...)` on the `AgentSession` for the LLM. A node override on
the `Agent` takes precedence over the matching component passed to `AgentSession`.

---

## 3. Entrypoint: current vs older form

**Current (1.5.x / docs `main`) — preferred:**
```python
from livekit.agents import AgentServer
server = AgentServer()

@server.rtc_session(agent_name="manuai")
async def entrypoint(ctx): ...

if __name__ == "__main__":
    agents.cli.run_app(server)
```

**Older (v1.0, April 2025) — still seen in many tutorials; may still run in 1.5.17:**
```python
from livekit import agents
from livekit.agents import AgentSession, Agent

async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()                 # note the explicit connect in the old form
    session = AgentSession(stt=..., llm=..., tts=..., vad=...)
    await session.start(room=ctx.room, agent=Agent(instructions="..."))
    await session.generate_reply(instructions="Greet the user.")

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
```

If `from livekit.agents import AgentServer` fails on your installed version, fall back to
the `WorkerOptions` form. Check with `pip show livekit-agents` and the examples at the
tag matching your version: `https://github.com/livekit/agents/releases`.

---

## 4. Frontend (the operator screen)

- Scaffold: `https://github.com/livekit-examples/agent-starter-react` (Next.js + LiveKit
  Agents UI components). Runs locally; point it at `ws://127.0.0.1:7880` with a token
  minted offline via `lk token create`.
- Live transcript: provided automatically by LiveKit's transcription protocol; render
  with the starter's transcript components / `useVoiceAssistant`.
- SOP card / citation / safety banner / escalation: read the agent participant's custom
  attributes (`sop_card_id`, `citation`, `safety`, `escalation`) via
  `useParticipantAttributes` and the auto-managed `lk.agent.state`
  (`initializing|listening|thinking|speaking`).
- PTT button: call the agent's `start_turn` / `end_turn` / `cancel_turn` RPC methods on
  button down/up; call `escalate` to flip the screen into supervisor mode.

For an air-gapped kiosk, build the Next.js app to static/standalone output and serve it
locally so the screen needs no internet at runtime.

---

## 5. Self-hosting cheat-sheet

```bash
# offline dev server
livekit-server --dev                       # devkey/secret, ws://127.0.0.1:7880

# pinned server
livekit-server --config config.yaml        # see SKILL.md for config.yaml

# offline token for the screen
lk token create --api-key devkey --api-secret secret \
  --join --room manuai --identity operator-1 --valid-for 24h

# docker (host networking for media perf)
docker run -d --network host \
  -v $(pwd)/config.yaml:/etc/livekit/config.yaml \
  livekit/livekit-server --config /etc/livekit/config.yaml
```

Ports: 7880 (HTTP/WS signal), 7881 (RTC/TCP), 50000-60000/UDP (RTC media; `--dev` uses a
smaller range). Keep `rtc.use_external_ip: false` on an air-gapped box.
