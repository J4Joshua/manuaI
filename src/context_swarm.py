#!/usr/bin/env python3
"""Async Moss context swarm — background SOP prefetch for a session.

After the first answer(), seeds from the primary Moss hits, then loops:
  LLM proposes follow-up Moss queries → search → dedupe → append to bubble.

Swarm chunks are merged into subsequent core.answer() retrieval (still cite-or-refuse
over the union of primary hits + prefetched chunks).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from typing import Callable

from common import chat_json

logger = logging.getLogger(__name__)

MAX_DEPTH = 3
QUERIES_PER_ROUND = 3
MAX_CHUNKS = 18
ROUND_PAUSE_SECS = 1.5

SWARM_SYSTEM = """You are a factory SOP research assistant. Given accumulated context
from prior Moss semantic searches, propose NEW search queries to retrieve related
safety procedures, lockout/tagout steps, fault-code references, and cross-referenced
SOPs the operator may need next on this machine.

Return ONLY a JSON object with exactly these keys:
  "queries": array of 1-3 short natural-language search strings,
  "done": boolean — true when no useful new queries remain

Rules:
- Do NOT repeat queries already listed under "Prior queries".
- Focus on gaps: LOTO, energy isolation, related fault codes, prerequisite procedures.
- Keep each query under 15 words."""


def swarm_enabled() -> bool:
    return os.environ.get("CONTEXT_SWARM", "1").strip().lower() not in ("0", "false", "no")


def empty_bubble() -> dict:
    return {"status": "idle", "lines": [], "updates": [], "chunk_count": 0}


def with_bubble(state: dict, swarm: "ContextSwarm | None") -> dict:
    out = dict(state)
    if swarm and swarm.enabled:
        out["context_bubble"] = swarm.snapshot()
    else:
        out["context_bubble"] = empty_bubble()
    return out


class AsyncRunner:
    """Persistent asyncio loop in a daemon thread for sync HTTP / voice callers."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="context-swarm-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def spawn(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self._loop)


class ContextSwarm:
    """Session-scoped Moss prefetch store (full corpus — no machine filter)."""

    def __init__(self, retriever, on_update: Callable[[dict], None] | None = None):
        self.retriever = retriever
        self.enabled = swarm_enabled()
        self._on_update = on_update
        self._chunks: dict[str, dict] = {}
        self._lines: list[dict] = []
        self._updates: list[dict] = []
        self._status = "idle"
        self._lock = asyncio.Lock()
        self._depth = 0
        self._swarm_started = False
        self._prior_queries: list[str] = []
        # Yield-to-foreground gate: Ollama is single-stream, so a background swarm
        # chat_json mid-flight queues the operator's next answer (~+2 s). core.answer
        # marks the foreground busy; the swarm awaits idle before each LLM/search call.
        self._fg_active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    def foreground_begin(self) -> None:
        """Called by core.answer (on the bg loop) when a real answer starts."""
        self._fg_active += 1
        self._idle.clear()

    def foreground_end(self) -> None:
        self._fg_active = max(0, self._fg_active - 1)
        if self._fg_active == 0:
            self._idle.set()

    async def _await_foreground_idle(self) -> None:
        """Pause the background swarm while a foreground answer is in flight."""
        if not self._idle.is_set():
            await self._idle.wait()

    def snapshot(self) -> dict:
        return {
            "status": self._status,
            "lines": list(self._lines),
            "updates": list(self._updates[-8:]),
            "chunk_count": len(self._chunks),
        }

    def get_hits(self) -> list[dict]:
        return list(self._chunks.values())

    def set_on_update(self, cb: Callable[[dict], None] | None) -> None:
        self._on_update = cb

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update(self.snapshot())
            except Exception:
                logger.exception("context_swarm on_update callback failed")

    def _append_update(
        self,
        *,
        added_lines: list[dict],
        source: str,
        query: str | None,
    ) -> None:
        n = len(added_lines)
        chunk_ids = [ln["chunk_id"] for ln in added_lines]
        preview = ", ".join(chunk_ids[:3])
        if len(chunk_ids) > 3:
            preview += f", +{len(chunk_ids) - 3} more"
        summary = f"+{n} {'chunk' if n == 1 else 'chunks'} added" if n else "No new chunks added"
        update = {
            "summary": summary,
            "chunk_ids": chunk_ids,
            "preview": preview,
            "source": source,
        }
        if query:
            update["query"] = query
        self._updates.append(update)
        self._updates = self._updates[-20:]

    async def after_answer(self, question: str, primary_hits: list[dict]) -> None:
        if not self.enabled or not primary_hits:
            return
        await self._ingest_hits(primary_hits, "seed", question)
        if not self._swarm_started:
            self._swarm_started = True
            asyncio.create_task(self._swarm_loop())

    async def _ingest_hits(self, hits: list[dict], source: str, query: str | None = None) -> bool:
        added = False
        added_lines: list[dict] = []
        async with self._lock:
            for h in hits:
                cid = h["id"]
                if cid in self._chunks:
                    continue
                self._chunks[cid] = h
                line = {
                    "text": f"{h['procedure_title']} — {h['section']}",
                    "sop_id": h["sop_id"],
                    "chunk_id": cid,
                    "score": round(float(h["score"]), 3),
                    "source": source,
                }
                if query:
                    line["query"] = query
                self._lines.append(line)
                added_lines.append(line)
                added = True
            if added:
                self._status = "gathering"
                self._append_update(added_lines=added_lines, source=source, query=query)
        if added:
            self._notify()
        return added

    async def refresh(self, question: str | None = None) -> dict:
        """Force one foreground swarm pass and return the latest UI snapshot."""
        if not self.enabled:
            return empty_bubble()

        async with self._lock:
            self._status = "refreshing"
        self._notify()

        before = len(self._chunks)
        if question and len(self._chunks) < MAX_CHUNKS:
            hits = await self.retriever.search(question, k=5)
            await self._ingest_hits(hits, "refresh", question)

        if len(self._chunks) < MAX_CHUNKS:
            queries = await self._propose_queries()
            for q in queries:
                if q in self._prior_queries:
                    continue
                self._prior_queries.append(q)
                hits = await self.retriever.search(q, k=3)
                await self._ingest_hits(hits, "refresh", q)

        async with self._lock:
            if len(self._chunks) == before:
                self._append_update(added_lines=[], source="refresh", query=question)
            self._status = "ready"
            snap = self.snapshot()
        self._notify()
        return snap

    def _format_context_doc(self) -> str:
        parts = []
        for ln in self._lines[-12:]:
            parts.append(f"- [{ln['sop_id']}] {ln['text']} (score {ln['score']})")
        return "\n".join(parts) or "(empty)"

    async def _propose_queries(self) -> list[str]:
        if len(self._chunks) >= MAX_CHUNKS:
            return []
        prior = "\n".join(f"- {q}" for q in self._prior_queries) or "(none)"
        user = (
            f"Accumulated context:\n{self._format_context_doc()}\n\n"
            f"Prior queries:\n{prior}"
        )
        raw = await asyncio.to_thread(chat_json, SWARM_SYSTEM, user)
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("context_swarm: bad JSON from query proposer")
            return []
        if out.get("done"):
            return []
        queries = [str(q).strip() for q in out.get("queries", []) if str(q).strip()]
        return queries[:QUERIES_PER_ROUND]

    async def _swarm_loop(self) -> None:
        try:
            while self._depth < MAX_DEPTH and len(self._chunks) < MAX_CHUNKS:
                await self._await_foreground_idle()  # never compete with a live answer
                self._depth += 1
                queries = await self._propose_queries()
                if not queries:
                    break
                for q in queries:
                    await self._await_foreground_idle()
                    self._prior_queries.append(q)
                    hits = await self.retriever.search(q, k=3)
                    await self._ingest_hits(hits, "swarm", q)
                await asyncio.sleep(ROUND_PAUSE_SECS)
            self._status = "ready"
            self._notify()
        except Exception:
            logger.exception("context_swarm loop failed")
            self._status = "ready"
            self._notify()


_swarm: ContextSwarm | None = None
_bg_runner: AsyncRunner | None = None


def get_bg_runner() -> AsyncRunner:
    global _bg_runner
    if _bg_runner is None:
        _bg_runner = AsyncRunner()
    return _bg_runner


def get_swarm(
    retriever,
    on_update: Callable[[dict], None] | None = None,
) -> ContextSwarm | None:
    global _swarm
    if not swarm_enabled():
        return None
    if _swarm is None:
        _swarm = ContextSwarm(retriever, on_update)
    elif on_update:
        _swarm.set_on_update(on_update)
    return _swarm


def live_bubble_snapshot(_machine_id: str | None = None) -> dict:
    """Latest bubble for /state polls (may be newer than the last answer payload)."""
    if _swarm is not None:
        return _swarm.snapshot()
    return empty_bubble()
