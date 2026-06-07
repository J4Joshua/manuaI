---
name: unsiloed
description: Parse PDF manuals/SOPs into structured Markdown+JSON with the Unsiloed Parse API, preserving tables, section headers, page numbers, and bounding boxes for citations. Use when building or debugging ManuAI's cloud document-ingestion pipeline (the one-time, wifi-on step that feeds the offline edge index).
---

# Unsiloed Parse API (ManuAI ingestion)

Unsiloed AI is a vision-first document parser. In ManuAI it is the **ingestion-time** parser only: it runs once, with wifi ON, OFF the offline query critical path. It turns factory manuals/SOPs (PDF) into structured Markdown + JSON segments that we then chunk by procedure, tag, embed locally, and ship to the edge box. It never runs at query time and is never on the ≤1.5s latency path.

Pipeline position: `Manuals/SOPs (PDF) → Unsiloed Parse → Markdown+JSON segments → chunk by procedure/section → tag {machine_id, manual_id, section, page, safety_flag} → local embed → Moss index → edge box`.

## When to use this skill

- Setting up or debugging the one-time cloud ingestion of SOP/manual PDFs.
- Choosing/using Unsiloed request parameters (OCR, table merge, page range, output fields).
- Turning Unsiloed output into procedure-level chunks that keep tables intact and carry section numbers + page numbers for grounded citations (e.g. "SOP-1187 §4.2, p.12").
- Answering "Markdown vs JSON?" and "will I hit rate limits ingesting ~10 docs?" (see Office-hours answers).
- NOT for anything at query time — Unsiloed is cloud-only and runs before going offline. For runtime, see `moss`, `mlx`, `qwen`.

## Quickstart (verified from docs 2026-06-06)

### 1. Auth / API key
- Get a key by signing up via Unsiloed (docs link a Cal.com booking + `support@unsiloed.ai`). Set it as an env var.
- Auth is a header: `api-key: <YOUR_KEY>` (NOT a `Bearer` token — confirmed in the API reference).
- Base URL: `https://prod.visionapi.unsiloed.ai`
- No official Python SDK exists; the docs use plain `requests`. `pip install requests` (Python 3.8+).

```bash
export UNSILOED_API_KEY="sk_..."   # your key
```

### 2. Submit one PDF (async — returns a job_id)

curl:
```bash
curl -sX POST "https://prod.visionapi.unsiloed.ai/parse" \
  -H "api-key: $UNSILOED_API_KEY" \
  -F "file=@SOP-1187.pdf"
# -> {"job_id":"...","status":"Starting", ...}
```

Python (submit + poll + save), straight from the official quickstart:
```python
import json, os, time, requests

API_KEY  = os.environ["UNSILOED_API_KEY"]
BASE_URL = "https://prod.visionapi.unsiloed.ai"

with open("SOP-1187.pdf", "rb") as f:
    r = requests.post(
        f"{BASE_URL}/parse",
        headers={"api-key": API_KEY},
        files={"file": ("SOP-1187.pdf", f, "application/pdf")},
    )
r.raise_for_status()
job_id = r.json()["job_id"]

# 3. Poll GET /parse/{job_id} until Succeeded
while True:
    result = requests.get(f"{BASE_URL}/parse/{job_id}",
                          headers={"api-key": API_KEY}).json()
    if result["status"] == "Succeeded":
        break
    if result["status"] == "Failed":
        raise RuntimeError(result.get("message", "parse failed"))
    time.sleep(5)   # poll interval; default timeout ~5 min

with open("SOP-1187.json", "w") as fp:
    json.dump(result, fp, indent=2)
```

The result has `total_chunks` and `chunks[]`. Each chunk groups adjacent `segments[]`; each segment has `segment_type`, `content`, `markdown`, `html`, `page_number`, `bbox`, `confidence`, and (per chunk) an `embed` string = the chunk's Markdown rolled into one. See `reference.md` for the full schema.

## ManuAI ingestion guidance (the important part)

**Use BOTH outputs, but drive chunking from JSON and store Markdown as the chunk body.** Request `markdown` + `json` and keep the structured segment metadata — do not flatten to a single Markdown blob, or you lose the page/section/bbox fields you need for citations.

Recommended request for SOPs (set these explicitly):
```python
data = {
    "ocr_strategy": "auto_detection",   # force_ocr for scanned/photographed SOPs
    "use_high_resolution": "true",      # better OCR on low-quality factory scans
    "merge_tables": "true",             # reconnect tables that span pages (torque specs etc.)
    "export_format": '["markdown","json"]',
    "output_fields": '{"markdown": true, "content": true, "bbox": true, "confidence": true}',
}
r = requests.post(f"{BASE_URL}/parse", headers={"api-key": API_KEY},
                  files={"file": ("SOP-1187.pdf", f, "application/pdf")}, data=data)
```

Building procedure-level chunks from the result:
1. **Walk segments in reading order** (already preserved across multi-column layouts).
2. **Split on `segment_type` in {`Title`, `SectionHeader`}** to start a new procedure/section chunk. Capture the header text — that is your citable section ("§4.2").
3. **Keep `Table` segments whole** inside their chunk. Store `segment.markdown` (a real Markdown table) so the table stays intact for both display and the LLM. With `merge_tables: true` a multi-page spec table arrives as one segment.
4. **Carry metadata onto every chunk** so the answer can be grounded-or-silent:
   ```python
   chunk_meta = {
       "machine_id":  machine_id,                 # you supply (from filename/folder)
       "manual_id":   "SOP-1187",                 # you supply
       "section":     header_text,                # from Title/SectionHeader segment
       "page":        segment["page_number"],     # for "p.12" citation
       "safety_flag": detect_safety(text),        # you derive (warning/danger/LOTO keywords)
   }
   ```
5. **Embed the chunk's Markdown body locally** and load into the `moss` index, then ship to edge.

**Markdown vs JSON — recommendation:** the LLM-facing chunk *body* should be **Markdown** (tables and headers render cleanly, vector-store friendly — Unsiloed's docs position Markdown as the "ready to drop into a vector store" format). But you must **keep the JSON segment wrapper** because section numbers (`SectionHeader` text), `page_number`, and `bbox` live there — those are what make a citation possible. So: chunk *content* = Markdown, chunk *metadata* = from JSON. (Full reasoning in Office-hours answer (a).)

## Key API reference (the 20% you use 80% of the time)

| Item | Value |
|---|---|
| Submit | `POST /parse` (multipart/form-data), header `api-key` |
| Poll | `GET /parse/{job_id}`, header `api-key` |
| Status values | `Starting` → `Succeeded` / `Failed` |
| Input | `file` (binary) **or** `url` (presigned/public) — one, not both |
| Formats in | PDF, PNG, JPEG, TIFF, PPT(X), DOC(X), XLS(X) |
| `ocr_strategy` | `auto_detection` (default) / `force_ocr` |
| `merge_tables` | reconnect tables across pages (use `true` for SOPs) |
| `use_high_resolution` | better OCR on poor scans (default true) |
| `page_range` | `"1-5"`, `"2,4,6"`, `"[1,3,5]"` |
| `output_fields` | toggle `html`/`markdown`/`content`/`bbox`/`confidence`/`embed`/`ocr`/`image` |
| `export_format` | `["markdown"]`, `["json"]`, `["docx"]` |
| Usage check | `GET /org/get_usage` → `current_usage`, `usage_limit`, `remaining_quota` (credits, ~30-day cycle) |

Segment types you'll branch on: `Title`, `SectionHeader`, `Text`, `Table`, `Picture`, `Caption`, `Formula`, `Footnote`, `ListItem`, `PageHeader`, `PageFooter`, `KeyValuePair`, `Signature`, `Seal`.

Deeper detail (full response schema, all params, processing modes, deployment) → `reference.md`.

## Gotchas / pitfalls

- **It's async — you MUST poll.** The POST returns immediately with `status: "Starting"`. Don't expect content in the first response. Default polling window in the docs is ~5 min; for big manuals raise your `max_attempts`.
- **Header is `api-key`, not `Authorization: Bearer`.** Easy to get wrong from muscle memory.
- **Scanned/photographed SOPs:** set `ocr_strategy: "force_ocr"` and keep `use_high_resolution: true`. Check `confidence` per segment and flag low-confidence chunks for human review before they reach the edge — a wrong torque value is a safety issue. Grounded-or-silent depends on clean OCR.
- **Tables across page breaks** fragment unless `merge_tables: true`.
- **Don't flatten to one Markdown string at ingest.** You'll lose `page_number`/`SectionHeader`/`bbox` and won't be able to cite. Keep the JSON.
- **Large files:** prefer the `url` input (presigned S3/GCS) for big PDFs instead of multipart upload; there are also v2 (presigned-upload) and v3 parse endpoints — see `reference.md` if multipart is flaky.
- **Cloud-only by default / data residency:** the standard API sends the doc to Unsiloed's cloud. That's fine for ManuAI ingestion (one-time, wifi-on, off the floor). If on-prem ingestion is required, Unsiloed's FAQ **confirms** self-hosted, air-gapped, and hybrid deployment options (run in your own AWS/Azure/GCP) — so "data never leaves the building" can extend to ingestion too (roadmap). Only the turnaround/availability for a hackathon timeframe needs a direct ask (`support@unsiloed.ai`).
- **Rate limits are not published** (see (b)). Build a small backoff/retry around the poll loop and serialize submissions to be safe.

## Office-hours answers

**(a) Best parse output for procedure-level chunking that preserves tables + section numbers for citations?**
Request **Markdown AND JSON together**, and chunk off the JSON. Concretely:
- Use the **JSON segment array** as the source of truth — it gives you `segment_type` (`Title`/`SectionHeader` = your citable "§4.2"), `page_number` (your "p.12"), `bbox`, and `confidence`. These are the fields that make grounded-or-silent citation possible; raw Markdown alone throws section/page structure away.
- Store each chunk's **body as Markdown** (`segment.markdown` / chunk `embed`) — tables come back as real Markdown tables that stay intact, render on screen, and embed cleanly into the Moss vector store.
- So: **JSON drives the chunk boundaries + citation metadata; Markdown is the chunk text.** Don't pick one — Unsiloed gives both in the same response.

**(b) Rate limits for ingesting ~10 docs in one day?**
**No public rate-limit numbers** are documented (verified: none on the API reference, FAQ, quickstart, or org/usage pages). What IS documented: billing is **credit/page-based** with a monthly `usage_limit` and `remaining_quota` you can check via `GET /org/get_usage` — and every `POST /parse` response returns `credit_used` + `quota_remaining` inline, so you can watch the balance per submission. Practical guidance: **~10 docs/day is trivially low-volume** — this is a YC-stage parser API, the concern is your monthly *credit/page quota*, not a per-second rate cap. Before the hackathon: (1) call `GET /org/get_usage` to confirm `remaining_quota` covers your total page count; (2) submit jobs **serially** with the poll loop (async means one in flight at a time is simplest); (3) if you must parallelize, add exponential backoff and treat HTTP 429 as the rate-limit signal. **To confirm with Unsiloed directly:** exact requests/min, concurrent-job ceiling, and pages-per-credit.

## Related skills

- **`moss`** — the local index Unsiloed output feeds into; Unsiloed's chunks+metadata are embedded and loaded here for offline retrieval.
- **`mlx`** — Apple-Silicon inference runtime for the offline query path.
- **`qwen`** — local LLM that reads the retrieved chunks and produces the grounded answer.
- **`ollama`** — local model serving (alt runtime).
- **`whisper-stt`** — offline speech-to-text for the operator's push-to-talk.
- **`local-tts`** — offline text-to-speech for spoken procedures.
- **`livekit-agents`** — voice agent orchestration on the edge box.

Flow: `whisper-stt → (retrieve from moss, built from Unsiloed output) → qwen on mlx → local-tts`, orchestrated by `livekit-agents`. Unsiloed sits one step upstream of `moss`, at ingestion only.

---
Docs: https://docs.unsiloed.ai · API ref: https://docs.unsiloed.ai/api-reference/parser/parse-document · Quickstart: https://docs.unsiloed.ai/quickstart · Index: https://docs.unsiloed.ai/llms.txt

**Verified on 2026-06-06** against the official Unsiloed docs (parse-document API reference, quickstart, parsing overview, org/usage, FAQ).

**Confirmed 2026-06-06:** self-hosted / on-prem / **air-gapped** / hybrid deployment IS offered (FAQ); inputs include PDF, PPT(X), DOC(X), and images (PNG/JPEG/TIFF); `/parse` (v1) is "stable indefinitely" with `/v2/parse/upload` (presigned) for larger files / higher throughput; the submit response returns `credit_used` + `quota_remaining`.

**Could NOT verify (treat as "to confirm"):**
- Exact rate limits / requests-per-minute / concurrent-job ceiling — not published anywhere public (email `support@unsiloed.ai`).
- Pages-per-credit and pricing tier numbers — usage-based (pay-as-you-go credits + enterprise) per docs, but no figures published.
- Hard file-size and max-page limits — not stated in docs.
- Turnaround/availability of self-hosted/air-gapped deployment within a hackathon timeframe (offered, but engagement details not public).
- Data-retention default (`expires_in` param exists; default value not documented).
