#!/usr/bin/env python3
"""Creates a maintenance release branch (e.g., release/1.1) from an existing source ref."""

import argparse
import os
import sys
from pathlib import Path

from common import (
    append_github_output,
    configure_git_credentials,
    extract_issue_field_line,
    require_maintainer,
    require_missing_remote_branch,
    require_remote_branch,
    run,
    validate_maintenance_branch,
    validate_release_target_branch,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Create a maintenance branch.")
    parser.add_argument("--maintenance-branch", help="Maintenance branch name (e.g. release/1.1)")
    parser.add_argument("--source-branch", help="Source branch name (e.g. main)")
    parser.add_argument("--source-ref", help="Source git ref (e.g. origin/main or a commit/tag)")
    return parser.parse_args()


def main():
    args = parse_args()

    issue_body = os.environ.get("ISSUE_BODY", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")

    maintenance_branch = args.maintenance_branch or extract_issue_field_line(issue_body, "Maintenance branch")
    source_branch = args.source_branch or extract_issue_field_line(issue_body, "Source branch") or "main"
    source_ref = args.source_ref or extract_issue_field_line(issue_body, "Source ref")

    require_maintainer()

    validate_maintenance_branch(maintenance_branch)
    validate_release_target_branch(source_branch)

    if not source_ref or source_ref == source_branch:
        source_ref = f"origin/{source_branch}"

    if source_ref.startswith("-"):
        raise ValueError(f"Source ref '{source_ref}' must not start with '-'")

    require_missing_remote_branch(maintenance_branch)
    require_remote_branch(source_branch)

    run(["git", "fetch", "origin", f"+refs/heads/{source_branch}:refs/remotes/origin/{source_branch}", "--tags"])

    # Verify commit exists
    commit_sha = run(["git", "rev-parse", "--verify", "--end-of-options", f"{source_ref}^{{commit}}"]).stdout.strip()

    # Verify ancestor
    is_ancestor = run(["git", "merge-base", "--is-ancestor", "--", f"{source_ref}^{{commit}}", f"origin/{source_branch}"], check=False)
    if is_ancestor.returncode != 0:
        raise RuntimeError(f"Source ref '{source_ref}' is not reachable from 'origin/{source_branch}'")

    configure_git_credentials()
    run(["git", "push", "origin", f"{source_ref}^{{commit}}:refs/heads/{maintenance_branch}"])

    if issue_number:
        run(["gh", "issue", "comment", issue_number, "--body", (
            f"Created `{maintenance_branch}` from `{source_ref}`.\n\n"
            "Next: open a Release issue, set Target branch to "
            f"`{maintenance_branch}`, and add `release:prepare`. You can close this branch-creation issue."
        )])

    append_github_output({
        "maintenance_branch": maintenance_branch,
        "source_ref": source_ref,
        "commit_sha": commit_sha,
    })


if __name__ == "__main__":
    main()
