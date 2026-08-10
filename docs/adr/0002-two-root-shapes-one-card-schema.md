# ADR 0002: two root shapes, one card schema

Status: accepted (Phase 1)

## Context

Megamind grew up single-root: one vault, one registry, wikis as
subdirectories. The fleet reality is many separate roots with their own
ownership and privacy. The options were: force every wiki into one mega-root
registry, invent a second card format for standalone wikis, or keep both root
shapes on one schema.

## Decision

Keep both shapes. A registry vault keeps its multi-wiki
`.megamind/registry.json`; a canonical wiki root (the Karpathy layout:
immutable `raw/`, compiled `wiki/`, `AGENTS.md` schema) carries a single
authoritative `.megamind/wiki-card.json`. Both validate against the same v2
field set (the card simply omits `path`, which is the root itself, and may
omit `privacy`). Catalog and preflight read both shapes through one code path.

## Consequences

- No wiki is forced to move or merge to join the fleet; adoption is additive.
- One schema means one validator, one access-policy engine, one catalog row
  shape; the two storage formats cannot drift apart semantically.
- The duplication is structural only (registry file vs card file), and the
  catalog projection is what unifies them for hosts.
- Later per-card governance (provisional status, evaluation state) extends
  the same v2 field set rather than inventing new containers.
