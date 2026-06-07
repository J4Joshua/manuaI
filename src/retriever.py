#!/usr/bin/env python3
"""Retriever seam (ARCHITECTURE.md §3c) — Moss-backed semantic search.

    async def search(self, question, machine_id, k=5) -> list[record]
    class attr  threshold: float | None

A `record` = chunk metadata + `text` + `score` (NO vector). Records have IDENTICAL keys:
    id, score(float), text, machine_id, sop_id, section, procedure_title,
    doc_type, page(None), safety_flag(bool), fault_codes(str)

LocalMossRetriever — disk-backed index (data/moss_index.json), Moss-embedded locally.
Fully offline cold start. Cosine over moss-minilm vectors; threshold=None (G15).

MossRetriever — cloud load_index + in-process hybrid query. threshold=None.
"""
import asyncio
import json
import os
from pathlib import Path

import inferedge_moss as moss
from inferedge_moss import QueryOptions

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
                "`.venv/bin/python src/moss_embed_local.py` first."
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


class MossRetriever:
    """Cloud-backed Moss: load_index (network once) then local hybrid query."""

    threshold = None

    def __init__(self, client, index_name, alpha=0.8, model_id=None):
        self.client = client
        self.index = index_name
        self.alpha = alpha
        self.model_id = model_id or os.environ.get("MOSS_MODEL_ID", "moss-minilm")
        self._loaded = False

    async def ensure_loaded(self):
        if not self._loaded:
            await self.client.load_index(self.index)
            self._loaded = True

    async def search(self, question, machine_id, k=5):
        await self.ensure_loaded()
        flt = {"$and": [{"field": "machine_id", "condition": {"$eq": machine_id}}]}
        qv = await asyncio.to_thread(moss_embed.embed_text, question)
        sr = await self.client.query(
            self.index,
            question,
            QueryOptions(top_k=k, alpha=self.alpha, filter=flt, embedding=qv),
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
                "page": None,
                "safety_flag": str(md.get("safety_flag")).lower() == "true",
                "fault_codes": md.get("fault_codes", ""),
            })
        return out


def make_moss_retriever(client=None, index_name=None, alpha=None):
    """Construct a cloud MossRetriever from .env (or explicit args)."""
    client = client or make_client()
    index_name = index_name or os.environ.get("MOSS_INDEX_NAME", "manuals")
    if alpha is None:
        alpha = float(os.environ.get("MOSS_ALPHA", "0.8"))
    return MossRetriever(client, index_name, alpha=alpha)


def make_retriever(prefer_local=True):
    """Default factory: local disk index when present, else cloud MossRetriever."""
    load_env()
    if prefer_local and paths.MOSS_INDEX_JSON.exists():
        return LocalMossRetriever()
    return make_moss_retriever()
