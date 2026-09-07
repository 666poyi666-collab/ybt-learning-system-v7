#!/usr/bin/env python3
"""Validate optional exam routes against source and learning prerequisites.

The validator is intentionally stricter than a JSON schema check.  A route is
only ``ready`` when its original source, source hash, page authority, and all
learning prerequisites are present.  Candidate routes may be retained for
later semantic review, but they cannot claim to be unlocked.  In particular,
an answer/analysis page can never be accepted as a question stem.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable


HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
PAGE_RE = re.compile(r"(?:第\s*)?(\d+)\s*(?:页|p(?:age)?\s*)", re.IGNORECASE)
QUESTION_RE = re.compile(r"(?:第\s*)?(\d+)\s*(?:题|問|q(?:uestion)?\s*)", re.IGNORECASE)
MAPPING_STATUSES = {"candidate", "visually_verified", "semantically_verified", "needs_review", "blocked"}
ROUTE_STATUSES = {"ready", "candidate", "needs_review", "blocked"}
UNLOCK_GRANULARITIES = {"after_cycle", "after_multiple_cycles", "after_section", "after_multiple_sections", "after_chapter"}
ROUTE_STATES = {
    "ready_for_optional_unlock", "needs_review", "blocked_external_prerequisite",
    "blocked_visual_evidence", "stale_source_mapping", "source_removed_or_replaced",
    "retired", "candidate", "ready", "blocked", "",
}
QUESTION_ROLES = {"question", "question_paper", "stem", "question_stem", "original", "原卷", "题面"}
ANSWER_ROLES = {"answer", "answer_only", "analysis", "solution", "答案", "解析"}
VISUAL_VERIFIED_VALUES = {
    "verified", "visually_verified", "vision_verified", "source_page_verified",
    "page_verified", "approved", "passed", "ready",
}
VISUAL_BLOCKED_VALUES = {"blocked", "unavailable", "failed", "invalid", "rejected"}
VISUAL_REVIEW_VALUES = {"pending", "blocked", "verified", "not_applicable"}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _id_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = item.get("course_key", item.get("course_id", item.get("section_id", item.get("cycle_id", item.get("id")))))
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _route_ids(route: dict[str, Any], plural: str, singular: str, aliases: Iterable[str] = ()) -> list[str]:
    values: list[str] = []
    for key in (plural, singular, *aliases):
        values.extend(_id_list(route.get(key)))
    return list(dict.fromkeys(values))


def _normalise_role(value: Any) -> str:
    return str(value or "unknown").strip().casefold().replace("-", "_").replace(" ", "_")


def _page_number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _is_question_authority(value: Any) -> bool:
    """Accept the two serialized authority forms used by current indexes."""

    return value is True or str(value or "").strip().casefold() in {
        "original_question_page", "question_page", "source_page", "原卷题面",
    }


def _visual_review_status(value: Any) -> str:
    """Normalize visual-review labels; unknown/missing values stay pending."""

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


def _page_visual_review_status(page: dict[str, Any]) -> str:
    # Prefer the canonical field, but accept legacy visual_status labels.
    if "visual_review_status" in page:
        return _visual_review_status(page.get("visual_review_status"))
    return _visual_review_status(page.get("visual_status"))


def _find_source(source_map: dict[str, dict[str, Any]], source_ref: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve canonical, full-hash, and compact source identities."""

    if source_ref in source_map:
        return source_ref, source_map[source_ref]
    lowered = source_ref.casefold()
    for canonical, source in source_map.items():
        aliases = {
            str(source.get("source_id") or ""),
            str(source.get("stable_source_id") or ""),
            str(source.get("sha256") or source.get("source_sha256") or source.get("source_pdf_sha256") or ""),
        }
        if any(alias and alias.casefold() == lowered for alias in aliases):
            return canonical, source
    return None


def _load_learning_ids(project_root: Path, course_catalog: Path | None) -> tuple[set[str], set[str], set[str], dict[str, str]]:
    """Collect known course/section/cycle identities from repository manifests."""

    course_keys: set[str] = set()
    section_ids: set[str] = set()
    cycle_ids: set[str] = set()
    cycle_sections: dict[str, str] = {}
    catalog_path = course_catalog or (project_root / "data" / "all_chapters_course_catalog.json")
    if catalog_path.is_file():
        try:
            catalog = _read_json(catalog_path)
            for row in catalog.get("courses", []):
                if isinstance(row, dict) and row.get("course_key"):
                    course_keys.add(str(row["course_key"]))
        except (OSError, ValueError, json.JSONDecodeError):
            # A malformed optional catalog is reported by callers only when a
            # route actually references an unknown course.
            pass
    for chapter in range(1, 6):
        manifest_path = project_root / f"chapter{chapter}_manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = _read_json(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for section in manifest.get("sections", []):
            if not isinstance(section, dict):
                continue
            section_id = str(section.get("id") or section.get("section_key") or "")
            if section_id:
                section_ids.add(section_id)
            for key in ("required_course_keys", "support_course_keys", "course_keys", "prerequisite_course_keys", "optional_course_keys"):
                course_keys.update(_id_list(section.get(key)))
            for cycle in section.get("learning_cycles", []):
                if not isinstance(cycle, dict):
                    continue
                cycle_id = str(cycle.get("id") or cycle.get("cycle_id") or "")
                if not cycle_id:
                    continue
                cycle_ids.add(cycle_id)
                if section_id:
                    cycle_sections[cycle_id] = section_id
                for key in ("course_keys", "prerequisite_course_keys", "optional_course_keys"):
                    course_keys.update(_id_list(cycle.get(key)))
    return course_keys, section_ids, cycle_ids, cycle_sections


def _source_records(payload: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str], list[str], dict[str, dict[int, dict[str, Any]]]]:
    errors: list[str] = []
    warnings: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    by_hash: dict[str, str] = {}
    pages_by_source: dict[str, dict[int, dict[str, Any]]] = {}
    sources = payload.get("sources", [])
    if isinstance(sources, dict):
        sources = list(sources.values())
    if not isinstance(sources, list):
        return {}, ["sources:must_be_list"], warnings, pages_by_source
    for index, raw in enumerate(sources):
        if not isinstance(raw, dict):
            errors.append(f"source[{index}]:must_be_object")
            continue
        source_id = str(raw.get("source_id") or raw.get("id") or "")
        if not source_id:
            errors.append(f"source[{index}]:missing_source_id")
            continue
        if source_id in by_id:
            errors.append(f"{source_id}:duplicate_source_id")
            continue
        source_hash = str(raw.get("sha256") or raw.get("source_sha256") or raw.get("source_pdf_sha256") or "").lower()
        if not source_hash:
            errors.append(f"{source_id}:missing_source_sha256")
        elif not HEX64_RE.fullmatch(source_hash):
            errors.append(f"{source_id}:invalid_source_sha256")
        elif source_hash in by_hash:
            errors.append(f"{source_id}:duplicate_source_sha256:{by_hash[source_hash]}")
        else:
            by_hash[source_hash] = source_id
        role = _normalise_role(raw.get("source_role"))
        authority = raw.get("question_authority")
        if role in ANSWER_ROLES or authority is False:
            if role in ANSWER_ROLES and authority is True:
                errors.append(f"{source_id}:answer_source_cannot_be_question_authority")
        elif role not in QUESTION_ROLES and authority is not True:
            warnings.append(f"{source_id}:source_role_unclassified")
        page_count = raw.get("page_count")
        if page_count is not None:
            try:
                if int(page_count) < 1:
                    errors.append(f"{source_id}:invalid_page_count")
            except (TypeError, ValueError):
                errors.append(f"{source_id}:invalid_page_count")
        by_id[source_id] = raw
        page_map: dict[int, dict[str, Any]] = {}
        pages = raw.get("pages", [])
        if isinstance(pages, dict):
            pages = list(pages.values())
        if not isinstance(pages, list):
            errors.append(f"{source_id}:pages_must_be_list")
            pages = []
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                errors.append(f"{source_id}:page[{page_index}]:must_be_object")
                continue
            try:
                page_number = int(page.get("pdf_page", page.get("page")))
            except (TypeError, ValueError):
                errors.append(f"{source_id}:page[{page_index}]:invalid_pdf_page")
                continue
            if page_number < 1:
                errors.append(f"{source_id}:page:{page_number}:invalid_pdf_page")
            if page_number in page_map:
                errors.append(f"{source_id}:page:{page_number}:duplicate_page")
            if page_count is not None:
                try:
                    if page_number > int(page_count):
                        errors.append(f"{source_id}:page:{page_number}:outside_page_count")
                except (TypeError, ValueError):
                    pass
            page_hash = str(page.get("page_image_sha256") or page.get("source_page_sha256") or "").lower()
            if page_hash and not HEX64_RE.fullmatch(page_hash):
                errors.append(f"{source_id}:page:{page_number}:invalid_page_image_sha256")
            page_role = _normalise_role(page.get("page_role", page.get("role")))
            page_authority = page.get("question_authority")
            if page_role in ANSWER_ROLES and _is_question_authority(page_authority):
                errors.append(f"{source_id}:page:{page_number}:answer_page_cannot_be_question_authority")
            if page_role in QUESTION_ROLES and page_authority is False:
                errors.append(f"{source_id}:page:{page_number}:question_page_authority_false")
            visual_status = _page_visual_review_status(page) if page_role in QUESTION_ROLES else "not_applicable"
            if page_role in QUESTION_ROLES and visual_status == "verified" and not HEX64_RE.fullmatch(page_hash):
                errors.append(f"{source_id}:page:{page_number}:visual_review_verified_requires_image_hash")
            if page_role in ANSWER_ROLES and _visual_review_status(page.get("visual_review_status", page.get("visual_status"))) == "verified":
                errors.append(f"{source_id}:page:{page_number}:answer_page_cannot_be_visual_verified")
            page_map[page_number] = page
        pages_by_source[source_id] = page_map
    return by_id, errors, warnings, pages_by_source


def _route_status(route: dict[str, Any], mapping_status: str) -> str:
    if route.get("blocked") is True:
        return "blocked"
    if route.get("needs_review") is True:
        return "needs_review"
    explicit = str(route.get("route_status") or route.get("status") or "").strip().casefold()
    if explicit:
        return explicit
    if mapping_status in {"blocked", "needs_review", "candidate"}:
        return mapping_status if mapping_status != "candidate" else "candidate"
    return "ready"


def _parse_ref_number(value: Any, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(str(value or ""))
    return int(match.group(1)) if match else None


def _validate_route(
    route: dict[str, Any],
    index: int,
    source_map: dict[str, dict[str, Any]],
    pages_by_source: dict[str, dict[int, dict[str, Any]]],
    course_keys: set[str],
    section_ids: set[str],
    cycle_ids: set[str],
    cycle_sections: dict[str, str],
    strict_visual: bool = False,
    visual_gate_enabled: bool = False,
) -> tuple[list[str], list[str], str]:
    errors: list[str] = []
    warnings: list[str] = []
    route_id = str(route.get("route_id") or route.get("id") or f"route[{index}]")
    optional = route.get("optional")
    if optional is not True:
        errors.append(f"{route_id}:exam_route_must_be_optional")
    if route.get("blocks_ybt_progress") is not False:
        errors.append(f"{route_id}:exam_route_must_be_non_blocking")
    mapping_status = str(route.get("mapping_status") or "").strip().casefold()
    if mapping_status not in MAPPING_STATUSES:
        errors.append(f"{route_id}:invalid_mapping_status")
        mapping_status = "candidate"
    status = _route_status(route, mapping_status)
    if status not in ROUTE_STATUSES:
        errors.append(f"{route_id}:invalid_route_status")
        status = "needs_review"
    route_state = str(route.get("route_state") or "").strip().casefold()
    unlock_status = str(route.get("unlock_status") or "").strip().casefold()
    if route_state not in ROUTE_STATES:
        errors.append(f"{route_id}:invalid_route_state")
    visual_gate_declared = any(
        key in route for key in ("visual_review_status", "visual_review_pages", "visual_review_evidence")
    )
    source_ref = route.get("source") if isinstance(route.get("source"), dict) else {}
    source_id = str(route.get("source_id") or source_ref.get("source_id") or "")
    canonical_source_id = source_id
    if not source_id:
        errors.append(f"{route_id}:missing_source_id")
        source = None
    else:
        resolved_source = _find_source(source_map, source_id)
        source = resolved_source[1] if resolved_source else None
        canonical_source_id = resolved_source[0] if resolved_source else source_id
        if source is None:
            errors.append(f"{route_id}:unknown_source:{source_id}")
        elif source.get("included_for_routes") is False:
            if status == "ready" or mapping_status in {"visually_verified", "semantically_verified"}:
                errors.append(f"{route_id}:source_not_allowlisted_for_routes")
            else:
                warnings.append(f"{route_id}:source_not_allowlisted_pending")
    page_number = route.get("pdf_page", route.get("page"))
    if page_number is None:
        page_number = _parse_ref_number(route.get("question_ref"), PAGE_RE)
    try:
        page_number = int(page_number) if page_number is not None else None
    except (TypeError, ValueError):
        page_number = None
    page_evidence = route.get("source_page_evidence")
    if not isinstance(page_evidence, list):
        page_evidence = []
    evidence_for_page = next(
        (item for item in page_evidence if isinstance(item, dict) and _page_number(item.get("pdf_page", item.get("page"))) == page_number),
        None,
    )
    route_source_hash = source_ref.get("sha256") or source_ref.get("source_sha256") or source_ref.get("source_pdf_sha256")
    route_hash = str(
        route.get("source_sha256")
        or route.get("source_pdf_sha256")
        or route_source_hash
        or (evidence_for_page or {}).get("source_pdf_sha256")
        or ""
    ).lower()
    if not route_hash:
        if status == "ready" or mapping_status == "semantically_verified":
            errors.append(f"{route_id}:missing_source_sha256")
        else:
            warnings.append(f"{route_id}:source_sha256_pending")
    elif not HEX64_RE.fullmatch(route_hash):
        errors.append(f"{route_id}:invalid_source_sha256")
    elif source is not None:
        source_hash = str(source.get("sha256") or source.get("source_sha256") or source.get("source_pdf_sha256") or "").lower()
        if source_hash and route_hash != source_hash:
            errors.append(f"{route_id}:source_sha256_mismatch")
    if page_number is None:
        if status == "ready" or mapping_status == "semantically_verified":
            errors.append(f"{route_id}:missing_pdf_page")
        else:
            warnings.append(f"{route_id}:pdf_page_pending")
    page = pages_by_source.get(canonical_source_id, {}).get(page_number) if source_id and page_number else None
    if page is None and evidence_for_page is not None:
        page = evidence_for_page
    if page is None and page_number is not None:
        if status == "ready" or mapping_status == "semantically_verified":
            errors.append(f"{route_id}:unknown_source_page:{page_number}")
        else:
            warnings.append(f"{route_id}:source_page_pending:{page_number}")
    if page is not None:
        role = _normalise_role(page.get("page_role", page.get("role")))
        raw_role = page.get("page_role", page.get("role"))
        role_is_question = raw_role is None or role in QUESTION_ROLES
        authority = _is_question_authority(page.get("question_authority")) and role_is_question
        if role in ANSWER_ROLES or not authority:
            if status in {"ready"} or mapping_status in {"visually_verified", "semantically_verified"}:
                errors.append(f"{route_id}:source_page_not_question_authority")
            else:
                warnings.append(f"{route_id}:source_page_needs_visual_review")
        page_hash = str(page.get("page_image_sha256") or page.get("source_page_sha256") or "").lower()
        route_page_hash = str(
            route.get("source_page_sha256")
            or route.get("page_image_sha256")
            or (evidence_for_page or {}).get("source_page_sha256")
            or (evidence_for_page or {}).get("page_image_sha256")
            or ""
        ).lower()
        if status == "ready" and not HEX64_RE.fullmatch(page_hash):
            errors.append(f"{route_id}:missing_source_page_sha256")
        if route_page_hash and page_hash and route_page_hash != page_hash:
            errors.append(f"{route_id}:source_page_sha256_mismatch")

    # Evaluate every page used by a multi-page question.  Checking only the
    # first page allowed a continuation page with an unreviewed diagram to be
    # treated as ready.
    route_page_numbers: list[int] = []
    for value in _as_list(route.get("pdf_pages") or route.get("pages") or page_number):
        number = _page_number(value)
        if number is not None and number not in route_page_numbers:
            route_page_numbers.append(number)
    visual_page_rows: list[tuple[int, dict[str, Any] | None, str]] = []
    for number in route_page_numbers:
        page_row = pages_by_source.get(canonical_source_id, {}).get(number) if canonical_source_id else None
        if page_row is None:
            page_row = next(
                (item for item in page_evidence if isinstance(item, dict) and _page_number(item.get("pdf_page", item.get("page"))) == number),
                None,
            )
        visual_page_rows.append((number, page_row, _page_visual_review_status(page_row) if isinstance(page_row, dict) else "pending"))
    visual_pending_pages = [number for number, _, state in visual_page_rows if state == "pending"]
    visual_blocked_pages = [number for number, _, state in visual_page_rows if state == "blocked"]
    visual_verified_pages = [number for number, _, state in visual_page_rows if state == "verified"]
    route_visual_status = _visual_review_status(route.get("visual_review_status")) if "visual_review_status" in route else None
    active_route = route.get("active", True) is not False
    route_state_requires_visual_gate = active_route and (visual_gate_enabled or strict_visual or visual_gate_declared)
    if route_state_requires_visual_gate:
        advertises_ready = route_state == "ready_for_optional_unlock" or status == "ready" or unlock_status == "unlocked"
        if advertises_ready:
            if not route_page_numbers:
                errors.append(f"{route_id}:visual_review_requires_source_pages")
            if visual_pending_pages:
                errors.append(f"{route_id}:visual_review_pending:" + ",".join(map(str, visual_pending_pages)))
            if visual_blocked_pages:
                errors.append(f"{route_id}:visual_review_blocked:" + ",".join(map(str, visual_blocked_pages)))
            if route_visual_status != "verified":
                errors.append(f"{route_id}:ready_route_requires_visual_review_status_verified")
            if route_page_numbers and len(visual_verified_pages) != len(route_page_numbers):
                errors.append(f"{route_id}:all_source_pages_must_be_visually_verified")
        elif unlock_status == "unlocked":
            errors.append(f"{route_id}:locked_review_route_cannot_be_unlocked")
    elif active_route and route_visual_status in {"blocked", "pending"} and status in {"ready"}:
        # Legacy routes without route_state are not failed by default, but a
        # newly added explicit pending field must never advertise ``ready``.
        errors.append(f"{route_id}:explicit_visual_review_pending_route_cannot_be_ready")
    question_ref = str(route.get("question_ref") or "").strip()
    if not question_ref:
        errors.append(f"{route_id}:missing_question_ref")
    question_number = route.get("question_number")
    if question_number is None:
        question_number = _parse_ref_number(question_ref, QUESTION_RE)
    try:
        question_number = int(question_number) if question_number is not None else None
    except (TypeError, ValueError):
        question_number = None
    if question_number is None:
        warnings.append(f"{route_id}:question_number_pending")
    question_id = str(route.get("question_id") or "").strip()
    if question_id and source is not None:
        canonical_id = str(source.get("source_id") or "")
        stable_id = str(source.get("stable_source_id") or "")
        if canonical_id and not any(question_id.startswith(f"{prefix}:") for prefix in (canonical_id, stable_id) if prefix):
            errors.append(f"{route_id}:question_id_source_mismatch")
        source_questions = source.get("questions", [])
        if isinstance(source_questions, list) and source_questions:
            known_question_ids = {str(item.get("question_id")) for item in source_questions if isinstance(item, dict) and item.get("question_id")}
            if question_id not in known_question_ids:
                if status == "ready" or mapping_status == "semantically_verified":
                    errors.append(f"{route_id}:unknown_question_id")
                else:
                    warnings.append(f"{route_id}:question_id_pending:{question_id}")
    required_courses = _route_ids(route, "required_course_keys", "required_course_key", ("required_courses", "course_keys"))
    unknown_courses = [value for value in required_courses if value not in course_keys]
    errors.extend(f"{route_id}:unknown_course:{value}" for value in unknown_courses)
    if not required_courses:
        if status == "ready" or mapping_status == "semantically_verified":
            errors.append(f"{route_id}:missing_required_courses")
        else:
            warnings.append(f"{route_id}:required_courses_pending")
    required_cycles = _route_ids(route, "required_cycle_ids", "required_cycle_id", ("required_cycles",))
    required_sections = _route_ids(route, "required_section_ids", "required_section_id", ("required_sections",))
    unknown_cycles = [value for value in required_cycles if value not in cycle_ids]
    unknown_sections = [value for value in required_sections if value not in section_ids]
    errors.extend(f"{route_id}:unknown_cycle:{value}" for value in unknown_cycles)
    errors.extend(f"{route_id}:unknown_section:{value}" for value in unknown_sections)
    for cycle in required_cycles:
        section = cycle_sections.get(cycle)
        if section and required_sections and section not in required_sections:
            warnings.append(f"{route_id}:cycle_section_not_listed:{cycle}:{section}")
    granularity = str(route.get("unlock_granularity") or route.get("unlock_after") or route.get("cadence") or "").strip().casefold()
    if granularity and granularity not in UNLOCK_GRANULARITIES:
        errors.append(f"{route_id}:invalid_unlock_granularity")
    if granularity == "after_cycle" and not required_cycles:
        errors.append(f"{route_id}:after_cycle_requires_cycle")
    if granularity == "after_multiple_cycles" and len(required_cycles) < 2:
        errors.append(f"{route_id}:after_multiple_cycles_requires_two_cycles")
    if granularity == "after_section" and not required_sections:
        errors.append(f"{route_id}:after_section_requires_section")
    if granularity == "after_multiple_sections" and len(required_sections) < 2:
        errors.append(f"{route_id}:after_multiple_sections_requires_two_sections")
    if granularity == "after_chapter" and not (required_sections or _route_ids(route, "required_chapter_ids", "required_chapter_id", ("required_chapters",))):
        errors.append(f"{route_id}:after_chapter_requires_chapter_or_section")
    uncertainties = _as_list(route.get("uncertainties") or route.get("review_reasons") or route.get("uncertainty"))
    blockers = _as_list(route.get("blockers") or route.get("blocking_reasons") or route.get("blocked_reason"))
    if status == "needs_review" and not uncertainties:
        errors.append(f"{route_id}:needs_review_requires_uncertainties")
    if status == "blocked" and not (uncertainties or blockers):
        errors.append(f"{route_id}:blocked_requires_reason")
    if status == "ready" and (uncertainties or blockers):
        errors.append(f"{route_id}:ready_route_has_open_uncertainty")
    unlock_status = str(route.get("unlock_status") or "").strip().casefold()
    if unlock_status == "unlocked" and status != "ready":
        errors.append(f"{route_id}:locked_review_route_cannot_be_unlocked")
    if status == "ready" and mapping_status != "semantically_verified":
        errors.append(f"{route_id}:ready_requires_semantically_verified")
    if mapping_status in {"needs_review", "blocked"} and status == "ready":
        errors.append(f"{route_id}:review_mapping_cannot_be_ready")
    return errors, warnings, status


def validate_payload(
    payload: dict[str, Any],
    project_root: Path,
    course_catalog: Path | None = None,
    strict_visual: bool = False,
) -> dict[str, Any]:
    course_keys, section_ids, cycle_ids, cycle_sections = _load_learning_ids(project_root, course_catalog)
    source_map, errors, warnings, pages_by_source = _source_records(payload)
    route_policy = payload.get("route_policy") if isinstance(payload.get("route_policy"), dict) else {}
    visual_gate_enabled = bool(strict_visual or route_policy.get("visual_review_gate"))
    routes = payload.get("routes", [])
    if isinstance(routes, dict):
        routes = list(routes.values())
    if not isinstance(routes, list):
        errors.append("routes:must_be_list")
        routes = []
    seen_route_ids: set[str] = set()
    seen_question_ids: set[str] = set()
    seen_question_keys: set[tuple[str, int, int, int]] = set()
    status_counts: dict[str, int] = {}
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            errors.append(f"route[{index}]:must_be_object")
            continue
        route_id = str(route.get("route_id") or route.get("id") or f"route[{index}]")
        if route_id in seen_route_ids:
            errors.append(f"{route_id}:duplicate_route_id")
        seen_route_ids.add(route_id)
        question_id = str(route.get("question_id") or "")
        if question_id:
            if question_id in seen_question_ids:
                errors.append(f"{route_id}:duplicate_question_id")
            seen_question_ids.add(question_id)
        source_id = str(route.get("source_id") or "")
        page = route.get("pdf_page", route.get("page"))
        number = route.get("question_number")
        occurrence = route.get("occurrence", 1)
        try:
            key = (source_id, int(page), int(number), int(occurrence))
        except (TypeError, ValueError):
            key = None
        if key is not None:
            if key in seen_question_keys:
                errors.append(f"{route_id}:duplicate_source_page_question")
            seen_question_keys.add(key)
        route_errors, route_warnings, status = _validate_route(
            route,
            index,
            source_map,
            pages_by_source,
            course_keys,
            section_ids,
            cycle_ids,
            cycle_sections,
            strict_visual=strict_visual,
            visual_gate_enabled=visual_gate_enabled,
        )
        errors.extend(route_errors)
        warnings.extend(route_warnings)
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "status": "passed" if not errors else "failed",
        "routes": len(routes),
        "sources": len(source_map),
        "course_keys": len(course_keys),
        "section_ids": len(section_ids),
        "cycle_ids": len(cycle_ids),
        "status_counts": status_counts,
        "errors": errors,
        "warnings": warnings,
        "visual_gate": {
            "enabled": visual_gate_enabled,
            "strict_requested": strict_visual,
            "legacy_manifest_notice": not visual_gate_enabled,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate optional exam routes and prerequisite evidence.")
    parser.add_argument("--manifest", type=Path, default=Path("data/exam_papers/manifest.json"))
    parser.add_argument("--project-root", type=Path, default=Path("."), help="repository root containing chapter manifests")
    parser.add_argument("--course-catalog", type=Path, help="optional course catalog JSON")
    parser.add_argument(
        "--strict-visual-review",
        action="store_true",
        help="require explicit verified visual review for every page of ready routes (also enabled by route_policy.visual_review_gate)",
    )
    args = parser.parse_args()
    try:
        payload = _read_json(args.manifest)
        report = validate_payload(
            payload,
            args.project_root.resolve(),
            args.course_catalog.resolve() if args.course_catalog else None,
            strict_visual=args.strict_visual_review,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "routes": 0, "errors": [str(error)], "warnings": []}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
