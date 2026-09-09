"""The Python language pack: tree-sitter parsing (RM-017) plus, from M2
onward, heuristic and SCIP-backed edge resolution (RM-020 through RM-022).
"""

from __future__ import annotations

from pathlib import Path

from repomind.languages.python.parser import parse_python_file
from repomind.model import ParsedFile


class PythonLanguagePack:
    """Structurally satisfies :class:`repomind.languages.base.LanguagePack`."""

    lang_id = "python"
    file_extensions = frozenset({".py"})

    def parse_file(self, path: Path, text: str, blob_sha: str) -> ParsedFile:
        return parse_python_file(path, text, blob_sha)


PYTHON_LANGUAGE_PACK = PythonLanguagePack()

__all__ = ["PYTHON_LANGUAGE_PACK", "PythonLanguagePack"]
