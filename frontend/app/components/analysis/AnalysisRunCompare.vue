<script setup lang="ts">
import type { AnalysisRun } from '~/types/analysis'

const props = defineProps<{
  primary: AnalysisRun
  comparison: AnalysisRun
  loading: boolean
}>()

const root = ref<HTMLElement | null>(null)

defineExpose({
  reveal() {
    root.value?.focus({ preventScroll: true })
    root.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  },
})

defineEmits<{
  close: []
}>()

const statusLabels: Record<string, string> = {
  queued: '排队中',
  preparing: '准备中',
  running: '运行中',
  succeeded: '已完成',
  failed: '失败',
}

function formatDuration(seconds?: number) {
  if (seconds === undefined) return '—'
  if (seconds < 1) return '<1 秒'
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes} 分 ${remainingSeconds} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

function workflowLink(run: AnalysisRun) {
  if (run.workflow.source_type === 'workflow_version') {
    return `/?section=artifacts&workflow=${encodeURIComponent(run.workflow.slug)}&workflowVersion=${run.workflow.revision}`
  }
  return `/wdl/${encodeURIComponent(run.workflow.slug)}?revision=${run.workflow.revision}`
}

function datasetLabel(run: AnalysisRun) {
  const control = run.request.control_dataset_name
  return control ? `${run.request.dataset_name} + ${control}` : run.request.dataset_name
}

function toolLabel(run: AnalysisRun) {
  const tools = run.workflow.graph_summary?.tools ?? []
  if (!tools.length) return '未记录'
  return [...tools]
    .sort((left, right) => left.name.localeCompare(right.name))
    .map(tool => `${tool.name} v${tool.version}`)
    .join('、')
}

const rows = computed(() => {
  const primary = props.primary
  const comparison = props.comparison
  return [
    {
      key: 'workflow',
      label: '流程版本',
      primary: `${primary.workflow.name} · v${primary.workflow.revision}`,
      comparison: `${comparison.workflow.name} · v${comparison.workflow.revision}`,
      link: true,
    },
    {
      key: 'digest',
      label: '流程摘要',
      primary: primary.workflow.digest,
      comparison: comparison.workflow.digest,
      mono: true,
    },
    { key: 'dataset', label: '原始数据', primary: datasetLabel(primary), comparison: datasetLabel(comparison) },
    { key: 'reference', label: '参考版本', primary: primary.request.reference_name || '—', comparison: comparison.request.reference_name || '—' },
    { key: 'panel', label: 'Panel', primary: primary.request.panel_name || '—', comparison: comparison.request.panel_name || '—' },
    { key: 'tools', label: '固定工具版本', primary: toolLabel(primary), comparison: toolLabel(comparison) },
    { key: 'status', label: '运行状态', primary: statusLabels[primary.status], comparison: statusLabels[comparison.status] },
    { key: 'total', label: '流程总耗时', primary: formatDuration(primary.timing?.total_seconds), comparison: formatDuration(comparison.timing?.total_seconds) },
    { key: 'execution', label: 'miniwdl 执行', primary: formatDuration(primary.timing?.execution_seconds), comparison: formatDuration(comparison.timing?.execution_seconds) },
    {
      key: 'cache',
      label: '缓存命中',
      primary: `${primary.timing?.cached_tasks ?? 0} / ${primary.timing?.tasks?.length ?? 0}`,
      comparison: `${comparison.timing?.cached_tasks ?? 0} / ${comparison.timing?.tasks?.length ?? 0}`,
    },
  ].map(row => ({ ...row, changed: row.primary !== row.comparison }))
})
</script>

<template>
  <section
    ref="root"
    class="analysis-run-compare"
    :aria-busy="loading"
    aria-labelledby="analysis-run-compare-title"
    tabindex="-1"
  >
    <header>
      <div>
        <span>固定版本与运行结果</span>
        <h2 id="analysis-run-compare-title">运行对比</h2>
      </div>
      <button class="button button--ghost" type="button" @click="$emit('close')">关闭对比</button>
    </header>

    <div class="analysis-run-compare__table-wrap">
      <table>
        <thead>
          <tr>
            <th scope="col">对比项</th>
            <th scope="col">
              <span>当前运行</span>
              <strong>{{ primary.sample_id }}</strong>
            </th>
            <th scope="col">
              <span>对比运行</span>
              <strong>{{ comparison.sample_id }}</strong>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.key" :class="{ 'is-different': row.changed }">
            <th scope="row">
              {{ row.label }}
              <span v-if="row.changed">不同</span>
            </th>
            <td :class="{ 'is-mono': row.mono }">
              <NuxtLink v-if="row.link" :to="workflowLink(primary)">{{ row.primary }}</NuxtLink>
              <span v-else :title="row.mono ? row.primary : undefined">{{ row.primary }}</span>
            </td>
            <td :class="{ 'is-mono': row.mono }">
              <NuxtLink v-if="row.link" :to="workflowLink(comparison)">{{ row.comparison }}</NuxtLink>
              <span v-else :title="row.mono ? row.comparison : undefined">{{ row.comparison }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
