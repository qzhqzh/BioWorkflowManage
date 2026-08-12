<script setup lang="ts">
import type { AnalysisRun } from '~/types/analysis'

defineProps<{
  runs: AnalysisRun[]
  selectedId: string
  comparisonId: string
  scopeLabel?: string
}>()

const emit = defineEmits<{
  select: [id: string]
  compare: [id: string]
  clearScope: []
}>()

const statusLabels: Record<string, string> = {
  queued: '排队',
  preparing: '准备',
  running: '运行中',
  cancel_requested: '取消中',
  succeeded: '完成',
  failed: '失败',
  canceled: '已取消',
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
        <span>{{ scopeLabel ? '当前流程版本' : '最近运行' }}</span>
        <h2>{{ scopeLabel || '记录' }}</h2>
      </div>
      <div class="analysis-run-list__header-actions">
        <button v-if="scopeLabel" type="button" class="text-button" @click="emit('clearScope')">查看全部</button>
        <span class="analysis-source-count">{{ runs.length }}</span>
      </div>
    </header>
    <div v-if="runs.length" class="analysis-run-list__items">
      <article
        v-for="run in runs"
        :key="run.id"
        :class="{ 'is-selected': selectedId === run.id }"
      >
        <button
          class="analysis-run-list__select"
          type="button"
          :aria-current="selectedId === run.id ? 'true' : undefined"
          @click="emit('select', run.id)"
        >
          <span>
            <strong>{{ run.sample_id }}</strong>
            <small>
              {{ run.workflow.name }} · v{{ run.workflow.revision }} ·
              {{ run.workflow.source_type === 'workflow_version' ? '发布版' : '历史 WDL' }} ·
              {{ formatTime(run.created_at) }}
            </small>
          </span>
          <i :class="`is-${run.status}`">{{ statusLabels[run.status] }}</i>
        </button>
        <button
          class="analysis-run-list__compare"
          type="button"
          :disabled="selectedId === run.id"
          :aria-pressed="comparisonId === run.id"
          :aria-label="comparisonId === run.id ? `移除 ${run.sample_id} 的对比` : `对比 ${run.sample_id}`"
          @click="emit('compare', run.id)"
        >
          {{ comparisonId === run.id ? '已对比' : selectedId === run.id ? '当前' : '对比' }}
        </button>
      </article>
    </div>
    <p v-else class="analysis-section-empty">
      {{ scopeLabel ? '这个流程版本还没有运行记录。' : '还没有运行记录。' }}
    </p>
  </section>
</template>
