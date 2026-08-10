"""Discover knowledge files under rag-documents/ (nested zip layout)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

from src.config import KNOWLEDGE_BASE_IDS

ALLOWED_EXT = {".pdf", ".docx", ".xlsx"}
BLOCKLIST_NAMES = {"ds_store", ".ds_store", "thumbs.db"}


@dataclass
class SourceFile:
    path: Path
    department: str
    knowledge_base_id: str
    filename: str

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()


def _infer_department(path: Path) -> str | None:
    """Map path segments to a known department."""
    valid = {kb.lower(): kb for kb in KNOWLEDGE_BASE_IDS}
    for part in path.parts:
        key = part.lower()
        # Finance-20260808T... or plain Finance
        base = key.split("-")[0]
        if key in valid:
            return valid[key]
        if base in valid:
            return valid[base]
    return None


def discover_documents(root: Path) -> List[SourceFile]:
    """Walk root; return allowlisted documents with department metadata."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"documents root not found: {root}")

    found: List[SourceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.lower() in BLOCKLIST_NAMES:
            continue
        if path.suffix.lower() not in ALLOWED_EXT:
            continue
        dept = _infer_department(path.relative_to(root))
        if dept is None:
            # try parent folder name
            dept = _infer_department(path.parent)
        if dept is None:
            continue
        found.append(
            SourceFile(
                path=path,
                department=dept,
                knowledge_base_id=dept,
                filename=path.name,
            )
        )
    return found


def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "doc"
