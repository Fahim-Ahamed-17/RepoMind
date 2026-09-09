"""``LanguagePack`` -- the single extension point for adding a language.

RM-016. F-4's dependency note makes this protocol's job explicit: "Adding a
language must require touching only ``languages/typescript/`` -- if it
doesn't, the LanguagePack protocol is wrong, and that is the finding."

Deliberately minimal for M1: parsing a file's text into symbols is all this
ticket needs. Edge extraction (heuristic name/scope matching, and the SCIP
subprocess integration) is RM-020 through RM-022 in M2, and will extend this
protocol once those tickets make its actual required shape concrete --
guessing those method signatures now, before anything calls them, is
exactly the kind of premature interface design docs/conventions.md and
AGENTS.md both warn against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from repomind.model import ParsedFile


class LanguagePack(Protocol):
    """Everything the index pipeline needs from one language."""

    lang_id: str
    """Matches :attr:`repomind.model.File.lang`, e.g. ``"python"``."""

    file_extensions: frozenset[str]
    """Lower-case, with the leading dot, e.g. ``{".py"}``."""

    def parse_file(self, path: Path, text: str, blob_sha: str) -> ParsedFile:
        """Parse one file's source into its symbols.

        ``path`` is the repo-relative path (forward slashes) -- used to
        derive qualified names, not to re-read the file; ``text`` is the
        already-read source. Must not raise on malformed/unparseable source:
        tree-sitter itself is error-tolerant, and a language pack should
        return whatever symbols it could still recover (or none) rather
        than fail the whole index run over one bad file.
        """
        ...
