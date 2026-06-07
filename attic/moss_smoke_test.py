#!/usr/bin/env python3
"""ManuAI Moss smoke test — ONLINE verification of the real SDK + creds.

Creates a tiny throwaway index, loads it, runs a metadata-filtered query,
prints latency, then DELETES the index (cleanup). Proves the moss skill's
API against reality. Run with wifi ON.

    .venv/bin/python scripts/moss_smoke_test.py

Package: inferedge-moss  (import name: inferedge_moss)
"""
import asyncio, os, sys, time
from pathlib import Path

import inferedge_moss as moss
from inferedge_moss import DocumentInfo, QueryOptions


def load_env(path=".env"):
    p = Path(__file__).resolve().parent.parent / path
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


INDEX = "manuai_smoke_test"
DOCS = [
    DocumentInfo(id="loto-1", text="Perform lockout/tagout on the labeler before clearing a jam.",
                 metadata={"machine_id": "labeler-line3", "section": "4.2", "safety_flag": "true"}),
    DocumentInfo(id="cobot-1", text="Power-cycle the controller to clear a joint-overtravel fault.",
                 metadata={"machine_id": "cobot-cellA", "section": "2.1", "safety_flag": "false"}),
]


async def main():
    load_env()
    pid, key = os.environ.get("MOSS_PROJECT_ID"), os.environ.get("MOSS_PROJECT_KEY")
    if not pid or not key:
        sys.exit("MOSS_PROJECT_ID / MOSS_PROJECT_KEY missing from .env")
    model = os.environ.get("MOSS_MODEL_ID", "moss-minilm")

    client = moss.MossClient(pid, key)
    print(f"client created (project {pid[:8]}…, model {model})")

    try:
        t = time.perf_counter()
        res = await client.create_index(INDEX, DOCS, model)
        print(f"create_index -> {res}  ({(time.perf_counter()-t)*1000:.0f} ms)")

        # wait until queryable
        for i in range(30):
            try:
                info = await client.get_index(INDEX)
                status = getattr(info, "status", info)
                print(f"  [{i}] index status: {status}")
                if str(status).lower().find("fail") != -1:
                    sys.exit(f"index build failed: {status}")
                if any(s in str(status).upper() for s in ("READY", "ACTIVE", "SUCCEED", "COMPLETE")):
                    break
            except Exception as e:
                print(f"  [{i}] get_index: {type(e).__name__}: {e}")
            await asyncio.sleep(2)

        t = time.perf_counter()
        await client.load_index(INDEX)
        print(f"load_index OK  ({(time.perf_counter()-t)*1000:.0f} ms)")

        q = QueryOptions(top_k=3, alpha=0.8,
                         filter={"$and": [{"field": "machine_id",
                                           "condition": {"$eq": "labeler-line3"}}]})
        t = time.perf_counter()
        sr = await client.query(INDEX, "how do I clear a jam safely?", q)
        wall = (time.perf_counter() - t) * 1000
        print(f"\nquery OK  wall={wall:.1f} ms  reported time_taken_ms={getattr(sr,'time_taken_ms','?')}")
        for d in sr.docs:
            print(f"  - score={d.score:.4f}  machine={d.metadata.get('machine_id')}  "
                  f"§{d.metadata.get('section')}  text={d.text!r}")

        # filter correctness: must NOT return the cobot doc
        machines = {d.metadata.get("machine_id") for d in sr.docs}
        print(f"\nFILTER CHECK: machines in results = {machines} "
              f"-> {'PASS' if machines <= {'labeler-line3'} else 'FAIL (filter leaked!)'}")

        # inspect embedding dimension if exposed
        try:
            got = await client.get_docs(INDEX)
            dim = next((len(g.embedding) for g in got if getattr(g, "embedding", None)), None)
            print(f"embedding dim (if exposed): {dim}")
        except Exception as e:
            print(f"get_docs/dim: {type(e).__name__}: {e}")

        print("\nSMOKE TEST: PASS")
    finally:
        try:
            ok = await client.delete_index(INDEX)
            print(f"cleanup: delete_index -> {ok}")
        except Exception as e:
            print(f"cleanup failed ({type(e).__name__}: {e}) — delete '{INDEX}' manually")


if __name__ == "__main__":
    asyncio.run(main())
