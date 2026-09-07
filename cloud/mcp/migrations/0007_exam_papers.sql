-- Additive exam-paper route storage.  Exam papers are an optional question
-- source and never participate in the 一本通 completion projection.
-- Question-page images are kept in R2; answer pages are metadata only.

CREATE TABLE IF NOT EXISTS exam_sources (
  source_id TEXT PRIMARY KEY,
  stable_source_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  file_name TEXT NOT NULL,
  relative_path TEXT,
  source_sha256 TEXT NOT NULL UNIQUE,
  source_role TEXT NOT NULL DEFAULT 'question_paper',
  question_authority INTEGER NOT NULL DEFAULT 1 CHECK (question_authority IN (0, 1)),
  allowlisted INTEGER NOT NULL DEFAULT 0 CHECK (allowlisted IN (0, 1)),
  included_for_routes INTEGER NOT NULL DEFAULT 1 CHECK (included_for_routes IN (0, 1)),
  page_count INTEGER NOT NULL DEFAULT 0,
  question_pdf_pages_json TEXT NOT NULL DEFAULT '[]',
  answer_pdf_pages_json TEXT NOT NULL DEFAULT '[]',
  question_page_ranges_json TEXT NOT NULL DEFAULT '[]',
  answer_page_ranges_json TEXT NOT NULL DEFAULT '[]',
  answer_separation_status TEXT NOT NULL DEFAULT 'needs_review',
  question_completeness TEXT NOT NULL DEFAULT 'unknown',
  answer_completeness TEXT NOT NULL DEFAULT 'unknown',
  question_count INTEGER NOT NULL DEFAULT 0,
  manifest_r2_key TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exam_sources_hash ON exam_sources(source_sha256);

CREATE TABLE IF NOT EXISTS exam_pages (
  source_id TEXT NOT NULL REFERENCES exam_sources(source_id),
  pdf_page INTEGER NOT NULL,
  page_role TEXT NOT NULL DEFAULT 'unknown',
  question_authority INTEGER NOT NULL DEFAULT 0 CHECK (question_authority IN (0, 1)),
  text_layer_available INTEGER NOT NULL DEFAULT 0 CHECK (text_layer_available IN (0, 1)),
  text_char_count INTEGER NOT NULL DEFAULT 0,
  text_sha256 TEXT,
  ocr_status TEXT,
  ocr_text_sha256 TEXT,
  ocr_confidence REAL,
  visual_status TEXT NOT NULL DEFAULT 'NEEDS_SOURCE_PAGE_REVIEW',
  page_pack_r2_key TEXT,
  page_image_sha256 TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(source_id, pdf_page)
);
CREATE INDEX IF NOT EXISTS idx_exam_pages_role
  ON exam_pages(source_id, page_role, question_authority, pdf_page);

CREATE TABLE IF NOT EXISTS exam_questions (
  question_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES exam_sources(source_id),
  source_sha256 TEXT NOT NULL,
  pdf_page INTEGER,
  pdf_pages_json TEXT NOT NULL DEFAULT '[]',
  question_number INTEGER,
  occurrence INTEGER NOT NULL DEFAULT 1,
  question_ref TEXT NOT NULL,
  question_authority TEXT NOT NULL DEFAULT 'original_question_page',
  stem_text TEXT NOT NULL DEFAULT '',
  stem_excerpt TEXT NOT NULL DEFAULT '',
  stem_text_sha256 TEXT,
  stem_text_status TEXT NOT NULL DEFAULT 'unavailable',
  extraction_method TEXT NOT NULL DEFAULT 'unavailable',
  mapping_profiles_json TEXT NOT NULL DEFAULT '[]',
  mapping_status TEXT NOT NULL DEFAULT 'candidate',
  mapping_confidence TEXT NOT NULL DEFAULT 'none',
  mapping_evidence_json TEXT NOT NULL DEFAULT '[]',
  topic_tags_json TEXT NOT NULL DEFAULT '[]',
  type_tags_json TEXT NOT NULL DEFAULT '[]',
  required_section_ids_json TEXT NOT NULL DEFAULT '[]',
  required_cycle_ids_json TEXT NOT NULL DEFAULT '[]',
  required_course_keys_json TEXT NOT NULL DEFAULT '[]',
  required_chapter_ids_json TEXT NOT NULL DEFAULT '[]',
  external_prerequisites_json TEXT NOT NULL DEFAULT '[]',
  uncertainties_json TEXT NOT NULL DEFAULT '[]',
  blockers_json TEXT NOT NULL DEFAULT '[]',
  route_status TEXT NOT NULL DEFAULT 'needs_review',
  route_state TEXT NOT NULL DEFAULT 'needs_review',
  unlock_status TEXT NOT NULL DEFAULT 'needs_review',
  needs_review INTEGER NOT NULL DEFAULT 1 CHECK (needs_review IN (0, 1)),
  blocked INTEGER NOT NULL DEFAULT 0 CHECK (blocked IN (0, 1)),
  optional INTEGER NOT NULL DEFAULT 1 CHECK (optional IN (0, 1)),
  blocks_ybt_progress INTEGER NOT NULL DEFAULT 0 CHECK (blocks_ybt_progress IN (0, 1)),
  unlock_policy_json TEXT NOT NULL DEFAULT '{}',
  answer_policy_json TEXT NOT NULL DEFAULT '{}',
  topic_summary TEXT NOT NULL DEFAULT '',
  recommended_path_json TEXT NOT NULL DEFAULT '[]',
  manifest_r2_key TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  UNIQUE(source_id, pdf_page, question_number, occurrence),
  FOREIGN KEY(source_id, pdf_page) REFERENCES exam_pages(source_id, pdf_page)
);
CREATE INDEX IF NOT EXISTS idx_exam_questions_source_order
  ON exam_questions(source_id, pdf_page, question_number, occurrence);
CREATE INDEX IF NOT EXISTS idx_exam_questions_route_state
  ON exam_questions(route_status, route_state, needs_review, blocked);

CREATE TABLE IF NOT EXISTS exam_question_evidence (
  question_id TEXT NOT NULL REFERENCES exam_questions(question_id),
  source_id TEXT NOT NULL,
  pdf_page INTEGER NOT NULL,
  source_pdf_sha256 TEXT NOT NULL,
  page_image_sha256 TEXT,
  page_pack_r2_key TEXT,
  question_authority INTEGER NOT NULL DEFAULT 0 CHECK (question_authority IN (0, 1)),
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(question_id, source_id, pdf_page),
  FOREIGN KEY(source_id, pdf_page) REFERENCES exam_pages(source_id, pdf_page)
);
CREATE INDEX IF NOT EXISTS idx_exam_evidence_page
  ON exam_question_evidence(source_id, pdf_page, question_authority);

CREATE TABLE IF NOT EXISTS exam_route_links (
  question_id TEXT NOT NULL REFERENCES exam_questions(question_id),
  route_type TEXT NOT NULL CHECK (route_type IN ('chapter', 'section', 'cycle', 'course')),
  route_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL DEFAULT 0,
  relationship TEXT NOT NULL DEFAULT 'required',
  confidence TEXT NOT NULL DEFAULT 'candidate',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(question_id, route_type, route_key)
);
CREATE INDEX IF NOT EXISTS idx_exam_route_lookup
  ON exam_route_links(route_type, route_key, ordinal, question_id);

CREATE TABLE IF NOT EXISTS exam_attempts (
  attempt_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL,
  question_id TEXT NOT NULL REFERENCES exam_questions(question_id),
  result TEXT NOT NULL CHECK (result IN ('correct', 'incorrect', 'partial', 'skipped', 'needs_review')),
  independent INTEGER NOT NULL DEFAULT 0 CHECK (independent IN (0, 1)),
  hint_level TEXT NOT NULL DEFAULT 'none' CHECK (hint_level IN ('none', 'minimal', 'method', 'solution_seen')),
  process_evidence TEXT NOT NULL,
  evidence_hash TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exam_attempt_user_created
  ON exam_attempts(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exam_attempt_question_created
  ON exam_attempts(question_id, created_at DESC);
