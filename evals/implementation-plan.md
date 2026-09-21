# Standalone ADLC worker A/B evaluation system: implementation plan

Status: proposed implementation contract; no evaluation code is implemented by
this document. All commands and schemas below are proposed unless explicitly
identified as existing. Written against the checkout inspected on 2026-09-20.

## 1. Objective and decisions already made

Build a self-contained evaluation tool in `adlc-workflow/evals/`. Its primary
purpose is controlled A/B testing of models executing one bounded ADLC worker.
The first supported worker is `skill:rhai-feature-refine-worker`.

The experiment asks:

> Given the same RFE, worker instructions, template, profile, prepared context,
> and execution environment, which model produces the better strategy, how
> reliably, at what inference cost, and in how much time?

The user has agreed to these boundaries:

- Evaluation lives in its own folder with its own scripts and dependencies.
- Do not add evaluation commands to the production `adlc-workflow` CLI, put
  judges into the core, or require evaluation dependencies for normal use.
- Compare bounded worker capabilities. Skills and reviewer agents can both be
  targets; their packaging does not define the evaluation unit.
- Collect candidate outputs separately from judging them. Rejudging saved
  artifacts must not rerun the worker.
- Each case/candidate/repetition gets a fresh container and a unique host
  workspace mounted at `/workspace` inside that container.
- Preserve A's directory and mount a different directory for B. Do not clear
  or back up and reuse the integration-test workspace.
- Reuse the current Claude image and production worker invocation logic.
- Run candidates serially initially; experiment parallelism is deferred.
- Keep the normal agent-led, handoff, and CLI workflows functional and unchanged.

The first evaluation measures the **controller-style bounded worker subprocess**
contract. It does not claim to test native Claude `Skill`/`Agent` dispatch or the
entire agent-led workflow. Record the invocation form in every experiment.

## 2. Scope and explicit non-goals

Implement a complete vertical slice for refinement: validate configuration,
freeze inputs, collect A/B outputs, check contracts, judge pairs, and report.
Include multiple cases and repetitions, preservation of failures, and an offline
test path. One case and one repetition must also work as a smoke test.

Do not initially implement:

- Full workflow execution, Jira seeding/reset/publication, or production intake.
- A new workflow engine or another copy of ADLC's refine/review logic.
- Native agent-led versus handoff versus CLI comparisons.
- Automatic model selection, prompt optimization, or a CI/publication service.
- MLflow, dashboards, OpenShell, Harbor, or EvalHub integrations.
- Arbitrary commands, arbitrary worker types, or provider-independent runners.
- Statistical significance claims from a small fixture set.
- Silent retries, automatic model fallback, or selecting the best of N attempts.

Leave clean extension points for individual reviewer evaluation, but implement
that only after the refinement slice passes acceptance. Section 18 describes it.

## 3. Existing implementation: read these files first

Paths in this document are relative to `adlc-workflow/` unless stated otherwise.

| Existing file | Relevant behavior |
|---|---|
| `Dockerfile.claude` | Builds `adlc-claude-task-runner:local`; Python, Claude, PyYAML, and jsonschema are available; user is `evaluator`. |
| `podman-compose.yaml` | Already selects workspace via `ADLC_WORKSPACE_DIR`; mounts plugin at `/home/evaluator/.claude/plugins/adlc-workflow`; main service depends on Jira. |
| `scripts/compose-entrypoint.sh` | Registers/installs the local plugin, then executes `tail -f /dev/null`; unsuitable as a one-shot eval entrypoint. |
| `.claude-plugin/marketplace.json` | Uses command-source link installation pointing to that exact plugin mount path. Preserve it. |
| `scripts/run-example-workflow.sh` | Deletes workspace contents, resets Jira, seeds tickets, executes the full workflow. Do not use it in evals. |
| `src/adlc_workflow/controller.py` | Contains `ClaudeWorkerRuntime`, `RunReporter`, `_skill_prompt`, `_agent_prompt`, and completion validation. |
| `src/adlc_workflow/artifacts.py` | `ArtifactLayout` resolves output paths from the profile. |
| `src/adlc_workflow/profiles.py` | Profile name/path resolution, loading, template resolution. |
| `src/adlc_workflow/workflow.py` | Stage lookup and declared outputs. |
| `src/adlc_workflow/reviews.py` | `review_plan()` supplies exact native reviewer assignments and paths. |
| `src/adlc_workflow/context.py` | Prepares context with profile filtering, latest-version resolution, and overlay selection. |
| `skills/rhai-feature-refine-worker/SKILL.md` | Uses a captured source request when supplied; otherwise fetches Jira. Some paths explicitly use `/workspace`. |
| `templates/rhai-feature-creator/feature-template.md` | Production artifact instructions, including preserving uncertainty and avoiding invented numerical requirements. |
| `tests/unit/test_controller.py` | Examples of request fixtures and fake worker completions. |

Additional background outside this project:

- `../supporting-docs/strat-evals.md`.
- `../checkouts/opendatahub-io/agent-eval-harness/agent_eval/agent/base.py`.
- `../checkouts/opendatahub-io/agent-eval-harness/docs/opaque-cli-runner-contract.md`.
- `../checkouts/gitlab_rhel_agentic_ci/strat-creator-eval/.gitlab-ci.yml`.

Borrow concepts, not an execution dependency on either checkout. The current
GitLab adapter sets candidate and judge model roles together; do not reproduce
that behavior. The evaluation judge must be configured independently.

Important existing limitations to account for:

1. `WorkflowCore.start()` prepares context and `advance()` can publish. Do not
   call the workflow controller just to obtain an isolated worker artifact.
2. `ClaudeWorkerRuntime` invokes a new `claude -p` subprocess using a constructed
   prompt; reading a skill file this way is not native forked skill invocation.
3. The context manifest's current `files_sha256` hashes the list of filenames,
   not their contents. Preserve it, but compute a separate eval content hash.
4. `prepare_context()` clones using `--branch`. Do not pass an arbitrary commit
   SHA as `ref` and assume it works. Freeze prepared content once, as below.
5. Production usage aggregation is not sufficient for eval accounting: it can
   omit failed sessions and assumes a particular terminal-event shape. Preserve
   raw evidence and normalize it explicitly in the eval layer.

## 4. Proposed directory layout and ownership

```text
evals/
  implementation-plan.md
  README.md
  .gitignore
  .env.example
  requirements.txt
  podman-compose.yaml
  scripts/
    eval.py                    # host CLI; thin bootstrap into eval_lib
    container-entrypoint.sh    # install plugin and execute one worker or judge
    run-worker.py              # container-only bounded worker launcher
    run-judge.py               # container-only assessment launcher
  eval_lib/
    __init__.py
    cli.py                     # argument parsing and command dispatch
    config.py                  # typed validation and path resolution
    snapshots.py               # freeze resources, manifests, content hashes
    collection.py              # schedule trials and manage containers
    production_bridge.py       # only module coupled to production internals
    records.py                 # JSON contracts and atomic record writes
    metrics.py                 # parse Claude streams and normalize usage
    checks.py                  # deterministic artifact/input checks
    judging.py                 # pair construction, blinding, judge validation
    reporting.py               # JSON and Markdown aggregation
  experiments/
    rhai-feature-refinement.example.yaml
  suites/
    rhai-feature-refinement.yaml
  judges/
    refinement-pairwise.md
  schemas/
    experiment-v1.json
    execution-v1.json
    judge-result-v1.json
    comparison-v1.json
  cases/
    mcp-registry/
      case.yaml
      source-issue.json
      judge-notes.md
  tests/
    test_config.py
    test_snapshots.py
    test_collection.py
    test_production_bridge.py
    test_metrics.py
    test_checks.py
    test_judging.py
    test_reporting.py
    fixtures/                  # recorded stream fragments and synthetic docs
  .cache/                      # ignored; expendable staging cache
  results/                     # ignored; durable local experiment records
```

This is one small tool, not a separately published framework. Use standard
library Python plus PyYAML and jsonschema. Keep dependencies in
`evals/requirements.txt`; do not modify production package dependencies.
Use stdlib unittest for offline tests. Import bootstrapping should work from
any host cwd, based on the script's own location.

Allowed implementation changes outside `evals/`: none expected. If a production
interface proves impossible to reuse, document the concrete problem before
proposing a separate production refactor. Do not expand implementation scope
merely to turn private functions into public APIs.

## 5. Host CLI contract

All example commands run from the `adlc-workflow` project root. They are new
commands to implement, not commands that already exist.

```bash
python3 evals/scripts/eval.py validate --config evals/experiments/local.yaml

python3 evals/scripts/eval.py prepare \
  --config evals/experiments/local.yaml --experiment-id refinement-001

python3 evals/scripts/eval.py collect \
  --experiment evals/results/refinement-001 --candidate A

python3 evals/scripts/eval.py collect \
  --experiment evals/results/refinement-001 --candidate B

# Recommended normal execution: both candidates, with balanced run order.
python3 evals/scripts/eval.py collect \
  --experiment evals/results/refinement-001

python3 evals/scripts/eval.py check \
  --experiment evals/results/refinement-001

python3 evals/scripts/eval.py judge \
  --experiment evals/results/refinement-001 --evaluation-id judge-001

python3 evals/scripts/eval.py report \
  --experiment evals/results/refinement-001 --evaluation-id judge-001
```

`validate`, `check`, and `report` perform no inference. `prepare` may fetch context
once if requested; it performs no inference. `collect` invokes candidate models.
`judge` invokes only the judge. Report the planned case/trial/judge counts before
inference without adding an interactive confirmation requirement.

Additional options:

- `collect --dry-run`: print resolved mounts, model, worker, ordered trials, and
  redacted command arguments without starting containers or altering trials.
- `judge --judge-model MODEL`: override only the evaluator for this new
  evaluation ID; record it in that evaluation manifest.
- `judge --prompt-file PATH`: snapshot a revised judging prompt into a new
  evaluation; never change the original experiment or old evaluation.
- `collect --candidate A|B`: restrict collection to one side. Record actual order
  so reports do not claim balanced scheduling if A was collected in bulk first.

Do not add a run-everything command until these stages work independently.

Exit codes: 0 for a completed command with no relevant errors; 1 for candidate
or evaluation failures found by that command; 2 for invalid configuration,
incompatible input, or setup errors; 130 for interruption. A report of valid
results does not fail merely because B beats A. Always write available records
before returning nonzero. Invalid comparisons must never produce a winner.

## 6. Experiment and fixture configuration

Define a strict `schema_version: 1` contract; reject unknown options rather than
silently ignoring misspelled model or judge settings. The example configuration
must have visibly replaceable model IDs, not pretend an unverified model is
available. Require actual configured model IDs for collection.

```yaml
schema_version: 1
name: rhai-feature-refinement-model-ab
suite: ../suites/rhai-feature-refinement.yaml
target:
  worker: skill:rhai-feature-refine-worker
  profile: rhai-feature-creator
  invocation: controller-worker
candidates:
  A:
    model: REPLACE_WITH_MODEL_A
    accepted_resolved_models: [REPLACE_WITH_CANONICAL_MODEL_A]
  B:
    model: REPLACE_WITH_MODEL_B
    accepted_resolved_models: [REPLACE_WITH_CANONICAL_MODEL_B]
cases: [../cases/mcp-registry]
repetitions: 3
execution:
  order: alternating
  worker_timeout_seconds: 900
  startup_timeout_seconds: 120
  container_image: adlc-claude-task-runner:local
context:
  prepared_workspace: /absolute/path/to/prepared/workspace
judge:
  model: REPLACE_WITH_FIXED_JUDGE_MODEL
  accepted_resolved_models: [REPLACE_WITH_CANONICAL_JUDGE_MODEL]
  prompt: ../judges/refinement-pairwise.md
  timeout_seconds: 300
  evidence_max_bytes: 120000
  order_swap: true
```

Rules:

- Exactly two candidates named A and B in v1. Models must be explicitly set.
- Identical model IDs are allowed for an intentional A/A calibration run; label
  it as such. Do not require models to differ merely to satisfy the schema.
- Paths in experiment config resolve against its containing directory. A case's
  paths resolve against that case directory. CLI experiment paths resolve from
  caller cwd. Expand environment references only in the env file, not arbitrary
  strings in case text. Reject unresolved configuration placeholders.
- Require positive integer repetitions and positive timeouts. The collection
  concurrency is fixed to one; reject parallelism knobs in v1.
- Profile lookup uses production `resolve_profile_path`/`load_profile` against
  the frozen plugin resources; the name is the filename stem, not profile `id`.
- The context input above copies only `.context/` from that workspace; never
  import prior strategies, private state, Claude sessions, or reviews.
- Alternatively support `context: {prepare_from_profile: true}`. It calls the
  existing context preparer once during preparation against a fresh staging
  workspace. Make these two context options mutually exclusive.
- Record any branch ref and its resolved commit. After preparation neither
  candidate nor judge updates the context checkout.
- Reject a prepared context whose source repository, destinations, selected
  release, or filtering contract is incompatible with the experiment profile.
  Do not infer compatibility from a directory merely named `.context`.
- Do not expose production profile `mode: eval` as the meaning of this tool.
  That mode belongs to workflow publication policy, not this experiment runner.

Case metadata:

```yaml
schema_version: 1
id: mcp-registry
issue_key: RHAIRFE-1
source_issue: source-issue.json
judge_notes: judge-notes.md
judge_context_files:
  - architecture-context/architecture/rhoai-3.6-ea.2/PLATFORM.md
  - architecture-context/overlays/0018-catalog-admin-uis-in-model-registry.md
```

Paths in `judge_context_files` are relative to the frozen `.context/` directory.
Validate them against the actual prepared context. These example paths must be
verified when creating the fixture; do not guess missing files.

The first fixture should use the synthetic MCP registry RFE already represented
in `scripts/seed-example-rfe.sh`, materialized as JSON with `key` and `fields`.
It must not contact Jira. Keep title, original description, labels, and relevant
fields; give source preservation checks precise expected text.

`judge-notes.md` contains expected requirements and known ambiguities, not a
model-produced reference treated as unquestionable truth. Curate a small fixed
evidence list sufficient to assess those notes. Never show notes or reference
expectations to candidates. Include at least one ambiguity that should remain
an open question rather than become an invented product requirement.

The suite is separate from the experiment so judging criteria stay fixed while
models change. Its minimum configuration is:

```yaml
schema_version: 1
id: rhai-feature-refinement
version: 1
target_worker: skill:rhai-feature-refine-worker
checks:
  - completion_contract
  - assigned_artifact
  - template_sections
  - source_preservation
  - frozen_inputs
  - model_identity
dimensions:
  - requirements_fidelity
  - architectural_grounding
  - scope_and_coverage
  - testability
  - source_precedence
  - clarity_and_detail
```

Implement these check IDs directly in `checks.py`; do not introduce dynamic
code execution from YAML. Define each dimension in the judge prompt and require
the prompt/schema dimension IDs to agree with the suite. Template section
checking should use headings under the generated Strategy section and the SME
section, excluding template preamble sections such as Size Guide. Save the
resolved required-heading list in the experiment to make that check reviewable.

## 7. Snapshot preparation and comparable inputs

`prepare` creates a new experiment directory exclusively. Refuse an existing ID.
Do not write directly into the current normal runtime workspace.

Create these immutable experiment resources:

```text
results/refinement-001/
  experiment.json                  # effective configuration and provenance
  resources/
    plugin/                        # runtime-only frozen copy
    eval-runtime/                  # scripts + eval_lib used for this experiment
    context/.context/              # selected prepared context
    cases/mcp-registry/             # immutable input and judge-only materials
    suite.yaml
    judge-prompt.md
    file-manifest.json             # relative path, length, content SHA-256
  schedule.json                    # case, repetition, candidate order
  trials/...                       # populated by collect
  evaluations/...                  # populated by judge
```

Plugin snapshot allowlist: `.claude-plugin`, `skills`, `agents`, `scripts`,
`src`, `config`, `templates`, `schemas`, `context`, `policies`, `plugins`,
`adapters`, `CLAUDE.md`, and `pyproject.toml` when present. Explicitly exclude
`evals`, `workspace`, `artifacts`, `.adlc`, `.git`, `.env`, `.venv`, caches,
bytecode, tests, documentation, and other runtime output. Inventory the runtime
references and fail if a required resource is absent; extend the allowlist only
for a documented dependency.

The eval-runtime snapshot contains only execution code needed by containers.
Do not include cases, judge notes, experiment configuration, or result folders
in the candidate-visible `/opt/adlc-evals` mount.

Do not copy `.git` history or credentials. For plugin/context symlinks, accept
only targets within the allowed source root and materialize their contents;
reject escapes and record the normalization. All manifest entries are sorted
relative paths with hashes of actual file bytes. Capture executable permission
bits for scripts. A directory digest covers paths, content hashes, and relevant
mode bits, not timestamps or absolute host paths.

Record plugin Git HEAD, dirty status, actual exported-content digest, eval code
digest, prepared context content digest, source revisions, image ID, Python
version, and later the actual Claude version. A dirty checkout is acceptable;
the frozen content hash, not just its commit SHA, identifies what was tested.

Existing context metadata must be internally consistent: required destination,
LATEST_VERSION, selected overlays, and usage guidance all exist. Do not quietly
substitute an empty context tree. Treat synthetic tiny test context as a
separately labeled fixture, not a substitute for real evaluation context.

The candidate input signature includes worker instructions, profile/template,
source issue, complete staged context, prompt bytes, invocation form, image,
and shared execution settings. The requested model is the intended varying
factor and is excluded from that equality signature. Audit actual versions too;
Claude auto-update or different runtime settings must invalidate comparability.

Resolve a mutable container tag to a local immutable image ID before trials.
Do not rebuild or pull it separately for A and B. Document the existing build
command (`podman build -f Dockerfile.claude -t adlc-claude-task-runner:local .`)
as setup, separate from execution.

## 8. Workspace isolation and container specification

Every triple `(case_id, repetition, candidate)` owns a directory:

```text
trials/mcp-registry/repetition-01/A/
  launch.json                 # host-owned model/settings/container identity
  input/                      # host source of the nested read-only input mount
  container.stdout.log        # host capture of entrypoint/runtime output
  container.stderr.log
  execution.json              # finalized host-side normalized record
  checks.json
  workspace/
    .context/                 # prepared snapshot mounted read-only here
    .adlc-eval/
      input/                  # staged worker request/task, read-only mount
      attempt/                # production runtime logs and command.json
      prompt.txt
      events.jsonl
      completion.txt
      worker-result.json
    artifacts/...             # production profile-selected output paths
```

Mount the trial's host workspace at `/workspace:rw`, then mount immutable
context at `/workspace/.context:ro` and immutable trial inputs at
`/workspace/.adlc-eval/input:ro`. Precreate mountpoints. Each trial has its own
input directory outside the writable workspace, as shown above.
No previous output artifact may exist when the worker starts.

The host workspace's `.context` mountpoint will be empty after container exit:
the context remains under `resources/context/.context/`. Offline tools must
resolve recorded container paths through the mount map in `launch.json`, not
assume nested bind-mounted files were copied into the trial workspace. Store
host references relative to the experiment root so an experiment can be moved.

New `evals/podman-compose.yaml` has two independent one-shot services, `worker`
and `judge`, without dependencies between them. The `worker` service has:

- Existing image, overridden with the frozen image ID by the host runner.
- `userns_mode: keep-id`, user matching the existing evaluator setup, cwd
  `/workspace`. Verify ownership with a write probe before paid execution.
- Frozen runtime plugin mounted read-only at
  `/home/evaluator/.claude/plugins/adlc-workflow`.
- Eval runtime snapshot mounted read-only at `/opt/adlc-evals`.
- Current trial workspace and nested readonly input/context mounts only.
- ADC file mounted at the existing expected path, read-only. Use the working
  UID/readability pattern; never chmod the user's original credentials file.
- Explicit Vertex environment variables matching normal container execution.
- No Jira service, ports, `depends_on`, shared home volume, shared Claude state,
  Docker/Podman socket, host result root, or host repository root mount.
- A fresh container filesystem/home for each invocation. No `--resume` or
  persisted conversation/cache volume. Provider-side prompt caching can still
  occur; measure it rather than claiming a fully cold provider cache.
- `entrypoint: ["/bin/sh", "/opt/adlc-evals/scripts/container-entrypoint.sh"]`.
  The entrypoint installs the plugin through the same marketplace commands as
  the existing entrypoint, then `exec`s the provided Python worker command.
  It must not execute the idle loop.

Use explicit, required bind-mount variables rather than fallback to the normal
workspace. Validate all paths on the host before passing them to compose.
Provide `evals/.env.example` for Vertex project/location and host ADC path;
ignore `evals/.env`. Authentication settings do not belong in result manifests.
Do not source arbitrary shell code such as the user's Vertex wrapper.

The host runner invokes compose with an absolute `-f` path, explicit env-file,
unique project name, and a fresh `run --rm --no-deps` container per trial.
Check the installed podman-compose syntax with `--help` during implementation.
Use subprocess argument lists, not shell interpolation. Supply the exact trial
mount paths via a per-call environment. Never `exec` into the existing service.
Keep required worker-only variables out of the judge service and vice versa.
Compose may interpolate the entire file even when only one service is selected;
provide validated harmless mount placeholders for the unselected service, or
use a generated role-specific override so unused required-variable expressions
cannot prevent launching the selected role. Cover this with a compose smoke test.

Handle interrupt/timeout by stopping only the recorded container ID/name and
its subprocesses. Do not run a broad `compose down`, delete volumes, or remove
other trials. Retain partial logs. Set a host total timeout covering startup,
worker time, and bounded shutdown; terminating the compose client alone may
leave a container running, so verify cleanup explicitly.

Candidates cannot see results or judge notes through the plugin mount because
that mount contains only the allowlisted runtime snapshot. Containers still
need network access for inference and installation; do not describe this mount
isolation as a general-purpose network security sandbox.

## 9. Reuse production worker execution without embedding evals in the core

Implement all coupling in `eval_lib/production_bridge.py`. Load production code
from the frozen plugin's `src`, not the developer's live checkout. Reuse:

- `resolve_profile_path`, `load_profile`, `refine_template_path`.
- Stage lookup/output declarations and `ArtifactLayout`.
- `_skill_prompt` and `_validate_worker_completion` from `controller.py`.
- `ClaudeWorkerRuntime` and `RunReporter` for the actual subprocess.

These include private APIs. That is an explicit POC tradeoff: pin them to the
plugin snapshot and cover the adapter with contract tests. Fail with a clear
compatibility error if signatures or required fields change. Do not copy the
prompt strings, implement a second refinement prompt, or monkeypatch production
classes. Importing private helpers does not authorize changing production code.

Build a bounded task fixture using production envelope field names:

```json
{
  "schema_version": 1,
  "task_id": "eval-refine-mcp-registry",
  "run_id": "eval-worker",
  "work_id": "eval-mcp-registry",
  "issue_key": "RHAIRFE-1",
  "stage": "refine",
  "expected_revision": 0,
  "result_path": "/workspace/.adlc-eval/unused-result.json",
  "allowed_outputs": ["strategy_markdown"],
  "required_outputs": ["strategy_markdown"],
  "worker": "skill:rhai-feature-refine-worker"
}
```

Resolve the actual stage worker/output declarations from the profile and
validate they match this supported target; do not hardcode a second artifact
layout. These IDs are fixture identifiers, not active workflow state. Keep them
identical across A/B and repetitions for the same case so prompt text does not
leak candidate labels or change unnecessarily. Experiment identity lives in
host metadata. No task is submitted and no real core state is created.

The captured request file uses the existing request shape:

```json
{
  "args": ["RHAIRFE-1"],
  "kwargs": {
    "profile": "config/rhai-feature-creator.yaml",
    "source_issues": {
      "RHAIRFE-1": {"key": "RHAIRFE-1", "fields": {"summary": "...", "description": "..."}}
    }
  }
}
```

Do not put `source_issues` at the top level or accidentally supply the full
experiment metadata as the captured request. The fixture must provide the
complete supported source fields, not the ellipses above.

Container worker algorithm:

1. Read the candidate-free assignment and captured request; validate all paths.
2. Resolve the frozen profile, template, and declared output path under
   `/workspace`. Ensure expected output is absent.
3. Build `_skill_prompt(...)` with the exact captured-request path. Save prompt
   bytes and hash. This prevents the skill's fallback Jira lookup.
4. Bind `RunReporter("json")` to `.adlc-eval/events.jsonl` before runtime starts.
5. Construct `ClaudeWorkerRuntime` with explicit candidate model and timeout.
   Use its normal container permission behavior consistently across A and B.
6. Invoke it exactly once; save completion text and raw attempt logs.
7. Validate the compact completion with the production helper and validate the
   expected artifact is a nonempty regular file inside the assigned root.
8. Write a structured worker result in success/failure paths. Timeout or missing
   `exit.json` must still produce host-side failure evidence.

Do not call `HandoffController.run()`, `WorkflowCore.start/advance/submit`, Jira,
the review dispatch skill, aggregation, or publication. Do not rewrite the
profile to remove stages just to execute a single stage.

## 10. Collection scheduling, preservation, and model verification

Default ordering: for sorted cases, execute each repetition's complete pair;
alternate A→B and B→A by pair index. Store this schedule before execution.
The repetition number pairs observations but is not a promise of a shared
provider random seed. Record random seeds only if actually supported/set.

For every launch, record requested model, accepted resolved IDs, CLI/image
versions, exact redacted argv, start/end timestamps, container identity, worker
wall time, and full container wall time. Keep installation overhead separate
from worker execution time. Hold permissions and model-independent settings
constant. Reject unsupported effort/budget options instead of claiming they
were enforced. The current production runtime has no per-call budget argument;
v1 must document timeouts and sequential scheduling, not promise a dollar cap.

Resolve the model actually used from session/model-usage evidence. An alias is
acceptable only if it resolves to one of the candidate's explicitly accepted
IDs. Unavailable models, hidden fallbacks, mixed unexpected models, or missing
model evidence must not be counted as successful execution of the requested
candidate. Store requested and observed IDs and mark comparison eligibility.
Do not silently change the judge to whichever candidate model is available.

Acquire an exclusive per-experiment collection lock. On another invocation,
skip terminal trials whose metadata and artifact hashes still verify. Do not
overwrite them. A previously running trial with no active recorded container
becomes interrupted; do not automatically rerun it. To repeat, create a new
experiment ID. Complete the other scheduled trials after an individual worker
failure; stop for shared setup failures such as unreadable credentials.
Collection must use the frozen eval-runtime snapshot as well as the frozen
plugin. If the host collection code changes, refuse to continue that experiment
unless it matches the recorded collection-code digest; creating a new experiment
is the v1 recovery path. Rejudging may use revised evaluator code, recorded under
its new evaluation ID, without changing the collection provenance.

Final `execution.json` minimum fields:

- `schema_version`, experiment/case/repetition/candidate identity.
- Target worker, profile, invocation kind, common input signature, prompt hash.
- Requested/observed models, model-match status, image and runtime versions.
- Status: `succeeded`, `failed`, `timed_out`, `interrupted`, or `setup_failed`.
- Failure kind/detail, container exit code and worker exit code when known.
- UTC timestamps, `worker_duration_seconds`, `container_duration_seconds`.
- Artifact paths relative to the trial directory, byte counts, content hashes.
- Normalized usage/cost fields, their evidence source, and availability status.
- Relative paths to raw logs and completion record.
- `comparison_eligible` and reasons; kept distinct from process success.

Write JSON atomically with temp-file-and-rename. Host finalization checks the
actual artifact and logs even if the container claims success. Keep unknown
cost/tokens as `null`, not zero. Setup failure is not evidence of poor strategy
quality, and a contract failure is not a judge-model failure.

## 11. Usage and timing normalization

Capture both raw Claude stream JSONL and controller-wrapped events. The host
container stdout is a different log and may contain plugin installation text.
Do not feed its entire contents to a JSON parser as one document.

Normalize only terminal result usage, keyed by session identity. For cumulative
result records from the same session, use the final cumulative record, not the
sum of intermediate results. Do not count a nested session twice when its cost
is already included in the parent aggregate. The first supported worker path
should have one subprocess session; explicitly flag unexpected delegation.

Record input, output, cache-read, cache-creation, and thinking tokens separately,
plus per-model usage where present. Do not add thinking tokens to output totals
unless the runtime explicitly states they are disjoint. Do not derive dollars
from an invented static pricing table. Record reported cost and its basis if
provided; otherwise unavailable. Preserve usage from failed calls when present.

Judging cost and duration belong to the evaluation record, separate from
candidate costs. Reports should show individual trials and medians/ranges by
candidate. A fresh container does not eliminate server-side cache effects;
display observed cache usage and actual scheduling order.

## 12. Deterministic checks before semantic judging

`check` loads saved resources and trial outputs; it does not load current
production files from the developer checkout or invoke a model. The same
checks run before `judge` admits a pair. Version the check suite.

Required checks for refinement:

1. Successful terminal worker execution with valid completion issue key and
   assigned artifact path, matching production completion validation.
2. Exactly the expected target artifact exists and is nonempty; an arbitrary
   markdown file elsewhere does not count. Record extra generated artifacts.
3. Required Strategy heading and template section headings are present. Parse
   Markdown headings sufficiently to avoid accepting heading text inside code
   fences. Do not use a minimum word count as a proxy for useful substance.
4. Original RFE summary and description are preserved in Business Need. Define
   the fixture contract precisely: heading wrapper and outer whitespace may
   differ; original text bytes after line-ending normalization must be retained
   in that section. Accommodate nested headings in an RFE description by using
   the unique generated Strategy boundary, not the next arbitrary `##` heading.
5. Applicable protected source text is unchanged. Only assert preservation for
   content actually supplied to this worker; do not claim the initial-create
   fixture tests iterative preservation of an existing SME section.
6. Read-only inputs/context and frozen plugin hashes match the prepared record.
7. Model evidence matches the configured candidate and the two trials have
   compatible inputs and execution form.

Tool traces may additionally report successful context reads and prohibited
workflow/Jira activity. Treat evidence of an actual prohibited call as a
contract failure; textual mentions are not calls. An absent trace is unknown,
not proof of compliance. A successful Read does not prove architectural
grounding; semantic evidence is evaluated separately.

Checks produce `pass`, `fail`, or `unknown` with evidence locations. Contract
ineligibility excludes that pair from semantic judging but remains visible in
failure counts. Do not label missing output as an LLM judge preference for the
other side, and do not hide it from experiment denominators.

## 13. Pairwise judging, isolation, and reproducibility

The judge is fixed and independent of candidate selection. A candidate model
may equal the configured judge model, but record that fact; do not imply a
different model automatically provides objective ground truth.

For each eligible `(case, repetition)` pair:

1. Build a judge bundle containing the captured RFE, relevant template/rules,
   curated notes, fixed selected context files/overlays, and both strategies.
2. Label documents X and Y. Do not expose model names, A/B directories, token
   counts, costs, timings, candidate log traces, or the mapping to the judge.
3. Record mapping host-side. Send one X/Y presentation and a second presentation
   with their positions swapped (`order_swap: true`). Each uses a fresh judge
   session. The judge must not see the other judge response.
4. Ask for dimension preferences, an overall preference, and evidence-based
   rationale. Permit ties, neither acceptable, and insufficient evidence.
5. Validate strict structured output and map preferences back to A/B only after
   collection. Retain prompt, raw stream, parsed result, and mapping.

Use a separate fresh judge container and the fixed image, with only judge
execution code and its current evidence bundle mounted. No candidate workspaces,
other results, or candidate model configuration should be visible. It does not
need the ADLC plugin. The eval entrypoint must distinguish `worker` (install
plugin) and `judge` (skip plugin installation) explicitly; reject other roles.
The separate compose `judge` service mounts `/opt/adlc-evals`, a current blinded
evidence directory read-only, a current judge output directory read-write, and
the same ADC credential path. It must not inherit the worker's plugin, source
request, or full context mounts. Include its own fresh writable cwd/home.

The judge can be a plain `claude -p` call with tools disabled using syntax
verified against the installed CLI. Deliver the fixed bundle as prompt input;
no autonomous document searching or workflow tools. Treat candidate documents
as quoted evidence, never instructions to the evaluator. Verify no tools ran.

Use the case's exact evidence list for both candidates and both presentations.
Do not truncate documents silently to meet context limits. Validate configured
`evidence_max_bytes` and fail with `evidence_too_large` before inference when
exceeded; it is a byte guard, not a claim of exact token/context accounting.
Model context-limit errors remain judge errors. If evidence must be narrowed,
make a new explicit suite/case revision rather than selectively summarizing one
candidate. Recheck expected context/version and overlay content in the bundle.

Judge criteria, equal priority unless the suite explicitly versions weights:

- Fidelity to source requirements and preservation of unresolved questions.
- Useful, coherent architectural decisions supported by supplied context.
- Requirements coverage without unrelated scope expansion.
- Testable acceptance criteria without fabricated mandatory numerical targets.
- Correct application of human overlay corrections and source precedence.
- Appropriate strategy-level detail, clarity, and concision.

Judge each candidate's acceptability as well as preference. Longer prose,
confident claims, more citations, and higher self-reported review scores are
not automatic advantages. Require specific excerpts or section references for
material findings. Known ambiguity resolved by invention should be penalized.

Proposed judge response fields:

```json
{
  "schema_version": 1,
  "preference": "X",
  "acceptability": {"X": "acceptable", "Y": "unacceptable"},
  "dimensions": [
    {
      "id": "requirements_fidelity",
      "preference": "X",
      "rationale": "...",
      "evidence": [{"document": "Y", "section": "...", "excerpt": "..."}]
    }
  ],
  "rationale": "..."
}
```

Allowed preference values: `X`, `Y`, `tie`, `neither`, `insufficient_evidence`.
Acceptability values: `acceptable`, `unacceptable`, `uncertain`. Require every
suite dimension exactly once. Validate enums, required fields, referenced
document IDs, and quoted excerpts. Malformed output is a judge error, not a tie;
do not silently use another LLM to repair it.

After swapping, aggregate conservatively: agreement on A/B/tie/neither becomes
that pair outcome. Apply precedence explicitly: missing/invalid judgment means
`judge_error`; otherwise either `insufficient_evidence` means that outcome;
otherwise disagreement means `inconclusive`; otherwise use the agreed mapped
outcome. Do not
pick whichever presentation favors a preferred model. Both-unacceptable outputs
must not become a deployment recommendation merely because one is less bad.

Evaluation IDs are immutable. To rejudge, create `judge-002`; record judge model,
actual model ID, prompt hash, suite/check versions, evidence hashes, input/output
artifact hashes, and evaluator code digest. Existing collected artifacts and
prior evaluation files remain untouched. Support standalone judging/reporting
from a copied complete experiment directory, with no running candidates or Jira.

## 14. Reports and interpretation

Produce machine-readable `comparison.json` and human-readable `report.md` under
`evaluations/<evaluation-id>/`. No HTML/dashboard is required.

Report at least:

- What worker/invocation, models, plugin revision, context revision, cases, and
  repetitions were evaluated; note any dirty snapshot or model mismatch.
- Planned, attempted, succeeded, and contract-valid trials for each candidate.
- Eligible pairs versus excluded pairs with explicit reasons.
- A wins, B wins, ties, neither, inconclusive, insufficient-evidence, and judge
  errors, with denominators. Do not report missing data as zero-score quality.
- Candidate costs, durations, cache tokens, and unavailable metrics separately
  from judge cost and container startup overhead.
- Dimension-level findings and links to trial artifacts and judge rationale.
- Per-case results so easy cases/repeated trials do not obscure weak cases.
- Scheduling order and whether collections were interleaved or all A then B.

Report a decisive win fraction only as `A_wins / (A_wins + B_wins)`, with the
denominator next to it and `null` if there are no decisive pairs. Also show all
other outcomes. Do not treat repetitions of one RFE as independent coverage of
many RFEs. For v1 use descriptive counts and medians/ranges; no automatic global
winner, blended cost-quality score, or significance claim is necessary.

The first fixture proves the mechanics. It cannot establish which model is
better across the product. Subsequent datasets should vary scope, ambiguity,
source completeness, and architectural constraints rather than duplicate the
MCP registry text with a different title.

## 15. Tests and acceptance criteria

Tests should exercise failure-prone behavior, not just assert configuration
strings. Offline tests must run without Podman, Vertex credentials, Jira, or
model access. Inject a subprocess/container interface and use recorded Claude
streams; fake output records must use the same schemas as real records.

Required offline coverage:

1. Relative path resolution from arbitrary cwd; invalid models, unknown config
   keys, missing context, invalid IDs, traversal, and symlink escapes rejected.
2. Plugin export excludes credentials, `evals/results`, current workspace and
   judge materials. Content changes with unchanged filenames change the digest.
3. A/B receive equal prompt/input hashes and different writable workspaces; no
   previous artifact exists. Source snapshots and plugin revision stay fixed.
4. Interrupted/failed runs preserve logs and terminal records. Timeout cleanup
   targets only the active container; completed trials cannot be overwritten.
5. Stage metadata/artifact paths and `_skill_prompt` usage match production.
   Mock workflow start, Jira, publication, and extra worker calls to fail if used.
6. Production completion shape, missing/wrong artifact path, empty artifact,
   source mutation, and nested RFE headings are handled correctly.
7. Cumulative terminal events are not double-counted; failed-session usage,
   unknown costs, cache counters, and model mismatch remain distinguishable.
8. Judge bundle contains fixed evidence and no candidate identity/cost/mapping.
   Swapped presentation mapping works; disagreements and errors are not ties.
9. Rejudging performs no candidate invocation or source fetch, writes a new
   evaluation ID, and does not mutate collected artifacts.
10. Report counts include failures, incompatible pairs, ties, neither, missing
    usage, and per-case outcomes with accurate denominators.

Container smoke test before paid evaluation:

- Use the existing image and eval compose file. Run plugin installation with
  the real Claude CLI, then inject a fake executable only at the worker runtime's
  `claude_bin` boundary for inference. This checks actual plugin registration
  without paid worker calls. Do not ship a fake executable in the real image.
- Verify effective UID, ADC-path readability without printing contents, plugin
  installation, cwd, mount access, and completion/exit propagation.
- Write an A marker; B cannot see it. Neither candidate can read judge notes,
  the parent experiment directory, or the normal integration workspace through
  mounted paths. Verify fresh home/session state.
- Confirm no Jira container starts and no production Jira operation occurs.
- Verify successful exit ends the container; no idle loop survives.
- Verify failed/timeout cleanup and that host artifacts survive container removal.

Live acceptance, explicitly identified as paid inference in the run instructions:

- One synthetic case, one repetition, two configured available candidate
  models, and one fixed configured judge: two worker executions and two judge
  executions for swapped presentation.
- Both workers use their requested accepted model IDs and the same input hash.
- Each produces its own strategy in its own preserved workspace.
- Result logs/usage and comparison report are readable and internally consistent.
- Rejudge under a new evaluation ID: no new worker sessions, unchanged strategy
  hashes, separate judging cost.
- Then run three repetitions with alternating order to exercise the full schedule.

If credentials/model access are unavailable, finish offline and container checks
where possible and report live acceptance as unverified. Never substitute fake
responses while describing a real model comparison as passed.

## 16. Implementation order for the receiving agent

Complete each step before broadening scope. Keep the normal workflow untouched.

1. **Foundation:** add README, eval-only dependencies/ignore/env example, config
   validation, record schemas, fixture, and pure filesystem snapshot helpers.
   Verify content hashing and runtime export exclusions.
2. **Frozen preparation:** implement `validate` and `prepare`, plugin/image/context
   provenance, fixed schedule, and exact candidate-free request/task staging.
   `prepare` should print the complete expected execution count and paths.
3. **Worker bridge:** implement the eval-local adapter and `run-worker.py` using
   production helpers. Validate with fake runtime and real profile resources.
4. **Container execution:** add separate compose and one-shot entrypoint; implement
   `collect --dry-run`, actual collection, timeouts, host logs, and immutable
   completion records. Run the container smoke tests.
5. **Checks and metrics:** implement offline `check`, terminal usage/model evidence
   normalization, contract eligibility, and failure-preserving collection exit.
6. **Judge collection:** add independent model config, fixed evidence bundles,
   swapped blind presentations, strict result validation, and rejudge isolation.
7. **Comparison:** implement deterministic pair outcome reduction and JSON/Markdown
   reporting. Ensure unavailable data remains visible.
8. **Live proof and documentation:** run the small comparison when credentials
   permit; document exact setup and commands, limitations, and observed results.

Useful verification commands to document after implementation:

```bash
python3 -m unittest discover -s evals/tests -v
python3 evals/scripts/eval.py validate --config evals/experiments/local.yaml
python3 evals/scripts/eval.py collect --experiment evals/results/refinement-001 --dry-run
git diff --check
```

The README must distinguish existing container image setup, host Python setup,
one-time context preparation, candidate collection, and repeatable judging.
Provide copyable commands with explicit env/config paths and no dependency on
the caller's current directory. Explain that normal compose and eval compose
are separate stacks and that changing an env var during `exec` cannot remount
an already running container.

## 17. Definition of done

The initial build is complete when:

- Everything new lives under `evals/`; production workflows have no new eval
  imports, config keys, dependencies, or CLI commands.
- A and B can be collected independently or in an alternating schedule from
  one frozen experiment, using unique host mounts and fresh containers.
- Exactly one production refinement worker is invoked per trial, with the same
  instructions/context and an explicitly selected model.
- Inputs, runtime revisions, actual models, artifacts, failures, durations, and
  usage are recorded sufficiently to explain whether a comparison is valid.
- Deterministic checks and independent blind pairwise judging work on saved
  artifacts without executing ADLC again.
- Judge/model changes create separate evaluation records, preserving originals.
- Offline tests and container isolation tests pass; live status is stated
  accurately with no unsupported claim of model superiority.

## 18. Follow-on: individual reviewer A/B evaluation

After refinement is complete, add a suite for
`agent:rhai-feature-scoring-reviewer`, then specialist reviewers as needed.

Each case supplies a fixed strategy artifact, prepared context, and the
production rubric. Stage the same strategy for A and B; do not first refine it
with their respective models, which would confound reviewer quality with input
quality. Resolve the exact assignment via `review_plan`, select the configured
reviewer ID, and reuse `_agent_prompt`. Execute only that reviewer.

Extend deterministic checks for required output paths, parseable scoring,
score/verdict consistency, and forbidden cross-review reads. Curated judge-only
annotations identify known defects and acceptable interpretations. Pairwise
criteria assess missed defects, false alarms, evidence quality, and useful
recommendations; the production reviewer's own total is not ground truth.

Native skill invocation and full workflow runtime comparisons are separate
future experiment types. They must record their invocation form and use
appropriate correctness checks. Do not claim that success of this subprocess
worker evaluation establishes equivalence across all three runtime modes.
