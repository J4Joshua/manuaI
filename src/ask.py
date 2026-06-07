#!/usr/bin/env python3
"""Thin CLI shim over core.answer + render (ARCHITECTURE.md §6).

    .venv/bin/python src/ask.py "the labeler on line 3 jammed and shows error E-42"
    .venv/bin/python src/ask.py --machine cobot-cellA "robot in cell A shows fault C4"

Uses make_retriever() — offline Moss index at data/moss_index.json.
"""
import argparse
import asyncio
import os

import core
import render
from retriever import make_retriever


def main():
    ap = argparse.ArgumentParser(description="ManuAI — grounded, cited, refuses-or-escalates.")
    ap.add_argument("question")
    ap.add_argument("--machine", default=os.getenv("MACHINE_ID", "labeler-line3"))
    args = ap.parse_args()

    retriever = make_retriever()
    state = asyncio.run(core.answer(args.question, args.machine, retriever))
    render.render(state)


if __name__ == "__main__":
    main()
