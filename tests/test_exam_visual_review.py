from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "report_exam_visual_review.py"
    spec = importlib.util.spec_from_file_location("exam_visual_review", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ExamVisualReviewTests(unittest.TestCase):
    def test_current_manifest_has_closed_visual_gate(self) -> None:
        module = load_module()
        manifest_path = ROOT / "data" / "exam_papers" / "manifest.json"
        report = module.build_report(module.load_json(manifest_path), manifest_path)
        self.assertEqual(report["counts"]["question_pages"], 17)
        self.assertEqual(report["counts"]["page_verified"], 17)
        self.assertEqual(report["counts"]["page_pending"], 0)
        self.assertEqual(report["counts"]["advertised_ready_but_unverified"], 0)
        self.assertFalse(report["legacy_manifest"])

    def test_all_continuation_pages_are_required_for_route_readiness(self) -> None:
        module = load_module()
        payload = {
            "route_policy": {"visual_review_gate": True},
            "sources": [{
                "source_id": "exam-test",
                "pages": [
                    {"pdf_page": 1, "page_role": "question", "visual_review_status": "verified", "page_image_sha256": "a" * 64},
                    {"pdf_page": 2, "page_role": "question", "visual_review_status": "pending", "page_image_sha256": "b" * 64},
                ],
            }],
            "routes": [{
                "route_id": "exam-test:p1:q1:r1", "source_id": "exam-test", "pdf_pages": [1, 2],
                "route_state": "ready_for_optional_unlock",
            }],
        }
        report = module.build_report(payload, ROOT / "synthetic.json")
        row = report["routes"][0]
        self.assertEqual(row["pending_pages"], [2])
        self.assertFalse(row["eligible_after_visual_review"])
        self.assertEqual(report["inconsistent_ready_route_ids"], ["exam-test:p1:q1:r1"])


if __name__ == "__main__":
    unittest.main()
