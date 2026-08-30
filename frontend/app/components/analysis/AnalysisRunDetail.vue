<script setup lang="ts">
import type { AnalysisRun } from '~/types/analysis'

const props = defineProps<{
  run: AnalysisRun | null
  loading: boolean
}>()

const statusLabels: Record<string, string> = {
  queued: '排队中',
  preparing: '准备中',
  running: '运行中',
  cancel_requested: '取消中',
  succeeded: '已完成',
  failed: '失败',
  canceled: '已取消',
}

const events = computed(() => [...(props.run?.events ?? [])].reverse().slice(0, 30))
const timing = computed(() => props.run?.timing)
const graphSummary = computed(() => props.run?.workflow.graph_summary)
const taskTimings = computed(() => timing.value?.tasks ?? [])
const outputIncomplete = computed(() => (
  props.run?.status === 'succeeded' && props.run.output_status === 'incomplete'
))
const runStatusLabel = computed(() => (
  outputIncomplete.value ? '输出不完整' : statusLabels[props.run?.status ?? 'queued']
))
const outputErrorDetails = computed(() => {
  const details = props.run?.error_details
  if (!details || !Object.keys(details).length) return ''
  const describe = (value: unknown) => {
    if (typeof value === 'string') return value
    if (value && typeof value === 'object') {
      const item = value as Record<string, unknown>
      const key = String(item.key ?? item.name ?? '<unknown>')
      return item.reason ? `${key} (${String(item.reason)})` : key
    }
    return String(value)
  }
  const lines: string[] = []
  if (Array.isArray(details.missing) && details.missing.length) {
    lines.push(`缺失：${details.missing.map(describe).join('、')}`)
  }
  if (Array.isArray(details.unverifiable) && details.unverifiable.length) {
    lines.push(`无法验证：${details.unverifiable.map(describe).join('、')}`)
  }
  return lines.join('\n')
})
const timingScale = computed(() => Math.max(
  timing.value?.execution_seconds ?? 0,
  ...taskTimings.value.map(task => task.offset_seconds + task.duration_seconds),
  1,
))
const workflowLink = computed(() => {
  if (!props.run) return ''
  if (props.run.workflow.source_type === 'workflow_version') {
    return `/?section=artifacts&workflow=${encodeURIComponent(props.run.workflow.slug)}&workflowVersion=${props.run.workflow.revision}`
  }
  return `/wdl/${encodeURIComponent(props.run.workflow.slug)}?revision=${props.run.workflow.revision}`
})

const taskStatusLabels = {
  running: '运行中',
  succeeded: '完成',
  failed: '失败',
} as const

function formatTime(value: string | null) {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function displayValue(value: unknown) {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
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

function taskBarStyle(offset: number, duration: number) {
  const left = Math.min(100, (offset / timingScale.value) * 100)
  const proportionalWidth = (duration / timingScale.value) * 100
  const width = Math.min(100 - left, Math.max(0.7, proportionalWidth))
  return { left: `${left}%`, width: `${width}%` }
}
</script>

<template>
  <section class="analysis-run-detail" :aria-busy="loading">
    <div v-if="!run" class="analysis-run-empty">
      <span aria-hidden="true">▶</span>
      <strong>选择配置后开始分析</strong>
      <p>运行状态、miniwdl 事件和结果会显示在这里。</p>
    </div>

    <template v-else>
      <header class="analysis-run-header">
        <div>
          <NuxtLink class="analysis-run-workflow-link" :to="workflowLink">
            {{ run.workflow.name }} · v{{ run.workflow.revision }}
            <span>{{ run.workflow.source_type === 'workflow_version' ? '查看流程版本 →' : '查看历史 WDL →' }}</span>
          </NuxtLink>
          <h2>{{ run.sample_id }}</h2>
          <p>{{ run.request.dataset_name }}<template v-if="run.request.control_dataset_name"> + {{ run.request.control_dataset_name }}</template></p>
        </div>
        <span class="analysis-run-status" :class="`analysis-run-status--${outputIncomplete ? 'incomplete' : run.status}`">
          {{ runStatusLabel }}
        </span>
      </header>

      <div class="analysis-progress-block">
        <div>
          <strong>{{ run.current_step }}</strong>
          <span>{{ run.progress }}%</span>
        </div>
        <progress :value="run.progress" max="100">{{ run.progress }}%</progress>
      </div>

      <dl class="analysis-run-meta">
        <div><dt>参考版本</dt><dd>{{ run.request.reference_name || '—' }}</dd></div>
        <div><dt>Panel</dt><dd>{{ run.request.panel_name || '—' }}</dd></div>
        <div><dt>提交人</dt><dd>{{ run.actor }}</dd></div>
        <div><dt>提交时间</dt><dd>{{ formatTime(run.created_at) }}</dd></div>
        <div><dt>开始时间</dt><dd>{{ formatTime(run.started_at) }}</dd></div>
        <div><dt>完成时间</dt><dd>{{ formatTime(run.finished_at) }}</dd></div>
      </dl>

      <details v-if="graphSummary" class="analysis-run-source-details">
        <summary>
          <span>本次固定的流程结构</span>
          <strong>{{ graphSummary.node_count }} 节点 · {{ graphSummary.edge_count }} 连接</strong>
        </summary>
        <div class="analysis-run-source-details__body">
          <div class="analysis-run-source-details__counts">
            <span>{{ graphSummary.input_count }} 输入</span>
            <span>{{ graphSummary.tool_count }} 工具</span>
            <span v-if="graphSummary.subworkflow_count">{{ graphSummary.subworkflow_count }} 子流程</span>
            <span>{{ graphSummary.output_count }} 输出</span>
          </div>
          <ul v-if="graphSummary.tools.length || graphSummary.subworkflows.length">
            <li v-for="tool in graphSummary.tools" :key="`tool:${tool.id}:${tool.version}`">
              <span>{{ tool.name }}</span><code>工具 v{{ tool.version }}</code>
            </li>
            <li v-for="subworkflow in graphSummary.subworkflows" :key="`subworkflow:${subworkflow.slug}:${subworkflow.version}`">
              <span>{{ subworkflow.name }}</span><code>子流程 v{{ subworkflow.version }}</code>
            </li>
          </ul>
          <code class="analysis-run-source-details__digest">{{ run.workflow.digest }}</code>
        </div>
      </details>

      <div v-if="run.error" class="analysis-run-error" :class="{ 'is-output-incomplete': outputIncomplete }" role="alert">
        <strong>{{ run.status === 'canceled' ? '运行已取消' : outputIncomplete ? '执行完成，但输出不完整' : '运行失败' }}</strong>
        <pre>{{ run.error }}</pre>
        <pre v-if="outputErrorDetails">{{ outputErrorDetails }}</pre>
      </div>

      <section v-if="timing && (run.started_at || taskTimings.length)" class="analysis-result-section analysis-timing-section">
        <header>
          <h3>耗时</h3>
          <span>{{ taskTimings.length }} 个 task</span>
        </header>
        <dl class="analysis-timing-summary">
          <div><dt>流程总耗时</dt><dd>{{ formatDuration(timing.total_seconds) }}</dd></div>
          <div><dt>miniwdl 执行</dt><dd>{{ formatDuration(timing.execution_seconds) }}</dd></div>
          <div><dt>排队等待</dt><dd>{{ formatDuration(timing.queue_seconds) }}</dd></div>
          <div><dt>缓存命中</dt><dd>{{ timing.cached_tasks ?? 0 }} / {{ taskTimings.length }}</dd></div>
        </dl>
        <div v-if="taskTimings.length" class="analysis-timing-chart">
          <div class="analysis-timing-scale" aria-hidden="true">
            <span>0</span>
            <span>{{ formatDuration(timingScale / 2) }}</span>
            <span>{{ formatDuration(timingScale) }}</span>
          </div>
          <ol>
            <li v-for="task in taskTimings" :key="task.id">
              <div class="analysis-timing-task">
                <strong>{{ task.name }}</strong>
                <span>
                  {{ taskStatusLabels[task.status] }}<template v-if="task.cached"> · 缓存</template>
                </span>
              </div>
              <div
                class="analysis-timing-track"
                :aria-label="`${task.name}，${formatDuration(task.duration_seconds)}`"
              >
                <i
                  :class="[`is-${task.status}`, { 'is-cached': task.cached }]"
                  :style="taskBarStyle(task.offset_seconds, task.duration_seconds)"
                />
              </div>
              <time>{{ formatDuration(task.duration_seconds) }}</time>
            </li>
          </ol>
          <footer>
            <span>Task 累计</span>
            <strong>{{ formatDuration(timing.task_seconds) }}</strong>
          </footer>
        </div>
        <p v-else class="analysis-section-empty">任务启动后显示各步骤耗时。</p>
      </section>

      <section class="analysis-result-section">
        <header>
          <h3>结果</h3>
          <span>{{ run.outputs.length }} 项</span>
        </header>
        <div v-if="run.outputs.length" class="analysis-output-list">
          <div v-for="output in run.outputs" :key="output.key" class="analysis-output-row">
            <div>
              <strong>{{ output.name || output.key }}</strong>
              <code>{{ output.key }}</code>
            </div>
            <span v-if="output.kind === 'file'">{{ output.size_label || '不可下载' }}</span>
            <span v-else-if="output.kind === 'directory'">{{ output.entry_count ?? 0 }} 项</span>
            <a
              v-if="output.kind === 'file' && output.download_url"
              class="button button--ghost button-link"
              :href="output.download_url"
              download
            >下载</a>
            <pre v-else-if="output.kind === 'value'">{{ displayValue(output.value) }}</pre>
            <pre v-else>{{ output.reason || '该输出无法验证。' }}</pre>
          </div>
        </div>
        <p v-else class="analysis-section-empty">
          {{ run.status === 'succeeded' ? '流程没有返回可展示的输出。' : '结果会在流程完成后出现。' }}
        </p>
      </section>

      <section class="analysis-result-section analysis-event-section">
        <header>
          <h3>运行事件</h3>
          <span>{{ run.events?.length ?? 0 }} 条</span>
        </header>
        <ol v-if="events.length" class="analysis-event-list">
          <li v-for="event in events" :key="event.id" :class="`is-${event.level}`">
            <i aria-hidden="true" />
            <div>
              <p>{{ event.message }}</p>
              <time :datetime="event.created_at">{{ formatTime(event.created_at) }}</time>
            </div>
          </li>
        </ol>
        <p v-else class="analysis-section-empty">还没有运行事件。</p>
      </section>
    </template>

    <div v-if="loading" class="analysis-detail-loading">正在刷新…</div>
  </section>
</template>
