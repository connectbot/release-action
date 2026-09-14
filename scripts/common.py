#!/usr/bin/env python3
"""Common helpers and validation functions for release automation."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


def run(
    cmd: List[str],
    cwd: Optional[Path] = None,
    capture_output: bool = True,
    check: bool = True,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Run a shell command with proper error handling and logging."""
    environ = os.environ.copy()
    if env:
        environ.update(env)
    res = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
        check=False,
        env=environ,
    )
    if check and res.returncode != 0:
        cmd_str = " ".join(cmd)
        err = res.stderr.strip() if res.stderr else res.stdout.strip()
        raise RuntimeError(f"Command '{cmd_str}' failed (code {res.returncode}): {err}")
    return res


def extract_issue_field(body: str, heading: str) -> str:
    """Extract field text under a '### <heading>' section from GitHub issue markdown."""
    pattern = rf"^###\s+{re.escape(heading)}\s*$"
    lines = body.splitlines()
    found = False
    extracted_lines: List[str] = []

    for line in lines:
        if re.match(pattern, line.strip(), re.IGNORECASE):
            found = True
            continue
        if found:
            if re.match(r"^###\s+", line.strip()):
                break
            extracted_lines.append(line)

    content = "\n".join(extracted_lines)
    # Remove HTML comments
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    return content.strip()


def extract_issue_field_line(body: str, heading: str) -> str:
    """Extract single stripped line under heading."""
    val = extract_issue_field(body, heading)
    if not val:
        return ""
    first_line = val.splitlines()[0].strip()
    return re.sub(r"\s+", "", first_line)


def validate_release_version(version: str) -> None:
    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", version):
        raise ValueError(f"Release version '{version}' must be in X.Y.Z format (e.g., 1.2.3)")


def validate_next_version(version: str) -> None:
    if not re.match(r"^[0-9]+\.[0-9]+\.[0-9]+-SNAPSHOT$", version):
        raise ValueError(f"Next version '{version}' must be in X.Y.Z-SNAPSHOT format (e.g., 1.2.4-SNAPSHOT)")


def validate_release_target_branch(branch: str) -> None:
    if not re.match(r"^(main|release/[0-9]+\.[0-9]+)$", branch):
        raise ValueError(f"Target branch '{branch}' must be 'main' or 'release/<major.minor>'")


def validate_maintenance_branch(branch: str) -> None:
    if not re.match(r"^release/[0-9]+\.[0-9]+$", branch):
        raise ValueError(f"Maintenance branch '{branch}' must look like 'release/1.1'")


def require_maintainer(actor: Optional[str] = None, repo: Optional[str] = None) -> None:
    """Verify that the triggering actor has maintain/admin permission on the repo."""
    actor = actor or os.environ.get("RELEASE_ACTOR")
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not actor:
        raise RuntimeError("Release automation requires RELEASE_ACTOR to be set.")
    if not repo:
        raise RuntimeError("Release automation requires GITHUB_REPOSITORY to be set.")

    res = run(["gh", "api", f"repos/{repo}/collaborators/{actor}/permission", "--jq", ".permission"])
    permission = res.stdout.strip()
    if permission not in ("admin", "maintain"):
        raise PermissionError(
            f"Release automation must be started by a maintainer with maintain/admin access. "
            f"'{actor}' has '{permission}' permission on '{repo}'."
        )


def configure_git_author(
    name: str = "github-actions[bot]",
    email: str = "41898282+github-actions[bot]@users.noreply.github.com",
) -> None:
    run(["git", "config", "user.name", name])
    run(["git", "config", "user.email", email])


def configure_git_credentials(push_token: Optional[str] = None) -> None:
    token = push_token or os.environ.get("PUSH_TOKEN")
    if not token:
        raise RuntimeError("PUSH_TOKEN is required for authenticated git operations.")

    home = Path(os.environ.get("HOME", "~")).expanduser()
    git_credentials = home / ".git-credentials"
    run(["git", "config", "--global", "credential.helper", "store"])

    with open(git_credentials, "w", encoding="utf-8") as f:
        f.write(f"https://x-access-token:{token}@github.com\n")
    os.chmod(git_credentials, 0o600)


def require_remote_branch(branch: str) -> None:
    res = run(["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"], check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Remote branch '{branch}' does not exist on origin.")


def require_missing_remote_branch(branch: str) -> None:
    res = run(["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"], check=False)
    if res.returncode == 0:
        raise RuntimeError(f"Remote branch '{branch}' already exists on origin.")


def require_missing_remote_tag(tag: str) -> None:
    res = run(["git", "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tag}"], check=False)
    if res.returncode == 0:
        raise RuntimeError(f"Remote tag '{tag}' already exists on origin.")


def append_github_output(outputs: Dict[str, str]) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a", encoding="utf-8") as f:
        for k, v in outputs.items():
            f.write(f"{k}={v}\n")
