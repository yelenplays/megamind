# ADR 0005: semantic reranking is reorder-only and fails typed

Status: accepted (Phase 2)

## Context

Pure lexical routing misses paraphrases and implicit intent. The plan allows a
local semantic layer, under hard constraints: no cloud embeddings, no network,
the frozen lexical path stays the permanent fallback and evaluation baseline,
and access decisions must never widen. The dangerous failure modes are a
semantic pass resurrecting an ineligible wiki, leaking filtered paths or
content, and silently changing policy when the semantic machinery is missing
or broken.

## Decision

The semantic layer (`megamind.semantic`) only reorders candidates that the
lexical ladder already surfaced and model-access filtering already authorized,
blending a deterministic local similarity (char n-gram cosine by default,
behind a small backend protocol) with the normalized lexical score at a fixed
0.4 weight. It receives only text each candidate may already expose; pointer
candidates contribute none. Thresholds, membership, budgets, and access are
computed from the lexical baseline alone, before reranking. The feature is
opt-in (`--semantic`); the default stays byte-identical lexical output. Every
rerank attempt returns a typed outcome - `disabled`, `ok`, `unavailable`, or
`error` with a reason - and any non-`ok` state returns the untouched lexical
order, so a corrupt or missing backend can never raise, guess, or shift
policy.

## Consequences

- A semantic pass can improve ordering for paraphrases but can never change
  what is loaded, offered, or filtered; adversarial tests pin this.
- The lexical baseline remains the permanent offline default and the
  evaluation baseline, exactly as the plan requires.
- Backend failures are inspectable data in the output document, not log
  noise, so hosts can prove which evidence caused a route.
- Future local embedding adapters plug into the protocol under the same
  constraints; hosted embedding services remain out of scope permanently.
