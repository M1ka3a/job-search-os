#!/usr/bin/env python3
"""Read and schedule the small, human-maintainable preparation state file."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "prep-state.yaml"
STATUSES = ("gap", "weak", "ready")
STATUS_PRIORITY = {"gap": 0, "weak": 1, "ready": 2}


class PrepStateError(ValueError):
    """Invalid or unreadable preparation state."""


def scalar(value: str) -> Any:
    value = value.strip()
    if value in {"null", "~", ""}:
        return None
    if value.isdigit():
        return int(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_prep_state(path: Path = STATE_PATH) -> dict[str, Any]:
    """Parse the intentionally small YAML shape used by prep-state.yaml."""
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise PrepStateError(f"Unable to read preparation state: {path}") from exc

    state: dict[str, Any] = {"topics": []}
    current: dict[str, Any] | None = None
    in_topics = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "topics:":
            in_topics = True
            continue
        if stripped.startswith("- "):
            if not in_topics:
                raise PrepStateError("Preparation state has a list outside topics")
            current = {}
            state["topics"].append(current)
            assignment = stripped[2:]
            if ":" not in assignment:
                raise PrepStateError(f"Invalid topic entry: {stripped}")
            key, value = assignment.split(":", 1)
            current[key.strip()] = scalar(value)
            continue
        if ":" not in stripped:
            raise PrepStateError(f"Invalid preparation state line: {stripped}")
        key, value = stripped.split(":", 1)
        key = key.strip()
        parsed = scalar(value)
        if in_topics:
            if current is None:
                raise PrepStateError(f"Topic field has no topic: {stripped}")
            current[key] = parsed
        else:
            state[key] = parsed

    topics = state.get("topics")
    if not isinstance(topics, list):
        raise PrepStateError("Preparation state must contain topics")
    seen: set[str] = set()
    for topic in topics:
        if not isinstance(topic, dict):
            raise PrepStateError("Each preparation topic must be an object")
        for field in ("id", "name", "status", "next_review"):
            if field not in topic:
                raise PrepStateError(f"Preparation topic is missing {field}: {topic}")
        topic_id = topic["id"]
        if not isinstance(topic_id, str) or not topic_id or topic_id in seen:
            raise PrepStateError(f"Invalid or duplicate preparation topic id: {topic_id}")
        seen.add(topic_id)
        if topic["status"] not in STATUSES:
            raise PrepStateError(f"Invalid preparation status for {topic_id}: {topic['status']}")
        for field in ("last_reviewed", "next_review"):
            value = topic[field]
            if value is not None:
                try:
                    date.fromisoformat(str(value))
                except ValueError as exc:
                    raise PrepStateError(f"Invalid {field} for {topic_id}: {value}") from exc
    return state


def next_review_after_successful_review(successful_reviews: int, review_date: date) -> str | None:
    """Return D+2, D+7, D+14, then stable/periodic (no forced date)."""
    offsets = (2, 7, 14)
    if successful_reviews >= len(offsets):
        return None
    return (review_date + timedelta(days=offsets[successful_reviews])).isoformat()


def due_topics(state: dict[str, Any], on_date: date | None = None) -> list[dict[str, Any]]:
    today = on_date or date.today()
    due: list[dict[str, Any]] = []
    for topic in state["topics"]:
        next_review = topic.get("next_review")
        if next_review is None:
            # A ready topic with no scheduled date is stable/periodic, not
            # automatically due. Unreviewed gap/weak topics remain actionable.
            if topic.get("status") == "ready" or int(topic.get("successful_reviews", 0)) >= 3:
                continue
            due.append(topic)
        elif date.fromisoformat(str(next_review)) <= today:
            due.append(topic)
    return sorted(
        due,
        key=lambda topic: (
            STATUS_PRIORITY[str(topic["status"])],
            str(topic.get("next_review") or "0000-00-00"),
            str(topic["name"]),
        ),
    )


def format_due_topics(state: dict[str, Any], on_date: date | None = None) -> str:
    due = due_topics(state, on_date)
    if not due:
        return "None due."
    return "\n".join(
        f"- {topic['name']} — status: {topic['status']}; due: {topic.get('next_review') or 'now'}"
        for topic in due
    )
