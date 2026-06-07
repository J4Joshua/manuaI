"""ManuAI Phase 3 — LiveKit push-to-talk voice agent (livekit-agents 1.5.17).

ARCHITECTURE
============
    mic (push-to-talk) → LiveKit room → mlx-whisper STT → core.answer(transcript)
                                                  ↓
                         speaker (Kokoro TTS) ← state["answer"]
                                                  ↓
                         data channel (topic "screen_state") → Phase 2 screen.html

The BRAIN is ``core.answer``: it retrieves SOPs (offline MossRetriever), gates,
calls Ollama (qwen2.5:3b), and returns a complete ``screen_state`` dict
(ARCHITECTURE.md §3b). We intercept the LiveKit LLM step via ``Agent.llm_node`` and
run ``core.answer`` there — no external LLM ever produces the spoken answer.

STT and TTS are FULLY LOCAL, in-process, no HTTP servers:
  * STT  — ``MlxWhisperSTT`` wraps ``mlx_whisper.transcribe`` (whisper-small-mlx).
  * TTS  — ``KokoroTTS`` wraps ``kokoro_onnx.Kokoro`` (kokoro-v1.0.onnx + voices-v1.0.bin).
  * VAD  — ``silero.VAD`` (in-process ONNX endpointing).
These are the SAME wrappers proven mic-free by ``voice_smoke.py``.

HOW TO RUN
==========
1.  Models on disk (wifi ON for the first pull):
        # Kokoro TTS weights -> models/  (one-time download):
        #   models/kokoro-v1.0.onnx   (~325 MB)
        #   models/voices-v1.0.bin    (~28 MB)
        #   from https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0
        # Whisper STT weights are pulled from HF on first transcribe
        #   (mlx-community/whisper-small-mlx) into the HF cache.

2.  Prove the local stack with NO mic / NO LiveKit (do this first):
        .venv/bin/python src/voice_smoke.py
        # jam -> answered + SOP-1187 ; bypass -> escalated

3.  Verify the agent builds (STT/TTS/VAD/session construct, no mic):
        .venv/bin/python src/agent.py check

4.  Start the self-hosted LiveKit server (no cloud, no internet):
        livekit-server --dev
        #   URL ws://127.0.0.1:7880   key devkey   secret secret

5.  Start Ollama with the model the brain uses (common.py -> qwen2.5:3b):
        ollama serve            # then: ollama run qwen2.5:3b   (to pull once)

6.  Start the agent worker (registers with the local LiveKit server):
        .venv/bin/python src/agent.py dev
        # or:  .venv/bin/python src/agent.py start   (production worker mode)

7.  Connect the Phase 2 frontend (screen.html) into LiveKit room "manuai" and
    hold the push-to-talk button to speak. A frontend token, generated offline:
        lk token create --api-key devkey --api-secret secret \
          --join --room manuai --identity operator-1 --valid-for 24h

WIFI-OFF GATE (G1)
==================
Everything above is local. LIVEKIT_URL defaults to ws://127.0.0.1:7880 (never a
cloud URL). Once the Kokoro + Whisper weights are cached, the whole round-trip runs
with wifi physically off. Complete one wifi-off push-to-talk round-trip before
trusting the setup.

NOTES (corrected against the REAL 1.5.17 API)
=============================================
- ``core.answer`` IS the LLM. We override ``llm_node``. The ``llm=`` kwarg on
  AgentSession is a STRUCTURAL placeholder: livekit-agents' AgentActivity guards
  generation on ``self.llm`` (voice/agent_activity.py) — if ``llm`` is unset the
  llm_node is NOT scheduled and the agent would be mute. So we pass a never-called
  ``openai.LLM.with_ollama`` instance to keep the node firing; ``llm_node`` replaces
  its body before any token reaches TTS.
- ``core.answer`` already runs the sync Ollama call via ``asyncio.to_thread`` (G9).
  We do NOT re-wrap it.
- screen_state is published once per turn over the LiveKit data channel so
  screen.html can ``applyState(JSON.parse(data))`` exactly like the SSE path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import AsyncIterable

import numpy as np

# ---------------------------------------------------------------------------
# Load .env BEFORE importing anything that reads env-vars (reuse retriever's
# stdlib-only loader — no python-dotenv needed; it is os.environ.setdefault).
# ---------------------------------------------------------------------------
from retriever import make_retriever, load_env

load_env()

# An EMPTY HF_TOKEN (the .env ships it blank) makes huggingface_hub send an empty
# "Authorization: Bearer " header → HF answers 401 even for PUBLIC repos. Drop it so
# the first-time Whisper weight pull works anonymously (harmless if a real token is set).
if not (os.environ.get("HF_TOKEN") or "").strip():
    os.environ.pop("HF_TOKEN", None)

# ---------------------------------------------------------------------------
# G1 safety: force the LOCAL server URL/creds if not already set — never cloud.
# ---------------------------------------------------------------------------
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "devkey")
os.environ.setdefault("LIVEKIT_API_SECRET", "secret")

from livekit import agents, rtc  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    ModelSettings,
    TurnHandlingOptions,
)
from livekit.agents import stt as stt_mod  # noqa: E402
from livekit.agents import tts as tts_mod  # noqa: E402
from livekit.agents import llm as llm_mod  # noqa: E402
from livekit.agents.utils.audio import combine_frames  # noqa: E402
from livekit.plugins import openai as lk_openai  # noqa: E402
from livekit.plugins import silero  # noqa: E402

import core
from context_swarm import get_swarm, with_bubble  # core.answer(question, machine_id, retriever) -> screen_state

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
import paths  # noqa: E402  (repo-root asset anchors)

MODELS = paths.MODELS

LIVEKIT_URL = os.environ["LIVEKIT_URL"]  # defaulted above
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")

MACHINE_ID = os.environ.get("MACHINE_ID", "labeler-line3")

# STT — local mlx-whisper (in-process; no HTTP server).
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "mlx-community/whisper-small")

# TTS — local Kokoro ONNX (in-process; no HTTP server).
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
KOKORO_MODEL_PATH = os.environ.get("KOKORO_MODEL_PATH", str(MODELS / "kokoro-v1.0.onnx"))
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", str(MODELS / "voices-v1.0.bin"))

# VAD endpointing (push-to-talk still uses VAD to flush the captured window).
VAD_MIN_SILENCE = float(os.environ.get("VAD_MIN_SILENCE", "0.4"))

# Turn mode: "manual" = push-to-talk via the start/end_turn RPCs (the DEMO default).
# "auto" = VAD ends turns on a pause — use it for a quick mic test via `agent.py console`
# or the LiveKit Playground (talk, pause, hear the answer; no custom frontend needed).
TURN_MODE = os.environ.get("TURN_MODE", "manual").strip().lower()

# Ollama placeholder LLM (never actually called — see module docstring).
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen2.5:3b")  # core.py brain uses qwen2.5:3b

ROOM_NAME = os.environ.get("ROOM_NAME", "manuai")
SCREEN_STATE_TOPIC = "screen_state"


def _resolve_whisper_repo(name: str) -> str:
    """Map WHISPER_MODEL to a repo that actually exists on HF.

    .env ships WHISPER_MODEL=mlx-community/whisper-small, but the real mlx-community
    repo is suffixed '-mlx' (bare 'whisper-small' 401s — it does not exist). Append
    '-mlx' to a bare mlx-community 'whisper-<size>'; pass through anything that already
    carries a suffix or is a non mlx-community / local path. Kept identical to
    voice_smoke._resolve_whisper_repo.
    """
    if "/" not in name:
        return name
    org, _, repo = name.partition("/")
    if org != "mlx-community":
        return name
    suffixes = ("-mlx", "-fp16", "-fp32", "-4bit", "-8bit", "-q4", "-bit")
    if repo.startswith("whisper-") and not any(s in repo for s in suffixes):
        return f"{name}-mlx"
    return name


WHISPER_REPO = _resolve_whisper_repo(WHISPER_MODEL)


# ===========================================================================
# Local STT — mlx-whisper, in-process. Subclass stt.STT and implement
# _recognize_impl: the default stt_node calls this with the captured AudioBuffer.
# ===========================================================================
class MlxWhisperSTT(stt_mod.STT):
    """Apple-Silicon mlx-whisper STT. Offline after the first weight pull."""

    def __init__(self, model_repo: str = WHISPER_REPO, language: str = "en") -> None:
        # Not streaming: we transcribe the whole committed turn at once. The framework
        # wraps a non-streaming STT for VAD-driven endpointing automatically.
        super().__init__(
            capabilities=stt_mod.STTCapabilities(streaming=False, interim_results=False)
        )
        self._model_repo = model_repo
        self._language = language

    def _transcribe_sync(self, wav_path: str) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            wav_path,
            path_or_hf_repo=self._model_repo,
            language=self._language,           # pin English — else Whisper mis-detects (Chinese)
            condition_on_previous_text=False,  # stop the repeated-token hallucination loop
        )
        return (result.get("text") or "").strip()

    async def _recognize_impl(
        self,
        buffer: "stt_mod.AudioBuffer",
        *,
        language=stt_mod.NOT_GIVEN if hasattr(stt_mod, "NOT_GIVEN") else None,
        conn_options=None,
    ) -> stt_mod.SpeechEvent:
        # Merge all captured frames into one, write a wav (the header carries the
        # real sample-rate so Whisper resamples correctly), transcribe off-loop.
        frame = combine_frames(buffer)
        wav_bytes = frame.to_wav_bytes()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            tf.write(wav_bytes)
            wav_path = tf.name
        try:
            text = await asyncio.to_thread(self._transcribe_sync, wav_path)
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

        logger.info("[STT] transcript=%r", text)
        return stt_mod.SpeechEvent(
            type=stt_mod.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt_mod.SpeechData(language=self._language, text=text)],
        )


# ===========================================================================
# Local TTS — Kokoro ONNX, in-process. Subclass tts.TTS + ChunkedStream; the
# default tts_node calls synthesize() per text segment and drains the AudioEmitter.
# ===========================================================================
KOKORO_SAMPLE_RATE = 24000  # Kokoro v1.0 outputs 24 kHz mono float32
KOKORO_NUM_CHANNELS = 1


class KokoroTTS(tts_mod.TTS):
    """Kokoro ONNX TTS. Fully offline once the .onnx + voices.bin are on disk."""

    def __init__(
        self,
        voice: str = TTS_VOICE,
        model_path: str = KOKORO_MODEL_PATH,
        voices_path: str = KOKORO_VOICES_PATH,
        lang: str = "en-us",
    ) -> None:
        super().__init__(
            capabilities=tts_mod.TTSCapabilities(streaming=False),
            sample_rate=KOKORO_SAMPLE_RATE,
            num_channels=KOKORO_NUM_CHANNELS,
        )
        self._voice = voice
        self._lang = lang
        self._model_path = model_path
        self._voices_path = voices_path
        self._kokoro = None  # lazy: building the ONNX session is heavy

    def _engine(self):
        if self._kokoro is None:
            from kokoro_onnx import Kokoro

            for p in (self._model_path, self._voices_path):
                if not Path(p).exists():
                    raise FileNotFoundError(
                        f"Kokoro model file missing: {p} — download the "
                        "'model-files-v1.0' release into models/."
                    )
            self._kokoro = Kokoro(self._model_path, self._voices_path)
        return self._kokoro

    def synthesize(
        self,
        text: str,
        *,
        conn_options=agents.DEFAULT_API_CONNECT_OPTIONS,
    ) -> "KokoroChunkedStream":
        return KokoroChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class KokoroChunkedStream(tts_mod.ChunkedStream):
    """Synthesize one text segment to int16 PCM and feed the AudioEmitter.

    AudioEmitter protocol (cribbed from livekit.plugins.openai SSEChunkedStream._run):
    initialize(request_id, sample_rate, num_channels, mime_type) → push(bytes)… → flush().
    Kokoro returns float32 in [-1, 1]; we scale to int16 PCM (mime audio/pcm).
    """

    def __init__(self, *, tts: KokoroTTS, input_text: str, conn_options) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts_impl = tts

    async def _run(self, output_emitter: "tts_mod.AudioEmitter") -> None:
        kokoro = self._tts_impl._engine()
        samples, sample_rate = await asyncio.to_thread(
            kokoro.create,
            self.input_text,
            self._tts_impl._voice,
            1.0,            # speed
            self._tts_impl._lang,
        )
        pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

        output_emitter.initialize(
            request_id=agents.utils.shortuuid("kokoro_tts_"),
            sample_rate=int(sample_rate),
            num_channels=KOKORO_NUM_CHANNELS,
            mime_type="audio/pcm",  # raw int16 PCM (verified: AudioEmitter re-frames it)
        )
        output_emitter.push(pcm16)
        output_emitter.flush()


# ---------------------------------------------------------------------------
# Retriever factory (constructed once per session, reused across turns)
# ---------------------------------------------------------------------------
def _make_retriever():
    retr = make_retriever()
    kind = type(retr).__name__
    logger.info("Retriever: %s", kind)
    return retr


# ---------------------------------------------------------------------------
# ManuAI voice agent — the only override is llm_node (the brain join-point).
# ---------------------------------------------------------------------------
class ManuAIAgent(Agent):
    def __init__(self, machine_id: str, retriever, swarm=None) -> None:
        super().__init__(
            instructions=(
                "You are ManuAI, an offline voice assistant for factory operators. "
                "Keep answers short, clear, and safety-first. If you cannot answer "
                "from provided SOP context, escalate to a supervisor."
            )
        )
        self.machine_id = machine_id
        self.retriever = retriever
        self.swarm = swarm
        self._last_state: dict = {}

    def _schedule_bubble_push(self, snap: dict) -> None:
        if not self._last_state:
            return
        updated = {**self._last_state, "context_bubble": snap}
        self._last_state = updated
        asyncio.create_task(_publish_screen_state(updated))

    async def llm_node(
        self,
        chat_ctx: "llm_mod.ChatContext",
        tools,
        model_settings: ModelSettings,
    ) -> AsyncIterable[str]:
        """Replace the LLM step with core.answer.

        Extract the latest user transcript, run the brain (retrieve → gate → Ollama →
        assemble screen_state), publish the full screen_state over the data channel,
        and yield state["answer"] as a single str chunk for the TTS node. Yielding a
        plain str from an async generator is an accepted llm_node return form in 1.5.17
        (verified: voice/generation.py handles str chunks).
        """
        transcript = _extract_transcript(chat_ctx)
        if not transcript:
            logger.warning("llm_node: no transcript in chat_ctx; skipping turn")
            return
        logger.info("llm_node: transcript=%r machine_id=%r", transcript, self.machine_id)

        # core.answer already runs the sync Ollama call via asyncio.to_thread (G9).
        state = await core.answer(
            transcript, self.machine_id, self.retriever, swarm=self.swarm
        )
        state = with_bubble(state, self.swarm)
        self._last_state = state
        logger.info(
            "llm_node: status=%r top_score=%.3f answer=%r",
            state.get("status"),
            state.get("top_score", 0.0),
            state.get("answer", "")[:80],
        )

        await _publish_screen_state(state)

        spoken = state.get("answer", "")
        if spoken:
            yield spoken


def _extract_transcript(chat_ctx: "llm_mod.ChatContext") -> str:
    """Latest user-turn text from a ChatContext (1.5.17: .items / .messages of
    ChatMessage with .role and .text_content)."""
    try:
        source = getattr(chat_ctx, "items", None) or getattr(chat_ctx, "messages", None) or []
        for msg in reversed(list(source)):
            if getattr(msg, "role", None) in ("user", "human"):
                text = getattr(msg, "text_content", None)
                if text is None:
                    raw = getattr(msg, "content", None)
                    if isinstance(raw, str):
                        text = raw
                    elif isinstance(raw, list):
                        text = " ".join(
                            p.get("text", "") if isinstance(p, dict) else str(p) for p in raw
                        ).strip()
                return (text or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("_extract_transcript: error reading chat_ctx: %s", exc)
    return ""


async def _publish_screen_state(state: dict) -> None:
    """Publish the full screen_state dict over the LiveKit room data channel.

    screen.html receives a Uint8Array, parses JSON, and calls applyState(state) — the
    SSE-path contract. A publish failure must never crash the voice pipeline.
    publish_data signature (rtc.LocalParticipant): (payload, *, reliable=True, topic="").
    """
    try:
        ctx = agents.get_job_context()
        if ctx is None:
            logger.warning("_publish_screen_state: no active job context; skipping")
            return
        payload = json.dumps(state).encode("utf-8")
        await ctx.room.local_participant.publish_data(
            payload, reliable=True, topic=SCREEN_STATE_TOPIC
        )
        logger.debug("Published screen_state (%d bytes) topic=%r", len(payload), SCREEN_STATE_TOPIC)
    except Exception as exc:  # noqa: BLE001
        logger.error("_publish_screen_state: failed: %s", exc)


# ---------------------------------------------------------------------------
# AgentSession builder
# ---------------------------------------------------------------------------
def _build_session() -> AgentSession:
    """Construct the AgentSession with local STT / TTS / VAD + a placeholder LLM.

    The llm= placeholder is REQUIRED: AgentActivity guards generation on self.llm
    (voice/agent_activity.py) — without it llm_node never fires and the agent is mute.
    It is never actually called; ManuAIAgent.llm_node replaces its body.
    """
    stt = MlxWhisperSTT()
    tts = KokoroTTS()
    vad = silero.VAD.load(min_silence_duration=VAD_MIN_SILENCE)

    # Structural placeholder — never produces a spoken token (llm_node takes over).
    llm_placeholder = lk_openai.LLM.with_ollama(
        model=LLM_MODEL,
        base_url=f"{OLLAMA_BASE_URL}/v1",
    )

    kwargs = dict(stt=stt, llm=llm_placeholder, tts=tts, vad=vad)
    if TURN_MODE == "manual":
        # Push-to-talk: no automatic VAD turn-ending — the frontend calls the
        # start_turn/end_turn RPCs. 1.5.17 prefers turn_handling over turn_detection=.
        kwargs["turn_handling"] = TurnHandlingOptions(turn_detection="manual")
    # TURN_MODE == "auto": omit turn_handling -> default VAD endpointing ends turns
    # (so `agent.py console` / the Playground work with talk-and-pause).
    return AgentSession(**kwargs)


# ---------------------------------------------------------------------------
# AgentServer entrypoint (1.5.17 form: @server.rtc_session)
# ---------------------------------------------------------------------------
server = AgentServer(
    ws_url=LIVEKIT_URL,
    api_key=LIVEKIT_API_KEY,
    api_secret=LIVEKIT_API_SECRET,
)


@server.rtc_session(agent_name="manuai")
async def entrypoint(ctx: agents.JobContext):
    """One operator session: build retriever + session, start in the room, wire
    push-to-talk RPCs, greet."""
    logger.info(
        "Session starting: room=%r machine_id=%r retriever=moss",
        ctx.room.name, MACHINE_ID,
    )

    retriever = _make_retriever()
    session = _build_session()
    swarm = get_swarm(MACHINE_ID, retriever)
    agent = ManuAIAgent(machine_id=MACHINE_ID, retriever=retriever, swarm=swarm)
    if swarm:
        swarm.set_on_update(agent._schedule_bubble_push)

    await session.start(room=ctx.room, agent=agent)

    # Push-to-talk (manual): keep the mic silent until the operator holds the button.
    # In auto mode (console/Playground quick test) leave the mic on so VAD ends turns.
    if TURN_MODE == "manual":
        session.input.set_audio_enabled(False)

    # ----- Observability -----
    @session.on("user_input_transcribed")
    def _on_user_transcript(ev):
        logger.info("[STT] transcript=%r final=%s", ev.transcript, ev.is_final)

    @session.on("conversation_item_added")
    def _on_item(ev):
        logger.info("[%s] %s", ev.item.role, (ev.item.text_content or "")[:120])

    @session.on("agent_state_changed")
    def _on_state(ev):
        logger.info("[state] %s -> %s", ev.old_state, ev.new_state)

    # ----- Push-to-talk RPC methods (the frontend calls these via performRpc) -----
    @ctx.room.local_participant.register_rpc_method("start_turn")
    async def start_turn(data: rtc.RpcInvocationData) -> str:
        session.interrupt()        # barge-in on any in-progress TTS
        session.clear_user_turn()  # discard stale buffered audio
        session.input.set_audio_enabled(True)
        logger.info("[PTT] start_turn: mic ON")
        return "listening"

    @ctx.room.local_participant.register_rpc_method("end_turn")
    async def end_turn(data: rtc.RpcInvocationData) -> str:
        session.input.set_audio_enabled(False)
        await session.commit_user_turn()  # finalize audio -> STT -> llm_node -> TTS
        logger.info("[PTT] end_turn: committed")
        return "thinking"

    @ctx.room.local_participant.register_rpc_method("refresh_context")
    async def refresh_context(data: rtc.RpcInvocationData) -> str:
        if not swarm:
            return json.dumps({"status": "disabled", "chunk_count": 0})
        try:
            payload = json.loads(data.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        question = (payload.get("question") or agent._last_state.get("question") or "").strip()
        snap = await swarm.refresh(question or None)
        state = {
            **agent._last_state,
            "machine_id": agent._last_state.get("machine_id") or MACHINE_ID,
            "context_bubble": snap,
        }
        agent._last_state = state
        await _publish_screen_state(state)
        return json.dumps({
            "status": snap.get("status"),
            "chunk_count": snap.get("chunk_count", 0),
        })

    @ctx.room.local_participant.register_rpc_method("cancel_turn")
    async def cancel_turn(data: rtc.RpcInvocationData) -> str:
        session.input.set_audio_enabled(False)
        session.clear_user_turn()
        logger.info("[PTT] cancel_turn: discarded")
        return "cancelled"

    # ----- Greeting (session.say speaks fixed text via TTS, bypassing llm_node) -----
    await session.say("ManuAI ready. Hold the talk button and speak your question.")


# ===========================================================================
# `agent.py check` — build STT/TTS/VAD/session/agent without a mic and exit 0.
# Verifiable headlessly; proves the SDK wiring constructs against 1.5.17.
# ===========================================================================
def _check() -> int:
    print("agent.py check: constructing local voice components...")
    stt = MlxWhisperSTT()
    print(f"  STT  : {type(stt).__name__} (repo={WHISPER_REPO!r}) OK")
    tts = KokoroTTS()
    print(f"  TTS  : {type(tts).__name__} (voice={TTS_VOICE!r}, sr={tts.sample_rate}) OK")
    vad = silero.VAD.load(min_silence_duration=VAD_MIN_SILENCE)
    print(f"  VAD  : {type(vad).__name__} OK")
    session = _build_session()
    print(f"  SESSION: {type(session).__name__} (turn_handling=manual) OK")
    agent = ManuAIAgent(machine_id=MACHINE_ID, retriever=make_retriever())
    print(f"  AGENT: {type(agent).__name__} (machine_id={MACHINE_ID!r}) OK")
    # Confirm the AgentServer constructed and is bound to the local URL.
    print(f"  SERVER: {type(server).__name__} ws_url={LIVEKIT_URL!r} OK")
    # The session wires its own local STT/TTS/VAD instances (not the ones above).
    assert isinstance(session.stt, MlxWhisperSTT), "session.stt is not local mlx-whisper"
    assert isinstance(session.tts, KokoroTTS), "session.tts is not local Kokoro"
    assert session.vad is not None, "session.vad missing"
    assert LIVEKIT_URL.startswith("ws://127.0.0.1") or LIVEKIT_URL.startswith(
        "ws://localhost"
    ), f"LIVEKIT_URL is not local: {LIVEKIT_URL!r} (G1)"
    print("agent.py check: PASS (local STT/TTS/VAD wired; LLM=core.answer via llm_node; URL local)")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        sys.exit(_check())
    # `dev` / `start` / `connect` etc. are dispatched by the livekit CLI from argv.
    agents.cli.run_app(server)
