import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { resolve, dirname, join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
const cloud = join(root, 'cloud/mcp')
const cli = join(cloud, 'node_modules/wrangler/bin/wrangler.js')
const sha = (bytes) => createHash('sha256').update(bytes).digest('hex')
const manifestBytes = readFileSync(join(root, 'data/exam_papers/manifest.json'))
const manifest = JSON.parse(manifestBytes)
function run(args, json = true) {
  let last
  for (let attempt = 0; attempt < 3; attempt++) {
    const result = spawnSync(process.execPath, [cli, ...args], { cwd: cloud, encoding: 'utf8', windowsHide: true })
    if (result.status === 0) return json ? JSON.parse(result.stdout) : result.stdout
    last = result.stderr || result.stdout
  }
  throw new Error(`Cloud verification command failed: ${args.slice(0, 3).join(' ')}: ${String(last).slice(0, 450)}`)
}
const sql = `SELECT
 (SELECT count(*) FROM exam_sources WHERE included_for_routes=1) sources,
 (SELECT count(*) FROM exam_questions WHERE route_status!='retired') questions,
 (SELECT count(*) FROM exam_question_evidence) question_evidence,
 (SELECT count(*) FROM exam_answer_sources WHERE active=1) answer_records,
 (SELECT count(DISTINCT question_id) FROM exam_answer_sources WHERE active=1) questions_with_reference,
 (SELECT count(*) FROM exam_answer_evidence) answer_evidence,
 (SELECT count(*) FROM exam_question_transcript_routes) teacher_routes,
 (SELECT count(*) FROM exam_sources WHERE included_for_routes=1 AND manifest_sha256!='${sha(manifestBytes)}') stale_manifests,
 (SELECT count(*) FROM exam_question_evidence e JOIN exam_pages p ON p.source_id=e.source_id AND p.pdf_page=e.pdf_page WHERE p.page_role!='question') answer_leak,
 (SELECT count(*) FROM learning_events) learning_events,
 (SELECT count(*) FROM learner_state) learner_state,
 (SELECT count(*) FROM exam_attempts) exam_attempts`
const live = run(['d1','execute','math-learning','--remote','--command',sql,'--json'])[0].results[0]
const backup = new DatabaseSync(':memory:')
backup.exec(readFileSync(join(root, 'tmp/math-before-exam-upgrade.sql'), 'utf8'))
const before = Object.fromEntries(['learning_events','learner_state','exam_attempts'].map((table) => [table, Number(backup.prepare(`SELECT count(*) n FROM ${table}`).get().n)]))
const canonicalRows = (rows) => JSON.stringify(rows.map((row) => JSON.stringify(Object.fromEntries(Object.keys(row).sort().map((key) => [key, row[key]])))).sort())
const progressHashes = {}
for (const table of Object.keys(before)) {
  const prior = backup.prepare(`SELECT * FROM ${table}`).all()
  const current = run(['d1','execute','math-learning','--remote','--command',`SELECT * FROM ${table}`,'--json'])[0].results
  progressHashes[table] = { before: sha(canonicalRows(prior)), after: sha(canonicalRows(current)) }
}
backup.close()
const progressUnchanged = Object.entries(before).every(([key, value]) => Number(live[key]) === value && progressHashes[key].before === progressHashes[key].after)
const base = 'https://math-learning-mcp.focuslink-poyi-6465e9.workers.dev'
const http = {}
for (const path of ['/healthz','/readyz','/.well-known/oauth-protected-resource','/mcp']) {
  const response = await fetch(base + path)
  http[path] = response.status
}
const report = {
  checked_at: new Date().toISOString(), manifest_sha256: sha(manifestBytes),
  live, progress_counts_before: before, progress_counts_unchanged: progressUnchanged,
  progress_hashes: progressHashes,
  progress_verification_scope: 'All rows compared by canonical SHA-256; no simulated writes or completion events created.',
  http, authenticated_chatgpt_session_tested: false,
  status: Number(live.sources) === manifest.sources.length && Number(live.questions) === manifest.routes.length
    && Number(live.stale_manifests) === 0 && Number(live.answer_leak) === 0 && progressUnchanged
    && http['/healthz'] === 200 && http['/readyz'] === 200 && http['/mcp'] === 401 ? 'passed' : 'failed',
}
mkdirSync(join(root, 'reports/all_chapters'), { recursive: true })
writeFileSync(join(root, 'reports/all_chapters/exam-cloud-production-verification.json'), JSON.stringify(report, null, 2) + '\n')
console.log(JSON.stringify(report, null, 2))
process.exitCode = report.status === 'passed' ? 0 : 1
