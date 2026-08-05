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

test.beforeEach(async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('用户名').fill('zhuqin')
  await page.getByLabel('密码').fill('zhuqin')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL('**/wdl')
  await page.goto('/')
  await expect(page.getByText('BioWorkflowManage', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '保存草稿' })).toContainText('草稿已保存')
})

test('can navigate libraries and switch the active workflow', async ({ page }) => {
  await openPrimarySection(page, '工具库')
  await expect(page.locator('.registry-list')).toBeVisible()

  await openPrimarySection(page, '流程库')
  await expect(page.locator('.workflow-index')).toBeVisible()

  await selectWorkflow(page, 'fastp demo')
  await expect(page.locator('.version-sidebar__title strong')).toHaveText('fastp demo')
  await expect(page.locator('.version-sidebar__title small')).toHaveText('fastp_demo')

  await selectWorkflow(page, 'fastp → BWA-MEM demo')
  await expect(page.locator('.version-sidebar__title strong')).toHaveText('fastp → BWA-MEM demo')
  await expect(page.locator('.version-sidebar__title small')).toHaveText('fastp_bwa_demo')
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
    await fastpNode.click()

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
    await reloadedFastpNode.click()
    await expect(page.locator('.parameter-field').filter({ hasText: 'threads' }).locator('input'))
      .toHaveValue(String(nextThreads))
  } finally {
    const restored = await request.put(workflowUrl, {
      data: originalDocument,
      headers: await csrfHeaders(page),
    })
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
    await inputNode.click()

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
    const restored = await request.put(workflowUrl, {
      data: originalDocument,
      headers: await csrfHeaders(page),
    })
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
    await expect(page.getByText('校验通过', { exact: true })).toBeVisible()

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
    const restored = await request.put(draftUrl, {
      data: { tool_spec: originalDraft.draft_spec },
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

  await page.route(`**${workflowUrl}`, async (route) => {
    if (route.request().method() !== 'PUT') return route.continue()
    mockedRequests.push(`PUT ${workflowUrl}`)
    await route.fulfill({ status: 200, json: beforeState[0] })
  })
  await page.route(`**${versionsUrl}`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    mockedRequests.push(`POST ${versionsUrl}`)
    await route.fulfill({ status: 201, json: { version: 999 } })
  })
  await page.route('**/api/v1/compilations', async (route) => {
    if (route.request().method() !== 'POST') return route.continue()
    mockedRequests.push('POST /api/v1/compilations')
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

  await page.getByRole('button', { name: '编译流程' }).click()
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
    `PUT ${workflowUrl}`,
    `POST ${versionsUrl}`,
    'POST /api/v1/compilations',
    `GET ${revisionsUrl}`,
  ])
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
