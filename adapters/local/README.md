# Local adapters

Local mode now exercises two real effect paths without production credentials:

- the Jira adapter creates a `Feature` in `RHAISTRAT`, links it to the source
  `RHAIRFE` with a `Cloners` link, writes the accepted strategy into its
  description, and verifies the publication comment and labels; and
- the archive adapter materializes the complete run bundle below
  `artifacts/published/<run-id>/` and verifies its manifest.

The archive is intentionally filesystem-backed in this first slice. A git
transport can push the same manifest and receipt contract later without
changing the workflow lifecycle.
