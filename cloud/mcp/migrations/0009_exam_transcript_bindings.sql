-- Answer-safe teacher-transcript evidence for optional exam routes.
-- This plane stores hashes, method signals and timestamp references only;
-- transcript sentences, question answers and learner progress stay elsewhere.

CREATE TABLE IF NOT EXISTS exam_transcript_imports (
  binding_fingerprint TEXT PRIMARY KEY,
  exam_manifest_sha256 TEXT NOT NULL,
  transcript_audit_sha256 TEXT NOT NULL,
  course_catalog_sha256 TEXT NOT NULL,
  index_r2_key TEXT,
  route_count INTEGER NOT NULL DEFAULT 0,
  evidence_count INTEGER NOT NULL DEFAULT 0,
  imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exam_transcript_evidence (
  evidence_key TEXT PRIMARY KEY,
  evidence_id TEXT NOT NULL,
  section_id TEXT NOT NULL,
  cycle_id TEXT NOT NULL,
  cycle_title TEXT,
  course_key TEXT NOT NULL,
  relation TEXT,
  semantic_status TEXT NOT NULL DEFAULT 'none',
  evidence_method TEXT,
  substantive_match_count INTEGER NOT NULL DEFAULT 0,
  matched_topic_terms_json TEXT NOT NULL DEFAULT '[]',
  teacher_method_signals_json TEXT NOT NULL DEFAULT '{}',
  sentence_indices_json TEXT NOT NULL DEFAULT '[]',
  time_spans_json TEXT NOT NULL DEFAULT '[]',
  timeline_status TEXT NOT NULL DEFAULT 'not_available',
  duration_s REAL,
  transcript_file TEXT,
  transcript_sha256 TEXT,
  transcript_text_sha256 TEXT,
  catalog_transcript_sha256 TEXT,
  catalog_transcript_text_sha256 TEXT,
  source_hashes_match_json TEXT NOT NULL DEFAULT '{}',
  verification_reasons_json TEXT NOT NULL DEFAULT '[]',
  eligible_for_teacher_method INTEGER NOT NULL DEFAULT 0 CHECK (eligible_for_teacher_method IN (0, 1)),
  binding_fingerprint TEXT NOT NULL REFERENCES exam_transcript_imports(binding_fingerprint),
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exam_transcript_evidence_course
  ON exam_transcript_evidence(course_key, cycle_id, eligible_for_teacher_method);

CREATE TABLE IF NOT EXISTS exam_question_transcript_links (
  question_id TEXT NOT NULL REFERENCES exam_questions(question_id),
  evidence_key TEXT NOT NULL REFERENCES exam_transcript_evidence(evidence_key),
  cycle_id TEXT NOT NULL,
  course_key TEXT NOT NULL,
  relation TEXT,
  binding_status TEXT NOT NULL DEFAULT 'blocked'
    CHECK (binding_status IN ('verified', 'review', 'blocked')),
  eligible_for_teacher_method INTEGER NOT NULL DEFAULT 0 CHECK (eligible_for_teacher_method IN (0, 1)),
  reasons_json TEXT NOT NULL DEFAULT '[]',
  ordinal INTEGER NOT NULL DEFAULT 0,
  binding_fingerprint TEXT NOT NULL REFERENCES exam_transcript_imports(binding_fingerprint),
  imported_at TEXT NOT NULL,
  PRIMARY KEY(question_id, evidence_key),
  UNIQUE(question_id, cycle_id, course_key),
  FOREIGN KEY(evidence_key) REFERENCES exam_transcript_evidence(evidence_key)
);
CREATE INDEX IF NOT EXISTS idx_exam_question_transcript_lookup
  ON exam_question_transcript_links(question_id, ordinal, binding_status);

CREATE TABLE IF NOT EXISTS exam_question_transcript_routes (
  question_id TEXT PRIMARY KEY REFERENCES exam_questions(question_id),
  binding_fingerprint TEXT NOT NULL REFERENCES exam_transcript_imports(binding_fingerprint),
  binding_status TEXT NOT NULL DEFAULT 'blocked'
    CHECK (binding_status IN ('verified', 'review', 'blocked')),
  eligible_for_teacher_method INTEGER NOT NULL DEFAULT 0 CHECK (eligible_for_teacher_method IN (0, 1)),
  reasons_json TEXT NOT NULL DEFAULT '[]',
  unresolved_cycles_json TEXT NOT NULL DEFAULT '[]',
  unresolved_courses_json TEXT NOT NULL DEFAULT '[]',
  unanchored_cycles_json TEXT NOT NULL DEFAULT '[]',
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exam_question_transcript_route_status
  ON exam_question_transcript_routes(binding_status, eligible_for_teacher_method);
