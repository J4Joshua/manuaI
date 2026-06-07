#!/usr/bin/env python3
"""ManuAI Moss OFFLINE test — run on the actual demo MacBook.

Proves the wifi-off demo path and the cold-start limitation.

Usage:
  # 1) Normal run (wifi ON). Builds+loads the index, then asks you to kill wifi
  #    and confirms a query still works offline. Leaves the index in place.
  .venv/bin/python scripts/moss_offline_test.py

  # 2) Cold-load test: with wifi STILL OFF, re-run with --coldload. It skips
  #    create and tries load_index offline. Expected to FAIL — proving you must
  #    load_index while online and keep the process alive for the demo.
  .venv/bin/python scripts/moss_offline_test.py --coldload

Package: inferedge-moss  (import name: inferedge_moss)
"""
import asyncio, os, sys, time
from pathlib import Path

import inferedge_moss as moss
from inferedge_moss import DocumentInfo, QueryOptions

INDEX = "manuai_offline_demo"
DOCS = [
    DocumentInfo(id="loto-1", text="Perform lockout/tagout on the labeler before clearing a jam.",
                 metadata={"machine_id": "labeler-line3", "section": "4.2", "safety_flag": "true"}),
    DocumentInfo(id="cobot-1", text="Power-cycle the controller to clear a joint-overtravel fault.",
                 metadata={"machine_id": "cobot-cellA", "section": "2.1", "safety_flag": "false"}),
]
FILTER = {"$and": [{"field": "machine_id", "condition": {"$eq": "labeler-line3"}}]}


def load_env(path=".env"):
    p = Path(__file__).resolve().parent.parent / path
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def client():
    load_env()
    pid, key = os.environ.get("MOSS_PROJECT_ID"), os.environ.get("MOSS_PROJECT_KEY")
    if not pid or not key:
        sys.exit("MOSS_PROJECT_ID / MOSS_PROJECT_KEY missing from .env")
    return moss.MossClient(pid, key), os.environ.get("MOSS_MODEL_ID", "moss-minilm")


async def query_once(c, label):
    t = time.perf_counter()
    sr = await c.query(INDEX, "how do I clear a jam safely?", QueryOptions(top_k=3, alpha=0.8, filter=FILTER))
    ms = (time.perf_counter() - t) * 1000
    hits = [f"{d.metadata.get('machine_id')} §{d.metadata.get('section')} score={d.score:.3f}" for d in sr.docs]
    print(f"  {label}: {ms:.1f} ms  hits={hits}")
    return sr


async def coldload():
    c, _ = client()
    print("COLD-LOAD TEST (expecting wifi OFF)…")
    try:
        t = time.perf_counter()
        await c.load_index(INDEX)
        print(f"  load_index SUCCEEDED offline in {(time.perf_counter()-t)*1000:.0f} ms "
              f"-> Moss can cold-load offline! (re-confirm with Moss team)")
        await query_once(c, "offline query after cold-load")
    except Exception as e:
        print(f"  load_index FAILED offline: {type(e).__name__}: {e}")
        print("  => EXPECTED. You must load_index while ONLINE and keep the process alive.")


async def main():
    c, model = client()
    print("== ONLINE phase ==")
    if INDEX not in {i.name for i in await c.list_indexes()}:
        await c.create_index(INDEX, DOCS, model)
        print(f"created index '{INDEX}'")
    t = time.perf_counter()
    await c.load_index(INDEX)
    print(f"load_index OK in {(time.perf_counter()-t)*1000:.0f} ms (this is the network-bound step)")
    await query_once(c, "online query")

    input("\n✋ TURN WIFI OFF now, then press Enter to query offline… ")

    print("\n== OFFLINE phase (process still alive) ==")
    await query_once(c, "OFFLINE query #1")
    await query_once(c, "OFFLINE query #2")
    print("\n✅ If both offline queries returned hits, the demo path works: "
          "load online -> keep process alive -> query offline.")
    print(f"   Index '{INDEX}' left in place. Keep wifi OFF and run with --coldload "
          f"to test whether a fresh process can load it offline.")


if __name__ == "__main__":
    asyncio.run(coldload() if "--coldload" in sys.argv else main())
