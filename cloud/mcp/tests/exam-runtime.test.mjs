import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import { stripTypeScriptTypes } from 'node:module'

// Execute the actual production function bodies (not regex assertions),
// excluding Worker imports that require the Cloudflare host.
const source = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
function loadFunction(name, next, globals = {}) {
  const start = source.indexOf(`async function ${name}(`)
  const end = source.indexOf(next, start)
  assert.ok(start >= 0 && end > start)
  const code = stripTypeScriptTypes(source.slice(start, end))
  return vm.runInNewContext(`${code}; ${name}`, globals)
}

test('current reset state overrides historical completion; absent projections retain fallback', async () => {
  const examCompletion = loadFunction('examCompletion', 'type ExamTitles', {
    USER_ID: 'isolated-test', parseJson: JSON.parse,
    completedStatus: (s) => s === 'completed',
  })
  const calls = []
  const env = { DB: { prepare(sql) { calls.push(sql); return { bind(id) {
    assert.equal(id, 'isolated-test')
    return { async all() { return { results: sql.includes('learner_state')
      ? [{ state_key: 'cycle:1.1-cycle-1', value_json: '{"status":"reset"}' }]
      : [{ event_type: 'cycle_completed', subject_type: 'cycle', subject_id: '1.1-cycle-1' },
         { event_type: 'course_listened', subject_type: 'course', subject_id: 'old-course' }] } } }
  } } } } }
  const state = await examCompletion(env)
  assert.equal(state.cycles.has('1.1-cycle-1'), false)
  assert.equal(state.courses.has('old-course'), true)
  assert.equal(calls.length, 2)
  assert.ok(calls.every((sql) => !/INSERT|UPDATE|DELETE/.test(sql)))
})

test('teacher evidence fails closed when current course content changes', async () => {
  const method = loadFunction('examTeacherMethod', 'async function systemStatus', {
    parseJson: JSON.parse, jsonArray: (s) => JSON.parse(s ?? '[]'),
    examTeacherMethodUnavailable: (reason) => ({ status: 'not_available', reason }),
  })
  const env = { DB: { prepare() { return { bind() { return {
    async first() { return { binding_status: 'verified', eligible_for_teacher_method: 1,
      bound_manifest_sha256: 'same', current_manifest_sha256: 'same' } },
    async all() { return { results: [{ eligible_for_teacher_method: 1,
      transcript_sha256: 'old', current_transcript_sha256: 'new' }] } },
  } } } } } }
  const result = await method(env, 'test-question')
  assert.equal(result.status, 'blocked')
  assert.equal(result.eligibleForTeacherMethod, false)
  assert.equal(result.evidence[0].eligibleForTeacherMethod, false)
  assert.ok(result.reasons.includes('source_binding_stale'))
})

test('teacher lookup does not disguise a database outage as missing content', async () => {
  const method = loadFunction('examTeacherMethod', 'async function systemStatus', {})
  await assert.rejects(() => method({ DB: { prepare() { throw new Error('database unavailable') } } }, 'test'), /database unavailable/)
})
