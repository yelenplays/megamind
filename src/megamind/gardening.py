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
from pathlib import Path
from typing import Any

from .card import card_file, serialize_wiki_card
from .fsops import (
    MEGAMIND_DIR,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    resolve_contained,
)
from .registry import (
    CURRENT_VERSION,
    REGISTRY_PATH,
    ROUTER_FILENAME,
    ROUTER_HEADER,
    ModelAccess,
    Registry,
    SourcePolicy,
    WikiEntry,
    generate_router,
    load_registry,
    registry_file,
    save_registry,
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


def _short(text: str, limit: int = MAX_TEXT) -> str:
    """Make a log/bridge summary safe without attempting to retain content."""
    text = re.sub(
        r"(?:sk|pk|api|token|secret|password|credential)[_-]?\w*\s*[:=]\s*\S+",
        "[redacted]",
        text,
        flags=re.I,
    )
    text = re.sub(r"(?:/Users|/home|[A-Za-z]:\\)[^\s]+", "[path]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _date(today: str | None) -> str:
    return today or ""


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
        if status not in _ALLOWED_TRANSITIONS[record.status] and status != record.status:
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


def make_nomination(
    wave: ResearchWave, entry: Mapping[str, Any], source_policy: str = ""
) -> Nomination:
    if not isinstance(entry, Mapping):
        raise GardenError("research nomination must be a JSON object")
    required = ("correlation_id", "gap_id", "wiki", "topic", "relationship")
    if any(not isinstance(entry.get(k), str) or not entry.get(k) for k in required):
        raise GardenError("research nomination is missing a required identity field")
    if entry["relationship"] not in {"direct", "first-order"}:
        raise GardenError("research waves are one-hop; deeper relationships are nominations only")
    return Nomination(
        str(entry["correlation_id"]),
        wave.wave_id,
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
    if not path.exists():
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
        "proposed" if eligible else "rejected",
        eligible,
        ineligible,
        proposal_rel.as_posix() if eligible else "",
        "" if eligible else "no eligible sources",
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


def provision_local_wiki(
    root: Path, name: str, path: str, criteria: ProvisionCriteria, *, today: str | None = None
) -> list[str]:
    criteria.validate()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise GardenError("wiki name must be a simple local identifier")
    # A root is one shape or the other, never both: adding a registry beside an
    # authoritative canonical card would leave discovery guessing which card
    # wins. Init and adopt refuse the same way.
    if card_file(root).is_file() and not registry_file(root).is_file():
        raise GardenError(
            "this root is a canonical single-wiki root, not a registry vault; provisioning "
            "a registry here would leave two cards claiming authority. Provision the new "
            "wiki in a registry vault instead"
        )
    # Never bootstrap a vault as a side effect, and never migrate one either:
    # a v1 registry has to be upgraded explicitly so its operator notes are seen.
    registry = load_registry(root)
    if registry.version < CURRENT_VERSION:
        raise GardenError(
            "registry is schema v1; run `megamind-axi migrate` first so every existing wiki "
            "gets its explicit access policy before a provisional wiki is registered"
        )
    if registry.wiki_by_name(name) is not None:
        raise GardenError(f"wiki already exists: {name}")
    entry = WikiEntry(
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
    new_registry = Registry(
        version=registry.version,
        budgets=registry.budgets,
        wikis=[*registry.wikis, entry],
    )
    # The complete registry plan is validated before a single byte is written,
    # so a rejected entry can never leave an orphan scaffold behind.
    serialize_registry(new_registry)
    wiki_dir = resolve_contained(root, path)
    if wiki_dir.exists():
        raise GardenError(f"wiki path already exists: {path}")
    wiki_dir.mkdir(parents=True)
    for directory in ("raw", "wiki", f"{MEGAMIND_DIR}/proposals", f"{MEGAMIND_DIR}/audit"):
        resolve_contained(root, Path(path) / directory).mkdir(parents=True, exist_ok=True)
    card_text = (
        "---\n"
        "megamind: routing-card\n"
        f"wiki: {name}\nprivacy: {criteria.privacy}\n"
        f"keywords: [{', '.join(entry.keywords)}]\n"
        "---\n\n"
        f"# {name} routing card\n\nAnswers: {criteria.scope}\n"
        f"Does not answer: {criteria.exclusions}\n"
    )
    files = {
        f"{path}/CARD.md": card_text,
        f"{path}/INDEX.md": f"---\nmegamind: index\nwiki: {name}\n---\n\n# {name} index\n\n",
        f"{path}/wiki/index.md": f"# {name} compiled index\n",
        f"{path}/wiki/log.md": f"# {name} log\n\n",
        # The nested card is authoritative for the wiki root itself, so its
        # paths are rooted there, not at the vault. Serializing it through the
        # card module also stops the two card formats from drifting apart.
        f"{path}/{MEGAMIND_DIR}/wiki-card.json": serialize_wiki_card(
            replace(entry, path=".", card="CARD.md", index="INDEX.md")
        ),
        f"{path}/{MEGAMIND_DIR}/gaps.jsonl": "",
    }
    for rel, text in files.items():
        atomic_write(root, rel, text)
    registry_backup = backup_existing(root, REGISTRY_PATH)
    save_registry(root, new_registry)
    router_path = resolve_contained(root, ROUTER_FILENAME)
    router_old = router_path.read_text(encoding="utf-8") if router_path.is_file() else ""
    if not router_old or ROUTER_HEADER in router_old:
        backup_existing(root, ROUTER_FILENAME)
        atomic_write(root, ROUTER_FILENAME, generate_router(new_registry))
    append_audit(
        root,
        "provisional-wiki-create",
        {
            "wiki": name,
            "path": path,
            "provisional": True,
            "registry_backup": registry_backup.name if registry_backup else None,
        },
    )
    append_log_event(
        wiki_dir,
        "evaluation",
        _date(today),
        "qualified provisional local wiki created",
        pages=[],
        sources=[],
        confidence="unknown",
        outcome="provisional",
        audit=f"provisional-wiki:{name}",
    )
    return [*sorted(files), REGISTRY_PATH.as_posix()]


def validate_gap_journal(root: Path) -> list[str]:
    try:
        GapStore(root).records()
    except GardenError as error:
        return [str(error)]
    return []
