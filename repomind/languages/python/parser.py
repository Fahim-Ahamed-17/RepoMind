"""Tree-sitter-based Python symbol extraction.

RM-017. Produces symbols only -- classes, functions, methods, and
module/class-level variables, each with a qualified name, span, signature
(for callables), and docstring. Edge extraction (imports, calls, inherits)
is M2's job (RM-020 onward); this module does not look at
``import_statement`` or ``call`` nodes at all yet, even though they were
observed while exploring the grammar for this ticket.

Scope, deliberately bounded:
  * Variables are extracted at module level and directly inside a class
    body only -- not inside function bodies. Indexing every local variable
    would multiply the symbol count many times over for no retrieval value;
    module/class-level names are the ones another file can actually
    reference.
  * Only a plain ``target`` (optionally ``: type`` and/or ``= value``)
    counts as a variable symbol -- this also covers bare dataclass-style
    field declarations (``x: int``, no value), which are common in this
    codebase itself. Tuple unpacking and augmented assignment (``x += 1``)
    are not treated as symbol definitions -- a reasonable simplification
    for what is fundamentally a best-effort heuristic signal, not a
    name-resolution engine (that is SCIP's job).
  * A decorated definition's symbol span starts at the decorator line, not
    the ``def``/``class`` keyword, since "where this symbol is" reads more
    usefully that way for citations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tree_sitter
import tree_sitter_python

from repomind.model import ParsedFile, ParsedSymbol, SymbolKind

if TYPE_CHECKING:
    from pathlib import Path

_LANGUAGE = tree_sitter.Language(tree_sitter_python.language())


def _file_qualified_name(rel_path: str) -> str:
    """The dotted import name a Python file would have, e.g.
    ``repomind/store/base.py`` -> ``repomind.store.base``, and an
    ``__init__.py`` -> its containing package's own name.
    """
    parts = rel_path.removesuffix(".py").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else rel_path


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
) -> tuple[tree_sitter.Node, tree_sitter.Point]:
    """If ``node`` is a ``decorated_definition``, return its inner
    function/class definition plus the *decorated* node's own start point
    (so the symbol's span includes the decorator lines). Otherwise return
    ``node`` unchanged with its own start point.
    """
    if node.type != "decorated_definition":
        return node, node.start_point
    inner = node.named_children[-1]
    return inner, node.start_point


class _Extractor:
    """Holds the per-file state (source bytes, accumulated symbols) that
    would otherwise have to thread through every recursive call.
    """

    def __init__(self, source: bytes) -> None:
        self._source = source
        self.symbols: list[ParsedSymbol] = []

    def walk_block(
        self,
        statements: list[tree_sitter.Node],
        scope_prefix: str,
        *,
        in_class: bool,
        extract_variables: bool,
    ) -> None:
        for raw in statements:
            node, start_point = _unwrap_decorated(raw)

            if node.type == "function_definition":
                self._visit_function(node, start_point, scope_prefix, is_method=in_class)
            elif node.type == "class_definition":
                self._visit_class(node, start_point, scope_prefix)
            elif extract_variables and node.type == "expression_statement":
                self._visit_possible_variable(node, scope_prefix)

    def _visit_function(
        self,
        node: tree_sitter.Node,
        start_point: tree_sitter.Point,
        scope_prefix: str,
        *,
        is_method: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(self._source, name_node)
        qualified_name = f"{scope_prefix}.{name}" if scope_prefix else name
        _, end_line = _line_span(node)

        self.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.METHOD if is_method else SymbolKind.FUNCTION,
                name=name,
                qualified_name=qualified_name,
                start_line=start_point.row + 1,
                end_line=end_line,
                signature=_signature_of(node, self._source),
                docstring=_docstring_of(node.child_by_field_name("body"), self._source),
            )
        )

        body = node.child_by_field_name("body")
        if body is not None:
            # Nested defs (closures) still get symbols -- variables inside a
            # function body deliberately do not, per the module docstring.
            self.walk_block(
                list(body.named_children),
                qualified_name,
                in_class=False,
                extract_variables=False,
            )

    def _visit_class(
        self, node: tree_sitter.Node, start_point: tree_sitter.Point, scope_prefix: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = _text(self._source, name_node)
        qualified_name = f"{scope_prefix}.{name}" if scope_prefix else name
        _, end_line = _line_span(node)

        self.symbols.append(
            ParsedSymbol(
                kind=SymbolKind.CLASS,
                name=name,
                qualified_name=qualified_name,
                start_line=start_point.row + 1,
                end_line=end_line,
                signature=None,
                docstring=_docstring_of(node.child_by_field_name("body"), self._source),
            )
        )

        body = node.child_by_field_name("body")
        if body is not None:
            self.walk_block(
                list(body.named_children),
                qualified_name,
                in_class=True,
                extract_variables=True,
            )

    def _visit_possible_variable(self, expr_stmt: tree_sitter.Node, scope_prefix: str) -> None:
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
            )
        )


def parse_python_file(path: Path, text: str, blob_sha: str) -> ParsedFile:
    source = text.encode("utf-8")
    parser = tree_sitter.Parser(_LANGUAGE)
    tree = parser.parse(source)

    rel_path = path.as_posix()
    file_qname = _file_qualified_name(rel_path)
    n_lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

    extractor = _Extractor(source)
    extractor.symbols.append(
        ParsedSymbol(
            kind=SymbolKind.FILE,
            name=path.name,
            qualified_name=file_qname,
            start_line=1,
            end_line=max(n_lines, 1),
            docstring=_docstring_of(tree.root_node, source),
        )
    )
    extractor.walk_block(
        list(tree.root_node.named_children),
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
    )
