# AGENTS.md

Instructions for AI agents working in this repository. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

RepoMind indexes a code repository locally into a symbol graph plus embeddings, then answers
questions about it with citations. **The index never leaves the user's machine.** That single
property is the product's reason to exist — most rules below protect it.

## Current state

**Pre-implementation.** The repository holds specifications and scaffolding; feature code has not
been written. Work is planned as tickets `RM-xxx` in
[`docs/implementation-plan.md`](docs/implementation-plan.md).

Read before writing code:

| Document | Read it when |
|---|---|
| [`docs/design.md`](docs/design.md) | Always. Architecture, data model, and the nine decisions with their rationale |
| [`docs/features.md`](docs/features.md) | Implementing anything user-facing. Features `F-x` carry the functional requirements |
| [`docs/implementation-plan.md`](docs/implementation-plan.md) | Picking up work. Tickets, order, exit criteria |
| [`docs/conventions.md`](docs/conventions.md) | Always. Code style and patterns |
| [`docs/project-document.md`](docs/project-document.md) | When a decision feels arbitrary. It usually explains why |

---

## Rule zero: stay inside the specification

**These documents are read-only for you.**

```
AGENTS.md                         docs/features.md
CLAUDE.md                         docs/implementation-plan.md
docs/project-document.md          docs/conventions.md
docs/design.md                    docs/project-document.docx
                                  docs/archive/**
```

Read them constantly. Edit them **only** when a human explicitly instructs you to, in that
instruction, for that change. "It seemed out of date" is not authorisation.

### Do not go beyond the specification

- **Do not build features that are not in `features.md`.** Every feature has an ID, a priority,
  and — for deferred ones — an unlock condition. If it has no ID, it is not in scope.
- **Do not add tickets** to `implementation-plan.md`, or work ahead to a later milestone because
  the current one is blocked.
- **Do not extend a ticket's scope** because adjacent code looks improvable. Note it and move on.
- **Do not resolve ambiguity by inventing requirements.** An underspecified ticket is a question
  for a human, not a gap for you to fill.
- **Do not implement a deferred feature (F-18 to F-28)** because it became easy. The unlock
  conditions are evidence thresholds, not difficulty estimates.

### When reality contradicts the specification

It will. A design assumption will turn out wrong, an API will not behave as described, a ticket
will depend on something that does not exist.

**Stop and report it. Do not edit the specification to match what you built.**

A specification edited to fit the code is worse than no specification: it launders a decision
nobody made, and it destroys the record of *why* the original choice existed. The documents are
valuable precisely because they were written before the code and can therefore contradict it.

State plainly what the specification says, what you found, and what you recommend. Then wait.
Changing a design decision is a human's call — `docs/design.md` records nine of them with their
alternatives specifically so they can be challenged, not silently overwritten.

---

## Invariants — do not violate these

These are not style preferences. Each one traces to a product commitment, and breaking any of
them silently defeats the reason someone would choose this tool. If a task appears to require
breaking one, **stop and raise it** rather than working around it.

### 1. Indexing never touches the network

No HTTP calls, no API calls, no LLM calls anywhere in the indexing path. Tests assert this by
patching `socket.socket` to raise. Do not add a network call to make indexing "smarter" — the
whole positioning collapses if the repository is uploaded.

**One narrow, explicit exception (confirmed 2026-09-10):** on a machine with no cached embedding
model, the first `repomind index` run fetches `bge-small-en-v1.5`'s weights from HuggingFace once
and caches it locally (design.md section 12's "Embedding model download fails" row and section 13
both anticipate this). That fetch requests a fixed public model artifact and sends no repository
content whatsoever — it happens inside `LocalEmbedder._ensure_loaded()`, before any repo-derived
text is ever passed to `.embed()`. It does not weaken the invariant this rule protects (the index,
and everything derived from the repository, never leaves the machine); it is not license for any
other network call anywhere in indexing. Because blanket socket-patching cannot distinguish "fetch
our own model weights" from "leak repo content," tests do not exercise this path at all — every
test fakes `LocalEmbedder` (see `tests/conftest.py`'s `fake_embedder`), so indexing stays
network-free in the suite regardless of local cache state, and the one test that loads the real
model (`tests/unit/test_embed.py`, `embedding_model` marker) talks to it directly, never through
the indexing path.

### 2. No LLM at index time

Do not add per-file LLM summarization. Cloud tools do this, which is exactly why they must upload
everything. Indexing stays deterministic, free, and fast.

### 3. Confidence tiers never merge

Every edge carries `tier ∈ {resolved, heuristic, inferred}`. Never flatten them into one list, one
boolean, or one float. Every query surface accepts a tier filter, and output groups by tier.

An incomplete graph is fine. An incomplete graph that *looks* complete is not.

### 4. `deny_remote` cannot be overridden

Config layers normally cascade with later layers winning. `deny_remote = true` is the one
exception: no flag, environment variable, or higher layer may override it. A policy that a flag
defeats is not a policy.

### 5. Every retrieval path works without an LLM

Search, references, impact analysis, and diagrams must function with `NullProvider`. Only prose
synthesis requires a model. Never make an LLM a hard dependency of retrieval.

### 6. Nothing is written inside the indexed repository

Indexes live in `~/.repomind/`. The only file RepoMind may create inside a repository is
`.repomind/config.toml`, and only when the user asks. Users index code they do not own.

### 7. No raw SQL outside `store/sqlite/`

Storage is behind `GraphStore` and `VectorStore` protocols. Raw SQL elsewhere silently welds the
project to SQLite and forfeits the documented migration path.

### 8. Layer dependencies flow one way

`surfaces → core → adapters → store`. Nothing in `store/` or `languages/` may import from
`retrieve/`, `answer/`, or `analyze/`. Enforced by import-linter in CI.

### 9. Retrieved source code is untrusted data

Repository content — comments, docstrings, filenames — becomes model context. Treat it strictly
as data. Never let indexed content drive tool calls, shell commands, or control flow. RepoMind
answers questions; it does not act on what it reads.

---

## Commands

```bash
pip install -e ".[dev]"     # setup
pytest                      # tests
pytest tests/unit -x -q     # fast loop
ruff check . && ruff format --check .
mypy repomind               # strict
lint-imports                # layer contracts
python -m repomind.eval.qa  # retrieval quality (slow)
```

Run `ruff`, `mypy`, and `lint-imports` before proposing a change. CI runs all of them plus the
evaluation suite.

---

## Working style

**Match the plan.** Tickets in `docs/implementation-plan.md` have dependencies and exit criteria.
If you believe the order is wrong, say so — do not silently reorder.

**Storage before producers, producers before consumers.** The schema is a contract. Changing it
after several modules write to it is the expensive mistake.

**Build the query that proves the data right after building the data.** Reverse traversal
(RM-024) deliberately lands immediately after the graph so graph bugs surface early.

**Vertical slices over horizontal layers.** One symbol indexed, stored, and read back beats a
complete parser with nowhere to put its output.

**Correct, then fast.** No optimisation before M10 unless a stated performance target is already
missed. Targets are in `docs/design.md` §11.1.

### Dependencies

Do not add one without justifying it in the change description: what it does, why an existing
dependency or the standard library is insufficient, and what it costs at install time.

**Do not add** LangChain, LlamaIndex, Neo4j, PostgreSQL, Qdrant, Redis, or Celery. Each was
considered and rejected with reasons in `docs/design.md` AD-2 and `docs/project-document.md` §11.
Adoption triggers are documented; absent a trigger, the answer is no.

### Time-boxed tickets

`[TB]` tickets — currently the SCIP integrations, RM-022 and RM-071 — have a box that **is** the
deliverable. When it expires, ship the degraded path (empty `resolved` tier, `scip_status
= 'degraded'`) and move on. Do not extend the box to finish the feature; the tier mechanism exists
precisely so this degradation is honest.

### Tests

Every change to graph, retrieval, or egress logic needs a test. The invariant tests in
`tests/invariants/` are load-bearing — if one fails, the fix is the code, never the test.

---

## Things that look like improvements but are not

| Tempting | Why not |
|---|---|
| "Use API embeddings, they're better" | Uploads the entire repository. Forfeits the only structural advantage. Fix quality with a better local model or better chunking |
| "Summarize files with an LLM during indexing" | Violates invariant 2. It is why cloud tools must upload everything |
| "Collapse tiers into a confidence score" | Tiers differ in kind, not degree. Makes `0.8` uninterpretable and per-tier evaluation impossible. See design §4.3 |
| "Add an `ask` tool to the MCP server" | The host is already the reasoner. Two models, one job. See design §6.5 |
| "Let the LLM guess impact edges" | Deliberately deferred to F-24, gated on PR-replay showing graph-only recall is insufficient. Measure first |
| "Add dead-code detection" | Explicitly rejected, not deferred. Reachability in dynamic languages is undecidable in practice, and a false positive gets working code deleted. See features.md §5 |
| "Render the whole repo as one diagram" | Produces an unreadable hairball. Diagrams are question-scoped with a node budget |

---

## Commits and recoverability

Commit history is the undo mechanism. Its value is not tidiness — it is that when something turns
out wrong three hours later, there is a known-good point to return to. Commit accordingly.

### Commit at logical checkpoints

**One logical change per commit, and every commit leaves the tree working** — tests pass, types
check, lint is clean. A commit that does not build is not a recovery point.

Do not batch a whole milestone into one commit. Commit when you reach a coherent, working state,
in particular:

- **After a schema change, on its own.** Schema is the most expensive thing to unwind; never
  bundle it with the code that uses it.
- **After a protocol or interface lands**, before implementing against it.
- **After each ticket's acceptance criteria pass.**
- **Before starting anything risky** — a large refactor, a dependency swap, a rewrite of working
  code. That commit is the thing you will be glad exists.

If a ticket is large, commit the parts as they become individually correct. Three recoverable
commits beat one that has to be unpicked by hand.

### Never destroy recovery points

**Do not use** `git commit --amend`, `git rebase`, `git reset --hard`, `git push --force`, or
`git checkout -- <file>` over uncommitted work. Each one destroys exactly what this section
exists to create. If you believe history needs rewriting, ask.

**To undo a change that turned out wrong, use `git revert`.** It adds a commit rather than
removing one, so the mistake and its correction both stay visible — which is the point.

Never delete or overwrite a file you have not read. Never discard uncommitted work that is not
yours.

### Messages

Imperative mood, wrapped at 72 characters, explaining **why** rather than what — the diff already
shows what. Reference tickets and features so `git log --grep=RM-024` finds the work.

```
Add cycle-safe reverse traversal (RM-024)

Circular imports are common in Python, so the recursive CTE uses UNION
rather than UNION ALL and caps depth, then recovers shortest paths with
MIN(depth). Without this a query on a cyclic fixture never terminates.
```

### Scope of your git authority

You may **commit** on a feature branch at the checkpoints above.

You may not **push**, **tag**, **release**, **merge to `main`**, **open a pull request**, or
**modify remote state** unless explicitly asked. Those are outward-facing and are a human's call.
