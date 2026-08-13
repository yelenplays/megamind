# ADR 0012: deterministic research state spine

## Status

Accepted for Slice 1.

## Decision

Megamind owns a local, network-free research state spine. Plans are
content-addressed `research-plan/v1` records. Job transitions are append-only
`research-job/v1` events validated by an explicit transition table, with fresh
attempt identities for retries. Packets and terminal outcomes are immutable
`research-packet/v1` and `research-outcome/v1` artifacts.

Host workers may provide frozen JSON receipts, but cannot mint eligibility,
quality, access, approval, or answer authorization. Every plan names one wiki,
and the research verdict plus the bound policy, card, and access digests are
derived from that wiki's validated card and from the access policy layer; a
host receipt can only withhold a cycle the card allows. Claim confidence,
packet confidence, and packet answerability are likewise derived from the
frozen records rather than read from the receipt. Evidence and claim IDs
are opaque and validated only through narrow resolver interfaces. Cancellation
retains all artifacts. Exact replay is a no-op; divergent replay and terminal
mutation are refusals. Policy, card, access, or plan digest drift requires a
new plan and proof.

Packets compile into the existing Markdown proposal shape. The existing
`evolve` write-ahead transaction validates research packet references, and
apply remains explicitly approved once per cycle. Research state is inert
control data and never substitutes for fresh route/preflight/admission when an
answer is requested.

## Consequences

The core remains deterministic and stdlib-only, while hosts retain ownership
of network access, extraction, cancellation receipts, and deferred delivery.
Doctor and review can inspect state without reading source bodies. Full
fetched bytes remain outside the vault; only bounded, validated records and
provenance references enter the wiki state.
