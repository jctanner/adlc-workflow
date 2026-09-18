---
name: adlc-feature-scope-reviewer
description: Review one ADLC feature strategy for scope and sizing.
context: fork
user-invocable: false
allowed-tools: Read, Write
---

# ADLC scope reviewer

Resolve the strategy path with
`"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key> --workspace /workspace`,
then read the returned path and the prepared
architecture context at `/workspace/.context/architecture-context/`. Assess
whether the strategy describes one coherent feature, has clear boundaries,
priorities, out-of-scope items, dependencies, and a credible effort estimate.

Check `overlays/` under the prepared context for active, relevant
human-authored corrections (excluding `README.md`), matching the target
release or `all` and the strategy's affected components or `platform`. Apply
them when they alter scope, dependencies, or platform constraints.

Resolve the review directory from the active profile and write the expected
`<issue-key>-scope-review.md` there with findings, severity, evidence, and
concrete recommendations. Return the same markdown to the calling review
worker. Do not use legacy skills, scripts, network access, or inline code.
