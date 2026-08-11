# Phase 5 governed pilot verification

Status: completed, rollback required

A task-owned restricted sandbox exercised exactly two synthetic card postures:
one cloud `digest-only` image card and one `public-reference` finance card with
an approved compiled index. The private evidence bundle is not part of this
repository.

## Verified through the public CLI

- `preflight-result/v2` kept the digest-only card to one approved digest and
  surfaced its 3-candidate, 4000-character budget. The full card authorized its
  compiled index with a 5-candidate, 6000-character budget. Negative cases were
  definitive no-matches; a cross-domain case loaded only the stronger card and
  kept the other as an offer.
- JSON and TOON captures were scanned for raw paths, withheld image paths,
  synthetic canary markers, and paths outside the restricted root. No scan
  finding occurred.
- Two semantic-identity gaps deduplicated. Lifecycle evidence covered reject,
  replay, reopen, attempt, cooldown, resolve, invalid supersession, and rollback
  reopen. A planned wave emitted one direct plus two first-order nominations and
  deferred one. Unknown capacity, reserve below 25 percent, fleet saturation,
  per-wiki saturation, insufficient duration, and active captain work all
  produced their typed pause or refusal.
- One eligible synthetic host result produced one immutable-source ingest
  proposal. Exact correlation replay was idempotent and divergent replay was
  refused. Megamind dispatched no worker and performed no network or external
  action.
- A governed compiled-page transaction changed an authorized route candidate
  from zero context characters to nonzero context, then restored zero. A second
  governed transaction proved that rollback refuses foreign content. Rolling
  back in reverse order restored the exact pre-change controlled-tree SHA-256,
  retained proposals and append-only evidence, and reopened the affected gap.
- A public-CLI integration test interrupts an apply after the write-ahead record
  and confirms replay completes it. Other tests cover stale tokens, foreign
  content, immutable raw refusal, and canonical-root routing budgets.

## Product findings

The pilot found that local route/capture/review/evolve originally required a
registry even at a canonical wiki root, canonical route could not consume the
card budget, and evolve neither rejected the canonical raw layer nor exposed a
controlled rollback. The repaired boundary now adapts the authoritative card
to those local surfaces, keeps raw immutable, and uses a durable
`megamind/evolve-rollback/v1` transaction with additive controlled-tree hashes.
Existing retrieval schemas and default candidate columns are unchanged.

## Real three-arm evaluation

The frozen task set, rubric, thresholds, model/provider identity, tools, effort,
seed, and three isolated snapshots were committed only to the task-private
evidence area. A machine-generated 256-bit mode-0600 key produced a separate
public plan, blind grader packet, and private unblinding map. The grader packet
was scanned for condition names, roots, raw snapshot digests, and key material;
none appeared. Three condition-blind executor packets name opaque labels and
commitments only.

No model result was fabricated and the preparing agent did not grade its own
work. Three host-executed sealed outputs validated as complete, with four tasks
per blind label and no cross-arm contamination. The blind aggregate target
accuracies were 0.25, 0.25, and 0.50; adjacent accuracies were 0.50, 0.50, and
0.75; provenance rates were 0.50 for every arm; and every arm reported zero
authorized context characters.

Scoring sealed those blind metrics before unblinding. The preregistered
comparison was -0.25 target improvement, -0.25 adjacent delta, and 0.00
provenance delta. It failed the 0.20 improvement floor and zero adjacent
regression tolerance. Prompt leak, canary leak, privacy violations, and model
access violations were all zero. The typed outcome is `rollback-required`, the
safe evaluation event is recorded, the relevant gap remains open with the
rollback reference, and isolated updated knowledge remains untrusted. Frozen
thresholds were not edited and no post-hoc rerun or promotion was attempted.
