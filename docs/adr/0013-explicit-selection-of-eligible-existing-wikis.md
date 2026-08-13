# ADR 0013: explicit selection of an eligible existing wiki is a separate authorization

## Status

Accepted

## Context

The picker needs a `Different existing wiki` disposition. A wiki that was not
an offer must not be passed through `select-offer`: that command intentionally
accepts only an exact current `offers[]` identity. The picker also cannot be
trusted to manufacture an eligible-name list or access decision.

## Decision

Add the `select-existing` operation with two typed phases:

- the list phase derives a complete, current eligible set from validated
  catalog rows, access policy, freshness, card/root facts, declared artifacts,
  and containment checks;
- the authorization phase recomputes that set and consumes one opaque
  selection identity before returning one bounded reader authorization.

The identity binds the exact request hash, complete catalog hash, model class,
owner and session identities, home identity, current UTC date, and eligible
set. Durable state makes reuse fail closed. Hidden/withheld, inaccessible,
provisional, pointer, broken, unavailable, stale, duplicate, absent-artifact,
and escaping entries are not eligible. Effective access and card budgets are
preserved exactly. The result uses `basis: selected-eligible-existing` and
`threshold_matched: false`; explicit choice grants consultation authority but
never raises routing confidence or answerability.

The state contains only hashes, identities, bounded card-derived facts, and
root-relative facts. It never stores prompt text or page content. Existing
`select-offer` remains unchanged.

## Consequences

The host can safely render an independently derived existing-wiki list and
choose an eligible wiki that the router did not offer. Catalog, card, model,
session, home, and date drift all require a new list. The operation adds a
small durable audit/state surface under the owning Megamind root, but performs
no wiki, proposal, host, network, or provider action.
