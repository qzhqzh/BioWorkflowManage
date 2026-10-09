<script setup lang="ts">
import type { ConsoleRun } from '~/types/operations'
import { formatRunTime, needsAttention } from '~/utils/operations'
import OperationsStatus from './OperationsStatus.vue'
import OperationsIcon from './OperationsIcon.vue'
defineProps<{ runs: ConsoleRun[]; filter: string; businessNames: Record<string, string>; overview?: boolean }>()
const emit = defineEmits<{ select: [id: string]; filter: [value: string]; all: [] }>()
const tabs = [
  { id: 'all', label: '全部' }, { id: 'attention', label: '需要关注' }, { id: 'active', label: '运行中' },
  { id: 'queued', label: '排队中' }, { id: 'failed', label: '失败' }, { id: 'succeeded', label: '已完成' }, { id: 'canceled', label: '已取消' },
]
</script>
<template>
  <section class="ops-panel ops-run-panel">
    <header class="ops-panel-heading"><h2>{{ overview ? '最近运行记录' : '运行记录' }}</h2><button v-if="overview" type="button" class="ops-text-button" @click="emit('all')">查看全部 <OperationsIcon name="chevron" /></button></header>
    <div class="ops-tabs" aria-label="按状态筛选"><button v-for="tab in tabs" :key="tab.id" type="button" :class="{ active: filter === tab.id }" :aria-pressed="filter === tab.id" @click="emit('filter', tab.id)">{{ tab.label }}</button></div>
    <div class="ops-table-scroll" tabindex="0" aria-label="运行记录表格，可横向滚动">
      <table class="ops-table"><thead><tr><th>样本 / 任务编号</th><th>业务流程</th><th>执行状态</th><th>当前进度</th><th>提交时间</th><th class="ops-action-cell">操作</th></tr></thead>
        <tbody><tr v-for="run in runs" :key="run.id">
          <td><strong class="ops-cell-title" :title="run.sampleLabel">{{ run.sampleLabel }}</strong><span class="ops-cell-sub" :title="run.id">{{ run.id.slice(0, 8) }}<span v-if="run.retry_of"> · 重跑</span></span></td>
          <td><span>{{ run.workflowLabel }}</span><span class="ops-cell-sub">{{ businessNames[run.external_ref.client_id] || run.external_ref.client_id }} · {{ run.analysis_product?.contract_version || '未绑定产品契约' }}</span></td>
          <td><OperationsStatus :status="run.status" /></td>
          <td><span class="ops-step-text" :class="{ 'ops-attention-text': needsAttention(run) }">{{ run.current_step || '后台尚未报告当前步骤' }}</span></td>
          <td class="ops-time">{{ formatRunTime(run.created_at) }}</td>
          <td class="ops-action-cell"><button type="button" class="ops-text-button" :aria-label="`查看 ${run.sampleLabel} 详情`" @click="emit('select', run.id)">查看详情</button></td>
        </tr></tbody>
      </table>
      <div v-if="!runs.length" class="ops-empty"><OperationsIcon name="file" /><strong>暂无符合条件的运行记录</strong><span>任务由业务系统提交后会显示在这里。</span></div>
    </div>
    <footer class="ops-table-footer"><span>本次筛选 {{ runs.length }} 条</span><span>按提交时间由近到远排列</span></footer>
  </section>
</template>
