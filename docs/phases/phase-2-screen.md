# Phase 2 — Screen (live context)

**Goal:** A glanceable screen that shows what the agent retrieved — transcript, answer, cited SOP, steps, safety banner, escalation — so judges can **see** the grounding. This is the "Co-Pilot live-context display" and what beats "a chatbot that talks."

| | |
|---|---|
| **Status** | ☐ TODO — do this **before** voice |
| **Depends on** | Phase 1 (consumes its structured JSON) |
| **Decisions** | D8 (structured output drives the UI), D9 (frontend) |
| **Demo beat** | the visible citation + the distinct escalation card |

## What "done" looks like
- [ ] For each query the screen shows: **transcript** (the question), **answer**, **cited SOP** (id · section · page), **numbered steps**, a **⚠ safety banner** when `safety_flag`, and a visually **distinct escalation card** on refusal.
- [ ] The screen is a **pure view** of the same D8 JSON the loop already emits — no answer logic in the UI.
- [ ] It updates **live** as a query is processed.
- [ ] A stranger can tell, in ~3 seconds, *what to do* and *where it came from*.

## High-level architecture
```
M1 loop ──structured JSON {answer, used_chunk_ids→citation, steps, safety, escalate}──▶ Screen (web page)

Screen panels:  [ transcript ]  [ answer + steps ]  [ SOP card: id · §x · p.n ]  [ ⚠ safety ]  [ escalate ]
```
The screen is a thin renderer of agent state. For now it can read the loop's JSON via any simple local feed; **Phase 3 swaps that feed to LiveKit's data channel** with no change to the panels.

## How to test
1. Covered query → answer + steps + SOP card + safety banner all render.
2. Refusal query → escalation card appears, **no fabricated steps**.
3. Side-by-side with the terminal: the screen shows the same facts, no extra/invented content.
4. Squint test: hide the terminal — is the screen alone enough to act on?

**Definition of done:** the three M1 beats are fully legible on screen without reading the terminal.

## Out of scope
- Voice (Phase 3). Polish/animation. QR machine-select (use a **preset/dropdown**). Multi-machine.

## Watch-outs
- Keep **all logic in the agent**; the UI only renders the JSON (so Phase 3 can reuse it untouched).
- Citation/steps/safety must come from **metadata**, never re-derived in the UI.
