#!/usr/bin/env python3
"""Phase 4 — Unsiloed ingestion (ARCHITECTURE.md §4b).

Pipeline (Unsiloed cloud + one-time, wifi ON for PDF parse only):
    real manual PDFs  →  Unsiloed Parse (HTTP, async-poll)
                      →  Unsiloed Extract (custom JSON schema, per-chunk)
                      →  normalize → corpus.py chunk schema
                      →  moss_ingest.embed_and_write (SOP + PDF union, offline Moss embed)

Usage:
    .venv/bin/python src/unsiloed_ingest.py [--dry-run] [--pdf path/to/manual.pdf]

    --dry-run   Normalise chunks and print stats; skip local Moss index build.
    --pdf       Process a single PDF instead of all manuals.
    (no args)   Process every data/machines/*/manuals/*.pdf in manifest.

Env vars (from .env / .env.example):
    UNSILOED_API_KEY       required (except --dry-run)
    UNSILOED_BASE_URL      default https://prod.visionapi.unsiloed.ai
    MOSS_MODEL_ID          default moss-minilm (local embedder, offline)

Imports: stdlib only for HTTP (urllib) + project-local corpus / moss_ingest.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Field-mapping table (Phase 4 / ARCHITECTURE §3a / G12)
# ─────────────────────────────────────────────────────────────────────────────
#
# corpus.py schema field  | Unsiloed source                            | Default / derivation
# ──────────────────────────────────────────────────────────────────────────────────────────
# id                      | f"{doc_id}--{i:03d}--{slug(section_heading)}" | ordinal prefix guarantees
#                         |                                            | uniqueness across headerless
#                         |                                            | chunks; parse-order stable
# sop_id                  | doc_id from manifest (str)                 | str(doc_id)
# section                 | SectionHeader / Title segment text         | "Overview" fallback
# machine_id              | manifest lookup by PDF path                | never auto-detected (G12)
# doc_type                | manifest "type" field                      | "manual"
# procedure_title         | Extract field "procedure_title"            | sop_id if Extract absent
# safety_flag (bool)      | Extract "safety_flag" (conf≥0.7)          | True if:
#                         |                                            |   • Extract conf < 0.7, OR
#                         |                                            |   • text ∋ WARNING/CAUTION/
#                         |                                            |     DANGER/LOTO, OR
#                         |                                            |   • manifest doc safety_flag
# fault_codes (str)       | Extract "error_codes[]" → ",".join()      | "" (empty string)
# page (int)              | segment["page_number"] (first segment)     | None
# text                    | f"{procedure_title}\n\n{chunk_markdown}"   | same pattern as corpus.py
#
# Steps (from Extract) are FOLDED INTO text via the parsed markdown — they are NOT
# stored as a separate key (corpus.py has no `steps` key). The schema has exactly
# 10 keys: id, sop_id, section, machine_id, doc_type, procedure_title, safety_flag,
# fault_codes, page, text.
# ─────────────────────────────────────────────────────────────────────────────

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Project-local imports (see requirements.txt).
import corpus
import paths
from moss_ingest import embed_and_write
from retriever import load_env

# ── Constants ────────────────────────────────────────────────────────────────

# Safety keywords that force safety_flag=True regardless of Extract confidence.
_SAFETY_RE = re.compile(
    r"\b(WARNING|CAUTION|DANGER|LOTO|LOCKOUT|TAGOUT|HAZARD|DO NOT)\b",
    re.IGNORECASE,
)

# Extract job poll interval (seconds) and timeout.
_POLL_INTERVAL = 5      # seconds between GET /parse/{job_id} calls
_POLL_TIMEOUT  = 360    # 6 minutes max per document

# Intentional-gap query topics from manifest.json.  Their ABSENCE in the corpus
# is what triggers the escalation beat — do NOT ingest documents that would
# answer them.  The PDFs listed in the manifest are real OEM manuals that
# discuss safety guards and soft-limits, but none of the 3 PDFs is itself an
# "intentional gap document" — the gaps are specific *query topics*, not doc
# paths.  This constant is a forward-looking guard: if future PDFs are added
# whose sole purpose is to cover these topics, skip them here.
_INTENTIONAL_GAP_KEYWORDS = frozenset({
    "bypass",
    "interlock",
    "disable",
    "widen",
    "safeguard stop",
})

# ── Env / configuration helpers ───────────────────────────────────────────────

def _require_api_key():
    """Exit with a helpful message if UNSILOED_API_KEY is not set."""
    key = os.environ.get("UNSILOED_API_KEY", "").strip()
    if not key:
        print(
            "\n[unsiloed_ingest] UNSILOED_API_KEY is not set.\n"
            "  1. Get a key at https://unsiloed.ai (or support@unsiloed.ai).\n"
            "  2. Add it to your .env:  UNSILOED_API_KEY=<your-key>\n"
            "  3. Re-run this script with wifi ON.\n\n"
            "Tip: pass --dry-run to normalise without calling Unsiloed.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _base_url():
    return os.environ.get("UNSILOED_BASE_URL", "https://prod.visionapi.unsiloed.ai").rstrip("/")


# ── Manifest helpers ──────────────────────────────────────────────────────────

def _load_manifest():
    """Return the parsed manifest.json dict."""
    p = paths.DATA / "manifest.json"
    with open(p) as f:
        return json.load(f)


def _build_path_index(manifest):
    """Return {relative_path_str: doc_dict} keyed by manifest doc["path"]."""
    return {d["path"]: d for d in manifest["documents"]}


def _find_pdfs():
    """Glob all OEM manuals under data/machines/*/manuals/*.pdf."""
    return sorted(paths.DATA.glob("machines/*/manuals/*.pdf"))


def _manifest_doc_for_pdf(pdf_path, path_index):
    """
    Look up a PDF in the manifest path index.
    pdf_path is an absolute Path; we match against the manifest's relative paths
    (which are relative to data/).  Returns the manifest document dict, or None.
    Handles --pdf paths outside data/ gracefully (ValueError → None → skip).
    """
    try:
        rel = str(pdf_path.relative_to(paths.DATA))
    except ValueError:
        # Path is outside data/ — cannot be in the manifest.
        return None
    return path_index.get(rel)


# ── Slug helper (mirrors corpus.py exactly) ───────────────────────────────────

def _slug(text):
    """Slug a section heading the same way corpus.py does."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "sec"


# ── Safety-flag derivation (G12) ─────────────────────────────────────────────

def _derive_safety_flag(text, extract_safety, extract_confidence, manifest_safety):
    """
    Return True (safe-default) when any of:
      • manifest doc has safety_flag: true
      • Extract confidence < 0.7 (uncertain extraction → err on the side of safety)
      • text contains WARNING / CAUTION / DANGER / LOTO keywords
    Otherwise trust Extract's boolean value.

    NOTE: when extract_safety is None (no Extract result), this returns True —
    fail-safe for procedure chunks whose safety status is unknown.
    """
    if manifest_safety:
        return True
    if _SAFETY_RE.search(text or ""):
        return True
    if extract_confidence is None or extract_confidence < 0.7:
        return True
    return bool(extract_safety)


# ── HTTP helpers (stdlib urllib only — no `requests`) ────────────────────────

def _http_get(url, headers):
    """Blocking GET → parsed JSON body."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _http_post_multipart(url, headers, fields, file_field, file_name, file_bytes):
    """
    Blocking multipart/form-data POST for the Unsiloed /parse endpoint.
    `fields` is {str: str} for text params; file_bytes is the raw PDF bytes.
    Returns parsed JSON body.
    """
    boundary = "ManuAIBoundary01"
    body_parts = []

    for name, value in fields.items():
        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    # File field
    body_parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    )
    body = (
        "".join(body_parts).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req_headers = {
        **headers,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    req = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _http_post_json(url, headers, payload):
    """Blocking JSON POST → parsed JSON body."""
    data = json.dumps(payload).encode()
    req_headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


# ── Unsiloed Parse (step 1) ───────────────────────────────────────────────────

# TODO(needs-api-key): _parse_pdf submits the real multipart POST and polls
# until Succeeded.  No API call is made here at import time.

def _submit_parse(pdf_path, api_key, base_url):
    """
    Submit one PDF to POST /parse and return the job_id.

    Parameters chosen per SKILL.md / reference.md:
      • merge_tables=true    — reconnect cross-page spec tables (torque specs etc.)
      • use_high_resolution  — better OCR on factory scans
      • output_fields        — markdown + content + bbox + confidence (enough for §3a)
      • export_format        — markdown + json (JSON drives chunk boundaries; Markdown is text)
      • ocr_strategy         — auto_detection (switch to force_ocr for photographed SOPs)

    # TODO(needs-api-key): This makes a live network call to Unsiloed.
    """
    headers = {"api-key": api_key}
    fields = {
        "ocr_strategy":       "auto_detection",
        "use_high_resolution": "true",
        "merge_tables":        "true",
        "export_format":       '["markdown","json"]',
        "output_fields":       '{"markdown": true, "content": true, "bbox": true, "confidence": true}',
    }
    file_bytes = pdf_path.read_bytes()
    url = f"{base_url}/parse"
    print(f"  → POST {url} ({pdf_path.name}, {len(file_bytes)//1024} KB)")
    result = _http_post_multipart(
        url, headers, fields,
        file_field="file",
        file_name=pdf_path.name,
        file_bytes=file_bytes,
    )
    job_id = result.get("job_id")
    if not job_id:
        raise RuntimeError(f"Unsiloed /parse did not return job_id: {result}")
    print(f"     job_id={job_id!r}  status={result.get('status')!r}")
    return job_id


def _poll_parse(job_id, api_key, base_url):
    """
    Poll GET /parse/{job_id} until Succeeded or Failed (or timeout).
    Returns the completed result dict (which contains `chunks`).

    # TODO(needs-api-key): This makes repeated live network calls to Unsiloed.
    """
    headers = {"api-key": api_key}
    url = f"{base_url}/parse/{job_id}"
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed > _POLL_TIMEOUT:
            raise TimeoutError(
                f"Unsiloed parse job {job_id!r} timed out after {_POLL_TIMEOUT}s"
            )
        result = _http_get(url, headers)
        status = result.get("status", "")
        if status == "Succeeded":
            total = result.get("total_chunks", "?")
            print(f"     Succeeded — {total} chunks")
            return result
        if status == "Failed":
            raise RuntimeError(
                f"Unsiloed parse job {job_id!r} failed: {result.get('message', result)}"
            )
        print(f"     status={status!r}  ({elapsed:.0f}s elapsed) — waiting {_POLL_INTERVAL}s…")
        time.sleep(_POLL_INTERVAL)


def parse_pdf(pdf_path, api_key, base_url):
    """
    Full Parse flow for one PDF: submit → poll → return raw result dict.
    This is the entry point for step 1 of the pipeline.

    # TODO(needs-api-key): Calls Unsiloed API.
    """
    job_id = _submit_parse(pdf_path, api_key, base_url)
    return _poll_parse(job_id, api_key, base_url)


# ── Unsiloed Extract (step 2) ─────────────────────────────────────────────────

# Custom JSON schema telling Unsiloed which fields to extract per chunk.
# Confidence + citations are returned by the Extract endpoint per field.
# Reference: reference.md §POST /extract (schema-based extraction).
#
# NOTE: The Extract endpoint path is NOT definitively confirmed in the skill docs
# (reference.md lists it as "POST /extract — Extract Data … not used by ManuAI
# ingest"). The endpoint, request body shape, and per-field response format
# below are reasoned from the Anthropic/Claude integration tool-use schemas
# described in reference.md.  FLAG FOR BOOTH VERIFICATION:
#   • Confirm /extract endpoint path and base URL (may differ from /parse).
#   • Confirm request body: does it accept {text: "…", schema: {…}} or
#     does it require a previously parsed job_id?
#   • Confirm response shape: {fields: {name: {value, confidence, citations[]}}}
#   • Confirm whether Extract operates per-chunk or per-document.
_EXTRACT_SCHEMA = {
    "procedure_title": {
        "type": "string",
        "description": "The title or name of the procedure or section.",
    },
    "error_codes": {
        "type": "array",
        "items": {"type": "string"},
        "description": "All fault/alarm/error codes mentioned (e.g. E-42, C4, C50).",
    },
    "safety_flag": {
        "type": "boolean",
        "description": (
            "True if this chunk contains a safety warning, caution, danger notice, "
            "lockout/tagout (LOTO) instruction, or any personal-injury risk."
        ),
    },
    "steps": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Ordered procedure steps, each as a plain string. "
            "Empty list if the chunk is reference material, not a procedure."
        ),
    },
}


def extract_chunk(chunk_text, api_key, base_url):
    """
    Call POST /extract with a custom schema for one chunk of parsed Markdown text.
    Returns a dict like:
        {
            "procedure_title":  {"value": "...", "confidence": 0.91, "citations": [...]},
            "error_codes":      {"value": ["E-42"], "confidence": 0.88, "citations": [...]},
            "safety_flag":      {"value": True, "confidence": 0.95, "citations": [...]},
            "steps":            {"value": ["Step 1…", "Step 2…"], "confidence": 0.72, ...},
        }
    Returns {} on any error (caller applies safe defaults).

    # TODO(needs-api-key): Calls Unsiloed API.
    # FLAG FOR BOOTH VERIFICATION: endpoint path, request shape, response shape.
    """
    headers = {"api-key": api_key}
    url = f"{base_url}/extract"
    payload = {"text": chunk_text, "schema": _EXTRACT_SCHEMA}
    try:
        return _http_post_json(url, headers, payload)
    except urllib.error.HTTPError as exc:
        print(f"     [extract] HTTP {exc.code} — skipping Extract for this chunk", file=sys.stderr)
        return {}
    except Exception as exc:  # pylint: disable=broad-except
        print(f"     [extract] error — {exc!r} — skipping Extract for this chunk", file=sys.stderr)
        return {}


# ── Parse result → section chunks ─────────────────────────────────────────────

def _section_heading(segments):
    """
    Find the leading SectionHeader or Title segment text in a chunk's segment list.
    Falls back to "Overview" if none found.
    """
    for seg in segments:
        if seg.get("segment_type") in ("Title", "SectionHeader"):
            text = (seg.get("content") or seg.get("markdown") or "").strip()
            if text:
                return text
    return "Overview"


def _first_page(segments):
    """Return the page_number of the first segment (int), or None."""
    for seg in segments:
        pn = seg.get("page_number")
        if pn is not None:
            try:
                return int(pn)
            except (TypeError, ValueError):
                pass
    return None


def _chunk_markdown(raw_chunk):
    """
    Best-effort Markdown text for a raw Unsiloed chunk.
    Prefer the top-level `embed` string (the chunk's Markdown rolled into one string).
    Fall back to concatenating each segment's `markdown` field.
    """
    embed_text = raw_chunk.get("embed", "").strip()
    if embed_text:
        return embed_text
    parts = []
    for seg in raw_chunk.get("segments", []):
        md = (seg.get("markdown") or seg.get("content") or "").strip()
        if md:
            parts.append(md)
    return "\n\n".join(parts)


# ── Normalize one Unsiloed chunk → corpus.py schema (step 3) ─────────────────

def normalize_chunk(
    raw_chunk,
    doc_info,
    api_key,
    base_url,
    chunk_index=0,
    dry_run=False,
):
    """
    Normalize one raw Unsiloed chunk into the corpus.py chunk schema (exactly 10 keys):
        {id, sop_id, section, machine_id, doc_type, procedure_title,
         safety_flag(bool), fault_codes(str), page, text}

    `doc_info`    — the manifest document dict for this PDF.
    `chunk_index` — zero-based ordinal of this chunk within the PDF (used to disambiguate ids).
    When `dry_run=True`, Extract is skipped (no API call).

    Field mapping (see top-of-file comment block):
      id               — f"{doc_id}--{i:03d}--{slug(section)}"
                         Ordinal prefix guarantees uniqueness across headerless chunks
                         (e.g. tables, figure captions, repeated "Overview" sections)
                         that would otherwise collide on the section slug alone.
                         Parse order is stable, so ids are stable across re-ingestion
                         of the same document.  Deviation from the task's
                         "slug(doc_id)-section" phrasing is intentional — uniqueness
                         wins over a simpler format (Moss dedupes by id, so collisions
                         silently discard chunks).
      sop_id           — str(doc_id)
      section          — SectionHeader/Title segment text; "Overview" if none
      machine_id       — from manifest (never auto-detected)
      doc_type         — manifest "type" field (default "manual")
      procedure_title  — Extract "procedure_title".value (conf≥0.7), else sop_id
      safety_flag      — derived (see _derive_safety_flag)
      fault_codes      — ",".join(Extract "error_codes".value) or ""
      page             — first segment page_number (int) or None
                         NOTE: retriever.py MossRetriever hardcodes page=None on
                         return — page is stored but won't surface at query time
                         until the retriever is updated (Phase 4 DoD handoff item).
      text             — f"{procedure_title}\n\n{chunk_markdown}"
    """
    doc_id       = str(doc_info["doc_id"])
    machine_id   = str(doc_info["machine_id"])
    doc_type     = str(doc_info.get("type", "manual"))
    manifest_safety = bool(doc_info.get("safety_flag", False))

    segments = raw_chunk.get("segments", [])
    section  = _section_heading(segments)
    page     = _first_page(segments)
    md_text  = _chunk_markdown(raw_chunk)

    # Step 2: Extract (skipped in dry-run or if empty chunk)
    # TODO(needs-api-key): the extract_chunk call below makes a live Unsiloed API call.
    extract_result = {}
    if not dry_run and md_text.strip():
        extract_result = extract_chunk(md_text, api_key, base_url)

    # Pull Extract fields (with confidence gating)
    def _extract_field(name):
        """Return (value, confidence) for an Extract field, or (None, None)."""
        field = extract_result.get(name, {})
        if not isinstance(field, dict):
            return None, None
        return field.get("value"), field.get("confidence")

    title_val, title_conf   = _extract_field("procedure_title")
    codes_val, codes_conf   = _extract_field("error_codes")
    safety_val, safety_conf = _extract_field("safety_flag")

    # Procedure title: trust Extract if confidence ≥ 0.7, else fall back to sop_id.
    if title_val and title_conf is not None and title_conf >= 0.7:
        procedure_title = str(title_val)
    else:
        procedure_title = doc_id  # conservative fallback

    # Fault codes: join list → comma-separated string (mirrors corpus.py).
    if codes_val and isinstance(codes_val, list):
        fault_codes = ",".join(str(c) for c in codes_val)
    else:
        fault_codes = ""

    # Safety flag: conservative derivation (G12).
    safety_flag = _derive_safety_flag(
        text=md_text,
        extract_safety=safety_val,
        extract_confidence=safety_conf,
        manifest_safety=manifest_safety,
    )

    # Chunk id: ordinal-prefixed to prevent collisions on repeated/headerless section slugs.
    # Format: "{doc_id}--{i:03d}--{slug(section)}"  (double-dash convention from corpus.py)
    chunk_id = f"{doc_id}--{chunk_index:03d}--{_slug(section)}"

    # Text: same pattern as corpus.py  f"{procedure_title}\n\n{section_text}"
    text = f"{procedure_title}\n\n{md_text}" if md_text.strip() else procedure_title

    return {
        "id":              chunk_id,
        "sop_id":          doc_id,
        "section":         section,
        "machine_id":      machine_id,
        "doc_type":        doc_type,
        "procedure_title": procedure_title,
        "safety_flag":     safety_flag,
        "fault_codes":     fault_codes,
        "page":            page,
        "text":            text,
    }


# ── Intentional-gap guard ─────────────────────────────────────────────────────

def _is_intentional_gap(doc_info):
    """
    Guard against accidentally ingesting documents whose content covers the
    intentional-gap query topics (manifest `intentional_gaps`).

    The gap queries are about bypassing safety interlocks and widening soft-limits.
    None of the 3 OEM PDFs currently in the manifest are themselves "gap docs" —
    they are real manuals and SHOULD be ingested.  This check is forward-looking:
    if a PDF whose doc_id or title strongly matches a gap keyword is later added,
    this function returns True and the PDF is skipped.

    The actual intentional-gap protection is corpus-level (those QUERY TOPICS have
    no positive-answer documents in the corpus) — not a path-based filter.
    """
    combined = " ".join([
        str(doc_info.get("doc_id", "")),
        str(doc_info.get("title", "")),
        str(doc_info.get("path", "")),
    ]).lower()
    return any(kw in combined for kw in _INTENTIONAL_GAP_KEYWORDS)


def write_local_index(pdf_chunks):
    """Rebuild data/moss_index.json from SOP chunks + Unsiloed PDF chunks."""
    sop_chunks = corpus.build_chunks()
    all_chunks = sop_chunks + list(pdf_chunks)
    by_machine = {}
    for c in all_chunks:
        by_machine[c["machine_id"]] = by_machine.get(c["machine_id"], 0) + 1
    print(
        f"\nLocal Moss index: {len(all_chunks)} chunks "
        f"({len(sop_chunks)} SOP + {len(pdf_chunks)} PDF)  by machine: {by_machine}"
    )
    for c in all_chunks:
        src = "PDF" if c.get("doc_type") == "manual" else "SOP"
        print(f"   [{src}] {c['id']:<32}  [{c['machine_id']}]  §={c['section']!r}")
    embed_and_write(all_chunks)


# ── Per-PDF processing ────────────────────────────────────────────────────────

def process_pdf(pdf_path, doc_info, api_key, base_url, dry_run=False):
    """
    Full pipeline for one PDF (steps 1 + 2 + 3):
      1. Parse (Unsiloed) → raw chunks
      2. For each raw chunk: Extract → normalize to corpus schema
    Returns list of normalized chunk dicts.

    # TODO(needs-api-key): Calls Unsiloed /parse and /extract APIs.
    """
    doc_id = doc_info["doc_id"]
    print(f"\nProcessing {pdf_path.name!r}  (doc_id={doc_id!r}, machine={doc_info['machine_id']!r})")

    if dry_run:
        # Dry-run: synthesize one dummy chunk per PDF to verify normalization logic.
        dummy_raw = {
            "segments": [
                {
                    "segment_type": "SectionHeader",
                    "content":      f"[DRY-RUN] {doc_id} Overview",
                    "markdown":     f"# {doc_id} Overview\n\nDry-run placeholder — no API call made.",
                    "page_number":  1,
                    "confidence":   1.0,
                }
            ],
            "embed": f"# {doc_id} Overview\n\nDry-run placeholder — no API call made.",
        }
        chunk = normalize_chunk(
            dummy_raw, doc_info, api_key="", base_url="",
            chunk_index=0, dry_run=True,
        )
        print(f"  [dry-run] 1 dummy chunk: id={chunk['id']!r}")
        return [chunk]

    # TODO(needs-api-key): Live path — makes real Unsiloed API calls below.
    parse_result = parse_pdf(pdf_path, api_key, base_url)
    raw_chunks   = parse_result.get("chunks", [])
    print(f"  Parse returned {len(raw_chunks)} raw chunks")

    normalized = []
    for i, raw in enumerate(raw_chunks):
        chunk = normalize_chunk(
            raw, doc_info, api_key, base_url,
            chunk_index=i, dry_run=False,
        )
        if not chunk["text"].strip():
            continue  # skip empty chunks
        normalized.append(chunk)
        seg_count = len(raw.get("segments", []))
        print(
            f"  chunk {i+1}/{len(raw_chunks)}  "
            f"id={chunk['id']!r}  page={chunk['page']}  "
            f"safety={chunk['safety_flag']}  codes={chunk['fault_codes']!r}  "
            f"segs={seg_count}"
        )
    print(f"  → {len(normalized)} normalized chunks (from {len(raw_chunks)} raw)")
    return normalized


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Phase 4 Unsiloed ingestion: real manual PDFs → Unsiloed Parse/Extract "
            "→ normalize → Moss index (alongside SOP chunks)."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Normalize chunks without calling Unsiloed or Moss "
            "(useful for schema/syntax verification when no API key is available)."
        ),
    )
    parser.add_argument(
        "--pdf",
        metavar="PATH",
        help="Process a single PDF path instead of all manuals (relative to repo root or absolute).",
    )
    args = parser.parse_args()

    # Load .env so env vars are available.
    load_env()

    # API key check — exit cleanly if unset (unless dry-run, which doesn't call Unsiloed).
    if not args.dry_run:
        api_key = _require_api_key()
    else:
        api_key = os.environ.get("UNSILOED_API_KEY", "")
        print("[dry-run] Skipping UNSILOED_API_KEY check — no API calls will be made.")

    base_url = _base_url()
    manifest  = _load_manifest()
    path_idx  = _build_path_index(manifest)

    # Determine which PDFs to process.
    if args.pdf:
        pdf_paths = [Path(args.pdf).resolve()]
    else:
        pdf_paths = _find_pdfs()

    print(f"\nUnsiloed ingestion — {len(pdf_paths)} PDF(s)  base_url={base_url!r}")
    print(f"  dry_run={args.dry_run}\n")

    all_chunks = []
    skipped    = []
    for pdf_path in pdf_paths:
        doc_info = _manifest_doc_for_pdf(pdf_path, path_idx)
        if doc_info is None:
            print(f"  SKIP {pdf_path.name!r} — not found in manifest (add it first)")
            skipped.append(pdf_path.name)
            continue
        if _is_intentional_gap(doc_info):
            print(f"  SKIP {pdf_path.name!r} — matches intentional-gap keyword guard")
            skipped.append(pdf_path.name)
            continue

        chunks = process_pdf(pdf_path, doc_info, api_key, base_url, dry_run=args.dry_run)
        all_chunks.extend(chunks)

    # Summary.
    by_machine = {}
    for c in all_chunks:
        by_machine[c["machine_id"]] = by_machine.get(c["machine_id"], 0) + 1
    print(
        f"\n── Normalization complete ──────────────────────────────────────────────\n"
        f"  {len(all_chunks)} Unsiloed chunks  by machine: {by_machine}\n"
        f"  {len(skipped)} PDF(s) skipped: {skipped or 'none'}"
    )

    if args.dry_run:
        print("\n[dry-run] Skipping local Moss index build.")
        return

    if not all_chunks:
        print("No chunks to index — nothing to do.")
        return

    write_local_index(all_chunks)
    print(
        "\nPhase 4 complete. data/moss_index.json now contains SOP + real-manual chunks.\n"
        "Next: ask a question whose answer lives only in the real manual to verify retrieval.\n"
    )


if __name__ == "__main__":
    main()
