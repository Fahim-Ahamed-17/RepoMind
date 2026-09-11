"""Local, CPU-only :class:`~repomind.embed.base.Embedder` via ``fastembed``
and ``bge-small-en-v1.5`` (RM-031, F-5).

**Why fastembed, not sentence-transformers:** pyproject.toml originally
pinned ``sentence-transformers``, flagged with an open question to
evaluate ``fastembed`` first -- ``sentence-transformers`` pulls PyTorch,
roughly 2 GB, in real tension with the five-minute install target
(project-document.md section 15). Confirmed directly before switching:
``fastembed`` adds ~150 MB to a virtualenv (no PyTorch at all -- it runs
the model through ``onnxruntime`` instead), natively supports
``BAAI/bge-small-en-v1.5`` as one of its bundled models, and produces the
384-dim, L2-normalised ``float32`` vectors design.md section 8 specifies,
verified by actually loading the model and embedding real text, not
assumed from the library's description.

design.md's own model choice, unchanged by the library swap: "33M
params, CPU, ~15ms/chunk. Local on any machine" -- and, separately,
"English-biased and code-imperfect, but it runs anywhere, which is the
binding requirement... forbids solving quality problems here with API
embeddings, since that would upload the repo."
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import platformdirs

from repomind.errors import EmbeddingError

if TYPE_CHECKING:
    from collections.abc import Sequence

#: design.md section 8 / 11.4: the only model this module knows about.
MODEL_NAME = "BAAI/bge-small-en-v1.5"

#: Must match the `chunk_vec` table's own `FLOAT[384]` column (design.md
#: section 4.2) -- not derived from the model at runtime, so a mismatch
#: between this constant and what the model actually returns fails loudly
#: (see `embed`'s own length check) rather than silently corrupting the
#: vector table one row at a time.
DIMENSIONS = 384

#: design.md section 11.4: "Embedding batches at 32 chunks."
BATCH_SIZE = 32


def _default_cache_dir() -> str:
    """A proper OS cache location for a large, freely-re-downloadable
    artifact -- unlike ``workspace.workspace_root()``'s own deliberate,
    invariant-mandated ``~/.repomind`` (design.md AD-5's literal wording),
    a model file is exactly what ``platformdirs`` exists for. The first
    real use of that already-declared dependency in this codebase.
    """
    return platformdirs.user_cache_dir("repomind")


class LocalEmbedder:
    """Structurally satisfies :class:`~repomind.embed.base.Embedder`."""

    dimensions = DIMENSIONS

    def __init__(self, *, model_name: str = MODEL_NAME, cache_dir: str | None = None) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir or _default_cache_dir()
        # Any, not fastembed.TextEmbedding: that type's own .embed() return
        # annotation reaches into numpy's typing internals, whose installed
        # stub (numpy 2.5.3) uses syntax mypy's configured python_version
        # (3.11, matching requires-python) refuses to parse -- confirmed
        # directly, not assumed. This module only ever calls .embed() on
        # it and treats each result as anything with a .tolist(), which
        # needs no more precision than Any gives it (docs/conventions.md:
        # explicit Any is allowed with a comment explaining why).
        self._model: Any = None

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        model = self._ensure_loaded()
        try:
            vectors = list(model.embed(list(texts), batch_size=BATCH_SIZE))
        except Exception as exc:
            raise EmbeddingError(
                f"embedding {len(texts)} chunk(s) with {self._model_name!r} failed: {exc}"
            ) from exc

        result = [v.tolist() for v in vectors]
        for vec in result:
            if len(vec) != self.dimensions:
                raise EmbeddingError(
                    f"{self._model_name!r} returned a {len(vec)}-dim vector, "
                    f"expected {self.dimensions} -- DIMENSIONS is out of sync with the "
                    f"configured model, or the model changed underneath it"
                )
        return result

    def _ensure_loaded(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise EmbeddingError(
                "fastembed is not installed -- run `pip install -e '.[dev]'` "
                "(or `pip install fastembed`) to enable local embeddings"
            ) from exc
        try:
            # lazy_load=True: the ONNX session and model weights are not
            # actually touched until the first embed() call below, not
            # here -- constructing a LocalEmbedder should be cheap even if
            # a run ends up never calling embed() at all (e.g. zero
            # chunks discovered).
            self._model = TextEmbedding(
                model_name=self._model_name, cache_dir=self._cache_dir, lazy_load=True
            )
        except Exception as exc:
            raise EmbeddingError(
                f"could not load embedding model {self._model_name!r}: {exc}. "
                f"If this is a first-run download failure (no network, or a "
                f"firewalled machine), download the model manually into "
                f"{self._cache_dir} -- see fastembed's own docs for the expected "
                f"cache layout -- and retry."
            ) from exc
        return self._model
