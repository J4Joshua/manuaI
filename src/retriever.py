#!/usr/bin/env python3
"""Retriever seam (ARCHITECTURE.md §3c) — the Moss swap point.

Both retrievers expose the SAME async interface so `core.answer()` is source-agnostic:

    async def search(self, question, machine_id=None, k=5) -> list[record]

  machine_id is accepted for API compatibility but ignored — search spans the full index.
  Each record carries machine_id metadata for citations/display.
    class attr  threshold: float | None

A `record` = chunk metadata + `text` + `score` (NO vector). Records have IDENTICAL keys:
    id, score(float), text, machine_id, sop_id, section, procedure_title,
    doc_type, page(None), safety_flag(bool), fault_codes(str)

- LocalMossRetriever — offline index at data/moss_index.json (full corpus; machine_id
  on each chunk is metadata for citations, not a search filter). threshold=None (G15).
  Primary path for server/agent/offline_demo.
- CosineRetriever — legacy offline stub over index.json. threshold=0.30.
- MossRetriever — sponsor-tech cloud path (load_index online, query local). threshold=None.
"""
import asyncio
import json
import os
from pathlib import Path

import inferedge_moss as moss
import moss_embed
from inferedge_moss import QueryOptions

from common import cosine, embed

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


def make_client():
    load_env()
    pid, key = os.environ.get("MOSS_PROJECT_ID"), os.environ.get("MOSS_PROJECT_KEY")
    if not pid or not key:
        raise SystemExit("MOSS_PROJECT_ID / MOSS_PROJECT_KEY missing from .env")
    return moss.MossClient(pid, key)


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


class LocalMossRetriever:
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

    async def search(self, question, machine_id=None, k=5):
        qv = await asyncio.to_thread(moss_embed.embed_text, question, self.model_id)
        scored = []
        for c in self.index:
            rec = _chunk_to_record(c, moss_embed.cosine(qv, c["vector"]))
            scored.append(rec)
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]


class CosineRetriever:
    """Offline stub: cosine over a local index.json built by ingest_local.py."""

    threshold = 0.30

    def __init__(self, index_path=None):
        path = Path(index_path) if index_path else paths.INDEX_JSON
        if not path.exists():
            raise SystemExit(f"No {path.name} — run `.venv/bin/python src/ingest_local.py` first.")
        with open(path) as f:
            self.index = json.load(f)

    async def search(self, question, machine_id=None, k=5):
        qv = await asyncio.to_thread(embed, question, "query")
        scored = []
        for c in self.index:
            rec = {k2: v for k2, v in c.items() if k2 != "vector"}
            rec["score"] = float(cosine(qv, c["vector"]))
            scored.append(rec)
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]


class MossRetriever:
    """Sponsor-tech path: Moss top-k hybrid query (alpha=0.8). Lets Moss embed (raw
    text in). load_index is the ONE network step — do it ONLINE, keep the process
    alive, then queries run locally even with wifi off (ARCHITECTURE.md §12a / G14)."""

    threshold = None

    def __init__(self, client, index_name, alpha=0.8):
        self.client = client
        self.index = index_name
        self.alpha = alpha
        self._loaded = False

    async def ensure_loaded(self):
        if not self._loaded:
            await self.client.load_index(self.index)
            self._loaded = True

    async def search(self, question, machine_id=None, k=5):
        await self.ensure_loaded()
        sr = await self.client.query(
            self.index, question, QueryOptions(top_k=k, alpha=self.alpha)
        )
        out = []
        for d in sr.docs:
            md = d.metadata or {}
            out.append({
                "id": d.id,
                "score": float(d.score),
                "text": d.text,
                "machine_id": md.get("machine_id"),
                "sop_id": md.get("sop_id"),
                "section": md.get("section"),
                "procedure_title": md.get("procedure_title") or md.get("title"),
                "doc_type": md.get("doc_type"),
                "page": int(md["page"]) if str(md.get("page", "")).strip().isdigit() else None,
                "safety_flag": str(md.get("safety_flag")).lower() == "true",
                "fault_codes": md.get("fault_codes", ""),
            })
        return out


def make_retriever():
    """Construct the offline local Moss retriever (requires data/moss_index.json)."""
    load_env()
    return LocalMossRetriever()


def build_retriever(kind="local"):
    """CLI helper: local (default) | stub | moss."""
    load_env()
    if kind == "stub":
        return CosineRetriever()
    if kind == "moss":
        idx = os.getenv("MOSS_INDEX_NAME", "manuals")
        return MossRetriever(make_client(), idx, alpha=float(os.getenv("MOSS_ALPHA", "0.8")))
    return LocalMossRetriever()
