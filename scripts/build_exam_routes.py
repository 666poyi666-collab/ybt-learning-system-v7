#!/usr/bin/env python3
"""Build a conservative, source-page-backed exam question route index.

The four papers currently supplied by the user are configured in
``data/exam_papers/mapping_rules.json``.  New files are discovered
incrementally, but are never assigned a curriculum route from keywords alone:
an unconfigured paper/question remains a review candidate.  Question pages are
the only stem authority; answer pages are recorded as a separate range and are
never included in ``stem_text``.

The script uses the bundled PDF/OCR dependencies when available.  OCR is a
locator only.  Every route retains the source PDF SHA, original PDF page and a
rendered page SHA so a client can fetch and visually verify the exact page.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "data" / "exam_papers" / "mapping_rules.json"
MANIFEST_PATH = ROOT / "data" / "exam_papers" / "manifest.json"
INDEX_PATH = ROOT / "data" / "exam_papers" / "question_index.json"
REPORT_PATH = ROOT / "reports" / "exam-papers-current.json"
REPORT_MD_PATH = ROOT / "reports" / "exam-papers-current.md"
GUIDE_PATH = ROOT / "data" / "exam_papers" / "learning_route_guide.md"
ASSET_ROOT = ROOT / "data" / "exam_papers" / "page_assets"

QUESTION_MARKER = re.compile(r"(?m)(?<!\d)(\d{1,3})\s*[.．、:：)）]")
QUESTION_BOUNDARY_MARKER = re.compile(r"(?m)(?:^|\n)\s*(?:[（(](?:多选|单选)[)）]\s*)?(\d{1,3})\s*[.．、:：]")
MATH_EXAM_HINT = re.compile(r"(数学).*(期中|期末|月考|考试|试题|试卷|检测)", re.I)
EXCLUDED_HINT = re.compile(r"(答案|解析|必刷题|讲义|教材|精讲|物理|生物)", re.I)

PROFILE_TYPE_TAGS: dict[str, list[str]] = {
    "spatial_vector_coordinate_parallel": ["空间向量坐标运算", "平行/垂直判定"],
    "spatial_vector_coordinate_dot": ["空间向量坐标数量积", "垂直与模长"],
    "spatial_vector_basis": ["基底表示与向量分解"],
    "spatial_vector_dot_length": ["数量积与模长"],
    "spatial_geometry_angle": ["空间角/异面直线角"],
    "spatial_geometry_distance": ["点面/点线距离"],
    "spatial_geometry_core": ["线面/面面位置关系", "空间角与距离"],
    "spatial_geometry_comprehensive": ["立体几何综合题"],
    "spatial_geometry_moving_fold": ["立体几何动点/轨迹综合"],
    "line_slope": ["直线倾斜角与斜率"],
    "line_equation": ["直线方程/截距/定点"],
    "line_distance_fixed_point": ["直线交点、距离与最值"],
    "circle_basic": ["圆的方程"],
    "circle_line": ["直线与圆的位置关系"],
    "circle_circle": ["圆与圆位置关系/公共弦"],
    "ellipse_basic": ["椭圆定义与标准方程"],
    "ellipse_property": ["椭圆几何性质/离心率"],
    "ellipse_advanced": ["椭圆综合/定点定值最值"],
    "hyperbola_basic": ["双曲线定义与标准方程"],
    "hyperbola_property": ["双曲线渐近线/离心率"],
    "hyperbola_advanced": ["双曲线综合/定点定值"],
    "parabola_basic": ["抛物线定义/焦点准线"],
    "parabola_property": ["抛物线几何性质"],
    "parabola_advanced": ["抛物线综合/动点定值"],
    "sequence_term": ["数列通项/中项"],
    "sequence_sum": ["数列前n项和"],
    "sequence_geometric": ["等比数列通项与求和"],
    "sequence_mixed": ["数列递推/分组综合求和"],
    "sequence_harmonic": ["调和数列与最值"],
}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_id(source_sha256: str) -> str:
    return f"exam-{source_sha256[:16]}"


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# A page image being present proves that the source can be opened; it does
# not prove that a person (or an approved vision pass) checked the printed
# question.  Keep that distinction explicit so a text-layer PDF cannot be
# unlocked accidentally.  The aliases below make the gate compatible with
# older sidecars and with the vocabulary used by the textbook visual pipeline.
VISUAL_VERIFIED_VALUES = {
    "verified", "visually_verified", "vision_verified", "source_page_verified",
    "page_verified", "approved", "passed", "ready",
}
VISUAL_BLOCKED_VALUES = {
    "blocked", "unavailable", "failed", "invalid", "rejected",
}


def normalise_visual_review_status(value: Any) -> str:
    """Return the small canonical state machine used by exam-page gates."""

    if value is True:
        return "verified"
    if value is False or value is None:
        return "pending"
    text = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if text in VISUAL_VERIFIED_VALUES:
        return "verified"
    if text in VISUAL_BLOCKED_VALUES:
        return "blocked"
    # Existing indexes use NEEDS_SOURCE_PAGE_REVIEW and a few clients use
    # NEEDS_PAGE_VISUAL/UNVERIFIED.  Treat unknown values conservatively.
    if "block" in text or "unavailable" in text or "invalid" in text or "fail" in text:
        return "blocked"
    return "pending"


def visual_review_declaration(rule: dict[str, Any], page_number: int) -> tuple[str, dict[str, Any]]:
    """Resolve an optional page-level review declaration from mapping rules.

    Supported forms are deliberately narrow and deterministic:

    ``true``/``"verified"`` verifies all question pages;
    ``[1, 2]`` verifies those page numbers; and
    ``{"status": "verified", "pages": [1, 2], "reviewer": ...}``
    supports an auditable declaration.  Missing declarations remain pending.
    """

    raw = rule.get("visual_verification", rule.get("visual_review"))
    details: dict[str, Any] = {"source": "mapping_rules" if raw is not None else "default"}
    selected: Any = raw
    if isinstance(raw, dict):
        details.update({
            key: raw[key]
            for key in ("reviewer", "reviewed_at", "method", "evidence_sha256")
            if raw.get(key) is not None
        })
        page_statuses = raw.get("page_statuses") or raw.get("statuses")
        if isinstance(page_statuses, dict):
            selected = page_statuses.get(str(page_number), page_statuses.get(page_number, raw.get("status")))
        else:
            pages = raw.get("pages") or raw.get("page_numbers") or raw.get("verified_pages")
            if pages is not None:
                # A scalar page list means only listed pages are verified. A
                # list of objects may carry a status per page.
                if isinstance(pages, list) and any(isinstance(item, dict) for item in pages):
                    selected = raw.get("status")
                    for item in pages:
                        if not isinstance(item, dict):
                            continue
                        number = item.get("pdf_page", item.get("page", item.get("number")))
                        try:
                            matches = int(number) == page_number
                        except (TypeError, ValueError):
                            matches = False
                        if matches:
                            selected = item.get("status", item.get("visual_status", item.get("verified")))
                            details.update({
                                key: item[key]
                                for key in ("reviewer", "reviewed_at", "method", "evidence_sha256")
                                if item.get(key) is not None
                            })
                            break
                elif isinstance(pages, list):
                    selected = "verified" if page_number in {
                        int(item) for item in pages if str(item).strip().isdigit()
                    } else "pending"
            else:
                selected = raw.get("status", raw.get("verified"))
    elif isinstance(raw, list):
        # A list of page numbers is the compact page allowlist form.
        selected = "verified" if page_number in {
            int(item) for item in raw if str(item).strip().isdigit()
        } else "pending"
    status = normalise_visual_review_status(selected)
    details["status"] = status
    return status, details


def derive_type_tags(profiles: list[str], explicit: Any) -> list[str]:
    tags = [str(item) for item in as_list(explicit) if str(item).strip()]
    for profile in profiles:
        for tag in PROFILE_TYPE_TAGS.get(profile, []):
            if tag not in tags:
                tags.append(tag)
    return tags


def normalise_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\r", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def page_ranges(value: Any) -> list[int]:
    """Expand [start, end] or an explicit page list into sorted page numbers."""
    values = [int(item) for item in as_list(value) if str(item).strip().isdigit()]
    if len(values) == 2 and values[0] <= values[1]:
        return list(range(values[0], values[1] + 1))
    return sorted(set(item for item in values if item > 0))


def candidate_pdf(path: Path, rules_by_filename: dict[str, dict[str, Any]]) -> bool:
    if path.name in rules_by_filename:
        return True
    name = path.name
    return bool(MATH_EXAM_HINT.search(name) and not EXCLUDED_HINT.search(name))


def pdf_pages_and_text(path: Path) -> tuple[int | None, list[str], str | None]:
    """Read a PDF text layer without making pypdf a hard runtime dependency."""
    texts: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        for page in reader.pages:
            try:
                texts.append(normalise_text(page.extract_text() or ""))
            except Exception:
                texts.append("")
        return len(reader.pages), texts, None
    except Exception as error:
        page_count: int | None = None
        try:
            completed = subprocess.run(
                ["pdfinfo", str(path)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=True,
            )
            match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, re.M)
            page_count = int(match.group(1)) if match else None
        except Exception:
            pass
        return page_count, [], type(error).__name__


def render_page(document: Any, pdf_page: int, output: Path) -> bytes | None:
    try:
        page = document[pdf_page - 1]
        pixmap = page.get_pixmap(dpi=144, alpha=False)
        data = pixmap.tobytes("jpg", jpg_quality=90)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.is_file() or sha256_file(output) != sha256_bytes(data):
            output.write_bytes(data)
        return data
    except Exception:
        return None


def ocr_page(image_path: Path) -> tuple[str, float | None, str]:
    try:
        from rapidocr_onnxruntime import RapidOCR

        result, _ = RapidOCR()(str(image_path))
        lines: list[str] = []
        scores: list[float] = []
        for row in result or []:
            if not isinstance(row, (list, tuple)) or len(row) < 3:
                continue
            text = str(row[1]).strip()
            if text:
                lines.append(text)
            try:
                scores.append(float(row[2]))
            except (TypeError, ValueError):
                pass
        return normalise_text("\n".join(lines)), (sum(scores) / len(scores) if scores else None), "rapidocr_onnxruntime"
    except Exception as error:
        return "", None, f"unavailable:{type(error).__name__}"


def stem_excerpt(page_text: str, number: int, next_number: int | None = None) -> str:
    """Return a locator excerpt, never a claim that OCR is the canonical stem."""
    if not page_text:
        return ""
    matches = list(QUESTION_BOUNDARY_MARKER.finditer(page_text)) or list(QUESTION_MARKER.finditer(page_text))
    chosen = None
    for match in matches:
        if int(match.group(1)) == number:
            chosen = match
            break
    if chosen is None:
        # A scan may drop the printed number while retaining surrounding text.
        # Returning the whole page would silently attach neighboring questions;
        # leave the stem empty and force an original-page review instead.
        return ""
    start = chosen.start()
    end = len(page_text)
    stop_number = next_number
    if stop_number is not None:
        for match in matches:
            if match.start() > start and int(match.group(1)) == stop_number:
                end = match.start()
                break
    return page_text[start:end].strip()[:2400]


def continuation_excerpt(page_text: str, number: int) -> str:
    """Take a continuation page only up to the next printed question."""
    if not page_text:
        return ""
    matches = list(QUESTION_BOUNDARY_MARKER.finditer(page_text)) or list(QUESTION_MARKER.finditer(page_text))
    end = len(page_text)
    for match in matches:
        marker_number = int(match.group(1))
        # Any later numbered question (including a section's next block) ends
        # the current stem.  A repeated lower number is usually an OCR option
        # fragment and is intentionally left in the continuation.
        if marker_number > number:
            end = match.start()
            break
    return page_text[:end].strip()[:2400]


def load_curriculum() -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[str, int], list[str]]:
    cycles_by_section: dict[str, list[dict[str, Any]]] = {}
    sections: dict[str, dict[str, Any]] = {}
    section_order: dict[str, int] = {}
    course_order: list[str] = []
    order = 0
    for chapter in range(1, 6):
        path = ROOT / f"chapter{chapter}_manifest.json"
        payload = load_json(path, {}) or {}
        for section in payload.get("sections", []):
            section_id = str(section.get("id") or "")
            if not section_id:
                continue
            sections[section_id] = {"chapter": chapter, **section}
            section_order[section_id] = order
            order += 1
            cycles = []
            for sequence, cycle in enumerate(section.get("learning_cycles", []), start=1):
                row = {"section_id": section_id, "section_order": section_order.get(section_id, order), "sequence": sequence, **cycle}
                cycles.append(row)
                for field in ("course_keys", "prerequisite_course_keys", "optional_course_keys"):
                    for key in row.get(field, []) or []:
                        key = str(key)
                        if key and key not in course_order:
                            course_order.append(key)
            cycles_by_section[section_id] = cycles
    return cycles_by_section, sections, section_order, course_order


def expand_profile(
    profile_name: str,
    profiles: dict[str, dict[str, Any]],
    cycles_by_section: dict[str, list[dict[str, Any]]],
    sections: dict[str, dict[str, Any]],
    section_order: dict[str, int],
    course_order: list[str],
) -> dict[str, Any]:
    profile = profiles.get(profile_name)
    if not profile:
        return {"known": False, "profile": profile_name, "sections": [], "cycles": [], "courses": [], "tags": []}
    selected_sections = [str(item) for item in profile.get("sections", [])]
    cycle_prefix = profile.get("cycle_prefix", {}) or {}
    selected_cycles: list[dict[str, Any]] = []
    explicit_cycle_ids = [str(item) for item in profile.get("cycle_ids", []) or []]
    if explicit_cycle_ids:
        by_cycle_id = {
            str(cycle.get("id") or cycle.get("cycle_id") or ""): cycle
            for cycles in cycles_by_section.values() for cycle in cycles
        }
        for cycle_id in explicit_cycle_ids:
            cycle = by_cycle_id.get(cycle_id)
            if cycle:
                selected_cycles.append(cycle)
                selected_sections.append(str(cycle.get("section_id") or ""))
    else:
        for section_id in selected_sections:
            cycles = cycles_by_section.get(section_id, [])
            limit = int(cycle_prefix.get(section_id, len(cycles)))
            selected_cycles.extend(cycles[:limit])
    # A profile may name a small set of additional course identities when a
    # cycle is a method-only block with no direct video.  Do not blindly add
    # every inherited prerequisite from the manifest: those lists are often
    # cumulative and would turn a one-question route into an entire chapter.
    explicit_courses = [str(item) for item in profile.get("course_keys", []) or []]
    selected_cycles.sort(key=lambda row: (int(row.get("section_order", section_order.get(str(row.get("section_id") or ""), 9999))), int(row.get("sequence", 0))))
    cycle_ids: list[str] = []
    courses: list[str] = []
    course_sources: dict[str, list[str]] = defaultdict(list)
    for cycle in selected_cycles:
        cycle_id = str(cycle.get("id") or cycle.get("cycle_id") or "")
        if cycle_id and cycle_id not in cycle_ids:
            cycle_ids.append(cycle_id)
        for field in (("course_keys", "prerequisite_course_keys")
                      if profile.get("include_prerequisite_courses", False)
                      else ("course_keys",)):
            for key in cycle.get(field, []) or []:
                key = str(key)
                if key and key not in courses:
                    courses.append(key)
                if key and cycle_id:
                    course_sources[key].append(cycle_id)
    courses.sort(key=lambda key: (course_order.index(key) if key in course_order else 99999, key))
    for key in explicit_courses:
        if key and key not in courses:
            courses.append(key)
    selected_sections = sorted(set(item for item in selected_sections if item), key=lambda key: section_order.get(key, 9999))
    return {
        "known": True,
        "profile": profile_name,
        "sections": selected_sections,
        "cycles": cycle_ids,
        "courses": courses,
        "course_sources": {key: list(dict.fromkeys(value)) for key, value in course_sources.items()},
        "tags": list(profile.get("tags", [])),
        "cycle_rows": selected_cycles,
    }


def infer_pages(question_number: int, texts: list[str]) -> list[int]:
    pages: list[int] = []
    for index, text in enumerate(texts, start=1):
        if re.search(rf"(?<!\d){question_number}\s*[.．、:：)）]", text):
            pages.append(index)
    return pages[:2] or ([1] if texts else [])


def relative_source_path(path: Path, source_root: Path) -> str:
    try:
        return path.relative_to(source_root).as_posix()
    except ValueError:
        return path.name


def build_source(path: Path, source_root: Path, rule: dict[str, Any] | None, profiles: dict[str, Any], curriculum: tuple[Any, ...], asset_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    cycles_by_section, sections, section_order, course_order = curriculum
    source_sha = sha256_file(path)
    sid = source_id(source_sha)
    page_count, texts, metadata_error = pdf_pages_and_text(path)
    if page_count is None:
        page_count = len(texts) or 0
    rule = rule or {}
    expected_rule_sha = str(rule.get("source_pdf_sha256") or rule.get("sha256") or "").lower()
    rule_mismatch = bool(expected_rule_sha and expected_rule_sha != source_sha)
    if rule_mismatch:
        # Never apply a mapping written for an older PDF with the same name.
        rule = {}
    q_pages = page_ranges(rule.get("question_pdf_pages"))
    a_pages = page_ranges(rule.get("answer_pdf_pages"))
    if not q_pages:
        answer_start = next((index + 1 for index, text in enumerate(texts) if "参考答案" in text or "答案与试题解析" in text), None)
        q_pages = list(range(1, (answer_start or page_count + 1)))
    if not a_pages and page_count:
        answer_start = next(
            (page for page in range(1, page_count + 1)
             if page not in q_pages and "参考答案" in (texts[page - 1] if page <= len(texts) else "")),
            None,
        )
        if answer_start:
            a_pages = list(range(answer_start, page_count + 1))
    q_pages = [page for page in q_pages if 1 <= page <= page_count]
    a_pages = [page for page in a_pages if 1 <= page <= page_count]
    known_question_pages = set(q_pages)

    # Render only original question pages.  Answer pages are intentionally not
    # copied into the learner-facing asset tree.
    document = None
    try:
        try:
            import pymupdf as pdf_module
        except ImportError:
            import fitz as pdf_module  # type: ignore
        document = pdf_module.open(str(path))
    except Exception:
        document = None
    page_rows: list[dict[str, Any]] = []
    ocr_texts: dict[int, str] = {}
    ocr_confidence: dict[int, float | None] = {}
    extraction_method: dict[int, str] = {}
    for page in range(1, page_count + 1):
        text = texts[page - 1] if page <= len(texts) else ""
        image_path = asset_root / sid / f"page-{page:03d}.jpg"
        image_bytes = render_page(document, page, image_path) if document is not None and page in known_question_pages else None
        image_sha = sha256_bytes(image_bytes) if image_bytes else (sha256_file(image_path) if image_path.is_file() else None)
        if page in known_question_pages and not text and image_path.is_file():
            text, confidence, method = ocr_page(image_path)
            ocr_texts[page] = text
            ocr_confidence[page] = confidence
            extraction_method[page] = method
        elif text:
            extraction_method[page] = "pdf_text_layer"
        else:
            extraction_method[page] = "unavailable"
        role = "question" if page in known_question_pages else "answer" if page in set(a_pages) else "unknown"
        if role == "question":
            declared_visual_status, visual_details = visual_review_declaration(rule, page)
            review_ref = rule.get("page_review_report")
            if review_ref:
                review_path = (ROOT / str(review_ref)).resolve()
                review = load_json(review_path, {}) if review_path.is_relative_to(ROOT) else {}
                observed = next((entry for entry in review.get("page_evidence", [])
                                 if entry.get("source") == sid and entry.get("page") == image_path.name), None)
                declared_visual_status = "verified" if observed and observed.get("sha256") == image_sha else "blocked"
                visual_details = {"status": declared_visual_status, "source": str(review_ref),
                                  "reviewer": review.get("reviewer"), "reviewed_at": review.get("review_date"),
                                  "method": review.get("method"), "evidence_sha256": observed.get("sha256") if observed else None}
            # A review declaration without an immutable page image is not
            # sufficient evidence.  Downgrade it rather than allowing a
            # broken renderer or stale asset to unlock a route.
            if declared_visual_status == "verified" and not re.fullmatch(r"[0-9a-f]{64}", str(image_sha or ""), re.I):
                declared_visual_status = "blocked"
                visual_details["status"] = declared_visual_status
                visual_details["reason"] = "question_page_image_missing_or_unhashed"
            visual_status = "VISUALLY_VERIFIED" if declared_visual_status == "verified" else "NEEDS_SOURCE_PAGE_REVIEW"
            visual_review_status = declared_visual_status
        else:
            visual_status = "NOT_LEARNER_SOURCE"
            visual_review_status = "not_applicable"
            visual_details = {"source": "not_a_question_page", "status": visual_review_status}
        page_rows.append({
            "pdf_page": page,
            "page_role": role,
            "question_authority": role == "question",
            "text_layer_available": bool(texts[page - 1].strip()) if page <= len(texts) else False,
            "text_char_count": len(text),
            "text_sha256": sha256_bytes(text.encode("utf-8")) if text else None,
            "ocr_status": "used" if page in ocr_texts else "not_needed" if text else "unavailable",
            "ocr_text_sha256": sha256_bytes(ocr_texts[page].encode("utf-8")) if page in ocr_texts and ocr_texts[page] else None,
            "ocr_confidence": ocr_confidence.get(page),
            "page_image_path": str(image_path.relative_to(ROOT)).replace("\\", "/") if image_path.is_file() and role == "question" else None,
            "page_image_sha256": image_sha if role == "question" else None,
            "visual_status": visual_status,
            "visual_review_status": visual_review_status,
            "visual_review_evidence": visual_details,
        })
    if document is not None:
        try:
            document.close()
        except Exception:
            pass

    q_meta = rule.get("questions", {}) if isinstance(rule.get("questions"), dict) else {}
    q_page_map = rule.get("question_pages", {}) if isinstance(rule.get("question_pages"), dict) else {}
    q_count = int(rule.get("question_count") or 0)
    if not q_count:
        numbers = []
        for page in q_pages:
            source_text = (texts[page - 1] if page <= len(texts) else "") or ocr_texts.get(page, "")
            numbers.extend(int(match.group(1)) for match in QUESTION_MARKER.finditer(source_text) if int(match.group(1)) < 100)
        q_count = max(numbers, default=0)
    source_row: dict[str, Any] = {
        "active": True,
        "lifecycle_status": "active",
        "source_id": sid,
        "stable_source_id": f"exam-{source_sha}",
        "sha256": source_sha,
        "source_pdf_sha256": source_sha,
        "relative_path": relative_source_path(path, source_root),
        "file_name": path.name,
        "source_role": "question_paper",
        "question_authority": True,
        "allowlisted": bool(rule),
        "included_for_routes": not rule_mismatch,
        "page_count": page_count,
        "question_pdf_pages": q_pages,
        "answer_pdf_pages": a_pages,
        "question_page_ranges": [[min(q_pages), max(q_pages)]] if q_pages else [],
        "answer_page_ranges": [[min(a_pages), max(a_pages)]] if a_pages else [],
        "answer_separation_status": "visually_verified" if q_pages and a_pages else "needs_review",
        "question_completeness": "complete" if rule and q_count else "unknown",
        "answer_completeness": rule.get("answer_completeness", "unknown"),
        "question_count": q_count,
        "text_layer_pages": sum(bool(texts[page - 1].strip()) for page in q_pages if page <= len(texts)),
        "ocr_question_pages": sorted(ocr_texts),
        "metadata_error": metadata_error,
        "mapping_rule_status": "stale_hash" if rule_mismatch else "matched" if expected_rule_sha else "not_configured",
        "visual_review_status": (
            "verified"
            if q_pages and all(row.get("visual_review_status") == "verified" for row in page_rows if row.get("pdf_page") in known_question_pages)
            else "blocked"
            if any(row.get("visual_review_status") == "blocked" for row in page_rows if row.get("pdf_page") in known_question_pages)
            else "pending"
        ),
        "visual_reviewed_question_page_count": sum(
            1 for row in page_rows
            if row.get("pdf_page") in known_question_pages and row.get("visual_review_status") == "verified"
        ),
        "visual_review_pending_question_pages": [
            row.get("pdf_page") for row in page_rows
            if row.get("pdf_page") in known_question_pages and row.get("visual_review_status") == "pending"
        ],
        "visual_review_blocked_question_pages": [
            row.get("pdf_page") for row in page_rows
            if row.get("pdf_page") in known_question_pages and row.get("visual_review_status") == "blocked"
        ],
        "pages": page_rows,
        "source_path_policy": "external_source_root; original PDF is not copied into repository",
    }
    routes: list[dict[str, Any]] = []
    for number in range(1, q_count + 1):
        meta = q_meta.get(str(number), {}) if isinstance(q_meta, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        raw_pages = q_page_map.get(str(number))
        question_pages = page_ranges(raw_pages) if raw_pages is not None else infer_pages(number, [texts[p - 1] if p <= len(texts) else ocr_texts.get(p, "") for p in q_pages])
        question_pages = [page for page in question_pages if page in known_question_pages]
        if not question_pages:
            question_pages = [q_pages[0]] if q_pages else []
        first_page = min(question_pages) if question_pages else None
        excerpts: list[str] = []
        first_marker_found = False
        for index, page in enumerate(question_pages):
            page_text = texts[page - 1] if page <= len(texts) and texts[page - 1] else ocr_texts.get(page, "")
            if index == 0:
                next_number = number + 1
                first_marker_found = any(int(match.group(1)) == number for match in QUESTION_BOUNDARY_MARKER.finditer(page_text))
                excerpt = stem_excerpt(page_text, number, next_number)
            else:
                excerpt = continuation_excerpt(page_text, number)
            if excerpt:
                excerpts.append(excerpt)
        stem = "\n\n".join(dict.fromkeys(excerpts)).strip()
        # OCR on a two-column scan can attach the previous/next printed item
        # when the current number is faint.  Never expose that mixed text as a
        # question stem; keep only a machine-side locator and require review.
        neighbor_markers = [
            int(match.group(1))
            for match in QUESTION_BOUNDARY_MARKER.finditer(stem)
            if int(match.group(1)) != number and 1 <= int(match.group(1)) <= 99
        ]
        contaminated_stem = bool(neighbor_markers) or not first_marker_found
        ocr_locator_excerpt = stem if contaminated_stem else None
        if contaminated_stem:
            stem = f"原卷第{number}题：OCR 切分包含邻题，必须查看原页图确认题面。"
        extraction = "pdf_text_layer" if all(extraction_method.get(page) == "pdf_text_layer" for page in question_pages) and question_pages else "ocr_locator" if stem else "unavailable"
        profiles_raw = meta.get("profiles", [])
        profiles_selected = [str(item) for item in as_list(profiles_raw)]
        expansions = [expand_profile(name, profiles, cycles_by_section, sections, section_order, course_order) for name in profiles_selected]
        known_expansions = [item for item in expansions if item["known"] and item["profile"] != "out_of_scope"]
        out_of_scope = any(item["profile"] == "out_of_scope" for item in expansions)
        required_sections: list[str] = []
        required_cycles: list[str] = []
        required_courses: list[str] = []
        tags: list[str] = []
        course_sources: dict[str, list[str]] = defaultdict(list)
        cycle_rows: dict[str, dict[str, Any]] = {}
        for expansion in expansions:
            for value in expansion.get("sections", []):
                if value not in required_sections:
                    required_sections.append(value)
            for value in expansion.get("cycles", []):
                if value not in required_cycles:
                    required_cycles.append(value)
            for value in expansion.get("courses", []):
                if value not in required_courses:
                    required_courses.append(value)
            for value in expansion.get("tags", []):
                if value not in tags:
                    tags.append(value)
            for key, values in expansion.get("course_sources", {}).items():
                course_sources[key].extend(values)
            for row in expansion.get("cycle_rows", []):
                cycle_id = str(row.get("id") or row.get("cycle_id") or "")
                if cycle_id:
                    cycle_rows[cycle_id] = row
        required_sections.sort(key=lambda value: section_order.get(value, 9999))
        required_cycles.sort(key=lambda value: (section_order.get(value.split("-cycle-")[0], 9999),
                                               int(value.rsplit("-", 1)[-1]) if value.rsplit("-", 1)[-1].isdigit() else 9999))
        required_courses.sort(key=lambda value: (course_order.index(value) if value in course_order else 99999, value))
        # A route can be mathematically mapped yet still be held for visual
        # review when OCR was needed, the paper explicitly requests review, or
        # any source page lacks an explicit visual-review declaration.  A
        # rendered image is evidence that the page is available, not evidence
        # that its formulae/figures were checked.
        visual_review_rows = [
            dict(row) for row in page_rows
            if row.get("pdf_page") in set(question_pages)
        ]
        # A source-wide declaration is the normal form.  Permit a question
        # to carry a narrower declaration when only one item's page has been
        # checked; this avoids falsely promoting unrelated questions.
        question_visual_rule = None
        if "visual_verification" in meta or "visual_review" in meta:
            question_visual_rule = {
                "visual_verification": meta.get("visual_verification", meta.get("visual_review")),
            }
        if question_visual_rule is not None:
            for row in visual_review_rows:
                declared_status, declared_details = visual_review_declaration(question_visual_rule, int(row["pdf_page"]))
                if declared_status == "verified" and not re.fullmatch(r"[0-9a-f]{64}", str(row.get("page_image_sha256") or ""), re.I):
                    declared_status = "blocked"
                    declared_details["status"] = declared_status
                    declared_details["reason"] = "question_page_image_missing_or_unhashed"
                row["visual_review_status"] = declared_status
                row["visual_status"] = "VISUALLY_VERIFIED" if declared_status == "verified" else "NEEDS_SOURCE_PAGE_REVIEW"
                row["visual_review_evidence"] = declared_details
        visual_review_pending_pages = [
            int(row["pdf_page"]) for row in visual_review_rows
            if row.get("visual_review_status") == "pending"
        ]
        visual_review_blocked_pages = [
            int(row["pdf_page"]) for row in visual_review_rows
            if row.get("visual_review_status") == "blocked"
        ]
        visual_review_status = (
            "blocked" if visual_review_blocked_pages
            else "verified" if visual_review_rows and not visual_review_pending_pages
            else "pending"
        )
        review_required = (
            bool(meta.get("force_review"))
            or extraction != "pdf_text_layer"
            or not stem
            or not first_marker_found
            or contaminated_stem
            or visual_review_status != "verified"
        )
        uncertainties = list(str(item) for item in as_list(meta.get("uncertainties")))
        if extraction == "ocr_locator":
            uncertainties.append("题面由 OCR 定位，公式、选项和图形必须回看原页图")
        if extraction == "unavailable":
            uncertainties.append("当前运行环境未取得可检索文字，必须直接查看原页图")
        if not first_marker_found:
            uncertainties.append("原卷页未可靠识别本题打印题号，题面切分需回看原页图")
        if visual_review_pending_pages:
            uncertainties.append(
                "原卷页尚未完成视觉复核："
                + ",".join(str(page) for page in visual_review_pending_pages)
                + "页"
            )
        if visual_review_blocked_pages:
            uncertainties.append(
                "原卷页视觉证据不可用："
                + ",".join(str(page) for page in visual_review_blocked_pages)
                + "页"
            )
        uncertainties = list(dict.fromkeys(uncertainties))
        if rule_mismatch:
            route_status = "needs_review"
            route_state = "stale_source_mapping"
            unlock_status = "needs_review"
            mapping_status = "candidate"
            blockers = ["原卷 SHA-256 已变化，旧映射不能复用"]
        elif out_of_scope:
            route_status = "blocked"
            route_state = "blocked_external_prerequisite"
            unlock_status = "blocked"
            mapping_status = "candidate"
            blockers = list(str(item) for item in as_list(meta.get("external_prerequisites"))) or ["题目不在当前选择性必修1路线范围内"]
        elif not known_expansions or not required_cycles:
            route_status = "needs_review"
            route_state = "needs_review"
            unlock_status = "needs_review"
            mapping_status = "candidate"
            blockers = ["尚未配置经审核的节次/循环映射"]
        elif visual_review_blocked_pages:
            route_status = "blocked"
            route_state = "blocked_visual_evidence"
            unlock_status = "blocked"
            mapping_status = "candidate"
            blockers = [
                "原卷题面页图缺失或哈希无效，无法完成视觉复核："
                + ",".join(str(page) for page in visual_review_blocked_pages)
                + "页"
            ]
        elif review_required:
            route_status = "needs_review"
            route_state = "needs_review"
            unlock_status = "needs_review"
            mapping_status = "candidate"
            blockers = []
        else:
            # The route definition is valid, but it is not unlocked merely by
            # existing in the manifest.  The query command evaluates explicit
            # cycle/course completion for the current learner.
            route_status = "candidate"
            route_state = "ready_for_optional_unlock"
            unlock_status = "locked_until_prerequisites"
            mapping_status = "semantically_verified"
            blockers = []
        if meta.get("force_review") and "该题含跨主题子问，需人工确认依赖闭包" not in uncertainties:
            uncertainties.append("该题含跨主题子问，需人工确认依赖闭包")
        if rule_mismatch and "原卷 SHA-256 已变化，需重新核对题面与映射" not in uncertainties:
            uncertainties.append("原卷 SHA-256 已变化，需重新核对题面与映射")
        question_id = f"{sid}:p{first_page or 0}:q{number}:r1"
        page_evidence = []
        for page in question_pages:
            page_row = next((row for row in page_rows if row["pdf_page"] == page), None)
            if not page_row:
                continue
            page_evidence.append({
                "pdf_page": page,
                "source_pdf_sha256": source_sha,
                "page_image_path": page_row.get("page_image_path"),
                "page_image_sha256": page_row.get("page_image_sha256"),
                "question_authority": True,
                "visual_status": page_row.get("visual_status"),
                "visual_review_status": page_row.get("visual_review_status"),
                "visual_review_evidence": page_row.get("visual_review_evidence"),
            })
        recommended_path = []
        for order, cycle_id in enumerate(required_cycles, start=1):
            cycle = cycle_rows.get(cycle_id, {})
            direct_courses = [str(item) for item in cycle.get("course_keys", []) or []]
            recommended_path.append({
                "order": order,
                "cycle_id": cycle_id,
                "cycle_title": cycle.get("title"),
                "course_keys": direct_courses,
                "action": "听完课程 -> 完成对应一本通循环 -> 独立自检",
            })
        route = {
            "active": True,
            "route_id": question_id,
            "question_id": question_id,
            "source_id": sid,
            "source_sha256": source_sha,
            "source_pdf_sha256": source_sha,
            "file_name": path.name,
            "pdf_page": first_page,
            "pdf_pages": question_pages,
            "question_number": number,
            "occurrence": 1,
            "question_ref": f"{path.name}：原卷第{first_page or '?'}页第{number}题",
            "question_authority": "original_question_page",
            "source_page_evidence": page_evidence,
            "visual_review_status": visual_review_status,
            "visual_review_pages": {
                "required": [int(row["pdf_page"]) for row in visual_review_rows],
                "verified": [int(row["pdf_page"]) for row in visual_review_rows if row.get("visual_review_status") == "verified"],
                "pending": visual_review_pending_pages,
                "blocked": visual_review_blocked_pages,
            },
            "stem_text": stem,
            "stem_excerpt": stem,
            "ocr_locator_excerpt": ocr_locator_excerpt,
            "stem_text_sha256": sha256_bytes(stem.encode("utf-8")) if stem else None,
            "stem_text_status": extraction,
            "extraction_method": extraction,
            "mapping_profiles": profiles_selected,
            "mapping_status": mapping_status,
            "mapping_confidence": "high" if mapping_status == "semantically_verified" else "medium" if profiles_selected else "none",
            "mapping_evidence": [{"kind": "reviewed_route_rule", "profile": name} for name in profiles_selected],
            "topic_tags": tags,
            "knowledge_tags": tags,
            "type_tags": (derive_type_tags(profiles_selected, meta.get("type_tags"))
                          or tags or (["当前教材范围外"] if out_of_scope else ["题型待核"])),
            "required_section_ids": required_sections,
            "required_cycle_ids": required_cycles,
            "required_course_keys": required_courses,
            "required_courses": [{"course_key": key, "cycle_ids": list(dict.fromkeys(course_sources.get(key, [])))} for key in required_courses],
            "recommended_path": recommended_path,
            "external_prerequisites": list(str(item) for item in as_list(meta.get("external_prerequisites"))),
            "uncertainties": uncertainties,
            "blockers": blockers,
            "route_status": route_status,
            "route_state": route_state,
            "unlock_status": unlock_status,
            "needs_review": route_status == "needs_review",
            "blocked": route_status == "blocked",
            "optional": True,
            "blocks_ybt_progress": False,
            "unlock_policy": {
                "mode": "all",
                "required_cycle_ids": required_cycles,
                "required_course_keys": required_courses,
                "review_required_before_unlock": bool(review_required or blockers),
                "visual_review_required": True,
                "visual_review_status": visual_review_status,
                "progress_source": "explicit_cycle_completion_and_course_consumption",
            },
            "answer_policy": {
                "answer_pages_are_not_question_authority": True,
                "answer_reference_status": "separate_source_pages_only",
                "answer_completeness": source_row.get("answer_completeness"),
            },
            "topic_summary": str(meta.get("summary") or ""),
        }
        routes.append(route)
    source_row["question_ids"] = [row["question_id"] for row in routes]
    source_row["route_count"] = len(routes)
    source_row["route_review_count"] = sum(bool(row["needs_review"]) for row in routes)
    source_row["route_blocked_count"] = sum(bool(row["blocked"]) for row in routes)
    return source_row, routes


def discover_paths(source_root: Path, rules_by_filename: dict[str, dict[str, Any]]) -> list[Path]:
    return sorted(
        [path for path in source_root.rglob("*") if path.is_file() and path.suffix.casefold() == ".pdf" and candidate_pdf(path, rules_by_filename)],
        key=lambda path: path.as_posix().casefold(),
    )


def merge_existing(current_sources: list[dict[str, Any]], current_routes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # A renamed/copied PDF has the same full hash. Keep one canonical source
    # and retain every observed relative path as an alias; do not duplicate
    # its question routes.
    dedup_sources: dict[str, dict[str, Any]] = {}
    dedup_routes: dict[str, dict[str, Any]] = {}
    for source in current_sources:
        key = str(source.get("sha256") or source.get("source_pdf_sha256") or source.get("source_id") or "")
        if key in dedup_sources:
            previous = dedup_sources[key]
            aliases = list(previous.get("aliases", []))
            alias = source.get("relative_path") or source.get("file_name")
            if alias and alias not in aliases and alias != previous.get("relative_path"):
                aliases.append(alias)
            previous["aliases"] = aliases
        else:
            dedup_sources[key] = source
    for route in current_routes:
        key = str(route.get("route_id") or route.get("question_id") or "")
        if key and key not in dedup_routes:
            dedup_routes[key] = route
    previous = load_json(MANIFEST_PATH, {}) or {}
    old_sources = previous.get("sources", []) if isinstance(previous, dict) else []
    old_routes = previous.get("routes", []) if isinstance(previous, dict) else []
    source_by_hash = {key: row for key, row in dedup_sources.items() if key}
    for row in old_sources:
        key = str(row.get("sha256") or row.get("source_pdf_sha256") or "")
        if key and key not in source_by_hash:
            historical = dict(row)
            historical["active"] = False
            historical["included_for_routes"] = False
            historical["route_ready"] = False
            historical["lifecycle_status"] = "source_removed_or_replaced"
            source_by_hash[key] = historical
    route_by_id = {key: row for key, row in dedup_routes.items() if key}
    for row in old_routes:
        key = str(row.get("route_id") or row.get("question_id") or "")
        if key and key not in route_by_id:
            historical = dict(row)
            historical["active"] = False
            historical["route_status"] = "blocked"
            historical["route_state"] = "source_removed_or_replaced"
            historical["unlock_status"] = "blocked"
            historical["blocked"] = True
            historical["needs_review"] = False
            blockers = [str(item) for item in historical.get("blockers", []) or []]
            if "原卷文件已移除或被新版本替换" not in blockers:
                blockers.append("原卷文件已移除或被新版本替换")
            historical["blockers"] = blockers
            route_by_id[key] = historical
    return list(source_by_hash.values()), list(route_by_id.values())


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# 试卷题目路线索引",
        "",
        f"生成时间：{payload.get('generated_at')}",
        "",
        "原卷是独立的可选题源，不会阻塞《一本通》主线。题面只引用原卷页；答案页仅作隔离元数据记录。‘路线已准备’不等于用户已完成前置或已解锁。",
        "",
        f"- 原卷：{payload.get('summary', {}).get('source_count', 0)} 份",
        f"- 题目：{payload.get('summary', {}).get('route_count', 0)} 道",
        f"- 路线已准备、等待真实前置（非已解锁）：{payload.get('summary', {}).get('ready_count', 0)} 道",
        f"- 原卷页已完成视觉复核：{payload.get('summary', {}).get('visual_verified_route_count', 0)} 道",
        f"- 原卷页待视觉复核：{payload.get('summary', {}).get('visual_review_pending_route_count', 0)} 道",
        f"- 待复核：{payload.get('summary', {}).get('review_count', 0)} 道",
        f"- 当前范围外/阻塞：{payload.get('summary', {}).get('blocked_count', 0)} 道",
        "",
        "## 使用方式",
        "",
        "1. 查询某题的 `required_cycle_ids` 与 `required_course_keys`。",
        "2. 只有显式完成全部循环、课程消费且题面复核通过，才显示为可选可做。",
        "3. 未配置或存在 OCR/跨主题不确定性的题，保留原页证据并标记待复核，不自动解锁。",
        "",
        "## 原卷清单",
        "",
        "| 原卷 | 题面页 | 答案页 | 题数 | 待复核 | 阻塞 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for source in payload.get("sources", []):
        lines.append(
            f"| {source.get('file_name')} | {','.join(map(str, source.get('question_pdf_pages', [])))} | "
            f"{','.join(map(str, source.get('answer_pdf_pages', [])))} | {source.get('question_count', 0)} | "
            f"{source.get('route_review_count', 0)} | {source.get('route_blocked_count', 0)} |"
        )
    lines.extend(["", "## 复核边界", "", "- `semantically_verified` 只表示规则已绑定到现有课程循环，不等于用户已经掌握。", "- `visual_review_status=verified` 才表示每一张原卷页都有明确复核声明和有效页图 SHA；页图存在本身不等于已复核。", "- `needs_review` 题必须先回看原页图，公式、选项和图形不能以 OCR 文本代替。", "- `blocked_external_prerequisite` 题会明确列出当前书外的先修内容。", ""])
    return "\n".join(lines)


def course_title_map() -> dict[str, dict[str, Any]]:
    catalog = load_json(ROOT / "data" / "all_chapters_course_catalog.json", {}) or {}
    rows = catalog.get("courses", []) if isinstance(catalog, dict) else []
    result = {
        str(row.get("course_key")): row
        for row in rows
        if isinstance(row, dict) and row.get("course_key")
    }
    # Frozen catalogs may intentionally keep a stable key as the title.  The
    # chapter manifests still carry the human-readable course title/id.
    for chapter in range(1, 6):
        manifest = load_json(ROOT / f"chapter{chapter}_manifest.json", {}) or {}
        collections = []
        if isinstance(manifest.get("courses"), dict):
            collections.extend(item for value in manifest["courses"].values() for item in (value if isinstance(value, list) else [value]))
        for key in ("course_inventory", "course_catalog"):
            value = manifest.get(key)
            if isinstance(value, list):
                collections.extend(value)
            elif isinstance(value, dict):
                collections.extend(item for row in value.values() for item in (row if isinstance(row, list) else [row]))
        for row in collections:
            if isinstance(row, dict) and row.get("course_key"):
                result[str(row["course_key"])] = {**result.get(str(row["course_key"]), {}), **row}
    # The frozen catalog intentionally preserves course keys as stable labels;
    # transcript filenames retain the human-facing title.  Use that title
    # when a catalog row has no readable one.
    by_course_id: dict[str, str] = {}
    transcript_root = ROOT / "data" / "course_transcripts"
    if transcript_root.is_dir():
        for path in transcript_root.glob("*.json"):
            match = re.match(r"^(\d+(?:\.\d+){1,5}(?:\.[a-z])?)\s+(.+)\.json$", path.name, re.I)
            if match:
                by_course_id[match.group(1)] = match.group(2)
    for key, row in result.items():
        course_id = str(row.get("course_id") or "")
        title = str(row.get("title") or "")
        if course_id in by_course_id and (not title or title == key or title == course_id):
            row["title"] = by_course_id[course_id]
    return result


def learning_route_guide(payload: dict[str, Any]) -> str:
    """Create a human-readable index without exposing any answer text."""
    titles = course_title_map()
    lines = [
        "# 试卷题目 -> 学习路径",
        "",
        "这是一份可选试卷路线。每道题都先列出课程和《一本通》循环；只有显式完成全部前置且题面复核通过，才可开始做题。这里不包含答案。",
        "",
    ]
    for source in payload.get("sources", []):
        sid = source.get("source_id")
        lines.extend([
            f"## {source.get('file_name')}",
            "",
            f"题面页：{','.join(map(str, source.get('question_pdf_pages', [])))}。",
            "",
        ])
        rows = [row for row in payload.get("routes", []) if row.get("source_id") == sid]
        for route in sorted(rows, key=lambda row: int(row.get("question_number") or 0)):
            status = route.get("route_state") or route.get("route_status")
            if route.get("blocked"):
                status_text = "阻塞：当前教材范围外"
            elif route.get("needs_review"):
                status_text = "待复核：先看原卷页图"
            else:
                status_text = "已建立映射：完成前置后可选做"
            lines.extend([
                f"### 第{route.get('question_number')}题（原卷第{route.get('pdf_page')}页）",
                "",
                f"题目摘要：{route.get('topic_summary') or '未提供摘要'}。",
                f"题型标签：{'、'.join(route.get('type_tags', []) or ['待复核'])}。",
                f"状态：{status_text}。",
            ])
            sections = route.get("required_section_ids", []) or []
            lines.append(f"需要先学的节次：{ '、'.join(f'`{item}`' for item in sections) if sections else '无（请先完成外部先修）'}。")
            cycles = route.get("recommended_path", []) or []
            if cycles:
                lines.extend(["", "需要先完成的循环：", ""])
                for item in cycles:
                    course_keys = item.get("course_keys", []) or []
                    lines.append(
                        f"- 第{str(item.get('cycle_id', '')).split('-cycle-')[0]}节 · 循环{str(item.get('cycle_id', '')).split('-cycle-')[-1]}：{item.get('cycle_title') or ''}"
                    )
            else:
                lines.append("需要先完成的循环：无（当前范围外或尚未建立可靠映射）。")
            courses = route.get("required_course_keys", []) or []
            if courses:
                lines.extend(["", "需要先听完的课程：", ""])
                for key in courses:
                    row = titles.get(str(key), {})
                    title = row.get("title") or str(key)
                    course_id = row.get("course_id")
                    label = f"{course_id} {title}" if course_id and str(course_id) != str(key) else title
                    lines.append(f"- {label}")
            if route.get("external_prerequisites"):
                lines.append(f"当前书外先修：{'、'.join(route['external_prerequisites'])}。")
            if route.get("uncertainties"):
                lines.append(f"复核提示：{'；'.join(route['uncertainties'])}。")
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True, help="包含用户原卷的目录，例如 Downloads")
    parser.add_argument("--asset-root", type=Path, default=ASSET_ROOT)
    parser.add_argument("--rules", type=Path, default=RULES_PATH)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    rules = load_json(args.rules, {}) or {}
    profiles = rules.get("profiles", {}) if isinstance(rules, dict) else {}
    source_rules = rules.get("sources", {}) if isinstance(rules, dict) else {}
    rules_by_filename = {str(row.get("file_name")): row for row in source_rules.values() if isinstance(row, dict) and row.get("file_name")}
    curriculum = load_curriculum()
    paths = discover_paths(source_root, rules_by_filename)
    if not paths:
        raise SystemExit(f"no candidate math exam PDFs found under {source_root}")
    sources: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    for path in paths:
        source_sha = sha256_file(path)
        sid = source_id(source_sha)
        rule = source_rules.get(sid)
        if rule is None:
            rule = rules_by_filename.get(path.name)
        source, source_routes = build_source(path, source_root, rule, profiles, curriculum, args.asset_root.resolve())
        sources.append(source)
        routes.extend(source_routes)
        print(json.dumps({"source_id": source["source_id"], "file": path.name, "questions": len(source_routes), "review": source["route_review_count"], "blocked": source["route_blocked_count"]}, ensure_ascii=False))
    merged_sources, merged_routes = merge_existing(sources, routes)
    # Keep source-level counts correct after incremental historical rows or a
    # same-SHA rename are merged.
    routes_by_source: dict[str, list[dict[str, Any]]] = {}
    for route in merged_routes:
        routes_by_source.setdefault(str(route.get("source_id") or ""), []).append(route)
    for source in merged_sources:
        source_routes = routes_by_source.get(str(source.get("source_id") or ""), [])
        source["question_ids"] = [str(route.get("question_id") or route.get("route_id")) for route in source_routes if route.get("question_id") or route.get("route_id")]
        source["route_count"] = len(source_routes)
        source["route_review_count"] = sum(bool(route.get("needs_review")) for route in source_routes)
        source["route_blocked_count"] = sum(bool(route.get("blocked")) for route in source_routes)
    merged_sources.sort(key=lambda row: (str(row.get("file_name") or ""), str(row.get("sha256") or "")))
    merged_routes.sort(key=lambda row: (str(row.get("source_id") or ""), int(row.get("question_number") or 0)))
    summary = {
        "source_count": len(merged_sources),
        "active_source_count": sum(bool(row.get("active", True)) for row in merged_sources),
        "route_count": len(merged_routes),
        "active_route_count": sum(bool(row.get("active", True)) for row in merged_routes),
        "ready_count": sum(row.get("route_state") == "ready_for_optional_unlock" for row in merged_routes),
        "review_count": sum(bool(row.get("needs_review")) for row in merged_routes),
        "blocked_count": sum(bool(row.get("blocked")) for row in merged_routes),
        "question_page_count": sum(len(row.get("question_pdf_pages", [])) for row in merged_sources),
        "visual_verified_route_count": sum(row.get("visual_review_status") == "verified" for row in merged_routes),
        "visual_review_pending_route_count": sum(row.get("visual_review_status") == "pending" for row in merged_routes),
        "visual_review_blocked_route_count": sum(row.get("visual_review_status") == "blocked" for row in merged_routes),
    }
    payload = {
        "schema_version": "math-exam-paper-manifest-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "indexed_with_review" if summary["review_count"] or summary["blocked_count"] else "ready_for_optional_routes",
        "source_root_label": source_root.name,
        "source_root_policy": "runtime parameter; no user absolute path persisted",
        "route_policy": {
            "optional": True,
            "blocks_ybt_progress": False,
            "unlock_granularity": ["after_cycle", "after_multiple_cycles", "after_section", "after_multiple_sections", "after_chapter"],
            "question_authority": "original_question_page",
            "answer_page_is_not_question_authority": True,
            "visual_review_gate": True,
            "visual_review_status_values": ["pending", "verified", "blocked", "not_applicable"],
            "visual_review_rule": "每一道题的所有原卷页必须有明确 verified 声明、有效页图 SHA-256，才可进入 ready_for_optional_unlock",
            "order": ["听完所需课程", "完成对应一本通循环", "完成所有跨循环或跨节前置", "回看原卷页并确认题面", "选做试卷题", "记录错因、题型和迁移方法"],
        },
        "summary": summary,
        "source_inventory_binding": {
            "path": "data/exam_papers/source_inventory.json" if (ROOT / "data/exam_papers/source_inventory.json").is_file() else None,
            "source_sha256s": sorted(str(row.get("sha256") or "") for row in (load_json(ROOT / "data/exam_papers/source_inventory.json", {}) or {}).get("sources", []) if row.get("sha256")) if (ROOT / "data/exam_papers/source_inventory.json").is_file() else [],
            "mapping_rules_path": str(args.rules.relative_to(ROOT)).replace("\\", "/") if args.rules.is_relative_to(ROOT) else str(args.rules),
            "mapping_rules_sha256": sha256_file(args.rules) if args.rules.is_file() else None,
        },
        "sources": merged_sources,
        "routes": merged_routes,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    save_json(args.manifest, payload)
    index_payload = {
        "schema_version": "math-exam-question-index-v1",
        "generated_at": payload["generated_at"],
        "source_manifest": str(args.manifest.relative_to(ROOT)).replace("\\", "/") if args.manifest.is_relative_to(ROOT) else str(args.manifest),
        "sources": [{"source_id": row.get("source_id"), "file_name": row.get("file_name"), "question_ids": row.get("question_ids", [])} for row in merged_sources],
        "questions": merged_routes,
        "by_question_id": {str(row.get("question_id")): row for row in merged_routes},
    }
    save_json(INDEX_PATH, index_payload)
    report = {"schema_version": "math-exam-route-report-v1", "generated_at": payload["generated_at"], "summary": summary, "sources": merged_sources, "route_status_counts": {status: sum(row.get("route_status") == status for row in merged_routes) for status in sorted({str(row.get("route_status")) for row in merged_routes})}}
    save_json(REPORT_PATH, report)
    REPORT_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD_PATH.write_text(markdown_report({**payload, "summary": summary}), encoding="utf-8")
    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUIDE_PATH.write_text(learning_route_guide(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], **summary, "manifest": str(args.manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
