import { expect, test, type Page } from '@playwright/test'

const runId = '11111111-1111-4111-8111-111111111111'
const analysisId = '22222222-2222-4222-8222-222222222222'
const admin = { username: 'admin', is_admin: true, role: 'admin', allowed_sections: ['overview', 'edit', 'runs', 'rawdata'] }
const run = {
  id: runId, external_ref: { client_id: 'okb', external_run_id: 'task-test-1', external_analysis_id: analysisId },
  analysis_product: { analysis_code: 'test-product', contract_version: '1.0', contract_digest: 'sha256:test' },
  workflow: { version_id: 1, slug: 'test-workflow', version: 1, source_digest: 'sha256:source' },
  status: 'succeeded', status_version: 4, output_status: 'complete', current_step: '分析完成', progress: 100,
  attempt: 1, retry_of: null, actor: 'test-service', error: null, outputs: [], timing: { tasks: [], total_seconds: 60 },
  created_at: '2026-10-01T00:00:00Z', started_at: '2026-10-01T00:00:01Z', finished_at: '2026-10-01T00:01:01Z', updated_at: '2026-10-01T00:01:01Z',
}
const metadata = { ...run, sample_id: 'TEST-REAL-RECORD', sample_name: '', execution_engine: 'nextflow', workflow: { name: 'Registered workflow' } }
async function mock(page: Page, status = 'succeeded') {
  const current = { ...run, status, output_status: status === 'succeeded' ? 'complete' : 'pending', current_step: status === 'queued' ? '等待 worker 领取' : status === 'failed' ? 'INPUT_CHANGED' : '分析完成' }
  const mutations: string[] = []
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    const reply = (data: unknown, code = 200) => route.fulfill({ status: code, contentType: 'application/json', body: JSON.stringify(data) })
    if (path === '/api/v1/auth/me') return reply({ user: admin })
    if (path === '/api/v1/integration/analysis-products') return reply({ results: [] })
    if (path === '/api/v1/integration/analysis-runs') return reply({ results: [current] })
    if (path === '/api/v1/analysis-runs') return reply({ results: [{ ...metadata, status }] })
    if (path === `/api/v1/integration/analysis-runs/${runId}/cancel`) {
      mutations.push(path)
      expect(route.request().method()).toBe('POST')
      current.status = 'canceled'; current.current_step = '任务已取消'
      return reply(current)
    }
    if (path === `/api/v1/integration/analysis-runs/${runId}`) return reply(current)
    if (path === `/api/v1/analysis-runs/${runId}`) return reply({ ...metadata, status: current.status })
    if (path.endsWith('/events')) return reply({ results: [], next_after_id: 0 })
    return reply({ error: { message: 'Test endpoint not supplied' } }, 404)
  })
  return mutations
}

test('运维首页按接口显示记录和零排队，不填充模拟任务', async ({ page }) => {
  await mock(page)
  await page.goto('/operations')
  await expect(page.locator('.ops-summary-grid').getByRole('button', { name: '排队中' })).toContainText('0')
  await expect(page.locator('.ops-summary-grid').getByRole('button', { name: '分析完成' })).toContainText('1')
  await expect(page.getByText('TEST-REAL-RECORD', { exact: true })).toBeVisible()
  await expect(page.getByText(/DEMO-004|演练设置|设计预览/)).toHaveCount(0)
  await page.getByRole('button', { name: '查看 TEST-REAL-RECORD 详情' }).click()
  await expect(page.getByRole('heading', { name: 'TEST-REAL-RECORD' })).toBeVisible()
  await expect(page.getByText('Nextflow', { exact: true })).toBeVisible()
})

test('接口失败明确报错，不显示正常或零任务', async ({ page }) => {
  await mock(page)
  await page.route('**/api/v1/integration/analysis-runs', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: '真实服务暂不可用' } }) }))
  await page.goto('/operations')
  await expect(page.getByRole('alert')).toContainText('真实服务暂不可用')
  await expect(page.locator('.ops-summary-grid strong').first()).toHaveText('—')
  await expect(page.getByText('已读取的记录中没有待处理异常。')).toHaveCount(0)
})

test('取消调用真实协议并以后端状态确认', async ({ page }) => {
  const mutations = await mock(page, 'queued')
  await page.goto(`/operations?view=runs&run=${runId}`)
  await page.getByRole('button', { name: '取消任务', exact: true }).click()
  await page.getByRole('button', { name: '确认取消任务', exact: true }).click()
  await expect(page.locator('.ops-detail-heading .ops-status')).toHaveText('已取消')
  expect(mutations).toHaveLength(1)
})

test('业务重跑不在浏览器创建脱离上游的任务', async ({ page }) => {
  await mock(page, 'failed')
  const writes: string[] = []
  page.on('request', request => { if (request.method() === 'POST') writes.push(request.url()) })
  await page.goto(`/operations?view=runs&run=${runId}`)
  await page.getByRole('button', { name: '前往业务系统重跑', exact: true }).click()
  await expect(page.getByRole('dialog')).toContainText('保持样本、分析结果和报告的关联')
  expect(writes).toHaveLength(0)
})
