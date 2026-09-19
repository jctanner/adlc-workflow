---
name: adlc-workflow-update-deps
description: Force update all vendored dependencies — rhai-request-assess skills and architecture context. Use when you want the latest versions.
user-invocable: true
disable-model-invocation: true
allowed-tools: Bash, Glob
---

Force update all vendored dependencies by removing cached copies and re-fetching.

## Steps

### 1. Update rhai-request-assess

The removal list mirrors everything `scripts/bootstrap-rhai-request-assess.sh` installs: the checkout, every skill directory it vendors, and the agent definitions it copies into `.claude/agents/`.

```bash
rm -rf .context/rhai-request-assess \
  .claude/skills/rhai-request-assess .claude/skills/rhai-initiative-assess .claude/skills/rhai-request-export-rubric \
  .claude/agents/rfe-scorer.md .claude/agents/initiative-scorer.md
bash scripts/bootstrap-rhai-request-assess.sh
```

### 2. Update architecture context

```bash
rm -rf .context/architecture-context
bash scripts/fetch-architecture-context.sh
```

### 3. Report

List what was updated and the versions fetched.

$ARGUMENTS
