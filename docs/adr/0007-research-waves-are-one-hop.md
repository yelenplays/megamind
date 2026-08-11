# ADR 0007: research waves are one hop, deeper topics are deferred nominations

Status: accepted (Phase 3)

## Context

A gap suggests neighbours, and each neighbour suggests more. Left unbounded, a
wave planned from one gap expands transitively until it is a crawl: unbounded
cost on a host budget Megamind cannot see, unbounded wall time, and a plan no
human can review before it is dispatched. The alternatives were an explicit
depth or breadth budget the host tunes, or a fixed bound in the engine.

## Decision

A wave is one hop. `plan_research_wave` emits the direct gap plus a bounded set
of first-order related topics as `nominations`; every remaining first-order
topic goes to `deferred_nominations`, and anything beyond first order is not
planned at all. `make_nomination` rejects any relationship other than `direct`
or `first-order`, so a deeper hop cannot re-enter through the host bridge.
Deferred topics are not discarded: they are durable gap identities a later
wave can pick up after reprioritization. Capacity is a typed host-supplied
fact, and every refusal reason - unknown capacity, a sub-25-percent measurable
quota reserve, the three-worker fleet limit, a worker already on the target
wiki, capacity that cannot last through the wave, or active captain work -
produces a typed `paused` or `refused` plan with no nominations. Megamind never
calls a quota tool, starts a worker, or reaches the network. The wave,
capacity, and nomination field sets are specified in `docs/schemas.md`.

## Consequences

- Wave cost is bounded and reviewable before dispatch, and the bound is a
  property of the engine rather than of host configuration it could get wrong.
- Deep exploration is still reachable, but only by a host that keeps choosing
  it one reviewed wave at a time, which is where the priority decision belongs.
- The bound is fixed in `megamind.gardening`, not configurable; changing it
  changes planned output and fails the wave tests, like the routing weights.
- A refusal is final data, not a retry hint. Hosts that work around a `paused`
  wave defeat the capacity contract, so the skill guidance states this
  explicitly.
