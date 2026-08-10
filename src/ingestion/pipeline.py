"""End-to-end ingest: discover → parse → chunk → embed → index."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import Settings, get_settings
from src.ingestion.chunk import chunk_document
from src.ingestion.discover import discover_documents
from src.ingestion.embed import embed_chunks
from src.ingestion.index import upsert_chunks
from src.ingestion.parse import parse_file
from src.models.schemas import ChunkRecord
from src.observability import get_logger, setup_logging

log = get_logger(__name__)


def run_ingest(
    source: Optional[Path] = None,
    settings: Optional[Settings] = None,
    *,
    force_local: bool = False,
) -> Dict[str, Any]:
    s = settings or get_settings()
    setup_logging(s.log_level)
    root = source or s.documents_path()
    log.info("ingest start root=%s mode=%s", root, s.effective_mode())

    files = discover_documents(root)
    if not files:
        raise RuntimeError(f"no documents found under {root}")

    all_chunks: List[ChunkRecord] = []
    per_file: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for sf in files:
        try:
            doc = parse_file(sf)
            chunks = chunk_document(doc, s)
            all_chunks.extend(chunks)
            per_file.append(
                {
                    "filename": sf.filename,
                    "department": sf.department,
                    "doc_id": doc.doc_id,
                    "sections": len(doc.sections),
                    "chunks": len(chunks),
                    "is_current": doc.is_current,
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("failed %s", sf.path)
            errors.append({"file": str(sf.path), "error": str(exc)[:300]})

    if not all_chunks:
        raise RuntimeError(f"no chunks produced; errors={errors}")

    embed_chunks(all_chunks, s)
    index_result = upsert_chunks(all_chunks, s, force_local=force_local)

    by_dept = Counter(c.department for c in all_chunks)
    summary = {
        "files_discovered": len(files),
        "files_ok": len(per_file),
        "files_failed": len(errors),
        "chunks_total": len(all_chunks),
        "chunks_by_department": dict(by_dept),
        "per_file": per_file,
        "errors": errors,
        "index": index_result,
    }
    log.info(
        "ingest complete files_ok=%s chunks=%s by_dept=%s",
        len(per_file),
        len(all_chunks),
        dict(by_dept),
    )
    return summary
