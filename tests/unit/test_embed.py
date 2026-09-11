"""Tests for RM-031's ``Embedder`` protocol and its local ``fastembed``
implementation.

The real model is never loaded here except in the one test marked
``embedding_model`` (excluded from the default CI run -- see
pyproject.toml's own marker list and .github/workflows/ci.yml) -- every
other test patches ``fastembed.TextEmbedding`` at the import site
``embed/local.py`` actually uses it from (a local import inside
``_ensure_loaded``, not a module-level one -- see that module's own
docstring on why: constructing a ``LocalEmbedder`` must stay cheap even
when a run never calls ``embed`` at all).
"""

from __future__ import annotations

import sys
from typing import Any, ClassVar

import numpy as np
import platformdirs
import pytest
from repomind.embed.local import DIMENSIONS, MODEL_NAME, LocalEmbedder
from repomind.errors import EmbeddingError


class _FakeModel:
    """Stands in for ``fastembed.TextEmbedding`` -- records every call it
    receives and returns deterministic, correctly-shaped vectors, so
    tests here exercise ``LocalEmbedder``'s own logic (lazy loading,
    caching, error wrapping) without the real ONNX model or a network
    call anywhere in the loop.
    """

    instances: ClassVar[list[_FakeModel]] = []

    def __init__(self, *, model_name: str, cache_dir: str, lazy_load: bool) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.lazy_load = lazy_load
        self.embed_calls: list[tuple[list[str], int]] = []
        _FakeModel.instances.append(self)

    def embed(self, texts: list[str], batch_size: int) -> list[Any]:
        self.embed_calls.append((texts, batch_size))
        return [np.zeros(DIMENSIONS, dtype=np.float32) for _ in texts]


@pytest.fixture(autouse=True)
def _reset_fake_model_instances() -> None:
    _FakeModel.instances = []


@pytest.fixture
def fake_fastembed(monkeypatch: pytest.MonkeyPatch) -> type[_FakeModel]:
    monkeypatch.setattr("fastembed.TextEmbedding", _FakeModel)
    return _FakeModel


# -- basic behaviour ----------------------------------------------------


def test_embed_of_empty_sequence_returns_empty_list_without_loading_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("should not attempt to load a model for an empty batch")

    monkeypatch.setattr("fastembed.TextEmbedding", _boom)

    assert LocalEmbedder().embed([]) == []


def test_embed_returns_one_vector_per_text_in_order(fake_fastembed: type[_FakeModel]) -> None:
    embedder = LocalEmbedder()

    vectors = embedder.embed(["def f(): pass", "class C: pass"])

    assert len(vectors) == 2
    assert all(len(v) == DIMENSIONS for v in vectors)
    assert all(isinstance(v, list) for v in vectors)


def test_dimensions_attribute_matches_the_constant() -> None:
    assert LocalEmbedder().dimensions == DIMENSIONS == 384


# -- lazy loading and caching --------------------------------------------


def test_model_is_not_constructed_until_the_first_embed_call(
    fake_fastembed: type[_FakeModel],
) -> None:
    LocalEmbedder()
    assert fake_fastembed.instances == []


def test_model_is_constructed_exactly_once_across_multiple_embed_calls(
    fake_fastembed: type[_FakeModel],
) -> None:
    embedder = LocalEmbedder()

    embedder.embed(["a"])
    embedder.embed(["b"])

    assert len(fake_fastembed.instances) == 1


def test_lazy_load_true_is_passed_to_the_underlying_model(
    fake_fastembed: type[_FakeModel],
) -> None:
    LocalEmbedder().embed(["a"])
    assert fake_fastembed.instances[0].lazy_load is True


def test_batch_size_32_is_passed_to_the_underlying_embed_call(
    fake_fastembed: type[_FakeModel],
) -> None:
    LocalEmbedder().embed(["a", "b", "c"])

    texts, batch_size = fake_fastembed.instances[0].embed_calls[0]
    assert texts == ["a", "b", "c"]
    assert batch_size == 32


# -- cache_dir ------------------------------------------------------------


def test_default_cache_dir_comes_from_platformdirs(fake_fastembed: type[_FakeModel]) -> None:
    LocalEmbedder().embed(["a"])
    assert fake_fastembed.instances[0].cache_dir == platformdirs.user_cache_dir("repomind")


def test_explicit_cache_dir_overrides_the_default(fake_fastembed: type[_FakeModel]) -> None:
    LocalEmbedder(cache_dir="/custom/cache").embed(["a"])
    assert fake_fastembed.instances[0].cache_dir == "/custom/cache"


def test_explicit_model_name_overrides_the_default(fake_fastembed: type[_FakeModel]) -> None:
    LocalEmbedder(model_name="BAAI/bge-small-en").embed(["a"])
    assert fake_fastembed.instances[0].model_name == "BAAI/bge-small-en"


# -- error handling ---------------------------------------------------------


def test_raises_embedding_error_when_fastembed_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)  # simulates ImportError on import

    with pytest.raises(EmbeddingError, match="not installed"):
        LocalEmbedder().embed(["a"])


def test_raises_embedding_error_when_model_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise OSError("no network and nothing cached")

    monkeypatch.setattr("fastembed.TextEmbedding", _raise)

    with pytest.raises(EmbeddingError, match=MODEL_NAME):
        LocalEmbedder().embed(["a"])


def test_raises_embedding_error_when_the_underlying_embed_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenModel(_FakeModel):
        def embed(self, texts: list[str], batch_size: int) -> list[Any]:
            raise RuntimeError("onnxruntime blew up")

    monkeypatch.setattr("fastembed.TextEmbedding", _BrokenModel)

    with pytest.raises(EmbeddingError, match="onnxruntime blew up"):
        LocalEmbedder().embed(["a"])


def test_raises_embedding_error_on_dimension_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WrongSizeModel(_FakeModel):
        def embed(self, texts: list[str], batch_size: int) -> list[Any]:
            return [np.zeros(123, dtype=np.float32) for _ in texts]

    monkeypatch.setattr("fastembed.TextEmbedding", _WrongSizeModel)

    with pytest.raises(EmbeddingError, match="123"):
        LocalEmbedder().embed(["a"])


# -- the real model -----------------------------------------------------


@pytest.mark.embedding_model
@pytest.mark.slow
@pytest.mark.enable_socket
def test_real_model_produces_normalized_384_dim_vectors_for_real_code() -> None:
    """The one test in this file touching the real model -- confirms what
    was verified by hand while resolving RM-031's fastembed-vs-
    sentence-transformers question stays true: 384 dims, float32,
    L2-normalised (norm ~1.0), for genuinely code-shaped text.
    """
    embedder = LocalEmbedder()

    vectors = embedder.embed(
        [
            "def make_widget(name: str) -> Widget:\n    return Widget(name)",
            "class Widget:\n    pass",
        ]
    )

    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == 384
        norm = sum(x * x for x in vec) ** 0.5
        assert 0.99 <= norm <= 1.01
