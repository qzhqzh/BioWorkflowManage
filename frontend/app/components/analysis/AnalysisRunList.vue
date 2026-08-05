<script setup lang="ts">
import type { AnalysisRun } from '~/types/analysis'

defineProps<{
  runs: AnalysisRun[]
  selectedId: string
}>()

const emit = defineEmits<{
  select: [id: string]
}>()

const statusLabels: Record<string, string> = {
  queued: '排队',
  preparing: '准备',
  running: '运行中',
  succeeded: '完成',
  failed: '失败',
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value))
}
</script>

<template>
  <section class="analysis-run-list">
    <header class="analysis-panel-header">
      <div>
        <span>最近运行</span>
        <h2>记录</h2>
      </div>
      <span class="analysis-source-count">{{ runs.length }}</span>
    </header>
    <div v-if="runs.length" class="analysis-run-list__items">
      <button
        v-for="run in runs"
        :key="run.id"
        type="button"
        :class="{ 'is-selected': selectedId === run.id }"
        @click="emit('select', run.id)"
      >
        <span>
          <strong>{{ run.sample_id }}</strong>
          <small>{{ run.workflow.name }} · {{ formatTime(run.created_at) }}</small>
        </span>
        <i :class="`is-${run.status}`">{{ statusLabels[run.status] }}</i>
      </button>
    </div>
    <p v-else class="analysis-section-empty">还没有运行记录。</p>
  </section>
</template>
