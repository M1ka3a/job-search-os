#!/usr/bin/env python3
"""Resolve configured Job Search Lark wiki resources using lark-cli."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "lark.yaml"
CACHE_PATH = ROOT / ".cache" / "lark-resources.json"


class ResolverError(RuntimeError):
    """A clear, user-facing resolver failure."""


def load_config(path: Path) -> tuple[dict[str, str], list[str]]:
    """Load the small, fixed YAML shape used by config/lark.yaml.

    This intentionally supports only the simple mapping/list structure needed
    by this repo, so the resolver has no third-party dependency.
    """
    if not path.is_file():
        raise ResolverError(f"Config file not found: {path}")

    root: dict[str, str] = {}
    pages: list[str] = []
    section: str | None = None

    for line_number, raw_line in enumerate(path.read_text().splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "root:":
            section = "root"
            continue
        if line == "pages:":
            section = "pages"
            continue
        if section == "root" and ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            if key not in {"title", "url"} or not value:
                raise ResolverError(f"Invalid root config at line {line_number}")
            root[key] = value
            continue
        if section == "pages" and line.startswith("-"):
            page = line[1:].strip()
            if page:
                pages.append(page)
            continue
        raise ResolverError(f"Unsupported config syntax at line {line_number}: {raw_line}")

    if root.get("title") != "Job Search HQ" or not root.get("url"):
        raise ResolverError("Config must define root.title and root.url")
    if not pages:
        raise ResolverError("Config must define at least one page")
    return root, pages


def run_lark(*args: str) -> dict[str, Any]:
    command = ["lark-cli", *args, "--as", "user", "--format", "json"]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise ResolverError(
            f"Lark CLI failed ({completed.returncode}): {details or 'no details'}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ResolverError(f"Lark CLI returned invalid JSON: {exc}") from exc
    if response.get("ok") is not True:
        error = response.get("error", response)
        raise ResolverError(f"Lark CLI returned an error: {json.dumps(error)}")
    return response


def resource_from_node(node: dict[str, Any]) -> dict[str, str]:
    fields = ("title", "node_token", "obj_token", "obj_type")
    missing = [field for field in fields if not node.get(field)]
    if missing:
        raise ResolverError(
            f"Lark node response is missing required fields: {', '.join(missing)}"
        )
    return {field: str(node[field]) for field in fields}


def resolve(root_config: dict[str, str], page_titles: list[str]) -> dict[str, Any]:
    root_response = run_lark("wiki", "+node-get", "--node-token", root_config["url"])
    root_resource = resource_from_node(root_response["data"])
    if root_resource["title"] != root_config["title"]:
        raise ResolverError(
            f"Root title mismatch: expected {root_config['title']!r}, "
            f"found {root_resource['title']!r}"
        )

    children_response = run_lark(
        "wiki",
        "+node-list",
        "--space-id",
        root_response["data"]["space_id"],
        "--parent-node-token",
        root_resource["node_token"],
        "--page-all",
    )
    children = children_response["data"].get("nodes", [])
    by_title = {child.get("title"): child for child in children}

    resolved_pages: dict[str, dict[str, str]] = {}
    for title in page_titles:
        child = by_title.get(title)
        if child is None:
            available = ", ".join(sorted(title for title in by_title if title))
            raise ResolverError(
                f"Configured page not found under {root_resource['title']!r}: {title!r}. "
                f"Available child pages: {available or '(none)'}"
            )
        resolved_pages[title] = resource_from_node(child)

    return {
        "root": {"title": root_resource["title"], "url": root_config["url"], **root_resource},
        "pages": resolved_pages,
    }


def main() -> int:
    requested_title = sys.argv[1] if len(sys.argv) == 2 else None
    if len(sys.argv) > 2:
        print("Usage: python scripts/resolve_lark_resources.py [PAGE TITLE]", file=sys.stderr)
        return 2

    try:
        root_config, page_titles = load_config(CONFIG_PATH)
        if requested_title and requested_title not in page_titles:
            raise ResolverError(
                f"Page {requested_title!r} is not configured in {CONFIG_PATH.relative_to(ROOT)}"
            )
        resource_map = resolve(root_config, page_titles)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(resource_map, indent=2) + "\n")
        if requested_title:
            print(json.dumps(resource_map["pages"][requested_title], indent=2))
        else:
            print(json.dumps(resource_map, indent=2))
        return 0
    except ResolverError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
