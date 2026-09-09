# RepoMind — Feature Specification (Phase 3)

| | |
|---|---|
| **Document** | Phase 3 — feature definition |
| **Depends on** | [`project-document.md`](project-document.md) (Phase 1) · [`design.md`](design.md) (Phase 2) |
| **Status** | Draft for approval |
| **Not included** | Implementation roadmap, milestones, task breakdown — those are Phase 4 |

---

## How to read this

**Priority scheme**

| | Meaning |
|---|---|
| **P0** | v1.0 is meaningless without it. Not cuttable |
| **P1** | v1.0 target. Cut only under schedule pressure, in the documented order below |
| **P2** | v1.1 (month 2) |
| **P3** | Conditional or future — gated on evidence, not scheduled |

**Cut order under pressure**, carried from Phase 1 §7.2: **F-12 (web UI) → F-8 + F-17 (impact analysis and its evaluation) → F-4 (TypeScript)**. The golden-set harness (F-16) is never cut.

**A note on detail levels.** Core features carry full functional requirements because they are being built. Post-MVP features carry requirements only where the design already determines them. Future features carry purpose and unlock conditions but deliberately **no invented requirements** — specifying features whose necessity is unproven is how scope creep enters a document.

---

## Feature index

### Core / MVP — v1.0 (month 1)

| ID | Feature | Priority | Depends on |
|---|---|---|---|
| F-1 | Repository indexing (Python) | P0 | — |
| F-2 | Confidence-tiered relationship graph | P0 | F-1 |
| F-3 | Incremental re-indexing | P0 | F-1 |
| F-4 | TypeScript / JavaScript support | P1 *(cut 3rd)* | F-1, F-2 |
| F-5 | Hybrid code search | P0 | F-1 |
| F-6 | Grounded question answering | P0 | F-5, F-13, F-14 |
| F-7 | Reverse-dependency queries | P0 | F-2 |
| F-8 | Change-impact analysis | P1 *(cut 2nd)* | F-2, F-7 |
| F-9 | Scoped architecture diagrams | P1 | F-2 |
| F-10 | Command-line interface | P0 | F-5, F-6, F-7 |
| F-11 | MCP server | P0 | F-5, F-7 |
| F-12 | Local web UI | P1 *(cut 1st)* | F-9, F-11 |
| F-13 | Pluggable LLM providers | P0 | — |
| F-14 | Egress control and policy | P0 | F-13 |
| F-15 | Repository registry and status | P0 | F-1 |
| F-16 | Golden-set evaluation harness | P0 *(never cut)* | F-5 |
| F-17 | Historical-PR replay evaluation | P1 *(with F-8)* | F-8 |

### Important post-MVP — v1.1 (month 2)

| ID | Feature | Priority | Unlock condition |
|---|---|---|---|
| F-18 | Documentation generation | P2 | F-16 shows retrieval is reliable |
| F-19 | Circular dependency detection | P2 | — |
| F-20 | Duplicate logic detection | P2 | — |
| F-21 | CI and git-hook integration | P2 | — |
| F-22 | Published evaluation benchmark | P2 | F-16, F-17 complete |

### Optional / future

| ID | Feature | Priority | Unlock condition |
|---|---|---|---|
| F-23 | Additional language packs | P3 | Users request a specific language |
| F-24 | Inferred-tier impact edges | P3 | F-17 shows graph-only recall is too low |
| F-25 | ANN vector backend | P3 | Measured query p95 > 500 ms |
| F-26 | VS Code extension | P3 | Demand from users of neither Claude Code nor Cursor |
| F-27 | Cross-repository queries | P3 | Repeated user request |
| F-28 | Hosted multi-user deployment | P3 | Local adoption demonstrably exists |
| — | Dead-code detection | **Rejected** | See §5 |

---

# 1. Core / MVP features

## F-1 — Repository indexing (Python)

**Purpose.** Turn a repository into a queryable local model: symbols, relationships, and embeddings persisted to one SQLite file.

**User value.** One command converts an unfamiliar codebase into something interrogable. No account, no upload, no configuration, no waiting on a server. This is the feature that makes every other feature possible, and the first thing a new user experiences.

**Functional requirements.**
1. `repomind index [PATH]` indexes a local path; a git URL is cloned to a temp location first.
2. File discovery respects `.gitignore`, skips known vendor and build directories (`node_modules`, `.venv`, `dist`, `build`, `site-packages`), and skips files above a configurable size cap (default 1 MB).
3. Extracts symbols of kind `module`, `class`, `function`, `method`, `variable`, each with qualified name, line span, signature, and docstring.
4. Extracts relationships of kind `imports`, `calls`, `inherits`, `references`, `defines`.
5. Records the indexed commit SHA against the repo.
6. Reports progress during indexing and a summary on completion: files, symbols, edges by tier, elapsed time.
7. **Makes no network connection at any point**, verified by test.
8. Completes a 5,000-file Python repository in under 5 minutes on a typical laptop.
9. Survives interruption: a re-run resumes rather than restarting.
10. `--no-scip` skips the SCIP subprocess entirely, for untrusted repositories.

**Dependencies.** None — this is the foundation.

**Technical implications.** Tree-sitter grammar and query files per language; SCIP subprocess with timeout (design AD-9); process pool for parsing; batched transactions per file group; `index_run` checkpointing. Requirement 7 constrains the whole module tree, and requirement 10 exists because SCIP indexers execute repository code (design §9.3).

**Priority.** P0.

---

## F-2 — Confidence-tiered relationship graph

**Purpose.** Record *how* each relationship was determined, and never merge relationships of different provenance.

**User value.** The user can distinguish a fact from a guess. Impact analysis reports "4 definitely affected, 9 possibly affected" rather than one undifferentiated list that invites misplaced confidence. This is what makes an incomplete graph safe to rely on.

**Functional requirements.**
1. Every edge carries `tier ∈ {resolved, heuristic, inferred}`.
2. SCIP resolution produces `resolved`; name and scope matching produces `heuristic`. **v1.0 emits no `inferred` edges** — the tier exists in the schema but is unused until F-24.
3. Every query surface accepts a tier filter. Default includes `resolved` and `heuristic`.
4. All output groups results by tier and never presents them merged.
5. `repomind status` reports edge counts per tier and the SCIP status for the repo.
6. When SCIP is unavailable or times out, the `resolved` tier is empty and this is stated explicitly at index time and in `status` — never silently.

**Dependencies.** F-1.

**Technical implications.** The `tier` column and its indices (design §4.2–4.3). Cross-cutting: every consumer in `retrieve/`, `analyze/`, and `answer/` must thread tier through rather than flattening it. **Cannot be retrofitted** — adding tiers later would mean re-deriving the entire graph and changing every query.

**Priority.** P0. Its presence is what allows F-1's SCIP integration to be optional without becoming dishonest.

---

## F-3 — Incremental re-indexing

**Purpose.** Keep the index current with the code at a cost proportional to what changed, not to repository size.

**User value.** The index tracks the repository instead of drifting from it — which is the failure mode of the stale documentation this project exists to replace. Re-indexing after a normal commit is fast enough not to be avoided.

**Functional requirements.**
1. Re-indexing compares the stored SHA to HEAD and processes only changed files plus their direct graph neighbours.
2. File-level change detection uses content hash, so a touched-but-unmodified file is skipped.
3. Deleted files cascade-remove their symbols, edges, and chunks.
4. A typical commit re-indexes in under 5 seconds.
5. `repomind status` reports drift when the index is behind HEAD.
6. Answers footer the indexed SHA, and `ask` warns when drift exceeds a threshold.
7. Repositories without git still index, with incremental updates disabled and the reason stated.

**Dependencies.** F-1.

**Technical implications.** `blob_sha` on `file` drives invalidation; foreign-key cascades handle deletion; neighbour recomputation is required because a changed file can invalidate edges *pointing into* it from unchanged files.

**Priority.** P0. Without it the tool is a one-shot snapshot, which recreates the staleness problem.

---

## F-4 — TypeScript / JavaScript support

**Purpose.** Extend indexing, resolution, and retrieval to the second language.

**User value.** Roughly doubles the repositories the tool is useful on. Many of the large, confusing codebases the primary user encounters are TypeScript.

**Functional requirements.**
1. `.ts`, `.tsx`, `.js`, `.jsx` files are parsed and indexed.
2. Symbols extracted: modules, classes, functions, methods, arrow-function consts, interfaces, types.
3. `scip-typescript` provides `resolved` edges; heuristic fallback applies identically to F-2.
4. Language detection is automatic; mixed-language repositories index both.
5. Retrieval quality on the golden set is comparable to Python (F-16).

**Dependencies.** F-1, F-2. Adding a language must require touching only `languages/typescript/` — if it doesn't, the `LanguagePack` protocol is wrong and that is the finding.

**Technical implications.** Second tree-sitter grammar plus query set; second SCIP integration with the same time-box discipline. Node toolchain becomes an optional runtime dependency, detected and degraded rather than required.

**Priority.** P1 — third in the cut order. The first SCIP integration proves the pattern, so this should run faster than F-1's; if it does not, that is the signal to cut.

---

## F-5 — Hybrid code search

**Purpose.** Find relevant code by meaning and by exact identifier, in one ranked result set.

**User value.** "Where is rate limiting handled" works, and so does searching for `AuthMiddleware` by name. Semantic search alone fails the second case; keyword search alone fails the first.

**Functional requirements.**
1. `repomind search "query"` returns ranked chunks with file, line span, and enclosing symbol.
2. Vector similarity and FTS5 BM25 results are fused by Reciprocal Rank Fusion (design AD-8).
3. Results are filterable by language, path prefix, and symbol kind.
4. Works with **no LLM configured** — this is run mode D.
5. Query p95 under 500 ms on a 5,000-file repository.
6. Degrades correctly when one retriever returns nothing.

**Dependencies.** F-1.

**Technical implications.** `sqlite-vec` and FTS5 tables kept in sync with `chunk` inside the same transaction. RRF needs no score normalisation, so no retuning when the embedding model changes.

**Priority.** P0. It is both a user-facing feature and the seed step of F-6.

---

## F-6 — Grounded question answering

**Purpose.** Answer natural-language questions about the repository with claims tied to verifiable source locations.

**User value.** The headline capability — ask what you would ask a colleague, and get an answer you can check rather than one you must trust. Verifiability is the differentiator against tools that return fluent, unfalsifiable prose.

**Functional requirements.**
1. `repomind ask "question"` returns prose with inline citations.
2. Every citation resolves to a real `file:line` span within the retrieved context.
3. **Citations that fail validation are dropped**, and the answer is flagged as partially unverified rather than presented as sound.
4. The response includes the traversal path — which edges were followed, and at which tier.
5. Retrieval is a bounded loop: seed, then at most 2 expansion hops (configurable).
6. Token usage and estimated cost are reported per query.
7. The answer footers the indexed SHA.
8. With no provider configured, returns retrieved context and citations without prose — mode D, not an error.

**Dependencies.** F-5, F-13, F-14.

**Technical implications.** Numbered context blocks in the prompt; a validator mapping cited block IDs back to spans; `unverified_claims` as part of the `Answer` type so callers cannot render an answer without also receiving what failed to check out (design §6.2). Requirement 3 is the mechanism behind Principle 2, not a nicety.

**Priority.** P0.

---

## F-7 — Reverse-dependency queries

**Purpose.** Answer "what depends on this" by direct graph traversal.

**User value.** The question a developer actually asks before changing anything, and the one that is genuinely tedious by hand. This is also the honest subset of impact analysis: pure traversal, no prediction, no fabrication risk.

**Functional requirements.**
1. `repomind refs SYMBOL` lists callers and referrers of a symbol.
2. `--depth N` walks transitively; default 1, capped at 4.
3. `--tier` filters by confidence tier; results are grouped by tier.
4. Each result shows the evidence location where the relationship is observable in source.
5. Cycles terminate — circular imports must not hang the query.
6. Symbols are addressable by qualified name or by `file:line`.
7. Requires no LLM.

**Dependencies.** F-2.

**Technical implications.** The recursive CTE in design §4.4, with `UNION` for cycle safety and `MIN(depth)` for shortest path. The `idx_edge_dst` index makes this the hot path it needs to be.

**Priority.** P0. It is the feature that most directly justifies building a graph at all.

---

## F-8 — Change-impact analysis

**Purpose.** Given a set of changes, report what else is likely affected and which tests exercise it.

**User value.** The differentiator. Answers "what might I break" before review rather than after CI fails, and narrows a slow test suite to the tests that actually touch the change.

**Functional requirements.**
1. `repomind impact` accepts the working-tree diff, `--staged`, or `--range A..B`.
2. Changed *lines* map to changed *symbols* by span overlap — a one-line edit inside one method seeds one symbol, not the whole file.
3. Reverse traversal is depth-bounded (default 3) and tier-filtered.
4. Output is grouped: **definitely affected** (resolved tier), **possibly affected** (heuristic), and **affected tests**.
5. Each result shows the path from the change to the consequence.
6. **No LLM is involved** — the result is deterministic and reproducible.
7. `--tests-only` emits a test selection list suitable for piping to a test runner.
8. Test association uses both convention (`test_*.py`, `*_test.py`, `tests/`) and `tests` edges.

**Dependencies.** F-2, F-7.

**Technical implications.** Diff parsing via GitPython; span-overlap mapping; reuses F-7's traversal with different seeding and ranking. Requirement 6 is what makes F-17's evaluation meaningful — a nondeterministic predictor cannot be scored cleanly.

**Priority.** P1 — second in the cut order. F-7 already delivers the trustworthy core; this adds diff-awareness and test selection on top.

---

## F-9 — Scoped architecture diagrams

**Purpose.** Render a *question-scoped* subgraph, never the whole repository.

**User value.** "Show me the authentication flow" produces something readable. Whole-repository diagrams produce a hairball that answers nothing — the failure mode that Phase 1 §7.6 documents.

**Functional requirements.**
1. `repomind graph SYMBOL --radius N` emits a subgraph centred on a symbol.
2. Node budget is enforced (default 40); over-budget subgraphs collapse least-relevant nodes into module clusters rather than truncating arbitrarily.
3. Mermaid output for CLI; JSON for programmatic and web consumption.
4. Edge styling distinguishes confidence tiers visually.
5. Diagrams are reachable from a question, not only from a symbol name.
6. Requires no LLM.

**Dependencies.** F-2.

**Technical implications.** Subgraph selection is the hard part, not rendering — relevance scoring by graph distance, edge kind, and tier. Budget enforcement must be in the selection step, since a renderer cannot make 1,000 nodes legible.

**Priority.** P1.

---

## F-10 — Command-line interface

**Purpose.** The primary surface for v1.0.

**User value.** Works everywhere, scripts cleanly, needs no editor integration or running server.

**Functional requirements.**
1. Commands: `index`, `ask`, `search`, `refs`, `impact`, `graph`, `list`, `status`, `serve`.
2. `--json` on every read command for scripting.
3. Human-readable default output with syntax-highlighted, clickable `file:line` citations.
4. Exit codes distinguish success, no-results, and error.
5. `--dry-run` on any command that would call a provider (F-14).
6. No server or daemon required for any command except `serve`.

**Dependencies.** F-5, F-6, F-7.

**Technical implications.** Typer over the library API directly (design AD-1). Requirement 6 is the practical consequence of the library-first architecture and is what makes the install a single step.

**Priority.** P0.

---

## F-11 — MCP server

**Purpose.** Expose retrieval primitives to agentic editors so the host model does the reasoning.

**User value.** For anyone already using Claude Code or Cursor, RepoMind becomes available inside their existing workflow with **no API key, no local model, and no additional cost** — the host's model does synthesis. This is run mode A, and it resolves the tool-switching contradiction identified in Phase 1.

**Functional requirements.**
1. Tools exposed: `search_code`, `get_symbol`, `find_references`, `impact_of_change`, `render_subgraph`, `repo_status`.
2. **No `ask` tool** — the host is the reasoner; offering synthesis would pay two models for one job.
3. Tool results carry citations and tier information as structured data.
4. `repomind serve --mcp` starts it; binds `127.0.0.1` only.
5. Works without any provider configured, since it never synthesises.
6. Setup requires no manual configuration beyond registering the server.

**Dependencies.** F-5, F-7.

**Technical implications.** Thin adapter over the library (design §6.5). Requirement 5 falls out of the architecture rather than needing special handling.

**Priority.** P0. Likely the highest-adoption surface, and the cheapest to build.

---

## F-12 — Local web UI

**Purpose.** Visual exploration: interactive graphs, search, and citation-linked answers.

**User value.** Some questions are spatial. An interactive dependency graph communicates structure in a way a terminal cannot, and it is the surface that best demonstrates the tool to someone watching.

**Functional requirements.**
1. `repomind serve --http` starts a local server and opens a browser.
2. Search with results linked to source.
3. Interactive React Flow rendering of F-9 subgraphs: pan, zoom, click-to-expand.
4. Question answering with citations navigable to code.
5. Read-only — no editing, no configuration through the UI.
6. Binds `127.0.0.1` only, no authentication and therefore no remote binding.

**Dependencies.** F-9, F-11 (reuses the FastAPI layer).

**Technical implications.** Next.js, React, TypeScript, Tailwind, React Flow. A read-only client over an API that already exists by the time this is built — which is why it costs 4 days rather than 8.

**Priority.** P1 — **first in the cut order**. Frontend polish overruns more reliably than anything else, and the CLI plus MCP already deliver the core value.

---

## F-13 — Pluggable LLM providers

**Purpose.** Let the user supply whatever intelligence they have, or none.

**User value.** Removes the hardware barrier without uploading the repository. A student on an 8 GB laptop, an engineer at a company that forbids third-party AI, and someone with an OpenAI key all get a working tool.

**Functional requirements.**
1. Four modes supported: MCP host (A), user's API key (B), local model (C), none (D).
2. Adapters: OpenAI-compatible (the documented default), Anthropic, Ollama, Null.
3. Provider selectable per invocation, per repo, or globally.
4. Bring-your-own-key only — no RepoMind-hosted proxy, no account, no sign-up.
5. Keys resolve from OS keyring, then environment. **Never written to any config file the tool creates.**
6. Provider errors retry with backoff, then degrade to mode D rather than failing outright.

**Dependencies.** None.

**Technical implications.** A narrow `Provider` protocol. The OpenAI-compatible adapter covers OpenRouter, Together, Groq, vLLM, and LM Studio through `base_url` alone, so two adapters reach nearly every provider.

**Priority.** P0.

---

## F-14 — Egress control and policy

**Purpose.** Make the privacy claim checkable rather than asserted.

**User value.** The user can see exactly what would leave their machine before it does, and a team lead can commit a policy that cannot be overridden by a flag. This is what makes "your repository is never uploaded" a verifiable property rather than marketing.

**Functional requirements.**
1. `--dry-run` prints the exact payload — files, line ranges, token count — and sends nothing.
2. `.repomind/config.toml` supports per-repo provider policy and is committable to git.
3. **`deny_remote = true` cannot be overridden by any higher config layer, including command-line flags.**
4. Redaction strips `.env` contents, gitignored file content, and high-entropy strings matching known key formats — applied to the assembled payload, so nothing added later can bypass it.
5. Token count and estimated cost are shown per query, with a session running total.
6. Retrieved source code is treated strictly as data; the synthesis prompt marks context as untrusted and RepoMind takes no action based on its contents.

**Dependencies.** F-13.

**Technical implications.** A single choke point in `llm/egress.py` that every provider call passes through. Requirement 3 is the one deliberate asymmetry in config layering (design §6.4) — a policy a flag can defeat is not a policy.

**Priority.** P0. The positioning depends on it, and retrofitting a choke point after providers are wired in is far harder than building one.

---

## F-15 — Repository registry and status

**Purpose.** Make centrally-stored indexes discoverable and their freshness legible.

**User value.** Answers "which repos have I indexed", "where did the index go", "is it current", and "how much disk is this using". Central storage (design AD-5) is the right call but costs discoverability; this is the repayment.

**Functional requirements.**
1. `repomind list` shows indexed repositories: path, SHA, index date, size on disk.
2. `repomind status [PATH]` shows SHA drift, SCIP status, symbol and edge counts by tier, and last index duration.
3. `repomind remove PATH` deletes an index.
4. The registry survives a repository being moved or deleted, and reports such entries as stale rather than erroring.
5. Total disk usage is reportable across all indexes.

**Dependencies.** F-1.

**Technical implications.** A registry file in `~/.repomind/` mapping path hashes to metadata. Requirement 4 matters because directories move, and a registry that breaks when they do is worse than none.

**Priority.** P0 — not glamorous, but central storage is unusable without it.

---

## F-16 — Golden-set evaluation harness

**Purpose.** Measure retrieval quality objectively and repeatably.

**User value.** Indirect but decisive. It is the difference between "this seems to work" and published numbers a user can check before trusting the tool. It is also the only way to know whether heuristic edges help or add noise.

**Functional requirements.**
1. Questions and expected source files stored as YAML under `eval/datasets/`.
2. 60–80 questions across 4–5 real repositories, covering Python and TypeScript.
3. Default runner measures **retrieval only** — no LLM — so results are reproducible and free.
4. Metrics: file-level precision and recall at k, reported **per confidence tier**.
5. Baselines run alongside: vector-only, FTS-only, and grep-plus-LLM.
6. Opt-in end-to-end mode measures citation validity with a provider configured.
7. Runs in CI on a fixed corpus, so retrieval regressions fail the build.

**Dependencies.** F-5.

**Technical implications.** Requirement 3 is what makes CI execution affordable. Requirement 5 is what tests the central hypothesis — that graph-augmented retrieval beats pure vector RAG — rather than assuming it.

**Priority.** P0. **Never cut**, regardless of schedule pressure. A tool with published numbers and no GUI is credible; the reverse is not.

---

## F-17 — Historical-PR replay evaluation

**Purpose.** Score impact analysis against ground truth that already exists in git history.

**User value.** Turns "our impact analysis is good" into a measured recall figure. Almost nothing in this space publishes such a number.

**Functional requirements.**
1. For a merged PR: check out the parent commit, index into a throwaway workspace, seed with one changed file, predict the rest.
2. Compare predictions against the actual changeset; report precision and recall **per tier**.
3. Baseline comparison against "files in the same directory".
4. Runs unattended over hundreds of PRs; no human labelling.
5. Results summarised in a report suitable for the README.

**Dependencies.** F-8.

**Technical implications.** Requires `Repo.index()` to accept an arbitrary destination — the main index stores a single SHA, so historical indexing needs disposable workspaces (design §10.2). This is why that parameter exists in the API.

**Priority.** P1 — coupled to F-8. If impact analysis is cut, this is cut with it.

---

# 2. Important post-MVP features — v1.1

## F-18 — Documentation generation

**Purpose.** Generate structured documentation — module overviews, onboarding guides, architecture summaries — from the index.

**User value.** Completes the "understand this repository" story for readers who want an orientation before asking specific questions.

**Requirements determined by design.** Regeneration must run in CI, or generated docs inherit the staleness problem this project exists to solve. Output must carry citations like any other answer. Generation is per-module and incremental, since regenerating everything on every commit is too expensive.

**Unlock condition.** F-16 shows retrieval is reliable. This was deferred in Phase 1 precisely so the decision would rest on measured numbers rather than optimism.

**Priority.** P2. The highest-value post-MVP feature.

---

## F-19 — Circular dependency detection

**Purpose.** Report import and call cycles.

**User value.** Cycles are a concrete, actionable code-health signal, and circular imports are a common source of confusing behaviour in Python.

**Requirements determined by design.** Cycle detection on the existing graph — nearly free once the graph exists. Must report per tier, since a cycle inferred from heuristic edges may not be real.

**Priority.** P2.

---

## F-20 — Duplicate logic detection

**Purpose.** Identify structurally similar code across the repository.

**User value.** Surfaces refactoring candidates and helps a newcomer recognise that three modules are doing the same thing.

**Requirements determined by design.** Uses existing chunk embeddings — high pairwise similarity between chunks in different files. No new model. Threshold must be tunable, since the useful setting varies by codebase.

**Priority.** P2.

---

## F-21 — CI and git-hook integration

**Purpose.** Keep the index current automatically.

**User value.** The index tracks the code without anyone remembering to re-run anything.

**Requirements determined by design.** Optional post-commit hook installable via `repomind hook install`; a GitHub Action for CI-side indexing and documentation refresh; impact analysis as a PR check reporting affected files and suggested tests.

**Priority.** P2.

---

## F-22 — Published evaluation benchmark

**Purpose.** Package the methodology, golden set, and replay harness as a standalone open benchmark for repository-QA systems.

**User value.** Serves the wider ecosystem, and — pragmatically — drives distribution better than any feature would. Almost no tool in this space publishes evaluation at all.

**Requirements determined by design.** This is packaging, not new engineering: the work exists by day 30. Needs a separate repository, documented methodology, a reproducible runner, and a submission format for other tools.

**Priority.** P2.

---

# 3. Optional / future features

Deliberately specified at purpose-and-condition level only. Writing functional requirements for features whose necessity is unproven is how scope creep enters a document.

| ID | Feature | Purpose | Unlock condition |
|---|---|---|---|
| **F-23** | Additional language packs (Go, Java, Rust) | Broaden reach | Python and TypeScript both score well on F-16, **and** users request a specific language. Each is another SCIP integration — cheaper by the third, not free |
| **F-24** | Inferred-tier impact edges | Catch coupling the graph cannot see: config-driven wiring, dynamic dispatch, implicit contracts | F-17 shows graph-only recall is unacceptably low. The `inferred` tier already exists in the schema, so this is activation rather than redesign — and it must be measured against the graph-only baseline it would replace |
| **F-25** | ANN vector backend | Restore query latency at scale | **Measured** p95 above 500 ms — roughly 800k chunks. Isolated behind `VectorStore`, so this is a swap |
| **F-26** | VS Code extension | Reach developers using neither Claude Code nor Cursor | Evidence such users exist and want it. MCP already covers the agentic editors |
| **F-27** | Cross-repository queries | Answer questions spanning multiple services | Repeated user request. Central storage (AD-5) already keeps this possible without a migration |
| **F-28** | Hosted multi-user deployment | Serve teams who will not self-host | Local adoption demonstrably exists first. Note this conflicts with the zero-upload positioning and would need a deliberate answer |

---

# 4. Cross-cutting requirements

These are not features but hold across all of them.

| Requirement | Applies to |
|---|---|
| Indexing makes no network calls, verified by test | F-1, F-3, F-4 |
| Every retrieval feature works with no LLM configured | F-5, F-7, F-8, F-9 |
| Results never merge confidence tiers | F-2, F-7, F-8, F-9 |
| Every read command supports `--json` | F-10 |
| Errors degrade to a less useful correct result, never a stack trace | All |
| Nothing is written inside the indexed repository unless the user creates `.repomind/config.toml` | F-1, F-3, F-15 |

---

# 5. Explicitly rejected

## Dead-code detection

**Rejected, not deferred.**

It would fit naturally alongside F-19 and F-20, and it is a commonly requested capability. But in dynamically-typed languages, reachability is undecidable in practice: reflection, dynamic imports, string-keyed dispatch, framework entry points, and test-only usage all produce code that is genuinely reachable but statically invisible.

A tool that confidently reports live code as dead causes real harm — someone deletes it. That directly contradicts Principle 3, *never fake certainty*. The confidence-tier mechanism does not rescue this case, because the failure is not low confidence but **absent evidence**, and an absence cannot be tiered.

If it is ever revisited, the honest form is inverted: report **observed usage** with evidence, and let the user draw the conclusion about what is unused.

---

## Phase gate

Phase 3 is complete. **No implementation roadmap, milestones, task breakdown, or development sequencing has been produced** — those are Phase 4.
