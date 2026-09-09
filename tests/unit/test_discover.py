from __future__ import annotations

from pathlib import Path

from repomind.ingest.discover import DEFAULT_SIZE_CAP_BYTES, discover_files


def test_walk_fallback_finds_all_files_in_a_non_git_directory(
    simple_fixture_repo: Path,
) -> None:
    files = list(discover_files(simple_fixture_repo))
    rel_paths = sorted(f.rel_path for f in files)
    assert rel_paths == [
        "pkg/__init__.py",
        "pkg/module_a.py",
        "pkg/module_b.py",
        "script.py",
    ]


def test_walk_fallback_skips_vendor_and_dot_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "leaked.py").write_text("y = 1", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "leaked2.py").write_text("z = 1", encoding="utf-8")
    (tmp_path / ".idea").mkdir()
    (tmp_path / ".idea" / "leaked3.py").write_text("w = 1", encoding="utf-8")

    files = {f.rel_path for f in discover_files(tmp_path)}
    assert files == {"src/real.py"}


def test_size_cap_excludes_large_files(tmp_path: Path) -> None:
    small = tmp_path / "small.py"
    small.write_text("x = 1", encoding="utf-8")
    big = tmp_path / "big.py"
    big.write_bytes(b"x" * (DEFAULT_SIZE_CAP_BYTES + 1))

    files = {f.rel_path for f in discover_files(tmp_path)}
    assert files == {"small.py"}


def test_size_cap_is_configurable(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x" * 100)
    files = list(discover_files(tmp_path, size_cap_bytes=50))
    assert files == []


def test_git_mode_respects_gitignore_and_includes_untracked(tmp_path: Path) -> None:
    from git import Repo as GitRepo

    repo = GitRepo.init(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("a = 1", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("b = 1", encoding="utf-8")
    (tmp_path / "untracked_but_not_ignored.py").write_text("c = 1", encoding="utf-8")
    repo.index.add(["tracked.py", ".gitignore"])
    repo.index.commit("initial")

    files = {f.rel_path for f in discover_files(tmp_path)}
    assert files == {".gitignore", "tracked.py", "untracked_but_not_ignored.py"}
    assert "ignored.py" not in files


def test_never_yields_files_under_the_workspace_root(
    isolated_workspace: Path, tmp_path: Path
) -> None:
    # isolated_workspace lives under tmp_path via the fixture; simulate it
    # already containing something and confirm discovery of tmp_path itself
    # never descends into it.
    isolated_workspace.mkdir(parents=True, exist_ok=True)
    (isolated_workspace / "leaked_index.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "real.py").write_text("y = 1", encoding="utf-8")

    files = {f.rel_path for f in discover_files(tmp_path)}
    assert "real.py" in files
    assert not any("leaked_index" in f for f in files)
