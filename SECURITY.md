# Security policy

## Supported versions

Only the latest released version of Megamind receives security fixes.

## What counts as a vulnerability here

Megamind is a local-first tool that writes files inside a configured vault
root. Reports we especially care about:

- Path containment bypasses: any way `route`, `capture`, `evolve`, `init`, or
  `doctor` can be made to read or write outside the vault root (traversal,
  symlink tricks, crafted registry entries)
- Privacy class bypasses: content of `digest-only` or `pointer-only` wikis
  leaking into routing output
- Approval bypasses: applying changes without a matching plan id, or creating
  top-level wikis without `--approve-new-wiki`
- Non-atomic or destructive write paths that can corrupt user content
- Evaluation blinding leaks: a blinding key, an unblinding map, or an arm's
  condition recoverable from a plan, grader packet, arm output, or audit
  event, or an evaluation artifact written into a vault or an evaluated root

## Reporting

Please report vulnerabilities privately via
[GitHub Security Advisories](https://github.com/yelenplays/megamind/security/advisories/new)
rather than public issues. Include a minimal reproduction (a synthetic vault
layout plus the command). You can expect an acknowledgment within a week.
Please give us reasonable time to fix before public disclosure.

## Scope notes

Megamind executes no network calls, no shell-outs, and no model inference in
core commands; reports about those surfaces likely concern your environment
rather than this project. The one process Megamind ever spawns is the release
benchmark measuring the shipped interfaces: `bench run` runs this project's
own CLI (`sys.executable -m megamind.cli`, no shell) over its frozen synthetic
fixture. No command invokes a model, worker, account, or external service.
Vaults themselves may contain untrusted Markdown; Megamind treats page content
as data, never as code.
