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
from pathlib import Path
from typing import Any

from .card import card_file, serialize_wiki_card
from .fsops import (
    MEGAMIND_DIR,
    PathEscapeError,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    remove_contained,
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
RESULT_SCHEMA = "megamind/research-result/v1"
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

    Dates reach durable records and log headings, so a non-ISO value has to
    fail typed before the write rather than becoming permanent journal state.
    """
    if not today:
        return ""
    try:
        return date.fromisoformat(today).isoformat()
    except ValueError as error:
        raise GardenError(f"dates must be ISO (YYYY-MM-DD): {today}") from error


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
        record = cls(
            gap_id=str(raw["gap_id"]),
            wiki=str(raw["wiki"]),
            topic=str(raw["topic"]),
            kind=str(raw["kind"]),
            status=str(raw["status"]),
            priority=priority,
            attempts=[dict(x) for x in attempts],
            cooldown_until=str(raw.get("cooldown_until", "")),
            rejection=dict(raw["rejection"]) if isinstance(raw.get("rejection"), dict) else None,
            reopened_from=str(raw.get("reopened_from", "")),
            superseded_by=str(raw.get("superseded_by", "")),
            related_topics=[str(x) for x in raw.get("related_topics", [])],
            created=str(raw.get("created", "")),
            updated=str(raw.get("updated", "")),
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
        record = self.get(gap_id)
        previous_status = record.status
        if status == record.status:
            # An exact repeat is a no-op: it appends no snapshot, keeps the
            # recorded date, and never rewrites a terminal rejection or
            # supersession. Anything that would change the record is not an
            # exact repeat, so it refuses instead of overwriting silently.
            conflicts: list[str] = []
            if cooldown_until is not None and cooldown_until != record.cooldown_until:
                conflicts.append("cooldown_until")
            if reason and _short(reason) != (record.rejection or {}).get("reason", ""):
                conflicts.append("reason")
            if superseded_by and superseded_by != record.superseded_by:
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
        if cooldown_until is not None:
            record.cooldown_until = cooldown_until
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
        if cooldown_until is not None:
            record.cooldown_until = cooldown_until
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
    eligible_sources: list[dict[str, str]]
    ineligible_sources: list[dict[str, str]]
    ingest_proposal: str
    reason: str = ""

    def to_data(self) -> dict[str, Any]:
        return {"schema": RESULT_SCHEMA, **asdict(self)}


def ingest_research_result(
    root: Path, nomination: Nomination, result: Mapping[str, Any]
) -> ResearchResult:
    if not isinstance(result, Mapping):
        raise GardenError("research result must be a JSON object")
    if result.get("correlation_id") != nomination.correlation_id:
        raise GardenError("research result correlation_id does not match nomination")
    sources = result.get("sources", [])
    if not isinstance(sources, list) or len(sources) > MAX_RESULT_SOURCES:
        raise GardenError(f"research result sources must be a list of at most {MAX_RESULT_SOURCES}")
    eligible: list[dict[str, str]] = []
    ineligible: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise GardenError("each research source must be an object")
        origin = source.get("origin", "")
        # An origin is an opaque host-supplied fact, including any URL: Megamind
        # records it and never resolves, fetches, or validates it against a
        # network. It must still be a real identifier so a proposal can cite it.
        if not isinstance(origin, str) or not origin.strip():
            raise GardenError("each research source must declare a non-empty origin string")
        item = {
            "origin": _short(origin, 300),
            "summary": _short(str(source.get("summary", "")), 500),
        }
        (eligible if source.get("eligible") is True else ineligible).append(item)
    if not eligible:
        # Nothing citable: refuse before the proposal, audit, and log writes.
        # Correlation is the idempotency key, so a proposal written here could
        # never be repaired by the replay that finally carries a real source.
        return ResearchResult(
            nomination.correlation_id, "rejected", [], ineligible, "", "no eligible sources"
        )
    # Correlation is the idempotency key. A replay cannot create a second
    # proposal merely because ineligible data was redacted or reordered.
    result_id = content_hash(nomination.correlation_id)
    proposal_rel = Path(MEGAMIND_DIR) / "proposals" / f"research-ingest-{result_id}.json"
    proposal = {
        "schema": "megamind/ingest-proposal/v1",
        "proposal_id": result_id,
        "correlation_id": nomination.correlation_id,
        "wiki": nomination.wiki,
        "topic": nomination.topic,
        "immutable_raw_required": True,
        "sources": eligible,
    }
    path = resolve_contained(root, proposal_rel)
    if path.exists():
        # The returned document points at this file, so it may never claim a
        # citation the file does not carry: divergence refuses, it never wins.
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GardenError(f"existing ingest proposal is unreadable: {error}") from error
        if not isinstance(stored, Mapping) or stored.get("sources") != eligible:
            raise GardenError(
                f"correlation {nomination.correlation_id} was already ingested with different "
                "eligible sources; nominate a new correlation instead of rewriting the proposal"
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
            "research result accepted as an immutable-source proposal",
            pages=[],
            sources=[x["origin"] for x in eligible],
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
        return {
            "schema": "megamind/provisional-wiki-plan/v1",
            "plan_id": self.plan_id,
            "wiki": self.name,
            "path": self.path,
            "files": [change["path"] for change in self.changes],
            "changes": list(self.changes),
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
PROVISION_STATES = ("pending", "applied", "rolled_back")


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


def _undo(root: Path, manifest: Mapping[str, Any], written: list[str]) -> None:
    backups = manifest["backups"]
    for rel in reversed(written):
        restore = backups.get(rel)
        if isinstance(restore, str):
            atomic_write(root, rel, restore)
        else:
            remove_contained(root, rel)
    remove_contained(root, str(manifest["plan"]["path"]))
    remove_contained(root, _manifest_rel(str(manifest["plan"]["plan_id"])))


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
    log_rel = _log_rel(manifest)
    changes = _manifest_changes(manifest)
    for rel, old, new in changes:
        current = _read_text(root, rel)
        if current == new or (rel == log_rel and _is_logged(current, new)):
            continue
        if current is not None and current != old:
            raise GardenError(
                f"provisional wiki apply cannot resume: {rel} changed outside the transaction; "
                "roll back with `--rollback --plan-id` and re-plan"
            )
        atomic_write(root, rel, new)
    return _commit(root, manifest, [rel for rel, _old, _new in changes])


def _resume_or_verify(
    root: Path, manifest: dict[str, Any], name: str, path: str
) -> tuple[str, list[str]]:
    plan = manifest["plan"]
    if plan["wiki"] != name or plan["path"] != path:
        raise GardenError(
            f"provisional wiki plan {plan['plan_id']} was recorded for {plan['wiki']} at "
            f"{plan['path']}, not {name} at {path}; re-run the dry run for these arguments"
        )
    if manifest["state"] == "applied":
        expected = {rel: new for rel, _old, new in _manifest_changes(manifest)}
        for rel in manifest["created"]:
            if _read_text(root, rel) != expected.get(rel):
                raise GardenError(
                    f"provisional wiki apply is not idempotent: generated file changed: {rel}"
                )
        return "noop", []
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
        "backups": backups,
        "backup_files": backup_files,
    }
    _write_manifest(root, manifest)
    written: list[str] = []
    try:
        for rel, _old, new in resolved_changes:
            atomic_write(root, rel, new)
            written.append(rel)
        return _commit(root, manifest, written)
    except BaseException:
        _undo(root, manifest, written)
        raise


def rollback_provision(root: Path, plan_id: str) -> list[str]:
    """Undo an applied or interrupted transaction, refusing on tampered content."""
    manifest = _read_manifest(root, plan_id)
    if manifest is None:
        raise GardenError(f"provisional wiki rollback manifest not found: {plan_id}")
    if manifest["state"] == "rolled_back":
        raise GardenError(f"provisional wiki plan was already rolled back: {plan_id}")
    pending = manifest["state"] == "pending"
    log_rel = _log_rel(manifest)
    changes = _manifest_changes(manifest)
    expected = {rel: new for rel, _old, new in changes}
    previous = {rel: old for rel, old, _new in changes}
    created = [str(rel) for rel in manifest["created"]]
    for rel in created:
        current = _read_text(root, rel)
        if current is None or current == expected.get(rel):
            continue
        # A pending transaction may not have reached this target yet, and may
        # already have appended its evaluation event to the wiki log.
        untouched = current == previous.get(rel)
        logged = rel == log_rel and _is_logged(current, expected.get(rel, ""))
        if pending and (untouched or logged):
            continue
        raise GardenError(f"rollback refused: generated file changed: {rel}")
    removed: list[str] = []
    backups = manifest["backups"]
    for rel in reversed(created):
        restore = backups.get(rel)
        if isinstance(restore, str):
            atomic_write(root, rel, restore)
        else:
            remove_contained(root, rel)
        removed.append(rel)
    remove_contained(root, str(manifest["plan"]["path"]))
    manifest["state"] = "rolled_back"
    manifest["created"] = []
    _write_manifest(root, manifest)
    append_audit(root, "provisional-wiki-rollback", {"plan_id": plan_id, "removed": removed})
    return removed


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
