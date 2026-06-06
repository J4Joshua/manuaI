# ManuAI — build phases

One file per phase: **what to achieve** + **how to test** + **high-level architecture** (light on implementation).
Full plan & decisions: [../PRD.md](../PRD.md). M1 code & run instructions: [../README.md](../README.md).

**Golden rule (solo):** M1 is sacred. Nothing on the critical path you've never run. Templates over hand-building. Record the proof the instant it works.

| Phase | Goal | Status | Depends on | Unlocks |
|------|------|--------|-----------|---------|
| [0 — Moss hello-world](phase-0-moss-helloworld.md) | Prove Moss runs locally, wifi off | ☐ TODO — *4pm office-hours goal* | — | the Moss swap |
| [1 — Core loop (M1)](phase-1-core-loop.md) | Offline grounded + cited + refuse loop | ✅ **DONE** | — (uses local stub) | the whole thesis |
| [2 — Screen](phase-2-screen.md) | Glanceable live-context UI | ☐ TODO | Phase 1 | "show, don't just talk" |
| [3 — Voice](phase-3-voice.md) | One push-to-talk round-trip | ☐ TODO | Phases 1–2 | the conversational demo |
| [4 — Unsiloed](phase-4-unsiloed.md) | Real manuals → breadth | ☐ TODO — *parallel/late* | Phase 0 + 1 | "hundreds of SOPs" claim |
| [5 — Harden + demo](phase-5-harden-demo.md) | Rehearsed 90s + backup video | ☐ TODO | all | a reliable demo |

**Critical path:** 0 → 1 → 2 → 3 → 5.   **Phase 4 is parallel/late** — off the critical path; if it fights you, M1 still stands.

**The Moss swap** is the bridge from Phase 0 to Phase 1: once Phase 0 proves Moss works, replace the cosine stub in `ask.py:retrieve()` with a Moss query (marked `MOSS SWAP POINT`). Everything downstream is unchanged.
