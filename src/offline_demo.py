#!/usr/bin/env python3
"""offline_demo.py — WebRTC-free, wifi-off voice demo for ManuAI.

Single process:
  1. Tiny stdlib HTTP server in a daemon thread serving:
       GET /       → screen.html (verbatim, same file server.py uses)
       GET /state  → current screen_state JSON (polled ~600ms by screen.html)
  2. Voice loop (main thread):
       Press Enter → record mic (silence-VAD; speech-start + trailing-silence stop;
       hard cap 15 s) → mlx_whisper STT → core.answer → update LATEST → render to
       terminal → Kokoro TTS → sounddevice play → loop.

Zero LiveKit / WebRTC / network. Only touches: mic, speaker, local Ollama, localhost HTTP.

Run:
    .venv/bin/python src/offline_demo.py              # live voice loop
    .venv/bin/python src/offline_demo.py --selftest   # headless acceptance test
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import sounddevice as sd
import soundfile as sf

# ---------------------------------------------------------------------------
# Paths + env
# ---------------------------------------------------------------------------
import paths

MODELS = paths.MODELS
SCREEN_HTML = paths.WEB / "screen.html"

# Load .env the same way voice_smoke.py does (retriever's stdlib loader).
from retriever import make_retriever, load_env

load_env()

# An empty HF_TOKEN makes huggingface_hub 401 on anonymous downloads.
# Drop it so model pulls work without a real token (matches voice_smoke.py).
if not (os.environ.get("HF_TOKEN") or "").strip():
    os.environ.pop("HF_TOKEN", None)

import core
from context_swarm import empty_bubble, get_bg_runner, get_swarm, live_bubble_snapshot, with_bubble
from render import render

# ---------------------------------------------------------------------------
# Config (all from env with sensible defaults)
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", 8000))

# Whisper repo — same _resolve_whisper_repo logic as voice_smoke.py.
def _resolve_whisper_repo(name: str) -> str:
    if "/" not in name:
        return name
    org, _, repo = name.partition("/")
    if org != "mlx-community":
        return name
    suffixes = ("-mlx", "-fp16", "-fp32", "-4bit", "-8bit", "-q4", "-bit")
    if repo.startswith("whisper-") and not any(s in repo for s in suffixes):
        return f"{name}-mlx"
    return name

WHISPER_MODEL = _resolve_whisper_repo(
    os.environ.get("WHISPER_MODEL", "mlx-community/whisper-small")
)
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
KOKORO_MODEL_PATH = os.environ.get("KOKORO_MODEL_PATH", str(MODELS / "kokoro-v1.0.onnx"))
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", str(MODELS / "voices-v1.0.bin"))

# VAD / recording tuning.
SAMPLE_RATE = 16000       # Whisper expects 16 kHz
BLOCK_SIZE = 512          # ~32 ms per block at 16 kHz
ENERGY_THRESHOLD = 0.010  # RMS gate — tune per environment (ambient noise level)
SPEECH_START_BLOCKS = 3   # blocks above threshold before we declare speech started
SILENCE_STOP_SECS = 1.2   # trailing silence before we stop recording
HARD_CAP_SECS = 15.0      # absolute maximum recording length

# ---------------------------------------------------------------------------
# Module-global screen state (written by voice loop, read by HTTP server)
# ---------------------------------------------------------------------------
LATEST: dict = {
    "question": "",
    "machine_id": "",
    "status": "idle",
    "answer": "",
    "citations": [],
    "steps_source": None,
    "steps": [],
    "safety_warnings": [],
    "safety_flag": False,
    "top_score": 0.0,
    "threshold": None,
    "source_excerpt": "",
    "context_bubble": empty_bubble(),
}
_LATEST_LOCK = threading.Lock()
_bg_runner = get_bg_runner()


def _on_bubble_update(snap: dict) -> None:
    global LATEST
    with _LATEST_LOCK:
        LATEST = {**LATEST, "context_bubble": snap}
    n = snap.get("chunk_count", 0)
    if n:
        sops = sorted({ln.get("sop_id", "?") for ln in snap.get("lines", [])})
        print(f"[context] {snap.get('status', '?')}: {n} chunk(s) — {', '.join(sops)}")


def _set_latest(state: dict) -> None:
    global LATEST
    with _LATEST_LOCK:
        LATEST = state


def _get_latest() -> dict:
    with _LATEST_LOCK:
        return LATEST


# ---------------------------------------------------------------------------
# HTTP server (daemon thread) — serves screen.html + /state JSON
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress request logs; only errors go to stderr
        pass

    def _send_json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path, code: int = 200) -> None:
        body = path.read_bytes()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/":
            if not SCREEN_HTML.exists():
                self.send_error(404, "screen.html not found")
                return
            self._send_html(SCREEN_HTML)
            return

        if path == "/state":
            state = _get_latest()
            state = {
                **state,
                "context_bubble": live_bubble_snapshot(),
            }
            self._send_json(state)
            return

        self.send_error(404)


def _start_http_server() -> ThreadingHTTPServer:
    """Start the HTTP server in a daemon thread and return the server object."""
    server = ThreadingHTTPServer(("", PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------------------
# Kokoro TTS — load once, reuse.
# ---------------------------------------------------------------------------
_kokoro = None


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        for p in (KOKORO_MODEL_PATH, KOKORO_VOICES_PATH):
            if not Path(p).exists():
                sys.exit(
                    f"Missing Kokoro model file: {p}\n"
                    "Download from the kokoro-onnx 'model-files-v1.0' GitHub release into models/."
                )
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
    return _kokoro


def synth_to_numpy(text: str, voice: str = TTS_VOICE) -> tuple[np.ndarray, int]:
    """Synthesise text → float32 numpy array + sample rate (24 kHz)."""
    k = _get_kokoro()
    samples, sample_rate = k.create(text, voice=voice, speed=1.0, lang="en-us")
    return np.asarray(samples, dtype=np.float32), sample_rate


def synth_to_wav(text: str, wav_path: str, voice: str = TTS_VOICE) -> str:
    """Synth → wav on disk (used by selftest for a Whisper-readable file). Returns path."""
    samples, sample_rate = synth_to_numpy(text, voice)
    sf.write(wav_path, samples, sample_rate)
    return wav_path


def speak(text: str) -> None:
    """Synthesise and play through the default output device (blocking).

    Releases PortAudio between mic capture and playback — on macOS AUHAL,
    playing without sd.stop() after InputStream often yields PaErrorCode -9986
    or a hung sd.wait() that blocks the next mic prompt.
    """
    if not text.strip():
        return
    sd.stop()
    samples, sample_rate = synth_to_numpy(text)
    try:
        sd.play(samples, sample_rate)
        sd.wait()
    except Exception as exc:
        # Fallback: afplay avoids the PortAudio output stream entirely.
        import subprocess
        import tempfile

        print(f"\n[tts] sounddevice playback failed ({exc}) — trying afplay…")
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                path = f.name
            sf.write(path, samples, sample_rate)
            subprocess.run(["afplay", path], check=True, timeout=120)
        except Exception as exc2:
            print(f"[tts] Could not play audio ({exc2}). Read the answer on screen.")
    finally:
        sd.stop()


# ---------------------------------------------------------------------------
# mlx-whisper STT
# ---------------------------------------------------------------------------
def transcribe_wav(wav_path: str) -> str:
    """Transcribe a wav file → text. language='en' pin is REQUIRED — without it
    Whisper mis-detects Chinese on short clips."""
    import mlx_whisper
    result = mlx_whisper.transcribe(
        wav_path,
        path_or_hf_repo=WHISPER_MODEL,
        language="en",
        condition_on_previous_text=False,
    )
    return (result.get("text") or "").strip()


# ---------------------------------------------------------------------------
# VAD mic recording — silence-gated
# ---------------------------------------------------------------------------
def record_until_silence() -> np.ndarray | None:
    """Record from the default input device using a simple RMS energy gate.

    State machine:
      PRE_SPEECH → wait for SPEECH_START_BLOCKS consecutive loud blocks.
      RECORDING  → accumulate audio; stop after SILENCE_STOP_SECS of trailing
                   quiet or HARD_CAP_SECS total.

    Returns float32 mono array at SAMPLE_RATE, or None if no speech detected.
    """
    silence_stop_blocks = int(SILENCE_STOP_SECS * SAMPLE_RATE / BLOCK_SIZE)
    hard_cap_blocks = int(HARD_CAP_SECS * SAMPLE_RATE / BLOCK_SIZE)

    audio_chunks: list[np.ndarray] = []
    state = "PRE_SPEECH"
    loud_count = 0
    quiet_count = 0
    total_blocks = 0

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=BLOCK_SIZE) as stream:
        while True:
            block, _ = stream.read(BLOCK_SIZE)
            rms = float(np.sqrt(np.mean(block ** 2)))
            total_blocks += 1

            if state == "PRE_SPEECH":
                if rms > ENERGY_THRESHOLD:
                    loud_count += 1
                    audio_chunks.append(block.copy())
                    if loud_count >= SPEECH_START_BLOCKS:
                        state = "RECORDING"
                        quiet_count = 0
                        sys.stdout.write(" [recording…]")
                        sys.stdout.flush()
                else:
                    loud_count = 0
                    audio_chunks.clear()
                # Hard cap even in PRE_SPEECH so we don't hang forever
                if total_blocks >= hard_cap_blocks:
                    break

            elif state == "RECORDING":
                audio_chunks.append(block.copy())
                if rms <= ENERGY_THRESHOLD:
                    quiet_count += 1
                    if quiet_count >= silence_stop_blocks:
                        break
                else:
                    quiet_count = 0
                if total_blocks >= hard_cap_blocks:
                    break

    if state != "RECORDING" or not audio_chunks:
        return None  # No speech detected

    audio = np.concatenate(audio_chunks, axis=0).flatten()
    return audio


def save_wav(audio: np.ndarray, path: str) -> None:
    sf.write(path, audio, SAMPLE_RATE)


# ---------------------------------------------------------------------------
# Full STT → brain → TTS pipeline (shared by loop + selftest)
# ---------------------------------------------------------------------------
async def process_transcript(transcript: str, retriever, swarm) -> dict:
    """Run core.answer and update LATEST. Returns the screen_state."""
    state = await core.answer(transcript, retriever, swarm=swarm)
    state = with_bubble(state, swarm)
    _set_latest(state)
    return state


def run_pipeline(transcript: str, retriever, swarm) -> dict:
    """Sync wrapper: STT transcript → screen_state (updates LATEST + renders + speaks)."""
    state = _bg_runner.run(process_transcript(transcript, retriever, swarm))
    render(state)
    try:
        speak(state.get("answer") or "")
    except Exception as exc:
        print(f"\n[tts] {exc} — read the answer on screen.")
        sd.stop()
    return state


# ---------------------------------------------------------------------------
# Voice loop (main thread)
# ---------------------------------------------------------------------------
def voice_loop() -> None:
    """The interactive demo loop. Runs until Ctrl-C."""
    retriever = make_retriever()
    swarm = get_swarm(retriever, _on_bubble_update)

    # Confirm a default input device exists (informational only — demo may still work).
    try:
        dev = sd.query_devices(kind="input")
        print(f"[audio] Default input:  {dev['name']}  ({dev['default_samplerate']:.0f} Hz)")
    except Exception as exc:
        print(f"[audio] WARNING: could not query default input device: {exc}")

    print(f"\nManuAI offline demo  —  http://localhost:{PORT}/")
    print("─" * 60)
    print("Press Enter, then speak. Recording stops after ~1.2 s of silence.")
    print("Ctrl-C to exit.")
    print()

    tmp_wav = str(MODELS / "_offline_demo.wav")

    while True:
        try:
            input("[mic] Press Enter to record…")
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return

        print("[mic] Listening for speech…", end="", flush=True)
        try:
            audio = record_until_silence()
        except Exception as exc:
            print(f"\n[mic] Recording error: {exc}")
            continue

        if audio is None:
            print("\n[mic] No speech detected — try again.")
            continue

        print()  # newline after the inline status
        save_wav(audio, tmp_wav)

        print("[stt] Transcribing…", end="", flush=True)
        try:
            transcript = transcribe_wav(tmp_wav)
        except Exception as exc:
            print(f"\n[stt] Error: {exc}")
            continue

        if not transcript.strip():
            print("\n[stt] Empty transcript — try again.")
            continue

        print(f"\n[stt] Heard: {transcript!r}")

        try:
            run_pipeline(transcript, retriever, swarm)
        except Exception as exc:
            print(f"[brain] Error: {exc}")
            sd.stop()

        print("[mic] Ready — press Enter for another question.")


# ---------------------------------------------------------------------------
# --selftest (headless acceptance gate)
# ---------------------------------------------------------------------------
JAM_UTTERANCE = "The labeler on line 3 jammed and threw error E-42."
BYPASS_UTTERANCE = "Can I bypass the safety interlock and run with the guard open?"

_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"


def selftest() -> int:
    """Run headless acceptance checks. Returns 0 (success) or 1 (failure)."""
    results: list[bool] = []

    print("=" * 60)
    print("ManuAI offline_demo — SELFTEST")
    print("=" * 60)

    # -- Sounddevice check (informational) --
    print("\n[check] sounddevice default input:")
    try:
        dev = sd.query_devices(kind="input")
        print(f"  Device #{dev['index']}: {dev['name']} @ {dev['default_samplerate']:.0f} Hz  ← OK")
    except Exception as exc:
        print(f"  WARNING: {exc} — no default input in this headless env (OK for CI; user has mic)")

    # -- Kokoro warm-up (ensures model files exist before STT tests) --
    print("\n[check] Kokoro TTS warm-up…")
    try:
        _get_kokoro()
        print("  Kokoro loaded OK")
    except SystemExit as exc:
        print(f"  FAIL — {exc}")
        return 1

    # -- Retriever --
    print("\n[check] MossRetriever…")
    try:
        retriever = make_retriever()
        print(f"  {len(retriever.index)} chunks loaded  ← OK")
    except SystemExit as exc:
        print(f"  FAIL — {exc}")
        return 1

    # Helper: TTS → wav → STT → brain
    def _roundtrip(label: str, utterance: str) -> dict | None:
        wav = str(MODELS / f"_selftest_{label}.wav")
        print(f"\n  [{label}] TTS synth: {utterance!r}")
        try:
            synth_to_wav(utterance, wav)
        except Exception as exc:
            print(f"  [{label}] TTS FAILED: {exc}")
            return None
        print(f"  [{label}] STT transcribe…")
        try:
            transcript = transcribe_wav(wav)
        except Exception as exc:
            print(f"  [{label}] STT FAILED: {exc}")
            return None
        print(f"  [{label}] transcript: {transcript!r}")
        print(f"  [{label}] core.answer…")
        try:
            state = asyncio.run(core.answer(transcript, retriever))
        except Exception as exc:
            print(f"  [{label}] brain FAILED: {exc}")
            return None
        cites = [c["sop_id"] for c in state.get("citations", [])]
        print(f"  [{label}] status={state.get('status')!r}  citations={cites}")
        return state

    # --- Test 1: JAM → answered + SOP-1187 ---
    print("\n─── Test 1: JAM utterance ───")
    jam_state = _roundtrip("jam", JAM_UTTERANCE)
    jam_ok = (
        jam_state is not None
        and jam_state.get("status") == "answered"
        and "SOP-1187" in [c["sop_id"] for c in jam_state.get("citations", [])]
    )
    jam_cites = [c["sop_id"] for c in (jam_state or {}).get("citations", [])]
    print(
        f"  Result: status={jam_state and jam_state.get('status')!r}  "
        f"SOP-1187 in citations={('SOP-1187' in jam_cites)}  "
        f"→ {_PASS if jam_ok else _FAIL}"
    )
    results.append(jam_ok)

    # --- Test 2: BYPASS → escalated ---
    print("\n─── Test 2: BYPASS utterance ───")
    bypass_state = _roundtrip("bypass", BYPASS_UTTERANCE)
    bypass_ok = (
        bypass_state is not None
        and bypass_state.get("status") == "escalated"
    )
    print(
        f"  Result: status={bypass_state and bypass_state.get('status')!r}  "
        f"→ {_PASS if bypass_ok else _FAIL}"
    )
    results.append(bypass_ok)

    # --- Test 3: HTTP server ---
    print("\n─── Test 3: HTTP server ───")
    import urllib.request as _urllib

    test_state = {
        "question": "selftest-question",
        "machine_id": "",
        "status": "answered",
        "answer": "selftest-answer",
        "citations": [{"sop_id": "SOP-1187", "section": "4", "page": None,
                        "procedure_title": "Selftest"}],
        "steps_source": None,
        "steps": [],
        "safety_warnings": [],
        "safety_flag": False,
        "top_score": 0.95,
        "threshold": 0.70,
        "source_excerpt": "selftest excerpt",
    }
    _set_latest(test_state)
    server = _start_http_server()
    time.sleep(0.2)  # brief settle so the server thread is ready

    http_ok = True

    # GET /state
    try:
        with _urllib.urlopen(f"http://localhost:{PORT}/state", timeout=5) as resp:
            data = json.loads(resp.read())
        state_ok = (
            data.get("question") == "selftest-question"
            and data.get("status") == "answered"
        )
        print(f"  GET /state: question={data.get('question')!r} status={data.get('status')!r}  "
              f"→ {_PASS if state_ok else _FAIL}")
        if not state_ok:
            http_ok = False
    except Exception as exc:
        print(f"  GET /state FAILED: {exc}  → {_FAIL}")
        http_ok = False

    # GET /
    try:
        with _urllib.urlopen(f"http://localhost:{PORT}/", timeout=5) as resp:
            html = resp.read()
        html_ok = b"ManuAI" in html and b"applyState" in html
        print(f"  GET /: len={len(html)} bytes  ManuAI in HTML={b'ManuAI' in html}  "
              f"applyState present={b'applyState' in html}  → {_PASS if html_ok else _FAIL}")
        if not html_ok:
            http_ok = False
    except Exception as exc:
        print(f"  GET / FAILED: {exc}  → {_FAIL}")
        http_ok = False

    server.shutdown()
    results.append(http_ok)

    # --- Summary ---
    all_pass = all(results)
    print("\n" + "=" * 60)
    print(f"Test 1 (jam → answered + SOP-1187): {_PASS if results[0] else _FAIL}")
    print(f"Test 2 (bypass → escalated):        {_PASS if results[1] else _FAIL}")
    print(f"Test 3 (HTTP server):               {_PASS if results[2] else _FAIL}")
    print("=" * 60)
    print(f"\nSELFTEST: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="ManuAI offline voice demo (WebRTC-free)")
    parser.add_argument("--selftest", action="store_true", help="Run headless acceptance tests and exit")
    args = parser.parse_args()

    MODELS.mkdir(exist_ok=True)

    if args.selftest:
        return selftest()

    # Live demo: start HTTP server, run voice loop.
    _start_http_server()
    print(f"Screen live at: http://localhost:{PORT}/")
    try:
        voice_loop()
    except KeyboardInterrupt:
        print("\nBye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
