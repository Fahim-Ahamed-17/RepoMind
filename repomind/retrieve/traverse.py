"""Bounded graph expansion for retrieval (F-6, RM-035, design.md AD-6).

Step 2 ("expand") of the CLI's bounded ask loop (design.md section 5.2):
takes the symbols retrieve/search.py's seed step (RM-033) already found
and walks outward through the graph -- callers, callees, and
definitions, in both directions -- up to a caller-configured number of
hops (F-6 requirement 5: "at most 2 expansion hops (configurable)").
"Callers, callees, definitions" describes what shows up along the way,
not a kind filter to apply: every edge kind is followed, the same
default every other traversal in this codebase uses (``edges_from``/
``edges_to`` themselves, ``reverse_dependencies``) -- only ``tiers``
narrows what counts.

Ranking and node-count budgeting are deliberately not this module's job:
step 3 of the same flow ("rank and budget, pack to token limit") is
RM-043's, once it exists. This module answers one question only --
"what is graph-reachable from the seeds, within the hop bound" -- and
hands back enough (:class:`TraversalStep`, F-6 requirement 4's own
"traversal path... which edges were followed, and at which tier") for a
later step to rank and trim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from repomind.model import Edge, Symbol, Tier
    from repomind.store.base import GraphStore

#: F-6 requirement 5's own default -- "at most 2 expansion hops
#: (configurable)".
DEFAULT_MAX_HOPS = 2


@dataclass(frozen=True, slots=True)
class TraversalStep:
    """One edge touched during expansion, at the hop it was found -- F-6
    requirement 4: "the traversal path... which edges were followed, and
    at which tier."
    """

    edge: Edge
    hop: int


@dataclass(frozen=True, slots=True)
class ExpandedSymbol:
    """One symbol reached by expansion, not a seed itself."""

    symbol: Symbol
    hop: int
    """Hops from the nearest seed at which this symbol was first
    reached -- mirrors ``analyze/reverse.py``'s ``ReferenceHit.depth``.
    """


@dataclass(frozen=True, slots=True)
class ExpansionResult:
    seeds: frozenset[int]
    """Symbol ids expansion started from -- the caller's own seed set
    (retrieve/search.py's hits, resolved to symbols), kept here only so
    a result is self-describing without threading it alongside separately.
    """

    expanded: dict[int, ExpandedSymbol]
    """Symbols reached by expansion, keyed by id. Never includes a seed
    -- the caller already has those -- and never a symbol beyond
    ``max_hops`` or excluded by a tier filter.
    """

    steps: list[TraversalStep]
    """The traversal path: every edge touched, deduplicated, sorted by
    ``(hop, edge.id)`` for a deterministic order (docs/conventions.md --
    never depend on set/dict iteration order). Includes edges between two
    already-known symbols (two seeds calling each other, say) -- real
    structure worth showing, even where it discovers nothing new.
    """


def expand(
    store: GraphStore,
    seed_symbol_ids: Iterable[int],
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
    tiers: Sequence[Tier] | None = None,
) -> ExpansionResult:
    """Breadth-first, bidirectional expansion from ``seed_symbol_ids``:
    each hop follows every incoming and outgoing edge of every symbol
    reached so far, via ``store.edges_from``/``store.edges_to``.
    ``tiers=None`` means every tier, the same default every other tier
    parameter in this codebase uses.

    Cycle-safe by construction: a symbol is only ever expanded once
    (tracked in ``visited``), so a cycle simply stops contributing new
    frontier nodes rather than looping -- unlike RM-024's
    ``reverse_dependencies``, this needs no recursive CTE, since
    ``max_hops`` is small and fixed by the caller rather than open-ended.

    An edge can surface twice within one hop -- once from its source
    symbol's ``edges_from``, once from its destination's ``edges_to``, if
    both happen to be in the same frontier -- so steps are deduplicated
    by ``edge.id`` as they are collected, not just at the end.
    """
    seeds = frozenset(seed_symbol_ids)
    visited = set(seeds)
    expanded: dict[int, ExpandedSymbol] = {}
    steps: list[TraversalStep] = []
    seen_edge_ids: set[int] = set()

    frontier = set(seeds)
    for hop in range(1, max_hops + 1):
        next_frontier: set[int] = set()
        for symbol_id in frontier:
            touched = (
                *store.edges_from(symbol_id, tiers=tiers),
                *store.edges_to(symbol_id, tiers=tiers),
            )
            for edge in touched:
                if edge.id is not None:
                    if edge.id in seen_edge_ids:
                        continue
                    seen_edge_ids.add(edge.id)
                steps.append(TraversalStep(edge=edge, hop=hop))

                other_id = (
                    edge.dst_symbol_id if edge.src_symbol_id == symbol_id else edge.src_symbol_id
                )
                if other_id not in visited:
                    visited.add(other_id)
                    next_frontier.add(other_id)

        if not next_frontier:
            break
        for symbol_id in next_frontier:
            symbol = store.get_symbol(symbol_id)
            if symbol is not None:
                expanded[symbol_id] = ExpandedSymbol(symbol=symbol, hop=hop)
        frontier = next_frontier

    steps.sort(key=lambda step: (step.hop, step.edge.id or 0))
    return ExpansionResult(seeds=seeds, expanded=expanded, steps=steps)
