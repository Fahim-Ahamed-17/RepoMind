## What and why

<!-- What changed, and the reason. The "why" matters more than the "what" -- the diff already
     shows what. -->

**Ticket:** RM-xxx
**Features:** F-x

## Verification

<!-- How you know it works. Name the tests, or the commands you ran on a real repository. -->

- [ ] Tests added or updated
- [ ] `ruff check` and `ruff format --check` clean
- [ ] `mypy repomind` clean
- [ ] `lint-imports` clean

## Invariants

Confirm none of these were weakened. See [AGENTS.md](../AGENTS.md).

- [ ] Indexing still makes no network calls
- [ ] No LLM introduced into the indexing path
- [ ] Confidence tiers are never merged or flattened
- [ ] `deny_remote` remains non-overridable
- [ ] Retrieval still works with no provider configured
- [ ] Nothing new is written inside the indexed repository
- [ ] No raw SQL outside `store/sqlite/`

## New dependencies

<!-- Delete if none. Otherwise: what it does, why the standard library or an existing dependency
     was insufficient, and its install cost. -->

## Documentation

- [ ] Design decisions that changed are updated in `docs/design.md` in this same commit
