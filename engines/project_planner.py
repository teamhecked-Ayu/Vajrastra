"""Safe source materialization for commit-aware planner context.

This adapter is deliberately outside core/. It consumes ProjectIndex metadata,
then reads only selected files from the same workspace after validating paths,
size bounds, and Python source digests.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Mapping, Tuple

from engines.project_understanding import ProjectIndex, ProjectIndexError

MAX_SOURCE_FILE_BYTES = 64_000
MAX_SOURCE_CONTEXT_CHARS = 16_000
MAX_SOURCE_FILES = 12


def render_project_context(
    index: ProjectIndex,
    instruction: str,
    *,
    current_commit_sha: str,
    max_files: int = MAX_SOURCE_FILES,
    max_chars: int = MAX_SOURCE_CONTEXT_CHARS,
) -> Tuple[str, Dict[str, object]]:
    """Return selected source text and selection metadata, rejecting stale data."""
    context = index.task_context(
        instruction,
        current_commit_sha=current_commit_sha,
        max_files=max_files,
        max_chars=max_chars,
    )
    root = Path(index.root).resolve()
    chunks = []
    used = 0
    for rel in context["selected_files"]:
        raw_candidate = root / str(rel)
        cursor = root
        for part in Path(str(rel)).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ProjectIndexError("selected path became a symlink after indexing")
        candidate = raw_candidate.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ProjectIndexError("selected path escapes indexed workspace") from exc
        if not candidate.is_file():
            continue
        size = candidate.stat().st_size
        if size > MAX_SOURCE_FILE_BYTES:
            continue
        try:
            source = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        record = index.python_files.get(str(rel))
        if record is not None:
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            if digest != record.sha256:
                raise ProjectIndexError(
                    "workspace changed after indexing; rebuild project index"
                )
        block = f"--- {rel} ---\\n{source.rstrip()}\\n"
        if used + len(block) > max_chars:
            break
        chunks.append(block)
        used += len(block)
    rendered = (
        "VERIFIED PROJECT CONTEXT (read-only source excerpts; do not assume "
        "unlisted behavior):\\n" + "\\n".join(chunks)
        if chunks else ""
    )
    return rendered, context
