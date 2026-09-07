#!/usr/bin/env python3
"""Answer the two learner-facing exam-route questions.

Examples (PowerShell):

    python scripts/query_exam_routes.py --source-id exam-e36... --question-number 7
    python scripts/query_exam_routes.py --question-id exam-e36...:p1:q7:r1 \
        --completed-cycle 1.1-cycle-1 --completed-course space_vector_ops

The command never derives completion from a course merely being present in a
manifest.  A cycle/course must be explicitly recorded in a progress JSON or
event log, so "听过" and "已经掌握" cannot be confused with route eligibility.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "exam_papers" / "manifest.json"

COMPLETE_STATUSES = {
    "complete", "completed", "done", "finished", "listened", "consumed",
    "passed", "full_pass", "full-pass", "verified", "confirmed",
}
COMPLETION_KEYS = {
    "completed_cycle_ids", "completed_cycles", "finished_cycle_ids",
    "completed_course_keys", "completed_courses", "listened_course_keys",
    "consumed_course_keys", "finished_course_keys",
}
VISUAL_VERIFIED_VALUES = {
    "verified", "visually_verified", "vision_verified", "source_page_verified",
    "page_verified", "approved", "passed", "ready",
}
VISUAL_BLOCKED_VALUES = {"blocked", "unavailable", "failed", "invalid", "rejected"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def is_complete_status(value: Any) -> bool:
    return str(value or "").strip().casefold().replace(" ", "_") in COMPLETE_STATUSES


def visual_review_status(value: Any) -> str:
    """Normalize review labels while treating missing/unknown as pending."""

    if value is True:
        return "verified"
    if value is False or value is None:
        return "pending"
    text = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if text in VISUAL_VERIFIED_VALUES:
        return "verified"
    if text in VISUAL_BLOCKED_VALUES or any(token in text for token in ("block", "fail", "invalid", "unavailable")):
        return "blocked"
    if text in {"not_applicable", "n_a", "na"}:
        return "not_applicable"
    return "pending"


def route_visual_gate(route: dict[str, Any]) -> dict[str, Any]:
    """Read the explicit source-page gate emitted by the route builder.

    Legacy manifests have no visual-review fields and remain query-compatible;
    newly generated routes carry these fields and are evaluated conservatively.
    """

    evidence_rows = route.get("source_page_evidence") if isinstance(route.get("source_page_evidence"), list) else []
    evidence_statuses = [
        visual_review_status(item.get("visual_review_status", item.get("visual_status")))
        for item in evidence_rows
        if isinstance(item, dict) and ("visual_review_status" in item or "visual_status" in item)
    ]
    explicit = any(key in route for key in ("visual_review_status", "visual_review_pages", "visual_review_evidence")) or bool(evidence_statuses)
    if not explicit:
        return {"declared": False, "status": "unknown", "pending_pages": [], "blocked_pages": [], "verified_pages": [], "missing_source_pages": False}
    status = visual_review_status(route.get("visual_review_status")) if "visual_review_status" in route else (
        "blocked" if "blocked" in evidence_statuses
        else "verified" if evidence_statuses and all(item == "verified" for item in evidence_statuses)
        else "pending"
    )
    pages = route.get("visual_review_pages") if isinstance(route.get("visual_review_pages"), dict) else {}
    pending = [int(item) for item in pages.get("pending", []) or [] if str(item).isdigit()]
    blocked = [int(item) for item in pages.get("blocked", []) or [] if str(item).isdigit()]
    verified = [int(item) for item in pages.get("verified", []) or [] if str(item).isdigit()]
    if not pages and evidence_rows:
        for item in evidence_rows:
            if not isinstance(item, dict):
                continue
            try:
                number = int(item.get("pdf_page", item.get("page")))
            except (TypeError, ValueError):
                continue
            state = visual_review_status(item.get("visual_review_status", item.get("visual_status")))
            if state == "verified":
                verified.append(number)
            elif state == "blocked":
                blocked.append(number)
            else:
                pending.append(number)
        pending = list(dict.fromkeys(pending))
        blocked = list(dict.fromkeys(blocked))
        verified = list(dict.fromkeys(verified))
    if status == "pending" and not pending:
        pending = [int(item) for item in pages.get("required", []) or [] if str(item).isdigit()]
    missing_source_pages = status == "verified" and not pages and not evidence_rows
    if missing_source_pages:
        status = "pending"
    return {
        "declared": True,
        "status": status,
        "pending_pages": pending,
        "blocked_pages": blocked,
        "verified_pages": verified,
        "missing_source_pages": missing_source_pages,
    }


def add_values(target: set[str], value: Any) -> None:
    for item in as_list(value):
        if isinstance(item, dict):
            item = item.get("id", item.get("cycle_id", item.get("course_key", item.get("course_id"))))
        if item is not None and str(item).strip():
            target.add(str(item).strip())


def collect_progress_value(value: Any, cycles: set[str], courses: set[str], *, key: str = "") -> None:
    """Collect only explicit completion claims from flexible local snapshots."""
    if isinstance(value, dict):
        lowered = key.casefold()
        if lowered in COMPLETION_KEYS:
            if "course" in lowered:
                add_values(courses, value)
            else:
                add_values(cycles, value)
        # Explicit cycle/course records may use {id, status} or
        # {cycle_id/course_key, completion_status}.
        identifier = value.get("cycle_id") or value.get("course_key") or value.get("id")
        status = value.get("status", value.get("completion_status", value.get("state")))
        if identifier and is_complete_status(status):
            identifier_text = str(identifier)
            if "course" in lowered or "course_key" in value:
                courses.add(identifier_text)
            else:
                cycles.add(identifier_text)
        for child_key, child in value.items():
            collect_progress_value(child, cycles, courses, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            collect_progress_value(child, cycles, courses, key=key)


def load_completion(paths: Iterable[Path]) -> tuple[set[str], set[str], list[str]]:
    cycles: set[str] = set()
    courses: set[str] = set()
    warnings: list[str] = []
    for path in paths:
        if not path.is_file():
            warnings.append(f"progress_not_found:{path}")
            continue
        try:
            if path.suffix.casefold() == ".jsonl":
                for line in path.read_text(encoding="utf-8-sig").splitlines():
                    if line.strip():
                        collect_progress_value(json.loads(line), cycles, courses)
            else:
                collect_progress_value(load_json(path), cycles, courses)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            warnings.append(f"progress_unreadable:{path}:{type(error).__name__}")
    return cycles, courses, warnings


def find_route(payload: dict[str, Any], question_id: str | None, source_id: str | None, question_number: int | None) -> dict[str, Any] | None:
    routes = payload.get("routes", [])
    if isinstance(routes, dict):
        routes = list(routes.values())
    for route in routes:
        if not isinstance(route, dict):
            continue
        if question_id and str(route.get("question_id") or route.get("route_id")) == question_id:
            return route
        if source_id and str(route.get("source_id")) == source_id and question_number is not None and int(route.get("question_number", -1)) == question_number:
            return route
    return None


def curriculum_titles() -> tuple[dict[str, str], dict[str, str]]:
    cycle_titles: dict[str, str] = {}
    course_titles: dict[str, str] = {}
    for chapter in range(1, 6):
        path = ROOT / f"chapter{chapter}_manifest.json"
        if not path.is_file():
            continue
        payload = load_json(path)
        for section in payload.get("sections", []):
            for cycle in section.get("learning_cycles", []):
                cycle_id = str(cycle.get("id") or cycle.get("cycle_id") or "")
                if cycle_id:
                    cycle_titles[cycle_id] = str(cycle.get("title") or cycle_id)
                for field in ("course_keys", "prerequisite_course_keys", "optional_course_keys"):
                    for key in cycle.get(field, []) or []:
                        course_titles.setdefault(str(key), str(key))
        collections: list[Any] = []
        if isinstance(payload.get("courses"), dict):
            collections.extend(item for value in payload["courses"].values() for item in (value if isinstance(value, list) else [value]))
        for key in ("course_inventory", "course_catalog"):
            value = payload.get(key)
            if isinstance(value, list):
                collections.extend(value)
            elif isinstance(value, dict):
                collections.extend(item for row in value.values() for item in (row if isinstance(row, list) else [row]))
        for row in collections:
            if isinstance(row, dict) and row.get("course_key"):
                course_titles[str(row["course_key"])] = str(row.get("title") or row.get("course_key"))
    catalog = ROOT / "data" / "all_chapters_course_catalog.json"
    if catalog.is_file():
        try:
            for row in load_json(catalog).get("courses", []):
                if isinstance(row, dict) and row.get("course_key"):
                    course_titles[str(row["course_key"])] = str(row.get("title") or row["course_key"])
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    transcript_root = ROOT / "data" / "course_transcripts"
    by_course_id: dict[str, str] = {}
    if transcript_root.is_dir():
        for path in transcript_root.glob("*.json"):
            match = re.match(r"^(\d+(?:\.\d+){1,5}(?:\.[a-z])?)\s+(.+)\.json$", path.name, re.I)
            if match:
                by_course_id[match.group(1)] = match.group(2)
    if catalog.is_file():
        try:
            for row in load_json(catalog).get("courses", []):
                if isinstance(row, dict) and row.get("course_key"):
                    course_id = str(row.get("course_id") or "")
                    current = course_titles.get(str(row["course_key"]), "")
                    if course_id in by_course_id and (not current or current == str(row["course_key"]) or current == course_id):
                        course_titles[str(row["course_key"])] = by_course_id[course_id]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return cycle_titles, course_titles


def query(route: dict[str, Any], completed_cycles: set[str], completed_courses: set[str], warnings: list[str]) -> dict[str, Any]:
    required_cycles = [str(item) for item in route.get("required_cycle_ids", []) or []]
    required_courses = []
    for item in route.get("required_course_keys", []) or []:
        if isinstance(item, dict):
            item = item.get("course_key", item.get("id"))
        if item is not None and str(item) not in required_courses:
            required_courses.append(str(item))
    cycle_titles, course_titles = curriculum_titles()
    missing_cycles = [item for item in required_cycles if item not in completed_cycles]
    missing_courses = [item for item in required_courses if item not in completed_courses]
    base_blocked = bool(route.get("blocked")) or str(route.get("route_status")) == "blocked" or route.get("active", True) is False
    review = bool(route.get("needs_review")) or str(route.get("route_status")) == "needs_review" or str(route.get("mapping_status")) not in {"semantically_verified"}
    visual_gate = route_visual_gate(route)
    visual_review_blocked = visual_gate.get("declared") and visual_gate.get("status") in {"pending", "blocked", "not_applicable"}
    if visual_review_blocked:
        review = True
    if base_blocked:
        decision = "原卷已移除/替换或当前范围外，暂不可做" if route.get("active", True) is False else "当前范围外，暂不可做"
        ready = False
    elif review:
        decision = (
            "先复核原卷页视觉证据，暂不自动解锁"
            if visual_review_blocked and not base_blocked
            else "先复核原卷题面/映射，暂不自动解锁"
        )
        ready = False
    elif missing_cycles or missing_courses:
        decision = "前置未完成，暂不可做"
        ready = False
    else:
        decision = "可以开始做（可选试卷题）"
        ready = True
    # Keep the next actions in the route's curriculum order and do not expose
    # answer text.
    next_actions = []
    for item in route.get("recommended_path", []) or []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("cycle_id") or "")
        if cid in missing_cycles:
            next_actions.append({
                "order": item.get("order"),
                "cycle_id": cid,
                "cycle_title": item.get("cycle_title") or cycle_titles.get(cid, cid),
                "course_keys": [str(value) for value in item.get("course_keys", []) or []],
                "action": item.get("action") or "听课 -> 完成一本通循环 -> 独立自检",
            })
    if not next_actions:
        # A course-only prerequisite may not have a direct cycle row.
        next_actions = [{"course_key": key, "course_title": course_titles.get(key, key), "action": "先完成课程消费记录"} for key in missing_courses]
    return {
        "ok": True,
        "route_id": route.get("route_id"),
        "question_id": route.get("question_id"),
        "question_ref": route.get("question_ref"),
        "question_number": route.get("question_number"),
        "topic_summary": route.get("topic_summary"),
        "decision": decision,
        "ready": ready,
        "mapping_status": route.get("mapping_status"),
        "route_status": route.get("route_status"),
        "required_section_ids": route.get("required_section_ids", []),
        "required_cycles": [{"cycle_id": item, "title": cycle_titles.get(item, item), "completed": item in completed_cycles} for item in required_cycles],
        "required_courses": [{"course_key": item, "title": course_titles.get(item, item), "completed": item in completed_courses} for item in required_courses],
        "missing_cycles": missing_cycles,
        "missing_courses": missing_courses,
        "next_actions": next_actions,
        "uncertainties": route.get("uncertainties", []),
        "blockers": route.get("blockers", []),
        "external_prerequisites": route.get("external_prerequisites", []),
        "source_page_evidence": route.get("source_page_evidence", []),
        "visual_review": visual_gate,
        "progress_warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--question-id")
    parser.add_argument("--source-id")
    parser.add_argument("--question-number", type=int)
    parser.add_argument("--progress", type=Path, action="append", default=[])
    parser.add_argument("--completed-cycle", action="append", default=[])
    parser.add_argument("--completed-course", action="append", default=[])
    parser.add_argument("--json", action="store_true", help="保留机器可读 JSON；默认同样输出 JSON 以便网页调用")
    args = parser.parse_args()
    payload = load_json(args.manifest)
    route = find_route(payload, args.question_id, args.source_id, args.question_number)
    if route is None:
        print(json.dumps({"ok": False, "error": "route_not_found"}, ensure_ascii=False, indent=2))
        return 1
    completed_cycles, completed_courses, warnings = load_completion(args.progress)
    completed_cycles.update(str(item) for item in args.completed_cycle)
    completed_courses.update(str(item) for item in args.completed_course)
    print(json.dumps(query(route, completed_cycles, completed_courses, warnings), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
