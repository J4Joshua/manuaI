#!/usr/bin/env python3
"""Shared corpus chunker: data/machines/*/sops/*.md -> section-level chunk dicts.

Used by BOTH ingest_local.py (stub / local nomic) and moss_ingest.py (Moss), so the
two indexes hold identical content + ids + metadata. One chunker = one source of truth.

chunk = {id, sop_id, section, machine_id, doc_type, procedure_title,
         safety_flag(bool), fault_codes(str), page(None), text}
`text` is what gets embedded + shown to the LLM; everything else is metadata.
"""
import glob
import re
from pathlib import Path

import yaml

import paths

FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def _parse(path):
    raw = Path(path).read_text()
    m = FM_RE.match(raw)
    if not m:
        return None, None
    return yaml.safe_load(m.group(1)), m.group(2).strip()


def _sections(body):
    """(heading, text) for the preamble (if substantive) + each '## ' section."""
    for p in re.split(r"\n(?=## )", body):
        p = p.strip()
        if not p:
            continue
        head = p.splitlines()[0]
        if head.startswith("## "):
            yield head[3:].strip(), p
        else:
            rest = "\n".join(l for l in p.splitlines() if not l.startswith("# ")).strip()
            if len(rest) > 40:                    # keep a real preamble (e.g. SOP-1192 fault table)
                yield "Overview", p


def build_chunks(pattern="machines/*/sops/*.md"):
    chunks = []
    for path in sorted(glob.glob(str(paths.DATA / pattern))):
        fm, body = _parse(path)
        if not fm:
            continue
        for heading, text in _sections(body):
            slug = re.sub(r"[^A-Za-z0-9]+", "-", heading).strip("-") or "sec"
            chunks.append({
                "id": f"{fm['doc_id']}--{slug}",
                "sop_id": str(fm["doc_id"]),
                "section": heading,
                "machine_id": str(fm["machine_id"]),
                "doc_type": str(fm.get("doc_type", "sop")),
                "procedure_title": fm["title"],
                "safety_flag": bool(fm.get("safety_flag", False)),
                "fault_codes": ",".join(fm.get("fault_codes", []) or []),
                "page": None,
                "text": f"{fm['title']}\n\n{text}",
            })
    return chunks


if __name__ == "__main__":
    cs = build_chunks()
    by = {}
    for c in cs:
        by[c["machine_id"]] = by.get(c["machine_id"], 0) + 1
    print(f"{len(cs)} chunks: {by}")
