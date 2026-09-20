#!/usr/bin/env python3
"""Build deterministic, compact context for the Morning Planner."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RESOLVER = ROOT / "scripts" / "resolve_lark_resources.py"
CACHE_DIR = ROOT / ".cache"
RESOURCE_CACHE = CACHE_DIR / "lark-resources.json"
CONTEXT_CACHE = CACHE_DIR / "planner-context.txt"
CONTEXT_METADATA = CACHE_DIR / "planner-context.json"
REQUIRED_PAGES = ("Current State", "Backlog", "Review Log")
MAX_FETCH_ATTEMPTS = 3
RETRY_DELAYS = (0.25, 0.5)


class ContextError(RuntimeError):
    """A clear, user-facing context preparation failure."""


class DocumentFetchError(ContextError):
    def __init__(self, message: str, *, transient: bool = False, invalid_resource: bool = False):
        super().__init__(message)
        self.transient = transient
        self.invalid_resource = invalid_resource


@dataclass
class Document:
    content: str
    revision_id: Any = None


def trace(message: str) -> None:
    if os.environ.get("PLANNER_TRACE") == "1":
        print(f"[planner-trace] {message}", file=sys.stderr)


def run_command(command: list[str]) -> str:
    trace("run: " + " ".join(command))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise ContextError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{details or 'no details'}"
        )
    return completed.stdout


def required_resources(resources: Any) -> bool:
    if not isinstance(resources, dict) or not isinstance(resources.get("pages"), dict):
        return False
    for title in REQUIRED_PAGES:
        resource = resources["pages"].get(title)
        if not isinstance(resource, dict):
            return False
        if not all(resource.get(field) for field in ("title", "node_token", "obj_token", "obj_type")):
            return False
    return True


def load_resource_cache() -> dict[str, Any] | None:
    if not RESOURCE_CACHE.is_file():
        trace("resource cache missing")
        return None
    try:
        resources = json.loads(RESOURCE_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        trace("resource cache unreadable")
        return None
    if not required_resources(resources):
        trace("resource cache missing required resources")
        return None
    trace("using cached resource tokens; resolver not invoked")
    return resources


def resolve_resources() -> dict[str, Any]:
    trace("resource cache unavailable; invoking resolver")
    output = run_command([sys.executable, str(RESOLVER)])
    try:
        resources = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ContextError(f"Resolver returned invalid JSON: {exc}") from exc
    if not required_resources(resources):
        raise ContextError(
            "Resolver did not return Current State, Backlog, and Review Log resources"
        )
    return resources


def classify_failure(details: str) -> tuple[bool, bool]:
    lowered = details.lower()
    invalid_resource = any(
        marker in lowered
        for marker in ("invalid document", "document not found", "not_found", "invalid_document")
    )
    explicit_non_transient = any(
        marker in lowered
        for marker in (
            "permission",
            "authorization",
            "authentication",
            "missing_scope",
            "unauthorized",
            "forbidden",
            "validation",
            "invalid_parameters",
            " 400",
            " 401",
            " 403",
            " 404",
        )
    )
    transient = not explicit_non_transient and any(
        marker in lowered
        for marker in (
            "timeout",
            "tls",
            "network",
            "connection",
            "temporar",
            " 429",
            " 502",
            " 503",
            " 504",
        )
    )
    return transient, invalid_resource


def fetch_document(document_token: str, title: str) -> Document:
    command = [
        "lark-cli",
        "docs",
        "+fetch",
        "--doc",
        document_token,
        "--as",
        "user",
        "--doc-format",
        "markdown",
        "--format",
        "json",
    ]
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        trace(f"fetch {title} attempt {attempt}/{MAX_FETCH_ATTEMPTS}")
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
                document = response["data"]["document"]
                return Document(document["content"], document.get("revision_id"))
            except (KeyError, TypeError) as exc:
                raise DocumentFetchError(
                    f"{title} response did not contain document content"
                ) from exc

        details = output.strip() or "no details"
        transient, invalid_resource = classify_failure(details)
        if not transient or attempt == MAX_FETCH_ATTEMPTS:
            raise DocumentFetchError(
                f"Unable to read {title}: {details}",
                transient=transient,
                invalid_resource=invalid_resource,
            )
        time.sleep(RETRY_DELAYS[attempt - 1])

    raise AssertionError("unreachable")


def heading_sections(text: str, level: int) -> dict[str, str]:
    """Return sections beginning at headings of exactly the requested level."""
    lines = text.splitlines()
    heading = re.compile(rf"^{'#' * level} ([^#].*)$")
    boundary = re.compile(rf"^#{{1,{level}}} ")
    found: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = heading.match(lines[index].strip())
        if not match:
            index += 1
            continue
        title = match.group(1).strip()
        end = index + 1
        while end < len(lines) and not boundary.match(lines[end].strip()):
            end += 1
        found[title] = "\n".join(lines[index + 1 : end]).strip()
        index = end
    return found


def nonempty(value: str | None) -> str:
    value = (value or "").strip()
    return value if value else "None recorded."


def review_section(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^## (\d{4}-\d{2}-\d{2})\s*$", text, re.MULTILINE))
    if not matches:
        return {
            "Date": "None recorded.",
            "Completed": "None recorded.",
            "Blocked": "None recorded.",
            "New Gaps": "None recorded.",
            "Carry Over": "None recorded.",
        }

    dated = [(date.fromisoformat(match.group(1)), match) for match in matches]
    _, latest = max(dated, key=lambda item: item[0])
    next_match = next((match for match in matches if match.start() > latest.start()), None)
    section_text = text[latest.end() : next_match.start() if next_match else None]
    sections = heading_sections(section_text, 3)
    return {
        "Date": latest.group(1),
        "Completed": nonempty(sections.get("Completed")),
        "Blocked": nonempty(sections.get("Blocked")),
        "New Gaps": nonempty(sections.get("New Gaps")),
        "Carry Over": nonempty(sections.get("Carry Over")),
    }


def build_context(documents: dict[str, Document]) -> str:
    overview = heading_sections(documents["Current State"].content, 1)
    backlog_sections = heading_sections(documents["Backlog"].content, 2)
    review = review_section(documents["Review Log"].content)

    return "\n".join(
        [
            "=== OVERVIEW ===",
            "",
            f"Goal:\n{nonempty(overview.get('Goal'))}",
            "",
            f"Strategy:\n{nonempty(overview.get('Career strategy'))}",
            "",
            f"Current Phase:\n{nonempty(overview.get('Current phase'))}",
            "",
            f"Known Strengths:\n{nonempty(overview.get('Current strengths'))}",
            "",
            f"Known Gaps:\n{nonempty(overview.get('Current known gaps'))}",
            "",
            f"Current Priorities:\n{nonempty(overview.get('Current focus'))}",
            "",
            "=== BACKLOG ===",
            "",
            f"Active:\n{nonempty(backlog_sections.get('Active'))}",
            "",
            f"Next:\n{nonempty(backlog_sections.get('Next'))}",
            "",
            f"Later:\n{nonempty(backlog_sections.get('Later'))}",
            "",
            "=== RECENT REVIEW ===",
            "",
            f"Date:\n{review['Date']}",
            "",
            f"Completed:\n{review['Completed']}",
            "",
            f"Blocked:\n{review['Blocked']}",
            "",
            f"New Gaps:\n{review['New Gaps']}",
            "",
            f"Carry Over:\n{review['Carry Over']}",
            "",
            "=== UPCOMING ===",
            "",
            "None recorded.",
        ]
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_context(context: str, documents: dict[str, Document]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_CACHE.write_text(context + "\n")
    metadata = {
        "generated_at": now_iso(),
        "source_freshness": {
            title: {"revision_id": document.revision_id}
            for title, document in documents.items()
        },
    }
    CONTEXT_METADATA.write_text(json.dumps(metadata, indent=2) + "\n")


def load_stale_context() -> str:
    if not CONTEXT_CACHE.is_file():
        raise ContextError("Live Lark reads failed and no previous planner context exists")
    context = CONTEXT_CACHE.read_text().rstrip()
    generated_at = "unknown"
    if CONTEXT_METADATA.is_file():
        try:
            metadata = json.loads(CONTEXT_METADATA.read_text())
            generated_at = str(metadata.get("generated_at", generated_at))
        except (OSError, json.JSONDecodeError):
            pass
    else:
        generated_at = datetime.fromtimestamp(
            CONTEXT_CACHE.stat().st_mtime, timezone.utc
        ).isoformat()
    return f"WARNING: Using stale planner context generated at {generated_at}.\n\n{context}"


def prepare_context() -> str:
    resources = load_resource_cache()
    if resources is None:
        resources = resolve_resources()

    def fetch_all(current_resources: dict[str, Any]) -> dict[str, Document]:
        pages = current_resources["pages"]
        return {
            title: fetch_document(pages[title]["obj_token"], title)
            for title in REQUIRED_PAGES
        }

    try:
        documents = fetch_all(resources)
    except DocumentFetchError as exc:
        if exc.invalid_resource:
            try:
                resources = resolve_resources()
                documents = fetch_all(resources)
            except (ContextError, DocumentFetchError) as refresh_error:
                trace(f"resource refresh failed: {refresh_error}")
                return load_stale_context()
        else:
            return load_stale_context()

    context = build_context(documents)
    save_context(context, documents)
    return context


def main() -> int:
    try:
        print(prepare_context())
        return 0
    except ContextError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
