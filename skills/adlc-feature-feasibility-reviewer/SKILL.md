---
name: adlc-feature-feasibility-reviewer
description: Review one ADLC feature strategy for technical feasibility.
context: fork
user-invocable: false
allowed-tools: Read, Write
---

# ADLC feasibility reviewer

Resolve the strategy path with
`"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key> --workspace /workspace`,
then read the returned path and the prepared
architecture context at `/workspace/.context/architecture-context/`. Assess
technical feasibility, dependencies, ownership, risks, and delivery
assumptions.

Check the `overlays/` directory under that context. Apply active overlays
(excluding `README.md`) matching the target release or `all` and the strategy's
affected components or `platform`; treat them as higher-priority corrections
to generated architecture facts.

Resolve the review directory from the active profile and write the expected
`<issue-key>-feasibility-review.md` there with findings, severity, evidence,
and concrete recommendations. Return the same markdown to the calling review
worker. Do not use legacy skills, scripts, network access, or inline code.
