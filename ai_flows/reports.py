"""Validate independent review reports and render their human-readable record.

This module validates evidence structure, not the truth of a review. The host
must create an independent reviewer and the coordinator must verify HEAD and CI.
"""

from html import escape
from pathlib import PurePosixPath, PureWindowsPath
import re


MAX_TEXT_LENGTH = 8000
MAX_ITEMS = 200
REPORT_FIELDS = {
    "schema_version",
    "reviewer",
    "reviewed_commit_sha",
    "round",
    "summary",
    "strengths",
    "blocking_findings",
    "suggestions",
    "acceptance_checks",
    "test_evidence",
}
FINDING_FIELDS = {"id", "priority", "file", "line", "evidence", "recommendation"}


class ReportError(ValueError):
    """The supplied review report does not conform to the report contract."""


def _object(value: object, fields: set[str], path: str) -> dict:
    if not isinstance(value, dict):
        raise ReportError(f"{path}: expected an object")
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing fields {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown fields {', '.join(sorted(str(key) for key in unknown))}")
        raise ReportError(f"{path}: {'; '.join(details)}")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReportError(f"{path}: expected a nonempty string")
    if len(value) > MAX_TEXT_LENGTH:
        raise ReportError(f"{path}: exceeds {MAX_TEXT_LENGTH} characters")
    if "\x00" in value:
        raise ReportError(f"{path}: NUL characters are not permitted")
    return value


def _integer(value: object, path: str, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        limit = f" between 1 and {maximum}" if maximum is not None else " greater than zero"
        raise ReportError(f"{path}: expected an integer{limit}")
    return value


def _items(value: object, path: str, required: bool = False) -> list:
    if not isinstance(value, list):
        raise ReportError(f"{path}: expected an array")
    if required and not value:
        raise ReportError(f"{path}: must contain at least one item")
    if len(value) > MAX_ITEMS:
        raise ReportError(f"{path}: exceeds {MAX_ITEMS} items")
    return value


def _strings(value: object, path: str) -> None:
    for index, item in enumerate(_items(value, path)):
        _text(item, f"{path}[{index}]")


def _relative_file(value: object, path: str) -> None:
    file = _text(value, path)
    posix = PurePosixPath(file)
    windows = PureWindowsPath(file)
    if (
        posix.is_absolute()
        or windows.root
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
        or not posix.parts
    ):
        raise ReportError(f"{path}: expected a repository-relative file path without '..'")


def _findings(value: object) -> None:
    ids = set()
    for index, item in enumerate(_items(value, "blocking_findings")):
        path = f"blocking_findings[{index}]"
        finding = _object(item, FINDING_FIELDS, path)
        finding_id = _text(finding["id"], f"{path}.id")
        if finding_id in ids:
            raise ReportError(f"{path}.id: duplicate finding ID")
        ids.add(finding_id)
        if finding["priority"] not in ("P0", "P1", "P2"):
            raise ReportError(f"{path}.priority: expected P0, P1, or P2")
        _relative_file(finding["file"], f"{path}.file")
        _integer(finding["line"], f"{path}.line")
        _text(finding["evidence"], f"{path}.evidence")
        _text(finding["recommendation"], f"{path}.recommendation")


def _evidence(value: object, path: str, description: str, evidence: str) -> None:
    for index, item in enumerate(_items(value, path, required=True)):
        item_path = f"{path}[{index}]"
        item = _object(item, {description, "passed", evidence}, item_path)
        _text(item[description], f"{item_path}.{description}")
        _text(item[evidence], f"{item_path}.{evidence}")
        if type(item["passed"]) is not bool:
            raise ReportError(f"{item_path}.passed: expected a boolean")


def validate_report(value: object) -> dict:
    """Validate the complete report, retaining failed evidence and object identity.

    A structurally valid report can contain blockers or failed acceptance/tests.
    Only the coordinator can decide whether it permits workflow completion.
    """
    report = _object(value, REPORT_FIELDS, "report")
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise ReportError("schema_version: expected integer 1")
    _text(report["reviewer"], "reviewer")
    sha = _text(report["reviewed_commit_sha"], "reviewed_commit_sha")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ReportError("reviewed_commit_sha: expected a complete lowercase 40-character SHA")
    _integer(report["round"], "round", maximum=100)
    _text(report["summary"], "summary")
    _strings(report["strengths"], "strengths")
    _strings(report["suggestions"], "suggestions")
    _findings(report["blocking_findings"])
    _evidence(report["acceptance_checks"], "acceptance_checks", "criterion", "evidence")
    _evidence(report["test_evidence"], "test_evidence", "command", "summary")
    return report


def _markdown(value: str) -> str:
    """Keep report text from creating HTML comments or Markdown structure."""
    value = escape(value, quote=False)
    value = re.sub(r"([\\`*_{}\[\]()#+\-!|])", r"\\\1", value)
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def render_review(report: dict) -> str:
    """Render validated evidence; machine-readable markers belong to the caller."""
    report = validate_report(report)
    lines = [
        "## 独立审查",
        "",
        f"审查者：{_markdown(report['reviewer'])}",
        f"提交：{report['reviewed_commit_sha']}",
        f"轮次：{report['round']}",
        "",
        _markdown(report["summary"]),
        "",
        "### 优点",
        "",
    ]
    lines.extend(f"- {_markdown(item)}" for item in report["strengths"])
    if not report["strengths"]:
        lines.append("未特别列出。")
    lines.extend(["", "### 阻塞问题", ""])
    for finding in report["blocking_findings"]:
        lines.extend([
            f"#### {_markdown(finding['id'])} · {finding['priority']}",
            "",
            f"位置：{_markdown(finding['file'])}，第 {finding['line']} 行。",
            "",
            f"证据：{_markdown(finding['evidence'])}",
            "",
            f"建议：{_markdown(finding['recommendation'])}",
            "",
        ])
    if not report["blocking_findings"]:
        lines.append("未发现已确认的阻塞问题。")
    lines.extend(["", "### 非阻塞建议", ""])
    lines.extend(f"- {_markdown(item)}" for item in report["suggestions"])
    if not report["suggestions"]:
        lines.append("无。")
    lines.extend(["", "### 验收检查", ""])
    for item in report["acceptance_checks"]:
        outcome = "通过" if item["passed"] else "未通过"
        lines.append(f"- [{outcome}] {_markdown(item['criterion'])}：{_markdown(item['evidence'])}")
    lines.extend(["", "### 测试证据", ""])
    for item in report["test_evidence"]:
        outcome = "通过" if item["passed"] else "未通过"
        lines.append(f"- [{outcome}] {_markdown(item['command'])}：{_markdown(item['summary'])}")
    return "\n".join(lines) + "\n"
