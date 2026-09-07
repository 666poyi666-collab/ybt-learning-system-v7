#!/usr/bin/env python3
"""Bind optional exam routes to exact teacher-transcript evidence.

The exam route manifest already declares which cycles and courses are required,
while ``audit_transcript_utilization.py`` records the transcript evidence for
each cycle/course pair.  This builder joins those two *explicit* contracts; it
never derives a course from a question title, OCR text, filename, or keyword.

The output is deliberately answer-safe.  It contains source hashes, evidence
IDs, method-signal labels, sentence indexes, and timestamp spans, but never
copies transcript sentences or answer content.  A binding is ``verified`` only
when all structural hashes and explicit anchors are valid and the audit's
semantic evidence meets the strict policy below.  ``review`` and ``blocked``
records remain useful to a future MCP client, but must not be presented as a
teacher-method proof or unlock a question.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXAM_MANIFEST = ROOT / "data" / "exam_papers" / "manifest.json"
DEFAULT_AUDIT = ROOT / "reports" / "all_chapters" / "transcript-utilization-current.json"
DEFAULT_CATALOG = ROOT / "data" / "all_chapters_course_catalog.json"
DEFAULT_OUTPUT = ROOT / "data" / "exam_papers" / "transcript_bindings.json"
DEFAULT_REPORT = ROOT / "reports" / "all_chapters" / "exam-transcript-bindings-current.json"

HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _id_list(value: Any) -> list[str]:
    result: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            item = item.get(
                "course_key",
                item.get("cycle_id", item.get("section_id", item.get("id"))),
            )
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _relative_path(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _resolve_transcript(root: Path, row: dict[str, Any]) -> Path:
    """Resolve only to the repository transcript root.

    Historical catalog rows can contain an absolute path from another device.
    We intentionally discard that prefix and use its basename inside the
    current repository.  No arbitrary external path is accepted.
    """

    transcript_root = root / "data" / "course_transcripts"
    raw = str(row.get("transcript_file") or "")
    if raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            local = (root / candidate).resolve()
            if local.is_file() and _relative_path(transcript_root, local) is not None:
                return local
        basename = Path(raw.replace("\\", "/")).name
        local = (transcript_root / basename).resolve()
        if local.is_file() and _relative_path(transcript_root, local) is not None:
            return local
    key = str(row.get("course_key") or "")
    course_id = str(row.get("course_id") or "")
    title = str(row.get("title") or "")
    patterns = [f"{key}.json", f"{course_id} {title}.json", f"{course_id}*.json"]
    for pattern in patterns:
        matches = sorted(transcript_root.glob(pattern))
        if matches:
            return matches[0].resolve()
    return (transcript_root / "__missing__.json").resolve()


def _catalog(root: Path, path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    try:
        payload = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, [f"catalog_read_error:{exc}"]
    if not isinstance(payload, dict):
        return {}, ["catalog_root_not_object"]
    rows: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(_as_list(payload.get("courses"))):
        if not isinstance(raw, dict):
            errors.append(f"catalog_course_{index}_not_object")
            continue
        key = str(raw.get("course_key") or "").strip()
        if not key:
            errors.append(f"catalog_course_{index}_missing_key")
            continue
        if key in rows:
            errors.append(f"catalog_duplicate_course:{key}")
            continue
        rows[key] = raw
    declared = payload.get("course_count")
    if declared is not None:
        try:
            if int(declared) != len(rows):
                errors.append(f"catalog_count_mismatch:{declared}!={len(rows)}")
        except (TypeError, ValueError):
            errors.append("catalog_count_invalid")
    return rows, errors


def _cycle_index(audit: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    index: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for section in _as_list(audit.get("sections")):
        if not isinstance(section, dict):
            errors.append("audit_section_not_object")
            continue
        section_id = str(section.get("section") or section.get("section_id") or "").strip()
        for cycle in _as_list(section.get("cycles")):
            if not isinstance(cycle, dict):
                errors.append(f"audit_cycle_not_object:{section_id}")
                continue
            cycle_id = str(cycle.get("cycle_id") or cycle.get("id") or "").strip()
            if not cycle_id:
                errors.append(f"audit_cycle_missing_id:{section_id}")
                continue
            if cycle_id in index:
                errors.append(f"audit_duplicate_cycle:{cycle_id}")
                continue
            index[cycle_id] = {
                "section_id": section_id,
                "cycle": cycle,
            }
    return index, errors


def _evidence_index(audit: dict[str, Any]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[str]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    errors: list[str] = []
    cycles, cycle_errors = _cycle_index(audit)
    errors.extend(cycle_errors)
    for cycle_id, info in cycles.items():
        cycle = info["cycle"]
        for raw in _as_list(cycle.get("semantic_evidence")):
            if not isinstance(raw, dict):
                errors.append(f"audit_evidence_not_object:{cycle_id}")
                continue
            course_key = str(raw.get("course_key") or "").strip()
            if not course_key:
                errors.append(f"audit_evidence_missing_course:{cycle_id}")
                continue
            index[(cycle_id, course_key)].append(
                {
                    "section_id": info["section_id"],
                    "cycle": cycle,
                    "evidence": raw,
                }
            )
    return index, errors


def _manifest_routes(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    routes = payload.get("routes")
    if isinstance(routes, dict):
        routes = list(routes.values())
    if not isinstance(routes, list):
        return [], ["exam_routes_not_list"]
    result: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            errors.append(f"exam_route_{index}_not_object")
            continue
        route_id = str(route.get("route_id") or route.get("question_id") or "").strip()
        if not route_id:
            errors.append(f"exam_route_{index}_missing_id")
            continue
        result.append(route)
    return result, errors


def _explicit_course_anchors(route: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    """Read only the explicit ``required_courses[].cycle_ids`` contract."""

    required_keys = _id_list(route.get("required_course_keys"))
    raw = route.get("required_courses")
    if not isinstance(raw, list):
        return {}, ["required_courses_not_explicit_list"]
    anchors: dict[str, list[str]] = {key: [] for key in required_keys}
    errors: list[str] = []
    seen_objects: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"required_course_anchor_{index}_not_object")
            continue
        key = str(item.get("course_key") or "").strip()
        if not key:
            errors.append(f"required_course_anchor_{index}_missing_course")
            continue
        if key not in anchors:
            errors.append(f"required_course_anchor_extra:{key}")
            continue
        if key in seen_objects:
            errors.append(f"required_course_anchor_duplicate:{key}")
        seen_objects.add(key)
        cycles = _id_list(item.get("cycle_ids"))
        anchors[key].extend(cycles)
    for key, cycles in anchors.items():
        anchors[key] = list(dict.fromkeys(cycles))
        if not cycles:
            errors.append(f"required_course_anchor_missing_cycle:{key}")
    return anchors, errors


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_spans(evidence: dict[str, Any], duration_s: float | None) -> list[str]:
    reasons: list[str] = []
    timeline = str(evidence.get("timeline_status") or "not_available")
    sentence_indices = evidence.get("sentence_indices")
    spans = evidence.get("time_spans")
    if not isinstance(sentence_indices, list):
        reasons.append("sentence_indices_not_list")
        sentence_indices = []
    if not isinstance(spans, list):
        reasons.append("time_spans_not_list")
        spans = []
    if timeline == "available":
        if not sentence_indices:
            reasons.append("sentence_evidence_missing")
        if not spans:
            reasons.append("time_span_evidence_missing")
        if len(spans) != len(sentence_indices):
            reasons.append("sentence_time_count_mismatch")
        for index in sentence_indices:
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                reasons.append("invalid_sentence_index")
                break
        for span in spans:
            if not isinstance(span, dict) or not _finite_number(span.get("start_s")) or not _finite_number(span.get("end_s")):
                reasons.append("invalid_time_span")
                break
            start = float(span["start_s"])
            end = float(span["end_s"])
            if start < 0 or end < start or duration_s is not None and end > duration_s + 0.01:
                reasons.append("time_span_out_of_bounds")
                break
    elif timeline in {"not_available", "unavailable", "invalid", ""}:
        reasons.append("timeline_unavailable")
    else:
        reasons.append("timeline_status_unknown")
    return list(dict.fromkeys(reasons))


def _project_terms(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in _as_list(evidence.get("matched_topic_terms")):
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term") or "").strip()
        if not term:
            continue
        projected.append(
            {
                "term": term,
                "matched_variant": raw.get("matched_variant"),
                "title_only": bool(raw.get("title_only")),
                "match_count_capped": raw.get("match_count_capped", 0),
            }
        )
    return projected


def _source_anchor_checks(payload: dict[str, Any], evidence: dict[str, Any], duration_s: float | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Re-read actual source rows; in-range timestamps alone are not evidence.

    Character offsets refer to the *raw* full_text (not a normalized copy).
    They are diagnostic pointers, not an independent semantic approval.
    """
    if not isinstance(payload, dict):
        return ["transcript_root_not_object"], []
    reasons: list[str] = []
    offsets: list[dict[str, Any]] = []
    body = str(payload.get("full_text") or "")
    terms = [row for row in _project_terms(evidence) if not row["title_only"]]
    for row in terms:
        variant = str(row.get("matched_variant") or row["term"])
        start = body.lower().find(variant.lower())
        if start < 0:
            reasons.append("transcript_topic_anchor_not_found")
        else:
            offsets.append({"term": row["term"], "start_char": start, "end_char": start + len(variant)})
    if not terms:
        reasons.append("no_substantive_body_anchor")
    indices = evidence.get("sentence_indices")
    spans = evidence.get("time_spans")
    rows = payload.get("sentences")
    rows = rows if isinstance(rows, list) else []
    selected_texts: list[str] = []
    if isinstance(indices, list):
        if len(set(str(index) for index in indices)) != len(indices):
            reasons.append("duplicate_sentence_index")
        for ordinal, index in enumerate(indices):
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(rows):
                reasons.append("invalid_sentence_source_index")
                continue
            row = rows[index]
            if not isinstance(row, dict) or not str(row.get("text") or "").strip():
                reasons.append("transcript_sentence_empty")
                continue
            selected_texts.append(str(row["text"]))
            if not isinstance(spans, list) or ordinal >= len(spans) or not isinstance(spans[ordinal], dict):
                continue
            for source_key, span_key in (("start", "start_s"), ("end", "end_s")):
                raw = row.get(source_key)
                declared = spans[ordinal].get(span_key)
                if not _finite_number(raw) or not _finite_number(declared):
                    reasons.append("invalid_sentence_source_timestamp")
                    continue
                # Same legacy unit contract as the source audit; never infer
                # units from a requested span, which would accept tampering.
                seconds = round(float(raw) / 1000 if duration_s and raw > max(duration_s * 4, 100) else float(raw), 3)
                if abs(seconds - float(declared)) > 0.001:
                    reasons.append("sentence_time_source_mismatch")
    if selected_texts and terms and not any(
        str(term.get("matched_variant") or term["term"]).lower() in sentence.lower()
        for term in terms for sentence in selected_texts
    ):
        reasons.append("transcript_substantive_sentence_anchor_missing")
    signals = evidence.get("signals")
    signals = signals if isinstance(signals, dict) else {}
    for category in _as_list(evidence.get("linked_signal_categories")):
        detail = signals.get(str(category))
        labels = _as_list(detail.get("matched_signal_labels")) if isinstance(detail, dict) else []
        # A claimed category without its original matching labels is not
        # independently reproducible. Keep it in review, never invent labels.
        candidates = selected_texts or [body]
        if not any(str(label).strip() and str(label).lower() in text.lower() for label in labels for text in candidates):
            reasons.append("linked_method_signal_unverified")
    return list(dict.fromkeys(reasons)), offsets


def _is_hard_reason(reason: str) -> bool:
    """Return whether a reason invalidates the evidence structure itself.

    Semantic quality and missing timelines are review conditions.  Hash,
    identity, malformed-source, and missing-anchor failures are hard stops so
    a consumer cannot mistake stale data for a valid transcript binding.
    """

    value = str(reason or "")
    hard_prefixes = (
        "required_",
        "course_not_",
        "anchor_",
        "cycle_not_",
        "course_cycle_evidence_missing",
        "transcript_",
        "audit_",
        "duplicate_",
        "evidence_id_",
        "invalid_",
        "sentence_indices_not_",
        "time_spans_not_",
        "sentence_time_",
        "time_span_",
    )
    return value.startswith(hard_prefixes)


def _reselect_body_sentences(payload: dict[str, Any], evidence: dict[str, Any], duration_s: float | None) -> dict[str, Any]:
    """Recover concrete body pointers omitted by the audit's 16-row cap.

    Only already-declared substantive terms and teaching labels are used.
    This does not invent new topics, approve title-only matches, or repair a
    corrupt original pointer. Every selected sentence contains both a topic
    and a teaching signal in the actual hash-bound source.
    """
    if not isinstance(payload, dict) or evidence.get("timeline_status") != "available":
        return evidence
    old_reasons, _ = _source_anchor_checks(payload, evidence, duration_s)
    allowed = {"transcript_substantive_sentence_anchor_missing", "linked_method_signal_unverified"}
    if _validate_spans(evidence, duration_s) or not old_reasons or any(reason not in allowed for reason in old_reasons):
        return evidence
    terms = [str(row.get("matched_variant") or row["term"]).lower() for row in _project_terms(evidence) if not row["title_only"]]
    signals = evidence.get("signals") or {}
    labels = [str(label).lower() for category in _as_list(evidence.get("linked_signal_categories"))
              for label in _as_list(signals.get(str(category), {}).get("matched_signal_labels")) if str(label).strip()]
    chosen: list[int] = []
    spans: list[dict[str, float]] = []
    for index, row in enumerate(payload.get("sentences") or []):
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").lower()
        if not any(term in text for term in terms) or not any(label in text for label in labels):
            continue
        if not _finite_number(row.get("start")) or not _finite_number(row.get("end")):
            continue
        def seconds(raw):
            return round(float(raw) / 1000 if duration_s and raw > max(duration_s * 4, 100) else float(raw), 3)
        span = {"start_s": seconds(row["start"]), "end_s": seconds(row["end"])}
        if span["start_s"] < 0 or span["end_s"] < span["start_s"] or duration_s and span["end_s"] > duration_s + 0.01:
            continue
        chosen.append(index)
        spans.append(span)
    if not chosen:
        return evidence
    # Keep all concrete intersections (not the old arbitrary first 16 cap).
    # Metadata-only pointers cannot leak answers; clients choose a short excerpt.
    return {**evidence, "sentence_indices": chosen, "time_spans": spans,
            "source_sentence_reselection": True,
            "original_sentence_selection_sha256": canonical_hash({"sentence_indices": evidence.get("sentence_indices"), "time_spans": evidence.get("time_spans")})}


def _project_evidence(
    *,
    root: Path,
    route: dict[str, Any],
    cycle_id: str,
    cycle_info: dict[str, Any],
    course_key: str,
    audit_evidence: dict[str, Any],
    catalog_row: dict[str, Any] | None,
    course_audit: dict[str, Any] | None,
) -> dict[str, Any]:
    cycle = cycle_info["cycle"]
    section_id = cycle_info["section_id"]
    evidence_id = str(audit_evidence.get("evidence_id") or "")
    expected_evidence_id = f"{section_id}:{cycle_id}:{course_key}"
    reasons: list[str] = []
    if evidence_id != expected_evidence_id:
        reasons.append("evidence_id_mismatch")
    catalog_row = catalog_row if isinstance(catalog_row, dict) else {}
    course_audit = course_audit if isinstance(course_audit, dict) else {}
    availability = course_audit.get("availability") if isinstance(course_audit.get("availability"), dict) else {}
    transcript_path = _resolve_transcript(root, catalog_row) if catalog_row else root / "data" / "course_transcripts" / "__missing__.json"
    actual_transcript_hash = sha256_file(transcript_path)
    actual_text_hash: str | None = None
    transcript_payload: dict[str, Any] = {}
    if transcript_path.is_file():
        try:
            transcript_payload = load_json(transcript_path)
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.append("transcript_json_invalid")
    full_text = str(transcript_payload.get("full_text") or "") if isinstance(transcript_payload, dict) else ""
    if full_text:
        actual_text_hash = sha256_text(full_text)
    expected_file_hash = str(catalog_row.get("transcript_sha256") or "").lower() or None
    expected_text_hash = str(catalog_row.get("transcript_text_sha256") or "").lower() or None
    audit_file_hash = str(availability.get("transcript_sha256") or "").lower() or None
    audit_text_hash = str(availability.get("full_text_sha256") or "").lower() or None
    file_hash_match = bool(actual_transcript_hash and expected_file_hash and actual_transcript_hash == expected_file_hash)
    text_hash_match = bool(actual_text_hash and expected_text_hash and actual_text_hash == expected_text_hash)
    audit_file_hash_match = bool(actual_transcript_hash and audit_file_hash and actual_transcript_hash == audit_file_hash)
    audit_text_hash_match = bool(actual_text_hash and audit_text_hash and actual_text_hash == audit_text_hash)
    if availability.get("status") != "available":
        reasons.append("audit_availability_not_available")
    if not transcript_path.is_file():
        reasons.append("transcript_missing")
    if not file_hash_match:
        reasons.append("transcript_sha256_mismatch")
    if not text_hash_match:
        reasons.append("transcript_text_sha256_mismatch")
    if not audit_file_hash_match:
        reasons.append("audit_transcript_sha256_mismatch")
    if not audit_text_hash_match:
        reasons.append("audit_transcript_text_sha256_mismatch")
    duration_raw = transcript_payload.get("duration_s") if isinstance(transcript_payload, dict) else None
    duration_s = float(duration_raw) if _finite_number(duration_raw) else None
    if all((file_hash_match, text_hash_match, audit_file_hash_match, audit_text_hash_match)):
        audit_evidence = _reselect_body_sentences(transcript_payload, audit_evidence, duration_s)
    span_reasons = _validate_spans(audit_evidence, duration_s)
    reasons.extend(span_reasons)
    anchor_reasons, body_offsets = _source_anchor_checks(transcript_payload, audit_evidence, duration_s)
    reasons.extend(anchor_reasons)
    semantic_status = str(audit_evidence.get("status") or "none")
    try:
        substantive_count = int(audit_evidence.get("substantive_match_count") or 0)
    except (TypeError, ValueError):
        substantive_count = 0
        reasons.append("audit_substantive_count_invalid")
    linked_signals = [str(value) for value in _as_list(audit_evidence.get("linked_signal_categories")) if str(value)]
    signal_categories = [str(value) for value in _as_list(audit_evidence.get("signal_categories")) if str(value)]
    # The transcript audit defines ``partial`` as a substantive topic match
    # with at least one linked teaching signal.  That is sufficient for a
    # concrete method pointer; ``full`` is a stronger quality level, not a
    # prerequisite for every legitimate course explanation.
    if semantic_status not in {"partial", "full"}:
        reasons.append("semantic_status_insufficient")
    if substantive_count < 1:
        reasons.append("no_substantive_topic_match")
    if not linked_signals:
        reasons.append("teacher_method_signal_missing")
    evidence = {
        # This key intentionally omits the exam route.  The same cycle/course
        # evidence can support many exam questions and is stored once in the
        # global evidence catalog; routes retain only references to it.
        "evidence_key": canonical_hash(
            {
                "cycle_id": cycle_id,
                "course_key": course_key,
                "evidence_id": evidence_id,
                "transcript_sha256": actual_transcript_hash,
                "transcript_text_sha256": actual_text_hash,
            }
        ),
        "evidence_id": evidence_id,
        "section_id": section_id,
        "cycle_id": cycle_id,
        "cycle_title": cycle.get("title"),
        "course_key": course_key,
        "relation": audit_evidence.get("relation"),
        "semantic_status": semantic_status,
        "evidence_method": audit_evidence.get("evidence_method"),
        "substantive_match_count": substantive_count,
        "matched_topic_terms": _project_terms(audit_evidence),
        "teacher_method_signals": {
            "linked_categories": linked_signals,
            "all_categories": signal_categories,
        },
        "sentence_indices": [value for value in _as_list(audit_evidence.get("sentence_indices")) if isinstance(value, int) and not isinstance(value, bool)],
        "time_spans": [
            {"start_s": span.get("start_s"), "end_s": span.get("end_s")}
            for span in _as_list(audit_evidence.get("time_spans"))
            if isinstance(span, dict)
        ],
        "timeline_status": str(audit_evidence.get("timeline_status") or "not_available"),
        "source_sentence_reselection": bool(audit_evidence.get("source_sentence_reselection")),
        "original_sentence_selection_sha256": audit_evidence.get("original_sentence_selection_sha256"),
        "timeline_verified": not span_reasons and not any("sentence" in reason or "time" in reason for reason in anchor_reasons),
        "body_character_anchors": body_offsets,
        "semantic_source_verified": semantic_status in {"partial", "full"} and substantive_count > 0 and bool(linked_signals) and not any(reason.startswith(("transcript_", "linked_method_")) for reason in anchor_reasons) and bool(body_offsets) and all((file_hash_match, text_hash_match, audit_file_hash_match, audit_text_hash_match)),
        "duration_s": duration_s,
        "transcript_file": _relative_path(root, transcript_path),
        "transcript_sha256": actual_transcript_hash,
        "transcript_text_sha256": actual_text_hash,
        "catalog_transcript_sha256": expected_file_hash,
        "catalog_transcript_text_sha256": expected_text_hash,
        "source_hashes_match": {
            "catalog_file": file_hash_match,
            "catalog_text": text_hash_match,
            "audit_file": audit_file_hash_match,
            "audit_text": audit_text_hash_match,
        },
        "verification_reasons": list(dict.fromkeys(reasons)),
    }
    evidence["eligible_for_teacher_method"] = not reasons
    return evidence


def _current_audit_source_errors(root: Path, audit: dict[str, Any], catalog_path: Path) -> list[str]:
    errors: list[str] = []
    sources = audit.get("sources") if isinstance(audit.get("sources"), dict) else {}
    source_bindings = sources.get("source_bindings") if isinstance(sources.get("source_bindings"), dict) else {}
    expected_catalog_hash = str(source_bindings.get("catalog_sha256") or "").lower()
    actual_catalog_hash = sha256_file(catalog_path)
    if not expected_catalog_hash:
        errors.append("audit_catalog_hash_missing")
    elif actual_catalog_hash != expected_catalog_hash:
        errors.append("audit_catalog_hash_stale")
    manifests = source_bindings.get("manifests") if isinstance(source_bindings.get("manifests"), dict) else {}
    if not manifests:
        errors.append("audit_manifest_hashes_missing")
    else:
        for chapter, expected in manifests.items():
            path = root / f"chapter{chapter}_manifest.json"
            actual = sha256_file(path)
            if actual != str(expected).lower():
                errors.append(f"audit_manifest_hash_stale:{chapter}")
    return errors


def build_bindings(
    project_root: Path | str = ROOT,
    *,
    exam_manifest_path: Path | None = None,
    audit_path: Path | None = None,
    catalog_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    exam_path = (exam_manifest_path or (root / DEFAULT_EXAM_MANIFEST.relative_to(ROOT))).resolve()
    audit_file = (audit_path or (root / DEFAULT_AUDIT.relative_to(ROOT))).resolve()
    catalog_file = (catalog_path or (root / DEFAULT_CATALOG.relative_to(ROOT))).resolve()
    global_errors: list[str] = []
    try:
        exam_payload = load_json(exam_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        exam_payload = {}
        global_errors.append(f"exam_manifest_read_error:{exc}")
    try:
        audit_payload = load_json(audit_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        audit_payload = {}
        global_errors.append(f"transcript_audit_read_error:{exc}")
    if not isinstance(exam_payload, dict):
        exam_payload = {}
        global_errors.append("exam_manifest_root_not_object")
    if not isinstance(audit_payload, dict):
        audit_payload = {}
        global_errors.append("transcript_audit_root_not_object")
    catalog, catalog_errors = _catalog(root, catalog_file)
    global_errors.extend(catalog_errors)
    routes, route_errors = _manifest_routes(exam_payload)
    global_errors.extend(route_errors)
    cycle_index, cycle_errors = _cycle_index(audit_payload)
    evidence_index, evidence_errors = _evidence_index(audit_payload)
    global_errors.extend(cycle_errors)
    global_errors.extend(evidence_errors)
    global_errors.extend(_current_audit_source_errors(root, audit_payload, catalog_file))
    course_audit_index = {
        str(row.get("course_key")): row
        for row in _as_list(audit_payload.get("courses"))
        if isinstance(row, dict) and row.get("course_key")
    }
    route_records: list[dict[str, Any]] = []
    evidence_catalog: dict[str, dict[str, Any]] = {}
    reason_counts: Counter[str] = Counter()
    evidence_count = 0
    eligible_evidence_count = 0
    for route in routes:
        route_id = str(route.get("route_id") or route.get("question_id") or "").strip()
        required_cycles = _id_list(route.get("required_cycle_ids"))
        required_courses = _id_list(route.get("required_course_keys"))
        anchors, anchor_errors = _explicit_course_anchors(route)
        reasons: list[str] = list(anchor_errors)
        if not required_cycles:
            reasons.append("required_cycles_missing")
        if not required_courses:
            reasons.append("required_courses_missing")
        cycle_records: list[dict[str, Any]] = []
        cycle_evidence_ids: dict[str, list[str]] = defaultdict(list)
        for order, cycle_id in enumerate(required_cycles, start=1):
            info = cycle_index.get(cycle_id)
            if not info:
                cycle_records.append(
                    {
                        "order": order,
                        "cycle_id": cycle_id,
                        "section_id": None,
                        "cycle_title": None,
                        "status": "blocked",
                        "course_keys": [],
                        "evidence_ids": [],
                        "reasons": ["cycle_not_in_transcript_audit"],
                    }
                )
                reasons.append(f"cycle_not_in_transcript_audit:{cycle_id}")
                continue
            cycle = info["cycle"]
            # A cycle is covered only by an *explicit* required_courses anchor.
            # Merely finding the same course in the general audit would be an
            # inference and could silently overstate the exam route.
            bound_keys = [
                key for key in required_courses
                if cycle_id in anchors.get(key, []) and (cycle_id, key) in evidence_index
            ]
            cycle_records.append(
                {
                    "order": order,
                    "cycle_id": cycle_id,
                    "section_id": info["section_id"],
                    "cycle_title": cycle.get("title"),
                    "status": "anchored" if bound_keys else "unanchored",
                    "course_keys": bound_keys,
                    "evidence_ids": [],
                    "reasons": [] if bound_keys else ["no_explicit_course_anchor"],
                }
            )
            # Unanchored cycles are commonly practice-only cycles that reuse a
            # course introduced earlier.  They are reported separately and do
            # not invent a transcript binding.
        course_records: list[dict[str, Any]] = []
        for course_key in required_courses:
            anchor_cycles = anchors.get(course_key, [])
            course_reasons: list[str] = []
            if course_key not in catalog:
                course_reasons.append("course_not_in_catalog")
            if course_key not in course_audit_index:
                course_reasons.append("course_not_in_transcript_audit")
            evidence_ids: list[str] = []
            for cycle_id in anchor_cycles:
                info = cycle_index.get(cycle_id)
                candidates = evidence_index.get((cycle_id, course_key), [])
                if not info:
                    course_reasons.append(f"anchor_cycle_not_in_audit:{cycle_id}")
                    continue
                if not candidates:
                    course_reasons.append(f"course_cycle_evidence_missing:{cycle_id}")
                    continue
                candidate_ids = {
                    str(row["evidence"].get("evidence_id") or "") for row in candidates
                }
                # The transcript audit can list the same evidence ID once for
                # a direct relation and once for a prerequisite relation.  It
                # is not an ambiguity: retain one deterministic row.  Distinct
                # evidence IDs for the same anchor remain a review condition.
                if len(candidate_ids) > 1:
                    course_reasons.append(f"duplicate_course_cycle_evidence:{cycle_id}:{course_key}")
                selected = sorted(
                    candidates,
                    key=lambda row: (0 if str(row["evidence"].get("relation") or "") == "direct" else 1, str(row["evidence"].get("evidence_id") or "")),
                )[0]
                evidence = _project_evidence(
                    root=root,
                    route=route,
                    cycle_id=cycle_id,
                    cycle_info=info,
                    course_key=course_key,
                    audit_evidence=selected["evidence"],
                    catalog_row=catalog.get(course_key),
                    course_audit=course_audit_index.get(course_key),
                )
                evidence_key = str(evidence["evidence_key"])
                evidence_catalog.setdefault(evidence_key, evidence)
                evidence_ids.append(evidence_key)
                if not evidence["eligible_for_teacher_method"]:
                    course_reasons.extend(str(reason) for reason in evidence["verification_reasons"])
                cycle_evidence_ids[cycle_id].append(evidence_key)
            course_reasons = list(dict.fromkeys(course_reasons))
            evidence_ids = list(dict.fromkeys(evidence_ids))
            hard_course_reason = any(_is_hard_reason(reason) for reason in course_reasons)
            course_status = "verified" if anchor_cycles and evidence_ids and not course_reasons else ("blocked" if not evidence_ids or hard_course_reason else "review")
            course_records.append(
                {
                    "course_key": course_key,
                    "anchor_cycle_ids": anchor_cycles,
                    "status": course_status,
                    "eligible_for_teacher_method": course_status == "verified",
                    "reasons": course_reasons,
                    "evidence_ids": evidence_ids,
                }
            )
            reasons.extend(f"course:{course_key}:{reason}" for reason in course_reasons)
        for cycle_record in cycle_records:
            cycle_record["evidence_ids"] = list(dict.fromkeys(cycle_evidence_ids.get(cycle_record["cycle_id"], [])))
        reasons = list(dict.fromkeys(reasons))
        structural_block = bool(global_errors) or any(
            reason.startswith(("required_", "course_not_", "anchor_", "cycle_not_", "course_cycle_evidence_missing", "transcript_", "audit_", "duplicate_"))
            for reason in reasons
        ) or any(row.get("status") == "blocked" for row in course_records)
        mapping_state = str(route.get("route_state") or route.get("route_status") or "needs_review")
        if mapping_state in {"blocked", "blocked_external_prerequisite"}:
            reasons.append("route_mapping_blocked")
            structural_block = True
        elif mapping_state in {"needs_review", "candidate"} or str(route.get("mapping_status") or "") != "semantically_verified":
            reasons.append("route_mapping_not_ready")
        reasons = list(dict.fromkeys(reasons))
        for reason in reasons:
            reason_counts[reason.split(":", 1)[0]] += 1
        all_courses_verified = bool(course_records) and all(row["status"] == "verified" for row in course_records)
        all_cycles_bound = bool(cycle_records) and all(
            row["status"] in {"anchored", "unanchored"} for row in cycle_records
        )
        if structural_block:
            binding_status = "blocked"
        elif all_courses_verified and all_cycles_bound and not reasons:
            binding_status = "verified"
        else:
            binding_status = "review"
        unresolved_courses = [row["course_key"] for row in course_records if row["status"] != "verified"]
        unresolved_cycles = [row["cycle_id"] for row in cycle_records if row["status"] == "blocked"]
        unanchored_cycles = [row["cycle_id"] for row in cycle_records if row["status"] == "unanchored"]
        route_records.append(
            {
                "route_id": route_id,
                "question_id": route.get("question_id"),
                "source_id": route.get("source_id"),
                "source_sha256": route.get("source_sha256") or route.get("source_pdf_sha256"),
                "question_number": route.get("question_number"),
                "pdf_page": route.get("pdf_page"),
                "question_ref": route.get("question_ref"),
                "topic_summary": route.get("topic_summary"),
                "required_section_ids": _id_list(route.get("required_section_ids")),
                "route_state": route.get("route_state"),
                "route_status": route.get("route_status"),
                "mapping_status": route.get("mapping_status"),
                "binding_status": binding_status,
                "eligible_for_teacher_method": binding_status == "verified",
                "reasons": reasons,
                "required_cycles": cycle_records,
                "required_courses": course_records,
                "unresolved_cycles": unresolved_cycles,
                "unanchored_cycles": unanchored_cycles,
                "unresolved_courses": unresolved_courses,
            }
        )
    evidence_count = len(evidence_catalog)
    eligible_evidence_count = sum(
        bool(row.get("eligible_for_teacher_method")) for row in evidence_catalog.values()
    )
    summary = {
        "routes": len(route_records),
        "verified_routes": sum(row["binding_status"] == "verified" for row in route_records),
        "review_routes": sum(row["binding_status"] == "review" for row in route_records),
        "blocked_routes": sum(row["binding_status"] == "blocked" for row in route_records),
        "evidence_bindings": evidence_count,
        "eligible_evidence_bindings": eligible_evidence_count,
        "unresolved_courses": sum(len(row["unresolved_courses"]) for row in route_records),
        "unresolved_cycles": sum(len(row["unresolved_cycles"]) for row in route_records),
        "unanchored_cycles": sum(len(row.get("unanchored_cycles", [])) for row in route_records),
        "reason_prefix_counts": dict(sorted(reason_counts.items())),
    }
    source_bindings = {
        "exam_manifest": {"path": _relative_path(root, exam_path), "sha256": sha256_file(exam_path)},
        "transcript_audit": {"path": _relative_path(root, audit_file), "sha256": sha256_file(audit_file)},
        "course_catalog": {"path": _relative_path(root, catalog_file), "sha256": sha256_file(catalog_file)},
    }
    payload_without_fingerprint = {
        "schema_version": "math-exam-transcript-bindings-v1",
        "artifact": "EXAM_ROUTE_TRANSCRIPT_BINDINGS",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "source_authority": "exact_cycle_course_evidence_from_transcript_audit",
            "title_filename_inference": "forbidden",
            "answer_safe": True,
            "verified_requires": [
                "explicit_required_courses_cycle_ids",
                "cycle_and_course_exist_in_audit",
                "catalog_and_audit_transcript_file_sha256_match",
                "catalog_and_audit_full_text_sha256_match",
                "semantic_status_partial_or_full",
                "substantive_topic_match",
                "teacher_method_signal",
                "timestamped_sentence_evidence",
                "all_required_courses_explicitly_anchored",
                "route_mapping_ready",
            ],
            "timeline_unavailable_policy": "review_only; never verified",
            "consumer_rule": "only binding_status=verified may be used as teacher-method proof",
        },
        "sources": source_bindings,
        "summary": summary,
        "global_errors": list(dict.fromkeys(global_errors)),
        "evidence_records": evidence_catalog,
        "routes": route_records,
    }
    fingerprint = canonical_hash({key: value for key, value in payload_without_fingerprint.items() if key != "generated_at"})
    payload_without_fingerprint["binding_fingerprint"] = fingerprint
    if global_errors:
        payload_without_fingerprint["status"] = "blocked"
    elif summary["blocked_routes"]:
        payload_without_fingerprint["status"] = "blocked"
    elif summary["review_routes"]:
        payload_without_fingerprint["status"] = "review_required"
    else:
        payload_without_fingerprint["status"] = "passed"
    return payload_without_fingerprint


def markdown_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# 试卷题目—教师文稿绑定审计",
        "",
        f"整体状态：`{payload.get('status')}`",
        f"绑定指纹：`{payload.get('binding_fingerprint')}`",
        "",
        "> 仅接受试卷路线显式声明的 cycle/course 锚点；标题、文件名和 OCR 关键词不能产生绑定。此报告不含文稿原句或答案。",
        "",
        "| 指标 | 数量 |",
        "|---|---:|",
        f"| 试卷题路线 | {summary.get('routes', 0)} |",
        f"| 严格通过 | {summary.get('verified_routes', 0)} |",
        f"| 需要复核 | {summary.get('review_routes', 0)} |",
        f"| 结构阻塞 | {summary.get('blocked_routes', 0)} |",
        f"| 文稿证据绑定 | {summary.get('evidence_bindings', 0)} |",
        f"| 可作为教师方法证据 | {summary.get('eligible_evidence_bindings', 0)} |",
        "",
        "## 路线状态",
        "",
        "| 题目路线 | 状态 | 未闭合课程 | 未覆盖循环 | 原因（前 3 项） |",
        "|---|---|---:|---:|---|",
    ]
    for route in payload.get("routes", []):
        reasons = "、".join(str(value) for value in route.get("reasons", [])[:3]) or "无"
        lines.append(
            f"| {route.get('route_id')} | {route.get('binding_status')} | "
            f"{len(route.get('unresolved_courses', []))} | {len(route.get('unresolved_cycles', []))} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## 使用边界",
            "",
            "- `verified` 才能作为教师方法讲解的证据入口；`review` 只可展示为待复核候选；`blocked` 不得用于解锁或宣称覆盖。",
            "- 证据行保存转写文件 SHA、全文 SHA、句子索引和时间片；不会复制文稿句子。",
            "- 真实课程消费仍只由学习事件记录，文稿证据绑定不等于用户已听课。",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--exam-manifest", type=Path)
    parser.add_argument("--transcript-audit", type=Path)
    parser.add_argument("--course-catalog", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    payload = build_bindings(
        root,
        exam_manifest_path=args.exam_manifest.resolve() if args.exam_manifest else None,
        audit_path=args.transcript_audit.resolve() if args.transcript_audit else None,
        catalog_path=args.course_catalog.resolve() if args.course_catalog else None,
    )
    output = args.output if args.output.is_absolute() else root / args.output
    report = args.report if args.report.is_absolute() else root / args.report
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.with_suffix(".md").write_text(markdown_report(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "summary": payload["summary"], "output": str(output)}, ensure_ascii=False))
    # Review/blocked routes are an expected audit result.  Exit non-zero only
    # for malformed/stale source contracts; callers can inspect `status`.
    return 1 if payload.get("global_errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
