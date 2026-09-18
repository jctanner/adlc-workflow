#!/bin/sh
set -eu

plugin_root=/home/evaluator/.claude/plugins/adlc-workflow

# Register and install the local marketplace using Claude's own plugin
# machinery. The marketplace command source uses link mode, so Claude loads
# the direct bind mount in place instead of copying it to the versioned cache.
claude plugin marketplace add "$plugin_root" --scope user
claude plugin install adlc-workflow@adlc-local --scope user --yes

echo 'ADLC Claude container ready.'
exec tail -f /dev/null
