from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "exam_papers" / "manifest.json"


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"generated_exam_{name.replace('.', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class GeneratedExamRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not MANIFEST.is_file():
            raise unittest.SkipTest("exam route manifest has not been generated")
        cls.payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.routes = cls.payload.get("routes", [])

    def test_four_sources_and_all_question_numbers_are_present(self) -> None:
        self.assertEqual(self.payload["schema_version"], "math-exam-paper-manifest-v2")
        self.assertEqual(len(self.payload["sources"]), 4)
        self.assertEqual(len(self.routes), 74)
        self.assertEqual(sorted(source["question_count"] for source in self.payload["sources"]), [14, 19, 19, 22])
        self.assertEqual(len({route["route_id"] for route in self.routes}), 74)

    def test_answer_pages_never_supply_question_authority(self) -> None:
        for source in self.payload["sources"]:
            answer_pages = set(source["answer_pdf_pages"])
            question_pages = set(source["question_pdf_pages"])
            self.assertTrue(answer_pages.isdisjoint(question_pages))
            for page in source["pages"]:
                if page["pdf_page"] in answer_pages:
                    self.assertEqual(page["page_role"], "answer")
                    self.assertFalse(page["question_authority"])
            for route in [row for row in self.routes if row["source_id"] == source["source_id"]]:
                self.assertIn(route["pdf_page"], question_pages)
                self.assertEqual(route["question_authority"], "original_question_page")
                self.assertTrue(all(page["pdf_page"] not in answer_pages for page in route["source_page_evidence"]))
                self.assertNotIn("参考答案", route.get("stem_text", ""))

    def test_routes_expose_bidirectional_prerequisites_and_conservative_states(self) -> None:
        mapped = [route for route in self.routes if route.get("route_state") == "ready_for_optional_unlock"]
        self.assertGreaterEqual(len(mapped), 40)
        self.assertTrue(all(route["required_cycle_ids"] for route in mapped))
        self.assertTrue(all(route["required_course_keys"] for route in mapped))
        self.assertTrue(all(route["type_tags"] for route in mapped))
        self.assertTrue(all(route["optional"] and route["blocks_ybt_progress"] is False for route in self.routes))
        self.assertTrue(any(route["route_status"] == "needs_review" for route in self.routes))
        self.assertTrue(any(route["route_status"] == "blocked" for route in self.routes))
        self.assertTrue(all(route.get("active", True) for route in self.routes))
        self.assertTrue(all("C:\\Users\\16408" not in json.dumps(route, ensure_ascii=False) for route in self.routes))

    def test_scan_only_route_does_not_publish_neighbor_question_text(self) -> None:
        scan_route = next(route for route in self.routes if route["source_id"] == "exam-280027cc9e6ccb5b" and route["question_number"] == 6)
        self.assertEqual(scan_route["route_status"], "blocked")
        self.assertNotIn("f（x）的图象", scan_route.get("stem_text", ""))
        self.assertTrue(scan_route.get("ocr_locator_excerpt"))

    def test_query_reports_missing_then_ready_without_answer_text(self) -> None:
        query_module = load_script("query_exam_routes.py")
        route = next(route for route in self.routes if route["route_state"] == "ready_for_optional_unlock")
        empty = query_module.query(route, set(), set(), [])
        self.assertFalse(empty["ready"])
        self.assertTrue(empty["missing_cycles"])
        self.assertTrue(empty["missing_courses"])
        completed = query_module.query(
            route,
            set(route["required_cycle_ids"]),
            set(route["required_course_keys"]),
            [],
        )
        self.assertTrue(completed["ready"])
        self.assertIn("可以开始做", completed["decision"])
        self.assertNotIn("答案", json.dumps(completed, ensure_ascii=False))

    def test_recommended_path_follows_curriculum_section_order(self) -> None:
        route = next(route for route in self.routes if route["source_id"] == "exam-e36f8b1c7340bdb7" and route["question_number"] == 1)
        ids = [str(item["cycle_id"]) for item in route["recommended_path"]]
        self.assertTrue(ids.index("1.1-cycle-1") < ids.index("1.2_1.3-cycle-1"))
        self.assertTrue(ids.index("1.2_1.3-cycle-1") < ids.index("1.2_1.3-cycle-7"))


if __name__ == "__main__":
    unittest.main()
