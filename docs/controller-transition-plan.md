# ADLC controller transition plan

## Objective and agreed behavior

Add a Python controller that drives the existing core and invokes Claude
subprocesses for LLM work. Keep the current Claude-driven orchestration
functional and optional. Both controllers share the same workflow definition,
gates, task/result protocol, artifact layout, and publication code.

Support three entry routes:

1. The existing plugin skill drives the run through Claude tools.
2. The plugin skill receives `--handoff`, launches the Python controller once,
   waits for it, and reports its compact terminal result.
3. A standard Python CLI runs the same controller directly, without an outer
   Claude process. The low-level protocol commands remain available as well.

Handoff is synchronous for this POC. It is not a detached service. Issue work
remains sequential within a batch; independent reviewers may run in parallel.
Keep this transition scoped to the existing stages and profiles.

## Current implementation and gaps

- `cli.py` and the installed `adlc-workflow` console entrypoint already expose
  `start`, `advance`, `submit`, and `status`. They do not run a worker loop.
- `WorkflowCore.start()` validates admission, prepares repository context,
  evaluates intake gates, and records a run. `advance()` returns tasks or
  performs publication after all items reach the publication boundary.
- The low-level core is Claude-independent, but it is not free of side effects:
  starting a run can fetch context; advancing a ready run can write to Jira and
  the archive. Launch-mode policy still applies to direct CLI calls.
- `selection.py` freezes explicit keys. Its selector fields do not implement
  Jira discovery. Gate evaluation is not itself a Jira search API.
- `reviews.py` currently produces native Claude Agent-tool assignments.
  Aggregation and score extraction are already Python operations. The scorer
  reviewer itself still requires an LLM.
- Refinement uses a forked skill; review dispatch runs in the parent session.
  Some worker instructions still hardcode `/workspace`, the feature profile,
  or a fresh Jira read. They need shared parameterized inputs.
- `StateStore.lock()` protects individual core operations. Two controllers can
  still receive the same pending task and launch duplicate workers.
- Run state is scoped by run ID, but public artifacts and prepared context can
  collide across runs sharing a workspace.
- The console script is packaged, but the current setuptools configuration
  does not explicitly bundle profiles, templates, agents, skills, and schemas.
  A working installed CLI outside the checkout must address resource loading.
- Profile selection currently uses a path. The desired name is the filename
  stem, such as `rhai-feature-creator`; the profile's current `id:
  adlc-workflow` is a different field.

## CLI and selection contract

Use `handoff` as a subcommand of the existing Python CLI. These are proposed
interfaces; only the low-level commands exist today:

```bash
# Existing orchestration remains the default.
claude -p "/adlc-workflow:adlc-workflow RHAIRFE-1"

# Outer Claude delegates the complete run.
claude -p "/adlc-workflow:adlc-workflow --handoff --profile=rhai-feature-creator RHAIRFE-1"

# Direct invocation of the same controller; no outer Claude.
adlc-workflow handoff --profile rhai-feature-creator RHAIRFE-1 RHAIRFE-2
python -m adlc_workflow.cli handoff --profile rhai-feature-creator RHAIRFE-1

# A profile without keys requests discovery.
adlc-workflow handoff --profile rhai-feature-creator
```

Retain `start --request`, `advance --run`, `submit --run --task --result`,
and `status --run`, including existing option placement and root overrides.
New handoff options must work after the subcommand as shown above.

Selection rules:

- An explicit profile resolves exactly to `config/<name>.yaml` under the
  install root. Preserve existing install-relative profile paths for callers
  already using them; reject traversal and unknown profiles.
- Keys without a profile use `ADLC_PROFILE`, then the existing
  `rhai-feature-creator` default. Do not infer a profile from an issue prefix.
- Explicit keys are deduplicated in caller order and all pass the initial gate.
  Preserve the current rejection of a batch containing an ineligible explicit
  issue; report per-issue reasons.
- A profile without keys requests one bounded discovery pass. Require a
  profile supplied by the caller or environment for this form; bare
  `handoff` is a usage error.
- Resolve the profile once and record the canonical path in the request and
  run. Reject conflicting CLI/request profile choices.
- Support `--workspace`, `--install-root`, `--mode`, `--model`,
  `--batch-size`, and `--batch-offset`. Default workspace to cwd and mode to
  the current example's `local`; local mode can still publish to Jira.
  Production must satisfy existing trust/admission rules.

Discovery belongs in a reusable Jira selection adapter. Compile the supported
source-gate predicates into a candidate query with pagination and stable
ordering, then capture full issues and use the existing gate evaluator as the
authority. Hydrate parent/linked fields when required by a gate. Query predicates
must not accidentally omit eligible issues, including Jira empty-field cases.
If a gate cannot provide a bounded candidate query, fail with an actionable
message rather than searching every Jira project.

Apply batch offset/limit to eligible, deduplicated candidates. Record query
provenance, ordering, exclusions, and captured input snapshots; then pass
explicit frozen keys to the core. Never poll for additional work during a run.
An empty eligible selection returns a successful no-work summary without
launching workers or publishing. Discovery is a captured selection, not a
transactionally consistent snapshot of a changing Jira database.

## Shared implementation boundaries

Add `controller.py` in the Python package. It calls `WorkflowCore` APIs
directly. Keep subprocess mechanics in a separate Claude worker runtime module,
and discovery in a Jira selection adapter. The plugin is an adapter to these
same interfaces.

The core continues to own state transitions, gates, task identity/revisions,
submission validation, public stage-output persistence, and publication.
The controller owns orchestration, worker attempts, timeouts, and scheduling.
Workers receive reasoning assignments and write assigned documents; they do
not call the workflow controller, submit results, or publish.

Refactor review assignments into transport-neutral inputs with adapters for
native Agent-tool dispatch and subprocess dispatch. Preserve `skill:` and
`agent:` references and reuse the checked-in worker instructions. The native
review skill remains usable in the original mode. Handoff executes review
planning, scheduling, aggregation, and scoring in Python, invoking only the
individual reviewer reasoning in Claude.

Do not introduce a second stage graph or hardcode the feature reviewer list in
the controller. Reuse profile stages and the existing stage/output contracts.
Fail explicitly on unsupported workers or stages.

## First milestone: prove the Claude subprocess boundary

Before implementing the full controller, run a small, nonpublishing experiment
against the installed Claude CLI and current official documentation. Record the
CLI version, exact invocation, and observed behavior.

Prove all of the following:

- A script launched by an outer `claude -p` can run and await a child
  `claude -p`. Check nested-session restrictions and session environment;
  do not assume the complete parent environment can be forwarded unchanged.
- Children discover the mounted plugin from a separate runtime cwd, with the
  same Vertex configuration and readable credentials.
- The supported launch mechanism selects the intended skill or agent,
  including agent tools/model and skill fork behavior. Do not assume a
  namespaced agent is a slash command.
- The parent waits through a long worker without duplicating the command or
  timing out its tool call. Define progress and cancellation behavior.
- Two small reviewer processes actually overlap and terminate cleanly.

Choose a supported worker invocation based on that evidence. Avoid duplicating
prompts or inventing CLI flags. Record any launch limitation before declaring
the handoff route functional. If needed, add a minimal invocation adapter that
loads the same worker definition through a verified CLI mechanism.

## Controller loop and worker contract

1. Resolve resources, admission policy, effective runtime settings, and issue
   selection. Capture source issues once for both gates and refinement.
2. Start a core run; retain the run ID and context manifest. Pin profile,
   template, rubric, worker-definition digests and context commit identity for
   any later continuation.
3. Request the next core task and dispatch its configured worker using the
   shared task inputs.
4. For refinement/decomposition, launch a bounded worker session. For review,
   build assignments, launch independent reviewers according to
   `reviewer_execution`, and wait for all required reviewers.
5. Validate each completion and assigned artifact. Run existing aggregation
   and scoring code after the review barrier. No LLM is needed to concatenate
   findings or extract rubric totals.
6. Construct and submit the existing task-result envelope through core APIs.
   Accepted stage-output persistence stays in `WorkflowCore.submit()`.
7. Advance until the core reports `complete` or `blocked`. Publication stays
   behind the existing batch barrier and admission policy.

A worker receives explicit install/workspace/profile paths, frozen issue input,
task/run/work identity, required template/context/overlay paths, and an assigned
output path. Parameterize these inputs in both execution modes so neither
depends on hardcoded `/workspace` paths or a second Jira fetch.

Use an argv array, explicit cwd, and noninteractive stdin (`DEVNULL` when the
prompt is passed as an argument); allocate no TTY. Pass the required runtime
environment without copying credentials into prompts, profiles, or logs.
Permission settings must be explicit launch policy; do not universally inherit
the example script's `--dangerously-skip-permissions`.

Capture raw Claude stream JSON and stderr separately for every attempt. Parse
the final completion event using the verified CLI format. Require successful
process exit, successful Claude completion, a valid compact worker result, and
the expected nonempty artifact. Exit zero or file existence alone is insufficient.
Validate task identity, permitted output path, and current-attempt ownership;
reject stale files from earlier attempts.

Keep full markdown and child transcripts out of the outer Claude context.
Return paths, status, concise errors, and final publication receipts. Controller
stdout contains one final JSON summary; progress goes to stderr. A human stream
renderer is optional and never part of protocol parsing.

## Execution settings

Add an optional, validated profile block; existing profiles remain valid.
Proposed initial fields:

```yaml
controller:
  runtime: claude
  model: claude-haiku-4-5
  reviewer_parallelism: 5
  task_timeout_seconds: 900
  max_attempts: 1
```

Effective model precedence is explicit controller `--model`,
`ADLC_CLAUDE_MODEL`, profile default, then Claude's default. Pass an effective
model explicitly to child launches and verify how it interacts with
agent-level `model` declarations; normalize or reject conflicting declarations
rather than silently ignoring the selected model. Other runtime settings use
CLI overrides followed by profile defaults and documented code defaults.

The outer Claude model is not automatically the child model. Explicitly pass
it through the handoff arguments or environment. Parent argv inspection can be
a later experiment, but is not a portable default. Record effective settings
without secrets. Defer per-worker override hierarchies until a concrete need
justifies them.

Respect `reviewer_execution: sequential`; a parallelism limit must not turn a
sequential profile into a parallel one. Apply the cap only to parallel reviews.

## Ownership, failures, and recovery

Introduce execution ownership in addition to existing short state locks.
A handoff controller owns a workspace for its lifetime because artifact and
context paths currently overlap across runs. Detect conflicting mutations from
the original controller and low-level mutating commands as well. Permit
read-only status checks. Do not hold the existing state lock across subprocess
execution, which would prevent core calls from acquiring it.

For this POC, concurrent runs use separate workspaces. Run IDs alone do not
provide artifact isolation. Unique controller attempt directories belong below
the resolved private run/work/task state root; document root precedence and
reuse the state store rather than inventing another private layout. Worker
documents remain assigned public artifacts; controller logs and execution
metadata are private and excluded from publication bundles.

Default to one attempt. If retries are enabled, bound them and record each
attempt; retry only classified worker/runtime failures. A semantic REVISE
verdict is a successful review outcome, not a process failure or an automatic
refinement loop. Missing reviewers, malformed results, or failed aggregation
must prevent review submission and publication.

On timeout or interruption, terminate and reap the child process groups,
including grandchildren, and retain diagnostics. Add core methods for recording
worker failure/interruption; the controller must not edit state JSON directly.
Proposed handoff exits: 0 complete/no-work, 1 blocked execution, 2 invalid
invocation/configuration, and 130 interrupted. Preserve existing low-level CLI
exit behavior.

Do not automatically restart a run or retry publication after a failure.
Existing publication can partially succeed and its current in-memory receipt
list can be lost when a later adapter fails. Persist each effect receipt as it
succeeds and reconcile existing Jira markers before enabling publication
recovery. A completed Jira write followed by archive failure must be reported
as partial publication, not rolled back or reported as complete.

Automatic resume is deferred in the first POC. Keep IDs, snapshots, attempts,
and receipts sufficient for diagnosis. Any later explicit resume operation must
validate pinned inputs, retain run/task identity, reuse only validated completed
work, and reconcile effects before retrying. The current blocked state is
terminal; resumability requires a deliberate core API.

## Implementation order and acceptance

1. **Verify subprocess feasibility.** Complete the nonpublishing experiment
   above for both direct Python and outer-Claude launch. Establish the actual
   skill/agent and stream protocols before building around them.

2. **Unify CLI/resources and worker inputs.** Add profile-name resolution,
   explicit parameterized assignments, and direct controller CLI scaffolding.
   Preserve the existing protocol. Support the mounted checkout through
   `--install-root`; package required non-Python resources and test a built
   wheel from another cwd before advertising standalone installation.
   Resource precedence: explicit install root, installed resources; workspace
   defaults to cwd. Preserve the shell wrapper's checkout resource resolution.

3. **Deliver one explicit-key handoff end to end.** Add execution ownership,
   attempt logging, serial worker execution, failure recording, deterministic
   review aggregation/scoring, and publication through existing core APIs.
   Persist partial publication receipts. Prove the direct CLI path first,
   then make the plugin `--handoff` branch invoke that same command once.
   Keep the original skill loop operational throughout.

4. **Enable reviewer concurrency.** Use a bounded subprocess scheduler and the
   profile's execution setting. Prove overlap, completion barriers, failure
   handling, and child cleanup. Issues remain sequential.

5. **Add profile-only discovery.** Implement paginated candidate selection,
   hydration and gate filtering, deterministic ordering, batch bounds,
   exclusion evidence, and empty-selection handling.

6. **Exercise both controllers and document operation.** Add an explicit mode
   switch to the example runner while retaining the current default. Expose
   profile/model overrides and comparable per-run logs. Workspace/Jira reset
   remains an example-runner behavior; neither controller resets user data.
   Document invocation, exit codes, publication effects, and manual diagnosis.

Use fake Claude executables and fixture adapters for protocol, timeout,
concurrency, stale-output, and failure tests. Keep live-model experiments small
(one issue, then two) and separate from ordinary unit tests.

Required acceptance scenarios:

- Console entrypoint and Python module run the full controller from a runtime
  cwd outside the install; low-level protocol commands still work without a
  Claude executable. Offline tests inject context/publication adapters.
- CLI/profile defaults and model propagation agree across direct and handoff
  entrypoints; explicit keys never trigger discovery.
- Parallel reviewers overlap, honor the cap, and produce all required files;
  a failed reviewer prevents aggregation/submission and leaves useful logs.
- Competing controllers cannot dispatch duplicate work in a shared workspace.
- Discovery handles pagination, empty fields, duplicates, gate exclusions,
  stable batching, and zero eligible issues.
- Both orchestration modes complete against the Jira emulator and preserve
  profile-defined artifact names, reviewer coverage, gates, and receipts.
- Partial Jira/archive failures preserve known effects without duplicate
  ticket creation during any tested reconciliation.

Compare deterministic fixtures exactly after normalizing run IDs and
timestamps. For live Claude runs, compare contracts and required evidence,
not identical markdown, scores, or timing. LLM content is nondeterministic.

This plan does not add batch-item parallelism, continuous discovery, a new
workflow language, automatic review/refine cycles, automatic resume, or fullsend
integration.

## POC implementation status

Implemented in this checkout:

- `adlc-workflow handoff` and `python -m adlc_workflow.cli handoff` drive the
  existing core through explicit issue keys or a profile-gated Jira discovery
  snapshot.
- Profile names resolve from the configuration filename stem, including
  `rhai-feature-creator`.
- The controller captures Jira inputs, freezes selection evidence, acquires a
  workspace lease, invokes bounded Claude subprocesses, persists private
  attempt logs, validates terminal stream events, and rejects stale artifacts.
- Direct controller invocation emits live controller and tagged worker events:
  human rendering by default, or normalized JSONL via `handoff --json`. Each
  run retains the same event stream privately alongside state and raw worker
  transcripts.
- Reviewer subprocesses honor profile parallelism; aggregation and score
  extraction remain deterministic Python work.
- Offline controller coverage verifies explicit and discovered selection,
  stale-output rejection, the reviewer concurrency cap, and the shared-workspace
  execution lease.
- The plugin recognizes `--handoff`, while the original Claude-driven path is
  still the default. The example runner and opt-in integration test expose a
  controller-mode switch.
- Publication receipts are persisted incrementally in private state so a later
  publication failure retains already-observed effects.

Verified against the running compose stack on 2026-09-20: an outer Claude
session launched the handoff controller, which processed `RHAIRFE-1`, launched
five reviewer subprocesses, created `RHAISTRAT-1`, and wrote the local archive
receipt. The outer skill must use a long foreground Bash timeout; its default
short timeout can otherwise stop waiting even though the controller completes.

Remaining work is intentionally deferred: an explicit resume/reconciliation
command, workspace-safe concurrent runs, packaging immutable plugin resources
inside a standalone wheel, and stronger schema validation of reviewer prose.
Batch-item parallelism is now an opt-in controller capability via
`--item-parallelism`; publication remains a batch barrier.
