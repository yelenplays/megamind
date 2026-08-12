"""Model-access policy: derive, clamp, and audit per-wiki access decisions.

Every wiki card carries a sensitivity (who may ever see it), per-axis model
access (what a local or cloud model context may receive: full, digest-only, or
none), a routing mode (full or pointer), and a catalog visibility. Any of these
may be unset; unset, unknown, or contradictory values derive to the most
restrictive honest default. No host, router, or later semantic layer may widen
what this module computes: explicit card values that exceed a ceiling are
clamped down and reported as policy findings, never honored.

Derivation order for each access axis:

1. an explicit card value, clamped by every applicable ceiling;
2. otherwise the default implied by the legacy privacy class, narrowed by the
   default the sensitivity implies: a company-private or collaborative wiki
   defaults to cloud none until its card states an explicit cloud policy;
3. the explicit sensitivity is reconciled with the privacy-derived
   sensitivity, and the more restrictive classification wins;
4. sensitivity ceilings key on that effective sensitivity: personal-local
   caps cloud at digest-only and unclassified caps cloud at none;
5. privacy ceilings independently preserve legacy boundaries: personal-local
   caps cloud at digest-only, digest-only caps both axes at digest-only, and
   pointer-only forces both axes to none and the mode to pointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import PRIVACY_SENSITIVITY
from .registry import WikiEntry

_ACCESS_RANK = {"none": 0, "digest-only": 1, "full": 2}
_SENSITIVITY_RANK = {
    "public-reference": 0,
    "company-private": 1,
    "collaborative": 1,
    "personal-local": 2,
    "unclassified": 3,
}

# (local, cloud) defaults implied by the legacy privacy class. "" privacy is
# the canonical wiki-card case: locally usable, cloud-restrictive.
_PRIVACY_DEFAULTS: dict[str, tuple[str, str]] = {
    "public-reference": ("full", "full"),
    "company-private": ("full", "none"),
    "personal-local": ("full", "digest-only"),
    "digest-only": ("digest-only", "digest-only"),
    "pointer-only": ("none", "none"),
    "": ("full", "none"),
}

_PRIVACY_LOCAL_CEILING: dict[str, str] = {
    "digest-only": "digest-only",
    "pointer-only": "none",
}

_PRIVACY_CLOUD_CEILING: dict[str, str] = {
    "personal-local": "digest-only",
    "digest-only": "digest-only",
    "pointer-only": "none",
}

_SENSITIVITY_CLOUD_CEILING: dict[str, str] = {
    "personal-local": "digest-only",
    "unclassified": "none",
}

# Cloud defaults a sensitivity implies while the card states no explicit cloud
# policy. These narrow the privacy default; an explicit card value still wins
# (that is what "requires an explicit policy" means) and is only cut down by
# the ceilings above.
_SENSITIVITY_CLOUD_DEFAULT: dict[str, str] = {
    "company-private": "none",
    "collaborative": "none",
}


@dataclass
class EffectivePolicy:
    """The access posture every consumer must honor, after derivation and clamping."""

    sensitivity: str
    local: str
    cloud: str
    routing_mode: str
    catalog_visibility: str
    derived: list[str] = field(default_factory=list)

    def access_for(self, model_class: str) -> str:
        return self.local if model_class == "local" else self.cloud


@dataclass
class PolicyFinding:
    severity: str  # "error" | "warning"
    message: str


def narrower(first: str, second: str) -> str:
    return first if _ACCESS_RANK[first] <= _ACCESS_RANK[second] else second


def effective_policy(wiki: WikiEntry) -> EffectivePolicy:
    """Compute the binding access posture for a wiki card."""
    derived: list[str] = []

    privacy_sensitivity = PRIVACY_SENSITIVITY.get(wiki.privacy)
    if wiki.sensitivity:
        sensitivity = wiki.sensitivity
        if (
            privacy_sensitivity is not None
            and _SENSITIVITY_RANK[privacy_sensitivity] > _SENSITIVITY_RANK[sensitivity]
        ):
            sensitivity = privacy_sensitivity
    else:
        sensitivity = privacy_sensitivity or "unclassified"
        derived.append("sensitivity")

    default_local, default_cloud = _PRIVACY_DEFAULTS[wiki.privacy]
    default_cloud = narrower(default_cloud, _SENSITIVITY_CLOUD_DEFAULT.get(sensitivity, "full"))
    local = wiki.model_access.local or default_local
    cloud = wiki.model_access.cloud or default_cloud
    if not wiki.model_access.local:
        derived.append("local")
    if not wiki.model_access.cloud:
        derived.append("cloud")

    sensitivity_ceiling = _SENSITIVITY_CLOUD_CEILING.get(sensitivity)
    privacy_ceiling_governs = "sensitivity" in derived and wiki.privacy in _PRIVACY_LOCAL_CEILING
    if sensitivity_ceiling is not None and not privacy_ceiling_governs:
        cloud = narrower(cloud, sensitivity_ceiling)
    if wiki.privacy in _PRIVACY_LOCAL_CEILING:
        local = narrower(local, _PRIVACY_LOCAL_CEILING[wiki.privacy])
    if wiki.privacy in _PRIVACY_CLOUD_CEILING:
        cloud = narrower(cloud, _PRIVACY_CLOUD_CEILING[wiki.privacy])

    if wiki.routing_mode:
        routing_mode = wiki.routing_mode
    else:
        routing_mode = "pointer" if wiki.privacy == "pointer-only" else "full"
        derived.append("routing_mode")
    if wiki.privacy == "pointer-only":
        routing_mode = "pointer"

    if wiki.catalog_visibility:
        visibility = wiki.catalog_visibility
    else:
        visibility = (
            "redacted"
            if wiki.privacy == "personal-local" or sensitivity == "personal-local"
            else "full"
        )
        derived.append("catalog_visibility")

    return EffectivePolicy(
        sensitivity=sensitivity,
        local=local,
        cloud=cloud,
        routing_mode=routing_mode,
        catalog_visibility=visibility,
        derived=derived,
    )


def policy_findings(wiki: WikiEntry) -> list[PolicyFinding]:
    """Cross-field access invariants. Errors are clamped contradictions."""
    findings: list[PolicyFinding] = []
    policy = effective_policy(wiki)
    name = wiki.name

    if wiki.sensitivity and wiki.sensitivity != policy.sensitivity:
        findings.append(
            PolicyFinding(
                "error",
                f"wiki {name} declares sensitivity '{wiki.sensitivity}' but privacy "
                f"only allows '{policy.sensitivity}'; the restrictive value wins",
            )
        )

    for axis in ("local", "cloud"):
        explicit = getattr(wiki.model_access, axis)
        if explicit and explicit != getattr(policy, axis):
            findings.append(
                PolicyFinding(
                    "error",
                    f"wiki {name} declares {axis} access '{explicit}' but its "
                    f"sensitivity/privacy only allows '{getattr(policy, axis)}'; "
                    "the restrictive value wins",
                )
            )
    if wiki.privacy == "pointer-only" and wiki.routing_mode == "full":
        findings.append(
            PolicyFinding(
                "error",
                f"wiki {name} is pointer-only but declares routing_mode 'full'; pointer mode wins",
            )
        )
    if policy.routing_mode == "pointer" and wiki.digest:
        findings.append(
            PolicyFinding(
                "warning",
                f"wiki {name} routes as pointer but sets a digest; pointer wikis expose no digest",
            )
        )
    if policy.sensitivity in ("company-private", "collaborative") and not wiki.model_access.cloud:
        findings.append(
            PolicyFinding(
                "warning",
                f"wiki {name} is {policy.sensitivity} without an explicit cloud access "
                "policy; cloud access defaults to none",
            )
        )
    if policy.sensitivity == "unclassified" and wiki.privacy not in (
        "digest-only",
        "pointer-only",
    ):
        findings.append(
            PolicyFinding(
                "warning",
                f"wiki {name} has no sensitivity classification; cloud access defaults to none",
            )
        )
    if wiki.source_policy.allowlist and wiki.source_policy.allowlist_status == "none":
        findings.append(
            PolicyFinding(
                "warning",
                f"wiki {name} points at an allowlist but its status is 'none'",
            )
        )
    return findings
