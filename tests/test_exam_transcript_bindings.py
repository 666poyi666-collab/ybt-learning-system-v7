from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_exam_transcript_bindings.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("exam_transcript_bindings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ExamTranscriptBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = load_builder()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "data" / "course_transcripts").mkdir(parents=True)
        (self.root / "data" / "exam_papers").mkdir(parents=True)
        (self.root / "reports" / "all_chapters").mkdir(parents=True)

        self.course_key = "course-alpha"
        transcript = {
            "duration_s": 120,
            "full_text": "定义方法，首先设元并检查。",
            "sentences": [{"start": 0, "end": 10_000, "text": "定义方法，首先设元并检查。"}],
        }
        self.transcript_path = self.root / "data" / "course_transcripts" / "alpha.json"
        self.transcript_path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        transcript_bytes = self.transcript_path.read_bytes()
        transcript_sha = hashlib.sha256(transcript_bytes).hexdigest()
        text_sha = hashlib.sha256(transcript["full_text"].encode("utf-8")).hexdigest()
        self.transcript_sha = transcript_sha
        self.text_sha = text_sha
        catalog = {
            "course_count": 1,
            "courses": [
                {
                    "course_key": self.course_key,
                    "course_id": "1.1.1.1",
                    "title": "Alpha 方法",
                    "transcript_file": "data/course_transcripts/alpha.json",
                    "transcript_sha256": transcript_sha,
                    "transcript_text_sha256": text_sha,
                    "timestamp_status": "available",
                }
            ],
        }
        self.catalog_path = self.root / "data" / "all_chapters_course_catalog.json"
        self.catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
        self._write_chapter_manifests()
        self.audit_path = self.root / "reports" / "all_chapters" / "transcript-utilization-current.json"
        self._write_audit()
        self.exam_path = self.root / "data" / "exam_papers" / "manifest.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_chapter_manifests(self) -> None:
        self.chapter_paths = {}
        for chapter in range(1, 6):
            path = self.root / f"chapter{chapter}_manifest.json"
            payload = {"sections": []}
            if chapter == 1:
                payload = {
                    "sections": [
                        {
                            "id": "1.1",
                            "learning_cycles": [{"cycle_id": "1.1-cycle-1", "title": "Alpha"}],
                        }
                    ]
                }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            self.chapter_paths[chapter] = path

    def _write_audit(self, *, semantic_status: str = "full", substantive: int = 1) -> None:
        manifest_hashes = {
            str(chapter): hashlib.sha256(path.read_bytes()).hexdigest()
            for chapter, path in self.chapter_paths.items()
        }
        audit = {
            "schema_version": "transcript-utilization-audit-v1",
            "sources": {
                "source_bindings": {
                    "catalog_sha256": hashlib.sha256(self.catalog_path.read_bytes()).hexdigest(),
                    "manifests": manifest_hashes,
                }
            },
            "courses": [
                {
                    "course_key": self.course_key,
                    "availability": {
                        "status": "available",
                        "transcript_sha256": self.transcript_sha,
                        "full_text_sha256": self.text_sha,
                    },
                }
            ],
            "sections": [
                {
                    "section": "1.1",
                    "cycles": [
                        {
                            "cycle_id": "1.1-cycle-1",
                            "title": "Alpha",
                            "semantic_evidence": [
                                {
                                    "evidence_id": f"1.1:1.1-cycle-1:{self.course_key}",
                                    "course_key": self.course_key,
                                    "relation": "direct",
                                    "status": semantic_status,
                                    "evidence_method": "fixture",
                                    "substantive_match_count": substantive,
                                    "matched_topic_terms": [
                                        {
                                            "term": "定义",
                                            "matched_variant": None,
                                            "title_only": substantive == 0,
                                            "match_count_capped": 1,
                                        }
                                    ],
                                    "linked_signal_categories": ["steps"],
                                    "signals": {"steps": {"matched_signal_labels": ["首先"]}},
                                    "signal_categories": ["steps"],
                                    "sentence_indices": [0],
                                    "time_spans": [{"start_s": 0, "end_s": 10}],
                                    "timeline_status": "available",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        self.audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")

    def _write_exam(self, *, explicit_anchor: bool = True) -> None:
        route = {
            "route_id": "exam-test:p1:q1:r1",
            "question_id": "exam-test:p1:q1:r1",
            "source_id": "exam-test",
            "source_sha256": "a" * 64,
            "route_state": "ready_for_optional_unlock",
            "route_status": "candidate",
            "mapping_status": "semantically_verified",
            "required_cycle_ids": ["1.1-cycle-1"],
            "required_course_keys": [self.course_key],
        }
        if explicit_anchor:
            route["required_courses"] = [{"course_key": self.course_key, "cycle_ids": ["1.1-cycle-1"]}]
        payload = {"schema_version": "math-exam-paper-manifest-v2", "routes": [route]}
        self.exam_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def build(self):
        return self.builder.build_bindings(
            self.root,
            exam_manifest_path=self.exam_path,
            audit_path=self.audit_path,
            catalog_path=self.catalog_path,
        )

    def test_exact_hash_and_explicit_anchor_produce_verified_binding(self) -> None:
        self._write_exam()
        payload = self.build()
        self.assertEqual(payload["summary"]["verified_routes"], 1)
        self.assertEqual(payload["status"], "passed")
        route = payload["routes"][0]
        self.assertTrue(route["eligible_for_teacher_method"])
        evidence_ids = route["required_courses"][0]["evidence_ids"]
        self.assertEqual(len(evidence_ids), 1)
        evidence = payload["evidence_records"][evidence_ids[0]]
        self.assertEqual(evidence["transcript_sha256"], self.transcript_sha)
        self.assertEqual(evidence["transcript_text_sha256"], self.text_sha)
        self.assertEqual(evidence["sentence_indices"], [0])
        self.assertEqual(evidence["time_spans"], [{"start_s": 0, "end_s": 10}])

    def test_binding_fingerprint_is_stable_across_generation_time(self) -> None:
        self._write_exam()
        first = self.build()
        second = self.build()
        self.assertEqual(first["binding_fingerprint"], second["binding_fingerprint"])
        self.assertEqual(set(first["evidence_records"]), set(second["evidence_records"]))

    def _mutate_evidence(self, **updates):
        payload = json.loads(self.audit_path.read_text(encoding="utf-8"))
        payload["sections"][0]["cycles"][0]["semantic_evidence"][0].update(updates)
        self.audit_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_in_bounds_but_wrong_source_time_is_blocked(self) -> None:
        self._write_exam()
        self._mutate_evidence(time_spans=[{"start_s": 1, "end_s": 11}])
        payload = self.build()
        self.assertEqual(payload["routes"][0]["binding_status"], "blocked")
        evidence = next(iter(payload["evidence_records"].values()))
        self.assertIn("sentence_time_source_mismatch", evidence["verification_reasons"])

    def test_nonexistent_sentence_index_is_blocked(self) -> None:
        self._write_exam()
        self._mutate_evidence(sentence_indices=[99])
        self.assertEqual(self.build()["routes"][0]["binding_status"], "blocked")

    def test_claimed_topic_missing_from_body_is_blocked(self) -> None:
        self._write_exam()
        self._mutate_evidence(matched_topic_terms=[{"term": "不存在的抛物线知识点", "title_only": False}])
        self.assertEqual(self.build()["routes"][0]["binding_status"], "blocked")

    def test_missing_timeline_separate_from_semantic_source(self) -> None:
        self._write_exam()
        self._mutate_evidence(sentence_indices=[], time_spans=[], timeline_status="not_available")
        payload = self.build()
        evidence = next(iter(payload["evidence_records"].values()))
        self.assertTrue(evidence["semantic_source_verified"])
        self.assertFalse(evidence["timeline_verified"])
        self.assertFalse(evidence["eligible_for_teacher_method"])
        self.assertEqual(payload["routes"][0]["binding_status"], "review")

    def test_title_only_flag_cannot_be_overridden_by_claimed_count(self) -> None:
        self._write_exam()
        self._mutate_evidence(matched_topic_terms=[{"term": "定义", "title_only": True}])
        self.assertFalse(self.build()["routes"][0]["eligible_for_teacher_method"])

    def test_fabricated_teaching_signal_stays_review(self) -> None:
        self._write_exam()
        self._mutate_evidence(signals={"steps": {"matched_signal_labels": ["凭空宣称的步骤"]}})
        evidence = next(iter(self.build()["evidence_records"].values()))
        self.assertFalse(evidence["semantic_source_verified"])
        self.assertIn("linked_method_signal_unverified", evidence["verification_reasons"])

    def test_reselects_real_topic_and_signal_beyond_old_cap(self) -> None:
        audit = json.loads(self.audit_path.read_text(encoding="utf-8"))
        evidence = audit["sections"][0]["cycles"][0]["semantic_evidence"][0]
        evidence["time_spans"] = [{"start_s": 0, "end_s": 1}]
        rows = [{"text": "定义", "start": i, "end": i + 1} for i in range(21)]
        rows[20]["text"] = "首先讲清定义，再检查条件。"
        payload = {"full_text": "".join(row["text"] for row in rows), "sentences": rows}
        selected = self.builder._reselect_body_sentences(payload, evidence, 120)
        self.assertEqual(selected["sentence_indices"], [20])
        self.assertEqual(selected["time_spans"], [{"start_s": 20, "end_s": 21}])
        self.assertTrue(selected["source_sentence_reselection"])
        self.assertEqual(self.builder._source_anchor_checks(payload, selected, 120)[0], [])

    def test_reselection_does_not_hide_corrupted_original_timestamp(self) -> None:
        audit = json.loads(self.audit_path.read_text(encoding="utf-8"))
        evidence = audit["sections"][0]["cycles"][0]["semantic_evidence"][0]
        evidence["time_spans"] = [{"start_s": 2, "end_s": 3}]
        payload = json.loads(self.transcript_path.read_text(encoding="utf-8"))
        selected = self.builder._reselect_body_sentences(payload, evidence, 120)
        self.assertNotIn("source_sentence_reselection", selected)

    def test_title_only_or_partial_evidence_stays_review(self) -> None:
        self._write_exam()
        self._write_audit(semantic_status="partial", substantive=0)
        payload = self.build()
        route = payload["routes"][0]
        self.assertEqual(route["binding_status"], "review")
        self.assertFalse(route["eligible_for_teacher_method"])
        self.assertIn("no_substantive_topic_match", route["required_courses"][0]["reasons"])

    def test_missing_explicit_anchor_is_blocked_and_not_inferred(self) -> None:
        self._write_exam(explicit_anchor=False)
        payload = self.build()
        route = payload["routes"][0]
        self.assertEqual(route["binding_status"], "blocked")
        self.assertEqual(route["required_courses"][0]["evidence_ids"], [])
        self.assertIn("required_courses_not_explicit_list", route["reasons"])

    def test_transcript_hash_change_blocks_route(self) -> None:
        self._write_exam()
        self.transcript_path.write_text("{\"full_text\":\"被改写\"}", encoding="utf-8")
        payload = self.build()
        route = payload["routes"][0]
        self.assertEqual(route["binding_status"], "blocked")
        evidence_key = route["required_courses"][0]["evidence_ids"][0]
        reasons = payload["evidence_records"][evidence_key]["verification_reasons"]
        self.assertIn("transcript_sha256_mismatch", reasons)

    def test_current_artifact_is_deduplicated_and_answer_safe(self) -> None:
        artifact = ROOT / "data" / "exam_papers" / "transcript_bindings.json"
        if not artifact.is_file():
            self.skipTest("generated artifact is absent")
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "math-exam-transcript-bindings-v1")
        self.assertEqual(payload["summary"]["routes"], 74)
        self.assertEqual(payload["summary"]["evidence_bindings"], len(payload["evidence_records"]))
        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        artifact_keys = set(keys(payload))
        self.assertNotIn("full_text", artifact_keys)
        self.assertNotIn("answer_content", artifact_keys)
        self.assertNotIn("答案", json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
