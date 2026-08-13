import { expect, test, type Page } from '@playwright/test'

const readyDataset = {
  id: 'ready-dataset',
  name: 'SAMPLE01',
  pair_key: 'batch-a/SAMPLE01_R{R}.fastq.gz',
  directory: 'batch-a',
  status: 'ready',
  missing_mates: [],
  issues: [],
  total_size: 2048,
  total_size_label: '2.0 KiB',
  first_seen_at: '2026-08-12T08:00:00Z',
  last_seen_at: '2026-08-12T08:10:00Z',
  last_changed_at: '2026-08-12T08:00:00Z',
  run_count: 0,
  recent_runs: [],
  files: [
    { mate: 1, name: 'SAMPLE01_R1.fastq.gz', relative_path: 'batch-a/SAMPLE01_R1.fastq.gz', size: 1024, size_label: '1.0 KiB', modified_at: '2026-08-12T08:00:00Z' },
    { mate: 2, name: 'SAMPLE01_R2.fastq.gz', relative_path: 'batch-a/SAMPLE01_R2.fastq.gz', size: 1024, size_label: '1.0 KiB', modified_at: '2026-08-12T08:00:00Z' },
  ],
}

const issueDataset = {
  id: 'issue-dataset',
  name: 'SAMPLE02',
  pair_key: 'batch-a/SAMPLE02_R{R}.fastq.gz',
  directory: 'batch-a',
  status: 'issue',
  missing_mates: [2],
  issues: [{ code: 'RAWDATA_MATE_MISSING', message: '缺少 R2 配对文件。' }],
  total_size: 1024,
  total_size_label: '1.0 KiB',
  files: [
    { mate: 1, name: 'SAMPLE02_R1.fastq.gz', relative_path: 'batch-a/SAMPLE02_R1.fastq.gz', size: 1024, size_label: '1.0 KiB', modified_at: '2026-08-12T08:00:00Z' },
  ],
}

const catalog = {
  root_directory: 'workspace/rawdata',
  root_status: 'ready',
  scanned_at: '2026-08-12T08:10:00Z',
  scan_limited: false,
  scan_limit: 2000,
  scan_entry_limit: 10000,
  scanned_entry_count: 8,
  summary: {
    file_count: 4,
    dataset_count: 2,
    ready_dataset_count: 1,
    issue_dataset_count: 1,
    unrecognized_fastq_count: 1,
    total_size: 4096,
    total_size_label: '4.0 KiB',
  },
  directories: [{
    path: 'batch-a', dataset_count: 2, ready_count: 1, issue_count: 1,
    unrecognized_count: 1, total_size: 4096, total_size_label: '4.0 KiB',
  }],
  datasets: [readyDataset, issueDataset],
  unrecognized_files: [{
    name: 'manual.fastq.gz', relative_path: 'batch-a/manual.fastq.gz', size: 1024,
    size_label: '1.0 KiB', modified_at: '2026-08-12T08:00:00Z',
  }],
  issues: [],
  index: {
    latest_scan_id: 'scan-1',
    latest_status: 'succeeded',
    active_scan_id: null,
    active_status: null,
    queued_at: null,
    started_at: null,
    finished_at: '2026-08-12T08:10:00Z',
    stale: false,
    policy: { max_files: 20000, max_entries: 100000, max_depth: 8, batch_entries: 1000 },
    repair_suggestions: [],
  },
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

test('运行用户检查原始数据问题并将可运行配对带入分析', async ({ page }) => {
  await mockOperatorAuth(page)
  await page.route('**/api/v1/rawdata/catalog**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog),
  }))

  await page.goto('/rawdata')

  await expect(page.getByRole('heading', { name: '原始数据', level: 1 })).toBeVisible()
  await expect(page.getByRole('searchbox', { name: '搜索原始数据' })).toBeVisible()
  await expect(page.getByRole('button', { name: /SAMPLE01.*可运行/ })).toBeVisible()
  await expect(page.getByText('1 组可运行')).toBeVisible()
  await expect(page.getByText('1 组待处理')).toBeVisible()
  await expect(page.getByRole('button', { name: '原始数据' })).toBeVisible()
  await expect(page.getByRole('button', { name: '运行分析' })).toBeVisible()
  await expect(page.getByRole('button', { name: '历史 WDL' })).toHaveCount(0)

  await page.locator('.rawdata-table').getByRole('button', { name: /SAMPLE02/ }).click()
  await expect(page.getByRole('complementary', { name: '原始数据详情' }).getByText('缺少 R2 配对文件。')).toBeVisible()
  await expect(page.getByRole('link', { name: '带入运行分析' })).toHaveCount(0)

  await page.locator('.rawdata-table').getByRole('button', { name: /SAMPLE01/ }).click()
  await expect(page.getByRole('link', { name: '带入运行分析' })).toHaveAttribute(
    'href',
    '/runs?dataset=ready-dataset',
  )
})

test('窄屏原始数据页面可滚动且没有页面级横向溢出', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await mockOperatorAuth(page)
  await page.route('**/api/v1/rawdata/catalog**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(catalog),
  }))

  await page.goto('/rawdata')

  await expect(page.getByRole('heading', { name: '原始数据', level: 1 })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390)
  await page.getByText('v1.0 · 稳定版').scrollIntoViewIfNeeded()
  await expect(page.getByText('v1.0 · 稳定版')).toBeVisible()
})

test('更新清单只创建后台扫描并持续展示成功快照', async ({ page }) => {
  await mockOperatorAuth(page)
  let queued = false
  await page.route('**/api/v1/rawdata/catalog**', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(queued ? {
      ...catalog,
      index: {
        ...catalog.index,
        active_scan_id: 'scan-2',
        active_status: 'running',
      },
    } : catalog),
  }))
  await page.route('**/api/v1/rawdata/scans', async (route) => {
    queued = true
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ id: 'scan-2', status: 'queued', created: true }),
    })
  })

  await page.goto('/rawdata')
  await page.getByRole('button', { name: '更新清单' }).click()

  await expect(page.getByText('后台正在更新清单')).toBeVisible()
  await expect(page.getByRole('button', { name: '扫描进行中' })).toBeDisabled()
  await expect(page.getByRole('button', { name: /SAMPLE01.*可运行/ })).toBeVisible()
})
