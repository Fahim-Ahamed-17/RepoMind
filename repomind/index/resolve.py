"""The heuristic resolver: turns raw :class:`ParsedReference` data into
``heuristic``-tier :class:`Edge` rows by name and scope matching.

RM-021. Design.md's flow diagram names this box explicitly: "heuristic
resolver -> heuristic edges". This module is the box.

Runs once per index, *after* every file has been parsed and its symbols
persisted -- resolving a reference genuinely needs the whole repo's symbol
table, since an import or a call routinely crosses file boundaries. See
``index/pipeline.py`` for how this fits into the run as a whole; ``defines``
edges are deliberately *not* computed here (they need no cross-file
resolution at all -- both ends are known within one file at parse time,
see ``ParsedSymbol.parent_qualified_name``) and are computed directly in
the pipeline instead.

The matching strategy, applied consistently everywhere a name needs
resolving (imports, inherits, calls, references):

  1. Try the raw text as an exact ``qualified_name`` match, repo-wide.
     Correct whenever the reference was already written in (or reduces to)
     fully-qualified form -- which RM-020 arranges for imports specifically
     (``pkg.mod.Foo``, not just ``pkg.mod`` or just ``Foo``).
  2. Walk *up* the enclosing scope chain from where the reference appears
     (``src_qualified_name``, then its parent, then its parent's parent,
     ...), trying ``{ancestor}.{bare_name}`` at each level. Correct for a
     name referring to something in the same file/class/module, which is
     the common case for a call, an inheritance base, or an annotation.
  3. Check whether this file's own IMPORTS references end in
     ``.{bare_name}`` (or equal it) -- correct for calling/referencing
     something this file itself imported under its original name. Cannot
     see through an alias (RM-020 records the imported name, not the local
     binding an ``as`` clause introduces) -- an aliased import's uses are a
     known, accepted gap at this tier; SCIP (the ``resolved`` tier) is
     exactly the mechanism that does not have this limitation.
  4. As a last resort, if exactly one symbol anywhere in the repo has this
     bare name (and, for INHERITS, is a class), use it. Skipped -- not
     guessed at -- when more than one candidate exists: a wrong heuristic
     edge is worse than a missing one (see docs/design.md section 4.3 on
     why tiers exist at all).

``self.x`` / ``cls.x`` calls get one more, narrower rule ahead of all of
these: resolve ``x`` against the *enclosing class's own members* by
walking up the scope chain to the nearest CLASS-kind ancestor. This is
reliable enough, and common enough, to deserve first refusal over the
general chain above.

Anything not resolved by any of these produces no edge. That is this
tier's expected failure mode, not a bug to chase -- see the module
docstring in ``languages/python/parser.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from repomind.model import Edge, EdgeKind, SymbolKind, Tier

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from repomind.model import ParsedReference, ParsedSymbol, Symbol

#: EdgeKind.INHERITS resolution additionally requires the match be one of
#: these -- resolving a base class to a same-named *function* would be a
#: heuristic edge actively worse than no edge at all.
_CLASS_KINDS = frozenset({SymbolKind.CLASS})

#: EdgeKind.CALLS and the decorator half of EdgeKind.REFERENCES should not
#: resolve to a bare variable of the same name -- a callable is a function,
#: method, or (for a decorator that is itself a class) a class.
_CALLABLE_KINDS = frozenset({SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS})


class _SymbolIndex:
    """The repo-wide lookup structures every resolution rule reads from.
    Built once per run, reused across every file's references.
    """

    def __init__(self, symbols: Iterable[Symbol]) -> None:
        self._by_qualified_name: dict[str, Symbol] = {}
        self._by_bare_name: dict[str, list[Symbol]] = {}
        for sym in symbols:
            self._by_qualified_name[sym.qualified_name] = sym
            bare = sym.qualified_name.rsplit(".", 1)[-1]
            self._by_bare_name.setdefault(bare, []).append(sym)

    def exact(self, qualified_name: str) -> Symbol | None:
        return self._by_qualified_name.get(qualified_name)

    def unique_by_bare_name(
        self, bare_name: str, allowed_kinds: frozenset[SymbolKind] | None = None
    ) -> Symbol | None:
        candidates = self._by_bare_name.get(bare_name, [])
        if allowed_kinds is not None:
            candidates = [c for c in candidates if c.kind in allowed_kinds]
        return candidates[0] if len(candidates) == 1 else None


def _ancestor_scopes(qualified_name: str) -> Iterator[str]:
    """``qualified_name`` itself, then each successively shorter dotted
    prefix, down to (but not including) the empty string. For
    ``pkg.mod.Widget.render``: itself, ``pkg.mod.Widget``, ``pkg.mod``,
    ``pkg``.
    """
    parts = qualified_name.split(".")
    for i in range(len(parts), 0, -1):
        yield ".".join(parts[:i])


def _resolve_self_or_cls_call(
    target_text: str, src_qualified_name: str, index: _SymbolIndex
) -> Symbol | None:
    """``self.method`` / ``cls.method`` (exactly one attribute hop) against
    the nearest enclosing class. Returns ``None`` for anything with more
    than one dot after ``self``/``cls`` (``self.a.b``) -- see this
    module's docstring.
    """
    prefix, _, rest = target_text.partition(".")
    if prefix not in ("self", "cls") or not rest or "." in rest:
        return None
    for ancestor in _ancestor_scopes(src_qualified_name):
        owner = index.exact(ancestor)
        if owner is not None and owner.kind == SymbolKind.CLASS:
            return index.exact(f"{ancestor}.{rest}")
    return None


def _resolve_via_same_file_imports(
    bare_name: str, same_file_import_targets: Sequence[str], index: _SymbolIndex
) -> Symbol | None:
    for target in same_file_import_targets:
        if target == bare_name or target.endswith(f".{bare_name}"):
            hit = index.exact(target)
            if hit is not None:
                return hit
    return None


def _resolve_name(
    target_text: str,
    src_qualified_name: str,
    same_file_import_targets: Sequence[str],
    index: _SymbolIndex,
    *,
    allowed_kinds: frozenset[SymbolKind] | None = None,
) -> Symbol | None:
    """The general-purpose rule chain described in the module docstring.
    Used for IMPORTS, INHERITS, REFERENCES, and any CALLS target the
    self/cls rule didn't already handle.

    A **dotted** ``target_text`` (``pkg.Base``, ``mod.func``) only ever
    gets step 1, the exact match -- never the ancestor-walk, same-file
    -import, or unique-by-bare-name fallbacks below. Those three all key
    off the *bare* trailing name (``Base``, ``func``), and matching only
    that against whatever same-named thing happens to be reachable would
    silently discard the qualifying prefix the reference actually named --
    a wrong edge, which this tier treats as worse than no edge (see the
    module docstring). Only a genuinely bare ``target_text`` -- no
    qualifying prefix to discard in the first place -- reaches them.
    """
    exact = index.exact(target_text)
    if exact is not None and (allowed_kinds is None or exact.kind in allowed_kinds):
        return exact
    if "." in target_text:
        return None

    for ancestor in _ancestor_scopes(src_qualified_name):
        hit = index.exact(f"{ancestor}.{target_text}")
        if hit is not None and (allowed_kinds is None or hit.kind in allowed_kinds):
            return hit

    via_import = _resolve_via_same_file_imports(target_text, same_file_import_targets, index)
    if via_import is not None and (allowed_kinds is None or via_import.kind in allowed_kinds):
        return via_import

    return index.unique_by_bare_name(target_text, allowed_kinds)


def _resolve_one(
    ref: ParsedReference,
    same_file_import_targets: Sequence[str],
    index: _SymbolIndex,
) -> Symbol | None:
    if ref.kind == EdgeKind.CALLS:
        # self.x()/cls.x() gets first refusal: it is reliable enough (and
        # common enough) to deserve resolving against the enclosing class
        # even where the general dotted-name rule below would refuse to
        # guess. Anything else -- bare, or any other dotted callee,
        # including a self/cls chain deeper than one hop -- falls through
        # to _resolve_name, whose own dotted-vs-bare rule (see its
        # docstring) takes it from there.
        via_self = _resolve_self_or_cls_call(ref.target_text, ref.src_qualified_name, index)
        if via_self is not None:
            return via_self
        return _resolve_name(
            ref.target_text,
            ref.src_qualified_name,
            same_file_import_targets,
            index,
            allowed_kinds=_CALLABLE_KINDS,
        )

    if ref.kind == EdgeKind.INHERITS:
        return _resolve_name(
            ref.target_text,
            ref.src_qualified_name,
            same_file_import_targets,
            index,
            allowed_kinds=_CLASS_KINDS,
        )

    if ref.kind == EdgeKind.REFERENCES:
        return _resolve_name(
            ref.target_text, ref.src_qualified_name, same_file_import_targets, index
        )

    if ref.kind == EdgeKind.IMPORTS:
        # target_text is already a full candidate qualified name (RM-020
        # arranges this); no scope-walking or same-file-import fallback
        # makes sense for resolving an import itself.
        return index.exact(ref.target_text)

    return None  # DEFINES/TESTS never reach this resolver -- see module docstring


def resolve_heuristic_edges(
    repo_id: int,
    all_symbols: Iterable[Symbol],
    file_references: Iterable[tuple[int, Sequence[ParsedReference]]],
) -> list[Edge]:
    """Resolve every file's raw references against the repo-wide symbol
    table, producing ``heuristic``-tier edges.

    ``file_references`` is ``(file_id, references)`` per file -- the
    ``file_id`` becomes each resulting edge's ``evidence_file_id``.
    Src/dst symbols are matched by ``qualified_name`` against
    ``all_symbols`` (typically ``store.list_symbols(repo_id)``, called once
    up front by the caller so this function stays free of any store
    dependency itself -- ``index/`` orchestrates, this module only
    resolves).
    """
    index = _SymbolIndex(all_symbols)
    edges: list[Edge] = []

    for file_id, references in file_references:
        same_file_import_targets = [r.target_text for r in references if r.kind == EdgeKind.IMPORTS]
        for ref in references:
            src = index.exact(ref.src_qualified_name)
            if src is None or src.id is None:
                continue  # should not happen -- src is always a just-persisted symbol
            dst = _resolve_one(ref, same_file_import_targets, index)
            if dst is None or dst.id is None or dst.id == src.id:
                continue  # unresolved, or a (rare) self-reference -- neither is an edge
            edges.append(
                Edge(
                    repo_id=repo_id,
                    src_symbol_id=src.id,
                    dst_symbol_id=dst.id,
                    kind=ref.kind,
                    tier=Tier.HEURISTIC,
                    evidence_file_id=file_id,
                    evidence_line=ref.evidence_line,
                )
            )

    return edges


def defines_edges_for_file(
    repo_id: int,
    file_id: int,
    parsed_symbols: Sequence[ParsedSymbol],
    persisted: Sequence[Symbol],
) -> list[Edge]:
    """``defines`` edges for one file's symbols, from the parent tracking
    RM-020 attached to each :class:`ParsedSymbol` -- fully resolvable
    within a single file, so this does not go through
    :func:`resolve_heuristic_edges` or need the repo-wide index at all.

    ``parsed_symbols`` is what the language pack produced (carries
    ``parent_qualified_name``); ``persisted`` is what
    ``store.replace_symbols`` returned for the same file in the same order
    (carries the database ``id`` each edge needs). Both are required
    because neither alone has both pieces -- see ``index/pipeline.py`` for
    why they are never separated by more than this one call.
    """
    qname_to_id = {s.qualified_name: s.id for s in persisted}
    edges: list[Edge] = []
    for parsed, persisted_sym in zip(parsed_symbols, persisted, strict=True):
        if parsed.parent_qualified_name is None or persisted_sym.id is None:
            continue
        parent_id = qname_to_id.get(parsed.parent_qualified_name)
        if parent_id is None:
            continue  # parent symbol missing (should not happen for a well-formed file)
        edges.append(
            Edge(
                repo_id=repo_id,
                src_symbol_id=parent_id,
                dst_symbol_id=persisted_sym.id,
                kind=EdgeKind.DEFINES,
                tier=Tier.HEURISTIC,
                evidence_file_id=file_id,
                evidence_line=persisted_sym.start_line,
            )
        )
    return edges
