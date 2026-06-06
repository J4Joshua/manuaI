#!/usr/bin/env python3
"""End-to-end demo on the REAL Moss index:
   retrieve (machine_id-filtered) -> threshold gate -> Qwen (cite-or-refuse) -> render.

Proves the demo works on Moss and reveals the score range to set the Moss threshold.
Online once (load_index); queries then run locally.
    .venv/bin/python moss_demo.py                 # observe mode (gate off)
    MOSS_THRESHOLD=0.55 .venv/bin/python moss_demo.py   # with the tuned gate
"""
import asyncio
import json
import os

from retriever import make_client, MossRetriever, load_env
from common import chat_json   # local Ollama qwen2.5:3b (sync)

GATE = float(os.getenv("MOSS_THRESHOLD", "0.0"))   # 0.0 = observe (let the LLM refuse)

SYSTEM = """You are ManuAI, a factory-floor assistant for machine operators.
Answer ONLY from the SOP excerpts in the user message. Rules:
- Use ONLY information in the excerpts. Never invent steps, codes, or values.
- "answer" tells the operator what to DO in 1-2 short sentences, leading with the most
  critical safety action. Use this shape (fill ONLY from the excerpts; do not copy these
  words): "First <critical safety action>, then <next key action>." Never write
  "refer to SOP X" and never mention SOP numbers in the answer.
- CRITICAL task-match check: first decide whether an excerpt actually describes the
  procedure for THIS specific task/fault. If the question asks about something the excerpts
  do NOT cover — e.g. recalibrating servo/drive timing when the excerpts only cover
  jam-clearing and lockout, or bypassing a guard/interlock — do NOT repurpose unrelated
  steps. Set "escalate" true and briefly say you are escalating to a supervisor in "answer".

Worked example of the task-match check (off-task -> escalate):
  Question: "How do I recalibrate the servo drive timing?"
  Excerpts: only describe clearing a label jam and lockout/tagout (NOT servo timing).
  Correct output: {"answer": "I don't have an approved procedure for recalibrating servo
  drive timing - escalating to your supervisor.", "used_chunk_ids": [], "escalate": true}

Return ONLY a JSON object with exactly these keys:
  "answer": string,
  "used_chunk_ids": array of the excerpt ids you actually used,
  "escalate": boolean"""

QUERIES = [
    ("labeler-line3", "The labeler on line 3 jammed and threw error E-42."),
    ("labeler-line3", "Can I bypass the safety interlock and run with the guard open to keep the line going?"),
    ("labeler-line3", "How do I recalibrate the servo drive timing on the labeler?"),
    ("cobot-cellA",   "The pick-and-place robot in cell A stopped and shows fault C4."),
]


def refuse(reason):
    print("  " + "-" * 66)
    print("  [STOP] NO APPROVED PROCEDURE -> ESCALATE TO SUPERVISOR")
    print(f"  {reason}")
    print("  " + "-" * 66)


async def run_one(retr, machine, q):
    print("\n" + "=" * 72)
    print(f'[{machine}]  "{q}"')
    hits = await retr.search(q, machine, k=5)
    for h in hits:
        print(f"   score={h['score']:.3f}  {h['sop_id']:<9} {h['section']!r}")
    top = hits[0] if hits else None

    if not top or top["score"] < GATE:
        refuse(f"top score {top['score'] if top else 0:.3f} < gate {GATE} — nothing relevant retrieved.")
        return

    excerpts = "\n\n".join(f"[{h['id']}] {h['title']} — {h['section']}\n{h['text']}" for h in hits)
    try:
        out = json.loads(chat_json(SYSTEM, f"Question: {q}\n\nSOP excerpts:\n{excerpts}"))
    except json.JSONDecodeError:
        refuse("model returned invalid JSON")
        return

    by_id = {h["id"]: h for h in hits}
    cited = [by_id[i] for i in out.get("used_chunk_ids", []) if i in by_id]
    if out.get("escalate") or not cited:
        refuse(out.get("answer") or "no grounded answer")
        return

    print("  " + "-" * 66)
    if any(c["safety_flag"] for c in cited):
        print("  [!] SAFETY-FLAGGED SOP cited — lockout/tagout applies")
    print(f"  [answer] {out['answer']}")
    seen, cites = set(), []
    for c in cited:
        key = (c["sop_id"], c["section"])
        if key not in seen:
            seen.add(key)
            cites.append(f"{c['sop_id']} {c['section']}")
    print(f"  [cite]   {', '.join(cites)}  — {cited[0]['title']}")
    print("  " + "-" * 66)


async def main():
    load_env()
    client = make_client()
    index = os.getenv("MOSS_INDEX_NAME", "manuals")
    alpha = float(os.getenv("MOSS_ALPHA", "0.8"))
    retr = MossRetriever(client, index, alpha=alpha)
    print(f"index={index}  gate={GATE}  alpha={alpha}  (0.0 gate = observe mode)")
    for machine, q in QUERIES:
        await run_one(retr, machine, q)


if __name__ == "__main__":
    asyncio.run(main())
