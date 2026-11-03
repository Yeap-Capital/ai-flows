"""JSON CLI. Models run in the host; this process validates remote state."""

import argparse
import json
import sys
from pathlib import Path

from .flow import Flow, FlowError, validate_config
from .github import GitHub, GitHubError
from .reports import ReportError


def read_text(path):
    file = Path(path)
    if file.stat().st_size > 1_000_000:
        raise FlowError(f"Input file is too large: {path}")
    return file.read_text(encoding="utf-8")


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser():
    result = argparse.ArgumentParser(description="GitHub gates for a cloud-agent issue → PR → review → fix workflow")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("doctor", "Check GitHub access and workflow configuration"),
        ("start", "Create or reuse a task Issue before implementing"),
        ("adopt", "Register an existing Issue without replacing its contents"),
        ("pr", "Open or reuse the task's draft PR"),
        ("review", "Publish a validated independent-agent review"),
        ("status", "Read current HEAD, review and CI gates"),
        ("pause", "Record a durable pause with a reason"),
        ("validate-config", "Validate configuration without network access"),
    ]:
        command = commands.add_parser(name, help=help_text)
        if name != "validate-config":
            command.add_argument("--repo", required=True, help="OWNER/REPO")
        if name in {"doctor", "start", "adopt", "validate-config"}:
            command.add_argument("--config", default=".ai-flows.json")
        if name in {"start", "adopt"}:
            command.add_argument("--key", required=True, help="Stable task identifier; reuse it on retries")
            command.add_argument("--max-fix-rounds", type=int)
        if name == "start":
            command.add_argument("--title", required=True)
            command.add_argument("--task-file", required=True)
            command.add_argument("--plan-file", required=True)
        if name in {"adopt", "pr"}:
            command.add_argument("--issue", type=positive, required=True)
        if name == "pr":
            command.add_argument("--branch", required=True, help="Already-pushed same-repository task branch")
            command.add_argument("--base", default="main")
            command.add_argument("--body-file")
        if name in {"review", "status", "pause"}:
            command.add_argument("--pr", type=positive, required=True)
        if name == "review":
            command.add_argument("--report", required=True)
        if name == "pause":
            command.add_argument("--reason", required=True)
        if name == "status":
            command.add_argument("--require-ready", action="store_true", help="Exit 3 unless state is ready")
    return result


def run(args):
    config = None
    if hasattr(args, "config"):
        config = json.loads(read_text(args.config))
        validate_config(config)
        if getattr(args, "max_fix_rounds", None) is not None:
            config["max_fix_rounds"] = args.max_fix_rounds
        validate_config(config)
    if args.command == "validate-config":
        return {"valid": True, **config}
    flow = Flow(GitHub(), args.repo)
    if args.command == "doctor":
        repo = flow.github.api(flow.root)
        return {
            "repo": repo["full_name"], "coordinator": flow.actor(),
            "permissions": repo.get("permissions", {}), "config": config,
            "agent_execution": "Host must supply actual independent agents; CLI does not call a model",
        }
    if args.command == "start":
        return flow.start(key=args.key, title=args.title, task=read_text(args.task_file), plan=read_text(args.plan_file), config=config)
    if args.command == "adopt":
        return flow.adopt(number=args.issue, key=args.key, config=config)
    if args.command == "pr":
        return flow.open_pr(issue_number=args.issue, branch=args.branch, base=args.base, body=read_text(args.body_file) if args.body_file else "")
    if args.command == "review":
        return flow.publish_review(number=args.pr, report=json.loads(read_text(args.report)))
    if args.command == "status":
        return flow.status(args.pr)
    if args.command == "pause":
        return flow.pause(number=args.pr, reason=args.reason)
    raise FlowError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except (FlowError, ReportError, GitHubError, OSError, ValueError) as error:
        print(json.dumps({"state": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if getattr(args, "require_ready", False) and result.get("state") != "ready":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
