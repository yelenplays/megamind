# The megamind-axi output contract

Megamind's executable boundary is an AXI (agent experience interface), built
against the ten principles at <https://axi.md/>. Agents are the first-class
consumer; humans read the same structured document, not a separate prose
rendering that could drift.

## One document per invocation

Stdout carries exactly one typed document. TOON is the default rendering;
`--format json` renders the exact same object, and golden tests enforce that
the two never diverge. Diagnostics never go to stdout. Every document starts
with a stable `schema_version`:

| schema_version | emitted by |
| --- | --- |
| `megamind/home/v1` | `megamind-axi` (no arguments) |
| `megamind/init-result/v1` | `init` |
| `megamind/route-result/v1` | `route` |
| `megamind/capture-result/v1` | `capture` |
| `megamind/evolve-plan/v1` | `evolve` (dry run) |
| `megamind/evolve-result/v1` | `evolve --apply` |
| `megamind/review-report/v1` | `review` |
| `megamind/doctor-report/v1` | `doctor` |
| `megamind/config/v1` | `config show` |
| `megamind/setup-plan/v1`, `megamind/setup-result/v1` | `setup skill` |
| `megamind/error/v1` | any failure |

Example (`megamind-axi route "pricing"` on the examples vault):

```toon
schema_version: megamind/route-result/v1
query: pricing
matched: true
candidates[1]{path,kind,score,reason}:
  ProductWiki/topics/pricing-v2.md,page,7,"keyword match: pricing"
context_chars: 446
max_context_chars: 8000
max_candidates: 5
notes[0]:
help[2]:
  Open `ProductWiki/topics/pricing-v2.md` first; it scored highest
  "Run `megamind-axi route \"pricing\" --fields path,kind,score,privacy,reasons` for detail"
```

## The ten principles, applied

1. **Token-efficient output**: TOON by default; tables for candidate and
   finding lists; wiki page bodies are never dumped into output.
2. **Minimal default schemas**: route candidates default to
   `path,kind,score,reason`; `--fields` opts into
   `wiki,privacy,chars,reasons`. Doctor findings carry exactly
   `check,severity,path,message`.
3. **Content truncation**: evolve diffs are bounded to 60 lines
   (`diff_truncated`, `diff_lines_total`, `--full`), review sections to 20
   items, doctor findings to 50, each with an explicit note.
4. **Pre-computed aggregates**: home returns proposal/review/doctor counts;
   review returns an `aggregates` object; route returns context-budget
   accounting; doctor returns error/warning counts.
5. **Definitive empty states**: `matched: false` with `candidates[0]`,
   `status: clean`, `status: healthy`, `status: duplicate`, and
   `status: noop` are structured successes, exit 0.
6. **Structured errors and exits**: `megamind/error/v1` with a stable `code`
   and sanitized message; exit 0 success/no-op, 1 operational failure
   (including doctor errors), 2 usage/config validation; no interactive
   prompts; unknown flags are rejected before any mutation.
7. **Explicit, local, zero-network integration**: `setup skill --dest DIR`
   installs the Agent Skill only where you point it; uninstall by deleting
   that directory. No hooks are installed implicitly; nothing touches shell
   or provider config; no command ever makes a network call.
8. **Content first**: no arguments returns the live local home: executable
   identity, one-sentence purpose, root, wikis, proposal/review/doctor
   aggregates, next actions. Never an argparse dump, never a mutation.
9. **Contextual disclosure**: every result, error, and no-op carries a small
   `help[]` of executable next commands with runtime values filled in (for
   example the exact `--apply --plan-id ...` command after a dry run).
   `--no-help-hints` suppresses it.
10. **Consistent help**: `--help` on every command is short and
    example-driven.

## Error codes

`usage_error`, `not_initialized`, `registry_invalid`, `capture_invalid`,
`proposal_not_found`, `plan_mismatch`, `approval_required`, `evolve_invalid`,
`path_escape`, `io_error`. Messages never include machine-specific absolute
paths from inside the vault model; registry paths are always root-relative.

## Testing the contract

`tests/test_cli.py` golden-tests TOON and JSON as renderings of the same
object, exit behavior, the no-args home, empty states, truncation, `--fields`,
`help[]` presence, and module execution outside the source tree. CI
additionally builds the wheel, installs it into a clean environment, and runs
`megamind-axi` from a scratch directory.
