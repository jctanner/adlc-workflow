"""Small, idempotent Jira REST adapter used by local publication."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class JiraAdapterError(RuntimeError):
    """Raised when a Jira effect cannot be applied or verified."""


class JiraPublisher:
    def __init__(self, base_url: str | None = None, token: str | None = None, mapping: dict[str, Any] | None = None):
        self.base_url = (base_url or os.environ.get("ADLC_JIRA_URL", "")).rstrip("/")
        self.token = token or os.environ.get("ADLC_JIRA_TOKEN", "")
        target = (mapping or {}).get("strategy_target", {})
        labels = (mapping or {}).get("labels", {})
        self.target_project = target.get("project", "RHAISTRAT")
        self.issue_type = target.get("issue_type", "Feature")
        self.link_type = target.get("link_type", "Cloners")
        self.auto_created_label = labels.get("auto_created", "strat-creator-auto-created")
        self.published_label = labels.get("published", "adlc-published")
        outcome_labels = labels.get("outcome", {})
        self.pass_label = outcome_labels.get("pass", "strat-creator-rubric-pass")
        self.needs_attention_label = outcome_labels.get(
            "needs_attention", "strat-creator-needs-attention"
        )
        if not self.base_url or not self.token:
            raise JiraAdapterError("ADLC_JIRA_URL and ADLC_JIRA_TOKEN are required")

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise JiraAdapterError(f"Jira {method} {path} failed: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JiraAdapterError(f"Jira returned invalid JSON for {method} {path}") from exc

    def publish(
        self,
        issue_key: str,
        run_id: str,
        work_id: str,
        strategy_path: str,
        review_path: str,
        verdict: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        issue_path = f"/rest/api/2/issue/{quote(issue_key, safe='')}"
        marker = f"<!-- adlc-publication:{run_id}:{work_id} -->"
        issue = self._request("GET", issue_path)
        fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
        summary = fields.get("summary", issue_key)
        strategy = Path(strategy_path)
        review = Path(review_path)
        # Paths passed by the core are workspace-relative. Resolve them from
        # the caller's current runtime directory only for this first adapter
        # slice; the caller supplies the actual text below.
        if not strategy.is_file() or not review.is_file():
            raise JiraAdapterError("strategy and review artifacts are required for Jira publication")
        strategy_text = strategy.read_text(encoding="utf-8")
        review_text = review.read_text(encoding="utf-8")

        strat_key = self._linked_strat_key(fields)
        created = False
        if not strat_key:
            labels = list(fields.get("labels", []))
            labels.extend([self.auto_created_label])
            labels = list(dict.fromkeys(labels))
            created_issue = self._request(
                "POST",
                "/rest/api/2/issue",
                {
                    "fields": {
                        "project": {"key": self.target_project},
                        "issuetype": {"name": self.issue_type},
                        "summary": summary,
                        "description": strategy_text,
                        "labels": labels,
                    }
                },
            )
            strat_key = created_issue.get("key") if isinstance(created_issue, dict) else None
            if not strat_key:
                raise JiraAdapterError(f"Jira did not return a RHAISTRAT key for {issue_key}")
            self._request(
                "POST",
                "/rest/api/2/issueLink",
                {
                    "type": {"name": self.link_type},
                    "inwardIssue": {"key": strat_key},
                    "outwardIssue": {"key": issue_key},
                },
            )
            linked_source = self._request("GET", issue_path)
            linked_fields = linked_source.get("fields", {}) if isinstance(linked_source, dict) else {}
            if self._linked_strat_key(linked_fields) != strat_key:
                raise JiraAdapterError(f"Jira Cloners link could not be verified for {issue_key}")
            created = True

        strat_path = f"/rest/api/2/issue/{quote(strat_key, safe='')}"
        strat = self._request("GET", strat_path)
        strat_fields = strat.get("fields", {}) if isinstance(strat, dict) else {}
        existing_description = str(strat_fields.get("description", ""))
        if existing_description != strategy_text:
            self._request("PUT", strat_path, {"fields": {"description": strategy_text}})

        outcome_label = (verdict or {}).get("label")
        if not isinstance(outcome_label, str) or not outcome_label:
            outcome_label = self.pass_label if "PASS" in review_text.upper() else self.needs_attention_label
        self._request(
            "PUT",
            strat_path,
            {"update": {"labels": [{"add": outcome_label}, {"add": self.published_label}]}},
        )
        comments = fields.get("comment", {}).get("comments", [])
        if any(marker in str(comment.get("body", "")) for comment in comments if isinstance(comment, dict)):
            return {"adapter": "jira", "source_key": issue_key, "issue_key": strat_key, "marker": marker, "status": "already_present", "created": created}

        comment = (
            f"{marker}\n"
            "ADLC workflow publication completed.\n"
            f"Run: {run_id}\n"
            f"Strategy artifact: {strategy_path}\n"
            f"Review artifact: {review_path}"
        )
        self._request(
            "PUT",
            issue_path,
            {
                "update": {
                    "comment": [{"add": {"body": comment}}],
                    "labels": [{"add": "adlc-published"}],
                }
            },
        )
        self._request("PUT", strat_path, {"update": {"comment": [{"add": {"body": comment}}]}})
        observed = self._request("GET", strat_path)
        observed_fields = observed.get("fields", {}) if isinstance(observed, dict) else {}
        observed_comments = observed_fields.get("comment", {}).get("comments", [])
        observed_labels = observed_fields.get("labels", [])
        verified_comment = any(marker in str(item.get("body", "")) for item in observed_comments if isinstance(item, dict))
        if not verified_comment or self.published_label not in observed_labels or not strat_key.startswith("RHAISTRAT-"):
            raise JiraAdapterError(f"Jira STRAT publication could not be verified for {issue_key}")
        return {"adapter": "jira", "source_key": issue_key, "issue_key": strat_key, "marker": marker, "status": "published", "created": created}

    @staticmethod
    def _linked_strat_key(fields: dict[str, Any]) -> str | None:
        for link in fields.get("issuelinks", []) or []:
            if not isinstance(link, dict):
                continue
            for side in ("inwardIssue", "outwardIssue"):
                key = link.get(side, {}).get("key")
                if isinstance(key, str) and key.startswith("RHAISTRAT-"):
                    return key
        return None
