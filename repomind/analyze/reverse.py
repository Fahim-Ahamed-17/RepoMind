"""Reverse-dependency queries: "what depends on this" (F-7).

RM-024. A thin, storage-agnostic layer over
:meth:`repomind.store.base.GraphStore.reverse_dependencies` -- the
recursive-CTE traversal itself lives there (AGENTS.md invariant 7: no raw
SQL outside ``store/sqlite/``); this module resolves a user-facing symbol
reference, applies F-7's stated bounds, and shapes the result for
presentation. Requires no LLM (F-7 requirement 7): every function here
takes a :class:`~repomind.store.base.GraphStore` and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from repomind.model import EdgeKind, Symbol, Tier
    from repomind.store.base import GraphStore

#: F-7 requirement 2: "default 1, capped at 4".
DEFAULT_DEPTH = 1
MAX_DEPTH = 4


@dataclass(frozen=True, slots=True)
class ReferenceHit:
    """One caller/referrer of the queried symbol, at its shortest distance
    from it. See ``GraphStore.reverse_dependencies`` for exactly what
    "shortest" and "one per kind" mean here.
    """

    source: Symbol
    """The dependent symbol -- e.g. the caller, for a CALLS hit."""

    kind: EdgeKind
    tier: Tier
    depth: int
    evidence_path: str | None
    """Repo-relative path where this relationship is observable in
    source (F-7 requirement 4), or ``None`` if the edge has no recorded
    evidence file (should not normally happen for a resolver-produced
    edge, but the schema allows it -- ``ON DELETE SET NULL``)."""

    evidence_line: int | None


@dataclass(frozen=True, slots=True)
class ReferencesResult:
    target: Symbol
    hits: list[ReferenceHit]
    """Sorted by depth, then tier, then source qualified name -- a stable,
    predictable order for both the CLI and any future JSON output, not an
    accident of SQL row order."""

    def by_tier(self) -> dict[Tier, list[ReferenceHit]]:
        """Grouped by tier (F-7 requirement 3), never merged (AGENTS.md
        invariant 3) -- callers render each group separately rather than
        flattening this back into one list.

        Deliberately not ``itertools.groupby``: ``hits`` is sorted
        depth-first (its own docstring), so the same tier's entries are
        not necessarily consecutive across different depths, and
        ``groupby`` only merges *consecutive* runs -- it would silently
        produce more than one group per tier and, fed straight into a
        dict comprehension, drop everything but the last one. A plain
        pass over the already-sorted list has no such requirement and
        keeps each tier's entries in their existing depth/name order.
        """
        result: dict[Tier, list[ReferenceHit]] = {}
        for hit in self.hits:
            result.setdefault(hit.tier, []).append(hit)
        return result


def resolve_symbol_ref(store: GraphStore, repo_id: int, ref: str) -> Symbol | None:
    """Resolve a user-typed ``SYMBOL`` argument: a qualified name
    (``pkg.mod.Class.method``) or a ``path/to/file.py:line`` location
    (F-7 requirement 6). ``path`` must be repo-relative with forward
    slashes, matching ``File.path`` -- the same form ``repomind index``'s
    own output already uses.

    Tried in this order: if ``ref`` contains a colon and the text after it
    is a bare line number, treat it as ``file:line`` first (a qualified
    Python name can never itself contain a colon, so this is unambiguous);
    otherwise, or if that lookup finds nothing, fall back to an exact
    qualified-name match.
    """
    path, sep, line_str = ref.rpartition(":")
    if sep and line_str.isdigit():
        file = store.get_file_by_path(repo_id, path)
        if file is not None and file.id is not None:
            sym = store.find_symbol_at_location(file.id, int(line_str))
            if sym is not None:
                return sym
    return store.find_symbol_by_qualified_name(repo_id, ref)


def find_references(
    store: GraphStore,
    repo_id: int,
    ref: str,
    *,
    depth: int = DEFAULT_DEPTH,
    tiers: Sequence[Tier] | None = None,
) -> ReferencesResult | None:
    """The main F-7 entry point. Returns ``None`` only when ``ref`` itself
    does not resolve to a known symbol -- an empty ``hits`` list (a
    resolved symbol nothing depends on) is a normal, valid result, not a
    failure.

    ``depth`` is clamped to ``[1, MAX_DEPTH]`` regardless of what the
    caller passes -- F-7 requirement 2 states these as hard bounds, not
    suggestions, and every surface (today: none yet; RM-045's future CLI
    flag included) should get the same enforcement rather than each
    re-implementing it.
    """
    target = resolve_symbol_ref(store, repo_id, ref)
    if target is None or target.id is None:
        return None

    clamped_depth = max(1, min(depth, MAX_DEPTH))
    pairs = store.reverse_dependencies(target.id, clamped_depth, tiers)

    file_cache: dict[int, str | None] = {}

    def path_of(file_id: int | None) -> str | None:
        if file_id is None:
            return None
        if file_id not in file_cache:
            file = store.get_file(file_id)
            file_cache[file_id] = file.path if file is not None else None
        return file_cache[file_id]

    hits = []
    for edge, hop_depth in pairs:
        source = store.get_symbol(edge.src_symbol_id)
        if source is None:
            continue  # should not happen for a well-formed graph; skip rather than raise
        hits.append(
            ReferenceHit(
                source=source,
                kind=edge.kind,
                tier=edge.tier,
                depth=hop_depth,
                evidence_path=path_of(edge.evidence_file_id),
                evidence_line=edge.evidence_line,
            )
        )

    hits.sort(key=lambda h: (h.depth, h.tier.value, h.source.qualified_name))
    return ReferencesResult(target=target, hits=hits)
