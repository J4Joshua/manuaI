# Phase 3 — Voice round-trip

**Goal:** One solid **push-to-talk** round-trip — speak a question, hear the grounded answer, see the screen update. Voice is the **ceiling, not the floor**: cloud fallback is fully acceptable; never let it threaten M1 or the screen.

| | |
|---|---|
| **Status** | ☐ TODO |
| **Depends on** | Phases 1 + 2 |
| **Decisions** | D2 (local target + cloud fallback), D9 (LiveKit room + Whisper + TTS) |
| **Demo beat** | the actual conversation — and it still works wifi-off if local |

## What "done" looks like
- [ ] **Push-to-talk:** hold → speak → release → transcribed → M1 loop → **spoken answer** + screen updates.
- [ ] Runs through a **self-hosted LiveKit room** (agent + the Phase 2 frontend in the same room).
- [ ] **Local target:** Whisper STT + local TTS. **Cloud STT/TTS fallback** behind a toggle if local drags past your cutoff.
- [ ] LLM **streams** to TTS sentence-by-sentence (latency).
- [ ] The refusal/escalation beat works by voice too.

## High-level architecture
```
mic (push-to-talk)
  └▶ LiveKit room ─▶ Agent [ Whisper STT ─▶ M1 loop (retrieve ▸ gate ▸ Qwen JSON) ─▶ TTS ]
                       │                                                   └▶ speaker (audio)
                       └──data channel──▶ Phase 2 screen (transcript, card, safety, escalate)
```
LiveKit's starter template gives ~80%: plug the M1 loop in as the "LLM" node; push the D8 JSON to the screen over the data channel.

## How to test
1. Say *"the labeler on line 3 jammed, error E-42"* → hear the action answer; screen shows card + safety banner.
2. Say the **bypass-interlock** question → hear the refusal; screen shows the escalation card.
3. Measure **end-of-speech → first spoken word**; target **≤1.5s** (note the actual).
4. **Fallback test:** disable local TTS → cloud fallback still completes a full round-trip.
5. If local voice is up: **wifi off → full spoken round-trip still works** (the dream demo).

**Definition of done:** one clean spoken round-trip for a **covered** query *and* the **refusal**, with the screen in sync.

## Out of scope
- Barge-in polish, multi-turn memory, wake-word, multilingual.

## Watch-outs
- Hard rule: if voice integration drags, **flip the cloud fallback and move on** — M1 + screen already carry the demo.
- Local voice needs **no GPU** (Whisper + Piper run on CPU); choose **Piper vs Kokoro** by whichever has the turnkey LiveKit plugin (check at the LiveKit booth).
