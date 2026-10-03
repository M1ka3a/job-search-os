#!/usr/bin/env python3
"""Write one dated section to the Daily Plan document."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
import re

from build_planner_context import (
    DocumentFetchError,
    classify_failure,
    fetch_document,
    load_resource_cache,
    resolve_resources,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TITLE = "Daily Plan"
MAX_UPDATE_ATTEMPTS = 3
RETRY_DELAYS = (0.25, 0.5)


class WriterError(RuntimeError):
    """A clear, user-facing writer failure."""


def resolve_daily_plan() -> dict[str, str]:
    resources = load_resource_cache()
    resource = resources.get("pages", {}).get(REQUIRED_TITLE) if resources else None
    if not isinstance(resource, dict) or not resource.get("obj_token"):
        resources = resolve_resources()
        resource = resources.get("pages", {}).get(REQUIRED_TITLE)
    if not isinstance(resource, dict) or resource.get("title") != REQUIRED_TITLE:
        raise WriterError("Resolver did not return the Daily Plan resource")
    if resource.get("obj_type") != "docx":
        raise WriterError("Daily Plan resource is not a writable docx document")
    return resource


def update_document(document_token: str, content: str) -> None:
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
            if response.get("data", {}).get("result") not in (None, "success"):
                raise WriterError(f"Daily Plan update was not successful: {output.strip()}")
            return

        details = output.strip() or "no details"
        transient, _ = classify_failure(details)
        if not transient or attempt == MAX_UPDATE_ATTEMPTS:
            raise WriterError(f"Unable to update Daily Plan: {details}")
        time.sleep(RETRY_DELAYS[attempt - 1])


def read_plan(path: Path) -> str:
    if not path.is_file():
        raise WriterError(f"Plan file not found: {path}")
    content = path.read_text().strip()
    if not content:
        raise WriterError(f"Plan file is empty: {path}")
    return content


DATE_HEADING = re.compile(r"(?m)^## (?P<date>\d{4}-\d{2}-\d{2})[ \t]*$")
ROOT_HEADING = re.compile(r"(?m)^# Daily Plan[ \t]*$")


def dated_plan_content(existing: str, today: str, plan: str) -> str:
    """Insert or replace today's section without rewriting other dates."""
    section = f"## {today}\n\n{plan.strip()}\n"
    headings = list(DATE_HEADING.finditer(existing))
    matching = [heading for heading in headings if heading.group("date") == today]

    if matching:
        heading = matching[0]
        next_heading = next(
            (candidate for candidate in headings if candidate.start() > heading.start()),
            None,
        )
        end = next_heading.start() if next_heading else len(existing)
        old_section = existing[heading.start() : end]
        trailing_newlines = old_section[len(old_section.rstrip("\r\n")) :]
        if not trailing_newlines:
            trailing_newlines = "\n\n" if next_heading else "\n"
        return existing[: heading.start()] + section + trailing_newlines + existing[end:]

    root = ROOT_HEADING.search(existing)
    if not root:
        raise WriterError("Daily Plan is missing the '# Daily Plan' heading")
    insertion = root.end()

    # Migrate the previous single-day format without guessing its date.
    if not headings:
        legacy_date = re.search(r"(?m)^Date:\s*(\d{4}-\d{2}-\d{2})\s*$", existing)
        if legacy_date:
            body = existing[root.end() :]
            body = re.sub(r"\A\s*Date:\s*\d{4}-\d{2}-\d{2}\s*", "", body, count=1)
            if legacy_date.group(1) == today:
                return existing[: root.start()] + "# Daily Plan\n\n" + section
            legacy_section = f"## {legacy_date.group(1)}\n\n{body.strip()}\n"
            return (
                existing[: root.start()]
                + "# Daily Plan\n\n"
                + section
                + "\n"
                + legacy_section
            )
        if existing[root.end() :].strip():
            raise WriterError("Daily Plan has no dated sections and its legacy date is unavailable")

    return existing[:insertion] + "\n\n" + section + existing[insertion:]


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/write_daily_plan.py PLAN_MARKDOWN", file=sys.stderr)
        return 2

    try:
        plan_path = Path(sys.argv[1]).resolve()
        plan = read_plan(plan_path)
        today = date.today().isoformat()
        resource = resolve_daily_plan()
        document_token = resource["obj_token"]

        try:
            existing = fetch_document(document_token, REQUIRED_TITLE).content
        except DocumentFetchError as exc:
            raise WriterError(f"Unable to read existing Daily Plan before update: {exc}") from exc
        content = dated_plan_content(existing, today, plan)

        try:
            update_document(document_token, content)
        except WriterError:
            raise

        try:
            read_back = fetch_document(document_token, REQUIRED_TITLE)
        except DocumentFetchError as exc:
            raise WriterError(f"Daily Plan write verification failed: {exc}") from exc
        plan_lines = [line.strip() for line in plan.splitlines() if line.strip()]
        section_pattern = re.compile(
            rf"(?ms)^## {re.escape(today)}[ \t]*\n(?P<section>.*?)(?=^## \d{{4}}-\d{{2}}-\d{{2}}[ \t]*$|\Z)"
        )
        section_match = section_pattern.search(read_back.content)
        if not section_match or any(marker not in section_match.group("section") for marker in (plan_lines[0], plan_lines[-1])):
            raise WriterError("Daily Plan write verification failed: written content was not found")
        if len(DATE_HEADING.findall(read_back.content)) != len(set(DATE_HEADING.findall(read_back.content))):
            raise WriterError("Daily Plan write verification failed: duplicate dated section")

        print(f"Daily Plan updated and verified for {today}.")
        return 0
    except (OSError, WriterError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
