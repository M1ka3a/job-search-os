#!/usr/bin/env python3
"""Replace the Daily Plan document with a generated markdown plan."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

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


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/write_daily_plan.py PLAN_MARKDOWN", file=sys.stderr)
        return 2

    try:
        plan_path = Path(sys.argv[1]).resolve()
        plan = read_plan(plan_path)
        today = date.today().isoformat()
        content = f"# Daily Plan\n\nDate: {today}\n\n{plan}\n"
        resource = resolve_daily_plan()
        document_token = resource["obj_token"]

        try:
            update_document(document_token, content)
        except WriterError:
            raise

        try:
            read_back = fetch_document(document_token, REQUIRED_TITLE)
        except DocumentFetchError as exc:
            raise WriterError(f"Daily Plan write verification failed: {exc}") from exc
        plan_lines = [line.strip() for line in plan.splitlines() if line.strip()]
        markers = [today, plan_lines[0], plan_lines[-1]]
        if any(marker not in read_back.content for marker in markers):
            raise WriterError("Daily Plan write verification failed: written content was not found")

        print(f"Daily Plan updated and verified for {today}.")
        return 0
    except (OSError, WriterError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
