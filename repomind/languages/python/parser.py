"""Tree-sitter-based Python symbol and reference extraction.

RM-017 (symbols) + RM-020 (raw references for edge extraction). Produces:

  * Symbols -- classes, functions, methods, and module/class-level
    variables, each with a qualified name, span, signature (for callables),
    docstring, and now its immediate ``parent_qualified_name`` (RM-020: the
    ``defines`` edge needs exactly this, and both ends are already known
    within a single file -- no cross-file resolution required).

  * Raw references (:class:`repomind.model.ParsedReference`) -- imports,
    class bases, calls, and a bounded set of "references" (decorators and
    type annotations). These are *unresolved*: matching ``target_text``
    against the repo's actual symbol table is entirely
    ``index/resolve.py``'s job (RM-021), not this module's. This file only
    records what was syntactically written.

Scope, deliberately bounded -- consistent with RM-017's own stated
philosophy ("a reasonable simplification for what is fundamentally a
best-effort heuristic signal, not a name-resolution engine; that is SCIP's
job"):

  * Variables are extracted at module level and directly inside a class
    body only -- not inside function bodies (unchanged from RM-017).
  * Calls and imports are both scanned throughout a scope's statements by
    the same recursive walk -- including inside
    ``if``/``for``/``while``/``try``/``with`` bodies (so a conditional
    import, e.g. ``try: import ujson as json / except ImportError: import
    json``, is captured in both branches), since those do not introduce a
    new named scope -- but scanning stops at anything that *does* (a
    nested ``function_definition``, ``class_definition``, ``lambda``, or
    ``decorated_definition``; the latter gets its own dispatch instead).
    Calls inside decorator arguments and default-parameter values are not
    scanned: both execute in the enclosing scope at definition time, but
    reaching them needs a second, more surgical walk for genuinely rare
    cases.
  * Attribute calls deeper than one level (``obj.attr.call()``) are
    recorded with their full dotted text but are not expected to resolve
    to anything -- attributing them correctly needs type inference, which
    is exactly the gap the ``resolved`` (SCIP) tier exists to close.
  * "References" covers exactly two things: decorator names, and type
    annotations (parameter, return, and variable). Not a general
    expression walker -- see the module-level note in ParsedReference.
  * A decorated definition's symbol span starts at the decorator line, not
    the ``def``/``class`` keyword (unchanged from RM-017).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tree_sitter
import tree_sitter_python

from repomind.model import EdgeKind, ParsedFile, ParsedReference, ParsedSymbol, SymbolKind

if TYPE_CHECKING:
    from pathlib import Path

_LANGUAGE = tree_sitter.Language(tree_sitter_python.language())

#: Node types that introduce a new named scope. Call-scanning and
#: reference-scanning both stop descending here -- each gets its own
#: dispatch (``_visit_function`` / ``_visit_class``) with its own
#: ``scope_prefix``, or in ``decorated_definition``'s case, is unwrapped
#: first and then dispatched the same way.
_SCOPE_BOUNDARY_TYPES = frozenset(
    {"function_definition", "class_definition", "lambda", "decorated_definition"}
)


def _file_qualified_name(rel_path: str) -> str:
    """The dotted import name a Python file would have, e.g.
    ``repomind/store/base.py`` -> ``repomind.store.base``, and an
    ``__init__.py`` -> its containing package's own name.

    A root-level ``__init__.py`` (no containing directory at all -- the
    indexed repo's own root *is* the package) has nothing left to strip
    the ``__init__`` segment down to. Found via a real fixture
    (tests/fixtures/cyclic, whose own ``__init__.py`` sits at its root)
    rather than by inspection: the naive fallback returned the literal
    string ``"__init__.py"`` -- extension and all, not even a dotted name
    -- which silently broke every qualified-name lookup for that one
    file's own symbol. Falling back to the bare ``"__init__"`` (already
    ``.py``-stripped) instead keeps this function's contract -- always a
    plain dotted name, never a raw path -- true in every case.
    """
    parts = rel_path.removesuffix(".py").split("/")
    if len(parts) > 1 and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else rel_path


def _dotted_package_of(rel_path: str) -> list[str]:
    """The dotted package *containing* ``rel_path`` -- what a relative
    import's leading ``.`` resolves against (RM-020).

    Not the same as :func:`_file_qualified_name` for a non-``__init__``
    module: ``pkg/sub/mod.py``'s own qualified name is ``pkg.sub.mod``, but
    its *containing package* -- what ``.`` means inside ``mod.py`` -- is
    ``pkg.sub``. For ``pkg/sub/__init__.py`` the two coincide (an
    ``__init__.py`` both *is* the package ``pkg.sub`` and is contained by
    it), which is exactly why plain dirname-based computation is correct
    for both cases without special-casing ``__init__.py`` here.
    """
    parts = rel_path.removesuffix(".py").split("/")
    if parts and parts[-1] == "__init__":
        return parts[:-1]
    return parts[:-1]


def _text(source: bytes, node: tree_sitter.Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _line_span(node: tree_sitter.Node) -> tuple[int, int]:
    """1-indexed, inclusive -- matches :attr:`repomind.model.Symbol.start_line`."""
    return node.start_point.row + 1, node.end_point.row + 1


def _docstring_of(body: tree_sitter.Node | None, source: bytes) -> str | None:
    if body is None or body.named_child_count == 0:
        return None
    first = body.named_child(0)
    if first is None or first.type != "expression_statement":
        return None
    expr = first.named_child(0)
    if expr is None or expr.type != "string":
        return None
    content = "".join(_text(source, c) for c in expr.named_children if c.type == "string_content")
    return content if content else None


def _signature_of(def_node: tree_sitter.Node, source: bytes) -> str | None:
    """``name(params) -> ret``, reconstructed from source spans rather than
    the AST's own repr, so it reads exactly as written (including default
    values, type annotations, and multi-line parameter lists collapsed to
    one line for display).
    """
    name = def_node.child_by_field_name("name")
    params = def_node.child_by_field_name("parameters")
    if name is None or params is None:
        return None
    sig = _text(source, name) + " ".join(_text(source, params).split())
    ret = def_node.child_by_field_name("return_type")
    if ret is not None:
        sig += f" -> {_text(source, ret)}"
    return sig


def _unwrap_decorated(
    node: tree_sitter.Node,
) -> tuple[tree_sitter.Node, tree_sitter.Point, list[tree_sitter.Node]]:
    """If ``node`` is a ``decorated_definition``, return its inner
    function/class definition, the *decorated* node's own start point (so
    the symbol's span includes the decorator lines), and its decorator
    nodes (RM-020: needed to emit REFERENCES edges for each one).
    Otherwise return ``node`` unchanged with its own start point and no
    decorators.
    """
    if node.type != "decorated_definition":
        return node, node.start_point, []
    inner = node.named_children[-1]
    decorators = [c for c in node.named_children[:-1] if c.type == "decorator"]
    return inner, node.start_point, decorators


def _unwrap_annotation(annotation: tree_sitter.Node | None) -> tree_sitter.Node | None:
    """A ``type`` field (parameter annotation, return type, or variable
    annotation) is itself a wrapper node of grammar type ``"type"`` with
    one named child -- the actual expression, e.g. ``(type (identifier))``
    for ``Config`` or ``(type (attribute))`` for ``pkg.Config``. Returns
    that inner node if it is a plain identifier/attribute worth recording
    as a reference, or ``None`` for anything more complex
    (``List[int]``-style subscripts, string forward references, etc.) --
    the same bounded, best-effort scope as everywhere else in this module.
    """
    if annotation is None or annotation.named_child_count == 0:
        return None
    inner = annotation.named_children[0]
    return inner if inner.type in ("identifier", "attribute") else None


def _base_name_of(node: tree_sitter.Node) -> tree_sitter.Node | None:
    """Unwrap a class-argument-list entry to the identifier/attribute
    naming an actual base class, or ``None`` if this entry isn't one.

    Handles ``Base``, ``pkg.Base`` directly; unwraps one level of
    ``Generic[T]``-style ``subscript`` to its ``value`` (``Generic``).
    Skips ``metaclass=...``-style ``keyword_argument`` entries and
    anything else too dynamic to name syntactically (``*bases``, a call
    that returns a class, etc.) -- consistent with this module's
    best-effort scope.
    """
    if node.type in ("identifier", "attribute"):
        return node
    if node.type == "subscript":
        value = node.child_by_field_name("value")
        if value is not None and value.type in ("identifier", "attribute"):
            return value
    return None


def _dotted_text_of_import_name(node: tree_sitter.Node, source: bytes) -> str | None:
    """The dotted module path named by one entry in an import list.

    Handles ``dotted_name`` directly, and ``aliased_import`` (``x as y``)
    by taking its ``dotted_name`` child -- the alias itself is a local
    binding name, not part of the target being referenced. Returns
    ``None`` for a ``wildcard_import`` (``from x import *``): there is no
    single name to record a reference to.
    """
    if node.type == "dotted_name":
        return _text(source, node)
    if node.type == "aliased_import":
        dotted = node.child_by_field_name("name") or next(
            (c for c in node.named_children if c.type == "dotted_name"), None
        )
        return _text(source, dotted) if dotted is not None else None
    return None


class _Extractor:
    """Holds the per-file state (source bytes, accumulated symbols and
    references) that would otherwise have to thread through every
    recursive call.
    """

    def __init__(self, source: bytes, rel_path: str) -> None:
        self._source = source
        self._rel_path = rel_path
        """Needed only to resolve a relative import's leading dots against
        this file's own containing package -- see
        :func:`_dotted_package_of`. Not used for anything else."""
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []

    # -- symbol extraction (RM-017) + defines (RM-020) --------------------

    def walk_block(
        self,
        statements: list[tree_sitter.Node],
        scope_prefix: str,
        parent_qualified_name: str | None,
        *,
        in_class: bool,
        extract_variables: bool,
    ) -> None:
        for raw in statements:
            node, start_point, decorators = _unwrap_decorated(raw)

            if node.type == "function_definition":
                self._visit_function(
                    node,
                    start_point,
                    decorators,
                    scope_prefix,
                    parent_qualified_name,
                    is_method=in_class,
                )
            elif node.type == "class_definition":
                self._visit_class(
                    node, start_point, decorators, scope_prefix, parent_qualified_name
                )
            elif extract_variables and node.type == "expression_statement":
                self._visit_possible_variable(node, scope_prefix, parent_qualified_name)

        self._scan_calls(statements, scope_prefix)

    def _visit_function(
        self,
        node: tree_sitter.Node,
        start_point: tree_sitter.Point,
        decorators: list[tree_sitter.Node],
        scope_prefix: str,
        parent_qualified_name: str | None,
        *,
        is_method: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(self._source, name_node)
        qualified_name = f"{scope_prefix}.{name}" if scope_prefix else name
        _, end_line = _line_span(node)
        def_start_line = start_point.row + 1

        self.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.METHOD if is_method else SymbolKind.FUNCTION,
                name=name,
                qualified_name=qualified_name,
                start_line=def_start_line,
                end_line=end_line,
                signature=_signature_of(node, self._source),
                docstring=_docstring_of(node.child_by_field_name("body"), self._source),
                parent_qualified_name=parent_qualified_name,
            )
        )

        for dec in decorators:
            self._reference_decorator(dec, qualified_name, def_start_line)
        self._reference_parameter_types(node, qualified_name, def_start_line)
        self._reference_return_type(node, qualified_name, def_start_line)

        body = node.child_by_field_name("body")
        if body is not None:
            # Nested defs (closures) still get symbols -- variables inside a
            # function body deliberately do not, per this module's scope note.
            self.walk_block(
                list(body.named_children),
                qualified_name,
                qualified_name,
                in_class=False,
                extract_variables=False,
            )

    def _visit_class(
        self,
        node: tree_sitter.Node,
        start_point: tree_sitter.Point,
        decorators: list[tree_sitter.Node],
        scope_prefix: str,
        parent_qualified_name: str | None,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(self._source, name_node)
        qualified_name = f"{scope_prefix}.{name}" if scope_prefix else name
        _, end_line = _line_span(node)
        def_start_line = start_point.row + 1

        self.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.CLASS,
                name=name,
                qualified_name=qualified_name,
                start_line=def_start_line,
                end_line=end_line,
                signature=None,
                docstring=_docstring_of(node.child_by_field_name("body"), self._source),
                parent_qualified_name=parent_qualified_name,
            )
        )

        for dec in decorators:
            self._reference_decorator(dec, qualified_name, def_start_line)

        bases = node.child_by_field_name("superclasses")
        if bases is not None:
            for arg in bases.named_children:
                base = _base_name_of(arg)
                if base is not None:
                    self.references.append(
                        ParsedReference(
                            kind=EdgeKind.INHERITS,
                            src_qualified_name=qualified_name,
                            target_text=_text(self._source, base),
                            evidence_line=base.start_point.row + 1,
                        )
                    )

        body = node.child_by_field_name("body")
        if body is not None:
            self.walk_block(
                list(body.named_children),
                qualified_name,
                qualified_name,
                in_class=True,
                extract_variables=True,
            )

    def _visit_possible_variable(
        self, expr_stmt: tree_sitter.Node, scope_prefix: str, parent_qualified_name: str | None
    ) -> None:
        assignment = expr_stmt.named_child(0)
        if assignment is None or assignment.type != "assignment":
            return
        target = assignment.child_by_field_name("left")
        if target is None or target.type != "identifier":
            return  # skip tuple unpacking etc. -- see module docstring
        name = _text(self._source, target)
        qualified_name = f"{scope_prefix}.{name}" if scope_prefix else name
        start_line, end_line = _line_span(expr_stmt)
        self.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.VARIABLE,
                name=name,
                qualified_name=qualified_name,
                start_line=start_line,
                end_line=end_line,
                parent_qualified_name=parent_qualified_name,
            )
        )

        ann = _unwrap_annotation(assignment.child_by_field_name("type"))
        if ann is not None:
            self.references.append(
                ParsedReference(
                    kind=EdgeKind.REFERENCES,
                    src_qualified_name=qualified_name,
                    target_text=_text(self._source, ann),
                    evidence_line=ann.start_point.row + 1,
                )
            )

    # -- imports (RM-020) --------------------------------------------------
    #
    # For `from X import Y [, Z as W]`, target_text is `X.Y` (the original
    # name, never the alias `W`) rather than just `X` -- deliberately
    # ambiguous-looking, and deliberately so: `X.Y` is *exactly* the
    # qualified name Y would have if it's a symbol defined in X, and
    # *exactly* the qualified name a submodule `X/Y.py` would have if it's
    # a module instead. Either way the resolver's plain
    # find_symbol_by_qualified_name(repo_id, "X.Y") finds it in one lookup
    # with no special-casing. Only when *neither* interpretation matches
    # does the resolver need to fall back to treating `X` alone as the
    # target (RM-021) -- module-to-module, "imports something from X" being
    # the best remaining true statement. One reference is emitted per
    # imported name, so `from pkg.mod import Foo, Bar as B` yields two
    # separate, independently resolvable targets, not one merged one.

    def _import_from_targets(self, node: tree_sitter.Node) -> list[str]:
        first = node.named_children[0] if node.named_children else None
        if first is None:
            return []
        if first.type == "dotted_name":
            module = _text(self._source, first)
        elif first.type == "relative_import":
            resolved = self._resolve_relative_module(first)
            if resolved is None:
                return []
            module = resolved
        else:
            return []

        names = [_dotted_text_of_import_name(c, self._source) for c in node.named_children[1:]]
        resolved_names = [n for n in names if n is not None]
        if not resolved_names:
            # A bare `from X import *`, or nothing recognisable followed the
            # module -- the module-level dependency is still real even
            # though no specific symbol can be named.
            return [module]
        return [f"{module}.{name}" for name in resolved_names]

    def _resolve_relative_module(self, relative_import: tree_sitter.Node) -> str | None:
        prefix = next(
            (c for c in relative_import.named_children if c.type == "import_prefix"), None
        )
        dots = len(_text(self._source, prefix)) if prefix is not None else 0
        submodule = next(
            (c for c in relative_import.named_children if c.type == "dotted_name"), None
        )
        base = _dotted_package_of(self._rel_path)
        levels_up = dots - 1
        if levels_up < 0 or levels_up > len(base):
            return None  # points above the repo root -- no local target
        target = base[: len(base) - levels_up] if levels_up else base
        if submodule is not None:
            target = [*target, *_text(self._source, submodule).split(".")]
        return ".".join(target) if target else None

    def _emit_import(self, scope_prefix: str, target: str, line: int) -> None:
        self.references.append(
            ParsedReference(
                kind=EdgeKind.IMPORTS,
                src_qualified_name=scope_prefix,
                target_text=target,
                evidence_line=line,
            )
        )

    # -- calls and imports (RM-020) ----------------------------------------
    # One recursive walk covers both: neither introduces a new scope, so a
    # call or an import nested inside `if`/`for`/`while`/`try`/`with` still
    # belongs to the *enclosing* function/class/module, and is still found
    # by continuing to recurse through those control-flow wrappers -- only
    # `_SCOPE_BOUNDARY_TYPES` stops the descent.

    def _scan_calls(self, statements: list[tree_sitter.Node], scope_prefix: str) -> None:
        for stmt in statements:
            self._scan_calls_in(stmt, scope_prefix)

    def _scan_calls_in(self, node: tree_sitter.Node, scope_prefix: str) -> None:
        line = node.start_point.row + 1

        if node.type == "call":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type in ("identifier", "attribute"):
                self.references.append(
                    ParsedReference(
                        kind=EdgeKind.CALLS,
                        src_qualified_name=scope_prefix,
                        target_text=_text(self._source, callee),
                        evidence_line=line,
                    )
                )
        elif node.type == "import_statement":
            for child in node.named_children:
                dotted = _dotted_text_of_import_name(child, self._source)
                if dotted is not None:
                    self._emit_import(scope_prefix, dotted, line)
        elif node.type == "import_from_statement":
            for target in self._import_from_targets(node):
                self._emit_import(scope_prefix, target, line)

        if node.type in _SCOPE_BOUNDARY_TYPES:
            return  # dispatched separately with its own scope -- don't double-scan
        # A statement matched above (call/import) still recurses into its
        # own children -- e.g. a call's arguments can contain further calls
        # (`f(g())`), and this is cheap insurance against missing anything
        # an import/call node might nest that a future grammar version adds.
        for child in node.named_children:
            self._scan_calls_in(child, scope_prefix)

    # -- references: decorators and type annotations (RM-020) -------------

    def _reference_decorator(
        self, decorator: tree_sitter.Node, src_qualified_name: str, evidence_line: int
    ) -> None:
        # decorator's single named child is either the name/attribute
        # itself (`@name`) or a `call` whose `function` field is the name
        # (`@name(args)`) -- the decorator *factory* being referenced.
        target = decorator.named_children[0] if decorator.named_children else None
        if target is not None and target.type == "call":
            target = target.child_by_field_name("function")
        if target is not None and target.type in ("identifier", "attribute"):
            self.references.append(
                ParsedReference(
                    kind=EdgeKind.REFERENCES,
                    src_qualified_name=src_qualified_name,
                    target_text=_text(self._source, target),
                    evidence_line=evidence_line,
                )
            )

    def _reference_parameter_types(
        self, def_node: tree_sitter.Node, src_qualified_name: str, evidence_line: int
    ) -> None:
        params = def_node.child_by_field_name("parameters")
        if params is None:
            return
        for param in params.named_children:
            if param.type not in ("typed_parameter", "typed_default_parameter"):
                continue
            ann = _unwrap_annotation(param.child_by_field_name("type"))
            if ann is not None:
                self.references.append(
                    ParsedReference(
                        kind=EdgeKind.REFERENCES,
                        src_qualified_name=src_qualified_name,
                        target_text=_text(self._source, ann),
                        evidence_line=evidence_line,
                    )
                )

    def _reference_return_type(
        self, def_node: tree_sitter.Node, src_qualified_name: str, evidence_line: int
    ) -> None:
        ret = _unwrap_annotation(def_node.child_by_field_name("return_type"))
        if ret is not None:
            self.references.append(
                ParsedReference(
                    kind=EdgeKind.REFERENCES,
                    src_qualified_name=src_qualified_name,
                    target_text=_text(self._source, ret),
                    evidence_line=evidence_line,
                )
            )


def parse_python_file(path: Path, text: str, blob_sha: str) -> ParsedFile:
    source = text.encode("utf-8")
    parser = tree_sitter.Parser(_LANGUAGE)
    tree = parser.parse(source)

    rel_path = path.as_posix()
    file_qname = _file_qualified_name(rel_path)
    n_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

    extractor = _Extractor(source, rel_path)
    extractor.symbols.append(
        ParsedSymbol(
            kind=SymbolKind.FILE,
            name=path.name,
            qualified_name=file_qname,
            start_line=1,
            end_line=max(n_lines, 1),
            docstring=_docstring_of(tree.root_node, source),
            parent_qualified_name=None,
        )
    )
    extractor.walk_block(
        list(tree.root_node.named_children),
        file_qname,
        file_qname,
        in_class=False,
        extract_variables=True,
    )

    return ParsedFile(
        path=rel_path,
        lang="python",
        blob_sha=blob_sha,
        n_lines=n_lines,
        symbols=extractor.symbols,
        references=extractor.references,
    )
