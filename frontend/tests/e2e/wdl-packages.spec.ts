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
    body: JSON.stringify({ user: { username: 'zhuqin' } }),
  }))
  await page.route('**/api/v1/wdl-packages/tags', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ results: [{ id: 1, name: '实体瘤', package_count: 1 }] }),
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

  const packageNav = page.getByRole('button', { name: 'WDL 工具包', exact: true })
  await expect(packageNav).toBeVisible()
  await expect(packageNav.locator('xpath=preceding-sibling::*[1]')).toHaveAccessibleName('工具库')
  await expect(page.getByRole('heading', { name: 'WDL 工具包' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Solid Tumor Tools' })).toBeVisible()
  await expect(page.getByText('1 task · 1 文件')).toBeVisible()
  await expect(page.getByText('2', { exact: true })).toBeVisible()
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
  await page.getByRole('textbox', { name: '版本', exact: true }).fill('1.1.0')
  await page.locator('.wdl-package-publish').getByRole('button', { name: '发布版本' }).click()
  await expect(page.getByText('已发布 1.1.0')).toBeVisible()

  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出 ZIP' }).click()
  const download = await downloadPromise
  expect(download.suggestedFilename()).toBe('solid-tumor-tools-1.1.0.zip')
})
