"""M1 query loop — the SACRED loop, runnable with WIFI OFF:

  question -> local embed -> retrieve + score -> THRESHOLD GATE -> Qwen (forced JSON)
           -> grounded answer with citation-from-metadata, OR refuse + escalate.

Everything runs locally via Ollama. Turn wifi off; it still works.
Run:  python3 ask.py "the labeler on line 3 jammed and shows error E-42"
"""
import argparse
import json
import os
import sys

from common import embed, chat_json, cosine

HERE = os.path.dirname(os.path.abspath(__file__))

# --- tunables (tune with real data / confirm threshold at Moss office hours) ---
TOP_K = 3
SCORE_THRESHOLD = 0.70       # top-hit cosine below this -> deterministic refuse (tuned to demo corpus; retune with real data)
DEFAULT_MACHINE = "labeler-line-3"

SYSTEM = """You are ManuAI, a factory-floor assistant for machine operators.
Answer ONLY from the SOP excerpts in the user message. Rules:
- Use ONLY information in the excerpts. Never invent steps, codes, or values.
- "answer" tells the operator what to DO in 1-2 short sentences, leading with the most
  critical safety action. Use this shape (fill ONLY from this question's excerpts; do not
  copy these words): "First <critical safety action>, then <next key action>." Never write
  "refer to SOP X" and never mention SOP numbers in the answer — the citation handles that.
- "used_chunk_ids": cite ONLY the excerpts you actually used — usually just one.
- If the excerpts contain no approved procedure for the question, do NOT guess: set
  "escalate" true and briefly say you are escalating to a supervisor in "answer".
Return ONLY a JSON object with exactly these keys:
  "answer": string,
  "used_chunk_ids": array of the excerpt ids you actually used,
  "escalate": boolean"""


def retrieve(question, machine):
    index_path = os.path.join(HERE, "index.json")
    if not os.path.exists(index_path):
        sys.exit("No index.json — run `python3 ingest.py` first.")
    qv = embed(question, kind="query")
    with open(index_path) as f:
        index = json.load(f)
    # metadata filter: this machine's docs + global ("all") policies
    cands = [c for c in index if c.get("machine_id") in (machine, "all")] or index
    for c in cands:
        c["score"] = cosine(qv, c["vector"])
    return sorted(cands, key=lambda c: c["score"], reverse=True)[:TOP_K]


def refuse(reason):
    print("\n" + "=" * 66)
    print("  [STOP]  NO APPROVED PROCEDURE — ESCALATING TO SUPERVISOR")
    print("=" * 66)
    print(f"  {reason}")
    print("  Supervisor flagged. Do not improvise on safety-critical steps.")
    print("=" * 66 + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--machine", default=DEFAULT_MACHINE)
    args = ap.parse_args()

    hits = retrieve(args.question, args.machine)
    top = hits[0]

    print(f'\n[mic] Operator: "{args.question}"   [machine: {args.machine}]')
    print(f"      top match: {top['id']}   score={top['score']:.3f}   (threshold {SCORE_THRESHOLD})")

    # THRESHOLD GATE — deterministic; does NOT depend on the 3B model's judgment
    if top["score"] < SCORE_THRESHOLD:
        refuse("No SOP in the local index matches closely enough to answer safely.")
        return

    excerpts = "\n\n".join(f'[{h["id"]}] {h["procedure_title"]}\n{h["text"]}' for h in hits)
    raw = chat_json(SYSTEM, f"Question: {args.question}\n\nSOP excerpts:\n{excerpts}")
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        refuse("Model did not return valid JSON — retry, or check Ollama is running.")
        return

    by_id = {h["id"]: h for h in hits}
    cited = [by_id[str(i).strip("[]")] for i in out.get("used_chunk_ids", [])
             if str(i).strip("[]") in by_id]

    if out.get("escalate") or not cited:
        refuse(out.get("answer") or "Could not ground an answer in the SOPs.")
        return

    # safety warnings come from the cited chunks' REAL metadata -> un-fakeable
    safety = []
    for c in cited:
        for w in c.get("safety_warnings", []):
            if w not in safety:
                safety.append(w)

    print("\n" + "=" * 66)
    if safety:
        print("  [!] SAFETY FIRST")
        for w in safety:
            print(f"      - {w}")
        print("-" * 66)
    print("  [answer]")
    print(f"      {out['answer']}")
    primary = next((c for c in cited if c.get("steps")), None)
    if primary:
        print("-" * 66)
        print(f"  [steps]  {primary['sop_id']} {primary['section']} — {primary['procedure_title']}")
        for i, s in enumerate(primary["steps"], 1):
            print(f"      {i}. {s}")
    print("-" * 66)
    print("  [source]" if len(cited) == 1 else "  [sources]")
    for c in cited:
        print(f"      {c['sop_id']} {c['section']} (p.{c['page']}) — {c['procedure_title']}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    main()
