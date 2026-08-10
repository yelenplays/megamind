# Context: Megamind domain language

The settled vocabulary for the federation foundation. Each term means exactly
this anywhere it appears in the project; the avoid lines name the common
misreadings. This file covers only what the current release implements.

**Knowledge engine**:
Megamind's role: the deterministic layer that owns wiki cataloging, routing,
proposals, and validation, without commissioning or scheduling work.
_Avoid_: agent controller, research daemon

**AI host**:
The agent environment (any Agent Skills-compatible host or orchestrator) that
invokes Megamind and owns orchestration, worker dispatch, budgets, and
approval authority.
_Avoid_: Megamind runtime

**Registry vault**:
One root holding many wikis as subdirectories, with `.megamind/registry.json`
as the multi-wiki card authority.
_Avoid_: fleet, mega-root

**Canonical wiki root**:
A single top-level wiki in the canonical layout: `AGENTS.md`, immutable
`raw/`, compiled `wiki/`, and `.megamind/` state with the authoritative card.
_Avoid_: subdirectory wiki, vault entry

**Wiki card**:
The authoritative per-wiki record of scope, exclusions, ownership, privacy,
model access, source policy, freshness, and routing signals; the registry
entry or `wiki-card.json` the catalog projects from.
_Avoid_: catalog row, hand-maintained router copy

**Wiki catalog**:
The generated projection of all eligible wiki cards that lets a host discover
and route across separate roots without combining their content or governance.
_Avoid_: mega-vault, source of truth

**Raw source**:
A human-curated, immutable input document or asset under `raw/` that the AI
may read and cite but never modify.
_Avoid_: wiki page, proposal

**Compiled wiki**:
The AI-maintained, interlinked Markdown synthesis under `wiki/`, navigable
through `index.md` and the append-only `log.md`.
_Avoid_: raw corpus, retrieval index

**Wiki schema**:
The `AGENTS.md` contract that defines a wiki's domain-specific page
conventions and its ingestion, query, maintenance, and source-handling
workflows.
_Avoid_: fleet catalog, routing card

**Model access**:
A per-wiki policy declaring whether a local or cloud model context may receive
full content, only an approved digest, or no content; unset or broken
classifications always resolve restrictively.
_Avoid_: privacy label, host preference

**Substantive request**:
A request that asks an AI to research, analyze, decide, write, design, plan,
or perform software work and therefore warrants wiki preflight.
_Avoid_: every message, every turn

**Wiki preflight**:
The host-side act of consulting the wiki catalog before answering or starting
a substantive request, with a proof identity showing the consultation
happened. Megamind supplies the typed result; running it is the host's choice.
_Avoid_: optional skill trigger, full-catalog prompt injection
