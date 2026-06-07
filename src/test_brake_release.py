#!/usr/bin/env python3
"""Ad-hoc query test — runs ONE operator query through the real program
(core.answer → screen_state) and renders it, plus a structured summary.

    .venv/bin/python src/test_brake_release.py                 # stub, offline, default machine
    .venv/bin/python src/test_brake_release.py --machine labeler-line3
    .venv/bin/python src/test_brake_release.py --retriever moss --chats   # sponsor path (needs wifi)

The query asks about a "brake release" fault. The shipped corpus has NO approved
SOP for brake release (it covers jams, protective-stop recovery, and LOTO), so
the correct, safe behavior is to REFUSE + ESCALATE rather than improvise a
safety-critical procedure. This script makes that visible (and exits non-zero if
the program ever invents a citation for an off-corpus query — the cite-or-refuse
invariant, ARCHITECTURE.md §3b).
"""
import argparse
import asyncio
import os
import sys

import core
import render
from retriever import CosineRetriever, MossRetriever, make_client

QUERY = "the energy went out, how do i fix it"


def _build(kind):
    if kind == "moss":
        return MossRetriever(make_client(), os.getenv("MOSS_INDEX_NAME", "manuals"),
                             alpha=float(os.getenv("MOSS_ALPHA", "0.8")))
    return CosineRetriever()


async def run(machine, kind, with_chats):
    retriever = _build(kind)
    chat_retriever = (
        MossRetriever(make_client(), os.getenv("CHAT_INDEX_NAME", "chats"),
                      alpha=float(os.getenv("MOSS_ALPHA", "0.8")))
        if with_chats else None
    )

    state = await core.answer(QUERY, machine, retriever, chat_retriever=chat_retriever)

    # 1) the operator-facing screen, exactly as the UI would render it
    render.render(state)

    # 2) a structured summary + the invariant check
    cites = sorted(c["sop_id"] for c in state["citations"])
    print("-" * 60)
    print(f"  query     : {QUERY!r}")
    print(f"  machine   : {machine}")
    print(f"  retriever : {kind}{'  (+chats)' if with_chats else ''}")
    print(f"  status    : {state['status']}")
    print(f"  top_score : {state['top_score']}   threshold: {state['threshold']}")
    print(f"  citations : {cites or '-'}")
    if state["status"] == "escalated":
        print(f"  reason    : {state.get('answer', '')}")
    print("-" * 60)

    # cite-or-refuse invariant: an answered state MUST carry >=1 SOP citation;
    # an escalated state MUST carry none. No approved SOP for brake release means
    # we expect 'escalated'. Either way, the invariant below must hold.
    if state["status"] == "answered" and not cites:
        print("  ✗ INVARIANT VIOLATED: answered with no SOP citation")
        return 1
    if state["status"] == "escalated" and cites:
        print("  ✗ INVARIANT VIOLATED: escalated but emitted a citation")
        return 1
    print(f"  ✓ cite-or-refuse invariant holds ({state['status']})")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Run the brake-release query through ManuAI.")
    ap.add_argument("--machine", default=os.getenv("MACHINE_ID", "cobot-cellA"))
    ap.add_argument("--retriever", choices=("stub", "moss"), default="stub")
    ap.add_argument("--chats", action="store_true",
                    help="also query the operator-chat index (needs wifi/load)")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args.machine, args.retriever, args.chats)))


if __name__ == "__main__":
    main()
