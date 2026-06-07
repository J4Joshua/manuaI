---
name: moss
description: Build and query a local-first, sub-10ms semantic index with Moss, including metadata filtering by machine_id. Use when implementing ManuAI's offline retrieval step or its ingestion-time index build.
---

# Moss — local-first, sub-10ms semantic retrieval (the STAR of ManuAI)

Moss (YC F25) is a real-time semantic-search runtime that runs **in-process** — browser, edge, on-device, or cloud — so retrieval returns from **local memory with no network round trip**. It collapses the multi-hop RAG stack (embed → vector DB → network → rerank) into one local runtime. In ManuAI it is the **offline-critical** component: push-to-talk → Whisper STT → **Moss retrieve (<10ms, on-disk, filtered by `machine_id`)** → Qwen → TTS, on one MacBook with no internet at query time.

## When to use this skill

- Building the **ingestion-time index** (chunks + metadata → Moss index → ship to the edge box).
- Implementing the **runtime retrieval step** (embed query locally → top-k from Moss → filter by `machine_id`).
- Wiring Moss into the LiveKit/voice loop, or into the Qwen cite-or-refuse prompt.
- Debugging poor retrieval (almost always an **embedding-parity** or **filter-syntax** issue).

## Verification status (read first)

Researched against primary sources on **2026-06-06**: moss.dev, docs.moss.dev (incl. `/llms.txt`, Python API reference, indexing, metadata-filtering, local-embeddings, storage-persistence, offline-first, real-time-indexing, LiveKit), the `usemoss/moss` GitHub README, and the YC launch.

**CONFIRMED from docs + PyPI (2026-06-06):**
- Python SDK is real: **`pip install inferedge-moss`** (PyPI `inferedge-moss`, v1.0.0b19, uploaded 2026-03-24, **Rust core**, "sub-millisecond retrieval with zero network latency"). **⚠ Import as `inferedge_moss`, NOT `moss`** — the docs' `from moss import …` is aspirational; the shipping wheel imports as `inferedge_moss`. **All methods are `async`** (use `await`).
- Built-in **on-device** embedding models (`moss-minilm` default, `moss-mediumlm` higher-accuracy). Pass **raw text**; Moss embeds it locally. Custom precomputed vectors supported via `model_id="custom"`.
- Core API (**introspected from the installed SDK 2026-06-06; all `async`**): `MossClient(project_id, project_key)`, `create_index(name, docs, model_id=None)`, `load_index(name, auto_refresh=False, polling_interval_in_seconds=600)`, `query(name, query, options=None) -> SearchResult`, `add_docs(name, docs, options=None)`, `delete_docs(name, ids)`, `get_docs`, `get_index`, `list_indexes`, `delete_index(name) -> bool`, `unload_index`. **⚠ `session()` and `push_index()` are NOT on `MossClient`** (the docs imply otherwise) — sessions / cloud-push are a separate path.
- Object shapes (introspected): `DocumentInfo(id, text, metadata, embedding=None)`; `QueryOptions(top_k, alpha, filter, embedding)`; `MutationOptions(upsert)`; `SearchResult(docs, index_name, query, time_taken_ms)`; each hit is `QueryResultDocumentInfo(id, text, metadata, score)`.
- **Metadata** attached per-document via `metadata={...}`; **filtering is evaluated on the locally-loaded index** with `$eq $ne $gt $gte $lt $lte $in $nin $near` composed by `$and`/`$or`. `machine_id` exact-match via `$eq` **empirically verified — a `labeler-line3` filter returned only the labeler doc, no leak.**
- **Per-query: no network — empirically confirmed.** On this project: **`query` ~7ms** (in-process), vs **`load_index` ~10s** (the cloud fetch) and **`create_index` ~5.5s**. The latency split *is* the offline story: load = network, query = local. (Reproduce with `scripts/moss_smoke_test.py`.)
- Local updates: `add_docs` / `delete_docs` mutate the in-memory index immediately; `push_index()` optionally syncs to cloud ("no server-side re-embedding").

**⚠ OFFLINE ARCHITECTURE REALITY (this changes the demo plan — read carefully):**
Moss is **local-first but cloud-anchored.** Per the docs: *"The canonical copy is always the cloud index — your local process holds an in-memory snapshot for fast retrieval."*
- `create_index` **builds the index in the cloud**; `load_index` **fetches** that snapshot into memory (a network call at load time).
- The disk cache that lets a reload **skip** the network fetch (`cachePath` on `loadIndex`) is **JS-SDK ONLY**. The **Python** `load_index(name, auto_refresh, polling_interval_in_seconds)` exposes **no documented cache/persist/offline-path parameter** — so there is no documented way in Python to cold-load a prebuilt index purely from disk while offline.
- **Auth needs the network once:** "project credentials are validated when a session is opened"; afterwards "tokens are cached and auto-refreshed." No documented air-gapped / offline-license mode. The production checklist *does* say "test offline mode" and "keep data local / persist indexes to a durable path," so an offline path is *intended* to exist — but its Python API is undocumented as of today.

**What this means for the wifi-OFF demo:** the path that works **today, per the docs** is — **authenticate + `load_index` while ONLINE during setup, keep the Python process ALIVE, then turn wifi OFF** → every query then runs locally with no network. Stage failure modes to avoid: a **process restart** while offline (re-`load_index` → cloud fetch), or **token expiry** mid-demo. **Escape hatch for a hard offline guarantee:** `model_id="custom"` — embed locally yourself (Ollama/MLX) and supply vectors at index time + `QueryOptions.embedding` at query time (raises `ValueError` if missing; "all vectors must share the same dimensionality"). That removes the built-in embedder, but you still must confirm `load_index`/auth can run offline. **Rehearse the exact wifi-off sequence before the demo** and put Q1/Q4 below at the top of the Moss office-hours list.

**STILL UNVERIFIED (ask at office hours):**
1. Can `load_index` + auth run **fully offline** (cold start, wifi off) in Python — and is there a Python disk-persist parameter equivalent to JS `cachePath`? (Q1/Q4)
2. **Embedding dimensions** of `moss-minilm` / `moss-mediumlm` (not published anywhere; `custom` only requires "all vectors share the same dimensionality").
3. Whether built-in embedding uses **Apple-Silicon GPU/Metal** or CPU (Rust core, device unstated).

Treat the Quickstart below as a **working pattern that must be rehearsed offline**, not a guarantee, until Q1/Q4 are confirmed.

## Quickstart — build an index + metadata-filtered query (PROPOSED PATTERN, pending office-hours confirmation on auth/persistence)

```python
import os, asyncio
from inferedge_moss import MossClient, DocumentInfo, QueryOptions, MutationOptions  # NOT `moss`

client = MossClient(os.getenv("MOSS_PROJECT_ID"), os.getenv("MOSS_PROJECT_KEY"))

# --- INGESTION (one-time, cloud OK): chunks + metadata -> index ---
docs = [
    DocumentInfo(
        id="cobot-cellA::m12::sec4.2::p37",
        text="To clear a joint-overtravel fault, power-cycle the controller ...",
        metadata={
            "machine_id": "cobot-cellA",
            "manual_id": "m12",
            "section": "4.2",
            "page": "37",
            "safety_flag": "true",
        },
    ),
    # ... thousands more
]
async def build():
    # Pass RAW TEXT; Moss embeds on-device with the chosen built-in model.
    await client.create_index("manuals", docs, "moss-minilm")  # model id is index-time choice

# --- RUNTIME (offline target): load once, then query in-process, filtered by machine_id ---
async def retrieve(query_text: str, machine_id: str):
    await client.load_index("manuals")          # loads into memory (persist via disk cache once confirmed)
    res = await client.query(
        "manuals",
        query_text,                              # embedded LOCALLY at query time
        QueryOptions(
            top_k=5,
            alpha=0.8,                           # 1.0=pure semantic, 0.0=pure keyword; default 0.8
            filter={
                "$and": [
                    {"field": "machine_id", "condition": {"$eq": machine_id}},
                ]
            },
        ),
    )
    return res.docs   # each hit: .id .text .metadata .score -> feed to Qwen with citations
                      # res also has .time_taken_ms / .index_name / .query

asyncio.run(build())
```

Notes:
- The `filter` dict structure is **confirmed**: a list of `{"field": <key>, "condition": {<op>: <value>}}` entries under `$and`/`$or`. A single condition can stand alone without a wrapper.
- Metadata values in the docs' examples are strings; if you need numeric range filters (`$gt` on `page`), store/compare consistently and confirm numeric-vs-string handling at office hours.

## ManuAI guidance

- **Index build at ingestion** (cloud OK): Unsiloed-parsed PDFs → chunk (~200–500 tokens, 10–20% overlap, per Moss guidance) → tag each chunk `{machine_id, manual_id, section, page, safety_flag}` → `create_index("manuals", docs, "moss-minilm")`. Then package/ship the index to the edge box. **Embed at ingestion with the SAME model you will use at query time.**
- **Query path** (must be offline): `load_index` once at boot, then per utterance `query(text, QueryOptions(top_k, filter={... machine_id $eq ...}))`. Pass the operator's current `machine_id` so an operator at `labeler-line3` never gets `cobot-cellA` content.
- **Latency budget (<50ms incl. embed):** Moss claims single-digit-ms local queries; keep `top_k` small (5–8), prefer `moss-minilm`, and reuse one loaded index. Confirm Apple-Silicon/MLX execution path for the embed step.
- **OFFLINE / local-embedding requirement:** verified that Moss embeds query text **on-device** ("embedding stays off the network path"). The remaining risk is **credential validation** needing a network handshake — pre-validate before going offline and confirm cached-credential behavior (Q1).
- **EMBEDDING-MODEL-PARITY rule (critical):** the index and the queries MUST use the **same embedding model**. If you build the index with `moss-minilm` you must query with `moss-minilm`. Mixing models (or mixing a Moss built-in model at index time with a different Ollama/MLX embed at query time) makes vectors live in different spaces and **retrieval silently degrades** — no error, just bad hits. Since Moss embeds both sides internally when you pass raw text, the safest path is: **let Moss embed both index and query** and never inject an external embedder unless you supply precomputed vectors on BOTH sides with one model.

## Office-hours Q&A (PRD)

**Q1 — Does query-time embedding run LOCALLY, or does Moss expect a cloud embedding API? (kills the offline demo if cloud)**
*Embedding + per-query: CONFIRMED local.* Docs: queries are "embedded and queried locally," "no network round trip per operation," "queries don't leave your process." *But `load_index` and auth are NOT offline:* the canonical index lives in the cloud and `load_index` fetches a snapshot; "credentials are validated when a session is opened" (network), then "tokens are cached and auto-refreshed." Python has **no** disk-cache parameter (JS-only `cachePath`). **Net:** load + authenticate while online, keep the process alive, then query offline. **Ask:** (1) can `load_index`/session run fully offline after a one-time online validation, with credentials + index snapshot cached to disk? (2) is there a Python equivalent of JS `cachePath` / an offline/edge license mode? (3) does an auto-refresh token expire and force a network call mid-session?

**Q2 — Pass RAW TEXT or PRECOMPUTED VECTORS? Which embedding model/dim recommended?**
*CONFIRMED:* pass **raw text** — Moss embeds with a built-in model. Models: `moss-minilm` (fast/lightweight, default) and `moss-mediumlm` (higher accuracy). Precomputed/custom vectors are supported (`custom` model + `embedding=` query vector). **Ask:** exact **dimension** of each model, which to use for technical manuals, and whether built-in inference uses MLX/Apple-Silicon GPU.

**Q3 — Metadata filtering by `machine_id` at query time — Python SDK example?**
*CONFIRMED.* Attach `metadata={"machine_id": "...", ...}` on each `DocumentInfo`; filter is evaluated on the locally-loaded index:
```python
QueryOptions(top_k=5, filter={"$and": [{"field": "machine_id", "condition": {"$eq": "cobot-cellA"}}]})
```
**Ask:** numeric vs string comparison semantics (for `page`/`$gt`), and whether filtering happens pre- or post-ANN (recall impact when a filter is very selective).

**Q4 — How are index updates / instant updates handled?**
*CONFIRMED.* `add_docs(index, docs, MutationOptions(upsert=True))` and `delete_docs(index, ids)` mutate the **in-memory** index immediately (single-digit ms, no network). `push_index()` optionally syncs a session/index to cloud with "no server-side re-embedding." **Ask:** the **Python** way to persist a built index to disk and reload it on a disconnected box (the docs show `cachePath` for JS only) — see Q1/persistence.

## Gotchas

- **Embedding parity** (top cause of bad results): same model at index-time and query-time, every time. No silent error if violated.
- **Offline embedding is fine; offline LOAD + AUTH are the open questions.** The index's canonical copy is in the cloud and `load_index` fetches it (Python has no documented disk-cache like JS `cachePath`); auth validates over the network on first session. So: authenticate + `load_index` while ONLINE, keep the process alive, THEN go offline — queries then run locally. Don't restart the process or let the token expire while offline. Rehearse this exact sequence BEFORE the demo.
- **Latency budget:** keep `top_k` low, reuse one `load_index`, prefer `moss-minilm`; don't re-load the index per utterance.
- **Filter semantics:** filter is `{"field","condition":{op:value}}` (NOT `{field: {op: value}}`); evaluated on the **locally loaded** index, so you must `load_index` (or open a session) before filtering. Metadata example values are strings — keep types consistent.
- **`alpha`** blends semantic/keyword (default 0.8). For exact part numbers / fault codes, lower alpha (more keyword) may help; tune per query class.

## Related skills

- **ollama**, **mlx** — local embedding and the same-model-parity concern (only relevant if you bypass Moss's built-in embedder with precomputed vectors).
- **unsiloed** — parses the PDFs that feed the chunks loaded into the Moss index.
- **qwen** — consumes the retrieved chunks (cite-or-refuse) using `.text` + `.metadata`.
- **livekit-agents** — orchestrates the voice loop; Moss exposes retrieval as a `@function_tool` (see reference.md).
- **whisper-stt** — produces the query text that gets embedded.
- **local-tts** — speaks the grounded answer.

## Footer

Docs: https://docs.moss.dev/docs · Python API: https://docs.moss.dev/docs/reference/python/api · GitHub: https://github.com/usemoss/moss · PyPI: https://pypi.org/project/inferedge-moss/ · Site: https://www.moss.dev/

See `reference.md` (same folder) for the full API surface, sessions, hybrid search, LiveKit tool pattern, and source list.

**Verified on 2026-06-06.**

**CONFIRMED 2026-06-06:** per-query runs with no network; the canonical index lives in the cloud and `load_index` fetches a snapshot; Python `load_index` has no disk-cache param (JS-only `cachePath`); auth validates over the network when a session opens, then caches tokens; `model_id="custom"` lets you supply your own vectors at index + query time.
**Explicitly UNVERIFIED:** (a) whether `load_index` + auth can run **fully offline** (cold start) after a one-time online validation, and whether the index snapshot + credentials can be cached to disk in Python; (b) embedding dimensions of `moss-minilm`/`moss-mediumlm`; (c) MLX/Apple-Silicon (GPU/Metal) execution path for built-in embedding; (d) numeric vs string filter comparison semantics; (e) whether a cached token expires and forces a network call mid-demo. Confirm all with the Moss team at office hours — (a) and (e) protect the wifi-off demo.
