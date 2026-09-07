from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANSWER_MANIFEST = ROOT / "data" / "exam_papers" / "answer_manifest.json"
QUESTION_MANIFEST = ROOT / "data" / "exam_papers" / "manifest.json"


def load_indexer():
    path = ROOT / "scripts" / "index_exam_answers.py"
    spec = importlib.util.spec_from_file_location("exam_answer_index", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ExamAnswerIndexTests(unittest.TestCase):
    def test_question_one_does_not_inherit_question_ten_errors(self) -> None:
        indexer = load_indexer()
        routes = [{"source_id": "test", "question_number": 1, "question_id": "test:q1"}]
        blocks = [{"number": 1, "pdf_pages": [2], "answer_text": "candidate"}]
        with patch.object(indexer, "parse_answer_blocks", return_value=(blocks, ["question-10:heading-not-found"])), patch.object(indexer, "parse_answer_tables", return_value=([], [])):
            rows, _ = indexer.build_answer_records({"source_id": "test"}, routes,
                [{"pdf_page": 2, "extraction_method": "rapidocr"}], ["", "candidate"])
        self.assertEqual(rows[0]["uncertainties"], [])
        self.assertEqual(rows[0]["extraction_method"], "rapidocr")
        self.assertEqual(rows[0]["answer_text_status"], "ocr_or_mixed_candidate")
        self.assertEqual(rows[0]["solution_completeness"], "unverified")

    def test_parser_separates_worked_answers_and_answer_key_tables(self) -> None:
        indexer = load_indexer()
        pages = [
            "题号\n1\n2\n3\n答案\nB\nAC\nD\n1. 【解答】解：过程一\n2. 【答案】C\n解析二",
            "3. （5分）\n【答案】D\n解析三",
        ]
        blocks, issues = indexer.parse_answer_blocks(pages, [1, 2, 3])
        self.assertEqual([row["number"] for row in blocks], [1, 2, 3])
        self.assertEqual(issues, [])
        tables, table_issues = indexer.parse_answer_tables(pages, [1, 2, 3])
        self.assertEqual([(row["number"], row["option_answer"]) for row in tables], [(1, "B"), (2, "AC"), (3, "D")])
        self.assertEqual(table_issues, [])

    def test_unpaired_table_fails_closed(self) -> None:
        indexer = load_indexer()
        tables, issues = indexer.parse_answer_tables(["题号\n1\n2\n答案\nA"], [1, 2])
        self.assertEqual(tables, [])
        self.assertIn("page-1:answer-table-unpaired", issues)

    def test_generated_answer_manifest_is_source_bound_and_grader_only(self) -> None:
        payload = json.loads(ANSWER_MANIFEST.read_text(encoding="utf-8"))
        question_bytes = QUESTION_MANIFEST.read_bytes()
        self.assertEqual(payload["schema_version"], "math-exam-answer-manifest-v1")
        self.assertEqual(payload["source_manifest_sha256"], hashlib.sha256(question_bytes).hexdigest())
        self.assertTrue(payload["answer_policy"]["answer_sources_are_grader_only"])
        self.assertFalse(payload["answer_policy"]["automatic_grading_allowed"])
        self.assertEqual(payload["summary"]["sources"], 4)
        self.assertEqual(payload["summary"]["answerPages"], 27)
        self.assertEqual(payload["summary"]["renderedPages"], 27)
        self.assertGreaterEqual(payload["summary"]["mapped"], 90)

        answer_ids: set[str] = set()
        for source in payload["sources"]:
            self.assertTrue(source["learner_context_forbidden"])
            self.assertNotIn("C:\\Users\\", json.dumps(source, ensure_ascii=False))
            for page in source["pages"]:
                self.assertEqual(page["page_role"], "answer")
                self.assertTrue(page["answer_authority"])
                self.assertFalse(page["question_authority"])
                image = ROOT / page["page_image_path"]
                self.assertTrue(image.is_file(), image)
                self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), page["page_image_sha256"])
            for answer in source["answers"]:
                self.assertNotIn(answer["answer_id"], answer_ids)
                answer_ids.add(answer["answer_id"])
                self.assertTrue(answer["review_required"])
                self.assertFalse(answer["automatic_grading_allowed"])
                if answer["question_id"]:
                    self.assertTrue(answer["evidence"])

    def test_changsha_table_is_available_but_later_questions_stay_unresolved(self) -> None:
        payload = json.loads(ANSWER_MANIFEST.read_text(encoding="utf-8"))
        source = next(row for row in payload["sources"] if row["source_id"] == "exam-280027cc9e6ccb5b")
        table_rows = [row for row in source["answers"] if row["mapping_status"] == "table_locator"]
        self.assertEqual([row["question_number"] for row in table_rows], list(range(1, 12)))
        self.assertTrue(all(row["answer_kind"] == "reference_answer_key" for row in table_rows))
        self.assertTrue(all(row["review_required"] and not row["automatic_grading_allowed"] for row in table_rows))
        unresolved = [row for row in source["answers"] if row["answer_ref"].endswith(":r1") and ":table:" not in row["answer_ref"] and not row["question_id"]]
        self.assertEqual([row["question_number"] for row in unresolved], list(range(11, 20)))
        partial = next(row for row in source["answers"] if row["question_number"] == 10 and row["answer_kind"] == "reference_solution")
        self.assertEqual(partial["solution_completeness"], "partial")


if __name__ == "__main__":
    unittest.main()
