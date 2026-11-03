"""GitHub is the durable store; the host agent supplies the actual workers."""

import base64
import hashlib
import json
import re
from urllib.parse import quote

from .reports import ReportError, render_review, validate_report


class FlowError(ValueError):
    """A workflow invariant was not met; no success may be inferred."""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def marker(kind, value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(data.encode()).decode()
    return f"<!-- ai-flows:{kind}:v1:{encoded} -->"


def read_marker(body, kind):
    """Only the final line is authoritative; prose cannot insert another record."""
    last = (body or "").rstrip().split("\n")[-1]
    match = re.fullmatch(rf"<!-- ai-flows:{kind}:v1:([A-Za-z0-9_=-]+) -->", last)
    if not match:
        return None
    try:
        value = json.loads(base64.b64decode(match[1], altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError) as error:
        raise FlowError(f"Invalid {kind} metadata") from error
    if not isinstance(value, dict):
        raise FlowError(f"Invalid {kind} metadata object")
    return value


def validate_config(config):
    if not isinstance(config, dict) or set(config) != {"required_checks", "max_fix_rounds"}:
        raise FlowError("Config must contain required_checks and max_fix_rounds only")
    checks = config["required_checks"]
    if (
        not isinstance(checks, list) or not checks
        or any(not isinstance(name, str) or not name.strip() for name in checks)
        or len(set(checks)) != len(checks)
    ):
        raise FlowError("required_checks must be a nonempty list of unique check names")
    maximum = config["max_fix_rounds"]
    if type(maximum) is not int or not 0 <= maximum <= 20:
        raise FlowError("max_fix_rounds must be an integer between 0 and 20")
    return config


class Flow:
    def __init__(self, github, repo):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo):
            raise FlowError("repo must be OWNER/REPO")
        if repo.split("/")[1] in {".", ".."}:
            raise FlowError("Invalid repository name")
        self.github = github
        self.repo = repo
        self.root = f"repos/{repo}"

    def actor(self):
        user = self.github.api("user")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            raise FlowError("Cannot identify the authenticated GitHub coordinator")
        return user["login"]

    def issue(self, number):
        issue = self.github.api(f"{self.root}/issues/{number}")
        if not isinstance(issue, dict) or "pull_request" in issue:
            raise FlowError("Expected a task Issue, not a pull request")
        meta = read_marker(issue.get("body"), "run")
        if meta is None:
            raise FlowError("Issue is not managed; use adopt to preserve and register it")
        expected = {"key", "task_digest", "coordinator", "required_checks", "max_fix_rounds"}
        if set(meta) != expected:
            raise FlowError("Issue has invalid workflow metadata fields")
        for name in ("key", "task_digest"):
            if not isinstance(meta[name], str) or not re.fullmatch(r"[0-9a-f]{64}", meta[name]):
                raise FlowError("Issue has invalid workflow identity")
        if not isinstance(meta["coordinator"], str) or not meta["coordinator"]:
            raise FlowError("Issue has invalid coordinator")
        validate_config({name: meta[name] for name in ("required_checks", "max_fix_rounds")})
        return issue, meta

    def _find_key(self, key):
        found = []
        for item in self.github.pages(f"{self.root}/issues", params={"state": "all"}):
            if "pull_request" not in item:
                meta = read_marker(item.get("body"), "run")
                if meta and meta.get("key") == key:
                    found.append(item)
        if len(found) > 1:
            raise FlowError("Duplicate run keys found; pause for manual reconciliation")
        return found[0] if found else None

    def _metadata(self, key, task_digest, config):
        if not isinstance(key, str) or not key.strip():
            raise FlowError("A stable nonempty task key is required")
        validate_config(config)
        return {
            "key": digest(f"{self.repo}\n{key}"),
            "task_digest": task_digest,
            "coordinator": self.actor(),
            **config,
        }

    def start(self, *, key, title, task, plan, config):
        if any(not isinstance(value, str) or not value.strip() for value in (title, task, plan)):
            raise FlowError("Title, task and plan must all be nonempty")
        if len(title) > 256:
            raise FlowError("Issue title is too long")
        fingerprint = digest(json.dumps([title, task, plan, config], sort_keys=True))
        meta = self._metadata(key, fingerprint, config)
        existing = self._find_key(meta["key"])
        if existing:
            _, saved = self.issue(existing["number"])
            if saved != meta:
                raise FlowError("Task key already exists with different requirements or settings")
            return {"issue": existing["number"], "url": existing["html_url"], "reused": True}
        body = f"## 任务\n\n{task.strip()}\n\n## 方案与验收条件\n\n{plan.strip()}\n\n{marker('run', meta)}"
        if len(body) > 60000:
            raise FlowError("Issue body exceeds the workflow's 60000-character limit")
        issue = self.github.api(f"{self.root}/issues", method="POST", payload={"title": title, "body": body})
        return {"issue": issue["number"], "url": issue["html_url"], "reused": False}

    def adopt(self, *, number, key, config):
        current = self.github.api(f"{self.root}/issues/{number}")
        if "pull_request" in current or current.get("state") != "open":
            raise FlowError("Only an open task Issue can be adopted")
        body = current.get("body") or ""
        existing_meta = read_marker(body, "run")
        meta = self._metadata(key, digest(body), config)
        found = self._find_key(meta["key"])
        if found and found["number"] != number:
            raise FlowError("Task key belongs to a different Issue")
        if existing_meta:
            _, saved = self.issue(number)
            if any(saved[name] != meta[name] for name in ("key", "coordinator", "required_checks", "max_fix_rounds")):
                raise FlowError("Existing Issue metadata conflicts with adoption")
            return {"issue": number, "url": current["html_url"], "reused": True}
        new_body = f"{body.rstrip()}\n\n{marker('run', meta)}"
        if len(new_body) > 60000:
            raise FlowError("Issue body is too long to adopt")
        self.github.api(f"{self.root}/issues/{number}", method="PATCH", payload={"body": new_body})
        return {"issue": number, "url": current["html_url"], "reused": False}

    def open_pr(self, *, issue_number, branch, base, body=""):
        issue, meta = self.issue(issue_number)
        if issue.get("state") != "open":
            raise FlowError("Task Issue must be open")
        self._require_actor(meta)
        if not branch or branch == base:
            raise FlowError("Use a separate nonempty task branch")
        expected = {"issue": issue_number, "run_key": meta["key"]}
        found = self.github.pages(
            f"{self.root}/pulls", params={"state": "all", "head": f"{self.repo.split('/')[0]}:{branch}"}
        )
        if len(found) > 1:
            raise FlowError("Multiple PRs use this task branch; reconcile manually")
        if found:
            item = found[0]
            if read_marker(item.get("body"), "pr") != expected:
                raise FlowError("Branch already belongs to an unrelated PR")
            if item.get("state") != "open":
                raise FlowError("Task PR is closed; do not silently create a replacement")
            return {"pr": item["number"], "url": item["html_url"], "reused": True}
        content = f"{body.strip()}\n\nCloses #{issue_number}\n\n{marker('pr', expected)}"
        if len(content) > 60000:
            raise FlowError("PR body exceeds the workflow limit")
        result = self.github.api(f"{self.root}/pulls", method="POST", payload={
            "title": issue["title"], "head": branch, "base": base,
            "body": content, "draft": True,
        })
        return {"pr": result["number"], "url": result["html_url"], "reused": False}

    def _require_actor(self, meta):
        if self.actor() != meta["coordinator"]:
            raise FlowError("Coordinator account changed; explicit manual handoff is required")

    def context(self, number):
        pr = self.github.api(f"{self.root}/pulls/{number}")
        if not isinstance(pr, dict) or pr.get("state") != "open":
            raise FlowError("PR must be open")
        identity = read_marker(pr.get("body"), "pr")
        if not identity or set(identity) != {"issue", "run_key"} or type(identity["issue"]) is not int:
            raise FlowError("PR has no valid ai-flows task identity")
        issue, meta = self.issue(identity["issue"])
        if issue.get("state") != "open" or identity["run_key"] != meta["key"]:
            raise FlowError("PR and task Issue identities do not match an open run")
        head = pr.get("head", {})
        if head.get("repo", {}).get("full_name") != self.repo:
            raise FlowError("Only same-repository task branches are supported")
        if not re.fullmatch(r"[0-9a-f]{40}", head.get("sha", "")):
            raise FlowError("PR has an invalid HEAD SHA")
        return pr, meta

    def reviews(self, number, meta):
        records = []
        items = self.github.pages(f"{self.root}/pulls/{number}/reviews")
        for item in sorted(items, key=lambda value: value["id"]):
            if item.get("user", {}).get("login") != meta["coordinator"]:
                continue
            saved = read_marker(item.get("body"), "review")
            if saved is None or saved.get("run_key") != meta["key"]:
                continue
            if set(saved) != {"run_key", "report"} or item.get("state") != "COMMENTED":
                raise FlowError("A managed review is invalid or dismissed; manual reconciliation required")
            try:
                report = validate_report(saved["report"])
            except ReportError as error:
                raise FlowError(f"Stored review is invalid: {error}") from error
            if item.get("commit_id") != report["reviewed_commit_sha"]:
                raise FlowError("Review commit differs from its recorded SHA")
            if report["round"] != len(records) + 1:
                raise FlowError("Review rounds are duplicated or out of order; reconcile manually")
            if report["round"] > meta["max_fix_rounds"] + 1:
                raise FlowError("Stored review exceeds the configured round limit")
            records.append({"report": report, "url": item["html_url"]})
        return records

    def _pause(self, number, meta):
        comments = self.github.pages(f"{self.root}/issues/{number}/comments")
        for comment in reversed(comments):
            if comment.get("user", {}).get("login") == meta["coordinator"]:
                saved = read_marker(comment.get("body"), "pause")
                if saved and saved.get("run_key") == meta["key"]:
                    return saved
        return None

    def publish_review(self, *, number, report):
        report = validate_report(report)
        pr, meta = self.context(number)
        self._require_actor(meta)
        if self._pause(number, meta):
            raise FlowError("Run is paused; manual reconciliation is required")
        if report["reviewed_commit_sha"] != pr["head"]["sha"]:
            raise FlowError("Stale review: reviewer must inspect the current PR HEAD")
        records = self.reviews(number, meta)
        for saved in records:
            if saved["report"] == report:
                return {"review_url": saved["url"], "round": report["round"], "reused": True}
        if report["round"] != len(records) + 1:
            raise FlowError("Report round must be the next consecutive review round")
        if report["round"] > meta["max_fix_rounds"] + 1:
            raise FlowError("Review limit reached; pause and request human judgment")
        if records and records[-1]["report"]["reviewed_commit_sha"] == report["reviewed_commit_sha"]:
            raise FlowError("No new commit since the last review; do not spend another round")
        text = render_review(report) + "\n\n" + marker("review", {"run_key": meta["key"], "report": report})
        if len(text) > 60000:
            raise FlowError("Rendered review exceeds 60000 characters")
        # A second read catches branch changes during report preparation.
        current, _ = self.context(number)
        if current["head"]["sha"] != report["reviewed_commit_sha"]:
            raise FlowError("PR HEAD changed before publishing; review the new commit")
        result = self.github.api(f"{self.root}/pulls/{number}/reviews", method="POST", payload={
            "event": "COMMENT", "commit_id": report["reviewed_commit_sha"], "body": text,
        })
        return {"review_url": result["html_url"], "round": report["round"], "reused": False}

    def checks(self, sha, required):
        runs = self.github.pages(f"{self.root}/commits/{sha}/check-runs", field="check_runs", params={"filter": "latest"})
        statuses = self.github.pages(f"{self.root}/commits/{sha}/statuses")
        contexts = {}
        for status in statuses:
            contexts.setdefault(status["context"], status["state"])
        missing, pending, failed = [], [], []
        for name in required:
            matching = [run for run in runs if run.get("name") == name]
            if matching:
                if any(run.get("status") == "completed" and run.get("conclusion") not in {"success"} for run in matching):
                    failed.append(name)
                elif any(run.get("status") != "completed" for run in matching):
                    pending.append(name)
            elif name in contexts:
                if contexts[name] in {"failure", "error"}:
                    failed.append(name)
                elif contexts[name] != "success":
                    pending.append(name)
            else:
                missing.append(name)
        return {"passed": not (missing or pending or failed), "missing": missing, "pending": pending, "failed": failed}

    @staticmethod
    def _unmet(report):
        return bool(report["blocking_findings"]) or any(not row["passed"] for row in report["acceptance_checks"] + report["test_evidence"])

    def status(self, number):
        pr, meta = self.context(number)
        records = self.reviews(number, meta)
        sha = pr["head"]["sha"]
        result = {
            "repo": self.repo, "pr": number, "url": pr["html_url"],
            "issue": read_marker(pr["body"], "pr")["issue"], "head_sha": sha,
            "round": len(records), "max_fix_rounds": meta["max_fix_rounds"],
            "next_round": len(records) + 1, "blockers": [], "state": "needs_review", "reason": "Current HEAD needs an independent review",
        }
        paused = self._pause(number, meta)
        latest = records[-1]["report"] if records else None
        if latest:
            result["blockers"] = latest["blocking_findings"]
        if paused:
            result.update(state="paused", reason=paused.get("reason", "Manual pause"))
        elif latest is None or latest["reviewed_commit_sha"] != sha:
            if len(records) >= meta["max_fix_rounds"] + 1:
                result.update(state="limit_reached", reason="No review rounds remain for the new HEAD")
        elif self._unmet(latest):
            if len(records) >= meta["max_fix_rounds"] + 1:
                result.update(state="limit_reached", reason="Review limit reached with unmet requirements")
            else:
                result.update(state="needs_fix", reason="Resolve blocking findings or failed acceptance/test evidence")
                if len(records) >= 2 and self._unmet(records[-2]["report"]):
                    old = {row["id"] for row in records[-2]["report"]["blocking_findings"]}
                    new = {row["id"] for row in latest["blocking_findings"]}
                    if old and old == new:
                        result.update(state="paused", reason="The same blocking findings remain after a fix; request human judgment")
        else:
            ci = self.checks(sha, meta["required_checks"])
            result["ci"] = ci
            if ci["failed"]:
                result.update(state="ci_failed", reason="Required CI checks failed")
            elif not ci["passed"]:
                result.update(state="waiting_ci", reason="Required CI is missing or unfinished")
            else:
                result.update(state="ready", reason="Latest HEAD reviewed, acceptance and tests passed, required CI succeeded")
        # Status is a snapshot, not permission to merge a different revision.
        final, _ = self.context(number)
        if final["head"]["sha"] != sha:
            raise FlowError("PR changed while reading status; retry against the new HEAD")
        return result

    def pause(self, *, number, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 8000:
            raise FlowError("A nonempty pause reason of at most 8000 characters is required")
        _, meta = self.context(number)
        self._require_actor(meta)
        previous = self._pause(number, meta)
        if previous:
            return {"state": "paused", "reason": previous["reason"], "reused": True}
        saved = {"run_key": meta["key"], "reason": reason}
        body = f"## 流程暂停\n\n{reason}\n\n{marker('pause', saved)}"
        comment = self.github.api(f"{self.root}/issues/{number}/comments", method="POST", payload={"body": body})
        return {"state": "paused", "reason": reason, "url": comment["html_url"], "reused": False}
