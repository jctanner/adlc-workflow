---
name: adlc-feature-scoring-reviewer
description: Score one ADLC feature strategy against the checked-in rubric.
context: fork
user-invocable: false
allowed-tools: Read, Write
---

# ADLC scoring reviewer

Resolve the strategy path with
`"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key> --workspace /workspace`,
then review the returned path using the rubric
resolved by running `realpath
"$CLAUDE_PLUGIN_ROOT/config/rubrics/feature-review.yaml"` in Bash. Pass the
resulting absolute path to Read; the Read tool does not expand shell variables.
Also use the prepared architecture context at
`/workspace/.context/architecture-context/`.

Check `overlays/` under that context for active, relevant human-authored
corrections (excluding `README.md`), matching the target release or `all` and
the strategy's affected components or `platform`. Use matching overlays when
grounding evidence and note any material correction they introduce.

Resolve the review directory from the active profile and write the expected
`<issue-key>-scorer-review.md` there.
Include one score and concise evidence for each rubric dimension, a total, and
a verdict. Return the same markdown to the calling review worker. Do not use
legacy skills, scripts, network access, or inline code.
