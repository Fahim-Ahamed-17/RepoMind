"""Tests for RM-030's symbol-boundary chunker.

Builds small, explicit ``ParsedFile``/``ParsedSymbol`` data by hand for
the merge/split/remainder rules themselves -- same approach
tests/unit/test_resolve.py takes for the heuristic resolver -- plus a
handful of tests running the real tree-sitter parser against
tests/fixtures/simple to prove the two modules actually agree on shape.

Every hand-built "normal-sized" or "oversized" function body below uses
``_padded_line`` repeated enough times to actually cross
``MERGE_BELOW_TOKENS``/``SPLIT_ABOVE_TOKENS`` -- checked against the real
``count_tokens`` in each test that depends on it, never assumed, since a
literal one-line ``def f(): return 1`` is itself only a handful of
tokens and would otherwise silently exercise the *merge* rule instead of
whatever the test actually means to check.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest
from repomind.index import chunker
from repomind.index.chunker import (
    MERGE_BELOW_TOKENS,
    SPLIT_ABOVE_TOKENS,
    chunk_file,
    count_tokens,
)
from repomind.languages.python import PYTHON_LANGUAGE_PACK
from repomind.model import ParsedFile, ParsedSymbol, SymbolKind


def _sym(
    kind: SymbolKind,
    name: str,
    start: int,
    end: int,
    *,
    parent: str = "m",
    signature: str | None = None,
    docstring: str | None = None,
) -> ParsedSymbol:
    return ParsedSymbol(
        kind=kind,
        name=name,
        qualified_name=f"{parent}.{name}",
        start_line=start,
        end_line=end,
        signature=signature,
        docstring=docstring,
        parent_qualified_name=parent,
    )


def _file_sym() -> ParsedSymbol:
    return ParsedSymbol(
        kind=SymbolKind.FILE,
        name="m",
        qualified_name="m",
        start_line=1,
        end_line=1,
        parent_qualified_name=None,
    )


def _parsed(symbols: list[ParsedSymbol], text: str) -> ParsedFile:
    return ParsedFile(
        path="m.py", lang="python", blob_sha="s", n_lines=len(text.splitlines()), symbols=symbols
    )


def _padded_line(n: int) -> str:
    """One source-like line, long enough that repeating it a known number
    of times reliably crosses a token threshold without hardcoding
    tiktoken's own count for any particular string.
    """
    return f"    value_{n} = compute_something(value_{n - 1}, constant_{n})\n"


def _normal_function(name: str, start_line: int) -> tuple[str, int]:
    """~150-200 tokens -- comfortably between MERGE_BELOW_TOKENS and
    SPLIT_ABOVE_TOKENS, unlike a literal one-line ``def f(): return 1``
    (a handful of tokens, i.e. itself squarely a *merge* case). Returns
    (text, end_line).
    """
    body = "".join(_padded_line(i) for i in range(1, 10))
    text = f'def {name}():\n    """A function long enough to be its own chunk."""\n{body}'
    return text, start_line + len(text.splitlines()) - 1


def _tiny_function(name: str, start_line: int) -> tuple[str, int]:
    """A handful of tokens -- genuinely below MERGE_BELOW_TOKENS."""
    text = f"def {name}():\n    pass\n"
    return text, start_line + 1


# -- count_tokens -----------------------------------------------------------


def test_count_tokens_of_empty_string_is_zero() -> None:
    assert count_tokens("") == 0


def test_count_tokens_is_positive_for_real_text() -> None:
    assert count_tokens("def f():\n    return 1\n") > 0


def test_building_the_encoding_never_needs_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENTS.md invariant 1, at the one place it nearly broke: tiktoken
    downloads ``cl100k_base`` on first use, and chunking builds it for
    every indexed file. ``index/tiktoken_cache/`` vendors the table so it
    never does.

    Pointing ``TIKTOKEN_CACHE_DIR`` at an empty directory here is what
    makes this a real test rather than a tautology -- it removes the warm
    cache a developer machine always has, which is exactly why the
    original bug passed locally and failed on every CI runner. Any fetch
    is blocked by pytest-socket (``--disable-socket``), so a regression
    fails here loudly instead of silently reaching the network.
    """
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(chunker, "_encoding", None)  # force a fresh load

    assert chunker.count_tokens("def f():\n    return 1\n") > 0
    assert not list(tmp_path.iterdir())  # nothing downloaded into the empty cache


# -- structural behaviour: remainder chunks ----------------------------------


def test_a_single_normal_sized_function_becomes_one_chunk() -> None:
    text, end = _normal_function("f", 1)
    assert MERGE_BELOW_TOKENS <= count_tokens(text) <= SPLIT_ABOVE_TOKENS
    parsed = _parsed([_file_sym(), _sym(SymbolKind.FUNCTION, "f", 1, end)], text)

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 1
    assert chunks[0].symbol_qualified_name == "m.f"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == end
    assert chunks[0].text == text


def test_content_before_the_first_symbol_becomes_a_remainder_chunk() -> None:
    fn_text, fn_end = _normal_function("f", 4)
    text = '"""Module docstring."""\n\n\n' + fn_text
    parsed = _parsed([_file_sym(), _sym(SymbolKind.FUNCTION, "f", 4, fn_end)], text)

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 2
    remainder, fn = chunks
    assert remainder.symbol_qualified_name is None
    assert remainder.start_line == 1
    assert remainder.end_line == 3
    assert "Module docstring" in remainder.text
    assert fn.symbol_qualified_name == "m.f"


def test_content_after_the_last_symbol_becomes_a_remainder_chunk() -> None:
    fn_text, fn_end = _normal_function("f", 1)
    text = fn_text + "\n\nMAIN_CONSTANT = f()\n"
    parsed = _parsed([_file_sym(), _sym(SymbolKind.FUNCTION, "f", 1, fn_end)], text)

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 2
    fn, remainder = chunks
    assert fn.symbol_qualified_name == "m.f"
    assert remainder.symbol_qualified_name is None
    assert remainder.start_line == fn_end + 1
    assert "MAIN_CONSTANT" in remainder.text


def test_gap_between_two_symbols_becomes_its_own_remainder_chunk() -> None:
    f_text, f_end = _normal_function("f", 1)
    gap_start = f_end + 1
    g_start = gap_start + 5  # 2 blank lines + "GLUE = 1" + 2 blank lines
    g_text, g_end = _normal_function("g", g_start)
    text = f"{f_text}\n\nGLUE = 1\n\n{g_text}"
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.FUNCTION, "f", 1, f_end),
            _sym(SymbolKind.FUNCTION, "g", g_start, g_end),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert [c.symbol_qualified_name for c in chunks] == ["m.f", None, "m.g"]
    gap = chunks[1]
    assert "GLUE" in gap.text
    assert gap.start_line == gap_start
    assert gap.end_line == g_start - 1


def test_purely_whitespace_gap_produces_no_chunk() -> None:
    f_text, f_end = _normal_function("f", 1)
    g_start = f_end + 3
    g_text, g_end = _normal_function("g", g_start)
    text = f"{f_text}\n\n{g_text}"
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.FUNCTION, "f", 1, f_end),
            _sym(SymbolKind.FUNCTION, "g", g_start, g_end),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert [c.symbol_qualified_name for c in chunks] == ["m.f", "m.g"]


def test_file_with_no_chunkable_symbols_is_one_remainder_chunk() -> None:
    text = "A = 1\nB = 2\n"
    parsed = _parsed(
        [_file_sym(), _sym(SymbolKind.VARIABLE, "A", 1, 1), _sym(SymbolKind.VARIABLE, "B", 2, 2)],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 1
    assert chunks[0].symbol_qualified_name is None
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2


def test_empty_file_produces_no_chunks() -> None:
    parsed = _parsed([_file_sym()], "")
    assert chunk_file(parsed, "") == []


def test_top_level_variable_is_not_a_chunk_anchor() -> None:
    fn_text, fn_end = _normal_function("f", 4)
    text = "CONFIG = {}\n\n\n" + fn_text
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.VARIABLE, "CONFIG", 1, 1),
            _sym(SymbolKind.FUNCTION, "f", 4, fn_end),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    # CONFIG never gets its own chunk with symbol_qualified_name="m.CONFIG"
    # -- it is part of the leading remainder instead.
    assert [c.symbol_qualified_name for c in chunks] == [None, "m.f"]


def test_nested_method_is_not_its_own_chunk_anchor() -> None:
    body = "".join(_padded_line(i) for i in range(1, 10))
    text = f'class C:\n    def m(self):\n        """A method long enough to matter."""\n{body}'
    end = len(text.splitlines())
    assert count_tokens(text) >= MERGE_BELOW_TOKENS
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.CLASS, "C", 1, end),
            _sym(SymbolKind.METHOD, "m", 2, end, parent="m.C"),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    # One chunk for the whole class (methods included), not a separate
    # one for C.m -- a top-level class's own span already covers it.
    assert len(chunks) == 1
    assert chunks[0].symbol_qualified_name == "m.C"
    assert chunks[0].text == text


def test_chunks_cover_the_whole_file_contiguously() -> None:
    f_text, f_end = _normal_function("f", 4)
    g_start = f_end + 3
    g_text, g_end = _normal_function("g", g_start)
    text = '"""doc"""\n\n\n' + f_text + "\n\nGLUE = 1\n\n" + g_text + "\n\nTRAILER = 1\n"
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.FUNCTION, "f", 4, f_end),
            _sym(SymbolKind.FUNCTION, "g", g_start, g_end),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    # Every real line belongs to exactly one chunk once whitespace-only
    # gaps (which produce no chunk) are accounted for -- no line missing,
    # none double-counted.
    covered: set[int] = set()
    for c in chunks:
        span = set(range(c.start_line, c.end_line + 1))
        assert not (span & covered), "overlapping chunk spans"
        covered |= span
    total_lines = len(text.splitlines())
    non_blank = {i for i, line in enumerate(text.splitlines(), start=1) if line.strip()}
    assert non_blank <= covered
    assert covered <= set(range(1, total_lines + 1))


# -- merge rule: symbols shorter than MERGE_BELOW_TOKENS ---------------------


def test_a_tiny_function_is_folded_into_the_surrounding_remainder() -> None:
    fn_text, fn_end = _tiny_function("f", 1)
    assert count_tokens(fn_text) < MERGE_BELOW_TOKENS
    text = fn_text + "\n\nGLUE = 1\n"
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.FUNCTION, "f", 1, fn_end),
            _sym(SymbolKind.VARIABLE, "GLUE", fn_end + 3, fn_end + 3),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 1
    assert chunks[0].symbol_qualified_name is None
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == len(text.splitlines())


def test_multiple_consecutive_tiny_functions_fold_into_one_remainder() -> None:
    f_text, f_end = _tiny_function("f", 1)
    g_start = f_end + 3
    g_text, g_end = _tiny_function("g", g_start)
    text = f"{f_text}\n\n{g_text}"
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.FUNCTION, "f", 1, f_end),
            _sym(SymbolKind.FUNCTION, "g", g_start, g_end),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 1
    assert chunks[0].symbol_qualified_name is None
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == g_end


# -- split rule: symbols longer than SPLIT_ABOVE_TOKENS ----------------------


def _oversized_function_text() -> tuple[str, int, int]:
    """A function whose body alone comfortably exceeds SPLIT_ABOVE_TOKENS,
    with a blank line every ~90 lines (empirically, comfortably under
    SPLIT_ABOVE_TOKENS worth of ``_padded_line`` text) so the split loop
    actually has a boundary to land on before overshooting the target by
    a wide margin. Returns (text, start_line, end_line) for the whole
    function.
    """
    body_lines: list[str] = []
    for i in range(1, 400):
        body_lines.append(_padded_line(i))
        if i % 25 == 0:
            body_lines.append("\n")
    text = 'def f():\n    """A function with a very long body."""\n' + "".join(body_lines)
    return text, 1, len(text.splitlines())


def test_an_oversized_function_is_split_into_multiple_chunks() -> None:
    text, start, end = _oversized_function_text()
    assert count_tokens(text) > SPLIT_ABOVE_TOKENS
    parsed = _parsed(
        [
            _file_sym(),
            _sym(
                SymbolKind.FUNCTION,
                "f",
                start,
                end,
                signature="f()",
                docstring="A function with a very long body.",
            ),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) > 1
    assert all(c.symbol_qualified_name == "m.f" for c in chunks)
    for c in chunks:
        assert c.n_tokens <= SPLIT_ABOVE_TOKENS


def test_split_parts_after_the_first_repeat_a_synthetic_header() -> None:
    text, start, end = _oversized_function_text()
    parsed = _parsed(
        [
            _file_sym(),
            _sym(
                SymbolKind.FUNCTION,
                "f",
                start,
                end,
                signature="f()",
                docstring="A function with a very long body.",
            ),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) > 1
    assert "def f():" not in chunks[0].text[10:]  # part 1: the real header only, once, at the top
    for part in chunks[1:]:
        assert "def f():" in part.text
        assert "A function with a very long body." in part.text


def test_an_oversized_class_repeats_a_class_header_not_a_def_header() -> None:
    # Classes never get a ParsedSymbol.signature (only functions/methods
    # do -- languages/python/parser.py), so the synthetic header for an
    # oversized class falls back to its own distinct branch: `class
    # Name:`, not `def Name():`.
    body_lines: list[str] = []
    for i in range(1, 400):
        body_lines.append(_padded_line(i))
        if i % 25 == 0:
            body_lines.append("\n")
    text = 'class C:\n    """A class with a very long body."""\n' + "".join(body_lines)
    end = len(text.splitlines())
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.CLASS, "C", 1, end, docstring="A class with a very long body."),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) > 1
    assert all(c.symbol_qualified_name == "m.C" for c in chunks)
    for part in chunks[1:]:
        assert "class C:" in part.text
        assert "A class with a very long body." in part.text


def test_split_parts_line_spans_are_contiguous_and_cover_the_whole_symbol() -> None:
    text, start, end = _oversized_function_text()
    parsed = _parsed(
        [
            _file_sym(),
            _sym(SymbolKind.FUNCTION, "f", start, end, signature="f()", docstring="doc"),
        ],
        text,
    )

    chunks = chunk_file(parsed, text)

    assert len(chunks) > 1
    assert chunks[0].start_line == start
    assert chunks[-1].end_line == end
    for prev, nxt in pairwise(chunks):
        assert nxt.start_line == prev.end_line + 1


def test_a_span_with_no_blank_line_is_emitted_whole_rather_than_looping_forever() -> None:
    # One giant single line (no blank line for the split loop to land on)
    # -- must terminate by emitting it whole, not hang or crash.
    huge_line = "x = " + " + ".join(f"value_{i}" for i in range(2000)) + "\n"
    text = "def f():\n" + huge_line
    parsed = _parsed(
        [_file_sym(), _sym(SymbolKind.FUNCTION, "f", 1, 2, signature="f()")],
        text,
    )
    assert count_tokens(text) > SPLIT_ABOVE_TOKENS

    chunks = chunk_file(parsed, text)

    assert len(chunks) == 1
    assert chunks[0].symbol_qualified_name == "m.f"
    assert chunks[0].start_line == 1
    assert chunks[0].end_line == 2


# -- interop with the real parser --------------------------------------------


def test_chunking_the_real_simple_fixture_produces_sane_chunks() -> None:
    path = Path(__file__).parent.parent / "fixtures" / "simple" / "pkg" / "module_a.py"
    text = path.read_text(encoding="utf-8")
    parsed = PYTHON_LANGUAGE_PACK.parse_file(Path("pkg/module_a.py"), text, blob_sha="s")

    chunks = chunk_file(parsed, text)

    names = {c.symbol_qualified_name for c in chunks}
    assert "pkg.module_a.Widget" in names  # the class, methods included
    # make_widget is only a few lines -- genuinely below MERGE_BELOW_TOKENS,
    # so (correctly) it is not its own anchor; it lives in the remainder.
    assert "pkg.module_a.make_widget" not in names
    # Widget.render is never its own chunk either -- it lives inside Widget's.
    assert "pkg.module_a.Widget.render" not in names
    for c in chunks:
        assert c.text  # never an empty chunk
        assert c.n_tokens == count_tokens(c.text)
