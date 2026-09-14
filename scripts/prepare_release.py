#!/usr/bin/env python3
"""Prepares a release branch, runs gradle release with no-push, and creates or updates a release PR."""

import argparse
import json
import os
import sys
from pathlib import Path

from common import (
    append_github_output,
    configure_git_author,
    configure_git_credentials,
    extract_issue_field_line,
    require_maintainer,
    require_missing_remote_branch,
    require_missing_remote_tag,
    require_remote_branch,
    run,
    validate_next_version,
    validate_release_target_branch,
    validate_release_version,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Prepare a release branch and pull request.")
    parser.add_argument("--release-version", help="Version to release (e.g. 1.2.3)")
    parser.add_argument("--next-version", help="Next snapshot version (e.g. 1.2.4-SNAPSHOT)")
    parser.add_argument("--target-branch", help="Target branch to release from (e.g. main)")
    parser.add_argument("--tag-prefix", default=os.environ.get("TAG_PREFIX", "v"), help="Tag prefix (e.g. 'v' or '')")
    parser.add_argument(
        "--gradle-no-push-prop",
        default=os.environ.get("GRADLE_NO_PUSH_PROP", "release.noPush"),
        help="Gradle property name that suppresses git push during release",
    )
    return parser.parse_args()


def upsert_pr(work_branch: str, target_branch: str, release_version: str, next_version: str, tag_name: str, issue_number: str) -> str:
    body = (
        f"Prepares {tag_name} from {target_branch}.\n\n"
        f"Release issue: #{issue_number}\n"
        f"Release version: {release_version}\n"
        f"Next version: {next_version}\n"
        f"Target branch: {target_branch}\n\n"
        "Next steps:\n\n"
        "1. Review this PR and submit an **Approve** review (Files changed → Review changes → Approve).\n"
        "2. Wait for all required CI checks to pass.\n"
        f"3. Add `release:publish` to issue #{issue_number}.\n\n"
        "**Leave this PR open and in draft. Do not merge it or mark it ready for review.** "
        "The draft disables GitHub's merge button; you can still submit a review. "
        "Publishing tags the release commit and advances the target branch automatically.\n"
    )

    pr_list = run(["gh", "pr", "list", "--head", work_branch, "--base", target_branch, "--state", "open", "--json", "number,isDraft"])
    prs = json.loads(pr_list.stdout)
    title = f"chore(release): {release_version}"

    if prs and len(prs) > 0:
        pr_number = str(prs[0]["number"])
        if not prs[0].get("isDraft"):
            run(["gh", "pr", "ready", pr_number, "--undo"])
        run(["gh", "pr", "edit", pr_number, "--title", title, "--body", body])
        print(f"Updated existing PR #{pr_number}")
    else:
        res = run(["gh", "pr", "create", "--draft", "--base", target_branch, "--head", work_branch, "--title", title, "--body", body])
        # gh pr create prints PR URL, find PR number from it
        pr_url = res.stdout.strip()
        pr_number = pr_url.split("/")[-1] if "/" in pr_url else ""
        print(f"Created new PR: {pr_url}")

    return pr_number


def main():
    args = parse_args()

    issue_body = os.environ.get("ISSUE_BODY", "")
    issue_number = os.environ.get("ISSUE_NUMBER", "")

    release_version = args.release_version or extract_issue_field_line(issue_body, "Release version")
    next_version = args.next_version or extract_issue_field_line(issue_body, "Next version")
    target_branch = args.target_branch or extract_issue_field_line(issue_body, "Target branch") or "main"

    require_maintainer()

    validate_release_version(release_version)
    validate_next_version(next_version)
    validate_release_target_branch(target_branch)

    tag_prefix = args.tag_prefix
    tag_name = f"{tag_prefix}{release_version}"
    work_branch = f"release-work/{release_version}"

    require_missing_remote_tag(tag_name)
    require_remote_branch(target_branch)
    require_missing_remote_branch(work_branch)

    configure_git_author()
    run(["git", "fetch", "origin", f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}", "--tags"])
    run(["git", "checkout", "-B", work_branch, f"origin/{target_branch}"])

    gradle_cmd = [
        "./gradlew",
        "release",
        f"-P{args.gradle_no_push_prop}=true",
        "-Prelease.useAutomaticVersion=true",
        f"-Prelease.releaseVersion={release_version}",
        f"-Prelease.newVersion={next_version}",
    ]
    print(f"Running: {' '.join(gradle_cmd)}")
    run(gradle_cmd)

    configure_git_credentials()
    run(["git", "push", "origin", f"HEAD:refs/heads/{work_branch}"])

    pr_number = upsert_pr(work_branch, target_branch, release_version, next_version, tag_name, issue_number)

    if issue_number:
        run(["gh", "issue", "comment", issue_number, "--body", (
            f"Prepared {tag_name} for `{target_branch}` in PR #{pr_number}.\n\n"
            "Next steps:\n\n"
            f"1. Open #{pr_number} and submit an **Approve** review "
            "(Files changed → Review changes → Approve).\n"
            "2. Wait for all required CI checks to pass.\n"
            "3. Add `release:publish` to **this issue**.\n\n"
            "**Leave the PR open and in draft. Do not merge it or mark it ready for review.** "
            "The publish action will tag the release and advance the target branch."
        )])

    append_github_output({
        "release_version": release_version,
        "next_version": next_version,
        "target_branch": target_branch,
        "work_branch": work_branch,
        "tag_name": tag_name,
        "pr_number": pr_number,
    })


if __name__ == "__main__":
    main()
