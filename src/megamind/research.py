"""Deterministic research state spine.

Megamind owns state and validation only.  Hosts may freeze artifacts and pass
JSON receipts to this module, but this module never fetches, schedules, or
calls a model.  Journals are append-only; plans, packets, and outcomes are
immutable content-addressed documents.

Permission is never a host claim.  Every plan names one wiki, and
``research_authority`` derives the verdict and the bound policy/card/access
digests from that wiki's validated card and from the access policy layer.  A
host receipt can only narrow that verdict, never widen it.  Packet confidence
and answerability follow the same rule: they are derived from the resolved
claim and contradiction records through a narrow resolver, never taken from
the receipt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, cast

from .access import effective_policy
from .card import load_wiki_card
from .confidence import RELIANCE_FLOOR, Confidence, answer_confidence
from .fsops import MEGAMIND_DIR, append_audit, atomic_write, content_hash, resolve_contained
from .models import KNOWLEDGE_TYPES, Document
from .registry import (
    RegistryNotInitialized,
    WikiEntry,
    load_registry,
    research_policy_allows,
)

PLAN_SCHEMA = "megamind/research-plan/v1"
JOB_SCHEMA = "megamind/research-job/v1"
PACKET_SCHEMA = "megamind/research-packet/v1"
OUTCOME_SCHEMA = "megamind/research-outcome/v1"
RESEARCH_DIR = Path(MEGAMIND_DIR) / "research"
PLANS_DIR = RESEARCH_DIR / "plans"
PACKETS_DIR = RESEARCH_DIR / "packets"
OUTCOMES_DIR = RESEARCH_DIR / "outcomes"
JOBS_PATH = RESEARCH_DIR / "jobs.jsonl"

ACTIVE_STATES = frozenset(
    {
        "gap-open",
        "permission-check",
        "planned",
        "discovering",
        "retrieving",
        "accepting",
        "extracting",
        "reconciling",
        "packet-ready",
        "change-proposed",
        "change-approved",
        "applying",
        "validating",
        "rerouting",
        "readmitting",
        "answer-gate",
        "awaiting-source-rights",
    }
)
TERMINAL_STATES = frozenset(
    {
        "policy-denied",
        "approval-required",
        "no-eligible-evidence",
        "evidence-deferred",
        "unresolved-contradiction",
        "change-rejected",
        "cancelled",
        "budget-exhausted",
        "tool-failed",
        "rolled-back",
        "admission-failed",
        "superseded",
        "answered",
        "answered-with-open-gap",
        "refused-missing-personal-facts",
        "refused-method-not-validated",
        "refused-professional-advice-required",
        "refused-insufficient-admitted-evidence",
    }
)
TERMINAL = ACTIVE_STATES | TERMINAL_STATES
# The table is deliberately explicit.  A missing edge is a refusal, not an
# implicit transition.  Terminal outcomes cannot be rewritten.
TRANSITIONS: dict[str, frozenset[str]] = {
    "gap-open": frozenset({"permission-check", "cancelled"}),
    "permission-check": frozenset({"policy-denied", "approval-required", "planned", "cancelled"}),
    "planned": frozenset({"discovering", "cancelled"}),
    "discovering": frozenset({"retrieving", "budget-exhausted", "tool-failed", "cancelled"}),
    "retrieving": frozenset({"accepting", "budget-exhausted", "tool-failed", "cancelled"}),
    "accepting": frozenset(
        {
            "extracting",
            "packet-ready",
            "evidence-deferred",
            "no-eligible-evidence",
            "budget-exhausted",
            "tool-failed",
            "cancelled",
        }
    ),
    "extracting": frozenset({"reconciling", "budget-exhausted", "tool-failed", "cancelled"}),
    "reconciling": frozenset(
        {"packet-ready", "unresolved-contradiction", "tool-failed", "cancelled"}
    ),
    "packet-ready": frozenset(
        {
            "change-proposed",
            "no-eligible-evidence",
            "evidence-deferred",
            "unresolved-contradiction",
            "cancelled",
        }
    ),
    "change-proposed": frozenset({"change-approved", "change-rejected", "cancelled"}),
    "change-approved": frozenset({"applying", "cancelled"}),
    "applying": frozenset({"validating", "rolled-back", "tool-failed", "cancelled"}),
    "validating": frozenset({"rerouting", "rolled-back", "tool-failed", "cancelled"}),
    "rerouting": frozenset({"readmitting", "admission-failed", "tool-failed", "cancelled"}),
    "readmitting": frozenset({"answer-gate", "admission-failed", "tool-failed", "cancelled"}),
    "answer-gate": frozenset(
        {
            "answered",
            "answered-with-open-gap",
            "refused-missing-personal-facts",
            "refused-method-not-validated",
            "refused-professional-advice-required",
            "refused-insufficient-admitted-evidence",
            "cancelled",
        }
    ),
    "awaiting-source-rights": frozenset({"planned", "cancelled", "tool-failed"}),
}
for _state in ACTIVE_STATES:
    if _state not in TRANSITIONS:
        TRANSITIONS[_state] = frozenset({"cancelled", "tool-failed", "budget-exhausted"})


class ResearchError(ValueError):
    code = "research_invalid"


class ResearchNotFound(ResearchError):
    code = "research_not_found"


class InvalidResearchTransition(ResearchError):
    code = "research_transition_invalid"


class ReplayConflict(ResearchError):
    code = "research_replay_conflict"


class ResearchDrift(ResearchError):
    code = "research_replan_required"


class ImmutableResearchError(ResearchError):
    code = "research_immutable"


def budget_status(ceilings: Mapping[str, int], usage: Mapping[str, int]) -> dict[str, Any]:
    """Compare host receipts with plan ceilings without mutating a ledger."""
    over: list[str] = []
    normalized: dict[str, int] = {}
    for name, limit in ceilings.items():
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ResearchError("budget ceilings must be non-negative integers")
        spent = usage.get(name, 0)
        if not isinstance(spent, int) or isinstance(spent, bool) or spent < 0:
            raise ResearchError("budget usage must be non-negative integers")
        normalized[name] = spent
        if spent > limit:
            over.append(name)
    return {
        "within": not over,
        "over": sorted(over),
        "ceilings": dict(ceilings),
        "usage": normalized,
    }


def require_budget(ceilings: Mapping[str, int], usage: Mapping[str, int]) -> dict[str, Any]:
    result = budget_status(ceilings, usage)
    if not result["within"]:
        raise ResearchError("budget-exhausted: plan ceiling exceeded")
    return result


def _stable(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _iso(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ResearchError(f"date must be ISO: {value}") from exc


def _string(value: object, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value):
        raise ResearchError(
            f"{label} must be a non-empty string" if required else f"{label} must be a string"
        )
    return value


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchError(f"{label} must be an object")
    return dict(value)


def _unknown(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if set(data) - allowed:
        raise ResearchError(f"{label} contains unknown fields")


def _hash_body(body: Mapping[str, Any]) -> str:
    return content_hash(_stable(body))


@dataclass(frozen=True)
class ResearchPlan:
    plan_id: str
    job_id: str
    wiki: str
    gap_id: str
    question: str
    must_not_answer: tuple[str, ...]
    required_claims: tuple[str, ...]
    inclusion: tuple[str, ...]
    exclusion: tuple[str, ...]
    capabilities: dict[str, bool]
    ceilings: dict[str, int]
    privacy_class: str
    change_envelope: str
    policy_digest: str
    card_digest: str
    access_digest: str
    request_hash: str = ""
    created: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "wiki": self.wiki,
            "gap_id": self.gap_id,
            "question": self.question,
            "must_not_answer": list(self.must_not_answer),
            "required_claims": list(self.required_claims),
            "inclusion": list(self.inclusion),
            "exclusion": list(self.exclusion),
            "capabilities": dict(self.capabilities),
            "ceilings": dict(self.ceilings),
            "privacy_class": self.privacy_class,
            "change_envelope": self.change_envelope,
            "policy_digest": self.policy_digest,
            "card_digest": self.card_digest,
            "access_digest": self.access_digest,
            "request_hash": self.request_hash,
            "created": self.created,
        }

    def to_data(self) -> dict[str, Any]:
        return {"schema": PLAN_SCHEMA, "plan_id": self.plan_id, **self.body()}


@dataclass(frozen=True)
class JobView:
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
    updated: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": JOB_SCHEMA,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "plan_id": self.plan_id,
            "gap_id": self.gap_id,
            "policy_digest": self.policy_digest,
            "card_digest": self.card_digest,
            "access_digest": self.access_digest,
            "event_id": self.event_id,
            "reason": self.reason,
            "artifact_ids": list(self.artifact_ids),
            "contradiction_ids": list(self.contradiction_ids),
            "updated": self.updated,
        }


@dataclass(frozen=True)
class ResearchAuthority:
    """The card-derived verdict that alone may authorize one research cycle.

    A host receipt is an observation.  Eligibility comes from the validated
    wiki card's research policy and from the access policy layer, and the
    digests below are computed here so a later card edit is drift, not a
    silently reused authorization.
    """

    wiki: str
    authorized: bool
    reason: str
    policy_digest: str
    card_digest: str
    access_digest: str
    max_sources_per_cycle: int
    apply_mode: str

    def to_data(self) -> dict[str, Any]:
        return {
            "wiki": self.wiki,
            "authorized": self.authorized,
            "reason": self.reason,
            "policy_digest": self.policy_digest,
            "card_digest": self.card_digest,
            "access_digest": self.access_digest,
            "max_sources_per_cycle": self.max_sources_per_cycle,
            "apply_mode": self.apply_mode,
        }


def research_authority(entry: WikiEntry) -> ResearchAuthority:
    """Derive the restrictive research verdict and bound digests for one card."""
    access = effective_policy(entry)
    denials: list[str] = []
    if entry.provisional:
        denials.append("wiki is provisional")
    if not research_policy_allows(entry):
        denials.append("card research policy does not authorize a research cycle")
    if access.local == "none" or access.routing_mode == "pointer":
        denials.append(f"access policy allows local '{access.local}' for this wiki")
    return ResearchAuthority(
        entry.name,
        not denials,
        "; ".join(denials) or "card research policy authorizes this cycle",
        _hash_body(asdict(entry.research_policy)),
        _hash_body(asdict(entry)),
        _hash_body(asdict(access)),
        entry.research_policy.max_sources_per_cycle,
        entry.research_policy.apply_mode,
    )


def resolve_wiki_entry(root: Path, name: str) -> WikiEntry:
    """Return the authoritative card entry a plan is bound to."""
    if not name:
        raise ResearchError("research plan must name a wiki")
    try:
        registry = load_registry(root)
    except RegistryNotInitialized:
        card = load_wiki_card(root)
        if card.name != name:
            raise ResearchError(
                f"wiki does not match the authoritative wiki card: {name}"
            ) from None
        return card
    entry = registry.wiki_by_name(name)
    if entry is None:
        raise ResearchError(f"wiki is not present in the authoritative registry: {name}")
    return entry


class ReferenceResolver(Protocol):
    def resolve(self, kind: str, identifier: str) -> bool: ...


class PacketFactResolver(ReferenceResolver, Protocol):
    """Narrow seam that returns typed facts about a reference, never content.

    A packet's confidence and answerability are computed from these facts, so
    the identifiers stay opaque and no receipt value reaches the frozen packet.
    """

    def claim_confidence(self, identifier: str) -> float | str: ...

    def contradiction_is_unresolved(self, identifier: str) -> bool: ...


class MappingResolver:
    """Narrow resolver used by packet/evolve validation; ids remain opaque."""

    def __init__(self, values: Mapping[str, set[str]]):
        self.values = values

    def resolve(self, kind: str, identifier: str) -> bool:
        return identifier in self.values.get(kind, set())


def make_plan(data: Mapping[str, Any], *, today: str | None = None) -> ResearchPlan:
    allowed = {
        "job_id",
        "wiki",
        "gap_id",
        "question",
        "must_not_answer",
        "required_claims",
        "inclusion",
        "exclusion",
        "capabilities",
        "ceilings",
        "privacy_class",
        "change_envelope",
        "policy_digest",
        "card_digest",
        "access_digest",
        "request_hash",
        "created",
    }
    _unknown(data, allowed, "research plan")
    question = _string(data.get("question"), "question", required=True)
    wiki = _string(data.get("wiki"), "wiki", required=True)
    gap_id = _string(data.get("gap_id"), "gap_id", required=True)
    job_id = _string(data.get("job_id", ""), "job_id")
    if not job_id:
        job_id = _hash_body({"wiki": wiki, "gap_id": gap_id, "question": question})
    policy = _string(data.get("policy_digest", ""), "policy_digest")
    card = _string(data.get("card_digest", ""), "card_digest")
    access = _string(data.get("access_digest", ""), "access_digest")
    if not policy or not card or not access:
        raise ResearchError("policy, card, and access digests are required")

    def strings(name: str) -> tuple[str, ...]:
        value = data.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ResearchError(f"{name} must be a list of strings")
        return tuple(value)

    caps = _object(data.get("capabilities", {}), "capabilities")
    if set(caps) - {"web", "transcript", "paid", "credentials"} or any(
        not isinstance(v, bool) for v in caps.values()
    ):
        raise ResearchError("capabilities are invalid")
    caps = {k: bool(caps.get(k, False)) for k in ("web", "transcript", "paid", "credentials")}
    ceilings = _object(data.get("ceilings", {}), "ceilings")
    if not ceilings or any(
        not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in ceilings.values()
    ):
        raise ResearchError("ceilings must contain non-negative integer limits")
    created = _iso(str(data.get("created", today or "")))
    body = {
        "job_id": job_id,
        "wiki": wiki,
        "gap_id": gap_id,
        "question": question,
        "must_not_answer": list(strings("must_not_answer")),
        "required_claims": list(strings("required_claims")),
        "inclusion": list(strings("inclusion")),
        "exclusion": list(strings("exclusion")),
        "capabilities": caps,
        "ceilings": dict(sorted((str(k), int(v)) for k, v in ceilings.items())),
        "privacy_class": _string(data.get("privacy_class", ""), "privacy_class"),
        "change_envelope": _string(data.get("change_envelope", "approval"), "change_envelope"),
        "policy_digest": policy,
        "card_digest": card,
        "access_digest": access,
        "request_hash": _string(data.get("request_hash", ""), "request_hash"),
        "created": created,
    }
    plan_id = _hash_body(body)
    if data.get("plan_id") not in (None, plan_id):
        raise ReplayConflict("plan_id does not match the plan body")
    return ResearchPlan(
        plan_id,
        job_id,
        wiki,
        gap_id,
        question,
        strings("must_not_answer"),
        strings("required_claims"),
        strings("inclusion"),
        strings("exclusion"),
        caps,
        cast(dict[str, int], body["ceilings"]),
        str(body["privacy_class"]),
        str(body["change_envelope"]),
        policy,
        card,
        access,
        str(body["request_hash"]),
        created,
    )


class ResearchStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, rel: Path) -> Path:
        return resolve_contained(self.root, rel)

    def save_plan(self, plan: ResearchPlan) -> Path:
        path = self._path(PLANS_DIR / f"{plan.plan_id}.json")
        if (
            path.is_file()
            and path.read_text(encoding="utf-8")
            != json.dumps(plan.to_data(), indent=2, sort_keys=True) + "\n"
        ):
            raise ImmutableResearchError("research plan is immutable")
        return atomic_write(
            self.root,
            PLANS_DIR / f"{plan.plan_id}.json",
            json.dumps(plan.to_data(), indent=2, sort_keys=True) + "\n",
        )

    def load_plan(self, plan_id: str) -> ResearchPlan:
        """Read back the frozen plan a job is bound to."""
        path = self._path(PLANS_DIR / f"{plan_id}.json")
        if not path.is_file():
            raise ResearchNotFound(f"research plan not found: {plan_id}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ResearchError("research plan is not valid JSON") from exc
        if not isinstance(data, dict) or data.get("schema") != PLAN_SCHEMA:
            raise ResearchError("research plan is not a typed plan document")
        plan = make_plan({k: v for k, v in data.items() if k not in ("schema", "plan_id")})
        if plan.plan_id != plan_id:
            raise ResearchError("stored research plan does not match its identifier")
        return plan

    def _events(self) -> list[dict[str, Any]]:
        path = self._path(JOBS_PATH)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchError("invalid research job journal entry") from exc
            if not isinstance(value, dict):
                raise ResearchError("invalid research job journal entry")
            events.append(_validated_event(value))
        _validate_event_history(events)
        return events

    def jobs(self) -> list[JobView]:
        latest: dict[str, JobView] = {}
        for event in self._events():
            latest[str(event["job_id"])] = _job_from_data(event)
        return [latest[k] for k in sorted(latest)]

    def get(self, job_id: str) -> JobView:
        for job in self.jobs():
            if job.job_id == job_id:
                return job
        raise ResearchNotFound(f"research job not found: {job_id}")

    def transition(
        self,
        job_id: str,
        to_state: str,
        *,
        plan_id: str,
        gap_id: str,
        attempt_id: str,
        expected_state: str | None = None,
        reason: str = "",
        artifact_ids: list[str] | None = None,
        contradiction_ids: list[str] | None = None,
        policy_digest: str = "",
        card_digest: str = "",
        access_digest: str = "",
        today: str | None = None,
        event_id: str | None = None,
    ) -> JobView:
        if to_state not in TERMINAL:
            raise InvalidResearchTransition(f"unknown research state: {to_state}")
        try:
            current = self.get(job_id)
        except ResearchNotFound:
            if expected_state not in (None, "", "gap-open"):
                raise InvalidResearchTransition("expected state does not create a job") from None
            current = None
        if current is not None:
            current_contradictions = list(current.contradiction_ids)
            effective_contradictions = sorted(
                set(contradiction_ids if contradiction_ids is not None else current_contradictions)
            )
            replay_payload = _event_payload(
                job_id,
                to_state,
                plan_id,
                gap_id,
                attempt_id,
                reason,
                artifact_ids or [],
                effective_contradictions,
                policy_digest or current.policy_digest,
                card_digest or current.card_digest,
                access_digest or current.access_digest,
            )
            if current.event_id == _hash_body(replay_payload):
                return current
            if expected_state and current.state != expected_state:
                raise InvalidResearchTransition(f"expected {expected_state}, found {current.state}")
            resuming_cancelled = (
                current.state == "cancelled"
                and to_state == "gap-open"
                and attempt_id != current.attempt_id
            )
            if resuming_cancelled:
                effective_contradictions = []
            if current.state in TERMINAL_STATES and not resuming_cancelled:
                candidate = _event_payload(
                    job_id,
                    to_state,
                    plan_id,
                    gap_id,
                    attempt_id,
                    reason,
                    artifact_ids or [],
                    effective_contradictions,
                    policy_digest or current.policy_digest,
                    card_digest or current.card_digest,
                    access_digest or current.access_digest,
                )
                if current.event_id == _hash_body(candidate):
                    return current
                raise ImmutableResearchError("terminal research facts are immutable")
            if current.state == to_state and not resuming_cancelled:
                raise ReplayConflict("divergent replay for the current state")
            if resuming_cancelled:
                policy_digest = policy_digest or current.policy_digest
                card_digest = card_digest or current.card_digest
                access_digest = access_digest or current.access_digest
                # A resumed attempt is a new proof, not a rewrite of the
                # cancelled terminal fact.
                pass
            if not resuming_cancelled and to_state not in TRANSITIONS.get(
                current.state, frozenset()
            ):
                raise InvalidResearchTransition(f"cannot transition {current.state} to {to_state}")
            if current.state == "accepting" and to_state == "extracting" and (
                sorted(set(artifact_ids or [])) != sorted(current.artifact_ids)
                or effective_contradictions != sorted(current.contradiction_ids)
            ):
                raise ReplayConflict("accepted artifact set is immutable")
            if current.attempt_id != attempt_id and not resuming_cancelled:
                raise ReplayConflict("attempt identity does not match current job")
            policy_digest = policy_digest or current.policy_digest
            card_digest = card_digest or current.card_digest
            access_digest = access_digest or current.access_digest
        else:
            effective_contradictions = sorted(set(contradiction_ids or []))
        if to_state in TERMINAL_STATES and not attempt_id:
            raise ResearchError("terminal state requires attempt_id")
        payload = _event_payload(
            job_id,
            to_state,
            plan_id,
            gap_id,
            attempt_id,
            reason,
            artifact_ids or [],
            effective_contradictions,
            policy_digest,
            card_digest,
            access_digest,
        )
        computed_event = _hash_body(payload)
        if event_id is not None and event_id != computed_event:
            raise ReplayConflict("event_id does not match transition")
        event = {
            "schema": JOB_SCHEMA,
            **payload,
            "event_id": computed_event,
            "updated": _iso(today),
        }
        existing = self._events()
        if existing and existing[-1].get("event_id") == computed_event:
            return _job_from_data(existing[-1])
        atomic_write(
            self.root,
            JOBS_PATH,
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in [*existing, event]),
            durable=True,
        )
        append_audit(
            self.root,
            "research-transition",
            {
                "job_id": job_id,
                "attempt_id": attempt_id,
                "state": to_state,
                "event_id": computed_event,
            },
        )
        return _job_from_data(event)

    def start(self, plan: ResearchPlan, *, today: str | None = None) -> JobView:
        attempt_id = _hash_body({"job_id": plan.job_id, "plan_id": plan.plan_id, "attempt": 1})
        try:
            current = self.get(plan.job_id)
            if (
                current.plan_id != plan.plan_id
                or current.policy_digest != plan.policy_digest
                or current.card_digest != plan.card_digest
                or current.access_digest != plan.access_digest
            ):
                raise ResearchDrift("plan or bound policy/card/access digest drift requires replan")
            return current
        except ResearchNotFound:
            pass
        return self.transition(
            plan.job_id,
            "gap-open",
            plan_id=plan.plan_id,
            gap_id=plan.gap_id,
            attempt_id=attempt_id,
            expected_state="gap-open",
            policy_digest=plan.policy_digest,
            card_digest=plan.card_digest,
            access_digest=plan.access_digest,
            today=today,
        )

    def new_attempt(self, plan: ResearchPlan, *, today: str | None = None) -> JobView:
        old = self.get(plan.job_id)
        attempt_no = (
            sum(
                1
                for event in self._events()
                if event.get("job_id") == plan.job_id and event.get("state") == "gap-open"
            )
            + 1
        )
        attempt = _hash_body(
            {"job_id": plan.job_id, "plan_id": plan.plan_id, "attempt": attempt_no}
        )
        return self.transition(
            plan.job_id,
            "gap-open",
            plan_id=plan.plan_id,
            gap_id=plan.gap_id,
            attempt_id=attempt,
            expected_state=old.state,
            policy_digest=plan.policy_digest,
            card_digest=plan.card_digest,
            access_digest=plan.access_digest,
            today=today,
        )

    def save_immutable(self, kind: str, identifier: str, value: Mapping[str, Any]) -> Path:
        # Only the artifact's own identity field is excluded from its hash. A
        # packet_id inside an outcome is content, and dropping it would refuse
        # every outcome the lane can produce.
        spec = {
            PACKET_SCHEMA: (PACKETS_DIR, "packet_id"),
            OUTCOME_SCHEMA: (OUTCOMES_DIR, "outcome_id"),
        }.get(kind)
        if spec is None:
            raise ResearchError("unknown immutable research artifact")
        directory, identity_field = spec
        artifact = dict(value)
        artifact.pop("schema", None)
        artifact.pop(identity_field, None)
        expected = content_hash(_stable(artifact))
        if identifier != expected:
            raise ResearchError("artifact id does not match content")
        payload = {"schema": kind, **dict(value)}
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        path = self._path(directory / f"{identifier}.json")
        if path.is_file() and path.read_text(encoding="utf-8") != text:
            raise ImmutableResearchError("research artifact is immutable")
        return atomic_write(self.root, directory / f"{identifier}.json", text)

    def save_packet(
        self, packet: Mapping[str, Any], resolver: PacketFactResolver | None = None
    ) -> Path:
        normalized = make_packet(packet, resolver)
        return self.save_immutable(PACKET_SCHEMA, str(normalized["packet_id"]), normalized)

    def save_outcome(self, outcome: ResearchOutcome | Mapping[str, Any]) -> Path:
        normalized = outcome if isinstance(outcome, ResearchOutcome) else make_outcome(outcome)
        value = normalized.to_data() if isinstance(normalized, ResearchOutcome) else normalized
        return self.save_immutable(OUTCOME_SCHEMA, str(value["outcome_id"]), value)

    def assert_no_drift(
        self, job: JobView, *, policy_digest: str, card_digest: str, access_digest: str
    ) -> None:
        if (job.policy_digest, job.card_digest, job.access_digest) != (
            policy_digest,
            card_digest,
            access_digest,
        ):
            raise ResearchDrift("policy, card, access, or plan drift requires replan")


def _event_payload(
    job_id: str,
    state: str,
    plan_id: str,
    gap_id: str,
    attempt_id: str,
    reason: str,
    artifact_ids: list[str],
    contradiction_ids: list[str],
    policy_digest: str,
    card_digest: str,
    access_digest: str,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "state": state,
        "plan_id": plan_id,
        "gap_id": gap_id,
        "policy_digest": policy_digest,
        "card_digest": card_digest,
        "access_digest": access_digest,
        "reason": reason,
        "artifact_ids": sorted(set(artifact_ids)),
        "contradiction_ids": sorted(set(contradiction_ids)),
    }


def _validated_event(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "schema",
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
        "artifact_ids",
        "contradiction_ids",
        "updated",
    }
    if set(value) != fields or value.get("schema") != JOB_SCHEMA:
        raise ResearchError("invalid research job journal entry")
    names = ("job_id", "attempt_id", "state", "plan_id", "gap_id")
    if any(not isinstance(value.get(name), str) or not value[name] for name in names):
        raise ResearchError("invalid research job journal entry")
    for name in ("policy_digest", "card_digest", "access_digest", "reason", "event_id", "updated"):
        if not isinstance(value.get(name), str):
            raise ResearchError("invalid research job journal entry")
    if value["state"] not in TERMINAL or not value["event_id"]:
        raise ResearchError("invalid research job journal entry")
    if not isinstance(value.get("artifact_ids"), list) or not all(
        isinstance(identifier, str) for identifier in value["artifact_ids"]
    ):
        raise ResearchError("invalid research job journal entry")
    if not isinstance(value.get("contradiction_ids"), list) or not all(
        isinstance(identifier, str) for identifier in value["contradiction_ids"]
    ):
        raise ResearchError("invalid research job journal entry")
    try:
        if _iso(value["updated"]) != value["updated"]:
            raise ResearchError("invalid research job journal entry")
    except ResearchError as error:
        raise ResearchError("invalid research job journal entry") from error
    payload = _event_payload(
        value["job_id"],
        value["state"],
        value["plan_id"],
        value["gap_id"],
        value["attempt_id"],
        value["reason"],
        value["artifact_ids"],
        value["contradiction_ids"],
        value["policy_digest"],
        value["card_digest"],
        value["access_digest"],
    )
    if (
        value["artifact_ids"] != payload["artifact_ids"]
        or value["contradiction_ids"] != payload["contradiction_ids"]
        or value["event_id"] != _hash_body(payload)
    ):
        raise ResearchError("invalid research job journal entry")
    return dict(value)


def _validate_event_history(events: list[Mapping[str, Any]]) -> None:
    latest: dict[str, Mapping[str, Any]] = {}
    for event in events:
        job_id = str(event["job_id"])
        current = latest.get(job_id)
        if current is None:
            if event["state"] != "gap-open":
                raise ResearchError("invalid research job journal transition")
            latest[job_id] = event
            continue
        resumed = (
            current["state"] == "cancelled"
            and event["state"] == "gap-open"
            and event["attempt_id"] != current["attempt_id"]
        )
        if resumed:
            latest[job_id] = event
            continue
        if current["state"] in TERMINAL_STATES:
            raise ResearchError("invalid research job journal transition")
        if event["attempt_id"] != current["attempt_id"] or any(
            event[name] != current[name]
            for name in ("plan_id", "gap_id", "policy_digest", "card_digest", "access_digest")
        ):
            raise ResearchError("invalid research job journal transition")
        if event["contradiction_ids"] != current["contradiction_ids"] and not (
            current["state"] == "retrieving" and event["state"] == "accepting"
        ):
            raise ResearchError("invalid research job journal transition")
        if current["state"] == "accepting" and event["state"] == "extracting" and (
            event["artifact_ids"] != current["artifact_ids"]
            or event["contradiction_ids"] != current["contradiction_ids"]
        ):
            raise ResearchError("invalid research job journal transition")
        if event["state"] not in TRANSITIONS.get(str(current["state"]), frozenset()):
            raise ResearchError("invalid research job journal transition")
        latest[job_id] = event


def _job_from_data(value: Mapping[str, Any]) -> JobView:
    return JobView(
        str(value["job_id"]),
        str(value["attempt_id"]),
        str(value["state"]),
        str(value["plan_id"]),
        str(value["gap_id"]),
        str(value.get("policy_digest", "")),
        str(value.get("card_digest", "")),
        str(value.get("access_digest", "")),
        str(value["event_id"]),
        str(value.get("reason", "")),
        tuple(str(x) for x in value.get("artifact_ids", [])),
        tuple(str(x) for x in value.get("contradiction_ids", [])),
        str(value.get("updated", "")),
    )


def make_discovery_receipt(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a host discovery receipt.  Candidate identifiers stay opaque."""
    _unknown(data, {"job_id", "attempt_id", "candidates", "usage", "note"}, "discovery receipt")
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list) or not all(isinstance(x, str) and x for x in candidates):
        raise ResearchError("discovery candidates must be non-empty opaque references")
    usage = _object(data.get("usage", {}), "usage")
    if not all(isinstance(key, str) and key for key in usage):
        raise ResearchError("discovery usage must be keyed by ceiling name")
    return {
        "job_id": _string(data.get("job_id"), "job_id", required=True),
        "attempt_id": _string(data.get("attempt_id"), "attempt_id", required=True),
        "candidates": sorted(set(candidates)),
        "usage": {str(key): value for key, value in usage.items()},
        "note": _string(data.get("note", ""), "note"),
    }


def make_claims_receipt(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the job envelope of a claim receipt; claim bodies are typed elsewhere."""
    _unknown(data, {"job_id", "attempt_id", "claims", "contradictions"}, "claims receipt")
    claims = data.get("claims", [])
    contradictions = data.get("contradictions", [])
    if not isinstance(claims, list) or not claims or not all(isinstance(x, dict) for x in claims):
        raise ResearchError("claims must be a non-empty list of claim objects")
    if not isinstance(contradictions, list) or not all(isinstance(x, dict) for x in contradictions):
        raise ResearchError("contradictions must be a list of contradiction objects")
    return {
        "job_id": _string(data.get("job_id"), "job_id", required=True),
        "attempt_id": _string(data.get("attempt_id"), "attempt_id", required=True),
        "claims": [dict(item) for item in claims],
        "contradictions": [dict(item) for item in contradictions],
    }


def _packet_body(packet: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema",
        "packet_id",
        "job_id",
        "attempt_id",
        "claims",
        "contradictions",
        "interpretation",
        "answerability",
        "confidence",
        "provenance",
    }
    _unknown(packet, allowed, "research packet")
    claims = packet.get("claims", [])
    contradictions = packet.get("contradictions", [])
    if not isinstance(claims, list) or not all(isinstance(x, str) for x in claims):
        raise ResearchError("packet claims must be opaque references")
    if not isinstance(contradictions, list) or not all(isinstance(x, str) for x in contradictions):
        raise ResearchError("packet contradictions must be opaque references")
    return {
        "job_id": _string(packet.get("job_id"), "job_id", required=True),
        "attempt_id": _string(packet.get("attempt_id"), "attempt_id", required=True),
        "claims": list(claims),
        "contradictions": list(contradictions),
        "interpretation": _string(packet.get("interpretation", ""), "interpretation"),
        "provenance": list(packet.get("provenance", []))
        if isinstance(packet.get("provenance", []), list)
        else [],
    }


def _score(value: float | str) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def derive_packet_facts(
    claims: list[str], contradictions: list[str], facts: PacketFactResolver | None
) -> tuple[float | str, dict[str, Any]]:
    """Compute packet confidence and answerability from the resolved records.

    A packet never exceeds its weakest relied-upon claim, an unknown claim
    keeps the packet unknown, and any unresolved contradiction refuses
    answerability outright.
    """
    if (claims or contradictions) and facts is None:
        raise ResearchError("packet claim and contradiction references require a resolver")
    scores = [facts.claim_confidence(claim) for claim in claims] if facts is not None else []
    unresolved = (
        [item for item in contradictions if facts.contradiction_is_unresolved(item)]
        if facts is not None
        else []
    )
    resolved = [_score(value) for value in scores]
    aggregate = answer_confidence([Confidence(score) for score in resolved])
    if unresolved:
        verdict = "contradicted"
    elif not resolved or not aggregate.meets_floor:
        verdict = "insufficient"
    else:
        verdict = "supported"
    return aggregate.render(), {
        "verdict": verdict,
        "claims": len(resolved),
        "unknown_claims": sum(1 for score in resolved if score is None),
        "below_floor_claims": sum(
            1 for score in resolved if score is not None and score < RELIANCE_FLOOR
        ),
        "unresolved_contradictions": len(unresolved),
        "reliance_floor": RELIANCE_FLOOR,
    }


def packet_content_id(packet: Mapping[str, Any]) -> str:
    if packet.get("schema") != PACKET_SCHEMA:
        raise ResearchError("packet schema is invalid")
    _packet_body(packet)
    confidence = packet.get("confidence")
    if confidence != "unknown" and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        raise ResearchError("packet confidence is invalid")
    answerability = packet.get("answerability")
    expected_answerability = {
        "verdict",
        "claims",
        "unknown_claims",
        "below_floor_claims",
        "unresolved_contradictions",
        "reliance_floor",
    }
    if not isinstance(answerability, dict) or set(answerability) != expected_answerability:
        raise ResearchError("packet answerability is invalid")
    if answerability.get("verdict") not in {"supported", "insufficient", "contradicted"}:
        raise ResearchError("packet answerability is invalid")
    if any(
        isinstance(answerability.get(name), bool)
        or not isinstance(answerability.get(name), int)
        or answerability[name] < 0
        for name in (
            "claims",
            "unknown_claims",
            "below_floor_claims",
            "unresolved_contradictions",
        )
    ) or isinstance(answerability.get("reliance_floor"), bool) or not isinstance(
        answerability.get("reliance_floor"), (int, float)
    ):
        raise ResearchError("packet answerability is invalid")
    return _hash_body({key: value for key, value in packet.items() if key not in {"schema", "packet_id"}})


def make_packet(
    packet: Mapping[str, Any], resolver: PacketFactResolver | None = None
) -> dict[str, Any]:
    if packet.get("schema") not in (None, PACKET_SCHEMA):
        raise ResearchError("packet schema is invalid")
    body = _packet_body(packet)
    if resolver is not None:
        for claim in body["claims"]:
            if not resolver.resolve("claim", claim):
                raise ResearchError("packet contains an unresolved claim reference")
        for contradiction in body["contradictions"]:
            if not resolver.resolve("contradiction", contradiction):
                raise ResearchError("packet contains an unresolved contradiction reference")
    confidence, answerability = derive_packet_facts(
        body["claims"], body["contradictions"], resolver
    )
    for key, derived in (("confidence", confidence), ("answerability", answerability)):
        supplied = packet.get(key)
        if supplied is not None and supplied != derived:
            raise ResearchError(
                f"packet {key} is derived from the resolved claims and cannot be supplied"
            )
    body["confidence"] = confidence
    body["answerability"] = answerability
    packet_id = packet_content_id({"schema": PACKET_SCHEMA, **body})
    if packet.get("packet_id") not in (None, packet_id):
        raise ReplayConflict("packet_id does not match content")
    return {"schema": PACKET_SCHEMA, "packet_id": packet_id, **body}


def compile_packet_proposal(
    root: Path,
    packet: Mapping[str, Any],
    *,
    source: str = "research",
    destination: str = "uncategorized",
    knowledge_type: str = "guidance",
    today: str | None = None,
    resolver: PacketFactResolver | None = None,
) -> tuple[str, Path]:
    if knowledge_type not in KNOWLEDGE_TYPES:
        raise ResearchError("unknown proposal knowledge type")
    normalized = make_packet(packet, resolver)
    packet_id = str(normalized["packet_id"])
    interpretation = str(normalized.get("interpretation", "")).strip()
    if not interpretation:
        raise ResearchError("packet interpretation is empty")
    body = f"# Research synthesis\n\n{interpretation}\n\n<!-- research-packet:{packet_id} -->\n"
    proposal_id = content_hash(body.strip())
    document = Document(
        frontmatter={
            "megamind": "proposal",
            "id": proposal_id,
            "type": knowledge_type,
            "status": "proposed",
            "source": source,
            "captured": _iso(today),
            "suggested_destination": destination,
            "provenance": [
                f"research-packet {packet_id}",
                *[f"claim {ref}" for ref in normalized["claims"]],
            ],
        },
        body=f"\n{body}\n",
    )
    from .capture import proposal_path

    path = proposal_path(root, proposal_id)
    text = document.render()
    if path.is_file() and path.read_text(encoding="utf-8") != text:
        raise ImmutableResearchError("proposal id collision")
    atomic_write(root, Path(MEGAMIND_DIR) / "proposals" / f"{proposal_id}.md", text)
    append_audit(
        root,
        "research-packet-compile",
        {"packet_id": packet_id, "proposal_id": proposal_id, "destination": destination},
    )
    return proposal_id, path


@dataclass(frozen=True)
class ResearchOutcome:
    outcome_id: str
    job_id: str
    attempt_id: str
    state: str
    plan_id: str
    packet_id: str
    reason: str
    artifact_ids: tuple[str, ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": OUTCOME_SCHEMA,
            "outcome_id": self.outcome_id,
            "job_id": self.job_id,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "plan_id": self.plan_id,
            "packet_id": self.packet_id,
            "reason": self.reason,
            "artifact_ids": list(self.artifact_ids),
        }


def make_outcome(data: Mapping[str, Any]) -> ResearchOutcome:
    allowed = {"job_id", "attempt_id", "state", "plan_id", "packet_id", "reason", "artifact_ids"}
    _unknown(data, allowed | {"outcome_id"}, "research outcome")
    state = _string(data.get("state"), "state", required=True)
    if state not in TERMINAL_STATES:
        raise ResearchError("outcome state must be terminal")
    ids = data.get("artifact_ids", [])
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise ResearchError("outcome artifact_ids are invalid")
    body = {
        "job_id": _string(data.get("job_id"), "job_id", required=True),
        "attempt_id": _string(data.get("attempt_id"), "attempt_id", required=True),
        "state": state,
        "plan_id": _string(data.get("plan_id"), "plan_id", required=True),
        "packet_id": _string(data.get("packet_id", ""), "packet_id"),
        "reason": _string(data.get("reason", ""), "reason"),
        "artifact_ids": sorted(set(ids)),
    }
    outcome_id = _hash_body(body)
    if data.get("outcome_id") not in (None, outcome_id):
        raise ReplayConflict("outcome_id does not match content")
    return ResearchOutcome(
        outcome_id,
        str(body["job_id"]),
        str(body["attempt_id"]),
        state,
        str(body["plan_id"]),
        str(body["packet_id"]),
        str(body["reason"]),
        tuple(cast(list[str], body["artifact_ids"])),
    )
