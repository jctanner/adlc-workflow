---
name: adlc-feature-review-worker
description: Produce the aggregate review artifact for one ADLC feature task.
context: fork
user-invocable: false
allowed-tools: Read, Write, Skill
---

# ADLC feature review worker

This is a bounded worker invoked by `adlc-workflow`. The argument contains one
`RHAIRFE-*` Jira key. Review the strategy and return the complete review
markdown to the calling workflow.

1. Resolve the active profile with Bash using
   `realpath "$CLAUDE_PLUGIN_ROOT/config/rhai-feature-creator.yaml"`, then
   pass the resulting absolute path to the Read tool. The Read tool does not
   expand shell variables. Read the profile and its `workflow.stages` list.
2. Read the `review` stage from `workflow.stages`, then invoke every reviewer
   declared by that stage in list order. The configured workers must run before
   aggregation. Do not substitute the legacy `rhai-feature-*` reviewer skills.
3. Resolve the review artifact directory from the active profile. Verify that
   every reviewer wrote its expected file before synthesizing the aggregate;
   do not assume a fixed review directory; resolve it from the active profile.
   Missing reviewer output is a review failure; do not silently continue.
4. Resolve the strategy path with
   `"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key>
   --workspace /workspace`, then read the returned path.
5. Read the small `/workspace/.context/context-manifest.json` and
   `/workspace/.context/architecture-context/LATEST_VERSION`, then selectively
   read the relevant `PLATFORM.md` and component documents. Do not read the
   entire context tree or a generated file listing, and do not clone or fetch
   context yourself.
6. Resolve the checked-in rubric with Bash using
   `realpath "$CLAUDE_PLUGIN_ROOT/config/rubrics/feature-review.yaml"`, then
   pass the resulting absolute path to the Read tool. Do not pass the shell
   variable literally as a `file_path`.
7. Resolve the aggregate review path with
   `"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" review <issue-key>
   --workspace /workspace`, then produce the review at that returned path.
   Include the strategy path and evidence used.
8. Return that markdown to the parent workflow so it can build and submit the
   result envelope.

Do not invoke legacy reviewer skills, `assess-strat`, missing helper scripts,
architecture-fetch scripts, curl, inline Python, or heredocs. Do not write
under `$CLAUDE_PLUGIN_ROOT`; the install tree is read-only. Do not claim
submission; the parent workflow owns result construction and submission.
