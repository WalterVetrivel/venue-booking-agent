"""Local CPU embeddings (bge-small-en-v1.5 via fastembed). The model downloads once, on first use."""

from functools import cache
from typing import Protocol

from backend.config import get_settings


class Embedder(Protocol):
    def passages(self, texts: list[str]) -> list[list[float]]: ...
    def query(self, text: str) -> list[float]: ...


class FastEmbedder:
    def __init__(self, model_name: str):
        from fastembed import TextEmbedding  # imported here: loading it is slow and tests use a fake

        self.model = TextEmbedding(model_name)

    def passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.passage_embed(texts)]

    def query(self, text: str) -> list[float]:
        return next(iter(self.model.query_embed(text))).tolist()


@cache
def get_embedder() -> Embedder:
    return FastEmbedder(get_settings().embedding_model)
