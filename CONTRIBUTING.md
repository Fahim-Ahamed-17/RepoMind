# Contributing to RepoMind

Thanks for looking. This is a college club project, built in the open.

If you are an AI agent, read [AGENTS.md](AGENTS.md) instead — it is written for you and is more
specific.

---

## Current state

**Pre-implementation.** The repository holds specifications and scaffolding; feature code has not
been written yet. The most useful contributions right now are **critiques of the plan** rather
than code: if something in [`docs/design.md`](docs/design.md) is wrong, saying so before it is
built is worth far more than fixing it afterwards.

---

## Setup

```bash
git clone https://github.com/fahim-ahamed-17/repomind
cd repomind
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Optional, for the `resolved` confidence tier:

```bash
npm install -g @sourcegraph/scip-python @sourcegraph/scip-typescript
```

Everything works without these — you simply get `heuristic` edges only, which is the same
degraded path users get when SCIP is unavailable. Worth experiencing at least once.

---

## Before you write code

Read, in this order:

1. [`docs/design.md`](docs/design.md) — architecture and the nine decisions, each with the
   alternatives that were considered
2. [`docs/conventions.md`](docs/conventions.md) — style and patterns
3. [`AGENTS.md`](AGENTS.md) — the invariants section applies to humans equally

**Specifications are changed deliberately, never incidentally.** If you find that the code and a
specification disagree, say so in an issue rather than editing the document to match the code —
that destroys the record of why the original decision was made. `docs/design.md` records nine
decisions with the alternatives considered, precisely so they can be argued with. Maintainer
review is required on those paths.

**The invariants are not negotiable.** Indexing never touches the network. No LLM at index time.
Confidence tiers never merge. `deny_remote` cannot be overridden. Retrieval works without an LLM.
Nothing is written inside the indexed repository.

Each one traces to a product commitment, and each is enforced by a test in `tests/invariants/`.
If a change appears to require breaking one, open an issue rather than a pull request — that is a
design conversation, not a code review.

---

## Workflow

1. **Open an issue first** for anything non-trivial. Cheaper to disagree about an approach than
   about a diff.
2. Branch: `feat/RM-024-reverse-traversal`, `fix/...`, `docs/...`, `chore/...`.
3. Small commits, imperative mood, explaining **why**. Reference `RM-xxx` and `F-x`.
4. Run `ruff check . && mypy repomind && lint-imports && pytest` before pushing.
5. Open a pull request and fill in the template, including the invariants checklist.

### Definition of done

Acceptance criteria pass; unit tests cover new logic and integration tests cover new pipeline
stages; mypy strict passes; ruff is clean; import-linter contracts hold; user-visible behaviour
is documented; and **any new dependency is justified in the pull request** — what it does, why
existing dependencies were insufficient, and its install cost.

---

## What is likely to be accepted

- Bug fixes with a regression test
- Additional golden-set questions (`eval/datasets/`) — genuinely valuable and needs no
  architectural context
- Test fixtures exercising resolution edge cases, especially in `tests/fixtures/messy/`
- Documentation corrections
- Performance improvements **with a benchmark demonstrating them**

## What is unlikely to be accepted

- Features from the deferred list (F-18 to F-28) without evidence their unlock condition is met.
  These were deferred deliberately, and the conditions are stated
- Dead-code detection. **Rejected, not deferred** — see [`docs/features.md`](docs/features.md) §5
- Anything requiring the repository to be uploaded
- LangChain, LlamaIndex, Neo4j, PostgreSQL, Qdrant, Redis, or Celery. Each was considered and
  rejected with reasons in `docs/design.md` AD-2; adoption triggers are documented
- Large refactors without a prior issue

None of this is meant to discourage — it is meant to save you writing something that gets turned
down. If you think one of these is wrong, argue it in an issue. The reasoning is written down
precisely so it can be challenged.

---

## Licensing

This project is licensed under [Apache-2.0](LICENSE). By contributing, you agree that your
contributions will be licensed under the same terms.

Copyright is held collectively by the club and its contributors, not by any individual.

Add yourself to [CONTRIBUTORS.md](CONTRIBUTORS.md) in your first pull request.

---

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
