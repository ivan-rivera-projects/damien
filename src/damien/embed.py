"""Embeddings and clustering: collapse a plan into review groups.

Why this exists: 200 messages in a real plan came from 140 senders, 102 of them
with one message each. So grouping by sender buys 1.4x — nothing. The messages
that deserve the *same* decision come from *different* senders (forty shops all
sending a promotion), which is what a vector is for.

Local and small on purpose: fastembed ships a ~130 MB ONNX model and needs no
torch. The vectors are stored per message, so clustering a plan costs one pass
and a re-worded question never invalidates them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from .models import Message

DIM = 384                     # fastembed's default (BAAI/bge-small-en-v1.5)
# Chosen from the data, not from taste. On a real 200-message plan the median
# pairwise cosine among marketing mail was 0.58 and only 3% of pairs reached 0.70:
#   0.80 -> 178 groups (no collapse at all)   0.70 -> 75
#   0.65 -> 13 groups, coherent              0.60 -> 10, but finance offers dissolved
#                              into the marketing blob, losing a useful distinction
DEFAULT_SIMILARITY = 0.65


def embed_text(message: Message) -> str:
    """What a message is *about*, as the embedder sees it — same facts the judge gets."""
    return "\n".join(filter(None, [
        message.subject or "",
        f"{message.from_name or ''} <{message.from_addr or ''}>".strip(),
        (message.snippet or "")[:300],
    ]))


class Embedder:
    """A local embedding model. Imported lazily: nothing else needs fastembed."""

    name = "bge-small-en-v1.5"

    def __init__(self, batch_size: int = 32):
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding  # optional extra: [vectors]
            self._model = TextEmbedding()
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return [list(map(float, v)) for v in self._load().embed(list(texts), batch_size=self.batch_size)]

    def embed_messages(self, messages: Sequence[Message]) -> list[list[float]]:
        return self.embed([embed_text(m) for m in messages])


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def cluster(items: Sequence[tuple[str, Sequence[float]]], *, similarity: float = DEFAULT_SIMILARITY) -> list[list[str]]:
    """Greedy nearest-centroid: each item joins the first group it is close enough to.

    Deterministic for a given input order. Returns groups of message ids, largest
    first. With numpy available (fastembed brings it) this is a matrix operation;
    without it, plain Python still clusters a few hundred messages instantly.
    """
    if not items:
        return []
    try:
        import numpy as np
    except ImportError:
        return _cluster_python(items, similarity)
    return _cluster_numpy(items, similarity, np)


def _cluster_numpy(items, similarity, np) -> list[list[str]]:
    ids = [i for i, _ in items]
    vectors = np.asarray([list(v) for _, v in items], dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    unit = vectors / np.where(norms == 0, 1.0, norms)

    centroids: list = []                     # unit vectors
    members: list[list[int]] = []
    counts: list[int] = []
    for idx in range(len(ids)):
        vec = unit[idx]
        if centroids:
            sims = np.asarray(centroids) @ vec      # cosine, because all are unit vectors
            best = int(np.argmax(sims))
            if float(sims[best]) >= similarity:
                members[best].append(idx)
                counts[best] += 1
                centroids[best] = centroids[best] + (vec - centroids[best]) / counts[best]
                centroids[best] = centroids[best] / max(1e-9, float(np.linalg.norm(centroids[best])))
                continue
        centroids.append(vec)
        members.append([idx])
        counts.append(1)
    groups = sorted(members, key=len, reverse=True)
    return [[ids[i] for i in group] for group in groups]


def _cluster_python(items, similarity) -> list[list[str]]:
    groups: list[tuple[list[float], list[str]]] = []
    for mid, vec in items:
        best, best_sim = None, -2.0
        for i, (centroid, _) in enumerate(groups):
            sim = cosine(vec, centroid)
            if sim > best_sim:
                best, best_sim = i, sim
        if best is not None and best_sim >= similarity:
            centroid, members = groups[best]
            n = len(members) + 1
            centroid = [(c * (n - 1) + v) / n for c, v in zip(centroid, vec, strict=True)]
            norm = math.sqrt(sum(c * c for c in centroid)) or 1.0
            groups[best] = ([c / norm for c in centroid], [*members, mid])
        else:
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            groups.append(([v / norm for v in vec], [mid]))
    groups.sort(key=lambda kv: len(kv[1]), reverse=True)
    return [members for _, members in groups]


def label_for(group_messages: Iterable[Message], limit: int = 3) -> str:
    """A short, honest name for a group: the senders' domains and a sample subject."""
    domains: list[str] = []
    for m in group_messages:
        domain = (m.from_addr or "").split("@")[-1]
        if domain and domain not in domains:
            domains.append(domain)
    return ", ".join(domains[:limit]) or "—"
