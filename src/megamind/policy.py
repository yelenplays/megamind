"""Strict per-wiki research policy records.

A research policy is a local, versioned permission document.  It is never a
source-quality verdict by itself: it only describes which deterministic facts
may be admitted for a wiki.  Missing policies are deliberately represented as
``None`` and mean that research is denied.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .confidence import QUALITY_BASE, SOURCE_QUALITIES

RESEARCH_POLICY_SCHEMA = "megamind/research-policy/v1"
CLAIM_TYPES = (
    "fact",
    "decision",
    "hypothesis",
    "procedure",
    "example",
    "guidance",
    "attributed-statement",
)
SOURCE_CLASSES = (
    "primary-literature",
    "systematic-review",
    "official-guidance",
    "practitioner",
    "video",
    "dataset",
)
VIDEO_QUALITIES = SOURCE_QUALITIES


class PolicyError(ValueError):
    """A research policy is malformed or not restrictive enough to parse."""

    code = "research_policy_invalid"


def _mapping(label: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError(f"{label} must be an object")
    return dict(value)


def _unknown(label: str, data: Mapping[str, object], allowed: set[str]) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise PolicyError(f"{label} has unknown field(s): {', '.join(extra)}")


def _str(label: str, value: object, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise PolicyError(f"{label} must be {'a non-empty ' if nonempty else 'a '}string")
    return value


def _strings(label: str, value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PolicyError(f"{label} must be a list of strings")
    return list(value)


@dataclass(frozen=True)
class ResearchTier:
    tier: int
    name: str
    quality: str
    matchers: tuple[dict[str, object], ...] = ()
    claim_types: tuple[str, ...] = CLAIM_TYPES
    sole_support: bool = False

    def to_data(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "name": self.name,
            "quality": self.quality,
            "matchers": [dict(item) for item in self.matchers],
            "claim_types": list(self.claim_types),
            "sole_support": self.sole_support,
        }


@dataclass(frozen=True)
class VideoPolicy:
    max_quality: str = "hypothesis"
    sole_support: bool = False
    claim_types: tuple[str, ...] = ("attributed-statement",)

    def to_data(self) -> dict[str, object]:
        return {
            "max_quality": self.max_quality,
            "sole_support": self.sole_support,
            "claim_types": list(self.claim_types),
        }


@dataclass(frozen=True)
class ResearchPolicy:
    """The validated, restrictive research-policy/v1 document."""

    tiers: tuple[ResearchTier, ...] = ()
    video: VideoPolicy = field(default_factory=VideoPolicy)
    quote_ceiling_chars: int = 400
    accepted_authorities: tuple[str, ...] = ()
    freshness_policy: dict[str, dict[str, object]] = field(default_factory=dict)
    research: str = "off"
    apply: str = "approval"
    max_sources_per_cycle: int = 20

    @property
    def schema(self) -> str:
        return RESEARCH_POLICY_SCHEMA

    @property
    def permitted(self) -> bool:
        return bool(self.tiers) and self.research != "off"

    def to_data(self) -> dict[str, object]:
        return {
            "schema": RESEARCH_POLICY_SCHEMA,
            "tiers": [tier.to_data() for tier in self.tiers],
            "video": self.video.to_data(),
            "quote_ceiling_chars": self.quote_ceiling_chars,
            "accepted_authorities": list(self.accepted_authorities),
            "freshness_policy": self.freshness_policy,
            "research": self.research,
            "apply": self.apply,
            "max_sources_per_cycle": self.max_sources_per_cycle,
        }

    @classmethod
    def from_data(cls, raw: object) -> ResearchPolicy:
        data = _mapping("research policy", raw)
        allowed = {
            "schema",
            "tiers",
            "video",
            "quote_ceiling_chars",
            "accepted_authorities",
            "freshness_policy",
            "research",
            "apply",
            "max_sources_per_cycle",
        }
        _unknown("research policy", data, allowed)
        if data.get("schema") != RESEARCH_POLICY_SCHEMA:
            raise PolicyError(f"research policy schema must be {RESEARCH_POLICY_SCHEMA}")
        tiers_raw = data.get("tiers")
        if not isinstance(tiers_raw, list):
            raise PolicyError("research policy tiers must be a list")
        tiers: list[ResearchTier] = []
        seen: set[int] = set()
        for index, item in enumerate(tiers_raw):
            tier_data = _mapping(f"research policy tiers[{index}]", item)
            _unknown(
                f"research policy tiers[{index}]",
                tier_data,
                {"tier", "name", "quality", "matchers", "claim_types", "sole_support"},
            )
            number = tier_data.get("tier")
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise PolicyError(f"research policy tiers[{index}] tier must be positive")
            if number in seen:
                raise PolicyError("research policy tiers must have unique tier numbers")
            seen.add(number)
            quality = _str(f"research policy tiers[{index}] quality", tier_data.get("quality"))
            if quality not in QUALITY_BASE:
                raise PolicyError(
                    f"research policy tier quality must be one of {', '.join(SOURCE_QUALITIES)}"
                )
            matchers_raw = tier_data.get("matchers", [])
            if not isinstance(matchers_raw, list):
                raise PolicyError(f"research policy tiers[{index}] matchers must be a list")
            matchers: list[dict[str, object]] = []
            for mindex, matcher in enumerate(matchers_raw):
                m = _mapping(f"research policy matcher {index}.{mindex}", matcher)
                if "kind" not in m or not isinstance(m["kind"], str) or not m["kind"]:
                    raise PolicyError("research policy matchers require a kind")
                # Matchers are structured facts.  Unknown matcher keys are
                # refused rather than silently becoming a future permission.
                _unknown(
                    f"research policy matcher {index}.{mindex}",
                    m,
                    {
                        "kind",
                        "registry",
                        "host",
                        "publisher",
                        "document_type",
                        "jurisdiction",
                        "max_tier",
                    },
                )
                matchers.append(m)
            claim_types = tuple(
                _strings(
                    f"research policy tiers[{index}] claim_types",
                    tier_data.get("claim_types", list(CLAIM_TYPES)),
                )
            )
            if any(item not in CLAIM_TYPES for item in claim_types):
                raise PolicyError("research policy tier has an unknown claim type")
            sole = tier_data.get("sole_support", False)
            if not isinstance(sole, bool):
                raise PolicyError("research policy tier sole_support must be boolean")
            tiers.append(
                ResearchTier(
                    number,
                    _str(
                        f"research policy tiers[{index}] name", tier_data.get("name"), nonempty=True
                    ),
                    quality,
                    tuple(matchers),
                    claim_types,
                    sole,
                )
            )
        video_data = _mapping("research policy video", data.get("video", {}))
        _unknown(
            "research policy video", video_data, {"max_quality", "sole_support", "claim_types"}
        )
        video_quality = _str(
            "research policy video max_quality", video_data.get("max_quality", "hypothesis")
        )
        if video_quality not in QUALITY_BASE:
            raise PolicyError("research policy video max_quality is invalid")
        video_sole = video_data.get("sole_support", False)
        if not isinstance(video_sole, bool):
            raise PolicyError("research policy video sole_support must be boolean")
        video_claims = tuple(
            _strings(
                "research policy video claim_types",
                video_data.get("claim_types", ["attributed-statement"]),
            )
        )
        if any(item not in CLAIM_TYPES for item in video_claims):
            raise PolicyError("research policy video has an unknown claim type")
        quote = data.get("quote_ceiling_chars", 400)
        max_sources = data.get("max_sources_per_cycle", 20)
        if isinstance(quote, bool) or not isinstance(quote, int) or quote <= 0:
            raise PolicyError("research policy quote_ceiling_chars must be positive")
        if isinstance(max_sources, bool) or not isinstance(max_sources, int) or max_sources <= 0:
            raise PolicyError("research policy max_sources_per_cycle must be positive")
        authorities = tuple(
            _strings("research policy accepted_authorities", data.get("accepted_authorities", []))
        )
        freshness = data.get("freshness_policy", {})
        if not isinstance(freshness, Mapping):
            raise PolicyError("research policy freshness_policy must be an object")
        normalized_freshness: dict[str, dict[str, object]] = {}
        for key, value in freshness.items():
            if not isinstance(key, str) or key not in CLAIM_TYPES:
                raise PolicyError("research policy freshness_policy has an unknown claim type")
            item = _mapping(f"research policy freshness_policy.{key}", value)
            _unknown(
                f"research policy freshness_policy.{key}", item, {"half_life_days", "expires_at"}
            )
            half = item.get("half_life_days")
            if half is not None and (
                isinstance(half, bool) or not isinstance(half, int) or half <= 0
            ):
                raise PolicyError("research policy freshness half_life_days must be positive")
            expires = item.get("expires_at", "")
            if not isinstance(expires, str):
                raise PolicyError("research policy freshness expires_at must be a string")
            normalized_freshness[key] = {"half_life_days": half, "expires_at": expires}
        research = _str("research policy research", data.get("research", "off"))
        apply = _str("research policy apply", data.get("apply", "approval"))
        if research not in {"off", "approval", "standing"} or apply not in {"approval", "standing"}:
            raise PolicyError("research policy research/apply has an invalid mode")
        return cls(
            tuple(sorted(tiers, key=lambda item: item.tier)),
            VideoPolicy(video_quality, video_sole, video_claims),
            quote,
            authorities,
            normalized_freshness,
            research,
            apply,
            max_sources,
        )


def policy_to_data(policy: ResearchPolicy | None) -> dict[str, object] | None:
    return None if policy is None else policy.to_data()


def policy_from_data(value: object) -> ResearchPolicy:
    return ResearchPolicy.from_data(value)


def tier_for_facts(
    policy: ResearchPolicy | None, facts: Mapping[str, object], claim_type: str
) -> ResearchTier | None:
    """Choose the first matching tier using only structured host facts.

    A missing policy, missing matcher, or ambiguous fact never widens access.
    The matcher implementation is intentionally small and deterministic; a
    host may supply facts, but it cannot supply a tier or quality verdict.
    """
    if policy is None or not policy.permitted or claim_type not in CLAIM_TYPES:
        return None
    for tier in policy.tiers:
        if claim_type not in tier.claim_types:
            continue
        if not tier.matchers:
            continue
        for matcher in tier.matchers:
            matched = True
            for key in ("registry", "host", "publisher", "document_type", "jurisdiction"):
                if key in matcher and facts.get(key) != matcher[key]:
                    matched = False
            if "max_tier" in matcher:
                current = facts.get("tier")
                maximum = matcher["max_tier"]
                if (
                    not isinstance(current, int)
                    or isinstance(current, bool)
                    or not isinstance(maximum, int)
                    or isinstance(maximum, bool)
                    or current > maximum
                ):
                    matched = False
            if matched:
                return tier
    return None


WEAKEST_QUALITY = SOURCE_QUALITIES[-1]


def clamp_quality(first: str, second: str) -> str:
    """The weaker of two quality labels; an unknown label clamps to the weakest.

    Quality ceilings compose in one direction only, so a per-class ceiling can
    narrow a tier's quality but a tier can never widen a ceiling.
    """
    if first not in QUALITY_BASE or second not in QUALITY_BASE:
        return WEAKEST_QUALITY
    return first if QUALITY_BASE[first] <= QUALITY_BASE[second] else second


def admitted_quality(
    policy: ResearchPolicy | None, tier: ResearchTier | None, source_class: str
) -> str:
    """The quality a wiki policy admits for a matched tier and source class."""
    if policy is None or tier is None:
        return ""
    if source_class == "video":
        return clamp_quality(tier.quality, policy.video.max_quality)
    return tier.quality


def admits_claim_type(policy: ResearchPolicy | None, source_class: str, claim_type: str) -> bool:
    """Whether a source class may carry a claim type at all under this policy."""
    if policy is None:
        return False
    if source_class == "video":
        return claim_type in policy.video.claim_types
    return claim_type in CLAIM_TYPES


def _iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def freshness_state(policy: ResearchPolicy | None, claim_type: str, as_of: str, today: str) -> str:
    """Derive a typed freshness state from the wiki policy and frozen dates.

    Freshness is a lookup over host-frozen dates, never a wall-clock read: an
    absent policy entry, an absent date, or an unparseable one stays ``unknown``
    so the confidence rubric keeps its restrictive cap instead of guessing.
    """
    entry = policy.freshness_policy.get(claim_type) if policy is not None else None
    today_date = _iso(today) if today else None
    if entry is None or today_date is None:
        return "unknown"
    expires_raw = entry.get("expires_at")
    expires = _iso(expires_raw) if isinstance(expires_raw, str) and expires_raw else None
    if isinstance(expires_raw, str) and expires_raw and expires is None:
        return "unknown"
    if expires is not None and today_date > expires:
        return "stale"
    half = entry.get("half_life_days")
    if isinstance(half, int) and not isinstance(half, bool):
        as_of_date = _iso(as_of) if as_of else None
        if as_of_date is None:
            return "unknown"
        return "stale" if (today_date - as_of_date).days > half else "fresh"
    return "fresh" if expires is not None else "unknown"
