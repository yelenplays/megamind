"""The local, network-free research job spine.

Hosts own discovery, retrieval, extraction, and scheduling.  This module only
records validated receipts and lifecycle facts, so it cannot fetch, dispatch,
or turn a packet into an answer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .fsops import (
    MEGAMIND_DIR,
    atomic_write,
    content_hash,
    identity_bytes,
    resolve_contained,
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


def make_plan(raw: object) -> dict[str, Any]:
    data = _map("research plan", raw)
    data.setdefault("schema", PLAN_SCHEMA)
    return validate_plan(data)


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
    raw: object, *, claims: Mapping[str, Mapping[str, Any]] | None = None
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
                raise ResearchError("packet references an unknown claim")
    contradiction_ids = sorted(
        _strings("packet contradiction_ids", data.get("contradiction_ids", []))
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
    if data.get("outcome_id") != expected:
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
            "plans": make_plan,
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
        atomic_write(self.root, rel, text)
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
            "plans": make_plan,
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

    def list(self, kind: str) -> list[dict[str, Any]]:
        return [self.get(kind, identifier) for identifier in self._identifiers(kind)]

    def scan(self, kind: str) -> ScanResult:
        """List every record, reporting rather than raising on a bad one."""
        results: ScanResult = []
        for identifier in self._identifiers(kind):
            try:
                results.append((identifier, self.get(kind, identifier), ""))
            except (ResearchError, OSError, UnicodeDecodeError) as error:
                results.append((identifier, None, str(error)))
        return results
