"""Symbol-boundary chunking (RM-030, F-5): cut one parsed file into
retrieval units along the boundaries tree-sitter already found, not fixed
windows -- "a function is the natural retrieval unit; fixed windows split
bodies mid-logic" (design.md section 10).

Only top-level ``FUNCTION``/``CLASS`` symbols are chunk anchors -- a
top-level class's own span already includes every method nested inside
it (tree-sitter's ``class_definition`` node covers its whole body), so
chunking each nested method too would just duplicate that same text in
two chunks. Module-level ``VARIABLE`` symbols and the file symbol itself
are never anchors either: a bare assignment is not a meaningful retrieval
unit on its own (design.md section 10's own example is a *function*).
Nothing about this is Python-specific in principle, but this module reads
:class:`~repomind.model.ParsedSymbol` data only -- it never re-parses, so
it stays in ``index/`` rather than ``languages/python/`` (design.md's own
file tree places it here) and needs no per-language dispatch to add a
second language later.

Sizing rule, applied per top-level symbol, verbatim from design.md
section 10 ("Chunk sizing"):

  * Shorter than :data:`MERGE_BELOW_TOKENS`: not its own chunk at all --
    folded into the surrounding non-symbol text (see ``_sweep`` below),
    which *is* "its parent scope" for a top-level symbol, whose parent is
    the file/module itself.
  * Longer than :data:`SPLIT_ABOVE_TOKENS`: split into several chunks
    (see :func:`_split_oversized`) along blank-line boundaries -- the
    practical proxy for "statement boundary" available without this
    module depending on tree-sitter directly, which would mean either
    Python-specific code here or a second traversal through a language
    pack this module has no other reason to need. Each part after the
    first repeats a synthesised signature/docstring header so every part
    stays self-describing (design.md: "signature and docstring repeated
    in each part").
  * Otherwise: exactly one chunk, the symbol's own span verbatim.

Everything not covered by a normal-or-split symbol chunk -- module
docstrings, imports, top-level variables, and any symbol that was folded
away for being too small -- becomes its own "remainder" chunk
(``symbol_qualified_name=None``, mirroring :attr:`Chunk.symbol_id`'s own
``NULL`` case), one per contiguous gap. A gap that is pure whitespace
produces no chunk at all -- an empty retrieval unit helps no one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import tiktoken

from repomind.model import ParsedChunk, SymbolKind

if TYPE_CHECKING:
    from repomind.model import ParsedFile, ParsedSymbol

#: design.md section 10: "Symbols shorter than 50 tokens merge with their
#: parent scope."
MERGE_BELOW_TOKENS = 50

#: design.md section 10: "Symbols longer than 800 tokens split at
#: statement boundaries."
SPLIT_ABOVE_TOKENS = 800

#: "A function is the natural retrieval unit" (design.md section 10) --
#: METHOD is deliberately absent: a top-level CLASS's own span already
#: contains every one of its methods, so methods are never independent
#: chunk anchors, only ever content inside their class's chunk (or inside
#: a split part of it, if the class as a whole is oversized).
_ANCHOR_KINDS = frozenset({SymbolKind.FUNCTION, SymbolKind.CLASS})

ENCODING_NAME = "cl100k_base"

#: The vendored copy of ``cl100k_base``'s merge-rank table, shipped inside
#: the package. ``tiktoken`` otherwise downloads it from
#: ``openaipublic.blob.core.windows.net`` the first time an encoding is
#: built, which would put an HTTP call squarely in the indexing path and
#: break AGENTS.md invariant 1 -- chunking calls :func:`count_tokens` for
#: every file. That is not hypothetical: it is exactly what CI caught,
#: with every chunking and indexing test failing on a cold cache while
#: passing on developer machines whose cache was already warm.
#:
#: The filename is not arbitrary -- ``tiktoken`` looks its cache entries
#: up by ``sha1`` of the blob URL, so this name is what makes the file
#: findable. See that directory's README.
_BUNDLED_CACHE_DIR = Path(__file__).parent / "tiktoken_cache"

#: `tiktoken`'s own encoding load has real cost (parsing its merge-rank
#: table); built once, lazily, and reused for every file in a run rather
#: than per call. design.md section 10: "tiktoken for budgeting ...
#: approximate for non-OpenAI providers; adequate for budget arithmetic"
#: -- cl100k_base is not tied to any one provider, just a reasonably
#: representative encoding for that approximation.
_encoding: tiktoken.Encoding | None = None


def _load_encoding() -> tiktoken.Encoding:
    """Build the encoding from the vendored table, never the network.

    ``TIKTOKEN_CACHE_DIR`` is the only knob ``tiktoken`` offers for this,
    and it is read at call time -- so it is set around this one call and
    restored afterwards rather than mutated for the whole process. Set
    unconditionally, not just when unset: a caller with their own
    ``TIKTOKEN_CACHE_DIR`` pointing somewhere that lacks this blob would
    otherwise send us straight back to the network, which is the one
    outcome this function exists to rule out.
    """
    previous = os.environ.get("TIKTOKEN_CACHE_DIR")
    os.environ["TIKTOKEN_CACHE_DIR"] = str(_BUNDLED_CACHE_DIR)
    try:
        return tiktoken.get_encoding(ENCODING_NAME)
    finally:
        if previous is None:
            os.environ.pop("TIKTOKEN_CACHE_DIR", None)
        else:
            os.environ["TIKTOKEN_CACHE_DIR"] = previous


def count_tokens(text: str) -> int:
    global _encoding
    if _encoding is None:
        _encoding = _load_encoding()
    return len(_encoding.encode(text, disallowed_special=()))


def chunk_file(parsed: ParsedFile, text: str) -> list[ParsedChunk]:
    """Chunk one already-parsed file's source text.

    ``text`` is the same decoded source ``index/pipeline.py`` already has
    in hand from reading the file -- this module never touches the
    filesystem itself, matching ``index/resolve.py``'s own boundary (act
    on data already extracted, not a second I/O pass).
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    file_symbol = next((s for s in parsed.symbols if s.kind == SymbolKind.FILE), None)
    file_qualified_name = file_symbol.qualified_name if file_symbol is not None else None

    anchors = sorted(
        (
            s
            for s in parsed.symbols
            if s.kind in _ANCHOR_KINDS and s.parent_qualified_name == file_qualified_name
        ),
        key=lambda s: s.start_line,
    )

    return _sweep(anchors, lines)


def _sweep(anchors: list[ParsedSymbol], lines: list[str]) -> list[ParsedChunk]:
    """Walk the file top to bottom, alternating remainder chunks (gaps,
    including any anchor folded away for being too small) with the
    anchors sized normally or split for being too large. Never overlaps,
    never skips a line -- every chunk's span sits end-to-end with its
    neighbours' across the whole file.
    """
    chunks: list[ParsedChunk] = []
    pending_start = 1

    def flush_remainder(end_line_inclusive: int) -> None:
        nonlocal pending_start
        if end_line_inclusive >= pending_start:
            span_text = _join(lines, pending_start, end_line_inclusive)
            if span_text.strip():
                chunks.append(
                    ParsedChunk(
                        start_line=pending_start,
                        end_line=end_line_inclusive,
                        text=span_text,
                        n_tokens=count_tokens(span_text),
                        symbol_qualified_name=None,
                    )
                )
        pending_start = end_line_inclusive + 1

    for sym in anchors:
        span_text = _join(lines, sym.start_line, sym.end_line)
        n_tokens = count_tokens(span_text)
        if n_tokens < MERGE_BELOW_TOKENS:
            continue  # too small to stand alone -- left for the next flush_remainder to absorb

        flush_remainder(sym.start_line - 1)
        if n_tokens > SPLIT_ABOVE_TOKENS:
            chunks.extend(_split_oversized(sym, lines))
        else:
            chunks.append(
                ParsedChunk(
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    text=span_text,
                    n_tokens=n_tokens,
                    symbol_qualified_name=sym.qualified_name,
                )
            )
        pending_start = sym.end_line + 1

    flush_remainder(len(lines))
    return chunks


def _split_oversized(sym: ParsedSymbol, lines: list[str]) -> list[ParsedChunk]:
    """Cutting only at blank lines means the actual size of a resulting
    part is approximate, not a hard guarantee -- exactly like tiktoken's
    own role here (design.md: "approximate ... adequate for budget
    arithmetic"). Targeting the *middle* of design.md's own 200-800
    "target" range, rather than shooting for 800 itself, keeps the
    typical case within that range even after whatever a real file's
    blank-line spacing adds on top; a stretch with no blank line at all
    for a long stretch can still overshoot 800, same as it would for any
    boundary-respecting splitter -- there is no way to keep every part
    under a hard cap without sometimes cutting mid-statement, which is
    the one thing this rule exists to avoid.
    """
    header = _synthetic_header(sym)
    target = max(SPLIT_ABOVE_TOKENS // 2 - count_tokens(header), MERGE_BELOW_TOKENS)

    span_lines = lines[sym.start_line - 1 : sym.end_line]
    parts: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for line in span_lines:
        current.append(line)
        current_tokens += count_tokens(line)
        if current_tokens >= target and not line.strip():
            parts.append(current)
            current = []
            current_tokens = 0
    if current:
        # Also covers "no blank line anywhere in the whole span" (e.g. one
        # single 900-token line): the loop above never cuts, so the entire
        # span ends up here, in one part, emitted whole rather than
        # looping forever looking for a boundary that will never appear.
        parts.append(current)

    chunks: list[ParsedChunk] = []
    line_cursor = sym.start_line
    for i, part_lines in enumerate(parts):
        part_text = "".join(part_lines)
        text = part_text if i == 0 else f"{header}\n{part_text}"
        chunks.append(
            ParsedChunk(
                start_line=line_cursor,
                end_line=line_cursor + len(part_lines) - 1,
                text=text,
                n_tokens=count_tokens(text),
                symbol_qualified_name=sym.qualified_name,
            )
        )
        line_cursor += len(part_lines)
    return chunks


def _synthetic_header(sym: ParsedSymbol) -> str:
    """A reconstructed, not literal, signature/docstring block -- avoids
    needing this symbol's exact original header line span (which would
    mean re-parsing; see this module's own docstring on staying
    tree-sitter-free). Only part one of a split symbol's chunks carries
    the *real* header, naturally, as the first slice of its own original
    text; this is only for the parts after it.
    """
    if sym.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and sym.signature:
        header = f"def {sym.signature}:"
    else:
        header = f"class {sym.name}:"
    if sym.docstring:
        header += f'\n    """{sym.docstring}"""'
    return header


def _join(lines: list[str], start_line: int, end_line: int) -> str:
    """``lines[start_line-1:end_line]`` joined back into text -- ``lines``
    is 0-indexed, spans here are the same 1-indexed, inclusive convention
    every other span in this codebase uses (``ParsedSymbol.start_line``,
    ``Symbol.start_line``, ...).
    """
    return "".join(lines[start_line - 1 : end_line])
