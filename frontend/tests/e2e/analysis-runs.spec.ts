import { expect, test, type Page } from '@playwright/test'

const dataset = {
  id: 'dataset-1',
  name: 'HX-ZY-260731-A1',
  pair_key: 'sample_{R}.fq.gz',
  total_size: 11642288355,
  total_size_label: '10.8 GiB',
  files: [
    { mate: 1, name: 'sample_R1.fq.gz', relative_path: 'sample_R1.fq.gz', size: 1, size_label: '5.3 GiB' },
    { mate: 2, name: 'sample_R2.fq.gz', relative_path: 'sample_R2.fq.gz', size: 1, size_label: '5.5 GiB' },
  ],
}

const workflow = {
  slug: 'solidtumorsingle',
  name: '实体瘤单样本',
  workflow_name: 'SolidTumorSingle',
  mode: 'single',
  description: '单样本分析',
  asset_name: 'SolidTumorSingle',
  revision: 3,
  digest: 'sha256:test',
  ready: true,
  diagnostic_count: 0,
  blockers: [],
}

function catalog(referenceReady = true) {
  const missing = referenceReady
    ? []
    : [{ path: 'hg19/reference/hg19.simp.fa', label: 'hg19 FASTA', kind: 'file', present: false }]
  return {
    rawdata_directory: 'workspace/rawdata',
    database_directory: 'workspace/databases',
    datasets: [dataset],
    workflows: [workflow, {
      ...workflow,
      slug: 'solidtumorpair',
      name: '实体瘤配对样本',
      workflow_name: 'SolidTumorPiar',
      mode: 'paired',
    }],
    database: {
      schema_version: 1,
      error: null,
      references: [{
        id: 'hg19', name: 'hg19 / GRCh37', ref_version: 'hg19', ready: referenceReady,
        requirements: missing, missing,
      }],
      panels: [{
        id: 'tumor-120-v4', name: '实体瘤 120 V4', reference: 'hg19', ready: true,
        requirements: [], missing: [],
      }],
    },
  }
}

function run(status = 'queued') {
  return {
    id: '8e8bd4d3-8c28-45ff-9c99-6ddc11e80e0a',
    workflow: {
      slug: 'solidtumorsingle', name: 'SolidTumorSingle', workflow_name: 'SolidTumorSingle',
      revision: 3, digest: 'sha256:test',
    },
    sample_id: 'HX-ZY-260731-A1',
    sample_name: 'HX-ZY-260731-A1',
    actor: 'zhuqin',
    status,
    progress: status === 'queued' ? 0 : 28,
    current_step: status === 'queued' ? '等待执行' : 'QC',
    request: {
      dataset_name: 'HX-ZY-260731-A1',
      control_dataset_name: null,
      reference_name: 'hg19 / GRCh37',
      panel_name: '实体瘤 120 V4',
      sample_type: 'tissue',
      sample_gender: '女',
    },
    error: '',
    outputs: [],
    events: [{
      id: 1, kind: 'status', level: 'info', message: '运行已进入队列。', details: {},
      created_at: '2026-08-04T08:00:00Z',
    }],
    created_at: '2026-08-04T08:00:00Z',
    started_at: null,
    finished_at: null,
    updated_at: '2026-08-04T08:00:00Z',
  }
}

async function mockAuth(page: Page) {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      user: {
        username: 'zhuqin', is_admin: true, role: 'admin',
        allowed_sections: ['edit', 'tools', 'packages', 'artifacts', 'runs', 'wdl', 'help'],
      },
    }),
  }))
}

async function mockOperatorAuth(page: Page) {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      user: {
        username: 'chaohuaiyu', is_admin: false, role: 'analysis_operator',
        allowed_sections: ['runs'],
      },
    }),
  }))
}

test('运行操作员进入其他页面时回到运行分析且只显示该菜单', async ({ page }) => {
  await mockOperatorAuth(page)
  await page.route('**/api/v1/analysis/catalog', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog()),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [] }),
  }))

  await page.goto('/wdl')

  await expect(page).toHaveURL(/\/runs$/)
  await expect(page.getByRole('button', { name: '运行分析' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'WDL 工作台' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '工具库' })).toHaveCount(0)
})

test('已完成运行展示总耗时和各 task 时间轴', async ({ page }) => {
  await mockAuth(page)
  const completedRun = {
    ...run('succeeded'),
    current_step: '分析完成',
    progress: 100,
    timing: {
      queue_seconds: 3.2,
      total_seconds: 117.4,
      execution_seconds: 114.5,
      task_seconds: 110.2,
      cached_tasks: 1,
      tasks: [
        {
          id: 'call-QC', name: 'QC', call: 'call-QC', status: 'succeeded', cached: true,
          offset_seconds: 0.1, duration_seconds: 0.08,
        },
        {
          id: 'call-Collect', name: 'Collect', call: 'call-Collect', status: 'succeeded', cached: false,
          offset_seconds: 4.5, duration_seconds: 109.0,
        },
      ],
    },
  }
  await page.route('**/api/v1/analysis/catalog', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog()),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [completedRun] }),
  }))
  await page.route('**/api/v1/analysis-runs/*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(completedRun),
  }))

  await page.goto('/runs')

  await expect(page.getByRole('heading', { name: '耗时' })).toBeVisible()
  await expect(page.getByText('1 分 57 秒')).toBeVisible()
  await expect(page.getByText('QC', { exact: true })).toBeVisible()
  await expect(page.getByText('完成 · 缓存')).toBeVisible()
  await expect(page.getByLabel('Collect，1 分 49 秒')).toBeVisible()
})

test('运行页选择原始数据和流程后提交并展示排队状态', async ({ page }) => {
  await mockAuth(page)
  await page.route('**/api/v1/analysis/catalog', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog()),
  }))
  await page.route('**/api/v1/analysis-runs', async (route) => {
    if (route.request().method() === 'POST') {
      const payload = route.request().postDataJSON()
      expect(payload.workflow).toBe('solidtumorsingle')
      expect(payload.dataset).toBe('dataset-1')
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(run()) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results: [] }) })
  })
  await page.route('**/api/v1/analysis-runs/*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(run('running')),
  }))

  await page.goto('/runs')

  await expect(page.getByRole('heading', { name: '运行分析' })).toBeVisible()
  await expect(page.getByLabel('分析样本')).toContainText('HX-ZY-260731-A1')
  await expect(page.getByText('sample_R1.fq.gz')).toBeVisible()
  await expect(page.getByText('实体瘤单样本')).toBeVisible()
  await expect(page.getByText('数据库检查通过。')).toBeVisible()
  await page.getByRole('button', { name: '开始分析' }).click()
  await expect(page.getByRole('heading', { name: 'HX-ZY-260731-A1' })).toBeVisible()
  await expect(page.getByText('排队中')).toBeVisible()
  await expect(page.getByText('运行已进入队列。')).toBeVisible()
})

test('数据库缺失项清晰可见且不会允许提交', async ({ page }) => {
  await mockAuth(page)
  await page.route('**/api/v1/analysis/catalog', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog(false)),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [] }),
  }))

  await page.goto('/runs')

  await expect(page.getByText('数据库还缺 1 项')).toBeVisible()
  const missingResources = page.locator('.analysis-missing-resources')
  if ((await missingResources.getAttribute('open')) === null) {
    await page.getByText('数据库还缺 1 项').click()
  }
  const missingPath = page.getByText('hg19/reference/hg19.simp.fa')
  await missingPath.scrollIntoViewIfNeeded()
  await expect(missingPath).toBeVisible()
  await expect(page.getByRole('button', { name: '开始分析' })).toBeDisabled()
})
