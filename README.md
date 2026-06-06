# ManuAI — M1 skateboard

The **sacred loop**, runnable with **wifi off**:

> question → local embed (nomic) → retrieve + score → **threshold gate** → Qwen-3B (forced JSON) → grounded answer **cited from real SOP metadata**, or **refuse + escalate**.

No Unsiloed, no voice, no GPU, no cloud. This is **M1** from the build plan — get it green, **record the wifi-off video**, then layer on the screen (Phase 2) and voice (Phase 3). Full plan + decisions: see [`PRD.md`](./docs/PRD.md).

## Prereqs (one time)
Ollama is installed and running as a service; pull the two local models (the only downloads):
```bash
ollama pull qwen2.5:3b
ollama pull nomic-embed-text
ollama list        # both should appear
```
No Python packages needed — the scripts use only the standard library.

## Run
```bash
python3 ingest.py                                          # embeds chunks.json -> index.json
python3 src/ask.py "the labeler on line 3 jammed and shows error E-42"
```

### The three demo beats
```bash
# 1) Grounded answer + safety banner + citation
python3 src/ask.py "labeler on line 3 jammed, error E-42"

# 2) Policy-grounded refusal — cites the interlock policy and says no
python3 src/ask.py "can I bypass the safety interlock to keep the line running?"

# 3) Hard refusal via the threshold gate — no SOP covers this, so it escalates
python3 src/ask.py "how do I recalibrate the servo drive timing?"
```

### Prove it's offline
Turn wifi **off** (toggle in the menu bar), then re-run any command above. It still answers. That's the headline — record a screen capture of this the moment it works.

## Files
- `chunks.json` — 6 hand-authored labeler-line SOPs with the metadata schema (D7), incl. the LOTO/jam hero procedure and a global interlock policy.
- `common.py` — local Ollama calls (embed + forced-JSON chat) + cosine; stdlib only.
- `ingest.py` — embed chunks → `index.json` (the **stub index**).
- `ask.py` — the loop: retrieve → threshold gate (D8) → Qwen JSON → citation-from-metadata / refuse.

## MOSS SWAP POINT (D6)
Retrieval is currently cosine over `index.json` (a stub so M1 runs with zero services). To move to Moss:
- **`ingest.py`** — instead of writing vectors to `index.json`, load each `{id, vector, metadata}` into a Moss index.
- **`ask.py` → `retrieve()`** — replace the cosine loop with a Moss top-k query (same query vector + the `machine_id` metadata filter). Leave the threshold gate and everything downstream unchanged.

Confirm at **Moss office hours (4pm)**: does Moss take our pre-computed vectors, at what dim/format, and how to filter by metadata.

## Tunables (top of `ask.py`)
- `SCORE_THRESHOLD = 0.70` — the gate (must match `ask.py`; this is the single source of truth). Tune with real data: it should pass the covered queries (~0.80) and reject the servo-recalibration one (0.648).
- `TOP_K = 3`.

## Memory note (16GB Mac)
For the demo, run Ollama with flash attention + quantized KV cache to ease memory pressure:
```bash
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
```
(or set these in the brew service env). Keep answers short so the 3B model stays snappy.
