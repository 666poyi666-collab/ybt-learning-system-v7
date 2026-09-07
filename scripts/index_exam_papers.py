#!/usr/bin/env python3
"""Build a source-page-backed inventory for user-provided exam papers.

The inventory is deliberately conservative. A filename can identify a likely
question paper, but it cannot establish a question's mathematical meaning.
Page OCR is therefore retained as search metadata only; the original page
image (and its hash) remains the authority for a question stem, formula,
option, or diagram.

An allowlist may be a JSON list, a JSON object containing ``files``, ``paths``,
``sha256`` or ``source_ids`` arrays, or a newline-delimited text file. Page
metadata is optional and may contain ``pages`` directly or a ``sources`` map
whose keys are filenames, relative paths, or source ids.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SUPPORTED = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp"}
QUESTION_RE = re.compile(r"(?m)^\s*(?:第\s*)?(\d{1,3})\s*[.．、)）:：]")
QUESTION_PAGE_ROLES = {"question", "question_paper", "stem", "question_stem", "original", "原卷", "题面"}
ANSWER_PAGE_ROLES = {"answer", "answer_only", "analysis", "solution", "答案", "解析"}
VISUAL_VERIFIED_VALUES = {
    "verified", "visually_verified", "vision_verified", "source_page_verified",
    "page_verified", "approved", "passed", "ready",
}
VISUAL_BLOCKED_VALUES = {"blocked", "unavailable", "failed", "invalid", "rejected"}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalise_visual_review_status(value: Any) -> str:
    """Normalize page-review labels without treating OCR as visual proof."""

    if value is True:
        return "verified"
    if value is False or value is None:
        return "pending"
    text = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if text in VISUAL_VERIFIED_VALUES:
        return "verified"
    if text in VISUAL_BLOCKED_VALUES or any(token in text for token in ("block", "fail", "invalid", "unavailable")):
        return "blocked"
    return "pending"


def source_role(name: str) -> str:
    """Classify a filename without granting answer files question authority."""

    compact = re.sub(r"\s+", "", name)
    has_answer = "答案" in compact or "解析" in compact
    has_original = "原卷" in compact or "试卷" in compact or "月考" in compact or "期中" in compact or "期末" in compact
    if has_answer and not ("原卷版" in compact or "含答案" in compact):
        return "answer_only"
    return "question_paper" if has_original else "unclassified"


def _pdfinfo_page_count(path: Path) -> int | None:
    """Use Poppler when available, without making it a hard dependency."""

    try:
        completed = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _empty_pdf_pages(page_count: int | None) -> list[dict[str, Any]]:
    if not page_count:
        return []
    return [
        {
            "pdf_page": page,
            "page_role": "unknown",
            "question_authority": False,
            "text_layer_available": None,
            "text_char_count": None,
            "text_sha256": None,
            "ocr_status": "not_run",
            "ocr_text_sha256": None,
            "ocr_confidence": None,
            "page_image_sha256": None,
            "visual_status": "NEEDS_SOURCE_PAGE_REVIEW",
            "visual_review_status": "pending",
        }
        for page in range(1, page_count + 1)
    ]


def pdf_metadata(path: Path) -> dict[str, Any]:
    """Return page-level text-layer metadata, degrading safely if pypdf is absent."""

    page_count = _pdfinfo_page_count(path)
    pages: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as error:  # one damaged page must not hide the source
                text = ""
                errors.append(type(error).__name__)
            pages.append({
                "pdf_page": index,
                "page_role": "unknown",
                "question_authority": False,
                "text_layer_available": bool(text.strip()),
                "text_char_count": len(text),
                "text_sha256": sha256_bytes(text.encode("utf-8")) if text else None,
                "ocr_status": "not_run",
                "ocr_text_sha256": None,
                "ocr_confidence": None,
                "page_image_sha256": None,
                "visual_status": "NEEDS_SOURCE_PAGE_REVIEW",
                "visual_review_status": "pending",
            })
    except Exception as error:
        errors.append(type(error).__name__)

    if not pages:
        pages = _empty_pdf_pages(page_count)
    result: dict[str, Any] = {
        "page_count": page_count,
        "text_layer_pages": sum(1 for page in pages if page.get("text_layer_available") is True),
        "pages": pages,
    }
    if errors:
        result["metadata_error"] = ",".join(dict.fromkeys(errors))
    return result


def docx_metadata(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            return {
                "has_document_xml": "word/document.xml" in names,
                "embedded_media_count": sum(name.startswith("word/media/") for name in names),
                "pages": [],
            }
    except (OSError, zipfile.BadZipFile) as error:
        return {"metadata_error": type(error).__name__, "pages": []}


def image_metadata(path: Path) -> dict[str, Any]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return {"width": image.width, "height": image.height, "image_format": image.format, "pages": []}
    except Exception as error:
        return {"metadata_error": type(error).__name__, "pages": []}


def _normalise_token(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    return text.casefold().lstrip("./")


def _allowlist_values(raw: Any) -> list[str]:
    """Flatten supported allowlist shapes into match tokens."""

    if isinstance(raw, list):
        return [str(value) for value in raw if isinstance(value, (str, int))]
    if isinstance(raw, str):
        return [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not isinstance(raw, dict):
        return []
    values: list[str] = []
    for key in ("files", "paths", "relative_paths", "file_names", "sha256", "source_ids", "allow", "entries"):
        candidate = raw.get(key)
        if isinstance(candidate, list):
            for value in candidate:
                if isinstance(value, (str, int)):
                    values.append(str(value))
                elif isinstance(value, dict):
                    for field in ("path", "relative_path", "file_name", "sha256", "source_id", "stable_source_id"):
                        if value.get(field):
                            values.append(str(value[field]))
        elif isinstance(candidate, str):
            values.append(candidate)
    if not values:
        values.extend(str(key) for key, enabled in raw.items() if enabled is True and isinstance(key, str))
    return values


def load_allowlist(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"configured": False, "path": None, "tokens": []}
    if not path.is_file():
        raise ValueError(f"allowlist is missing: {path}")
    text = path.read_text(encoding="utf-8-sig")
    try:
        raw: Any = json.loads(text)
    except json.JSONDecodeError:
        raw = text
    tokens = [_normalise_token(value) for value in _allowlist_values(raw)]
    # Selector order has no semantic meaning; sort it so repeated scans and
    # inventory fingerprints remain stable when a user merely reorders lines.
    tokens = sorted(dict.fromkeys(token for token in tokens if token))
    if not tokens:
        raise ValueError(f"allowlist has no selectors: {path}")
    return {"configured": True, "path": str(path), "tokens": tokens}


def source_id_for_hash(source_hash: str) -> str:
    # Keep the established compact id while retaining the full hash in every row.
    return f"exam-{source_hash[:16]}"


def _selector_candidates(path: Path, root: Path, source_hash: str, source_id: str) -> set[str]:
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return {
        _normalise_token(relative),
        _normalise_token(path.name),
        _normalise_token(str(path.resolve())),
        _normalise_token(source_hash),
        _normalise_token(source_id),
    }


def matches_allowlist(path: Path, root: Path, source_hash: str, source_id: str, allowlist: dict[str, Any]) -> bool:
    if not allowlist.get("configured"):
        return True
    candidates = _selector_candidates(path, root, source_hash, source_id)
    return any(fnmatch.fnmatchcase(candidate, token) for token in allowlist.get("tokens", []) for candidate in candidates)


def _allowlist_path_match(path: Path, root: Path, allowlist: dict[str, Any]) -> bool:
    """Fast pre-filter for path selectors before hashing a large Downloads tree."""

    if not allowlist.get("configured"):
        return True
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    candidates = {_normalise_token(relative), _normalise_token(path.name), _normalise_token(str(path.resolve()))}
    tokens = [str(token) for token in allowlist.get("tokens", [])]
    # Wildcard source/hash selectors cannot be resolved from a path; retain a
    # full scan so a later content hash check can select them correctly.
    if any("*" in token or "?" in token for token in tokens if token.casefold().startswith("exam-") or re.fullmatch(r"[0-9a-f*?]{16,64}", token.casefold())):
        return True
    path_tokens = [token for token in tokens if not re.fullmatch(r"(?:exam-)?[0-9a-f]{16,64}", token)]
    # Hash/source-id selectors cannot be resolved without reading the file.
    if not path_tokens:
        return True
    return any(fnmatch.fnmatchcase(candidate, token) for token in path_tokens for candidate in candidates)


def _source_selector_values(source: dict[str, Any]) -> Iterable[str]:
    for key in ("source_id", "stable_source_id", "sha256", "source_pdf_sha256", "relative_path", "file_name", "path"):
        value = source.get(key)
        if value:
            yield _normalise_token(value)


def _select_page_sidecar(raw: Any, path: Path, root: Path, source_hash: str, source_id: str) -> dict[str, Any] | None:
    """Select one source's page metadata from several small sidecar shapes."""

    selectors = _selector_candidates(path, root, source_hash, source_id)
    if isinstance(raw, list):
        return {"pages": raw}
    if not isinstance(raw, dict):
        return None
    direct = {_normalise_token(value) for value in (raw.get("source_id"), raw.get("sha256"), raw.get("file_name"), raw.get("relative_path")) if value}
    if direct and direct.intersection(selectors):
        return raw
    selected: dict[str, Any] | None = None
    for key in ("sources", "papers", "documents"):
        collection = raw.get(key)
        if isinstance(collection, dict):
            for selector, value in collection.items():
                if _normalise_token(selector) in selectors:
                    if isinstance(value, dict):
                        selected = dict(value)
                        break
                    if isinstance(value, list):
                        selected = {"pages": value}
                        break
            else:
                selected = None
            if selected is not None:
                break
        elif isinstance(collection, list):
            for value in collection:
                if isinstance(value, dict) and set(_source_selector_values(value)).intersection(selectors):
                    selected = dict(value)
                    break
            else:
                selected = None
            if selected is not None:
                break
    if selected is not None:
        # question_index.json keeps question records at the top level while
        # source rows only carry question_ids. Attach matching records so the
        # page index can still emit deterministic question identities.
        root_questions = None
        if isinstance(raw, dict):
            root_questions = raw.get("questions") or raw.get("routes")
        if isinstance(root_questions, list):
            source_questions = [
                dict(question) for question in root_questions
                if isinstance(question, dict)
                and (_normalise_token(question.get("source_id")) in selectors or _normalise_token(question.get("source_sha256")) in selectors)
            ]
            if source_questions:
                selected["questions"] = [*selected.get("questions", []), *source_questions]
        return selected
    if isinstance(raw.get("pages"), list):
        return raw
    return None


def _page_number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalise_question_numbers(value: Any) -> list[int]:
    values = value if isinstance(value, list) else [value]
    result: list[int] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("question_number", item.get("number"))
        match = re.search(r"\d+", str(item or ""))
        if match:
            number = int(match.group(0))
            if number > 0:
                result.append(number)
    return list(dict.fromkeys(result))


def _normalise_page_role(value: Any) -> str:
    role = str(value or "unknown").strip().casefold().replace("-", "_").replace(" ", "_")
    if role in QUESTION_PAGE_ROLES:
        return "question"
    if role in ANSWER_PAGE_ROLES:
        return "answer"
    return role or "unknown"


def _sidecar_page_rows(sidecar: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(sidecar, dict):
        return []
    rows = [dict(row) for row in sidecar.get("pages", []) if isinstance(row, dict)]
    # Permit compact range declarations in addition to one row per page.
    role_ranges = {
        "question": (
            sidecar.get("question_pages") or sidecar.get("stem_pages")
            or sidecar.get("question_pdf_pages") or sidecar.get("question_page_ranges")
            or sidecar.get("question_page_range") or sidecar.get("stem_page_range"),
            bool(sidecar.get("question_page_range") or sidecar.get("stem_page_range") or sidecar.get("question_page_ranges")),
        ),
        "answer": (
            sidecar.get("answer_pages") or sidecar.get("analysis_pages")
            or sidecar.get("answer_pdf_pages") or sidecar.get("answer_page_ranges")
            or sidecar.get("answer_page_range") or sidecar.get("analysis_page_range"),
            bool(sidecar.get("answer_page_range") or sidecar.get("analysis_page_range") or sidecar.get("answer_page_ranges")),
        ),
    }
    for role, (values, singular_range) in role_ranges.items():
        if not isinstance(values, list):
            continue
        # Singular *_page_range commonly uses [start, end].
        if singular_range and len(values) == 2 and all(isinstance(item, (int, float, str)) for item in values):
            values = [values]
        for value in values:
            if isinstance(value, (list, tuple)) and len(value) == 2:
                try:
                    start, end = int(value[0]), int(value[1])
                except (TypeError, ValueError):
                    continue
                page_values = range(start, end + 1)
            else:
                page_values = [value]
            for page in page_values:
                number = _page_number(page)
                if number is not None:
                    rows.append({"pdf_page": number, "page_role": role})
    page_roles = sidecar.get("page_roles")
    if isinstance(page_roles, dict):
        for page, role in page_roles.items():
            number = _page_number(page)
            if number is not None:
                rows.append({"pdf_page": number, "page_role": role})
    return rows


def _merge_page_metadata(base_pages: list[dict[str, Any]], sidecar: dict[str, Any] | None, sidecar_root: Path | None) -> list[dict[str, Any]]:
    by_page = {int(page["pdf_page"]): dict(page) for page in base_pages if _page_number(page.get("pdf_page"))}
    for raw in _sidecar_page_rows(sidecar):
        if not isinstance(raw, dict):
            continue
        page_number = _page_number(raw.get("pdf_page", raw.get("page")))
        if page_number is None:
            continue
        merged = {**by_page.get(page_number, {"pdf_page": page_number}), **raw, "pdf_page": page_number}
        if "role" in merged and "page_role" not in raw:
            merged["page_role"] = merged["role"]
        if "source_role" in merged and "page_role" not in raw:
            merged["page_role"] = merged["source_role"]
        merged["page_role"] = _normalise_page_role(merged.get("page_role"))
        merged["question_authority"] = bool(merged.get("question_authority")) or merged["page_role"] == "question"
        if merged["page_role"] in ANSWER_PAGE_ROLES:
            merged["question_authority"] = False
        ocr_text = merged.get("ocr_text")
        if ocr_text and not merged.get("ocr_text_sha256"):
            merged["ocr_text_sha256"] = sha256_bytes(str(ocr_text).encode("utf-8"))
        if ocr_text:
            merged["ocr_status"] = "available"
        elif merged.get("ocr_text_sha256"):
            merged["ocr_status"] = "available_hash_only"
        else:
            merged.setdefault("ocr_status", "not_run")
        image_path = merged.get("page_image_path")
        if image_path and sidecar_root:
            candidate = Path(str(image_path))
            if not candidate.is_absolute():
                candidate = sidecar_root / candidate
            if candidate.is_file():
                merged["page_image_sha256"] = merged.get("page_image_sha256") or sha256_file(candidate)
                merged["page_image_path"] = str(candidate.resolve())
        if "question_numbers" in merged:
            merged["question_numbers"] = _normalise_question_numbers(merged["question_numbers"])
        # ``visual_status`` is a legacy display label; the canonical gate is
        # ``visual_review_status``.  Never infer verification from a text
        # layer, OCR confidence, or a page image alone.
        if merged["page_role"] == "question":
            visual_value = (
                raw.get("visual_review_status")
                if "visual_review_status" in raw
                else raw.get("visual_status")
                if "visual_status" in raw
                else merged.get("visual_review_status", merged.get("visual_status"))
            )
            merged["visual_review_status"] = normalise_visual_review_status(visual_value)
            if merged["visual_review_status"] == "verified":
                merged["visual_status"] = "VISUALLY_VERIFIED"
            elif merged["visual_review_status"] == "blocked":
                merged["visual_status"] = "NEEDS_SOURCE_PAGE_REVIEW"
            else:
                merged["visual_status"] = "NEEDS_SOURCE_PAGE_REVIEW"
        else:
            merged["visual_review_status"] = "not_applicable"
        by_page[page_number] = merged
    # Some generated indexes keep full question records at source level. Move
    # those records onto their page rows so the same stable-ID logic applies.
    if isinstance(sidecar, dict) and isinstance(sidecar.get("questions"), list):
        for question in sidecar["questions"]:
            if not isinstance(question, dict):
                continue
            page_number = _page_number(question.get("pdf_page", question.get("page")))
            if page_number is None:
                continue
            page = by_page.setdefault(page_number, {"pdf_page": page_number, "page_role": "unknown", "question_authority": False})
            page.setdefault("questions", []).append(dict(question))
    for page in by_page.values():
        page["page_role"] = _normalise_page_role(page.get("page_role"))
        page["question_authority"] = bool(page.get("question_authority")) and page["page_role"] == "question"
        if page["page_role"] == "question":
            page["visual_review_status"] = normalise_visual_review_status(
                page.get("visual_review_status", page.get("visual_status"))
            )
        else:
            page["visual_review_status"] = "not_applicable"
    return [by_page[number] for number in sorted(by_page)]


def _question_records(source: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page in source.get("pages", []):
        page_number = _page_number(page.get("pdf_page"))
        if page_number is None:
            continue
        explicit = page.get("questions")
        candidates: list[dict[str, Any]] = []
        if isinstance(explicit, list):
            for value in explicit:
                if isinstance(value, dict):
                    numbers = _normalise_question_numbers([value.get("question_number", value.get("number"))])
                    if numbers:
                        candidates.append({**value, "question_number": numbers[0], "extraction_method": "page_metadata"})
                else:
                    numbers = _normalise_question_numbers(value)
                    if numbers:
                        candidates.append({"question_number": numbers[0], "extraction_method": "page_metadata"})
        numbers = _normalise_question_numbers(page.get("question_numbers"))
        text = str(page.get("ocr_text") or page.get("text") or "")
        if not candidates:
            candidates.extend({"question_number": number, "extraction_method": "text_regex"} for number in numbers)
            if not candidates:
                candidates.extend({"question_number": int(match.group(1)), "extraction_method": "text_regex"} for match in QUESTION_RE.finditer(text))
        seen: dict[int, int] = defaultdict(int)
        for candidate in candidates:
            number = int(candidate["question_number"])
            seen[number] += 1
            occurrence = int(candidate.get("occurrence") or seen[number])
            source_id = str(source["source_id"])
            excerpt = str(candidate.get("ocr_excerpt") or candidate.get("excerpt") or "")
            page_authority = bool(source.get("question_authority")) and bool(page.get("question_authority")) and page.get("page_role") == "question"
            records.append({
                "question_id": f"{source_id}:p{page_number}:q{number}:r{occurrence}",
                "source_id": source_id,
                "source_sha256": source["sha256"],
                "pdf_page": page_number,
                "question_number": number,
                "occurrence": occurrence,
                "question_ref": str(candidate.get("question_ref") or f"第{page_number}页第{number}题"),
                "ocr_excerpt": excerpt[:1200],
                "ocr_excerpt_sha256": sha256_bytes(excerpt.encode("utf-8")) if excerpt else None,
                "source_page_sha256": page.get("page_image_sha256"),
                "visual_status": page.get("visual_status", "NEEDS_SOURCE_PAGE_REVIEW"),
                "visual_review_status": page.get("visual_review_status", "pending"),
                "mapping_status": str(candidate.get("mapping_status") or page.get("mapping_status") or ("candidate" if page_authority else "needs_review")),
                "question_authority": page_authority,
                "page_role": page.get("page_role", "unknown"),
                "extraction_method": candidate.get("extraction_method", "page_metadata"),
            })
    return sorted(records, key=lambda row: (row["pdf_page"], row["question_number"], row["occurrence"]))


def _read_previous(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.is_file():
        raise ValueError(f"previous inventory is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"previous inventory root must be an object: {path}")
    return value


def source_evidence_sha256(row: dict[str, Any]) -> str:
    """Hash page-role/visual/question metadata used by route consumers.

    The PDF hash alone cannot detect a corrected sidecar (for example a page
    role or review decision changing).  Exclude generated paths and timestamps
    so a rename does not look like an evidence mutation.
    """

    pages = []
    for page in row.get("pages", []) if isinstance(row.get("pages"), list) else []:
        if not isinstance(page, dict):
            continue
        pages.append({
            key: page.get(key)
            for key in (
                "pdf_page", "page_role", "question_authority", "page_image_sha256",
                "text_sha256", "ocr_text_sha256", "question_numbers", "visual_status",
                "visual_review_status",
            )
        })
    payload = {
        "sha256": row.get("sha256"),
        "source_role": row.get("source_role"),
        "question_authority": row.get("question_authority"),
        "pages": sorted(pages, key=lambda page: int(page.get("pdf_page") or 0)),
    }
    return sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _incremental_summary(
    previous: dict[str, Any] | None,
    current: list[dict[str, Any]],
    discovered_paths: Iterable[Path] | None = None,
    selected_paths: Iterable[Path] | None = None,
    root: Path | None = None,
    allowlist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, auditable source diff.

    ``--previous`` compares both PDF identity and page evidence.  A source
    disappearing because the allowlist changed is reported as ``deallowed``
    rather than as a destructive removal.  The optional positional arguments
    preserve compatibility with callers that used the old two-argument helper.
    """

    current_rows = [row for row in current if isinstance(row, dict) and row.get("source_id")]
    current_by_id = {str(row.get("source_id")): row for row in current_rows}
    current_allowlist_tokens = sorted(str(token) for token in (allowlist or {}).get("tokens", []) if str(token))
    current_allowlist_hash = (
        sha256_bytes(json.dumps(current_allowlist_tokens, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if allowlist and allowlist.get("configured")
        else None
    )
    if previous is None:
        new_ids = sorted(current_by_id)
        result = {
            "mode": "full",
            "new_source_ids": new_ids,
            "unchanged_source_ids": [],
            "changed_source_ids": [],
            "evidence_changed_source_ids": [],
            "path_changed_source_ids": [],
            "removed_source_ids": [],
            "deallowed_source_ids": [],
            "replaced_sources": [],
            "source_changes": [
                {"status": "new", "source_id": source_id}
                for source_id in new_ids
            ],
            "allowlist_changed": False,
            "allowlist_added_tokens": current_allowlist_tokens,
            "allowlist_removed_tokens": [],
            "previous_inventory_sha256": None,
        }
        if current_allowlist_hash:
            result["allowlist_sha256"] = current_allowlist_hash
        return result

    old_rows = previous.get("sources", []) if isinstance(previous, dict) else []
    if not isinstance(old_rows, list):
        old_rows = []
    old_rows = [row for row in old_rows if isinstance(row, dict) and row.get("source_id")]
    old_by_id = {str(row.get("source_id")): row for row in old_rows}
    new_ids = set(current_by_id) - set(old_by_id)
    removed_ids = set(old_by_id) - set(current_by_id)
    unchanged_ids: set[str] = set()
    changed_ids: set[str] = set()
    evidence_changed_ids: set[str] = set()
    path_changed_ids: set[str] = set()
    replaced_sources: list[dict[str, Any]] = []
    source_changes: list[dict[str, Any]] = []

    def row_path(row: dict[str, Any]) -> str:
        return _normalise_token(str(row.get("relative_path") or row.get("file_name") or ""))

    old_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in old_rows:
        if row_path(row):
            old_by_path[row_path(row)].append(row)
    for row in current_rows:
        if row_path(row):
            current_by_path[row_path(row)].append(row)

    # Same path with a new full hash is an explicit replacement.  Pairing here
    # prevents it from being reported as an unrelated add+remove event.
    for path_key in sorted(set(old_by_path) & set(current_by_path)):
        old_row = old_by_path[path_key][0]
        current_row = current_by_path[path_key][0]
        old_hash = str(old_row.get("sha256") or "").lower()
        current_hash = str(current_row.get("sha256") or "").lower()
        if old_hash != current_hash:
            old_id = str(old_row.get("source_id"))
            current_id = str(current_row.get("source_id"))
            removed_ids.discard(old_id)
            new_ids.discard(current_id)
            changed_ids.add(current_id)
            replacement = {
                "old_source_id": old_id,
                "new_source_id": current_id,
                "relative_path": str(current_row.get("relative_path") or current_row.get("file_name") or ""),
                "old_sha256": old_hash,
                "new_sha256": current_hash,
                "status": "changed",
            }
            replaced_sources.append(replacement)
            source_changes.append(replacement)

    for source_id in sorted(set(current_by_id) & set(old_by_id)):
        old_row, current_row = old_by_id[source_id], current_by_id[source_id]
        old_hash = str(old_row.get("sha256") or old_row.get("source_pdf_sha256") or "").lower()
        current_hash = str(current_row.get("sha256") or current_row.get("source_pdf_sha256") or "").lower()
        if old_hash != current_hash:
            changed_ids.add(source_id)
            source_changes.append({"status": "changed", "source_id": source_id, "old_sha256": old_hash, "new_sha256": current_hash})
            continue
        old_evidence = str(old_row.get("evidence_sha256") or source_evidence_sha256(old_row))
        current_evidence = str(current_row.get("evidence_sha256") or source_evidence_sha256(current_row))
        if old_evidence != current_evidence:
            evidence_changed_ids.add(source_id)
            changed_ids.add(source_id)
            source_changes.append({"status": "evidence_changed", "source_id": source_id, "evidence_sha256": current_evidence})
        else:
            unchanged_ids.add(source_id)
            if row_path(old_row) != row_path(current_row):
                path_changed_ids.add(source_id)
                source_changes.append({"status": "renamed", "source_id": source_id, "old_path": row_path(old_row), "new_path": row_path(current_row)})

    # Detect old rows omitted solely because the current allowlist filtered the
    # path.  This requires no extra hashing and is safe for path selectors.
    discovered_keys = {
        _normalise_token(path.relative_to(root).as_posix() if root is not None and path.is_relative_to(root) else path.name)
        for path in (discovered_paths or [])
    }
    selected_keys = {
        _normalise_token(path.relative_to(root).as_posix() if root is not None and path.is_relative_to(root) else path.name)
        for path in (selected_paths or [])
    }
    deallowed_ids: set[str] = set()
    if discovered_keys:
        for source_id in sorted(removed_ids):
            old_path = row_path(old_by_id[source_id])
            if old_path in discovered_keys and old_path not in selected_keys:
                deallowed_ids.add(source_id)
        removed_ids -= deallowed_ids
        source_changes.extend({"status": "deallowed", "source_id": source_id, "path": row_path(old_by_id[source_id])} for source_id in sorted(deallowed_ids))

    # A source that is present in both snapshots can still change eligibility
    # when a selector is edited.  Keep this visible to callers.
    newly_allowlisted: set[str] = set()
    currently_deallowed: set[str] = set()
    for source_id in sorted(set(current_by_id) & set(old_by_id)):
        old_allowed = bool(old_by_id[source_id].get("allowlisted"))
        current_allowed = bool(current_by_id[source_id].get("allowlisted"))
        if old_allowed != current_allowed:
            if current_allowed:
                newly_allowlisted.add(source_id)
            else:
                currently_deallowed.add(source_id)
            source_changes.append({"status": "allowlist_enabled" if current_allowed else "allowlist_disabled", "source_id": source_id})

    old_allowlist = previous.get("allowlist", {}) if isinstance(previous, dict) else {}
    old_tokens = sorted(str(token) for token in old_allowlist.get("tokens", []) if str(token))
    # Older inventories only persisted selector_count/path.  In that case a
    # hash is unavailable; expose ``unknown`` instead of claiming unchanged.
    old_allowlist_hash = previous.get("allowlist_sha256") or old_allowlist.get("sha256")
    allowlist_changed = bool(old_tokens or current_allowlist_tokens or old_allowlist_hash or current_allowlist_hash) and (
        old_tokens != current_allowlist_tokens
        or (old_allowlist_hash and current_allowlist_hash and old_allowlist_hash != current_allowlist_hash)
        or bool(old_allowlist.get("configured")) != bool((allowlist or {}).get("configured"))
    )
    result = {
        "mode": "incremental",
        "new_source_ids": sorted(new_ids),
        "unchanged_source_ids": sorted(unchanged_ids),
        "changed_source_ids": sorted(changed_ids),
        "evidence_changed_source_ids": sorted(evidence_changed_ids),
        "path_changed_source_ids": sorted(path_changed_ids),
        "removed_source_ids": sorted(removed_ids),
        "deallowed_source_ids": sorted(deallowed_ids),
        "newly_allowlisted_source_ids": sorted(newly_allowlisted),
        "currently_deallowed_source_ids": sorted(currently_deallowed),
        "replaced_sources": sorted(replaced_sources, key=lambda row: (row.get("relative_path", ""), row.get("new_source_id", ""))),
        "source_changes": sorted(source_changes, key=lambda row: (str(row.get("source_id") or row.get("new_source_id") or ""), str(row.get("status") or ""))),
        "allowlist_changed": allowlist_changed,
        "allowlist_added_tokens": sorted(set(current_allowlist_tokens) - set(old_tokens)),
        "allowlist_removed_tokens": sorted(set(old_tokens) - set(current_allowlist_tokens)),
        "previous_inventory_sha256": previous.get("inventory_sha256") if isinstance(previous, dict) else None,
    }
    if current_allowlist_hash:
        result["allowlist_sha256"] = current_allowlist_hash
    return result


def _build_source_row(path: Path, root: Path, source_hash: str, allowlist: dict[str, Any], page_payload: Any, page_metadata_root: Path | None) -> dict[str, Any]:
    source_id = source_id_for_hash(source_hash)
    role = source_role(path.name)
    metadata = pdf_metadata(path) if path.suffix.lower() == ".pdf" else docx_metadata(path) if path.suffix.lower() == ".docx" else image_metadata(path)
    sidecar = _select_page_sidecar(page_payload, path, root, source_hash, source_id) if page_payload is not None else None
    pages = _merge_page_metadata(list(metadata.get("pages", [])), sidecar, page_metadata_root)
    if pages and not metadata.get("page_count"):
        metadata["page_count"] = len(pages)
    allowlisted = matches_allowlist(path, root, source_hash, source_id, allowlist)
    row: dict[str, Any] = {
        "source_id": source_id,
        "stable_source_id": f"exam-{source_hash}",
        "relative_path": path.relative_to(root).as_posix(),
        "file_name": path.name,
        "extension": path.suffix.lower(),
        "size_bytes": path.stat().st_size,
        "sha256": source_hash,
        "source_role": role,
        "question_authority": role == "question_paper",
        "allowlisted": allowlisted,
        "included_for_routes": bool(allowlisted and role == "question_paper"),
        "pages": pages,
        **{key: value for key, value in metadata.items() if key != "pages"},
    }
    row["questions"] = _question_records(row)
    row["question_pages_verified"] = sum(1 for page in pages if page.get("question_authority") is True)
    row["answer_pages_detected"] = sum(1 for page in pages if page.get("page_role") == "answer")
    row["authoritative_question_count"] = sum(1 for question in row["questions"] if question.get("question_authority") is True)
    question_pages = [page for page in pages if page.get("page_role") == "question"]
    row["visual_review_status"] = (
        "blocked" if any(page.get("visual_review_status") == "blocked" for page in question_pages)
        else "verified" if question_pages and all(page.get("visual_review_status") == "verified" for page in question_pages)
        else "pending"
    )
    row["visual_reviewed_question_page_count"] = sum(
        page.get("visual_review_status") == "verified" for page in question_pages
    )
    row["visual_review_pending_question_pages"] = [
        page.get("pdf_page") for page in question_pages if page.get("visual_review_status") == "pending"
    ]
    row["visual_review_blocked_question_pages"] = [
        page.get("pdf_page") for page in question_pages if page.get("visual_review_status") == "blocked"
    ]
    row["route_ready"] = bool(row["included_for_routes"] and row["question_pages_verified"] > 0)
    # ``route_ready`` historically means that a question-page candidate can be
    # emitted.  Keep it for compatibility and expose the stricter gate under a
    # separate field so callers cannot mistake candidate discovery for review.
    row["visual_review_ready"] = bool(row["route_ready"] and row["visual_review_status"] == "verified")
    row["evidence_sha256"] = source_evidence_sha256(row)
    return row


def _refresh_source_review_fields(row: dict[str, Any]) -> None:
    """Recompute page/route readiness after same-SHA alias merging."""

    pages = [page for page in row.get("pages", []) if isinstance(page, dict)]
    question_pages = [page for page in pages if page.get("page_role") == "question"]
    row["question_pages_verified"] = sum(page.get("question_authority") is True for page in pages)
    row["answer_pages_detected"] = sum(page.get("page_role") == "answer" for page in pages)
    row["authoritative_question_count"] = sum(
        question.get("question_authority") is True
        for question in row.get("questions", [])
        if isinstance(question, dict)
    )
    row["visual_review_status"] = (
        "blocked" if any(page.get("visual_review_status") == "blocked" for page in question_pages)
        else "verified" if question_pages and all(page.get("visual_review_status") == "verified" for page in question_pages)
        else "pending"
    )
    row["visual_reviewed_question_page_count"] = sum(
        page.get("visual_review_status") == "verified" for page in question_pages
    )
    row["visual_review_pending_question_pages"] = [
        page.get("pdf_page") for page in question_pages if page.get("visual_review_status") == "pending"
    ]
    row["visual_review_blocked_question_pages"] = [
        page.get("pdf_page") for page in question_pages if page.get("visual_review_status") == "blocked"
    ]
    row["route_ready"] = bool(row.get("included_for_routes") and row.get("question_pages_verified", 0) > 0)
    row["visual_review_ready"] = bool(row["route_ready"] and row["visual_review_status"] == "verified")
    row["evidence_sha256"] = source_evidence_sha256(row)


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_hash = str(row["sha256"])
        existing = grouped.get(source_hash)
        if existing is None:
            row["aliases"] = []
            grouped[source_hash] = row
            continue
        # Prefer a filename classified as an original question paper when
        # aliases share bytes (for example ``试卷.pdf`` and ``答案.pdf``).
        if row.get("source_role") == "question_paper" and existing.get("source_role") != "question_paper":
            row["aliases"] = list(existing.get("aliases", []))
            row["aliases"].append({"relative_path": existing["relative_path"], "file_name": existing["file_name"], "size_bytes": existing["size_bytes"]})
            row["allowlisted"] = bool(row.get("allowlisted") or existing.get("allowlisted"))
            row["included_for_routes"] = bool(row.get("included_for_routes") or existing.get("included_for_routes"))
            _refresh_source_review_fields(row)
            grouped[source_hash] = row
            continue
        existing["aliases"].append({"relative_path": row["relative_path"], "file_name": row["file_name"], "size_bytes": row["size_bytes"]})
        existing["allowlisted"] = bool(existing.get("allowlisted") or row.get("allowlisted"))
        existing["included_for_routes"] = bool(existing.get("included_for_routes") or row.get("included_for_routes"))
        if row.get("source_role") == "question_paper" and existing.get("source_role") != "question_paper":
            existing["source_role"] = "question_paper"
            existing["question_authority"] = True
            existing["questions"] = _question_records(existing)
        _refresh_source_review_fields(existing)
    return [grouped[key] for key in sorted(grouped)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Index original exam papers with page/hash evidence and stable question ids.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("data/exam_papers/source_inventory.json"))
    parser.add_argument("--allowlist", "--math-allowlist", dest="allowlist", type=Path, help="JSON or newline-delimited selectors for permitted original math papers")
    parser.add_argument("--page-metadata", "--page-index", "--page-sidecar", dest="page_metadata", type=Path, help="optional OCR/page-image metadata JSON sidecar")
    parser.add_argument("--previous", "--previous-inventory", dest="previous", type=Path, help="previous inventory for incremental diff")
    parser.add_argument("--diff-output", type=Path, help="write a compact machine-readable incremental diff report (default: <output>.diff.json)")
    parser.add_argument("--fail-on-change", action="store_true", help="return exit code 2 when new/changed/removed/deallowed sources are detected")
    args = parser.parse_args()
    root = args.source_root.resolve()
    if not root.is_dir():
        raise SystemExit(f"source root is missing: {root}")
    try:
        allowlist = load_allowlist(args.allowlist.resolve() if args.allowlist else None)
        previous = _read_previous(args.previous.resolve() if args.previous else None)
        page_payload = json.loads(args.page_metadata.read_text(encoding="utf-8-sig")) if args.page_metadata else None
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    page_metadata_root = args.page_metadata.resolve().parent if args.page_metadata else None

    discovered_paths = sorted((item for item in root.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED), key=lambda item: item.as_posix().casefold())
    # With an explicit allowlist, do not hash unrelated media and downloaded
    # books. Hash selectors (SHA/source id) still require a full scan because
    # their match can only be known after reading a file.
    paths = [path for path in discovered_paths if _allowlist_path_match(path, root, allowlist)]
    rows = [_build_source_row(path, root, sha256_file(path), allowlist, page_payload, page_metadata_root) for path in paths]
    rows = _deduplicate(rows)
    for row in rows:
        row["questions"] = _question_records(row)
    selected = [row for row in rows if row.get("included_for_routes")]
    payload = {
        "schema_version": "math-exam-source-inventory-v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root_label": root.name,
        "source_count": len(rows),
        "file_count": len(paths),
        "discovered_file_count": len(discovered_paths),
        "question_paper_count": sum(row["source_role"] == "question_paper" for row in rows),
        "selected_question_paper_count": len(selected),
        "answer_only_count": sum(row["source_role"] == "answer_only" for row in rows),
        "excluded_count": sum(not row.get("included_for_routes") for row in rows),
        "question_count": sum(len(row.get("questions", [])) for row in rows),
        "authoritative_question_count": sum(sum(1 for question in row.get("questions", []) if question.get("question_authority") is True) for row in rows),
        "selected_source_ids": [row["source_id"] for row in selected],
        "allowlist": {
            "configured": bool(allowlist.get("configured")),
            "path": allowlist.get("path"),
            "selector_count": len(allowlist.get("tokens", [])),
            "tokens": list(allowlist.get("tokens", [])),
            "filtered_file_count": len(discovered_paths) - len(paths),
        },
        "incremental": _incremental_summary(
            previous,
            rows,
            discovered_paths=discovered_paths,
            selected_paths=paths,
            root=root,
            allowlist=allowlist,
        ),
        "evidence_policy": {
            "source_page_is_question_authority": True,
            "ocr_text_is_search_aid_only": True,
            "answer_only_sources_cannot_supply_question_stems": True,
            "unmapped_questions_require_review": True,
            "visual_review_required_before_route_unlock": True,
            "visual_review_status_values": ["pending", "verified", "blocked", "not_applicable"],
        },
        "sources": rows,
    }
    # Stable inventory identity for downstream import evidence.  Timestamps
    # and the diff report are intentionally excluded so a repeat scan of the
    # same inputs has the same fingerprint.
    fingerprint_payload = {
        key: value for key, value in payload.items()
        if key not in {"generated_at", "incremental", "inventory_sha256"}
    }
    payload["inventory_sha256"] = sha256_bytes(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    payload["incremental"]["current_inventory_sha256"] = payload["inventory_sha256"]
    if allowlist.get("configured"):
        payload["allowlist"]["sha256"] = sha256_bytes(
            json.dumps(sorted(payload["allowlist"].get("tokens", [])), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    diff_path = args.diff_output.resolve() if args.diff_output else args.output.with_suffix(".diff.json")
    diff_payload = {
        "schema_version": "math-exam-source-diff-v1",
        "generated_at": payload["generated_at"],
        "source_root_label": payload["source_root_label"],
        "inventory_sha256": payload["inventory_sha256"],
        "incremental": payload["incremental"],
        "counts": {
            "sources": payload["source_count"],
            "files_scanned": payload["file_count"],
            "files_discovered": payload["discovered_file_count"],
            "questions": payload["question_count"],
        },
    }
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(json.dumps(diff_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {key: payload[key] for key in ("source_count", "file_count", "question_paper_count", "selected_question_paper_count", "answer_only_count", "question_count", "authoritative_question_count")}
    summary["inventory_sha256"] = payload["inventory_sha256"]
    summary["incremental"] = {
        key: payload["incremental"].get(key, [])
        for key in ("mode", "new_source_ids", "unchanged_source_ids", "changed_source_ids", "evidence_changed_source_ids", "removed_source_ids", "deallowed_source_ids")
    }
    summary["diff_output"] = str(diff_path)
    print(json.dumps(summary, ensure_ascii=False))
    if args.fail_on_change:
        diff = payload["incremental"]
        changed = bool(diff.get("allowlist_changed")) or any(
            diff.get(key)
            for key in (
                "new_source_ids", "changed_source_ids", "evidence_changed_source_ids",
                "removed_source_ids", "deallowed_source_ids", "newly_allowlisted_source_ids",
                "currently_deallowed_source_ids",
            )
        )
        return 2 if changed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
