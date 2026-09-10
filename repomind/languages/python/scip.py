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
design.md AD-9.

**Why this pins to scip-python 0.3.0, not "latest":** every published
version from 0.3.1 through the newest, 0.6.6, hard-crashes on *any*
invocation on Windows, confirmed empirically by running each one --
``new RegExp(path.sep, 'g')`` somewhere in ``PythonEnvironment.ts`` is an
invalid regular expression when ``path.sep`` is a bare backslash, which it
always is on Windows, in every Node.js version, unconditionally (verified
directly: ``new RegExp(require('path').sep, 'g')`` throws in plain
``node -e`` with no scip-python involved at all). 0.3.0 is the newest
version that predates whatever change introduced this, and was confirmed
by actually running it end-to-end on Windows (no WSL, no container) --
real indexing of real fixtures, producing a real ``index.scip`` this
module's own parser reads correctly. Two more Windows-specific quirks in
0.3.0 itself, also found empirically rather than assumed, are worked
around below rather than documented as known failures:

  * Passing ``--cwd`` explicitly (rather than relying on the subprocess's
    own working directory) makes file discovery silently find nothing --
    zero documents, no error. So this module never passes ``--cwd``; it
    sets the *subprocess's* cwd instead (``subprocess.run(cwd=root)``),
    which scip-python's own ``process.cwd()`` default picks up correctly.
  * ``--output`` with an absolute path is silently mishandled on Windows
    -- the tool joins it onto its cwd as if it were still relative,
    producing a nonsense path (the cwd, followed by the whole absolute
    path glued on as if it were relative) and failing with ENOENT. A
    relative path with leading ``..`` segments resolves correctly and
    lands outside ``root`` -- which is required anyway, since AGENTS.md
    invariant 6 forbids writing into the indexed repository even
    transiently. This module computes that relative path itself
    (``os.path.relpath``) rather than accept an absolute one from the
    caller and have it silently go to the wrong place.

A third, much larger problem, also found only by running this against a
real repository rather than small hand-built fixtures: with no
``--exclude``, scip-python walks *everything* under the indexed
directory, including ``.venv``/``site-packages`` -- meaning it does full
type-checking on every installed dependency's own source (pytest,
typing_extensions, every transitive package) as if it were project code.
Against this project's own ~58-file source tree, that meant 1500+ extra
"files" from site-packages alone, multi-minute runs, and -- combined with
an empty ``--environment`` (below) giving it no record that those
packages are already resolved, installed dependencies -- an actual
``JavaScript heap out of memory`` crash. Excluding dependency directories
(the same names ``ingest/discover.py``'s ``DEFAULT_SKIP_DIR_NAMES``
already excludes from repomind's own file discovery -- imported from
there rather than duplicated, so the two policies cannot drift apart) is
what actually fixes this: the identical repository indexed in under 5
seconds once site-packages was out of scope, no heap increase needed.

Separately, and still worth keeping regardless: scip-python's own
package-environment detection (``pip list`` + ``pip show -f <every
installed package>``, used to attach precise version metadata to
third-party symbols) can overflow Node's default subprocess output buffer
(``ENOBUFS``) on a machine with a large Python environment on PATH --
reproduced directly against this machine's own base Python install. This
module always passes ``--environment`` pointing at an empty package list,
unconditionally, which skips that step entirely. The cost is real but
narrow: symbols from third-party packages resolve with less precise
version metadata. Nothing this module actually uses is affected -- stdlib
resolution comes from pyright's bundled typeshed regardless, and
intra-repo resolution (what RM-022 exists for) is unaffected, confirmed
against the real, non-trivial cross-file references in
tests/fixtures/simple and in this project's own source.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from repomind.errors import ScipUnavailableError
from repomind.ingest.discover import DEFAULT_SKIP_DIR_NAMES
from repomind.languages.python import scip_pb2

#: The only binary this module ever invokes. Resolved via PATH at call
#: time (never a hardcoded absolute path) so a user's own npm global-bin
#: directory, however it is configured, is respected -- same expectation
#: as any other PATH-resolved dev tool this project shells out to.
SCIP_PYTHON_BINARY = "scip-python"

#: The only version this module is confirmed to work with -- see this
#: module's own docstring for why "latest" is actively broken on Windows.
#: Referenced in every install instruction this module prints; not
#: enforced at runtime (checking an installed binary's own version would
#: mean invoking it, defeating the point), so an installed 0.3.0 is a
#: recommendation the user must actually follow, not something this
#: module can verify from here.
SCIP_PYTHON_VERSION = "0.3.0"

#: design.md section 11.1's own v1 target repo size (~5k files) indexes in
#: well under this on a typical laptop; a hang past 2 minutes is already an
#: anomaly worth degrading for rather than blocking `repomind index` on.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: SymbolRole.Definition from scip.proto -- a bitset, not an enum value;
#: "is the Definition bit set" is `role & _DEFINITION_ROLE`, never `==`.
_DEFINITION_ROLE = 0x1

#: Comma-separated glob patterns for scip-python's own ``--exclude``,
#: built from the exact directory names ``ingest/discover.py`` already
#: excludes from repomind's own file discovery -- see this module's
#: docstring for why leaving this unset is not merely slower but can
#: crash the subprocess outright (it walks and type-checks every
#: installed dependency under e.g. ``.venv`` as if it were project code).
_EXCLUDE_PATTERN = ",".join(f"**/{name}/**" for name in sorted(DEFAULT_SKIP_DIR_NAMES))


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


def _install_hint() -> str:
    return (
        f"install it (`npm install -g @sourcegraph/scip-python@{SCIP_PYTHON_VERSION}` -- "
        f"not `latest`, which crashes on Windows) or pass --no-scip"
    )


def run_scip_python(
    root: Path,
    output_path: Path,
    *,
    project_name: str = "repomind",
    project_version: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ScipIndex:
    """Run ``scip-python`` against ``root`` and parse its output.

    ``output_path`` should live in this repo's own workspace directory
    (``workspace.repo_workspace_dir``), never inside ``root`` itself --
    AGENTS.md invariant 6 forbids writing into the indexed repository, and
    the primary user is routinely exploring code they do not own. This
    function honours that even though the underlying tool's own
    ``--output`` handling would happily write there by accident (see this
    module's docstring) -- it computes a relative path from ``root`` to
    ``output_path`` and passes that, never an absolute path.

    ``project_version`` should be the commit the caller is indexing at
    (``repomind.ingest.git.current_sha``), passed through explicitly
    rather than left to scip-python's own git detection: that detection
    fails outright in a non-git directory ("Must either pass
    --project-version or run from within a git repository"), and this
    module has no reason to depend on scip-python's own idea of "which
    commit" matching this project's when both are trivially available to
    the caller already. ``None`` becomes a fixed placeholder, not an
    omitted flag.

    Raises :class:`ScipUnavailableError` -- naming what failed and what the
    caller can do about it (docs/conventions.md's own error-message rule)
    -- for every failure mode: the binary is not on PATH, ``output_path``
    is not reachable from ``root`` via a relative path (different drives,
    on Windows), the subprocess exits non-zero, it runs past ``timeout``,
    or the output file it was supposed to produce is missing or is not a
    valid SCIP index. Never raises anything else; a caller only ever needs
    to catch this one type.
    """
    if not is_available():
        raise ScipUnavailableError(f"{SCIP_PYTHON_BINARY!r} not found on PATH; {_install_hint()}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        relative_output = os.path.relpath(output_path, root)
    except ValueError as exc:
        # Windows-only in practice: os.path.relpath refuses to cross drive
        # letters (C: vs D:). No relative path can express that, and an
        # absolute one is silently mishandled by this tool (see module
        # docstring) -- there is no correct way to invoke it in that case.
        raise ScipUnavailableError(
            f"cannot express {output_path} relative to {root} (different drives?): {exc}"
        ) from exc

    # Skips scip-python's own pip-based environment detection entirely --
    # see this module's docstring for the real ENOBUFS failure this avoids
    # unconditionally, on every machine, not just ones known to hit it.
    environment_path = output_path.parent / "scip_environment.json"
    environment_path.write_text("[]", encoding="utf-8")

    args = [
        SCIP_PYTHON_BINARY,
        "index",
        "--project-name",
        project_name,
        "--project-version",
        project_version or "unversioned",
        "--output",
        relative_output,
        "--environment",
        str(environment_path),
        "--exclude",
        _EXCLUDE_PATTERN,
        "--no-progress-bar",
    ]
    try:
        # shell=True on Windows: an npm global install puts a `.cmd` shim
        # on PATH, not a directly-executable binary, and CreateProcess
        # cannot launch a `.cmd` without going through a shell. Passing
        # `args` as a list (not a pre-joined string) keeps subprocess's own
        # quoting (`list2cmdline`) in charge of escaping even with
        # shell=True, rather than hand-building a command string ourselves.
        # cwd=root, not --cwd: see module docstring -- passing --cwd
        # explicitly makes this version silently discover zero files.
        result = subprocess.run(  # noqa: S603 -- args is a fixed list this
            # module builds itself (never a shell string), and every
            # variable part (project_name, project_version, output,
            # environment) is passed as its own list element, never
            # concatenated into one -- exactly what S603 wants verified by
            # hand when it cannot infer it.
            args,
            cwd=root,
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
        # scip.proto requires '/' "including on Windows" -- scip-python
        # 0.3.0 does not honour that on Windows, confirmed empirically
        # (emits "pkg\\module_a.py"). Normalising here, once, keeps every
        # consumer of ScipIndex (index/resolve.py, index/pipeline.py) free
        # to assume the same forward-slash convention repomind.model.File
        # already documents for its own `path` field, regardless of which
        # platform produced this particular index.scip.
        relative_path = doc.relative_path.replace("\\", "/")
        for occ in doc.occurrences:
            if not occ.symbol:
                continue
            line = _occurrence_line(occ)
            if line is None:
                continue
            key = (relative_path, line)
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
