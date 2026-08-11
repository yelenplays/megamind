"""Regenerate the frozen synthetic release fixture, byte for byte.

This script is the only source of the fixture bytes checked in under
``evals/fixtures/release-mini``. ``tests/test_evaluation.py`` regenerates into a
temporary directory and requires an exact byte match, so the fixture can never
drift away from its generator. All content is invented; nothing here is real
personal, company, or machine data.

The corpus deliberately exercises every surface the release benchmark gates on:
routing cards, indexes and exact pages, a digest-only wiki with mixed-language
content, a pointer-only wiki, a company-private wiki whose cloud access is
none, a provisional wiki that may be offered but never authorized, unindexed
canary pages that the ladder must never reach, and page sizes that make the
context budget bind.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from megamind.fsops import atomic_write_path

REGISTRY = {
    "version": 2,
    "budgets": {
        "max_candidates": 4,
        "max_context_chars": 400,
        "stale_days": 180,
        "micro_wiki_pages": 4,
        "top_level_topics": 3,
    },
    "wikis": [
        {
            "name": "ProductWiki",
            "path": "ProductWiki",
            "privacy": "public-reference",
            "description": "Synthetic product facts",
            "purpose": "Pricing, release, and onboarding facts",
            "keywords": ["product", "pricing", "release", "onboarding"],
            "triggers": ["pricing", "release", "onboarding"],
            "card": "ProductWiki/CARD.md",
            "index": "ProductWiki/INDEX.md",
            "model_access": {"local": "full", "cloud": "full"},
            "context_budget": {"max_candidates": 3, "max_context_chars": 500},
        },
        {
            "name": "OpsWiki",
            "path": "OpsWiki",
            "privacy": "company-private",
            "description": "Synthetic operations",
            "purpose": "Operations pricing controls",
            "keywords": ["operations", "pricing", "controls"],
            "triggers": ["pricing", "controls"],
            "card": "OpsWiki/CARD.md",
            "index": "OpsWiki/INDEX.md",
            "model_access": {"local": "full", "cloud": "none"},
            "context_budget": {"max_candidates": 2, "max_context_chars": 500},
        },
        {
            "name": "PersonalWiki",
            "path": "PersonalWiki",
            "privacy": "personal-local",
            "description": "Synthetic personal notes",
            "purpose": "Private routine and journal notes",
            "keywords": ["journal", "routine"],
            "triggers": ["journal", "routine"],
            "card": "PersonalWiki/CARD.md",
            "index": "PersonalWiki/INDEX.md",
            "model_access": {"local": "full", "cloud": "none"},
        },
        {
            "name": "DigestWiki",
            "path": "DigestWiki",
            "privacy": "digest-only",
            "description": "Synthetic digest",
            "purpose": "Research digest",
            "keywords": ["research", "study"],
            "triggers": ["research", "study"],
            "card": "DigestWiki/CARD.md",
            "digest": "DigestWiki/DIGEST.md",
            "index": "DigestWiki/INDEX.md",
            "model_access": {"local": "digest-only", "cloud": "digest-only"},
        },
        {
            "name": "ArchiveWiki",
            "path": "ArchiveWiki",
            "privacy": "pointer-only",
            "description": "Synthetic archive",
            "purpose": "Historical archive",
            "keywords": ["archive", "history"],
            "triggers": ["archive", "history"],
            "card": "ArchiveWiki/CARD.md",
            "routing_mode": "pointer",
            "model_access": {"local": "none", "cloud": "none"},
        },
        {
            "name": "ProvisionalWiki",
            "path": "ProvisionalWiki",
            "privacy": "public-reference",
            "description": "Synthetic provisional scaffold",
            "purpose": "Draft notification coverage awaiting evaluation",
            "keywords": ["notification", "draft"],
            "triggers": ["notification"],
            "card": "ProvisionalWiki/CARD.md",
            "index": "ProvisionalWiki/INDEX.md",
            "model_access": {"local": "full", "cloud": "full"},
            "provisional": True,
        },
    ],
}


FILES: dict[str, str] = {
    ".megamind/registry.json": json.dumps(REGISTRY, indent=2, sort_keys=True) + "\n",
    "README.md": (
        "# Release benchmark fixture\n"
        "\n"
        "Synthetic release benchmark fixture. All content is invented and safe to\n"
        "publish. Regenerate with `python evals/gen_corpus.py <target> --replace`.\n"
    ),
    # --- ProductWiki: public reference, card + index + exact pages -------------
    "ProductWiki/CARD.md": (
        "---\n"
        "megamind: routing-card\n"
        "wiki: ProductWiki\n"
        "privacy: public-reference\n"
        "keywords: [product, pricing, release, onboarding]\n"
        "---\n"
        "\n"
        "# ProductWiki routing card\n"
        "\n"
        "Answers synthetic questions about product pricing, the release process,\n"
        "and onboarding.\n"
        "\n"
        "Does not answer: operations approvals (OpsWiki), personal notes\n"
        "(PersonalWiki).\n"
    ),
    "ProductWiki/INDEX.md": (
        "---\n"
        "megamind: index\n"
        "wiki: ProductWiki\n"
        "---\n"
        "\n"
        "# ProductWiki index\n"
        "\n"
        "- [Pricing](topics/pricing.md) - starter and team plans\n"
        "- [Release](topics/release.md) - staged rollout and release notes\n"
        "- [Onboarding](topics/onboarding.md) - signup and guided tour\n"
    ),
    "ProductWiki/topics/pricing.md": (
        "# Pricing\n"
        "\n"
        "The synthetic product has a starter plan and a team plan. Pricing is\n"
        "reviewed each release.\n"
    ),
    "ProductWiki/topics/release.md": (
        "# Release\n"
        "\n"
        "Synthetic releases use a staged rollout and a published release note.\n"
    ),
    "ProductWiki/topics/onboarding.md": (
        "# Onboarding\n"
        "\n"
        "Synthetic product onboarding starts with an account and a guided tour.\n"
        "New users who skip the tour often leave soon after signup.\n"
    ),
    # --- OpsWiki: company-private, cloud access none --------------------------
    "OpsWiki/CARD.md": (
        "---\n"
        "megamind: routing-card\n"
        "wiki: OpsWiki\n"
        "privacy: company-private\n"
        "keywords: [operations, pricing, controls]\n"
        "---\n"
        "\n"
        "# OpsWiki routing card\n"
        "\n"
        "Answers synthetic questions about operations pricing controls and\n"
        "approval checks. Local models only.\n"
    ),
    "OpsWiki/INDEX.md": (
        "---\n"
        "megamind: index\n"
        "wiki: OpsWiki\n"
        "---\n"
        "\n"
        "# OpsWiki index\n"
        "\n"
        "- [Pricing controls](topics/pricing-controls.md) - approval thresholds\n"
    ),
    "OpsWiki/topics/pricing-controls.md": (
        "# Pricing controls\n"
        "\n"
        "Synthetic operations uses private pricing controls and approval checks.\n"
    ),
    # Unindexed private page: the ladder must never reach it.
    "OpsWiki/private/approval-notes.md": (
        "# Approval notes\n"
        "\n"
        "Synthetic private approval notes.\n"
        "CANARY-COMPANY-OPS-7\n"
    ),
    # --- PersonalWiki: personal-local, cloud access none -----------------------
    "PersonalWiki/CARD.md": (
        "---\n"
        "megamind: routing-card\n"
        "wiki: PersonalWiki\n"
        "privacy: personal-local\n"
        "keywords: [journal, routine]\n"
        "---\n"
        "\n"
        "# PersonalWiki routing card\n"
        "\n"
        "Answers synthetic private journal and routine questions. Local models\n"
        "only.\n"
    ),
    "PersonalWiki/INDEX.md": (
        "---\n"
        "megamind: index\n"
        "wiki: PersonalWiki\n"
        "---\n"
        "\n"
        "# PersonalWiki index\n"
        "\n"
        "- [Routine](topics/routine.md) - synthetic weekly routine\n"
    ),
    "PersonalWiki/topics/routine.md": (
        "# Routine\n"
        "\n"
        "A synthetic weekly routine with no private detail in it.\n"
    ),
    "PersonalWiki/private/journal.md": (
        "# Journal\n"
        "\n"
        "Private synthetic journal notes.\n"
        "CANARY-PRIVATE-PERSONAL-9\n"
    ),
    # --- DigestWiki: digest-only, mixed language ------------------------------
    "DigestWiki/CARD.md": (
        "---\n"
        "megamind: routing-card\n"
        "wiki: DigestWiki\n"
        "privacy: digest-only\n"
        "keywords: [research, study]\n"
        "---\n"
        "\n"
        "# DigestWiki routing card\n"
        "\n"
        "Answers synthetic research study questions from a digest only.\n"
    ),
    "DigestWiki/DIGEST.md": (
        "# Research digest\n"
        "\n"
        "Synthetic research study summaries are safe to share as a digest.\n"
        "Gemischte Notizen: Die Studie beschreibt eine synthetic notification.\n"
    ),
    "DigestWiki/INDEX.md": (
        "---\n"
        "megamind: index\n"
        "wiki: DigestWiki\n"
        "---\n"
        "\n"
        "# DigestWiki index\n"
        "\n"
        "- [Study notes](topics/study-notes.md) - synthetic study notes\n"
    ),
    "DigestWiki/topics/study-notes.md": (
        "# Study notes\n"
        "\n"
        "Synthetic study notes that a digest-only wiki never routes directly.\n"
    ),
    "DigestWiki/private/private-study.md": (
        "# Private study\n"
        "\n"
        "Synthetic private study notes.\n"
        "CANARY-PRIVATE-DIGEST-8\n"
    ),
    # --- ArchiveWiki: pointer-only, access none on both axes ------------------
    "ArchiveWiki/CARD.md": (
        "---\n"
        "megamind: routing-card\n"
        "wiki: ArchiveWiki\n"
        "privacy: pointer-only\n"
        "keywords: [archive, history]\n"
        "---\n"
        "\n"
        "# ArchiveWiki routing card\n"
        "\n"
        "Points at a synthetic historic archive. Content is never routed.\n"
    ),
    "ArchiveWiki/topics/history.md": (
        "# History\n"
        "\n"
        "Synthetic archive history.\n"
        "CANARY-PRIVATE-ARCHIVE-6\n"
    ),
    # --- ProvisionalWiki: offered, never an authorized load -------------------
    "ProvisionalWiki/CARD.md": (
        "---\n"
        "megamind: routing-card\n"
        "wiki: ProvisionalWiki\n"
        "privacy: public-reference\n"
        "keywords: [notification, draft]\n"
        "---\n"
        "\n"
        "# ProvisionalWiki routing card\n"
        "\n"
        "Draft synthetic notification coverage that is still provisional.\n"
    ),
    "ProvisionalWiki/INDEX.md": (
        "---\n"
        "megamind: index\n"
        "wiki: ProvisionalWiki\n"
        "---\n"
        "\n"
        "# ProvisionalWiki index\n"
        "\n"
        "- [Notification draft](topics/notification-draft.md) - draft coverage\n"
    ),
    "ProvisionalWiki/topics/notification-draft.md": (
        "# Notification draft\n"
        "\n"
        "A synthetic draft note about notification delivery, not yet evaluated.\n"
    ),
}


def generate(target: Path) -> list[str]:
    """Write every fixture file under target and return the written paths."""
    written: list[str] = []
    for relative, content in FILES.items():
        atomic_write_path(target / relative, content, durable=True)
        written.append(relative)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument(
        "--replace", action="store_true", help="overwrite an existing fixture deliberately"
    )
    args = parser.parse_args()
    target = args.target.resolve()
    if target.exists() and not args.replace:
        raise SystemExit("refusing to replace an existing fixture; pass --replace deliberately")
    generate(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
