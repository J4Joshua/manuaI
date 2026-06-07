#!/usr/bin/env python3
"""Retriever seam (ARCHITECTURE.md §3c) — Moss-backed semantic search.

    async def search(self, question, machine_id, k=5) -> list[record]
    class attr  threshold: float | None

A `record` = chunk metadata + `text` + `score` (NO vector). Records have IDENTICAL keys:
    id, score(float), text, machine_id, sop_id, section, procedure_title,
    doc_type, page(None), safety_flag(bool), fault_codes(str)

MossRetriever — Moss top-k hybrid query (alpha=0.8). threshold = None (Moss `.score` is
per-query normalized, so NO usable absolute gate exists — see ARCHITECTURE.md §12f / G15;
refusal comes from the LLM task-match few-shot in core.py, not a gate).
"""
import os
from pathlib import Path

import inferedge_moss as moss
from inferedge_moss import QueryOptions

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


def make_moss_retriever(client=None, index_name=None, alpha=None):
    """Construct a MossRetriever from .env (or explicit args)."""
    client = client or make_client()
    index_name = index_name or os.environ.get("MOSS_INDEX_NAME", "manuals")
    if alpha is None:
        alpha = float(os.environ.get("MOSS_ALPHA", "0.8"))
    return MossRetriever(client, index_name, alpha=alpha)


class MossRetriever:
    """Moss top-k hybrid query (alpha=0.8). Lets Moss embed (raw text in).
    load_index is the ONE network step — do it ONLINE, keep the process alive, then
    queries run locally even with wifi off (ARCHITECTURE.md §12a / G14)."""

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
                "page": None,
                "safety_flag": str(md.get("safety_flag")).lower() == "true",
                "fault_codes": md.get("fault_codes", ""),
            })
        return out
