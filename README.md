# RepoMind

**Repository intelligence that runs on your machine.**

An open-source tool that builds a structured, queryable model of a codebase locally —
parsing it into a symbol graph, resolving cross-file relationships, and indexing it for
semantic search — so you can ask questions about an unfamiliar repository and get answers
that cite their sources and show which code paths they traversed.

Indexing is always local and requires no LLM. Your repository is never uploaded.

---

## Status

**Phase 1 complete — validation and product definition. Not yet in development.**

| Phase | State |
|---|---|
| 1 — Idea validation | ✅ Complete — verdict: *validated, conditional on scope discipline* |
| 2 — System design | ✅ Complete — [`docs/design.md`](docs/design.md), awaiting approval |
| 3 — Feature definition | ✅ Complete — [`docs/features.md`](docs/features.md), awaiting approval |
| 4 — Implementation plan | ✅ Complete — [`docs/implementation-plan.md`](docs/implementation-plan.md) |

No feature code exists yet. The repository holds specifications plus tooling and CI
scaffolding, ready for implementation to begin at ticket RM-010.

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) · AI agents: [AGENTS.md](AGENTS.md)

## Documents

| File | What it is |
|---|---|
| [`docs/project-document.md`](docs/project-document.md) | **Canonical project document.** Validation findings, positioning, scope, architecture direction, evaluation strategy, risks, success criteria |
| [`docs/design.md`](docs/design.md) | **System design (Phase 2).** Architecture, module breakdown, data model, interfaces, 9 architectural decisions with alternatives and trade-offs, security, failure handling, scalability |
| [`docs/features.md`](docs/features.md) | **Feature specification (Phase 3).** 28 features across MVP, post-MVP, and future, with purpose, user value, functional requirements, dependencies, technical implications, and priority |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | **Implementation plan (Phase 4).** Milestones with exit criteria, ~45 tickets with dependencies and estimates, testing and deployment strategy, risks, MVP-to-production evolution |
| [`docs/conventions.md`](docs/conventions.md) | Code conventions: style, structure, errors, logging, SQL, testing, git |
| [`AGENTS.md`](AGENTS.md) | Instructions for AI agents — invariants, commands, working style |
| [`docs/project-document.docx`](docs/project-document.docx) | Word version of the above, for sharing. *Generated from the Markdown — edit the Markdown, not this* |
| [`docs/archive/original-project-idea.docx`](docs/archive/original-project-idea.docx) | The original project idea, kept for provenance. Superseded, retained deliberately so the reasoning behind the changes stays visible |

## Licensing

**Apache-2.0 is the intended license** (see §13 of the project document), but no `LICENSE`
file has been added yet.

Before publishing, confirm your institution does not claim IP over student project work.
Some do, and it is not fixable after the fact.

## Attribution

A college club project. Original idea document by Sanjeev P S.
