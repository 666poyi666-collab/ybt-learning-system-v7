#!/usr/bin/env python3
"""Report source-page visual-review readiness without changing a manifest.

The report is intentionally read-only.  It is useful for legacy manifests
whose route state predates the page-level visual gate: those routes remain
queryable, but the report exposes any advertised-ready route whose source page
is still pending review.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "exam_papers" / "manifest.json"
VERIFIED = {
    "verified", "visually_verified", "vision_verified", "source_page_verified",
    "page_verified", "approved", "passed", "ready",
}
BLOCKED = {"blocked", "unavailable", "failed", "invalid", "rejected"}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest root must be an object: {path}")
    return value


def normalise_status(value: Any) -> str:
    if value is True:
        return "verified"
    if value is False or value is None:
        return "pending"
    text = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    if text in VERIFIED:
        return "verified"
    if text in BLOCKED or any(token in text for token in ("block", "fail", "invalid", "unavailable")):
        return "blocked"
    if text in {"not_applicable", "n_a", "na"}:
        return "not_applicable"
    return "pending"


def page_status(page: dict[str, Any]) -> str:
    if "visual_review_status" in page:
        return normalise_status(page.get("visual_review_status"))
    return normalise_status(page.get("visual_status"))


def source_pages(payload: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for source in payload.get("sources", []) if isinstance(payload.get("sources"), list) else []:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "")
        for page in source.get("pages", []) if isinstance(source.get("pages"), list) else []:
            if not isinstance(page, dict):
                continue
            try:
                number = int(page.get("pdf_page", page.get("page")))
            except (TypeError, ValueError):
                continue
            result[(source_id, number)] = page
    return result


def route_review(route: dict[str, Any], pages: dict[tuple[str, int], dict[str, Any]]) -> dict[str, Any]:
    source_id = str(route.get("source_id") or "")
    values = route.get("pdf_pages") or route.get("pages") or ([route.get("pdf_page")] if route.get("pdf_page") else [])
    page_numbers: list[int] = []
    for value in values if isinstance(values, list) else [values]:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in page_numbers:
            page_numbers.append(number)
    statuses: dict[int, str] = {}
    for number in page_numbers:
        page = pages.get((source_id, number))
        if page is None:
            # Fall back to route evidence for custom/synthetic manifests.
            for evidence in route.get("source_page_evidence", []) if isinstance(route.get("source_page_evidence"), list) else []:
                if not isinstance(evidence, dict):
                    continue
                try:
                    evidence_page = int(evidence.get("pdf_page", evidence.get("page")))
                except (TypeError, ValueError):
                    continue
                if evidence_page == number:
                    page = evidence
                    break
        statuses[number] = page_status(page) if page is not None else "pending"
    explicit = normalise_status(route.get("visual_review_status")) if "visual_review_status" in route else None
    pending = [number for number, status in statuses.items() if status == "pending"]
    blocked = [number for number, status in statuses.items() if status == "blocked"]
    verified = [number for number, status in statuses.items() if status == "verified"]
    status = "blocked" if blocked else "verified" if statuses and not pending and len(verified) == len(statuses) else "pending"
    if explicit == "blocked":
        status = "blocked"
    elif explicit == "pending":
        status = "pending"
    elif explicit == "verified" and status == "verified":
        status = "verified"
    return {
        "route_id": str(route.get("route_id") or route.get("question_id") or ""),
        "source_id": source_id,
        "active": route.get("active", True) is not False,
        "route_state": route.get("route_state"),
        "route_status": route.get("route_status"),
        "visual_review_status": status,
        "required_pages": page_numbers,
        "verified_pages": verified,
        "pending_pages": pending,
        "blocked_pages": blocked,
        "advertised_ready": route.get("route_state") == "ready_for_optional_unlock",
        "eligible_after_visual_review": status == "verified",
    }


def build_report(payload: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    pages = source_pages(payload)
    page_rows = []
    for (source_id, number), page in sorted(pages.items()):
        if page.get("page_role") not in {"question", "question_paper", "stem", "question_stem", "original", "原卷", "题面"}:
            continue
        page_rows.append({
            "source_id": source_id,
            "pdf_page": number,
            "status": page_status(page),
            "page_image_sha256_present": bool(page.get("page_image_sha256") or page.get("source_page_sha256")),
        })
    routes = [
        route_review(route, pages)
        for route in payload.get("routes", [])
        if isinstance(route, dict) and route.get("active", True) is not False
    ]
    advertised_ready = [row for row in routes if row["advertised_ready"]]
    inconsistent = [
        row["route_id"] for row in advertised_ready
        if not row["eligible_after_visual_review"]
    ]
    counts = {
        "question_pages": len(page_rows),
        "page_verified": sum(row["status"] == "verified" for row in page_rows),
        "page_pending": sum(row["status"] == "pending" for row in page_rows),
        "page_blocked": sum(row["status"] == "blocked" for row in page_rows),
        "routes": len(routes),
        "routes_visual_verified": sum(row["visual_review_status"] == "verified" for row in routes),
        "routes_visual_pending": sum(row["visual_review_status"] == "pending" for row in routes),
        "routes_visual_blocked": sum(row["visual_review_status"] == "blocked" for row in routes),
        "advertised_ready_routes": len(advertised_ready),
        "advertised_ready_but_unverified": len(inconsistent),
    }
    policy = payload.get("route_policy") if isinstance(payload.get("route_policy"), dict) else {}
    return {
        "schema_version": "math-exam-visual-review-report-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest_path.relative_to(ROOT)).replace("\\", "/") if manifest_path.is_relative_to(ROOT) else str(manifest_path),
        "manifest_generated_at": payload.get("generated_at"),
        "visual_review_gate_enabled": bool(policy.get("visual_review_gate")),
        "legacy_manifest": not bool(policy.get("visual_review_gate")),
        "counts": counts,
        "inconsistent_ready_route_ids": inconsistent,
        "pages": page_rows,
        "routes": routes,
    }


def markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# 试卷原卷页视觉复核状态",
        "",
        f"来源 manifest：`{report['manifest']}`",
        f"视觉门禁：{'已启用' if report['visual_review_gate_enabled'] else '旧版未声明（查询仍按保守规则处理）'}",
        "",
        f"- 题面页：{counts['question_pages']}（已复核 {counts['page_verified']}，待复核 {counts['page_pending']}，阻塞 {counts['page_blocked']}）",
        f"- 活跃路线：{counts['routes']}（视觉已复核 {counts['routes_visual_verified']}，待复核 {counts['routes_visual_pending']}，阻塞 {counts['routes_visual_blocked']}）",
        f"- manifest 宣称可解锁但视觉未通过：{counts['advertised_ready_but_unverified']} 条",
        "",
        "规则：页图存在不等于已复核；一道题的所有原卷/续页都必须为 `verified` 才能解锁。此报告不修改进度或 manifest。",
        "",
    ]
    if report["inconsistent_ready_route_ids"]:
        lines.extend(["## 待修正路线", ""])
        lines.extend(f"- `{route_id}`" for route_id in report["inconsistent_ready_route_ids"])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Report exam source-page visual review status without changing the manifest.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--strict", action="store_true", help="return 1 when an advertised-ready route lacks verified pages")
    args = parser.parse_args()
    try:
        manifest = args.manifest.resolve()
        report = build_report(load_json(manifest), manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    json_output = args.json_output.resolve() if args.json_output else None
    markdown_output = args.markdown_output.resolve() if args.markdown_output else None
    if json_output:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if markdown_output:
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"status": "passed", "counts": report["counts"], "legacy_manifest": report["legacy_manifest"]}, ensure_ascii=False))
    return 1 if args.strict and report["inconsistent_ready_route_ids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
