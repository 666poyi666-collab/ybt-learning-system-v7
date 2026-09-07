import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { resolve, dirname, join } from 'node:path'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
const version = process.argv[2]
if (!/^exam-v1-[a-f0-9]{16}$/.test(version ?? '')) throw new Error('Pass an exact import version')
const plan = JSON.parse(readFileSync(join(root, 'tmp/math-exam-import', version, 'plan.json')))
const output = join(root, 'tmp/exam-r2-verification', version)
mkdirSync(output, { recursive: true })
const hashes = []
for (const [index, object] of plan.objects.entries()) {
  const path = join(output, `${index}.json`)
  const response = spawnSync(process.execPath, [join(root,'cloud/mcp/node_modules/wrangler/bin/wrangler.js'),
    'r2','object','get',`math-learning-content/${object.key}`,'--file',path,'--remote'],
    { cwd: join(root,'cloud/mcp'), encoding: 'utf8', windowsHide: true })
  if (response.status !== 0) throw new Error(`R2 verification failed for ${object.key}`)
  const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex')
  hashes.push({ key: object.key, local_sha256: sha(object.path), remote_sha256: sha(path) })
}
const report = { checked_at: new Date().toISOString(), version, object_count: hashes.length,
  status: hashes.every((x) => x.local_sha256 === x.remote_sha256) ? 'passed' : 'failed', objects: hashes }
writeFileSync(join(root,'reports/all_chapters/exam-r2-verification.json'), JSON.stringify(report,null,2)+'\n')
console.log(JSON.stringify({ status: report.status, version, objects: hashes.length }))
process.exitCode = report.status === 'passed' ? 0 : 1
