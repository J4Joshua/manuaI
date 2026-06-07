#!/usr/bin/env python3
"""Thin CLI shim over core.answer + render (ARCHITECTURE.md §6).

    .venv/bin/python src/ask.py "the labeler on line 3 jammed and shows error E-42"
    .venv/bin/python src/ask.py --machine cobot-cellA "robot in cell A shows fault C4"
    .venv/bin/python src/ask.py --retriever moss "..."   # sponsor-tech path (needs wifi to load)

stub  (default) → CosineRetriever over index.json  — bulletproof-offline.
moss            → MossClient + MossRetriever        — load_index is the one network step.
"""
import argparse
import asyncio
import os

import core
import render
from retriever import CosineRetriever, MossRetriever, make_client


def build_retriever(kind):
    if kind == "moss":
        client = make_client()
        index = os.getenv("MOSS_INDEX_NAME", "manuals")
        alpha = float(os.getenv("MOSS_ALPHA", "0.8"))
        return MossRetriever(client, index, alpha=alpha)
    return CosineRetriever()


def build_chat_retriever():
    """Secondary operator-chat retriever (Moss `chats` index). Supplemental: corroborates
    / guides the SOP-grounded answer, never cites, never flips a refusal."""
    index = os.getenv("CHAT_INDEX_NAME", "chats")
    return MossRetriever(make_client(), index, alpha=float(os.getenv("MOSS_ALPHA", "0.8")))


def main():
    ap = argparse.ArgumentParser(description="ManuAI — grounded, cited, refuses-or-escalates.")
    ap.add_argument("question")
    ap.add_argument("--machine", default=os.getenv("MACHINE_ID", "labeler-line3"))
    ap.add_argument("--retriever", choices=("stub", "moss"), default="stub")
    ap.add_argument("--chats", action="store_true",
                    help="also query the operator-chat `chats` Moss index for corroboration (needs wifi/load)")
    args = ap.parse_args()

    retriever = build_retriever(args.retriever)
    chat_retriever = build_chat_retriever() if args.chats else None
    state = asyncio.run(core.answer(args.question, args.machine, retriever, chat_retriever=chat_retriever))
    render.render(state)


if __name__ == "__main__":
    main()
