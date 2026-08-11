"""Model-access-aware catalog preflight for substantive requests.

A host submits a privacy-safe request representation plus its declared model
class (local or cloud). Preflight answers at the catalog level only: which
wikis cover the request, why, and exactly what the declared model class may
follow up on. It never returns page content from any root, never mutates a
wiki, never writes a host record, never calls a model or the network.

Filtering happens before any path is handed out: wikis whose effective access
for the declared class is ``none`` move to the privacy-filtered list, pointer
wikis return location metadata and zero content, digest-only wikis expose only
their approved digest. The deterministic content-hashed ``preflight_id`` binds
the request hash, the catalog snapshot, the model class, and the result, so a
host can prove preflight ran without storing the raw prompt.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field

from .catalog import Catalog, RootRef, build_catalog
from .confidence import (
    AMBIGUITY_BAND,
    OFFER_FLOOR,
    RELIANCE_FLOOR,
    SIGNAL_STRENGTH,
    authorize,
    route_confidence,
)
from .fsops import content_hash
from .routing import bounded_names, tokenize
from .semantic import SemanticBackend, disabled_outcome
from .semantic import rerank as semantic_rerank

WEIGHT_TRIGGER = 3
WEIGHT_NAME = 2
WEIGHT_TEXT = 1

MODEL_CLASSES = ("local", "cloud")

# The one owner of the lexical signal classes. `_score_row` emits reasons and
# counts for exactly these classes and every derived summary reads them back
# from here.
SIGNAL_CLASSES = ("trigger", "name", "scope")

THRESHOLDS: dict[str, float] = {
    "reliance_floor": RELIANCE_FLOOR,
    "offer_floor": OFFER_FLOOR,
    "ambiguity_band": AMBIGUITY_BAND,
}


@dataclass
class PreflightResult:
    request: str
    model_class: str
    status: str
    matches: list[dict[str, object]] = field(default_factory=list)
    offers: list[dict[str, object]] = field(default_factory=list)
    filtered: list[dict[str, object]] = field(default_factory=list)
    declined: list[dict[str, object]] = field(default_factory=list)
    root_issues: list[dict[str, object]] = field(default_factory=list)
    redacted_count: int = 0
    confidence: float | None = None
    thresholds: dict[str, float] = field(default_factory=lambda: dict(THRESHOLDS))
    semantic: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    catalog_hash: str = ""
    request_hash: str = ""
    preflight_id: str = ""


def _token_set(text: str) -> set[str]:
    return set(tokenize(text))


def _str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _access_level(row: dict[str, object], model_class: str) -> str:
    access = row.get("model_access")
    if isinstance(access, dict):
        level = access.get("cloud" if model_class == "cloud" else "local")
        if isinstance(level, str) and level:
            return level
    return "none"


def _best_signal(signals: dict[str, float], token: str, strength: float) -> None:
    if strength > signals.get(token, 0.0):
        signals[token] = strength


def _reason(signal_class: str, token: str) -> str:
    return f"{signal_class} match: {token}"


@dataclass(frozen=True)
class _Lexical:
    """One card's lexical scoring outcome, produced once by `_score_row`.

    `reasons` keeps the literal public reason strings and `counts` records the
    same evidence per signal class as it is produced, so nothing downstream has
    to take the reason wording apart again to recover what matched.
    """

    score: int
    reasons: list[str]
    signals: dict[str, float]
    counts: dict[str, int]


def _score_row(row: dict[str, object], query_tokens: list[str]) -> _Lexical:
    score = 0
    reasons: list[str] = []
    signals: dict[str, float] = {}
    counts = dict.fromkeys(SIGNAL_CLASSES, 0)
    trigger_tokens: set[str] = set()
    for item in _str_list(row.get("keywords")) + _str_list(row.get("triggers")):
        trigger_tokens.update(_token_set(item))
    name_tokens = _token_set(str(row.get("name", "")))
    text_tokens = _token_set(
        " ".join(
            [
                str(row.get("purpose", "")),
                " ".join(_str_list(row.get("answers"))),
                " ".join(_str_list(row.get("examples"))),
            ]
        )
    )
    buckets: dict[str, tuple[set[str], int, float]] = {
        "trigger": (trigger_tokens, WEIGHT_TRIGGER, SIGNAL_STRENGTH["trigger"]),
        "name": (name_tokens, WEIGHT_NAME, SIGNAL_STRENGTH["name"]),
        "scope": (text_tokens, WEIGHT_TEXT, SIGNAL_STRENGTH["text"]),
    }
    for token in query_tokens:
        for signal_class in SIGNAL_CLASSES:
            candidates, weight, strength = buckets[signal_class]
            if token in candidates:
                score += weight
                reasons.append(_reason(signal_class, token))
                counts[signal_class] += 1
                _best_signal(signals, token, strength)
    return _Lexical(score=score, reasons=reasons, signals=signals, counts=counts)


def _card_text(row: dict[str, object]) -> str:
    """The card-level text a semantic pass may see: exactly the declared card
    fields the lexical layer already scores, never page content."""
    return " ".join(
        [
            str(row.get("name", "")),
            str(row.get("purpose", "")),
            " ".join(_str_list(row.get("answers"))),
            " ".join(_str_list(row.get("examples"))),
            " ".join(_str_list(row.get("keywords"))),
            " ".join(_str_list(row.get("triggers"))),
        ]
    )


def _declined(row: dict[str, object], query_tokens: list[str]) -> str | None:
    negative: set[str] = set()
    for item in _str_list(row.get("negative_triggers")):
        negative.update(_token_set(item))
    for token in query_tokens:
        if token in negative:
            return f"negative trigger match: {token}"
    return None


@dataclass(frozen=True)
class _FollowUp:
    """The single owner of what an authorized match may load next.

    `loadable` states whether `text` actually hands the host a path it may
    load. Every follow-up that instead says "load nothing" or exposes location
    metadata only reports ``False``, so nothing derived from a load path (the
    context budget above all) can disagree with the sentence beside it.
    """

    text: str
    loadable: bool


def _follow_up(row: dict[str, object], request: str, access: str) -> _FollowUp:
    root = str(row.get("root", ""))
    paths = row.get("paths", {})
    assert isinstance(paths, dict)
    if row.get("routing_mode") == "pointer":
        return _FollowUp(
            f"Open {root} manually; pointer wikis expose location metadata only", False
        )
    if access == "digest-only":
        digest = str(paths.get("digest") or "")
        if digest:
            return _FollowUp(
                f"Read only the approved digest {root}/{digest}; nothing else may be loaded", True
            )
        return _FollowUp(
            f"{root} allows digest-only access but declares no digest; load nothing", False
        )
    if row.get("source") == "registry":
        # `help[]` steps are executable with runtime values filled in. Both the
        # root and the host-supplied request are untrusted text, so shell-quote
        # each so the emitted follow-up stays exactly one runnable command.
        return _FollowUp(
            f"Run `megamind-axi --root {shlex.quote(root)} route {shlex.quote(request)}` "
            "for the bounded ladder",
            True,
        )
    index = str(paths.get("index") or "wiki/index.md")
    return _FollowUp(f"Open {root}/{index} and follow its links within the context budget", True)


def _evidence_summary(
    row: dict[str, object],
    query_tokens: list[str],
    lexical: _Lexical,
    semantic_score: float | None,
) -> dict[str, object]:
    """Return bounded card provenance without echoing request tokens.

    The lexical scorer still uses the exact same reasons and signals for
    confidence, and `matches[].reasons` still carries them verbatim. This
    summary is the privacy-minimized view: signal classes and numeric coverage
    explain the route without retaining query-derived words or page content.
    `lexical_classes` is the ordered subset of `signal_counts` that actually
    fired, stated once so a consumer never has to guess which encoding leads.
    """
    counts = lexical.counts
    matched_terms = len(lexical.signals)
    return {
        "routing_class": "lexical-card",
        "coverage": {
            "matched_terms": matched_terms,
            "request_terms": len(query_tokens),
            "ratio": round(matched_terms / len(query_tokens), 4) if query_tokens else 0.0,
        },
        "signal_counts": dict(counts),
        "provenance": {
            "source": "canonical-card" if row.get("source") == "wiki-card" else "registry-card",
            "scope": "declared card metadata only",
            "page_content": False,
        },
        "lexical_classes": [
            signal_class for signal_class in SIGNAL_CLASSES if counts[signal_class]
        ],
        "semantic": semantic_score,
    }


def _context_budget(row: dict[str, object]) -> dict[str, int] | None:
    """Return only numeric card budget values for an authorized match."""
    value = row.get("context_budget")
    if not isinstance(value, dict):
        return None
    budget = {
        key: value[key]
        for key in ("max_candidates", "max_context_chars")
        if key in value and isinstance(value[key], int) and not isinstance(value[key], bool)
    }
    return budget or None


def run_preflight(
    refs: list[RootRef],
    request: str,
    model_class: str,
    catalog: Catalog | None = None,
    semantic: SemanticBackend | None = None,
) -> PreflightResult:
    """Route a request at the catalog level. Deterministic and read-only."""
    if catalog is None:
        catalog = build_catalog(refs)
    result = PreflightResult(
        request=request,
        model_class=model_class,
        status="no-match",
        catalog_hash=catalog.catalog_hash,
        request_hash=content_hash(request),
    )
    outcome = disabled_outcome()
    query_tokens = tokenize(request)
    if not query_tokens:
        result.status = "no-match"
        result.notes.append("no usable terms in request")
    for note in catalog.notes:
        result.notes.append(note)

    scored: list[tuple[_Lexical, dict[str, object]]] = []
    for row in catalog.rows:
        status = str(row.get("status", "ok"))
        if status != "ok":
            if status == "redacted":
                result.redacted_count += 1
            else:
                result.root_issues.append(
                    {"root": row.get("root"), "status": status, "error": row.get("error", "")}
                )
            continue
        if row.get("catalog_visibility") == "hidden":
            result.redacted_count += 1
            continue
        if not query_tokens:
            continue
        reason = _declined(row, query_tokens)
        if reason is not None:
            result.declined.append(
                {"name": row.get("name"), "root": row.get("root"), "reason": reason}
            )
            continue
        lexical = _score_row(row, query_tokens)
        if lexical.score > 0:
            scored.append((lexical, row))

    eligible: list[tuple[_Lexical, dict[str, object]]] = []
    for lexical, row in scored:
        # Pointer mode exposes location metadata and zero content; that is the
        # privacy-safe pointer a "none" access level is still allowed to return.
        if row.get("routing_mode") == "pointer":
            eligible.append((lexical, row))
            continue
        access = _access_level(row, model_class)
        if access == "none":
            result.filtered.append(
                {
                    "name": row.get("name"),
                    "root": row.get("root"),
                    "access": "none",
                    "reason": f"{model_class} access is none for this wiki",
                }
            )
            continue
        eligible.append((lexical, row))

    ok_rows = [row for row in catalog.rows if str(row.get("status", "ok")) == "ok"]
    if not catalog.rows:
        result.status = "unavailable"
        result.notes.append("no wiki roots supplied the catalog")
    elif not ok_rows:
        result.status = "unavailable"
        result.notes.append("no usable wiki cards: every root is broken, unreadable, or withheld")
    elif not query_tokens:
        pass
    elif not scored:
        if result.redacted_count:
            result.status = "privacy-filtered"
            result.notes.append("matching wikis are withheld from this projection")
        else:
            result.status = "no-match"
            result.notes.append("no wiki matched this request")
    elif not eligible:
        result.status = "privacy-filtered"
        result.notes.append("matching wikis are not accessible to this model class")
    else:
        eligible.sort(key=lambda item: (-item[0].score, str(item[1].get("name"))))
        confidences = [
            route_confidence(
                [lexical.signals.get(token, 0.0) for token in query_tokens], len(query_tokens)
            )
            for lexical, _row in eligible
        ]
        # The no-match floor drops evidence too weak to offer, by name only.
        paired = list(zip(eligible, confidences, strict=True))
        strong = [
            (lexical, row, confidence)
            for (lexical, row), confidence in paired
            if confidence >= OFFER_FLOOR
        ]
        too_weak = [
            str(row.get("name")) for (_lex, row), confidence in paired if confidence < OFFER_FLOOR
        ]
        if too_weak:
            result.notes.append(
                f"below the no-match floor ({OFFER_FLOOR}): omitted {bounded_names(too_weak)}"
            )

        # Thresholds, membership, and the authorized set all come from lexical
        # confidence in lexical order, before any reranking. `authorize` names
        # which rows the decision covers, so only a row that itself reached the
        # reliance floor can ever become a loadable match.
        lexical_confs = [confidence for _lex, _row, confidence in strong]
        decision, authorized = authorize(lexical_confs)
        result.confidence = max(lexical_confs) if lexical_confs else None

        match_indices: list[int] = []
        offer_indices: list[int] = []
        if not strong:
            result.status = "no-match"
            result.notes.append(
                f"all matching wikis fall below the no-match floor ({OFFER_FLOOR}): staying quiet"
            )
        elif decision == "load":
            result.status = "matched"
            match_indices = list(authorized)
            loadable = set(authorized)
            offer_indices = [index for index in range(len(strong)) if index not in loadable]
            if offer_indices:
                result.notes.append(
                    f"only wikis at or above the reliance floor ({RELIANCE_FLOOR}) are loadable "
                    "matches; the weaker ones stay offers with no loadable paths"
                )
        else:
            result.status = "ambiguous"
            if result.confidence is not None and result.confidence >= RELIANCE_FLOOR:
                offer_indices = list(authorized)
                result.notes.append(
                    f"top candidates are within the ambiguity band ({AMBIGUITY_BAND}): "
                    "offer a choice instead of loading"
                )
                banded = set(authorized)
                outside = [
                    str(strong[index][1].get("name"))
                    for index in range(len(strong))
                    if index not in banded
                ]
                if outside:
                    result.notes.append(
                        f"outside the ambiguity band ({AMBIGUITY_BAND}): omitted "
                        f"{bounded_names(outside)}"
                    )
            else:
                offer_indices = list(range(len(strong)))
                result.notes.append(
                    f"route confidence {result.confidence} is below the reliance floor "
                    f"({RELIANCE_FLOOR}): offer choices, load nothing automatically"
                )

        # Optional local semantic rerank over the already-selected packet only.
        # Filtered wikis never enter this list, so reranking can never resurrect
        # an ineligible wiki or expose its card beyond what the lexical layer
        # already scored, and it cannot move a row between matches and offers.
        shown = sorted(set(match_indices) | set(offer_indices))
        order, outcome = semantic_rerank(
            request,
            [float(strong[index][0].score) for index in shown],
            [_card_text(strong[index][1]) for index in shown],
            semantic,
        )
        semantic_scores: dict[int, float] = {}
        if outcome.status == "ok":
            semantic_scores = dict(zip(shown, outcome.scores, strict=True))
        ranked = [shown[position] for position in order]

        def _entry(index: int) -> dict[str, object]:
            lexical, row, confidence = strong[index]
            return {
                "name": row.get("name"),
                "root": row.get("root"),
                "score": lexical.score,
                "confidence": {
                    "score": confidence,
                    "meets_floor": confidence >= RELIANCE_FLOOR,
                },
                "freshness": row.get("freshness"),
                # The per-token signals stay internal to confidence; the public
                # summary keeps only their counts and classes.
                "evidence": _evidence_summary(
                    row, query_tokens, lexical, semantic_scores.get(index)
                ),
                "reasons": lexical.reasons[:5],
            }

        match_set, offer_set = set(match_indices), set(offer_indices)
        for index in ranked:
            if index not in match_set:
                continue
            row = strong[index][1]
            access = _access_level(row, model_class)
            paths = row.get("paths", {})
            assert isinstance(paths, dict)
            if row.get("routing_mode") == "pointer":
                allows: list[str] = []
            elif access == "digest-only":
                digest = str(paths.get("digest") or "")
                allows = [digest] if digest else []
            else:
                allows = [str(paths[key]) for key in ("card", "digest", "index") if paths.get(key)]
            follow_up = _follow_up(row, request, access)
            entry = _entry(index)
            entry["access"] = access
            entry["routing_mode"] = row.get("routing_mode")
            entry["allows"] = allows
            entry["follow_up"] = follow_up.text
            # A budget only bounds what a load path may consume, so it belongs
            # to an authorized entry whose follow-up actually hands one out.
            # `allows` alone cannot answer that: a registry match with no
            # declared card, digest, or index still gets the executable route
            # ladder, while pointer wikis and digest-only wikis without a
            # digest are told to load nothing at all.
            if access != "none" and follow_up.loadable:
                budget = _context_budget(row)
                if budget is not None:
                    entry["context_budget"] = budget
            result.matches.append(entry)
        for index in ranked:
            if index in offer_set:
                result.offers.append(_entry(index))
        if result.offers:
            result.notes.append("offer the listed wikis as choices; load nothing until picked")

    result.semantic = outcome.to_dict()
    # The public packet grew, but proof inputs stay stable: the card budget is
    # already covered by catalog_hash and the lexical evidence summary is a
    # deterministic function of the request hash, catalog, and model class.
    # `evidence.semantic` is the exception: it reflects the host-selected
    # semantic backend, which is not a proof input, so a rerank that changes
    # scores without changing the ranked order keeps the same preflight_id.
    proof = {
        "request_hash": result.request_hash,
        "catalog_hash": result.catalog_hash,
        "model_class": model_class,
        "status": result.status,
        "matches": [str(match.get("name")) for match in result.matches],
        "offers": [str(offer.get("name")) for offer in result.offers],
        "filtered": [str(entry.get("name")) for entry in result.filtered],
        "redacted_count": result.redacted_count,
    }
    result.preflight_id = content_hash(json.dumps(proof, sort_keys=True))
    return result
