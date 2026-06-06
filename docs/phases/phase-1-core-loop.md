# Phase 1 — Core loop (M1) ✅ DONE

**Goal:** The sacred loop — offline, grounded, cited, refuses-or-escalates — over hand-authored SOP chunks. This alone proves the thesis.

| | |
|---|---|
| **Status** | ✅ **BUILT & validated locally** (see [../README.md](../README.md)) |
| **Depends on** | nothing — uses a local cosine **stub** so it runs with zero external services |
| **Decisions** | D1, D4, D6, D7 (hand-authored for M1), D8 |
| **Demo beat** | the whole demo: grounded answer + citation, and the "I won't guess, escalating" refusal |

## What "done" looks like — all met
- [x] Grounded answer with **safety-first banner + numbered steps + citation**, all from real chunk metadata.
- [x] **Policy refusal** — unsafe ask cites the policy and says no.
- [x] **Deterministic threshold gate** — off-domain ask refuses *without calling the model*.
- [x] **Semantic** retrieval — paraphrases with zero keyword overlap still hit the right SOP.
- [x] Runs **entirely locally** (only talks to `localhost:11434`) → wifi-off capable by construction.

## High-level architecture
```
question (text)
  └▶ embed query (nomic, local)
       └▶ retrieve top-k  [STUB: cosine over index.json  ──MOSS SWAP POINT──▶ Moss]
            └▶ THRESHOLD GATE ──(below cutoff)──▶ refuse + escalate
                 └▶ Qwen-3B (forced JSON: answer, used_chunk_ids, escalate)
                      └▶ render from REAL metadata: safety ▸ answer ▸ steps ▸ citation
                                                   (or refuse if model escalates / cites nothing)
```

## How to test
- The three beats + a paraphrase (commands in [../README.md](../README.md)):
  - jam/E-42 → grounded answer + steps + `SOP-1187 §4.2`
  - bypass interlock → policy refusal
  - servo timing → deterministic gate refusal (0.648 < 0.70)
  - "ribbon ran out" → finds ribbon SOP at 0.804 (semantic)
- **Wifi-off test:** turn wifi off, re-run any query → still answers. **Record this** — it's your headline + safety-net video.

**Definition of done:** (met) all beats green + wifi-off proof recorded.

## Out of scope (later phases)
- Moss (stubbed → Phase 0 + swap), screen (Phase 2), voice (Phase 3), Unsiloed (Phase 4).

## Watch-outs
- `SCORE_THRESHOLD = 0.70` is tuned to the 6 demo chunks — **re-tune** with the real corpus (Phase 5).
- The 3B occasionally names an SOP in the spoken line — cosmetic; the citation is the source of truth.
