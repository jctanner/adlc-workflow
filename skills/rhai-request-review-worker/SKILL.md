---
name: rhai-request-review-worker
description: Dispatch parallel RHAIRFE reviewers and aggregate their request review.
user-invocable: false
allowed-tools: Bash, Read, Write, Task, Agent, TaskOutput
---

# RHAIRFE request review worker

Run the prewritten review planner for the current issue and request profile:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-review-plan" <issue-key> \
  --workspace /workspace \
  --install-root "$CLAUDE_PLUGIN_ROOT" \
  --profile config/rhai-request-creator.yaml
```

Launch every returned assignment with the native Task/Agent tool before waiting
on any result when `execution` is `parallel`. Use each assignment's task object
unchanged, including `run_in_background: true`. Do not wrap reviewers in a
general-purpose subagent or invoke them through Skill calls.

After all reviewer tasks complete, run the planner again with `--check
--wait-seconds 30`. If any output is missing or empty, fail the review rather
than aggregating partial evidence. Then invoke the checked-in aggregator, which
uses the profile's `review.aggregate.inputs`, `schema`, and `renderer`:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-aggregate-review" <issue-key> \
  --workspace /workspace \
  --install-root "$CLAUDE_PLUGIN_ROOT" \
  --profile config/rhai-request-creator.yaml
```

Do not hardcode the assessment/feasibility list or replace configured findings.

Then run the deterministic scorer:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-score-review" <issue-key> \
  --workspace /workspace \
  --install-root "$CLAUDE_PLUGIN_ROOT" \
  --profile config/rhai-request-creator.yaml
```

Return the aggregate path and a short verdict only. Do not submit a workflow
result or modify private state. All generated documents belong under
`/workspace`; plugin resources are read-only.
