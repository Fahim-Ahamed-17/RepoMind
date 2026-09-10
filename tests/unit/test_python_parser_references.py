"""Tests for RM-020's raw-reference extraction: imports, inherits, calls,
references (decorators + annotations), and defines (parent tracking).

Deliberately a separate file from test_python_parser.py: that file is about
*symbols*, this one is about the *references* layered on top -- mixing the
two would make either file's failure output harder to scan for what
actually broke. See docs/conventions.md "one concept per module", applied
here to tests too.
"""

from __future__ import annotations

from pathlib import Path

from repomind.languages.python import PYTHON_LANGUAGE_PACK
from repomind.model import EdgeKind, ParsedFile, ParsedReference, SymbolKind


def _parse(text: str, path: str = "m.py") -> ParsedFile:
    return PYTHON_LANGUAGE_PACK.parse_file(Path(path), text, blob_sha="sha")


def _by_qname(pf: ParsedFile, qname: str):
    return next(s for s in pf.symbols if s.qualified_name == qname)


def _refs(pf: ParsedFile, kind: EdgeKind) -> list[ParsedReference]:
    return [r for r in pf.references if r.kind == kind]


def _targets(pf: ParsedFile, kind: EdgeKind) -> set[str]:
    return {r.target_text for r in _refs(pf, kind)}


# -- defines (parent_qualified_name) --------------------------------------


def test_top_level_symbol_parent_is_the_file() -> None:
    pf = _parse("def f():\n    pass\n")
    assert _by_qname(pf, "m.f").parent_qualified_name == "m"


def test_method_parent_is_its_class() -> None:
    pf = _parse("class C:\n    def m(self):\n        pass\n")
    assert _by_qname(pf, "m.C.m").parent_qualified_name == "m.C"


def test_nested_function_parent_is_the_enclosing_function() -> None:
    pf = _parse("def outer():\n    def inner():\n        pass\n")
    assert _by_qname(pf, "m.outer.inner").parent_qualified_name == "m.outer"


def test_class_level_variable_parent_is_its_class() -> None:
    pf = _parse("class C:\n    x: int\n")
    assert _by_qname(pf, "m.C.x").parent_qualified_name == "m.C"


def test_file_symbol_has_no_parent() -> None:
    pf = _parse("x = 1\n")
    assert pf.symbols[0].kind == SymbolKind.FILE
    assert pf.symbols[0].parent_qualified_name is None


# -- imports ----------------------------------------------------------------


def test_bare_import_target_is_the_dotted_path() -> None:
    pf = _parse("import os\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"os"}


def test_dotted_import_target_is_the_full_path() -> None:
    pf = _parse("import os.path\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"os.path"}


def test_aliased_import_target_ignores_the_alias() -> None:
    pf = _parse("import os.path as p\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"os.path"}


def test_comma_separated_import_yields_one_reference_per_module() -> None:
    pf = _parse("import a, b\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"a", "b"}


def test_from_import_target_combines_module_and_original_name() -> None:
    """Not just the module -- see the module-level comment above
    _import_from_targets: `pkg.mod.Foo` is exactly Foo's qualified name if
    it is a symbol, and exactly a submodule's qualified name if it is one,
    letting the resolver try a single exact-match lookup either way.
    """
    pf = _parse("from pkg.mod import Foo\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.mod.Foo"}


def test_from_import_alias_uses_original_name_not_the_alias() -> None:
    pf = _parse("from pkg.mod import Bar as B\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.mod.Bar"}


def test_from_import_multiple_names_yields_one_reference_each() -> None:
    pf = _parse("from pkg.mod import Foo, Bar as B\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.mod.Foo", "pkg.mod.Bar"}


def test_from_import_wildcard_falls_back_to_the_module() -> None:
    pf = _parse("from pkg.mod import *\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.mod"}


def test_relative_import_single_dot_resolves_against_containing_package() -> None:
    # pkg/mod.py's containing package is "pkg"; `from . import sibling`
    # names something at "pkg.sibling".
    pf = _parse("from . import sibling\n", path="pkg/mod.py")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.sibling"}


def test_relative_import_with_submodule_appends_it() -> None:
    pf = _parse("from .sub import helper\n", path="pkg/mod.py")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.sub.helper"}


def test_relative_import_from_init_py_resolves_against_its_own_package() -> None:
    # pkg/sub/__init__.py *is* the pkg.sub package -- "." inside it also
    # means pkg.sub, not pkg.sub's parent.
    pf = _parse("from . import x\n", path="pkg/sub/__init__.py")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.sub.x"}


def test_relative_import_double_dot_goes_up_a_level() -> None:
    pf = _parse("from .. import thing\n", path="pkg/sub/mod.py")
    assert _targets(pf, EdgeKind.IMPORTS) == {"pkg.thing"}


def test_relative_import_past_the_repo_root_yields_no_reference() -> None:
    # pkg/mod.py's package is just "pkg" (one level) -- going up two levels
    # points above anything this parser can know about.
    pf = _parse("from .. import thing\n", path="pkg/mod.py")
    assert _targets(pf, EdgeKind.IMPORTS) == set()


def test_import_inside_a_function_is_attributed_to_that_function() -> None:
    pf = _parse("def f():\n    import json\n")
    refs = _refs(pf, EdgeKind.IMPORTS)
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.f"


def test_import_inside_a_conditional_is_still_captured() -> None:
    """The messy/real-world case: a try/except import fallback. Both
    branches are unconditionally reachable as far as static extraction is
    concerned, so both are captured -- see the module docstring's note on
    this specific example.
    """
    pf = _parse("try:\n    import ujson as j\nexcept ImportError:\n    import json as j\n")
    assert _targets(pf, EdgeKind.IMPORTS) == {"ujson", "json"}


# -- inherits -----------------------------------------------------------


def test_single_base_class() -> None:
    pf = _parse("class Foo(Base):\n    pass\n")
    refs = _refs(pf, EdgeKind.INHERITS)
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.Foo"
    assert refs[0].target_text == "Base"


def test_dotted_base_class() -> None:
    pf = _parse("class Foo(pkg.Base):\n    pass\n")
    assert _targets(pf, EdgeKind.INHERITS) == {"pkg.Base"}


def test_multiple_base_classes() -> None:
    pf = _parse("class Foo(Base1, Base2):\n    pass\n")
    assert _targets(pf, EdgeKind.INHERITS) == {"Base1", "Base2"}


def test_metaclass_keyword_argument_is_not_a_base() -> None:
    pf = _parse("class Foo(Base, metaclass=Meta):\n    pass\n")
    assert _targets(pf, EdgeKind.INHERITS) == {"Base"}


def test_generic_subscript_base_unwraps_to_the_base_name() -> None:
    pf = _parse("class Foo(Generic[T]):\n    pass\n")
    assert _targets(pf, EdgeKind.INHERITS) == {"Generic"}


def test_class_with_no_parens_has_no_inherits_reference() -> None:
    pf = _parse("class Foo:\n    pass\n")
    assert _refs(pf, EdgeKind.INHERITS) == []


def test_class_with_empty_parens_has_no_inherits_reference() -> None:
    pf = _parse("class Foo():\n    pass\n")
    assert _refs(pf, EdgeKind.INHERITS) == []


# -- calls --------------------------------------------------------------


def test_bare_function_call() -> None:
    pf = _parse("def f():\n    g()\n")
    refs = _refs(pf, EdgeKind.CALLS)
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.f"
    assert refs[0].target_text == "g"


def test_attribute_call_records_the_full_dotted_text() -> None:
    pf = _parse("def f():\n    self.method()\n")
    assert _targets(pf, EdgeKind.CALLS) == {"self.method"}


def test_deep_attribute_call_records_full_text_even_though_unresolvable() -> None:
    """Recorded as raw data regardless -- resolving `obj.attr.call` needs
    type inference the heuristic tier deliberately does not attempt (that
    is what the `resolved`/SCIP tier is for). Extraction's job is only to
    record what was written; RM-021's resolver is what will fail to match
    this, not this parser.
    """
    pf = _parse("def f():\n    obj.attr.call()\n")
    assert _targets(pf, EdgeKind.CALLS) == {"obj.attr.call"}


def test_module_level_call_is_attributed_to_the_file() -> None:
    pf = _parse("x = make()\n")
    refs = _refs(pf, EdgeKind.CALLS)
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m"


def test_class_body_call_is_attributed_to_the_class_not_a_method() -> None:
    pf = _parse("class C:\n    x = compute()\n")
    refs = [r for r in _refs(pf, EdgeKind.CALLS) if r.target_text == "compute"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.C"


def test_call_inside_a_method_is_attributed_to_that_method_not_the_class() -> None:
    pf = _parse("class C:\n    def m(self):\n        helper()\n")
    refs = [r for r in _refs(pf, EdgeKind.CALLS) if r.target_text == "helper"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.C.m"


def test_call_nested_inside_if_is_still_attributed_to_the_enclosing_function() -> None:
    pf = _parse("def f():\n    if True:\n        g()\n")
    refs = [r for r in _refs(pf, EdgeKind.CALLS) if r.target_text == "g"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.f"


def test_call_nested_inside_for_and_try_is_still_found() -> None:
    pf = _parse(
        "def f():\n"
        "    for x in y:\n"
        "        try:\n"
        "            g(x)\n"
        "        except E:\n"
        "            h()\n"
    )
    assert _targets(pf, EdgeKind.CALLS) >= {"g", "h"}


def test_call_inside_nested_function_is_not_double_attributed_to_the_outer_one() -> None:
    pf = _parse("def outer():\n    def inner():\n        g()\n")
    refs = [r for r in _refs(pf, EdgeKind.CALLS) if r.target_text == "g"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.outer.inner"  # not m.outer


def test_recursive_call_is_captured() -> None:
    pf = _parse("def f():\n    f()\n")
    refs = [r for r in _refs(pf, EdgeKind.CALLS) if r.target_text == "f"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.f"


def test_nested_call_arguments_are_both_captured() -> None:
    pf = _parse("def f():\n    outer(inner())\n")
    assert _targets(pf, EdgeKind.CALLS) == {"outer", "inner"}


def test_call_with_non_callable_expression_target_is_skipped() -> None:
    # `factory()()` -- the outer call's callee is itself a `call` node, not
    # an identifier/attribute; only the inner g() is a recordable callee.
    pf = _parse("def f():\n    factory()()\n")
    assert _targets(pf, EdgeKind.CALLS) == {"factory"}


# -- references: decorators ----------------------------------------------


def test_bare_decorator_reference() -> None:
    pf = _parse("@deco\ndef f():\n    pass\n")
    refs = [r for r in _refs(pf, EdgeKind.REFERENCES) if r.target_text == "deco"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.f"


def test_dotted_decorator_reference() -> None:
    pf = _parse("@pkg.deco\ndef f():\n    pass\n")
    assert "pkg.deco" in _targets(pf, EdgeKind.REFERENCES)


def test_decorator_factory_call_references_the_factory_name() -> None:
    pf = _parse("@pkg.deco(arg)\ndef f():\n    pass\n")
    assert "pkg.deco" in _targets(pf, EdgeKind.REFERENCES)


def test_stacked_decorators_each_produce_a_reference() -> None:
    pf = _parse("@a\n@b\ndef f():\n    pass\n")
    assert {"a", "b"} <= _targets(pf, EdgeKind.REFERENCES)


def test_class_decorator_reference() -> None:
    pf = _parse("@register\nclass C:\n    pass\n")
    refs = [r for r in _refs(pf, EdgeKind.REFERENCES) if r.target_text == "register"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.C"


# -- references: type annotations ----------------------------------------


def test_parameter_annotation_reference() -> None:
    pf = _parse("def f(x: Config):\n    pass\n")
    refs = [r for r in _refs(pf, EdgeKind.REFERENCES) if r.target_text == "Config"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.f"


def test_parameter_annotation_with_default_value_reference() -> None:
    pf = _parse("def f(x: Config = None):\n    pass\n")
    assert "Config" in _targets(pf, EdgeKind.REFERENCES)


def test_return_type_annotation_reference() -> None:
    pf = _parse("def f() -> Widget:\n    pass\n")
    assert "Widget" in _targets(pf, EdgeKind.REFERENCES)


def test_dotted_annotation_reference() -> None:
    pf = _parse("def f(x: pkg.Config) -> pkg.Widget:\n    pass\n")
    assert {"pkg.Config", "pkg.Widget"} <= _targets(pf, EdgeKind.REFERENCES)


def test_variable_annotation_reference() -> None:
    pf = _parse("x: Config = load()\n")
    refs = [r for r in _refs(pf, EdgeKind.REFERENCES) if r.target_text == "Config"]
    assert len(refs) == 1
    assert refs[0].src_qualified_name == "m.x"  # the variable itself, not the module


def test_bare_annotation_without_value_still_yields_a_reference() -> None:
    pf = _parse("class C:\n    x: Config\n")
    assert "Config" in _targets(pf, EdgeKind.REFERENCES)


def test_builtin_type_annotation_is_still_recorded_as_a_reference() -> None:
    """Not filtered here -- `int` will simply fail to resolve to anything
    in RM-021's resolver, the same as any other unmatched reference. This
    parser's job is to record what was written, not to know what is
    "interesting" enough to keep.
    """
    pf = _parse("def f(x: int) -> bool:\n    pass\n")
    assert {"int", "bool"} <= _targets(pf, EdgeKind.REFERENCES)
