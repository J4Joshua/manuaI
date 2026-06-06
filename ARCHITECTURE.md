# ManuAI — Architecture

> Reconciled from a 6-agent phase simulation (one architect per phase) whose job was to find the gaps in the seams. This file is the **single source of truth for cross-phase contracts**. Where it disagrees with a phase doc, this wins. Vision + decisions: [PRD.md](./PRD.md). Phase goals/tests: [phases/](./phases/).

**Harmony verdict:** the phases compose **cleanly — conditional on two things:** (1) the **Phase 1.5 refactor** that turns M1 from a print-only CLI into an importable `answer() → screen_state`, and (2) holding every consumer to the **one `screen_state` contract** below. With those, Phases 2/3/4 are drop-in. The open risks are concentrated in the **wifi-off guarantee** (§7) — audit them before rehearsals.

---

## 1. System at a glance

Two planes. The **runtime plane** is 100% local and must survive wifi-off. The **ingestion plane** is cloud + one-time and never touches the live query path.

```
INGESTION PLANE (cloud, one-time, wifi OK)          RUNTIME PLANE (local, wifi OFF)
─────────────────────────────────────────          ────────────────────────────────────────
 real manuals ─▶ Unsiloed ─▶ normalize ─┐            mic / typed ─▶ core.answer() ─▶ voice + screen
                                         ▼                              │
                          local embed (nomic) ─▶ Moss index ◀───────────┘  (retriever.search)
```

Everything in the runtime plane talks only to `localhost`.

---

## 2. File structure

**Current (M1 as-built — validated):**
```
manuai/
├── PRD.md                  vision · Decision Log D1–D10 · solo build plan
├── ARCHITECTURE.md         ← this file
├── README.md               M1 run instructions
├── chunks.json             6 hand-authored SOP chunks (the corpus + schema)
├── common.py               local Ollama calls (embed, chat_json) + cosine   [stdlib only]
├── ingest.py               embed chunks → index.json
├── ask.py                  M1 query loop — CLI, PRINTS today
├── index.json              built vector index (the Moss stand-in)
└── phases/                 phase-0…5 docs + index
```

**Planned (after the refactor + phases — names are the contract, not mandates):**
```
manuai/
├── core.py            answer(question, machine_id, retriever) -> screen_state     [Phase 1.5]
├── retriever.py       Retriever seam: CosineRetriever (stub) | MossRetriever       [Phase 1.5 + 0]
├── render.py          terminal render(screen_state)                                [Phase 1.5]
├── ask.py             thin CLI shim over core.answer + render
├── test_beats.py      regression: the 4 canonical beats pass/refuse                [Phase 1.5/5]
├── moss_hello.py      Phase 0 proof script (3 vectors in, query, wifi off)         [Phase 0]
├── server.py          local http.server: serves screen.html + SSE feed            [Phase 2]
├── screen.html        5-panel UI; single entry point applyState(screen_state)      [Phase 2/3]
├── agent.py           LiveKit agent: STT → core.answer → TTS + data-channel push   [Phase 3]
└── unsiloed_ingest.py PDFs → Unsiloed → normalize → embed → Moss                    [Phase 4]
```

---

## 3. The core contracts (the harmony anchors)

### 3a. Chunk / record schema — the source-agnostic data model
Identical whether hand-authored (`chunks.json`) or Unsiloed-produced (Phase 4). `text` is embedded + shown to the LLM; everything else is metadata; `id` is the citation key.
```
{ id, sop_id, section, page:int, machine_id ("labeler-line-3" | "all"),
  doc_type ("troubleshooting"|"maintenance"|"safety"|"operation"),
  procedure_title, error_codes:[str], safety_flag:bool, safety_warnings:[str],
  steps:[str], text:str, vector:[768 floats] }
```
A **record** (what the retriever returns) = this chunk **minus `vector`, plus `score:float`**.

### 3b. `screen_state` — THE universal contract ⭐
The one object every consumer programs to. Produced once by `core.answer()`; consumed **identically** by the CLI renderer, the SSE screen (Phase 2), and the LiveKit data channel (Phase 3). The renderer never changes between phases.
```
screen_state = {
  "question":        str,                    # transcript or typed input
  "machine_id":      str,                    # e.g. "labeler-line-3"  (CLI arg is --machine)
  "status":          "answered" | "escalated",
  "answer":          str,                    # the spoken line, in BOTH states
  "citations":       [ {sop_id, section, page:int, procedure_title} ],
  "steps_source":    {sop_id, section, procedure_title} | null,   # header for the steps panel
  "steps":           [str],
  "safety_warnings": [str],
  "top_score":       float,
  "threshold":       float                   # makes the gate legible on stage
}
```
**Invariants (enforced in `core.answer`, not hoped for):**
- `status=="escalated"` ⇒ `citations=[]`, `steps=[]`, `steps_source=null`, `safety_warnings=[]` (but `answer` carries the escalation reason — and, for a *policy* refusal, the `citations` MAY name the policy, e.g. `SAFE-001`).
- `status=="answered"` ⇒ `len(citations) >= 1`.
- **Never** contains `vector` or `text`. Always JSON-serializable.
- **Every** exit path returns one — including all three refusal branches (gate miss, JSON-decode fail, escalate/no-cite). *Today M1 prints and returns `None` on refusal; that is gap G4.*

### 3c. Retriever seam — the Moss swap point
```
class Retriever(Protocol):
    def search(self, question: str, machine_id: str, k: int = 3) -> list[record]: ...
        # embeds the query LOCALLY (nomic), returns top-k records sorted by score desc
```
- **`CosineRetriever`** (stub, today): loads `index.json` once at construction (not per call), cosine over vectors, `machine_id ∈ {machine,"all"}` filter, top-k.
- **`MossRetriever`** (Phase 0 → swap): Moss top-k query. **Must normalize `score` to cosine-equivalent [0,1], higher-is-better**, or the 0.70 gate silently inverts (gap G7).

### 3d. The brain
```
core.answer(question, machine_id, retriever) -> screen_state
   retriever.search → THRESHOLD GATE (deterministic) → chat_json (forced JSON)
   → validate cited ⊆ retrieved → assemble screen_state (all fields from metadata)
```

---

## 4. Critical data flow — runtime query (text & voice share this spine)
```
input  (CLI arg | typed box | STT transcript)
  │
  ▼   core.answer(question, machine_id, retriever)
  ├─ retriever.search(q, machine_id, 3)
  │     embed(q,"query") ──Ollama nomic (local)──▶ [Cosine over index.json ⇒swap⇒ Moss] ▶ top-3 records+score
  │
  ├─ THRESHOLD GATE  top.score < 0.70 ? ──yes──▶ screen_state{status:"escalated"} ─────────────┐
  │                                       no                                                   │
  ├─ chat_json(sys,user) ──Ollama qwen2.5:3b, format:json──▶ {answer, used_chunk_ids, escalate}│
  │     validate cited ⊆ retrieved ;  escalate/no-cite ──▶ escalated ────────────────────────┤
  │                                                                                            │
  └─ screen_state{status:"answered", answer, citations[], steps, steps_source, safety[], …} ──┤
                                                                                               ▼
                                            ONE screen_state dict (§3b)
        ┌───────────────────────┬──────────────────────────────┬───────────────────────────────┐
        ▼                       ▼                              ▼                               ▼
   render() → terminal    SSE → screen.html              LiveKit data channel            TTS speaks `answer`
   (ask.py CLI)           (Phase 2 transport)            → screen.html (Phase 3)         (Phase 3, sentence-streamed)
```
**Voice nuance (Phase 3):** the spoken `answer` is **streamed** sentence-by-sentence to TTS for latency, but the `screen_state` is assembled only after the **full** JSON parses (steps/citations/safety need the complete object) — so the card "pops in" ~100–300 ms after audio starts. A gate-miss refusal is a fixed string → no LLM, no streaming, *faster* than a hit.

## 4b. Data flow — ingestion (Phase 4, off the critical path)
```
real manual PDFs ─▶ Unsiloed [ Split? ▶ Parse(→chunks+pages) ▶ Extract(custom schema→codes/safety/steps) ]
   ─▶ normalize to §3a chunk schema  ─▶ embed(text,"document") Ollama nomic ─▶ Moss index (+ hand-authored)
   (CLOUD · one-time · produces records byte-identical to hand-authored → nothing downstream changes)
```

---

## 5. Systems & processes at runtime
| Process | Role | Endpoint | Talks to |
|---|---|---|---|
| **Ollama** (official app) | Qwen2.5-3B + nomic-embed | `localhost:11434` | core, retriever, agent |
| **Moss** | local vector retrieval | in-proc / local (TBD) | retriever |
| **livekit-server** | media + data routing | `ws://localhost:7880` | agent, frontend |
| **agent.py** | STT → core.answer → TTS + data-channel | joins LiveKit room | Ollama, Moss, LiveKit |
| **server.py** | serves `screen.html` + SSE | `localhost:8080` | browser |
| **browser** (`screen.html`) | the 5-panel UI | — | server (Phase 2) / LiveKit (Phase 3) |

**Memory budget (16GB Mac, resident at demo time — fits with ~9GB free):**
| macOS+browser | Qwen-3B q4 | nomic | distil-whisper | Piper | livekit-server | agent | **Total** |
|---|---|---|---|---|---|---|---|
| ~3.0 | ~2.5 | ~0.3 | ~0.4 | ~0.1 | ~0.1 | ~0.3 | **~6.7 GB** |

Steady-state is fine; the real risk is **Ollama cold-start** eviction (`OLLAMA_KEEP_ALIVE=5m` default) → set to `1h` + warm-up query. Use `OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0`.

---

## 6. The Phase 1.5 refactor (prerequisite for Phases 2 & 3)
M1 is validated but its logic lives in `ask.py:main()` and **prints** — it is not importable and emits no object. Before any UI/voice work, extract (behavior byte-identical):
- `retriever.py::CosineRetriever.search(...)` — the Moss swap seam; loads index once.
- `core.py::answer(question, machine_id, retriever) -> screen_state` — gate, LLM, validation, assembly; **no prints, no `sys.exit`**; every branch returns a `screen_state`.
- `render.py::render(screen_state)` — all current `print()` logic.
- `ask.py` → 10-line CLI shim over the three.
- Also: move Ollama HTTP calls behind an interface that can run in a **thread executor** (the sync `urllib` call blocks LiveKit's async loop — gap G9).

This is ~30–45 min and unblocks everything downstream. It does **not** change what M1 does.

---

## 7. Gap register (ranked by demo-threat)
Found by the audit; owner = the phase that fixes it.
| # | Gap | Owner | Fix | Status |
|---|---|---|---|---|
| G1 | **LiveKit starter defaults to `wss://*.livekit.cloud`** → voice dies the instant wifi is off | P3 | Force `LIVEKIT_URL=ws://localhost` + local token; **DoD = a wifi-off push-to-talk round-trip in isolation** | OPEN |
| G2 | **M1 prints, not returns; not importable** | P1.5 | The §6 refactor (`core.answer → screen_state`, `Retriever`, `render`) | OPEN |
| G3 | **No typed-input fallback (R2)** — only path to the screen is voice | P2 | Text box → `core.answer` → same `screen_state`; precedes cloud voice in the ladder | OPEN |
| G4 | **Refusal branches emit nothing** → escalation card never shows | P1.5/P2 | All 4 exits return `screen_state`; escalated carries the policy `citations` for "per SAFE-001" | OPEN |
| G5 | **Frontend CDN assets** (fonts/JS) 404 when offline | P2 | Bundle + serve all assets locally; zero CDN | OPEN |
| G6 | **Only 2 of ~5 models pre-pulled** (Whisper/Piper/Kokoro download on first use) | P0/P3 | Pre-pull all + verify offline (§9) | OPEN |
| G7 | **Moss score metric unknown** → 0.70 gate may invert silently | P0 | Confirm metric/range/direction at office hours; normalization shim in `MossRetriever` | OPEN |
| G8 | **Threshold is a window, not a number**; Phase 4 can break the refusal beat; README was stale | P1.5/P5 | `test_beats.py` run after every corpus/threshold change | README **FIXED**; test OPEN |
| G9 | **Sync `urllib` blocks the async agent loop** | P3 | Run Ollama calls in a thread executor / async client | OPEN |
| G10 | **`machine_id` filter falls back to the *whole* index** when empty → wrong-machine SOPs at scale | P4 | Drop the silent fallback or surface it; fine at 6 chunks | OPEN |
| G11 | **Ollama cold-start** blows the ≤1.5s latency target | P3/P5 | `OLLAMA_KEEP_ALIVE=1h` + warm-up; disable app update-check | OPEN |
| G12 | **Unsiloed won't cleanly give** `machine_id` / `safety_flag` / stable `id` / `steps[]` | P4 | Normalizer rules: machine_id = manual ingest arg; default `safety_flag=true` < 0.7 conf + WARNING/CAUTION regex; id = stable slug; steps fallback to text | OPEN |
| G13 | **`format:json` over Ollama's OpenAI-compat path + streaming** is underspecified | P3 | `response_format:{type:"json_object"}` or prompt-enforced; buffer full stream then parse for `screen_state` | OPEN |

---

## 8. Wifi-off airtightness checklist (the headline depends on this)
- [ ] LiveKit: `LIVEKIT_URL=ws://localhost`, local token, **no** cloud URL (G1).
- [ ] Frontend: all fonts/JS/CSS bundled + served locally, no CDN (G5).
- [ ] All models pre-pulled and verified offline (G6, §9).
- [ ] Ollama app auto-update / telemetry dismissed; `OLLAMA_KEEP_ALIVE=1h` (G11).
- [ ] Moss query path makes **no** network call — cold test with wifi physically off (G7).
- [ ] No NTP / analytics / model-update beacons fire mid-demo.
- [ ] **Gate:** one full push-to-talk round-trip with **wifi physically off**, in isolation, before any rehearsal (this is Phase 3's DoD).

## 9. Pre-pull model checklist (do before the venue)
`qwen2.5:3b` ✅ · `nomic-embed-text` ✅ · **Whisper** (distil-small.en) ☐ · **Piper** voice ☐ · Kokoro (only if used) ☐ — then confirm each loads with wifi off.

## 10. Fallback ladder (Phase 5)
| Rung | Mode | Trigger | Wifi-off headline? |
|---|---|---|---|
| **R1** | Full local voice (Whisper + local TTS + self-hosted LiveKit) | default | ✅ full |
| **R2** | **Typed input** in the frontend + screen rendering | STT mishears / mic / TTS / LiveKit flaky, but LLM+Moss up | ✅ intact |
| **R3** | Cloud STT/TTS | local voice stack won't start at all | ❌ drop the "wifi's been off" line |
| **R4** | Recorded backup video (M1 terminal, wifi-off visible) | anything visibly broken live | ✅ (recorded offline) |

R2 must precede R3 — typed input keeps the headline; cloud voice forfeits it. **R2 does not exist yet (G3).**

## 11. Open verifies — office hours (4pm)
- **Moss:** accepts pre-computed 768-dim vectors (insert + query)? score metric/range/direction? returns metadata or just `{id,score}`? server-side `machine_id` filter? upsert/rebuild? any network call (test cold)? → resolves G7, shapes `MossRetriever`.
- **LiveKit:** self-host local-only (no Redis), `ws://localhost`, local token mint? → resolves G1.
- **Unsiloed:** Parse chunk granularity control, custom Extract schema, rate limits for ~10 docs. → shapes G12.

---

## 12. Support-repo integration (2026-06-06) — what changed
Pulled `J4Joshua/manuaI` (origin/main): **8 `.claude/skills/`** (moss, ollama, qwen, mlx, livekit-agents, whisper-stt, local-tts, unsiloed), a **real `data/` corpus** (2 machines · OEM PDFs + authored SOPs + `manifest.json` with demo scenarios & intentional gaps), **pre-built Moss test scripts**, a pinned `requirements.txt`, and a documented `.env` (Moss creds present). Our M1 code + planning docs are preserved (untracked). The skills did primary-source research that **resolves several gaps and surfaces one new high-severity risk.**

### 12a. ⚠ Moss is cloud-anchored — the wifi-off story is narrower than we assumed (NEW · high severity)
Confirmed in the `moss` skill (introspected + smoke-tested against `inferedge-moss==1.0.0b19`):
- **Per-query is genuinely local** (~7 ms, embeds on-device, no network) — the offline claim holds *for queries*.
- **BUT `create_index` builds in the cloud, `load_index` fetches the snapshot over the network (~10 s), and auth validates over the network once.** Python b19 has **no disk-cache / cold-offline-load** (the JS `cachePath` is JS-only).
- **∴ the only working wifi-off sequence: authenticate + `load_index` while ONLINE → keep the Python process ALIVE → turn wifi OFF → query locally.** A process **restart while offline**, or a **token expiry mid-demo**, breaks it. (= **G14**.) `scripts/moss_offline_test.py` rehearses exactly this (`--coldload` proves the cold-offline failure).
- **Consequence:** our **cosine stub (`index.json` on disk) is the *more* robustly-offline path** — it cold-loads from disk with zero network, ever. So the stub is not just dev-convenience: it's the **bulletproof-offline fallback and the backup-video engine.** Keep both retrievers — **Moss = the sponsor-tech "real" demo, stub = the guarantee.**

### 12b. Embedding — let Moss embed (D6 nuance)
Moss ships a **built-in on-device embedder** (`moss-minilm` default); you pass **raw text** and it embeds index + query locally — which already satisfies D6's real goal (local query embedding). So `MossRetriever` passes **raw text** (not a vector); `CosineRetriever` keeps **nomic-embed-text**. Parity holds *within* each retriever. The two have **different score scales** → the **0.70 gate is stub-only; Moss needs its own re-tune** (`.score` is hybrid semantic/keyword, `alpha=0.8`). The Retriever seam (§3c) abstracts both; `answer()` is unchanged.

### 12c. Gap-register updates
- **G1 (LiveKit cloud default) → ADDRESSED in config:** `.env` sets `LIVEKIT_URL=ws://127.0.0.1:7880` + `livekit-server --dev`. Still verify the agent/frontend read it (DoD: wifi-off round-trip).
- **G6 (model pre-pull) → SPECIFIED:** `requirements.txt`/`.env` pin `mlx-whisper` (`whisper-small`), `kokoro-onnx`, `mlx-lm` (Qwen, MLX-first; our Ollama is the supported alt), `HF_HUB_OFFLINE=1` for the edge box. Pre-pull weights + verify offline still required.
- **G7 (Moss score metric) → RESOLVED (smoke test, 2026-06-06):** ran `moss_smoke_test.py` on the live project — creds valid; **query ~5 ms local**, `load_index` ~6 s (the network step — confirms G14); **`$eq machine_id` filter verified non-leaking**; **`.score` is normalized [0,1], higher=better** (top match = 1.000) → same direction as the cosine gate, so the gate won't invert. **The threshold *value* still needs Moss-specific tuning** on the real corpus (Moss strong matches hit ~1.0 vs nomic ~0.8). Embedding dim not exposed (irrelevant while letting Moss embed).
- **G8 (refusal beat vs corpus) → DE-RISKED by data:** `manifest.json → intentional_gaps` names the queries to **never** ingest — the refusal beats are designed into the corpus.
- **NEW G14 (Moss cloud-anchored load/auth):** see 12a. Owner P0/P5. Mitigation: load-online→stay-alive sequence + cosine-stub fallback + rehearse with `moss_offline_test.py`.

### 12d. Corpus upgrade (supersedes the toy `chunks.json`)
`data/` is the real corpus: **labeler-line3** (Label-Aire 3115NV PDF + SOP-1187 jam/E-42, SOP-1190 LOTO, SOP-1192 fault-code-ref) and **cobot-cellA** (UR20) as a second scenario, each with a demo script in `manifest.json`. Adopt it: ingestion reads `data/machines/*/sops/*.md` (rich frontmatter maps ~1:1 to §3a), and Phase 4 adds the OEM PDFs via Unsiloed. **Standardize `machine_id` on `labeler-line3`** (repo spelling; our `chunks.json` used `labeler-line-3`).

### 12e. Moss office-hours questions (supersede the §11 Moss bullet)
1. Can `load_index` + auth run **fully offline (cold start)** in Python after a one-time online validation — any disk-persist param like JS `cachePath`? *(protects wifi-off)*
2. Exact **embedding dimension** of `moss-minilm` / `moss-mediumlm`; which for technical manuals?
3. Built-in embedding on **Apple-Silicon GPU/MLX** or CPU?
4. Does a cached auth token **expire and force a network call** mid-session? *(protects the demo)*
5. Numeric vs string **filter comparison** semantics (for `page` `$gt`).

### 12f. Moss demo run (2026-06-06) — works end-to-end, with one structural caveat
Built the real index (`moss_ingest.py`: **21 section-chunks** from `data/.../sops/*.md`, tagged by `machine_id`) and ran `moss_demo.py` (retrieve → gate → Qwen-3B cite-or-refuse → render) on four queries:
- **labeler jam E-42** → answers LOTO-first + cites SOP-1187 + safety banner ✅
- **bypass interlock** (the scripted refusal) → escalates, grounded in the "no approved procedure" SOP text ✅
- **cobot C4** → answers + cites SOP-2201; **`machine_id` filter cleanly separated cobot from labeler** ✅
- **servo drive timing** (off-domain probe) → escalates ✅ *(after the fix below)*

**Structural finding — the absolute threshold gate is UNUSABLE on Moss.** Moss `.score` is **per-query normalized** (top hit ≈ 1.000 every query; whole range ~0.85–1.0; confirmed at `alpha=0.8` and `1.0`). So **no absolute threshold** passes the good queries (top ≈ 1.0) yet rejects an off-domain-but-same-machine query (servo top = 0.968). The deterministic gate (D8) therefore **works only on the nomic-cosine stub** (raw, non-normalized cosine — servo there = 0.648 < 0.70). On Moss, refusal must come from the **LLM task-match judgment**:
- A **3B won't do the task-match from instructions alone** (it repurposed LOTO steps for the servo query even with an explicit rule). **A single few-shot negative example fixed it** — servo now escalates, legit queries still answer. That few-shot is now the load-bearing refusal mechanism on the Moss path.
- **Net (G15):** D8's gate is **stub-only**; on Moss, refusal = strict prompt **+ few-shot**. Two more reasons the stub stays: a real deterministic gate *and* bulletproof-offline. (`moss-mediumlm` or a 7B grounding model may discriminate better — untested.)

Artifacts: `moss_ingest.py`, `retriever.py` (`MossRetriever` = the §3c seam), `moss_demo.py`. Index `manuals` (21 chunks) is live in the project.

---

## 13. Build status — overnight run (branch `build/demo-mvp`, not pushed)

**Done + verified (committed):**
- **Phase 1.5 refactor** — `core.answer(q, machine, retriever) → screen_state` over the Retriever seam (`CosineRetriever` stub gate 0.70 / `MossRetriever` gate None, G15); `render.py`; thin `ask.py`; shared `corpus.py` chunker; `ingest_local.py`. (`68b33c4`)
- **`test_beats.py`** — 4-beat regression, all pass on the stub. (`b659466`)
- **Phase 2 screen** — `server.py` + `screen.html`: one `applyState(screen_state)` renderer, `/state` poll, `/ask` typed-input (the **R2 fallback**), fully inline/no-CDN. Verified jam→answered+SOP-1187, bypass→escalated. (`16416cd`)

**Scaffolded (code + syntax-checked; NOT run — need your hardware/keys):**
- **Phase 3 `agent.py`** — LiveKit 1.5.x voice; `core.answer` is the brain; publishes `screen_state` over the data channel; `LIVEKIT_URL` defaults to `ws://127.0.0.1:7880`. Flagged 1.5.x API assumptions to verify once deps install. (`6405f2c`)
- **Phase 4 `unsiloed_ingest.py`** — PDF → Parse/Extract → `corpus` schema → Moss; needs `UNSILOED_API_KEY`; field-mapping table inside. (`6405f2c`)

**Gap-register deltas:** G3 (typed-input) → **addressed** (screen.html). G5 (CDN offline) → **addressed** (inline). G1 (LiveKit cloud default) → **partly** (`agent.py` defaults local; still needs the wifi-off round-trip DoD). G8 → **addressed** (`test_beats.py`).

**New handoff notes:**
- `MossRetriever.search` returns `page=None`; Phase 4's page-citation DoD needs it to read `md.get("page")` from Moss metadata (page is stored at ingest).
- Implemented `screen_state` **extends §3b** with `safety_flag: bool` + a bounded `source_excerpt: str` (≤500 chars) — Phase 2/3 consumers rely on these.
- Real-corpus chunks have no structured `steps[]` (steps live in the section `text` / `source_excerpt`); the numbered-steps panel only fills for corpora that carry `steps[]`. Optional enhancement: parse §4 Procedure into `steps[]` in `corpus.py`.
