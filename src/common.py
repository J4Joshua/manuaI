"""Shared local helpers for the M1 skateboard.

Talks to the local Ollama server over HTTP using ONLY the Python standard library
(no pip installs needed). Once the two models are pulled, everything here runs offline.
"""
import json
import math
import urllib.request

OLLAMA = "http://localhost:11434"
EMBED_MODEL = "nomic-embed-text"   # D6 — same model for index AND query
LLM_MODEL = "qwen2.5:3b"           # D4 — Qwen2.5-3B, served by Ollama


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
    """Local embedding. nomic-embed-text is trained with task prefixes, so we add
    'search_document:' at index time and 'search_query:' at query time. It is the
    SAME model on both sides (required) — the prefix only signals the role."""
    prefix = "search_query: " if kind == "query" else "search_document: "
    return _post("/api/embeddings", {"model": EMBED_MODEL, "prompt": prefix + text})["embedding"]


def chat_json(system, user):
    """Local LLM call with FORCED valid JSON (Ollama format='json'); temperature 0
    for a stable demo. A small model won't reliably emit clean JSON without this."""
    out = _post("/api/chat", {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    })
    return out["message"]["content"]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)
