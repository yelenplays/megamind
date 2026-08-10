"""Optional local semantic reranking behind the lexical baseline.

The deterministic lexical ladder stays the source of truth and the offline
default. A semantic backend may only *reorder* candidates that the lexical
layer already surfaced and that model-access filtering already authorized; it
never changes membership, budgets, thresholds, or access, and it never sees
text the candidate is not already allowed to expose (pointer candidates get
no text at all). Everything runs locally and deterministically: the built-in
backend is character n-gram cosine similarity, with no embeddings, no model
calls, and no network.

The layer fails typed, never silently: ``disabled`` (the default), ``ok``,
``unavailable`` (the backend cannot serve), or ``error`` (the backend is
corrupt or raised). In every non-``ok`` state the lexical order is returned
unchanged and the outcome states why, so policy never shifts under the hood.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# How much the semantic similarity moves the blended ordering. The lexical
# baseline keeps the majority share; thresholds and match decisions always
# use lexical confidence alone.
SEMANTIC_BLEND = 0.4

SEMANTIC_STATUSES: tuple[str, ...] = ("disabled", "ok", "unavailable", "error")

NGRAM_SIZE = 3


@runtime_checkable
class SemanticBackend(Protocol):
    """A local similarity engine. Implementations must be deterministic."""

    name: str

    def unavailable_reason(self) -> str | None:
        """None when the backend can serve; otherwise a plain-English reason."""
        ...

    def similarity(self, query: str, text: str) -> float:
        """A deterministic similarity in [0, 1]; 0 for empty input."""
        ...


def _ngrams(text: str) -> dict[str, int]:
    """Character n-gram counts over normalized text, for cosine similarity."""
    normalized = f"  {' '.join(text.lower().split())}  "
    grams: dict[str, int] = {}
    for index in range(len(normalized) - NGRAM_SIZE + 1):
        gram = normalized[index : index + NGRAM_SIZE]
        grams[gram] = grams.get(gram, 0) + 1
    return grams


def ngram_similarity(first: str, second: str) -> float:
    """Cosine similarity over character n-gram count vectors; deterministic."""
    if not first.strip() or not second.strip():
        return 0.0
    left, right = _ngrams(first), _ngrams(second)
    dot = sum(count * right.get(gram, 0) for gram, count in left.items())
    if dot == 0:
        return 0.0
    norm = math.sqrt(sum(count * count for count in left.values())) * math.sqrt(
        sum(count * count for count in right.values())
    )
    return round(dot / norm, 4) if norm else 0.0


@dataclass
class NgramBackend:
    """The built-in backend: local char n-gram cosine, always available."""

    name: str = "char-ngram"

    def unavailable_reason(self) -> str | None:
        return None

    def similarity(self, query: str, text: str) -> float:
        return ngram_similarity(query, text)


@dataclass
class RerankOutcome:
    """The typed, inspectable result of a rerank attempt.

    ``scores`` is positional: entry *i* is the similarity of candidate *i* in
    the order the caller supplied. Candidates are never identified by a key
    here, because caller-side keys (page paths, wiki names) are not guaranteed
    unique and collapsing two candidates onto one similarity would silently
    mis-score both.
    """

    status: str
    backend: str
    reason: str = ""
    scores: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"status": self.status, "backend": self.backend}
        if self.reason:
            data["reason"] = self.reason
        return data


def disabled_outcome() -> RerankOutcome:
    return RerankOutcome(status="disabled", backend="none", reason="semantic reranking not enabled")


def rerank(
    query: str,
    lexical_scores: list[float],
    texts: list[str],
    backend: SemanticBackend | None,
) -> tuple[list[int], RerankOutcome]:
    """Blend lexical and semantic scores and return the new ordering.

    ``texts`` carries only text each candidate is already authorized to
    expose, and may be empty (such candidates keep a similarity of 0), aligned
    positionally with ``lexical_scores``. The returned list is a permutation of
    ``range(len(lexical_scores))``: on any backend problem it is the identity
    permutation and the outcome says why. This function never raises for
    backend failures.
    """
    identity = list(range(len(lexical_scores)))
    if backend is None:
        return identity, disabled_outcome()
    try:
        reason = backend.unavailable_reason()
    except Exception as error:  # a corrupt backend must fail typed
        return identity, RerankOutcome(
            status="error", backend=backend.name, reason=f"availability check failed: {error}"
        )
    if reason is not None:
        return identity, RerankOutcome(status="unavailable", backend=backend.name, reason=reason)
    try:
        scores = [
            round(backend.similarity(query, text), 4) if text else 0.0
            for _lexical, text in zip(lexical_scores, texts, strict=True)
        ]
    except Exception as error:
        return identity, RerankOutcome(
            status="error", backend=backend.name, reason=f"similarity failed: {error}"
        )

    peak = max(lexical_scores, default=0.0)

    def blended(index: int) -> float:
        lexical = lexical_scores[index] / peak if peak > 0 else 0.0
        return (1.0 - SEMANTIC_BLEND) * lexical + SEMANTIC_BLEND * scores[index]

    # Stable: ties keep the lexical order, so a semantic pass can never
    # introduce nondeterminism or reorder equal-evidence candidates.
    order = sorted(identity, key=lambda index: (-blended(index), index))
    return order, RerankOutcome(status="ok", backend=backend.name, scores=scores)
