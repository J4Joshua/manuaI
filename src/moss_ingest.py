#!/usr/bin/env python3
"""Build the Moss cloud index from the shared corpus chunker (corpus.build_chunks).

Embeds locally with Moss's on-device embedder, then uploads precomputed vectors
(model_id=custom) so cloud and local indexes stay in parity.

Online — create_index builds in the cloud (ARCHITECTURE.md §12a). Run with wifi ON:
    .venv/bin/python src/moss_ingest.py

Also run moss_embed_local.py to refresh data/moss_index.json for offline retrieval.
"""
import asyncio
import os

import corpus
import moss_corpus
import moss_embed
from retriever import load_env, make_client


async def main():
    load_env()
    client = make_client()
    index = os.getenv("MOSS_INDEX_NAME", "manuals")

    chunks = corpus.build_chunks()
    texts = [c["text"] for c in chunks]
    mid = moss_embed.model_id()
    print(f"embedding {len(texts)} chunks locally with {mid}…")
    vectors = moss_embed.embed_texts(texts, mid)
    docs = [
        moss_corpus.chunk_to_doc_info(c, vec)
        for c, vec in zip(chunks, vectors)
    ]

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
    print(f"create_index('{index}', {len(docs)} docs, 'custom')…")
    await client.create_index(index, docs, "custom")
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
