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

const publishedWorkflow = {
  ...workflow,
  slug: 'published:fastp_bwa_demo:2',
  source_slug: 'fastp_bwa_demo',
  source_type: 'workflow_version',
  name: 'fastp → BWA-MEM demo',
  workflow_name: 'fastp_bwa_demo',
  description: '流程库中的固定发布版本',
  revision: 2,
  digest: 'sha256:published',
  requires_reference: false,
  requires_panel: false,
  graph_summary: {
    node_count: 4,
    edge_count: 3,
    input_count: 2,
    tool_count: 1,
    subworkflow_count: 0,
    output_count: 1,
    tools: [{ id: 'fastp', name: 'fastp', version: '0.23.4' }],
    subworkflows: [],
  },
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
    }, publishedWorkflow],
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

function publishedRun(status = 'succeeded') {
  return {
    ...run(status),
    workflow: {
      slug: 'fastp_bwa_demo',
      name: 'fastp → BWA-MEM demo',
      workflow_name: 'fastp_bwa_demo',
      revision: 2,
      digest: 'sha256:published',
      source_type: 'workflow_version',
      graph_summary: publishedWorkflow.graph_summary,
    },
  }
}

async function mockAuth(page: Page) {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      user: {
        username: 'zhuqin', is_admin: true, role: 'admin',
        allowed_sections: ['overview', 'edit', 'tools', 'packages', 'artifacts', 'resources', 'rawdata', 'runs', 'wdl', 'help'],
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
        allowed_sections: ['rawdata', 'runs'],
      },
    }),
  }))
}

test('运行操作员进入其他页面时回到运行分析且只显示数据与运行菜单', async ({ page }) => {
  const hydrationMessages: string[] = []
  page.on('console', (message) => {
    if (message.text().includes('Hydration')) hydrationMessages.push(message.text())
  })
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
  await expect(page.getByRole('button', { name: '原始数据' })).toBeVisible()
  await expect(page.getByRole('button', { name: '运行分析' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'WDL 工作台' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '工具库' })).toHaveCount(0)
  expect(hydrationMessages).toEqual([])
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

test('运行记录可回到对应的已发布流程版本', async ({ page }) => {
  await mockAuth(page)
  const item = publishedRun()
  let catalogRequestUrl = ''
  await page.route('**/api/v1/analysis/catalog*', (route) => {
    catalogRequestUrl = route.request().url()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(catalog()),
    })
  })
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [item] }),
  }))
  await page.route('**/api/v1/analysis-runs/*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(item),
  }))

  await page.goto(`/runs?workflow=fastp_bwa_demo&revision=2&run=${item.id}`)
  await expect(page).toHaveURL(new RegExp(`run=${item.id}`))
  await expect(page.locator('.analysis-run-list')).toContainText('v2 · 发布版')

  const setup = page.locator('.analysis-setup')
  await expect(setup.locator('input[type="radio"][value="published:fastp_bwa_demo:2"]')).toBeChecked()
  expect(catalogRequestUrl).toContain('workflow=fastp_bwa_demo')
  expect(catalogRequestUrl).toContain('revision=2')
  const sourceLink = setup.getByRole('link', { name: '查看发布版本', exact: true })
  await expect(sourceLink).toHaveAttribute('href', '/?section=artifacts&workflow=fastp_bwa_demo&workflowVersion=2')
  await expect(setup.getByLabel('固定流程结构摘要')).toContainText('1 工具')
  await setup.getByText('核对固定内容', { exact: true }).click()
  await expect(setup.getByText('工具 v0.23.4')).toBeVisible()

  const link = page.getByRole('link', { name: /fastp → BWA-MEM demo · v2/ })
  await expect(link).toBeVisible()
  await expect(link).toHaveAttribute('href', '/?section=artifacts&workflow=fastp_bwa_demo&workflowVersion=2')
  await page.getByText('本次固定的流程结构', { exact: true }).click()
  await expect(page.locator('.analysis-run-source-details')).toContainText('fastp')
})

test('从历史 WDL 修订进入运行页时精确投递该修订', async ({ page }) => {
  await mockAuth(page)
  const historicalWorkflow = {
    ...workflow,
    slug: 'wdl-asset:solidtumorsingle:1',
    source_slug: 'solidtumorsingle',
    source_type: 'wdl_asset',
    revision: 1,
    digest: 'sha256:historical-v1',
  }
  const scopedCatalog = catalog()
  scopedCatalog.workflows = [workflow, historicalWorkflow, publishedWorkflow]
  let catalogRequestUrl = ''
  let submitRequest: Record<string, any> | undefined
  await page.route('**/api/v1/analysis/catalog*', (route) => {
    catalogRequestUrl = route.request().url()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(scopedCatalog),
    })
  })
  await page.route('**/api/v1/analysis-runs', (route) => {
    if (route.request().method() === 'POST') {
      submitRequest = route.request().postDataJSON()
      const item = {
        ...run('queued'),
        workflow: {
          slug: 'solidtumorsingle',
          name: 'SolidTumorSingle',
          workflow_name: 'SolidTumorSingle',
          revision: 1,
          digest: 'sha256:historical-v1',
          source_type: 'wdl_asset',
        },
      }
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify(item),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [] }),
    })
  })

  await page.goto('/runs?workflow=solidtumorsingle&revision=1')

  const setup = page.locator('.analysis-setup')
  await expect(
    setup.locator('input[type="radio"][value="wdl-asset:solidtumorsingle:1"]'),
  ).toBeChecked()
  expect(catalogRequestUrl).toContain('workflow=solidtumorsingle')
  expect(catalogRequestUrl).toContain('revision=1')
  await expect(setup.getByRole('link', { name: '查看历史 WDL', exact: true })).toHaveAttribute(
    'href',
    '/wdl/solidtumorsingle?revision=1',
  )
  await setup.getByRole('button', { name: '开始分析', exact: true }).click()
  expect(submitRequest?.workflow).toBe('wdl-asset:solidtumorsingle:1')
})

test('从流程版本进入运行页时只显示该版本的记录', async ({ page }) => {
  await mockAuth(page)
  const unrelated = run('succeeded')
  const matching = {
    ...publishedRun('succeeded'),
    id: '89ec2974-c895-43ba-9aa3-7db70a44c215',
    sample_id: 'FASTP-DEMO-01',
    sample_name: 'FASTP-DEMO-01',
  }
  await page.route('**/api/v1/analysis/catalog*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog()),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [unrelated, matching] }),
  }))
  await page.route('**/api/v1/analysis-runs/*', (route) => {
    const item = route.request().url().includes(matching.id) ? matching : unrelated
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(item),
    })
  })

  await page.goto('/runs?workflow=fastp_bwa_demo&revision=2')

  const runList = page.locator('.analysis-run-list')
  await expect(runList.getByRole('heading', { name: 'fastp → BWA-MEM demo · v2' })).toBeVisible()
  await expect(runList.getByText('FASTP-DEMO-01', { exact: true })).toBeVisible()
  await expect(runList.getByText('HX-ZY-260731-A1', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('heading', { name: 'FASTP-DEMO-01' })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(runList.getByRole('button', { name: '查看全部' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390)
  await page.setViewportSize({ width: 1280, height: 720 })

  await runList.getByRole('button', { name: '查看全部' }).click()
  await expect(runList.getByRole('heading', { name: '记录', exact: true })).toBeVisible()
  await expect(runList.getByText('FASTP-DEMO-01', { exact: true })).toBeVisible()
  await expect(runList.getByText('HX-ZY-260731-A1', { exact: true })).toBeVisible()
  await expect(page).not.toHaveURL(/workflow=/)
})

test('流程版本没有运行记录时显示明确空状态', async ({ page }) => {
  await mockAuth(page)
  await page.route('**/api/v1/analysis/catalog*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog()),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [run('succeeded')] }),
  }))

  await page.goto('/runs?workflow=fastp_bwa_demo&revision=2')

  await expect(page.locator('.analysis-run-list').getByText('这个流程版本还没有运行记录。')).toBeVisible()
  await expect(page.locator('.analysis-run-content')).toContainText('选择配置后开始分析')
})

test('运行记录可通过共享 URL 比较流程版本、工具和耗时', async ({ page }) => {
  const hydrationErrors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().includes('Hydration')) {
      hydrationErrors.push(message.text())
    }
  })
  await mockAuth(page)
  const primary = {
    ...run('succeeded'),
    timing: {
      total_seconds: 117.4,
      execution_seconds: 114.5,
      cached_tasks: 1,
      tasks: [{
        id: 'call-QC', name: 'QC', call: 'call-QC', status: 'succeeded', cached: true,
        offset_seconds: 0, duration_seconds: 10,
      }],
    },
  }
  const comparison = {
    ...publishedRun('succeeded'),
    id: '6e9d5399-4f9d-4d63-91c3-c6a3f4ef55eb',
    sample_id: 'HX-ZY-260731-A2',
    sample_name: 'HX-ZY-260731-A2',
    request: {
      ...run().request,
      dataset_name: 'HX-ZY-260731-A2',
      reference_name: null,
      panel_name: null,
    },
    timing: {
      total_seconds: 78.2,
      execution_seconds: 75.8,
      cached_tasks: 0,
      tasks: [{
        id: 'call-fastp', name: 'fastp', call: 'call-fastp', status: 'succeeded', cached: false,
        offset_seconds: 0, duration_seconds: 72,
      }],
    },
  }

  await page.route('**/api/v1/analysis/catalog*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog()),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [primary, comparison] }),
  }))
  await page.route('**/api/v1/analysis-runs/*', (route) => {
    const item = route.request().url().includes(comparison.id) ? comparison : primary
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(item),
    })
  })

  await page.goto(`/runs?run=${primary.id}&compare=${comparison.id}`)

  const comparePanel = page.getByRole('region', { name: '运行对比' })
  await expect(comparePanel).toBeVisible()
  await expect(comparePanel.getByRole('columnheader', { name: /当前运行 HX-ZY-260731-A1/ })).toBeVisible()
  await expect(comparePanel.getByRole('columnheader', { name: /对比运行 HX-ZY-260731-A2/ })).toBeVisible()
  await expect(comparePanel.getByRole('row', { name: /流程版本/ })).toContainText('不同')
  await expect(comparePanel.getByRole('row', { name: /固定工具版本/ })).toContainText('fastp v0.23.4')
  await expect(comparePanel.getByRole('row', { name: /流程总耗时/ })).toContainText('1 分 57 秒')
  await expect(comparePanel.getByRole('row', { name: /流程总耗时/ })).toContainText('1 分 18 秒')

  await comparePanel.getByRole('button', { name: '关闭对比' }).click()
  await expect(comparePanel).toHaveCount(0)
  await expect(page).not.toHaveURL(/compare=/)

  await page.getByRole('button', { name: '对比 HX-ZY-260731-A2' }).click()
  await expect(comparePanel).toBeFocused()
  await expect(page).toHaveURL(new RegExp(`compare=${comparison.id}`))
  expect(hydrationErrors).toEqual([])
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

test('原始数据入口指定的数据集会被选中并保留在共享地址中', async ({ page }) => {
  await mockAuth(page)
  const secondDataset = {
    ...dataset,
    id: 'dataset-2',
    name: 'HX-ZY-260731-A2',
    pair_key: 'sample-a2_{R}.fq.gz',
  }
  const scopedCatalog = catalog()
  scopedCatalog.datasets = [dataset, secondDataset]
  await page.route('**/api/v1/analysis/catalog', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(scopedCatalog),
  }))
  await page.route('**/api/v1/analysis-runs', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [] }),
  }))

  await page.goto('/runs?dataset=dataset-2')

  await expect(page.getByLabel('分析样本')).toHaveValue('dataset-2')
  await expect(page).toHaveURL(/dataset=dataset-2/)
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
