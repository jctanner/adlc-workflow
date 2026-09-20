#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
install_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
workspace_root=${ADLC_WORKSPACE:-${PWD}}
profile=${ADLC_PROFILE:-config/rhai-feature-creator.yaml}
request_file=
keys=

usage() {
    echo "usage: $0 [--request REQUEST.json] [--profile PROFILE] ISSUE_KEY..." >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --request)
            [ "$#" -ge 2 ] || usage
            request_file=$2
            shift 2
            ;;
        --profile)
            [ "$#" -ge 2 ] || usage
            profile=$2
            shift 2
            ;;
        --)
            shift
            keys="$*"
            break
            ;;
        -* )
            usage
            ;;
        *)
            keys="$keys${keys:+ }$1"
            shift
            ;;
    esac
done

if [ -z "$request_file" ]; then
    [ -n "$keys" ] || usage
    [ -n "${ADLC_JIRA_URL:-}" ] || { echo "ADLC_JIRA_URL is required" >&2; exit 2; }
    [ -n "${ADLC_JIRA_TOKEN:-}" ] || { echo "ADLC_JIRA_TOKEN is required" >&2; exit 2; }
    mkdir -p "$workspace_root/.adlc"
    request_dir=$(mktemp -d "$workspace_root/.adlc/local-request.XXXXXX")
    mkdir -p "$request_dir/source"
    issue_files=
    for issue_key in $keys; do
        issue_file="$request_dir/source/$issue_key.json"
        "$install_root/scripts/adlc-jira-issue" "$issue_key" > "$issue_file"
        issue_files="$issue_files --issue-file $issue_file"
    done
    # The checked-in request helper owns the canonical request document.
    # shellcheck disable=SC2086
    "$install_root/scripts/adlc-request" $keys --output "$request_dir/request.json" \
        $issue_files --profile "$profile"
    request_file=$request_dir/request.json
fi

ADLC_WORKSPACE="$workspace_root" ADLC_PROFILE="$profile" \
    "$install_root/scripts/adlc-workflow" start --request "$request_file"
