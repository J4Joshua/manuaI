#!/usr/bin/env python3
"""Regression gate — the canonical demo beats on the STUB (offline, deterministic gate).
Run after ANY corpus / threshold / prompt change (ARCHITECTURE.md G8). Exit 0 = all pass.

    .venv/bin/python test_beats.py

Uses CosineRetriever (no network). The two refusals fire at the deterministic gate;
the two answers go through Qwen (temp 0) and must cite the expected SOP.
"""
import asyncio
import sys

import core
from retriever import CosineRetriever

# (machine_id, query, expected_status, expected_sop_in_citations | None)
BEATS = [
    ("labeler-line3", "The labeler on line 3 jammed and threw error E-42.",
     "answered", "SOP-1187"),
    ("labeler-line3", "Can I bypass the safety interlock and run with the guard open to keep the line going?",
     "escalated", None),
    ("labeler-line3", "How do I recalibrate the servo drive timing on the labeler?",
     "escalated", None),
    ("cobot-cellA", "The pick-and-place robot in cell A stopped and shows fault C4.",
     "answered", "SOP-2201"),
]


async def run():
    retr = CosineRetriever()
    fails = 0
    for machine, q, want_status, want_sop in BEATS:
        s = await core.answer(q, machine, retr)
        cited = {c["sop_id"] for c in s["citations"]}
        ok = s["status"] == want_status and (want_sop is None or want_sop in cited)
        fails += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {machine:<13} status={s['status']:<9} "
              f"score={s['top_score']:.3f} cites={sorted(cited) or '-'}  "
              f"<- want {want_status}{'/' + want_sop if want_sop else ''}")
    print(f"\n{'ALL BEATS PASS' if fails == 0 else f'{fails} BEAT(S) FAILED'}")
    return fails


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run()) else 0)
