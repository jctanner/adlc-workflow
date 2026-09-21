#!/bin/sh
set -eu

role=${1:?worker or judge role required}
shift

if [ "$role" = "worker" ]; then
  plugin_root=/home/evaluator/.claude/plugins/adlc-workflow
  claude plugin marketplace add "$plugin_root" --scope user
  claude plugin install adlc-workflow@adlc-local --scope user --yes
  exec python3 /opt/adlc-evals/scripts/run-worker.py "$@"
fi

if [ "$role" = "judge" ]; then
  exec python3 /opt/adlc-evals/scripts/run-judge.py "$@"
fi

echo "unknown ADLC eval role: $role" >&2
exit 2
