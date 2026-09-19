---
name: rhai-feature-review-worker
description: Dispatch concurrent reviewer agents and aggregate one ADLC feature review in the parent session.
user-invocable: false
allowed-tools: Bash, Read, Write, Task, Agent, TaskOutput
---

# ADLC feature review worker

This skill runs in the **parent conversation**, without `context: fork`, so
the reviewers are sibling agents. Invoke it with the current task's issue key
and, optionally, `--workspace <absolute-runtime-path>` (default `/workspace`)
and `--profile <install-relative-profile>`.

1. Run the prewritten helper, substituting the actual issue key and workspace:

   ```bash
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-review-plan" <issue-key> --workspace <workspace>
   ```

   Pass `--profile` when supplied. Its assignments resolve the configured
   `agent:` references to Claude's plugin-qualified agent names and provide
   absolute input/output paths. Keep this JSON in context; do not rewrite it
   or create a launcher script.
2. For `execution: parallel`, launch **every** assignment using the native
   Task tool (named Agent in some versions). Use each assignment's `task`
   object as the tool arguments, including `run_in_background: true`.
   Launch all assignments before waiting on any result. Do not use Skill
   calls for individual reviewers, wrap them in general-purpose agents, or
   invoke a second scoring agent. For `execution: sequential`, dispatch each
   assignment and wait for it before dispatching the next.
3. Collect the completion of every launched agent using task notifications or
   the available task-result tool. A spawn refusal or failed reviewer is a
   review failure; report it rather than silently substituting your own review.
   After all agents succeed, run the same helper with `--check`. Nonzero exit
   means an output is missing or empty: do not aggregate yet. File existence
   alone is insufficient while a reviewer is still running.
4. Read the five assigned outputs and the strategy. Write the aggregate to
   `aggregate_path` from the plan, citing evidence and preserving the rubric's
   scores and scale. Do not copy another ticket's review or replace individual
   findings with a fresh assessment. Read additional prepared context only
   where needed to reconcile findings.
5. Return the aggregate path and a short verdict to the workflow. The workflow
   builds the envelope with `adlc-result` from this exact file and submits it.
   Review dispatch never advances, submits, or modifies private workflow state.

Code and configuration live in the read-only plugin install; all generated
documents live in the runtime workspace. Use the helper for path resolution;
do not generate Python, heredocs, shell loops, or subprocess Claude sessions.
