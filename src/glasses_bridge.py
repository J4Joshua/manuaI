#!/usr/bin/env python3
"""glasses_bridge.py — offline Meta Ray-Ban glasses → ManuAI audio bridge.

This is `offline_demo.py` with ONE thing swapped: the audio source is a WebSocket
from the glasses (relayed by the unmodified `mc-goggles` iOS app) instead of the
laptop microphone. Everything downstream — Whisper → `core.answer` → Kokoro TTS on
the laptop + the live SOP screen — is reused VERBATIM from `offline_demo` (imported,
never copied). See docs/PRD-glasses.md and the `glasses-bridge` skill.

One process, two servers, two ports:
  • WebSocket (this module, `websockets` lib) on GLASSES_PORT (default 8766) — the
    glasses audio socket the iOS app dials.
  • offline_demo._start_http_server() on PORT (default 8000), in a daemon thread —
    the existing screen (GET / → screen.html, GET /state → live screen_state).

The unmodified iOS app opens THREE WebSockets at startup plus an occasional POST.
We satisfy all four so the app never errors / reconnect-loops, but only *act* on audio:

  ws  /publish-audio?agent=1  THE REAL WORK. First msg = JSON text header
                              {"sampleRate":48000,"channels":1}; then raw Float32-LE
                              mono PCM frames → streaming VAD → resample to 16 kHz →
                              transcribe_wav → run_pipeline (speaks on the laptop +
                              updates the screen).
  ws  /publish                Video uplink. Accept, reply {"type":"video_off"} on
                              connect, then drain & discard all JPEG/control frames.
  ws  /agent-audio            Glasses-speaker downlink. Accept and idle — output is
                              the laptop, so we NEVER send anything back (locked: no
                              glasses-speaker downlink).
  POST /publish/photo         Full-res still. See the caveat below — it is closed
                              harmlessly; it only fires on a user capture (out of
                              MVP scope) and never at startup.

Scope is LOCKED (do not expand): audio-IN only; answer OUT on the laptop (speaker +
screen). No glasses-speaker downlink, no video, no photo, no multi-turn/session —
each utterance is an independent one-shot core.answer() via run_pipeline.

────────────────────────────────────────────────────────────────────────────────
POST /publish/photo caveat (verified, deliberate)
────────────────────────────────────────────────────────────────────────────────
The `websockets` handshake parser accepts ONLY `GET` — a literal `POST` raises
`InvalidMessage` *before* any `process_request` hook runs (verified on both
websockets 15.0.1 and 16.0). Returning a true 200 to the POST on the same port
would mean owning the accept loop and routing the CRITICAL /publish-audio stream
through hand-rolled sans-I/O framing (or a localhost proxy splice) — i.e. adding
risk to the most important path for an endpoint that is out of MVP scope and never
fires during the audio demo. So the bridge keeps /publish-audio on the battle-tested
high-level `websockets` API and lets the stray POST close harmlessly, while
`process_request` DOES return 200 to any parseable non-WebSocket request (a GET to
/publish/photo, a browser/health check, curl). This is a documented trade, not an
oversight.

────────────────────────────────────────────────────────────────────────────────
Run
────────────────────────────────────────────────────────────────────────────────
    .venv/bin/python src/glasses_bridge.py                 # live bridge (Mac)
    .venv/bin/python src/glasses_bridge.py --selftest      # canonical beats over WS (Mac)
    .venv/bin/python src/glasses_bridge.py --selftest-wire # headless wire test (no models)

LOCAL-MAC verification (cannot run in a headless/CI sandbox — needs models + a mic
levels + a speaker): `--selftest` synthesises the two canonical utterances with
Kokoro, streams them as Float32 48 kHz frames into /publish-audio, and asserts the
real screen_state — jam → answered + SOP-1187 (laptop speaks); bypass → escalated.
It needs Kokoro model files, mlx-whisper (Apple Silicon), and a running Ollama with
qwen2.5:3b + nomic-embed-text. `--selftest-wire` stubs STT/brain/TTS so the WS
framing + streaming VAD + 48k→16k resample + the four endpoints + the speaking-guard
are all exercised with ZERO models — that is the headless gate. ALSO retune the VAD
for HFP on-device (GLASSES_ENERGY_THRESHOLD etc.) before a live demo: the laptop
ENERGY_THRESHOLD=0.010 will not transfer to glasses-over-Bluetooth levels.
"""
from __future__ import annotations

import argparse
import asyncio
import http
import json
import logging
import os
import sys
import tempfile
import time
from urllib.parse import urlparse

import numpy as np
from scipy.signal import resample_poly
import sounddevice as sd
import soundfile as sf
import websockets
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

# Importing offline_demo is the whole point: it runs load_env() + pops a blank
# HF_TOKEN at import (so anonymous model pulls work) and defines every helper +
# constant we reuse. We import the brain/TTS/STT entry points as module globals so
# they are swappable for the headless wire test; everything else we reach through
# the `offline_demo` module object (and never modify offline_demo.py itself).
import offline_demo
from offline_demo import transcribe_wav, run_pipeline, synth_to_wav
from retriever import CosineRetriever

# ---------------------------------------------------------------------------
# Config — env-overridable; defaults INHERIT offline_demo so the brain/screen
# behave identically. The VAD constants get glasses-specific env overrides
# because HFP/Bluetooth levels differ from a laptop mic (retune on-device).
# ---------------------------------------------------------------------------
GLASSES_PORT = int(os.environ.get("GLASSES_PORT", 8766))   # matches the iOS app default
GLASSES_HOST = os.environ.get("GLASSES_HOST", "0.0.0.0")   # all interfaces (phone on LAN)

SAMPLE_RATE = offline_demo.SAMPLE_RATE      # 16000 — what Whisper wants
BLOCK_SIZE = offline_demo.BLOCK_SIZE        # 512 @16k (~32 ms); scaled to in_rate below

ENERGY_THRESHOLD = float(os.environ.get("GLASSES_ENERGY_THRESHOLD", offline_demo.ENERGY_THRESHOLD))
SPEECH_START_BLOCKS = int(os.environ.get("GLASSES_SPEECH_START_BLOCKS", offline_demo.SPEECH_START_BLOCKS))
SILENCE_STOP_SECS = float(os.environ.get("GLASSES_SILENCE_STOP_SECS", offline_demo.SILENCE_STOP_SECS))
HARD_CAP_SECS = float(os.environ.get("GLASSES_HARD_CAP_SECS", offline_demo.HARD_CAP_SECS))

# Echo / barge-in guard (D8): the laptop speaker is in the room with an open glasses
# mic, so TTS leaks back in. While a pipeline runs AND for a short cooldown after, we
# DROP incoming audio so the answer doesn't re-trigger the loop. Best-effort, not AEC.
SPEAK_COOLDOWN_SECS = float(os.environ.get("GLASSES_SPEAK_COOLDOWN_SECS", 1.0))

# Trusted-LAN demo: no per-message size cap, so a large (discarded) JPEG video frame
# on /publish never trips a close → reconnect-loop. Audio frames are tiny regardless.
MAX_WS_MESSAGE = None

# ---------------------------------------------------------------------------
# Module state (single shared retriever + the speaking-guard flags)
# ---------------------------------------------------------------------------
_retriever: CosineRetriever | None = None
_utterance_lock: asyncio.Lock | None = None   # serialise utterances (created in the loop)
_speaking = False                              # True while a pipeline runs
_cooldown_until = 0.0                          # loop.time() until which we keep dropping
_tasks: set = set()                            # strong refs so dispatch tasks aren't GC'd mid-flight


def _log(msg: str) -> None:
    print(f"[glasses] {msg}", flush=True)


class _DropHandshakeNoise(logging.Filter):
    """Suppress the one expected handshake error: a literal `POST /publish/photo`
    can't be parsed by the GET-only handshake, so `websockets` logs an ERROR + a
    traceback. That's harmless (the server survives; the POST is out-of-scope and
    only fires on a user shutter-tap), and a scary traceback during a demo looks
    like a bug — so we drop just that message. Every other websockets error still logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "opening handshake failed" not in record.getMessage()


def _quiet_handshake_noise() -> None:
    logging.getLogger("websockets.server").addFilter(_DropHandshakeNoise())


# ---------------------------------------------------------------------------
# Streaming VAD — an INTENTIONAL copy of offline_demo.record_until_silence's RMS
# state machine (the skill says to mirror it; offline_demo's version is pull-based —
# stream.read() in a blocking loop — and offline_demo.py must NOT be modified, so the
# logic can't be shared). Fed by incoming WS frames at `in_rate` instead of an
# InputStream. Analysis blocks are BLOCK_SIZE scaled to in_rate so the timing matches
# the laptop VAD in real seconds, whatever rate the glasses send. If the VAD logic or
# constants change in offline_demo, keep this in sync by hand.
# ---------------------------------------------------------------------------
class StreamingVAD:
    def __init__(self, in_rate: int):
        self.block = max(1, int(round(BLOCK_SIZE * in_rate / SAMPLE_RATE)))
        self.silence_stop_blocks = max(1, int(SILENCE_STOP_SECS * in_rate / self.block))
        self.hard_cap_blocks = max(1, int(HARD_CAP_SECS * in_rate / self.block))
        self.reset()

    def reset(self) -> None:
        self._buf = np.empty(0, dtype=np.float32)
        self._chunks: list[np.ndarray] = []
        self._state = "PRE_SPEECH"
        self._loud = 0
        self._quiet = 0

    def feed(self, frame: np.ndarray):
        """Append a frame (float32 @ in_rate); return the completed segment
        (float32 @ in_rate) when speech ends, else None."""
        self._buf = frame if self._buf.size == 0 else np.concatenate((self._buf, frame))
        while self._buf.size >= self.block:
            block = self._buf[:self.block]
            self._buf = self._buf[self.block:]
            seg = self._step(block)
            if seg is not None:
                return seg
        return None

    def _step(self, block: np.ndarray):
        rms = float(np.sqrt(np.mean(block ** 2)))

        if self._state == "PRE_SPEECH":
            # Always-listening: no hard cap here (mirror's PRE_SPEECH cap would stop a
            # one-shot recorder; the bridge just keeps waiting for speech to start).
            if rms > ENERGY_THRESHOLD:
                self._loud += 1
                self._chunks.append(block.copy())
                if self._loud >= SPEECH_START_BLOCKS:
                    self._state = "RECORDING"
                    self._quiet = 0
            else:
                self._loud = 0
                self._chunks.clear()
            return None

        # RECORDING — accumulate; stop on trailing silence or the hard cap.
        self._chunks.append(block.copy())
        if rms <= ENERGY_THRESHOLD:
            self._quiet += 1
            if self._quiet >= self.silence_stop_blocks:
                return self._finish()
        else:
            self._quiet = 0
        if len(self._chunks) >= self.hard_cap_blocks:
            return self._finish()
        return None

    def _finish(self):
        chunks = self._chunks
        ok = self._state == "RECORDING" and bool(chunks)
        self.reset()
        return np.concatenate(chunks).astype(np.float32) if ok else None


def resample_to_16k(audio: np.ndarray, in_rate: int) -> np.ndarray:
    """48k (or whatever the header says) → 16 kHz mono float32 for Whisper.

    We resample ourselves (scipy) and never rely on ffmpeg — offline_demo feeds
    Whisper 16 kHz wavs deliberately for exactly this reason.
    """
    audio = np.asarray(audio, dtype=np.float32).flatten()
    if in_rate == SAMPLE_RATE:
        return audio
    return resample_poly(audio, SAMPLE_RATE, in_rate).astype(np.float32)


# ---------------------------------------------------------------------------
# Per-utterance pipeline (runs in a worker thread — never on the asyncio loop)
# ---------------------------------------------------------------------------
def _process_segment(segment: np.ndarray, in_rate: int) -> None:
    """Resample → 16 kHz wav → transcribe_wav → run_pipeline. BLOCKING: must run
    inside asyncio.to_thread so mlx_whisper.transcribe and Kokoro sd.play/sd.wait
    never block the event loop."""
    audio16 = resample_to_16k(segment, in_rate)
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="glasses_seg_")
    os.close(fd)
    try:
        sf.write(wav_path, audio16, SAMPLE_RATE)
        transcript = (transcribe_wav(wav_path) or "").strip()
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    if not transcript:
        _log("empty transcript — ignoring segment")
        return
    _log(f"heard: {transcript!r}")
    # run_pipeline = core.answer → _set_latest → render → speak (laptop), reused verbatim.
    # NB: transcribe_wav (above) and run_pipeline are called by BARE NAME (module globals)
    # so --selftest-wire can rebind them to stubs — don't qualify them as offline_demo.*.
    run_pipeline(transcript, _retriever)


def _guarded(loop: asyncio.AbstractEventLoop) -> bool:
    return _speaking or loop.time() < _cooldown_until


async def _dispatch_utterance(segment: np.ndarray, in_rate: int) -> None:
    """Serialise + run one utterance, holding the speaking-guard for its duration."""
    global _speaking, _cooldown_until
    loop = asyncio.get_running_loop()
    try:
        async with _utterance_lock:   # never overlap two answers
            await asyncio.to_thread(_process_segment, segment, in_rate)
    except Exception as exc:  # a bad utterance must not kill the bridge
        _log(f"pipeline error: {exc}")
    finally:
        _cooldown_until = loop.time() + SPEAK_COOLDOWN_SECS
        _speaking = False


# ---------------------------------------------------------------------------
# WebSocket endpoint handlers
# ---------------------------------------------------------------------------
async def handle_publish_audio(ws) -> None:
    """ws /publish-audio?agent=1 — the real work. Header (text) then Float32-LE frames."""
    global _speaking
    loop = asyncio.get_running_loop()
    in_rate: int | None = None
    vad: StreamingVAD | None = None
    _log("audio client connected (/publish-audio)")

    try:
        async for message in ws:
            if isinstance(message, str):
                # ONLY the first text message is the header; ignore any later text so a
                # stray frame can't re-init the VAD and discard an in-progress utterance.
                if in_rate is not None:
                    _log(f"ignoring text after header: {message[:60]!r}")
                    continue
                try:
                    hdr = json.loads(message)
                    in_rate = int(hdr.get("sampleRate", 48000))
                    vad = StreamingVAD(in_rate)
                    _log(f"header: sampleRate={in_rate} channels={hdr.get('channels')}")
                except (json.JSONDecodeError, ValueError, TypeError):
                    _log(f"ignoring non-header text message: {message[:60]!r}")
                continue

            # Binary frame = raw Float32-LE mono PCM.
            if vad is None:                       # no header seen → assume 48 kHz
                in_rate = 48000
                vad = StreamingVAD(in_rate)
                _log("no header before first frame — assuming sampleRate=48000")

            if _guarded(loop):                    # speaking-guard: drop & reset while busy
                vad.reset()
                continue

            try:
                frame = np.frombuffer(message, dtype="<f4")
            except ValueError:
                continue
            if frame.size == 0:
                continue

            segment = vad.feed(frame)
            if segment is not None:
                _speaking = True                  # engage guard synchronously, pre-task
                task = asyncio.create_task(_dispatch_utterance(segment, in_rate))
                _tasks.add(task)                  # strong ref: asyncio only weak-refs tasks
                task.add_done_callback(_tasks.discard)
    except ConnectionClosed:
        pass
    finally:
        _log("audio client disconnected (/publish-audio)")


async def handle_publish(ws) -> None:
    """ws /publish — video uplink. Tell the glasses to stop sending video, then drain."""
    try:
        await ws.send(json.dumps({"type": "video_off"}))   # stop wasting BT bandwidth
        async for _message in ws:
            pass                                            # discard JPEG + control JSON
    except ConnectionClosed:
        pass


async def handle_agent_audio(ws) -> None:
    """ws /agent-audio — glasses-speaker downlink. Accept and idle: output is the
    laptop, so we send NOTHING (locked: never open this send path)."""
    try:
        async for _message in ws:
            pass
    except ConnectionClosed:
        pass


async def _ws_handler(ws) -> None:
    route = urlparse(ws.request.path).path
    if route == "/publish-audio":
        await handle_publish_audio(ws)
    elif route == "/publish":
        await handle_publish(ws)
    elif route == "/agent-audio":
        await handle_agent_audio(ws)
    else:
        await ws.close(code=1008, reason="unknown endpoint")


def _process_request(connection, request):
    """Return 200 to any parseable non-WebSocket HTTP (health check, browser, a GET
    to /publish/photo). Real WS upgrades fall through (return None). A literal POST is
    rejected by the GET-only handshake parser before this runs — see module docstring."""
    upgrade = (request.headers.get("Upgrade") or "").lower()
    if upgrade != "websocket":
        return connection.respond(http.HTTPStatus.OK, "ManuAI glasses bridge: OK\n")
    return None


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------
def _install_speak_guard() -> None:
    """Make offline_demo.speak degrade gracefully: skip playback when there is no
    output device (headless/CI), and never let a TTS failure crash the bridge — the
    screen still updates either way. We rebind the attribute at runtime; offline_demo.py
    is not modified, and run_pipeline (reused verbatim) picks up the rebinding."""
    real_speak = offline_demo.speak
    try:
        sd.query_devices(kind="output")
        have_output = True
    except Exception as exc:  # noqa: BLE001 — any failure means "no usable output"
        have_output = False
        _log(f"no audio output device ({exc}) — TTS playback disabled; screen still updates")

    def guarded(text: str) -> None:
        if not have_output:
            return
        try:
            real_speak(text)
        except SystemExit as exc:      # _get_kokoro() exits if model files are missing
            _log(f"TTS unavailable ({exc}); continuing without audio")
        except Exception as exc:       # noqa: BLE001
            _log(f"TTS error ({exc}); continuing")

    offline_demo.speak = guarded


async def _serve_forever() -> None:
    global _utterance_lock
    _utterance_lock = asyncio.Lock()
    async with serve(_ws_handler, GLASSES_HOST, GLASSES_PORT,
                     process_request=_process_request, max_size=MAX_WS_MESSAGE):
        _log(f"glasses audio WS  : ws://<mac-ip>:{GLASSES_PORT}/publish-audio")
        _log(f"screen            : http://localhost:{offline_demo.PORT}/")
        _log("waiting for the glasses app… (Ctrl-C to exit)")
        await asyncio.Future()   # run until cancelled


def run_live() -> int:
    global _retriever
    offline_demo.MODELS.mkdir(exist_ok=True)
    _quiet_handshake_noise()
    _install_speak_guard()
    _retriever = CosineRetriever()
    offline_demo._start_http_server()
    _log(f"index loaded: {len(_retriever.index)} chunks · machine_id={offline_demo.MACHINE_ID}")
    try:
        asyncio.run(_serve_forever())
    except KeyboardInterrupt:
        _log("bye.")
    return 0


# ===========================================================================
# Verification — loopback client + selftest (full, Mac) and wire test (headless)
# ===========================================================================
JAM_UTTERANCE = offline_demo.JAM_UTTERANCE
BYPASS_UTTERANCE = offline_demo.BYPASS_UTTERANCE
_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"

# Wire-test scratchpad (populated by the stubs installed in --selftest-wire).
_WIRE: dict = {}


def _idle_state() -> dict:
    # A fully clean screen_state (every field reset) so no field leaks across beats.
    return {
        "question": "", "machine_id": "", "status": "idle", "answer": "",
        "citations": [], "steps_source": None, "steps": [], "safety_warnings": [],
        "safety_flag": False, "top_score": 0.0, "threshold": None, "source_excerpt": "",
    }


def _http_get(port: int, path: str):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return r.status, r.read()


def _fixture_48k(audio: np.ndarray, in_rate: int) -> np.ndarray:
    """Wrap a mono float32 utterance with leading/trailing silence so the streaming
    VAD sees speech-start then silence-stop, and return it at 48 kHz (glasses rate)."""
    audio = np.asarray(audio, dtype=np.float32).flatten()
    if in_rate != 48000:
        audio = resample_poly(audio, 48000, in_rate).astype(np.float32)
    pre = np.zeros(int(0.3 * 48000), dtype=np.float32)
    post = np.zeros(int((SILENCE_STOP_SECS + 0.8) * 48000), dtype=np.float32)
    return np.concatenate([pre, audio, post])


def _synth_fixture(utterance: str) -> np.ndarray:
    """Real fixture (Mac): Kokoro-synth the utterance, then present it at 48 kHz."""
    fd, wav = tempfile.mkstemp(suffix=".wav", prefix="glasses_fix_")
    os.close(fd)
    try:
        synth_to_wav(utterance, wav)
        audio, sr = sf.read(wav, dtype="float32")
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return _fixture_48k(audio, sr)


def _tone_fixture(secs: float = 2.0) -> np.ndarray:
    """Synthetic fixture (no models): a 220 Hz tone (RMS≈0.14 ≫ threshold) → one segment."""
    t = np.arange(int(secs * 48000)) / 48000.0
    tone = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    return _fixture_48k(tone, 48000)


async def loopback_stream(audio48k: np.ndarray, in_rate: int = 48000,
                          host: str = "127.0.0.1", frame_secs: float = 0.04) -> None:
    """The loopback WS client: connect /publish-audio, send the JSON header, then
    stream the audio as Float32-LE frames at `in_rate`."""
    uri = f"ws://{host}:{GLASSES_PORT}/publish-audio?agent=1"
    frame = max(1, int(frame_secs * in_rate))
    async with websockets.connect(uri, max_size=MAX_WS_MESSAGE) as ws:
        await ws.send(json.dumps({"sampleRate": in_rate, "channels": 1}))
        for i in range(0, len(audio48k), frame):
            await ws.send(audio48k[i:i + frame].astype("<f4").tobytes())
            await asyncio.sleep(0.003)   # gentle pacing; keeps the server queue drained


async def _await_state(timeout: float) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        st = offline_demo._get_latest()
        if st.get("status") in ("answered", "escalated"):
            return st
        await asyncio.sleep(0.1)
    return offline_demo._get_latest()


async def _await_guard_release(timeout: float = 30.0) -> bool:
    """Wait until the speaking-guard clears. `run_pipeline` sets the screen state
    BEFORE the (blocking) laptop speak() finishes, so a beat isn't really "done"
    until _speaking is False and the cooldown has elapsed — otherwise the next beat's
    audio would be dropped by the guard."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not _guarded(loop):
            return True
        await asyncio.sleep(0.1)
    return False


async def _drive_beat(label, utterance, expect_status, expect_sop, *, stub) -> bool:
    offline_demo._set_latest(_idle_state())
    if stub:
        _WIRE["last_wav"] = None
        _WIRE["expect_status"] = expect_status
        _WIRE["expect_sop"] = expect_sop
        audio48 = _tone_fixture()
    else:
        audio48 = _synth_fixture(utterance)

    await loopback_stream(audio48)
    state = await _await_state(15 if stub else 60)

    cites = [c.get("sop_id") for c in state.get("citations", [])]
    ok = state.get("status") == expect_status
    if expect_sop:
        ok = ok and expect_sop in cites
    detail = ""
    if stub:
        wav = _WIRE.get("last_wav")
        wav_ok = bool(wav) and wav["rate"] == SAMPLE_RATE and wav["dur"] > 0.3
        ok = ok and wav_ok
        detail = f" | 16k-wav={wav}"
    print(f"  [{label}] status={state.get('status')!r} cites={cites or '-'}"
          f"{detail}  → {_PASS if ok else _FAIL}")
    await _await_guard_release()   # let speak()+cooldown finish before the next beat
    return ok


async def _check_publish_endpoint() -> bool:
    uri = f"ws://127.0.0.1:{GLASSES_PORT}/publish"
    try:
        async with websockets.connect(uri, max_size=MAX_WS_MESSAGE) as ws:
            msg = await asyncio.wait_for(ws.recv(), timeout=3)
            ok = json.loads(msg).get("type") == "video_off"
            await ws.send(b"\xff\xd8\xff\xe0fake-jpeg-bytes")   # drained & discarded
            await ws.send(json.dumps({"type": "pause"}))
            await asyncio.sleep(0.2)
    except Exception as exc:  # noqa: BLE001
        print(f"  [/publish] error: {exc}  → {_FAIL}")
        return False
    print(f"  [/publish] video_off on connect + drains JPEG  → {_PASS if ok else _FAIL}")
    return ok


async def _check_agent_audio_endpoint() -> bool:
    uri = f"ws://127.0.0.1:{GLASSES_PORT}/agent-audio"
    ok = True
    try:
        async with websockets.connect(uri, max_size=MAX_WS_MESSAGE) as ws:
            try:                                   # server must send NOTHING
                await asyncio.wait_for(ws.recv(), timeout=0.8)
                ok = False
            except asyncio.TimeoutError:
                pass
            await ws.send(b"\x00\x00")             # still nothing back
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.4)
                ok = False
            except asyncio.TimeoutError:
                pass
    except Exception as exc:  # noqa: BLE001
        print(f"  [/agent-audio] error: {exc}  → {_FAIL}")
        return False
    print(f"  [/agent-audio] idle, sends nothing  → {_PASS if ok else _FAIL}")
    return ok


async def _check_post_photo() -> bool:
    """A literal POST /publish/photo closes harmlessly (GET-only handshake) and must
    NOT crash the bridge; a GET to the same path still 200s via process_request."""
    ok = True
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", GLASSES_PORT)
        body = b"\xff\xd8jpeg-bytes"
        writer.write(b"POST /publish/photo HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        await writer.drain()
        try:
            await asyncio.wait_for(reader.read(64), timeout=2)   # closed/empty is expected
        except asyncio.TimeoutError:
            pass
        writer.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  [POST] error: {exc}  → {_FAIL}")
        return False

    try:
        status, _ = await asyncio.to_thread(_http_get, GLASSES_PORT, "/publish/photo")  # server survived → GET 200s
        ok = status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"  [POST] server unresponsive after POST: {exc}  → {_FAIL}")
        return False
    print(f"  [POST] /publish/photo closes harmlessly; bridge survives (GET→{status})  → {_PASS if ok else _FAIL}")
    return ok


async def _check_http_200() -> bool:
    ok = True
    try:
        status, _ = await asyncio.to_thread(_http_get, GLASSES_PORT, "/")
        ws_ok = status == 200
        ok = ok and ws_ok
        print(f"  [http] GET :{GLASSES_PORT}/ (non-WS) → {status}  → {_PASS if ws_ok else _FAIL}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [http] GET :{GLASSES_PORT}/ error: {exc}  → {_FAIL}")
        ok = False
    try:
        status, body = await asyncio.to_thread(_http_get, offline_demo.PORT, "/state")
        scr_ok = status == 200 and b"status" in body
        ok = ok and scr_ok
        print(f"  [http] GET :{offline_demo.PORT}/state (screen) → {status}  → {_PASS if scr_ok else _FAIL}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [http] GET :{offline_demo.PORT}/state error: {exc}  → {_FAIL}")
        ok = False
    return ok


async def _check_speaking_guard() -> bool:
    """During one utterance's processing, a second burst must be DROPPED (not answered).
    Wire-only: the stub pipeline sleeps so the guard window is observable."""
    _WIRE["transcribe_calls"] = 0
    _WIRE["pipeline_sleep"] = 0.6
    offline_demo._set_latest(_idle_state())
    _WIRE["expect_status"] = "answered"
    _WIRE["expect_sop"] = None
    try:
        # Two bursts back-to-back over two connections (the guard is global, not
        # per-socket); the 2nd lands inside the guard+cooldown window and is dropped.
        await loopback_stream(_tone_fixture(1.2))
        await loopback_stream(_tone_fixture(1.2))
        await asyncio.sleep(SPEAK_COOLDOWN_SECS + 1.0)
    finally:
        _WIRE["pipeline_sleep"] = 0.0
    calls = _WIRE.get("transcribe_calls", 0)
    ok = calls == 1
    print(f"  [guard] transcribe calls during guarded window = {calls} (want 1)  → {_PASS if ok else _FAIL}")
    return ok


def _install_wire_stubs() -> None:
    """Replace STT/brain so the wire test runs with ZERO models. We swap the module
    globals _process_segment calls (transcribe_wav, run_pipeline)."""
    global transcribe_wav, run_pipeline

    def stub_transcribe(wav_path):
        data, sr = sf.read(wav_path, dtype="float32")
        _WIRE["last_wav"] = {"rate": int(sr), "dur": round(len(data) / sr, 3), "n": len(data)}
        _WIRE["transcribe_calls"] = _WIRE.get("transcribe_calls", 0) + 1
        return "wire-test utterance"

    def stub_pipeline(transcript, retriever):
        if _WIRE.get("pipeline_sleep"):
            time.sleep(_WIRE["pipeline_sleep"])   # widen the guard window for the test
        status = _WIRE.get("expect_status", "answered")
        sop = _WIRE.get("expect_sop")
        st = _idle_state()
        st.update({
            "question": transcript,
            "status": status,
            "answer": "wire-test answer",
            "citations": ([{"sop_id": sop, "section": "4", "page": None,
                            "procedure_title": "Wire"}] if (status == "answered" and sop) else []),
            "top_score": 0.99,
        })
        offline_demo._set_latest(st)
        return st

    transcribe_wav = stub_transcribe
    run_pipeline = stub_pipeline


async def _run_suite(*, stub: bool) -> int:
    global _utterance_lock, _retriever
    _utterance_lock = asyncio.Lock()
    _retriever = CosineRetriever()
    _quiet_handshake_noise()
    _install_speak_guard()
    if stub:
        _install_wire_stubs()
    offline_demo.MODELS.mkdir(exist_ok=True)
    offline_demo._start_http_server()

    results: list[bool] = []
    async with serve(_ws_handler, "127.0.0.1", GLASSES_PORT,
                     process_request=_process_request, max_size=MAX_WS_MESSAGE):
        print("\n─── Beat 1: JAM (→ answered + SOP-1187) ───")
        results.append(await _drive_beat("jam", JAM_UTTERANCE, "answered", "SOP-1187", stub=stub))

        print("\n─── Beat 2: BYPASS (→ escalated) ───")
        results.append(await _drive_beat("bypass", BYPASS_UTTERANCE, "escalated", None, stub=stub))

        print("\n─── Endpoints ───")
        results.append(await _check_publish_endpoint())
        results.append(await _check_agent_audio_endpoint())
        results.append(await _check_post_photo())
        results.append(await _check_http_200())

        if stub:
            print("\n─── Speaking-guard (echo/barge-in) ───")
            results.append(await _check_speaking_guard())

    all_pass = all(results)
    print("\n" + "=" * 64)
    print(f"glasses_bridge {'--selftest-wire' if stub else '--selftest'}: "
          f"{'PASS' if all_pass else 'FAIL'}  ({sum(results)}/{len(results)} checks)")
    print("=" * 64)
    return 0 if all_pass else 1


def selftest(*, stub: bool) -> int:
    label = "WIRE TEST (headless, stubbed STT/brain/TTS)" if stub else "SELFTEST (full pipeline)"
    print("=" * 64)
    print(f"ManuAI glasses_bridge — {label}")
    print("=" * 64)
    if not stub:
        print("Requires: Kokoro model files, mlx-whisper (Apple Silicon), Ollama with "
              "qwen2.5:3b + nomic-embed-text.\nThis is a LOCAL-MAC step — it cannot pass "
              "in a headless sandbox without those.\n")
    return asyncio.run(_run_suite(stub=stub))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="ManuAI offline glasses→audio bridge (raw WebSocket, wifi-off)")
    parser.add_argument("--selftest", action="store_true",
                        help="Drive the two canonical beats through the WS path (full pipeline; LOCAL-MAC).")
    parser.add_argument("--selftest-wire", action="store_true",
                        help="Headless gate: WS framing + VAD + resample + 4 endpoints + speaking-guard, no models.")
    args = parser.parse_args()

    if args.selftest_wire:
        return selftest(stub=True)
    if args.selftest:
        return selftest(stub=False)
    return run_live()


if __name__ == "__main__":
    sys.exit(main())
