"""Deterministic lexical routing ladder: card -> digest -> index -> exact pages.

No embeddings, no network, no model calls. Scoring is plain token overlap with
fixed weights, stable sorting, and explicit context budgets, so the same query
against the same vault always returns the same answer, with reasons.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .fsops import resolve_contained
from .links import extract_links
from .models import CONTENT_VISIBLE_PRIVACY, parse_document
from .registry import Registry, WikiEntry

_WORD = re.compile(r"[a-z0-9]+")

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "done",
        "how",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "i",
        "we",
        "you",
        "they",
        "it",
        "he",
        "she",
        "of",
        "in",
        "on",
        "for",
        "to",
        "with",
        "and",
        "or",
        "not",
        "no",
        "my",
        "our",
        "your",
        "their",
        "its",
        "this",
        "that",
        "these",
        "those",
        "about",
        "can",
        "could",
        "should",
        "would",
        "will",
        "shall",
        "may",
        "might",
        "me",
        "us",
        "them",
        "him",
        "her",
        "at",
        "by",
        "from",
        "as",
        "if",
        "then",
        "than",
        "into",
        "over",
        "under",
        "out",
        "up",
        "down",
        "there",
        "here",
        "just",
        "also",
        "very",
        "more",
        "most",
        "some",
        "any",
        "all",
        "each",
    ]
)

# Fixed scoring weights of the ladder. Documented in docs/architecture.md.
WEIGHT_KEYWORD = 3
WEIGHT_NAME = 2
WEIGHT_DESCRIPTION = 1
WEIGHT_CARD_BODY = 1
WEIGHT_INDEX_LABEL = 2
WEIGHT_INDEX_TARGET = 1
WEIGHT_DIGEST = 1

MAX_WIKIS_DESCENDED = 3


def _normalize(token: str) -> str:
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Lowercased, stopword-filtered, naively singularized tokens, order preserved."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in _WORD.findall(text.lower()):
        if raw in STOPWORDS or len(raw) < 2:
            continue
        token = _normalize(raw)
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


def _token_set(text: str) -> set[str]:
    return set(tokenize(text))


@dataclass
class RouteCandidate:
    wiki: str
    path: str
    kind: str  # "page" | "digest" | "index" | "card" | "pointer"
    score: int
    reasons: list[str] = field(default_factory=list)
    privacy: str = "public-reference"
    chars: int = 0


@dataclass
class RouteResult:
    query: str
    matched: bool
    candidates: list[RouteCandidate]
    context_chars: int
    max_candidates: int
    max_context_chars: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_if_exists(root: Path, rel: str) -> str | None:
    if not rel:
        return None
    try:
        path = resolve_contained(root, rel)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _score_wiki(
    wiki: WikiEntry, query_tokens: list[str], card_text: str | None
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    keyword_tokens: set[str] = set()
    for keyword in wiki.keywords:
        keyword_tokens.update(_token_set(keyword))
    name_tokens = _token_set(wiki.name)
    description_tokens = _token_set(wiki.description)
    card_tokens = _token_set(card_text) if card_text else set()
    for token in query_tokens:
        if token in keyword_tokens:
            score += WEIGHT_KEYWORD
            reasons.append(f"keyword match: {token}")
        if token in name_tokens:
            score += WEIGHT_NAME
            reasons.append(f"name match: {token}")
        if token in description_tokens:
            score += WEIGHT_DESCRIPTION
            reasons.append(f"description match: {token}")
        if token in card_tokens and token not in keyword_tokens:
            score += WEIGHT_CARD_BODY
            reasons.append(f"card match: {token}")
    return score, reasons


def _index_candidates(
    root: Path,
    wiki: WikiEntry,
    wiki_score: int,
    query_tokens: list[str],
    index_text: str,
) -> list[RouteCandidate]:
    document = parse_document(index_text)
    index_dir = Path(wiki.index).parent
    candidates: list[RouteCandidate] = []
    for link in extract_links(document.body):
        if link.style != "markdown":
            continue
        entry_score = 0
        reasons: list[str] = []
        label_tokens = _token_set(link.label)
        target_tokens = _token_set(link.target.replace("/", " ").replace("-", " "))
        for token in query_tokens:
            if token in label_tokens:
                entry_score += WEIGHT_INDEX_LABEL
                reasons.append(f"index entry match: {token}")
            elif token in target_tokens:
                entry_score += WEIGHT_INDEX_TARGET
                reasons.append(f"index path match: {token}")
        if entry_score == 0:
            continue
        page_rel = (index_dir / link.target.split("#", 1)[0]).as_posix()
        try:
            page_path = resolve_contained(root, page_rel)
        except ValueError:
            continue
        # Characters, not bytes: every artifact must be measured in the unit the
        # budget and the reported context_chars promise.
        chars = len(page_path.read_text(encoding="utf-8")) if page_path.is_file() else 0
        candidates.append(
            RouteCandidate(
                wiki=wiki.name,
                path=page_rel,
                kind="page",
                score=wiki_score + entry_score,
                reasons=reasons,
                privacy=wiki.privacy,
                chars=chars,
            )
        )
    return candidates


def route(root: Path, registry: Registry, query: str) -> RouteResult:
    """Run the routing ladder for a query. Deterministic and read-only."""
    budgets = registry.budgets
    query_tokens = tokenize(query)
    notes: list[str] = []
    if not query_tokens:
        return RouteResult(
            query=query,
            matched=False,
            candidates=[],
            context_chars=0,
            max_candidates=budgets.max_candidates,
            max_context_chars=budgets.max_context_chars,
            notes=["no usable terms in query"],
        )

    scored_wikis: list[tuple[int, WikiEntry, list[str]]] = []
    for wiki in registry.wikis:
        card_text = _read_if_exists(root, wiki.card)
        score, reasons = _score_wiki(wiki, query_tokens, card_text)
        if score > 0:
            scored_wikis.append((score, wiki, reasons))
    scored_wikis.sort(key=lambda item: (-item[0], item[1].name))

    candidates: list[RouteCandidate] = []
    for wiki_score, wiki, wiki_reasons in scored_wikis[:MAX_WIKIS_DESCENDED]:
        if wiki.privacy == "pointer-only":
            candidates.append(
                RouteCandidate(
                    wiki=wiki.name,
                    path=wiki.path,
                    kind="pointer",
                    score=wiki_score,
                    reasons=[*wiki_reasons, "pointer-only: content not routed, open manually"],
                    privacy=wiki.privacy,
                )
            )
            continue
        digest_text = _read_if_exists(root, wiki.digest)
        if wiki.privacy == "digest-only":
            path = wiki.digest or wiki.path
            candidates.append(
                RouteCandidate(
                    wiki=wiki.name,
                    path=path,
                    kind="digest",
                    score=wiki_score,
                    reasons=[*wiki_reasons, "digest-only: exact pages not routed"],
                    privacy=wiki.privacy,
                    chars=len(digest_text) if digest_text else 0,
                )
            )
            continue
        digest_bonus = 0
        digest_reasons: list[str] = []
        if digest_text:
            digest_tokens = _token_set(digest_text)
            for token in query_tokens:
                if token in digest_tokens:
                    digest_bonus += WEIGHT_DIGEST
                    digest_reasons.append(f"digest match: {token}")
        index_text = _read_if_exists(root, wiki.index)
        page_candidates: list[RouteCandidate] = []
        if index_text is not None:
            page_candidates = _index_candidates(
                root, wiki, wiki_score + digest_bonus, query_tokens, index_text
            )
            for candidate in page_candidates:
                candidate.reasons = wiki_reasons + digest_reasons + candidate.reasons
        if page_candidates:
            candidates.extend(page_candidates)
        else:
            # Ladder fallback: no page matched, hand back the smallest useful artifact.
            fallback_path, fallback_kind = _fallback_artifact(wiki)
            fallback_text = _read_if_exists(root, fallback_path)
            candidates.append(
                RouteCandidate(
                    wiki=wiki.name,
                    path=fallback_path,
                    kind=fallback_kind,
                    score=wiki_score + digest_bonus,
                    reasons=[
                        *wiki_reasons,
                        *digest_reasons,
                        "no index entry matched: start from this artifact",
                    ],
                    privacy=wiki.privacy,
                    chars=len(fallback_text) if fallback_text else 0,
                )
            )

    candidates.sort(key=lambda c: (-c.score, c.path))
    selected: list[RouteCandidate] = []
    context_chars = 0
    for candidate in candidates:
        if len(selected) >= budgets.max_candidates:
            notes.append("candidate budget reached: further matches omitted")
            break
        # Every artifact whose content is handed back counts against the budget,
        # so context_chars can never exceed max_context_chars. pointer-only
        # candidates expose paths alone and contribute nothing. The highest
        # scoring candidate is always kept, even alone over budget, so a real
        # match never degrades into a silent empty result.
        exposes_content = candidate.privacy in CONTENT_VISIBLE_PRIVACY or candidate.kind == "digest"
        counts = exposes_content and candidate.kind != "pointer" and candidate.chars > 0
        if counts and selected and context_chars + candidate.chars > budgets.max_context_chars:
            notes.append(f"context budget reached: omitted {candidate.path}")
            continue
        selected.append(candidate)
        if counts:
            context_chars += candidate.chars

    if not selected:
        notes.append("no wiki matched this query")
    return RouteResult(
        query=query,
        matched=bool(selected),
        candidates=selected,
        context_chars=context_chars,
        max_candidates=budgets.max_candidates,
        max_context_chars=budgets.max_context_chars,
        notes=notes,
    )


def _fallback_artifact(wiki: WikiEntry) -> tuple[str, str]:
    if wiki.digest:
        return wiki.digest, "digest"
    if wiki.index:
        return wiki.index, "index"
    if wiki.card:
        return wiki.card, "card"
    return wiki.path, "pointer"
