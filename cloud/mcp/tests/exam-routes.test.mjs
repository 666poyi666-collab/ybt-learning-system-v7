import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const cloudRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = resolve(cloudRoot, '..', '..')

function runExamImport() {
  const child = spawnSync(process.execPath, [resolve(cloudRoot, 'scripts', 'import_exam_papers.mjs'), '--index', resolve(repoRoot, 'data', 'exam_papers', 'manifest.json')], {
    cwd: cloudRoot,
    encoding: 'utf8',
    windowsHide: true,
  })
  assert.equal(child.status, 0, child.stderr || child.stdout)
  const summary = JSON.parse(child.stdout)
  const planPath = resolve(repoRoot, 'tmp', 'math-exam-import', summary.version, 'plan.json')
  return { summary, plan: JSON.parse(readFileSync(planPath, 'utf8')) }
}

function runExamImportFromQuestionIndex() {
  const child = spawnSync(process.execPath, [resolve(cloudRoot, 'scripts', 'import_exam_papers.mjs'), '--index', resolve(repoRoot, 'data', 'exam_papers', 'question_index.json')], {
    cwd: cloudRoot,
    encoding: 'utf8',
    windowsHide: true,
  })
  assert.equal(child.status, 0, child.stderr || child.stdout)
  return JSON.parse(child.stdout)
}

function executePlan(db, plan) {
  for (const path of plan.sqlPaths) db.exec(readFileSync(path, 'utf8'))
}

function scalar(db, sql) { return Number(db.prepare(sql).get().n) }

test('exam migration is additive and the generated route import is answer-isolated', () => {
  const manifest = JSON.parse(readFileSync(resolve(repoRoot, 'data', 'exam_papers', 'manifest.json'), 'utf8'))
  const answerManifest = JSON.parse(readFileSync(resolve(repoRoot, 'data', 'exam_papers', 'answer_manifest.json'), 'utf8'))
  const activeSources = manifest.sources.filter((source) => source.active !== false)
  const activeRoutes = manifest.routes.filter((route) => route.active !== false)
  const expectedSources = activeSources.length
  const expectedPages = activeSources.reduce((total, source) => total + (source.pages?.length ?? 0), 0)
  const expectedQuestionPages = activeSources.reduce((total, source) => total + (source.pages ?? []).filter((page) => page.question_authority === true).length, 0)
  const expectedAnswerPages = expectedPages - expectedQuestionPages
  const expectedSourcePacks = activeSources.filter((source) => (source.pages ?? []).some((page) => page.question_authority === true && page.page_image_path)).length
  const expectedQuestions = activeRoutes.length
  const expectedEvidence = activeRoutes.reduce((total, route) => total + (route.source_page_evidence?.length ?? 0), 0)
  const activeAnswerSources = answerManifest.sources.filter((source) => activeSources.some((candidate) => candidate.source_id === source.source_id))
  const expectedAnswerSources = activeAnswerSources.reduce((total, source) => total + (source.answers?.length ?? 0), 0)
  const expectedAnswerEvidence = activeAnswerSources.reduce((total, source) => total + (source.answers ?? []).reduce((count, answer) => count + (answer.evidence?.length ?? 0), 0), 0)
  const expectedAnswerPageRows = activeAnswerSources.reduce((total, source) => total + (source.pages?.length ?? 0), 0)
  const expectedAnswerPacks = activeAnswerSources.filter((source) => (source.pages?.length ?? 0) > 0).length
  const transcriptManifest = JSON.parse(readFileSync(resolve(repoRoot, 'data', 'exam_papers', 'transcript_bindings.json'), 'utf8'))
  const expectedTranscriptEvidence = Object.keys(transcriptManifest.evidence_records ?? {}).length
  const expectedTranscriptRoutes = (transcriptManifest.routes ?? []).length
  const expectedTranscriptLinks = (transcriptManifest.routes ?? []).reduce((total, route) => total + (route.required_courses ?? []).reduce((subtotal, course) => subtotal + (course.evidence_ids?.length ?? 0), 0), 0)
  const { summary, plan } = runExamImport()
  assert.equal(summary.schema_version, 'ybt-cloud-exam-import-plan-v1')
  assert.equal(summary.sources, expectedSources)
  assert.equal(summary.pages, expectedPages)
  assert.equal(summary.questionPages, expectedQuestionPages)
  assert.equal(summary.answerPagesExcluded, expectedAnswerPages)
  assert.equal(summary.questions, expectedQuestions)
  assert.ok(summary.routeLinks > 0)
  assert.equal(summary.evidenceLinks, expectedEvidence)
  assert.equal(summary.answerSources, expectedAnswerSources)
  assert.equal(summary.answerEvidence, expectedAnswerEvidence)
  assert.equal(summary.answerPageAssets, expectedAnswerPageRows)
  assert.equal(summary.answerPageObjects, expectedAnswerPacks)
  assert.equal(summary.transcriptEvidence, expectedTranscriptEvidence)
  assert.equal(summary.transcriptLinks, expectedTranscriptLinks)
  assert.equal(summary.transcriptBindingObjects, 1)
  assert.equal(summary.r2Objects, expectedSourcePacks + 1 + expectedAnswerPacks + 1 + 1)
  const compactSummary = runExamImportFromQuestionIndex()
  assert.equal(compactSummary.version, summary.version)
  assert.equal(compactSummary.manifest_sha256, summary.manifest_sha256)

  const db = new DatabaseSync(':memory:')
  db.exec('PRAGMA foreign_keys=ON')
  for (const file of readdirSync(resolve(cloudRoot, 'migrations')).filter((name) => name.endsWith('.sql')).sort()) {
    db.exec(readFileSync(resolve(cloudRoot, 'migrations', file), 'utf8'))
  }
  executePlan(db, plan)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_sources'), expectedSources)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_pages'), expectedPages)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_pages WHERE question_authority=1'), expectedQuestionPages)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_questions'), expectedQuestions)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_question_evidence'), expectedEvidence)
  assert.ok(scalar(db, 'SELECT COUNT(*) AS n FROM exam_route_links') > 0)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_attempts'), 0)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_answer_sources'), expectedAnswerSources)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_answer_evidence'), expectedAnswerEvidence)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_answer_sources WHERE automatic_grading_allowed=1'), 0)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_transcript_evidence'), expectedTranscriptEvidence)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_question_transcript_routes'), expectedTranscriptRoutes)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_question_transcript_links'), expectedTranscriptLinks)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_pages WHERE question_authority=0 AND page_pack_r2_key IS NOT NULL'), 0)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_question_evidence WHERE question_authority=0'), 0)

  // Replaying the same plan must not duplicate routes or source rows.
  executePlan(db, plan)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_sources'), expectedSources)
  assert.equal(scalar(db, 'SELECT COUNT(*) AS n FROM exam_questions'), expectedQuestions)
  assert.ok(scalar(db, 'SELECT COUNT(*) AS n FROM exam_route_links') > 0)

  const pageObjects = plan.objects.filter((object) => object.key.includes('/question-pages.json'))
  assert.equal(pageObjects.length, expectedSourcePacks)
  const answerPages = new Set(activeSources.flatMap((source) => (source.answer_pdf_pages ?? []).map((page) => `${source.source_id}:${page}`)))
  for (const object of pageObjects) {
    const pack = JSON.parse(readFileSync(object.path, 'utf8'))
    assert.equal(pack.schema_version, 'ybt-cloud-exam-page-pack-v1')
    assert.equal(pack.answer_pages_included, false)
    for (const key of Object.keys(pack.pages ?? {})) assert.equal(answerPages.has(key), false)
  }
  const answerObjects = plan.objects.filter((object) => object.key.includes('/answer-pages.json'))
  assert.equal(answerObjects.length, expectedAnswerPacks)
  for (const object of answerObjects) {
    const pack = JSON.parse(readFileSync(object.path, 'utf8'))
    assert.equal(pack.schema_version, 'ybt-cloud-exam-answer-page-pack-v1')
    assert.equal(pack.consumer_guard, 'GRADER_ONLY_SOURCE_EVIDENCE')
    assert.equal(pack.answer_pages_included, true)
    assert.equal(pack.learner_context_forbidden, true)
    for (const key of Object.keys(pack.pages ?? {})) assert.equal(answerPages.has(key), true)
  }
  assert.equal(plan.objects.filter((object) => object.key.endsWith('/answer-index.json')).length, 1)
  db.close()
})

test('worker exposes bidirectional exam routes and optional attempts with explicit isolation', () => {
  const source = readFileSync(resolve(cloudRoot, 'src/index.ts'), 'utf8')
  const migration = readFileSync(resolve(cloudRoot, 'migrations/0007_exam_papers.sql'), 'utf8')
  const answerMigration = readFileSync(resolve(cloudRoot, 'migrations/0008_exam_answer_evidence.sql'), 'utf8')
  const transcriptMigration = readFileSync(resolve(cloudRoot, 'migrations/0009_exam_transcript_bindings.sql'), 'utf8')
  const importer = readFileSync(resolve(cloudRoot, 'scripts/import_exam_papers.mjs'), 'utf8')
  assert.match(migration, /CREATE TABLE IF NOT EXISTS exam_sources/)
  assert.match(migration, /CREATE TABLE IF NOT EXISTS exam_questions/)
  assert.match(migration, /CREATE TABLE IF NOT EXISTS exam_route_links/)
  assert.match(migration, /CREATE TABLE IF NOT EXISTS exam_attempts/)
  assert.doesNotMatch(migration, /answer_text/)
  assert.match(source, /math_get_exam_routes/)
  assert.match(source, /math_get_exam_papers/)
  assert.match(source, /math_get_exam_question/)
  assert.match(source, /math_record_exam_attempt/)
  assert.match(source, /exam_route_locked/)
  assert.match(source, /doesNotWriteLearningProgress/)
  assert.match(source, /answerPagesAreMetadataOnly/)
  assert.match(source, /typeTags/)
  assert.match(importer, /answer_pages_included: false/)
  assert.match(importer, /NOT EXISTS \(SELECT 1 FROM exam_attempts/)
  assert.match(importer, /question_authority/)
  assert.match(answerMigration, /CREATE TABLE IF NOT EXISTS exam_answer_sources/)
  assert.match(answerMigration, /CREATE TABLE IF NOT EXISTS exam_answer_evidence/)
  assert.match(transcriptMigration, /CREATE TABLE IF NOT EXISTS exam_transcript_evidence/)
  assert.match(transcriptMigration, /CREATE TABLE IF NOT EXISTS exam_question_transcript_links/)
  assert.match(transcriptMigration, /CREATE TABLE IF NOT EXISTS exam_question_transcript_routes/)
  assert.match(source, /math_get_exam_answer_sources/)
  assert.match(source, /ybt-cloud-exam-answer-page-pack-v1/)
  assert.match(source, /GRADER_ONLY_SOURCE_EVIDENCE/)
  assert.match(importer, /answerManifestSha/)
  assert.match(importer, /answer_pages_included: true/)
  assert.match(importer, /transcriptBindingFingerprint/)
  assert.match(source, /eligibleForTeacherMethod/)
})
