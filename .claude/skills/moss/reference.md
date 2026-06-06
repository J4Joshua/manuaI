# Moss — reference (deep detail)

Companion to `SKILL.md`. Read from primary sources on **2026-06-06**; items marked *(unverified)* were not confirmable from the docs.

> **⚠ Installed-SDK reality check (`inferedge-moss==1.0.0b19`, introspected + smoke-tested 2026-06-06).**
> The shipped wheel is **behind the docs**. Confirmed present on `MossClient` (all `async`):
> `create_index, load_index, query, add_docs, delete_docs, get_docs, get_index, list_indexes, delete_index, unload_index, get_job_status`.
> **NOT present in this build (docs list them, wheel does not):** `session()`, `push_index()`, `load_indexes()`, `query_multi_index()`, `unload_indexes()`, and any `SessionIndex` class. Treat the Sessions and Multi-index sections below as **docs-only / future**, not usable in b19.
> **Import is `inferedge_moss`** (not `moss`). Smoke-tested latencies on a live project: `create_index` ~5.5s, `load_index` ~10s (cloud fetch), `query` ~7ms (local). Metadata `$eq` filter verified to not leak. See `scripts/moss_smoke_test.py`.

## Install

```bash
pip install inferedge-moss   # PyPI name. The docs' `pip install moss` is WRONG.
```
Import name is **`inferedge_moss`** (the docs' `from moss import …` does not work). All client methods are **async**.
Built on Rust bindings; runtime described by YC launch as Rust + WASM, embeddable, "<20kB" runtime, official JS + Python SDKs (also Swift, Elixir, C/libmoss, Browser/WASM).

## Core objects

- `MossClient(MOSS_PROJECT_ID, MOSS_PROJECT_KEY)` — cloud index management + local query. Credentials from the Moss portal. **Verified: `create_index`/`load_index` hit the network (load ~10s); `query` is local (~7ms).**
- ~~`SessionIndex` / `client.session(...)`~~ — **not in `inferedge-moss==1.0.0b19`** (docs-only; see reality-check above).
- `DocumentInfo(id, text, metadata=None, embedding=None)` — `metadata` is a flat dict used for filtering; `embedding` only for `model_id="custom"`.
- `QueryOptions(top_k, alpha=0.8, filter=None, embedding=None)`.
- `MutationOptions(upsert=True)`, `GetDocumentsOptions(doc_ids=[...])`.

## Index lifecycle

```python
await client.create_index("manuals", documents, "moss-minilm")  # raw text -> embedded locally
info     = await client.get_index("manuals")
indexes  = await client.list_indexes()
await client.delete_index("manuals")
```

## Documents (updates)

```python
await client.add_docs("manuals", new_docs, MutationOptions(upsert=True))
all_docs = await client.get_docs("manuals")
some     = await client.get_docs("manuals", GetDocumentsOptions(doc_ids=["doc1"]))
await client.delete_docs("manuals", ["doc6", "doc7"])
```
Local mutations are immediate (single-digit ms, no network). `push_index()` syncs to cloud, "no server-side re-embedding."

## Query / retrieval

```python
await client.load_index("manuals")
res = await client.query("manuals", "clear joint overtravel fault",
                         QueryOptions(top_k=5, alpha=0.8))
await client.unload_index("manuals")
# res.docs -> hits, each QueryResultDocumentInfo(.id .text .metadata .score)
# res also has .time_taken_ms / .index_name / .query
```
- `alpha`: 1.0 = pure semantic, 0.0 = pure keyword, default 0.8 (hybrid search is built in).
- `embedding=`: supply a precomputed query vector (when using the `custom` model).
- Queries embed the query text with a **local** model and run against the in-memory index — no per-query network round trip.

## Metadata filtering (confirmed syntax)

Operators: `$eq $ne $gt $gte $lt $lte $in $nin $near` (`$near` uses `"lat,lng,radiusMeters"`). Compose with `$and` / `$or` (nestable).

```python
QueryOptions(
    top_k=5,
    filter={
        "$and": [
            {"field": "machine_id", "condition": {"$eq": "cobot-cellA"}},
            {"field": "safety_flag", "condition": {"$eq": "true"}},
        ]
    },
)
```
A single condition can appear without an `$and`/`$or` wrapper. Filtering is evaluated on the **locally loaded** index, so `load_index` (or a session) must precede a filtered query.

## Embedding models

| model | notes | dimension |
|---|---|---|
| `moss-minilm` | default, fast/lightweight, on-device | *(unverified)* |
| `moss-mediumlm` | higher accuracy, on-device | *(unverified)* |
| `custom` | bring your own precomputed vectors (`embedding=`) | your choice |

Built-in models embed both index docs and query text on-device; "text and resulting vectors stay on the machine." Apple-Silicon/MLX vs CPU execution path is *(unverified)*.

## Sessions (per-call / real-time) — ⚠ docs-only, NOT in v1.0.0b19

> `client.session(...)` / `SessionIndex` / `push_index()` are documented but **absent from the installed wheel** (b19). The snippet below is from the docs; it will `AttributeError` today. Use a second regular index (`add_docs`/`delete_docs`) for short-term recall until sessions ship.

```python
session = await client.session(index_name="call-123")
await session.add_docs([DocumentInfo(id="turn-1", text="...")])
hits = await session.query("query text", QueryOptions(top_k=3))
await session.push_index()   # optional sync to cloud (handoff)
```
Useful in ManuAI if you want short-term recall of the current conversation alongside the long-term `manuals` index.

## Multi-index search — ⚠ docs-only, NOT in v1.0.0b19

> `load_indexes` / `query_multi_index` / `unload_indexes` are documented but **absent from b19**. For ManuAI one `manuals` index filtered by `machine_id` is the right design anyway.

```python
await client.load_indexes(["manuals", "sops"])
results = await client.query_multi_index(["manuals", "sops"], "query", QueryOptions(top_k=6))
```

## Persistence

- JS SDK: `client.loadIndex('idx', { cachePath: '/var/cache/moss' })` caches the downloaded index to disk; subsequent loads skip the network fetch when cloud data is unchanged; auto-refresh writes through so restarts stay warm.
- **Python: no disk-cache parameter in b19.** `load_index(name, auto_refresh=False, polling_interval_in_seconds=600)` has no `cachePath` equivalent, and it was measured doing a **~10s cloud fetch** — i.e. there is currently **no documented way to cold-load a prebuilt index from disk offline in Python**. The cloud index is the "canonical copy." ManuAI demo path: `load_index` ONLINE, keep the process alive, then go wifi-off (query is local ~7ms). Confirm a true offline cold-load with the Moss team — see SKILL Q1/Q4 and `scripts/moss_offline_test.py`.

## LiveKit voice integration

Moss is exposed to the agent LLM as a function tool:

```python
@function_tool
async def search_knowledge_base(self, context: RunContext, query: str) -> str:
    """Search the equipment manuals."""
    results = await self.moss.query(
        KNOWLEDGE_INDEX, query, QueryOptions(top_k=5, alpha=0.8)
    )
    return "\n".join(f"- {d.text}" for d in results.docs)
```
A per-call session can index conversation turns locally (`search_conversation`) and push to cloud at call end for handoff. For ManuAI, add the `machine_id` filter inside the tool and surface `.metadata` (section/page) so Qwen can cite or refuse.

## Source list (read 2026-06-06)

- Site: https://www.moss.dev/
- Docs home: https://docs.moss.dev/docs
- Doc index: https://docs.moss.dev/llms.txt
- Python API reference: https://docs.moss.dev/docs/reference/python/api (`.md`)
- Quickstart: https://docs.moss.dev/docs/start/quickstart (`.md`)
- Indexing data: https://docs.moss.dev/docs/integrate/indexing-data.md
- Metadata filtering: https://docs.moss.dev/docs/integrate/metadata-filtering.md
- Local embeddings: https://docs.moss.dev/docs/build/local-embeddings.md
- Offline-first / sub-10ms: https://docs.moss.dev/docs/build/offline-first-search.md
- Real-time local indexing: https://docs.moss.dev/docs/build/real-time-local-indexing.md
- Storage & persistence: https://docs.moss.dev/docs/integrate/storage-persistence.md
- Authentication: https://docs.moss.dev/docs/integrate/authentication.md
- Deployment/production: https://docs.moss.dev/docs/integrate/deployment-production.md
- LiveKit integration: https://docs.moss.dev/docs/integrations/livekit.md
- GitHub: https://github.com/usemoss/moss
- PyPI: https://pypi.org/project/inferedge-moss/
- YC company: https://www.ycombinator.com/companies/moss
- YC launch: https://www.ycombinator.com/launches/Oiq-moss-real-time-semantic-search-for-conversational-ai
