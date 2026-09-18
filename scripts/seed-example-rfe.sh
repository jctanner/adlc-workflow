#!/usr/bin/env bash
set -Eeuo pipefail

jira_url="${ADLC_JIRA_URL:-http://127.0.0.1:8080}"
jira_token="${ADLC_JIRA_TOKEN:-${JIRA_TOKEN:-jira-emulator-default-token}}"
summary="${ADLC_EXAMPLE_RFE_SUMMARY:-Provide an MCP server registry for RHOAI}"

jira_url="${jira_url%/}"

payload=$(cat <<'JSON'
{
  "fields": {
    "project": {"key": "RHAIRFE"},
    "issuetype": {"name": "Feature Request"},
    "summary": "__SUMMARY__",
    "labels": ["rfe-creator-rubric-pass", "strat-creator-3.6"],
    "customfield_10855": "3.6 GA RHOAI RELEASE",
    "description": "## Summary\nRHOAI needs a discoverable registry of available Model Context Protocol (MCP) servers so administrators and users can understand which integrations are supported, how they are configured, and which capabilities they expose.\n\n## Problem Statement\nToday, information about MCP servers is distributed across repositories, deployment documentation, and individual product integrations. Users cannot reliably discover available servers from one RHOAI surface, compare their capabilities, or determine which deployment and security prerequisites apply. This makes adoption slower and increases the risk of using unsupported or incorrectly configured integrations.\n\n## Affected Customers\nRHOAI administrators, platform engineers, data scientists, application developers, and solution teams who evaluate or operate MCP-based integrations.\n\n## Business Justification\nA maintained registry would reduce discovery and configuration effort, improve consistency across RHOAI deployments, and provide a foundation for supportability and lifecycle communication as the MCP ecosystem grows.\n\n## User Scenarios\n1. As an RHOAI administrator, I need to browse available MCP servers and their deployment prerequisites so that I can select integrations appropriate for my cluster.\n2. As a developer, I need to see the capabilities, ownership, compatibility, and documentation for an MCP server so that I can use it correctly.\n3. As a support or product team member, I need a maintained source of registry metadata so that availability and lifecycle status are communicated consistently.\n\n## Acceptance Criteria\n- [ ] RHOAI provides a documented registry format for MCP server entries.\n- [ ] Each entry identifies the server, owner, capabilities, supported RHOAI compatibility, deployment requirements, security considerations, and documentation links.\n- [ ] Users can discover the registry through an RHOAI-supported interface or repository.\n- [ ] The registry defines ownership and review expectations for adding, changing, deprecating, and removing entries.\n- [ ] Registry metadata can distinguish supported servers from experimental or community-provided servers.\n- [ ] The design does not require executable MCP server code to be stored in the registry itself.\n\n## Success Criteria\nUsers can identify an appropriate MCP server and its prerequisites from the registry without searching across unrelated repositories. Registry entries have clear ownership and remain current through an agreed review process.\n\n## Scope\n### In Scope\nThe registry metadata model, discovery surface, ownership and review process, compatibility/status fields, and documentation for contributing entries.\n\n### Out of Scope\nImplementing or hosting every MCP server, replacing MCP server authentication, and defining a universal runtime orchestration layer for MCP servers.\n\n## Open Questions\nWhat is the initial authoritative storage and discovery surface? Which RHOAI release and deployment dimensions must compatibility metadata cover? Should the first version include only Red Hat-maintained servers or also reviewed community servers?"
  }
}
JSON
)
command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required to encode the Jira payload" >&2
  exit 1
}
payload=$(PAYLOAD="$payload" SUMMARY="$summary" python3 -c '
import json
import os

value = json.loads(os.environ["PAYLOAD"])
value["fields"]["summary"] = os.environ["SUMMARY"]
print(json.dumps(value))
')

response=$(curl --fail-with-body --silent --show-error \
  -X POST "${jira_url}/rest/api/2/issue" \
  -H "Authorization: Bearer ${jira_token}" \
  -H 'Content-Type: application/json' \
  --data-binary "$payload")

printf '%s\n' "$response"
if command -v python3 >/dev/null 2>&1; then
  script_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
  issue_key=$(printf '%s' "$response" | "$script_root/json-field" key)
  printf 'Seeded example RFE: %s\n' "$issue_key" >&2
fi
