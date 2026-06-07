#!/usr/bin/env python3
"""Ground-truth latency benchmark for the offline path. Reusable before/after gate.

Measures the dominant stages on THIS machine, warmed (steady-state), so improvements
are provable, not estimated. Run:  .venv/bin/python scripts/bench_latency.py
"""
import sys, time, statistics, asyncio, json
sys.path.insert(0, "src")

import core
from retriever import make_retriever

JAM = "The labeler on line 3 jammed and threw error E-42."
# (machine_id was dropped from the retrieval/answer API in the corpus-wide-retrieval change.)

def t():
    return time.perf_counter()

def stat(label, samples):
    if not samples:
        print(f"  {label:34s}  (no samples)"); return
    lo, hi = min(samples), max(samples)
    med = statistics.median(samples)
    print(f"  {label:34s}  med={med*1000:7.0f} ms   min={lo*1000:7.0f}  max={hi*1000:7.0f}  n={len(samples)}")

def main():
    print("="*72)
    print("ManuAI latency benchmark —", time.strftime("%Y-%m-%d %H:%M:%S"))
    print("="*72)

    r = make_retriever()

    # --- Retrieval (embed query + cosine over the corpus) ---
    print("\n[1] Retrieval  (Moss-minilm embed + cosine over index)")
    samples = []
    for i in range(6):
        s = t()
        asyncio.run(r.search(JAM, k=5))
        samples.append(t()-s)
    stat("retrieval.search (first incl warm)", samples[:1])
    stat("retrieval.search (warm)", samples[1:])

    # --- Full core.answer (retrieval + LLM forced-JSON), cold then warm ---
    print("\n[2] core.answer  (retrieval + Qwen2.5:3b forced-JSON, no num_predict cap)")
    samples = []
    for i in range(6):
        s = t()
        st = asyncio.run(core.answer(JAM, r))
        samples.append(t()-s)
    stat("core.answer (turn 1 / cold)", samples[:1])
    stat("core.answer (warm)", samples[1:])
    ans = st.get("answer","")
    print(f"      → status={st.get('status')}  answer_len={len(ans)} chars  answer={ans[:90]!r}")

    # --- Raw LLM call timing via common.chat_json (isolates the LLM) ---
    print("\n[3] LLM only  (common.chat_json — what core spends in the model)")
    from common import chat_json
    sys_p = core.SYSTEM
    hits = asyncio.run(r.search(JAM, k=5))
    excerpts = "\n\n".join(f"[{h['id']}] {h['procedure_title']} — {h['section']}\n{h['text']}" for h in hits)
    user = f"Question: {JAM}\n\nSOP excerpts:\n{excerpts}"
    samples = []
    for i in range(6):
        s = t(); chat_json(sys_p, user); samples.append(t()-s)
    stat("chat_json (turn 1)", samples[:1])
    stat("chat_json (warm)", samples[1:])

    # --- TTS: full answer vs first sentence (time-to-first-audio proxy) ---
    print("\n[4] TTS  (Kokoro synth — time to produce samples, NOT incl playback)")
    try:
        sys.path.insert(0, "src")
        from offline_demo import synth_to_numpy, _get_kokoro
        _get_kokoro()
        # warm-up (first synth eats ONNX graph warm-up)
        synth_to_numpy("ok")
        full = ans or "First lock out and tag out the labeler, then clear the jammed label web."
        first_sent = full.split(".")[0] + "." if "." in full else full
        s = t(); synth_to_numpy(full); full_dt = t()-s
        s = t(); synth_to_numpy(first_sent); sent_dt = t()-s
        print(f"  {'TTS full answer ('+str(len(full))+' ch)':34s}  {full_dt*1000:7.0f} ms")
        print(f"  {'TTS first sentence ('+str(len(first_sent))+' ch)':34s}  {sent_dt*1000:7.0f} ms")
        print(f"      → sentence-streaming would cut time-to-first-audio by ~{(full_dt-sent_dt)*1000:.0f} ms")
    except Exception as e:
        print(f"  TTS bench skipped: {e}")

    print("\n" + "="*72)
    print("Fixed costs not measured here: VAD trailing silence (~1.2s), audio playback.")
    print("="*72)

if __name__ == "__main__":
    main()
