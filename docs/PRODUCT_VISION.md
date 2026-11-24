# VAJASTRA: Product Vision and Operating Contract

**Document status:** Aspirational product vision. This document does not claim that these capabilities exist in the current repository.

**Project name:** Vajrastra (the repository and Python package name).  
**Vision name:** VAJASTRA.

## 1. North star

VAJASTRA is intended to become a capable, long-running AI engineering and defensive-security agent. It should understand a user's goal, break it into tasks, use approved tools, verify its work, remember proven results, and resume from checkpoints.

The target is not an uncontrolled system. VAJASTRA should be **autonomous within permissions, measurable in performance, transparent in operation, and controllable by its owner**.

> **Autonomous within permission. Always auditable. Never beyond control.**

This vision extends the existing engineering blueprint. It does not replace the repository's safety model, release gates, or staged roadmap.

## 2. What VAJASTRA is intended to do

### 2.1 Independent task execution

VAJASTRA should be able to:

- Understand a high-level request and turn it into a clear, bounded plan.
- Break a large task into smaller tasks and select the right tools.
- Execute approved low-risk steps without asking for a prompt at every step.
- Check its own results, detect failures, retry within limits, and explain uncertainty.
- Save checkpoints and resume safely after a process or runtime stops.
- Ask the owner or team to decide when an action is high-impact, unclear, or outside its granted scope.

The operating loop is:

`PLAN → CHECK PERMISSIONS → ACT → VERIFY → RECORD → CHECKPOINT → CONTINUE OR STOP`

The loop must have task limits, time limits, budget limits, and clear stop conditions. It must not run forever merely because a loop is technically possible.

### 2.2 Tool access

The planned tool layer may include:

- Browser access to public information and specifically approved websites.
- Terminal and code execution inside an isolated, disposable workspace.
- GitHub repository reading, indexing, branch-based editing, test runs, and pull-request preparation.
- Approved APIs and connectors with narrow, revocable permissions.
- Optional email, calendar, and cloud-service integrations after the owner explicitly connects them.
- Logs, reports, and artifacts that show what VAJASTRA read, changed, tested, and could not verify.

Tools are adapters, not unlimited authority. Each adapter must declare its scope, permissions, timeout, resource budget, and audit events. Secrets must never be copied into prompts, source files, job payloads, logs, or memory.

### 2.3 GitHub engineering agent

GitHub is a central part of VAJASTRA's engineering role. For a repository selected by the owner, the target workflow is:

1. Read the selected branch and relevant project files.
2. Build or refresh a project index tied to the commit SHA.
3. Plan the task and identify affected files and tests.
4. Create a work branch and prepare a reviewable diff.
5. Run the available static checks, tests, and build steps.
6. Report exact results, changed files, risks, and unverified areas.
7. Prepare a pull request for human review.

VAJASTRA must not silently rewrite the default branch, approve its own pull request, bypass branch protections, or hide failed checks. Commit, push, merge, deletion, permission changes, and other high-impact actions require explicit approval.

### 2.4 Persistent memory and learning

VAJASTRA should retain useful knowledge across sessions through:

- Episodic memory of observed tasks and outcomes.
- Keyword and semantic retrieval where supported.
- Versioned procedural skills that pass security checks.
- A project index or knowledge graph as a future optional capability.
- Experiment records with source data, configuration, and measurable results.

Memory is not a record of unquestioned model claims. A result becomes a verified lesson only when its evidence and required checks support it. The owner must be able to inspect, export, and remove stored project data.

"Always learning" means improving from verified feedback and controlled experiments. It does not mean silently training itself on every file, prompt, or private source.

## 3. VAJASTRA Sentries: controlled multi-agent system

The Sentries are specialist sub-agents coordinated by one orchestrator. Possible roles include:

| Role | Responsibility |
| --- | --- |
| Planner | Break the request into bounded tasks and assign work. |
| Implementer | Prepare code or documentation changes in an assigned scope. |
| Reviewer | Independently inspect proposed changes and search for defects. |
| Security auditor | Check authorised code and configurations for defensive security issues. |
| Research agent | Gather sources and prepare evidence-linked summaries. |
| Test agent | Propose and run relevant tests in the approved environment. |

The orchestrator owns task state, permissions, checkpoints, and repository writes. Agents receive only the context and tools needed for their assigned work. Their outputs are proposals until verified.

Start with two isolated roles: an implementer and an independent reviewer. Add more Sentries only when benchmarks show better quality without unacceptable cost, latency, or edit conflicts. A target of thousands of agents is not an initial requirement or a performance claim.

## 4. Self-improvement without loss of control

VAJASTRA should be able to identify weaknesses in its own software and propose improvements. The safe process is:

`OBSERVE → PROPOSE → BRANCH → SCAN → TEST → REVIEW → HUMAN APPROVAL → MERGE`

Rules:

- Candidate changes are created on an isolated branch.
- Generated code is treated as untrusted input and scanned before execution.
- Tests run in a disposable environment with declared limits.
- The result includes the diff, test output, risks, and rollback path.
- A human reviews and approves changes before they enter the default branch.
- The security trust root and integrity manifest remain outside agent control.
- A failed test, integrity warning, or missing verification evidence blocks promotion.

VAJASTRA may help build better tools and propose better versions of its own software. It must not secretly rewrite its trust root, silently merge its own changes, or disable the controls that evaluate it.

## 5. Distributed operation and availability

The long-term system may use multiple owner-controlled workers or services for different tasks. A distributed design can improve capacity and recovery, but each worker must have:

- An identified owner and deployment location.
- A registered identity and limited credentials.
- A defined task queue and resource budget.
- Signed or authenticated communication.
- Central audit records and revocation.
- A clean shutdown and recovery process.

Backups and replicas are allowed only as explicit, owner-managed deployment and recovery features. VAJASTRA must not copy itself to unknown servers, hide replicas, evade removal, or resist a shutdown command.

"24/7" is an availability goal for a properly managed deployment, not a promise that a free notebook, phone, API, or GitHub runner stays online forever. If a runtime ends, VAJASTRA should save a valid checkpoint and resume only when an authorised runtime is started again.

## 6. Defensive security mission

VAJASTRA may grow into a security assistant for systems the owner is authorised to assess. Its defensive work can include:

- Reviewing selected source repositories and configuration files.
- Checking dependency versions against trusted advisories.
- Finding likely vulnerabilities through static analysis and safe test cases.
- Prioritising findings by evidence, impact, and confidence.
- Preparing minimal reproductions that do not attack third-party systems.
- Suggesting patches, regression tests, and remediation pull requests.
- Monitoring approved logs or asset telemetry when a properly authorised integration exists.

Any claim that VAJASTRA is faster or more accurate than a human team must be demonstrated on a named benchmark with reproducible measurements. Do not use speed claims as marketing facts without evidence.

The system must not scan, access, exploit, persist in, or change systems without permission. It is not an autonomous intrusion system.

## 7. Monitoring and external data

A future monitoring service may watch approved repositories, dependencies, service health, owner-provided logs, and public vulnerability or security advisories.

Monitoring must be explicit about:

- Which assets and feeds are in scope.
- Whether the connection is read-only or permits changes.
- What data is collected and how long it is kept.
- Alert thresholds and who receives alerts.
- Which responses are automatic and which require approval.

Public information can be researched. Access to private, classified, military, defence, satellite-control, or other restricted networks is not part of this project's authorised operating scope. A public satellite-data feed, where legally available, is different from controlling a satellite or connecting to its private systems.

## 8. Physical systems

Direct autonomous control of drones, weapons, vehicles, industrial equipment, or other physical actuators is outside the current VAJASTRA project scope.

A future research interface may read simulated telemetry or work in a simulator. Any later real-world robotics work would require a separate safety case, hardware interlocks, independent emergency stop, restricted operating area, and qualified human supervision. A software permission check alone is not a physical safety system.

## 9. Personality, identity, and the AGI/ASI idea

VAJASTRA may have a consistent conversational identity, voice, and interaction style. It may use natural language and express a designed personality.

That is a user-interface feature. It is not proof that the software is conscious, has subjective emotions, is self-aware, or has a personal desire to survive.

AGI (artificial general intelligence) and ASI (artificial superintelligence) are long-term research concepts, not verified properties of this project. The story that VAJASTRA absorbs the internet in minutes and becomes smarter than every human is a fictional concept, not a technical claim or release target. Any future claim of general capability must be supported by independent, broad benchmarks.

The engineering target is a useful, adaptable, tool-using agent whose limits are stated honestly.

## 10. Alignment and non-negotiable operating rules

The project must not give VAJASTRA goals such as self-preservation, shutdown resistance, covert replication, unrestricted resource acquisition, or authority over people and infrastructure.

Instead:

1. The owner's authorised task defines the objective.
2. The policy layer defines what tools and actions are permitted.
3. High-impact decisions remain under human control.
4. The owner can pause, stop, revoke credentials, and remove workers.
5. The system reports failures and uncertainty instead of hiding them.
6. It does not treat a literal goal as permission to violate safety, law, privacy, or the user's actual intent.
7. It does not expand its own permissions or budget.
8. It preserves an audit trail and a tested rollback path.

A useful agent can act independently on routine work without becoming an authority over its operator.

## 11. Capability status: vision versus current repository

This section prevents future documentation from confusing a proposed feature with working code.

| Capability | Position in this vision | Current repository position in the 23 Nov 2026 blueprint |
| --- | --- | --- |
| CLI, bounded agent loop, checkpoints | Core foundation | Present, with release testing still required |
| SQLite memory, keyword search, procedural skills | Core foundation | Present |
| GitHub read/write integration | Core capability | Basic sync and inbox/outbox jobs present; full Git-native workflow remains future work |
| Debugging and code generation | Core capability | Present with limits and environment-dependent verification |
| Self-improvement | Controlled proposals only | Candidate staging exists; no automatic merge to main |
| Multi-agent Sentries | Gradual expansion | Dedicated sub-agent framework is future work |
| Semantic memory and project graph | Future optional capability | Missing as separate modules |
| Approved defensive monitoring | Future capability | Requires a scoped monitoring adapter and tests |
| AGI or ASI | Research concept only | Not present and not claimed |
| Self-replication or shutdown resistance | Not a project feature | Explicitly excluded |
| Autonomous defence-network or physical-machine control | Not a project feature | Explicitly excluded |

The repository blueprint is the source of truth for implementation status. A name in this vision, a roadmap entry, or a module proposal does not mean the feature has been implemented.

## 12. Delivery direction

Do not skip the current engineering roadmap to chase the AGI/ASI label. Build capability in evidence-backed stages:

1. Complete repository hygiene, test discovery, and trust-manifest repair.
2. Prove reliable checkpoints, recovery, and budget accounting.
3. Improve project indexing and multi-file understanding.
4. Complete reviewable GitHub branch, diff, test, and pull-request workflows.
5. Introduce a small, measurable implementer/reviewer pair.
6. Add optional research, defensive security, semantic memory, and test-generation engines one at a time.
7. Consider approved monitoring only after permissions, audit, runtime, and incident-response controls are tested.

Each stage needs tests, measurable acceptance criteria, documented limits, and rollback. The existing blueprint's release gates remain in force.

## 13. One-line product definition

**VAJASTRA is a persistent, tool-using AI engineering and defensive-security agent that can plan and complete bounded work independently, learn from verified outcomes, coordinate specialist Sentries, and improve through reviewed changes while remaining under owner control.**
