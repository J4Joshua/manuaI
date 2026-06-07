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
import time
from datetime import datetime, timezone
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
    return {"status": "idle", "lines": [], "updates": [], "queries": [], "chunk_count": 0}


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


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
        self._queries: list[dict] = []
        self._query_seq = 0
        self._swarm_task: asyncio.Task | None = None
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
            "queries": list(self._queries[-20:]),
            "chunk_count": len(self._chunks),
        }

    def get_hits(self) -> list[dict]:
        return list(self._chunks.values())

    def set_on_update(self, cb: Callable[[dict], None] | None) -> None:
        self._on_update = cb

    async def reset_for_question(self) -> None:
        """Clear session bubble state so each operator prompt starts fresh."""
        async with self._lock:
            if self._swarm_task and not self._swarm_task.done():
                self._swarm_task.cancel()
            self._swarm_task = None
            self._chunks.clear()
            self._lines.clear()
            self._updates.clear()
            self._queries.clear()
            self._prior_queries.clear()
            self._status = "idle"
            self._depth = 0
            self._swarm_started = False
            self._query_seq = 0
        self._notify()

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
        query_id: str | None = None,
        duration_ms: int | None = None,
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
        if query_id:
            update["query_id"] = query_id
        if duration_ms is not None:
            update["duration_ms"] = duration_ms
        self._updates.append(update)
        self._updates = self._updates[-20:]

    def _start_query(self, query: str | None, source: str) -> str:
        qid = f"q{self._query_seq}"
        self._query_seq += 1
        self._queries.append(
            {
                "id": qid,
                "query": (query or "").strip(),
                "source": source,
                "status": "running",
                "started_at": _iso_now(),
                "finished_at": None,
                "duration_ms": None,
                "chunk_ids": [],
                "chunks_added": 0,
            }
        )
        return qid

    def _finish_query(
        self,
        query_id: str,
        *,
        added_lines: list[dict],
        duration_ms: int,
    ) -> None:
        for q in self._queries:
            if q["id"] != query_id:
                continue
            q["status"] = "done"
            q["finished_at"] = _iso_now()
            q["duration_ms"] = duration_ms
            q["chunk_ids"] = [ln["chunk_id"] for ln in added_lines]
            q["chunks_added"] = len(added_lines)
            break

    async def _execute_retrieval(
        self,
        query: str | None,
        source: str,
        *,
        k: int = 3,
        hits: list[dict] | None = None,
    ) -> bool:
        """Run one Moss query with live bubble pushes: running → chunks → done + timing."""
        qtext = (query or "").strip()
        t0 = time.perf_counter()

        async with self._lock:
            if source == "refresh" and self._status == "refreshing":
                pass
            else:
                self._status = "gathering"
            query_id = self._start_query(qtext, source)
        self._notify()

        if hits is None:
            hits = await self.retriever.search(qtext, k=k) if qtext else []

        added_lines: list[dict] = []
        added = False
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
                if qtext:
                    line["query"] = qtext
                self._lines.append(line)
                added_lines.append(line)
                added = True
            duration_ms = max(0, round((time.perf_counter() - t0) * 1000))
            self._finish_query(query_id, added_lines=added_lines, duration_ms=duration_ms)
            if added:
                self._append_update(
                    added_lines=added_lines,
                    source=source,
                    query=qtext or None,
                    query_id=query_id,
                    duration_ms=duration_ms,
                )
        self._notify()
        return added

    async def after_answer(self, question: str, primary_hits: list[dict]) -> None:
        if not self.enabled or not primary_hits:
            return
        await self._execute_retrieval(question, "seed", hits=primary_hits)
        if not self._swarm_started:
            self._swarm_started = True
            self._swarm_task = asyncio.create_task(self._swarm_loop())

    async def refresh(self, question: str | None = None) -> dict:
        """Force one foreground swarm pass and return the latest UI snapshot."""
        if not self.enabled:
            return empty_bubble()

        async with self._lock:
            self._status = "refreshing"
        self._notify()

        before = len(self._chunks)
        if question and len(self._chunks) < MAX_CHUNKS:
            await self._execute_retrieval(question, "refresh", k=5)

        if len(self._chunks) < MAX_CHUNKS:
            queries = await self._propose_queries()
            for q in queries:
                if q in self._prior_queries:
                    continue
                self._prior_queries.append(q)
                await self._execute_retrieval(q, "refresh", k=3)

        async with self._lock:
            if len(self._chunks) == before and question:
                query_id = self._start_query(question, "refresh")
                self._finish_query(query_id, added_lines=[], duration_ms=0)
                self._append_update(
                    added_lines=[],
                    source="refresh",
                    query=question,
                    query_id=query_id,
                    duration_ms=0,
                )
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
                    await self._execute_retrieval(q, "swarm", k=3)
                await asyncio.sleep(ROUND_PAUSE_SECS)
            self._status = "ready"
            self._notify()
        except asyncio.CancelledError:
            return
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
