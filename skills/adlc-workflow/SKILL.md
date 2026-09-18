---
name: adlc-workflow
description: Run the deterministic ADLC workflow for one or more Jira issues.
---

# /adlc-workflow:adlc-workflow

Run the deterministic ADLC workflow for one or more explicitly supplied Jira
issues. The same run processes each selected issue in order; never use shared
flat task or result aliases when more than one issue is selected.

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

   ```bash
   mkdir -p /workspace/.adlc/batch-input/source
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-jira-issue" RHAIRFE-1 \
     > /workspace/.adlc/batch-input/source/RHAIRFE-1.json
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-jira-issue" RHAIRFE-2 \
     > /workspace/.adlc/batch-input/source/RHAIRFE-2.json
   ```
3. Create `/workspace/.adlc/batch-input/request.json` by running the
   prewritten command once, passing every issue key and one matching
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
     RHAIRFE-1 RHAIRFE-2 --output /workspace/.adlc/batch-input/request.json \
     --issue-file /workspace/.adlc/batch-input/source/RHAIRFE-1.json \
     --issue-file /workspace/.adlc/batch-input/source/RHAIRFE-2.json
   ```

4. Invoke the installed wrapper at
   `$CLAUDE_PLUGIN_ROOT/scripts/adlc-workflow`. Do not construct an inline
   Python invocation or reimplement the CLI in a heredoc. Global CLI options
   are already handled by the wrapper. Use:

   ```bash
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-workflow" start \
     --request /workspace/.adlc/batch-input/request.json
   ```

   Use install root `$CLAUDE_PLUGIN_ROOT`, workspace `/workspace`, and profile
   `config/rhai-feature-creator.yaml`. Code, profiles, templates, and schemas
   must be read from the install root; never copy them into `/workspace`.
   Artifacts and private state must be written under `/workspace`.
5. Repeatedly advance the run until it is terminal. Each task envelope names
   its `issue_key` and `work_id`; use a run- and work-scoped scratch directory
   such as `/workspace/.adlc/runs/<run-id>/<work-id>/` for task and result
   envelopes. Never use `current-task.json`, `task.json`, `refine-result.json`,
   or other flat aliases. For a refine task, write the strategy markdown with the Write tool,
   then use the prewritten result-envelope command below. Do not generate the
   JSON envelope with inline Python, a heredoc, or a pipe:

   ```json
   {
     "task_id": "<task_id>",
     "run_id": "<run_id>",
     "revision": 0,
     "outputs": {"strategy_markdown": "<complete strategy markdown>"}
   }
   ```

   ```bash
   "$CLAUDE_PLUGIN_ROOT/scripts/adlc-result" \
     --task-file <task-json-file> \
     --markdown-file <strategy-markdown-file> \
     --field strategy_markdown \
     --output <task-result-path>
   ```

   Submit with
   `"$CLAUDE_PLUGIN_ROOT/scripts/adlc-submit" <run-id> <task-id> <task-result-path>`.
   Before advancing to the next stage, persist the accepted strategy markdown as the
   public artifact by running:

   First resolve the destination with
   `"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" task <issue-key>
   --workspace /workspace`, then pass that returned path to `adlc-persist`.

   Do not stage a second copy with an ad-hoc `cp` command. Submit the result
   through the CLI and confirm that submission was accepted.
6. For each review task, write the review markdown with
   the Write tool, and use the same `adlc-result` command with
   `--field review_markdown` to construct the result. Submit it with
   `adlc-submit` and confirm acceptance.
   Resolve the review destination with
   `"$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path" review <issue-key>
   --workspace /workspace`, then persist the accepted review markdown there.
7. After each submitted result, advance again and inspect the envelope. If its
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
