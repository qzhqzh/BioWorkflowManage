import { expect, test, type Page } from '@playwright/test'

const admin = {
  username: 'admin',
  is_admin: true,
  role: 'admin',
  allowed_sections: ['overview', 'edit', 'tools', 'packages', 'artifacts', 'resources', 'rawdata', 'runs', 'wdl', 'help'],
}

async function mockUser(page: Page, user: Record<string, unknown>) {
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ user }),
  }))
}

test('受限账号稳定落在无权限页', async ({ page }) => {
  await mockUser(page, {
    username: 'restricted',
    is_admin: false,
    role: 'restricted',
    allowed_sections: [],
  })

  await page.goto('/')

  await expect(page).toHaveURL(/\/no-access$/)
  await expect(page.getByRole('heading', { name: '暂无可访问的功能' })).toBeVisible()
})

test('总览只读取有界最近流程并使用服务端聚合', async ({ page }) => {
  await mockUser(page, admin)
  const workflowRequests: URL[] = []
  await page.route(/\/api\/v1\/editor\/workflows(?:\?.*)?$/, (route) => {
    const url = new URL(route.request().url())
    workflowRequests.push(url)
    const pageNumber = Number(url.searchParams.get('page') || '1')
    const results = Array.from({ length: 5 }, (_, index) => ({
      slug: `workflow-${index + 1}`,
      name: `Workflow ${index + 1}`,
      kind: 'workflow',
      latest_version: null,
      updated_at: '2026-08-29T00:00:00Z',
      is_mine: true,
    }))
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        results,
        page: pageNumber,
        page_size: 50,
        total: 51,
        has_next: true,
        summary: {
          my_total: 51,
          my_subworkflows: 0,
          my_published_subworkflows: 0,
          my_draft_subworkflows: 0,
          my_workflows: 51,
          my_draft_workflows: 51,
        },
      }),
    })
  })
  for (const endpoint of ['tools', 'wdl-packages', 'wdl-assets', 'analysis-runs']) {
    await page.route(`**/api/v1/${endpoint}*`, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [] }),
    }))
  }
  await page.route('**/api/v1/analysis/catalog*', route => route.fulfill({
    status: 503,
    contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'UNAVAILABLE' } }),
  }))

  await page.goto('/overview')

  await expect(page.getByText('51 个属于你 · 51 个草稿待发布')).toBeVisible()
  expect(workflowRequests).toHaveLength(1)
  expect(workflowRequests[0].searchParams.get('page_size')).toBe('5')
  expect(workflowRequests[0].searchParams.get('owner')).toBe('mine')
})

test('编辑器按需读取后续流程页', async ({ page }) => {
  await mockUser(page, admin)
  const requestedPages: string[] = []
  const requestedQueries: string[] = []
  const workflowDocument = {
    slug: 'fastp_bwa_demo',
    name: 'fastp BWA demo',
    description: '',
    kind: 'workflow',
    document_version: 1,
    document_digest: `sha256:${'1'.repeat(64)}`,
    latest_version: 1,
    is_mine: true,
    tool_specs: [],
    workflow_graph: {
      schema_version: '1.0.0',
      id: 'fastp_bwa_demo',
      name: 'fastp BWA demo',
      description: '',
      target: { language: 'wdl', version: '1.0' },
      nodes: [],
      edges: [],
      layout: { nodes: {}, viewport: { x: 0, y: 0, zoom: 1 } },
    },
    editor_document: { nodes: [], viewport: { x: 0, y: 0, zoom: 1 } },
  }
  await page.route(/\/api\/v1\/editor\/workflows\/[^/?]+(?:\/[^?]+)?(?:\?.*)?$/, (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/compilations') || path.endsWith('/wdl-versions')) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ results: [] }),
      })
    }
    const slug = path.split('/').at(-1) || workflowDocument.slug
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...workflowDocument,
        slug,
        name: slug,
        workflow_graph: {
          ...workflowDocument.workflow_graph,
          id: slug,
          name: slug,
        },
      }),
    })
  })
  await page.route(/\/api\/v1\/tools(?:\?.*)?$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [] }),
  }))
  await page.route(/\/api\/v1\/editor\/workflows(?:\?.*)?$/, (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    const url = new URL(route.request().url())
    const pageNumber = url.searchParams.get('page') || '1'
    const query = url.searchParams.get('q') || ''
    requestedPages.push(pageNumber)
    requestedQueries.push(query)
    const results = query
      ? [{
          slug: 'hidden-workflow',
          name: 'Hidden workflow',
          kind: 'workflow',
          latest_version: null,
          updated_at: '2026-08-29T00:00:00Z',
          is_mine: true,
          latest_version_snapshot: null,
        }]
      : [{
          slug: `workflow-page-${pageNumber}`,
          name: `Workflow page ${pageNumber}`,
          kind: 'workflow',
          latest_version: null,
          updated_at: '2026-08-29T00:00:00Z',
          is_mine: true,
          latest_version_snapshot: null,
        }]
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        results,
        page: Number(pageNumber),
        page_size: 50,
        total: query ? 1 : 51,
        has_next: !query && pageNumber === '1',
      }),
    })
  })

  await page.goto('/?section=artifacts')
  await expect.poll(() => requestedPages).toEqual(['1'])
  await page.getByRole('button', { name: '加载更多流程' }).click()
  await expect.poll(() => requestedPages).toEqual(['1', '2'])
  await expect(page.getByText('Workflow page 2')).toBeVisible()

  await page.getByLabel('搜索流程或 ID').fill('Hidden workflow')
  await expect.poll(() => requestedQueries.at(-1)).toBe('Hidden workflow')
  await expect(page.getByText('Hidden workflow')).toBeVisible()
})
