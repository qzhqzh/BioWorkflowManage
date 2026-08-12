import { expect, test, type Page } from '@playwright/test'

test.describe.configure({ mode: 'serial' })

async function openPrimarySection(page: Page, name: '工具库' | '流程库') {
  await page.getByLabel('主要导航').getByRole('button', { name, exact: true }).click()
  await expect(page.getByRole('heading', { name, exact: true, level: 1 })).toBeVisible()
}

async function selectWorkflow(page: Page, name: string) {
  const workflowIndex = page.locator('.workflow-index')
  const workflowButton = workflowIndex.getByRole('button').filter({ hasText: name })

  await expect(workflowButton).toBeVisible()
  await workflowButton.click()
}

async function csrfHeaders(page: Page) {
  const cookie = (await page.context().cookies()).find(item => item.name === 'csrftoken')
  return cookie ? { 'X-CSRFToken': decodeURIComponent(cookie.value) } : {}
}

async function restoreWorkflow(page: Page, workflowUrl: string, originalDocument: Record<string, any>) {
  const currentResponse = await page.request.get(workflowUrl)
  expect(currentResponse.ok()).toBe(true)
  const current = await currentResponse.json()
  return page.request.put(workflowUrl, {
    data: {
      ...originalDocument,
      base_document_version: current.document_version,
      base_document_digest: current.document_digest,
    },
    headers: await csrfHeaders(page),
  })
}

test.beforeEach(async ({ page }) => {
  const authProbe = page.waitForResponse(response => response.url().endsWith('/api/v1/auth/me'))
  await page.goto('/login')
  await authProbe
  await page.getByLabel('用户名').fill('zhuqin')
  await page.getByLabel('密码').fill('zhuqin')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL('**/overview')
  await page.goto('/')
  await expect(page.getByText('BioWorkflowManage', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
})

test('can navigate libraries and switch the active workflow', async ({ page }) => {
  await openPrimarySection(page, '工具库')
  await expect(page.locator('.registry-list')).toBeVisible()

  await openPrimarySection(page, '流程库')
  await expect(page).toHaveURL(/section=artifacts/)
  await expect(page.locator('.workflow-index')).toBeVisible()
  const graphSummary = page.getByLabel('流程画布结构摘要')
  await expect(graphSummary).toBeVisible()
  await expect(graphSummary.getByRole('button', { name: '编辑画布', exact: true })).toBeVisible()
  await graphSummary.getByRole('button', { name: '编辑画布', exact: true }).click()
  await expect(page).toHaveURL(/section=edit/)
  await page.reload()
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
  await openPrimarySection(page, '流程库')
  await expect(graphSummary).toContainText(/\d+ 个节点 · \d+ 条连接/)

  await selectWorkflow(page, 'fastp demo')
  await expect(page.locator('.version-sidebar__title strong')).toHaveText('fastp demo')
  await expect(page.locator('.version-sidebar__title small')).toHaveText('fastp_demo')

  await selectWorkflow(page, 'fastp → BWA-MEM demo')
  await expect(page.locator('.version-sidebar__title strong')).toHaveText('fastp → BWA-MEM demo')
  await expect(page.locator('.version-sidebar__title small')).toHaveText('fastp_bwa_demo')
  await expect(page.getByRole('link', { name: '下载 WDL', exact: true })).toHaveAttribute(
    'download',
    /^fastp_bwa_demo-(?:v\d+|draft)(?:-wdl-v\d+)?\.wdl$/,
  )

  const versionsResponse = await page.request.get('/api/v1/editor/workflows/fastp_bwa_demo/versions')
  expect(versionsResponse.ok()).toBe(true)
  const versionIndex = await versionsResponse.json()
  const version = versionIndex.results[0]?.version ?? 0
  expect(version).toBeGreaterThan(0)
  const publishedVersion = page.locator('.version-row').filter({
    has: page.locator('strong', { hasText: new RegExp(`^v${version}$`) }),
  }).first()
  await publishedVersion.click()
  await expect(page).toHaveURL(new RegExp(`workflowVersion=${version}`))
  await expect(page.locator('.published-version-banner')).toContainText(`v${version} 只读发布快照`)
  await expect(page.getByRole('link', { name: '运行与记录', exact: true })).toHaveAttribute(
    'href',
    `/runs?workflow=fastp_bwa_demo&revision=${version}`,
  )
})

test('从当前工具节点进入工具包时保留流程与节点上下文', async ({ page }) => {
  const workflowResponse = await page.request.get('/api/v1/editor/workflows/fastp_bwa_demo')
  expect(workflowResponse.ok()).toBe(true)
  const workflow = await workflowResponse.json()
  const toolNode = workflow.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.type === 'tool' && item.tool_ref?.id === 'fastp',
  )
  expect(toolNode).toBeTruthy()

  await page.goto('/?section=edit&workflow=fastp_bwa_demo')
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
  await page.locator(`.vue-flow__node[data-id="${toolNode.id}"]`).evaluate(
    element => (element as HTMLElement).click(),
  )
  await page.getByRole('button', { name: '以此工具创建工具包' }).click()

  await expect(page).toHaveURL(new RegExp(
    `/wdl-packages\\?from=editor&workflow=fastp_bwa_demo&node=${encodeURIComponent(toolNode.id)}`,
  ))
})

test('从画布工具节点打开精确的固定版本', async ({ page }) => {
  const workflowResponse = await page.request.get('/api/v1/editor/workflows/fastp_bwa_demo')
  expect(workflowResponse.ok()).toBe(true)
  const workflow = await workflowResponse.json()
  const toolNode = workflow.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.type === 'tool',
  )
  const toolSpec = workflow.tool_specs.find(
    (item: Record<string, any>) => item.id === toolNode?.tool_ref?.id,
  )
  expect(toolNode).toBeTruthy()
  expect(toolSpec).toBeTruthy()

  const registryResponse = await page.request.get('/api/v1/tools')
  expect(registryResponse.ok()).toBe(true)
  const registry = await registryResponse.json()
  await page.route('**/api/v1/tools', async (route) => {
    if (new URL(route.request().url()).pathname !== '/api/v1/tools') return route.continue()
    const results = registry.results.map((item: Record<string, any>) => (
      item.tool_id === toolNode.tool_ref.id
        ? { ...item, draft_status: null, draft_version: null, draft_digest: null }
        : item
    ))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results }),
    })
  })

  await page.goto('/?section=edit&workflow=fastp_bwa_demo')
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
  await page.locator(`.vue-flow__node[data-id="${toolNode.id}"]`).dispatchEvent('click')
  await expect(page.locator('.inspector-panel').getByRole('heading', { name: 'fastp' })).toBeVisible()
  await page.getByRole('button', { name: '查看固定版本', exact: true }).click()

  await expect(page).toHaveURL(new RegExp(
    `section=tools.*tool=${encodeURIComponent(toolNode.tool_ref.id)}.*toolVersion=${encodeURIComponent(toolNode.tool_ref.tool_version)}`,
  ))
  const inspector = page.getByLabel('工具版本检查器')
  await expect(inspector).toBeVisible()
  await expect(inspector.locator('.tool-version-snapshot')).toContainText(
    `v${toolNode.tool_ref.tool_version} 固定内容`,
  )
  await expect(inspector.locator('.tool-version-list button.is-active code')).toContainText(
    toolNode.tool_ref.digest.slice(0, 18),
  )
  await expect(inspector.locator('.tool-command-preview')).toContainText(toolSpec.command.template)
})

test('创建子流程后直接打开画布并可从我的子流程再次找到', async ({ page }) => {
  const listResponse = await page.request.get('/api/v1/editor/workflows')
  expect(listResponse.ok()).toBe(true)
  const existing = await listResponse.json()
  const slug = 'e2e_my_qc_subflow'
  const createdDocument = {
    slug,
    name: '我的 E2E QC 子流程',
    description: '验证创建与再次发现路径',
    kind: 'subworkflow',
    created_by: 'zhuqin',
    updated_by: 'zhuqin',
    is_mine: true,
    latest_version: null,
    document_version: 1,
    document_digest: `sha256:${'7'.repeat(64)}`,
    subworkflow_references: [],
    tool_specs: [],
    workflow_graph: {
      schema_version: '1.0.0',
      id: slug,
      name: '我的 E2E QC 子流程',
      description: '验证创建与再次发现路径',
      target: { language: 'wdl', version: '1.0', profile: 'miniwdl-compatible' },
      nodes: [{
        id: 'input_file',
        type: 'workflow_input',
        label: '输入文件',
        port: {
          name: 'value', wdl_type: 'File', semantic_type: 'core.file.any', required: true,
        },
      }],
      edges: [],
      layout: {
        nodes: { input_file: { x: 80, y: 120 } },
        viewport: { x: 0, y: 0, zoom: 1 },
      },
    },
    editor_document: {
      nodes: [{ id: 'input_file', position: { x: 80, y: 120 } }],
      viewport: { x: 0, y: 0, zoom: 1 },
    },
  }
  let created = false

  await page.route(/\/api\/v1\/editor\/workflows(?:\/[^?]*)?(?:\?.*)?$/, async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/editor/workflows' && request.method() === 'POST') {
      created = true
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(createdDocument) })
      return
    }
    if (path === '/api/v1/editor/workflows' && request.method() === 'GET' && created) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ results: [createdDocument, ...existing.results] }),
      })
      return
    }
    if (path === `/api/v1/editor/workflows/${slug}`) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(createdDocument) })
      return
    }
    if (path === `/api/v1/editor/workflows/${slug}/compilations` || path === `/api/v1/editor/workflows/${slug}/wdl-versions`) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ results: [] }) })
      return
    }
    await route.fallback()
  })

  await page.getByLabel('当前流程').getByRole('button', { name: '新建子流程', exact: true }).click()
  const form = page.locator('.canvas-workflow-create')
  await form.getByLabel('名称', { exact: true }).fill(createdDocument.name)
  await form.getByLabel('流程 ID', { exact: true }).fill(slug)
  await form.getByLabel('说明', { exact: true }).fill(createdDocument.description)
  await form.getByRole('button', { name: '创建并打开画布', exact: true }).click()

  await expect(page).toHaveURL(new RegExp(`section=edit.*workflow=${slug}`))
  await expect(page.getByLabel('当前流程')).toContainText(createdDocument.name)
  await expect(page.getByLabel('子流程画布准备')).toBeVisible()
  await page.getByLabel('当前流程').getByRole('button', { name: /我的子流程 \d+/ }).click()
  await expect(page).toHaveURL(/section=artifacts.*owner=mine.*kind=subworkflow/)
  await expect(page.locator('.workflow-index').getByText(createdDocument.name, { exact: true })).toBeVisible()
})

test('WDL 映射出的工具草稿可从画布直接进入审查', async ({ page }) => {
  const workflowResponse = await page.request.get('/api/v1/editor/workflows/fastp_bwa_demo')
  expect(workflowResponse.ok()).toBe(true)
  const workflow = await workflowResponse.json()
  const node = workflow.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.type === 'tool' && item.tool_ref?.id === 'fastp',
  )
  expect(node).toBeTruthy()

  const registryResponse = await page.request.get('/api/v1/tools')
  expect(registryResponse.ok()).toBe(true)
  const registry = await registryResponse.json()
  const results = registry.results.map((item: Record<string, any>) => (
    item.tool_id === 'fastp'
      ? {
          ...item,
          draft_status: 'valid',
          draft_version: node.tool_ref.tool_version,
          draft_digest: node.tool_ref.digest,
        }
      : item
  ))
  await page.route('**/api/v1/tools', async (route) => {
    if (new URL(route.request().url()).pathname !== '/api/v1/tools') return route.continue()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results }),
    })
  })

  await page.goto('/?section=edit&workflow=fastp_bwa_demo')
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
  await page.locator(`.vue-flow__node[data-id="${node.id}"]`).evaluate(
    element => (element as HTMLElement).click(),
  )
  const referenceState = page.locator('.tool-reference-state')
  await expect(referenceState).toContainText('待发布工具草稿')
  await referenceState.getByRole('button', { name: '审查工具草稿', exact: true }).click()

  await expect(page).toHaveURL(/section=tools.*tool=fastp.*toolDraft=1/)
  const inspector = page.getByLabel('工具版本检查器')
  await expect(inspector).toBeVisible()
  await expect(inspector.locator('.tool-draft-workspace--active')).toBeVisible()
  await expect(inspector.getByRole('heading', { name: '编辑工具草稿', exact: true })).toBeVisible()

  await page.setViewportSize({ width: 390, height: 844 })
  const inspectorBody = inspector.locator('.tool-version-panel__body')
  const narrowLayout = await inspectorBody.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    documentWidth: document.documentElement.scrollWidth,
  }))
  expect(narrowLayout.scrollHeight).toBeGreaterThan(narrowLayout.clientHeight)
  expect(narrowLayout.documentWidth).toBe(390)
  await inspectorBody.evaluate(element => { element.scrollTop = element.scrollHeight })
  await expect(inspector.getByRole('button', { name: '删除', exact: true }).last()).toBeVisible()
})

test('流程列表失败时可以在当前页面重试', async ({ page }) => {
  let shouldFail = true
  const routeHandler = async (route: import('@playwright/test').Route) => {
    if (route.request().method() !== 'GET' || !route.request().url().endsWith('/api/v1/editor/workflows')) {
      return route.continue()
    }
    if (!shouldFail) return route.continue()
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { message: 'unavailable' } }) })
  }
  await page.route('**/api/v1/editor/workflows', routeHandler)
  await page.goto('/?section=artifacts&workflow=fastp_bwa_demo')
  await expect(page.getByText('流程列表载入失败', { exact: true })).toBeVisible()

  shouldFail = false
  await page.getByRole('button', { name: '重新加载', exact: true }).click()
  await expect(page.getByRole('heading', { name: '我的流程' })).toBeVisible()
  await page.unroute('**/api/v1/editor/workflows', routeHandler)
})

test('keeps workflow creation and tool versions visible in the current workspace', async ({ page }) => {
  let createPayload: Record<string, any> | undefined
  const createRoute = async (route: import('@playwright/test').Route) => {
    if (route.request().method() !== 'POST') return route.continue()
    createPayload = route.request().postDataJSON()
    await route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'WORKFLOW_ALREADY_EXISTS', message: '测试流程已存在。' } }),
    })
  }
  await page.route('**/api/v1/editor/workflows', createRoute)

  await expect(page.getByRole('button', { name: /我的子流程 \d+/ })).toBeVisible()
  await expect(page.getByRole('button', { name: '新建子流程', exact: true }).first()).toBeVisible()
  await expect(
    page.getByLabel('当前流程').getByRole('button', { name: '创建工具包', exact: true }),
  ).toBeVisible()
  await expect(page.locator('.library-panel__footer').getByRole('button', { name: '创建 WDL 工具包', exact: true })).toBeVisible()
  await page.getByRole('tab', { name: '子流程', exact: true }).click()
  await expect(page.getByRole('button', { name: '只看我的', exact: true })).toBeVisible()
  await page.locator('.library-panel__footer').getByRole('button', { name: '新建子流程', exact: true }).click()
  const canvasCreate = page.locator('.canvas-workflow-create')
  await expect(canvasCreate).toBeVisible()
  await expect(canvasCreate.getByText('新建子流程', { exact: true })).toBeVisible()
  await expect(page.getByLabel('名称', { exact: true })).toBeFocused()
  await page.getByLabel('名称', { exact: true }).fill('测试子流程')
  await page.getByLabel('流程 ID', { exact: true }).fill('test_subworkflow')
  await canvasCreate.getByRole('button', { name: '创建并打开画布' }).click()
  await expect(canvasCreate.getByRole('alert')).toContainText('测试流程已存在')
  expect(createPayload).toMatchObject({
    slug: 'test_subworkflow',
    name: '测试子流程',
    kind: 'subworkflow',
  })
  await canvasCreate.getByRole('button', { name: '取消', exact: true }).click()
  await page.unroute('**/api/v1/editor/workflows', createRoute)

  await openPrimarySection(page, '流程库')
  await expect(page.getByRole('heading', { name: '我的子流程' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '我的流程' })).toBeVisible()
  await expect(page.getByLabel('搜索流程或 ID')).toBeVisible()
  await expect(page.getByLabel('流程归属').getByRole('button', { name: '我的', exact: true })).toBeVisible()
  await expect(page.getByLabel('流程类型')).toBeVisible()
  await expect(page.getByLabel('发布状态')).toBeVisible()
  await page.getByLabel('搜索流程或 ID').fill('no-such-workflow-for-filter-test')
  await expect(page.getByText('没有匹配的流程', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '清除筛选', exact: true }).first().click()
  await expect(page.getByRole('heading', { name: '我的流程' })).toBeVisible()
  await page.getByRole('button', { name: '新建子流程', exact: true }).click()
  await expect(page.getByText('创建后直接进入画布。')).toBeVisible()
  await expect(page.getByLabel('名称', { exact: true })).toBeFocused()
  await expect(page.getByRole('button', { name: '创建并打开画布' })).toBeVisible()

  await openPrimarySection(page, '工具库')
  const firstTool = page.locator('.registry-row').first()
  await expect(firstTool).toBeVisible()
  await firstTool.getByRole('button', { name: /查看版本|已展开/ }).click()
  const inspector = page.getByLabel('工具版本检查器')
  await expect(inspector).toBeVisible()
  await expect(inspector.getByText('随版本锁定')).toBeVisible()
  await expect(page).toHaveURL(/section=tools.*tool=.*toolVersion=/)
  const versionRail = inspector.getByLabel('已发布版本')
  const versionContent = inspector.locator('.tool-version-snapshot')
  await expect(versionRail).toBeVisible()
  await expect(versionContent).toBeVisible()
  const bounds = await inspector.boundingBox()
  const railBounds = await versionRail.boundingBox()
  const contentBounds = await versionContent.boundingBox()
  expect(bounds).not.toBeNull()
  expect(railBounds).not.toBeNull()
  expect(contentBounds).not.toBeNull()
  expect(bounds!.y).toBeLessThan(page.viewportSize()!.height)
  expect(railBounds!.x).toBeLessThan(contentBounds!.x)
  expect(Math.abs(railBounds!.y - contentBounds!.y)).toBeLessThan(8)

  await inspector.getByRole('button', { name: '关闭', exact: true }).click()
  await expect(inspector).toHaveCount(0)
  await expect(firstTool.getByRole('button', { name: '查看版本', exact: true })).toBeFocused()

  await page.setViewportSize({ width: 780, height: 900 })
  await firstTool.getByRole('button', { name: '查看版本', exact: true }).click()
  const narrowInspector = page.getByLabel('工具版本检查器')
  await expect(narrowInspector).toBeVisible()
  await expect(page.locator('.tool-version-backdrop')).toBeVisible()
  expect(await narrowInspector.evaluate(element => getComputedStyle(element).position)).toBe('fixed')
  await page.keyboard.press('Escape')
  await expect(narrowInspector).toHaveCount(0)
  await expect(firstTool.getByRole('button', { name: '查看版本', exact: true })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(780)
})

test('中等宽度隐藏侧栏时仍可进入我的流程并新建', async ({ page }) => {
  await page.setViewportSize({ width: 780, height: 900 })

  const topbar = page.locator('.topbar')
  await expect(topbar.getByRole('button', { name: '我的流程', exact: true })).toBeVisible()
  await expect(topbar.getByRole('button', { name: '新建', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(780)

  await topbar.getByRole('button', { name: '新建', exact: true }).click()
  await expect(page.getByRole('heading', { name: '流程库', exact: true, level: 1 })).toBeVisible()
  await expect(page.getByText('创建后直接进入画布。')).toBeVisible()
  await expect(page.getByLabel('名称', { exact: true })).toBeFocused()
})

test('从运行链接打开精确的只读发布快照', async ({ page }) => {
  const workflowSlug = 'fastp_demo'
  const versionsResponse = await page.request.get(`/api/v1/editor/workflows/${workflowSlug}/versions`)
  expect(versionsResponse.ok()).toBe(true)
  const versions = await versionsResponse.json()
  let snapshot: Record<string, any> | undefined
  for (const version of versions.results) {
    const response = await page.request.get(
      `/api/v1/editor/workflows/${workflowSlug}/versions/${version.version}`,
    )
    expect(response.ok()).toBe(true)
    const candidate = await response.json()
    if (candidate.compiled_bundle?.files?.[candidate.compiled_bundle?.entrypoint]) {
      snapshot = candidate
      break
    }
  }
  expect(snapshot).toBeTruthy()
  const exactSnapshot = snapshot!
  const entrypoint = exactSnapshot.compiled_bundle.entrypoint
  const expectedWdl = exactSnapshot.compiled_bundle.files[entrypoint]

  await page.goto(
    `/?section=artifacts&workflow=${workflowSlug}&workflowVersion=${exactSnapshot.version}`,
  )

  const banner = page.locator('.published-version-banner')
  await expect(banner).toContainText(`v${exactSnapshot.version} 只读发布快照`)
  await expect(banner).toContainText(exactSnapshot.semantic_digest)
  await expect(page.getByRole('link', { name: '运行与记录', exact: true })).toHaveAttribute(
    'href',
    `/runs?workflow=${workflowSlug}&revision=${exactSnapshot.version}`,
  )
  const graphSummary = page.getByLabel('流程画布结构摘要')
  await expect(graphSummary).toContainText(`发布版本 v${exactSnapshot.version}`)
  await expect(graphSummary.getByRole('button', { name: '打开当前画布', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '编辑为新版本' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '保存人工版本' })).toHaveCount(0)
  await expect(page.getByLabel('选择 WDL 版本')).toHaveCount(0)

  const renderedLines = await page.getByLabel('WDL 只读预览').locator('.code-line > code').allTextContents()
  expect(renderedLines.map(line => line === ' ' ? '' : line).join('\n').trim()).toBe(expectedWdl.trim())
})

test('人工修改 WDL 时明确选择派生稿或历史资产', async ({ page }) => {
  await openPrimarySection(page, '流程库')
  await selectWorkflow(page, 'fastp → BWA-MEM demo')
  const revisionIndexResponse = await page.request.get(
    '/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions',
  )
  expect(revisionIndexResponse.ok()).toBe(true)
  const revisionIndex = await revisionIndexResponse.json()
  const baseRevision = revisionIndex.results[0]
  expect(baseRevision?.workflow_version).toBeGreaterThan(0)

  let derivedRequest: Record<string, any> | undefined
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    derivedRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 999,
        workflow_slug: 'fastp_bwa_demo',
        version: 999,
        source: 'manual',
        artifact_role: 'derived_draft',
        executable: false,
        digest: 'sha256:derived',
        workflow_version: baseRevision.workflow_version,
        base_wdl_revision: derivedRequest?.base_wdl_version,
        created_by: 'zhuqin',
        note: derivedRequest?.note,
        base_workflow_version: {
          version: baseRevision.workflow_version,
          semantic_digest: 'sha256:workflow-v1',
          compiler_profile: 'compiler-core-v1',
        },
        run_source: null,
        validation: { status: 'valid', diagnostics: [] },
        content: derivedRequest?.content,
        created_at: new Date().toISOString(),
      }),
    })
  })

  await page.getByRole('button', { name: '修改 WDL', exact: true }).click()
  const destination = page.getByLabel('选择 WDL 修改去向')
  await expect(destination).toContainText('画布和已发布 WorkflowVersion 不会随手工 WDL 自动变化')
  await expect(destination.getByRole('button', { name: /创建派生稿/ })).toBeVisible()
  await expect(destination.getByRole('button', { name: /转为历史 WDL 资产/ })).toBeVisible()

  await destination.getByRole('button', { name: /创建派生稿/ }).click()
  const editor = page.getByLabel('编辑 WDL 内容')
  await editor.fill(`${await editor.inputValue()}\n# derived draft`)
  await page.getByRole('button', { name: '保存派生稿', exact: true }).click()
  await expect(page.getByText('当前内容是不可运行的派生稿')).toBeVisible()
  expect(derivedRequest?.source).toBe('manual')
  expect(derivedRequest?.workflow_version).toBe(baseRevision.workflow_version)
  expect(derivedRequest?.base_wdl_version).toBe(baseRevision.version)
  expect(derivedRequest?.base_wdl_digest).toBe(baseRevision.digest)
  expect(derivedRequest?.content).toContain('# derived draft')

  let assetRequest: Record<string, any> | undefined
  await page.route('**/api/v1/wdl-assets', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    assetRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({ slug: 'fastp-bwa-derived' }),
    })
  })
  await page.route('**/api/v1/wdl-assets/fastp-bwa-derived', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      slug: 'fastp-bwa-derived',
      name: 'fastp → BWA-MEM demo WDL',
      description: 'Derived workflow WDL.',
      source_filename: 'fastp_bwa_demo.wdl',
      source_repository: 'bioworkflow://editor/workflows/fastp_bwa_demo',
      source_revision: `workflow-v${baseRevision.workflow_version}:wdl-v999`,
      lifecycle: 'active',
      metadata_version: 1,
      tags: [],
      created_by: 'zhuqin',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      revision_count: 1,
      file_count: 1,
      current_revision: {
        version: 1,
        operation: 'import',
        content: derivedRequest?.content,
        digest: 'sha256:asset',
        diff: '',
        note: 'Derived from workflow.',
        actor: 'zhuqin',
        analysis: { status: 'valid', summary: {}, imports: [], tasks: [], workflows: [], diagnostics: [] },
        entrypoint: 'fastp_bwa_demo.wdl',
        files: [{
          path: 'fastp_bwa_demo.wdl',
          content: derivedRequest?.content,
          digest: 'sha256:file',
          is_entry: true,
          read_only: false,
          origin: 'asset',
          analysis: { status: 'valid', summary: {}, imports: [], tasks: [], workflows: [], diagnostics: [] },
        }],
        package_references: [],
        created_at: new Date().toISOString(),
      },
      revisions: [],
      audit_events: [],
    }),
  }))

  await page.getByRole('button', { name: '修改 WDL', exact: true }).click()
  await page.getByRole('button', { name: /转为历史 WDL 资产/ }).click()
  await page.waitForURL('**/wdl/fastp-bwa-derived')
  expect(assetRequest?.source_repository).toBe('bioworkflow://editor/workflows/fastp_bwa_demo')
  expect(assetRequest?.source_revision).toBe(`workflow-v${baseRevision.workflow_version}:wdl-v999`)
  expect(assetRequest?.content).toContain('# derived draft')
  const sourceLink = page.getByRole('link', {
    name: `来源流程 fastp_bwa_demo · WDL v999 · 基于 v${baseRevision.workflow_version}`,
  })
  await expect(sourceLink).toBeVisible()
  await expect(sourceLink).toHaveAttribute('href', /workflow=fastp_bwa_demo.*wdlVersion=999/)
})

test('WDL 派生稿需审阅并逐类确认后才写入画布草稿', async ({ page }) => {
  const workflowUrl = '/api/v1/editor/workflows/fastp_bwa_demo'
  const liveDocumentResponse = await page.request.get(workflowUrl)
  expect(liveDocumentResponse.ok()).toBe(true)
  const liveDocument = await liveDocumentResponse.json()
  const liveFastpNode = liveDocument.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.type === 'tool' && item.tool_ref?.id === 'fastp',
  )
  const liveFastpSpec = liveDocument.tool_specs.find(
    (item: Record<string, any>) => item.id === 'fastp',
  )
  const liveBwaNode = liveDocument.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.type === 'tool' && item.tool_ref?.id === 'bwa_mem',
  )
  const liveBwaSpec = liveDocument.tool_specs.find(
    (item: Record<string, any>) => item.id === 'bwa_mem',
  )
  expect(liveFastpNode).toBeTruthy()
  expect(liveFastpSpec).toBeTruthy()
  expect(liveBwaNode).toBeTruthy()
  expect(liveBwaSpec).toBeTruthy()
  const draftVersion = '0.23.4-wdl.700'
  const draftDigest = `sha256:${'7'.repeat(64)}`
  const bwaDraftVersion = '0.7.17-wdl.700'
  const bwaDraftDigest = `sha256:${'6'.repeat(64)}`
  const appliedDocument = structuredClone(liveDocument)
  const appliedFastpNode = appliedDocument.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.id === liveFastpNode.id,
  )
  const appliedFastpSpec = appliedDocument.tool_specs.find(
    (item: Record<string, any>) => item.id === 'fastp',
  )
  const appliedBwaNode = appliedDocument.workflow_graph.nodes.find(
    (item: Record<string, any>) => item.id === liveBwaNode.id,
  )
  const appliedBwaSpec = appliedDocument.tool_specs.find(
    (item: Record<string, any>) => item.id === 'bwa_mem',
  )
  appliedFastpNode.tool_ref = {
    ...appliedFastpNode.tool_ref,
    tool_version: draftVersion,
    digest: draftDigest,
  }
  appliedFastpSpec.tool_version = draftVersion
  appliedFastpSpec.command = {
    ...appliedFastpSpec.command,
    template: 'fastp --new',
  }
  appliedBwaNode.tool_ref = {
    ...appliedBwaNode.tool_ref,
    tool_version: bwaDraftVersion,
    digest: bwaDraftDigest,
  }
  appliedBwaSpec.tool_version = bwaDraftVersion
  appliedBwaSpec.command = {
    ...appliedBwaSpec.command,
    template: 'bwa mem --new',
  }
  appliedDocument.document_version = liveDocument.document_version + 1
  appliedDocument.document_digest = `sha256:${'8'.repeat(64)}`
  const liveRegistryResponse = await page.request.get('/api/v1/tools')
  expect(liveRegistryResponse.ok()).toBe(true)
  const liveRegistry = await liveRegistryResponse.json()
  let workflowApplied = false
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo', async (route) => {
    if (new URL(route.request().url()).pathname !== workflowUrl) return route.continue()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(workflowApplied ? appliedDocument : liveDocument),
    })
  })
  await page.route('**/api/v1/tools', async (route) => {
    if (new URL(route.request().url()).pathname !== '/api/v1/tools') return route.continue()
    const results = liveRegistry.results.map((item: Record<string, any>) => {
      if (!workflowApplied) return item
      if (item.tool_id === 'fastp') {
        return {
          ...item,
          draft_status: 'valid',
          draft_version: draftVersion,
          draft_digest: draftDigest,
        }
      }
      if (item.tool_id === 'bwa_mem') {
        return {
          ...item,
          draft_status: 'valid',
          draft_version: bwaDraftVersion,
          draft_digest: bwaDraftDigest,
        }
      }
      return item
    })
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results }),
    })
  })
  const liveRevisionResponse = await page.request.get(`${workflowUrl}/wdl-versions`)
  expect(liveRevisionResponse.ok()).toBe(true)
  const liveRevisionIndex = await liveRevisionResponse.json()
  const liveRevisionDetailResponse = await page.request.get(
    `${workflowUrl}/wdl-versions/${liveRevisionIndex.results[0].version}`,
  )
  expect(liveRevisionDetailResponse.ok()).toBe(true)
  const liveWdl = (await liveRevisionDetailResponse.json()).content
  const derivedRevision = {
    id: 700,
    workflow_slug: 'fastp_bwa_demo',
    version: 700,
    source: 'manual',
    artifact_role: 'derived_draft',
    executable: false,
    digest: 'sha256:derived-700',
    workflow_version: 1,
    base_wdl_revision: 1,
    created_by: 'zhuqin',
    note: 'Review WDL changes.',
    validation: { status: 'valid', diagnostics: [] },
    content: liveWdl,
    created_at: new Date().toISOString(),
  }
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ workflow_slug: 'fastp_bwa_demo', results: [derivedRevision] }),
    })
  })
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/700', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(derivedRevision),
  }))

  let proposalRequest: Record<string, any> | undefined
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/700/graph-proposals', async (route) => {
    proposalRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        id: 701,
        status: 'ready',
        proposal_digest: 'sha256:proposal-701',
        base_document_version: proposalRequest?.base_document_version,
        base_document_digest: proposalRequest?.base_document_digest,
        summary: { workflow_change_count: 1, tool_draft_count: 2, instance_change_count: 1 },
        changes: {
          workflow_structure: [{ kind: 'edge_added', subject: 'fastp.clean → align.reads', detail: '' }],
          tool_versions: [
            { kind: 'tool_draft_created', subject: 'fastp', detail: '0.23.4 → 0.23.4-wdl.700；固定内容：命令模板' },
            { kind: 'tool_draft_created', subject: 'bwa_mem', detail: '0.7.17 → 0.7.17-wdl.700；固定内容：命令模板' },
          ],
          instance_parameters: [],
        },
        tool_drafts: [
          {
            tool_id: 'fastp',
            base_version: '0.23.4',
            proposed_version: draftVersion,
            changed_fields: ['命令模板'],
            field_diffs: [{
              field: 'command',
              label: '命令模板',
              before: { shell: 'bash', strict_mode: true, template: 'fastp --old' },
              after: { shell: 'bash', strict_mode: true, template: 'fastp --new' },
            }],
          },
          {
            tool_id: 'bwa_mem',
            base_version: '0.7.17',
            proposed_version: bwaDraftVersion,
            changed_fields: ['命令模板'],
            field_diffs: [{
              field: 'command',
              label: '命令模板',
              before: { shell: 'bash', strict_mode: true, template: 'bwa mem --old' },
              after: { shell: 'bash', strict_mode: true, template: 'bwa mem --new' },
            }],
          },
        ],
        required_confirmations: ['workflow_structure', 'tool_versions'],
        warnings: ['已发布版本保持不变。'],
        blocking_issues: [],
      }),
    })
  })
  let applyRequest: Record<string, any> | undefined
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-graph-proposals/701/apply', async (route) => {
    applyRequest = route.request().postDataJSON()
    workflowApplied = true
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        proposal: { id: 701, status: 'applied' },
        workflow: {
          slug: 'fastp_bwa_demo',
          document_version: liveDocument.document_version + 1,
        },
      }),
    })
  })

  await page.reload()
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
  await openPrimarySection(page, '流程库')
  await page.getByRole('button', { name: '检查画布影响', exact: true }).click()
  const proposal = page.getByLabel('WDL 对画布的变更提案')
  await expect(proposal).toContainText('画布变更 1')
  await expect(proposal).toContainText('新工具草稿 2')
  await expect(proposal).toContainText('命令模板')
  const fixedDiffs = proposal.getByText('查看 1 项固定内容差异', { exact: true })
  await expect(fixedDiffs).toHaveCount(2)
  await fixedDiffs.first().click()
  await expect(proposal).toContainText('当前')
  await expect(proposal).toContainText('fastp --old')
  await expect(proposal).toContainText('WDL 提议')
  await expect(proposal).toContainText('fastp --new')
  await fixedDiffs.nth(1).click()
  await expect(proposal).toContainText('bwa mem --old')
  await expect(proposal).toContainText('bwa mem --new')
  const applyButton = proposal.getByRole('button', { name: '应用到画布草稿' })
  await expect(applyButton).toBeDisabled()
  await proposal.getByLabel('确认画布结构').check()
  await expect(applyButton).toBeDisabled()
  await proposal.getByLabel('确认工具固定内容').check()
  await expect(applyButton).toBeEnabled()
  await applyButton.click()

  expect(proposalRequest).toEqual({
    base_document_version: liveDocument.document_version,
    base_document_digest: liveDocument.document_digest,
  })
  expect(applyRequest).toEqual({
    proposal_digest: 'sha256:proposal-701',
    base_document_version: liveDocument.document_version,
    base_document_digest: liveDocument.document_digest,
    confirm_sections: ['workflow_structure', 'tool_versions'],
  })
  await expect(page.locator('#workflow-canvas')).toBeVisible()
  await expect(page).toHaveURL(/section=edit/)
  await expect(
    page.locator(`.vue-flow__node[data-id="${liveFastpNode.id}"] .flow-node--selected`),
  ).toBeVisible()
  await expect(
    page.locator(`.vue-flow__node[data-id="${liveBwaNode.id}"] .flow-node--selected`),
  ).toBeVisible()
  await expect(page.getByRole('tab', { name: '属性', exact: true })).toHaveAttribute('aria-selected', 'true')
  const referenceState = page.locator('.tool-reference-state')
  await expect(referenceState).toContainText('待发布工具草稿')
  await expect(referenceState.getByRole('button', { name: '审查工具草稿', exact: true })).toBeVisible()
})

test('切换 WDL 修订时等待正确正文并继承该修订的来源版本', async ({ page }) => {
  const liveIndex = await page.request.get('/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions')
  expect(liveIndex.ok()).toBe(true)
  const liveResults = (await liveIndex.json()).results
  const liveDetail = await page.request.get(
    `/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/${liveResults[0].version}`,
  )
  expect(liveDetail.ok()).toBe(true)
  const validWdl = (await liveDetail.json()).content
  const createdAt = new Date().toISOString()
  const revision = (
    version: number,
    workflowVersion: number | null,
    source: 'system' | 'manual',
    content?: string,
  ) => ({
    id: version,
    workflow_slug: 'fastp_bwa_demo',
    version,
    source,
    artifact_role: source === 'system' ? 'compiled_snapshot' : 'derived_draft',
    executable: false,
    digest: `sha256:${version}`,
    workflow_version: workflowVersion,
    base_wdl_revision: source === 'manual' ? 900 : null,
    created_by: source === 'manual' ? 'other-user' : 'zhuqin',
    note: '',
    base_workflow_version: workflowVersion ? {
      version: workflowVersion,
      semantic_digest: `sha256:workflow-v${workflowVersion}`,
      compiler_profile: 'compiler-core-v1',
    } : null,
    run_source: source === 'system' && workflowVersion
      ? { type: 'workflow_version', version: workflowVersion, semantic_digest: `sha256:workflow-v${workflowVersion}` }
      : null,
    validation: { status: 'valid', diagnostics: [] },
    ...(content === undefined ? {} : { content }),
    created_at: createdAt,
  })

  let derivedRequest: Record<string, any> | undefined
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          workflow_slug: 'fastp_bwa_demo',
          results: [revision(902, 2, 'system'), revision(901, null, 'manual')],
        }),
      })
      return
    }
    derivedRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify(revision(903, null, 'manual', derivedRequest?.content)),
    })
  })
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/902', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(revision(902, 2, 'system', `${validWdl}\n# latest revision`)),
  }))
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/wdl-versions/901', async (route) => {
    await new Promise(resolve => setTimeout(resolve, 600))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(revision(901, null, 'manual', `${validWdl}\n# selected old revision`)),
    })
  })

  await page.reload()
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
  await openPrimarySection(page, '流程库')
  await page.getByLabel('选择 WDL 版本').selectOption('901')
  await expect(page.getByText('正在读取 WDL 修订…')).toBeVisible()
  await expect(page.getByRole('button', { name: '修改 WDL', exact: true })).toBeDisabled()
  await expect(page.getByText('正在读取 WDL 修订…')).toBeHidden()
  await expect(page.getByLabel('WDL 只读预览')).toContainText('# selected old revision')
  await expect(page.getByLabel('WDL 只读预览')).not.toContainText('# latest revision')

  await page.getByRole('button', { name: '修改 WDL', exact: true }).click()
  await page.getByRole('button', { name: /创建派生稿/ }).click()
  await expect(page.getByLabel('选择 WDL 版本')).toBeDisabled()
  await expect(page.getByRole('button', { name: '验证', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: /发布流程版本|版本已发布|发布失败/ })).toBeDisabled()
  page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('WDL 派生稿尚未保存')
    await dialog.dismiss()
  })
  const editingUrl = page.url()
  await page.getByLabel('主要导航').getByRole('button', { name: '运行分析', exact: true }).click()
  await expect(page).toHaveURL(editingUrl)
  await expect(page.getByLabel('编辑 WDL 内容')).toBeVisible()
  const editor = page.getByLabel('编辑 WDL 内容')
  await editor.fill(`${await editor.inputValue()}\n# next derived revision`)
  await page.getByRole('button', { name: '保存派生稿', exact: true }).click()
  expect(derivedRequest?.workflow_version).toBeNull()
  expect(derivedRequest?.base_wdl_version).toBe(901)
  expect(derivedRequest?.base_wdl_digest).toBe('sha256:901')

  let assetRequest: Record<string, any> | undefined
  await page.route('**/api/v1/wdl-assets', async (route) => {
    assetRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({ slug: 'unbound-derived' }),
    })
  })
  await page.getByRole('button', { name: '修改 WDL', exact: true }).click()
  await page.getByRole('button', { name: /转为历史 WDL 资产/ }).click()
  await page.waitForURL('**/wdl/unbound-derived')
  expect(assetRequest?.source_revision).toBe('workflow-draft:wdl-v903')
})

test('shows a scrollable WDL preview for the compiled demo', async ({ page }) => {
  await openPrimarySection(page, '流程库')
  await selectWorkflow(page, 'fastp → BWA-MEM demo')

  const preview = page.getByLabel('WDL 只读预览')
  await expect(preview).toBeVisible()
  await expect(preview.locator('code').filter({ hasText: 'version 1.0' }).first()).toBeVisible()
  await expect(preview).toHaveCSS('overflow-y', 'auto')

  const canScroll = await preview.evaluate(element => element.scrollHeight > element.clientHeight)
  expect(canScroll).toBe(true)
})

test('persists a tool parameter edit and restores the original workflow', async ({ page }) => {
  const request = page.request
  const workflowUrl = '/api/v1/editor/workflows/fastp_bwa_demo'
  const originalResponse = await request.get(workflowUrl)
  expect(originalResponse.ok()).toBe(true)
  const originalDocument = await originalResponse.json()
  const originalFastp = originalDocument.workflow_graph.nodes.find(
    (node: Record<string, any>) => node.id === 'fastp_1',
  )
  const originalThreads = originalFastp?.parameter_values?.threads
  const nextThreads = originalThreads === 7 ? 8 : 7

  try {
    const fastpNode = page.locator('.vue-flow__node').filter({ hasText: 'fastp_1' }).first()
    await expect(fastpNode).toBeVisible()
    await fastpNode.dispatchEvent('click')

    const threadsField = page.locator('.parameter-field').filter({ hasText: 'threads' })
    const threadsInput = threadsField.locator('input')
    await expect(threadsInput).toBeVisible()

    const savedResponse = page.waitForResponse(response =>
      response.url().endsWith(workflowUrl)
      && response.request().method() === 'PUT'
      && response.ok(),
    )
    await threadsInput.fill(String(nextThreads))
    await threadsInput.blur()
    await savedResponse

    const persistedResponse = await request.get(workflowUrl)
    const persistedDocument = await persistedResponse.json()
    const persistedFastp = persistedDocument.workflow_graph.nodes.find(
      (node: Record<string, any>) => node.id === 'fastp_1',
    )
    expect(persistedFastp.parameter_values.threads).toBe(nextThreads)

    await page.reload()
    await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
    const reloadedFastpNode = page.locator('.vue-flow__node').filter({ hasText: 'fastp_1' }).first()
    await reloadedFastpNode.dispatchEvent('click')
    await expect(page.locator('.parameter-field').filter({ hasText: 'threads' }).locator('input'))
      .toHaveValue(String(nextThreads))
  } finally {
    const restored = await restoreWorkflow(page, workflowUrl, originalDocument)
    expect(restored.ok()).toBe(true)
    await page.close()
  }
})

test('shows validation diagnostics for an incompatible semantic type', async ({ page }) => {
  const request = page.request
  const workflowUrl = '/api/v1/editor/workflows/fastp_bwa_demo'
  const originalResponse = await request.get(workflowUrl)
  expect(originalResponse.ok()).toBe(true)
  const originalDocument = await originalResponse.json()

  try {
    const inputNode = page.locator('.vue-flow__node').filter({ hasText: 'input_reads_1' }).first()
    await expect(inputNode).toBeVisible()
    await inputNode.dispatchEvent('click')

    const semanticTypeInput = page.getByRole('textbox', { name: /语义类型/ })
    const savedResponse = page.waitForResponse(response =>
      response.url().endsWith(workflowUrl)
      && response.request().method() === 'PUT'
      && response.ok(),
    )
    await semanticTypeInput.fill('bio.invalid.fastq')
    await semanticTypeInput.blur()
    await savedResponse

    const validationResponsePromise = page.waitForResponse(response =>
      response.url().endsWith('/api/v1/validations/workflow-graph')
      && response.request().method() === 'POST',
    )
    await page.getByRole('button', { name: '验证', exact: true }).click()
    const validationResponse = await validationResponsePromise
    const validation = await validationResponse.json()

    expect(validation.validation.status).toBe('invalid')
    expect(validation.validation.diagnostics.length).toBeGreaterThan(0)
    await expect(page.getByRole('tab', { name: /诊断/ })).toHaveAttribute('aria-selected', 'true')
    await expect(page.locator('.diagnostic').first()).toBeVisible()
  } finally {
    const restored = await restoreWorkflow(page, workflowUrl, originalDocument)
    expect(restored.ok()).toBe(true)
    await page.close()
  }
})

test('画布过期时保留本地草稿并要求显式处理冲突', async ({ page }) => {
  const request = page.request
  const workflowUrl = '/api/v1/editor/workflows/fastp_bwa_demo'
  const originalResponse = await request.get(workflowUrl)
  expect(originalResponse.ok()).toBe(true)
  const originalDocument = await originalResponse.json()

  try {
    const remoteSaved = await request.put(workflowUrl, {
      data: {
        ...originalDocument,
        description: 'Remote collaboration update.',
        base_document_version: originalDocument.document_version,
        base_document_digest: originalDocument.document_digest,
      },
      headers: await csrfHeaders(page),
    })
    expect(remoteSaved.ok()).toBe(true)

    const fastpNode = page.locator('.vue-flow__node').filter({ hasText: 'fastp_1' }).first()
    await fastpNode.dispatchEvent('click')
    const threadsInput = page.locator('.parameter-field').filter({ hasText: 'threads' }).locator('input')
    const conflictResponse = page.waitForResponse(response =>
      response.url().endsWith(workflowUrl)
      && response.request().method() === 'PUT'
      && response.status() === 409,
    )
    await threadsInput.fill('9')
    await threadsInput.blur()
    await conflictResponse

    const conflict = page.getByRole('alert').filter({ hasText: '远端画布已更新' })
    await expect(conflict).toBeVisible()
    await expect(conflict).toContainText('本地改动没有被覆盖')
    await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('存在保存冲突')
    const loadRemote = conflict.getByRole('button', { name: '加载远端版本' })
    await expect(loadRemote).toBeDisabled()

    let leaveMessage = ''
    const leaveHandled = new Promise<void>((resolve) => {
      page.once('dialog', async (dialog) => {
        leaveMessage = dialog.message()
        await dialog.dismiss()
        resolve()
      })
    })
    const conflictUrl = page.url()
    await page.getByRole('button', { name: '运行分析', exact: true }).click()
    await leaveHandled
    expect(leaveMessage).toContain('本地草稿尚未下载')
    await expect(page).toHaveURL(conflictUrl)
    await expect(conflict).toBeVisible()

    const download = page.waitForEvent('download')
    await conflict.getByRole('button', { name: '下载本地草稿' }).click()
    expect((await download).suggestedFilename()).toMatch(/^fastp_bwa_demo\.local-draft-v\d+\.json$/)
    await expect(loadRemote).toBeEnabled()
    await loadRemote.click()

    await expect(conflict).toHaveCount(0)
    await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
    const current = await (await request.get(workflowUrl)).json()
    expect(current.description).toBe('Remote collaboration update.')
  } finally {
    const restored = await restoreWorkflow(page, workflowUrl, originalDocument)
    expect(restored.ok()).toBe(true)
    await page.close()
  }
})

test('rejects changing a published tool version and restores the original draft', async ({ page }) => {
  const request = page.request
  const toolId = 'fastp'
  const draftUrl = `/api/v1/tools/${toolId}/drafts`
  const publishUrl = `/api/v1/tools/${toolId}/publish`
  const versionsUrl = `/api/v1/tools/${toolId}/versions`
  const originalDraftResponse = await request.get(draftUrl)
  expect(originalDraftResponse.ok()).toBe(true)
  const originalDraft = await originalDraftResponse.json()
  const originalVersionsResponse = await request.get(versionsUrl)
  expect(originalVersionsResponse.ok()).toBe(true)
  const originalVersions = await originalVersionsResponse.json()
  expect(
    originalVersions.results.some(
      (version: Record<string, any>) => version.version === originalDraft.draft_spec.tool_version,
    ),
  ).toBe(true)

  try {
    await openPrimarySection(page, '工具库')
    await page.getByPlaceholder('搜索工具名称或版本').fill(toolId)
    const toolRow = page.locator('.registry-row').filter({ hasText: toolId })
    await expect(toolRow).toHaveCount(1)
    await toolRow.getByRole('button', { name: /查看版本|已展开/ }).click()

    await page.getByLabel('工具版本检查器').getByRole('button', { name: /待发布草稿/ }).click()
    await expect(page).toHaveURL(/toolDraft=1/)
    const editor = page.locator('.tool-editor')
    const changedDescription = `${originalDraft.draft_spec.description} E2E immutable boundary.`
    const description = editor.getByLabel('说明', { exact: true })
    await expect(description).toHaveValue(originalDraft.draft_spec.description)
    await description.fill(changedDescription)

    const savedResponsePromise = page.waitForResponse(response =>
      response.url().endsWith(draftUrl)
      && response.request().method() === 'PUT',
    )
    await editor.getByRole('button', { name: '保存草稿', exact: true }).click()
    const savedResponse = await savedResponsePromise
    expect(savedResponse.ok()).toBe(true)
    await expect(editor.getByText('校验通过', { exact: true })).toBeVisible()

    const publishResponsePromise = page.waitForResponse(response =>
      response.url().endsWith(publishUrl)
      && response.request().method() === 'POST',
    )
    await editor.getByRole('button', { name: '发布新版本', exact: true }).click()
    const publishResponse = await publishResponsePromise
    expect(publishResponse.status()).toBe(409)
    expect((await publishResponse.json()).error.code).toBe('TOOL_VERSION_IMMUTABLE')

    const publishError = page.getByRole('alert')
    await expect(publishError).toContainText('TOOL_VERSION_IMMUTABLE')
    await expect(publishError).toContainText(/该工具版本已发布且内容不可修改.*提升软件版本/)
  } finally {
    const currentDraftResponse = await request.get(draftUrl)
    expect(currentDraftResponse.ok()).toBe(true)
    const currentDraft = await currentDraftResponse.json()
    const restored = await request.put(draftUrl, {
      data: {
        tool_spec: originalDraft.draft_spec,
        base_draft_version: currentDraft.draft_version,
        base_draft_digest: currentDraft.draft_digest,
      },
      headers: await csrfHeaders(page),
    })
    expect(restored.ok()).toBe(true)

    const restoredDraftResponse = await request.get(draftUrl)
    expect(restoredDraftResponse.ok()).toBe(true)
    const restoredDraft = await restoredDraftResponse.json()
    expect(restoredDraft.draft_spec).toEqual(originalDraft.draft_spec)
    expect(restoredDraft.validation.status).toBe(originalDraft.validation.status)

    const restoredVersionsResponse = await request.get(versionsUrl)
    expect(restoredVersionsResponse.ok()).toBe(true)
    expect(await restoredVersionsResponse.json()).toEqual(originalVersions)
    await page.close()
  }
})

test('shows all mocked compile artifacts without writing compilation history', async ({ page }) => {
  const request = page.request
  const workflowSlug = 'fastp_bwa_demo'
  const workflowUrl = `/api/v1/editor/workflows/${workflowSlug}`
  const versionsUrl = `${workflowUrl}/versions`
  const compilationsUrl = `${workflowUrl}/compilations`
  const revisionsUrl = `${workflowUrl}/wdl-versions`
  const beforeResponses = await Promise.all([
    request.get(workflowUrl),
    request.get(versionsUrl),
    request.get(compilationsUrl),
    request.get(revisionsUrl),
  ])
  for (const response of beforeResponses) expect(response.ok()).toBe(true)
  const beforeState = await Promise.all(beforeResponses.map(response => response.json()))

  const mockWdl = [
    'version 1.0',
    '',
    'workflow fastp_bwa_demo {',
    '  input { File reads_1 }',
    '  output { File aligned_bam = reads_1 }',
    '}',
    '',
  ].join('\n')
  const mockArtifacts = [
    {
      name: 'compiler-ir.json',
      media_type: 'application/json',
      digest: 'sha256:1111111111111111111111111111111111111111111111111111111111111111',
      content: '{\n  "workflow": "fastp_bwa_demo"\n}\n',
    },
    {
      name: 'workflow.wdl',
      media_type: 'application/wdl',
      digest: 'sha256:2222222222222222222222222222222222222222222222222222222222222222',
      content: mockWdl,
    },
    {
      name: 'inputs.template.json',
      media_type: 'application/json',
      digest: 'sha256:3333333333333333333333333333333333333333333333333333333333333333',
      content: '{\n  "fastp_bwa_demo.reads_1": null\n}\n',
    },
    {
      name: 'compile-manifest.json',
      media_type: 'application/json',
      digest: 'sha256:4444444444444444444444444444444444444444444444444444444444444444',
      content: '{\n  "compiler_contract": "phase1"\n}\n',
    },
  ]
  const mockedRequests: string[] = []
  let publishPayload: Record<string, any> | undefined
  let compilePayload: Record<string, any> | undefined

  await page.route(`**${workflowUrl}`, async (route) => {
    if (route.request().method() !== 'PUT') return route.continue()
    mockedRequests.push(`PUT ${workflowUrl}`)
    await route.fulfill({ status: 200, json: beforeState[0] })
  })
  await page.route(`**${versionsUrl}`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    mockedRequests.push(`POST ${versionsUrl}`)
    publishPayload = route.request().postDataJSON()
    await route.fulfill({ status: 201, json: { version: 999 } })
  })
  await page.route('**/api/v1/compilations', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    mockedRequests.push('POST /api/v1/compilations')
    compilePayload = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      json: {
        status: 'succeeded',
        request_id: 'e2e-mocked-compilation',
        validation: { status: 'valid', diagnostics: [] },
        artifacts: mockArtifacts,
        wdl_revision: null,
      },
    })
  })
  await page.route(`**${revisionsUrl}`, async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    mockedRequests.push(`GET ${revisionsUrl}`)
    await route.fulfill({
      status: 200,
      json: {
        workflow_slug: workflowSlug,
        results: [{
          version: 999,
          source: 'system',
          content: mockWdl,
          workflow_version: 999,
          validation: { status: 'valid', diagnostics: [] },
          created_at: '2026-07-28T00:00:00Z',
        }],
      },
    })
  })

  const workflowLibraryRefresh = page.waitForResponse(response =>
    response.url().endsWith('/api/v1/editor/workflows')
    && response.request().method() === 'GET',
  )
  await page.getByRole('button', { name: '发布流程版本' }).click()
  await workflowLibraryRefresh
  await expect(page.getByRole('heading', { name: '流程库', exact: true, level: 1 })).toBeVisible()
  await expect(page.locator('.version-row').filter({ hasText: 'v999' })).toBeVisible()
  const preview = page.getByLabel('WDL 只读预览')
  await expect(preview).toContainText('workflow fastp_bwa_demo')

  await page.getByLabel('主要导航').getByRole('button', { name: '编辑器', exact: true }).click()
  await expect(page.locator('#workflow-canvas')).toBeVisible()
  await page.getByRole('tab', { name: /产物/ }).click()
  for (const artifact of mockArtifacts) {
    await expect(page.locator('.artifact-card').filter({ hasText: artifact.name })).toBeVisible()
  }
  const wdlCard = page.locator('.artifact-card').filter({ hasText: 'workflow.wdl' })
  await wdlCard.getByRole('button', { name: '预览' }).click()
  const artifactDialog = page.getByRole('dialog', { name: '预览 workflow.wdl' })
  await expect(artifactDialog).toContainText('workflow fastp_bwa_demo')
  await artifactDialog.getByRole('button', { name: '关闭预览' }).click()

  expect(mockedRequests).toEqual([
    `POST ${versionsUrl}`,
    'POST /api/v1/compilations',
    `GET ${revisionsUrl}`,
  ])
  expect(publishPayload).toMatchObject({
    reuse_unchanged: true,
    base_document_version: beforeState[0].document_version,
    base_document_digest: beforeState[0].document_digest,
  })
  const expectedGraph = beforeState[0].workflow_graph
  const { layout: _expectedLayout, ...expectedSemanticGraph } = expectedGraph
  expect(compilePayload).toMatchObject({
    workflow_graph: {
      ...expectedSemanticGraph,
    },
    tool_specs: beforeState[0].tool_specs,
    workflow_version: 999,
  })
  const afterResponses = await Promise.all([
    request.get(workflowUrl),
    request.get(versionsUrl),
    request.get(compilationsUrl),
    request.get(revisionsUrl),
  ])
  for (const response of afterResponses) expect(response.ok()).toBe(true)
  const afterState = await Promise.all(afterResponses.map(response => response.json()))
  expect(afterState).toEqual(beforeState)
})
