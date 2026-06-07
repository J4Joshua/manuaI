#!/usr/bin/env python3
"""Build the Moss-embedded local corpus index (data/moss_index.json).

Embeds section-chunks from data/machines/*/sops/*.md with Moss's on-device
embedder (PyEmbeddingService / moss-minilm by default). Fully offline — no Moss
cloud calls. Run after adding or editing SOPs:

    .venv/bin/python src/moss_ingest.py

The output is consumed by MossRetriever (retriever.py) for cold-start retrieval.
"""
import json
from pathlib import Path

import corpus
import moss_embed
import paths
from retriever import load_env


def embed_and_write(chunks: list[dict]) -> Path:
    """Embed chunks with Moss locally and write data/moss_index.json."""
    load_env()
    mid = moss_embed.model_id()
    texts = [c["text"] for c in chunks]
    print(f"embedding {len(texts)} chunks with {mid} (local, offline)…")
    vectors = moss_embed.embed_texts(texts, mid)
    dim = len(vectors[0]) if vectors else moss_embed.embed_dim(mid)

    records = []
    for c, vec in zip(chunks, vectors):
        rec = {k: v for k, v in c.items()}
        rec["vector"] = vec
        records.append(rec)

    out = paths.MOSS_INDEX_JSON
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model_id": mid, "embed_dim": dim, "chunks": records}
    with open(out, "w") as f:
        json.dump(payload, f)

    by = {}
    for c in records:
        by[c["machine_id"]] = by.get(c["machine_id"], 0) + 1
    print(f"wrote {out.relative_to(paths.REPO)}: {len(records)} chunks  model={mid} dim={dim}")
    for machine, n in sorted(by.items()):
        print(f"   {machine:<16} {n}")
    return out


def main():
    embed_and_write(corpus.build_chunks())


if __name__ == "__main__":
    main()
