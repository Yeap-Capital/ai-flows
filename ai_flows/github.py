"""Small GitHub boundary using the caller's existing gh authentication."""

import json
import subprocess
from urllib.parse import urlencode


class GitHubError(RuntimeError):
    """A request failed or returned an unexpected response."""


class GitHub:
    def api(self, path, *, method="GET", payload=None):
        command = ["gh", "api", "--method", method, path]
        data = None
        if payload is not None:
            command += ["--input", "-"]
            data = json.dumps(payload, ensure_ascii=False)
        try:
            result = subprocess.run(
                command, input=data, text=True, capture_output=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitHubError(f"GitHub request could not complete: {error}") from error
        if result.returncode:
            # gh's standard diagnostics never include the authentication token.
            raise GitHubError(result.stderr.strip()[:2000] or "GitHub request failed")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError as error:
            raise GitHubError("GitHub returned invalid JSON") from error

    def pages(self, path, *, field=None, params=None):
        """Read every page; never silently decide from a truncated first page."""
        collected = []
        for page in range(1, 1001):
            query = dict(params or {}, per_page=100, page=page)
            value = self.api(f"{path}?{urlencode(query)}")
            batch = value.get(field) if field and isinstance(value, dict) else value
            if not isinstance(batch, list):
                raise GitHubError(f"Expected an array from {path}")
            collected.extend(batch)
            if len(batch) < 100:
                return collected
        raise GitHubError("Pagination limit reached; refusing an incomplete result")
