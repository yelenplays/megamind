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
    decide,
    route_confidence,
)
from .fsops import content_hash
from .routing import tokenize
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


def _follow_up(row: dict[str, object], request: str, access: str) -> str:
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
        # The request is a host-supplied prompt representation: shell-quote it so
        # the emitted follow-up stays exactly one runnable command.
        return (
            f"Run `megamind-axi --root {shlex.quote(root)} route {shlex.quote(request)}` "
            "for the bounded ladder"
        )
    index = str(paths.get("index") or "wiki/index.md")
    return f"Open {root}/{index} and follow its links within the context budget"


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
        for (_s, row, _r, _sig), confidence in paired:
            if confidence < OFFER_FLOOR:
                result.notes.append(
                    f"below the no-match floor ({OFFER_FLOOR}): omitted {row.get('name')}"
                )

        # Optional local semantic rerank over already-authorized rows only.
        # Filtered wikis never enter this list, so reranking can never
        # resurrect an ineligible wiki or expose its card beyond what the
        # lexical layer already scored.
        keys = [str(row.get("name")) for _s, row, _r, _sig, _c in strong]
        order, outcome = semantic_rerank(
            request,
            keys,
            [float(score) for score, _r, _re, _si, _c in strong],
            [_card_text(row) for _s, row, _r, _sig, _c in strong],
            semantic,
        )
        strong = [strong[index] for index in order]
        semantic_scores = outcome.scores if outcome.status == "ok" else {}

        # Thresholds decide on lexical confidence in lexical order; the
        # semantic pass may reorder what is shown but never recomputes these.
        lexical_confs = [
            confidence
            for _s, _r, _re, _sig, confidence in sorted(
                strong, key=lambda item: (-item[0], str(item[1].get("name")))
            )
        ]
        decision, offer_count = decide(lexical_confs)
        result.confidence = lexical_confs[0] if lexical_confs else None

        def _entry(
            score: int,
            row: dict[str, object],
            reasons: list[str],
            confidence: float,
        ) -> dict[str, object]:
            return {
                "name": row.get("name"),
                "root": row.get("root"),
                "score": score,
                "confidence": {
                    "score": confidence,
                    "meets_floor": confidence >= RELIANCE_FLOOR,
                },
                "freshness": row.get("freshness"),
                "evidence": {
                    "lexical": reasons[:5],
                    "semantic": semantic_scores.get(str(row.get("name"))),
                },
                "reasons": reasons[:5],
            }

        if not strong:
            result.status = "no-match"
            result.notes.append(
                f"all matching wikis fall below the no-match floor ({OFFER_FLOOR}): staying quiet"
            )
        elif decision == "load":
            result.status = "matched"
            for score, row, reasons, _signals, confidence in strong:
                access = _access_level(row, model_class)
                paths = row.get("paths", {})
                assert isinstance(paths, dict)
                if row.get("routing_mode") == "pointer":
                    allows: list[str] = []
                elif access == "digest-only":
                    digest = str(paths.get("digest") or "")
                    allows = [digest] if digest else []
                else:
                    allows = [
                        str(paths[key]) for key in ("card", "digest", "index") if paths.get(key)
                    ]
                entry = _entry(score, row, reasons, confidence)
                entry["access"] = access
                entry["routing_mode"] = row.get("routing_mode")
                entry["allows"] = allows
                entry["follow_up"] = _follow_up(row, request, access)
                result.matches.append(entry)
        else:
            result.status = "ambiguous"
            if decision == "offer" and (lexical_confs and lexical_confs[0] >= RELIANCE_FLOOR):
                shown = strong[:offer_count]
                result.notes.append(
                    f"top candidates are within the ambiguity band ({AMBIGUITY_BAND}): "
                    "offer a choice instead of loading"
                )
            else:
                shown = strong
                result.notes.append(
                    f"route confidence {lexical_confs[0]} is below the reliance floor "
                    f"({RELIANCE_FLOOR}): offer choices, load nothing automatically"
                )
            for score, row, reasons, _signals, confidence in shown:
                result.offers.append(_entry(score, row, reasons, confidence))
            result.notes.append("offer the listed wikis as choices; load nothing until picked")

    result.semantic = outcome.to_dict()
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
