#!/usr/bin/env python3
"""Unit tests for release automation scripts."""

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import common
import comment_issue_failure
import prepare_release
import publish_release


class TestMaintainerAuthorization(unittest.TestCase):
    @patch("common.run")
    def test_admin_and_maintain_can_release(self, run):
        for permission in ("admin", "maintain"):
            with self.subTest(permission=permission):
                run.return_value = MagicMock(stdout=f"{permission}\n")
                common.require_maintainer("maintainer", "connectbot/cbssh")
                run.assert_called_with([
                    "gh", "api", "repos/connectbot/cbssh/collaborators/maintainer/permission",
                    "--jq", ".permission",
                ])

    @patch("common.run")
    def test_lesser_or_unknown_permissions_cannot_release(self, run):
        for permission in ("write", "triage", "read", "none", "", "unknown"):
            with self.subTest(permission=permission):
                run.return_value = MagicMock(stdout=f"{permission}\n")
                with self.assertRaisesRegex(PermissionError, "maintain/admin access"):
                    common.require_maintainer("contributor", "connectbot/cbssh")


class TestPrepareGuidance(unittest.TestCase):
    @patch("prepare_release.run")
    def test_new_pr_is_draft_and_explains_approval_without_merging(self, run):
        run.side_effect = [
            MagicMock(stdout="[]"),
            MagicMock(stdout="https://github.com/connectbot/cbssh/pull/261\n"),
        ]
        number = prepare_release.upsert_pr(
            "release-work/0.4.2", "main", "0.4.2", "0.4.3-SNAPSHOT", "v0.4.2", "260",
        )
        self.assertEqual(number, "261")
        command = run.call_args.args[0]
        self.assertIn("--draft", command)
        body = command[command.index("--body") + 1]
        self.assertIn("Approve", body)
        self.assertIn("Do not merge", body)
        self.assertIn("issue #260", body)
        self.assertIn("release:publish", body)

    @patch("prepare_release.run")
    def test_existing_draft_is_updated_without_reconverting_it(self, run):
        run.return_value = MagicMock(stdout='[{"number": 261, "isDraft": true}]')
        prepare_release.upsert_pr(
            "release-work/0.4.2", "main", "0.4.2", "0.4.3-SNAPSHOT", "v0.4.2", "260",
        )
        commands = [call.args[0][:3] for call in run.call_args_list]
        self.assertIn(["gh", "pr", "edit"], commands)
        self.assertNotIn(["gh", "pr", "ready"], commands)


class TestPublishReadiness(unittest.TestCase):
    def setUp(self):
        self.pr = {
            "isDraft": True,
            "headRefOid": "reviewed-sha",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "DRAFT",
        }
        self.reviews = [{
            "id": 1,
            "state": "APPROVED",
            "commit_id": "reviewed-sha",
            "submitted_at": "2026-09-16T01:00:00Z",
            "user": {"login": "reviewer"},
        }]
        self.checks = [{"name": "test", "workflow": "CI", "bucket": "pass", "state": "SUCCESS"}]

    def validate(self, checks=None, returncode=0, reviews=None, permission="maintain", **changes):
        pr = {**self.pr, **changes}

        def run_command(command, **_kwargs):
            if command[:4] == ["gh", "api", "--paginate", "--slurp"]:
                return MagicMock(
                    stdout=json.dumps([self.reviews if reviews is None else reviews]),
                    stderr="", returncode=0,
                )
            if command[:2] == ["gh", "api"]:
                return MagicMock(stdout=f"{permission}\n", stderr="", returncode=0)
            return MagicMock(
                stdout=json.dumps(self.checks if checks is None else checks),
                stderr="", returncode=returncode,
            )

        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "connectbot/cbssh"}), \
                patch("publish_release.run", side_effect=run_command):
            publish_release.require_publish_ready("261", pr)

    def test_approved_draft_with_passing_required_checks_can_publish(self):
        self.validate()

    def test_existing_non_draft_release_can_still_publish(self):
        self.validate(isDraft=False, mergeStateStatus="CLEAN")

    def test_missing_approval_explained_before_generic_blocked_state(self):
        with self.assertRaises(RuntimeError) as error:
            self.validate(reviews=[], mergeStateStatus="BLOCKED")
        self.assertIn("Approve review for its current head commit", str(error.exception))
        self.assertIn("do not merge", str(error.exception))
        self.assertNotIn("merge state BLOCKED", str(error.exception))

    def test_approval_for_previous_head_does_not_count(self):
        with self.assertRaisesRegex(RuntimeError, "current head commit"):
            self.validate(reviews=[{**self.reviews[0], "commit_id": "stale-sha"}])

    def test_later_request_changes_supersedes_approval(self):
        reviews = [
            self.reviews[0],
            {**self.reviews[0], "id": 2, "state": "CHANGES_REQUESTED"},
        ]
        with self.assertRaisesRegex(RuntimeError, "current head commit"):
            self.validate(reviews=reviews)

    def test_later_approval_supersedes_request_changes(self):
        reviews = [
            {**self.reviews[0], "state": "CHANGES_REQUESTED"},
            {**self.reviews[0], "id": 2, "state": "APPROVED"},
        ]
        self.validate(reviews=reviews)

    def test_comment_does_not_supersede_approval(self):
        reviews = [
            self.reviews[0],
            {**self.reviews[0], "id": 2, "state": "COMMENTED"},
        ]
        self.validate(reviews=reviews)

    def test_dismissed_approval_does_not_count(self):
        with self.assertRaisesRegex(RuntimeError, "current head commit"):
            self.validate(reviews=[{**self.reviews[0], "state": "DISMISSED"}])

    def test_approval_accepts_maintainer_permissions(self):
        for permission in ("maintain", "admin"):
            with self.subTest(permission=permission):
                self.validate(permission=permission)

    def test_approval_requires_maintainer_permission(self):
        for permission in ("write", "triage", "read", "none"):
            with self.subTest(permission=permission), self.assertRaisesRegex(RuntimeError, "maintain or admin"):
                self.validate(permission=permission)

    def test_approval_and_pending_check_reported_together(self):
        with self.assertRaises(RuntimeError) as error:
            self.validate(
                checks=[{**self.checks[0], "bucket": "pending", "state": "IN_PROGRESS"}],
                returncode=8, reviews=[],
            )
        self.assertIn("Approve review for its current head commit", str(error.exception))
        self.assertIn("Wait for required check 'test'", str(error.exception))

    def test_failed_required_check_is_actionable_even_with_nonzero_cli_exit(self):
        with self.assertRaisesRegex(RuntimeError, "Fix or rerun required check 'test'"):
            self.validate(
                checks=[{**self.checks[0], "bucket": "fail", "state": "FAILURE"}], returncode=1,
            )

    def test_skipped_or_cancelled_required_checks_do_not_pass(self):
        for bucket in ("skipping", "cancel"):
            with self.subTest(bucket=bucket), self.assertRaisesRegex(RuntimeError, "Fix or rerun"):
                self.validate(checks=[{**self.checks[0], "bucket": bucket}])

    def test_missing_required_checks_explains_draft_ci_setup(self):
        with self.assertRaisesRegex(RuntimeError, "make sure CI runs on draft release PRs"):
            self.validate(checks=[])

    def test_draft_does_not_bypass_conflicts(self):
        with self.assertRaisesRegex(RuntimeError, "Resolve conflicts"):
            self.validate(mergeable="CONFLICTING")

    def test_draft_does_not_bypass_other_blocked_states(self):
        for state in ("BLOCKED", "BEHIND", "DIRTY", "UNKNOWN", "UNSTABLE"):
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, "remaining branch requirements"):
                self.validate(mergeStateStatus=state)

    def test_draft_status_requires_actual_draft(self):
        with self.assertRaises(RuntimeError):
            self.validate(isDraft=False)

    def test_api_failure_does_not_look_like_passing_checks(self):
        def run_command(command, **_kwargs):
            if command[:4] == ["gh", "api", "--paginate", "--slurp"]:
                return MagicMock(stdout=json.dumps([self.reviews]), stderr="", returncode=0)
            if command[:2] == ["gh", "api"]:
                return MagicMock(stdout="maintain\n", stderr="", returncode=0)
            return MagicMock(stdout="", stderr="Resource not accessible by integration", returncode=1)

        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "connectbot/cbssh"}), \
                patch("publish_release.run", side_effect=run_command):
            with self.assertRaisesRegex(RuntimeError, "Checks and Commit statuses read permissions"):
                publish_release.require_publish_ready("261", self.pr)

    def test_review_api_failure_does_not_look_like_missing_approval(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "connectbot/cbssh"}), \
                patch("publish_release.run", side_effect=[
                    MagicMock(stdout="", stderr="Resource not accessible by integration", returncode=1),
                    MagicMock(stdout=json.dumps(self.checks), stderr="", returncode=0),
                ]):
            with self.assertRaisesRegex(RuntimeError, "Pull requests read permission"):
                publish_release.require_publish_ready("261", self.pr)

    def test_reviewer_permission_api_failure_is_reported(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "connectbot/cbssh"}), \
                patch("publish_release.run", side_effect=[
                    MagicMock(stdout=json.dumps([self.reviews]), stderr="", returncode=0),
                    MagicMock(stdout="", stderr="Resource not accessible by integration", returncode=1),
                    MagicMock(stdout=json.dumps(self.checks), stderr="", returncode=0),
                ]):
            with self.assertRaisesRegex(RuntimeError, "Metadata read permission"):
                publish_release.require_publish_ready("261", self.pr)

    @patch("publish_release.append_github_output")
    @patch("publish_release.configure_git_credentials")
    @patch("publish_release.configure_git_author")
    @patch("publish_release.require_missing_remote_tag")
    @patch("publish_release.require_maintainer")
    @patch("publish_release.parse_args")
    @patch("publish_release.run")
    @patch.dict(os.environ, {"GITHUB_REPOSITORY": "connectbot/cbssh"})
    def test_head_change_after_review_prevents_tag_and_push(self, run, args, *_mocks):
        args.return_value = MagicMock(
            release_version="1.2.3", next_version="1.2.4-SNAPSHOT",
            target_branch="main", tag_prefix="v",
        )
        run.side_effect = [
            MagicMock(stdout='[{"number": 261}]'),
            MagicMock(stdout=json.dumps({
                **self.pr, "baseRefName": "main", "headRefName": "release-work/1.2.3",
                "headRefOid": "reviewed-sha",
            })),
            MagicMock(stdout=json.dumps([self.reviews]), returncode=0),
            MagicMock(stdout="maintain\n", returncode=0),
            MagicMock(stdout=json.dumps(self.checks), returncode=0),
            MagicMock(),  # Fetch branches.
            MagicMock(stdout="changed-sha\n"),
        ]
        with self.assertRaisesRegex(RuntimeError, "changed after PR checks"):
            publish_release.main()
        commands = [call.args[0][:2] for call in run.call_args_list]
        self.assertNotIn(["git", "tag"], commands)
        self.assertNotIn(["git", "push"], commands)


class TestFailureGuidance(unittest.TestCase):
    @patch("comment_issue_failure.run")
    @patch("comment_issue_failure.parse_args")
    def test_failure_comment_includes_redacted_log_and_retry_steps(self, args, run):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "publish.log"
            log.write_text("Release requires approval; token=secret-token", encoding="utf-8")
            args.return_value = MagicMock(log_file=str(log), workflow_name="Publish release")
            with patch.dict(os.environ, {
                "ISSUE_NUMBER": "260", "GITHUB_REPOSITORY": "connectbot/cbssh",
                "GITHUB_RUN_ID": "123", "GH_TOKEN": "secret-token",
                "GITHUB_SERVER_URL": "https://github.com",
            }):
                comment_issue_failure.main()
        command = run.call_args.args[0]
        body = command[command.index("--body") + 1]
        self.assertIn("<redacted token>", body)
        self.assertNotIn("secret-token", body)
        self.assertIn("Approve", body)
        self.assertIn("do not merge", body)
        self.assertIn("Re-run failed jobs", body)
        self.assertIn("remove and reapply `release:publish`", body)
        self.assertIn("https://github.com/connectbot/cbssh/actions/runs/123", body)

    def test_prepare_retry_warns_about_existing_branch(self):
        self.assertIn("already pushed", comment_issue_failure.next_steps("Prepare release"))

    def test_branch_completion_points_to_release_issue(self):
        self.assertIn("open a Release issue", comment_issue_failure.next_steps("Create release branch"))


class TestFieldExtraction(unittest.TestCase):
    def test_extract_field_simple(self):
        body = (
            "### Release version\n\n"
            "1.2.3\n\n"
            "### Next version\n\n"
            "1.2.4-SNAPSHOT\n\n"
            "### Target branch\n\n"
            "main\n"
        )
        self.assertEqual(common.extract_issue_field_line(body, "Release version"), "1.2.3")
        self.assertEqual(common.extract_issue_field_line(body, "Next version"), "1.2.4-SNAPSHOT")
        self.assertEqual(common.extract_issue_field_line(body, "Target branch"), "main")

    def test_extract_field_with_html_comments(self):
        body = (
            "### Release version\n"
            "<!-- enter version below -->\n"
            "2.0.0\n"
            "<!-- end of comment -->\n\n"
            "### Next version\n"
            "2.0.1-SNAPSHOT\n"
        )
        self.assertEqual(common.extract_issue_field_line(body, "Release version"), "2.0.0")
        self.assertEqual(common.extract_issue_field_line(body, "Next version"), "2.0.1-SNAPSHOT")

    def test_extract_multiline_field(self):
        body = (
            "### Release notes\n"
            "Initial line\n\n"
            "* Feature 1\n"
            "* Feature 2\n\n"
            "### Another field\n"
            "value\n"
        )
        notes = common.extract_issue_field(body, "Release notes")
        self.assertIn("* Feature 1", notes)
        self.assertIn("* Feature 2", notes)
        self.assertNotIn("### Another field", notes)

    def test_extract_missing_field(self):
        body = "### Some field\nvalue\n"
        self.assertEqual(common.extract_issue_field(body, "Nonexistent"), "")
        self.assertEqual(common.extract_issue_field_line(body, "Nonexistent"), "")


class TestValidation(unittest.TestCase):
    def test_validate_release_version(self):
        common.validate_release_version("0.1.0")
        common.validate_release_version("1.2.3")
        common.validate_release_version("10.200.3000")

        with self.assertRaises(ValueError):
            common.validate_release_version("v1.2.3")
        with self.assertRaises(ValueError):
            common.validate_release_version("1.2")
        with self.assertRaises(ValueError):
            common.validate_release_version("1.2.3-SNAPSHOT")
        with self.assertRaises(ValueError):
            common.validate_release_version("1.2.3.4")

    def test_validate_next_version(self):
        common.validate_next_version("0.1.1-SNAPSHOT")
        common.validate_next_version("1.2.4-SNAPSHOT")

        with self.assertRaises(ValueError):
            common.validate_next_version("1.2.4")
        with self.assertRaises(ValueError):
            common.validate_next_version("v1.2.4-SNAPSHOT")

    def test_validate_release_target_branch(self):
        common.validate_release_target_branch("main")
        common.validate_release_target_branch("release/1.0")
        common.validate_release_target_branch("release/12.34")

        with self.assertRaises(ValueError):
            common.validate_release_target_branch("master")
        with self.assertRaises(ValueError):
            common.validate_release_target_branch("feature/branch")
        with self.assertRaises(ValueError):
            common.validate_release_target_branch("release/1.0.1")

    def test_validate_maintenance_branch(self):
        common.validate_maintenance_branch("release/1.0")
        common.validate_maintenance_branch("release/2.5")

        with self.assertRaises(ValueError):
            common.validate_maintenance_branch("main")
        with self.assertRaises(ValueError):
            common.validate_maintenance_branch("release/1")


class TestRedaction(unittest.TestCase):
    def test_redact_tokens(self):
        with patch.dict(os.environ, {
            "PUSH_TOKEN": "secret_push_12345",
            "GH_TOKEN": "secret_gh_67890",
            "GITHUB_TOKEN": "secret_github_abcde",
        }):
            log = "Pushing using token secret_push_12345 and secret_gh_67890 to GitHub"
            redacted = comment_issue_failure.redact_tokens(log)
            self.assertNotIn("secret_push_12345", redacted)
            self.assertNotIn("secret_gh_67890", redacted)
            self.assertIn("<redacted token>", redacted)


if __name__ == "__main__":
    unittest.main()
