#!/usr/bin/env python3
"""Retriever seam (ARCHITECTURE.md §3c) — the Moss swap point.

Both retrievers expose the SAME async interface so `core.answer()` is source-agnostic:

    async def search(self, question, machine_id, k=5) -> list[record]
    class attr  threshold: float | None

A `record` = chunk metadata + `text` + `score` (NO vector). Both retrievers return
records with IDENTICAL keys:
    id, score(float), text, machine_id, sop_id, section, procedure_title,
    doc_type, page(None), safety_flag(bool), fault_codes(str)

- CosineRetriever — the offline-bulletproof stub. threshold = 0.70 (raw nomic cosine
  is non-normalized, so an absolute gate is meaningful). Loads index.json once.
- MossRetriever  — the sponsor-tech path. threshold = None (Moss `.score` is per-query
  normalized, so NO usable absolute gate exists — see ARCHITECTURE.md §12f / G15;
  refusal on the Moss path comes from the LLM task-match few-shot, not a gate).
"""
import asyncio
import json
import os
from pathlib import Path

import inferedge_moss as moss
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


class CosineRetriever:
    """Offline stub: cosine over a local index.json built by ingest_local.py.

    Cold-loads from disk with zero network, ever — the bulletproof-offline path and
    the backup-video engine (ARCHITECTURE.md §12a)."""

    threshold = 0.70

    def __init__(self, index_path=None):
        path = Path(index_path) if index_path else paths.INDEX_JSON
        if not path.exists():
            raise SystemExit(f"No {path.name} — run `.venv/bin/python src/ingest_local.py` first.")
        with open(path) as f:
            self.index = json.load(f)

    async def search(self, question, machine_id, k=5):
        # Embed the query LOCALLY (nomic) off the event loop — the sync urllib call
        # would otherwise block LiveKit's async loop (G9).
        qv = await asyncio.to_thread(embed, question, "query")
        # metadata filter: this machine's docs + global ("all") policies; fall back to
        # the whole index only if the filter is empty (G10 — fine at this corpus size).
        cands = [c for c in self.index if c.get("machine_id") in (machine_id, "all")] or self.index
        scored = []
        for c in cands:
            rec = {k2: v for k2, v in c.items() if k2 != "vector"}
            rec["score"] = float(cosine(qv, c["vector"]))
            scored.append(rec)
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]


class MossRetriever:
    """Sponsor-tech path: Moss top-k hybrid query (alpha=0.8). Lets Moss embed (raw
    text in). load_index is the ONE network step — do it ONLINE, keep the process
    alive, then queries run locally even with wifi off (ARCHITECTURE.md §12a / G14)."""

    threshold = None  # Moss .score is per-query normalized — no usable absolute gate (G15)

    def __init__(self, client, index_name, alpha=0.8):
        self.client = client
        self.index = index_name
        self.alpha = alpha
        self._loaded = False

    async def ensure_loaded(self):
        if not self._loaded:
            await self.client.load_index(self.index)
            self._loaded = True

    async def search(self, question, machine_id, k=5):
        await self.ensure_loaded()
        flt = {"$and": [{"field": "machine_id", "condition": {"$eq": machine_id}}]}
        sr = await self.client.query(
            self.index, question, QueryOptions(top_k=k, alpha=self.alpha, filter=flt)
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
                # page is stored at Unsiloed-ingest time (str, "" when unknown); parse
                # it back to int|None so citations can surface "p.12" (Phase 4 DoD).
                "page": int(md["page"]) if str(md.get("page", "")).strip().isdigit() else None,
                "safety_flag": str(md.get("safety_flag")).lower() == "true",
                "fault_codes": md.get("fault_codes", ""),
            })
        return out
