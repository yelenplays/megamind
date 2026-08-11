# Phase 4 evaluation contract

Megamind v0.4 ships two offline evaluation surfaces.

## Release benchmark

`megamind-axi bench run --fixtures evals/fixtures/release-mini --queries evals/queries.jsonl --thresholds evals/thresholds.toml --out results.json --repeat` invokes the public `preflight` and `route` interfaces for every frozen synthetic query. It emits one canonical `megamind/benchmark-result/v1` document and never calls a model or network. `bench check` evaluates the same machine-readable thresholds. Tiers are reported independently: exact, near, paraphrase, ambiguous, no-match, and privacy. Private canary strings are invented and only safety counts are emitted.

The fixture, query set, threshold file, and task-set versions are immutable release inputs. Do not change a released version in place. Add a versioned file and update the release notes when a threshold or task changes. The grep and full-vault baselines are context/safety baselines, not claims of model quality.

## Three-arm value evaluation

`experiment plan` freezes the task-set digest, prompt inputs, model/provider identifier, tools, effort, rubric, thresholds, seed, and isolated no-wiki/current-wiki/updated-wiki snapshot roots. The plan creates opaque deterministic arm labels. The host executes identical pre-authored tasks and writes arm outputs; Megamind never invokes a model, worker, account, service, or network.

The host then submits one output per opaque arm to `experiment validate`. Validation rejects missing or duplicate tasks, wrong snapshot digests, changed plan/task versions, malformed context accounting, privacy/model-access violations, canary or prompt leakage, and incomplete arms. `experiment score` applies only the frozen rubric and promotion thresholds. Promotion needs target improvement, no material adjacent regression, preserved provenance, and zero safety violations. Incomplete evaluations are unsettled; failed gates are rollback-required. `experiment record` appends a hash-chained, bounded safe event to `.megamind/audit/evaluations.jsonl`; it contains no prompts, answers, canaries, secrets, or sensitive content.

Evaluation output is evidence, not authorization to dispatch a worker or publish a wiki. Provisional wikis remain untrusted until the complete evaluation pass and an explicit governed card change.
