"""Phase 3 governed autonomous gardening primitives.

This module is deliberately a host bridge, not a worker runtime.  It stores
small, typed, replayable facts and emits plans.  It never starts a worker,
reads the network, or chooses a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from .card import card_file, serialize_wiki_card
from .confidence import CLEAN_CORRECTION, CORRECTION_STATUSES, UNKNOWN_ORIGIN_IDS
from .fsops import (
    MEGAMIND_DIR,
    PathEscapeError,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    remove_contained,
    remove_empty_directory,
    resolve_contained,
)
from .registry import (
    CURRENT_VERSION,
    REGISTRY_PATH,
    ROUTER_FILENAME,
    ROUTER_HEADER,
    ModelAccess,
    Registry,
    RegistryError,
    SourcePolicy,
    WikiEntry,
    generate_router,
    load_registry,
    registry_file,
    serialize_registry,
)

GAP_SCHEMA = "megamind/gap/v1"
NOMINATION_SCHEMA = "megamind/research-nomination/v1"
RESULT_SCHEMA_V1 = "megamind/research-result/v1"
RESULT_SCHEMA_V2 = "megamind/research-result/v2"
# The current result document is selected per input: v1 remains readable as a
# restrictive legacy nomination, while only v2 can mint an ingest proposal.
RESULT_SCHEMA = RESULT_SCHEMA_V2
INGEST_PROPOSAL_SCHEMA_V2 = "megamind/ingest-proposal/v2"
WAVE_SCHEMA = "megamind/research-wave/v1"
GAP_KINDS = ("missing", "weak", "stale", "contradictory")
GAP_STATUSES = (
    "open",
    "nominated",
    "planned",
    "in_progress",
    "paused",
    "resolved",
    "rejected",
    "superseded",
)
EVENT_TYPES = (
    "preflight",
    "query",
    "ingest",
    "research-wave",
    "gap-transition",
    "compiled-page-mutation",
    "lint",
    "confidence-check",
    "validation",
    "evaluation",
    "rollback",
)
MAX_RESULT_SOURCES = 20
MAX_TEXT = 1000
_ACCEPTANCE_FIELDS = {"origin_id", "retrieval", "publication", "snapshot", "rights", "corrections"}
_SOURCE_V1_FIELDS = {"origin", "summary", "eligible"}
_SOURCE_V2_FIELDS = {"origin", "summary", "acceptance"}
_RESULT_FIELDS = {"schema", "correlation_id", "sources"}
_DATE_PRECISIONS = {"exact", "month", "year"}
_QUOTE_POLICIES = {"quote-free", "quote-bounded", "no-quote"}
_SNAPSHOT_POLICIES = {"local-snapshot-allowed", "no-store"}
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class GardenError(ValueError):
    code = "garden_invalid"


class GapNotFound(GardenError):
    code = "gap_not_found"


class InvalidTransition(GardenError):
    code = "gap_transition_invalid"


def _stable(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _norm(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


# Both patterns are anchored to a token boundary on the left. Redaction runs
# over origins too, and an origin must survive as a citable identifier, so a
# credential keyword buried inside a word ("capital: raised") and a local-path
# prefix buried inside a URL ("https://host/home/page") must not match.
_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|pk|api|token|secret|password|credential)[_-]?\w*\s*[:=]\s*\S+",
    re.I,
)
_LOCAL_PATH_RE = re.compile(r"(?<![\w.\-~%@])(?:/Users|/home|[A-Za-z]:\\)[^\s]+")


def _short(text: str, limit: int = MAX_TEXT) -> str:
    """Make a log/bridge summary safe without attempting to retain content."""
    text = _CREDENTIAL_RE.sub("[redacted]", text)
    text = _LOCAL_PATH_RE.sub("[path]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _date(today: str | None) -> str:
    """Normalize a host-supplied date, refusing anything that is not ISO.

    This is the single owner of date validity for gap records. Every date that
    reaches a durable record or a log heading passes through it, so a malformed
    value fails typed before the write instead of becoming permanent state, and
    two spellings of one day can never read as two different values.
    """
    if not today:
        return ""
    try:
        return date.fromisoformat(today).isoformat()
    except ValueError as error:
        raise GardenError(f"dates must be ISO (YYYY-MM-DD): {today}") from error


def _require_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    """Refuse unknown keys by naming the allowed set, never the offending key.

    These refusals become a typed ``reason`` on the returned document, so they
    are built from this module's own constants: a host-supplied key name is
    data, and data never travels back out as message text.
    """
    if not set(value) <= allowed:
        raise GardenError(
            f"{label} contains unknown field(s); allowed: {', '.join(sorted(allowed))}"
        )


def _date_fact(value: object, label: str, *, exact_only: bool = False) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise GardenError(f"{label} must be an object with date and precision")
    _require_fields(value, {"date", "precision"}, label)
    raw_date = value.get("date")
    precision = value.get("precision")
    if not isinstance(raw_date, str) or not isinstance(precision, str):
        raise GardenError(f"{label} date and precision must be strings")
    if not isinstance(precision, str) or precision not in _DATE_PRECISIONS:
        raise GardenError(f"{label} precision must be one of {', '.join(sorted(_DATE_PRECISIONS))}")
    if precision == "unknown" or (exact_only and precision != "exact"):
        raise GardenError(f"{label} precision is not sufficiently certain")
    formats = {"exact": r"^\d{4}-\d{2}-\d{2}$", "month": r"^\d{4}-\d{2}$", "year": r"^\d{4}$"}
    if not re.fullmatch(formats[precision], raw_date):
        raise GardenError(f"{label} date does not match precision {precision}")
    try:
        if precision == "exact":
            date.fromisoformat(raw_date)
        elif precision == "month":
            date.fromisoformat(raw_date + "-01")
        else:
            date.fromisoformat(raw_date + "-01-01")
    except ValueError as error:
        raise GardenError(f"{label} date is not valid ISO") from error
    return {"date": raw_date, "precision": precision}


def _earliest_day(fact: Mapping[str, str]) -> date:
    """The first instant a validated date fact can denote.

    A coarse precision is an interval, not a point, so ordering two facts is
    only honest against the earliest day each interval can start on.
    """
    raw_date = fact["date"]
    suffix = {"exact": "", "month": "-01", "year": "-01-01"}[fact["precision"]]
    return date.fromisoformat(raw_date + suffix)


def _fact_text(value: object, label: str, limit: int) -> str:
    """Bound and redact a host string before it can reach a durable record.

    Acceptance facts are written into the proposal file and the returned
    document, so they cross the same projection boundary as origins and
    summaries: over-long input fails typed rather than being silently cut, and
    a credential assignment or local path never survives into the vault.
    """
    if not isinstance(value, str) or not value.strip():
        raise GardenError(f"{label} must be a non-empty string")
    if len(value) > limit:
        raise GardenError(f"{label} must be at most {limit} characters")
    safe = _short(value, limit)
    if not safe:
        raise GardenError(f"{label} must be a non-empty string")
    return safe


def _validate_acceptance(source: Mapping[str, Any]) -> dict[str, Any]:
    """Validate host facts; source prose never participates in these gates."""
    acceptance = source.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise GardenError("research-result/v2 source acceptance must be an object")
    _require_fields(acceptance, _ACCEPTANCE_FIELDS, "acceptance")
    origin_id = _fact_text(acceptance.get("origin_id"), "acceptance origin_id", 300)
    if origin_id.casefold() in UNKNOWN_ORIGIN_IDS:
        raise GardenError("acceptance origin_id independence is unknown")
    retrieval = _date_fact(acceptance.get("retrieval"), "acceptance retrieval", exact_only=True)
    publication = _date_fact(acceptance.get("publication"), "acceptance publication")
    # Evidence cannot be retrieved before the earliest day it could exist.
    if _earliest_day(publication) > _earliest_day(retrieval):
        raise GardenError("acceptance dates are contradictory: publication is after retrieval")

    snapshot = acceptance.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise GardenError("acceptance snapshot must be an object")
    _require_fields(snapshot, {"sha256", "normalized_sha256"}, "acceptance snapshot")
    hashes: dict[str, str] = {}
    for key in ("sha256", "normalized_sha256"):
        value = snapshot.get(key)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise GardenError(f"acceptance snapshot {key} must be a SHA-256 digest")
        hashes[key] = value.lower()

    rights = acceptance.get("rights")
    if not isinstance(rights, Mapping):
        raise GardenError("acceptance rights must be an object")
    _require_fields(rights, {"license", "quote_policy", "snapshot_policy"}, "acceptance rights")
    license_name = _fact_text(rights.get("license"), "acceptance rights license", 300)
    quote_policy = rights.get("quote_policy")
    snapshot_policy = rights.get("snapshot_policy")
    if not isinstance(quote_policy, str) or quote_policy not in _QUOTE_POLICIES:
        raise GardenError(
            f"acceptance rights quote_policy must be one of {', '.join(sorted(_QUOTE_POLICIES))}"
        )
    if not isinstance(snapshot_policy, str) or snapshot_policy not in _SNAPSHOT_POLICIES:
        raise GardenError(
            "acceptance rights snapshot_policy must be one of "
            f"{', '.join(sorted(_SNAPSHOT_POLICIES))}"
        )

    corrections = acceptance.get("corrections")
    if not isinstance(corrections, Mapping):
        raise GardenError("acceptance corrections must be an object")
    _require_fields(
        corrections, {"status", "checked_at", "method", "notice_ids"}, "acceptance corrections"
    )
    correction_status = corrections.get("status")
    notice_ids = corrections.get("notice_ids")
    if not isinstance(correction_status, str) or correction_status not in CORRECTION_STATUSES:
        raise GardenError(
            f"acceptance corrections status must be one of {', '.join(CORRECTION_STATUSES)}"
        )
    method = _fact_text(corrections.get("method"), "acceptance corrections method", 300)
    if not isinstance(notice_ids, list) or any(not isinstance(item, str) for item in notice_ids):
        raise GardenError("acceptance corrections notice_ids must be a list of strings")
    checked_at = _date_fact(
        corrections.get("checked_at"), "acceptance corrections checked_at", exact_only=True
    )
    if correction_status == CLEAN_CORRECTION and notice_ids:
        raise GardenError("acceptance corrections are contradictory: clean has notice_ids")
    if correction_status != CLEAN_CORRECTION:
        raise GardenError(f"source correction status is not clean: {correction_status}")

    return {
        "origin_id": origin_id,
        "retrieval": retrieval,
        "publication": publication,
        "snapshot": hashes,
        "rights": {
            "license": license_name,
            "quote_policy": quote_policy,
            "snapshot_policy": snapshot_policy,
        },
        "corrections": {
            "status": correction_status,
            "checked_at": checked_at,
            "method": method,
            "notice_ids": sorted(_short(item, 200) for item in notice_ids),
        },
    }


@dataclass(frozen=True)
class PriorityInputs:
    impact: int = 0
    urgency: int = 0
    repeat_demand: int = 0
    coverage: int = 0
    confidence_risk: int = 0

    def score(self) -> int:
        values = (
            self.impact,
            self.urgency,
            self.repeat_demand,
            self.coverage,
            self.confidence_risk,
        )
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 5 for v in values):
            raise GardenError("priority inputs must be integers from 0 through 5")
        return (
            self.impact * 3
            + self.urgency * 3
            + self.repeat_demand * 2
            + self.coverage
            + self.confidence_risk
        )


@dataclass
class GapRecord:
    gap_id: str
    wiki: str
    topic: str
    kind: str
    status: str = "open"
    priority: PriorityInputs = field(default_factory=PriorityInputs)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    cooldown_until: str = ""
    rejection: dict[str, str] | None = None
    reopened_from: str = ""
    superseded_by: str = ""
    related_topics: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    identity: str = ""

    @staticmethod
    def identity_for(wiki: str, topic: str, kind: str) -> str:
        return content_hash(_stable({"wiki": _norm(wiki), "topic": _norm(topic), "kind": kind}))

    @classmethod
    def new(
        cls,
        wiki: str,
        topic: str,
        kind: str = "missing",
        *,
        priority: PriorityInputs | None = None,
        related_topics: Iterable[str] = (),
        today: str | None = None,
    ) -> GapRecord:
        if not _norm(wiki) or not _norm(topic) or kind not in GAP_KINDS:
            raise GardenError("wiki, topic, and a valid gap kind are required")
        identity = cls.identity_for(wiki, topic, kind)
        return cls(
            identity,
            wiki,
            topic,
            kind,
            priority=priority or PriorityInputs(),
            related_topics=sorted({_norm(x) for x in related_topics if _norm(x)}),
            created=_date(today),
            updated=_date(today),
            identity=identity,
        )

    def to_data(self) -> dict[str, Any]:
        self.priority.score()
        return {
            "schema": GAP_SCHEMA,
            **asdict(self),
            "priority": {**asdict(self.priority), "score": self.priority.score()},
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> GapRecord:
        if not isinstance(raw, Mapping):
            raise GardenError("gap record must be a JSON object")
        if raw.get("schema") != GAP_SCHEMA:
            raise GardenError("gap record schema must be megamind/gap/v1")
        allowed = {
            "schema",
            "gap_id",
            "wiki",
            "topic",
            "kind",
            "status",
            "priority",
            "attempts",
            "cooldown_until",
            "rejection",
            "reopened_from",
            "superseded_by",
            "related_topics",
            "created",
            "updated",
            "identity",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise GardenError(f"gap record has unknown field(s): {', '.join(sorted(unknown))}")
        for key in ("gap_id", "wiki", "topic", "kind", "status"):
            if not isinstance(raw.get(key), str):
                raise GardenError(f"gap {key} must be a string")
        if raw["kind"] not in GAP_KINDS or raw["status"] not in GAP_STATUSES:
            raise GardenError("gap kind or lifecycle status is invalid")
        p = raw.get("priority", {})
        if not isinstance(p, Mapping):
            raise GardenError("gap priority must be an object")
        priority = PriorityInputs(
            **{
                k: p.get(k, 0)
                for k in ("impact", "urgency", "repeat_demand", "coverage", "confidence_risk")
            }
        )
        attempts = raw.get("attempts", [])
        if not isinstance(attempts, list) or any(not isinstance(x, dict) for x in attempts):
            raise GardenError("gap attempts must be a list of objects")
        # Replay revalidates every date the record carries through the same
        # owner the write path uses, so a hand-edited journal fails typed at
        # read rather than surviving until the next mutation touches it.
        replayed_attempts = [dict(x) for x in attempts]
        for entry in replayed_attempts:
            if "date" in entry:
                entry["date"] = _date(str(entry["date"]))
        rejection = dict(raw["rejection"]) if isinstance(raw.get("rejection"), dict) else None
        if rejection is not None and "date" in rejection:
            rejection["date"] = _date(str(rejection["date"]))
        record = cls(
            gap_id=str(raw["gap_id"]),
            wiki=str(raw["wiki"]),
            topic=str(raw["topic"]),
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            priority=priority,
            attempts=replayed_attempts,
            cooldown_until=_date(str(raw.get("cooldown_until", ""))),
            rejection=rejection,
            reopened_from=str(raw.get("reopened_from", "")),
            superseded_by=str(raw.get("superseded_by", "")),
            related_topics=[str(x) for x in raw.get("related_topics", [])],
            created=_date(str(raw.get("created", ""))),
            updated=_date(str(raw.get("updated", ""))),
            identity=str(raw.get("identity", raw["gap_id"])),
        )
        if (
            record.identity != record.identity_for(record.wiki, record.topic, record.kind)
            or record.gap_id != record.identity
        ):
            raise GardenError("gap identity does not match its semantic identity")
        record.priority.score()
        return record


_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("nominated", "planned", "rejected", "resolved"),
    "nominated": ("planned", "open", "rejected", "paused"),
    "planned": ("in_progress", "paused", "open"),
    "in_progress": ("resolved", "paused", "rejected", "open", "superseded"),
    "paused": ("planned", "open", "rejected"),
    "rejected": ("open", "superseded"),
    "resolved": ("open", "superseded"),
    "superseded": (),
}


class GapStore:
    """Append-only snapshot journal; latest snapshot wins and replay is idempotent."""

    def __init__(self, root: Path):
        self.root = root
        self.path = resolve_contained(root, Path(MEGAMIND_DIR) / "gaps.jsonl")

    def records(self) -> list[GapRecord]:
        if not self.path.is_file():
            return []
        latest: dict[str, GapRecord] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = GapRecord.from_data(json.loads(line))
            except (json.JSONDecodeError, GardenError, TypeError) as error:
                raise GardenError(f"invalid gap journal entry: {error}") from error
            latest[record.gap_id] = record
        return [latest[key] for key in sorted(latest)]

    def get(self, gap_id: str) -> GapRecord:
        for record in self.records():
            if record.gap_id == gap_id:
                return record
        raise GapNotFound(f"gap not found: {gap_id}")

    def _append(self, record: GapRecord, event: str, details: dict[str, str]) -> GapRecord:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_data(), sort_keys=True, separators=(",", ":")) + "\n"
        old = self.path.read_text(encoding="utf-8") if self.path.is_file() else ""
        # Atomic replacement prevents a crash from leaving a half JSON line.
        atomic_write(self.root, Path(MEGAMIND_DIR) / "gaps.jsonl", old + line)
        append_audit(
            self.root,
            "gap-transition",
            # The caller's detail keys stay flat beside the identity fields, so
            # the audit record says which status a transition moved to and which
            # attempt an attempt recorded.
            {
                "gap_id": record.gap_id,
                "event": event,
                "audit_ref": f"gaps.jsonl:{record.gap_id}",
                **details,
            },
        )
        append_log_event(
            self.root,
            "gap-transition",
            _date(record.updated),
            f"gap {event}",
            pages=[],
            sources=[],
            confidence="unknown",
            outcome=record.status,
            audit=f"gaps.jsonl:{record.gap_id}",
        )
        return record

    def create(self, record: GapRecord) -> GapRecord:
        existing = next((x for x in self.records() if x.identity == record.identity), None)
        if existing:
            return existing
        return self._append(record, "created", {"gap_id": record.gap_id})

    def transition(
        self,
        gap_id: str,
        status: str,
        *,
        today: str | None = None,
        reason: str = "",
        cooldown_until: str | None = None,
        superseded_by: str = "",
    ) -> GapRecord:
        if status not in GAP_STATUSES:
            raise InvalidTransition(f"unknown gap status: {status}")
        cooldown = None if cooldown_until is None else _date(cooldown_until)
        record = self.get(gap_id)
        previous_status = record.status
        if status == record.status:
            # An exact repeat is a no-op: it appends no snapshot, keeps the
            # recorded date, and never rewrites a terminal rejection or
            # supersession. Anything that would change the record is not an
            # exact repeat, so it refuses instead of overwriting silently.
            # Only a field this status actually persists can conflict: a reason
            # is stored on a rejection and `superseded_by` on a supersession, so
            # passing either to any other status changes nothing and its replay
            # has to stay a true no-op rather than refusing on a phantom edit.
            conflicts: list[str] = []
            if cooldown is not None and cooldown != record.cooldown_until:
                conflicts.append("cooldown_until")
            if (
                status == "rejected"
                and reason
                and _short(reason) != (record.rejection or {}).get("reason", "")
            ):
                conflicts.append("reason")
            if status == "superseded" and superseded_by and superseded_by != record.superseded_by:
                conflicts.append("superseded_by")
            if conflicts:
                raise InvalidTransition(
                    f"gap {gap_id} is already {status}: repeating a transition is idempotent "
                    f"and cannot change {', '.join(conflicts)}"
                )
            return record
        if status not in _ALLOWED_TRANSITIONS[record.status]:
            raise InvalidTransition(
                f"cannot transition gap {gap_id} from {record.status} to {status}"
            )
        record.status = status
        record.updated = _date(today) or record.updated
        # An omitted cooldown keeps the recorded backoff; only an explicit value
        # (including an explicit empty string) may clear it.
        if cooldown is not None:
            record.cooldown_until = cooldown
        if status == "rejected":
            record.rejection = {"reason": _short(reason), "date": record.updated}
        if status == "open" and previous_status != "open":
            record.reopened_from = previous_status
            record.rejection = None
        if status == "superseded":
            if not superseded_by:
                raise InvalidTransition("superseded gaps require superseded_by")
            record.superseded_by = superseded_by
        return self._append(record, "transition", {"status": status})

    def attempt(
        self,
        gap_id: str,
        outcome: str,
        *,
        correlation_id: str = "",
        today: str | None = None,
        cooldown_until: str | None = None,
    ) -> GapRecord:
        cooldown = None if cooldown_until is None else _date(cooldown_until)
        record = self.get(gap_id)
        attempt_id = content_hash(
            _stable(
                {
                    "gap": gap_id,
                    "n": len(record.attempts) + 1,
                    "correlation": correlation_id,
                    "outcome": outcome,
                }
            )
        )
        record.attempts.append(
            {
                "attempt_id": attempt_id,
                "outcome": _short(outcome, 200),
                "correlation_id": correlation_id,
                "date": _date(today),
            }
        )
        if cooldown is not None:
            record.cooldown_until = cooldown
        record.updated = _date(today) or record.updated
        return self._append(record, "attempt", {"attempt_id": attempt_id})


@dataclass(frozen=True)
class CapacityInput:
    known: bool
    active_workers: int
    active_wikis: Mapping[str, int]
    applicable_quota: int | float | None
    reserve_quota: int | float | None
    wave_units: int = 1
    available_units: int | None = None
    captain_work: bool = False

    def refusal(self, target_wiki: str = "") -> str | None:
        if not self.known:
            return "capacity_unknown"
        if self.active_workers < 0 or any(v < 0 for v in self.active_wikis.values()):
            return "capacity_invalid"
        if self.active_workers >= 3:
            return "fleet_worker_limit"
        if target_wiki and self.active_wikis.get(target_wiki, 0) >= 1:
            return "wiki_worker_limit"
        if self.applicable_quota is None or self.reserve_quota is None:
            return "quota_unknown"
        if self.applicable_quota <= 0 or self.reserve_quota / self.applicable_quota < 0.25:
            return "quota_reserve_below_25_percent"
        if self.wave_units <= 0 or (
            self.available_units is not None and self.available_units < self.wave_units
        ):
            return "capacity_cannot_last_through_wave"
        if self.captain_work:
            return "active_captain_work_priority"
        return None


@dataclass(frozen=True)
class ResearchWave:
    wave_id: str
    status: str
    reason: str
    direct_gap: str
    nominations: list[dict[str, Any]]
    deferred_nominations: list[dict[str, Any]]
    capacity: dict[str, Any]

    def to_data(self) -> dict[str, Any]:
        return {"schema": WAVE_SCHEMA, **asdict(self)}


def plan_research_wave(
    gaps: Iterable[GapRecord],
    direct_gap_id: str,
    capacity: CapacityInput,
    *,
    today: str | None = None,
) -> ResearchWave:
    by_id = {g.gap_id: g for g in gaps}
    if direct_gap_id not in by_id:
        raise GapNotFound(f"gap not found: {direct_gap_id}")
    direct = by_id[direct_gap_id]
    refusal = capacity.refusal(direct.wiki)
    wave_id = content_hash(
        _stable({"gap": direct_gap_id, "today": today or "", "capacity": asdict(capacity)})
    )
    base = {
        "gap_id": direct.gap_id,
        "wiki": direct.wiki,
        "topic": direct.topic,
        "relationship": "direct",
        "correlation_id": content_hash(f"{wave_id}:direct:{direct.gap_id}"),
    }
    related = []
    for topic in sorted(set(direct.related_topics)):
        candidate = next(
            (
                g
                for g in by_id.values()
                if g.wiki == direct.wiki
                and _norm(g.topic) == _norm(topic)
                and g.gap_id != direct.gap_id
            ),
            None,
        )
        related.append(
            {
                "gap_id": (
                    candidate.gap_id
                    if candidate
                    else GapRecord.identity_for(direct.wiki, topic, "missing")
                ),
                "wiki": direct.wiki,
                "topic": topic,
                "relationship": "first-order",
                "correlation_id": content_hash(f"{wave_id}:related:{_norm(topic)}"),
            }
        )
    if refusal:
        return ResearchWave(
            wave_id,
            "paused"
            if refusal
            in {
                "capacity_unknown",
                "quota_unknown",
                "capacity_cannot_last_through_wave",
                "active_captain_work_priority",
            }
            else "refused",
            refusal,
            direct_gap_id,
            [],
            [base, *related],
            asdict(capacity),
        )
    nominations = [base, *related[:2]]  # direct plus bounded first-order topics only
    deferred = related[2:]
    return ResearchWave(
        wave_id, "planned", "", direct_gap_id, nominations, deferred, asdict(capacity)
    )


@dataclass(frozen=True)
class Nomination:
    correlation_id: str
    wave_id: str
    gap_id: str
    wiki: str
    topic: str
    relationship: str
    source_policy: str
    payload_budget: int = MAX_RESULT_SOURCES

    def to_data(self) -> dict[str, Any]:
        return {"schema": NOMINATION_SCHEMA, **asdict(self)}


def make_nomination(wave_id: str, entry: Mapping[str, Any], source_policy: str = "") -> Nomination:
    if not isinstance(entry, Mapping):
        raise GardenError("research nomination must be a JSON object")
    required = ("correlation_id", "gap_id", "wiki", "topic", "relationship")
    if any(not isinstance(entry.get(k), str) or not entry.get(k) for k in required):
        raise GardenError("research nomination is missing a required identity field")
    if entry["relationship"] not in {"direct", "first-order"}:
        raise GardenError("research waves are one-hop; deeper relationships are nominations only")
    return Nomination(
        str(entry["correlation_id"]),
        str(wave_id),
        str(entry["gap_id"]),
        str(entry["wiki"]),
        str(entry["topic"]),
        str(entry["relationship"]),
        _short(source_policy, 300),
    )


@dataclass(frozen=True)
class ResearchResult:
    correlation_id: str
    status: str
    eligible_sources: list[dict[str, Any]]
    ineligible_sources: list[dict[str, Any]]
    ingest_proposal: str
    reason: str = ""
    schema: str = RESULT_SCHEMA_V1

    def to_data(self) -> dict[str, Any]:
        return {"schema": self.schema, **asdict(self)}


def ingest_research_result(
    root: Path, nomination: Nomination, result: Mapping[str, Any]
) -> ResearchResult:
    if not isinstance(result, Mapping):
        raise GardenError("research result must be a JSON object")
    _require_fields(result, _RESULT_FIELDS, "research result")
    schema = result.get("schema", RESULT_SCHEMA_V1)
    if not isinstance(schema, str) or schema not in {RESULT_SCHEMA_V1, RESULT_SCHEMA_V2}:
        raise GardenError(
            f"research result schema must be {RESULT_SCHEMA_V1} or {RESULT_SCHEMA_V2}"
        )
    if result.get("correlation_id") != nomination.correlation_id:
        raise GardenError("research result correlation_id does not match nomination")
    sources = result.get("sources", [])
    if not isinstance(sources, list) or len(sources) > MAX_RESULT_SOURCES:
        raise GardenError(f"research result sources must be a list of at most {MAX_RESULT_SOURCES}")
    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    source_fields = _SOURCE_V2_FIELDS if schema == RESULT_SCHEMA_V2 else _SOURCE_V1_FIELDS
    for source in sources:
        if not isinstance(source, Mapping):
            raise GardenError("each research source must be an object")
        _require_fields(source, source_fields, "research source")
        origin = source.get("origin", "")
        # Origins remain opaque display facts. They are never fetched or
        # interpreted as instructions; v2's origin_id is the separate typed
        # independence identity used by confidence arithmetic.
        if not isinstance(origin, str) or not origin.strip():
            raise GardenError("each research source must declare a non-empty origin string")
        summary = source.get("summary", "")
        if not isinstance(summary, str):
            raise GardenError("research source summary must be a string")
        base: dict[str, Any] = {
            "origin": _short(origin, 300),
            "summary": _short(summary, 500),
        }
        if schema == RESULT_SCHEMA_V1:
            # v1 is deliberately readable, but its caller label is only a
            # legacy nomination. It has no evidence, quality, rights, or
            # autonomous-apply authority and therefore cannot mint a proposal.
            ineligible.append({**base, "reason": "legacy_v1_requires_v2_acceptance"})
            continue
        try:
            acceptance = _validate_acceptance(source)
        except GardenError as error:
            ineligible.append({**base, "reason": _short(f"acceptance_invalid: {error}", 300)})
            continue
        accepted = {**base, "origin_id": acceptance["origin_id"], "acceptance": acceptance}
        eligible.append(accepted)
    if not eligible:
        return ResearchResult(
            nomination.correlation_id,
            "rejected",
            [],
            ineligible,
            "",
            "legacy_v1_requires_v2_acceptance"
            if schema == RESULT_SCHEMA_V1
            else "no eligible sources",
            schema,
        )
    result_id = content_hash(nomination.correlation_id)
    proposal_rel = Path(MEGAMIND_DIR) / "proposals" / f"research-ingest-{result_id}.json"
    proposal = {
        "schema": INGEST_PROPOSAL_SCHEMA_V2,
        "proposal_id": result_id,
        "correlation_id": nomination.correlation_id,
        "wiki": nomination.wiki,
        "topic": nomination.topic,
        "immutable_raw_required": True,
        "sources": eligible,
    }
    path = resolve_contained(root, proposal_rel)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GardenError(f"existing ingest proposal is unreadable: {error}") from error
        if not isinstance(stored, Mapping):
            raise GardenError("existing ingest proposal is not a JSON object")
        diverged = sorted(
            set(stored).symmetric_difference(proposal)
            | {key for key in proposal if key in stored and stored[key] != proposal[key]}
        )
        if diverged:
            raise GardenError(
                f"correlation {nomination.correlation_id} was already ingested with different "
                f"{', '.join(diverged)}; nominate a new correlation instead of rewriting the "
                "proposal"
            )
    else:
        atomic_write(root, proposal_rel, json.dumps(proposal, sort_keys=True, indent=2) + "\n")
        append_audit(
            root,
            "research-ingest-proposal",
            {
                "proposal_id": result_id,
                "correlation_id": nomination.correlation_id,
                "path": proposal_rel.as_posix(),
            },
        )
        append_log_event(
            root,
            "ingest",
            "",
            "research result accepted as a v2 immutable-source proposal",
            pages=[],
            sources=[str(x["origin"]) for x in eligible],
            confidence="unknown",
            outcome="proposed",
            audit=f"research-ingest-{result_id}",
        )
    return ResearchResult(
        nomination.correlation_id,
        "proposed",
        eligible,
        ineligible,
        proposal_rel.as_posix(),
        "",
        schema,
    )


def append_log_event(
    root: Path,
    event_type: str,
    event_date: str,
    summary: str,
    *,
    pages: Iterable[str],
    sources: Iterable[str],
    confidence: str,
    outcome: str,
    audit: str,
) -> str:
    if event_type not in EVENT_TYPES:
        raise GardenError(f"unknown log event type: {event_type}")
    safe_pages = [_short(str(x), 200) for x in list(pages)[:20]]
    safe_sources = [_short(str(x), 200) for x in list(sources)[:20]]
    payload = {
        "date": event_date,
        "type": event_type,
        "summary": _short(summary, 300),
        "pages": safe_pages,
        "sources": safe_sources,
        "confidence": _short(confidence, 40),
        "outcome": _short(outcome, 100),
        "audit": _short(audit, 200),
    }
    event_id = content_hash(_stable(payload))
    rel = Path("wiki") / "log.md"
    path = resolve_contained(root, rel)
    if not path.is_file():
        return event_id
    old = path.read_text(encoding="utf-8")
    marker = f"<!-- megamind:event:{event_id} -->"
    if marker in old:
        return event_id
    block = (
        "\n"
        + marker
        + "\n"
        + f"## {event_date or 'undated'} {event_type}\n"
        + "".join(
            f"- {key}: {json.dumps(value, sort_keys=True)}\n"
            for key, value in payload.items()
            if key != "type"
        )
    )
    atomic_write(root, rel, old.rstrip("\n") + block)
    return event_id


@dataclass(frozen=True)
class ProvisionCriteria:
    accepted_domain: str
    repeat_demand: int
    multi_topic: int
    overlap: str
    scope: str
    exclusions: str
    owner: str
    source_policy: str
    privacy: str
    model_access: str
    seed_topics: tuple[str, ...]
    maintenance: str

    def validate(self) -> None:
        if (
            not self.accepted_domain.strip()
            or self.repeat_demand < 2
            or self.multi_topic < 2
            or not self.overlap.strip()
            or not self.scope.strip()
            or not self.exclusions.strip()
            or not self.owner.strip()
            or not self.source_policy.strip()
            or self.privacy not in {"personal-local", "company-private", "public-reference"}
            or self.model_access not in {"local", "full", "digest-only", "none"}
            or len([x for x in self.seed_topics if x.strip()]) < 2
            or not self.maintenance.strip()
        ):
            raise GardenError("provisional wiki criteria are not all satisfied")


@dataclass(frozen=True)
class ProvisionPlan:
    plan_id: str
    name: str
    path: str
    changes: tuple[dict[str, str | None], ...]
    notes: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        # Every nested value is copied, not aliased: the manifest is enriched
        # with the bytes that actually landed, and the reviewed plan must keep
        # hashing to the plan_id that approved it.
        return {
            "schema": "megamind/provisional-wiki-plan/v1",
            "plan_id": self.plan_id,
            "wiki": self.name,
            "path": self.path,
            "files": [change["path"] for change in self.changes],
            "changes": [dict(change) for change in self.changes],
            "notes": list(self.notes),
        }


def _provision_entry(name: str, path: str, criteria: ProvisionCriteria) -> WikiEntry:
    return WikiEntry(
        name=name,
        path=path,
        privacy=criteria.privacy,
        description=criteria.scope,
        keywords=[_norm(x) for x in criteria.seed_topics],
        card=f"{path}/CARD.md",
        index=f"{path}/INDEX.md",
        purpose=criteria.accepted_domain,
        scope_boundaries=f"{criteria.scope}; exclusions: {criteria.exclusions}",
        owners=[criteria.owner],
        model_access=ModelAccess(
            local="full" if criteria.model_access in {"local", "full"} else criteria.model_access,
            cloud="none",
        ),
        source_policy=SourcePolicy(summary=criteria.source_policy),
        provisional=True,
    )


def plan_provision_wiki(
    root: Path, name: str, path: str, criteria: ProvisionCriteria, *, today: str | None = None
) -> ProvisionPlan:
    """Build a side-effect-free, content-bound provisional-wiki plan."""
    criteria.validate()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise GardenError("wiki name must be a simple local identifier")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or not path.strip():
        raise RegistryError("provisional wiki path must be root-relative and must not contain '..'")
    if card_file(root).is_file() and not registry_file(root).is_file():
        raise GardenError(
            "this root is a canonical single-wiki root, not a registry vault; provisioning "
            "a registry here would leave two cards claiming authority"
        )
    registry = load_registry(root)
    if registry.version < CURRENT_VERSION:
        raise GardenError(
            "registry is schema v1; run `megamind-axi migrate` first so every existing wiki "
            "gets its explicit access policy before a provisional wiki is registered"
        )
    if registry.wiki_by_name(name) is not None:
        raise GardenError(f"wiki already exists: {name}")
    wiki_dir = resolve_contained(root, path)
    if wiki_dir.exists():
        raise GardenError(f"wiki path already exists: {path}")
    entry = _provision_entry(name, path, criteria)
    new_registry = Registry(
        version=registry.version, budgets=registry.budgets, wikis=[*registry.wikis, entry]
    )
    registry_text = serialize_registry(new_registry)
    card_text = (
        "---\nmegamind: routing-card\n"
        f"wiki: {name}\nprivacy: {criteria.privacy}\n"
        f"keywords: [{', '.join(entry.keywords)}]\n---\n\n"
        f"# {name} routing card\n\nAnswers: {criteria.scope}\n"
        f"Does not answer: {criteria.exclusions}\n"
    )
    files: dict[str, str] = {
        f"{path}/CARD.md": card_text,
        f"{path}/INDEX.md": f"---\nmegamind: index\nwiki: {name}\n---\n\n# {name} index\n\n",
        f"{path}/wiki/index.md": f"# {name} compiled index\n",
        f"{path}/wiki/log.md": f"# {name} log\n\n",
        f"{path}/{MEGAMIND_DIR}/wiki-card.json": serialize_wiki_card(
            replace(entry, path=".", card="CARD.md", index="INDEX.md")
        ),
        f"{path}/{MEGAMIND_DIR}/gaps.jsonl": "",
        REGISTRY_PATH.as_posix(): registry_text,
    }
    router_path = resolve_contained(root, ROUTER_FILENAME)
    router_old = router_path.read_text(encoding="utf-8") if router_path.is_file() else None
    notes: list[str] = []
    if router_old is None or ROUTER_HEADER in router_old:
        files[ROUTER_FILENAME] = generate_router(new_registry)
    else:
        notes.append("ROUTER.md is hand-edited and is left unchanged")
    changes = tuple(
        {
            "path": rel,
            "old": (
                registry_file(root).read_text(encoding="utf-8")
                if rel == REGISTRY_PATH.as_posix()
                else router_old
                if rel == ROUTER_FILENAME
                else None
            ),
            "new": text,
        }
        for rel, text in sorted(files.items())
    )
    plan_id = content_hash(
        _stable({"name": name, "path": path, "today": today or "", "changes": changes})
    )
    return ProvisionPlan(plan_id, name, path, changes, tuple(notes))


PROVISION_MANIFEST_SCHEMA = "megamind/provisional-wiki-rollback/v1"
# `partial` is the state an in-process undo leaves behind when it found content
# it was not allowed to delete: the transaction is undone but its record must
# survive, because the wiki directory did too and only that record explains why.
PROVISION_STATES = ("pending", "applied", "partial", "rolled_back")
MAX_PRESERVED = 20


def _bounded_preserved(entries: list[str]) -> tuple[list[str], int]:
    """A bounded sample plus the real total, so no reader mistakes one for the other."""
    return entries[:MAX_PRESERVED], len(entries)


@dataclass(frozen=True)
class RollbackOutcome:
    """What a recovery actually did, so a partial undo is never read as a full one.

    ``wiki`` and ``path`` are the identity the manifest recorded, not the
    spelling the caller used, so a response can never agree with a host that
    believed it was undoing something else.
    """

    status: str  # "rolled_back" | "partial"
    wiki: str
    path: str
    removed: list[str]
    preserved: list[str]
    preserved_total: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UndoOutcome:
    """What the in-process undo did, so its caller can say recovery is still owed."""

    status: str  # "undone" | "recovery_required"
    plan_id: str
    preserved: list[str]
    preserved_total: int = 0


class ProvisionRecoveryRequired(GardenError):
    """An apply failed and its undo kept content it did not write.

    The write-ahead record survives so the transaction can be finished or rolled
    back explicitly; the failure that caused it stays as the exception cause.
    """

    code = "provision_recovery_required"

    def __init__(self, message: str, outcome: UndoOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome


def _manifest_rel(plan_id: str) -> Path:
    return Path(MEGAMIND_DIR) / "audit" / f"provisional-wiki-{plan_id}.json"


def _read_text(root: Path, rel: str) -> str | None:
    """Read a contained file, treating only absence as a missing value."""
    path = resolve_contained(root, rel)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GardenError(f"provisional wiki target is unreadable: {rel}: {error}") from error


def _read_manifest(root: Path, plan_id: str) -> dict[str, Any] | None:
    """Load the write-ahead transaction record, or None when there is none."""
    raw = _read_text(root, _manifest_rel(plan_id).as_posix())
    if raw is None:
        return None
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise GardenError(
            f"provisional wiki transaction manifest is unreadable: {error}"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("schema") != PROVISION_MANIFEST_SCHEMA:
        raise GardenError("provisional wiki transaction manifest is invalid")
    plan = manifest.get("plan")
    created = manifest.get("created")
    created_dirs = manifest.get("created_dirs", [])
    backups = manifest.get("backups")
    if (
        manifest.get("state") not in PROVISION_STATES
        or not isinstance(plan, dict)
        or not isinstance(plan.get("plan_id"), str)
        or not isinstance(plan.get("wiki"), str)
        or not isinstance(plan.get("path"), str)
        or not isinstance(plan.get("changes"), list)
        or not isinstance(created, list)
        or not all(isinstance(item, str) for item in created)
        or not isinstance(created_dirs, list)
        or not all(isinstance(item, str) for item in created_dirs)
        or not isinstance(backups, dict)
        or not all(isinstance(value, str) for value in backups.values())
    ):
        raise GardenError("provisional wiki transaction manifest is malformed")
    return manifest


def _manifest_changes(manifest: Mapping[str, Any]) -> list[tuple[str, str | None, str]]:
    changes: list[tuple[str, str | None, str]] = []
    for change in manifest["plan"]["changes"]:
        if not isinstance(change, Mapping):
            raise GardenError("provisional wiki transaction manifest is malformed")
        rel, old, new = change.get("path"), change.get("old"), change.get("new")
        if (
            not isinstance(rel, str)
            or not isinstance(new, str)
            or (old is not None and not isinstance(old, str))
        ):
            raise GardenError("provisional wiki transaction manifest is malformed")
        changes.append((rel, old, new))
    return changes


def _write_manifest(root: Path, manifest: Mapping[str, Any]) -> None:
    """Persist and flush the transaction record before anything depends on it."""
    atomic_write(
        root,
        _manifest_rel(str(manifest["plan"]["plan_id"])),
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        durable=True,
    )


def _log_rel(manifest: Mapping[str, Any]) -> str:
    return f"{manifest['plan']['path']}/wiki/log.md"


def _is_logged(current: str | None, expected: str) -> bool:
    """The wiki log is append-only, so a pending apply may already have added
    its evaluation event on top of the planned text."""
    return current is not None and current.startswith(expected.rstrip("\n"))


TARGET_DONE = "done"
TARGET_INCOMPLETE = "incomplete"
TARGET_FOREIGN = "foreign"


def _target_state(current: str | None, old: str | None, new: str, appendable: bool) -> str:
    """Classify one target against the transaction record.

    ``incomplete`` is the state a target is in when the transaction has not
    materialized it yet: absent, or still carrying the content the plan
    recorded as its prior version. It is the same fact whether the manifest
    says ``pending`` or ``applied``, because a rename that never reached disk
    leaves exactly this trace, so both recovery paths read it the same way.
    ``foreign`` is content the transaction neither wrote nor replaced.
    """
    if current == new or (appendable and _is_logged(current, new)):
        return TARGET_DONE
    if current is None or current == old:
        return TARGET_INCOMPLETE
    return TARGET_FOREIGN


def _is_regular_file(root: Path, rel: str) -> bool:
    """Whether a target is still the plain file the transaction wrote."""
    path = resolve_contained(root, rel)
    return not path.exists() or path.is_file()


def _target_states(root: Path, manifest: Mapping[str, Any]) -> dict[str, str]:
    log_rel = _log_rel(manifest)
    states: dict[str, str] = {}
    for rel, old, new in _manifest_changes(manifest):
        # A directory standing where a target belongs reads as absent through
        # the text reader, which would let a removal treat it as a file. It is
        # content the transaction did not write, so it is foreign like any other.
        if not _is_regular_file(root, rel):
            states[rel] = TARGET_FOREIGN
            continue
        states[rel] = _target_state(_read_text(root, rel), old, new, rel == log_rel)
    return states


def _remove_target(root: Path, rel: str) -> bool:
    """Remove one tracked target, never a directory that took its place."""
    if not _is_regular_file(root, rel):
        return False
    remove_contained(root, rel, durable=True)
    return True


def _wiki_dirs(rels: Iterable[str], base: str) -> list[str]:
    """Every directory at or under the wiki path that a target needs, deepest first.

    Root-relative and normalized, so the same directory named two ways ("a/b"
    and "./a/b/") is one entry. Directories outside the wiki path are excluded:
    the transaction may have created them, but it cannot prove it did.
    """
    base_path = PurePosixPath(base)
    dirs: set[PurePosixPath] = {base_path}
    for rel in rels:
        for parent in PurePosixPath(rel).parents:
            if parent == base_path or base_path in parent.parents:
                dirs.add(parent)
    return [d.as_posix() for d in sorted(dirs, key=lambda d: (-len(d.parts), d.as_posix()))]


def _transaction_dirs(manifest: Mapping[str, Any]) -> list[str]:
    """Directories the transaction is known to have created, deepest first.

    ``created_dirs`` is recorded before the first mutation, so it proves which
    directories were absent when the transaction started. A record written
    without it falls back to the directories its own targets required, which is
    the same set for every plan this module produces.
    """
    base = str(manifest["plan"]["path"])
    known = _wiki_dirs((rel for rel, _old, _new in _manifest_changes(manifest)), base)
    recorded = manifest.get("created_dirs")
    if recorded is None:
        return known
    proven = {str(rel) for rel in recorded}
    return [rel for rel in known if rel in proven]


def _prune_transaction_dirs(root: Path, manifest: Mapping[str, Any]) -> list[str]:
    """Remove transaction-created directories that are empty; keep the rest.

    Cleanup is never recursive. A directory that still holds anything the
    transaction did not itself create is left exactly as it is, and the foreign
    entries are returned so the recovery can report what it preserved instead of
    silently destroying content that was authored after the apply.
    """
    created = _transaction_dirs(manifest)
    owned = set(created)
    preserved: set[str] = set()
    for rel in created:
        if remove_empty_directory(root, rel, durable=True):
            continue
        try:
            path = resolve_contained(root, rel)
        except PathEscapeError:
            continue
        if not path.is_dir() or path.is_symlink():
            continue
        preserved.update(
            entry
            for entry in (f"{rel}/{child.name}" for child in path.iterdir())
            if entry not in owned
        )
    return sorted(preserved)


def _is_reverted(root: Path, manifest: Mapping[str, Any], written: Iterable[str]) -> bool:
    """Whether every target the undo touched carries its recorded prior value again."""
    backups = manifest["backups"]
    for rel in written:
        expected = backups.get(rel)
        try:
            current = _read_text(root, rel)
        except (GardenError, PathEscapeError, OSError, UnicodeDecodeError):
            return False
        if current != (expected if isinstance(expected, str) else None):
            return False
    return True


def _undo(root: Path, manifest: dict[str, Any], written: list[str]) -> UndoOutcome:
    """Undo a failed apply, keeping the record whenever the undo was not complete.

    The manifest is the only durable explanation for anything the undo had to
    leave behind, so it is removed only after a verified complete undo. When
    foreign content survived, the record is marked ``partial``, the preserved
    sample and its total are audited, and the caller is told recovery is owed.
    """
    plan_id = str(manifest["plan"]["plan_id"])
    backups = manifest["backups"]
    for rel in reversed(written):
        restore = backups.get(rel)
        if isinstance(restore, str):
            atomic_write(root, rel, restore, durable=True)
        else:
            _remove_target(root, rel)
    found = _prune_transaction_dirs(root, manifest)
    preserved, total = _bounded_preserved(found)
    if not found and _is_reverted(root, manifest, written):
        remove_contained(root, _manifest_rel(plan_id), durable=True)
        return UndoOutcome("undone", plan_id, [], 0)
    manifest["state"] = "partial"
    _write_manifest(root, manifest)
    append_audit(
        root,
        "provisional-wiki-undo",
        {
            "plan_id": plan_id,
            "state": "partial",
            "preserved": preserved,
            "preserved_total": total,
        },
    )
    return UndoOutcome("recovery_required", plan_id, preserved, total)


def _commit(root: Path, manifest: dict[str, Any], created: list[str]) -> list[str]:
    """Close the transaction: log the event, record the real bytes, audit it."""
    plan = manifest["plan"]
    plan_id = str(plan["plan_id"])
    append_log_event(
        resolve_contained(root, str(plan["path"])),
        "evaluation",
        "",
        "qualified provisional local wiki created",
        pages=[],
        sources=[],
        confidence="unknown",
        outcome="provisional",
        audit=_manifest_rel(plan_id).as_posix(),
    )
    log_rel = _log_rel(manifest)
    log_text = _read_text(root, log_rel)
    for change in plan["changes"]:
        if isinstance(change, dict) and change.get("path") == log_rel and log_text is not None:
            change["new"] = log_text
    if log_text is not None:
        # The event append is the last mutation of a target, so the log crosses
        # the same durability boundary as every other file before the record
        # that commits it can claim they all landed.
        atomic_write(root, log_rel, log_text, durable=True)
    incomplete = [
        rel for rel, state in _target_states(root, manifest).items() if state != TARGET_DONE
    ]
    if incomplete:
        raise GardenError(
            f"provisional wiki apply did not land every target: {', '.join(sorted(incomplete))}"
        )
    manifest["state"] = "applied"
    manifest["created"] = created
    _write_manifest(root, manifest)
    backup_files = manifest.get("backup_files", {})
    registry_backup = backup_files.get(REGISTRY_PATH.as_posix())
    append_audit(
        root,
        "provisional-wiki-create",
        {
            "wiki": str(plan["wiki"]),
            "path": str(plan["path"]),
            "plan_id": plan_id,
            "provisional": True,
            "registry_backup": registry_backup if isinstance(registry_backup, str) else None,
            "manifest": _manifest_rel(plan_id).as_posix(),
        },
    )
    return created


def _resume(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Finish an apply that a signal or power loss interrupted mid-transaction.

    The manifest carries the whole plan, so recovery never needs a second plan
    and never trusts anything the transaction did not itself write.
    """
    changes = _manifest_changes(manifest)
    states = _target_states(root, manifest)
    foreign = sorted(rel for rel, state in states.items() if state == TARGET_FOREIGN)
    if foreign:
        raise GardenError(
            f"provisional wiki apply cannot resume: {foreign[0]} changed outside the "
            "transaction; roll back with `--rollback --plan-id` and re-plan"
        )
    for rel, _old, new in changes:
        if states[rel] != TARGET_DONE:
            atomic_write(root, rel, new, durable=True)
    return _commit(root, manifest, [rel for rel, _old, _new in changes])


def _same_target(root: Path, recorded: str, supplied: str) -> bool:
    """Whether two spellings name the same contained path.

    Both sides are resolved against the root, so `./Wiki` and `Wiki` are one
    target while a traversal or a symlink that leaves the root raises
    ``PathEscapeError`` instead of quietly matching or quietly missing.
    """
    return resolve_contained(root, recorded) == resolve_contained(root, supplied)


def _require_recorded_identity(
    root: Path, manifest: Mapping[str, Any], name: str, path: str
) -> Mapping[str, Any]:
    """Refuse before any read of backups or any mutation unless this is that plan.

    A plan id alone names a transaction, so a pasted id from another wiki would
    otherwise undo that wiki under this one's name. Identity is the whole triple
    the plan was recorded with, and every recovery path checks it the same way
    and before it touches anything.
    """
    plan: Mapping[str, Any] = manifest["plan"]
    if plan["wiki"] != name or not _same_target(root, str(plan["path"]), path):
        raise GardenError(
            f"provisional wiki plan {plan['plan_id']} was recorded for {plan['wiki']} at "
            f"{plan['path']}, not {name} at {path}; name the wiki this plan created"
        )
    return plan


def _resume_or_verify(
    root: Path, manifest: dict[str, Any], name: str, path: str
) -> tuple[str, list[str]]:
    _require_recorded_identity(root, manifest, name, path)
    if manifest["state"] == "applied":
        states = _target_states(root, manifest)
        foreign = sorted(rel for rel, state in states.items() if state == TARGET_FOREIGN)
        if foreign:
            raise GardenError(
                f"provisional wiki apply is not idempotent: generated file changed: {foreign[0]}"
            )
        if all(state == TARGET_DONE for state in states.values()):
            return "noop", []
        # An applied marker over targets that did not all land is an incomplete
        # commit, not a success, so it finishes the transaction instead of
        # reporting a no-op over content that is not there.
        return "applied", _resume(root, manifest)
    return "applied", _resume(root, manifest)


def resume_provision(
    root: Path, plan_id: str, name: str, path: str
) -> tuple[str, list[str]] | None:
    """Replay or recover an apply from its transaction record alone.

    Returns None when there is nothing to resume, so a caller falls through to
    planning. A record that exists but was written for other arguments refuses
    rather than applying something the reviewer did not approve.
    """
    manifest = _read_manifest(root, plan_id)
    if manifest is None or manifest["state"] == "rolled_back":
        return None
    if manifest["plan"]["plan_id"] != plan_id:
        raise GardenError(f"provisional wiki transaction manifest is not for plan {plan_id}")
    return _resume_or_verify(root, manifest, name, path)


def apply_provision_plan(root: Path, plan: ProvisionPlan, approved_plan_id: str) -> list[str]:
    """Apply exactly a reviewed plan as a write-ahead transaction.

    The manifest and every required backup are persisted and flushed before the
    first target mutation, so an interrupted apply is always recoverable: a
    later apply resumes it and `rollback_provision` undoes it completely.
    """
    if approved_plan_id != plan.plan_id:
        raise GardenError(
            f"provisional wiki plan id mismatch: expected {plan.plan_id}; re-run the dry run"
        )
    existing = _read_manifest(root, plan.plan_id)
    if existing is not None and existing["state"] != "rolled_back":
        return _resume_or_verify(root, existing, plan.name, plan.path)[1]
    resolved_changes: list[tuple[str, str | None, str]] = []
    for change in plan.changes:
        raw_rel, raw_old, raw_new = change["path"], change["old"], change["new"]
        if (
            not isinstance(raw_rel, str)
            or (raw_old is not None and not isinstance(raw_old, str))
            or not isinstance(raw_new, str)
        ):
            raise GardenError("provisional wiki plan contains invalid change data")
        resolved_changes.append((raw_rel, raw_old, raw_new))
    for rel, old, _new in resolved_changes:
        if _read_text(root, rel) != old:
            raise GardenError(f"provisional wiki plan is stale or tampered: {rel}")
    backups: dict[str, str] = {}
    backup_files: dict[str, str] = {}
    for rel, old, _new in resolved_changes:
        if old is not None:
            backups[rel] = old
            backup = backup_existing(root, rel, durable=True)
            if backup is not None:
                backup_files[rel] = backup.name
    manifest: dict[str, Any] = {
        "schema": PROVISION_MANIFEST_SCHEMA,
        "state": "pending",
        "plan": plan.to_data(),
        # The intended set, so a rollback after a crash covers every target the
        # transaction was still allowed to touch, written or not.
        "created": [rel for rel, _old, _new in resolved_changes],
        # Recorded before the first mutation, so recovery can prove which
        # directories this transaction created and may therefore prune. A
        # directory that already existed is never a transaction artifact.
        "created_dirs": [
            rel
            for rel in _wiki_dirs((rel for rel, _old, _new in resolved_changes), plan.path)
            if not resolve_contained(root, rel).exists()
        ],
        "backups": backups,
        "backup_files": backup_files,
    }
    _write_manifest(root, manifest)
    written: list[str] = []
    try:
        for rel, _old, new in resolved_changes:
            atomic_write(root, rel, new, durable=True)
            written.append(rel)
        return _commit(root, manifest, written)
    except BaseException as error:
        outcome = _undo(root, manifest, written)
        # A signal is not an operational failure to retype: it keeps propagating
        # as itself, and the record the undo kept is what recovery reads.
        if outcome.status == "recovery_required" and isinstance(error, Exception):
            raise ProvisionRecoveryRequired(
                f"provisional wiki apply failed and its undo kept {outcome.preserved_total} "
                f"path(s) under {plan.path} that it did not write; the transaction record "
                f"survives, so re-run `--apply --plan-id {outcome.plan_id}` to finish it or "
                f"`--rollback --plan-id {outcome.plan_id}` to undo it",
                outcome,
            ) from error
        raise


def rollback_provision(root: Path, plan_id: str, name: str, path: str) -> RollbackOutcome:
    """Undo an applied or interrupted transaction, refusing on tampered content.

    The wiki this undoes is the one the manifest recorded, so the caller has to
    name it: a plan id pasted from another transaction refuses instead of
    quietly undoing that wiki under this name. Identity is checked before any
    backup is read, any file changes, any audit is appended, and the record is
    removed, so every refusal leaves the vault untouched.

    Only the exact files the manifest tracks are restored or removed, and only
    directories the transaction created and left empty are pruned. Content
    authored inside the provisional wiki after the apply is never the
    transaction's to delete: it survives, and the outcome says so.
    """
    manifest = _read_manifest(root, plan_id)
    if manifest is None:
        raise GardenError(f"provisional wiki rollback manifest not found: {plan_id}")
    if manifest["plan"]["plan_id"] != plan_id:
        raise GardenError(f"provisional wiki transaction manifest is not for plan {plan_id}")
    plan = _require_recorded_identity(root, manifest, name, path)
    if manifest["state"] == "rolled_back":
        raise GardenError(f"provisional wiki plan was already rolled back: {plan_id}")
    # A target the transaction never materialized is undone whatever the record
    # says, so a commit that did not fully reach disk rolls back rather than
    # wedging between a `noop` it cannot honour and a refusal it does not earn.
    states = _target_states(root, manifest)
    created = [str(rel) for rel in manifest["created"]]
    for rel in created:
        if states.get(rel, TARGET_FOREIGN) == TARGET_FOREIGN:
            raise GardenError(f"rollback refused: generated file changed: {rel}")
    removed: list[str] = []
    backups = manifest["backups"]
    for rel in reversed(created):
        restore = backups.get(rel)
        if isinstance(restore, str):
            atomic_write(root, rel, restore, durable=True)
        elif not _remove_target(root, rel):
            continue
        removed.append(rel)
    found = _prune_transaction_dirs(root, manifest)
    preserved, preserved_total = _bounded_preserved(found)
    notes: list[str] = []
    if preserved_total > len(preserved):
        notes.append(f"preserved entries truncated to {len(preserved)} of {preserved_total}")
    manifest["state"] = "rolled_back"
    manifest["created"] = []
    _write_manifest(root, manifest)
    # The audit record is the only durable account of what survived, so the
    # total travels with the bounded sample rather than only in the response.
    append_audit(
        root,
        "provisional-wiki-rollback",
        {
            "plan_id": plan_id,
            "wiki": str(plan["wiki"]),
            "path": str(plan["path"]),
            "removed": removed,
            "preserved": preserved,
            "preserved_total": preserved_total,
        },
    )
    return RollbackOutcome(
        status="partial" if found else "rolled_back",
        wiki=str(plan["wiki"]),
        path=str(plan["path"]),
        removed=removed,
        preserved=preserved,
        preserved_total=preserved_total,
        notes=notes,
    )


def provision_local_wiki(
    root: Path, name: str, path: str, criteria: ProvisionCriteria, *, today: str | None = None
) -> list[str]:
    """Backward-compatible API: explicitly plans then applies its own plan."""
    plan = plan_provision_wiki(root, name, path, criteria, today=today)
    return apply_provision_plan(root, plan, plan.plan_id)


def validate_gap_journal(root: Path) -> list[str]:
    """Report why the journal is unusable instead of raising.

    Doctor calls this while building a report, so an escaping symlink or an
    unreadable file has to come back as a finding. Raising here would replace
    the whole report with a single error document, defeating the symlink check
    that runs beside it.
    """
    try:
        GapStore(root).records()
    except (GardenError, PathEscapeError, OSError, UnicodeDecodeError) as error:
        return [str(error)]
    return []
