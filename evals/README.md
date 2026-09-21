# ADLC worker A/B evaluations

This directory compares bounded ADLC workers using frozen inputs and isolated
containers. It is independent from the production `adlc-workflow` CLI.

Start by copying `.env.example` to `.env`, preparing a workspace that contains
only the desired `.context/` snapshot, and creating a local experiment config
from `experiments/rhai-feature-refinement.example.yaml`.

```bash
python3 evals/scripts/eval.py validate --config evals/experiments/local.yaml
python3 evals/scripts/eval.py prepare --config evals/experiments/local.yaml --experiment-id refinement-001
python3 evals/scripts/eval.py collect --experiment evals/results/refinement-001 --dry-run
python3 evals/scripts/eval.py collect --experiment evals/results/refinement-001
python3 evals/scripts/eval.py check --experiment evals/results/refinement-001
python3 evals/scripts/eval.py judge --experiment evals/results/refinement-001 --evaluation-id judge-001
python3 evals/scripts/eval.py report --experiment evals/results/refinement-001 --evaluation-id judge-001
```

`prepare` snapshots the runtime plugin, context, cases, suite, and judge prompt.
`collect` runs exactly one controller-style bounded worker per trial in a new
container. `judge` consumes saved artifacts only; it never reruns candidates.

The current vertical slice supports `skill:rhai-feature-refine-worker` only.
It does not evaluate the whole workflow, native agent-led dispatch, Jira, or
publication. Results contain internal source material and must remain local.

Render saved results without making model calls:

```bash
python3 evals/scripts/render-html.py \
  --experiment evals/results/<experiment-id>
```

This writes `<experiment-id>/report.html`, including partial runs that stopped
at deterministic gates. It embeds the generated strategy artifacts and may
therefore contain internal source material; keep it local.

For a one-repetition smoke comparison of `claude-haiku-4-5` and
`claude-sonnet-5`, run [RUN_EVAL_HAIKU_VS_SONNET.sh](../../RUN_EVAL_HAIKU_VS_SONNET.sh)
from the repository root. It snapshots the existing `workspace/.context`, so
that directory and Vertex credentials (`ANTHROPIC_VERTEX_PROJECT_ID` plus a
readable `ADLC_HOST_ADC_PATH`) must exist first. The script uses
`claude-opus-4-6` as its fixed blinded judge in a distinct evaluator container.
