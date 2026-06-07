#!/usr/bin/env python3
"""Retriever seam (ARCHITECTURE.md §3c) — offline Moss-backed semantic search.

    async def search(self, question, machine_id, k=5) -> list[record]
    class attr  threshold: float | None

A `record` = chunk metadata + `text` + `score` (NO vector). Records have IDENTICAL keys:
    id, score(float), text, machine_id, sop_id, section, procedure_title,
    doc_type, page(None), safety_flag(bool), fault_codes(str)

MossRetriever — disk-backed index (data/moss_index.json), Moss-embedded locally via
PyEmbeddingService. Fully offline cold start. threshold=None (G15).
"""
import asyncio
import json
import os
from pathlib import Path

import moss_embed
import paths


def load_env(path=None):
    p = Path(path) if path else paths.ENV_FILE
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _chunk_to_record(c: dict, score: float) -> dict:
    return {
        "id": c["id"],
        "score": float(score),
        "text": c["text"],
        "machine_id": c["machine_id"],
        "sop_id": c["sop_id"],
        "section": c["section"],
        "procedure_title": c["procedure_title"],
        "doc_type": c["doc_type"],
        "page": c.get("page"),
        "safety_flag": bool(c["safety_flag"]),
        "fault_codes": c.get("fault_codes", ""),
    }


class MossRetriever:
    """Offline retriever: cosine over data/moss_index.json (Moss-embedded corpus)."""

    threshold = None

    def __init__(self, index_path=None, model_id=None):
        path = Path(index_path) if index_path else paths.MOSS_INDEX_JSON
        if not path.exists():
            raise SystemExit(
                f"No {path.relative_to(paths.REPO)} — run "
                "`.venv/bin/python src/moss_ingest.py` first."
            )
        with open(path) as f:
            data = json.load(f)
        self.model_id = model_id or data["model_id"]
        self.index = data["chunks"]

    async def search(self, question, machine_id, k=5):
        qv = await asyncio.to_thread(moss_embed.embed_text, question, self.model_id)
        cands = [c for c in self.index if c.get("machine_id") in (machine_id, "all")] or self.index
        scored = []
        for c in cands:
            rec = _chunk_to_record(c, moss_embed.cosine(qv, c["vector"]))
            scored.append(rec)
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]


def make_retriever():
    """Construct the offline Moss retriever (requires data/moss_index.json)."""
    load_env()
    return MossRetriever()
