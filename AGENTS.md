# Vajrastra — Architecture & Engineering Rules

Vajrastra is a universal, cross-OS autonomous engineer: a hyper-debugger and ML
scientist that runs at **$0** on Android (no Termux), Parrot OS, Linux, macOS,
Windows and Google Colab. Phase 1 ships the substrate: a hardened security
layer, zero-amnesia memory, cross-OS routing and a working CLI/REPL.

---

## 1. Non-negotiable constraints

1. **Standard library only.** Every module under `core/` and `vajrastra.py`
   imports nothing outside the Python 3.10+ standard library. `pyproject.toml`
   declares `dependencies = []` and that must stay empty. No pip install, no
   virtualenv, no build step is ever required to run Vajrastra.
2. **`python vajrastra.py` must work, unchanged, on every supported OS.**
   No shell-specific syntax, no `termios`/`fork`/`fcntl`, no POSIX-only paths,
   no assumption that `/` exists, no case-insensitive import assumptions.
   Always open text files with `encoding="utf-8"` and `errors="replace"` where
   the input is not ours.
3. **Zero cost.** No paid API is required for Phase 1. Heavy compute borrows
   Google Colab through the GitHub API bridge; the local machine never needs a
   GPU to be useful.
4. **Fail loud, fail safe.** Probes (GPU, RAM, distro) return `unknown`/`0`
   instead of raising. Security and integrity failures *must* raise.

---

## 2. Repository map

```
vajrastra.py              CLI + interactive REPL (entrypoint: `vajrastra`)
pyproject.toml            packaging metadata, zero runtime dependencies
AGENTS.md                 this document: architecture + engineering rules
core/
  __init__.py             package marker
  security_guard.py       IMMUTABLE: AST firewall + SHA-256 integrity lock
  os_adapter.py           host detection, hardware profile, execution routing
  memory_engine.py        SQLite episodic ledger, FTS index, procedural skills
memory_bank/
  lessons.sqlite          tier-1 episodic ledger (created at runtime)
  skills/                 tier-3 reusable procedures (`<slug>.json` + `<slug>.py`)
  plans/ bridge/outbox/ research/ evolution/ sync/   generated artefacts
tests/                    stdlib `unittest` suites for every module
core/security_lock.json   sealed SHA-256 manifest of the trusted core
```

---

## 3. Architecture

### 3.1 `core/security_guard.py` — the trust root (immutable)

**This file is never modified by Vajrastra or by its self-evolution loop.**
It is the only module allowed to decide what code may exist on disk.

* **AST static firewall.** `SecurityGuard.scan_source()` parses code with
  `ast` and reports violations in these categories:
  * `process_execution` — `os.system`, `os.popen`, `os.exec*`, `os.spawn*`,
    `subprocess.*`, `pty.spawn`
  * `dynamic_execution` — `eval`, `exec`, `compile`, `__import__`,
    `marshal.loads`, `pickle.loads`
  * `destructive_fs` — `shutil.rmtree`, `os.remove/unlink/rmdir`, `Path.unlink`
  * `network` — raw sockets, `urllib`, `http.client`, `ftplib`, `smtplib`
  * `native_bridge` — `ctypes.CDLL`, `cffi`
  * `privilege_change` — `os.setuid`, `os.chroot`, `os.forkpty`
  * `shell_injection` — `rm -rf /`, fork bombs, `curl … | sh`, `/dev/tcp/`
  * `secret_literal` — hard-coded GitHub/OpenAI/AWS/Slack/Google credentials
  * `secret_exfiltration` — reading `*TOKEN|*SECRET|*PASSWORD*` from the
    environment **and** opening a network channel in the same unit

  Import aliases are resolved (`import os as o`, `from os import system`),
  so renaming the callee does not evade the firewall.

* **Capability grants.** A module that legitimately needs a capability must
  declare it inline: `# vajra-allow: network`. Granted findings move to
  `waived` and the scan still records them. Nothing is granted implicitly.

* **Integrity lock.** `SecurityGuard.seal()` writes `core/security_lock.json`
  containing a SHA-256 digest per core file plus a bundle digest.
  `verify()` recomputes them and reports `modified`, `missing` and
  `unexpected` files; `assert_integrity()` raises `IntegrityError` on drift.
  Re-sealing is deliberate (`seal(force=True)`) and shows up as a git diff —
  the lock is committed, so tampering is always visible in review.

* **Write gate.** All code writes go through `write_patch()`, which
  (a) refuses any path under `core/` and (b) scans the payload first. A
  self-modified module therefore cannot disable the firewall that judges it.

### 3.2 `core/memory_engine.py` — zero-amnesia memory (three tiers)

| Tier | Location | Purpose |
| --- | --- | --- |
| 1. Episodic ledger | `memory_bank/lessons.sqlite` | `error_signature` → `root_cause` → `verified_patch` → `experiment_metrics`, with hash + tag indexes |
| 2. Keyword index | `lessons_fts` (SQLite FTS5) | fast recall over signature, cause, patch and tags; automatic `LIKE` fallback when FTS5 is unavailable |
| 3. Procedural skills | `memory_bank/skills/` | reusable, firewall-cleared code: `<slug>.json` metadata + `<slug>.py` |

Rules:

* **Signature normalisation is mandatory.** `normalize_signature()` removes
  ANSI escapes, temp paths, absolute `.py` paths, hex addresses, line numbers
  and whitespace noise; `signature_hash()` hashes the lower-cased result. The
  same bug in a different directory must hit the same row.
* **Merges never lose knowledge.** A later `record_lesson()` with empty
  `root_cause`/`verified_patch` keeps the stored values; non-empty values win.
* **Verified patches only.** `verified=True` increments `success_count`.
  A lesson without `verified_patch` is *open* and appears in the evolution
  backlog — never present an unverified patch as a fix.
* **Metrics are structured.** `experiment_metrics` is a JSON object
  (loss, accuracy, latency, fuzz coverage…), never prose.
* **Skills are code, so they are scanned.** `SkillStore.save_skill()` refuses
  anything the firewall rejects. Skills that fail a later scan are reported by
  `vajrastra evolve`.

### 3.3 `core/os_adapter.py` — dual-mode execution

| Host | Detected as | Mode for heavy work (GPU/training/fuzz/benchmark) | Mode for light work |
| --- | --- | --- | --- |
| Linux | `linux` | Local Native | Local Native |
| Parrot OS | `parrot` | Local Native | Local Native |
| macOS | `macos` | Local Native | Local Native |
| Windows | `windows` | Local Native | Local Native |
| Android (no Termux) | `android` | **Cloud Bridge → Google Colab via GitHub API** | Local Native (CPU) |

* Detection uses `sys.platform`, `ANDROID_ROOT`/`ANDROID_DATA`/`ANDROID_STORAGE`
  and `/system/build.prop` for Android; `/etc/os-release` (`ID`, `ID_LIKE`,
  `NAME`) for Parrot. Unknown hosts degrade to `unknown`.
* Hardware profiling is best-effort: `nvidia-smi` (CUDA), `rocm-smi`/`/dev/kfd`
  (ROCm), `system_profiler` (Metal), `/proc/meminfo`, `sysctl`, Win32
  `GlobalMemoryStatusEx`. Every probe is time-bounded and optional.
* `OSAdapter.route(task_class)` returns a `RoutingDecision` with mode, reason,
  bridge readiness and notes. Override with `VAJRASTRA_EXECUTION_MODE=local|cloud`.
* The GitHub token is only ever *presence-checked* (`VAJRASTRA_GITHUB_TOKEN`,
  `GITHUB_TOKEN`, `GH_TOKEN`) — never printed, logged, stored or committed.

### 3.4 `vajrastra.py` — CLI and REPL

```
vajrastra fix <target>          analyse source/tree/traceback, recall verified patches
vajrastra build "<prompt>"      classify + route + write a plan or a Colab job spec
vajrastra research "<topic>"    mine local memory, write memory_bank/research/<slug>.md
vajrastra evolve                integrity + skill firewall audit, evolution report
vajrastra memory stats          ledger counters
vajrastra memory search <q>     signature hash first, then keyword recall
vajrastra sync [--import PATH]  export or merge a portable memory snapshot
```

No arguments opens the REPL (`vajrastra>`). Workspace root resolution:
`--root` → `VAJRASTRA_ROOT` → the directory containing `vajrastra.py`. Exit
codes: `0` success, `1` findings/backlog, `2` usage or security/integrity error.

Phase 1 is deliberately honest: `build`, `research` and `fix` produce plans,
routing decisions and recalled knowledge. They do not invent code or claim
network results that were never fetched.

---

## 4. Engineering rules

**Security**

1. Never weaken, bypass or "temporarily disable" `core/security_guard.py`.
   Any change to it is a reviewed, re-sealed architectural change.
2. All generated or retrieved code is scanned *before* it is written or run.
3. No secrets in code, tests, docs, logs or memory. Tokens come from the
   environment and are only checked for presence.
4. No `eval`/`exec`/`compile` anywhere in the codebase.
5. Network access is never implicit: declare it with `# vajra-allow: network`
   and keep it in the narrowest possible function.

**Memory**

6. Every fixed bug becomes a lesson: normalised signature, root cause,
   verified patch, metrics, tags. No lesson without a reproduction.
7. Never delete a verified lesson; supersede it with a newer patch.
8. One skill = one responsibility, with a description and tags, and it must
   pass the firewall.

**Code**

9. Type hints on public functions, dataclasses for records, no mutable
   default arguments, no bare `except:`.
10. Every failure path is explicit: raise `SecurityViolation`, `IntegrityError`,
    `ValueError` or `KeyError`; never swallow silently.
11. Modules stay importable in isolation; `core/` must never import
    `vajrastra.py`.
12. Cross-OS first: `pathlib.Path`, `os.cpu_count()`, `sys.platform`,
    `shutil.which`; never assume a POSIX-only command exists.

**Self-evolution protocol**

13. Propose → normalise signature → look up the ledger → generate candidate →
    `SecurityGuard.scan_source()` → `write_patch()` (core paths refused) →
    run tests → record the lesson with `verified=True` and metrics → only then
    promote to a skill.
14. If the integrity check fails, evolution stops and reports. It never
    re-seals on its own to silence a drift warning.

**Verification**

15. Before any commit: `python -m compileall -q core vajrastra.py tests` and
    `python -m unittest discover -s tests -v` must both pass.
16. Tests use `unittest` and `tempfile`; they must never touch the repository's
    real `memory_bank/` (`VAJRASTRA_ROOT` points at a temp directory).

---

## 5. Roadmap

* **Phase 1 (this commit).** Security layer, memory engine, OS adapter,
  CLI/REPL, tests, sealed core.
* **Phase 2.** Planner + model-provider abstraction (still dependency-free at
  the core), autonomous patch loop wired to `write_patch`, richer GPU probes.
* **Phase 3.** Colab bridge worker: GitHub-API dispatch, job polling, metric
  ingestion back into `lessons.sqlite`.
* **Phase 4.** Fleet sync: signed snapshots, cross-device skill merge, Parrot
  OS security-tooling skills.
