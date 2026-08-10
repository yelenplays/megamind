# ADR 0003: the fleet catalog is always a generated projection

Status: accepted (Phase 1)

## Context

A host needs one fleet-wide view to route across separate wikis. A
hand-maintained router file rots immediately, and a shared editable catalog
would become a second, conflicting authority over per-wiki policy - including
the ability to accidentally republish what a sensitive card withholds.

## Decision

The catalog is generated from the per-wiki cards on every invocation, read
only, and never hand-edited. Redaction (`full`, `redacted`, `hidden`) is
applied at the projection boundary from the card's own policy. The
human-readable rendering is byte-stable for the same inputs, and
`catalog --check-projection` drift-checks any checked-in copy against the
cards so the two cannot silently diverge. Broken, unreachable, stale, and
withheld entries are stated explicitly rather than omitted.

## Consequences

- There is exactly one authority per wiki: its card. Everything else
  regenerates.
- Drift is detectable mechanically (CI or doctor-style checks on the
  projection file), which is what makes "the host consulted the catalog" mean
  "the host consulted current routing truth".
- A catalog row can never leak page content, because the catalog never reads
  pages; sensitivity leaks are bounded by the card's declared visibility.
- Regeneration cost is paid per invocation; acceptable at Phase 1 fleet sizes
  and worth it for the authority guarantee.
