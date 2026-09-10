"""SCIP subprocess runner + protobuf ingest for Python (design.md AD-9, F-2).

RM-022, ``[TB]`` 2.0 days max. ``scip-python`` (npm, Sourcegraph's fork of
Pyright: github.com/sourcegraph/scip-python) is the only producer this
module knows about; TypeScript's own ``scip-typescript`` runner is RM-071,
a separate module, later.

This module never touches the store or the resolver -- mirrors
``parser.py``'s own boundary (``languages/`` produces plain data, ``index/``
turns it into persisted edges). A successful run produces a
:class:`ScipIndex` carrying exactly two lookups, one per SCIP occurrence
role (``SymbolRole.Definition``, ``scip.proto``):

  * ``definitions``: ``(relative_path, 0-indexed line) -> scip symbol
    string``, for occurrences with the Definition role set. This is what
    lets ``index/resolve.py`` map a scip symbol string back to one of our
    own persisted :class:`repomind.model.Symbol` rows, via
    ``GraphStore.find_symbol_at_location``.
  * ``references``: ``(relative_path, 0-indexed line) -> scip symbol
    string``, for occurrences *without* the Definition role -- i.e. a use,
    not a definition.

Both dicts drop a ``(path, line)`` key entirely rather than guess when more
than one distinct symbol occurs on the same line -- consistent with
``index/resolve.py``'s own heuristic-tier philosophy ("a wrong edge is
worse than a missing one"). This matters because
:class:`repomind.model.ParsedReference` (``parser.py``, RM-020) only ever
records a *line*, not a character range, so a signature like
``def f(a: TypeA, b: TypeB) -> TypeC:`` written on one line is genuinely
ambiguous at this join's granularity: which of three same-line reference
occurrences is which ``ParsedReference``? Refusing to pick is the same
policy call ``index/resolve.py`` already makes for its own bare-name
fallback, applied here for the same reason.

Any failure -- binary missing, non-zero exit, timeout, or unparseable
output -- raises :class:`repomind.errors.ScipUnavailableError`. The caller
(``index/pipeline.py``) is expected to catch it and fall back to
heuristic-only resolution with ``scip_status = ScipStatus.DEGRADED``, per
design.md AD-9. This is not a hidden corner case: confirmed empirically
during RM-022 that the latest published ``scip-python`` (0.6.6, no newer
version exists) hard-crashes on *any* invocation on Windows -- a genuine
upstream bug (``new RegExp(path.sep, 'g')`` in ``PythonEnvironment.ts`` is
an invalid regular expression when ``path.sep`` is a bare backslash, which
it is on Windows). So on that platform this fallback path is not a
theoretical edge case, it is the only path that ever runs. The CLI
invocation shape below (``scip-python index --cwd ... --output ...
--quiet``) was itself confirmed by reading the installed package's own
bundled ``commander`` option definitions statically (``dist/scip-python.js``),
since the tool cannot be exercised end-to-end on this development machine
to observe it directly -- notably, ``index`` takes no positional path
argument at all (the project root comes from ``--cwd``), which the
package's own README example (``scip-python index . --project-name=...``)
does not make obvious.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from repomind.errors import ScipUnavailableError
from repomind.languages.python import scip_pb2

#: The only binary this module ever invokes. Resolved via PATH at call
#: time (never a hardcoded absolute path) so a user's own npm global-bin
#: directory, however it is configured, is respected -- same expectation
#: as any other PATH-resolved dev tool this project shells out to.
SCIP_PYTHON_BINARY = "scip-python"

#: design.md section 11.1's own v1 target repo size (~5k files) indexes in
#: well under this on a typical laptop; a hang past 2 minutes is already an
#: anomaly worth degrading for rather than blocking `repomind index` on.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: SymbolRole.Definition from scip.proto -- a bitset, not an enum value;
#: "is the Definition bit set" is `role & _DEFINITION_ROLE`, never `==`.
_DEFINITION_ROLE = 0x1


@dataclass(frozen=True, slots=True)
class ScipIndex:
    """The two per-line lookups ``index/resolve.py`` needs. See this
    module's docstring for why both are pre-filtered to drop ambiguous
    same-line entries rather than pick one arbitrarily.
    """

    definitions: dict[tuple[str, int], str] = field(default_factory=dict)
    references: dict[tuple[str, int], str] = field(default_factory=dict)


def is_available() -> bool:
    """Fast, side-effect-free check for whether ``scip-python`` is even
    worth attempting -- lets a caller (or a future ``repomind status``)
    report "SCIP not installed" without spawning a process.
    """
    return shutil.which(SCIP_PYTHON_BINARY) is not None


def run_scip_python(
    root: Path,
    output_path: Path,
    *,
    project_name: str = "repomind",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ScipIndex:
    """Run ``scip-python`` against ``root`` and parse its output.

    ``output_path`` should live in this repo's own workspace directory
    (``workspace.repo_workspace_dir``), never inside ``root`` itself --
    AGENTS.md invariant 6 forbids writing into the indexed repository, and
    the primary user is routinely exploring code they do not own.

    Raises :class:`ScipUnavailableError` -- naming what failed and what the
    caller can do about it (docs/conventions.md's own error-message rule)
    -- for every failure mode: the binary is not on PATH, the subprocess
    exits non-zero, it runs past ``timeout``, or the output file it was
    supposed to produce is missing or is not a valid SCIP index. Never
    raises anything else; a caller only ever needs to catch this one type.
    """
    if not is_available():
        raise ScipUnavailableError(
            f"{SCIP_PYTHON_BINARY!r} not found on PATH; install it "
            f"(`npm install -g @sourcegraph/scip-python`) or pass --no-scip"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        SCIP_PYTHON_BINARY,
        "index",
        "--cwd",
        str(root),
        "--project-name",
        project_name,
        "--output",
        str(output_path),
        "--quiet",
    ]
    try:
        # shell=True on Windows: an npm global install puts a `.cmd` shim
        # on PATH, not a directly-executable binary, and CreateProcess
        # cannot launch a `.cmd` without going through a shell. Passing
        # `args` as a list (not a pre-joined string) keeps subprocess's own
        # quoting (`list2cmdline`) in charge of escaping even with
        # shell=True, rather than hand-building a command string ourselves.
        result = subprocess.run(  # noqa: S603 -- args is a fixed list this
            # module builds itself (never a shell string), and every
            # variable part (root, project_name, output_path) is passed as
            # its own list element, never concatenated into one -- exactly
            # what S603 wants verified by hand when it cannot infer it.
            args,
            timeout=timeout,
            capture_output=True,
            shell=(os.name == "nt"),
        )
    except FileNotFoundError as exc:
        raise ScipUnavailableError(f"{SCIP_PYTHON_BINARY!r} could not be executed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScipUnavailableError(
            f"{SCIP_PYTHON_BINARY!r} did not finish within {timeout:.0f}s"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ScipUnavailableError(
            f"{SCIP_PYTHON_BINARY!r} exited {result.returncode}: {stderr[:500] or '(no stderr)'}"
        )

    if not output_path.exists():
        raise ScipUnavailableError(
            f"{SCIP_PYTHON_BINARY!r} exited 0 but did not produce {output_path}"
        )

    return parse_scip_index(output_path)


def parse_scip_index(path: Path) -> ScipIndex:
    """Deserialise one ``index.scip`` file -- a single serialised
    ``scip.Index`` protobuf message, the on-disk convention every SCIP CLI
    tool (``scip print``, ``scip snapshot``, ...) reads and writes, not the
    length-delimited streaming form ``scip.proto`` separately mentions as
    an option for producers with a very large payload.
    """
    try:
        data = path.read_bytes()
        index = scip_pb2.Index()
        index.ParseFromString(data)
    except Exception as exc:
        # Deliberately broad: a malformed/truncated/non-SCIP file can fail
        # in several different ways inside protobuf's own C++-backed
        # parser (DecodeError, plain ValueError for some inputs, ...) and
        # every one of them means exactly the same thing to this module's
        # caller -- degrade, do not distinguish.
        raise ScipUnavailableError(f"{path} is not a valid SCIP index: {exc}") from exc

    definitions: dict[tuple[str, int], str] = {}
    references: dict[tuple[str, int], str] = {}
    # Per-line multiplicity, tracked only long enough to decide ambiguity;
    # the *_ambiguous sets are what actually gets consulted before a key is
    # allowed into the dicts above.
    definition_symbols_at: dict[tuple[str, int], set[str]] = {}
    reference_symbols_at: dict[tuple[str, int], set[str]] = {}

    for doc in index.documents:
        for occ in doc.occurrences:
            if not occ.symbol:
                continue
            line = _occurrence_line(occ)
            if line is None:
                continue
            key = (doc.relative_path, line)
            if occ.symbol_roles & _DEFINITION_ROLE:
                definition_symbols_at.setdefault(key, set()).add(occ.symbol)
            else:
                reference_symbols_at.setdefault(key, set()).add(occ.symbol)

    for key, symbols in definition_symbols_at.items():
        if len(symbols) == 1:
            definitions[key] = next(iter(symbols))
    for key, symbols in reference_symbols_at.items():
        if len(symbols) == 1:
            references[key] = next(iter(symbols))

    return ScipIndex(definitions=definitions, references=references)


def _occurrence_line(occ: scip_pb2.Occurrence) -> int | None:
    """The 0-indexed start line of ``occ``, from whichever of the two range
    encodings ``scip.proto`` says is actually populated (``typed_range``
    takes precedence over the deprecated ``range`` field when both are
    set; see ``Occurrence``'s own docstring in scip.proto).
    """
    which = occ.WhichOneof("typed_range")
    if which == "single_line_range":
        return occ.single_line_range.line
    if which == "multi_line_range":
        return occ.multi_line_range.start_line
    if len(occ.range) in (3, 4):
        return occ.range[0]
    return None
