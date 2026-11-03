"""Exercise the subprocess/HTTP boundary without using live GitHub credentials."""

import json
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ai_flows.github import GitHub, GitHubError


class GitHubBoundaryTest(unittest.TestCase):
    def test_payload_is_json_stdin_not_shell_code(self):
        with patch("ai_flows.github.subprocess.run", return_value=subprocess.CompletedProcess([], 0, '{"number":1}', "")) as run:
            payload = {"body": "literal $(touch /tmp/unsafe); `anything`\ntext"}
            result = GitHub().api("repos/owner/repo/issues", method="POST", payload=payload)
            self.assertEqual(result["number"], 1)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["gh", "api", "--method", "POST", "repos/owner/repo/issues", "--input", "-"])
            self.assertEqual(json.loads(kwargs["input"]), payload)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["timeout"], 60)

    def test_timeout_and_missing_binary_are_failures(self):
        for error in (subprocess.TimeoutExpired(["gh"], 60), FileNotFoundError("gh not installed")):
            with self.subTest(error=type(error).__name__), patch("ai_flows.github.subprocess.run", side_effect=error), self.assertRaises(GitHubError):
                GitHub().api("user")

    def test_unauthorized_and_invalid_json_are_failures(self):
        for response in (subprocess.CompletedProcess([], 1, "", "HTTP 401 Unauthorized"), subprocess.CompletedProcess([], 0, "<html>not JSON</html>", "")):
            with self.subTest(response=response), patch("ai_flows.github.subprocess.run", return_value=response), self.assertRaises(GitHubError):
                GitHub().api("user")

    def test_pagination_reads_past_a_full_first_page(self):
        requests = []

        class PagedGitHub(GitHub):
            def api(self, path, **kwargs):
                requests.append(path)
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                return list(range(100)) if page == 1 else [100]

        rows = PagedGitHub().pages("repos/owner/repo/issues", params={"state": "all"})
        self.assertEqual(rows, list(range(101)))
        self.assertEqual(len(requests), 2)
        self.assertEqual(parse_qs(urlsplit(requests[1]).query)["state"], ["all"])

    def test_check_run_pagination_uses_named_array(self):
        class PagedGitHub(GitHub):
            def api(self, path, **kwargs):
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                return {"total_count": 101, "check_runs": [{"id": n} for n in range(100)] if page == 1 else [{"id": 100}]}

        rows = PagedGitHub().pages("repos/owner/repo/commits/sha/check-runs", field="check_runs", params={"filter": "latest"})
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1]["id"], 100)

    def test_invalid_paginated_response_is_not_a_successful_empty_list(self):
        class BadGitHub(GitHub):
            def api(self, path, **kwargs):
                return {"message": "Unavailable"}

        with self.assertRaises(GitHubError):
            BadGitHub().pages("repos/owner/repo/issues")


if __name__ == "__main__":
    unittest.main()
