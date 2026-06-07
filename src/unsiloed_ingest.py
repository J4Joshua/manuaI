#!/usr/bin/env python3
"""Unsiloed ingestion — ANY SOP/PDF → Unsiloed Parse + Extract + chunk → Moss.

Unsiloed does ALL of the work that turns a raw document into indexable records:
  • PARSE   (POST /parse)      — layout/OCR → ordered, typed segments + Markdown
  • CHUNK   (Parse `chunks[]`) — Unsiloed groups adjacent segments into chunks; we
                                 take each Unsiloed chunk as ONE record (we never
                                 re-chunk — chunk boundaries are Unsiloed's).
  • EXTRACT (POST /v2/extract) — schema-driven structured fields (title, error
                                 codes, safety) with confidence scores.
Then we normalize each chunk to the §3a corpus schema and load the whole set into
Moss (alongside the authored .md SOP chunks). This is the one-time, wifi-ON,
off-the-query-path ingestion step (ARCHITECTURE.md §4b, §12).

Endpoints below are VERIFIED live (2026-06-06) against the real API — they differ
from the published docs in a few places (noted inline):
  • parse  status == "Succeeded"; result.chunks[] = [{chunk_id, embed, segments[]}]
  • extract status == "completed"; result[field] = {value, score:{grounding_score,
    extraction_score}, page_no?, bboxes?}  (score is an OBJECT, not a float)

Usage:
    .venv/bin/python src/unsiloed_ingest.py                      # all data/machines/*/manuals/*.pdf
    .venv/bin/python src/unsiloed_ingest.py FILE_OR_DIR ...      # specific PDFs / folders of PDFs
    .venv/bin/python src/unsiloed_ingest.py --machine cobot-cellA some.pdf
    .venv/bin/python src/unsiloed_ingest.py --pages 1-10 big.pdf # limit pages (credits ≈ 5/page)
    .venv/bin/python src/unsiloed_ingest.py --dry-run ...        # parse+extract+normalize, print, DON'T touch Moss
    .venv/bin/python src/unsiloed_ingest.py --no-sops ...        # index ONLY the Unsiloed chunks (omit authored SOPs)
    .venv/bin/python src/unsiloed_ingest.py --json-out FILE ...  # also dump normalized chunks as JSON

Env (.env): UNSILOED_API_KEY (required), UNSILOED_BASE_URL, MOSS_PROJECT_ID/KEY,
MOSS_INDEX_NAME (default "manuals"), MOSS_MODEL_ID (default "moss-minilm").
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import corpus
import paths
from retriever import load_env, make_client

# ── Constants ────────────────────────────────────────────────────────────────

_POLL_INTERVAL = 4       # seconds between status polls
_POLL_TIMEOUT = 600      # 10 min per document (big manuals OCR slowly)
_CREDITS_PER_PAGE = 5    # observed; for the cost estimate only

# Force safety_flag=True when a chunk's text trips any of these (a wrong "safe"
# is a worker-injury risk → fail safe). Mirrors the gate in core/render.
_SAFETY_RE = re.compile(
    r"\b(WARNING|CAUTION|DANGER|NOTICE|LOTO|LOCK\s?OUT|TAG\s?OUT|HAZARD|"
    r"DO NOT|E-?STOP|EMERGENCY STOP|ELECTRICAL SHOCK|PINCH POINT)\b",
    re.IGNORECASE,
)

# Document-level extraction schema (JSON Schema). Unsiloed Extract is per-DOCUMENT,
# so these describe the whole file; per-chunk fields (section, page) come from Parse,
# and doc-level error_codes are distributed to the chunks whose text contains them.
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_title": {
            "type": "string",
            "description": "The title or name of this manual / SOP / procedure document.",
        },
        "equipment_name": {
            "type": "string",
            "description": "The machine, equipment, or model this document is about (e.g. 'Label-Aire 3115NV', 'Universal Robots UR20').",
        },
        "error_codes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every fault, alarm, or error code mentioned anywhere in the document, verbatim (e.g. 'E-42', 'C4', 'C50').",
        },
        "has_safety_warnings": {
            "type": "boolean",
            "description": "True if the document contains any safety warning, caution, danger notice, lockout/tagout instruction, or personal-injury hazard.",
        },
    },
    "required": ["document_title"],
    "additionalProperties": False,
}

# Confidence floor: below this, fall back to conservative defaults (G12).
_CONF_FLOOR = 0.3


# ── Config helpers ────────────────────────────────────────────────────────────

def _require_api_key() -> str:
    key = os.environ.get("UNSILOED_API_KEY", "").strip()
    if not key:
        sys.exit(
            "UNSILOED_API_KEY is not set.\n"
            "  1. Get a key at https://unsiloed.ai (or support@unsiloed.ai).\n"
            "  2. Put it in .env:  UNSILOED_API_KEY=<your-key>\n"
        )
    return key


def _base_url() -> str:
    return os.environ.get("UNSILOED_BASE_URL", "https://prod.visionapi.unsiloed.ai").rstrip("/")


def _headers(api_key: str) -> dict:
    return {"api-key": api_key}


# ── Document metadata resolution (works WITHOUT the manifest) ──────────────────

def _load_manifest_index() -> dict:
    """Optional enrichment: {relative_path: doc_dict} from data/manifest.json.
    Absent/unreadable manifest is fine — metadata is then derived from the path."""
    p = paths.DATA / "manifest.json"
    try:
        docs = json.loads(p.read_text()).get("documents", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {d.get("path"): d for d in docs if d.get("path")}


def _slug(text: str) -> str:
    """Slug a heading the same way corpus.py does."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-") or "sec"


def resolve_doc_meta(pdf_path: Path, manifest_index: dict, machine_arg: str | None) -> dict:
    """Resolve {sop_id, machine_id, doc_type, manifest_safety} for ANY file.

    Order: manifest (by path under data/) → path pattern machines/<id>/<type>/ →
    --machine arg → 'all'. doc_id defaults to a stable slug of the filename stem so
    ingestion works on files that were never registered in the manifest."""
    rel = None
    try:
        rel = str(pdf_path.resolve().relative_to(paths.DATA))
    except ValueError:
        rel = None

    doc = manifest_index.get(rel) if rel else None
    if doc:
        return {
            "sop_id": str(doc["doc_id"]),
            "machine_id": str(doc.get("machine_id") or machine_arg or "all"),
            "doc_type": str(doc.get("type", "manual")),
            "manifest_safety": bool(doc.get("safety_flag", False)),
        }

    # Not in the manifest — derive from the path / args.
    machine_id = machine_arg or "all"
    doc_type = "manual"
    m = re.search(r"machines/([^/]+)/(manuals|sops|reference)/", str(pdf_path).replace("\\", "/"))
    if m:
        machine_id = machine_arg or m.group(1)
        doc_type = {"manuals": "manual", "sops": "sop", "reference": "reference"}[m.group(2)]
    return {
        "sop_id": _slug(pdf_path.stem).upper(),
        "machine_id": machine_id,
        "doc_type": doc_type,
        "manifest_safety": False,
    }


# ── Unsiloed PARSE (chunking happens here, server-side) ────────────────────────

def parse_document(pdf_path: Path, api_key: str, base_url: str, pages: str | None = None) -> dict:
    """POST /parse (multipart) → poll GET /parse/{job_id} until Succeeded.
    Returns the completed result dict (carries `chunks[]` — the Unsiloed chunking)."""
    data = {
        "ocr_strategy": "auto_detection",     # force_ocr for photographed SOPs
        "use_high_resolution": "true",        # better OCR on factory scans
        "merge_tables": "true",               # keep cross-page spec tables whole
        "export_format": '["markdown","json"]',
        "output_fields": '{"markdown": true, "content": true, "bbox": true, "confidence": true}',
    }
    if pages:
        data["page_range"] = pages
    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{base_url}/parse",
            headers=_headers(api_key),
            files={"file": (pdf_path.name, f, "application/pdf")},
            data=data,
            timeout=120,
        )
    r.raise_for_status()
    sub = r.json()
    job_id = sub.get("job_id")
    if not job_id:
        raise RuntimeError(f"/parse returned no job_id: {sub}")
    print(f"  parse  job={job_id}  credits_used={sub.get('credit_used')}  quota_left={sub.get('quota_remaining')}")
    return _poll(f"{base_url}/parse/{job_id}", api_key, ok={"succeeded"}, what="parse")


# ── Unsiloed EXTRACT (structured fields, per document) ─────────────────────────

def extract_document(pdf_path: Path, api_key: str, base_url: str, schema: dict | None = None) -> dict:
    """POST /v2/extract (multipart pdf_file + schema_data) → poll GET /extract/{job_id}.
    Returns the `result` dict: {field: {value, score:{grounding_score, extraction_score}, ...}}.
    `schema` defaults to the manual/SOP EXTRACT_SCHEMA; chat_ingest passes its own.
    Returns {} on any failure (caller falls back to parse-derived + conservative defaults)."""
    try:
        with open(pdf_path, "rb") as f:
            r = requests.post(
                f"{base_url}/v2/extract",
                headers=_headers(api_key),
                files={"pdf_file": (pdf_path.name, f, "application/pdf")},
                data={
                    "schema_data": json.dumps(schema or EXTRACT_SCHEMA),
                    "model": "gamma",            # recommended default tier
                    "enable_citations": "true",  # page_no + bboxes per field
                },
                timeout=120,
            )
        r.raise_for_status()
        sub = r.json()
        job_id = sub.get("job_id")
        if not job_id:
            print(f"  extract: no job_id ({sub}) — continuing without Extract", file=sys.stderr)
            return {}
        print(f"  extract job={job_id}  quota_left={sub.get('quota_remaining')}")
        done = _poll(f"{base_url}/extract/{job_id}", api_key, ok={"completed", "succeeded"}, what="extract")
        return done.get("result", {}) or {}
    except (requests.RequestException, RuntimeError, TimeoutError) as exc:
        print(f"  extract failed ({exc}) — continuing without Extract", file=sys.stderr)
        return {}


def _poll(url: str, api_key: str, ok: set[str], what: str) -> dict:
    """Poll a job URL until status ∈ ok (case-insensitive). Raises on Failed/timeout."""
    start = time.monotonic()
    while True:
        r = requests.get(url, headers=_headers(api_key), timeout=60)
        r.raise_for_status()
        res = r.json()
        status = str(res.get("status", "")).lower()
        if status in ok:
            return res
        if status in ("failed", "error"):
            raise RuntimeError(f"{what} job failed: {res.get('message', res)}")
        if time.monotonic() - start > _POLL_TIMEOUT:
            raise TimeoutError(f"{what} job timed out after {_POLL_TIMEOUT}s (last status={status!r})")
        time.sleep(_POLL_INTERVAL)


# ── Extract-field access (score is a nested object on the real API) ─────────────

def _clean_value(v):
    """Unwrap citation-wrapped Extract values. With enable_citations=true the API wraps
    each value (and each ARRAY element) as {'__value__': {'value': X, 'score':…, 'citation':…}};
    strip that back to plain X / [X, …] so downstream sees clean scalars + string lists."""
    def unwrap(e):
        if isinstance(e, dict) and "__value__" in e:
            inner = e["__value__"]
            return inner.get("value") if isinstance(inner, dict) else inner
        return e
    if isinstance(v, list):
        return [unwrap(e) for e in v]
    return unwrap(v)


def _field(extract_result: dict, name: str) -> tuple[object, float]:
    """Return (value, confidence) for one Extract field. confidence = max(extraction,
    grounding) score; 0.0 if absent. Real API: result[name] = {value, score:{...}}."""
    f = extract_result.get(name)
    if not isinstance(f, dict):
        return None, 0.0
    score = f.get("score")
    if isinstance(score, dict):
        conf = max(float(score.get("extraction_score", 0) or 0),
                   float(score.get("grounding_score", 0) or 0))
    else:
        conf = float(score or 0)
    return _clean_value(f.get("value")), conf


# ── Parse-chunk helpers ────────────────────────────────────────────────────────

def _section_heading(segments: list) -> str:
    for s in segments:
        if s.get("segment_type") in ("Title", "SectionHeader"):
            t = (s.get("content") or s.get("markdown") or "").strip().lstrip("#").strip()
            if t:
                return t
    return "Overview"


def _first_page(segments: list):
    for s in segments:
        pn = s.get("page_number")
        if pn is not None:
            try:
                return int(pn)
            except (TypeError, ValueError):
                pass
    return None


def _chunk_markdown(chunk: dict) -> str:
    embed = (chunk.get("embed") or "").strip()
    if embed:
        return embed
    parts = [(s.get("markdown") or s.get("content") or "").strip() for s in chunk.get("segments", [])]
    return "\n\n".join(p for p in parts if p)


# ── Normalize: Unsiloed chunks + Extract → §3a corpus records ─────────────────

def normalize(parse_result: dict, extract_result: dict, meta: dict) -> list[dict]:
    """Turn one document's Unsiloed output into corpus-schema chunk records (10 keys).
    ONE Unsiloed chunk → ONE record (no re-chunking). Doc-level Extract fields enrich
    every chunk; per-chunk section/page come from Parse; doc-level error_codes are
    attributed to the chunks whose text actually contains them."""
    doc_id = meta["sop_id"]

    # Document-level Extract fields (Unsiloed-extracted).
    title_val, title_conf = _field(extract_result, "document_title")
    codes_val, _ = _field(extract_result, "error_codes")
    safety_val, safety_conf = _field(extract_result, "has_safety_warnings")
    doc_title = str(title_val) if (title_val and title_conf >= _CONF_FLOOR) else doc_id
    doc_codes = [str(c).strip() for c in (codes_val or []) if str(c).strip()]
    # Conservative doc-level safety: keyword/manifest win; trust a False only if confident.
    doc_safe_default = bool(meta["manifest_safety"]) or (
        bool(safety_val) if safety_conf >= _CONF_FLOOR else True
    )

    chunks = parse_result.get("chunks", [])
    out = []
    for i, ch in enumerate(chunks):
        segs = ch.get("segments", [])
        # Drop chrome-only chunks (page numbers, running headers/footers).
        if segs and all(s.get("segment_type") in ("PageHeader", "PageFooter") for s in segs):
            continue
        md = _chunk_markdown(ch)
        if not md.strip():
            continue

        section = _section_heading(segs)
        procedure_title = section if section != "Overview" else doc_title

        # Fault codes for THIS chunk = doc-level extracted codes that appear in its text.
        here = [c for c in doc_codes if c.lower() in md.lower()]
        fault_codes = ",".join(dict.fromkeys(here))  # de-dupe, keep order

        # Safety: per-chunk keyword OR (doc has warnings & no local signal) OR manifest.
        safety_flag = bool(_SAFETY_RE.search(md)) or doc_safe_default

        out.append({
            "id": f"{doc_id}--{i:03d}--{_slug(section)}",
            "sop_id": doc_id,
            "section": section,
            "machine_id": meta["machine_id"],
            "doc_type": meta["doc_type"],
            "procedure_title": procedure_title,
            "safety_flag": safety_flag,
            "fault_codes": fault_codes,
            "page": _first_page(segs),
            "text": f"{procedure_title}\n\n{md}",
        })
    return out


# ── Per-document driver ────────────────────────────────────────────────────────

def process(pdf_path: Path, meta: dict, api_key: str, base_url: str, pages: str | None) -> list[dict]:
    print(f"\n{pdf_path.name}  → doc_id={meta['sop_id']!r} machine={meta['machine_id']!r} type={meta['doc_type']!r}")
    parse_result = parse_document(pdf_path, api_key, base_url, pages)
    print(f"  parsed: {parse_result.get('total_chunks', len(parse_result.get('chunks', [])))} chunks, "
          f"{parse_result.get('page_count', '?')} pages")
    extract_result = extract_document(pdf_path, api_key, base_url)
    if extract_result:
        tv, tc = _field(extract_result, "document_title")
        cv, _ = _field(extract_result, "error_codes")
        print(f"  extracted: title={tv!r} (conf {tc:.2f})  error_codes={cv}")
    records = normalize(parse_result, extract_result, meta)
    print(f"  → {len(records)} records (safety: {sum(r['safety_flag'] for r in records)}, "
          f"with codes: {sum(1 for r in records if r['fault_codes'])})")
    return records


# ── Moss load (Unsiloed chunks + authored SOP chunks) ─────────────────────────

def _to_doc_info(c: dict):
    from inferedge_moss import DocumentInfo
    return DocumentInfo(
        id=c["id"],
        text=c["text"],
        metadata={
            "machine_id": str(c["machine_id"]),
            "sop_id": str(c["sop_id"]),
            "doc_type": str(c["doc_type"]),
            "procedure_title": str(c["procedure_title"]),
            "section": str(c["section"]),
            "safety_flag": "true" if c["safety_flag"] else "false",
            "fault_codes": str(c.get("fault_codes", "")),
            "page": str(c["page"]) if c.get("page") is not None else "",
        },
    )


async def load_into_moss(unsiloed_chunks: list[dict], with_sops: bool, index_name: str | None = None):
    """(Re)build the Moss index from the authored .md SOP chunks (unless --no-sops)
    plus the Unsiloed PDF chunks. create_index is the cloud build step (§12a)."""
    client = make_client()
    index = index_name or os.getenv("MOSS_INDEX_NAME", "manuals")
    model = os.getenv("MOSS_MODEL_ID", "moss-minilm")

    sop_docs = [_to_doc_info(c) for c in corpus.build_chunks()] if with_sops else []
    pdf_docs = [_to_doc_info(c) for c in unsiloed_chunks]
    all_docs = sop_docs + pdf_docs

    by_machine: dict[str, int] = {}
    for d in all_docs:
        by_machine[d.metadata["machine_id"]] = by_machine.get(d.metadata["machine_id"], 0) + 1
    print(f"\nMoss '{index}': {len(all_docs)} docs ({len(sop_docs)} SOP + {len(pdf_docs)} Unsiloed)  by machine: {by_machine}")

    existing = {i.name for i in await client.list_indexes()}
    if index in existing:
        print(f"  deleting existing '{index}'…")
        await client.delete_index(index)
    print(f"  create_index('{index}', {len(all_docs)} docs, '{model}')…")
    await client.create_index(index, all_docs, model)
    for _ in range(60):
        st = str(getattr(await client.get_index(index), "status", "?"))
        if any(s in st.upper() for s in ("READY", "ACTIVE", "COMPLETE", "SUCCEED")):
            print(f"  index READY ({st})")
            return
        if "FAIL" in st.upper():
            raise SystemExit(f"Moss index build failed: {st}")
        await asyncio.sleep(2)
    print("  warning: index not confirmed READY within poll window (continuing).")


# ── File discovery ─────────────────────────────────────────────────────────────

def _collect_pdfs(args_paths: list[str]) -> list[Path]:
    if not args_paths:
        return sorted(paths.DATA.glob("machines/*/manuals/*.pdf"))
    out: list[Path] = []
    for a in args_paths:
        p = Path(a)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.pdf")))
        elif p.is_file():
            out.append(p)
        else:
            print(f"  SKIP {a!r} — not found", file=sys.stderr)
    return out


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Unsiloed ingestion: any PDF → Parse+Extract+chunk → Moss.")
    ap.add_argument("paths", nargs="*", help="PDF files or directories (default: data/machines/*/manuals/*.pdf)")
    ap.add_argument("--machine", help="machine_id for files not resolvable from the manifest/path")
    ap.add_argument("--pages", help="page range to limit cost, e.g. '1-10' or '2,4,6'")
    ap.add_argument("--dry-run", action="store_true", help="Parse+Extract+normalize and print; do NOT touch Moss")
    ap.add_argument("--no-sops", action="store_true", help="index only the Unsiloed chunks (skip authored .md SOPs)")
    ap.add_argument("--index", help="override MOSS_INDEX_NAME (e.g. a throwaway test index)")
    ap.add_argument("--json-out", metavar="FILE", help="also write the normalized chunks to FILE as JSON")
    args = ap.parse_args()

    load_env()
    api_key = _require_api_key()
    base_url = _base_url()
    manifest_index = _load_manifest_index()

    pdfs = _collect_pdfs(args.paths)
    if not pdfs:
        sys.exit("No PDFs to process.")
    print(f"Unsiloed ingestion — {len(pdfs)} file(s)  base_url={base_url}")
    print(f"  est. cost ≈ {_CREDITS_PER_PAGE}×pages per file (Parse) + per-file Extract\n")

    all_chunks: list[dict] = []
    for pdf in pdfs:
        meta = resolve_doc_meta(pdf, manifest_index, args.machine)
        try:
            all_chunks.extend(process(pdf, meta, api_key, base_url, args.pages))
        except (requests.RequestException, RuntimeError, TimeoutError) as exc:
            print(f"  ERROR on {pdf.name}: {exc} — skipping this file", file=sys.stderr)

    by_machine: dict[str, int] = {}
    for c in all_chunks:
        by_machine[c["machine_id"]] = by_machine.get(c["machine_id"], 0) + 1
    print(f"\n── {len(all_chunks)} Unsiloed chunks total  by machine: {by_machine} ──")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(all_chunks, indent=2))
        print(f"  wrote {args.json_out}")

    if args.dry_run:
        print("\n[dry-run] Skipping Moss. Re-run without --dry-run to (re)build the index.")
        return
    if not all_chunks and args.no_sops:
        print("No chunks to load — nothing to do.")
        return

    asyncio.run(load_into_moss(all_chunks, with_sops=not args.no_sops, index_name=args.index))
    print(
        "\nDone — Moss index rebuilt with the Unsiloed chunks.\n"
        "Verify:  .venv/bin/python src/ask.py --retriever moss \"<a question only the manual answers>\"\n"
        "Wifi-off (G14): load_index is a network fetch — authenticate + load ONLINE, keep the\n"
        "process alive, THEN go offline; the cosine stub (index.json) is the bulletproof-offline path."
    )


if __name__ == "__main__":
    main()
