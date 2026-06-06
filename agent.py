"""ManuAI Phase 3 — LiveKit push-to-talk voice agent.

ARCHITECTURE
============
    mic (push-to-talk) → LiveKit room → Whisper STT → core.answer(transcript)
                                                  ↓
                         speaker (TTS) ← state["answer"]
                                                  ↓
                         data channel → Phase 2 screen (full screen_state JSON)

The livekit-agents orchestration shell (AgentServer / AgentSession) handles
audio transport, push-to-talk, and STT → TTS plumbing.  The BRAIN is
``core.answer``:  it retrieves SOPs, gates, calls Ollama, and returns a
complete ``screen_state`` dict (ARCHITECTURE.md §3b).  The ``llm_node``
override below is where these two worlds are joined.

HOW TO RUN
==========
1.  Install deps (python ≥ 3.10, Apple Silicon):

        pip install -r requirements.txt

2.  Pre-pull model weights while wifi is ON (do this at venue setup):

        # Whisper (STT)
        python -c "import mlx_whisper; mlx_whisper.transcribe.__doc__"
        # model weights are fetched on first use from HF; set WHISPER_MODEL first.

        # Kokoro ONNX weights (TTS) — download manually:
        # https://github.com/thewh1teagle/kokoro-onnx/releases (model-files-v1.0)
        #   kokoro-v1.0.onnx  (~310 MB)   → place in project root or set KOKORO_MODEL_PATH
        #   voices-v1.0.bin               → place in project root or set KOKORO_VOICES_PATH

3.  Start the local LiveKit server (no cloud, no internet):

        livekit-server --dev
        # API key:    devkey
        # API secret: secret
        # URL:        ws://127.0.0.1:7880

4.  Start Ollama (must be running before the agent starts):

        ollama serve
        # Then confirm Qwen is pulled: ollama run qwen2.5:7b-instruct

5.  (Optional) Start a local Whisper OpenAI-compatible server if you prefer
    the server model over in-process mlx-whisper.  The default below uses
    the openai plugin pointing at STT_BASE_URL.

6.  (Optional) Start a local Kokoro/Piper OpenAI-compatible TTS server if
    you prefer the server model over in-process kokoro-onnx.  Default below
    points at TTS_BASE_URL.

7.  Start the agent worker:

        python agent.py dev
        # or: python agent.py start   (production worker pool mode)

8.  Connect the Phase 2 frontend (screen.html) via LiveKit room "manuai".
    Generate a frontend token offline:

        lk token create --api-key devkey --api-secret secret \\
          --join --room manuai --identity operator-1 --valid-for 24h

9.  Push-to-talk from the frontend:  hold the PTT button → speak → release.
    The agent transcribes, queries core.answer, speaks the reply, and pushes
    the full screen_state JSON over the room data channel.

WIFI-OFF GATE (G1 — REQUIRED BEFORE ANY REHEARSAL)
====================================================
Complete at least ONE successful push-to-talk round-trip with **wifi
physically switched off** before trusting the setup.  The demo headline
depends on this.

Sequence:
  a) While wifi ON:  authenticate Moss (if RETRIEVER=moss) and run
     `moss_offline_test.py --coldload` to warm the Moss session.
  b) Turn wifi OFF at the physical switch.
  c) Run a full push-to-talk question → spoken answer → screen update.
  d) If anything reaches the network (DNS, beacon, HF Hub download), it
     must not block the round-trip.

FALLBACK LADDER
===============
  R1 (default)  full local voice:  Whisper + Kokoro + self-hosted LiveKit
  R2            typed input in the Phase 2 frontend (core.answer still works)
  R3            cloud STT/TTS:  set TTS_ENGINE=openai-cloud / STT cloud flag
                (forfeits the "wifi off" headline)
  R4            recorded backup video (M1 terminal, wifi-off visible)

ARCHITECTURE NOTES
==================
- `core.answer` IS the LLM (retrieval + gate + Ollama + assembly).  We
  override ``llm_node`` to call it; the ``llm=`` kwarg on AgentSession is a
  *structural placeholder only* (see note in _build_session).
- ``core.answer`` already calls Ollama via ``asyncio.to_thread`` (G9 fixed).
  Do NOT re-wrap in another thread.
- LIVEKIT_URL defaults to ws://127.0.0.1:7880 — never a cloud URL (G1).
- screen_state is published once per turn over the LiveKit data channel so the
  Phase 2 screen.html can call applyState(JSON.parse(data)) identically to the
  SSE path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env BEFORE any other imports that read env-vars.
# We reuse retriever.load_env (stdlib only, no python-dotenv needed).
# ---------------------------------------------------------------------------
from retriever import CosineRetriever, MossRetriever, make_client, load_env

load_env()  # idempotent; os.environ.setdefault — does not overwrite shell env

# ---------------------------------------------------------------------------
# G1 safety: force the local server URL if it has not been set.
# This MUST default to 127.0.0.1 — never a cloud URL.
# ---------------------------------------------------------------------------
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "secret")

# ---------------------------------------------------------------------------
# Now import LiveKit — plugins are NOT installed in dev/scaffold mode.
# Each livekit import block carries a TODO(needs-hardware) marker so the
# installer knows exactly which lines require the packages from requirements.txt.
# ---------------------------------------------------------------------------
# TODO(needs-hardware): install livekit-agents~=1.5.17, livekit-plugins-silero,
#   livekit-plugins-openai  (see requirements.txt)
from livekit import agents, rtc  # noqa: E402
from livekit.agents import (  # noqa: E402
    AgentServer,
    AgentSession,
    Agent,
    ModelSettings,
)
from livekit.agents import tts as tts_mod, tokenize  # noqa: E402
from livekit.agents import stt as stt_mod  # noqa: E402
from livekit.agents import llm as llm_mod  # noqa: E402

# TODO(needs-hardware): livekit-plugins-openai — provides STT/TTS that talk to
#   local OpenAI-compatible servers (Whisper at STT_BASE_URL, Kokoro at TTS_BASE_URL).
from livekit.plugins import openai as lk_openai  # noqa: E402

# TODO(needs-hardware): livekit-plugins-silero — local ONNX VAD, runs in-process,
#   no network.  Required for push-to-talk endpointing inside the captured window.
from livekit.plugins import silero  # noqa: E402

import core  # core.answer(question, machine_id, retriever) -> screen_state

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("manuai.agent")


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
LIVEKIT_URL = os.environ["LIVEKIT_URL"]  # already defaulted above

MACHINE_ID = os.environ.get("MACHINE_ID", "labeler-line3")  # §12d: no dash before "3"

# Retriever selection: stub (CosineRetriever, offline-bulletproof) or moss
RETRIEVER_ENV = os.environ.get("RETRIEVER", "stub").strip().lower()

# Whisper STT config — for the openai plugin pointing at a local server.
# If you run mlx-whisper in-process instead, override stt_node (see below).
STT_BASE_URL = os.environ.get("STT_BASE_URL", "http://127.0.0.1:9000/v1")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-small")

# TTS config — for the openai plugin pointing at a local Kokoro/Piper server.
TTS_BASE_URL = os.environ.get("TTS_BASE_URL", "http://127.0.0.1:8880/v1")
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
TTS_ENGINE = os.environ.get("TTS_ENGINE", "kokoro")  # kokoro | piper | openai-cloud

# Kokoro ONNX in-process paths (used when TTS_ENGINE=kokoro and no TTS server is running)
KOKORO_MODEL_PATH = os.environ.get("KOKORO_MODEL_PATH", "kokoro-v1.0.onnx")
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", "voices-v1.0.bin")

# Room name (the data channel is scoped to this room)
ROOM_NAME = os.environ.get("ROOM_NAME", "manuai")

# Data-channel topic string — Phase 2 screen.html should listen for this topic.
SCREEN_STATE_TOPIC = "screen_state"


# ---------------------------------------------------------------------------
# Retriever factory
# ---------------------------------------------------------------------------
def _make_retriever():
    """Return the configured retriever.

    - RETRIEVER=stub  (default) → CosineRetriever  — disk-local, wifi-off safe always.
    - RETRIEVER=moss             → MossRetriever    — sponsor-tech path; requires
                                                      load_index to be called while
                                                      wifi is ON (ARCHITECTURE.md §12a).

    The retriever is constructed ONCE at worker startup and reused across all turns
    (index.json is loaded once by CosineRetriever; Moss keeps its session alive).
    """
    if RETRIEVER_ENV == "moss":
        logger.info("Retriever: Moss (load_index will run on first search; keep wifi ON)")
        client = make_client()
        index_name = os.environ.get("MOSS_INDEX_NAME", "manuals")
        return MossRetriever(client, index_name)
    else:
        if RETRIEVER_ENV != "stub":
            logger.warning("Unknown RETRIEVER=%r; falling back to stub (CosineRetriever)", RETRIEVER_ENV)
        logger.info("Retriever: CosineRetriever (offline-bulletproof, disk-local)")
        return CosineRetriever()


# ---------------------------------------------------------------------------
# ManuAI voice agent
# ---------------------------------------------------------------------------
class ManuAIAgent(Agent):
    """LiveKit Agent that routes every transcribed turn through core.answer.

    The brain (retrieval + gate + Ollama + assembly) lives in core.answer — this
    class is pure orchestration: receive transcript → call brain → speak answer →
    publish screen_state.

    llm_node is the single join-point between the LiveKit pipeline and core.answer.
    """

    def __init__(self, machine_id: str, retriever) -> None:
        super().__init__(
            # Instructions are provided so the agent has a persona for any framework
            # calls that bypass llm_node (e.g. generate_reply greeting).  They are NOT
            # the grounding mechanism — that lives in core.SYSTEM.
            instructions=(
                "You are ManuAI, an offline voice assistant for factory operators. "
                "Keep answers short, clear, and safety-first. "
                "If you cannot answer from provided SOP context, escalate to a supervisor."
            )
        )
        self.machine_id = machine_id
        self.retriever = retriever

    async def llm_node(
        self,
        chat_ctx: llm_mod.ChatContext,
        tools,
        model_settings: ModelSettings,
    ):
        """Override the LLM step.  Instead of forwarding to an LLM service, we call
        core.answer which already embeds (CosineRetriever) / queries Moss, gates,
        calls Ollama, and returns the full screen_state.

        The transcript is extracted from the last user message in chat_ctx.
        We yield state["answer"] as a single text chunk so the TTS node can
        begin speaking immediately (no streaming latency here — core.answer is
        synchronous over Ollama; the spoken answer is typically 1-2 sentences
        and appears in full).

        screen_state is published once per turn via the room data channel so the
        Phase 2 screen.html receives the full state (§3b) identically to the SSE path.

        Assumptions flagged:
        - chat_ctx iteration API: we iterate messages assuming they have .role and
          .text_content (or similar) attributes.  Verify against your installed
          livekit-agents version.
        - yield type: we yield a plain str.  In 1.5.x llm_node may expect
          llm.ChatChunk objects; if so, wrap as llm_mod.ChatChunk(delta=llm_mod.ChoiceDelta(role="assistant", content=text)).
          Flag in report.
        - publish_data: called with keyword args reliable=True and topic=SCREEN_STATE_TOPIC.
          Verify the exact signature of room.local_participant.publish_data in your version.
        """
        # --- Extract transcript from the latest user turn ----------------------
        transcript = _extract_transcript(chat_ctx)
        if not transcript:
            # No user text — yield an empty string to avoid hanging the pipeline.
            logger.warning("llm_node: no transcript found in chat_ctx; skipping turn")
            return
        logger.info("llm_node: transcript=%r machine_id=%r", transcript, self.machine_id)

        # --- Call core.answer (the brain) -------------------------------------
        # core.answer already runs sync Ollama calls via asyncio.to_thread (G9).
        # Do NOT re-wrap in to_thread — it is already async-safe.
        state = await core.answer(transcript, self.machine_id, self.retriever)

        logger.info(
            "llm_node: status=%r top_score=%.3f answer=%r",
            state.get("status"),
            state.get("top_score", 0.0),
            state.get("answer", "")[:80],
        )

        # --- Publish full screen_state over the data channel ------------------
        # The Phase 2 screen.html listens for this and calls applyState(parsed).
        # Published once per turn.  TODO(needs-hardware): room.local_participant
        # must be connected (livekit-server --dev must be running).
        await _publish_screen_state(state)

        # --- Yield the spoken text to the TTS pipeline ------------------------
        # state["answer"] carries the reply in BOTH statuses (answered + escalated).
        # On escalation, core._escalated sets answer=reason — no special-casing needed.
        spoken = state.get("answer", "")
        if spoken:
            # TODO(needs-hardware): verify that yielding a plain str from llm_node is
            # correct for livekit-agents==1.5.17.  If it expects llm.ChatChunk, wrap:
            #   yield llm_mod.ChatChunk(delta=llm_mod.ChoiceDelta(role="assistant", content=spoken))
            yield spoken


def _extract_transcript(chat_ctx: llm_mod.ChatContext) -> str:
    """Extract the latest user-turn text from a ChatContext.

    Iterates messages in reverse; returns the first user message's text.

    TODO(needs-hardware): verify that ChatContext is iterable and that individual
    messages expose .role and .text_content (or similar) in livekit-agents==1.5.17.
    The exact attribute names may differ — check against the installed package.
    """
    try:
        # Attempt common attribute names; real verification requires installed deps.
        # Try both known attribute names: .messages (primary) and .items (fallback).
        # The whole round-trip depends on this; if both are absent the turn is a no-op.
        _msg_source = getattr(chat_ctx, "messages", None) or getattr(chat_ctx, "items", None) or []
        messages = list(_msg_source)
        for msg in reversed(messages):
            role = getattr(msg, "role", None)
            if role in ("user", "human"):
                # text_content is used in the reference.md examples
                text = getattr(msg, "text_content", None)
                if text is None:
                    # fallback: some versions use .content (a str or list of parts)
                    raw = getattr(msg, "content", None)
                    if isinstance(raw, str):
                        text = raw
                    elif isinstance(raw, list):
                        # content-part list: concatenate text parts
                        text = " ".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in raw
                        ).strip()
                return (text or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("_extract_transcript: error reading chat_ctx: %s", exc)
    return ""


async def _publish_screen_state(state: dict) -> None:
    """Publish the full screen_state dict over the LiveKit room data channel.

    The Phase 2 screen.html receives this as a Uint8Array, parses JSON, and calls
    applyState(state) — identical to the SSE path (ARCHITECTURE.md §4).

    Called once per turn, after core.answer returns the complete state.

    TODO(needs-hardware): requires livekit-server to be running and the agent to
    have joined a room.  agents.get_job_context() returns None if called outside
    an active job — guard with a try/except or a None check.

    Flagged assumption: publish_data signature.  In 1.5.x the expected form is:
        await room.local_participant.publish_data(
            payload: bytes,
            reliable: bool = True,
            topic: str | None = None,
        )
    Verify this matches your installed livekit SDK.
    """
    try:
        ctx = agents.get_job_context()
        if ctx is None:
            logger.warning("_publish_screen_state: no active job context; skipping publish")
            return
        payload = json.dumps(state).encode("utf-8")
        await ctx.room.local_participant.publish_data(
            payload,
            reliable=True,
            topic=SCREEN_STATE_TOPIC,
        )
        logger.debug("Published screen_state (%d bytes) topic=%r", len(payload), SCREEN_STATE_TOPIC)
    except Exception as exc:  # noqa: BLE001
        # A publish failure must NOT crash the voice pipeline; the screen is best-effort.
        logger.error("_publish_screen_state: failed: %s", exc)


# ---------------------------------------------------------------------------
# AgentSession builder
# ---------------------------------------------------------------------------
def _build_session(retriever) -> AgentSession:
    """Construct the AgentSession with local STT / TTS / VAD.

    STT and TTS use the livekit-plugins-openai client pointing at local
    OpenAI-compatible servers (Whisper at STT_BASE_URL, Kokoro/Piper at TTS_BASE_URL).
    This is the "Option A" path from the livekit-agents skill: the openai plugin
    talks to local OpenAI-compatible HTTP servers you run separately.  This path
    does NOT use the pinned in-process deps (mlx-whisper, kokoro-onnx) from
    requirements.txt — those back the "Option B" custom-node path (reference.md §2).
    See report for the full dep-path distinction.  Option A is chosen here because it
    is the recommended start per the skill and produces less unverifiable custom code.

    NOTE on llm= kwarg
    ------------------
    The llm= arg is a *structural placeholder* to satisfy a possible required-arg
    check in AgentSession.__init__.  The actual LLM call is intercepted and replaced
    by core.answer inside ManuAIAgent.llm_node above.  The openai.LLM.with_ollama
    instance here will NEVER produce a spoken response — llm_node completely takes
    over before any tokens reach the TTS stage.

    TODO(needs-hardware): All three components below require:
      - livekit-plugins-openai and livekit-plugins-silero installed
      - A local Whisper OpenAI-compatible server running at STT_BASE_URL
        (e.g. faster-whisper-server, whisper.cpp with OpenAI endpoint)
      - A local Kokoro or Piper OpenAI-compatible server at TTS_BASE_URL
        (e.g. kokoro-fastapi, piper-tts shim)
      - Ollama running at OLLAMA_BASE_URL with the LLM model pulled
      - livekit-server --dev (or production config) running at LIVEKIT_URL
    """
    # TODO(needs-hardware): openai.STT requires livekit-plugins-openai installed
    #   and a Whisper-compatible server at STT_BASE_URL.
    stt = lk_openai.STT(
        base_url=STT_BASE_URL,
        model="whisper-1",  # the model name the local server expects (usually "whisper-1")
        language="en",       # pin to English — faster + avoids language-detection errors
    )

    # Wrap TTS in StreamAdapter for sentence-by-sentence synthesis so the operator
    # hears the first sentence while the rest is still being generated.
    # TODO(needs-hardware): openai.TTS requires livekit-plugins-openai installed
    #   and a TTS-compatible server at TTS_BASE_URL.
    #
    # Flagged assumption: tokenize.basic.SentenceTokenizer import path.  The skill
    # docs say this is correct for 1.5.x but flag it may be tokenize.blingfire;
    # verify with: python -c "from livekit.agents import tokenize; print(dir(tokenize))"
    base_tts = lk_openai.TTS(
        base_url=TTS_BASE_URL,
        model=TTS_ENGINE,   # "kokoro" or "piper" — model name your local server expects
        voice=TTS_VOICE,
    )
    streaming_tts = tts_mod.StreamAdapter(
        tts=base_tts,
        sentence_tokenizer=tokenize.basic.SentenceTokenizer(),
    )

    # TODO(needs-hardware): silero.VAD requires livekit-plugins-silero installed.
    #   It runs in-process via ONNX — offline-safe, no network.
    vad = silero.VAD.load(
        min_silence_duration=0.4,  # seconds of silence after speech ends the window
    )

    ollama_base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    llm_model = os.environ.get("LLM_MODEL", "qwen2.5:7b-instruct")

    # Structural LLM placeholder — see docstring above.
    # TODO(needs-hardware): verify openai.LLM.with_ollama signature and that omitting
    #   llm= is NOT allowed by AgentSession in your installed version.
    _llm_placeholder = lk_openai.LLM.with_ollama(
        model=llm_model,
        base_url=f"{ollama_base_url}/v1",
    )

    # Flagged assumption: turn_detection="manual" is a direct kwarg to AgentSession.
    # The skill docs confirm this pattern for 1.5.x; TurnHandlingOptions wrapper is
    # the alternative.  If this kwarg is rejected, try:
    #   from livekit.agents import TurnHandlingOptions
    #   turn_handling=TurnHandlingOptions(turn_detection="manual")
    session = AgentSession(
        stt=stt,
        llm=_llm_placeholder,
        tts=streaming_tts,
        vad=vad,
        turn_detection="manual",  # push-to-talk: disable automatic VAD turn-ending
    )
    return session


# ---------------------------------------------------------------------------
# AgentServer entrypoint (current 1.5.x form)
# ---------------------------------------------------------------------------
server = AgentServer()


@server.rtc_session(agent_name="manuai")
async def entrypoint(ctx: agents.JobContext):
    """Lifecycle of one operator session.

    1. Build the retriever once (warm it here if Moss).
    2. Build the AgentSession (STT/TTS/VAD wiring).
    3. Start the session in the LiveKit room.
    4. Disable the mic — wait for PTT.
    5. Register RPC methods so the Phase 2 frontend can drive PTT and escalate.
    6. (Optional) warm up TTS with a silent synthesize to avoid first-call latency.
    7. Send a brief greeting.

    Flagged assumption: @server.rtc_session(agent_name="manuai") requires the
    frontend to dispatch to the "manuai" agent.  For a single-operator kiosk you
    may omit agent_name to auto-join all rooms — confirm against your config.

    TODO(needs-hardware): everything in this function requires livekit-server running
    and all plugins installed (STT server, TTS server, Ollama, Silero).
    """
    logger.info("Session starting: room=%r machine_id=%r retriever=%r",
                ctx.room.name, MACHINE_ID, RETRIEVER_ENV)

    retriever = _make_retriever()

    session = _build_session(retriever)
    agent = ManuAIAgent(machine_id=MACHINE_ID, retriever=retriever)

    # Start the session (connects audio/data subscriptions in the room)
    await session.start(room=ctx.room, agent=agent)

    # Push-to-talk: keep the mic silent until the operator holds the button.
    session.input.set_audio_enabled(False)

    # ----- Observability -----
    @session.on("user_input_transcribed")
    def _on_user_transcript(ev):
        # ev.transcript = transcribed text; ev.is_final = final vs interim
        logger.info("[STT] transcript=%r final=%s", ev.transcript, ev.is_final)

    @session.on("conversation_item_added")
    def _on_item(ev):
        logger.info("[%s] %s", ev.item.role, (ev.item.text_content or "")[:120])

    @session.on("agent_state_changed")
    def _on_state(ev):
        # lk.agent.state participant attribute is updated automatically by the framework.
        logger.info("[state] %s → %s", ev.old_state, ev.new_state)

    # ----- Push-to-talk RPC methods -----
    # The frontend calls these via performRpc() when the PTT button is pressed/released.
    # Flagged assumption: register_rpc_method is called on local_participant and the
    # decorated coroutine receives an rtc.RpcInvocationData object.  Verify in 1.5.x.

    @ctx.room.local_participant.register_rpc_method("start_turn")
    async def start_turn(data: rtc.RpcInvocationData) -> str:
        """PTT button pressed: barge-in on any current speech, open mic."""
        # TODO(needs-hardware): session.interrupt / clear_user_turn / set_audio_enabled
        #   require an active AgentSession connected to a running livekit-server.
        session.interrupt()          # cut off any in-progress TTS playback (barge-in)
        session.clear_user_turn()    # discard any stale buffered audio from prior turn
        session.input.set_audio_enabled(True)
        logger.info("[PTT] start_turn: mic ON")
        return "listening"

    @ctx.room.local_participant.register_rpc_method("end_turn")
    async def end_turn(data: rtc.RpcInvocationData) -> str:
        """PTT button released: close mic, finalize audio, trigger STT→core.answer→TTS."""
        # TODO(needs-hardware): commit_user_turn triggers the STT pipeline; requires
        #   a connected Whisper server at STT_BASE_URL and livekit-server running.
        session.input.set_audio_enabled(False)
        # Flagged assumption: commit_user_turn takes these kwargs in 1.5.x.
        # Confirmed in push_to_talk.py example (livekit-agents skill reference.md).
        await session.commit_user_turn(
            transcript_timeout=5.0,    # seconds to wait for final STT result
            stt_flush_duration=2.0,    # seconds after VAD silence before forcing flush
        )
        logger.info("[PTT] end_turn: committed → STT → core.answer → TTS")
        return "thinking"

    @ctx.room.local_participant.register_rpc_method("cancel_turn")
    async def cancel_turn(data: rtc.RpcInvocationData) -> str:
        """PTT cancelled mid-press (e.g. accidental): discard audio, stay quiet."""
        session.input.set_audio_enabled(False)
        session.clear_user_turn()
        logger.info("[PTT] cancel_turn: discarded")
        return "cancelled"

    # ----- Greeting -----
    # generate_reply uses the framework LLM — which in our case hits the placeholder
    # llm_node (core.answer) with no user transcript, which returns safely.
    # Alternatively, use session.say() to speak a fixed string without the LLM.
    #
    # Flagged assumption: session.say() availability in 1.5.17.  If unavailable,
    # use generate_reply(instructions="Greet the operator briefly.") instead.
    try:
        await session.say(
            "ManuAI ready. Hold the talk button and speak your question."
        )
    except AttributeError:
        # session.say may not exist in all 1.5.x builds; fall back to generate_reply.
        await session.generate_reply(
            instructions="Greet the factory operator in one short sentence."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Flagged assumption: agents.cli.run_app(server) is the correct 1.5.x runner.
    # If AgentServer is not importable in your installed version, fall back to:
    #   agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
    # after converting @server.rtc_session to a plain async def entrypoint(ctx) with
    # await ctx.connect() at the top.
    agents.cli.run_app(server)
