#!/usr/bin/env python3
"""Build the Moss-embedded local corpus index (data/moss_index.json).

Embeds section-chunks from data/machines/*/sops/*.md with Moss's on-device
embedder (PyEmbeddingService / moss-minilm by default). Fully offline — no Moss
cloud calls. Run after adding or editing SOPs:

    .venv/bin/python src/moss_ingest.py

Pull already-ingested Unsiloed chunks from the cloud Moss index (wifi-on, once):

    .venv/bin/python src/moss_ingest.py --from-cloud

Or rebuild from a saved chunk JSON (e.g. data/unsiloed_chunks.json):

    .venv/bin/python src/moss_ingest.py --chunks data/unsiloed_chunks.json

The output is consumed by LocalMossRetriever (retriever.py) for cold-start retrieval.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import corpus
import moss_embed
import paths
from retriever import load_env, make_client


def embed_and_write(chunks: list[dict], batch_size: int = 128) -> Path:
    """Embed chunks with Moss locally and write data/moss_index.json."""
    load_env()
    mid = moss_embed.model_id()
    print(f"embedding {len(chunks)} chunks with {mid} (local, offline)…")

    records = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        texts = [c["text"] for c in batch]
        vectors = moss_embed.embed_texts(texts, mid)
        for c, vec in zip(batch, vectors):
            rec = {k: v for k, v in c.items()}
            rec["vector"] = vec
            records.append(rec)
        done = min(start + batch_size, len(chunks))
        print(f"  … {done}/{len(chunks)}")
    dim = len(records[0]["vector"]) if records else moss_embed.embed_dim(mid)

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


def _doc_to_chunk(doc) -> dict:
    """Cloud Moss DocumentInfo → local moss_index chunk record (no vector yet)."""
    md = doc.metadata or {}
    page_raw = md.get("page", "")
    page = int(page_raw) if str(page_raw).strip().isdigit() else None
    machine_id = md.get("machine_id") or "all"
    if machine_id in ("None", "null"):
        machine_id = "all"
    sf = str(md.get("safety_flag", "false")).lower() == "true"
    return {
        "id": doc.id,
        "sop_id": md.get("sop_id") or doc.id,
        "section": md.get("section") or "Overview",
        "machine_id": machine_id,
        "doc_type": md.get("doc_type") or "manual",
        "procedure_title": md.get("procedure_title") or doc.id,
        "safety_flag": sf,
        "fault_codes": md.get("fault_codes") or "",
        "page": page,
        "text": doc.text or "",
    }


async def fetch_cloud_chunks(index_name: str | None = None) -> list[dict]:
    """Export all documents from the cloud Moss index (post-unsiloed_ingest)."""
    load_env()
    index = index_name or os.getenv("MOSS_INDEX_NAME", "manuals")
    client = make_client()
    names = {i.name for i in await client.list_indexes()}
    if index not in names:
        raise SystemExit(f"Moss index {index!r} not found (have: {sorted(names)})")
    docs = await client.get_docs(index)
    return [_doc_to_chunk(d) for d in docs]


def load_chunks_json(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "chunks" in data:
        return data["chunks"]
    if isinstance(data, list):
        return data
    raise SystemExit(f"{path}: expected a chunk list or {{chunks: [...]}}")


def main():
    ap = argparse.ArgumentParser(description="Build data/moss_index.json (local Moss embedder).")
    ap.add_argument("--from-cloud", action="store_true",
                    help="export chunks from cloud Moss MOSS_INDEX_NAME (wifi-on) and embed locally")
    ap.add_argument("--chunks", metavar="FILE",
                    help="embed chunks from a JSON file (list or {chunks:[...]}) instead of SOP .md")
    ap.add_argument("--save-chunks", metavar="FILE",
                    help="when using --from-cloud, also write the exported chunks to FILE")
    ap.add_argument("--index", help="cloud Moss index name (default: MOSS_INDEX_NAME or 'manuals')")
    args = ap.parse_args()

    if args.from_cloud:
        chunks = asyncio.run(fetch_cloud_chunks(args.index))
        if args.save_chunks:
            out = Path(args.save_chunks)
            if not out.is_absolute():
                out = paths.REPO / out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(chunks, indent=2))
            print(f"saved {len(chunks)} chunks → {out.relative_to(paths.REPO)}")
    elif args.chunks:
        p = Path(args.chunks)
        if not p.is_absolute():
            p = paths.REPO / p
        chunks = load_chunks_json(p)
    else:
        chunks = corpus.build_chunks()

    embed_and_write(chunks)


if __name__ == "__main__":
    main()
