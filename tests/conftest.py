"""Shared fixtures: a fully synthetic demo vault. No real-world content."""

from __future__ import annotations

from pathlib import Path

import pytest

from megamind.registry import ModelAccess, WikiEntry, load_registry, save_registry
from megamind.scaffold import init_vault


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


PRODUCT_CARD = """---
megamind: routing-card
wiki: ProductWiki
privacy: public-reference
keywords: [product, pricing, feature, roadmap, release]
---

# ProductWiki routing card

Answers questions about the synthetic product: pricing model, feature flags,
release process, roadmap decisions.
"""

PRODUCT_DIGEST = """---
megamind: digest
wiki: ProductWiki
---

# ProductWiki digest

Pricing uses a flat monthly plan. Features ship behind feature flags.
Releases follow a weekly train.
"""

PRODUCT_INDEX = """---
megamind: index
wiki: ProductWiki
---

# ProductWiki index

- [Pricing model](topics/pricing-model.md) - flat plan decision
- [Feature flags](topics/feature-flags.md) - rollout procedure
- [Release process](topics/release-process.md) - weekly train
"""

PRICING_PAGE = """---
title: Pricing model
type: decision
status: active
created: 2026-01-10
updated: 2026-01-10
provenance:
  - synthetic example
---

# Pricing model

The product uses one flat monthly plan. See [Feature flags](feature-flags.md).
"""

FLAGS_PAGE = """---
title: Feature flags
type: procedure
status: active
created: 2026-01-11
updated: 2026-01-11
provenance:
  - synthetic example
---

# Feature flags

New features roll out behind flags, 10 percent at a time.
"""

RELEASE_PAGE = """---
title: Release process
type: procedure
status: active
created: 2026-01-12
updated: 2026-01-12
provenance:
  - synthetic example
---

# Release process

Weekly release train. Cut on Monday, ship on Thursday.
"""

BRAND_CARD = """---
megamind: routing-card
wiki: BrandingWiki
privacy: company-private
keywords: [brand, branding, logo, color, palette, voice, tone]
---

# BrandingWiki routing card

Answers questions about the synthetic brand: colors, logo usage, voice and tone.
"""

BRAND_DIGEST = """---
megamind: digest
wiki: BrandingWiki
---

# BrandingWiki digest

The brand uses a deep teal palette and a calm, direct voice.
"""

BRAND_INDEX = """---
megamind: index
wiki: BrandingWiki
---

# BrandingWiki index

- [Color palette](topics/color-palette.md) - teal primary
- [Brand voice](topics/brand-voice.md) - calm and direct
"""

COLOR_PAGE = """---
title: Color palette
type: guidance
status: active
created: 2026-02-01
updated: 2026-02-01
provenance:
  - synthetic example
---

# Color palette

Primary is deep teal. Accent is warm sand.
"""

VOICE_PAGE = """---
title: Brand voice
type: guidance
status: active
created: 2026-02-02
updated: 2026-02-02
provenance:
  - synthetic example
---

# Brand voice

Calm, direct, no hype.
"""

RESEARCH_DIGEST = """---
megamind: digest
wiki: ResearchDigest
---

# Research digest

Summarized findings about synthetic user interviews. Full notes stay local.
"""


def build_vault(base: Path) -> Path:
    """A synthetic v2 vault with public, private, digest-only, and pointer-only wikis."""
    root = base / "vault"
    init_vault(root, starter=True)

    write(root, "ProductWiki/CARD.md", PRODUCT_CARD)
    write(root, "ProductWiki/DIGEST.md", PRODUCT_DIGEST)
    write(root, "ProductWiki/INDEX.md", PRODUCT_INDEX)
    write(root, "ProductWiki/topics/pricing-model.md", PRICING_PAGE)
    write(root, "ProductWiki/topics/feature-flags.md", FLAGS_PAGE)
    write(root, "ProductWiki/topics/release-process.md", RELEASE_PAGE)

    write(root, "BrandingWiki/CARD.md", BRAND_CARD)
    write(root, "BrandingWiki/DIGEST.md", BRAND_DIGEST)
    write(root, "BrandingWiki/INDEX.md", BRAND_INDEX)
    write(root, "BrandingWiki/topics/color-palette.md", COLOR_PAGE)
    write(root, "BrandingWiki/topics/brand-voice.md", VOICE_PAGE)

    write(root, "ResearchDigest/DIGEST.md", RESEARCH_DIGEST)
    (root / "ArchiveBox").mkdir()

    registry = load_registry(root)
    registry.version = 2
    starter = registry.wiki_by_name("StarterWiki")
    assert starter is not None
    starter.sensitivity = "public-reference"
    starter.model_access = ModelAccess(local="full", cloud="full")
    registry.wikis.extend(
        [
            WikiEntry(
                name="ProductWiki",
                path="ProductWiki",
                privacy="public-reference",
                description="Synthetic product knowledge: pricing, features, releases.",
                keywords=["product", "pricing", "feature", "roadmap", "release"],
                card="ProductWiki/CARD.md",
                digest="ProductWiki/DIGEST.md",
                index="ProductWiki/INDEX.md",
                sensitivity="public-reference",
                model_access=ModelAccess(local="full", cloud="full"),
            ),
            WikiEntry(
                name="BrandingWiki",
                path="BrandingWiki",
                privacy="company-private",
                description="Synthetic brand guidance: colors, logo, voice.",
                keywords=["brand", "branding", "logo", "color", "voice", "tone"],
                card="BrandingWiki/CARD.md",
                digest="BrandingWiki/DIGEST.md",
                index="BrandingWiki/INDEX.md",
                sensitivity="company-private",
                model_access=ModelAccess(local="full", cloud="none"),
            ),
            WikiEntry(
                name="ResearchDigest",
                path="ResearchDigest",
                privacy="digest-only",
                description="Synthetic research summaries; only the digest is routable.",
                keywords=["research", "interview", "study"],
                digest="ResearchDigest/DIGEST.md",
                model_access=ModelAccess(local="digest-only", cloud="digest-only"),
            ),
            WikiEntry(
                name="ArchiveBox",
                path="ArchiveBox",
                privacy="pointer-only",
                description="Synthetic archive; content is never routed, only pointed to.",
                keywords=["archive", "history"],
                model_access=ModelAccess(local="none", cloud="none"),
                routing_mode="pointer",
            ),
        ]
    )
    save_registry(root, registry)
    init_vault(root)  # refresh the generated router after registry edits
    return root


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return build_vault(tmp_path)
