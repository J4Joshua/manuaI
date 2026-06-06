"""M1 ingest — embed hand-authored SOP chunks LOCALLY and build a local index.

Builds a STUB index (index.json) that ask.py searches by cosine, so the M1 loop runs
with ZERO external services. Prove the loop first; THEN swap this for Moss (see the
MOSS SWAP POINT in README.md). Unsiloed is NOT used yet — that's Phase 4, off the
critical path. The same embedding model is used here and in ask.py (required).
"""
import json
import os

from common import embed

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(HERE, "chunks.json")) as f:
        chunks = json.load(f)

    index = []
    for c in chunks:
        c = dict(c)
        c["vector"] = embed(c["text"], kind="document")
        index.append(c)
        print(f"  embedded {c['id']:<14} dim={len(c['vector']):<4} {c['procedure_title']}")

    with open(os.path.join(HERE, "index.json"), "w") as f:
        json.dump(index, f)
    print(f"\n[ok] Built index.json ({len(index)} chunks).  Next: python3 ask.py \"...\"")


if __name__ == "__main__":
    main()
