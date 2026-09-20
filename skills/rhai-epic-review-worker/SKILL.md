---
name: rhai-epic-review-worker
description: Review and aggregate one profile-driven epic decomposition.
user-invocable: false
allowed-tools: Bash, Read, Write, Task, Agent, TaskOutput
---

# RHAISTRAT epic review worker

Run the prewritten review planner for the current issue and epic profile:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-review-plan" <issue-key> \
  --workspace /workspace \
  --install-root "$CLAUDE_PLUGIN_ROOT" \
  --profile config/rhai-epic-creator.yaml
```

Emit this warning before dispatch: `agent-led mode does not enforce reviewer
parallelism; running reviewers serially`. Dispatch each returned assignment
with the native Task/Agent tool, wait for it to succeed, then dispatch the
next. Use each assignment's task object unchanged except set
`run_in_background: false` when present. After all assignments complete, run
the planner with `--check --wait-seconds 30`; do not aggregate partial output.

Then invoke the checked-in aggregator, which reads the profile's
`review.aggregate.inputs`, validates its `schema`, and applies its `renderer`:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-aggregate-review" <issue-key> \
  --workspace /workspace \
  --install-root "$CLAUDE_PLUGIN_ROOT" \
  --profile config/rhai-epic-creator.yaml
```

Return only the aggregate path and a short verdict; do not submit a result or
modify private state.

Before returning, run the deterministic scorer:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-score-review" <issue-key> \
  --workspace /workspace \
  --install-root "$CLAUDE_PLUGIN_ROOT" \
  --profile config/rhai-epic-creator.yaml
```
