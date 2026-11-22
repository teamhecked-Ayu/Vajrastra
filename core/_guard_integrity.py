"""Integrity lock half of the immutable security layer.

Sealing, verification and the guarded patch writer live here so that every
file of the trust root stays small enough to audit in one sitting.  The mixin
is only ever consumed by :class:`core.security_guard.SecurityGuard`; it is
sealed and write-protected exactly like the rest of ``core/``.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from core._guard_rules import (
    LOCK_RELPATH,
    LOCK_VERSION,
    IntegrityError,
    IntegrityReport,
    ScanResult,
    SecurityViolation,
    bundle_digest,
    sha256_file,
)

class GuardIntegrityMixin:
    """Seal, verify and atomically write the trusted core."""

    # --------------------------------------------------------------- integrity
    def seal(self, *, force: bool = False) -> Dict[str, Any]:
        """Write the SHA-256 lock manifest for the trusted core."""
        if self.lock_path.exists() and not force:
            raise IntegrityError(
                f"{LOCK_RELPATH} already exists; pass force=True to reseal deliberately"
            )
        files: Dict[str, str] = {}
        for rel in self.manifest_targets():
            absolute = self.root / rel
            if absolute.is_file():
                files[rel] = sha256_file(absolute)
        if not files:
            raise IntegrityError(f"no files found to seal under {self.root}")
        manifest = {
            "version": LOCK_VERSION,
            "algorithm": "sha256",
            "generated_by": "core.security_guard",
            "sealed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "immutable": list(self.immutable),
            "files": files,
            "bundle_sha256": bundle_digest(files),
        }
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.lock_path.with_name(self.lock_path.name + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.lock_path)
        return manifest

    def read_manifest(self) -> Optional[Dict[str, Any]]:
        if not self.lock_path.is_file():
            return None
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def verify(self) -> IntegrityReport:
        """Recompute digests and compare them with the sealed manifest."""
        manifest = self.read_manifest()
        if not manifest:
            return IntegrityReport(
                ok=False, status="unsealed", lock_path=self.relpath(self.lock_path)
            )

        recorded: Dict[str, str] = dict(manifest.get("files") or {})
        checked: List[str] = []
        modified: List[str] = []
        missing: List[str] = []

        for rel, digest in sorted(recorded.items()):
            absolute = self.root / rel
            if not absolute.is_file():
                missing.append(rel)
                continue
            checked.append(rel)
            if sha256_file(absolute) != digest:
                modified.append(rel)

        current = {
            rel: sha256_file(self.root / rel) for rel in checked if (self.root / rel).is_file()
        }
        expected = set(recorded)
        unexpected = [rel for rel in self.manifest_targets() if rel not in expected]

        ok = not modified and not missing
        return IntegrityReport(
            ok=ok,
            status="verified" if ok else "drift",
            checked=tuple(checked),
            modified=tuple(modified),
            missing=tuple(missing),
            unexpected=tuple(sorted(unexpected)),
            bundle_sha256=bundle_digest(current) if current else "",
            expected_bundle=str(manifest.get("bundle_sha256", "")),
            sealed_at=str(manifest.get("sealed_at", "")),
            lock_path=self.relpath(self.lock_path),
        )

    def assert_integrity(self) -> IntegrityReport:
        report = self.verify()
        if report.status == "unsealed":
            raise IntegrityError(
                "trusted core is unsealed; run `python -m core.security_guard seal` "
                "before relying on self-evolution"
            )
        if not report.ok:
            raise IntegrityError("trusted core integrity check failed: " + report.describe())
        return report

    # ------------------------------------------------------------------ patching
    def write_patch(
        self,
        path: Path | str,
        source: str,
        *,
        authorized: Iterable[str] = (),
        force: bool = False,
    ) -> ScanResult:
        """Validate *source* and atomically write it to *path*.

        The immutable security layer and the trusted core tree are refused
        outright -- that is what stops a self-modified module from disabling the
        firewall that is supposed to judge it.
        """
        target = Path(path)
        if not target.is_absolute():
            target = self.root / target
        rel = self.relpath(target)

        if self.is_protected(target) and not force:
            raise SecurityViolation(
                f"refusing to patch protected module {rel}: the security layer is immutable"
            )

        result = self.scan_source(source, filename=rel, authorized=authorized)
        if not result.ok:
            raise SecurityViolation(f"refusing to write unsafe code to {rel}", result.violations)

        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir-target.parent, delete=False, suffix=".vajra"
        )
        try:
            handle.write(source)
            handle.flush()
            handle.close()
            Path(handle.name).replace(target)
        finally:
            if not handle.closed:
                handle.close()
            leftover = Path(handle.name)
            if leftover.exists():
                leftover.unlink()
        return result

    def audit(self) -> Dict[str, Any]:
        """Full trust report: integrity plus a scan of every manifest target."""
        report = self.verify()
        scans = [self.scan_file(rel) for rel in report.checked]
        return {
            "integrity": report.as_dict(),
            "scans": [scan.as_dict() for scan in scans],
            "protected": sorted(self.manifest_targets()),
        }


