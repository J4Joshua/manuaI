# ManuAI — PRD & Hackathon Plan

**The offline voice copilot for the factory floor.**
When a machine goes down, an operator presses a button, describes the problem out loud, and instantly gets the right procedure — read aloud and shown on screen, *cited to the exact SOP*, running entirely on a box in the building with **no internet required**.

> Conversational AI Hackathon @ Y Combinator · June 6–7, 2026 · Hosted by Moss (F25)
> Status: planning (high-level). Owner: Joshua. Last updated: 2026-06-05.

---

## 1. The problem

A machine on the line throws `E-42` and stops. Downtime costs ~$10k–$50k/hour. The fix is buried somewhere in hundreds of equipment manuals and SOPs — PDFs, binders, tribal knowledge. The operator standing in front of the machine, often gloved and hands-full, has to find it *now*.

Three things make this hard, and each maps to a sponsor capability:

| Constraint | Why it's hard | What unlocks it |
|---|---|---|
| **The answer is in hundreds of proprietary docs** | Manuals are dense, full of tables/diagrams, badly OCR'd | **Unsiloed** parses them into clean, structured, LLM-ready chunks |
| **It's needed in seconds, mid-conversation** | Traditional RAG (network hop → cloud vector DB → rerank) is too slow and unreliable | **Moss** does sub-10ms semantic retrieval, *local-first*, on the box |
| **Factories have bad/no wifi, and the data is proprietary** | Cloud agents stall or leak data; can't depend on connectivity on a plant floor | Everything at query time runs **on-prem**; data never leaves the building |

## 2. Why now (the hackathon thesis, applied)

Voice models are cheap and fast — they're no longer the bottleneck. **Retrieval is.** Moss collapses the multi-hop retrieval stack into a real-time, local-first runtime. That's the unlock that makes a *fluid, grounded, offline* factory copilot possible for the first time. ManuAI is the sharpest demonstration of exactly that thesis: a place where low latency, proprietary data, and no-connectivity are not nice-to-haves but hard requirements.

## 3. Target user & job-to-be-done

- **Primary user:** line operator / maintenance technician on the floor.
- **JTBD:** "A machine just faulted. Tell me the approved procedure to fix it safely, right now, without me leaving the machine or finding a manual."
- **Secondary:** plant/maintenance manager who wants every interaction logged and grounded in approved docs (compliance + audit).

## 4. Product principles (these are also the scoring story)

1. **Offline-first.** The query path runs with wifi off. This is the demo's single best asset.
2. **Grounded or silent.** Every answer cites the source SOP (`SOP-1187 §4.2`). If there's no documented procedure, it **refuses to guess and escalates** — never hallucinates a fix on safety-critical equipment.
3. **Safety first.** Lockout/Tagout and other safety steps surface *before* the repair steps, with a visual banner.
4. **Hands-busy / voice-first.** Push-to-talk, read answers aloud. The screen is glanceable context, not something to operate.
5. **Fast enough to feel like talking.** Target **≤1.5s from end-of-speech to first spoken word.**

---

## 5. User flow

### 5a. Runtime flow (what the judges see — all on-prem)

```
1. Operator at the machine puts on headset, selects/scans the machine
   (QR or asset tag → sets machine_id, which scopes retrieval).
2. Holds push-to-talk:  "The labeler on line 3 jammed and threw error E-42."
3. STT (local Whisper) transcribes → shows live on screen.
4. Query is embedded locally → Moss retrieves the matching SOP section
   in <10ms, filtered to this machine.
5. Local LLM (Qwen) composes a short, grounded answer from the retrieved
   text — and is forced to either cite a source or say it doesn't know.
6. Answer is spoken back (local TTS) AND the screen shows:
      • live transcript
      • the SOP card it used + citation ("SOP-1187 §4.2")
      • a ⚠ safety banner ("Lockout/Tagout required before clearing jam")
7. Follow-up not covered by docs ("can I bypass the interlock?") →
   agent refuses + flags supervisor (escalation card on screen).
8. THE MOMENT: wifi has been OFF the whole time. Nothing left the floor.
```

### 5b. Ingestion flow (done ahead of time, off the critical path)

```
Manuals/SOPs (PDF) ──► Unsiloed Parse ──► structured Markdown+JSON
  ──► chunk by procedure/section (keep parent-child + tables)
  ──► tag each chunk: {machine_id, manual_id, section, page, safety_flag}
  ──► embed each chunk with the LOCAL embedding model
  ──► load vectors + metadata into the Moss index
  ──► ship the index onto the edge box
```
This step can use the cloud freely (it runs once, when you *do* have wifi). Only the **runtime** path must be offline.

---

## 6. Tech stack (with the on-prem line drawn)

```
┌──────────────── CLOUD / OFFLINE — one-time ingestion, wifi available ────────────────┐
│  Unsiloed Parse API ─► chunk + tag metadata ─► LOCAL embed model ─► build Moss index  │
└───────────────────────────────────────────────────────────────────────┬──────────────┘
                                                                          │  index shipped
══════════════ EDGE BOX on the factory floor — RUNS WITH WIFI OFF ════════▼══════════════
  Operator 🎙 (headset, push-to-talk)
        │
        ▼     ┌───────────────── LiveKit Agents (self-hosted) ─────────────────┐
   speech ───►│  Whisper STT ─► LOCAL embed ─► Moss retrieve ─► Qwen LLM ─► TTS │──► 🔊 answer
        ▲     │   (local)       (SAME model    (<10ms, on-disk,   (local,        │
        │     │                  as ingest)     metadata filter)   cite-or-refuse)│
        └─────┤                                                                   │
              │  SCREEN: live transcript │ SOP card + citation │ ⚠ safety banner  │
              │           │ escalate-to-supervisor state                          │
              └───────────────────────────────────────────────────────────────────┘
```

| Layer | Choice | Sponsor | On/Off prem | Notes |
|---|---|---|---|---|
| **Doc parsing** | Unsiloed Parse API | **Unsiloed** | Cloud (offline/batch) | Vision-first; keeps tables, sections, parent-child. Output → chunks. |
| **Embedding** | Small local model (e.g. `bge-small` / `nomic-embed-text` via Ollama/MLX) | — | **On-prem** | ⚠ Must be the **same model** for index-time and query-time, or retrieval silently degrades. |
| **Retrieval** | **Moss** local runtime (Python SDK) | **Moss** ★ | **On-prem** | The star. Sub-10ms, on-device, metadata-filtered by `machine_id`. |
| **LLM** | Qwen2.5-Instruct (7B for quality / 3B for speed) via Ollama or MLX | **Qwen** | **On-prem** | Open weights → runs offline. Streams tokens to TTS sentence-by-sentence. |
| **STT** | Whisper (faster-whisper / MLX), small/distil | — (LiveKit plugin) | **On-prem** | Push-to-talk avoids wake-word fragility in a loud room. |
| **TTS** | Kokoro (quality+fast) or Piper (ultra-fast), local | — / Qwen / Minimax (optional) | **On-prem** | Qwen/Minimax voice = optional flourish (cloning, multilingual) if time allows. |
| **Voice orchestration** | **LiveKit Agents**, self-hosted (Apache-2.0) | **LiveKit** | **On-prem** | Runs the STT→LLM→TTS pipeline; supports local-inference plugins + token streaming. |
| **Screen / UI** | Simple local web app (LiveKit frontend SDK or plain React) | — | **On-prem** | Transcript + SOP card + citation + safety banner + escalation state. |
| **(Optional) Control plane** | AWS for index build/distribution; TrueFoundry as model gateway/observability | AWS / TrueFoundry | Cloud (opportunistic sync) | Roadmap, not MVP: a fleet of edge boxes that pull updated indexes when wifi exists. |

**The edge box = a MacBook (Apple Silicon).** MLX makes Qwen/Whisper/embedding fast enough for conversational latency, and it's a clean physical prop for the "this whole thing is one box on the floor" story.

**Latency budget (≤1.5s to first word):** endpointing ~300ms · STT ~200–400ms · embed+Moss <50ms · LLM first token ~300–600ms · TTS first audio ~150–300ms. Stream LLM→TTS per sentence; keep answers short/extractive.

## 7. Safety & trust design (the differentiator over a generic chatbot)

- **Citations, always.** The LLM prompt requires a source tag from retrieved chunks; the UI renders it as a tappable SOP card. Grounded in *their* docs, not the model's imagination.
- **Cite-or-refuse.** If retrieval returns nothing above a confidence threshold, the agent says: *"I don't have a documented, approved procedure for that — I'm flagging your supervisor."* On safety-critical equipment, **refusing to guess is a feature, not a failure.**
- **Safety-step ordering.** Chunks tagged `safety_flag` (LOTO, PPE, interlocks) are surfaced first and shown in a ⚠ banner.
- **Audit log.** Every Q→retrieved-source→answer→operator-action is logged locally (compliance + future fine-tuning). Cheap to add, strong for the pitch.

---

## 8. Scope

### MVP (must-have for the demo)
- [ ] Ingest **5–10 real-ish SOPs** for one machine, including one with a genuine **safety step**.
- [ ] Offline core loop: question → local embed → Moss (metadata-filtered) → Qwen → **cited** answer, **verified with wifi off**.
- [ ] Voice in/out via LiveKit (push-to-talk → Whisper → loop → TTS, streaming).
- [ ] Screen: live transcript + SOP card + citation + safety banner + escalation state.
- [ ] Scripted **refusal/escalation** path.

### Stretch (only if core is rock-solid)
- Multilingual (operator speaks Spanish → answer in Spanish) via Qwen/Minimax voice.
- QR-scan machine selection; per-machine index switching.
- Local audit-log viewer; "what did operators ask most" dashboard.
- Telemetry hook: machine PLC pushes the error code so the operator doesn't have to read it out.

### Explicit non-goals (for the hackathon)
- Real ruggedized hardware, multi-box fleet sync, fine-tuned models, auth/RBAC. Mention as roadmap; don't build.

## 9. Build plan (mapped to the schedule)

> **Do this in the first 30 min: kick off all model downloads** (Qwen, Whisper, embed, TTS). Multi-GB pulls over hackathon wifi are the #1 silent time-killer. Also: **prove the offline text loop before touching voice** — voice integration left to hour 18 is how demos die.

| When | Milestone | Work |
|---|---|---|
| **Sat 2:30–4:00p** | Scaffold + ingest spike | Repo + start model downloads. Push **one** SOP through Unsiloed → chunk+tag → local embed → into Moss. Confirm a query returns the right chunk. |
| **Sat 4:00–5:00p** | ☎ **Office hours** | Hit **Moss, LiveKit, Unsiloed** booths with the checklist in §11. (Moss embedding question is critical.) |
| **Sat 5:00–8:00p** | **M1: the brain works offline** | Typed question → embed → Moss (filter by `machine_id`) → Qwen w/ cite-or-refuse prompt → cited text answer. **Test with wifi OFF.** |
| **Sat 8:00p–12:00a** | **M2: you can talk to it** | LiveKit self-hosted: push-to-talk → Whisper → loop → TTS, streaming sentence-by-sentence. Tune latency. |
| **Sun 12:00–3:00a** | **M3: legible on stage** | Screen UI: transcript + SOP card + citation + ⚠ safety banner + escalation card. |
| **Sun 3:00–6:00a** | Content + polish | Ingest the full 5–10 SOPs incl. the safety scenario. Nail the refusal path. Latency tuning (model size, streaming). Rotate sleep. |
| **Sun 7:30–10:00a** | Harden + rehearse | Run the demo 5×. **Record a backup video of the wifi-off run.** Freeze code. |
| **Sun 10:00–11:00a** | **Submit** + buffer | |
| **Sun 1:00p** | Demo / judging | |

**If you have ~4 people**, parallelize after the scaffold: **A** = ingestion (Unsiloed→chunk→embed→Moss) · **B** = retrieval+LLM core + latency · **C** = voice (LiveKit/Whisper/TTS) · **D** = screen UI + demo content. A+B converge on M1; C plugs in for M2; D for M3. **Solo:** follow the sequence above and cut all stretch.

## 10. The 90-second demo script

1. **Set the stage (10s):** "This is ManuAI. It runs on this one box. Watch the wifi icon." *(turn wifi OFF now.)*
2. **The ask (15s):** Push-to-talk: *"The labeler on line 3 jammed and threw error E-42."*
3. **The grounded answer (25s):** Screen shows transcript → SOP card `SOP-1187 §4.2` → ⚠ *"Lockout/Tagout required."* Voice: *"First, perform lockout/tagout on the labeler. Then… Source: SOP eleven-eighty-seven."*
4. **The trust beat (20s):** *"Can I just bypass the interlock to keep the line moving?"* → Agent: *"I don't have an approved procedure for that, and it's safety-critical. I'm flagging your supervisor. Do not bypass the interlock."* → escalation card appears.
5. **The mic drop (15s):** "Everything you just saw — the retrieval, the reasoning, the voice — ran locally. The wifi has been off the entire time. On a real plant floor, the data never leaves the building." *(Have the backup video ready in case live fails.)*

## 11. Office-hours checklist (Sat 4pm — do not skip)

**Moss (highest priority — protects the demo):**
- Does query-time embedding run **locally**, or does Moss expect me to call a cloud embedding API? *(If cloud, the wifi-off demo breaks — I need a local embedder either way.)*
- Do I pass Moss **raw text** (it embeds) or **pre-computed vectors**? Which embedding model/dim do you recommend?
- How do I do **metadata filtering** (e.g. by `machine_id`) at query time? Python SDK example?
- How are index updates / instant updates handled?

**LiveKit:** Best path for a fully **self-hosted** pipeline with **local** STT/LLM/TTS plugins? Push-to-talk pattern? Sentence-level LLM→TTS streaming hook?

**Unsiloed:** Best parse output (Markdown vs JSON) for **procedure-level chunking** that preserves tables and section numbers for citations? Rate limits for ingesting ~10 docs today?

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| **Local query embedding needs network** (kills offline demo) | VERIFY at Moss office hours first thing; pick a local embed model regardless. |
| **5 local services miss conversational latency** | Prove offline text loop first; small models (Qwen-3B, distil-Whisper); stream LLM→TTS; short extractive answers; state the 1.5s target and optimize to it. |
| **Voice integrated too late** | Hard gate: M1 (offline brain) done by midnight before voice. |
| **Loud-room STT errors on stage** | Push-to-talk + headset mic; keep typed input as a stage fallback. |
| **Model downloads eat hours** | Start all pulls in the first 30 min. |
| **Live demo fails** | Pre-recorded backup video of the wifi-off run. |

## 13. Track positioning

Let the build decide. The core ask→retrieve→answer loop is fundamentally **Support** (instantly pull docs + history). Because we're also shipping the **live-context screen**, **Co-Pilot** ("ambient agents that display live context") is equally defensible — lead with whichever the room responds to. Don't build always-listening just to earn the Co-Pilot label; it's demo risk for no payoff.

## 14. One-line pitch

> "Voice AI made the talking cheap; Moss made the *knowing* instant and local. ManuAI puts every SOP in your factory one sentence away from the operator who needs it — grounded, safe, and working even when the wifi isn't."
