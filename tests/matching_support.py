"""Shared test utilities for the matching pipeline (not production code)."""

import hashlib
import math

from app.knowledge.qdrant import QdrantIndex
from app.matching.aliases import normalize_term


class WordEmbedder:
    """Deterministic bag-of-words vectors: texts sharing words have higher cosine."""

    dimensions = 64

    def __init__(self, fingerprint="match-test-v1"):
        self.fingerprint = fingerprint
        self.encoded = 0

    def chunks(self, text):
        words = text.split()
        if not words:
            raise ValueError("Empty embedding input")
        return [" ".join(words[i : i + 40]) for i in range(0, len(words), 40)]

    def encode(self, texts):
        self.encoded += len(texts)
        return [self.vector(text) for text in texts]

    def vector(self, text):
        values = [0.0] * self.dimensions
        for word in normalize_term(text).split() or ["empty"]:
            values[int(hashlib.sha256(word.encode()).hexdigest(), 16) % self.dimensions] += 1
        norm = math.sqrt(sum(v * v for v in values))
        return [v / norm for v in values]


def demo_settings(settings):
    return settings.model_copy(
        update={
            "platform_users_collection": "demo_users",
            "platform_candidates_collection": "demo_candidates",
            "platform_companies_collection": "demo_companies",
            "platform_offers_collection": "demo_offers",
        }
    )


def qdrant_for(client, embedder):
    return QdrantIndex(client, embedder.dimensions, embedder.fingerprint)
