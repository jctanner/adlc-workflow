# adlc-workflow agent

The agent is the runtime-facing wrapper around the deterministic workflow core.

Use the checked-in commands under `$CLAUDE_PLUGIN_ROOT/scripts/` for protocol
operations:

- `adlc-jira-issue` fetches Jira input.
- `adlc-request` creates the canonical request file.
- `adlc-workflow` starts the core.
- `adlc-task` advances a run and saves the task envelope.
- `adlc-result` builds a validated result envelope around a worker fragment.
- `adlc-submit` submits the result.
- `adlc-persist` copies non-assembled worker output into the public artifact
  tree; never use it for assembled feature refinement.

Do not generate inline Python, curl, heredoc, or JSON-envelope commands. The
commands bind the immutable install root to the plugin and the mutable
workspace to `$ADLC_WORKSPACE` or the current directory. The request file must use the
versioned shape:

```json
{
  "args": ["RHAIRFE-1", "RHAIRFE-2"],
  "kwargs": {
    "operation": "create",
    "mode": "local",
    "identity": "claude"
  }
}
```

Run `start`, then repeatedly run `advance`. For an assembled feature-refine
task, the task's `fragment_path` is the only model-writable output: pass that
path and the captured request path to the refine worker, then pass the returned
fragment to `adlc-result`. `adlc-submit` deterministically renders the public
feature document, including the immutable source-derived Business Need and
template-owned SME footer. For each task, write the result at
the supplied `result_path` with `task_id`, `run_id`, `revision`, and an
`outputs` object containing the stage-specific output. Submit it with
`submit`, and continue until the core returns `complete` or `blocked`.
