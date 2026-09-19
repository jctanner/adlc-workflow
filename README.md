# adlc-workflow

Skeleton for the deterministic ADLC workflow proposed in
`proposals/013-fullsend-strat-workflow/proposal.md`.

This tree defines the intended boundaries only. Implementation, deployment
values, credentials, and production adapters are intentionally absent.

## Intended shape

- `src/adlc_workflow/` — deterministic lifecycle, task protocol, state, and
  effect coordination.
- `skills/` and `agents/` — agent-facing interfaces and runtime definitions.
- `plugins/` — versioned concern packages with manifests, prompts, schemas, and
  fixtures.
- `config/`, `policies/`, and `schemas/` — reviewed configuration and contracts.
- `adapters/` — production, local-emulator, and evaluation boundaries.
- `artifacts/` — the shared result-bundle layout.
- `scripts/` — launch and trusted validation entrypoints.
- `tests/` — contract, integration, and Fullsend acceptance-test locations.

See the proposal for the ownership, lifecycle, and acceptance requirements.

## Reviewer agents

The feature profile declares five `agent:` reviewers. The review dispatch
skill runs in the parent session and launches native background Task/Agent
calls; individual reviewers no longer use forked Skill calls. The
`adlc-review-plan` helper supplies profile-selected paths and agent assignments.
Dispatch waits for all agents to succeed and checks their output files before
aggregation.

To test native reviewer concurrency without resetting Jira or running the
whole workflow, run this inside the configured Claude container:

```sh
podman-compose exec claude python3 \
  /home/evaluator/.claude/plugins/adlc-workflow/scripts/test-reviewer-concurrency.py
```

This makes a live model call (budget cap $1) using isolated temporary fixture
documents and a fixture rubric. It retains the JSONL log and asserts that all
five reviewer executions start before the first completion, all succeed, and
all output files exist. It tests native agent dispatch; a full workflow run
is still needed to check the parent skill's orchestration and aggregation.
The production `config/rubrics/rhai-feature-review.yaml` is a pinned,
repository-local copy of the approved four-dimension strategy rubric.

## Claude/Jira integration test

The end-to-end test starts the local `checkouts/jctanner/jira-emulator`
checkout, creates `RHAIRFE-1`, copies this project into a temporary runtime
workspace, and mounts the source at `/tmp/adlc-workflow`. The container copies
that mount into `/home/evaluator/.claude/plugins/adlc-workflow` and invokes the
installed plugin from `/workspace`.

Build the shared agent image when the launcher image is not already present:

```sh
podman build -t adlc-claude-task-runner:local -f adlc-workflow/Dockerfile.claude adlc-workflow
```

Then run the opt-in test with Vertex settings exported, or with the supported
values available in `~/bin/claude.vertex`:

```sh
export ADLC_RUN_LIVE_AGENT=1
make -C adlc-workflow integration
```

The test mounts Google ADC read-only and never prints its contents. Override
`ADLC_AGENT_IMAGE` or `ADLC_CLAUDE_MODEL` when using a different image/model.

## Podman Compose

For a repeatable two-container run, use `podman-compose.yaml`:

```sh
cd adlc-workflow
cp .env.example .env
$EDITOR .env
podman-compose -f podman-compose.yaml up --build --abort-on-container-exit
```

The Claude service stays running idle so it can be inspected interactively.
In another terminal, invoke the workflow with:

```sh
podman-compose exec claude claude --dangerously-skip-permissions \
  --model "$ADLC_CLAUDE_MODEL" \
  -p "/adlc-workflow:adlc-workflow $ADLC_ISSUE_KEY"
```

The compose entrypoint registers the direct mount before the container becomes
idle, so `claude plugin list` should show
`adlc-workflow@adlc-local` as enabled. `--plugin-dir` is only needed when
running Claude outside this compose stack.

The plugin mount provides
`/home/evaluator/.claude/plugins/adlc-workflow/scripts/run-example-workflow.sh`.
It seeds an example RFE, invokes the workflow for the newly returned issue key,
and tees combined Claude output to `/workspace/adlc-workflow-run.log`:

```sh
podman-compose exec claude \
  /home/evaluator/.claude/plugins/adlc-workflow/scripts/run-example-workflow.sh
podman-compose exec claude tail -f /workspace/adlc-workflow-run.log
```

Or open a shell inside the agent container:

```sh
podman-compose exec claude bash
```

From the repository root, pass the environment file explicitly:

```sh
podman-compose --env-file adlc-workflow/.env \
  -f adlc-workflow/podman-compose.yaml up --build --abort-on-container-exit
```

The local `.env` file is gitignored; `.env.example` is the committed template.
Compose loads `.env` automatically for interpolation, including
`ANTHROPIC_VERTEX_PROJECT_ID`.

The ADLC workflow skills use the checked-in commands in `scripts/` for Jira
fetches, request creation, task advancement, result-envelope construction, and
submission. Worker skills should write only the substantive markdown; they
should not generate inline Python or heredoc commands for protocol operations.

The `claude` service receives `ADLC_JIRA_URL=http://jira-emulator:8080`,
`ADLC_JIRA_TOKEN`, `JIRA_TOKEN`, and the read-only ADC mount. The emulator is
started in strict authentication mode by default and shares the configured
token with the agent. Set `JIRA_TOKEN`, `JIRA_USER`, `ADLC_ISSUE_KEY`, or
`ADLC_CLAUDE_MODEL` to override the local defaults. Set
`ADLC_HOST_ADC_PATH` if the ADC file is not at
`$HOME/.config/gcloud/application_default_credentials.json`.

Seed the emulator with the example RFE about an RHOAI MCP server registry:

```sh
./scripts/seed-example-rfe.sh
```

The helper uses `http://127.0.0.1:8080` by default and applies both
`rfe-creator-rubric-pass` and `strat-creator-3.6`. From inside the Claude
container, its compose-provided `ADLC_JIRA_URL` automatically points at
`http://jira-emulator:8080` instead.

The compose service mounts the project directly at
`/home/evaluator/.claude/plugins/adlc-workflow`. On startup it uses Claude's
normal local-marketplace commands to register and enable the plugin. Because
the marketplace entry uses a `command` source in `link` mode, Claude loads the
direct mount in place rather than copying it to the versioned cache. The
runtime workspace is bind-mounted from `./workspace` by default; override it
with `ADLC_WORKSPACE_DIR` when needed.
The workspace bind mount contains only runtime requests, artifacts, and private
state; immutable code and configuration remain in the plugin mount. Jira data
remains in a named volume. Remove the Jira volume when you want a fresh Jira
database:

```sh
podman-compose -f adlc-workflow/podman-compose.yaml down -v
```
