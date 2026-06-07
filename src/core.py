#!/usr/bin/env python3
"""core.answer — the brain (ARCHITECTURE.md §3d).

    async def answer(question, retriever, k=5) -> screen_state (dict)

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

# DECOUPLED VERIFICATION (safety-critical): the answer-vs-escalate decision is made from
# SOPs ALONE (above) — chats can NEVER flip a refusal or make an off-task query answerable.
# Chats are used in a SEPARATE second pass that only ANNOTATES an already-grounded answer,
# checking whether prior operator incidents corroborate or conflict with it. This keeps the
# refusal as reliable as the no-chats path (G15) while still using chats to verify/guide.
VERIFY_SYSTEM = """You compare a factory assistant's SOP-grounded answer against informal
PRIOR OPERATOR INCIDENTS (chat logs of how similar past issues were handled). You do NOT
change the answer. Return ONLY a JSON object: {"corroboration_note": string, "agrees": boolean}.
- If a prior incident clearly resolved a similar issue the SAME way the answer describes:
  agrees=true, note = one short sentence, e.g. "Matches a prior incident resolved the same way."
- If a prior incident CONFLICTS with the answer: agrees=false, note = a short caution, e.g.
  "Caution: a prior incident handled this differently — confirm with a supervisor."
- If the prior incidents are not relevant to this answer: note = "", agrees=false.
Base it ONLY on the provided text; never invent."""

SOURCE_EXCERPT_LIMIT = 500


def _inferred_machine_id(cited: list, primary_hits: list) -> str:
    """Display-only label from chunk metadata — not an operator input."""
    for pool in (cited, primary_hits):
        for h in pool:
            mid = h.get("machine_id")
            if mid and mid not in ("all", "None", ""):
                return str(mid)
    return ""


def _escalated(question, reason, top_score, threshold, corroboration=None, primary_hits=None):
    """A screen_state for any refusal branch. Invariants (§3b): citations/steps/
    safety_warnings empty, steps_source None, safety_flag False, source_excerpt "".
    `corroboration` (prior operator incidents) is supplemental + additive — it may be
    shown even on a refusal (e.g. operators also refused to bypass a guard) and never
    affects the refusal decision, so it does not touch any §3b invariant."""
    return {
        "question": question,
        "machine_id": _inferred_machine_id([], primary_hits or []),
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
        "corroboration": corroboration or [],
        "corroboration_note": "",
    }


def _merge_hits(primary_hits: list, swarm) -> list:
    """Primary Moss hits first, then swarm-prefetched chunks (deduped by id)."""
    if not swarm:
        return list(primary_hits)
    seen = {h["id"] for h in primary_hits}
    merged = list(primary_hits)
    for h in swarm.get_hits():
        if h["id"] not in seen:
            merged.append(h)
            seen.add(h["id"])
    return merged


async def _finish(state, swarm, question, primary_hits):
    if swarm and primary_hits:
        await swarm.after_answer(question, primary_hits)
    return state


async def _safe_search(chat_retriever, question, k):
    """Chat retrieval is supplemental — a failure (index missing, network) must never
    break the authoritative answer. Returns [] on any error."""
    try:
        return await chat_retriever.search(question, None, k)
    except Exception:  # noqa: BLE001 - supplemental source, degrade gracefully
        return []


def _corroboration(chat_hits, limit=3):
    """Dedupe chat chunks to distinct prior-incident threads (by sop_id = thread id),
    keeping the best-scoring chunk per thread. Returns a small, screen-safe list."""
    best = {}
    for h in chat_hits:
        tid = h.get("sop_id")
        if tid and tid not in best:
            best[tid] = {
                "chat_id": tid,
                "summary": h.get("procedure_title") or "",
                "machine_id": h.get("machine_id"),
                "score": round(float(h.get("score", 0.0)), 3),
            }
    return list(best.values())[:limit]


async def answer(
    question,
    retriever,
    k=5,
    chat_retriever=None,
    swarm=None,
):
    """retriever = the authoritative SOP/manual index (decides answered vs escalated).
    chat_retriever (optional) = the SECONDARY operator-chat index, queried in parallel:
    prior similar incidents that corroborate/guide the answer but never cite and never
    flip a refusal (ARCHITECTURE.md §3d).
    swarm (optional) = background Moss context prefetch; supplemental chunks merged into
    the LLM prompt after the primary retrieval."""
    threshold = retriever.threshold

    # Two simultaneous, source-separated retrievals: SOPs (authoritative) + chats (supplemental).
    if chat_retriever is not None:
        primary_hits, chat_hits = await asyncio.gather(
            retriever.search(question, k=k),
            _safe_search(chat_retriever, question, 3),
        )
    else:
        primary_hits, chat_hits = await retriever.search(question, k=k), []

    hits = _merge_hits(primary_hits, swarm)
    top_score = primary_hits[0]["score"] if primary_hits else 0.0
    corroboration = _corroboration(chat_hits)

    # THRESHOLD GATE — deterministic, does NOT depend on the model OR on chats. Fires
    # only for the stub (threshold is a real number); Moss has threshold=None so it
    # always proceeds. Chats NEVER influence this decision.
    if not primary_hits or (threshold is not None and top_score < threshold):
        return await _finish(
            _escalated(
                question,
                "No SOP matches closely enough to answer safely.",
                top_score, threshold, corroboration,
                primary_hits=primary_hits,
            ),
            swarm, question, primary_hits,
        )

    # PRIMARY decision is SOP-ONLY (chats are NOT in this prompt) — so the answer-vs-escalate
    # behavior is identical to the no-chats path and chats can never weaken a refusal.
    excerpts = "\n\n".join(
        f"[{h['id']}] {h['procedure_title']} — {h['section']}\n{h['text']}" for h in hits
    )
    raw = await asyncio.to_thread(chat_json, SYSTEM, f"Question: {question}\n\nSOP excerpts:\n{excerpts}")
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        return await _finish(
            _escalated(
                question,
                "Model did not return valid JSON — retry, or check Ollama is running.",
                top_score, threshold, corroboration,
                primary_hits=primary_hits,
            ),
            swarm, question, primary_hits,
        )

    # validate cited ⊆ retrieved (strip any surrounding brackets the model may emit)
    by_id = {h["id"]: h for h in hits}
    cited = [by_id[str(i).strip("[]")] for i in out.get("used_chunk_ids", [])
             if str(i).strip("[]") in by_id]

    if out.get("escalate") or not cited or not out.get("answer"):
        return await _finish(
            _escalated(
                question,
                out.get("answer") or "Could not ground an answer in the SOPs.",
                top_score, threshold, corroboration,
                primary_hits=primary_hits,
            ),
            swarm, question, primary_hits,
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

    # SEPARATE verification pass: only annotates the already-grounded answer (never the
    # decision). Skipped entirely when there are no chats.
    corroboration_note = ""
    if chat_hits:
        corroboration_note = await asyncio.to_thread(_verify_with_chats, out["answer"], chat_hits)

    state = {
        "question": question,
        "machine_id": _inferred_machine_id(cited, primary_hits),
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
        "corroboration": corroboration,
        "corroboration_note": corroboration_note,
    }
    return await _finish(state, swarm, question, primary_hits)


def _verify_with_chats(answer_text, chat_hits):
    """Second-pass cross-check (runs only when chats exist). Returns a short note on
    whether prior operator incidents corroborate/conflict with the SOP-grounded answer.
    Cannot change the answer or the status. Returns "" on any problem (degrade gracefully)."""
    seen, prior = set(), []
    for h in chat_hits:
        tid = h.get("sop_id")
        if tid in seen:
            continue
        seen.add(tid)
        prior.append(f"[{tid}] {h.get('text', '')}")
    if not prior:
        return ""
    user = f"Answer given to the operator:\n{answer_text}\n\nPrior operator incidents:\n" + "\n\n".join(prior)
    try:
        out = json.loads(chat_json(VERIFY_SYSTEM, user))
        return (out.get("corroboration_note") or "").strip()
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""
