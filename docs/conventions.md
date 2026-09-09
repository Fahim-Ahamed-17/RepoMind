# Code conventions

Companion to [`AGENTS.md`](../AGENTS.md). That file carries the invariants; this one carries style
and patterns. Where they conflict, the invariants win.

---

## Python

**Version.** 3.11+. Use `X | None` rather than `Optional[X]`, `list[X]` rather than `List[X]`,
`Self` from `typing`, and `StrEnum` where an enum is stringly-typed.

**Formatting.** ruff format, 100-column lines. Not negotiable and not worth discussing — the
formatter decides.

**Typing.** mypy strict across `repomind/`. Every public function is annotated. `Any` requires a
comment explaining why. Prefer `Protocol` over ABCs for the extension points (`LanguagePack`,
`Provider`, `GraphStore`, `VectorStore`, `Embedder`) — structural typing keeps adapters
independent of the core.

**Naming.**

| Thing | Convention |
|---|---|
| Modules, functions, variables | `snake_case` |
| Classes, protocols | `PascalCase` — protocols are not suffixed `Protocol` |
| Constants | `UPPER_SNAKE` |
| Private | Single leading underscore |
| Booleans | Read as a predicate: `is_resolved`, `has_scip`, `should_skip` |

Names carry domain meaning. `symbol_id` not `sid`, `qualified_name` not `qname` (except in SQL
aliases, where brevity is conventional). The vocabulary of `docs/design.md` §4 — symbol, edge,
tier, chunk, span — is the shared language; use those words for those things and no others.

---

## Structure

**One concept per module.** If a module needs "and" to describe it, split it.

**Dataclasses for data, functions for logic.** Avoid classes that exist only to hold a method.
`frozen=True` on anything that crosses a module boundary.

**Protocols live with their consumer, implementations with their technology.** `store/base.py`
defines `GraphStore`; `store/sqlite/graph.py` implements it. The core imports the protocol and
never the implementation.

**Constructor injection, no globals.** Nothing reaches for a module-level singleton connection or
config. Pass dependencies in — it is what makes the invariant tests possible.

---

## Errors

A small hierarchy in `repomind/errors.py`, all inheriting `RepoMindError`:

```python
class RepoMindError(Exception): ...

class IndexError(RepoMindError): ...        # indexing failed
class NotIndexedError(RepoMindError): ...   # repo has no index
class StaleIndexError(RepoMindError): ...   # index behind HEAD beyond threshold
class ProviderError(RepoMindError): ...     # LLM provider failed
class EgressDeniedError(RepoMindError): ... # deny_remote blocked a call
class SchemaVersionError(RepoMindError): ...# database schema mismatch
```

**Rules.**

1. Raise domain errors, not `ValueError` or bare `Exception`.
2. Every error message says what failed, why, and what the user can do. `"scip-python not found on PATH; install it or pass --no-scip"` — not `"SCIP failed"`.
3. **Never swallow an exception silently.** Degrade loudly: log, record status, tell the user.
4. Errors crossing to a surface become an exit code and a readable message, never a traceback.
5. `EgressDeniedError` is never caught and retried. It is a policy decision, not a transient fault.

**Degradation over failure.** The system prefers a less useful correct result to an error. SCIP
unavailable means an empty `resolved` tier, not a failed index. Provider unavailable means
retrieved context without prose, not a crash. Both paths must say what was lost.

---

## Logging

`structlog`, never `print`.

```python
log.info("index.file.parsed", path=path, symbols=len(symbols), lang=lang)
log.warning("scip.degraded", repo=repo.id, reason="timeout", seconds=120)
```

Dotted event names with structured fields, not interpolated sentences. `INFO` for milestones,
`DEBUG` for per-file detail, `WARNING` for degradation the user should know about, `ERROR` for
failures. **Never log file contents, prompts, or API keys** — logs are a bypass around the egress
guard if you are careless.

User-facing CLI output is `rich`, not logging. They are different channels for different readers.

---

## SQL

Lives only in `store/sqlite/`. Schema in `schema.sql`, versioned by a `schema_version` pragma.

Always parameterised — never f-strings or concatenation, including for internal values. Explicit
column lists, never `SELECT *`. Every query bounded: `LIMIT` on anything user-facing, depth caps
on recursive CTEs. Comment any non-obvious query with what it does and why it is shaped that way —
the recursive traversal in design §4.4 is the model.

Writes batch inside one transaction per file group, so an interrupted index leaves a consistent
database.

---

## Testing

**Layout mirrors the source tree**, plus `tests/invariants/` for the non-negotiables and
`tests/fixtures/` for the three synthetic repos (`simple/`, `cyclic/`, `messy/`).

**Names state the behaviour, not the method:**
`test_reverse_traversal_terminates_on_circular_imports`, not `test_traverse_2`.

**Arrange–act–assert**, visually separated. One behaviour per test — a test asserting five things
reports one failure and hides four.

**Fixtures over setup methods.** Repo fixtures are session-scoped and read-only; anything mutated
gets a fresh temporary workspace.

**No network in any test.** Blocked globally by an autouse fixture. Provider tests use recorded
responses; live-provider tests are marked `@pytest.mark.provider` and excluded from CI.

**Determinism.** Seed anything random, freeze the clock, never depend on filesystem or dict
ordering. A flaky test is treated as a failing test.

**Coverage floor 80%** on `repomind/`, excluding surfaces. Coverage is a floor, not a target —
90% coverage of trivial code with untested error paths is worse than the number suggests.

---

## Documentation

**Docstrings** on public functions: one summary line, then `Args`/`Returns`/`Raises` when
non-obvious. Skip them on self-evident private helpers — a docstring restating the signature is
noise.

**Comments explain why, never what.** If *what* is unclear, fix the code. Load-bearing comments
are the ones recording a decision:

```python
# UNION rather than UNION ALL: deduplicates rows so cyclic imports
# terminate. MIN(depth) below recovers the shortest path. See design.md §4.4.
```

**When code contradicts a document, one of them is wrong — fix both.** Design decisions live in
`docs/design.md` as `AD-x`; a change that invalidates one updates it in the same commit.

---

## Web UI (month 2)

TypeScript strict, no `any`. Function components with hooks. Tailwind utilities; a component only
when markup repeats three times. Server components by default, `"use client"` only where
interactivity requires it. All API types generated from the FastAPI OpenAPI schema — never
hand-written, or they drift.

---

## Git

**Branches:** `feat/RM-024-reverse-traversal`, `fix/...`, `docs/...`, `chore/...`.

**Commits:** imperative, wrapped at 72, explaining why. Reference `RM-xxx` and `F-x`. One logical
change per commit — formatting churn goes in its own.

**Pull requests** state what changed and why, which ticket and features, how it was verified, and
any new dependency with its justification.
