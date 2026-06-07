# Unsiloed Parse API — full reference

Deep detail backing `SKILL.md`. Verified 2026-06-06 from the official docs unless marked "to confirm".

Base URL: `https://prod.visionapi.unsiloed.ai`
Auth header (all endpoints): `api-key: <YOUR_KEY>`
Docs index: https://docs.unsiloed.ai/llms.txt

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /parse` | Submit a document for parsing (multipart/form-data). Returns a `job_id`. |
| `GET /parse/{job_id}` | Poll a parse job; returns status and (when done) chunks. |
| `GET /org/get_usage` | Org usage/quota in credits. |
| `POST /extract` (Extract Data) | Schema-based field extraction (not used by ManuAI ingest). |
| `POST` Split / Classify | Multi-doc splitting, classification. Out of scope for ManuAI. |

There are also `parse-document-v2` (presigned upload) and `parse-document-v3` variants in the API reference — useful if multipart upload of large PDFs is unreliable. Prefer the `url` input (presigned/public URL) for big files on the base endpoint.

## POST /parse — request

Content-Type: `multipart/form-data`. Provide exactly one of `file` / `url`.

### Input (one required)
| Param | Type | Notes |
|---|---|---|
| `file` | binary | PDF, PNG, JPEG, TIFF, PPT, PPTX, DOC, DOCX, XLS, XLSX |
| `url` | string | Presigned or public URL of the document |

### Processing config
| Param | Type | Default | Options / notes |
|---|---|---|---|
| `layout_analysis` | string | `smart_layout_detection` | `smart_layout_detection`, `page_by_page`, `advanced_layout_detection` |
| `ocr_strategy` | string | `auto_detection` | `auto_detection`, `force_ocr` |
| `ocr_engine` | string | `UnsiloedBeta` | `UnsiloedBeta`, `UnsiloedHawk`, `UnsiloedStorm` |
| `agentic_ocr` | string | (off) | `standard`, `advanced` |
| `use_high_resolution` | boolean | `true` | Improves OCR on low-quality scans |
| `merge_tables` | boolean | `false` | Reconstruct tables spanning pages |
| `validate_segments` | string(JSON array) | `[]` | e.g. `["Table","Picture","Formula"]` |
| `segment_filter` | string | `all` | Comma-separated types or `all` |
| `extract_strikethrough` | boolean | `false` | |
| `extract_colors` | boolean | `false` | Transfer text color from PDF |
| `extract_links` | boolean | `false` | Attach hyperlink URLs |
| `xml_citation` | boolean | `false` | Bibliography/in-text citations (PDF only) |
| `page_range` | string | (all) | `"1-5"`, `"2,4,6"`, `"[1,3,5]"` |
| `segment_type_naming` | string | `Unsiloed` | `Unsiloed`, `Other` |
| `error_handling` | string | `Continue` | `Continue`, `Fail` |
| `expires_in` | integer | (plan default) | Seconds until result deletion |

### Output config
| Param | Type | Notes |
|---|---|---|
| `output_fields` | string(JSON object) | Toggle: `html`, `markdown`, `ocr`, `image`, `content`, `bbox`, `confidence`, `embed` |
| `export_format` | string(JSON array) | `["docx"]`, `["markdown"]`, `["json"]` |
| `segment_analysis` | string(JSON object) | Per segment-type `html`/`markdown` generation strategy + `model_id` |

## Responses

### Initial (POST /parse)
```json
{
  "job_id": "string",
  "status": "Starting",
  "file_name": "string",
  "created_at": "ISO 8601",
  "message": "string",
  "credit_used": 0,
  "quota_remaining": 0,
  "merge_tables": false
}
```

### Completed (GET /parse/{job_id})
```json
{
  "job_id": "string",
  "status": "Succeeded",
  "created_at": "ISO 8601",
  "started_at": "ISO 8601",
  "finished_at": "ISO 8601",
  "total_chunks": 0,
  "chunks": [
    {
      "embed": "chunk markdown rolled into one string",
      "segments": [
        {
          "segment_type": "SectionHeader",
          "content": "string",
          "markdown": "string",
          "html": "string",
          "image": "base64 or null",
          "page_number": 1,
          "segment_id": "UUID",
          "confidence": 0.0,
          "page_width": 0.0,
          "page_height": 0.0,
          "bbox": { "left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0 },
          "ocr": [
            { "bbox": { "left": 0.0, "top": 0.0, "width": 0.0, "height": 0.0 },
              "text": "string", "confidence": 0.0 }
          ]
        }
      ]
    }
  ]
}
```
Note: `embed` is documented at the chunk level ("the chunk's Markdown rolled into one string"). Field presence depends on `output_fields`. Status values: `Starting`, `Succeeded`, `Failed`.

## Structure preserved
- Two-level hierarchy: **chunks** group one or more adjacent **segments** (atomic labeled regions).
- Reading order preserved across multi-column layouts.
- Sections/headings → `Title` / `SectionHeader` segments and Markdown headers.
- Tables → Markdown tables, kept whole (`merge_tables` for cross-page).
- Page numbers + page dimensions, bounding boxes, per-segment confidence.
- Parent-child is implied via chunk grouping + `segment_id` (no explicit parent pointer field documented — "to confirm").

## Segment types
`Title`, `SectionHeader`, `Text`, `Table`, `Picture`, `Caption`, `Formula`, `Footnote`, `ListItem`, `PageHeader`, `PageFooter`, `KeyValuePair`, `Signature`, `Seal`.

## Processing modes
Docs mention preset bundles **Fast / Accurate / Agentic** (`/document-processing/parsing/processing-modes`) layering over the params above. Exact param mappings: to confirm.

## Usage / quota — GET /org/get_usage
Returns `current_usage`, `usage_limit`, `remaining_quota` (credits), `last_billed_date`. Cycle resets ~every 30 days. Whether a credit == a page is not stated — to confirm.

## Anthropic / Claude integration (optional)
Docs ship tool-use JSON schemas (NOT an MCP server) for an agentic loop: `unsiloed_parse_document`, `unsiloed_extract_data`, `unsiloed_classify_document`, `unsiloed_split_document`, `unsiloed_get_job_result`. Because Claude can't upload binaries, those tools require a **public/presigned URL** input. `pip install anthropic requests`. Not needed for ManuAI's batch ingest — plain `requests` is simpler.

## Deployment
Self-hosted within your own AWS/Azure/GCP, including air-gapped/isolated networks, is advertised. Useful if a customer forbids cloud ingestion. Availability/terms for a hackathon: to confirm with Unsiloed.

## Not publicly documented (to confirm with Unsiloed)
- Rate limits (req/min), concurrent-job ceiling, HTTP 429 behavior.
- Pages-per-credit, pricing tiers.
- Hard file-size / max-page limits.
- `expires_in` default retention.
- Explicit parent-child relationship field.

## Source pages (verified 2026-06-06)
- https://docs.unsiloed.ai/api-reference/parser/parse-document
- https://docs.unsiloed.ai/quickstart
- https://docs.unsiloed.ai/document-processing/parsing/parsing
- https://docs.unsiloed.ai/api-reference/organization/usage
- https://docs.unsiloed.ai/faq/general
- https://docs.unsiloed.ai/integrations/claude-integration
- https://docs.unsiloed.ai/llms.txt
