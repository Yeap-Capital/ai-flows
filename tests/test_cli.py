"""Exercise the public CLI in real child processes, without GitHub writes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def call(self, *arguments, cwd=None):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        return subprocess.run(
            [sys.executable, "-m", "ai_flows", *arguments],
            cwd=cwd or ROOT, env=env, text=True, capture_output=True, timeout=10,
        )

    def test_help_does_not_require_credentials(self):
        result = self.call("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("review", result.stdout)

    def test_validate_config_works_outside_repository_with_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "workflow config.json"
            config.write_text(json.dumps({"required_checks": ["test"], "max_fix_rounds": 0}))
            result = self.call("validate-config", "--config", str(config), cwd=directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_missing_config_fails_with_json_diagnostic(self):
        result = self.call("validate-config", "--config", "/does-not-exist.json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stderr)["state"], "error")

    def test_empty_required_checks_cannot_disable_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"required_checks": [], "max_fix_rounds": 3}')
            result = self.call("validate-config", "--config", str(config))
        self.assertEqual(result.returncode, 1)
        self.assertIn("required_checks", json.loads(result.stderr)["error"])

    def test_nonobject_config_with_override_fails_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text("[]")
            result = self.call("adopt", "--repo", "owner/repo", "--issue", "1", "--key", "task", "--max-fix-rounds", "3", "--config", str(config))
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(json.loads(result.stderr)["state"], "error")

    def test_invalid_report_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text("{}")
            result = self.call("review", "--repo", "owner/repo", "--pr", "1", "--report", str(report))
        self.assertEqual(result.returncode, 1)
        self.assertIn("report", json.loads(result.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
