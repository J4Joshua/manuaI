#!/usr/bin/env python3
"""Retriever seam (ARCHITECTURE.md §3c).

`MossRetriever.search(question, machine_id, k) -> list[record]` is the Moss
implementation of the seam. The offline-bulletproof CosineRetriever stub lives in
ask.py today; both return the same record shape so `answer()` is source-agnostic.

record = {id, score, text, machine_id, sop_id, section, title, doc_type,
          safety_flag(bool), fault_codes(str)}
"""
import os
from pathlib import Path

import inferedge_moss as moss
from inferedge_moss import QueryOptions

HERE = Path(__file__).resolve().parent


def load_env(path=".env"):
    p = HERE / path
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


class MossRetriever:
    def __init__(self, client, index_name, alpha=0.8):
        self.client = client
        self.index = index_name
        self.alpha = alpha
        self._loaded = False

    async def ensure_loaded(self):
        # The NETWORK step (~6s cloud fetch). Do it ONLINE, keep the process alive,
        # then queries below run locally even with wifi off (see ARCHITECTURE.md G14).
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
                "title": md.get("title"),
                "doc_type": md.get("doc_type"),
                "safety_flag": str(md.get("safety_flag")).lower() == "true",
                "fault_codes": md.get("fault_codes", ""),
            })
        return out
