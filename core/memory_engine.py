"""Zero-amnesia memory for Vajrastra.

Three tiers, all inside ``memory_bank/``:

1. **Episodic ledger** (``memory_bank/lessons.sqlite``) -- one row per distinct
   failure mode: ``error_signature`` -> ``root_cause`` -> ``verified_patch``
   plus ``experiment_metrics``.  Signatures are normalised (paths, line numbers,
   pointers and ANSI escapes removed) and hashed so the same bug is found
   instantly no matter where or when it recurs.
2. **Keyword index** -- an FTS5 virtual table over the ledger, with a pure
   ``LIKE`` fallback for SQLite builds without FTS5.
3. **Procedural skills** (``memory_bank/skills/``) -- reusable, already-verified
   code with metadata.  Every skill passes the security firewall before it is
   written to disk.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.security_guard import SecurityGuard, SecurityViolation, sha256_text

DB_RELPATH = "memory_bank/lessons.sqlite"
SKILLS_RELPATH = "memory_bank/skills"
SCHEMA_VERSION = 1
SNIPPET_LIMIT = 4000

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_ADDR_RE = re.compile(r"\bat\s+(?:0x)?[0-9a-f]{8,}\b")
_LINE_RE = re.compile(r"\bline\s+\d+\b", re.IGNORECASE)
_FILE_RE = re.compile(r'(?:[A-Za-z]:\\\\|(?:\.{0,2}/))[^\s"\']+\.py')
_TMP_RE = re.compile(r"(?:/tmp|/var/folders|/private/var|C:\\\\Users)[^\s\"']*")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    signature_hash    TEXT NOT NULL UNIQUE,
    error_signature   TEXT NOT NULL,
    root_cause        TEXT NOT NULL DEFAULT '',
    verified_patch    TEXT NOT NULL DEFAULT '',
    experiment_metrics TEXT NOT NULL DEFAULT '{}',
    tags              TEXT NOT NULL DEFAULT '',
    skill_ref         TEXT,
    success_count     INTEGER NOT NULL DEFAULT 0,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_hash ON lessons (signature_hash);
CREATE INDEX IF NOT EXISTS idx_lessons_updated ON lessons (updated_at);
"""

_FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5("
    "lesson_id UNINDEXED, body, tokenize='porter unicode61')"
)


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_signature(raw: str, limit: int = SNIPPET_LIMIT) -> str:
    """Strip volatile detail so the same failure produces the same signature."""
    text = unicodedata.normalize("NFKC", raw or "")
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _FILE_RE.sub("<path>", text)
    text = _TMP_RE.sub("<tmp>", text)
    text = _HEX_RE.sub("0xADDR", text)
    text = _ADDR_RE.sub("at <addr>", text)
    text = _LINE_RE.sub("line <n>", text)
    text = re.sub(r":\d+:\d+", ":<n>:<n>", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()[:limit]


def signature_hash(raw: str) -> str:
    return sha256_text(normalize_signature(raw).lower())


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "skill"


def _split_tags(tags: Iterable[str] | str | None) -> List[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = re.split(r"[,\s]+", tags)
    else:
        raw = [str(tag) for tag in tags]
    seen: List[str] = []
    for tag in raw:
        cleaned = tag.strip().lower()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


@dataclass(frozen=True)
class LessonRecord:
    """One row of the episodic ledger."""

    id: int
    signature_hash: str
    error_signature: str
    root_cause: str = ""
    verified_patch: str = ""
    experiment_metrics: Mapping[str, Any] = field(default_factory=dict)
    tags: Tuple[str, ...] = ()
    skill_ref: Optional[str] = None
    success_count: int = 0
    hit_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    @property
    def solved(self) -> bool:
        return bool(self.verified_patch.strip())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "signature_hash": self.signature_hash,
            "error_signature": self.error_signature,
            "root_cause": self.root_cause,
            "verified_patch": self.verified_patch,
            "experiment_metrics": dict(self.experiment_metrics),
            "tags": list(self.tags),
            "skill_ref": self.skill_ref,
            "success_count": self.success_count,
            "hit_count": self.hit_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "LessonRecord":
        try:
            metrics = json.loads(row["experiment_metrics"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metrics = {"raw": row["experiment_metrics"]}
        return cls(
            id=int(row["id"]),
            signature_hash=str(row["signature_hash"]),
            error_signature=str(row["error_signature"]),
            root_cause=str(row["root_cause"] or ""),
            verified_patch=str(row["verified_patch"] or ""),
            experiment_metrics=metrics if isinstance(metrics, dict) else {"value": metrics},
            tags=tuple(_split_tags(row["tags"])),
            skill_ref=row["skill_ref"],
            success_count=int(row["success_count"] or 0),
            hit_count=int(row["hit_count"] or 0),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )


@dataclass(frozen=True)
class SkillRecord:
    """A reusable, firewall-cleared procedure stored on disk."""

    name: str
    slug: str
    description: str = ""
    tags: Tuple[str, ...] = ()
    language: str = "python"
    code_sha256: str = ""
    created_at: str = ""
    updated_at: str = ""
    path: str = ""
    code: str = ""

    def as_dict(self, include_code: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "tags": list(self.tags),
            "language": self.language,
            "code_sha256": self.code_sha256,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": self.path,
        }
        if include_code:
            payload["code"] = self.code
        return payload


class SkillStore:
    """Tier-3 procedural memory: reusable skills as ``memory_bank/skills/*``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / SKILLS_RELPATH
        self.directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _meta_path(self, slug: str) -> Path:
        return self.directory / f"{slug}.json"

    def _code_path(self, slug: str) -> Path:
        return self.directory / f"{slug}.py"

    def exists(self, name: str) -> bool:
        return self._meta_path(slugify(name)).is_file()

    # ------------------------------------------------------------------ write
    def save_skill(
        self,
        name: str,
        code: str = "",
        *,
        description: str = "",
        tags: Iterable[str] | str | None = None,
        language: str = "python",
        validate: bool = True,
        overwrite: bool = True,
        authorized: Iterable[str] = (),
    ) -> SkillRecord:
        """Persist a skill after clearing it through the security firewall."""
        slug = slugify(name)
        meta_path = self._meta_path(slug)
        if meta_path.exists() and not overwrite:
            raise FileExistsError(f"skill '{slug}' already exists")

        if validate and language == "python" and code.strip():
            guard = SecurityGuard(self.root)
            guard.require_safe_source(code, filename=f"{SKILLS_RELPATH}/{slug}.py", authorized=authorized)

        now = utc_now()
        created_at = now
        if meta_path.is_file():
            try:
                previous = json.loads(meta_path.read_text(encoding="utf-8"))
                created_at = str(previous.get("created_at") or now)
            except (OSError, json.JSONDecodeError):
                created_at = now

        code_path = self._code_path(slug)
        if code.strip():
            code_path.write_text(code, encoding="utf-8")
        elif code_path.is_file():
            code_path.unlink()

        meta = {
            "name": name,
            "slug": slug,
            "description": description,
            "tags": _split_tags(tags),
            "language": language,
            "code_sha256": sha256_text(code) if code else "",
            "created_at": created_at,
            "updated_at": now,
        }
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return SkillRecord(
            name=name,
            slug=slug,
            description=description,
            tags=tuple(meta["tags"]),
            language=language,
            code_sha256=meta["code_sha256"],
            created_at=created_at,
            updated_at=now,
            path=str(code_path.relative_to(self.root)) if code.strip() else "",
            code=code,
        )

    # ------------------------------------------------------------------- read
    def load_skill(self, name: str, *, include_code: bool = True) -> SkillRecord:
        slug = slugify(name)
        meta_path = self._meta_path(slug)
        if not meta_path.is_file():
            raise KeyError(f"no skill named '{name}'")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        code = ""
        code_path = self._code_path(slug)
        if include_code and code_path.is_file():
            code = code_path.read_text(encoding="utf-8", errors="replace")
        return SkillRecord(
            name=str(meta.get("name") or name),
            slug=slug,
            description=str(meta.get("description") or ""),
            tags=tuple(_split_tags(meta.get("tags"))),
            language=str(meta.get("language") or "python"),
            code_sha256=str(meta.get("code_sha256") or ""),
            created_at=str(meta.get("created_at") or ""),
            updated_at=str(meta.get("updated_at") or ""),
            path=str(code_path.relative_to(self.root)) if code_path.is_file() else "",
            code=code,
        )

    def list_skills(self, *, tag: Optional[str] = None, limit: int = 200) -> List[SkillRecord]:
        wanted = tag.strip().lower() if tag else None
        records: List[SkillRecord] = []
        for meta_path in sorted(self.directory.glob("*.json")):
            try:
                record = self.load_skill(meta_path.stem, include_code=False)
            except (KeyError, OSError, json.JSONDecodeError):
                continue
            if wanted and wanted not in record.tags:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def search_skills(self, query: str, *, limit: int = 20) -> List[SkillRecord]:
        terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_\-]{2,}", query or "")]
        scored: List[Tuple[int, SkillRecord]] = []
        for record in self.list_skills():
            haystack = " ".join(
                [record.name, record.description, " ".join(record.tags), record.slug]
            ).lower()
            score = sum(haystack.count(term) for term in terms)
            if score:
                scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [record for _, record in scored[:limit]]

    def count(self) -> int:
        return len(list(self.directory.glob("*.json")))

    def delete_skill(self, name: str) -> bool:
        slug = slugify(name)
        removed = False
        for path in (self._meta_path(slug), self._code_path(slug)):
            if path.is_file():
                path.unlink()
                removed = True
        return removed


class MemoryEngine:
    """Episodic ledger + keyword index + procedural skill store."""

    def __init__(self, root: Path | str, db_path: Optional[Path | str] = None) -> None:
        self.root = Path(root).resolve()
        self.db_path = Path(db_path).resolve() if db_path else self.root / DB_RELPATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self.fts_enabled = False
        with self._lock:
            self._configure()
            self._init_schema()
        self.skills = SkillStore(self.root)

    # ------------------------------------------------------------- lifecycle
    def _configure(self) -> None:
        cursor = self._connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")

    def _init_schema(self) -> None:
        cursor = self._connection.cursor()
        cursor.executescript(_SCHEMA)
        try:
            cursor.execute(_FTS_SCHEMA)
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False
        cursor.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        cursor.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('fts_enabled', ?)",
            ("1" if self.fts_enabled else "0",),
        )
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "MemoryEngine":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------------------------------------------------------------- ledger
    def record_lesson(
        self,
        error_signature: str,
        *,
        root_cause: str = "",
        verified_patch: str = "",
        experiment_metrics: Optional[Mapping[str, Any]] = None,
        tags: Iterable[str] | str | None = None,
        skill_ref: Optional[str] = None,
        verified: bool = False,
    ) -> LessonRecord:
        """Insert or merge a lesson.  Verified patches never get overwritten by blanks."""
        normalized = normalize_signature(error_signature)
        digest = sha256_text(normalized.lower())
        metrics_json = json.dumps(dict(experiment_metrics or {}), sort_keys=True)
        tags_text = ",".join(_split_tags(tags))
        now = utc_now()
        increment = 1 if verified else 0
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(
                """
                INSERT INTO lessons (
                    signature_hash, error_signature, root_cause, verified_patch,
                    experiment_metrics, tags, skill_ref, success_count, hit_count,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(signature_hash) DO UPDATE SET
                    error_signature    = excluded.error_signature,
                    root_cause         = CASE WHEN excluded.root_cause <> ''
                                              THEN excluded.root_cause ELSE lessons.root_cause END,
                    verified_patch     = CASE WHEN excluded.verified_patch <> ''
                                              THEN excluded.verified_patch ELSE lessons.verified_patch END,
                    experiment_metrics = CASE WHEN excluded.experiment_metrics <> '{}'
                                              THEN excluded.experiment_metrics
                                              ELSE lessons.experiment_metrics END,
                    tags               = CASE WHEN excluded.tags <> ''
                                              THEN excluded.tags ELSE lessons.tags END,
                    skill_ref          = COALESCE(excluded.skill_ref, lessons.skill_ref),
                    success_count      = lessons.success_count + ?,
                    updated_at         = excluded.updated_at
                """,
                (
                    digest,
                    normalized,
                    root_cause,
                    verified_patch,
                    metrics_json,
                    tags_text,
                    skill_ref,
                    increment,
                    now,
                    now,
                    increment,
                ),
            )
            self._connection.commit()
            row = cursor.execute(
                "SELECT * FROM lessons WHERE signature_hash = ?", (digest,)
            ).fetchone()
            self._index_row(row)
        return LessonRecord.from_row(row)

    def _index_row(self, row: sqlite3.Row) -> None:
        if not self.fts_enabled:
            return
        body = " ".join(
            [
                str(row["error_signature"] or ""),
                str(row["root_cause"] or ""),
                str(row["verified_patch"] or ""),
                str(row["tags"] or ""),
            ]
        )
        cursor = self._connection.cursor()
        cursor.execute("DELETE FROM lessons_fts WHERE lesson_id = ?", (row["id"],))
        cursor.execute(
            "INSERT INTO lessons_fts (lesson_id, body) VALUES (?, ?)", (row["id"], body)
        )
        self._connection.commit()

    def get_by_signature(self, error_signature: str) -> Optional[LessonRecord]:
        return self.get_by_hash(sha256_text(normalize_signature(error_signature).lower()))

    def get_by_hash(self, digest: str) -> Optional[LessonRecord]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM lessons WHERE signature_hash = ?", (digest,)
            ).fetchone()
        return LessonRecord.from_row(row) if row else None

    def get(self, lesson_id: int) -> Optional[LessonRecord]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM lessons WHERE id = ?", (lesson_id,)
            ).fetchone()
        return LessonRecord.from_row(row) if row else None

    def record_hit(self, lesson_id: int, *, resolved: bool = False) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE lessons SET hit_count = hit_count + 1, "
                "success_count = success_count + ?, updated_at = ? WHERE id = ?",
                (1 if resolved else 0, utc_now(), lesson_id),
            )
            self._connection.commit()

    def search(self, query: str, *, limit: int = 20, tag: Optional[str] = None) -> List[LessonRecord]:
        """Fast keyword lookup; falls back to LIKE when FTS5 is unavailable."""
        terms = [term for term in re.findall(r"[A-Za-z0-9_\.\-]{2,}", query or "")][:12]
        if not terms:
            return self.recent(limit=limit)
        wanted_tag = tag.strip().lower() if tag else None

        with self._lock:
            if self.fts_enabled:
                fts_query = " ".join('"' + term.replace('"', '""') + '"' for term in terms)
                try:
                    rows = self._connection.execute(
                        """
                        SELECT l.* FROM lessons_fts f
                        JOIN lessons l ON l.id = f.lesson_id
                        WHERE f MATCH ?
                        ORDER BY bm25(f)
                        LIMIT ?
                        """,
                        (fts_query, limit * 4),
                    ).fetchall()
                    records = [LessonRecord.from_row(row) for row in rows]
                    if wanted_tag:
                        records = [r for r in records if wanted_tag in r.tags]
                    if records:
                        return records[:limit]
                except sqlite3.OperationalError:
                    pass

            clauses = []
            params: List[Any] = []
            for term in terms:
                clauses.append(
                    "(error_signature LIKE ? OR root_cause LIKE ? OR verified_patch LIKE ? OR tags LIKE ?)"
                )
                wildcard = f"%{term}%"
                params.extend([wildcard, wildcard, wildcard, wildcard])
            sql = "SELECT * FROM lessons WHERE " + " OR ".join(clauses)
            sql += " ORDER BY success_count DESC, updated_at DESC LIMIT ?"
            params.append(limit * 4)
            rows = self._connection.execute(sql, params).fetchall()
        records = [LessonRecord.from_row(row) for row in rows]
        if wanted_tag:
            records = [record for record in records if wanted_tag in record.tags]
        return records[:limit]

    def recent(self, *, limit: int = 10) -> List[LessonRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM lessons ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [LessonRecord.from_row(row) for row in rows]

    def unresolved(self, *, limit: int = 20) -> List[LessonRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM lessons WHERE TRIM(verified_patch) = '' "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [LessonRecord.from_row(row) for row in rows]

    def all_tags(self) -> List[Tuple[str, int]]:
        counts: Dict[str, int] = {}
        with self._lock:
            rows = self._connection.execute("SELECT tags FROM lessons").fetchall()
        for row in rows:
            for tag in _split_tags(row["tags"]):
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = int(self._connection.execute("SELECT COUNT(*) FROM lessons").fetchone()[0])
            solved = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM lessons WHERE TRIM(verified_patch) <> ''"
                ).fetchone()[0]
            )
            hits = int(
                self._connection.execute("SELECT COALESCE(SUM(hit_count), 0) FROM lessons").fetchone()[0]
            )
        try:
            db_bytes = self.db_path.stat().st_size
        except OSError:
            db_bytes = 0
        return {
            "db_path": str(self.db_path),
            "lessons": total,
            "verified_patches": solved,
            "unsolved": total - solved,
            "total_hits": hits,
            "top_tags": self.all_tags()[:10],
            "fts_enabled": self.fts_enabled,
            "db_bytes": db_bytes,
            "skills": self.skills.count(),
            "skill_dir": str(self.skills.directory),
        }

    # ---------------------------------------------------------------- sync
    def export_snapshot(self) -> Dict[str, Any]:
        """Portable snapshot used by ``vajrastra sync`` and the Colab bridge."""
        with self._lock:
            rows = self._connection.execute("SELECT * FROM lessons ORDER BY id").fetchall()
        lessons = [LessonRecord.from_row(row).as_dict() for row in rows]
        skills = [
            skill.as_dict(include_code=True) for skill in self.skills.list_skills()
        ]
        return {
            "version": SCHEMA_VERSION,
            "exported_at": utc_now(),
            "lessons": lessons,
            "skills": skills,
        }

    def import_snapshot(self, snapshot: Mapping[str, Any]) -> Dict[str, int]:
        imported_lessons = 0
        imported_skills = 0
        for lesson in snapshot.get("lessons") or []:
            if not isinstance(lesson, Mapping):
                continue
            signature = str(lesson.get("error_signature") or "").strip()
            if not signature:
                continue
            self.record_lesson(
                signature,
                root_cause=str(lesson.get("root_cause") or ""),
                verified_patch=str(lesson.get("verified_patch") or ""),
                experiment_metrics=lesson.get("experiment_metrics") or {},
                tags=lesson.get("tags") or (),
                skill_ref=lesson.get("skill_ref"),
                verified=bool(lesson.get("success_count")),
            )
            imported_lessons += 1
        for skill in snapshot.get("skills") or []:
            if not isinstance(skill, Mapping) or not skill.get("name"):
                continue
            code = str(skill.get("code") or "")
            try:
                self.skills.save_skill(
                    str(skill["name"]),
                    code,
                    description=str(skill.get("description") or ""),
                    tags=skill.get("tags") or (),
                    language=str(skill.get("language") or "python"),
                    validate=bool(code.strip()),
                )
            except SecurityViolation:
                continue
            imported_skills += 1
        return {"lessons": imported_lessons, "skills": imported_skills}
