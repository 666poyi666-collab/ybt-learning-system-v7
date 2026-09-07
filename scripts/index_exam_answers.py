#!/usr/bin/env python3
"""Build an isolated, source-bound answer evidence index for exam papers.

The normal exam index intentionally excludes answer pages from learner
contexts.  This companion index keeps those pages in a separate evidence
plane so a teacher/model can compare a learner attempt with the reference
solution only after an explicit request.  A detected number is a locator, not
proof of correctness: every record remains review-required and cannot be used
for automatic grading.

The command is deliberately fail-closed.  A missing source, hash mismatch,
unreadable page, ambiguous question number, or unavailable OCR never gets
silently attached to another question.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "math-exam-answer-manifest-v1"
ANSWER_LABEL_RE = re.compile(
    r"(?m)^\s*(?:第\s*)?(\d{1,3})\s*[.．、:：)）]\s*"
)
ANSWER_SIGNAL_RE = re.compile(
    r"答案|解析|解答|证明|故选|故答案|解：|解:|详解|点评|评"
)
ANSWER_HEADING_MARKER_RE = re.compile(r"(?:【\s*(?:答\s*案|解\s*答)|^\s*(?:答\s*案|解\s*答)|（?\s*\d+\s*分)")
TABLE_HEADER_RE = re.compile(r"^\s*题\s*号\s*$")
TABLE_ANSWER_RE = re.compile(r"^\s*答\s*案\s*$")
TABLE_OPTION_RE = re.compile(r"^[A-DＡ-Ｄ](?:\s*[A-DＡ-Ｄ]){0,3}$")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalise_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\r", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def marker_text(value: str) -> str:
    """Compact spaces inserted between Chinese answer-marker characters."""

    return re.sub(r"答\s*案", "答案", re.sub(r"解\s*析", "解析", value))


def compact_line(value: str) -> str:
    return re.sub(r"\s+", "", marker_text(value))


def _source_list(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    value = manifest.get("sources", [])
    if isinstance(value, dict):
        value = list(value.values())
    return [item for item in value if isinstance(item, dict)]


def _route_list(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    value = manifest.get("routes", [])
    if isinstance(value, dict):
        value = list(value.values())
    return [item for item in value if isinstance(item, dict)]


def _answer_pages(source: dict[str, Any]) -> list[int]:
    pages: set[int] = set()
    for value in source.get("answer_pdf_pages", []) or []:
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.add(page)
    for page in source.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        role = str(page.get("page_role", "")).strip().casefold()
        if role in {"answer", "answer_only", "analysis", "solution", "答案", "解析"}:
            try:
                number = int(page.get("pdf_page"))
            except (TypeError, ValueError):
                continue
            if number > 0:
                pages.add(number)
    return sorted(pages)


def _expected_questions(routes: Iterable[dict[str, Any]], source_id: str) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for route in routes:
        if route.get("active", True) is False or str(route.get("source_id", "")) != source_id:
            continue
        try:
            number = int(route.get("question_number"))
        except (TypeError, ValueError):
            continue
        question_id = str(route.get("question_id") or route.get("route_id") or "").strip()
        if not question_id:
            continue
        result.setdefault(number, []).append(question_id)
    return result


def resolve_source_path(source: dict[str, Any], source_root: Path, manifest_path: Path) -> Path | None:
    """Resolve a source without persisting a machine-specific absolute path."""

    names = []
    for key in ("relative_path", "file_name"):
        value = str(source.get(key, "")).strip().replace("\\", "/")
        if value and value not in names:
            names.append(value)
    candidates: list[Path] = []
    for name in names:
        candidate = Path(name)
        if candidate.is_absolute():
            candidates.append(candidate)
        else:
            candidates.extend((source_root / candidate, manifest_path.parent / candidate))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def extract_pdf_text(path: Path) -> tuple[list[str], str | None]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        texts: list[str] = []
        for page in reader.pages:
            try:
                texts.append(normalise_text(page.extract_text() or ""))
            except Exception:
                texts.append("")
        return texts, None
    except Exception as error:  # pragma: no cover - depends on local runtime
        return [], type(error).__name__


def open_pdf(path: Path) -> Any | None:
    try:
        try:
            import pymupdf as pdf_module
        except ImportError:
            import fitz as pdf_module  # type: ignore
        return pdf_module.open(str(path))
    except Exception:
        return None


def render_answer_page(document: Any, page_number: int, destination: Path, dpi: int) -> tuple[str | None, str | None, str | None]:
    if document is None:
        return None, None, "renderer_unavailable"
    try:
        page = document[page_number - 1]
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        data = pixmap.tobytes("jpg", jpg_quality=90)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return str(destination), sha256_bytes(data), None
    except Exception as error:  # pragma: no cover - damaged PDF dependent
        return None, None, type(error).__name__


def ocr_image(path: Path) -> tuple[str, float | None, str]:
    try:
        from rapidocr_onnxruntime import RapidOCR

        result, _ = RapidOCR()(str(path))
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
    except Exception as error:  # pragma: no cover - optional OCR dependency
        return "", None, f"unavailable:{type(error).__name__}"


def parse_answer_blocks(
    page_texts: list[str],
    expected_numbers: Iterable[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Locate answer blocks and page spans using explicit printed numbers.

    Only punctuation-delimited number headings are accepted.  Number-only
    table rows (the common answer summary table) are intentionally ignored;
    this prevents a table row or a formula line from becoming an answer.
    """

    expected = set(int(number) for number in expected_numbers)
    candidates: list[dict[str, Any]] = []
    for page_index, raw in enumerate(page_texts, start=1):
        text = normalise_text(raw)
        if not text:
            continue
        for match in ANSWER_LABEL_RE.finditer(text):
            number = int(match.group(1))
            if number not in expected:
                continue
            # A heading must carry a score/answer marker on the same line or
            # immediately following lines.  This rejects formula fragments
            # such as ``2.`` and page-footer fractions that otherwise look
            # like question numbers.
            line_end = text.find("\n", match.end())
            line_end = len(text) if line_end < 0 else line_end
            line = text[match.start():line_end]
            following = text[match.end(): min(len(text), match.end() + 180)]
            next_nonempty = next((part.strip() for part in following.splitlines() if part.strip()), "")
            # Only the heading line and its immediate continuation may grant
            # heading status.  Looking several lines ahead misclassifies a
            # conclusion such as ``故选：B`` inside the previous answer.
            explicit_heading = bool(
                ANSWER_HEADING_MARKER_RE.search(marker_text(line))
                or ANSWER_HEADING_MARKER_RE.search(marker_text(next_nonempty))
            )
            has_signal_after = explicit_heading
            # Some Chinese PDF producers place the final printed number after
            # the answer text in the extraction stream (the visual heading is
            # still at the top of the page).  Accept that shape only when the
            # preceding page tail contains an answer marker and the number is
            # close to the end; it remains review-required downstream.
            reversed_heading = (
                not has_signal_after
                and match.start() >= max(0, len(text) - 80)
                and bool(re.search(r"答\s*案", marker_text(text[:match.start()])))
            )
            if not has_signal_after and not reversed_heading:
                continue
            candidates.append({
                "number": number,
                "page": page_index,
                # For reversed streams the answer body precedes the heading.
                "start": 0 if reversed_heading else match.start(),
                "heading_start": match.start(),
                "text": text,
                "reversed_heading": reversed_heading,
            })

    # Keep the first unambiguous heading for each (page, number). Duplicate
    # headings are retained as ambiguity evidence and never auto-bound.
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate["page"], candidate["number"]), []).append(candidate)
    ambiguities: list[str] = []
    unique_candidates: list[dict[str, Any]] = []
    for key, values in grouped.items():
        if len(values) != 1:
            ambiguities.append(f"page-{key[0]}:question-{key[1]}:duplicate-heading")
            continue
        unique_candidates.append(values[0])

    unique_candidates.sort(key=lambda item: (item["page"], item["start"]))
    blocks: list[dict[str, Any]] = []
    for index, candidate in enumerate(unique_candidates):
        next_candidate = unique_candidates[index + 1] if index + 1 < len(unique_candidates) else None
        start_page = int(candidate["page"])
        end_page = int(next_candidate["page"]) if next_candidate else len(page_texts)
        # Extract the tail of the start page and all complete continuation
        # pages.  A block ending at a page boundary is valid evidence even if
        # the next heading is on a later page.
        chunks: list[str] = []
        first_text = str(candidate["text"])
        chunks.append(first_text[int(candidate["start"]):])
        for page_number in range(start_page + 1, end_page + 1):
            if page_number <= len(page_texts):
                chunks.append(normalise_text(page_texts[page_number - 1]))
        if next_candidate and int(next_candidate["page"]) == start_page:
            # Trim to the next heading when two answers share a page.
            trim_at = int(next_candidate.get("heading_start", next_candidate["start"])) if next_candidate.get("reversed_heading") else int(next_candidate["start"])
            chunks[0] = first_text[int(candidate["start"]): trim_at]
        elif next_candidate and int(next_candidate["page"]) > start_page:
            # The final continuation page belongs to the next answer's page;
            # include only the prefix before its heading.  This preserves a
            # solution that crosses a PDF page boundary without stealing the
            # next question's answer body.
            chunks = [chunks[0]] + [normalise_text(page_texts[p - 1]) for p in range(start_page + 1, int(next_candidate["page"]))]
            next_page_text = normalise_text(page_texts[int(next_candidate["page"]) - 1])
            if next_page_text and not next_candidate.get("reversed_heading"):
                chunks.append(next_page_text[: int(next_candidate.get("start", 0))])
        answer_text = normalise_text("\n".join(chunk for chunk in chunks if chunk))
        page_end = start_page if next_candidate and int(next_candidate["page"]) == start_page else (
            int(next_candidate["page"]) if next_candidate and int(next_candidate["page"]) > start_page and not next_candidate.get("reversed_heading")
            else int(next_candidate["page"]) - 1 if next_candidate and int(next_candidate["page"]) > start_page else len(page_texts)
        )
        blocks.append({
            "number": int(candidate["number"]),
            "pdf_pages": list(range(start_page, max(start_page, page_end) + 1)),
            "start_page": start_page,
            "answer_text": answer_text,
            "reversed_heading": bool(candidate.get("reversed_heading")),
        })

    # A sequence gap is evidence of incomplete parsing, not permission to
    # shift subsequent answers by one.
    seen_numbers = [int(block["number"]) for block in blocks]
    for number in sorted(expected):
        if number not in seen_numbers:
            ambiguities.append(f"question-{number}:heading-not-found")
    if seen_numbers != sorted(set(seen_numbers)):
        ambiguities.append("question-heading-order-or-duplicate")
    return blocks, sorted(set(ambiguities))


def parse_answer_tables(
    page_texts: list[str],
    expected_numbers: Iterable[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract option-only answer-key tables as separate review locators.

    PDF text extraction often reorders table cells (``答案`` may appear
    before or after the number row).  We therefore collect number and option
    cells within the bounded table window and pair them only when counts and
    order are exact.  A table locator is never promoted to an automatic
    grading source.
    """

    expected = set(int(number) for number in expected_numbers)
    blocks: list[dict[str, Any]] = []
    issues: list[str] = []
    for page_index, raw in enumerate(page_texts, start=1):
        text = normalise_text(raw)
        if not text:
            continue
        lines = [line.strip() for line in text.splitlines()]
        header_indexes = [index for index, line in enumerate(lines) if TABLE_HEADER_RE.match(line)]
        for header_index in header_indexes:
            # A table normally ends at the first section heading or detailed
            # numbered solution.  Keep a generous but finite window so a
            # malformed table cannot consume the whole answer page.
            window: list[str] = []
            for line in lines[header_index + 1: header_index + 45]:
                compact = compact_line(line)
                if TABLE_HEADER_RE.match(line) and window:
                    break
                if re.match(r"^\d{1,3}[.．、:：)）]", compact) and window:
                    break
                window.append(line)
            if not any(TABLE_ANSWER_RE.match(line) for line in window):
                # Some text layers omit the marker; without it we cannot
                # distinguish a random sequence of choices from a table.
                continue
            numbers: list[int] = []
            options: list[str] = []
            for line in window:
                compact = compact_line(line)
                if re.fullmatch(r"\d{1,3}", compact):
                    number = int(compact)
                    if number in expected and number not in numbers:
                        numbers.append(number)
                    continue
                if TABLE_OPTION_RE.fullmatch(compact):
                    options.append(compact.translate(str.maketrans("ＡＢＣＤ", "ABCD")))
            if not numbers or len(numbers) != len(options):
                issues.append(f"page-{page_index}:answer-table-unpaired")
                continue
            # The table must enumerate a contiguous ordered subset.  If the
            # extraction interleaves cells, sort by the printed number while
            # retaining option order only when the number sequence itself is
            # monotonic; otherwise fail closed.
            if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
                issues.append(f"page-{page_index}:answer-table-order-ambiguous")
                continue
            for number, option in zip(numbers, options):
                blocks.append({
                    "number": number,
                    "pdf_pages": [page_index],
                    "start_page": page_index,
                    "answer_text": f"答案表格：{option}",
                    "table_locator": True,
                    "option_answer": option,
                })
    return blocks, sorted(set(issues))


def build_answer_records(
    source: dict[str, Any],
    routes: Iterable[dict[str, Any]],
    page_rows: list[dict[str, Any]],
    page_texts: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    source_id = str(source.get("source_id", ""))
    expected = _expected_questions(routes, source_id)
    blocks, parse_issues = parse_answer_blocks(page_texts, expected)
    table_blocks, table_issues = parse_answer_tables(page_texts, expected)
    parse_issues = sorted(set(parse_issues + table_issues))
    by_number: dict[int, list[dict[str, Any]]] = {}
    for block in blocks:
        by_number.setdefault(int(block["number"]), []).append(block)
    records: list[dict[str, Any]] = []
    for number in sorted(expected):
        question_ids = expected[number]
        candidates = by_number.get(number, [])
        # A number mapping is only considered a locator when exactly one route
        # and one heading exist.  It still remains review-required.
        bound = len(question_ids) == 1 and len(candidates) == 1 and not any(
            issue in parse_issues for issue in (f"question-{number}:heading-not-found", f"question-heading-order-or-duplicate")
        )
        block = candidates[0] if len(candidates) == 1 else None
        pages = block.get("pdf_pages", []) if block else []
        text = str(block.get("answer_text", "")) if block else ""
        answer_id = f"{source_id}:q{number}:r1"
        issues = [issue for issue in parse_issues if issue.startswith(f"question-{number}:") or issue == "question-heading-order-or-duplicate"]
        methods = sorted({str(page.get("extraction_method", "unavailable"))
                          for page in page_rows if page.get("pdf_page") in pages})
        extraction_method = methods[0] if len(methods) == 1 else "mixed" if methods else "unavailable"
        records.append({
            "answer_id": answer_id,
            "source_id": source_id,
            "source_sha256": str(source.get("source_sha256") or source.get("sha256") or ""),
            "question_id": question_ids[0] if bound else None,
            "question_number": number,
            "occurrence": 1,
            "answer_ref": f"q{number}:r1",
            "answer_text": text,
            "answer_excerpt": text[:600],
            "answer_text_sha256": sha256_bytes(text.encode("utf-8")) if text else None,
            "answer_text_status": ("text_layer_candidate" if extraction_method == "pdf_text_layer" else "ocr_or_mixed_candidate") if text else "unavailable",
            "extraction_method": extraction_method if text else "unavailable",
            "solution_completeness": "unverified" if text else "missing",
            "mapping_status": "candidate" if bound else "needs_review",
            "mapping_confidence": "high" if bound else "none",
            "mapping_evidence": {
                "numberHeadingDetected": bool(block),
                "sourcePageBound": bool(pages),
                "parseIssues": issues,
            },
            "answer_kind": "reference_solution",
            "review_required": True,
            "automatic_grading_allowed": False,
            "evidence_pages": pages,
            "uncertainties": issues,
        })
    # Keep the compact answer-key table as a second source variant.  It is
    # useful for comparison, but intentionally remains review-required and
    # cannot replace a worked reference solution.
    for table in table_blocks:
        number = int(table["number"])
        question_ids = expected.get(number, [])
        table_id = f"{source_id}:q{number}:table:r1"
        records.append({
            "answer_id": table_id,
            "source_id": source_id,
            "source_sha256": str(source.get("source_sha256") or source.get("sha256") or ""),
            "question_id": question_ids[0] if len(question_ids) == 1 else None,
            "question_number": number,
            "occurrence": 1,
            "answer_ref": f"q{number}:table:r1",
            "answer_text": table.get("answer_text", ""),
            "answer_excerpt": table.get("answer_text", ""),
            "answer_text_sha256": sha256_bytes(str(table.get("answer_text", "")).encode("utf-8")),
            "answer_text_status": "table_option_candidate",
            "extraction_method": "answer_summary_table",
            "mapping_status": "table_locator",
            "mapping_confidence": "medium",
            "mapping_evidence": {
                "kind": "answer_summary_table",
                "optionOnly": True,
                "parseIssues": table_issues,
            },
            "answer_kind": "reference_answer_key",
            "solution_completeness": "answer_key_only",
            "review_required": True,
            "automatic_grading_allowed": False,
            "evidence_pages": table.get("pdf_pages", []),
            "uncertainties": ["仅选项答案，必须与原页视觉核对"],
        })
    return records, parse_issues


def _relative_repo_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.name


def build_index(manifest_path: Path, source_root: Path, output_path: Path, asset_root: Path, dpi: int = 144) -> dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema_version") != "math-exam-paper-manifest-v2":
        raise ValueError(f"unsupported exam manifest: {manifest.get('schema_version')}")
    # ``manifest.json`` lives at <repo>/data/exam_papers.  Resolve the
    # repository root explicitly so emitted asset paths are portable and do
    # not accidentally gain a second ``exam_papers`` segment on import.
    resolved_manifest = manifest_path.resolve()
    repo_root = resolved_manifest.parents[2] if len(resolved_manifest.parents) > 2 else resolved_manifest.parent
    routes = _route_list(manifest)
    sources_out: list[dict[str, Any]] = []
    summary = {"sources": 0, "sourceReady": 0, "sourceBlocked": 0, "answerPages": 0, "answers": 0, "mapped": 0, "unresolved": 0, "renderedPages": 0}
    for source in _source_list(manifest):
        if source.get("active", True) is False:
            continue
        source_id = str(source.get("source_id", ""))
        expected_hash = str(source.get("source_sha256") or source.get("sha256") or "").lower()
        answer_pages = _answer_pages(source)
        summary["sources"] += 1
        summary["answerPages"] += len(answer_pages)
        source_path = resolve_source_path(source, source_root, manifest_path)
        source_status = "ready"
        source_issue = None
        actual_hash = None
        texts: list[str] = []
        document = None
        if source_path is None:
            source_status, source_issue = "source_unavailable", "source_file_not_found"
        else:
            actual_hash = sha256_file(source_path)
            if expected_hash and actual_hash != expected_hash:
                source_status, source_issue = "source_hash_mismatch", "source_sha256_mismatch"
            else:
                texts, extraction_error = extract_pdf_text(source_path)
                document = open_pdf(source_path)
                if extraction_error and document is None:
                    source_status, source_issue = "source_unreadable", extraction_error
        page_rows: list[dict[str, Any]] = []
        answer_texts: list[str] = []
        source_asset_root = asset_root / source_id
        for page_number in answer_pages:
            text = texts[page_number - 1] if page_number <= len(texts) else ""
            image_path = source_asset_root / f"page-{page_number:03d}.jpg"
            relative_image = None
            image_hash = None
            render_issue = None
            if source_status == "ready":
                rendered, image_hash, render_issue = render_answer_page(document, page_number, image_path, dpi)
                if rendered:
                    relative_image = _relative_repo_path(Path(rendered), repo_root)
                    summary["renderedPages"] += 1
                if not text and rendered:
                    text, confidence, method = ocr_image(Path(rendered))
                    extraction_method = method
                    ocr_confidence = confidence
                    ocr_status = "used" if text else method
                else:
                    extraction_method = "pdf_text_layer" if text else "unavailable"
                    ocr_confidence = None
                    ocr_status = "not_needed" if text else "unavailable"
            else:
                extraction_method = "unavailable"
                ocr_confidence = None
                ocr_status = "unavailable"
            answer_texts.append(text)
            page_rows.append({
                "pdf_page": page_number,
                "page_role": "answer",
                "answer_authority": True,
                "question_authority": False,
                "text_layer_available": bool(texts[page_number - 1].strip()) if page_number <= len(texts) else False,
                "text_char_count": len(text),
                "text_sha256": sha256_bytes(text.encode("utf-8")) if text else None,
                "ocr_status": ocr_status,
                "ocr_confidence": ocr_confidence,
                "extraction_method": extraction_method,
                "visual_status": "ANSWER_SOURCE_PAGE_REVIEW_REQUIRED",
                "page_image_path": relative_image,
                "page_image_sha256": image_hash,
                "render_error": render_issue,
            })
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
        if source_status != "ready":
            answer_texts = ["" for _ in answer_pages]
        # Keep parser page coordinates equal to PDF page coordinates.  The
        # answer-only text list is sparse (question pages are blank), otherwise
        # a heading on PDF page 9 would be incorrectly recorded as page 4.
        sparse_texts = [""] * (max(answer_pages, default=0))
        for page_number, text in zip(answer_pages, answer_texts):
            if page_number > 0:
                sparse_texts[page_number - 1] = text
        records, parse_issues = build_answer_records(source, routes, page_rows, sparse_texts)
        review_path = repo_root / "data/exam_papers/answer_review.json"
        source_review = json.loads(review_path.read_text(encoding="utf-8")).get("sources", {}).get(source_id, {}) if review_path.is_file() else {}
        if source_review and source_review.get("source_sha256") != (expected_hash or actual_hash):
            raise ValueError(f"answer review source hash is stale: {source_id}")
        for record in records:
            review = source_review.get("solutions", {}).get(str(record["question_number"]), {})
            if review and record["answer_kind"] == "reference_solution":
                record["solution_completeness"] = review["completeness"]
                record["uncertainties"].append(review["note"])
                if review.get("pages") and not record.get("question_id"):
                    targets = _expected_questions(routes, source_id).get(record["question_number"], [])
                    if len(targets) == 1:
                        record["question_id"] = targets[0]
                        record["evidence_pages"] = review["pages"]
                        record["mapping_status"] = "candidate"
                        record["mapping_evidence"]["kind"] = "visual_source_page_locator"
            record["mapping_evidence"]["solutionCompleteness"] = record.get("solution_completeness", "unverified")
        # Attach page evidence and hashes to each logical answer record.
        page_by_number = {int(row["pdf_page"]): row for row in page_rows}
        for record in records:
            record["evidence"] = []
            for page_number in record.pop("evidence_pages", []):
                page = page_by_number.get(int(page_number))
                if not page:
                    continue
                record["evidence"].append({
                    "pdf_page": int(page_number),
                    "source_pdf_sha256": expected_hash or actual_hash,
                    "page_image_path": page.get("page_image_path"),
                    "page_image_sha256": page.get("page_image_sha256"),
                    "page_role": "answer",
                    "answer_authority": True,
                })
            if not record["question_id"]:
                record["mapping_status"] = "needs_review"
                record["review_required"] = True
        mapped = sum(1 for record in records if record.get("question_id"))
        summary["answers"] += len(records)
        summary["mapped"] += mapped
        summary["unresolved"] += len(records) - mapped
        summary["uniqueQuestionsWithReference"] = summary.get("uniqueQuestionsWithReference", 0) + len({record["question_id"] for record in records if record.get("question_id")})
        summary["uniqueQuestionsTotal"] = summary.get("uniqueQuestionsTotal", 0) + len(_expected_questions(routes, source_id))
        if source_status == "ready" and (source_issue or not records):
            source_status = "needs_review"
        if source_status == "ready":
            summary["sourceReady"] += 1
        else:
            summary["sourceBlocked"] += 1
        sources_out.append({
            "source_id": source_id,
            "stable_source_id": source.get("stable_source_id"),
            "file_name": source.get("file_name"),
            "source_sha256": expected_hash or actual_hash,
            "source_path_status": source_status,
            "source_issue": source_issue,
            "answer_pages": answer_pages,
            "answer_page_count": len(answer_pages),
            "answer_completeness": source.get("answer_completeness", "unknown"),
            "parse_issues": parse_issues,
            "pages": page_rows,
            "answers": records,
            "learner_context_forbidden": True,
        })
    output = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": _relative_repo_path(manifest_path, repo_root),
        "source_manifest_sha256": sha256_bytes(manifest_bytes),
        "answer_policy": {
            "answer_pages_are_not_question_authority": True,
            "answer_sources_are_grader_only": True,
            "automatic_grading_allowed": False,
            "mapping_requires_explicit_review": True,
            "unresolved_mappings_fail_closed": True,
        },
        "summary": summary,
        "sources": sources_out,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/exam_papers/manifest.json"))
    parser.add_argument("--source-root", type=Path, default=Path("."), help="directory containing the allowlisted source PDFs")
    parser.add_argument("--output", type=Path, default=Path("data/exam_papers/answer_manifest.json"))
    parser.add_argument("--asset-root", type=Path, default=Path("data/exam_papers/answer_assets"))
    parser.add_argument("--dpi", type=int, default=144)
    args = parser.parse_args()
    payload = build_index(args.manifest.resolve(), args.source_root.resolve(), args.output, args.asset_root, max(72, min(args.dpi, 300)))
    print(json.dumps({"schema_version": payload["schema_version"], "summary": payload["summary"], "output": str(args.output)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
