"""Source-page regressions: topic alignment is distinct from learner mastery."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ExamSemanticMappingRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = json.loads((ROOT / 'data/exam_papers/mapping_rules.json').read_text(encoding='utf-8'))

    def test_numbering_and_cross_page_continuations(self):
        sources = self.rules['sources']
        for source in sources.values():
            self.assertEqual(set(source['questions']), {str(i) for i in range(1, source['question_count'] + 1)})
        for sid, n, pages in [('exam-6f9d44087bb37f8b', '7', [1, 2]), ('exam-9dcdf2f2023b5eba', '8', [1, 2]), ('exam-e36f8b1c7340bdb7', '8', [1, 2]), ('exam-e36f8b1c7340bdb7', '12', [2, 3])]:
            self.assertEqual(sources[sid]['question_pages'][n], pages)

    def test_linxiang_erzhong_not_shifted(self):
        questions = self.rules['sources']['exam-6f9d44087bb37f8b']['questions']
        for n, profile in [('8', 'ellipse_basic'), ('9', 'line_equation'), ('10', 'sequence_sum'), ('11', 'ellipse_property'), ('13', 'out_of_scope'), ('14', 'out_of_scope')]:
            self.assertIn(profile, questions[n]['profiles'])
        self.assertIn('射击', questions['14']['summary'])
        self.assertIn('翻折', questions['12']['summary'])

    def test_sequence_concepts_use_correct_sections(self):
        p = self.rules['profiles']
        self.assertIn('4.2', p['sequence_term']['sections'])
        self.assertIn('4.2', p['sequence_sum']['sections'])
        self.assertIn('4.4', p['sequence_geometric']['sections'])
        self.assertNotIn('4.2', p['sequence_geometric']['sections'])
        self.assertIn('4.2', p['sequence_harmonic']['sections'])
        self.assertNotIn('4.4', p['sequence_harmonic']['sections'])

    def test_vector_and_fold_topic_regressions(self):
        sources = self.rules['sources']
        q = sources['exam-e36f8b1c7340bdb7']['questions']
        self.assertEqual(q['9']['profiles'], ['spatial_vector_basis'])
        self.assertEqual(q['10']['profiles'], ['vector_projection_basis_relations'])
        self.assertNotIn('spatial_geometry_moving_fold', sources['exam-280027cc9e6ccb5b']['questions']['19']['profiles'])
        self.assertIn('二维', sources['exam-9dcdf2f2023b5eba']['questions']['12']['summary'])

    def test_review_evidence_hashes_match_all_seventeen_pages(self):
        report = json.loads((ROOT / 'reports/all_chapters/exam-semantic-page-review-20260907.json').read_text(encoding='utf-8'))
        self.assertEqual(len(report['questions']), 74)
        self.assertEqual(len(report['page_evidence']), 17)
        for page in report['page_evidence']:
            path = ROOT / 'data/exam_papers/page_assets' / page['source'] / page['page']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), page['sha256'])


if __name__ == '__main__':
    unittest.main()
