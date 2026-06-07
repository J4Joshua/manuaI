"""Shared corpus → Moss DocumentInfo helpers (ingest + cloud upload)."""
from inferedge_moss import DocumentInfo

import corpus


def chunk_metadata(c: dict) -> dict[str, str]:
    """Moss requires all metadata values to be strings."""
    return {
        "machine_id": str(c["machine_id"]),
        "sop_id": str(c["sop_id"]),
        "doc_type": str(c["doc_type"]),
        "procedure_title": str(c["procedure_title"]),
        "section": str(c["section"]),
        "safety_flag": "true" if c["safety_flag"] else "false",
        "fault_codes": str(c.get("fault_codes", "")),
        "page": str(c["page"]) if c.get("page") is not None else "",
    }


def chunk_to_doc_info(c: dict, embedding: list[float] | None = None) -> DocumentInfo:
    return DocumentInfo(
        id=c["id"],
        text=c["text"],
        metadata=chunk_metadata(c),
        embedding=embedding,
    )


def build_document_infos(chunks: list[dict] | None = None) -> list[DocumentInfo]:
    return [chunk_to_doc_info(c) for c in (chunks if chunks is not None else corpus.build_chunks())]
