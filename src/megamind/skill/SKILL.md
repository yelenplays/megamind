---
name: megamind
description: Route questions to the right Markdown wiki pages and garden knowledge safely with the megamind-axi executable. Use when the user asks where knowledge lives in their vault or wiki, wants to capture a note or decision from a conversation, asks to update or supersede outdated wiki pages, wants duplicates, stale pages, or dead links found, or asks to validate a Megamind vault. Works locally on Markdown and Obsidian vaults with no embeddings or network.
---

# Megamind

Drive everything through `megamind-axi`. Every invocation prints exactly one
typed TOON document on stdout (add `--format json` for JSON of the same
object); each document carries a `help[]` of ready-to-run next commands, so
follow those over guessing flags. Running `megamind-axi` with no arguments
shows the vault home: wikis, proposal counts, review and doctor aggregates.

Megamind never publishes automatically: capture writes proposal drafts, and
permanent changes require the plan id from a dry run as an approval token.

## Find knowledge

Run `megamind-axi route "<question>"`. Open only the returned candidate
paths, best first; each row carries a reason and respects context budgets.
`matched: false` with `candidates[0]` is a definitive no-match: say so
instead of guessing. Candidates of kind `pointer` must be opened manually and
never quoted; kind `digest` exposes only summary content.

## Capture knowledge

When a conversation produces a durable fact, decision, hypothesis, procedure,
example, or guidance:

```sh
megamind-axi capture --text "<the knowledge>" --type <type> --source "<origin>"
```

This writes a private proposal draft with provenance and a suggested
destination. It never touches wiki pages, so capture liberally; identical
content dedupes (`status: duplicate` is a success, not an error).

## Evolve knowledge (requires approval)

1. `megamind-axi evolve <proposal-id>` prints the plan with a bounded diff
   and a `plan_id`. Nothing changes. Use `--full` if `diff_truncated: true`.
2. Show the human the diff. Only after they approve, run the apply command
   from `help[]` (it carries `--apply --plan-id <plan_id>`).
3. To replace an outdated page instead of appending, add
   `--supersedes <old-page.md>`; the old page is marked superseded, not deleted.
4. If `creates_new_wiki: true`, stop and ask the human explicitly; only then
   add `--approve-new-wiki`.

Never edit `.megamind/registry.json` or apply plans without an explicit human
go-ahead.

## Maintain the vault

- `megamind-axi review` reports open proposals, duplicates, stale pages, dead
  links, and promotion candidates; `status: clean` means nothing needs work.
- `megamind-axi doctor` validates the vault; exit 1 means real errors and the
  `findings` table names each offending file. Run it after any approved change.

## References

- [commands.md](references/commands.md) - full flag reference, schemas, exits
- [concepts.md](references/concepts.md) - types, lifecycle, privacy classes
