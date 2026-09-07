-- Independent answer evidence plane for exam papers.
--
-- These tables are deliberately separate from exam_questions and the learner
-- question payload.  Answer pages are grader/teacher evidence only; they are
-- never copied into the question-page R2 pack and never participate in YBT
-- progress or exam unlock calculations.

CREATE TABLE IF NOT EXISTS exam_answer_sources (
  answer_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES exam_sources(source_id),
  source_sha256 TEXT NOT NULL,
  question_id TEXT REFERENCES exam_questions(question_id),
  question_number INTEGER,
  occurrence INTEGER NOT NULL DEFAULT 1,
  answer_ref TEXT NOT NULL,
  answer_text TEXT NOT NULL DEFAULT '',
  answer_excerpt TEXT NOT NULL DEFAULT '',
  answer_text_sha256 TEXT,
  answer_text_status TEXT NOT NULL DEFAULT 'unavailable',
  extraction_method TEXT NOT NULL DEFAULT 'unavailable',
  mapping_status TEXT NOT NULL DEFAULT 'needs_review'
    CHECK (mapping_status IN ('candidate', 'table_locator', 'visually_verified', 'semantically_verified', 'needs_review', 'retired')),
  mapping_confidence TEXT NOT NULL DEFAULT 'none'
    CHECK (mapping_confidence IN ('none', 'low', 'medium', 'high')),
  mapping_evidence_json TEXT NOT NULL DEFAULT '{}',
  answer_kind TEXT NOT NULL DEFAULT 'reference_solution',
  review_required INTEGER NOT NULL DEFAULT 1 CHECK (review_required IN (0, 1)),
  automatic_grading_allowed INTEGER NOT NULL DEFAULT 0 CHECK (automatic_grading_allowed IN (0, 1)),
  uncertainties_json TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  manifest_r2_key TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  UNIQUE(source_id, answer_ref)
);
CREATE INDEX IF NOT EXISTS idx_exam_answers_question
  ON exam_answer_sources(question_id, active, mapping_status);
CREATE INDEX IF NOT EXISTS idx_exam_answers_source_order
  ON exam_answer_sources(source_id, question_number, occurrence);

CREATE TABLE IF NOT EXISTS exam_answer_evidence (
  answer_id TEXT NOT NULL REFERENCES exam_answer_sources(answer_id),
  source_id TEXT NOT NULL REFERENCES exam_sources(source_id),
  pdf_page INTEGER NOT NULL,
  source_pdf_sha256 TEXT NOT NULL,
  page_image_sha256 TEXT,
  page_pack_r2_key TEXT,
  page_asset_key TEXT,
  answer_authority INTEGER NOT NULL DEFAULT 1 CHECK (answer_authority = 1),
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(answer_id, source_id, pdf_page),
  FOREIGN KEY(source_id, pdf_page) REFERENCES exam_pages(source_id, pdf_page)
);
CREATE INDEX IF NOT EXISTS idx_exam_answer_evidence_page
  ON exam_answer_evidence(source_id, pdf_page, answer_id);
