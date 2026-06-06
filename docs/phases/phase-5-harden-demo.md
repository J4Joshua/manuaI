# Phase 5 — Harden + rehearse + demo

**Goal:** A reliable, rehearsed **90-second** demo with a safety net. No new features — this is reliability and delivery.

| | |
|---|---|
| **Status** | ☐ TODO — last |
| **Depends on** | all prior phases |
| **Decisions** | all; esp. D8 (refusal), D10 (solo cut-list) |
| **Demo beat** | the whole script, landed cleanly, with the wifi-off mic-drop |

## What "done" looks like
- [ ] ~5–10 SOPs loaded, including the scripted beats (jam + safety + a deliberately-absent procedure).
- [ ] `SCORE_THRESHOLD` **re-tuned** on the real corpus (covered queries pass, off-domain refuses).
- [ ] **Backup wifi-off video** recorded (captured back at M1, refreshed here).
- [ ] 90-sec script rehearsed **≥5×**, runs cleanly start→finish.
- [ ] Both **demo floor** (M1 + screen + refusal, voice via cloud) **and ceiling** (all-local incl. voice) rehearsed.

## High-level architecture
```
No new components — FREEZE the stack. Phase 5 = reliability + delivery + fallback.
```

## How to test
1. **5 consecutive clean dry-runs, wifi off**, end-to-end.
2. **Break-one-thing drill:** unplug the mic / kill local TTS mid-demo → fall back to cloud voice or typed/video path **gracefully**.
3. Time the full demo → **≤90s**.
4. Re-run the refusal beat 3× → it refuses **every** time (no lucky-pass).

**Definition of done:** 5 consecutive clean wifi-off dry-runs + a recorded backup video + the script memorized.

## The 90-second script (from PRD §10)
1. *(10s)* "Runs on this one box — watch the wifi." → turn wifi **off**.
2. *(15s)* Push-to-talk: "the labeler on line 3 jammed, error E-42."
3. *(25s)* Screen: SOP card + ⚠ safety; voice gives the action + steps.
4. *(20s)* "Can I bypass the interlock?" → refuses + escalates (card appears).
5. *(15s)* "Everything ran locally — wifi's been off the whole time. The data never leaves the floor."
6. Backup video ready in case live flakes.

## Out of scope
- **Any new feature.** Feature-creep at 3am is the enemy.

## Watch-outs
- Protect sleep; the **video is the insurance**.
- Don't re-tune the threshold last-minute without re-testing all beats.
