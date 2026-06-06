#!/usr/bin/env python3
"""Build the offline stub index (index.json) for CosineRetriever.

Uses the shared corpus chunker (corpus.build_chunks) so the stub and the Moss index
hold IDENTICAL content + ids + metadata. Embeds each chunk's `text` LOCALLY with
nomic-embed-text (D6) and stores the 768-d vector alongside the metadata.

    .venv/bin/python src/ingest_local.py
"""
import json

import common
import corpus
import paths


def main():
    chunks = corpus.build_chunks()
    for c in chunks:
        c["vector"] = common.embed(c["text"], "document")

    out = paths.INDEX_JSON
    with open(out, "w") as f:
        json.dump(chunks, f)

    by = {}
    for c in chunks:
        by[c["machine_id"]] = by.get(c["machine_id"], 0) + 1
    print(f"wrote {out.name}: {len(chunks)} chunks")
    for mid, n in sorted(by.items()):
        print(f"   {mid:<16} {n}")


if __name__ == "__main__":
    main()
