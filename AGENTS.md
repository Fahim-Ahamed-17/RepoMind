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

## Invariants — do not violate these

These are not style preferences. Each one traces to a product commitment, and breaking any of
them silently defeats the reason someone would choose this tool. If a task appears to require
breaking one, **stop and raise it** rather than working around it.

### 1. Indexing never touches the network

No HTTP calls, no API calls, no LLM calls anywhere in the indexing path. Tests assert this by
patching `socket.socket` to raise. Do not add a network call to make indexing "smarter" — the
whole positioning collapses if the repository is uploaded.

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

## Commits

Imperative mood, explain **why** rather than what, wrap at 72 characters. Reference tickets and
features where relevant: `RM-024`, `F-7`.

```
Add cycle-safe reverse traversal (RM-024)

Circular imports are common in Python, so the recursive CTE uses UNION
rather than UNION ALL and caps depth, then recovers shortest paths with
MIN(depth). Without this a query on a cyclic fixture never terminates.
```

Do not commit unless asked. Do not push, tag, or release.
