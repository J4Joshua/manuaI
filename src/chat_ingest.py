#!/usr/bin/env python3
"""Chat ingestion — operator chat logs → Unsiloed → a SEPARATE Moss `chats` index.

Chats (Slack-style operator threads) are a SECONDARY, supplemental data source: at
query time Moss retrieves prior incidents that resemble the operator's problem, and
they act as corroboration / guidance for the LLM — NOT as an approved procedure and
NEVER as a citation. The authoritative grounding stays the SOP/manual `manuals` index.

Per the design, this reuses **Unsiloed** for parse + extract + chunk (no bespoke
parser). Unsiloed ingests documents, not raw JSON, so the only added step is a thin
render of each chat thread → a 1-page PDF; everything after that is the same pipeline
as unsiloed_ingest.py, with a chat-specific Extract schema and `doc_type="chat"`.

    chat thread (.json) → render PDF → Unsiloed Parse (chunk) + Extract → normalize → Moss `chats`

Chat thread JSON shape (see data/chats/<machine_id>/*.json):
    {thread_id, machine_id, channel, date, topic, messages:[{user, ts, text}, ...]}

Usage:
    .venv/bin/python src/chat_ingest.py                       # all data/chats/**/*.json
    .venv/bin/python src/chat_ingest.py FILE_OR_DIR ...
    .venv/bin/python src/chat_ingest.py --machine cobot-cellA some_thread.json
    .venv/bin/python src/chat_ingest.py --dry-run            # parse+extract+normalize, print, no Moss
    .venv/bin/python src/chat_ingest.py --index chats-test   # build a throwaway index
    .venv/bin/python src/chat_ingest.py --json-out FILE      # also dump normalized chat chunks

Env: same as unsiloed_ingest.py (UNSILOED_API_KEY, MOSS_*). Index defaults to "chats".
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sys
import tempfile
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import XPos, YPos

import paths
from retriever import load_env
# Reuse the Unsiloed pipeline + helpers verbatim — chats go through the SAME engine.
from unsiloed_ingest import (
    _CONF_FLOOR, _SAFETY_RE, _base_url, _chunk_markdown, _field, _require_api_key,
    _section_heading, _slug, extract_document, load_into_moss, parse_document,
)

DEFAULT_INDEX = "chats"

# Chat-specific Extract schema (Unsiloed /v2/extract). Per-document = per-thread.
CHAT_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "incident_summary": {
            "type": "string",
            "description": "One sentence: what the equipment problem was and how the operators resolved it.",
        },
        "equipment_problem": {
            "type": "string",
            "description": "The symptom or fault the operators were discussing (e.g. 'E-42 label-web jam at peel tip').",
        },
        "error_codes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every fault, alarm, or error code mentioned in the conversation (e.g. 'E-42', 'C4').",
        },
        "resolution": {
            "type": "string",
            "description": "The concrete fix or steps the operators actually applied to resolve the issue.",
        },
        "resolved": {
            "type": "boolean",
            "description": "True if the conversation ends with the issue resolved / equipment back in service.",
        },
    },
    "required": ["incident_summary"],
    "additionalProperties": False,
}


# ── Chat thread → PDF (the only added step; Unsiloed does the real work) ───────

def render_thread_pdf(thread: dict, out_path: Path) -> Path:
    """Render one chat thread to a clean, text-layer PDF that Unsiloed can parse."""
    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def line(txt: str, size=11, style="", gap=5):
        pdf.set_font("Helvetica", style, size)
        # latin-1 only (core fonts); drop anything else so fpdf never errors.
        safe = txt.encode("latin-1", "replace").decode("latin-1")
        # new_x=LMARGIN + new_y=NEXT returns the cursor to the left margin and advances
        # a line, so the next multi_cell always has full page width to work with.
        pdf.multi_cell(0, gap, safe, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    topic = thread.get("topic") or thread.get("thread_id", "Operator chat")
    line(f"Operator chat thread: {topic}", size=14, style="B", gap=7)
    meta = "  ".join(
        f"{k}: {thread[k]}" for k in ("thread_id", "machine_id", "channel", "date") if thread.get(k)
    )
    line(meta, size=9, gap=5)
    pdf.ln(2)
    for m in thread.get("messages", []):
        who = f"[{m.get('ts','')}] {m.get('user','operator')}:"
        line(who, size=10, style="B", gap=5)
        line(m.get("text", ""), size=11, gap=5)
        pdf.ln(1)

    pdf.output(str(out_path))
    return out_path


# ── Thread metadata resolution (no manifest needed) ───────────────────────────

def resolve_thread_meta(path: Path, thread: dict, machine_arg: str | None) -> dict:
    """thread_id + machine_id from the JSON, falling back to the path / --machine."""
    machine = thread.get("machine_id") or machine_arg
    if not machine:
        m = [p for p in path.parts if p in ("labeler-line3", "cobot-cellA")]
        machine = m[0] if m else (machine_arg or "all")
    return {
        "thread_id": str(thread.get("thread_id") or _slug(path.stem).upper()),
        "machine_id": str(machine),
        "topic": thread.get("topic") or thread.get("thread_id") or path.stem,
    }


# ── Normalize Unsiloed output → chat records (same 10-key schema, doc_type=chat) ─

def normalize_chat(parse_result: dict, extract_result: dict, meta: dict) -> list[dict]:
    """One Unsiloed chunk → one chat record. doc_type='chat'. The extracted
    resolution/summary are folded into `text` (so they embed + reach the LLM) and
    `procedure_title` (so the screen shows the gist of the prior incident)."""
    tid = meta["thread_id"]
    summary_v, summary_c = _field(extract_result, "incident_summary")
    resolution_v, _ = _field(extract_result, "resolution")
    resolved_v, _ = _field(extract_result, "resolved")
    codes_v, _ = _field(extract_result, "error_codes")
    summary = str(summary_v) if (summary_v and summary_c >= _CONF_FLOOR) else meta["topic"]
    resolution = str(resolution_v) if resolution_v else ""
    resolved = "resolved" if resolved_v else "unresolved"
    doc_codes = [str(c).strip() for c in (codes_v or []) if str(c).strip()]

    # Guidance preamble: makes the prior-incident outcome explicit to retrieval + LLM.
    preamble = f"[Prior operator incident — {resolved}] {summary}".strip()
    if resolution:
        preamble += f"\nResolution: {resolution}"

    out = []
    for i, ch in enumerate(parse_result.get("chunks", [])):
        segs = ch.get("segments", [])
        if segs and all(s.get("segment_type") in ("PageHeader", "PageFooter") for s in segs):
            continue
        md = _chunk_markdown(ch)
        if not md.strip():
            continue
        section = _section_heading(segs)
        here = [c for c in doc_codes if c.lower() in md.lower()]
        out.append({
            "id": f"{tid}--{i:03d}--{_slug(section)}",
            "sop_id": tid,                       # the thread id is the "source" id for chats
            "section": meta["topic"],
            "machine_id": meta["machine_id"],
            "doc_type": "chat",
            "procedure_title": summary,          # one-line incident gist (shown as corroboration)
            "safety_flag": bool(_SAFETY_RE.search(md)),
            "fault_codes": ",".join(dict.fromkeys(here)),
            "page": None,
            "text": f"{preamble}\n\n{md}",
        })
    return out


# ── Per-thread driver ──────────────────────────────────────────────────────────

def process_thread(path: Path, meta: dict, api_key: str, base_url: str, tmpdir: Path) -> list[dict]:
    print(f"\n{path.name}  → thread={meta['thread_id']!r} machine={meta['machine_id']!r}")
    pdf_path = render_thread_pdf(json.loads(path.read_text()), tmpdir / f"{meta['thread_id']}.pdf")
    parse_result = parse_document(pdf_path, api_key, base_url)
    print(f"  parsed: {parse_result.get('total_chunks', len(parse_result.get('chunks', [])))} chunks")
    extract_result = extract_document(pdf_path, api_key, base_url, schema=CHAT_EXTRACT_SCHEMA)
    if extract_result:
        sv, sc = _field(extract_result, "incident_summary")
        cv, _ = _field(extract_result, "error_codes")
        rv, _ = _field(extract_result, "resolved")
        print(f"  extracted: summary={str(sv)[:70]!r} (conf {sc:.2f})  codes={cv}  resolved={rv}")
    records = normalize_chat(parse_result, extract_result, meta)
    print(f"  → {len(records)} chat records")
    return records


def _collect(args_paths: list[str]) -> list[Path]:
    if not args_paths:
        return sorted(Path(p) for p in glob.glob(str(paths.DATA / "chats" / "**" / "*.json"), recursive=True))
    out: list[Path] = []
    for a in args_paths:
        p = Path(a)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.json")))
        elif p.is_file():
            out.append(p)
        else:
            print(f"  SKIP {a!r} — not found", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description="Chat ingestion: operator chats → Unsiloed → separate Moss `chats` index.")
    ap.add_argument("paths", nargs="*", help="chat .json files or dirs (default: data/chats/**/*.json)")
    ap.add_argument("--machine", help="machine_id for threads missing one")
    ap.add_argument("--dry-run", action="store_true", help="parse+extract+normalize and print; do NOT touch Moss")
    ap.add_argument("--index", default=DEFAULT_INDEX, help=f"Moss index name (default {DEFAULT_INDEX!r})")
    ap.add_argument("--json-out", metavar="FILE", help="also write the normalized chat chunks to FILE")
    args = ap.parse_args()

    load_env()
    api_key = _require_api_key()
    base_url = _base_url()

    threads = _collect(args.paths)
    if not threads:
        sys.exit("No chat threads to process (looked under data/chats/**/*.json).")
    print(f"Chat ingestion — {len(threads)} thread(s) → Moss index {args.index!r}  base_url={base_url}")

    all_records: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        for path in threads:
            thread = json.loads(path.read_text())
            meta = resolve_thread_meta(path, thread, args.machine)
            try:
                all_records.extend(process_thread(path, meta, api_key, base_url, tmpdir))
            except Exception as exc:  # noqa: BLE001 - one bad thread shouldn't abort the batch
                print(f"  ERROR on {path.name}: {exc} — skipping", file=sys.stderr)

    by_machine: dict[str, int] = {}
    for c in all_records:
        by_machine[c["machine_id"]] = by_machine.get(c["machine_id"], 0) + 1
    print(f"\n── {len(all_records)} chat records  by machine: {by_machine} ──")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(all_records, indent=2))
        print(f"  wrote {args.json_out}")

    if args.dry_run:
        print("\n[dry-run] Skipping Moss. Re-run without --dry-run to build the chats index.")
        return
    if not all_records:
        print("No chat records — nothing to load.")
        return

    # with_sops=False: the chats index is chat-ONLY (kept separate from `manuals`).
    asyncio.run(load_into_moss(all_records, with_sops=False, index_name=args.index))
    print(
        f"\nDone — Moss '{args.index}' built from operator chats (supplemental source).\n"
        "core.answer queries this alongside `manuals`; chats corroborate/guide, never cite."
    )


if __name__ == "__main__":
    main()
