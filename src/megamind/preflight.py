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


def _score_row(
    row: dict[str, object], query_tokens: list[str]
) -> tuple[int, list[str], dict[str, float]]:
    score = 0
    reasons: list[str] = []
    signals: dict[str, float] = {}
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
    for token in query_tokens:
        if token in trigger_tokens:
            score += WEIGHT_TRIGGER
            reasons.append(f"trigger match: {token}")
            _best_signal(signals, token, SIGNAL_STRENGTH["trigger"])
        if token in name_tokens:
            score += WEIGHT_NAME
            reasons.append(f"name match: {token}")
            _best_signal(signals, token, SIGNAL_STRENGTH["name"])
        if token in text_tokens:
            score += WEIGHT_TEXT
            reasons.append(f"scope match: {token}")
            _best_signal(signals, token, SIGNAL_STRENGTH["text"])
    return score, reasons, signals


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


def _follow_up(row: dict[str, object], access: str) -> str:
    root = str(row.get("root", ""))
    paths = row.get("paths", {})
    assert isinstance(paths, dict)
    if row.get("routing_mode") == "pointer":
        return f"Open {root} manually; pointer wikis expose location metadata only"
    if access == "digest-only":
        digest = str(paths.get("digest") or "")
        if digest:
            return f"Read only the approved digest {root}/{digest}; nothing else may be loaded"
        return f"{root} allows digest-only access but declares no digest; load nothing"
    if row.get("source") == "registry":
        # Do not echo the host request into a result or runnable command. The
        # host already retains its privacy-safe request representation and can
        # invoke the bounded ladder with that value when it chooses to proceed.
        return (
            f"Run `megamind-axi --root {root} route <the original request>` for the bounded ladder"
        )
    index = str(paths.get("index") or "wiki/index.md")
    return f"Open {root}/{index} and follow its links within the context budget"


def _evidence_summary(
    row: dict[str, object],
    query_tokens: list[str],
    reasons: list[str],
    signals: dict[str, float],
    semantic_score: float | None,
) -> dict[str, object]:
    """Return bounded card provenance without echoing request tokens.

    The lexical scorer still uses the exact same reasons and signals for
    confidence. Only the public packet changes: signal classes and numeric
    coverage explain the route without retaining query-derived words or page
    content.
    """
    classes = ("trigger", "name", "scope")
    counts = {
        signal_class: sum(1 for reason in reasons if reason.startswith(f"{signal_class} match:"))
        for signal_class in classes
    }
    return {
        "routing_class": "lexical-card",
        "coverage": {
            "matched_terms": len(signals),
            "request_terms": len(query_tokens),
            "ratio": round(len(signals) / len(query_tokens), 4) if query_tokens else 0.0,
        },
        "signal_classes": [signal_class for signal_class in classes if counts[signal_class]],
        "signal_counts": counts,
        "provenance": {
            "source": "canonical-card" if row.get("source") == "wiki-card" else "registry-card",
            "scope": "declared card metadata only",
            "page_content": False,
        },
        "lexical": [signal_class for signal_class in classes if counts[signal_class]],
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

    scored: list[tuple[int, dict[str, object], list[str], dict[str, float]]] = []
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
        score, reasons, signals = _score_row(row, query_tokens)
        if score > 0:
            scored.append((score, row, reasons, signals))

    eligible: list[tuple[int, dict[str, object], list[str], dict[str, float]]] = []
    for score, row, reasons, signals in scored:
        # Pointer mode exposes location metadata and zero content; that is the
        # privacy-safe pointer a "none" access level is still allowed to return.
        if row.get("routing_mode") == "pointer":
            eligible.append((score, row, reasons, signals))
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
        eligible.append((score, row, reasons, signals))

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
        eligible.sort(key=lambda item: (-item[0], str(item[1].get("name"))))
        confidences = [
            route_confidence([signals.get(token, 0.0) for token in query_tokens], len(query_tokens))
            for _score, _row, _reasons, signals in eligible
        ]
        # The no-match floor drops evidence too weak to offer, by name only.
        paired = list(zip(eligible, confidences, strict=True))
        strong = [
            (score, row, reasons, signals, confidence)
            for (score, row, reasons, signals), confidence in paired
            if confidence >= OFFER_FLOOR
        ]
        too_weak = [
            str(row.get("name"))
            for (_s, row, _r, _sig), confidence in paired
            if confidence < OFFER_FLOOR
        ]
        if too_weak:
            result.notes.append(
                f"below the no-match floor ({OFFER_FLOOR}): omitted {bounded_names(too_weak)}"
            )

        # Thresholds, membership, and the authorized set all come from lexical
        # confidence in lexical order, before any reranking. `authorize` names
        # which rows the decision covers, so only a row that itself reached the
        # reliance floor can ever become a loadable match.
        lexical_confs = [confidence for _s, _r, _re, _sig, confidence in strong]
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
            [float(strong[index][0]) for index in shown],
            [_card_text(strong[index][1]) for index in shown],
            semantic,
        )
        semantic_scores: dict[int, float] = {}
        if outcome.status == "ok":
            semantic_scores = dict(zip(shown, outcome.scores, strict=True))
        ranked = [shown[position] for position in order]

        def _entry(index: int) -> dict[str, object]:
            score, row, reasons, signals, confidence = strong[index]
            return {
                "name": row.get("name"),
                "root": row.get("root"),
                "score": score,
                "confidence": {
                    "score": confidence,
                    "meets_floor": confidence >= RELIANCE_FLOOR,
                },
                "freshness": row.get("freshness"),
                "evidence": _evidence_summary(
                    row,
                    query_tokens,
                    reasons,
                    # The signals are kept internal for confidence and reduced
                    # to counts/classes in the public evidence summary.
                    signals,
                    semantic_scores.get(index),
                ),
                # Keep the v2 key, but make its values safe provenance classes
                # rather than request-derived reason text.
                "reasons": [
                    signal_class
                    for signal_class in ("trigger", "name", "scope")
                    if any(reason.startswith(f"{signal_class} match:") for reason in reasons)
                ],
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
            entry = _entry(index)
            entry["access"] = access
            entry["routing_mode"] = row.get("routing_mode")
            entry["allows"] = allows
            entry["follow_up"] = _follow_up(row, access)
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
    # The public packet is additive, but proof inputs stay stable: the card
    # budget is already covered by catalog_hash and the evidence summary is a
    # deterministic function of the request hash, catalog, and model class.
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
