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
  succeeded: '已完成',
  failed: '失败',
}

const events = computed(() => [...(props.run?.events ?? [])].reverse().slice(0, 30))

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
          <span>{{ run.workflow.name }} · v{{ run.workflow.revision }}</span>
          <h2>{{ run.sample_id }}</h2>
          <p>{{ run.request.dataset_name }}<template v-if="run.request.control_dataset_name"> + {{ run.request.control_dataset_name }}</template></p>
        </div>
        <span class="analysis-run-status" :class="`analysis-run-status--${run.status}`">
          {{ statusLabels[run.status] }}
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
        <div><dt>参考版本</dt><dd>{{ run.request.reference_name }}</dd></div>
        <div><dt>Panel</dt><dd>{{ run.request.panel_name }}</dd></div>
        <div><dt>提交人</dt><dd>{{ run.actor }}</dd></div>
        <div><dt>提交时间</dt><dd>{{ formatTime(run.created_at) }}</dd></div>
        <div><dt>开始时间</dt><dd>{{ formatTime(run.started_at) }}</dd></div>
        <div><dt>完成时间</dt><dd>{{ formatTime(run.finished_at) }}</dd></div>
      </dl>

      <div v-if="run.error" class="analysis-run-error" role="alert">
        <strong>运行失败</strong>
        <pre>{{ run.error }}</pre>
      </div>

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
            <span v-if="output.kind === 'file'">{{ output.size_label }}</span>
            <a
              v-if="output.kind === 'file' && output.download_url"
              class="button button--ghost button-link"
              :href="output.download_url"
              download
            >下载</a>
            <pre v-else>{{ displayValue(output.value) }}</pre>
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
