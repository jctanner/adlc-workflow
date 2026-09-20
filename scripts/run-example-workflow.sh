#!/usr/bin/env bash
set -Eeuo pipefail

plugin_root="${CLAUDE_PLUGIN_ROOT:-/home/evaluator/.claude/plugins/adlc-workflow}"
log_file="${ADLC_RUN_LOG:-/workspace/adlc-workflow-run.log}"
seed_script="$plugin_root/scripts/seed-example-rfe.sh"
example_count="${ADLC_EXAMPLE_RFE_COUNT:-1}"

# This is a disposable integration-test workspace. Clear visible and hidden
# entries explicitly; `/workspace/*` alone does not match dot-directories.
echo "Clearing previous ADLC runtime state and artifacts..."
rm -rf /workspace/* /workspace/.[!.]* /workspace/..?*
rm -f "$log_file"
mkdir -p "$(dirname "$log_file")"
touch "$log_file"
exec > >(tee -a "$log_file") 2>&1

echo "=== ADLC example workflow ==="
echo "Log: $log_file"
echo "Resetting Jira emulator database..."
curl --fail-with-body --silent --show-error \
  -X POST "${ADLC_JIRA_URL%/}/api/admin/reset" \
  -H "Authorization: Bearer ${ADLC_JIRA_TOKEN}" \
  -H 'Accept: application/json'

echo "Seeding ${example_count} Jira example RFEs..."

issue_keys=()
for index in $(seq 1 "$example_count"); do
  seed_response=$(ADLC_EXAMPLE_RFE_SUMMARY="Provide MCP server registry example ${index}" "$seed_script")
  printf '%s\n' "$seed_response"
  issue_key=$(printf '%s' "$seed_response" | "$plugin_root/scripts/json-field" key)
  issue_keys+=("$issue_key")
done

echo "Running batch workflow for ${#issue_keys[@]} issues: ${issue_keys[*]}..."
prompt="/adlc-workflow:adlc-workflow ${issue_keys[*]}"

exec claude \
  --dangerously-skip-permissions \
  --model "${ADLC_CLAUDE_MODEL:-claude-haiku-4-5}" \
  --output-format stream-json \
  --verbose \
  -p "$prompt"
