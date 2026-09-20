#!/usr/bin/env python3
"""Validate and deterministically apply a confirmed Evening Review proposal."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from build_planner_context import (
    ContextError,
    DocumentFetchError,
    classify_failure,
    fetch_document,
    load_resource_cache,
    resolve_resources,
)


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_PAGES = ("Review Log", "Backlog", "Current State")
BUCKETS = ("Active", "Next", "Later")
MAX_UPDATE_ATTEMPTS = 3
RETRY_DELAYS = (0.25, 0.5)


class ReviewError(RuntimeError):
    """A clear, user-facing review failure."""


def load_proposal(path: Path) -> dict[str, Any]:
    try:
        proposal = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Invalid proposal JSON: {path}") from exc
    validate_proposal(proposal)
    return proposal


def require_string(value: Any, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or "\n" in value:
        raise ReviewError(f"Invalid {label}: expected a single-line string")


def validate_proposal(proposal: Any) -> None:
    if not isinstance(proposal, dict):
        raise ReviewError("Proposal must be a JSON object")
    required = {
        "date",
        "completed",
        "partial",
        "blocked",
        "new_gaps",
        "carry_over",
        "backlog_changes",
        "current_state_changes",
    }
    if set(proposal) != required:
        raise ReviewError("Proposal fields do not match the required review schema")
    require_string(proposal["date"], "date")
    if proposal["date"] != date.today().isoformat():
        raise ReviewError(f"Proposal date must be {date.today().isoformat()}")

    for field in ("completed", "partial", "blocked", "new_gaps", "carry_over", "backlog_changes", "current_state_changes"):
        if not isinstance(proposal[field], list):
            raise ReviewError(f"Proposal field {field!r} must be an array")
    for item in proposal["completed"]:
        validate_object(item, ("task", "evidence"), "completed item")
    for item in proposal["partial"]:
        validate_object(item, ("task", "progress", "remaining"), "partial item")
    for item in proposal["blocked"]:
        validate_object(item, ("task", "reason"), "blocked item")
    for item in proposal["new_gaps"] + proposal["carry_over"]:
        require_string(item, "review list item")
    for item in proposal["backlog_changes"]:
        validate_object(item, ("item", "from", "to", "reason"), "backlog change")
        if item["from"] not in (*BUCKETS, "", "None") or item["to"] not in BUCKETS:
            raise ReviewError("Backlog changes must use Active, Next, Later, or None")
    for item in proposal["current_state_changes"]:
        validate_object(item, ("field", "old", "new", "evidence"), "Current State change")


def validate_object(value: Any, fields: tuple[str, ...], label: str) -> None:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ReviewError(f"Invalid {label} structure")
    for field in fields:
        require_string(value[field], f"{label}.{field}", allow_empty=field == "old")


def resources_for_apply() -> dict[str, Any]:
    resources = load_resource_cache()
    if resources and all(title in resources.get("pages", {}) for title in ALLOWED_PAGES):
        return resources
    return resolve_resources()


def update_document(document_token: str, content: str, title: str) -> None:
    command = [
        "lark-cli",
        "docs",
        "+update",
        "--doc",
        document_token,
        "--as",
        "user",
        "--doc-format",
        "markdown",
        "--command",
        "overwrite",
        "--content",
        "-",
        "--format",
        "json",
    ]
    for attempt in range(1, MAX_UPDATE_ATTEMPTS + 1):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            input=content,
            text=True,
            capture_output=True,
            check=False,
        )
        output = completed.stdout or completed.stderr
        try:
            response = json.loads(output)
        except json.JSONDecodeError:
            response = None
        if completed.returncode == 0 and isinstance(response, dict) and response.get("ok") is True:
            return
        details = output.strip() or "no details"
        transient, _ = classify_failure(details)
        if not transient or attempt == MAX_UPDATE_ATTEMPTS:
            raise ReviewError(f"Unable to update {title}: {details}")
        time.sleep(RETRY_DELAYS[attempt - 1])


def sections(text: str) -> dict[str, tuple[int, int]]:
    lines = text.splitlines()
    starts: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = re.fullmatch(r"## (Active|Next|Later)\s*", line.strip())
        if match:
            starts[match.group(1)] = index
    if set(starts) != set(BUCKETS):
        raise ReviewError("Backlog does not contain the required Active/Next/Later structure")
    ordered = sorted(starts.items(), key=lambda item: item[1])
    return {
        title: (start, ordered[index + 1][1] if index + 1 < len(ordered) else len(lines))
        for index, (title, start) in enumerate(ordered)
    }


def item_lines(lines: list[str], bounds: tuple[int, int], item: str) -> list[int]:
    start, end = bounds
    return [
        index
        for index in range(start + 1, end)
        if lines[index].strip() in {f"- {item}", f"* {item}"}
    ]


def apply_backlog_changes(text: str, changes: list[dict[str, str]]) -> str:
    lines = text.splitlines()
    for change in changes:
        item = change["item"].strip()
        source = change["from"]
        destination = change["to"]
        bounds = sections("\n".join(lines))
        all_matches = sum((item_lines(lines, section, item) for section in bounds.values()), [])
        if source in BUCKETS:
            matches = item_lines(lines, bounds[source], item)
            if len(matches) != 1:
                raise ReviewError(
                    f"Cannot safely move backlog item {item!r}: found {len(matches)} in {source}"
                )
            if len(all_matches) != 1:
                raise ReviewError(f"Backlog item {item!r} is not uniquely identifiable")
            source_index = matches[0]
            lines.pop(source_index)
        else:
            if all_matches:
                raise ReviewError(f"Backlog item {item!r} already exists; refusing to duplicate it")

        bounds = sections("\n".join(lines))
        insert_at = bounds[destination][1]
        while insert_at > bounds[destination][0] + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, f"- {item}")
    return "\n".join(lines)


def review_markdown(proposal: dict[str, Any]) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- None recorded."

    completed = [f"{item['task']} — {item['evidence']}" for item in proposal["completed"]]
    partial = [
        f"{item['task']} — {item['progress']}; remaining: {item['remaining']}"
        for item in proposal["partial"]
    ]
    blocked = [f"{item['task']} — {item['reason']}" for item in proposal["blocked"]]
    return "\n".join(
        [
            f"## {proposal['date']}",
            "",
            "### Completed",
            bullets(completed),
            "",
            "### Partial",
            bullets(partial),
            "",
            "### Blocked",
            bullets(blocked),
            "",
            "### New Gaps",
            bullets(proposal["new_gaps"]),
            "",
            "### Carry Over",
            bullets(proposal["carry_over"]),
        ]
    )


def preview(proposal: dict[str, Any]) -> str:
    def lines(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- None"

    completed = [f"{item['task']}: {item['evidence']}" for item in proposal["completed"]]
    partial = [f"{item['task']}: {item['progress']} (remaining: {item['remaining']})" for item in proposal["partial"]]
    blocked = [f"{item['task']}: {item['reason']}" for item in proposal["blocked"]]
    backlog = [f"{item['item']}: {item['from'] or 'None'} → {item['to']}" for item in proposal["backlog_changes"]]
    state = [f"{item['field']}: {item['old']} → {item['new']}" for item in proposal["current_state_changes"]]
    return "\n".join(
        [
            f"Evening Review — {proposal['date']}",
            "",
            "Completed",
            lines(completed),
            "",
            "Partial",
            lines(partial),
            "",
            "Blocked / Not Done",
            lines(blocked),
            "",
            "New Gaps",
            lines(proposal["new_gaps"]),
            "",
            "Carry Over",
            lines(proposal["carry_over"]),
            "",
            "Backlog Changes",
            lines(backlog),
            "",
            "Current State Changes",
            lines(state),
        ]
    )


def apply(path: Path) -> list[str]:
    proposal = load_proposal(path)
    resources = resources_for_apply()
    pages = resources["pages"]
    documents = {
        title: fetch_document(pages[title]["obj_token"], title)
        for title in ALLOWED_PAGES
    }

    review_content = documents["Review Log"].content
    if re.search(rf"^## {re.escape(proposal['date'])}\s*$", review_content, re.MULTILINE):
        raise ReviewError(f"Review Log already contains a {proposal['date']} entry")
    backlog_content = apply_backlog_changes(documents["Backlog"].content, proposal["backlog_changes"])
    current_state_content = documents["Current State"].content
    for change in proposal["current_state_changes"]:
        if not change["old"] or current_state_content.count(change["old"]) != 1:
            raise ReviewError(f"Current State old value is not uniquely matched for {change['field']!r}")
        current_state_content = current_state_content.replace(change["old"], change["new"], 1)

    modified: list[str] = []
    try:
        if backlog_content != documents["Backlog"].content:
            update_document(pages["Backlog"]["obj_token"], backlog_content, "Backlog")
            if "# Backlog" not in fetch_document(pages["Backlog"]["obj_token"], "Backlog").content:
                raise ReviewError("Backlog read-back verification failed")
            modified.append("Backlog")
        if current_state_content != documents["Current State"].content:
            update_document(pages["Current State"]["obj_token"], current_state_content, "Current State")
            if not all(
                change["new"] in fetch_document(pages["Current State"]["obj_token"], "Current State").content
                for change in proposal["current_state_changes"]
            ):
                raise ReviewError("Current State read-back verification failed")
            modified.append("Current State")
        new_review = review_content.rstrip() + "\n\n" + review_markdown(proposal) + "\n"
        update_document(pages["Review Log"]["obj_token"], new_review, "Review Log")
        if not re.search(rf"^## {re.escape(proposal['date'])}\s*$", fetch_document(pages["Review Log"]["obj_token"], "Review Log").content, re.MULTILINE):
            raise ReviewError("Review Log read-back verification failed")
        modified.append("Review Log")
    except (ReviewError, DocumentFetchError) as exc:
        raise ReviewError(f"Partial failure. Modified pages: {', '.join(modified) or 'none'}. {exc}") from exc
    return modified


def main() -> int:
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[1] not in {"--preview", "--validate-only"}):
        print("Usage: python3 scripts/apply_review.py [--preview|--validate-only] PROPOSAL_JSON", file=sys.stderr)
        return 2
    mode = sys.argv[1] if len(sys.argv) == 3 else None
    path = Path(sys.argv[-1]).resolve()
    try:
        proposal = load_proposal(path)
        if mode == "--preview":
            print(preview(proposal))
        elif mode == "--validate-only":
            print("Review proposal is valid.")
        else:
            modified = apply(path)
            print(f"Modified Lark pages: {', '.join(modified)}")
        return 0
    except (OSError, ContextError, ReviewError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
