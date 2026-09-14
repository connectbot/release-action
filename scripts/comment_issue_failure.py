#!/usr/bin/env python3
"""Posts a redacted failure log comment to the release issue on failure."""

import argparse
import os
import sys
from pathlib import Path

from common import run


def parse_args():
    parser = argparse.ArgumentParser(description="Post failure details to release issue.")
    parser.add_argument("log_file", help="Path to log file")
    parser.add_argument("workflow_name", help="Name of workflow/action step that failed")
    return parser.parse_args()


def redact_tokens(content: str) -> str:
    tokens = [
        os.environ.get("PUSH_TOKEN"),
        os.environ.get("GH_TOKEN"),
        os.environ.get("GITHUB_TOKEN"),
    ]
    redacted = content
    for token in tokens:
        if token and len(token) > 3:
            redacted = redacted.replace(token, "<redacted token>")
    return redacted


def next_steps(workflow_name: str) -> str:
    if workflow_name == "Publish release":
        return (
            "Next: resolve the blockers above. Submit an **Approve** review on the release PR "
            "and wait for all required checks to pass. **Leave the PR open and in draft; do not merge it.**\n\n"
            "If no release tag was pushed, use **Re-run failed jobs** on the linked run, "
            "or remove and reapply `release:publish` on this issue. "
            "If the tag already exists, check the tag-triggered publishing run before retrying."
        )
    if workflow_name == "Prepare release":
        return (
            "Next: fix the error above. If the release work branch was already pushed, "
            "check for an existing release PR before retrying; preparation cannot recreate "
            "an existing branch. Otherwise use **Re-run failed jobs** on the linked run "
            "or remove and reapply `release:prepare` on this issue."
        )
    return (
        "Next: fix the error above and check whether the maintenance branch was already created. "
        "If it exists, open a Release issue targeting that branch. Otherwise use "
        "**Re-run failed jobs** on the linked run or remove and reapply `release:branch`."
    )


def main():
    args = parse_args()
    issue_number = os.environ.get("ISSUE_NUMBER", "")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")

    log_path = Path(args.log_file)
    tail_lines = ""

    if log_path.exists() and log_path.stat().st_size > 0:
        raw_log = log_path.read_text(encoding="utf-8", errors="replace")
        redacted = redact_tokens(raw_log)
        lines = redacted.splitlines()
        tail_lines = "\n".join(lines[-80:])
    else:
        tail_lines = "No script output was captured."

    body = (
        f"{args.workflow_name} failed.\n\n"
        f"```text\n{tail_lines}\n```\n\n"
        f"{next_steps(args.workflow_name)}\n\n"
        f"Run: {server_url}/{repository}/actions/runs/{run_id}\n"
    )

    if issue_number:
        run(["gh", "issue", "comment", issue_number, "--repo", repository, "--body", body])
        print(f"Posted failure comment to issue #{issue_number}")
    else:
        print(f"No ISSUE_NUMBER set; failure message was:\n{body}")


if __name__ == "__main__":
    main()
