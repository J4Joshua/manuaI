# Phase 0 — Moss hello-world

**Goal:** Prove Moss runs **locally** — put vectors in, query them, wifi off — before betting anything on it.

| | |
|---|---|
| **Status** | ☐ TODO — this is your **first technical task** and your **4pm office-hours goal** |
| **Depends on** | nothing (just the local embedder from M1) |
| **Decisions** | D6 (embedding), D1 (offline) |
| **Why first** | Moss is the one novel dependency the whole demo rests on. Ollama / LiveKit / Whisper are well-trodden; Moss is not. De-risk it before building on it. |

## What "done" looks like
- [ ] Moss SDK installed and importable locally (Python).
- [ ] You can **insert ≥3 vectors + metadata** into a local Moss index.
- [ ] You can **query top-k** and get the expected nearest neighbour back.
- [ ] It works with **wifi OFF** (no cloud call at query time).
- [ ] Answers recorded for the 3 open questions: (1) does Moss accept our **pre-computed** vectors and at what **dim/format**? (2) how to **filter by metadata** (`machine_id`)? (3) how do **index updates** work?

## High-level architecture
```
nomic-embed-text (Ollama, local) ──vectors──▶ Moss local index ◀──query vector── test script
                                                    │
                                          metadata filter (machine_id)
```
Just Moss + our local embeddings. No LLM, no voice, no UI yet.

## How to test
1. Embed 3 short strings locally → insert into Moss with simple metadata.
2. Query with a 4th string related to one of them → expect that one ranked #1.
3. **Turn wifi off, re-run** → still returns the right result.
4. Try a metadata-filtered query (e.g. only `machine_id="labeler-line-3"`) → confirm filtering works.

**Definition of done:** a tiny script puts 3 vectors into Moss and returns the correct nearest neighbour **with wifi off**, and you've left office hours knowing how vectors/dims/filters/updates work.

## Out of scope
- Replacing the M1 stub (that's the **Moss swap**, done *after* this passes).
- Loading the full corpus, the LLM, the UI.

## Risks / watch-outs
- If Moss expects **cloud** embedding → we supply our own vectors instead (D6 already plans for this). Confirm at office hours.
- SDK / install friction is exactly why this is Phase 0 and not discovered at 2am.
