#!/usr/bin/env python3
"""core.answer — the brain (ARCHITECTURE.md §3d).

    async def answer(question, machine_id, retriever, k=5) -> screen_state (dict)

The ONE function every consumer programs to. Produces a single `screen_state` dict on
EVERY exit path (§3b) — no prints, no sys.exit. Flow:

    retriever.search → THRESHOLD GATE (Moss: threshold=None, skipped) → chat_json (forced
    JSON) → validate cited ⊆ retrieved → assemble screen_state (all fields from metadata).

Refusal on Moss (threshold=None) comes from the LLM task-match judgment — the SYSTEM
prompt below carries a load-bearing few-shot negative example. Copy it verbatim — it is
what makes off-domain refusal reliable on a 3B.
"""
import asyncio
import json

from common import chat_json

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

SOURCE_EXCERPT_LIMIT = 500


def _escalated(question, machine_id, reason, top_score, threshold):
    """A screen_state for any refusal branch. Invariants (§3b): citations/steps/
    safety_warnings empty, steps_source None, safety_flag False, source_excerpt ""."""
    return {
        "question": question,
        "machine_id": machine_id,
        "status": "escalated",
        "answer": reason,
        "citations": [],
        "steps_source": None,
        "steps": [],
        "safety_warnings": [],
        "safety_flag": False,
        "top_score": round(float(top_score), 3),
        "threshold": threshold,
        "source_excerpt": "",
    }


async def answer(question, machine_id, retriever, k=5):
    threshold = retriever.threshold

    hits = await retriever.search(question, machine_id, k)
    top_score = hits[0]["score"] if hits else 0.0

    # THRESHOLD GATE — deterministic when threshold is set. Moss has threshold=None.
    if not hits or (threshold is not None and top_score < threshold):
        return _escalated(
            question, machine_id,
            "No SOP matches closely enough to answer safely.",
            top_score, threshold,
        )

    excerpts = "\n\n".join(
        f"[{h['id']}] {h['procedure_title']} — {h['section']}\n{h['text']}" for h in hits
    )
    raw = await asyncio.to_thread(
        chat_json, SYSTEM, f"Question: {question}\n\nSOP excerpts:\n{excerpts}"
    )
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return _escalated(
            question, machine_id,
            "Model did not return valid JSON — retry, or check Ollama is running.",
            top_score, threshold,
        )

    # validate cited ⊆ retrieved (strip any surrounding brackets the model may emit)
    by_id = {h["id"]: h for h in hits}
    cited = [by_id[str(i).strip("[]")] for i in out.get("used_chunk_ids", [])
             if str(i).strip("[]") in by_id]

    if out.get("escalate") or not cited or not out.get("answer"):
        return _escalated(
            question, machine_id,
            out.get("answer") or "Could not ground an answer in the SOPs.",
            top_score, threshold,
        )

    # ---- ANSWERED — assemble from cited-chunk metadata (un-fakeable) ----
    citations, seen = [], set()
    for c in cited:
        key = (c["sop_id"], c["section"])
        if key in seen:
            continue
        seen.add(key)
        citations.append({
            "sop_id": c["sop_id"],
            "section": c["section"],
            "page": c.get("page"),
            "procedure_title": c["procedure_title"],
        })

    # steps / safety_warnings come from cited-chunk metadata IF present (the real corpus
    # has none → []). safety_flag = any cited chunk's flag. source_excerpt = primary cited
    # chunk text, truncated.
    steps, steps_source = [], None
    primary_steps = next((c for c in cited if c.get("steps")), None)
    if primary_steps:
        steps = list(primary_steps["steps"])
        steps_source = {
            "sop_id": primary_steps["sop_id"],
            "section": primary_steps["section"],
            "procedure_title": primary_steps["procedure_title"],
        }

    safety_warnings = []
    for c in cited:
        for w in c.get("safety_warnings", []) or []:
            if w not in safety_warnings:
                safety_warnings.append(w)

    excerpt = (cited[0].get("text") or "")[:SOURCE_EXCERPT_LIMIT]

    return {
        "question": question,
        "machine_id": machine_id,
        "status": "answered",
        "answer": out["answer"],
        "citations": citations,
        "steps_source": steps_source,
        "steps": steps,
        "safety_warnings": safety_warnings,
        "safety_flag": any(c["safety_flag"] for c in cited),
        "top_score": round(float(top_score), 3),
        "threshold": threshold,
        "source_excerpt": excerpt,
    }
