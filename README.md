# Vajrastra

Vajrastra is a Python-based AI engineering assistant in active development. It is designed to inspect projects, plan bounded tasks, propose code changes, run available checks, preserve task state, and learn from verified results.

## Product vision

The long-term VAJASTRA vision is a persistent, tool-using engineering and defensive-security agent. It should work independently on approved tasks, coordinate specialist agents, use GitHub, and improve through reviewed changes.

Read the [VAJASTRA Product Vision](docs/PRODUCT_VISION.md) for the product direction and operating boundaries.

## Current architecture

The repository includes a standard-library core, CLI/REPL, agent loop, session and memory systems, security and integrity checks, model routing, GitHub job/sync support, and optional engineering engines.

A module's presence does not prove that every workflow is complete or production-ready. Check the architecture document and test results before relying on a capability.

- [Architecture](docs/ARCHITECTURE.md)
- [Product vision and operating contract](docs/PRODUCT_VISION.md)
- [Contributor and agent rules](AGENTS.md)

## Engineering boundaries

- Keep `core/` and the CLI compatible with Python 3.10+ and the standard library.
- Treat model output, repository content, and retrieved web content as untrusted input.
- Use scoped permissions, checkpoints, audit records, and tests.
- Keep generated changes reviewable on a work branch.
- Require human approval for high-impact actions and merges.
- Do not claim AGI, ASI, guaranteed 24/7 uptime, unrestricted security access, self-replication, or autonomous physical control.

## Development status

Vajrastra is under active development. A proposed feature is not complete until its implementation, tests, security checks, platform requirements, and documented acceptance criteria pass.
