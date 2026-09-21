---
name: adlc-workflow
description: Run the deterministic ADLC workflow for one or more Jira issues.
---

# /adlc-workflow:adlc-workflow

Run the deterministic ADLC workflow for one or more explicitly supplied Jira
issues. The same run processes each selected issue in order; never use shared
flat task or result aliases when more than one issue is selected.

When the first argument is `--handoff`, do not perform the interactive
controller loop below. Invoke the installed controller once, wait for its
terminal JSON response, and report that response without reproducing worker
documents:

```bash
"$CLAUDE_PLUGIN_ROOT/scripts/adlc-workflow" handoff --workspace /workspace \
  --profile <profile-name> <issue-key>...
```

For example:

```text
/adlc-workflow:adlc-workflow --handoff --profile=rhai-feature-creator RHAIRFE-1
```

Handoff accepts a profile with or without explicit issue keys. A profile-only
handoff freezes one discovered batch before it starts. Pass
`--dangerously-skip-permissions` only when the caller has explicitly selected
that child-process policy. The normal invocation below remains the
Claude-driven controller.

`--handoff` is consumed by this skill. Do not pass it to the controller command;
preserve the supplied profile, issue keys, and any other handoff options.
Run that command in the foreground with the Bash tool timeout set to at least
900000 milliseconds. Do not background it, schedule a wakeup, or return a
progress report before the controller returns its terminal JSON.

The positional arguments are Jira issue keys, for example
`/adlc-workflow:adlc-workflow RHAIRFE-1 RHAIRFE-2`. Use the installed plugin checkout as the source of
the workflow package and the current working directory as the runtime project.
Do not write mutable execution state into the installed plugin.

Execution contract:

1. Verify that at least one issue key was supplied.
2. Require `ADLC_JIRA_URL` and `ADLC_JIRA_TOKEN` to be set. Capture every issue
   with the prewritten command below, using one distinct file per issue. The
   captured JSON is both refinement context and the immutable input to the
   profile's stage gates. Do not
   generate an inline `curl`, Python, or heredoc command. Use `ADLC_JIRA_URL`
   exactly as supplied; never substitute `localhost`, `127.0.0.1`, or another
   default. Use the issue summary and description as refinement context.

   Use one unique staging directory for the whole invocation. Do not use a
   shared `batch-input` directory, because two workflow invocations must not
   overwrite one another's captured Jira input:

   ```bash
   mkdir -p /workspace/.adlc
   request_dir=$(mktemp -d /workspace/.adlc/request.XXXXXX)
   mkdir -p "$request_dir/source"
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-jira-issue" RHAIRFE-1 \
     > "$request_dir/source/RHAIRFE-1.json"
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-jira-issue" RHAIRFE-2 \
     > "$request_dir/source/RHAIRFE-2.json"
   ```
3. Create `$request_dir/request.json` by running the prewritten command once,
   passing every issue key and one matching
   `--issue-file` argument per key. Keep the argument order identical. Do not
   construct the JSON with an inline Python or heredoc command:

   ```json
   {
     "args": ["RHAIRFE-1"],
     "kwargs": {
       "operation": "create",
       "mode": "local",
       "identity": "claude"
     }
   }
   ```

   ```bash
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-request" \
     RHAIRFE-1 RHAIRFE-2 --output "$request_dir/request.json" \
     --issue-file "$request_dir/source/RHAIRFE-1.json" \
     --issue-file "$request_dir/source/RHAIRFE-2.json"
   ```

4. Invoke the installed wrapper at
   `$CLAUDE_PLUGIN_ROOT/scripts/adlc-workflow`. Do not construct an inline
   Python invocation or reimplement the CLI in a heredoc. Global CLI options
   are already handled by the wrapper. Use:

   ```bash
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-workflow" start \
     --request "$request_dir/request.json"
   ```

   Use install root `$CLAUDE_PLUGIN_ROOT`, workspace `/workspace`, and profile
   `config/rhai-feature-creator.yaml`. Code, profiles, templates, and schemas
   must be read from the install root; never copy them into `/workspace`.
   Artifacts and private state must be written under `/workspace`.
5. Repeatedly advance the run until it is terminal. Each task envelope names
   its `issue_key` and `work_id`; use a run- and work-scoped scratch directory
   such as `/workspace/.adlc/state/runs/<run-id>/items/<work-id>/` for task and
   result envelopes. Never use `current-task.json`, `task.json`,
   `refine-result.json`, or other flat aliases. For a refine task, invoke the
   plugin-qualified Skill named by the task's `worker` value (for example,
   `skill:rhai-feature-refine-worker` becomes
   `adlc-workflow:rhai-feature-refine-worker`). Pass the task file, its
   `fragment_path`, and captured `$request_dir/request.json` path in its
   arguments. The worker
   writes only a private strategy fragment and returns its path; it must never
   write a public feature artifact. Use that fragment path directly with the
   prewritten result-envelope command below. On `adlc-submit`, the core
   deterministically assembles the public feature document from the captured
   Jira summary/description, the validated strategy fragment, and the template
   SME footer. Do not
   generate the JSON envelope with inline Python, a heredoc, or a pipe:

   Obtain every task envelope with the prewritten task helper. Do not invoke
   `adlc-workflow advance` directly and do not invent a task filename:

   ```bash
   task_file=$("$CLAUDE_PLUGIN_ROOT/scripts/adlc-task" \
     <run-id> \
     "/workspace/.adlc/state/runs/<run-id>/items/<work-id>/tasks/<task-id>.json")
   ```

   The helper supplies the required `--run` argument and creates the parent
   directories. Use the returned path as `--task-file`. For a refine task,
   read `fragment_path` from that task file with `jq -r .fragment_path` and
   invoke the worker with these exact arguments:

   ```text
   RHAIRFE-1 --task <task-file> --fragment-path <fragment-path> --captured-source <request-dir>/request.json
   ```

   Do not substitute the public artifact path for `fragment_path`.

   ```bash
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-result" \
     --task-file <task-json-file> \
     --markdown-file <strategy-fragment-file> \
     --field strategy_markdown \
     --output <task-result-path>
   ```

   Submit with
   `"$CLAUDE_PLUGIN_ROOT/scripts/adlc-submit" <run-id> <task-id> <task-result-path>`.
   The core persists the accepted strategy markdown to the profile-selected
   public artifact when the result is submitted. Do not copy or persist a
   second artifact manually. Submit the result through the CLI and confirm
   that submission was accepted.
6. For a decomposition task, invoke the plugin-qualified Skill named by the
   task's `worker` value in the same way. That worker writes the decomposition
   summary and any profile-scoped child artifacts, returning only its absolute
   `artifact_path`. Build the result with `adlc-result --field
   decomposition_markdown`, then submit it with `adlc-submit`. Do not return or
   reproduce the decomposition markdown in the parent context.
7. For each review task, invoke the plugin-qualified Skill named by the task's
   `worker` value (for example, `skill:rhai-feature-review-worker` becomes
   `adlc-workflow:rhai-feature-review-worker`) with the task's `issue_key`.
   This dispatch skill runs in this parent session and launches the profile's
   native reviewer agents. Do not wrap it in a forked subagent. After it
   returns the aggregate path, use that exact file with the same `adlc-result`
   command and `--field review_markdown` to construct the result. Submit it
   with `adlc-submit` and confirm acceptance.
   The core persists the accepted review markdown to the profile-selected
   public artifact when the result is submitted. Do not copy or persist a
   second artifact manually.
8. After each submitted result, advance again and inspect the envelope. If its
   `kind` is `complete` or
   `blocked`, stop immediately: it is a terminal response, not a task, and
   must never be passed to `adlc-submit`. Only invoke `adlc-submit` for a JSON
   envelope whose `kind` is `task` and which contains a real `task_id`.
   Report the terminal response. When publication succeeds, include the
   publication receipts in the report. A publication failure must remain
   `blocked` and include its reason.

Use normal Claude tools for these operations. Never claim a task was submitted
unless the CLI accepted the result.

## ADLC hard constraints

When this skill is invoked as `/adlc-workflow:adlc-workflow RHAIRFE-*`, these
constraints override any legacy worker instructions:

- Use only the checked-in commands under `$CLAUDE_PLUGIN_ROOT/scripts/` for
  Jira access and workflow protocol operations. Never invoke those Python
  scripts through `bash`.
- Never write under `$CLAUDE_PLUGIN_ROOT`; it is a read-only install mount.
- Never create ad-hoc Python files, heredocs, inline Python, inline curl, or
  hand-built workflow JSON. Use the helper commands and the Write tool for
  substantive markdown only.
- Keep all runtime files under `/workspace`, including generated strategy and
  review artifacts.
- Treat `/workspace/.context/context-manifest.json` as a small metadata file.
  Do not attempt to read the entire prepared context tree or reconstruct a
  file listing from the manifest; read only the relevant documents needed for
  the current task.
