import { expect, test, type Page } from '@playwright/test'

const qcSource = `version 1.0

task QC {
  input {
    File fastq
  }
  command <<<
    cp "~{fastq}" clean.fastq
  >>>
  output {
    File clean_fastq = "clean.fastq"
  }
}
`

function analysis() {
  return {
    status: 'valid',
    parsed: true,
    wdl_version: '1.0',
    wdl_versions: ['1.0'],
    package: {
      file_count: 1,
      module_count: 1,
      resolved_import_count: 0,
      missing_import_count: 0,
      external_import_count: 0,
    },
    summary: { task_count: 1, workflow_count: 0, import_count: 0, error_count: 0 },
    files: [{
      path: 'task/qc.wdl',
      digest: `sha256:${'a'.repeat(64)}`,
      status: 'valid',
      parsed: true,
      reachable: true,
      task_count: 1,
      workflow_count: 0,
      import_count: 0,
      diagnostics: [],
      wdl_version: '1.0',
    }],
    imports: [],
    tasks: [{
      id: 'task/qc.wdl::QC',
      name: 'QC',
      file_path: 'task/qc.wdl',
      line: 3,
      end_line: 15,
      inputs: [{ name: 'fastq', type: 'File', line: 5 }],
      outputs: [{ name: 'clean_fastq', type: 'File', line: 13 }],
      runtime_keys: [],
    }],
    workflows: [],
    diagnostics: [],
  }
}

function packageVersion(version = '1.0.0', includeContent = false) {
  return {
    version,
    digest: `sha256:${version === '1.0.0' ? 'b' : 'c'.repeat(64)}`,
    source_repository: 'example/minwdl',
    source_revision: 'abc123',
    note: '初始导入',
    actor: 'zhuqin',
    analysis: analysis(),
    file_count: 1,
    files: [{
      path: 'task/qc.wdl',
      digest: `sha256:${'a'.repeat(64)}`,
      analysis: analysis().files[0],
      ...(includeContent ? { content: qcSource } : {}),
    }],
    created_at: '2026-08-04T02:00:00Z',
  }
}

async function mockPackageApi(page: Page) {
  let versions = [packageVersion()]
  await page.route('**/api/v1/auth/me', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      user: {
        username: 'zhuqin', is_admin: true, role: 'admin',
        allowed_sections: ['overview', 'edit', 'tools', 'packages', 'artifacts', 'runs', 'wdl', 'help'],
      },
    }),
  }))
  await page.route('**/api/v1/wdl-packages/tags', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [{ id: 1, name: '实体瘤', package_count: 1 }] }),
  }))
  await page.route('**/api/v1/wdl-packages/preview', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      preview_digest: `sha256:${'d'.repeat(64)}`,
      can_publish: true,
      analysis: analysis(),
    }),
  }))
  await page.route('**/api/v1/wdl-packages/solid-tumor-tools/export*', route => route.fulfill({
    status: 200,
    contentType: 'application/zip',
    body: Buffer.from('package-zip'),
  }))
  await page.route('**/api/v1/wdl-packages/solid-tumor-tools/versions/*', route => {
    const version = decodeURIComponent(route.request().url().split('/').pop()!)
    const item = versions.find(candidate => candidate.version === version) ?? versions[0]!
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...item, files: packageVersion(item.version, true).files }),
    })
  })
  await page.route('**/api/v1/wdl-packages/solid-tumor-tools/versions', async (route) => {
    if (route.request().method() === 'POST') {
      const created = packageVersion('1.1.0', true)
      versions = [created, ...versions]
      await route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(created) })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ results: versions }),
    })
  })
  await page.route('**/api/v1/wdl-packages/solid-tumor-tools', async (route) => {
    const lifecycle = route.request().method() === 'PATCH'
      ? (route.request().postDataJSON() as { lifecycle: 'active' | 'archived' }).lifecycle
      : 'active'
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        slug: 'solid-tumor-tools',
        name: 'Solid Tumor Tools',
        description: '实体瘤公共 Task',
        lifecycle,
        tags: ['实体瘤'],
        created_by: 'zhuqin',
        created_at: '2026-08-04T02:00:00Z',
        updated_at: '2026-08-04T02:00:00Z',
        version_count: versions.length,
        reference_count: 2,
        references: [{
          asset_slug: 'solid-tumor-single',
          asset_name: 'Solid Tumor Single',
          asset_lifecycle: 'active',
          revision: 2,
          package_version: '1.0.0',
          mount_prefix: '',
          digest: packageVersion().digest,
          created_at: '2026-08-04T03:00:00Z',
        }],
        latest_version: versions[0],
        versions,
        audit_events: [{
          id: 1,
          action: 'publish_version',
          actor: 'zhuqin',
          note: '初始导入',
          version: '1.0.0',
          changes: {},
          created_at: '2026-08-04T02:00:00Z',
        }],
      }),
    })
  })
  await page.route(/\/api\/v1\/wdl-packages(?:\?.*)?$/, route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        results: [{
        slug: 'solid-tumor-tools',
        name: 'Solid Tumor Tools',
        description: '实体瘤公共 Task',
        lifecycle: 'active',
        tags: ['实体瘤'],
        created_by: 'zhuqin',
        created_at: '2026-08-04T02:00:00Z',
        updated_at: '2026-08-04T02:00:00Z',
        version_count: 1,
        reference_count: 2,
        latest_version: packageVersion(),
        }],
      }),
    }))
}

test('places WDL packages directly below tool library and shows the registry table', async ({ page }) => {
  await mockPackageApi(page)
  await page.goto('/wdl-packages')

  const packageNav = page.getByRole('button', { name: '工具包', exact: true })
  await expect(packageNav).toBeVisible()
  await expect(packageNav.locator('xpath=preceding-sibling::*[1]')).toHaveAccessibleName('工具库')
  await expect(page.getByRole('heading', { name: 'WDL 工具包' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Solid Tumor Tools' })).toBeVisible()
  await expect(page.getByText('1 task · 1 文件')).toBeVisible()
  await expect(page.getByText('2', { exact: true })).toBeVisible()
})

test('returns to the originating workflow editor', async ({ page }) => {
  await mockPackageApi(page)
  const toolDigest = `sha256:${'e'.repeat(64)}`
  const documentDigest = `sha256:${'f'.repeat(64)}`
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/tool-package-source', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      workflow: {
        slug: 'fastp_bwa_demo', name: 'fastp → BWA-MEM demo',
        document_version: 1, document_digest: documentDigest,
      },
      files: [{
        path: 'tasks/QC.wdl', content: qcSource, tool_id: 'QC',
        tool_version: '1.0.0', tool_digest: toolDigest,
      }],
      preview_digest: `sha256:${'d'.repeat(64)}`,
      can_publish: true,
      analysis: analysis(),
    }),
  }))
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      slug: 'fastp_bwa_demo', name: 'fastp → BWA-MEM demo', description: '',
      document_version: 1, document_digest: documentDigest,
      workflow_graph: { nodes: [{ id: 'qc', type: 'tool', tool_ref: { digest: toolDigest } }] },
      tool_specs: [{ id: 'QC' }],
    }),
  }))
  await page.goto('/wdl-packages?from=editor&workflow=fastp_bwa_demo&node=qc')

  await page.getByRole('button', { name: '返回编辑器', exact: true }).click()

  await expect(page).toHaveURL(/\/?section=edit&workflow=fastp_bwa_demo$/)
})

test('creates a package from selected tool versions on the current canvas', async ({ page }) => {
  await mockPackageApi(page)
  const qcDigest = `sha256:${'1'.repeat(64)}`
  const alignDigest = `sha256:${'2'.repeat(64)}`
  let sourceRequest: Record<string, any> | undefined
  let createPayload: Record<string, any> | undefined
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo/tool-package-source', async (route) => {
    sourceRequest = route.request().postDataJSON()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        workflow: {
          slug: 'fastp_bwa_demo',
          name: 'fastp → BWA-MEM demo',
          document_version: 7,
          document_digest: `sha256:${'3'.repeat(64)}`,
        },
        files: [
          { path: 'tasks/QC.wdl', content: qcSource, tool_id: 'QC', tool_version: '1.0.0', tool_digest: qcDigest },
          { path: 'tasks/Align.wdl', content: qcSource.replaceAll('QC', 'Align'), tool_id: 'Align', tool_version: '2.1.0', tool_digest: alignDigest },
        ],
        preview_digest: `sha256:${'4'.repeat(64)}`,
        can_publish: true,
        analysis: {
          ...analysis(),
          summary: { ...analysis().summary, task_count: 2 },
          tasks: [
            analysis().tasks[0],
            { ...analysis().tasks[0], id: 'tasks/Align.wdl::Align', name: 'Align', file_path: 'tasks/Align.wdl' },
          ],
        },
      }),
    })
  })
  await page.route('**/api/v1/editor/workflows/fastp_bwa_demo', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      slug: 'fastp_bwa_demo',
      name: 'fastp → BWA-MEM demo',
      description: '',
      document_version: 7,
      document_digest: `sha256:${'3'.repeat(64)}`,
      workflow_graph: {
        nodes: [
          { id: 'qc', type: 'tool', tool_ref: { digest: qcDigest } },
          { id: 'align', type: 'tool', tool_ref: { digest: alignDigest } },
        ],
      },
      tool_specs: [{ id: 'QC' }, { id: 'Align' }],
    }),
  }))
  await page.route(/\/api\/v1\/wdl-packages$/, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    createPayload = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({ slug: 'fastp-bwa-tools' }),
    })
  })

  await page.goto('/wdl-packages?from=editor&workflow=fastp_bwa_demo&node=qc')

  await expect(page.getByRole('button', { name: '当前画布', exact: true })).toHaveAttribute('aria-pressed', 'true')
  const source = page.getByLabel('当前画布工具版本')
  await expect(source).toContainText('1 / 2 个工具版本将进入工具包')
  await expect(source.getByText('QC', { exact: true })).toBeVisible()
  await expect(source.getByText('Align', { exact: true })).toBeVisible()
  await expect(source.getByRole('checkbox').nth(0)).toBeChecked()
  await expect(source.getByRole('checkbox').nth(1)).not.toBeChecked()
  expect(sourceRequest).toEqual({
    base_document_version: 7,
    base_document_digest: `sha256:${'3'.repeat(64)}`,
    tool_digests: [qcDigest, alignDigest],
  })

  await source.getByRole('checkbox').nth(1).check()
  await expect(source).toContainText('2 / 2 个工具版本将进入工具包')
  await source.getByRole('checkbox').nth(1).uncheck()
  await expect(source).toContainText('1 / 2 个工具版本将进入工具包')
  await page.getByRole('button', { name: '分析内容', exact: true }).click()
  await page.getByLabel('名称', { exact: true }).fill('fastp BWA 工具包')
  await page.getByRole('button', { name: '创建固定版本', exact: true }).click()

  await expect.poll(() => createPayload).toBeTruthy()
  expect(createPayload?.files).toEqual([{ path: 'tasks/QC.wdl', content: qcSource }])
  expect(createPayload?.source_repository).toBe('bioworkflow://editor/workflows/fastp_bwa_demo')
  expect(createPayload?.source_revision).toBe('draft-v7')
  await expect(page).toHaveURL(/\/wdl-packages\/fastp-bwa-tools$/)
})

test('opens an exact historical WDL revision as package source', async ({ page }) => {
  await mockPackageApi(page)
  await page.route('**/api/v1/wdl-assets/solid-tumor-hg38', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      slug: 'solid-tumor-hg38',
      name: '实体瘤 WDL',
      description: '',
      source_filename: 'solid-tumor.wdl',
      source_repository: 'https://example.invalid/wdl',
      source_revision: 'main',
      lifecycle: 'active',
      metadata_version: 1,
      tags: ['实体瘤', 'hg38'],
      created_by: 'zhuqin',
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
      revision_count: 1,
      file_count: 1,
      current_revision: {
        version: 1,
        files: [{ path: 'tasks/qc.wdl', content: qcSource, digest: `sha256:${'c'.repeat(64)}` }],
      },
    }),
  }))

  await page.goto('/wdl-packages?from=wdl&asset=solid-tumor-hg38&revision=1')

  await expect(page.getByRole('button', { name: '当前历史 WDL', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await expect(page.getByLabel('历史 WDL 源文件')).toContainText('tasks/qc.wdl')
  await page.getByRole('button', { name: '返回 WDL', exact: true }).click()
  await expect(page).toHaveURL(/\/wdl\/solid-tumor-hg38\?revision=1$/)
})

test('creates a WDL tool package from the built-in task template without a ZIP', async ({ page }) => {
  await mockPackageApi(page)
  let createPayload: Record<string, any> | undefined
  await page.route(/\/api\/v1\/wdl-packages$/, async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    createPayload = route.request().postDataJSON()
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({ slug: 'my-first-tools' }),
    })
  })

  await page.goto('/wdl-packages')
  await page.getByRole('button', { name: '创建工具包', exact: true }).click()

  await expect(page.getByRole('button', { name: '从模板开始' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('button', { name: '选择 ZIP' })).toHaveCount(0)
  await expect(page.getByLabel('名称', { exact: true })).toHaveCount(0)
  await page.getByLabel('WDL 内容').fill(qcSource)
  await page.getByRole('button', { name: '分析内容', exact: true }).click()

  await expect(page.getByText('内容检查通过', { exact: true })).toBeVisible()
  await expect(page.getByLabel('工具包分析预览').getByText('QC', { exact: true })).toBeVisible()
  await page.getByLabel('名称', { exact: true }).fill('我的质控工具')
  await page.getByRole('button', { name: '创建固定版本', exact: true }).click()

  await expect.poll(() => createPayload).toBeTruthy()
  expect(createPayload?.files).toEqual([{ path: 'tasks/example_task.wdl', content: qcSource }])
  expect(createPayload?.name).toBe('我的质控工具')
  expect(createPayload?.confirm_preview).toBe(true)
  expect(createPayload?.preview_digest).toBe(`sha256:${'d'.repeat(64)}`)
  await expect(page).toHaveURL(/\/wdl-packages\/my-first-tools$/)
})

test('opens an immutable package version, inspects tasks, publishes and exports', async ({ page }) => {
  await mockPackageApi(page)
  await page.goto('/wdl-packages/solid-tumor-tools')

  await expect(page.locator('.monaco-editor')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: /QC/ })).toBeVisible()
  await page.getByRole('button', { name: /QC/ }).click()
  await expect(page.getByText('不可变版本 · 只读')).toBeVisible()

  await page.getByRole('tab', { name: /引用/ }).click()
  await expect(page.getByRole('link', { name: /Solid Tumor Single/ })).toBeVisible()

  await page.getByRole('button', { name: '发布新版本' }).click()
  await page.locator('input[type="file"]').setInputFiles({
    name: 'solid-tools-1.1.0.zip',
    mimeType: 'application/zip',
    buffer: Buffer.from('fake zip'),
  })
  await page.locator('.wdl-package-publish').getByRole('button', { name: '分析内容' }).click()
  await expect(page.getByLabel('新版本分析预览')).toContainText('1 task · 1 文件')
  await page.getByRole('textbox', { name: '版本', exact: true }).fill('1.1.0')
  await page.locator('.wdl-package-publish').getByRole('button', { name: '发布版本' }).click()
  await expect(page.getByText('已发布 1.1.0')).toBeVisible()
  await expect(page).toHaveURL(/version=1\.1\.0/)
  await page.reload()
  await expect(page.getByLabel('工具包版本来源')).toContainText('1.1.0')

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 ZIP' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('solid-tumor-tools-1.1.0.zip')
})
