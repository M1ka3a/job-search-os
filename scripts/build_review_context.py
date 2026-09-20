#!/usr/bin/env python3
"""Build deterministic context for an Evening Review."""

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
    Document,
    ContextError,
    classify_failure,
    fetch_document,
    load_resource_cache,
    resolve_resources,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PAGES = ("Daily Plan", "Current State", "Backlog", "Review Log")
MAX_ATTEMPTS = 3
RETRY_DELAYS = (0.25, 0.5)


def resources_for_review() -> dict[str, Any]:
    resources = load_resource_cache()
    if resources and all(title in resources.get("pages", {}) for title in REQUIRED_PAGES):
        return resources
    return resolve_resources()


def fetch_scoped(document_token: str, args: list[str], title: str) -> str:
    command = [
        "lark-cli",
        "docs",
        "+fetch",
        "--doc",
        document_token,
        "--as",
        "user",
        "--format",
        "json",
        *args,
    ]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        completed = subprocess.run(
            command,
            cwd=ROOT,
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
            try:
                return response["data"]["document"]["content"]
            except (KeyError, TypeError) as exc:
                raise ContextError(f"{title} response did not contain content") from exc

        details = output.strip() or "no details"
        transient, _ = classify_failure(details)
        if not transient or attempt == MAX_ATTEMPTS:
            raise ContextError(f"Unable to read {title}: {details}")
        time.sleep(RETRY_DELAYS[attempt - 1])
    raise AssertionError("unreachable")


def latest_review(document_token: str) -> str:
    outline = fetch_scoped(
        document_token,
        [
            "--doc-format",
            "xml",
            "--scope",
            "outline",
            "--detail",
            "with-ids",
            "--max-depth",
            "3",
        ],
        "Review Log outline",
    )
    entries = re.findall(r'<h2 id="([^"]+)">(\d{4}-\d{2}-\d{2})</h2>', outline)
    if not entries:
        return "None recorded."
    block_id, _ = max(entries, key=lambda item: date.fromisoformat(item[1]))
    section = fetch_scoped(
        document_token,
        [
            "--doc-format",
            "markdown",
            "--scope",
            "section",
            "--start-block-id",
            block_id,
        ],
        "latest Review Log entry",
    ).strip()
    section = re.sub(r"^<fragment[^>]*>\s*", "", section)
    section = re.sub(r"\s*</fragment>\s*$", "", section)
    return section.strip()


def main() -> int:
    try:
        resources = resources_for_review()
        pages = resources["pages"]
        daily_plan = fetch_document(pages["Daily Plan"]["obj_token"], "Daily Plan")
        current_state = fetch_document(pages["Current State"]["obj_token"], "Current State")
        backlog = fetch_document(pages["Backlog"]["obj_token"], "Backlog")
        review = latest_review(pages["Review Log"]["obj_token"])
        print(
            "\n".join(
                [
                    "=== TODAY'S PLAN ===",
                    daily_plan.content.strip(),
                    "",
                    "=== CURRENT STATE ===",
                    current_state.content.strip(),
                    "",
                    "=== BACKLOG ===",
                    backlog.content.strip(),
                    "",
                    "=== PREVIOUS REVIEW ===",
                    review,
                ]
            )
        )
        return 0
    except (ContextError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
