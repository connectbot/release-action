#!/usr/bin/env python3
"""Publishes a prepared release after PR approval and passing CI checks."""

import argparse
import json
import os
import sys
from pathlib import Path

from common import (
    append_github_output,
    configure_git_author,
    configure_git_credentials,
    extract_issue_field,
    extract_issue_field_line,
    require_maintainer,
    require_missing_remote_tag,
    run,
    validate_next_version,
    validate_release_target_branch,
    validate_release_version,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Publish a validated release.")
    parser.add_argument("--release-version", help="Version to release (e.g. 1.2.3)")
    parser.add_argument("--next-version", help="Next snapshot version (e.g. 1.2.4-SNAPSHOT)")
    parser.add_argument("--target-branch", help="Target branch (e.g. main)")
    parser.add_argument("--tag-prefix", default=os.environ.get("TAG_PREFIX", "v"), help="Tag prefix (e.g. 'v' or '')")
    return parser.parse_args()


def find_release_commit(target_branch: str, work_branch: str, release_version: str) -> str:
    res = run(["git", "rev-list", "--reverse", f"origin/{target_branch}..origin/{work_branch}"])
    commits = [c.strip() for c in res.stdout.splitlines() if c.strip()]
    expected_line = f"version={release_version}"

    for commit in commits:
        show_res = run(["git", "show", f"{commit}:gradle.properties"], check=False)
        if show_res.returncode == 0:
            for line in show_res.stdout.splitlines():
                if line.strip() == expected_line:
                    return commit

    raise RuntimeError(f"Could not find release commit with '{expected_line}' in branch {work_branch}")


def require_publish_ready(pr_number: str, pr_data: dict) -> None:
    """Explain actionable blockers before GitHub's aggregate merge status."""
    blockers = []
    if pr_data.get("reviewDecision") != "APPROVED":
        blockers.append(
            f"Open PR #{pr_number} and submit an Approve review "
            "(Files changed → Review changes → Approve). Satisfy all required reviews "
            "and resolve any requested changes. Leave the PR open and in draft; do not merge it."
        )

    result = run([
        "gh", "pr", "checks", pr_number, "--required", "--json", "bucket,name,state,workflow",
    ], check=False)
    # gh uses 1 for failing checks and 8 for pending checks; both still return JSON.
    if result.returncode not in (0, 1, 8) or not result.stdout.strip():
        raise RuntimeError(
            f"Could not read required checks for PR #{pr_number}. "
            "Check the token's Checks and Commit statuses read permissions. "
            f"{result.stderr.strip()}"
        )
    checks = json.loads(result.stdout)
    if not checks:
        blockers.append(
            "No required CI checks were reported. Configure at least one required check "
            "on the target branch and make sure CI runs on draft release PRs."
        )
    for check in checks:
        if check.get("bucket") != "pass":
            action = "Wait for" if check.get("bucket") == "pending" else "Fix or rerun"
            blockers.append(
                f"{action} required check '{check.get('name')}' "
                f"[{check.get('workflow')}]: {check.get('state')}."
            )

    if pr_data.get("mergeable") != "MERGEABLE":
        blockers.append(
            f"PR #{pr_number} is not currently mergeable ({pr_data.get('mergeable')}). "
            "Resolve conflicts, or wait for GitHub to finish calculating mergeability."
        )

    if not blockers:
        state = pr_data.get("mergeStateStatus")
        # A draft intentionally blocks GitHub's merge button. Reviews and required
        # checks are validated independently above; other blocked states still fail.
        if state != "CLEAN" and not (pr_data.get("isDraft") and state == "DRAFT"):
            blockers.append(
                f"PR #{pr_number} has merge state {state}. Open the PR's merge section "
                "and resolve the remaining branch requirements before retrying."
            )

    if blockers:
        raise RuntimeError("Release is not ready:\n\n" + "\n".join(f"- {b}" for b in blockers))


def main():
    args = parse_args()

    issue_body = os.environ.get("ISSUE_BODY", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")

    release_version = args.release_version or extract_issue_field_line(issue_body, "Release version")
    next_version = args.next_version or extract_issue_field_line(issue_body, "Next version")
    target_branch = args.target_branch or extract_issue_field_line(issue_body, "Target branch") or "main"
    release_notes = extract_issue_field(issue_body, "Release notes")

    require_maintainer()

    validate_release_version(release_version)
    validate_next_version(next_version)
    validate_release_target_branch(target_branch)

    tag_prefix = args.tag_prefix
    tag_name = f"{tag_prefix}{release_version}"
    work_branch = f"release-work/{release_version}"

    require_missing_remote_tag(tag_name)

    # 1. Locate Release PR
    pr_list_res = run([
        "gh", "pr", "list",
        "--head", work_branch,
        "--base", target_branch,
        "--state", "open",
        "--json", "number",
    ])
    prs = json.loads(pr_list_res.stdout)
    if not prs:
        raise RuntimeError(
            f"No open release PR found for {work_branch} into {target_branch}. "
            "If preparation has not run, add release:prepare to the release issue. "
            "If the PR was closed, reopen it. If it was merged manually, inspect the "
            "target branch and tags before recovering the release; do not rerun preparation blindly."
        )

    pr_number = str(prs[0]["number"])

    # 2. Verify PR state and approval
    pr_view_res = run([
        "gh", "pr", "view", pr_number,
        "--json", "baseRefName,headRefName,headRefOid,mergeable,mergeStateStatus,reviewDecision,isDraft",
    ])
    pr_data = json.loads(pr_view_res.stdout)

    if pr_data.get("baseRefName") != target_branch or pr_data.get("headRefName") != work_branch:
        raise RuntimeError("Release PR branches do not match target branch and work branch.")

    require_publish_ready(pr_number, pr_data)

    # 4. Fetch branches and verify remote commit
    configure_git_author()
    run([
        "git", "fetch", "origin",
        f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
        f"+refs/heads/{work_branch}:refs/remotes/origin/{work_branch}",
        "--tags",
    ])

    head_ref_oid = pr_data.get("headRefOid")
    current_work_oid = run(["git", "rev-parse", f"origin/{work_branch}"]).stdout.strip()
    if current_work_oid != head_ref_oid:
        raise RuntimeError("Release branch changed after PR checks were inspected.")

    # 5. Find release commit and verify tip commit
    release_commit = find_release_commit(target_branch, work_branch, release_version)

    tip_properties = run(["git", "show", f"origin/{work_branch}:gradle.properties"]).stdout
    expected_next = f"version={next_version}"
    if expected_next not in [line.strip() for line in tip_properties.splitlines()]:
        raise RuntimeError(f"Release branch tip does not contain '{expected_next}'")

    # 6. Create annotated tag
    tag_message = f"Release {tag_name}"
    if release_notes:
        tag_message += f"\n\n{release_notes}"

    run(["git", "tag", "-a", tag_name, release_commit, "-m", tag_message])

    tag_type = run(["git", "cat-file", "-t", tag_name]).stdout.strip()
    if tag_type != "tag":
        raise RuntimeError(f"{tag_name} is not an annotated tag.")

    # 7. Atomically push fast-forward of target branch and new tag
    configure_git_credentials()
    run([
        "git", "push", "--atomic", "--follow-tags", "origin",
        f"refs/remotes/origin/{work_branch}:refs/heads/{target_branch}",
    ])

    # 8. Post comment to issue
    if issue_number:
        run(["gh", "issue", "comment", issue_number, "--body", (
            f"Published tag `{tag_name}` and advanced `{target_branch}` to `{next_version}`.\n\n"
            "Next: check the repository's Actions tab for the tag-triggered artifact publishing run. "
            "This action does not upload artifacts. Once publishing succeeds, close this release issue."
        )])

    append_github_output({
        "tag_name": tag_name,
        "release_commit": release_commit,
        "target_branch": target_branch,
    })


if __name__ == "__main__":
    main()
