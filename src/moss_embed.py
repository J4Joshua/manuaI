"""Local Moss embedding via moss_core.PyEmbeddingService (offline, in-process).

Uses the same built-in models as Moss index/query (moss-minilm default). Embed at
ingestion with this module and at query time with the same model_id — parity rule.
"""
import math
import os

import moss_core

_embedders: dict[str, moss_core.PyEmbeddingService] = {}


def model_id() -> str:
    mid = os.environ.get("MOSS_MODEL_ID", "moss-minilm")
    if mid == "custom":
        raise SystemExit(
            "MOSS_MODEL_ID=custom is for precomputed vectors only; "
            "set moss-minilm (or moss-mediumlm) for local embedding."
        )
    return mid


def get_embedder(mid: str | None = None) -> moss_core.PyEmbeddingService:
    mid = mid or model_id()
    svc = _embedders.get(mid)
    if svc is None:
        svc = moss_core.PyEmbeddingService(mid)
        svc.load_model()
        _embedders[mid] = svc
    return svc


def embed_dim(mid: str | None = None) -> int:
    mid = mid or model_id()
    vec = get_embedder(mid).create_embedding("probe")
    return len(vec)


def embed_text(text: str, mid: str | None = None) -> list[float]:
    return list(get_embedder(mid).create_embedding(text))


def embed_texts(texts: list[str], mid: str | None = None) -> list[list[float]]:
    return [list(v) for v in get_embedder(mid).create_embeddings(texts)]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)
