#!/usr/bin/env python3
"""Build the REAL Moss index from data/machines/*/sops/*.md — one chunk per
'## ' section, tagged with machine_id/sop_id/section/safety_flag/fault_codes.

Online (create_index builds in the cloud). Run with wifi ON:
    .venv/bin/python moss_ingest.py

Lets Moss embed (raw text in, model moss-minilm) per ARCHITECTURE.md §12b.
PDFs under data/.../manuals/ are Phase 4 (Unsiloed) — not ingested here.
"""
import asyncio
import glob
import os
import re
from pathlib import Path

import yaml
from inferedge_moss import DocumentInfo

from retriever import load_env, make_client

HERE = Path(__file__).resolve().parent
FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def parse_md(path):
    raw = Path(path).read_text()
    m = FM_RE.match(raw)
    if not m:
        return None, None
    return yaml.safe_load(m.group(1)), m.group(2).strip()


def sections(body):
    """(heading, text) for the preamble (if substantive) + each '## ' section."""
    for p in re.split(r"\n(?=## )", body):
        p = p.strip()
        if not p:
            continue
        first = p.splitlines()[0]
        if first.startswith("## "):
            yield first[3:].strip(), p
        else:
            rest = "\n".join(l for l in p.splitlines() if not l.startswith("# ")).strip()
            if len(rest) > 40:           # keep a real preamble (e.g. SOP-1192 fault table)
                yield "Overview", p


def build_docs():
    docs = []
    for path in sorted(glob.glob(str(HERE / "data/machines/*/sops/*.md"))):
        fm, body = parse_md(path)
        if not fm:
            continue
        base = {
            "machine_id": str(fm["machine_id"]),
            "sop_id": str(fm["doc_id"]),
            "doc_type": str(fm.get("doc_type", "sop")),
            "title": fm["title"],
            "safety_flag": str(fm.get("safety_flag", False)).lower(),
            "fault_codes": ",".join(fm.get("fault_codes", []) or []),
        }
        for heading, text in sections(body):
            slug = re.sub(r"[^A-Za-z0-9]+", "-", heading).strip("-") or "sec"
            docs.append(DocumentInfo(
                id=f"{fm['doc_id']}--{slug}",
                text=f"{fm['title']}\n\n{text}",
                metadata={**base, "section": heading},
            ))
    return docs


async def main():
    load_env()
    client = make_client()
    index = os.getenv("MOSS_INDEX_NAME", "manuals")
    model = os.getenv("MOSS_MODEL_ID", "moss-minilm")

    docs = build_docs()
    by_machine = {}
    for d in docs:
        by_machine[d.metadata["machine_id"]] = by_machine.get(d.metadata["machine_id"], 0) + 1
    print(f"built {len(docs)} section-chunks: {by_machine}")
    for d in docs:
        print(f"   {d.id:<28} [{d.metadata['machine_id']}] §={d.metadata['section']!r}")

    existing = {i.name for i in await client.list_indexes()}
    if index in existing:
        print(f"deleting existing '{index}'…")
        await client.delete_index(index)
    print(f"create_index('{index}', {len(docs)} docs, '{model}')…")
    await client.create_index(index, docs, model)
    for _ in range(45):
        st = str(getattr(await client.get_index(index), "status", "?"))
        if any(s in st.upper() for s in ("READY", "ACTIVE", "COMPLETE", "SUCCEED")):
            print(f"index READY ({st})")
            return
        if "FAIL" in st.upper():
            raise SystemExit(f"index build failed: {st}")
        await asyncio.sleep(2)
    print("warning: index not confirmed READY (continuing).")


if __name__ == "__main__":
    asyncio.run(main())
