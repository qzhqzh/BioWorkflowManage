import type { AnalysisProduct, BusinessLink, ConsoleRun, IntegrationRun, RunMetadata } from '~/types/operations'

export const runStatusLabels: Record<string, string> = {
  queued: '排队中', preparing: '准备中', running: '运行中', cancel_requested: '取消中',
  succeeded: '已完成', failed: '失败', canceled: '已取消',
}
export const outputStatusLabels: Record<string, string> = {
  pending: '等待输出', complete: '结果完整', incomplete: '结果不完整', unavailable: '结果不可用',
}
export function isActiveRun(status: string) {
  return ['queued', 'preparing', 'running', 'cancel_requested'].includes(status)
}
export function needsAttention(run: IntegrationRun) {
  return run.status === 'failed' || (run.status === 'succeeded' && run.output_status !== 'complete')
}
export function formatRunTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
export function formatRunDuration(seconds?: number) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—'
  const total = Math.max(0, Math.round(seconds))
  if (total < 60) return `${total} 秒`
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${total % 60} 秒`
  return `${Math.floor(total / 3600)} 小时 ${Math.floor((total % 3600) / 60)} 分`
}
export function formatOutputSize(size?: number) {
  if (typeof size !== 'number') return '—'
  if (size < 1024) return `${size} B`
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KiB`
  if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MiB`
  return `${(size / 1024 ** 3).toFixed(1)} GiB`
}
export function normalizeRun(run: IntegrationRun, metadata: RunMetadata | undefined, products: AnalysisProduct[]): ConsoleRun {
  const product = products.find(item => item.analysis_code === run.analysis_product?.analysis_code
    && item.contract_version === run.analysis_product?.contract_version)
  const engine = metadata?.execution_engine
  return {
    ...run,
    sampleLabel: metadata?.sample_id || metadata?.sample_name || run.external_ref.external_run_id,
    workflowLabel: product?.name || metadata?.workflow?.name || run.analysis_product?.analysis_code || run.workflow.slug || '未提供流程名称',
    engineLabel: engine === 'nextflow' ? 'Nextflow' : engine === 'miniwdl' ? 'MiniWDL' : engine || '未提供',
  }
}
export function parseBusinessLinks(value: unknown): BusinessLink[] {
  try {
    const entries = typeof value === 'string' ? JSON.parse(value) : value
    if (!Array.isArray(entries)) return []
    return entries.filter((item): item is BusinessLink => {
      if (!item || typeof item.clientId !== 'string' || typeof item.name !== 'string' || typeof item.analysisUrlTemplate !== 'string') return false
      const url = new URL(item.analysisUrlTemplate.replaceAll('{analysis_id}', 'id').replaceAll('{run_id}', 'id'))
      return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
    })
  } catch { return [] }
}
export function businessRunUrl(run: IntegrationRun | null, links: BusinessLink[]): string {
  if (!run) return ''
  const link = links.find(item => item.clientId === run.external_ref.client_id)
  if (!link) return ''
  if (link.analysisUrlTemplate.includes('{analysis_id}') && !run.external_ref.external_analysis_id) return ''
  return link.analysisUrlTemplate
    .replaceAll('{analysis_id}', encodeURIComponent(run.external_ref.external_analysis_id || ''))
    .replaceAll('{run_id}', encodeURIComponent(run.external_ref.external_run_id))
}
export function safeDownloadUrl(value: string | undefined, origin: string): string {
  if (!value || !origin) return ''
  try {
    const target = new URL(value, origin)
    if (target.origin !== origin || !target.pathname.startsWith('/api/v1/')) return ''
    return target.pathname + target.search
  } catch { return '' }
}
export function apiErrorMessage(error: unknown, fallback: string): string {
  const value = error as { data?: { error?: { message?: string } }; statusCode?: number; response?: { status?: number } }
  const status = value?.statusCode || value?.response?.status
  if (status === 401) return '登录已过期，请重新登录。'
  if (status === 403) return '当前账号无权读取业务分析记录，请联系管理员。'
  return value?.data?.error?.message || fallback
}
