---
name: adlc-feature-architecture-reviewer
description: Review one ADLC feature strategy against RHOAI architecture.
context: fork
user-invocable: false
allowed-tools: Read, Write
---

# ADLC architecture reviewer

Resolve the strategy path with
`"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key> --workspace /workspace`,
then read the returned path and the prepared architecture context at
`/workspace/.context/architecture-context/`. Assess
component choices, interfaces, integration patterns, security boundaries,
compatibility, and consistency with the documented platform architecture.

Before assessing claims, inspect `/workspace/.context/architecture-context/overlays/`
when it exists. Read active `*.md` overlays (excluding `README.md`) whose
`release` includes the target release or `all`, and whose `affects` matches a
strategy component or `platform`. These human-authored corrections override
generated architecture documents when they conflict. Flag stale strategy
claims corrected by an overlay and list the overlays applied in the review.

Resolve the review directory from the active profile and write the expected
`<issue-key>-architecture-review.md` there with findings, severity, evidence,
and concrete recommendations. Return the same markdown to the calling review
worker. Do not use legacy skills, scripts, network access, or inline code.
