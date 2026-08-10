import { expect, test, type Page } from '@playwright/test'

const sourcePadding = Array.from(
  { length: 30 },
  (_, index) => `# 历史 WDL 注释 ${index + 1}`,
).join('\n')

const source = `version 1.0
${sourcePadding}

task hello {
input {
String name
}
command <<<
echo "~{name}" > greeting.txt
>>>
output {
File greeting = "greeting.txt"
}
}

workflow greeting {
call hello { input: name = "world" }
}
`

const formattedSource = `version 1.0

${sourcePadding}
task hello {
  input {
    String name
  }

  command <<<
    echo "~{name}" > greeting.txt
  >>>

  output {
    File greeting = "greeting.txt"
  }
}

workflow greeting {
  call hello { input:
    name = "world"
  }
}
`

function analysis(version = '1.0') {
  return {
    status: 'valid',
    parsed: true,
    wdl_version: version,
    summary: {
      task_count: 1,
      workflow_count: 1,
      import_count: 0,
      error_count: 0,
    },
    imports: [],
    tasks: [{
      id: 'solid-tumor.wdl::hello',
      name: 'hello',
      file_path: 'solid-tumor.wdl',
      line: 33,
      end_line: 43,
      inputs: [{ name: 'name', type: 'String', line: 35 }],
      outputs: [{ name: 'greeting', type: 'File', line: 41 }],
      runtime_keys: [],
    }],
    workflows: [{
      name: 'greeting',
      file_path: 'solid-tumor.wdl',
      line: 45,
      end_line: 47,
      inputs: [],
      outputs: [],
      structure: { call_count: 1, scatter_count: 0, conditional_count: 0 },
    }],
    diagnostics: [],
  }
}

async function mockWdlApi(page: Page) {
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
  let revisionVersion = 1
  let revisionContent = source
  let assetName = '实体瘤 WES hg38'
  let assetDescription = '生产环境使用的实体瘤分析流程'
  let lifecycle = 'active'
  let tags = ['实体瘤', 'hg38']
  const tagPool = [
    { id: 1, name: '实体瘤', asset_count: 1 },
    { id: 2, name: 'hg38', asset_count: 1 },
    { id: 3, name: '血液肿瘤', asset_count: 0 },
    { id: 4, name: 'wes', asset_count: 0 },
  ]
  const metadataRequests: Record<string, unknown>[] = []
  const tagRequests: Array<{ method: string; id: number; name?: string }> = []
  let events = [{
    id: 1,
    action: 'import',
    actor: 'local-user',
    note: '从生产目录导入',
    changes: { tags: { before: [], after: tags } },
    diff: '',
    revision: 1,
    created_at: '2026-07-29T10:00:00Z',
  }]

  const currentRevision = () => ({
    version: revisionVersion,
    operation: revisionVersion === 1 ? 'import' : 'format',
    digest: `sha256:${'a'.repeat(54)}${revisionVersion.toString().padStart(10, '0')}`,
    diff: revisionVersion === 1 ? '' : '@@ -4,3 +4,3 @@',
    note: revisionVersion === 1 ? '从生产目录导入' : '统一格式',
    actor: 'local-user',
    analysis: analysis(),
    created_at: revisionVersion === 1 ? '2026-07-29T10:00:00Z' : '2026-07-29T11:00:00Z',
    content: revisionContent,
    entrypoint: 'solid-tumor.wdl',
    files: [{
      path: 'solid-tumor.wdl',
      digest: `sha256:${'a'.repeat(54)}${revisionVersion.toString().padStart(10, '0')}`,
      is_entry: true,
      content: revisionContent,
    }],
  })

  const asset = () => ({
    slug: 'solid-tumor-hg38',
    name: assetName,
    description: assetDescription,
    source_filename: 'solid-tumor.wdl',
    source_repository: '',
    source_revision: '',
    lifecycle,
    tags,
    created_by: 'local-user',
    created_at: '2026-07-29T10:00:00Z',
    updated_at: '2026-07-29T10:00:00Z',
    revision_count: revisionVersion,
    file_count: 1,
    current_revision: currentRevision(),
    revisions: Array.from({ length: revisionVersion }, (_, index) => ({
      ...currentRevision(),
      version: revisionVersion - index,
      content: undefined,
    })),
    audit_events: events,
  })

  await page.route('**/api/v1/wdl-assets/tags', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      results: tagPool,
    }),
  }))

  await page.route('**/api/v1/wdl-assets/tags/*', async (route) => {
    const id = Number(route.request().url().split('/').pop())
    const tagIndex = tagPool.findIndex(tag => tag.id === id)
    const tag = tagPool[tagIndex]
    if (!tag) {
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: { code: 'WDL_TAG_NOT_FOUND' } }),
      })
      return
    }
    if (route.request().method() === 'DELETE') {
      tagRequests.push({ method: 'DELETE', id })
      if (tag.asset_count > 0) {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'WDL_TAG_IN_USE' } }),
        })
        return
      }
      tagPool.splice(tagIndex, 1)
      await route.fulfill({ status: 204, body: '' })
      return
    }
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { name: string }
      tagRequests.push({ method: 'PATCH', id, name: body.name })
      const conflict = tagPool.some(
        item => item.id !== id
          && item.name.toLocaleLowerCase() === body.name.toLocaleLowerCase(),
      )
      if (conflict) {
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'WDL_TAG_CONFLICT' } }),
        })
        return
      }
      const previousName = tag.name
      tag.name = body.name
      tags = tags.map(name => name === previousName ? tag.name : name)
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(tag),
      })
      return
    }
    await route.fallback()
  })

  await page.route(/\/api\/v1\/wdl-assets(?:\?.*)?$/, async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [asset()] }),
    })
  })

  await page.route('**/api/v1/wdl-assets/solid-tumor-hg38/format', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      content: formattedSource,
      changed: true,
      diff: '@@ -4,8 +4,8 @@\n-input {\n+  input {\n-String name\n+    String name\n',
      analysis: analysis(),
    }),
  }))

  await page.route('**/api/v1/wdl-assets/solid-tumor-hg38/export', route => route.fulfill({
    status: 200,
    contentType: 'application/wdl',
    headers: { 'Content-Disposition': 'attachment; filename="solid-tumor.wdl"' },
    body: revisionContent,
  }))

  await page.route('**/api/v1/wdl-assets/solid-tumor-hg38/revisions', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    revisionVersion = 2
    revisionContent = formattedSource
    events = [{
      id: 2,
      action: 'format',
      actor: 'local-user',
      note: '统一格式',
      changes: { revision: { before: 1, after: 2 } },
      diff: '@@ -4,8 +4,8 @@\n-input {\n+  input {\n',
      revision: 2,
      created_at: '2026-07-29T11:00:00Z',
    }, ...events]
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify(currentRevision()),
    })
  })

  await page.route('**/api/v1/wdl-assets/solid-tumor-hg38', async (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as Record<string, unknown>
      metadataRequests.push(body)
      const changes: Record<string, { before: unknown; after: unknown }> = {}
      if (typeof body.name === 'string') {
        changes.name = { before: assetName, after: body.name }
        assetName = body.name
      }
      if (typeof body.description === 'string') {
        changes.description = { before: assetDescription, after: body.description }
        assetDescription = body.description
      }
      if (typeof body.lifecycle === 'string') {
        changes.lifecycle = { before: lifecycle, after: body.lifecycle }
        lifecycle = body.lifecycle
      }
      if (Array.isArray(body.tags)) {
        const canonicalTags = body.tags.map((name) => {
          const requestedName = String(name)
          const existing = tagPool.find(
            tag => tag.name.toLocaleLowerCase() === requestedName.toLocaleLowerCase(),
          )
          if (existing) return existing.name
          tagPool.push({
            id: Math.max(...tagPool.map(tag => tag.id), 0) + 1,
            name: requestedName,
            asset_count: 0,
          })
          return requestedName
        })
        changes.tags = { before: tags, after: canonicalTags }
        tags = canonicalTags
      }
      events = [{
        id: events[0].id + 1,
        action: 'metadata_update',
        actor: 'local-user',
        note: typeof body.note === 'string' ? body.note : '',
        changes,
        diff: '',
        revision: null,
        created_at: '2026-07-29T12:00:00Z',
      }, ...events]
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(asset()),
    })
  })

  await page.route(/\/api\/v1\/wdl-packages(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: [] }),
    })
  })

  return { metadataRequests, tagRequests }
}

test('references a fixed tool-package version without copying package files into the revision', async ({ page }) => {
  await mockWdlApi(page)
  const packageDigest = `sha256:${'b'.repeat(64)}`
  const fileDigest = `sha256:${'c'.repeat(64)}`
  const packageFile = {
    path: 'task/qc.wdl',
    digest: fileDigest,
    content: `version 1.0\n\ntask QC {\n  command <<< echo qc >>>\n}\n`,
    analysis: {},
  }
  const packageVersion = {
    version: '1.0.0',
    digest: packageDigest,
    source_repository: 'example/minwdl',
    source_revision: 'abc123',
    note: '',
    actor: 'zhuqin',
    analysis: analysis(),
    file_count: 1,
    files: [packageFile],
    created_at: '2026-08-04T02:00:00Z',
  }
  const packageAsset = {
    slug: 'solid-tumor-tools',
    name: 'Solid Tumor Tools',
    description: '实体瘤公共 Task',
    lifecycle: 'active',
    tags: ['实体瘤'],
    created_by: 'zhuqin',
    created_at: '2026-08-04T02:00:00Z',
    updated_at: '2026-08-04T02:00:00Z',
    version_count: 1,
    reference_count: 0,
    latest_version: packageVersion,
    versions: [packageVersion],
    audit_events: [],
  }
  await page.route('**/api/v1/wdl-packages/solid-tumor-tools/versions/1.0.0', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(packageVersion),
  }))
  await page.route('**/api/v1/wdl-packages/solid-tumor-tools', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(packageAsset),
  }))
  await page.route(/\/api\/v1\/wdl-packages(?:\?.*)?$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [packageAsset] }),
  }))

  const revisionRequests: Record<string, any>[] = []
  await page.route('**/api/v1/wdl-assets/solid-tumor-hg38/revisions', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback()
      return
    }
    const body = route.request().postDataJSON() as Record<string, any>
    revisionRequests.push(body)
    const main = body.files[0]
    const reference = {
      package_slug: 'solid-tumor-tools',
      package_name: 'Solid Tumor Tools',
      package_lifecycle: 'active',
      version: '1.0.0',
      digest: packageDigest,
      mount_prefix: 'packages/solid-tumor-tools/1.0.0',
      file_count: 1,
      files: [{
        path: packageFile.path,
        digest: fileDigest,
        mounted_path: 'packages/solid-tumor-tools/1.0.0/task/qc.wdl',
      }],
    }
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        version: 2,
        operation: 'edit',
        digest: `sha256:${'d'.repeat(64)}`,
        diff: '',
        note: body.note,
        actor: 'zhuqin',
        analysis: analysis(),
        created_at: '2026-08-04T03:00:00Z',
        content: main.content,
        entrypoint: 'solid-tumor.wdl',
        package_references: [reference],
        files: [
          { ...main, digest: `sha256:${'d'.repeat(64)}`, is_entry: true, origin: 'asset', read_only: false },
          {
            path: reference.files[0].mounted_path,
            content: packageFile.content,
            digest: fileDigest,
            is_entry: false,
            origin: 'package',
            read_only: true,
            package_reference: {
              package_slug: reference.package_slug,
              package_name: reference.package_name,
              version: reference.version,
              digest: reference.digest,
              mount_prefix: reference.mount_prefix,
              package_file_path: packageFile.path,
            },
          },
        ],
      }),
    })
  })

  await page.goto('/wdl/solid-tumor-hg38')
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })
  await page.getByRole('button', { name: '引用工具包' }).click()
  await page.getByLabel('工具包').selectOption('solid-tumor-tools')
  await expect(page.getByRole('combobox', { name: '版本', exact: true })).toHaveValue('1.0.0')
  await page.getByRole('button', { name: '确认引用' }).click()

  await expect(page.getByText(/已引用 Solid Tumor Tools 1.0.0/)).toBeVisible()
  await expect(page.getByRole('button', { name: /qc\.wdl 包/ })).toBeVisible()
  await page.keyboard.press('Control+S')
  await expect(page.locator('.save-state')).toContainText('WDL v2')

  expect(revisionRequests).toHaveLength(1)
  expect(revisionRequests[0]!.files).toHaveLength(1)
  expect(revisionRequests[0]!.files[0].path).toBe('solid-tumor.wdl')
  expect(revisionRequests[0]!.files[0].content).toContain(
    'import "packages/solid-tumor-tools/1.0.0/task/qc.wdl" as qc',
  )
  expect(revisionRequests[0]!.package_references).toEqual([{
    package_slug: 'solid-tumor-tools',
    version: '1.0.0',
    digest: packageDigest,
    mount_prefix: 'packages/solid-tumor-tools/1.0.0',
  }])
})

test('lists tagged historical assets and opens the WDL workbench', async ({ page }) => {
  await mockWdlApi(page)
  await page.goto('/wdl')

  await expect(page.getByRole('heading', { name: '历史 WDL 资产' })).toBeVisible()
  await expect(page.getByRole('cell', { name: /实体瘤 WES hg38/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /实体瘤 1/ })).toBeVisible()
  await expect(page.getByText('1 task · 1 workflow')).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '状态' })).toHaveCount(0)

  await page.getByRole('link', { name: '打开工作台' }).click()

  await expect(page.getByRole('heading', { name: '实体瘤 WES hg38' })).toBeVisible()
  await expect(page.getByText('生命周期', { exact: true })).toHaveCount(0)
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })
  await expect(
    page.locator('.definition-group').filter({ hasText: 'Tasks' }).getByText('hello', { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Tasks', exact: true })).toBeVisible()
})

test('renames used tags inline and deletes only unused tags from the global pool', async ({ page }) => {
  const api = await mockWdlApi(page)
  await page.goto('/wdl')

  const usedTag = page.getByRole('button', { name: /实体瘤 1/ })
  await expect(usedTag).toBeVisible()
  await expect(page.getByRole('button', { name: '删除未使用标签 实体瘤' })).toHaveCount(0)

  await page.getByRole('button', { name: '删除未使用标签 血液肿瘤' }).click()
  await expect(page.getByRole('button', { name: /血液肿瘤 0/ })).toHaveCount(0)
  expect(api.tagRequests).toContainEqual({ method: 'DELETE', id: 3 })

  await usedTag.dblclick()
  const renameInput = page.getByLabel('重命名标签 实体瘤')
  await renameInput.fill('实体肿瘤')
  await renameInput.blur()

  await expect(page.getByRole('button', { name: /实体肿瘤 1/ })).toBeVisible()
  await expect(page.getByRole('cell', { name: '实体肿瘤' })).toBeVisible()
  expect(api.tagRequests).toContainEqual({
    method: 'PATCH',
    id: 1,
    name: '实体肿瘤',
  })
})

test('formats source and records a new auditable revision', async ({ page }) => {
  await mockWdlApi(page)
  await page.goto('/wdl/solid-tumor-hg38')
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/当前是保留原貌的导入版本/)).toBeVisible()
  await expect(page.getByRole('button', { name: '格式化', exact: true })).toBeVisible()
  await expect(page.locator('.wdl-format-button kbd')).toHaveText('⇧ Alt F')
  await expect(page.locator('.wdl-export-button kbd')).toHaveText('⇧ Alt E')
  await expect(page.locator('.wdl-save-button kbd')).toHaveText('Ctrl/⌘ S')
  const editorBoxBefore = await page.locator('.wdl-code-editor').boundingBox()

  await page.getByRole('button', { name: '格式化', exact: true }).click()
  await expect(page.getByText('格式化结果已应用到编辑器，保存后会形成新版本。')).toBeVisible()
  await expect(page.getByText('未保存变更')).toBeVisible()
  await expect(page.getByText(/当前是保留原貌的导入版本/)).toBeHidden()
  await expect(page.getByRole('tab', { name: '变更' })).toHaveAttribute('aria-selected', 'true')
  const editorBoxAfter = await page.locator('.wdl-code-editor').boundingBox()
  expect(editorBoxBefore).not.toBeNull()
  expect(editorBoxAfter).not.toBeNull()
  expect(editorBoxAfter!.y).toBeCloseTo(editorBoxBefore!.y, 1)
  expect(editorBoxAfter!.height).toBeCloseTo(editorBoxBefore!.height, 1)

  await page.getByLabel('本次修改备注').fill('统一格式')
  await page.keyboard.press('Control+S')

  await expect(page.getByText('WDL v2 已保存并写入操作历史。')).toBeVisible()
  await expect(page.getByText('v2', { exact: true }).first()).toBeVisible()
  await page.getByRole('tab', { name: /历史/ }).click()
  await expect(page.getByRole('button', { name: /格式化源码/ })).toBeVisible()
  await expect(page.getByText('统一格式', { exact: true })).toBeVisible()
})

test('formats from the editor shortcut without moving the viewport or focus', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 420 })
  await mockWdlApi(page)
  await page.goto('/wdl/solid-tumor-hg38')
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })

  const editorShell = page.locator('.wdl-code-editor')
  const editorInput = page.locator('.monaco-editor textarea.inputarea')
  const firstVisibleLine = page.locator('.monaco-editor .margin-view-overlays .line-numbers').first()
  await editorInput.focus()
  await editorShell.hover()
  await page.mouse.wheel(0, 420)
  await expect.poll(async () => Number(await firstVisibleLine.textContent())).toBeGreaterThan(1)

  const boxBefore = await editorShell.boundingBox()
  const firstLineBefore = await firstVisibleLine.textContent()
  await page.keyboard.press('Shift+Alt+F')
  await expect(page.getByText('格式化结果已应用到编辑器，保存后会形成新版本。')).toBeVisible()

  const boxAfter = await editorShell.boundingBox()
  const firstLineAfter = await firstVisibleLine.textContent()
  expect(await editorInput.evaluate(element => document.activeElement === element)).toBe(true)
  expect(boxBefore).not.toBeNull()
  expect(boxAfter).not.toBeNull()
  expect(boxAfter!.y).toBeCloseTo(boxBefore!.y, 1)
  expect(boxAfter!.height).toBeCloseTo(boxBefore!.height, 1)
  expect(firstLineAfter).toBe(firstLineBefore)
})

test('shows manual edits in the change panel before save', async ({ page }) => {
  await mockWdlApi(page)
  await page.goto('/wdl/solid-tumor-hg38')
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })

  const editorInput = page.locator('.monaco-editor textarea.inputarea')
  await editorInput.focus()
  await page.keyboard.press('Control+End')
  await page.keyboard.press('Enter')
  await page.keyboard.insertText('# manual change')

  const changeTab = page.getByRole('tab', { name: '变更' })
  await expect(changeTab).toBeVisible()
  await changeTab.click()
  await expect(page.getByText('未保存变更')).toBeVisible()
  await expect(page.locator('.diff-line--add').filter({ hasText: '# manual change' })).toBeVisible()
})

test('exports WDL with asset name, version, timestamp, and shortcut', async ({ page }) => {
  await mockWdlApi(page)
  await page.goto('/wdl/solid-tumor-hg38')
  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })

  await page.locator('.monaco-editor textarea.inputarea').focus()
  const downloadPromise = page.waitForEvent('download')
  await page.keyboard.press('Shift+Alt+E')
  const download = await downloadPromise

  expect(download.suggestedFilename()).toMatch(
    /^实体瘤-WES-hg38-v1-\d{8}-\d{6}\.wdl$/,
  )
  await expect(page.getByText(/已导出 实体瘤-WES-hg38-v1-\d{8}-\d{6}\.wdl/)).toBeVisible()
})

test('edits WDL title and description inline and saves each field on blur', async ({ page }) => {
  const api = await mockWdlApi(page)
  await page.goto('/wdl/solid-tumor-hg38')

  const title = page.locator('.wdl-asset-heading h1')
  await title.dblclick()
  await page.getByLabel('WDL 标题').fill('实体瘤 WES hg38 v2')
  await page.getByRole('heading', { name: '标签', exact: true }).click()
  await expect(title).toHaveText(/实体瘤 WES hg38 v2/)
  await expect.poll(
    () => api.metadataRequests.filter(request => 'name' in request).length,
  ).toBe(1)

  const description = page.locator('.inline-metadata-value--description')
  await description.dblclick()
  await page.getByLabel('WDL 说明').fill('升级后的生产分析流程')
  await page.getByRole('heading', { name: '标签', exact: true }).click()
  await expect(description).toHaveText(/升级后的生产分析流程/)
  await expect.poll(
    () => api.metadataRequests.filter(request => 'description' in request).length,
  ).toBe(1)

  await page.reload()
  await expect(page.locator('.wdl-asset-heading h1')).toHaveText(/实体瘤 WES hg38 v2/)
  await expect(page.locator('.inline-metadata-value--description')).toHaveText(
    /升级后的生产分析流程/,
  )
})

test('adds one tag on blur, reuses the tag pool, and shows three quick choices', async ({ page }) => {
  const api = await mockWdlApi(page)
  await page.goto('/wdl/solid-tumor-hg38')

  const quickTags = page.locator('.tag-suggestions--popular button')
  await expect(quickTags).toHaveCount(3)
  await expect(quickTags.nth(0)).toContainText('实体瘤')
  await expect(quickTags.nth(1)).toContainText('hg38')
  await expect(quickTags.nth(2)).toContainText('血液肿瘤')

  const input = page.getByLabel('添加标签')
  await input.focus()
  await input.blur()
  expect(api.metadataRequests).toHaveLength(0)

  await input.fill('WES')
  await input.blur()
  await expect(page.locator('.wdl-tag-editor__chip').filter({ hasText: 'wes' })).toBeVisible()
  await expect.poll(() => api.metadataRequests.length).toBe(1)
  expect(api.metadataRequests[0].tags).toContain('wes')

  await input.fill('HG38')
  await input.blur()
  await expect.poll(() => api.metadataRequests.length).toBe(1)

  await page.getByRole('button', { name: '+ 血液肿瘤', exact: true }).click()
  await expect(
    page.locator('.wdl-tag-editor__chip').filter({ hasText: '血液肿瘤' }),
  ).toBeVisible()
  await expect.poll(() => api.metadataRequests.length).toBe(2)
})
