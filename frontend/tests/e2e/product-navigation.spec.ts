import { expect, test } from '@playwright/test'

test.describe.configure({ mode: 'serial' })

test.beforeEach(async ({ page }) => {
  const authProbe = page.waitForResponse(response => response.url().endsWith('/api/v1/auth/me'))
  await page.goto('/login')
  await authProbe
  await page.getByLabel('用户名').fill('zhuqin')
  await page.getByLabel('密码').fill('zhuqin')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL('**/overview')
})

test('管理员从总览进入子流程创建并返回总览', async ({ page }) => {
  await expect(page.getByRole('heading', { name: /从画布继续/, level: 1 })).toBeVisible()
  await expect(page.getByRole('heading', { name: '从可复用节点到运行', level: 2 })).toBeVisible()

  const navigationLabels = (await page.getByLabel('主要导航').locator('.rail__item').allTextContents())
    .map(label => label.replace(/\s+/g, ''))
  expect(navigationLabels.slice(0, 9)).toEqual([
    'O总览',
    '⌘编辑器',
    '{}流程库',
    'T工具库',
    'P工具包',
    'R资源库',
    'D原始数据',
    '▶运行分析',
    'W历史WDL',
  ])

  await page.getByRole('link', { name: '新建子流程', exact: true }).first().click()
  await expect(page).toHaveURL(/section=artifacts/)
  await expect(page).toHaveURL(/owner=mine/)
  await expect(page).toHaveURL(/kind=subworkflow/)
  await expect(page.locator('.workflow-create-panel')).toBeVisible()
  await expect(page.locator('.workflow-create-panel').getByText('新建子流程', { exact: true })).toBeVisible()

  await page.getByLabel('主要导航').getByRole('button', { name: '总览', exact: true }).click()
  await expect(page).toHaveURL(/\/overview$/)
  await expect(page.getByRole('heading', { name: /从画布继续/, level: 1 })).toBeVisible()
})

test('我的子流程入口保留归属与类型筛选', async ({ page }) => {
  await page.getByRole('link', { name: '我的子流程', exact: true }).first().click()
  await expect(page).toHaveURL(/section=artifacts/)
  await expect(page.locator('.workflow-owner-filter').getByRole('button', { name: '我的', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByLabel('流程类型')).toHaveValue('subworkflow')
  await expect(page.getByRole('heading', { name: /我的子流程/ }).first()).toBeVisible()
  await expect(page.locator('.version-sidebar__title strong')).toHaveText('fastp QC reusable subflow')
  await expect(page.locator('.version-sidebar__title > span')).toHaveText('子流程')
  await expect(page).toHaveURL(/owner=mine/)
  await expect(page).toHaveURL(/kind=subworkflow/)
  await expect(page.getByLabel('流程画布结构摘要').getByRole('link', { name: '创建工具包' })).toHaveAttribute(
    'href',
    '/wdl-packages?from=editor&workflow=fastp_qc_subflow',
  )
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.version-sidebar__title strong')).toHaveText('fastp QC reusable subflow')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390)
})
