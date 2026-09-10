"""``Embedder`` -- the single extension point for turning chunk text into
vectors (F-5, RM-031).

Deliberately minimal, matching ``languages/base.py``'s own stated
philosophy: one method, because embedding text into a fixed-length vector
is genuinely all ``index/pipeline.py`` needs from this seam once RM-032
makes persistence concrete. Structural typing (``Protocol``, not ABC) per
docs/conventions.md, same as :class:`~repomind.store.base.GraphStore` and
:class:`~repomind.languages.base.LanguagePack`.

"Batched" (RM-031's own title) is a property of the *call shape*, not
separate machinery bolted on afterwards: ``embed`` always takes a
sequence and returns one vector per input, in the same order, so a
caller decides its own batch size (design.md section 11.4: "Embedding
batches at 32 chunks") by how many texts it passes at once.

"Resumable" is a property of what this protocol deliberately does *not*
do: an :class:`Embedder` keeps no state across calls tying one call's
inputs to another's. A caller that embeds chunks 1-32, is interrupted,
and later re-embeds chunks 33-64 gets exactly the results it would have
gotten embedding all 64 at once -- nothing here to invalidate or replay.
The actual checkpointing (design.md: "Both [parsing and embedding]
checkpoint into ``index_run``") is ``index/pipeline.py``'s job once
RM-032/033 give it something to persist embeddings into; this protocol
only has to not get in the way of that, which statelessness guarantees
for free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class Embedder(Protocol):
    """Turns text into fixed-length vectors, entirely locally (AGENTS.md
    invariant 1 -- no egress at index time, embedding included).
    """

    dimensions: int
    """The length of every vector this embedder returns. Must match
    whichever model is actually configured -- the `chunk_vec` table's own
    ``FLOAT[384]`` column (design.md section 4.2) is sized for
    ``bge-small-en-v1.5`` specifically, not this attribute in the
    abstract; a future embedder with a different model is a schema change,
    not just a new class satisfying this protocol.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed ``texts``, returning one vector per text, in the same
        order. ``texts`` may be empty, returning ``[]`` -- never an error
        for a caller between symbols with nothing left to embed this
        round.

        Raises :class:`repomind.errors.EmbeddingError` if the underlying
        model cannot be loaded or run. Never silently returns zero
        vectors or a wrong-length result -- design.md's own failure table
        treats "indexing without embeddings" as not a useful degraded
        state, unlike SCIP's ``resolved`` tier.
        """
        ...
