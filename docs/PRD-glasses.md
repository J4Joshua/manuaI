# ManuAI × Meta Ray-Bans — Glasses Integration PRD

**Hands-free ManuAI: the operator speaks a fault from their glasses and hears the grounded SOP back — gloved, eyes-up, and with the wifi off.**

> Vision & integration plan · **no committed schedule.** Owner: Joshua. Last updated: 2026-06-06.
> Companion to the core `PRD.md`. Sources: `GLASSESINTEGRATION.md` (the integration assessment) and the `glasses-bridge` skill (the build spec). Inline **Dn** tags trace to the numbered decisions from the design log that produced this doc; **§n** means a section *here* unless it says *core PRD*.

---

## 1. The vision

The core ManuAI demo already does the hard thing: an operator speaks a fault and hears the right SOP back — cited, safe, and with the wifi off. The glasses make it **hands-free**.

Today the operator still stands at a laptop and holds a mic. On a real floor they're **gloved, hands full, and nowhere near a screen**. Meta Ray-Bans remove the laptop from their hands *and* their sightline: they speak the fault into the air and — in the production vision — hear the grounded answer **in their ear**, with the safety warning and citation read aloud. Both hands and both eyes stay on the machine. This is the strictly-stronger "gloved operator on the floor" story a laptop mic can't tell. (A Bluetooth lapel mic would be a better *mic*; the glasses are a better *operator experience* — see §2.)

**The moment (≈20s).** Gloved operator, wifi visibly off. *"The labeler on line 3 jammed and threw error E-42."* → the answer comes back: *"First, lockout/tagout the labeler… Source: SOP eleven-eighty-seven."* Then the trust beat: *"Can I bypass the interlock to keep the line moving?"* → *"I don't have an approved procedure for that, and it's safety-critical — I'm flagging your supervisor. Do not bypass the interlock."* The operator never touched a laptop. Nothing left the building.

**Demo reality vs. production.** In the production vision the answer is spoken into the operator's ear over the glasses speaker. **In the demo it plays on the laptop speaker** — because the judges have to hear it, and because laptop-out is the de-risked path (the glasses-speaker downlink adds a resample plus delicate Bluetooth-audio routing; see §7). So in the demo the glasses are purely the *input*; the laptop is the brain box on the cart, doing the speaking and the screen.

## 2. Why glasses

**The job-to-be-done is unchanged from the core PRD** — *"A machine just faulted. Tell me the approved procedure to fix it safely, right now, without me leaving the machine."* Glasses sharpen the last clause: *without leaving, without stopping, without a free hand.*

The operator's reality is the argument. They're **gloved** (a touchscreen is unreliable), their **hands are full** (a tool, a part, a panel), and the fault is often **somewhere a fixed screen isn't** — behind, under, or inside the machine. A laptop-mic demo quietly assumes the operator walks back to a cart, holds push-to-talk, and reads a screen. The glasses delete all three: speak where you stand, hear it where you are, keep your eyes on the fault.

**Why not just a Bluetooth lapel mic?** A lapel mic solves *roaming audio* and nothing else — you'd still need a screen for the card. The glasses are the better *experience*, not just the better mic: audio in the ear, and — with the camera (a stretch, §7) — the path to ManuAI *seeing what the operator sees* ("what's this error code?" → the glasses read it). The mic is table stakes; **untethered + eyes-up, and eventually eyes-shared, is the point.**

The **secondary user** is still served: every interaction stays logged and grounded, and the on-screen card lives on as the supervisor / station / audit view (§3 of the core PRD; D3 here) even when the operator never looks at it.

## 3. What we're building (the MVP slice)

One sentence: **the glasses microphone replaces the laptop microphone.** The bridge is a new module — `src/glasses_bridge.py` — that wraps `offline_demo.py` in a WebSocket front-end: the verified **Whisper → `core.answer` → Kokoro-TTS + screen** processing loop is reused **verbatim**, and the bridge adds only a few small new pieces (listed in §4).

| | |
|---|---|
| **IN** | Glasses mic → Bluetooth **HFP** (Hands-Free Profile) → Float32 48 kHz mono PCM → `ws /publish-audio` |
| **OUT** | **Laptop speaker** (so the audience hears the answer) **+ laptop screen** — the live SOP card, citation (`SOP-1187 §4.2`), and ⚠ safety banner, exactly as `offline_demo` renders today |
| **NOT in the MVP** | Glasses-speaker output · video · photo capture · multi-turn memory |

**Why this slice.** The integration assessment describes the *broad* capability — video, photo, and audio *output to the glasses speaker*. We deliberately take the **narrow slice**: audio-in, answer-on-the-laptop. It's the part that is (a) fully offline with no unknowns, (b) reused almost entirely from working code, and (c) already enough to land the hands-free story. The broader features are tiered into the vision/stretch (§7), not built now.

**Brain reused as-is → one-shot.** Each utterance is an independent `core.answer(question, machine_id, retriever)` call — no session, no memory of the last turn. Multi-turn conversation and an in-memory session object (running transcript + confidence) are genuinely valuable, but they're a **core-brain** change that pays off across *every* input path — laptop, glasses, and LiveKit alike — not a glasses concern. They live in a **separate "conversational core" PRD** (see Appendix), not here.

## 4. How it works

The glasses never talk to the Mac directly. `mc-goggles` — Mitra Chem's internal, already-working iOS app that relays the Ray-Ban mic to a server over a raw WebSocket — does that hop. So **ManuAI's only job is to *be* the server** `mc-goggles` already talks to, for the audio path.

```
 Meta Ray-Bans        iPhone — mc-goggles (re-bundled com.joshua.manuai)        Mac — ManuAI
┌──────────┐ Bluetooth ┌──────────────────────────────────┐  LAN WebSocket  ┌─────────────────────┐
│ mic ─────┼── HFP ───►│ HFP audio → Float32 48 kHz mono   ├─ ws /publish-audio ──────────────►│ VAD segment         │
│          │           │  (audio/WS logic unchanged —      │                 │  → 48k→16k resample │
│ speaker  │ (unused — │   only the host is repointed)     │                 │  → Whisper (en)     │
│   ✗      │  output   │                                   │                 │  → core.answer      │
│ camera   │  is the   │                                   │                 │  → Kokoro TTS ─► 🔊 laptop speaker
│   ✗      │  laptop)  │                                   │                 │  → screen_state ► 🖥 laptop screen :8000
└──────────┘           └──────────────────────────────────┘                 └─────────────────────┘
  Glasses↔Phone = Bluetooth (no internet)        Phone↔Mac = LAN only (hotspot/router, no WAN)
```

The unmodified app opens three WebSockets at startup plus an occasional POST. The bridge **satisfies all four** so the app never errors or reconnect-loops, but only *acts* on audio:

| Path | App's intent | What the bridge does |
|---|---|---|
| `ws /publish-audio?agent=1` | mic uplink | **The real work** — JSON header → Float32 frames → VAD → resample → Whisper → brain → laptop TTS + screen |
| `ws /publish` | video uplink (JPEG) | Accept, drain & discard; reply `{"type":"video_off"}` so it stops sending video |
| `ws /agent-audio` | glasses-speaker downlink | Accept and **idle** — output is the laptop, so we send nothing |
| `POST /publish/photo` | full-res still | Return `200`, discard (only fires on a user capture) |

**One process, two servers, two ports:** the `websockets` library on **8766** for audio, and `offline_demo`'s stdlib HTTP server on **8000** for the screen (`GET /` → `screen.html`, `GET /state` → the live `screen_state` — the dict that drives the on-screen SOP card). Not FastAPI.

**Reused, not rebuilt** (imported from `offline_demo`): `transcribe_wav` (Whisper, `language="en"` pinned), `run_pipeline` (`core.answer` → render → **speak on the laptop**), `_set_latest`, `_start_http_server`, the `CosineRetriever` offline stub, `WHISPER_MODEL`, the VAD constants. **The only genuinely new code:** the 4 endpoints + WS framing, the **48k→16k resample** (`scipy.resample_poly`), the **streaming VAD** (mirror `record_until_silence`), and the **speaking-guard** (D8). Add `websockets` to `requirements.txt`.

**On the iOS side:** the relay is `mc-goggles`, **re-bundled as `com.joshua.manuai`** (change the bundle identifier and re-sign under our own Apple developer identity — all other `Info.plist` keys are preserved) and repointed at the Mac's LAN/hotspot IP (a single host constant). Its audio/WebSocket logic is *unchanged* — we re-identify and repoint it, we don't rewrite it.

**Verify without glasses.** A loopback `--selftest` replays a `synth_to_wav` utterance as Float32 48 kHz frames into `/publish-audio` and asserts the two canonical beats through the *full* WS path — jam → *answered* + SOP-1187 + the laptop speaks; bypass → *escalated* — mirroring `offline_demo.selftest`. It proves the bridge end-to-end with **zero hardware**, and doubles as the **live-demo backup** if the glasses flake on stage.

## 5. Offline analysis

This is the section that earns the "wifi-off" headline, so it's stated plainly.

| Link | Transport | Needs internet? |
|---|---|---|
| Glasses ↔ phone (audio) | Bluetooth **HFP** | **No** |
| Phone ↔ Mac | LAN WebSocket (hotspot or router) | **No WAN** |
| *(Possible)* DAT registration | Meta AI app, one-time | **Only if** the app forces a DAT session at launch — see below |

**The registration question (the one real unknown).** The online step everyone worries about — **DAT registration** (registering the app with Meta's glasses SDK) — exists only for the **camera/video** session. **Audio rides HFP through the iOS audio session and never touches DAT** (there is no `MWDATAudio` module). So an audio-only build *may* need **no registration at all** — just Bluetooth pairing + the iOS mic permission. We therefore **build and run without DAT registration first, then test the wifi-off / no-registration case** (launch the app cold, wifi off, no prior registration — does `/publish-audio` still connect and stream?). We fall back to a one-time online registration *only if* the unmodified app insists on a DAT session at startup. Best case: **the MVP has zero online steps, ever.**

**Networking without a router.** The phone↔Mac hop needs a *local* network, not the internet. Canonical demo path: **iPhone Personal Hotspot** — the phone *is* the router, both land on `172.20.10.x`, traffic stays on the WiFi radio with no tower involved (visibly no infrastructure on stage). Fallback: a local router with the **WAN unplugged** (same SSID). The app's `Info.plist` already permits plaintext `ws://` to private ranges (incl. `172.20.10.x`), so the hotspot needs no plist change — and the `com.joshua.manuai` re-bundle inherits that setting.

**The boundary, plainly:** once the glasses are paired (and registered *if* required, once), the entire loop — mic → STT → retrieve → LLM → TTS → screen — runs with the WAN unplugged. *Caveat to test:* a **SIM-less phone may hide the hotspot toggle** (§8).

## 6. Architecture & the path (why raw-WS, not WebRTC)

**Path A — raw WebSocket — is *the* path, not a stepping stone.** The reason is structural: per ARCHITECTURE **G16, WebRTC can't go offline** — that's the very reason ManuAI's wifi-off core is `offline_demo`, not the LiveKit pipeline. So the *only* offline-capable transport for the glasses is raw-WS — and `mc-goggles` already owns exactly that transport. Path A is both the lowest-effort *and* the only thesis-consistent choice.

**Path B — WebRTC / LiveKit — is parked, and not recommended.** It would align the glasses with ManuAI's *wifi-ON* LiveKit pipeline, but it **sacrifices offline** (G16) and runs straight into the AVAudioSession-vs-HFP routing risk the assessment flags (`PROTOCOL.md` §2.6: WebRTC wants to own the audio session; DAT's HFP routing is delicate). It only makes sense if you abandon the headline. We don't.

Framed positively for the pitch: **we deliberately *don't* use WebRTC here, because it can't go offline.** The constraint is the differentiator.

## 7. Scope & non-goals

**MVP (built):** audio-in from the glasses → answer on the laptop speaker + screen. One-shot. (§3.)

**Stretch — the vision tiers, in order:**
1. **Glasses-speaker output** — the true in-ear experience. Adds the `ws /agent-audio` downlink, an Int16 24 kHz resample, and careful AVAudioSession/HFP handling.
2. **On-demand photo to read the error code / nameplate** — *"what's the error code?"* → operator taps, glasses snap a frame → OCR / a vision model extracts the code or asset tag → scopes `machine_id` or seeds the query. The operator never reads a code aloud.
3. **Live video grounding** — ManuAI sees what the operator sees, continuously. Far-future: needs a VLM and continuous Bluetooth-Classic video.

*Tiers 2–3 drag in DAT registration and the camera de-risk test (§8); the MVP deliberately avoids both.*

**Non-goals:**
- Editing the iOS app's **audio/WebSocket logic** — we only re-bundle (`com.joshua.manuai`) and repoint the host.
- **WebRTC** for the MVP (§6).
- **Multi-turn / session memory** — that's the separate conversational-core PRD (Appendix), not glasses.

**Positioning — lead with Support.** The highlight is **grounded retrieval from the SOP knowledge base** (Moss — the local-first semantic retrieval runtime — plus cite-or-refuse) — that's the Support track. Glasses make that retrieval *hands-free*; they do **not** pivot us to a Co-Pilot/ambient framing. The product answers *discrete spoken questions* and grounds them in approved docs — the value is the *knowing*, not an ambient always-on display. (The MVP segments speech with **server-side VAD**; a glasses-button push-to-talk is future, since it needs app changes.) Glasses strengthen the Support story; they don't change the track.

## 8. Risks & open questions

| Risk / unknown | Mitigation / cheap test |
|---|---|
| **Does audio-only truly need *no* DAT registration?** | The MVP-gating unknown. Test: cold launch, wifi off, no prior registration → does `/publish-audio` stream? Fall back to one-time registration only if it doesn't. |
| **Registration token expiry** over a long offline run (if registration *is* needed) | Undocumented. If the demo runs offline for an extended period, register fresh beforehand and test duration. |
| **HFP VAD threshold** — laptop's `ENERGY_THRESHOLD=0.010` won't transfer to glasses-over-Bluetooth levels | Tuning, not design. Retune speech-start / silence-stop on-device before the demo. |
| **Echo / barge-in** — laptop speaker + open glasses mic re-triggers the loop | Best-effort speaking-guard (D8): drop incoming audio while the pipeline runs + a short cooldown. Not AEC; fine for a controlled demo. |
| **Always-listening VAD** false-triggers on a noisy floor | Controlled demo (close mic, retuned threshold). The hands-free *vision* wants a wake-word or glasses-button PTT — needs app changes, so it's future. |
| **SIM-less phone may hide the hotspot toggle** | Test on the actual demo phone; router-WAN-unplugged is the fallback network. |
| **Camera de-risk test** — does a *started* DAT camera session survive wifi-off? | **Stretch-only.** Gates the photo/video tiers (§7), *not* the MVP. Register online → wifi off → start a DAT session → observe. |
| **Live demo fails / no glasses on hand** | The loopback `--selftest` (§4) replays a synth'd utterance through the WS path — proves the bridge with zero hardware and is the stage backup; keep a recorded wifi-off run too. |

---

## Appendix — parked for the conversational-core PRD

**Status: parked — not in scope for this PRD.** These came out of the design interview but belong to the **brain**, not the glasses (they pay off on the laptop and LiveKit paths too). Captured here so the next (conversational-core) PRD picks them up:

- **D6 — Multi-turn / stateful loop** (vs. the one-shot MVP).
- **D7 — One in-memory session object:** `turns[]` where each turn *is* a `screen_state` (no new schema) · `confidence` = the per-turn retrieval `top_score`, tracked + shown (passive for now) · `last_retrieval` (carried SOP chunks) · reset via a **"Clear session" button** on the screen.
- **Open Q6 — multi-turn retrieval & refuse:** *Design 1* (bridge prepends a transcript window to the query; **zero `core.py` changes**) vs. *Design 2* (additive `history` / `carried_context` params on `core.answer`). Refusal routes through the existing cite-or-refuse few-shot either way (ARCHITECTURE G15).

## References

- `GLASSESINTEGRATION.md` — the integration assessment (effort, Paths A/B, offline analysis).
- `.claude/skills/glasses-bridge/{SKILL.md,reference.md}` — the build spec: wire contract, reuse map, pitfalls.
- `~/mc-goggles/PROTOCOL.md` §1 — the authoritative glasses↔server WebSocket contract.
- `docs/ARCHITECTURE.md` — G14 (Moss is cloud-anchored → use the stub offline), G15 (refusal via few-shot on the Moss path), G16 (WebRTC can't go offline).
- `src/offline_demo.py` · `src/core.py` · `src/retriever.py` — the pipeline the bridge wraps.
