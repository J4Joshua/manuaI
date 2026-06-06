#!/usr/bin/env python3
"""Build the REAL Moss index from the shared corpus chunker (corpus.build_chunks),
so the stub (index.json) and Moss hold IDENTICAL content + ids + metadata.

Online — create_index builds in the cloud (ARCHITECTURE.md §12a). Run with wifi ON:
    .venv/bin/python moss_ingest.py

Lets Moss embed (raw text in, model moss-minilm) per §12b. All metadata VALUES must be
strings (Moss requirement) — safety_flag is "true"/"false", page is omitted (None).
PDFs under data/.../manuals/ are Phase 4 (Unsiloed) — not ingested here.
"""
import asyncio
import os
from pathlib import Path

from inferedge_moss import DocumentInfo

import corpus
from retriever import load_env, make_client

HERE = Path(__file__).resolve().parent


def build_docs():
    docs = []
    for c in corpus.build_chunks():
        docs.append(DocumentInfo(
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
            },
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
