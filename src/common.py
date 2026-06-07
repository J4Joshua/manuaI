"""Shared local helpers for the M1 skateboard.

Talks to the local Ollama server over HTTP using ONLY the Python standard library
(no pip installs needed). Once the models are pulled, everything here runs offline.
"""
import json
import math
import os
import urllib.request

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"   # D6 — CosineRetriever stub (index.json path)
LLM_MODEL = "qwen2.5:3b"           # D4 — Qwen2.5-3B, served by Ollama

# Keep the model resident between turns so a pause in the demo never triggers an
# Ollama eviction → cold reload (measured cold turn-1 = 7.6 s vs ~2.0 s warm). For the
# absolute cold-start win also export OLLAMA_FLASH_ATTENTION=1 and
# OLLAMA_KV_CACHE_TYPE=q8_0 before `ollama serve` (server-level, can't be set per-call).
LLM_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "1h")
# Runaway safety cap only — answers are 1–2 sentences (~70–90 tokens), so this never
# binds in practice; it just bounds a pathological generation. NOT a latency lever
# (measured: the LLM is prompt-processing-bound, not generation-bound).
LLM_NUM_PREDICT = int(os.environ.get("LLM_NUM_PREDICT", "256"))


def _post(path, payload, timeout=180):
    req = urllib.request.Request(
        OLLAMA + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def embed(text, kind="document"):
    """Local embedding for the CosineRetriever stub (nomic-embed-text via Ollama)."""
    prefix = "search_query: " if kind == "query" else "search_document: "
    return _post("/api/embeddings", {"model": EMBED_MODEL, "prompt": prefix + text})["embedding"]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def chat_json(system, user):
    """Local LLM call with FORCED valid JSON (Ollama format='json'); temperature 0
    for a stable demo. A small model won't reliably emit clean JSON without this.
    keep_alive pins the model resident; num_predict is a runaway safety cap."""
    out = _post("/api/chat", {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": LLM_KEEP_ALIVE,
        "options": {"temperature": 0, "num_predict": LLM_NUM_PREDICT},
    })
    return out["message"]["content"]


def warmup_llm():
    """Load + pin Qwen so the FIRST real turn isn't a cold 7.6 s reload. Cheap (one
    tiny generation). Safe to call at startup; swallows errors (warmup is best-effort)."""
    try:
        _post("/api/chat", {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": "ok"}],
            "format": "json",
            "stream": False,
            "keep_alive": LLM_KEEP_ALIVE,
            "options": {"temperature": 0, "num_predict": 1},
        }, timeout=120)
        return True
    except Exception:
        return False


def warmup_embed():
    """Warm the nomic embedder (stub path) so its first query isn't a cold load."""
    try:
        embed("warmup", "query")
        return True
    except Exception:
        return False
