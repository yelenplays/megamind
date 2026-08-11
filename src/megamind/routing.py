"""Deterministic lexical routing ladder: card -> digest -> index -> exact pages.

No embeddings, no network, no model calls. Scoring is plain token overlap with
fixed weights, stable sorting, and explicit context budgets, so the same query
against the same vault always returns the same answer, with reasons.

Phase 2 layers three deterministic additions on top, none of which changes the
lexical baseline: per-candidate route confidence with explicit reliance,
ambiguity, and no-match thresholds (see ``megamind.confidence``); optional
local semantic reranking that only reorders already-surfaced candidates (see
``megamind.semantic``); and per-candidate freshness/provenance so the bounded
evidence packet states what it rests on.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .confidence import (
    AMBIGUITY_BAND,
    OFFER_FLOOR,
    RELIANCE_FLOOR,
    SIGNAL_STRENGTH,
    authorize,
    route_confidence,
)
from .fsops import resolve_contained
from .links import extract_links
from .models import CONTENT_VISIBLE_PRIVACY, parse_document
from .registry import Registry, WikiEntry
from .semantic import SemanticBackend, disabled_outcome
from .semantic import rerank as semantic_rerank

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

# Notes are not counted against the context budget, so a note that lists what
# was dropped carries its own cap: it names this many items and then states how
# many more there were. Matches the truncation contract in docs/axi.md.
OMISSION_NAMES = 5

THRESHOLDS: dict[str, float] = {
    "reliance_floor": RELIANCE_FLOOR,
    "offer_floor": OFFER_FLOOR,
    "ambiguity_band": AMBIGUITY_BAND,
}


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


def bounded_names(names: list[str]) -> str:
    """Render dropped items for a single note: the first few, then a count."""
    shown = names[:OMISSION_NAMES]
    remaining = len(names) - len(shown)
    listed = ", ".join(shown)
    return f"{listed}, and {remaining} more" if remaining else listed


@dataclass
class RouteCandidate:
    wiki: str
    path: str
    kind: str  # "page" | "digest" | "index" | "card" | "pointer"
    score: int
    reasons: list[str] = field(default_factory=list)
    privacy: str = "public-reference"
    chars: int = 0
    confidence: float = 0.0
    freshness: dict[str, object] = field(default_factory=dict)
    semantic_score: float | None = None
    # A provisional wiki may be surfaced and offered, never auto-loaded.
    provisional: bool = False


@dataclass
class RouteResult:
    query: str
    matched: bool
    candidates: list[RouteCandidate]
    context_chars: int
    max_candidates: int
    max_context_chars: int
    decision: str = "no-match"  # "load" | "offer" | "no-match"
    confidence: float | None = None
    thresholds: dict[str, float] = field(default_factory=lambda: dict(THRESHOLDS))
    semantic: dict[str, object] = field(default_factory=dict)
    # One governance row per emitted candidate, keyed by the candidate's own
    # root-relative path. It is a sidecar rather than a candidate column so the
    # default candidate field set stays exactly what route-result/v2 promised,
    # while the trust posture is always available without opting in.
    governance: list[dict[str, object]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_if_exists(root: Path, rel: str) -> str | None:
    """Read a vault artifact, treating unreadable or non-UTF-8 files as absent."""
    if not rel:
        return None
    try:
        path = resolve_contained(root, rel)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _char_count(path: Path) -> int:
    """Size of an already-resolved artifact in characters; 0 when unreadable."""
    try:
        return len(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return 0


def _best_signal(signals: dict[str, float], token: str, strength: float) -> None:
    """Keep the strongest signal class a token matched."""
    if strength > signals.get(token, 0.0):
        signals[token] = strength


def _score_wiki(
    wiki: WikiEntry, query_tokens: list[str], card_text: str | None
) -> tuple[int, list[str], dict[str, float]]:
    score = 0
    reasons: list[str] = []
    signals: dict[str, float] = {}
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
            _best_signal(signals, token, SIGNAL_STRENGTH["trigger"])
        if token in name_tokens:
            score += WEIGHT_NAME
            reasons.append(f"name match: {token}")
            _best_signal(signals, token, SIGNAL_STRENGTH["name"])
        if token in description_tokens:
            score += WEIGHT_DESCRIPTION
            reasons.append(f"description match: {token}")
            _best_signal(signals, token, SIGNAL_STRENGTH["text"])
        if token in card_tokens and token not in keyword_tokens:
            score += WEIGHT_CARD_BODY
            reasons.append(f"card match: {token}")
            _best_signal(signals, token, SIGNAL_STRENGTH["text"])
    return score, reasons, signals


def _index_candidates(
    root: Path,
    wiki: WikiEntry,
    wiki_score: int,
    query_tokens: list[str],
    index_text: str,
    signals: dict[str, float],
) -> list[RouteCandidate]:
    document = parse_document(index_text)
    index_dir = Path(wiki.index).parent
    candidates: list[RouteCandidate] = []
    for link in extract_links(document.body):
        if link.style != "markdown":
            continue
        entry_score = 0
        reasons: list[str] = []
        entry_signals = dict(signals)
        label_tokens = _token_set(link.label)
        target_tokens = _token_set(link.target.replace("/", " ").replace("-", " "))
        for token in query_tokens:
            if token in label_tokens:
                entry_score += WEIGHT_INDEX_LABEL
                reasons.append(f"index entry match: {token}")
                _best_signal(entry_signals, token, SIGNAL_STRENGTH["index-label"])
            elif token in target_tokens:
                entry_score += WEIGHT_INDEX_TARGET
                reasons.append(f"index path match: {token}")
                _best_signal(entry_signals, token, SIGNAL_STRENGTH["index-target"])
        if entry_score == 0:
            continue
        page_rel = (index_dir / link.target.split("#", 1)[0]).as_posix()
        try:
            page_path = resolve_contained(root, page_rel)
        except ValueError:
            continue
        # Candidates always carry the canonical root-relative path: links that
        # climb out of the index directory with ".." resolve to the same page,
        # and agents consume the emitted path literally.
        page_rel = page_path.relative_to(root.resolve()).as_posix()
        # Characters, not bytes: every artifact must be measured in the unit the
        # budget and the reported context_chars promise. An unreadable page still
        # routes as a candidate; doctor is what reports it.
        chars = _char_count(page_path)
        candidates.append(
            RouteCandidate(
                wiki=wiki.name,
                path=page_rel,
                kind="page",
                score=wiki_score + entry_score,
                reasons=reasons,
                privacy=wiki.privacy,
                chars=chars,
                confidence=route_confidence(
                    [entry_signals.get(token, 0.0) for token in query_tokens], len(query_tokens)
                ),
                provisional=wiki.provisional,
            )
        )
    return candidates


def _candidate_freshness(
    root: Path, candidate: RouteCandidate, today: date | None, stale_days: int
) -> None:
    """Attach provenance freshness from the artifact's frontmatter.

    Reads the ``updated`` field of the candidate's own artifact. Without a
    reference date the age and staleness stay ``None`` (unknown) rather than
    being computed from a wall clock; an artifact without a parseable date is
    unknown too. Pointer candidates expose no content, so nothing is read.
    """
    freshness: dict[str, object] = {"updated": None, "age_days": None, "stale": None}
    if candidate.kind != "pointer":
        text = _read_if_exists(root, candidate.path)
        if text is not None:
            try:
                document = parse_document(text)
            except ValueError:
                document = None
            if document is not None:
                raw = str(document.frontmatter.get("updated", ""))
                try:
                    updated = date.fromisoformat(raw) if raw else None
                except ValueError:
                    updated = None
                if updated is not None:
                    freshness["updated"] = raw
                    if today is not None:
                        age = (today - updated).days
                        freshness["age_days"] = age
                        freshness["stale"] = age > stale_days
    candidate.freshness = freshness


def route(
    root: Path,
    registry: Registry,
    query: str,
    today: date | None = None,
    semantic: SemanticBackend | None = None,
) -> RouteResult:
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
            semantic=disabled_outcome().to_dict(),
            notes=["no usable terms in query"],
        )

    scored_wikis: list[tuple[int, WikiEntry, list[str], dict[str, float]]] = []
    for wiki in registry.wikis:
        card_text = _read_if_exists(root, wiki.card)
        score, reasons, signals = _score_wiki(wiki, query_tokens, card_text)
        if score > 0:
            scored_wikis.append((score, wiki, reasons, signals))
    scored_wikis.sort(key=lambda item: (-item[0], item[1].name))

    candidates: list[RouteCandidate] = []
    for wiki_score, wiki, wiki_reasons, signals in scored_wikis[:MAX_WIKIS_DESCENDED]:
        if wiki.privacy == "pointer-only":
            candidates.append(
                RouteCandidate(
                    wiki=wiki.name,
                    path=wiki.path,
                    kind="pointer",
                    score=wiki_score,
                    reasons=[*wiki_reasons, "pointer-only: content not routed, open manually"],
                    privacy=wiki.privacy,
                    confidence=route_confidence(
                        [signals.get(token, 0.0) for token in query_tokens], len(query_tokens)
                    ),
                    provisional=wiki.provisional,
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
                    confidence=route_confidence(
                        [signals.get(token, 0.0) for token in query_tokens], len(query_tokens)
                    ),
                    provisional=wiki.provisional,
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
                    _best_signal(signals, token, SIGNAL_STRENGTH["digest"])
        index_text = _read_if_exists(root, wiki.index)
        page_candidates: list[RouteCandidate] = []
        if index_text is not None:
            page_candidates = _index_candidates(
                root, wiki, wiki_score + digest_bonus, query_tokens, index_text, signals
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
                    confidence=route_confidence(
                        [signals.get(token, 0.0) for token in query_tokens], len(query_tokens)
                    ),
                    provisional=wiki.provisional,
                )
            )

    candidates.sort(key=lambda c: (-c.score, c.path))

    # The no-match floor: evidence too weak to rely on or offer is dropped
    # with a note, so a weak hit never degrades into silent content loading.
    kept: list[RouteCandidate] = []
    too_weak: list[str] = []
    for candidate in candidates:
        if candidate.confidence < OFFER_FLOOR:
            too_weak.append(candidate.path)
        else:
            kept.append(candidate)
    candidates = kept
    if too_weak:
        notes.append(f"below the no-match floor ({OFFER_FLOOR}): omitted {bounded_names(too_weak)}")

    # Thresholds always decide on lexical confidence; the optional semantic
    # pass below may reorder but never recomputes them. `authorize` also names
    # which candidates the decision covers, so a `load` packet can never carry
    # a candidate that did not itself clear the reliance floor.
    decision, authorized = authorize([candidate.confidence for candidate in candidates])
    top_confidence = max((candidate.confidence for candidate in candidates), default=None)

    # Governance gate, applied after the thresholds and never widening them: a
    # provisional wiki is not trusted active knowledge until confidence
    # coverage and a later evaluation pass, so it may be offered as an explicit
    # choice but never enters a packet the host is told it may load.
    untrusted: list[int] = []
    withheld: list[str] = []
    governance_downgrade = False
    if decision == "load":
        untrusted = [index for index in authorized if candidates[index].provisional]
        if untrusted:
            authorized = [index for index in authorized if index not in set(untrusted)]
            withheld = [candidates[index].path for index in untrusted]
        if not authorized:
            decision = "offer"
            governance_downgrade = True

    if decision == "load":
        # Provisional candidates are accounted for by the governance note below;
        # the reliance-floor note must not claim them for a reason not theirs.
        loadable = set(authorized) | set(untrusted)
        demoted = [
            candidate.path for index, candidate in enumerate(candidates) if index not in loadable
        ]
        candidates = [candidates[index] for index in authorized]
        if demoted:
            notes.append(
                f"below the reliance floor ({RELIANCE_FLOOR}): omitted {bounded_names(demoted)}; "
                "a load packet carries only candidates that may be opened"
            )
    elif governance_downgrade:
        notes.append(
            f"governance downgrade, not a confidence downgrade: route confidence {top_confidence} "
            f"meets the reliance floor ({RELIANCE_FLOOR}), but every candidate that cleared it is "
            "provisional: offer choices, load nothing automatically"
        )
    elif decision == "offer" and top_confidence is not None:
        if top_confidence < RELIANCE_FLOOR:
            notes.append(
                f"route confidence {top_confidence} is below the reliance floor "
                f"({RELIANCE_FLOOR}): offer choices, load nothing automatically"
            )
        else:
            notes.append(
                f"top candidates are within the ambiguity band ({AMBIGUITY_BAND}): "
                "offer a choice instead of loading"
            )

    selected: list[RouteCandidate] = []
    over_budget: list[str] = []
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
            over_budget.append(candidate.path)
            continue
        selected.append(candidate)
        if counts:
            context_chars += candidate.chars
    if over_budget:
        notes.append(f"context budget reached: omitted {bounded_names(over_budget)}")

    # Optional local semantic rerank: it runs last, over the packet the lexical
    # ladder already selected, so thresholds, membership, and budgets are all
    # settled before it can touch anything. It sees only text each candidate is
    # already authorized to expose (pointer candidates get none), and reads no
    # content at all while it is disabled. Any backend failure returns the
    # lexical order with a typed, inspectable outcome.
    texts = (
        [
            "" if candidate.kind == "pointer" else (_read_if_exists(root, candidate.path) or "")
            for candidate in selected
        ]
        if semantic is not None
        else ["" for _ in selected]
    )
    order, outcome = semantic_rerank(
        query, [float(candidate.score) for candidate in selected], texts, semantic
    )
    if outcome.status == "ok":
        for candidate, similarity in zip(selected, outcome.scores, strict=True):
            candidate.semantic_score = similarity
    selected = [selected[index] for index in order]

    for candidate in selected:
        _candidate_freshness(root, candidate, today, budgets.stale_days)

    if not selected:
        notes.append("no wiki matched this query")

    # The sidecar states the trust posture of every emitted candidate, whatever
    # the decision and whatever `--fields` the host asked for. It is named once
    # more in the notes so a reader of the prose sees the same governance fact.
    governance: list[dict[str, object]] = [
        {
            "path": candidate.path,
            "provisional": candidate.provisional,
            "trusted": not candidate.provisional,
        }
        for candidate in selected
    ]
    named = sorted(
        set(withheld) | {candidate.path for candidate in selected if candidate.provisional}
    )
    if named:
        notes.append(
            "governance gate, not a confidence threshold: provisional wikis may be offered or "
            "nominated but are never an authorized load until confidence coverage and a later "
            f"evaluation pass: {bounded_names(named)}"
        )

    return RouteResult(
        query=query,
        matched=bool(selected),
        candidates=selected,
        context_chars=context_chars,
        max_candidates=budgets.max_candidates,
        max_context_chars=budgets.max_context_chars,
        decision=decision if selected else "no-match",
        confidence=top_confidence if selected else None,
        semantic=outcome.to_dict(),
        governance=governance,
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
