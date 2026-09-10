"""Tests for RM-022's SCIP subprocess runner and protobuf ingest.

``scip-python`` itself is never invoked here -- the real binary cannot be
exercised end-to-end on this project's own dev machine (see scip.py's
module docstring for the confirmed upstream Windows bug), and even where
it can run, a unit suite should not depend on a third-party binary's
presence or exact behaviour. Instead:

  * ``parse_scip_index`` is tested against real, in-memory ``scip_pb2``
    messages this file builds and serialises itself -- genuine protobuf
    bytes, just not produced by the real tool.
  * ``run_scip_python`` is tested by monkeypatching ``shutil.which`` and
    ``subprocess.run`` at the boundary, the same way
    ``tests/integration/test_pipeline.py`` monkeypatches ``_index_one_file``
    to simulate a failure without needing the real failure condition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from repomind.errors import ScipUnavailableError
from repomind.languages.python import scip_pb2
from repomind.languages.python.scip import (
    ScipIndex,
    is_available,
    parse_scip_index,
    run_scip_python,
)


def _write_index(path: Path, doc_builders: list) -> None:
    index = scip_pb2.Index()
    for build in doc_builders:
        build(index.documents.add())
    path.write_bytes(index.SerializeToString())


def _occ(doc, symbol: str, *, definition: bool = False, line: int | None = None) -> None:
    occ = doc.occurrences.add()
    occ.symbol = symbol
    occ.symbol_roles = 1 if definition else 0
    if line is not None:
        occ.range.extend([line, 0, 5])


# -- parse_scip_index -------------------------------------------------------


def test_parses_a_definition_and_a_reference(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        _occ(d, "sym-f", definition=True, line=0)
        _occ(d, "sym-g", definition=False, line=2)

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.definitions == {("a.py", 0): "sym-f"}
    assert result.references == {("a.py", 2): "sym-g"}


def test_ambiguous_same_line_references_are_dropped_not_guessed_at(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        _occ(d, "sym-typea", definition=False, line=3)
        _occ(d, "sym-typeb", definition=False, line=3)

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {}


def test_ambiguous_same_line_definitions_are_dropped_not_guessed_at(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        _occ(d, "sym-x", definition=True, line=1)
        _occ(d, "sym-y", definition=True, line=1)

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.definitions == {}


def test_repeated_same_symbol_on_the_same_line_is_not_ambiguous(tmp_path: Path) -> None:
    # Two occurrences of the *same* symbol on one line (e.g. it is written
    # twice) is not the same as two different symbols -- only one distinct
    # symbol is present, so this must still resolve.
    def doc(d):
        d.relative_path = "a.py"
        _occ(d, "sym-f", definition=False, line=3)
        _occ(d, "sym-f", definition=False, line=3)

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {("a.py", 3): "sym-f"}


def test_occurrence_with_empty_symbol_is_ignored(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        occ = d.occurrences.add()
        occ.symbol = ""
        occ.range.extend([0, 0, 5])

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.definitions == {}
    assert result.references == {}


def test_occurrence_with_no_range_information_is_ignored(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        occ = d.occurrences.add()
        occ.symbol = "sym-f"
        # No range, no single_line_range, no multi_line_range set at all.

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {}


def test_single_line_range_is_used_for_the_line(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        occ = d.occurrences.add()
        occ.symbol = "sym-f"
        occ.single_line_range.line = 7
        occ.single_line_range.start_character = 0
        occ.single_line_range.end_character = 5

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {("a.py", 7): "sym-f"}


def test_multi_line_range_uses_the_start_line(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        occ = d.occurrences.add()
        occ.symbol = "sym-f"
        occ.multi_line_range.start_line = 4
        occ.multi_line_range.end_line = 6

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {("a.py", 4): "sym-f"}


def test_typed_range_takes_precedence_over_the_deprecated_range_field(tmp_path: Path) -> None:
    # scip.proto: "When both typed_range and the deprecated range field
    # are set, typed_range takes precedence." A producer should not do
    # this in practice, but a consumer must still follow the spec.
    def doc(d):
        d.relative_path = "a.py"
        occ = d.occurrences.add()
        occ.symbol = "sym-f"
        occ.range.extend([99, 0, 5])  # deliberately wrong, must be ignored
        occ.single_line_range.line = 2
        occ.single_line_range.start_character = 0
        occ.single_line_range.end_character = 5

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {("a.py", 2): "sym-f"}


def test_four_element_deprecated_range_is_read_the_same_as_three(tmp_path: Path) -> None:
    def doc(d):
        d.relative_path = "a.py"
        occ = d.occurrences.add()
        occ.symbol = "sym-f"
        occ.range.extend([5, 0, 6, 3])  # [startLine, startChar, endLine, endChar]

    path = tmp_path / "index.scip"
    _write_index(path, [doc])

    result = parse_scip_index(path)
    assert result.references == {("a.py", 5): "sym-f"}


def test_multiple_documents_are_all_read(tmp_path: Path) -> None:
    def doc_a(d):
        d.relative_path = "a.py"
        _occ(d, "sym-a", definition=True, line=0)

    def doc_b(d):
        d.relative_path = "b.py"
        _occ(d, "sym-b", definition=True, line=0)

    path = tmp_path / "index.scip"
    _write_index(path, [doc_a, doc_b])

    result = parse_scip_index(path)
    assert result.definitions == {("a.py", 0): "sym-a", ("b.py", 0): "sym-b"}


def test_a_file_that_is_not_a_valid_scip_index_raises_scip_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "index.scip"
    path.write_bytes(b"not a protobuf message at all, just garbage bytes \xff\xfe")

    with pytest.raises(ScipUnavailableError):
        parse_scip_index(path)


# -- is_available -----------------------------------------------------------


def test_is_available_true_when_shutil_which_finds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: r"C:\fake\scip-python.cmd")
    assert is_available() is True


def test_is_available_false_when_shutil_which_finds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert is_available() is False


# -- run_scip_python ----------------------------------------------------------


def test_raises_when_binary_is_not_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(ScipUnavailableError, match="not found on PATH"):
        run_scip_python(tmp_path, tmp_path / "out" / "index.scip")


def test_raises_on_non_zero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/scip-python")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=b"boom"
        ),
    )

    with pytest.raises(ScipUnavailableError, match="exited 1"):
        run_scip_python(tmp_path, tmp_path / "index.scip")


def test_raises_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/scip-python")

    def _raise_timeout(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="scip-python", timeout=1.0)

    monkeypatch.setattr("subprocess.run", _raise_timeout)

    with pytest.raises(ScipUnavailableError, match="did not finish"):
        run_scip_python(tmp_path, tmp_path / "index.scip", timeout=1.0)


def test_raises_when_binary_disappears_between_which_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/scip-python")

    def _raise_not_found(*a: object, **k: object) -> None:
        raise FileNotFoundError("no such file")

    monkeypatch.setattr("subprocess.run", _raise_not_found)

    with pytest.raises(ScipUnavailableError, match="could not be executed"):
        run_scip_python(tmp_path, tmp_path / "index.scip")


def test_raises_when_exit_zero_but_no_output_file_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/scip-python")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""),
    )

    with pytest.raises(ScipUnavailableError, match="did not produce"):
        run_scip_python(tmp_path, tmp_path / "index.scip")


def test_success_parses_the_produced_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "workspace" / "index.scip"
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/scip-python")

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # A real scip-python invocation writes --output itself; the fake
        # here does the same so run_scip_python's own parse step is
        # exercised against real bytes, not a mock return value.
        _write_index(
            output_path,
            [lambda d: (setattr(d, "relative_path", "a.py"), _occ(d, "sym-f", line=0))],
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("subprocess.run", _fake_run)

    result = run_scip_python(tmp_path, output_path)
    assert isinstance(result, ScipIndex)
    assert result.references == {("a.py", 0): "sym-f"}
