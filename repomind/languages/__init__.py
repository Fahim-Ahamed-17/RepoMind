"""Language pack registry.

A plain dict, not a plugin-discovery mechanism: with exactly one language
implemented, building discoverable registration machinery now would be
premature. RM-070 (TypeScript, M7) adds a second entry here; if that
requires touching anything beyond this file and ``languages/typescript/``,
the LanguagePack protocol (``base.py``) was designed wrong -- see F-4's own
dependency note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from repomind.languages.python import PYTHON_LANGUAGE_PACK

if TYPE_CHECKING:
    from repomind.languages.base import LanguagePack

_PACKS_BY_EXTENSION: dict[str, LanguagePack] = dict.fromkeys(
    PYTHON_LANGUAGE_PACK.file_extensions, PYTHON_LANGUAGE_PACK
)


def get_language_pack_for_extension(extension: str) -> LanguagePack | None:
    """``extension`` includes the leading dot, e.g. ``".py"``. Returns
    ``None`` for anything not indexable as source -- callers use that to
    skip the file, not to raise.
    """
    return _PACKS_BY_EXTENSION.get(extension.lower())


__all__ = ["get_language_pack_for_extension"]
