from __future__ import annotations

from pathlib import Path

from repomind.languages.python import PYTHON_LANGUAGE_PACK
from repomind.model import SymbolKind


def _parse(text: str, path: str = "m.py"):
    return PYTHON_LANGUAGE_PACK.parse_file(Path(path), text, blob_sha="sha")


def _by_qname(pf, qname: str):
    return next(s for s in pf.symbols if s.qualified_name == qname)


def test_file_symbol_uses_dotted_module_name() -> None:
    pf = _parse("x = 1\n", path="pkg/sub/mod.py")
    file_sym = pf.symbols[0]
    assert file_sym.kind == SymbolKind.FILE
    assert file_sym.qualified_name == "pkg.sub.mod"


def test_init_py_qualified_name_is_the_package_itself() -> None:
    pf = _parse("x = 1\n", path="pkg/sub/__init__.py")
    assert pf.symbols[0].qualified_name == "pkg.sub"


def test_root_level_init_py_qualified_name_is_bare_init_not_the_raw_path() -> None:
    """The indexed repo's own root can itself be a package, with no
    containing directory to strip __init__ down to -- found via
    tests/fixtures/cyclic's own __init__.py, whose qualified name silently
    came out as the literal ``"__init__.py"`` (path, extension and all,
    not a dotted name) before this was fixed.
    """
    pf = _parse("x = 1\n", path="__init__.py")
    assert pf.symbols[0].qualified_name == "__init__"


def test_class_and_method_kinds_and_nesting() -> None:
    pf = _parse("class Outer:\n    class Inner:\n        def method(self):\n            pass\n")
    outer = _by_qname(pf, "m.Outer")
    inner = _by_qname(pf, "m.Outer.Inner")
    method = _by_qname(pf, "m.Outer.Inner.method")
    assert outer.kind == SymbolKind.CLASS
    assert inner.kind == SymbolKind.CLASS
    assert method.kind == SymbolKind.METHOD


def test_module_level_function_is_function_not_method() -> None:
    pf = _parse("def f():\n    pass\n")
    assert _by_qname(pf, "m.f").kind == SymbolKind.FUNCTION


def test_nested_function_inside_function_is_still_a_function() -> None:
    pf = _parse("def outer():\n    def inner():\n        pass\n    return inner\n")
    assert _by_qname(pf, "m.outer.inner").kind == SymbolKind.FUNCTION


def test_async_def_is_extracted_like_a_regular_function() -> None:
    pf = _parse("async def f(x):\n    return x\n")
    f = _by_qname(pf, "m.f")
    assert f.kind == SymbolKind.FUNCTION
    assert f.signature == "f(x)"


def test_decorated_definition_span_includes_the_decorator_line() -> None:
    pf = _parse("@decorator\ndef f():\n    pass\n")
    f = _by_qname(pf, "m.f")
    assert f.start_line == 1  # the @decorator line, not the def line (2)


def test_stacked_decorators_span_the_topmost_one() -> None:
    pf = _parse("@a\n@b\n@c\ndef f():\n    pass\n")
    assert _by_qname(pf, "m.f").start_line == 1


def test_docstrings_captured_for_module_class_and_function() -> None:
    pf = _parse(
        '"""mod doc"""\nclass C:\n    """class doc"""\n    def m(self):\n        """method doc"""\n'
    )
    assert pf.symbols[0].docstring == "mod doc"
    assert _by_qname(pf, "m.C").docstring == "class doc"
    assert _by_qname(pf, "m.C.m").docstring == "method doc"


def test_trailing_string_is_not_mistaken_for_a_docstring() -> None:
    pf = _parse("class C:\n    x = 1\n    'not a docstring'\n")
    assert _by_qname(pf, "m.C").docstring is None


def test_variable_with_value_is_extracted() -> None:
    pf = _parse("X = 1\n")
    v = _by_qname(pf, "m.X")
    assert v.kind == SymbolKind.VARIABLE


def test_bare_annotated_declaration_without_value_is_extracted() -> None:
    """Common in dataclasses -- `x: int` with no `=`. See parser.py's
    module docstring: this is a real plus over the originally-stated
    "target = value" scope, verified against this codebase's own style.
    """
    pf = _parse("class C:\n    x: int\n")
    v = _by_qname(pf, "m.C.x")
    assert v.kind == SymbolKind.VARIABLE


def test_tuple_unpacking_is_not_extracted_as_a_symbol() -> None:
    pf = _parse("a, b = 1, 2\n")
    names = {s.qualified_name for s in pf.symbols}
    assert "m.a" not in names
    assert "m.b" not in names


def test_augmented_assignment_is_not_extracted_as_a_symbol() -> None:
    pf = _parse("x = 1\nx += 1\n")
    # exactly one VARIABLE symbol named x, not two
    assert sum(1 for s in pf.symbols if s.qualified_name == "m.x") == 1


def test_variables_inside_function_bodies_are_not_extracted() -> None:
    pf = _parse("def f():\n    local = 1\n    return local\n")
    names = {s.qualified_name for s in pf.symbols}
    assert "m.f.local" not in names
    assert not any(n.endswith(".local") for n in names)


def test_class_level_variables_are_extracted_but_not_inside_their_methods() -> None:
    pf = _parse(
        "class C:\n    count = 0\n    def m(self):\n        local = 1\n        return local\n"
    )
    names = {s.qualified_name for s in pf.symbols}
    assert "m.C.count" in names
    assert "m.C.m.local" not in names


def test_signature_reflects_defaults_and_annotations_verbatim() -> None:
    pf = _parse("def f(a: int, b: str = 'x') -> bool:\n    return True\n")
    assert _by_qname(pf, "m.f").signature == "f(a: int, b: str = 'x') -> bool"


def test_empty_file_yields_only_the_file_symbol() -> None:
    pf = _parse("")
    assert len(pf.symbols) == 1
    assert pf.symbols[0].kind == SymbolKind.FILE
    assert pf.n_lines == 0
    assert pf.symbols[0].end_line == 1  # span is never zero-length


def test_malformed_source_does_not_raise() -> None:
    """Tree-sitter is error-tolerant; the parser must not add a hard
    requirement of syntactic validity on top of that (LanguagePack
    protocol: "must not raise on malformed/unparseable source").
    """
    pf = _parse("def f(:\n    this is not valid python at all !!!\nclass \n")
    assert pf.symbols[0].kind == SymbolKind.FILE  # at minimum, the file itself


def test_line_numbers_are_one_indexed() -> None:
    pf = _parse("def f():\n    pass\n")
    f = _by_qname(pf, "m.f")
    assert f.start_line == 1
    assert f.end_line == 2
