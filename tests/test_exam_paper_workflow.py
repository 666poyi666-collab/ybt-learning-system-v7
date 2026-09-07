from __future__ import annotations

import json
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"exam_test_{name.replace('.', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ExamPaperWorkflowTests(unittest.TestCase):
    def test_manifest_is_optional_and_answer_pages_are_not_authority(self) -> None:
        payload = json.loads((ROOT / "data" / "exam_papers" / "manifest.json").read_text(encoding="utf-8"))
        policy = payload["route_policy"]
        self.assertTrue(policy["optional"])
        self.assertFalse(policy["blocks_ybt_progress"])
        self.assertTrue(policy["answer_page_is_not_question_authority"])

    def test_empty_route_manifest_is_valid_until_question_papers_arrive(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_exam_routes.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_indexer_allowlist_page_sidecar_and_stable_question_ids(self) -> None:
        indexer = load_script("index_exam_papers.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "2025期中试卷.pdf"
            paper.write_bytes(b"not-a-real-pdf-but-the-sidecar-is-authoritative")
            (root / "notes.pdf").write_bytes(b"unrelated")
            allowlist = root / "allowlist.json"
            allowlist.write_text(json.dumps({"files": [paper.name]}, ensure_ascii=False), encoding="utf-8")
            sidecar = root / "pages.json"
            sidecar.write_text(json.dumps({
                "sources": {paper.name: {"pages": [
                    {"pdf_page": 1, "page_role": "question", "question_numbers": [1, 2], "ocr_text": "1. stem\n2. stem", "page_image_sha256": "a" * 64},
                    {"pdf_page": 2, "page_role": "answer", "question_numbers": [1], "page_image_sha256": "b" * 64},
                ]}},
            }, ensure_ascii=False), encoding="utf-8")
            first = root / "first.json"
            second = root / "second.json"
            for output in (first, second):
                result = subprocess.run([
                    sys.executable, str(ROOT / "scripts" / "index_exam_papers.py"),
                    "--source-root", str(root), "--output", str(output),
                    "--allowlist", str(allowlist), "--page-sidecar", str(sidecar),
                ], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            left = json.loads(first.read_text(encoding="utf-8"))
            right = json.loads(second.read_text(encoding="utf-8"))
            self.assertEqual(left["schema_version"], "math-exam-source-inventory-v2")
            self.assertEqual(left["source_count"], 1)
            self.assertEqual(left["discovered_file_count"], 2)
            self.assertEqual(left["allowlist"]["filtered_file_count"], 1)
            self.assertEqual(left["selected_question_paper_count"], 1)
            source = next(row for row in left["sources"] if row["file_name"] == paper.name)
            self.assertEqual(source["question_pages_verified"], 1)
            self.assertEqual(source["answer_pages_detected"], 1)
            self.assertTrue(source["route_ready"])
            questions = source["questions"]
            self.assertEqual([row["question_id"] for row in questions], [
                f"{source['source_id']}:p1:q1:r1",
                f"{source['source_id']}:p1:q2:r1",
                f"{source['source_id']}:p2:q1:r1",
            ])
            self.assertEqual([row["question_authority"] for row in questions], [True, True, False])
            self.assertEqual(
                [row["question_id"] for row in questions],
                [row["question_id"] for row in next(row for row in right["sources"] if row["file_name"] == paper.name)["questions"]],
            )

    def test_validator_rejects_ready_route_on_answer_page(self) -> None:
        validator = load_script("validate_exam_routes.py")
        source_hash = "c" * 64
        payload = {
            "sources": [{
                "source_id": "exam-test",
                "sha256": source_hash,
                "source_role": "question_paper",
                "question_authority": True,
                "page_count": 1,
                "pages": [{"pdf_page": 1, "page_role": "answer", "question_authority": False, "page_image_sha256": "d" * 64}],
            }],
            "routes": [{
                "route_id": "exam-test:p1:q1:r1",
                "source_id": "exam-test",
                "source_sha256": source_hash,
                "source_page_sha256": "d" * 64,
                "pdf_page": 1,
                "question_number": 1,
                "question_ref": "第1页第1题",
                "required_course_keys": ["space_vector_ops"],
                "required_cycle_ids": ["1.1-cycle-1"],
                "required_section_ids": ["1.1"],
                "unlock_granularity": "after_cycle",
                "mapping_status": "semantically_verified",
                "route_status": "ready",
                "optional": True,
                "blocks_ybt_progress": False,
            }],
        }
        report = validator.validate_payload(payload, ROOT)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("source_page_not_question_authority" in error for error in report["errors"]))

    def test_validator_allows_explicit_review_block_for_unverified_page(self) -> None:
        validator = load_script("validate_exam_routes.py")
        source_hash = hashlib.sha256(b"paper").hexdigest()
        payload = {
            "sources": [{
                "source_id": "exam-review",
                "sha256": source_hash,
                "source_role": "question_paper",
                "question_authority": True,
                "page_count": 1,
                "pages": [{"pdf_page": 1, "page_role": "unknown", "question_authority": False}],
            }],
            "routes": [{
                "route_id": "exam-review:p1:q1:r1",
                "source_id": "exam-review",
                "pdf_page": 1,
                "question_ref": "第1页第1题",
                "required_cycle_ids": ["1.1-cycle-1"],
                "unlock_granularity": "after_cycle",
                "mapping_status": "candidate",
                "route_status": "needs_review",
                "uncertainties": ["尚未核对原卷页角色"],
                "optional": True,
                "blocks_ybt_progress": False,
            }],
        }
        report = validator.validate_payload(payload, ROOT)
        self.assertEqual(report["status"], "passed", report["errors"])
        self.assertEqual(report["status_counts"], {"needs_review": 1})

    def test_validator_rejects_explicit_unknown_page_even_if_authority_flag_is_true(self) -> None:
        validator = load_script("validate_exam_routes.py")
        source_hash = "e" * 64
        payload = {
            "sources": [{
                "source_id": "exam-unknown",
                "sha256": source_hash,
                "source_role": "question_paper",
                "question_authority": True,
                "page_count": 1,
                "pages": [{"pdf_page": 1, "page_role": "unknown", "question_authority": True, "page_image_sha256": "f" * 64}],
            }],
            "routes": [{
                "route_id": "exam-unknown:p1:q1:r1",
                "source_id": "exam-unknown",
                "source_sha256": source_hash,
                "source_page_sha256": "f" * 64,
                "pdf_page": 1,
                "question_number": 1,
                "question_ref": "第1页第1题",
                "required_course_keys": ["space_vector_ops"],
                "required_cycle_ids": ["1.1-cycle-1"],
                "unlock_granularity": "after_cycle",
                "mapping_status": "semantically_verified",
                "route_status": "ready",
                "optional": True,
                "blocks_ybt_progress": False,
            }],
        }
        report = validator.validate_payload(payload, ROOT)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("source_page_not_question_authority" in error for error in report["errors"]))

    def test_visual_gate_rejects_pending_page_on_ready_route(self) -> None:
        validator = load_script("validate_exam_routes.py")
        source_hash = hashlib.sha256(b"visual-paper").hexdigest()
        page_hash = hashlib.sha256(b"visual-page").hexdigest()
        payload = {
            "route_policy": {"visual_review_gate": True},
            "sources": [{
                "source_id": "exam-visual",
                "sha256": source_hash,
                "source_role": "question_paper",
                "question_authority": True,
                "page_count": 1,
                "pages": [{
                    "pdf_page": 1,
                    "page_role": "question",
                    "question_authority": True,
                    "page_image_sha256": page_hash,
                    "visual_review_status": "pending",
                }],
            }],
            "routes": [{
                "route_id": "exam-visual:p1:q1:r1",
                "question_id": "exam-visual:p1:q1:r1",
                "source_id": "exam-visual",
                "source_sha256": source_hash,
                "source_page_sha256": page_hash,
                "pdf_page": 1,
                "pdf_pages": [1],
                "question_number": 1,
                "question_ref": "第1页第1题",
                "required_course_keys": ["space_vector_ops"],
                "required_cycle_ids": ["1.1-cycle-1"],
                "required_section_ids": ["1.1"],
                "unlock_granularity": "after_cycle",
                "mapping_status": "semantically_verified",
                "route_status": "candidate",
                "route_state": "ready_for_optional_unlock",
                "visual_review_status": "pending",
                "visual_review_pages": {"required": [1], "verified": [], "pending": [1], "blocked": []},
                "optional": True,
                "blocks_ybt_progress": False,
                "uncertainties": [],
                "blockers": [],
            }],
        }
        report = validator.validate_payload(payload, ROOT)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("visual_review_pending" in error for error in report["errors"]))

    def test_visual_gate_accepts_verified_all_pages(self) -> None:
        validator = load_script("validate_exam_routes.py")
        source_hash = hashlib.sha256(b"visual-paper-ok").hexdigest()
        page_hash = hashlib.sha256(b"visual-page-ok").hexdigest()
        payload = {
            "route_policy": {"visual_review_gate": True},
            "sources": [{
                "source_id": "exam-visual-ok", "sha256": source_hash,
                "source_role": "question_paper", "question_authority": True,
                "page_count": 1,
                "pages": [{"pdf_page": 1, "page_role": "question", "question_authority": True,
                           "page_image_sha256": page_hash, "visual_review_status": "verified"}],
            }],
            "routes": [{
                "route_id": "exam-visual-ok:p1:q1:r1", "question_id": "exam-visual-ok:p1:q1:r1",
                "source_id": "exam-visual-ok", "source_sha256": source_hash, "source_page_sha256": page_hash,
                "pdf_page": 1, "pdf_pages": [1], "question_number": 1, "question_ref": "第1页第1题",
                "required_course_keys": ["space_vector_ops"], "required_cycle_ids": ["1.1-cycle-1"],
                "required_section_ids": ["1.1"], "unlock_granularity": "after_cycle",
                "mapping_status": "semantically_verified", "route_status": "candidate",
                "route_state": "ready_for_optional_unlock", "visual_review_status": "verified",
                "visual_review_pages": {"required": [1], "verified": [1], "pending": [], "blocked": []},
                "optional": True, "blocks_ybt_progress": False,
            }],
        }
        report = validator.validate_payload(payload, ROOT)
        self.assertEqual(report["status"], "passed", report["errors"])

    def test_query_explicit_visual_pending_never_unlocks(self) -> None:
        query_module = load_script("query_exam_routes.py")
        route = {
            "route_id": "exam-visual-query:p1:q1:r1",
            "question_id": "exam-visual-query:p1:q1:r1",
            "required_cycle_ids": [], "required_course_keys": [],
            "mapping_status": "semantically_verified", "route_status": "candidate",
            "route_state": "ready_for_optional_unlock", "visual_review_status": "pending",
            "visual_review_pages": {"required": [2], "verified": [], "pending": [2], "blocked": []},
            "optional": True, "blocks_ybt_progress": False,
        }
        result = query_module.query(route, set(), set(), [])
        self.assertFalse(result["ready"])
        self.assertEqual(result["visual_review"]["pending_pages"], [2])
        self.assertIn("视觉证据", result["decision"])

    def test_incremental_diff_distinguishes_replacement_and_allowlist_filter(self) -> None:
        indexer = load_script("index_exam_papers.py")
        old_hash = "a" * 64
        new_hash = "b" * 64
        previous = {
            "inventory_sha256": "p" * 64,
            "allowlist": {"configured": True, "tokens": ["old.pdf"]},
            "sources": [
                {"source_id": f"exam-{old_hash[:16]}", "sha256": old_hash, "relative_path": "changed.pdf", "allowlisted": True, "pages": []},
                {"source_id": "exam-removed", "sha256": "c" * 64, "relative_path": "filtered.pdf", "allowlisted": True, "pages": []},
            ],
        }
        current = [{"source_id": f"exam-{new_hash[:16]}", "sha256": new_hash, "relative_path": "changed.pdf", "allowlisted": True, "pages": []}]
        root = Path(".").resolve()
        discovered = [root / "changed.pdf", root / "filtered.pdf"]
        selected = [root / "changed.pdf"]
        diff = indexer._incremental_summary(previous, current, discovered, selected, root, {"configured": True, "tokens": ["new.pdf"]})
        self.assertEqual(diff["changed_source_ids"], [f"exam-{new_hash[:16]}"])
        self.assertEqual(diff["deallowed_source_ids"], ["exam-removed"])
        self.assertEqual(diff["removed_source_ids"], [])
        self.assertTrue(diff["allowlist_changed"])


if __name__ == "__main__":
    unittest.main()
