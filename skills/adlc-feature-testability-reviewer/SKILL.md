---
name: adlc-feature-testability-reviewer
description: Review one ADLC feature strategy for testability.
context: fork
user-invocable: false
allowed-tools: Read, Write
---

# ADLC testability reviewer

Resolve the strategy path with
`"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key> --workspace /workspace`,
then read the returned path, the checked-in
feature rubric, and the prepared architecture context at
`/workspace/.context/architecture-context/`. Assess whether requirements,
acceptance criteria, metrics, failure modes, and security boundaries are
observable and verifiable.

Check `overlays/` under that context for active, relevant human-authored
corrections (excluding `README.md`), matching the target release or `all` and
the strategy's affected components or `platform`. Use them when they change
the architecture facts or constraints underlying testability findings.

Resolve the review directory from the active profile and write the expected
`<issue-key>-testability-review.md` there with findings, severity, evidence,
and concrete recommendations. Return the same markdown to the calling review
worker. Do not use legacy skills, scripts, network access, or inline code.
