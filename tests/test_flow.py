"""Exercise workflow transitions against a small in-memory GitHub boundary."""

from copy import deepcopy
import unittest

from ai_flows.flow import Flow, FlowError, marker, read_marker, validate_config


REPO = "owner/project"
SHA = "a" * 40
NEW_SHA = "b" * 40
CONFIG = {"required_checks": ["unit-tests", "workflow-validation"], "max_fix_rounds": 3}


def report(sha=SHA, round_number=1, *, blocking=False):
    return {
        "schema_version": 1,
        "reviewer": "independent-reviewer",
        "reviewed_commit_sha": sha,
        "round": round_number,
        "summary": "Inspected the current code and task acceptance requirements.",
        "strengths": ["The change stays within the task scope."],
        "blocking_findings": ([{
            "id": "F001", "priority": "P1", "file": "src/main.py", "line": 3,
            "evidence": "The failure path returns success.",
            "recommendation": "Return a failing status and add a regression test.",
        }] if blocking else []),
        "suggestions": ["A future change may add a shorter help example."],
        "acceptance_checks": [{"criterion": "Task behavior", "passed": True, "evidence": "Scenario exercised."}],
        "test_evidence": [{"command": "python -m unittest", "passed": True, "summary": "Tests passed."}],
    }


class MemoryGitHub:
    """An API-level fake with records shaped like the actual GitHub REST API."""

    def __init__(self):
        self.login = "coordinator"
        self.issues = {}
        self.pulls = {}
        self.reviews = {}
        self.comments = {}
        self.check_runs = {}
        self.statuses = {}
        self.next_number = 1
        self.next_review = 100
        self.writes = []
        self.reads = []
        self.before_api = None
        self.before_pages = None

    def api(self, path, *, method="GET", payload=None):
        if self.before_api:
            self.before_api(path, method, payload)
        self.reads.append((path, method))
        if method != "GET":
            self.writes.append((path, method, deepcopy(payload)))
        if path == "user":
            return {"login": self.login}
        relative = path.removeprefix(f"repos/{REPO}/")
        pieces = relative.split("/")
        if relative == "issues" and method == "POST":
            number = self.next_number
            self.next_number += 1
            value = {"number": number, "html_url": f"https://github.com/{REPO}/issues/{number}", "state": "open", **payload}
            self.issues[number] = deepcopy(value)
            return deepcopy(value)
        if len(pieces) == 2 and pieces[0] == "issues":
            number = int(pieces[1])
            if method == "PATCH":
                self.issues[number].update(deepcopy(payload))
            return deepcopy(self.issues[number])
        if relative == "pulls" and method == "POST":
            number = self.next_number
            self.next_number += 1
            value = {
                "number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
                "state": "open", **payload,
                "head": {"ref": payload["head"], "sha": SHA, "repo": {"full_name": REPO}},
                "base": {"ref": payload["base"]},
            }
            self.pulls[number] = deepcopy(value)
            return deepcopy(value)
        if len(pieces) == 2 and pieces[0] == "pulls":
            return deepcopy(self.pulls[int(pieces[1])])
        if len(pieces) == 3 and pieces[0] == "pulls" and pieces[2] == "reviews" and method == "POST":
            number = int(pieces[1])
            value = {
                "id": self.next_review, "user": {"login": self.login},
                "state": "COMMENTED", "commit_id": payload["commit_id"],
                "body": payload["body"], "html_url": f"https://github.com/{REPO}/pull/{number}#review-{self.next_review}",
            }
            self.next_review += 1
            self.reviews.setdefault(number, []).append(deepcopy(value))
            return deepcopy(value)
        if len(pieces) == 3 and pieces[0] == "issues" and pieces[2] == "comments" and method == "POST":
            number = int(pieces[1])
            value = {"user": {"login": self.login}, "body": payload["body"], "html_url": f"https://github.com/{REPO}/pull/{number}#comment"}
            self.comments.setdefault(number, []).append(deepcopy(value))
            return deepcopy(value)
        raise AssertionError(f"Unexpected API request: {method} {path}")

    def pages(self, path, *, field=None, params=None):
        if self.before_pages:
            self.before_pages(path, field, params)
        self.reads.append((path, "PAGES"))
        relative = path.removeprefix(f"repos/{REPO}/")
        pieces = relative.split("/")
        if relative == "issues":
            return deepcopy(list(self.issues.values()))
        if relative == "pulls":
            branch = params["head"].split(":", 1)[1]
            return deepcopy([value for value in self.pulls.values() if value["head"]["ref"] == branch])
        if len(pieces) == 3 and pieces[0] == "pulls" and pieces[2] == "reviews":
            return deepcopy(self.reviews.get(int(pieces[1]), []))
        if len(pieces) == 3 and pieces[0] == "issues" and pieces[2] == "comments":
            return deepcopy(self.comments.get(int(pieces[1]), []))
        if len(pieces) == 3 and pieces[0] == "commits" and pieces[2] == "check-runs":
            return deepcopy(self.check_runs.get(pieces[1], []))
        if len(pieces) == 3 and pieces[0] == "commits" and pieces[2] == "statuses":
            return deepcopy(self.statuses.get(pieces[1], []))
        raise AssertionError(f"Unexpected paginated request: {path}")

    def pass_checks(self, sha=SHA):
        self.check_runs[sha] = [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in CONFIG["required_checks"]
        ]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.github = MemoryGitHub()
        self.flow = Flow(self.github, REPO)

    def start(self, **changes):
        arguments = {"key": "task-001", "title": "Implement task", "task": "Fix the actual behavior.", "plan": "Add a regression check and make it pass.", "config": deepcopy(CONFIG)}
        arguments.update(changes)
        return self.flow.start(**arguments)

    def task_pr(self, *, max_fix_rounds=3):
        config = {**CONFIG, "max_fix_rounds": max_fix_rounds}
        issue = self.start(config=config)["issue"]
        pr = self.flow.open_pr(issue_number=issue, branch=f"ai/issue-{issue}-task", base="main")["pr"]
        return issue, pr

    def clean_pr(self):
        issue, pr = self.task_pr()
        self.flow.publish_review(number=pr, report=report())
        self.github.pass_checks()
        return issue, pr

    def test_start_reuses_the_same_task_without_another_write(self):
        first = self.start()
        writes = len(self.github.writes)
        second = self.start()
        self.assertEqual(first["issue"], second["issue"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.github.writes), writes)
        body = self.github.issues[first["issue"]]["body"]
        self.assertIn("Fix the actual behavior.", body)
        self.assertIn("Add a regression check", body)

    def test_same_key_cannot_silently_change_requirements(self):
        self.start()
        for changes in ({"task": "A different task."}, {"plan": "A different plan."}, {"config": {**CONFIG, "max_fix_rounds": 2}}):
            with self.subTest(changes=changes), self.assertRaises(FlowError):
                self.start(**changes)
        self.assertEqual(len(self.github.issues), 1)

    def test_duplicate_remote_run_keys_require_manual_reconciliation(self):
        first = self.start()["issue"]
        duplicate = deepcopy(self.github.issues[first])
        duplicate.update(number=77, html_url="issue/77")
        self.github.issues[77] = duplicate
        with self.assertRaisesRegex(FlowError, "Duplicate"):
            self.start()

    def test_adopt_preserves_body_and_is_repeatable(self):
        self.github.issues[7] = {"number": 7, "body": "Existing human plan.\n\nKeep this text.", "title": "Existing task", "state": "open", "html_url": "issue/7"}
        first = self.flow.adopt(number=7, key="existing", config=CONFIG)
        self.assertFalse(first["reused"])
        self.assertTrue(self.github.issues[7]["body"].startswith("Existing human plan.\n\nKeep this text."))
        self.assertIsNotNone(read_marker(self.github.issues[7]["body"], "run"))
        self.assertTrue(self.flow.adopt(number=7, key="existing", config=CONFIG)["reused"])

    def test_pr_creation_reuses_original_and_is_draft(self):
        issue, pr = self.task_pr()
        second = self.flow.open_pr(issue_number=issue, branch=f"ai/issue-{issue}-task", base="main")
        self.assertEqual(second["pr"], pr)
        self.assertTrue(second["reused"])
        self.assertTrue(self.github.pulls[pr]["draft"])
        self.assertIn(f"Closes #{issue}", self.github.pulls[pr]["body"])

    def test_unrelated_or_closed_pr_is_not_reused(self):
        issue, pr = self.task_pr()
        self.github.pulls[pr]["body"] = "Unrelated PR."
        with self.assertRaises(FlowError):
            self.flow.open_pr(issue_number=issue, branch=f"ai/issue-{issue}-task", base="main")
        self.github.pulls[pr]["body"] = marker("pr", {"issue": issue, "run_key": read_marker(self.github.issues[issue]["body"], "run")["key"]})
        self.github.pulls[pr]["state"] = "closed"
        with self.assertRaises(FlowError):
            self.flow.open_pr(issue_number=issue, branch=f"ai/issue-{issue}-task", base="main")

    def test_review_requires_current_head_and_consecutive_round(self):
        _, pr = self.task_pr()
        with self.assertRaisesRegex(FlowError, "Stale"):
            self.flow.publish_review(number=pr, report=report(NEW_SHA))
        with self.assertRaisesRegex(FlowError, "consecutive"):
            self.flow.publish_review(number=pr, report=report(round_number=2))
        self.assertEqual(self.github.reviews.get(pr, []), [])

    def test_review_retry_is_idempotent_and_cannot_re_review_same_sha(self):
        _, pr = self.task_pr()
        first = self.flow.publish_review(number=pr, report=report())
        second = self.flow.publish_review(number=pr, report=report())
        self.assertEqual(first["review_url"], second["review_url"])
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.github.reviews[pr]), 1)
        with self.assertRaisesRegex(FlowError, "No new commit"):
            self.flow.publish_review(number=pr, report=report(round_number=2))

    def test_posted_review_is_comment_and_pins_exact_commit(self):
        _, pr = self.task_pr()
        self.flow.publish_review(number=pr, report=report())
        submitted = self.github.writes[-1][2]
        self.assertEqual(submitted["event"], "COMMENT")
        self.assertEqual(submitted["commit_id"], SHA)
        record = read_marker(submitted["body"], "review")
        self.assertEqual(record["report"], report())

    def test_ready_requires_latest_clean_review_and_all_checks(self):
        _, pr = self.clean_pr()
        result = self.flow.status(pr)
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["ci"]["passed"])
        self.assertEqual(result["head_sha"], SHA)
        self.assertEqual(result["round"], 1)

    def test_nonblocking_suggestions_do_not_prevent_ready(self):
        _, pr = self.clean_pr()
        self.assertTrue(report()["suggestions"])
        self.assertEqual(self.flow.status(pr)["state"], "ready")

    def test_missing_pending_failed_or_skipped_checks_never_pass(self):
        _, pr = self.clean_pr()
        conditions = [
            ([], "waiting_ci"),
            ([{"name": "unit-tests", "status": "completed", "conclusion": "success"}], "waiting_ci"),
            ([{"name": "unit-tests", "status": "in_progress", "conclusion": None}], "waiting_ci"),
            ([{"name": "unit-tests", "status": "completed", "conclusion": "failure"}], "ci_failed"),
            ([{"name": "unit-tests", "status": "completed", "conclusion": "skipped"}], "ci_failed"),
            ([{"name": "unit-tests", "status": "completed", "conclusion": "neutral"}], "ci_failed"),
        ]
        for checks, expected in conditions:
            with self.subTest(checks=checks):
                self.github.check_runs[SHA] = checks
                self.assertEqual(self.flow.status(pr)["state"], expected)

    def test_duplicate_check_name_with_pending_run_does_not_pass(self):
        _, pr = self.clean_pr()
        self.github.check_runs[SHA].append({"name": "unit-tests", "status": "queued", "conclusion": None})
        self.assertEqual(self.flow.status(pr)["state"], "waiting_ci")

    def test_commit_status_fallback_uses_the_latest_state_per_context(self):
        _, pr = self.clean_pr()
        self.github.check_runs[SHA] = []
        self.github.statuses[SHA] = [
            {"context": "unit-tests", "state": "pending"},
            {"context": "unit-tests", "state": "success"},
            {"context": "workflow-validation", "state": "success"},
        ]
        self.assertEqual(self.flow.status(pr)["state"], "waiting_ci")
        self.github.statuses[SHA][0]["state"] = "failure"
        self.assertEqual(self.flow.status(pr)["state"], "ci_failed")
        self.github.statuses[SHA][0]["state"] = "success"
        self.assertEqual(self.flow.status(pr)["state"], "ready")

    def test_failed_acceptance_or_test_evidence_requires_fix(self):
        for category in ("acceptance_checks", "test_evidence"):
            with self.subTest(category=category):
                self.setUp()
                _, pr = self.task_pr()
                value = report()
                value[category][0]["passed"] = False
                self.flow.publish_review(number=pr, report=value)
                self.github.pass_checks()
                self.assertEqual(self.flow.status(pr)["state"], "needs_fix")

    def test_new_commit_invalidates_old_clean_review_and_old_ci(self):
        _, pr = self.clean_pr()
        self.github.pulls[pr]["head"]["sha"] = NEW_SHA
        self.assertEqual(self.flow.status(pr)["state"], "needs_review")
        self.flow.publish_review(number=pr, report=report(NEW_SHA, 2))
        result = self.flow.status(pr)
        self.assertEqual(result["state"], "waiting_ci")
        self.assertIn((f"repos/{REPO}/commits/{NEW_SHA}/check-runs", "PAGES"), self.github.reads)
        self.github.pass_checks(NEW_SHA)
        self.assertEqual(self.flow.status(pr)["state"], "ready")

    def test_foreign_actor_cannot_forge_a_clean_review(self):
        _, pr = self.task_pr()
        _, meta = self.flow.issue(1)
        self.github.reviews[pr] = [{
            "id": 1, "user": {"login": "outsider"}, "state": "COMMENTED", "commit_id": SHA,
            "body": marker("review", {"run_key": meta["key"], "report": report()}), "html_url": "foreign/review",
        }]
        self.github.pass_checks()
        self.assertEqual(self.flow.status(pr)["state"], "needs_review")

    def test_commit_id_mismatch_or_dismissed_review_is_rejected(self):
        _, pr = self.clean_pr()
        self.github.reviews[pr][0]["commit_id"] = NEW_SHA
        with self.assertRaisesRegex(FlowError, "commit"):
            self.flow.status(pr)
        self.github.reviews[pr][0]["commit_id"] = SHA
        self.github.reviews[pr][0]["state"] = "DISMISSED"
        with self.assertRaisesRegex(FlowError, "dismissed"):
            self.flow.status(pr)

    def test_zero_fix_budget_stops_after_initial_blocking_review(self):
        _, pr = self.task_pr(max_fix_rounds=0)
        self.flow.publish_review(number=pr, report=report(blocking=True))
        self.assertEqual(self.flow.status(pr)["state"], "limit_reached")
        self.github.pulls[pr]["head"]["sha"] = NEW_SHA
        with self.assertRaisesRegex(FlowError, "limit"):
            self.flow.publish_review(number=pr, report=report(NEW_SHA, 2))
        self.assertEqual(self.flow.status(pr)["state"], "limit_reached")

    def test_same_blocker_after_fix_pauses_for_human_judgment(self):
        _, pr = self.task_pr()
        self.flow.publish_review(number=pr, report=report(blocking=True))
        self.github.pulls[pr]["head"]["sha"] = NEW_SHA
        self.flow.publish_review(number=pr, report=report(NEW_SHA, 2, blocking=True))
        self.assertEqual(self.flow.status(pr)["state"], "paused")

    def test_explicit_pause_is_durable_idempotent_and_blocks_publication(self):
        _, pr = self.task_pr()
        self.flow.pause(number=pr, reason="Conflicting acceptance criteria need a decision.")
        second = self.flow.pause(number=pr, reason="Another reason.")
        self.assertTrue(second["reused"])
        self.assertEqual(self.flow.status(pr)["state"], "paused")
        with self.assertRaisesRegex(FlowError, "paused"):
            self.flow.publish_review(number=pr, report=report())

    def test_head_change_during_status_is_not_reported_ready(self):
        _, pr = self.clean_pr()
        reads = 0

        def change_head(path, method, payload):
            nonlocal reads
            if path == f"repos/{REPO}/pulls/{pr}" and method == "GET":
                reads += 1
                if reads == 2:
                    self.github.pulls[pr]["head"]["sha"] = NEW_SHA

        self.github.before_api = change_head
        with self.assertRaisesRegex(FlowError, "changed"):
            self.flow.status(pr)

    def test_head_change_before_review_publication_is_rejected(self):
        _, pr = self.task_pr()
        reads = 0

        def change_head(path, method, payload):
            nonlocal reads
            if path == f"repos/{REPO}/pulls/{pr}" and method == "GET":
                reads += 1
                if reads == 2:
                    self.github.pulls[pr]["head"]["sha"] = NEW_SHA

        self.github.before_api = change_head
        with self.assertRaisesRegex(FlowError, "changed"):
            self.flow.publish_review(number=pr, report=report())
        self.assertEqual(self.github.reviews.get(pr, []), [])

    def test_changed_coordinator_cannot_post_or_pause(self):
        _, pr = self.task_pr()
        self.github.login = "someone-else"
        with self.assertRaisesRegex(FlowError, "Coordinator"):
            self.flow.publish_review(number=pr, report=report())
        with self.assertRaisesRegex(FlowError, "Coordinator"):
            self.flow.pause(number=pr, reason="Pause")

    def test_fork_head_cannot_enter_managed_loop(self):
        _, pr = self.task_pr()
        self.github.pulls[pr]["head"]["repo"]["full_name"] = "outsider/project"
        with self.assertRaisesRegex(FlowError, "same-repository"):
            self.flow.status(pr)

    def test_configuration_cannot_disable_all_required_checks(self):
        for value in ({"required_checks": [], "max_fix_rounds": 3}, {"required_checks": ["unit", "unit"], "max_fix_rounds": 3}, {"required_checks": ["unit"], "max_fix_rounds": True}):
            with self.subTest(value=value), self.assertRaises(FlowError):
                validate_config(value)

    def test_embedded_marker_is_not_authoritative(self):
        body = marker("run", {"attacker": True}) + "\nHuman text follows."
        self.assertIsNone(read_marker(body, "run"))


if __name__ == "__main__":
    unittest.main()
