"""The local, network-free research job spine.

Hosts own discovery, retrieval, extraction, and scheduling.  This module only
records validated receipts and lifecycle facts, so it cannot fetch, dispatch,
or turn a packet into an answer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .fsops import (
    MEGAMIND_DIR,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    identity_bytes,
    resolve_contained,
)
from .confidence import RELIANCE_FLOOR, Confidence, answer_confidence

LEGACY_JOBS_PATH = Path(MEGAMIND_DIR) / "research" / "jobs.jsonl"
LEGACY_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "gap-open": frozenset({"permission-check", "cancelled"}),
        "permission-check": frozenset(
            {"planned", "cancelled", "policy-denied", "approval-required"}
        ),
        "planned": frozenset({"discovering", "cancelled"}),
        "discovering": frozenset({"retrieving", "cancelled", "budget-exhausted"}),
        "retrieving": frozenset({"accepting", "cancelled", "budget-exhausted"}),
        "accepting": frozenset({"extracting", "packet-ready", "cancelled"}),
        "extracting": frozenset({"reconciling", "cancelled"}),
        "reconciling": frozenset(
            {"packet-ready", "cancelled", "unresolved-contradiction"}
        ),
    }
)

PLAN_SCHEMA = "megamind/research-plan/v1"
JOB_SCHEMA = "megamind/research-job/v1"
PACKET_SCHEMA = "megamind/research-packet/v1"
OUTCOME_SCHEMA = "megamind/research-outcome/v1"
CANDIDATE_SCHEMA = "megamind/source-candidate/v1"

# A job is lifecycle state and a candidate is a discovery observation: their
# ids cover only the identity they are keyed by, so the remaining fields are
# the very things a later stage advances.  Plans, packets, and outcomes hash
# their whole body and therefore stay byte-immutable.
MUTABLE_FIELDS: dict[str, frozenset[str]] = {
    "plans": frozenset(),
    "jobs": frozenset({"state", "attempt", "events"}),
    "packets": frozenset(),
    "outcomes": frozenset(),
    "candidates": frozenset({"found_by", "rank", "status", "reason"}),
}

# One stored record per entry: its identifier, the validated record when it
# reads, and the problem that stopped it when it does not.
ScanResult = list[tuple[str, dict[str, Any] | None, str]]


class ResearchError(ValueError):
    code = "research_invalid"


class ReplayConflict(ResearchError):
    code = "research_replay_conflict"


class ResearchDrift(ResearchError):
    code = "research_replan_required"


class MappingResolver:
    def __init__(self, values: Mapping[str, set[str]]):
        self.values = values

    def resolve(self, kind: str, identifier: str) -> bool:
        return identifier in self.values.get(kind, set())


@dataclass(frozen=True)
class LegacyPlan:
    plan_id: str
    job_id: str
    wiki: str
    gap_id: str
    question: str
    policy_digest: str
    card_digest: str
    access_digest: str
    ceilings: dict[str, int]


@dataclass(frozen=True)
class LegacyJob:
    job_id: str
    attempt_id: str
    state: str
    plan_id: str
    gap_id: str
    policy_digest: str
    card_digest: str
    access_digest: str
    event_id: str
    reason: str = ""
    artifact_ids: tuple[str, ...] = ()
    contradiction_ids: tuple[str, ...] = ()


def _map(label: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchError(f"{label} must be an object")
    return dict(value)


def _unknown(label: str, data: Mapping[str, object], allowed: set[str]) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise ResearchError(f"{label} has unknown field(s): {', '.join(extra)}")


def _str(label: str, value: object, *, required: bool = True, limit: int = 1000) -> str:
    if not isinstance(value, str) or (required and not value.strip()):
        raise ResearchError(f"{label} must be {'a non-empty ' if required else 'a '}string")
    if len(value) > limit:
        raise ResearchError(f"{label} exceeds its length limit")
    return value


def _strings(label: str, value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ResearchError(f"{label} must be a list of strings")
    return list(value)


def _hash_body(schema: str, body: Mapping[str, Any]) -> str:
    return content_hash(
        json.dumps({"schema": schema, **body}, sort_keys=True, separators=(",", ":"))
    )


def validate_plan(raw: object) -> dict[str, Any]:
    data = _map("research plan", raw)
    allowed = {
        "schema",
        "plan_id",
        "gap_id",
        "wiki",
        "question",
        "must_not_answer",
        "required_claims",
        "inclusion",
        "exclusion",
        "capabilities",
        "budget",
        "privacy_class",
        "change_envelope",
        "policy_digest",
        "card_digest",
        "access_digest",
        "today",
    }
    _unknown("research plan", data, allowed)
    if data.get("schema") != PLAN_SCHEMA:
        raise ResearchError(f"research plan schema must be {PLAN_SCHEMA}")
    body: dict[str, Any] = {
        "gap_id": _str("plan gap_id", data.get("gap_id"), limit=64),
        "wiki": _str("plan wiki", data.get("wiki"), limit=300),
        "question": _str("plan question", data.get("question"), limit=1000),
        "must_not_answer": _strings("plan must_not_answer", data.get("must_not_answer", [])),
        "required_claims": _strings("plan required_claims", data.get("required_claims", [])),
        "inclusion": _strings("plan inclusion", data.get("inclusion", [])),
        "exclusion": _strings("plan exclusion", data.get("exclusion", [])),
        "capabilities": _map(
            "plan capabilities",
            data.get(
                "capabilities",
                {"web": False, "transcript": False, "paid": False, "credentials": False},
            ),
        ),
        "budget": _map(
            "plan budget",
            data.get(
                "budget",
                {
                    "queries": 0,
                    "candidates": 0,
                    "retrievals": 0,
                    "bytes": 0,
                    "videos": 0,
                    "cost": 0,
                },
            ),
        ),
        "privacy_class": _str(
            "plan privacy_class", data.get("privacy_class", "public-reference"), limit=40
        ),
        "change_envelope": _str(
            "plan change_envelope", data.get("change_envelope", "approval"), limit=30
        ),
        "policy_digest": _str(
            "plan policy_digest", data.get("policy_digest", ""), required=False, limit=64
        ),
        "card_digest": _str(
            "plan card_digest", data.get("card_digest", ""), required=False, limit=64
        ),
        "access_digest": _str(
            "plan access_digest", data.get("access_digest", ""), required=False, limit=64
        ),
        "today": _str("plan today", data.get("today", ""), required=False, limit=10),
    }
    for key, value in body["capabilities"].items():
        if not isinstance(key, str) or not isinstance(value, bool):
            raise ResearchError("plan capabilities must be boolean facts")
    for key, value in body["budget"].items():
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
        ):
            raise ResearchError("plan budget values must be non-negative numbers")
    expected = _hash_body(PLAN_SCHEMA, body)
    if data.get("plan_id", expected) != expected:
        raise ResearchError("plan_id does not match the complete plan body")
    return {"schema": PLAN_SCHEMA, "plan_id": expected, **body}


def _make_receipt_plan(raw: object) -> dict[str, Any]:
    data = _map("research plan", raw)
    data.setdefault("schema", PLAN_SCHEMA)
    return validate_plan(data)


def _legacy_plan(raw: Mapping[str, Any]) -> LegacyPlan:
    required = ("wiki", "gap_id", "question", "policy_digest", "card_digest", "access_digest")
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
        raise ResearchError("wiki must be a non-empty string")
    ceilings = raw.get("ceilings")
    if not isinstance(ceilings, Mapping) or not ceilings or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in ceilings.values()
    ):
        raise ResearchError("ceilings must contain non-negative integer limits")
    body = {
        "wiki": raw["wiki"],
        "gap_id": raw["gap_id"],
        "question": raw["question"],
        "policy_digest": raw["policy_digest"],
        "card_digest": raw["card_digest"],
        "access_digest": raw["access_digest"],
        "ceilings": dict(sorted((str(key), value) for key, value in ceilings.items())),
    }
    job_id = content_hash(json.dumps({key: body[key] for key in ("wiki", "gap_id", "question")}, sort_keys=True, separators=(",", ":")))
    plan_id = content_hash(json.dumps({"job_id": job_id, **body}, sort_keys=True, separators=(",", ":")))
    if raw.get("plan_id") not in (None, plan_id):
        raise ReplayConflict("plan_id does not match the plan body")
    return LegacyPlan(plan_id, job_id, str(body["wiki"]), str(body["gap_id"]), str(body["question"]), str(body["policy_digest"]), str(body["card_digest"]), str(body["access_digest"]), dict(body["ceilings"]))


def make_plan(raw: object, *, today: str | None = None) -> dict[str, Any] | LegacyPlan:
    data = _map("research plan", raw)
    if "ceilings" in data:
        return _legacy_plan(data)
    return _make_receipt_plan(data)


def validate_candidate(raw: object) -> dict[str, Any]:
    data = _map("source candidate", raw)
    _unknown(
        "source candidate",
        data,
        {"schema", "candidate_id", "origin", "found_by", "query_hash", "rank", "status", "reason"},
    )
    if data.get("schema") != CANDIDATE_SCHEMA:
        raise ResearchError(f"source candidate schema must be {CANDIDATE_SCHEMA}")
    origin = _str("candidate origin", data.get("origin"), limit=2000)
    candidate_id = content_hash(
        json.dumps(
            {"origin": origin, "query_hash": data.get("query_hash", "")},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if data.get("candidate_id") != candidate_id:
        raise ResearchError("candidate_id does not match candidate identity")
    rank = data.get("rank", 0)
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
        raise ResearchError("candidate rank must be non-negative")
    status = _str("candidate status", data.get("status", "discovered"), limit=20)
    if status not in {"discovered", "retrieved", "excluded", "failed", "deferred"}:
        raise ResearchError("candidate status is invalid")
    return {
        "schema": CANDIDATE_SCHEMA,
        "candidate_id": candidate_id,
        "origin": origin,
        "found_by": _str("candidate found_by", data.get("found_by", ""), required=False, limit=200),
        "query_hash": _str(
            "candidate query_hash", data.get("query_hash", ""), required=False, limit=64
        ),
        "rank": rank,
        "status": status,
        "reason": _str("candidate reason", data.get("reason", ""), required=False, limit=300),
    }


def validate_job(raw: object) -> dict[str, Any]:
    data = _map("research job", raw)
    _unknown(
        "research job",
        data,
        {"schema", "job_id", "plan_id", "gap_id", "state", "attempt", "events"},
    )
    if data.get("schema") != JOB_SCHEMA:
        raise ResearchError(f"research job schema must be {JOB_SCHEMA}")
    plan_id = _str("job plan_id", data.get("plan_id"), limit=64)
    gap_id = _str("job gap_id", data.get("gap_id"), limit=64)
    job_id = _str("job job_id", data.get("job_id"), limit=64)
    expected = content_hash(
        json.dumps({"plan_id": plan_id, "gap_id": gap_id}, sort_keys=True, separators=(",", ":"))
    )
    if job_id != expected:
        raise ResearchError("job_id does not match plan and gap")
    state = _str("job state", data.get("state", "planned"), limit=40)
    states = {
        "gap-open",
        "permission-check",
        "policy-denied",
        "approval-required",
        "planned",
        "discovering",
        "retrieving",
        "accepting",
        "extracting",
        "reconciling",
        "packet-ready",
        "no-eligible-evidence",
        "evidence-deferred",
        "unresolved-contradiction",
        "cancelled",
        "budget-exhausted",
        "tool-failed",
        "change-proposed",
        "change-rejected",
        "change-approved",
        "applying",
        "rolled-back",
        "validating",
        "rerouting",
        "readmitting",
        "admission-failed",
        "answered",
        "answered-with-open-gap",
    }
    if state not in states:
        raise ResearchError("job state is invalid")
    attempt = data.get("attempt", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ResearchError("job attempt must be positive")
    events = data.get("events", [])
    if not isinstance(events, list) or any(not isinstance(item, Mapping) for item in events):
        raise ResearchError("job events must be a list of objects")
    return {
        "schema": JOB_SCHEMA,
        "job_id": job_id,
        "plan_id": plan_id,
        "gap_id": gap_id,
        "state": state,
        "attempt": attempt,
        "events": [dict(item) for item in events],
    }


def validate_packet(
    raw: object,
    *,
    claims: Mapping[str, Mapping[str, Any]] | None = None,
    contradictions: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    data = _map("research packet", raw)
    _unknown(
        "research packet",
        data,
        {
            "schema",
            "packet_id",
            "plan_id",
            "claim_ids",
            "contradiction_ids",
            "interpretation",
            "supported_by",
            "answerability",
            "confidence",
        },
    )
    if data.get("schema") != PACKET_SCHEMA:
        raise ResearchError(f"research packet schema must be {PACKET_SCHEMA}")
    plan_id = _str("packet plan_id", data.get("plan_id"), limit=64)
    claim_ids = sorted(_strings("packet claim_ids", data.get("claim_ids", [])))
    if claims is not None:
        for claim_id in claim_ids:
            if claim_id not in claims:
                raise ResearchError(f"packet references an unknown claim: {claim_id}")
    contradiction_ids = sorted(
        _strings("packet contradiction_ids", data.get("contradiction_ids", []))
    )
    if contradictions is not None:
        for contradiction_id in contradiction_ids:
            if contradiction_id not in contradictions:
                raise ResearchError(
                    f"packet references an unknown contradiction: {contradiction_id}"
                )
    interpretation = _str(
        "packet interpretation", data.get("interpretation", ""), required=False, limit=5000
    )
    supported_by = _strings("packet supported_by", data.get("supported_by", []))
    answerability = _map("packet answerability", data.get("answerability", {}))
    for key, value in answerability.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ResearchError("packet answerability values must be strings")
    confidence = data.get("confidence", "unknown")
    if not (
        confidence == "unknown"
        or (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 1
        )
    ):
        raise ResearchError("packet confidence must be unknown or a number in [0,1]")
    body = {
        "plan_id": plan_id,
        "claim_ids": claim_ids,
        "contradiction_ids": contradiction_ids,
        "interpretation": interpretation,
        "supported_by": supported_by,
        "answerability": answerability,
        "confidence": confidence,
    }
    expected = _hash_body(PACKET_SCHEMA, body)
    if data.get("packet_id") != expected:
        raise ResearchError("packet_id does not match packet content")
    return {"schema": PACKET_SCHEMA, "packet_id": expected, **body}


def packet_content_id(packet: Mapping[str, Any]) -> str:
    """Return the validated immutable identity of a stored research packet."""
    return str(validate_packet(packet)["packet_id"])


def make_packet(
    packet: Mapping[str, Any], resolver: Any | None = None
) -> dict[str, Any]:
    if "claims" not in packet and "claim_ids" in packet:
        return validate_packet(packet)
    claims = packet.get("claims", [])
    contradictions = packet.get("contradictions", [])
    if not isinstance(claims, list) or not all(isinstance(item, str) for item in claims):
        raise ResearchError("packet claims must be opaque references")
    if not isinstance(contradictions, list) or not all(isinstance(item, str) for item in contradictions):
        raise ResearchError("packet contradictions must be opaque references")
    if (claims or contradictions) and resolver is None:
        raise ResearchError("packet claim and contradiction references require a resolver")
    if resolver is not None:
        if any(not resolver.resolve("claim", item) for item in claims):
            raise ResearchError("packet contains an unresolved claim reference")
        if any(not resolver.resolve("contradiction", item) for item in contradictions):
            raise ResearchError("packet contains an unresolved contradiction reference")
    scores = [resolver.claim_confidence(item) for item in claims] if resolver is not None else []
    confidence = answer_confidence(
        [Confidence(float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else Confidence(None) for value in scores]
    ).render()
    unresolved = [
        item for item in contradictions if resolver is not None and resolver.contradiction_is_unresolved(item)
    ]
    below = sum(
        1 for value in scores if isinstance(value, (int, float)) and not isinstance(value, bool) and value < RELIANCE_FLOOR
    )
    answerability = {
        "verdict": "contradicted" if unresolved else "supported" if scores and not below and confidence != "unknown" else "insufficient",
        "claims": len(scores),
        "unknown_claims": sum(1 for value in scores if value == "unknown"),
        "below_floor_claims": below,
        "unresolved_contradictions": len(unresolved),
        "reliance_floor": RELIANCE_FLOOR,
    }
    body = {
        "job_id": _str("packet job_id", packet.get("job_id")),
        "attempt_id": _str("packet attempt_id", packet.get("attempt_id")),
        "claims": claims,
        "contradictions": contradictions,
        "interpretation": _str("packet interpretation", packet.get("interpretation", ""), required=False),
        "confidence": confidence,
        "answerability": answerability,
        "provenance": [],
    }
    packet_id = content_hash(json.dumps(body, sort_keys=True, separators=(",", ":")))
    if packet.get("packet_id") not in (None, packet_id):
        raise ReplayConflict("packet_id does not match content")
    return {"schema": PACKET_SCHEMA, "packet_id": packet_id, **body}


def validate_outcome(raw: object) -> dict[str, Any]:
    data = _map("research outcome", raw)
    allowed = {
        "schema",
        "outcome_id",
        "job_id",
        "plan_id",
        "status",
        "artifact_ids",
        "packet_id",
        "failure",
        "today",
    }
    _unknown("research outcome", data, allowed)
    if data.get("schema") != OUTCOME_SCHEMA:
        raise ResearchError(f"research outcome schema must be {OUTCOME_SCHEMA}")
    body = {
        "job_id": _str("outcome job_id", data.get("job_id"), limit=64),
        "plan_id": _str("outcome plan_id", data.get("plan_id"), limit=64),
        "status": _str("outcome status", data.get("status"), limit=40),
        "artifact_ids": sorted(_strings("outcome artifact_ids", data.get("artifact_ids", []))),
        "packet_id": _str("outcome packet_id", data.get("packet_id", ""), required=False, limit=64),
        "failure": _str("outcome failure", data.get("failure", ""), required=False, limit=300),
        "today": _str("outcome today", data.get("today", ""), required=False, limit=10),
    }
    if body["status"] not in {"completed", "research-pending", "failed", "cancelled", "deferred"}:
        raise ResearchError("outcome status is invalid")
    expected = _hash_body(OUTCOME_SCHEMA, body)
    if data.get("outcome_id") not in (None, expected):
        raise ResearchError("outcome_id does not match outcome content")
    return {"schema": OUTCOME_SCHEMA, "outcome_id": expected, **body}


class ResearchStore:
    """Small content-addressed local store. No worker or network behavior."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, identifier: str) -> Path:
        if kind not in {"plans", "jobs", "packets", "outcomes", "candidates"}:
            raise ResearchError("unknown research store kind")
        suffix = ".jsonl" if kind == "jobs" else ".json"
        return Path(MEGAMIND_DIR) / "research" / kind / f"{identifier}{suffix}"

    def put(self, kind: str, data: Mapping[str, Any]) -> Path:
        validators = {
            "plans": _make_receipt_plan,
            "jobs": validate_job,
            "packets": validate_packet,
            "outcomes": validate_outcome,
            "candidates": validate_candidate,
        }
        canonical = validators[kind](data)
        id_key = {
            "plans": "plan_id",
            "jobs": "job_id",
            "packets": "packet_id",
            "outcomes": "outcome_id",
            "candidates": "candidate_id",
        }[kind]
        identifier = str(canonical[id_key])
        rel = self._path(kind, identifier)
        path = resolve_contained(self.root, rel)
        text = json.dumps(canonical, sort_keys=True, indent=2) + "\n"
        if path.is_file():
            mutable = MUTABLE_FIELDS[kind]
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ResearchError("stored research record is not valid JSON") from error
            if identity_bytes(existing, mutable) != identity_bytes(canonical, mutable):
                raise ResearchError("content-addressed research record has different bytes")
            backup_existing(self.root, rel)
        atomic_write(self.root, rel, text)
        append_audit(self.root, "research-record", {"kind": kind, "record_id": identifier})
        return path

    def get(self, kind: str, identifier: str) -> dict[str, Any]:
        path = resolve_contained(self.root, self._path(kind, identifier))
        if not path.is_file():
            raise ResearchError("research record not found")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ResearchError("research record is not valid JSON") from error
        return {
            "plans": _make_receipt_plan,
            "jobs": validate_job,
            "packets": validate_packet,
            "outcomes": validate_outcome,
            "candidates": validate_candidate,
        }[kind](raw)

    def _identifiers(self, kind: str) -> list[str]:
        directory = resolve_contained(self.root, Path(MEGAMIND_DIR) / "research" / kind)
        if not directory.is_dir():
            return []
        pattern = "*.jsonl" if kind == "jobs" else "*.json"
        return [path.stem for path in sorted(directory.glob(pattern))]

    def scan(self, kind: str) -> ScanResult:
        """List every record, reporting rather than raising on a bad one."""
        results: ScanResult = []
        for identifier in self._identifiers(kind):
            try:
                results.append((identifier, self.get(kind, identifier), ""))
            except (ResearchError, OSError, UnicodeDecodeError) as error:
                results.append((identifier, None, str(error)))
        return results

    def _legacy_events(self) -> list[dict[str, Any]]:
        path = resolve_contained(self.root, LEGACY_JOBS_PATH)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ResearchError("invalid research job journal entry") from error
            if not isinstance(value, dict):
                raise ResearchError("invalid research job journal entry")
            fields = {
                "schema", "job_id", "attempt_id", "state", "plan_id", "gap_id",
                "policy_digest", "card_digest", "access_digest", "event_id", "reason",
                "artifact_ids", "contradiction_ids", "updated",
            }
            if set(value) != fields or value.get("schema") != JOB_SCHEMA:
                raise ResearchError("invalid research job journal entry")
            string_fields = {
                "job_id",
                "attempt_id",
                "state",
                "plan_id",
                "gap_id",
                "policy_digest",
                "card_digest",
                "access_digest",
                "event_id",
                "reason",
                "updated",
            }
            if any(not isinstance(value[field], str) for field in string_fields):
                raise ResearchError("invalid research job journal entry")
            if any(not value[field] for field in ("job_id", "attempt_id", "state", "plan_id", "gap_id", "event_id")):
                raise ResearchError("invalid research job journal entry")
            for field in ("artifact_ids", "contradiction_ids"):
                identifiers = value[field]
                if (
                    not isinstance(identifiers, list)
                    or any(not isinstance(identifier, str) or not identifier for identifier in identifiers)
                    or identifiers != sorted(set(identifiers))
                ):
                    raise ResearchError("invalid research job journal entry")
            payload = {key: value[key] for key in fields - {"schema", "event_id", "updated"}}
            if value.get("event_id") != content_hash(json.dumps(payload, sort_keys=True, separators=(",", ":"))):
                raise ResearchError("invalid research job journal entry")
            events.append(value)
        latest: dict[str, dict[str, Any]] = {}
        for event in events:
            job_id = str(event["job_id"])
            prior = latest.get(job_id)
            if prior is None:
                if event["state"] != "gap-open":
                    raise ResearchError("invalid research job journal transition")
            else:
                resumed = (
                    prior["state"] == "cancelled"
                    and event["state"] == "gap-open"
                    and event["attempt_id"] != prior["attempt_id"]
                )
                if resumed:
                    if event["artifact_ids"] or event["contradiction_ids"]:
                        raise ResearchError("invalid research job journal transition")
                elif (
                    event["attempt_id"] != prior["attempt_id"]
                    or any(event[key] != prior[key] for key in ("plan_id", "gap_id", "policy_digest", "card_digest", "access_digest"))
                    or event["state"] not in LEGACY_TRANSITIONS.get(
                        str(prior["state"]), frozenset()
                    )
                    or (prior["state"] == "accepting" and (event["artifact_ids"] != prior["artifact_ids"] or event["contradiction_ids"] != prior["contradiction_ids"]))
                ):
                    raise ResearchError("invalid research job journal transition")
            latest[job_id] = event
        return events

    def jobs(self) -> list[LegacyJob]:
        latest: dict[str, LegacyJob] = {}
        for event in self._legacy_events():
            latest[str(event["job_id"])] = LegacyJob(
                str(event["job_id"]), str(event["attempt_id"]), str(event["state"]),
                str(event["plan_id"]), str(event["gap_id"]), str(event["policy_digest"]),
                str(event["card_digest"]), str(event["access_digest"]), str(event["event_id"]),
                str(event["reason"]), tuple(event["artifact_ids"]), tuple(event["contradiction_ids"]),
            )
        return [latest[key] for key in sorted(latest)]

    def _legacy_job(self, job_id: str) -> LegacyJob | None:
        return next((job for job in self.jobs() if job.job_id == job_id), None)

    def start(self, plan: LegacyPlan, *, today: str | None = None) -> LegacyJob:
        current = self._legacy_job(plan.job_id)
        if current is not None:
            self.assert_no_drift(
                current,
                policy_digest=plan.policy_digest,
                card_digest=plan.card_digest,
                access_digest=plan.access_digest,
            )
            if current.plan_id != plan.plan_id:
                raise ResearchDrift("plan or bound policy/card/access digest drift requires replan")
            return current
        attempt_id = content_hash(json.dumps({"job_id": plan.job_id, "plan_id": plan.plan_id, "attempt": 1}, sort_keys=True, separators=(",", ":")))
        return self.transition(
            plan.job_id, "gap-open", plan_id=plan.plan_id, gap_id=plan.gap_id,
            attempt_id=attempt_id, policy_digest=plan.policy_digest, card_digest=plan.card_digest,
            access_digest=plan.access_digest, today=today,
        )

    def transition(
        self,
        job_id: str,
        state: str,
        *,
        plan_id: str,
        gap_id: str,
        attempt_id: str,
        reason: str = "",
        artifact_ids: list[str] | None = None,
        contradiction_ids: list[str] | None = None,
        policy_digest: str = "",
        card_digest: str = "",
        access_digest: str = "",
        today: str | None = None,
        expected_state: str | None = None,
    ) -> LegacyJob:
        current = self._legacy_job(job_id)
        if current is not None:
            if expected_state and current.state != expected_state:
                raise ResearchError(f"expected {expected_state}, found {current.state}")
            resumed = (
                current.state == "cancelled"
                and state == "gap-open"
                and attempt_id != current.attempt_id
            )
            if current.plan_id != plan_id or current.gap_id != gap_id or (
                current.attempt_id != attempt_id and not resumed
            ):
                raise ReplayConflict("attempt identity does not match current job")
            policy_digest = policy_digest or current.policy_digest
            card_digest = card_digest or current.card_digest
            access_digest = access_digest or current.access_digest
            effective_artifacts = sorted(set(artifact_ids or [])) if resumed else sorted(set(artifact_ids if artifact_ids is not None else current.artifact_ids))
            effective_contradictions = sorted(set(contradiction_ids or [])) if resumed else sorted(set(contradiction_ids if contradiction_ids is not None else current.contradiction_ids))
            if current.state == "accepting" and (
                effective_artifacts != sorted(current.artifact_ids)
                or effective_contradictions != sorted(current.contradiction_ids)
            ):
                raise ReplayConflict("accepted artifact set is immutable")
            if (
                not resumed
                and state != current.state
                and state not in LEGACY_TRANSITIONS.get(current.state, frozenset())
            ):
                raise ResearchError(f"cannot transition {current.state} to {state}")
        else:
            effective_artifacts = sorted(set(artifact_ids or []))
            effective_contradictions = sorted(set(contradiction_ids or []))
        payload = {
            "job_id": job_id, "attempt_id": attempt_id, "state": state, "plan_id": plan_id,
            "gap_id": gap_id, "policy_digest": policy_digest, "card_digest": card_digest,
            "access_digest": access_digest, "reason": reason, "artifact_ids": effective_artifacts,
            "contradiction_ids": effective_contradictions,
        }
        event_id = content_hash(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if current is not None and current.event_id == event_id:
            return current
        if current is not None and current.state == state:
            raise ReplayConflict("divergent replay for the current state")
        event = {"schema": JOB_SCHEMA, **payload, "event_id": event_id, "updated": today or ""}
        events = self._legacy_events()
        backup_existing(self.root, LEGACY_JOBS_PATH)
        atomic_write(
            self.root, LEGACY_JOBS_PATH,
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in [*events, event]),
        )
        append_audit(
            self.root,
            "research-transition",
            {"job_id": job_id, "attempt_id": attempt_id, "state": state, "event_id": event_id},
        )
        return LegacyJob(job_id, attempt_id, state, plan_id, gap_id, policy_digest, card_digest, access_digest, event_id, reason, tuple(effective_artifacts), tuple(effective_contradictions))

    def assert_no_drift(
        self, job: LegacyJob, *, policy_digest: str, card_digest: str, access_digest: str
    ) -> None:
        if (job.policy_digest, job.card_digest, job.access_digest) != (
            policy_digest, card_digest, access_digest,
        ):
            raise ResearchDrift("policy, card, access, or plan drift requires replan")

    def save_packet(self, packet: Mapping[str, Any], resolver: Any | None = None) -> Path:
        normalized = make_packet(packet, resolver)
        rel = Path(MEGAMIND_DIR) / "research" / "packets" / f"{normalized['packet_id']}.json"
        path = resolve_contained(self.root, rel)
        text = json.dumps(normalized, indent=2, sort_keys=True) + "\n"
        if path.is_file():
            if path.read_text(encoding="utf-8") != text:
                raise ResearchError("research artifact is immutable")
            return path
        result = atomic_write(self.root, rel, text)
        append_audit(self.root, "research-packet", {"packet_id": str(normalized["packet_id"])})
        return result
