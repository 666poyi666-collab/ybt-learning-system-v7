#!/usr/bin/env node

/**
 * Import the source-page-backed exam route manifest into Cloudflare R2/D1.
 *
 * The question manifest is intentionally treated as a route index, not as an
 * answer book.  Question pages and answer pages are imported into separate
 * R2 packs.  The answer pack is grader-only evidence and is never exposed by
 * learner-facing question tools.
 */

import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const scriptDir = dirname(fileURLToPath(import.meta.url))
const cloudRoot = resolve(scriptDir, '..')
const repoRoot = resolve(cloudRoot, '..', '..')
const args = process.argv.slice(2)
const remote = args.includes('--remote')
const indexFlag = args.indexOf('--index')
const indexPath = resolve(indexFlag >= 0 && args[indexFlag + 1]
  ? args[indexFlag + 1]
  : join(repoRoot, 'data', 'exam_papers', 'manifest.json'))
const answerFlag = args.indexOf('--answers')
const answerIndexPath = resolve(answerFlag >= 0 && args[answerFlag + 1]
  ? args[answerFlag + 1]
  : join(repoRoot, 'data', 'exam_papers', 'answer_manifest.json'))
const noAnswers = args.includes('--no-answers')
const transcriptFlag = args.indexOf('--transcripts')
const transcriptIndexPath = resolve(transcriptFlag >= 0 && args[transcriptFlag + 1]
  ? args[transcriptFlag + 1]
  : join(repoRoot, 'data', 'exam_papers', 'transcript_bindings.json'))
const noTranscripts = args.includes('--no-transcripts')
const indexRoot = dirname(indexPath)
const bucket = 'math-learning-content'
const database = 'math-learning'
const wranglerCli = join(cloudRoot, 'node_modules', 'wrangler', 'bin', 'wrangler.js')
const MAX_SQL_CHUNK_BYTES = 1_000_000

function sha256(value) { return createHash('sha256').update(value).digest('hex') }

function sqlString(value) {
  if (value === null || value === undefined) return 'NULL'
  return `'${String(value).replaceAll("'", "''")}'`
}

function sqlNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'NULL'
  return String(Math.trunc(Number(value)))
}

function sqlReal(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'NULL'
  return String(Number(value))
}

function sqlJson(value, fallback = '{}') {
  if (value === null || value === undefined) return sqlString(fallback)
  return sqlString(JSON.stringify(value))
}

function boolNumber(value) { return value ? 1 : 0 }

function chunkStatements(statements, maxBytes = MAX_SQL_CHUNK_BYTES) {
  const chunks = []
  let current = []
  let bytes = 0
  for (const statement of statements) {
    const size = Buffer.byteLength(statement, 'utf8') + 2
    if (current.length && bytes + size > maxBytes) {
      chunks.push(current.join('\n'))
      current = []
      bytes = 0
    }
    current.push(statement)
    bytes += size
  }
  if (current.length) chunks.push(current.join('\n'))
  return chunks
}

function run(command, commandArgs, options = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, commandArgs, {
      cwd: options.cwd ?? repoRoot,
      stdio: options.capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
      shell: false,
      windowsHide: true,
    })
    let stdout = ''
    let stderr = ''
    child.stdout?.on('data', (chunk) => { stdout += chunk.toString() })
    child.stderr?.on('data', (chunk) => { stderr += chunk.toString() })
    child.on('error', reject)
    child.on('close', (code) => code === 0
      ? resolvePromise(options.capture ? stdout : '')
      : reject(new Error(`${command} ${commandArgs.join(' ')} failed with ${code}\n${stderr}`)))
  })
}

async function runWithRetry(command, commandArgs, options = {}, attempts = 5) {
  let lastError
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await run(command, commandArgs, options)
    } catch (error) {
      lastError = error
      if (attempt === attempts) break
      const waitMs = 1000 * 2 ** (attempt - 1)
      console.warn(`retry ${attempt}/${attempts - 1} after ${waitMs}ms`)
      await new Promise((resolvePromise) => setTimeout(resolvePromise, waitMs))
    }
  }
  throw lastError
}

async function fileSha256(path) {
  return sha256(await readFile(path))
}

function sourcePages(source) {
  return Array.isArray(source?.pages) ? source.pages.filter((page) => page && typeof page === 'object') : []
}

function routeList(index) {
  return Array.isArray(index?.routes)
    ? index.routes.filter((route) => route && typeof route === 'object')
    : Object.values(index?.routes ?? {}).filter((route) => route && typeof route === 'object')
}

function sourceList(index) {
  return Array.isArray(index?.sources)
    ? index.sources.filter((source) => source && typeof source === 'object')
    : Object.values(index?.sources ?? {}).filter((source) => source && typeof source === 'object')
}

function listValues(value) {
  return Array.isArray(value) ? value.map((item) => {
    if (item && typeof item === 'object') {
      return String(item.id ?? item.key ?? item.cycle_id ?? item.course_key ?? item.section_key
        ?? item.chapter_id ?? item.section_id ?? item.cycleId ?? item.courseKey ?? item.sectionKey
        ?? item.chapterKey ?? '')
    }
    return String(item ?? '')
  }).filter(Boolean) : []
}

function routeValues(route, ...keys) {
  for (const key of keys) {
    if (route && route[key] !== undefined && route[key] !== null) {
      const values = listValues(route[key])
      if (values.length) return values
    }
  }
  return []
}

function unique(values) { return [...new Set(values)] }

function objectValues(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return []
  return Object.entries(value).map(([key, row]) => ({ key, ...(row && typeof row === 'object' && !Array.isArray(row) ? row : {}) }))
}

function transcriptRoutes(index) {
  return Array.isArray(index?.routes)
    ? index.routes.filter((route) => route && typeof route === 'object')
    : objectValues(index?.routes)
}

function transcriptEvidenceRecords(index) {
  return objectValues(index?.evidence_records)
}

function transcriptStatus(value) {
  const normalized = String(value ?? '').trim().toLowerCase()
  return ['verified', 'review', 'blocked'].includes(normalized) ? normalized : 'blocked'
}

function transcriptBindingInsert(index, bindingFingerprint, manifestSha, indexKey, importedAt) {
  const sources = index?.sources && typeof index.sources === 'object' ? index.sources : {}
  const audit = sources.transcript_audit && typeof sources.transcript_audit === 'object' ? sources.transcript_audit : {}
  const catalog = sources.course_catalog && typeof sources.course_catalog === 'object' ? sources.course_catalog : {}
  const routeCount = Number(index?.summary?.routes ?? transcriptRoutes(index).length)
  const evidenceCount = Number(index?.summary?.evidence_bindings ?? transcriptEvidenceRecords(index).length)
  return `INSERT INTO exam_transcript_imports (binding_fingerprint,exam_manifest_sha256,transcript_audit_sha256,course_catalog_sha256,index_r2_key,route_count,evidence_count,imported_at) VALUES (${sqlString(bindingFingerprint)},${sqlString(manifestSha)},${sqlString(audit.sha256 ?? '')},${sqlString(catalog.sha256 ?? '')},${sqlString(indexKey)},${sqlNumber(routeCount)},${sqlNumber(evidenceCount)},${sqlString(importedAt)}) ON CONFLICT(binding_fingerprint) DO UPDATE SET exam_manifest_sha256=excluded.exam_manifest_sha256,transcript_audit_sha256=excluded.transcript_audit_sha256,course_catalog_sha256=excluded.course_catalog_sha256,index_r2_key=excluded.index_r2_key,route_count=excluded.route_count,evidence_count=excluded.evidence_count,imported_at=excluded.imported_at;`
}

function transcriptEvidenceInsert(record, bindingFingerprint, importedAt) {
  const key = String(record.evidence_key ?? record.key ?? '')
  const fields = [
    'evidence_key', 'evidence_id', 'section_id', 'cycle_id', 'cycle_title', 'course_key', 'relation',
    'semantic_status', 'evidence_method', 'substantive_match_count', 'matched_topic_terms_json',
    'teacher_method_signals_json', 'sentence_indices_json', 'time_spans_json', 'timeline_status',
    'duration_s', 'transcript_file', 'transcript_sha256', 'transcript_text_sha256',
    'catalog_transcript_sha256', 'catalog_transcript_text_sha256', 'source_hashes_match_json',
    'verification_reasons_json', 'eligible_for_teacher_method', 'binding_fingerprint', 'imported_at',
  ]
  const values = [
    key, record.evidence_id ?? '', record.section_id ?? '', record.cycle_id ?? '', record.cycle_title ?? null,
    record.course_key ?? '', record.relation ?? null, record.semantic_status ?? 'none', record.evidence_method ?? null,
    record.substantive_match_count ?? 0, record.matched_topic_terms ?? [], record.teacher_method_signals ?? {},
    record.sentence_indices ?? [], record.time_spans ?? [], record.timeline_status ?? 'not_available',
    record.duration_s ?? null, record.transcript_file ?? null, record.transcript_sha256 ?? null,
    record.transcript_text_sha256 ?? null, record.catalog_transcript_sha256 ?? null,
    record.catalog_transcript_text_sha256 ?? null, record.source_hashes_match ?? {},
    record.verification_reasons ?? [], boolNumber(record.eligible_for_teacher_method === true), bindingFingerprint, importedAt,
  ]
  const jsonFields = new Set([
    'matched_topic_terms_json', 'teacher_method_signals_json', 'sentence_indices_json', 'time_spans_json',
    'source_hashes_match_json', 'verification_reasons_json',
  ])
  const valuesSql = values.map((value, index) => {
    const field = fields[index]
    if (jsonFields.has(field)) return sqlJson(value, field.includes('indices') || field.includes('spans') || field.includes('reasons') || field.includes('terms') ? '[]' : '{}')
    if (field === 'substantive_match_count') return sqlNumber(value)
    if (field === 'duration_s') return sqlReal(value)
    if (field === 'eligible_for_teacher_method') return String(value)
    return sqlString(value)
  })
  const updates = fields.filter((field) => field !== 'evidence_key').map((field) => `${field}=excluded.${field}`).join(',')
  return `INSERT INTO exam_transcript_evidence (${fields.join(',')}) VALUES (${valuesSql.join(',')}) ON CONFLICT(evidence_key) DO UPDATE SET ${updates};`
}

function transcriptRouteInsert(route, bindingFingerprint, importedAt) {
  const questionId = String(route.question_id ?? route.route_id ?? '')
  const fields = ['question_id', 'binding_fingerprint', 'binding_status', 'eligible_for_teacher_method', 'reasons_json', 'unresolved_cycles_json', 'unresolved_courses_json', 'unanchored_cycles_json', 'imported_at']
  const values = [
    questionId, bindingFingerprint, transcriptStatus(route.binding_status), boolNumber(route.eligible_for_teacher_method === true),
    route.reasons ?? [], route.unresolved_cycles ?? [], route.unresolved_courses ?? [], route.unanchored_cycles ?? [], importedAt,
  ]
  const valuesSql = values.map((value, index) => {
    const field = fields[index]
    if (field.endsWith('_json')) return sqlJson(value, '[]')
    if (field === 'eligible_for_teacher_method') return String(value)
    return sqlString(value)
  })
  const updates = fields.filter((field) => field !== 'question_id').map((field) => `${field}=excluded.${field}`).join(',')
  return `INSERT INTO exam_question_transcript_routes (${fields.join(',')}) VALUES (${valuesSql.join(',')}) ON CONFLICT(question_id) DO UPDATE SET ${updates};`
}

function transcriptLinkInsert(questionId, evidenceKey, record, courseBinding, route, ordinal, bindingFingerprint, importedAt) {
  const routeReasons = Array.isArray(route?.reasons) ? route.reasons : []
  const courseReasons = Array.isArray(courseBinding?.reasons) ? courseBinding.reasons : []
  const evidenceReasons = Array.isArray(record?.verification_reasons) ? record.verification_reasons : []
  const status = transcriptStatus(route?.binding_status)
  const eligible = status === 'verified' && courseBinding?.eligible_for_teacher_method === true && record?.eligible_for_teacher_method === true
  return `INSERT INTO exam_question_transcript_links (question_id,evidence_key,cycle_id,course_key,relation,binding_status,eligible_for_teacher_method,reasons_json,ordinal,binding_fingerprint,imported_at) VALUES (${sqlString(questionId)},${sqlString(evidenceKey)},${sqlString(record?.cycle_id ?? courseBinding?.cycle_id ?? '')},${sqlString(record?.course_key ?? courseBinding?.course_key ?? '')},${sqlString(record?.relation ?? courseBinding?.relation ?? null)},${sqlString(status)},${boolNumber(eligible)},${sqlJson(unique([...routeReasons, ...courseReasons, ...evidenceReasons]), '[]')},${sqlNumber(ordinal)},${sqlString(bindingFingerprint)},${sqlString(importedAt)}) ON CONFLICT(question_id,evidence_key) DO UPDATE SET cycle_id=excluded.cycle_id,course_key=excluded.course_key,relation=excluded.relation,binding_status=excluded.binding_status,eligible_for_teacher_method=excluded.eligible_for_teacher_method,reasons_json=excluded.reasons_json,ordinal=excluded.ordinal,binding_fingerprint=excluded.binding_fingerprint,imported_at=excluded.imported_at;`
}

function safeMetadata(value, omit = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const blocked = new Set(omit)
  return Object.fromEntries(Object.entries(value).filter(([key]) => !blocked.has(key)))
}

function isQuestionAuthorityPage(page) {
  if (!page || page.question_authority !== true) return false
  const role = String(page.page_role ?? '').trim().toLowerCase()
  return role === 'question'
}

function safeRelativePath(value) {
  const raw = String(value ?? '').replaceAll('\\', '/')
  if (!raw || raw.startsWith('/') || /^[A-Za-z]:\//.test(raw) || raw.split('/').includes('..')) return null
  return raw
}

function resolveAssetPath(value) {
  const raw = String(value ?? '')
  if (!raw) return resolve(repoRoot, '__missing_exam_asset__')
  // The generated manifest stores repository-relative paths.  Accept an
  // adjacent sidecar/index directory as well so custom manifests remain
  // usable without rewriting their paths.
  const direct = resolve(repoRoot, raw)
  if (raw.startsWith('/') || /^[A-Za-z]:[\\/]/.test(raw) || direct.startsWith(repoRoot) && raw.startsWith('data/')) return direct
  return resolve(indexRoot, raw)
}

async function sectionChapterMap() {
  const map = new Map()
  for (let chapter = 1; chapter <= 5; chapter += 1) {
    try {
      const payload = JSON.parse(await readFile(join(repoRoot, `chapter${chapter}_manifest.json`), 'utf8'))
      for (const section of payload.sections ?? []) {
        const key = String(section.id ?? section.section_key ?? '')
        if (key) map.set(key, String(chapter))
      }
    } catch {
      // A custom/synthetic manifest may not ship chapter manifests.  In that
      // case chapter links are simply omitted; section/cycle/course links stay.
    }
  }
  return map
}

function sourceInsert(source, indexKey, importedAt, manifestSha) {
  const sourceId = String(source.source_id)
  const stableId = String(source.stable_source_id ?? `exam-${source.source_sha256 ?? source.sha256}`)
  const sourceSha = String(source.source_sha256 ?? source.sha256 ?? '')
  return `INSERT INTO exam_sources (source_id,stable_source_id,title,file_name,relative_path,source_sha256,source_role,question_authority,allowlisted,included_for_routes,page_count,question_pdf_pages_json,answer_pdf_pages_json,question_page_ranges_json,answer_page_ranges_json,answer_separation_status,question_completeness,answer_completeness,question_count,manifest_r2_key,manifest_sha256,metadata_json,imported_at) VALUES (${sqlString(sourceId)},${sqlString(stableId)},${sqlString(source.title ?? source.file_name ?? sourceId)},${sqlString(source.file_name ?? sourceId)},${sqlString(safeRelativePath(source.relative_path))},${sqlString(sourceSha)},${sqlString(source.source_role ?? 'question_paper')},${boolNumber(source.question_authority !== false)},${boolNumber(source.allowlisted)},${boolNumber(source.included_for_routes !== false)},${sqlNumber(source.page_count ?? 0)},${sqlJson(source.question_pdf_pages, '[]')},${sqlJson(source.answer_pdf_pages, '[]')},${sqlJson(source.question_page_ranges, '[]')},${sqlJson(source.answer_page_ranges, '[]')},${sqlString(source.answer_separation_status ?? 'needs_review')},${sqlString(source.question_completeness ?? 'unknown')},${sqlString(source.answer_completeness ?? 'unknown')},${sqlNumber(source.question_count ?? 0)},${sqlString(indexKey)},${sqlString(manifestSha)},${sqlJson(safeMetadata(source, ['pages','question_ids','answers','answer_text','solutions','answer_content']))},${sqlString(importedAt)}) ON CONFLICT(source_id) DO UPDATE SET stable_source_id=excluded.stable_source_id,title=excluded.title,file_name=excluded.file_name,relative_path=excluded.relative_path,source_sha256=excluded.source_sha256,source_role=excluded.source_role,question_authority=excluded.question_authority,allowlisted=excluded.allowlisted,included_for_routes=excluded.included_for_routes,page_count=excluded.page_count,question_pdf_pages_json=excluded.question_pdf_pages_json,answer_pdf_pages_json=excluded.answer_pdf_pages_json,question_page_ranges_json=excluded.question_page_ranges_json,answer_page_ranges_json=excluded.answer_page_ranges_json,answer_separation_status=excluded.answer_separation_status,question_completeness=excluded.question_completeness,answer_completeness=excluded.answer_completeness,question_count=excluded.question_count,manifest_r2_key=excluded.manifest_r2_key,manifest_sha256=excluded.manifest_sha256,metadata_json=excluded.metadata_json,imported_at=excluded.imported_at;`
}

function pageInsert(sourceId, page, packKey) {
  const authority = isQuestionAuthorityPage(page)
  const metadata = safeMetadata(page, ['page_image_path'])
  return `INSERT INTO exam_pages (source_id,pdf_page,page_role,question_authority,text_layer_available,text_char_count,text_sha256,ocr_status,ocr_text_sha256,ocr_confidence,visual_status,page_pack_r2_key,page_image_sha256,metadata_json) VALUES (${sqlString(sourceId)},${sqlNumber(page.pdf_page)},${sqlString(page.page_role ?? 'unknown')},${boolNumber(authority)},${boolNumber(page.text_layer_available)},${sqlNumber(page.text_char_count ?? 0)},${sqlString(page.text_sha256)},${sqlString(page.ocr_status)},${sqlString(page.ocr_text_sha256)},${sqlReal(page.ocr_confidence)},${sqlString(page.visual_status ?? 'NEEDS_SOURCE_PAGE_REVIEW')},${authority ? sqlString(packKey) : 'NULL'},${authority ? sqlString(page.page_image_sha256) : 'NULL'},${sqlJson(metadata)}) ON CONFLICT(source_id,pdf_page) DO UPDATE SET page_role=excluded.page_role,question_authority=excluded.question_authority,text_layer_available=excluded.text_layer_available,text_char_count=excluded.text_char_count,text_sha256=excluded.text_sha256,ocr_status=excluded.ocr_status,ocr_text_sha256=excluded.ocr_text_sha256,ocr_confidence=excluded.ocr_confidence,visual_status=excluded.visual_status,page_pack_r2_key=excluded.page_pack_r2_key,page_image_sha256=excluded.page_image_sha256,metadata_json=excluded.metadata_json;`
}

function questionInsert(route, sourceId, manifestKey, importedAt, chapterBySection = new Map()) {
  const routeId = String(route.question_id ?? route.route_id)
  const sections = unique(routeValues(route, 'required_section_ids', 'required_sections', 'section_ids'))
  const chapters = unique([...routeValues(route, 'required_chapter_ids', 'required_chapters', 'chapter_ids'), ...sections.map((section) => chapterBySection.get(section)).filter(Boolean)])
  const needsReview = route.needs_review === true || route.route_status === 'needs_review'
    || route.mapping_status !== 'semantically_verified' || route.route_state !== 'ready_for_optional_unlock'
  const blocked = route.blocked === true || route.route_status === 'blocked' || route.route_state === 'blocked_external_prerequisite'
  const fields = [
    'question_id', 'source_id', 'source_sha256', 'pdf_page', 'pdf_pages_json', 'question_number', 'occurrence',
    'question_ref', 'question_authority', 'stem_text', 'stem_excerpt', 'stem_text_sha256', 'stem_text_status',
    'extraction_method', 'mapping_profiles_json', 'mapping_status', 'mapping_confidence', 'mapping_evidence_json',
    'topic_tags_json', 'type_tags_json', 'required_section_ids_json', 'required_cycle_ids_json',
    'required_course_keys_json', 'required_chapter_ids_json', 'external_prerequisites_json', 'uncertainties_json',
    'blockers_json', 'route_status', 'route_state', 'unlock_status', 'needs_review', 'blocked', 'optional',
    'blocks_ybt_progress', 'unlock_policy_json', 'answer_policy_json', 'topic_summary', 'recommended_path_json',
    'manifest_r2_key', 'imported_at',
  ]
  const values = [
    routeId, sourceId, route.source_sha256 ?? route.source_pdf_sha256, route.pdf_page, route.pdf_pages ?? [],
    route.question_number, route.occurrence ?? 1, route.question_ref ?? routeId,
    route.question_authority ?? 'original_question_page', route.stem_text ?? '', route.stem_excerpt ?? route.stem_text ?? '',
    route.stem_text_sha256, route.stem_text_status ?? 'unavailable', route.extraction_method ?? 'unavailable',
    route.mapping_profiles ?? [], route.mapping_status ?? 'candidate', route.mapping_confidence ?? 'none',
    route.mapping_evidence ?? [], route.topic_tags ?? [], route.type_tags ?? [], sections,
    routeValues(route, 'required_cycle_ids', 'required_cycles', 'cycle_ids'), routeValues(route, 'required_course_keys', 'required_courses', 'course_keys'), chapters,
    route.external_prerequisites ?? [], route.uncertainties ?? [], route.blockers ?? [], route.route_status ?? 'needs_review',
    route.route_state ?? 'needs_review', route.unlock_status ?? 'needs_review', boolNumber(needsReview),
    boolNumber(blocked), boolNumber(route.optional !== false), boolNumber(route.blocks_ybt_progress),
    route.unlock_policy ?? {}, route.answer_policy ?? {}, route.topic_summary ?? '', route.recommended_path ?? [],
    manifestKey, importedAt,
  ]
  const jsonFields = new Set([
    'pdf_pages_json', 'mapping_profiles_json', 'mapping_evidence_json', 'topic_tags_json', 'type_tags_json',
    'required_section_ids_json', 'required_cycle_ids_json', 'required_course_keys_json', 'required_chapter_ids_json',
    'external_prerequisites_json', 'uncertainties_json', 'blockers_json', 'unlock_policy_json', 'answer_policy_json',
    'recommended_path_json',
  ])
  const valuesSql = values.map((value, index) => {
    const field = fields[index]
    return jsonFields.has(field) ? sqlJson(value, field.endsWith('_json') && field.includes('ids') ? '[]' : '{}')
      : field === 'pdf_page' || field === 'question_number' || field === 'occurrence' ? sqlNumber(value)
        : field === 'needs_review' || field === 'blocked' || field === 'optional' || field === 'blocks_ybt_progress' ? String(value)
          : sqlString(value)
  })
  const updates = fields.filter((field) => field !== 'question_id').map((field) => `${field}=excluded.${field}`).join(',')
  return `INSERT INTO exam_questions (${fields.join(',')}) VALUES (${valuesSql.join(',')}) ON CONFLICT(question_id) DO UPDATE SET ${updates};`
}

function routeLinks(route, chapterBySection) {
  const questionId = String(route.question_id ?? route.route_id)
  const links = []
  const add = (type, key, ordinal, confidence, evidence) => {
    if (!key) return
    links.push({ questionId, routeType: type, routeKey: String(key), ordinal, confidence: confidence ?? 'candidate', evidence: evidence ?? {} })
  }
  const mappingConfidence = route.mapping_status === 'semantically_verified' ? 'semantically_verified' : String(route.mapping_confidence || 'candidate')
  const evidence = { mappingStatus: route.mapping_status ?? 'candidate', profiles: route.mapping_profiles ?? [] }
  let ordinal = 0
  for (const key of unique(routeValues(route, 'required_section_ids', 'required_sections', 'section_ids'))) {
    add('section', key, ordinal++, mappingConfidence, evidence)
    const chapter = chapterBySection.get(key)
    if (chapter) add('chapter', chapter, 0, mappingConfidence, evidence)
  }
  for (const key of unique(routeValues(route, 'required_chapter_ids', 'required_chapters', 'chapter_ids'))) add('chapter', key, 0, mappingConfidence, evidence)
  ordinal = 0
  for (const key of unique(routeValues(route, 'required_cycle_ids', 'required_cycles', 'cycle_ids'))) add('cycle', key, ordinal++, mappingConfidence, evidence)
  ordinal = 0
  for (const key of unique(routeValues(route, 'required_course_keys', 'required_courses', 'course_keys'))) add('course', key, ordinal++, mappingConfidence, evidence)
  const seen = new Set()
  return links.filter((link) => {
    const key = `${link.routeType}:${link.routeKey}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function evidenceRows(route, pageMap) {
  const questionId = String(route.question_id ?? route.route_id)
  const rows = []
  for (const raw of route.source_page_evidence ?? []) {
    if (!raw || typeof raw !== 'object') continue
    const page = Number(raw.pdf_page)
    if (!Number.isInteger(page) || page < 1) continue
    const pageRow = pageMap.get(`${route.source_id}:${page}`)
    rows.push({
      questionId,
      sourceId: String(route.source_id),
      pdfPage: page,
      sourcePdfSha256: String(raw.source_pdf_sha256 ?? route.source_pdf_sha256 ?? route.source_sha256 ?? ''),
      pageImageSha256: raw.page_image_sha256 ?? pageRow?.page_image_sha256 ?? null,
      pagePackR2Key: pageRow?.question_authority ? pageRow.page_pack_r2_key : null,
      authority: raw.question_authority === true && pageRow?.question_authority === true,
      evidence: safeMetadata(raw, ['page_image_path']),
    })
  }
  return rows
}

function answerSourceInsert(answer, sourceId, manifestKey, importedAt) {
  const answerId = String(answer.answer_id ?? '')
  if (!answerId) throw new Error('exam answer is missing answer_id')
  const fields = [
    'answer_id', 'source_id', 'source_sha256', 'question_id', 'question_number', 'occurrence', 'answer_ref',
    'answer_text', 'answer_excerpt', 'answer_text_sha256', 'answer_text_status', 'extraction_method',
    'mapping_status', 'mapping_confidence', 'mapping_evidence_json', 'answer_kind', 'review_required',
    'automatic_grading_allowed', 'uncertainties_json', 'active', 'manifest_r2_key', 'imported_at',
  ]
  const values = [
    answerId, sourceId, answer.source_sha256 ?? '', answer.question_id ?? null,
    answer.question_number, answer.occurrence ?? 1, answer.answer_ref ?? answerId,
    answer.answer_text ?? '', answer.answer_excerpt ?? '', answer.answer_text_sha256 ?? null,
    answer.answer_text_status ?? 'unavailable', answer.extraction_method ?? 'unavailable',
    answer.mapping_status ?? 'needs_review', answer.mapping_confidence ?? 'none', answer.mapping_evidence ?? {},
    answer.answer_kind ?? 'reference_solution', boolNumber(answer.review_required !== false),
    // Exam answer pages are reference evidence only.  Keep this hard false
    // even if a hand-edited index attempts to opt into automatic grading.
    0, answer.uncertainties ?? [], boolNumber(answer.active !== false), manifestKey, importedAt,
  ]
  const jsonFields = new Set(['mapping_evidence_json', 'uncertainties_json'])
  const valuesSql = values.map((value, index) => {
    const field = fields[index]
    if (jsonFields.has(field)) return sqlJson(value, field === 'uncertainties_json' ? '[]' : '{}')
    if (['question_number', 'occurrence'].includes(field)) return sqlNumber(value)
    if (['review_required', 'automatic_grading_allowed', 'active'].includes(field)) return String(value)
    return sqlString(value)
  })
  const updates = fields.filter((field) => field !== 'answer_id').map((field) => `${field}=excluded.${field}`).join(',')
  return `INSERT INTO exam_answer_sources (${fields.join(',')}) VALUES (${valuesSql.join(',')}) ON CONFLICT(answer_id) DO UPDATE SET ${updates};`
}

function answerEvidenceInsert(answerId, sourceId, evidence, packKey) {
  const pdfPage = Number(evidence?.pdf_page)
  if (!Number.isInteger(pdfPage) || pdfPage < 1) return null
  const sourceHash = String(evidence?.source_pdf_sha256 ?? '')
  const imageHash = evidence?.page_image_sha256 ?? null
  const assetKey = `${sourceId}:${pdfPage}`
  return `INSERT INTO exam_answer_evidence (answer_id,source_id,pdf_page,source_pdf_sha256,page_image_sha256,page_pack_r2_key,page_asset_key,answer_authority,evidence_json) VALUES (${sqlString(answerId)},${sqlString(sourceId)},${sqlNumber(pdfPage)},${sqlString(sourceHash)},${sqlString(imageHash)},${sqlString(packKey)},${sqlString(assetKey)},1,${sqlJson({ pageRole: 'answer', answerAuthority: true })}) ON CONFLICT(answer_id,source_id,pdf_page) DO UPDATE SET source_pdf_sha256=excluded.source_pdf_sha256,page_image_sha256=excluded.page_image_sha256,page_pack_r2_key=excluded.page_pack_r2_key,page_asset_key=excluded.page_asset_key,evidence_json=excluded.evidence_json;`
}

async function main() {
  let manifestBytes = await readFile(indexPath)
  let manifestSha = sha256(manifestBytes)
  let index = JSON.parse(manifestBytes.toString('utf8'))
  // Accept the compact question index emitted alongside the full manifest.
  // It carries a repository-relative `source_manifest` pointer; using the
  // manifest keeps page-role and original-page evidence intact.
  if (index.schema_version === 'math-exam-question-index-v1' && index.source_manifest) {
    const referenced = String(index.source_manifest)
    const rootCandidate = resolve(repoRoot, referenced)
    const manifestPath = existsSync(rootCandidate) ? rootCandidate : resolve(indexRoot, referenced)
    manifestBytes = await readFile(manifestPath)
    manifestSha = sha256(manifestBytes)
    index = JSON.parse(manifestBytes.toString('utf8'))
  }
  if (index.schema_version !== 'math-exam-paper-manifest-v2') throw new Error(`unsupported exam manifest: ${index.schema_version}`)
  let answerIndex = null
  let answerManifestSha = null
  if (!noAnswers && existsSync(answerIndexPath)) {
    const answerBytes = await readFile(answerIndexPath)
    answerManifestSha = sha256(answerBytes)
    answerIndex = JSON.parse(answerBytes.toString('utf8'))
    if (answerIndex.schema_version !== 'math-exam-answer-manifest-v1') {
      throw new Error(`unsupported exam answer manifest: ${answerIndex.schema_version}`)
    }
    const boundManifestSha = String(answerIndex.source_manifest_sha256 ?? '').toLowerCase()
    if (!boundManifestSha || boundManifestSha !== manifestSha.toLowerCase()) {
      throw new Error('exam answer manifest is stale or bound to a different question manifest')
    }
  }
  let transcriptIndex = null
  let transcriptManifestSha = null
  if (!noTranscripts && existsSync(transcriptIndexPath)) {
    const transcriptBytes = await readFile(transcriptIndexPath)
    transcriptManifestSha = sha256(transcriptBytes)
    transcriptIndex = JSON.parse(transcriptBytes.toString('utf8'))
    if (transcriptIndex.schema_version !== 'math-exam-transcript-bindings-v1') {
      throw new Error(`unsupported exam transcript binding manifest: ${transcriptIndex.schema_version}`)
    }
    const boundManifestSha = String(transcriptIndex.sources?.exam_manifest?.sha256 ?? '').toLowerCase()
    if (!boundManifestSha || boundManifestSha !== manifestSha.toLowerCase()) {
      throw new Error('exam transcript binding manifest is stale or bound to a different question manifest')
    }
  }
  // Historical rows retained by the local incremental manifest are useful for
  // audit, but must not force a re-import of page assets that may no longer be
  // present after a PDF was replaced or removed.
  const allSources = sourceList(index)
  const allRoutes = routeList(index)
  const sources = allSources.filter((source) => source.active !== false)
  const routes = allRoutes.filter((route) => route.active !== false)
  if (!sources.length || !routes.length) throw new Error('exam manifest has no sources or routes')
  const sourceById = new Map(sources.map((source) => [String(source.source_id), source]))
  const answerSourceList = Array.isArray(answerIndex?.sources)
    ? answerIndex.sources.filter((source) => source && typeof source === 'object')
    : Object.values(answerIndex?.sources ?? {}).filter((source) => source && typeof source === 'object')
  const answerSourcesById = new Map(answerSourceList.map((source) => [String(source.source_id), source]))
  if (answerIndex) {
    const missingAnswerSources = sources
      .map((source) => String(source.source_id))
      .filter((sourceId) => !answerSourcesById.has(sourceId))
    if (missingAnswerSources.length) {
      throw new Error(`exam answer manifest missing active sources: ${missingAnswerSources.join(',')}`)
    }
  }
  for (const answerSource of answerSourcesById.values()) {
    const source = sourceById.get(String(answerSource.source_id))
    if (!source) throw new Error(`exam answer manifest references unknown source: ${answerSource.source_id}`)
    const sourceHash = String(source.source_sha256 ?? source.sha256 ?? '').toLowerCase()
    if (sourceHash && String(answerSource.source_sha256 ?? '').toLowerCase() !== sourceHash) {
      throw new Error(`exam answer source hash mismatch: ${answerSource.source_id}`)
    }
    for (const page of Array.isArray(answerSource.pages) ? answerSource.pages : []) {
      const role = String(page?.page_role ?? '').toLowerCase()
      if (page?.question_authority === true || (!role.includes('answer') && !role.includes('解析') && !role.includes('solution'))) {
        throw new Error(`exam answer page is not isolated: ${answerSource.source_id}:${page?.pdf_page ?? ''}`)
      }
    }
  }
  const transcriptEvidence = transcriptEvidenceRecords(transcriptIndex)
  const transcriptRouteRows = transcriptRoutes(transcriptIndex)
  const transcriptEvidenceByKey = new Map(transcriptEvidence.map((record) => [String(record.key ?? record.evidence_key ?? ''), record]))
  const transcriptRouteByQuestion = new Map(transcriptRouteRows.map((route) => [String(route.question_id ?? route.route_id ?? ''), route]))
  const seenQuestionIds = new Set()
  for (const route of routes) {
    const questionId = String(route.question_id ?? route.route_id ?? '')
    const sourceId = String(route.source_id ?? '')
    if (!questionId || seenQuestionIds.has(questionId)) throw new Error(`duplicate or missing exam question id: ${questionId}`)
    seenQuestionIds.add(questionId)
    const source = sourceById.get(sourceId)
    if (!source) throw new Error(`exam route references unknown source: ${sourceId}`)
    const sourceHash = String(source.source_sha256 ?? source.sha256 ?? '').toLowerCase()
    const routeHash = String(route.source_sha256 ?? route.source_pdf_sha256 ?? sourceHash).toLowerCase()
    if (sourceHash && routeHash && sourceHash !== routeHash) throw new Error(`exam route source hash mismatch: ${questionId}`)
  }
  let transcriptFingerprint = null
  if (transcriptIndex) {
    transcriptFingerprint = String(transcriptIndex.binding_fingerprint ?? '').trim()
    if (!/^[0-9a-f]{64}$/i.test(transcriptFingerprint)) throw new Error('exam transcript binding fingerprint is missing or invalid')
    const transcriptSources = transcriptIndex.sources && typeof transcriptIndex.sources === 'object' ? transcriptIndex.sources : {}
    for (const field of ['exam_manifest', 'transcript_audit', 'course_catalog']) {
      const value = String(transcriptSources[field]?.sha256 ?? '').toLowerCase()
      if (!/^[0-9a-f]{64}$/.test(value)) throw new Error(`exam transcript binding source hash missing: ${field}`)
    }
    for (const record of transcriptEvidence) {
      const key = String(record.key ?? record.evidence_key ?? '')
      if (!/^[0-9a-f]{64}$/i.test(key) || String(record.evidence_key ?? key) !== key) throw new Error(`invalid exam transcript evidence key: ${key}`)
      if (!record.cycle_id || !record.course_key || !record.evidence_id) throw new Error(`incomplete exam transcript evidence: ${key}`)
    }
    for (const route of transcriptRouteRows) {
      const questionId = String(route.question_id ?? route.route_id ?? '')
      if (!seenQuestionIds.has(questionId)) throw new Error(`exam transcript binding references unknown question: ${questionId}`)
      if (!['verified', 'review', 'blocked'].includes(transcriptStatus(route.binding_status))) throw new Error(`invalid exam transcript route status: ${questionId}`)
    }
    const missingTranscriptRoutes = routes
      .map((route) => String(route.question_id ?? route.route_id ?? ''))
      .filter((questionId) => !transcriptRouteByQuestion.has(questionId))
    if (missingTranscriptRoutes.length) throw new Error(`exam transcript binding missing routes: ${missingTranscriptRoutes.slice(0, 5).join(',')}`)
  }
  const fingerprint = JSON.stringify({
    schema: index.schema_version,
    routePolicy: index.route_policy,
    sources: allSources.map((source) => [source.source_id, source.active !== false, source.source_sha256 ?? source.sha256, source.question_pdf_pages, source.answer_pdf_pages, source.pages?.map((page) => [page.pdf_page, page.page_role, page.question_authority, page.page_image_sha256])]),
    routes: routes.map((route) => [route.question_id ?? route.route_id, route.pdf_page, route.pdf_pages, route.question_number, route.mapping_status,
      routeValues(route, 'required_section_ids', 'required_sections', 'section_ids'),
      routeValues(route, 'required_cycle_ids', 'required_cycles', 'cycle_ids'),
      routeValues(route, 'required_course_keys', 'required_courses', 'course_keys'),
      routeValues(route, 'required_chapter_ids', 'required_chapters', 'chapter_ids'), route.route_status, route.route_state]),
    answerManifestSha,
    answers: [...answerSourcesById.values()].flatMap((source) => (source.answers ?? []).map((answer) => [answer.answer_id, answer.question_id, answer.question_number, answer.answer_text_sha256, answer.mapping_status, answer.evidence?.map((item) => [item.pdf_page, item.page_image_sha256])])),
    transcriptManifestSha,
    transcriptFingerprint,
  })
  const version = `exam-v1-${sha256(fingerprint).slice(0, 16)}`
  const outputRoot = join(repoRoot, 'tmp', 'math-exam-import', version)
  await rm(outputRoot, { recursive: true, force: true })
  await mkdir(outputRoot, { recursive: true })
  const indexKey = `exams/${version}/index.json`
  const answerIndexKey = `exams/${version}/answer-index.json`
  const transcriptIndexKey = `exams/${version}/transcript-bindings.json`
  const importedAt = new Date().toISOString()
  const chapterBySection = await sectionChapterMap()
  const statements = []
  const objects = []
  const sourcePageMaps = new Map()
  const answerPageMaps = new Map()
  let questionPages = 0
  let answerPagesExcluded = 0
  let answerPageAssets = 0
  let answerEvidenceCount = 0
  let transcriptEvidenceCount = 0
  let transcriptLinkCount = 0

  if (transcriptIndex && transcriptFingerprint) {
    statements.push(transcriptBindingInsert(transcriptIndex, transcriptFingerprint, manifestSha, transcriptIndexKey, importedAt))
    for (const record of transcriptEvidence) {
      statements.push(transcriptEvidenceInsert(record, transcriptFingerprint, importedAt))
      transcriptEvidenceCount += 1
    }
  }

  // Retire entries removed by the incremental local index without deleting
  // their historical question attempts or source records.
  for (const source of allSources.filter((candidate) => candidate.active === false)) {
    statements.push(`UPDATE exam_sources SET included_for_routes=0 WHERE source_id=${sqlString(source.source_id)};`)
  }
  for (const route of allRoutes.filter((candidate) => candidate.active === false)) {
    statements.push(`UPDATE exam_questions SET route_status='retired',route_state='retired',unlock_status='retired',needs_review=1 WHERE question_id=${sqlString(route.question_id ?? route.route_id)};`)
    statements.push(`UPDATE exam_question_transcript_routes SET binding_status='blocked',eligible_for_teacher_method=0 WHERE question_id=${sqlString(route.question_id ?? route.route_id)};`)
    statements.push(`DELETE FROM exam_question_transcript_links WHERE question_id=${sqlString(route.question_id ?? route.route_id)};`)
  }

  for (const source of sources) {
    const sourceId = String(source.source_id)
    statements.push(sourceInsert(source, indexKey, importedAt, manifestSha))
    const pages = sourcePages(source)
    const questionPageRows = pages.filter((page) => isQuestionAuthorityPage(page) && page.page_image_path)
    const packKey = `exams/${version}/${sourceId}/question-pages.json`
    const pack = { schema_version: 'ybt-cloud-exam-page-pack-v1', version, source_id: sourceId, answer_pages_included: false, pages: {} }
    const pageMap = new Map()
    for (const page of pages) {
      const isAuthority = isQuestionAuthorityPage(page)
      if (!isAuthority) answerPagesExcluded += 1
      let pagePackKey = null
      if (isAuthority && page.page_image_path) {
        const imagePath = resolveAssetPath(page.page_image_path)
        const imageBytes = await readFile(imagePath)
        const actualSha = sha256(imageBytes)
        if (page.page_image_sha256 && actualSha !== String(page.page_image_sha256).toLowerCase()) {
          throw new Error(`exam page image hash mismatch: ${imagePath}`)
        }
        const imageSha = String(page.page_image_sha256 ?? actualSha).toLowerCase()
        pack.pages[`${sourceId}:${page.pdf_page}`] = { mimeType: 'image/jpeg', sha256: imageSha, data: imageBytes.toString('base64') }
        pagePackKey = packKey
        questionPages += 1
      }
      const normalized = { ...page, question_authority: isAuthority, page_pack_r2_key: pagePackKey }
      pageMap.set(`${sourceId}:${page.pdf_page}`, normalized)
      statements.push(pageInsert(sourceId, normalized, packKey))
    }
    sourcePageMaps.set(sourceId, pageMap)
    if (questionPageRows.length) {
      const packPath = join(outputRoot, `${sourceId}-question-pages.json`)
      await writeFile(packPath, JSON.stringify(pack), 'utf8')
      objects.push({ key: packKey, path: packPath })
    }

    // Answer assets use a separate, explicitly grader-only R2 pack.  They are
    // never added to the question pack or returned by math_get_exam_question.
    const answerSource = answerSourcesById.get(sourceId)
    const answerPages = Array.isArray(answerSource?.pages)
      ? answerSource.pages.filter((page) => page && typeof page === 'object')
      : []
    const answerPackKey = `exams/${version}/${sourceId}/answer-pages.json`
    const answerPack = {
      schema_version: 'ybt-cloud-exam-answer-page-pack-v1',
      consumer_guard: 'GRADER_ONLY_SOURCE_EVIDENCE',
      version,
      source_id: sourceId,
      answer_pages_included: true,
      learner_context_forbidden: true,
      pages: {},
    }
    const answerPageMap = new Map()
    for (const page of answerPages) {
      const pageNumber = Number(page.pdf_page)
      let pagePackKey = null
      let imageHash = page.page_image_sha256 ?? null
      if (page.page_image_path) {
        const imagePath = resolveAssetPath(page.page_image_path)
        const imageBytes = await readFile(imagePath)
        const actualSha = sha256(imageBytes)
        if (imageHash && actualSha !== String(imageHash).toLowerCase()) {
          throw new Error(`exam answer page image hash mismatch: ${imagePath}`)
        }
        imageHash = String(imageHash ?? actualSha).toLowerCase()
        answerPack.pages[`${sourceId}:${pageNumber}`] = {
          mimeType: 'image/jpeg',
          sha256: imageHash,
          data: imageBytes.toString('base64'),
        }
        pagePackKey = answerPackKey
        answerPageAssets += 1
      }
      const normalized = { ...page, pdf_page: pageNumber, page_pack_r2_key: pagePackKey, page_image_sha256: imageHash }
      answerPageMap.set(`${sourceId}:${pageNumber}`, normalized)
    }
    answerPageMaps.set(sourceId, answerPageMap)
    if (Object.keys(answerPack.pages).length || answerPages.length) {
      const answerPackPath = join(outputRoot, `${sourceId}-answer-pages.json`)
      await writeFile(answerPackPath, JSON.stringify(answerPack), 'utf8')
      objects.push({ key: answerPackKey, path: answerPackPath })
    }
  }

  const compactIndex = {
    schema_version: 'ybt-cloud-exam-index-v1',
    version,
    source_manifest_schema: index.schema_version,
    generated_at: index.generated_at ?? null,
    route_policy: index.route_policy ?? {},
    evidence_policy: {
      question_page_is_authority: true,
      answer_pages_are_metadata_only: true,
      text_is_search_aid_only: true,
      answer_pages_in_r2: 'separate_grader_only_pack',
      answer_index_r2_key: answerIndex ? answerIndexKey : null,
      transcript_bindings_r2_key: transcriptIndex ? transcriptIndexKey : null,
    },
    sources: sources.map((source) => ({
      source_id: source.source_id,
      stable_source_id: source.stable_source_id,
      file_name: source.file_name,
      source_sha256: source.source_sha256 ?? source.sha256,
      page_count: source.page_count,
      question_pdf_pages: source.question_pdf_pages ?? [],
      answer_pdf_pages: source.answer_pdf_pages ?? [],
      question_count: source.question_count ?? 0,
      answer_completeness: source.answer_completeness ?? 'unknown',
      answerEvidenceAvailable: Boolean(answerSourcesById.get(String(source.source_id))),
      answerPagesAreGraderOnly: true,
    })),
    routes,
  }
  const compactPath = join(outputRoot, 'index.json')
  await writeFile(compactPath, JSON.stringify(compactIndex), 'utf8')
  objects.unshift({ key: indexKey, path: compactPath })
  if (answerIndex) {
    // The compact answer index intentionally omits answer text.  Full text is
    // stored in D1 and is returned only by the explicit grader-only tool.
    const compactAnswerIndex = {
      schema_version: 'ybt-cloud-exam-answer-index-v1',
      consumer_guard: 'GRADER_ONLY_SOURCE_EVIDENCE',
      version,
      source_manifest_sha256: manifestSha,
      answer_manifest_sha256: answerManifestSha,
      learner_context_forbidden: true,
      sources: [...answerSourcesById.values()].map((source) => ({
        source_id: source.source_id,
        source_sha256: source.source_sha256,
        answer_pages: source.answer_pages ?? [],
        answer_count: Array.isArray(source.answers) ? source.answers.length : 0,
        mapped_count: Array.isArray(source.answers) ? source.answers.filter((answer) => answer.question_id).length : 0,
        unresolved_count: Array.isArray(source.answers) ? source.answers.filter((answer) => !answer.question_id).length : 0,
      })),
    }
    const compactAnswerPath = join(outputRoot, 'answer-index.json')
    await writeFile(compactAnswerPath, JSON.stringify(compactAnswerIndex), 'utf8')
    objects.push({ key: answerIndexKey, path: compactAnswerPath })
  }
  if (transcriptIndex && transcriptFingerprint) {
    // The binding index contains only hashes, sentence indexes, time spans and
    // method-signal metadata.  It deliberately contains no transcript text or
    // answers, so it is safe as a route/evidence fallback but remains distinct
    // from the learner transcript payload.
    const compactTranscriptIndex = {
      schema_version: 'ybt-cloud-exam-transcript-binding-index-v1',
      consumer_guard: 'ANSWER_SAFE_TEACHER_METHOD_EVIDENCE',
      version,
      binding_fingerprint: transcriptFingerprint,
      source_manifest_sha256: manifestSha,
      transcript_manifest_sha256: transcriptManifestSha,
      sources: transcriptIndex.sources,
      summary: transcriptIndex.summary,
      evidence_records: transcriptEvidence,
      routes: transcriptRouteRows,
      learner_context_forbidden: false,
      transcript_text_included: false,
      answer_content_included: false,
    }
    const compactTranscriptPath = join(outputRoot, 'transcript-bindings.json')
    await writeFile(compactTranscriptPath, JSON.stringify(compactTranscriptIndex), 'utf8')
    objects.push({ key: transcriptIndexKey, path: compactTranscriptPath })
  }

  const desiredQuestionBySource = new Map()
  for (const route of routes) {
    const sourceId = String(route.source_id)
    const questionId = String(route.question_id ?? route.route_id)
    if (!desiredQuestionBySource.has(sourceId)) desiredQuestionBySource.set(sourceId, [])
    desiredQuestionBySource.get(sourceId).push(questionId)
    statements.push(`DELETE FROM exam_route_links WHERE question_id=${sqlString(questionId)};`)
    statements.push(`DELETE FROM exam_question_evidence WHERE question_id=${sqlString(questionId)};`)
    statements.push(questionInsert(route, sourceId, indexKey, importedAt, chapterBySection))
    const links = routeLinks(route, chapterBySection)
    for (const link of links) statements.push(`INSERT OR IGNORE INTO exam_route_links (question_id,route_type,route_key,ordinal,relationship,confidence,evidence_json) VALUES (${sqlString(link.questionId)},${sqlString(link.routeType)},${sqlString(link.routeKey)},${sqlNumber(link.ordinal)},'required',${sqlString(link.confidence)},${sqlJson(link.evidence)});`)
    const pageMap = sourcePageMaps.get(sourceId) ?? new Map()
    for (const evidence of evidenceRows(route, pageMap)) statements.push(`INSERT OR IGNORE INTO exam_question_evidence (question_id,source_id,pdf_page,source_pdf_sha256,page_image_sha256,page_pack_r2_key,question_authority,evidence_json) VALUES (${sqlString(evidence.questionId)},${sqlString(evidence.sourceId)},${sqlNumber(evidence.pdfPage)},${sqlString(evidence.sourcePdfSha256)},${sqlString(evidence.pageImageSha256)},${sqlString(evidence.pagePackR2Key)},${boolNumber(evidence.authority)},${sqlJson(evidence.evidence)});`)
  }

  if (transcriptIndex && transcriptFingerprint) {
    for (const bindingRoute of transcriptRouteRows) {
      const questionId = String(bindingRoute.question_id ?? bindingRoute.route_id ?? '')
      if (!questionId) continue
      statements.push(`DELETE FROM exam_question_transcript_links WHERE question_id=${sqlString(questionId)};`)
      statements.push(transcriptRouteInsert(bindingRoute, transcriptFingerprint, importedAt))
      const cycleOrder = new Map((Array.isArray(bindingRoute.required_cycles) ? bindingRoute.required_cycles : []).map((cycle, index) => [String(cycle?.cycle_id ?? ''), index]))
      const courseBindings = Array.isArray(bindingRoute.required_courses) ? bindingRoute.required_courses : []
      for (const courseBinding of courseBindings) {
        const evidenceIds = Array.isArray(courseBinding?.evidence_ids) ? courseBinding.evidence_ids : []
        for (const evidenceId of evidenceIds) {
          const key = String(evidenceId ?? '')
          const record = transcriptEvidenceByKey.get(key)
          if (!record) throw new Error(`exam transcript route references unknown evidence: ${questionId}:${key}`)
          statements.push(transcriptLinkInsert(questionId, key, record, courseBinding, bindingRoute, cycleOrder.get(String(record.cycle_id ?? '')) ?? 0, transcriptFingerprint, importedAt))
          transcriptLinkCount += 1
        }
      }
    }
  }

  const desiredAnswersBySource = new Map()
  if (answerIndex) {
    for (const answerSource of answerSourcesById.values()) {
      const sourceId = String(answerSource.source_id)
      const answerPackKey = `exams/${version}/${sourceId}/answer-pages.json`
      const answerPageMap = answerPageMaps.get(sourceId) ?? new Map()
      const answers = Array.isArray(answerSource.answers) ? answerSource.answers : []
      const desiredIds = []
      for (const answer of answers) {
        const answerId = String(answer.answer_id ?? '')
        if (!answerId) throw new Error(`exam answer is missing answer_id for source ${sourceId}`)
        desiredIds.push(answerId)
        const questionId = answer.question_id ? String(answer.question_id) : null
        if (questionId && !seenQuestionIds.has(questionId)) {
          throw new Error(`exam answer references unknown question: ${questionId}`)
        }
        statements.push(`DELETE FROM exam_answer_evidence WHERE answer_id=${sqlString(answerId)};`)
        statements.push(answerSourceInsert({ ...answer, question_id: questionId }, sourceId, answerIndexKey, importedAt))
        for (const rawEvidence of Array.isArray(answer.evidence) ? answer.evidence : []) {
          const pageNumber = Number(rawEvidence?.pdf_page)
          const page = answerPageMap.get(`${sourceId}:${pageNumber}`)
          const evidence = {
            ...rawEvidence,
            source_pdf_sha256: rawEvidence?.source_pdf_sha256 || answer.source_sha256 || sourceById.get(sourceId)?.source_sha256,
            page_image_sha256: rawEvidence?.page_image_sha256 || page?.page_image_sha256 || null,
          }
          const sql = answerEvidenceInsert(answerId, sourceId, evidence, page?.page_pack_r2_key ?? (page ? answerPackKey : null))
          if (sql) {
            statements.push(sql)
            answerEvidenceCount += 1
          }
        }
      }
      desiredAnswersBySource.set(sourceId, unique(desiredIds))
    }
  }

  // Remove stale generated rows for a source, but retain any question that has
  // a user attempt so historical evidence remains queryable.
  for (const source of sources) {
    const sourceId = String(source.source_id)
    const desired = unique(desiredQuestionBySource.get(sourceId) ?? [])
    const questionFilter = desired.length
      ? `source_id=${sqlString(sourceId)} AND question_id NOT IN (${desired.map(sqlString).join(',')}) AND NOT EXISTS (SELECT 1 FROM exam_attempts a WHERE a.question_id=exam_questions.question_id)`
      : `source_id=${sqlString(sourceId)} AND NOT EXISTS (SELECT 1 FROM exam_attempts a WHERE a.question_id=exam_questions.question_id)`
    const filter = `${questionFilter} AND NOT EXISTS (SELECT 1 FROM exam_answer_sources a WHERE a.question_id=exam_questions.question_id)`
    statements.push(`DELETE FROM exam_route_links WHERE question_id IN (SELECT question_id FROM exam_questions WHERE ${filter});`)
    statements.push(`DELETE FROM exam_question_evidence WHERE question_id IN (SELECT question_id FROM exam_questions WHERE ${filter});`)
    statements.push(`DELETE FROM exam_question_transcript_links WHERE question_id IN (SELECT question_id FROM exam_questions WHERE ${filter});`)
    statements.push(`DELETE FROM exam_question_transcript_routes WHERE question_id IN (SELECT question_id FROM exam_questions WHERE ${filter});`)
    statements.push(`DELETE FROM exam_questions WHERE ${filter};`)
    const pages = sourcePages(source)
    const desiredPages = pages.map((page) => sqlNumber(page.pdf_page)).filter((value) => value !== 'NULL').join(',')
    if (desiredPages) statements.push(`DELETE FROM exam_pages WHERE source_id=${sqlString(sourceId)} AND pdf_page NOT IN (${desiredPages}) AND NOT EXISTS (SELECT 1 FROM exam_questions q WHERE q.source_id=exam_pages.source_id AND q.pdf_page=exam_pages.pdf_page);`)
    if (answerIndex) {
      const desiredAnswers = desiredAnswersBySource.get(sourceId) ?? []
      if (desiredAnswers.length) {
        statements.push(`UPDATE exam_answer_sources SET active=0,mapping_status='retired',review_required=1,automatic_grading_allowed=0 WHERE source_id=${sqlString(sourceId)} AND answer_id NOT IN (${desiredAnswers.map(sqlString).join(',')});`)
      } else {
        statements.push(`UPDATE exam_answer_sources SET active=0,mapping_status='retired',review_required=1,automatic_grading_allowed=0 WHERE source_id=${sqlString(sourceId)};`)
      }
    }
  }

  const sqlPaths = []
  for (const [number, sql] of chunkStatements(statements).entries()) {
    const path = join(outputRoot, 'sql', `${String(number + 1).padStart(3, '0')}.sql`)
    await mkdir(dirname(path), { recursive: true })
    await writeFile(path, `${sql}\n`, 'utf8')
    sqlPaths.push(path)
  }
  const plan = {
    schema_version: 'ybt-cloud-exam-import-plan-v1',
    version,
    manifest_sha256: manifestSha,
    sources: sources.length,
    pages: sources.reduce((count, source) => count + sourcePages(source).length, 0),
    questionPages,
    answerPagesExcluded,
    answerManifestSha256: answerManifestSha,
    answerSources: answerIndex ? [...answerSourcesById.values()].reduce((count, source) => count + (Array.isArray(source.answers) ? source.answers.length : 0), 0) : 0,
    answerEvidence: answerEvidenceCount,
    answerPageAssets,
    answerPageObjects: answerIndex ? [...answerSourcesById.values()].filter((source) => Array.isArray(source.pages) && source.pages.length > 0).length : 0,
    transcriptManifestSha256: transcriptManifestSha,
    transcriptBindingFingerprint: transcriptFingerprint,
    transcriptEvidence: transcriptEvidenceCount,
    transcriptLinks: transcriptLinkCount,
    transcriptBindingObjects: transcriptIndex ? 1 : 0,
    questions: routes.length,
    routeLinks: statements.filter((statement) => statement.startsWith('INSERT OR IGNORE INTO exam_route_links')).length,
    evidenceLinks: statements.filter((statement) => statement.startsWith('INSERT OR IGNORE INTO exam_question_evidence')).length,
    r2Objects: objects.length,
    sqlChunks: sqlPaths.length,
    objects,
    sqlPaths,
  }
  await writeFile(join(outputRoot, 'plan.json'), JSON.stringify(plan, null, 2), 'utf8')
  console.log(JSON.stringify({ ...plan, objects: undefined, sqlPaths: undefined }, null, 2))
  if (!remote) return
  for (const object of objects) await runWithRetry(process.execPath, [wranglerCli, 'r2', 'object', 'put', `${bucket}/${object.key}`, '--file', object.path, '--content-type', 'application/json', '--remote', '-y'], { cwd: cloudRoot })
  for (const path of sqlPaths) await runWithRetry(process.execPath, [wranglerCli, 'd1', 'execute', database, '--remote', '--file', path, '--yes'], { cwd: cloudRoot })
  console.log(`exam import complete: ${version}`)
}

export { chunkStatements, routeLinks, questionInsert, transcriptEvidenceInsert, transcriptLinkInsert, transcriptRouteInsert }

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error.stack ?? error)
    process.exitCode = 1
  })
}
