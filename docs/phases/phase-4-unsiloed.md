# Phase 4 — Unsiloed ingestion (breadth)

**Goal:** Run **real** labeler/coder manuals through Unsiloed and load them into Moss, to back the "hundreds of SOPs" claim and the authenticity flex. **Parallel / late — off the critical path.** If it fights you, M1 still stands.

| | |
|---|---|
| **Status** | ☐ TODO — parallel/late, **not** on the critical path |
| **Depends on** | Phase 0 (Moss working) + Phase 1 (the loop) |
| **Decisions** | D7 (full Unsiloed pipeline) |
| **Demo beat** | "indexed N procedures across M real manuals" + a query answered from a real manual |

## What "done" looks like
- [ ] ≥1 **real** manual run through Unsiloed **Parse** → structured chunks with page info.
- [ ] Unsiloed **Extract** (custom schema) → `error_codes[]`, `safety_flag`, `steps` with confidence + citations.
- [ ] Chunks embedded locally (**same nomic model**) and loaded into **Moss** alongside the hand-authored ones.
- [ ] The loop answers a question whose answer lives **only** in a real manual, with a correct **page citation**.

## High-level architecture
```
real manual PDFs
  └▶ Unsiloed [ Parse → chunks+pages | Extract(schema) → codes/safety/steps | Split if merged ]
       └▶ local embed (nomic) ─▶ Moss index  (+ hand-authored chunks)

Ingestion is CLOUD + one-time / offline — it NEVER touches the live query path.
```

## How to test
1. Ask a question answerable **only** from the real manual → correct grounded answer + **real page citation**.
2. Count chunks in Moss → a concrete breadth number for the pitch ("247 procedures, 6 manuals").
3. **Wifi-off still works at query time** (ingestion was done earlier, with wifi).
4. Spot-check 3 Extracted chunks → error codes / safety flags are right (trust but verify the confidence scores).

**Definition of done:** ≥1 demo query answered from a genuinely Unsiloed-parsed manual with a real citation — **and** M1 is unaffected if this phase is skipped.

## Out of scope
- Perfect parsing of every manual; multi-machine routing; live ingestion.

## Watch-outs
- External API on a deadline → keep it **off the critical path**; the hand-authored corpus is the fallback.
- Verify at the **Unsiloed booth:** Parse chunk granularity, custom Extract schema, rate limits for ~10 docs.
