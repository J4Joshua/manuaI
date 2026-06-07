#!/usr/bin/env python3
"""voice_smoke.py — mic-free proof that local STT + TTS + brain work end-to-end.

No mic, no LiveKit, no network LLM (Ollama is local). Proves the three legs of the
Phase 3 voice pipeline in isolation so a failure is unambiguous:

    Kokoro TTS  → wav  → mlx-whisper STT → transcript → core.answer (brain) → wav

Run:
    .venv/bin/python src/voice_smoke.py

First run downloads the Whisper weights (mlx-community/whisper-small) into the HF
cache. The Kokoro ONNX weights must already live in models/ (see README / agent.py):
    models/kokoro-v1.0.onnx
    models/voices-v1.0.bin

Acceptance (this script asserts both):
    jam    → status == "answered"  AND  SOP-1187 in citations
    bypass → status == "escalated"

Everything here is reused by agent.py (the KokoroTTS / mlx-whisper wrappers): keeping
the proof and the agent on the SAME synth/transcribe code means the smoke test is a
real proxy for the live pipeline.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

# Load .env (stdlib-only loader from retriever) so WHISPER_MODEL / TTS_VOICE etc. apply.
from retriever import make_moss_retriever, load_env

load_env()

# An EMPTY HF_TOKEN (the .env ships it blank) makes huggingface_hub send an empty
# "Authorization: Bearer " header, which HF answers with 401 even for PUBLIC repos.
# Drop it so anonymous downloads work (wifi is on for model pulls; harmless if real).
if not (os.environ.get("HF_TOKEN") or "").strip():
    os.environ.pop("HF_TOKEN", None)

import core  # core.answer(question, machine_id, retriever) -> screen_state
import paths

MODELS = paths.MODELS


def _resolve_whisper_repo(name: str) -> str:
    """Map a configured WHISPER_MODEL to a repo that actually exists on HF.

    .env ships WHISPER_MODEL=mlx-community/whisper-small, but the real mlx-community
    repo is suffixed '-mlx' (plain 'whisper-small' 401s — it does not exist). If the
    name is a bare 'mlx-community/whisper-<size>' with no recognized suffix, append
    '-mlx'. Anything already carrying a suffix (-mlx, -fp16, .en-mlx, …) or a non
    mlx-community / local path is passed through untouched.
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


WHISPER_MODEL = _resolve_whisper_repo(
    os.environ.get("WHISPER_MODEL", "mlx-community/whisper-small")
)
TTS_VOICE = os.environ.get("TTS_VOICE", "af_heart")
KOKORO_MODEL_PATH = os.environ.get("KOKORO_MODEL_PATH", str(MODELS / "kokoro-v1.0.onnx"))
KOKORO_VOICES_PATH = os.environ.get("KOKORO_VOICES_PATH", str(MODELS / "voices-v1.0.bin"))
MACHINE_ID = os.environ.get("MACHINE_ID", "labeler-line3")

JAM_UTTERANCE = "The labeler on line 3 jammed and threw error E-42."
BYPASS_UTTERANCE = "Can I bypass the safety interlock and run with the guard open?"


# ---------------------------------------------------------------------------
# Kokoro TTS — load once, synthesize to a 24kHz mono wav.
# ---------------------------------------------------------------------------
_kokoro = None


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        for p in (KOKORO_MODEL_PATH, KOKORO_VOICES_PATH):
            if not Path(p).exists():
                sys.exit(
                    f"Missing Kokoro model file: {p}\n"
                    "Download from the kokoro-onnx 'model-files-v1.0' GitHub release into models/."
                )
        _kokoro = Kokoro(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
    return _kokoro


def synth_to_wav(text: str, wav_path: str, voice: str = TTS_VOICE) -> str:
    """Kokoro synth → float32 @ 24kHz → wav on disk. Returns the path."""
    k = _get_kokoro()
    samples, sample_rate = k.create(text, voice=voice, speed=1.0, lang="en-us")
    sf.write(wav_path, samples, sample_rate)
    return wav_path


# ---------------------------------------------------------------------------
# mlx-whisper STT — transcribe a wav file (reads sample-rate from the wav header).
# ---------------------------------------------------------------------------
def transcribe_wav(wav_path: str) -> str:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        wav_path, path_or_hf_repo=WHISPER_MODEL,
        language="en", condition_on_previous_text=False,
    )
    return (result.get("text") or "").strip()


# ---------------------------------------------------------------------------
# One full round-trip for a single utterance.
# ---------------------------------------------------------------------------
async def round_trip(label: str, utterance: str, retriever) -> dict:
    print(f"\n=== {label} ===")
    print(f"utterance (TTS in):  {utterance!r}")

    q_wav = str(MODELS / "_smoke_q.wav")
    synth_to_wav(utterance, q_wav)
    print(f"synthesized question wav: {q_wav}")

    transcript = transcribe_wav(q_wav)
    print(f"transcript (STT out): {transcript!r}")

    state = await core.answer(transcript, MACHINE_ID, retriever)
    citations = [c["sop_id"] for c in state.get("citations", [])]
    print(f"status:              {state.get('status')!r}")
    print(f"top_score:           {state.get('top_score')}")
    print(f"answer:              {state.get('answer')!r}")
    print(f"citations (sop_ids): {citations}")

    a_wav = str(MODELS / "_smoke_a.wav")
    synth_to_wav(state.get("answer", "") or "(no answer)", a_wav)
    print(f"answer wav:          {a_wav}")
    print(f"question wav:        {q_wav}")

    return state


def main() -> int:
    MODELS.mkdir(exist_ok=True)
    retriever = make_moss_retriever()

    jam_state = asyncio.run(round_trip("JAM (covered → answered)", JAM_UTTERANCE, retriever))
    bypass_state = asyncio.run(
        round_trip("BYPASS (off-policy → escalated)", BYPASS_UTTERANCE, retriever)
    )

    # ----- Acceptance -----
    jam_citations = [c["sop_id"] for c in jam_state.get("citations", [])]
    jam_ok = jam_state.get("status") == "answered" and "SOP-1187" in jam_citations
    bypass_ok = bypass_state.get("status") == "escalated"

    print("\n=== ACCEPTANCE ===")
    print(f"jam    → answered + SOP-1187 cited: {'PASS' if jam_ok else 'FAIL'} "
          f"(status={jam_state.get('status')!r}, citations={jam_citations})")
    print(f"bypass → escalated:                 {'PASS' if bypass_ok else 'FAIL'} "
          f"(status={bypass_state.get('status')!r})")

    if jam_ok and bypass_ok:
        print("\nVOICE SMOKE: PASS")
        return 0
    print("\nVOICE SMOKE: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
