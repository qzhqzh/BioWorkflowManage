<script setup lang="ts">
import type { AnalysisRunEvent } from '~/types/analysis'
import type { BusinessLink, ConsoleRun, RunMetadata } from '~/types/operations'
import { businessRunUrl, formatOutputSize, formatRunDuration, formatRunTime, outputStatusLabels, safeDownloadUrl } from '~/utils/operations'
import OperationsIcon from './OperationsIcon.vue'
import OperationsStatus from './OperationsStatus.vue'
const props = defineProps<{
  run: ConsoleRun; metadata: RunMetadata | null; events: AnalysisRunEvent[]; eventsError: string;
  metadataError: string; eventsLoading: boolean; hasMoreEvents: boolean; links: BusinessLink[]; businessName: string;
}>()
const emit = defineEmits<{
  back: []; refresh: []; copy: [value: string]; cancel: []; retry: []; select: [id: string]; moreEvents: [];
}>()
const tab = ref('overview')
const origin = ref('')
onMounted(() => { origin.value = location.origin })
const businessUrl = computed(() => businessRunUrl(props.run, props.links))
const canCancel = computed(() => ['queued', 'preparing', 'running'].includes(props.run.status))
const canRetry = computed(() => ['failed', 'canceled'].includes(props.run.status))
const outputs = computed(() => props.run.outputs || [])
const recentEvents = computed(() => props.events.slice(-12))
const duration = computed(() => formatRunDuration(props.run.timing?.total_seconds))
const eventText = computed(() => props.events.map(event => `${formatRunTime(event.created_at)} [${event.level}] ${event.message}`).join('\n'))
</script>
<template>
  <div>
    <button type="button" class="ops-text-button ops-back" @click="emit('back')"><OperationsIcon name="back" />返回运行记录</button>
    <header class="ops-detail-heading"><div><div class="ops-title-row"><h1>{{ run.sampleLabel }}</h1><OperationsStatus :status="run.status" /></div><p>{{ run.workflowLabel }} · 契约 {{ run.analysis_product?.contract_version || '未绑定' }}</p></div><div class="ops-heading-actions"><button type="button" class="ops-button ops-neutral" @click="emit('copy', run.id)"><OperationsIcon name="copy" />复制任务编号</button><button v-if="canCancel" type="button" class="ops-button ops-danger" @click="emit('cancel')">取消任务</button><button v-else-if="run.status === 'cancel_requested'" type="button" class="ops-button ops-neutral" disabled>等待取消确认</button><button v-if="canRetry" type="button" class="ops-button ops-primary" @click="emit('retry')">前往业务系统重跑</button></div></header>
    <div v-if="run.retry_of" class="ops-notice"><OperationsIcon name="info" /><span>本次任务基于已有运行创建。</span><button type="button" class="ops-text-button" @click="emit('select', run.retry_of!)">查看原任务</button></div>
    <dl class="ops-metadata"><div><dt>来源业务</dt><dd>{{ businessName }}</dd></div><div><dt>执行引擎</dt><dd>{{ run.engineLabel }}</dd></div><div><dt>提交时间</dt><dd>{{ formatRunTime(run.created_at) }}</dd></div><div><dt>运行耗时</dt><dd>{{ duration }}</dd></div></dl>
    <div class="ops-tabs ops-detail-tabs"><button v-for="item in [{id:'overview',label:'运行概况'},{id:'events',label:'运行事件'},{id:'outputs',label:'输出文件'}]" :key="item.id" type="button" :class="{ active: tab===item.id }" :aria-pressed="tab===item.id" @click="tab=item.id">{{ item.label }}</button></div>
    <div v-if="tab==='overview'" class="ops-detail-grid">
      <section class="ops-panel"><header class="ops-panel-heading"><h2>执行进度</h2><button type="button" class="ops-text-button" @click="emit('refresh')"><OperationsIcon name="refresh" />刷新</button></header>
        <div class="ops-current-step" :class="{ 'ops-current-step--error': run.status==='failed' }"><OperationsIcon :name="run.status==='succeeded'?'check':run.status==='failed'?'alert':'clock'" /><div><strong>{{ run.current_step || '后台尚未报告当前步骤' }}</strong><span>更新于 {{ formatRunTime(run.updated_at) }}</span></div></div>
        <div v-if="run.status==='queued'" class="ops-notice"><OperationsIcon name="clock" /><span>任务已提交，尚未被执行器领取。{{ run.current_step && !['queued','排队中','运行已进入队列。'].includes(run.current_step) ? '具体进度以上方后台信息为准。' : '后台尚未报告具体等待原因。' }}</span></div>
        <div v-if="run.status==='cancel_requested'" class="ops-notice"><OperationsIcon name="clock" /><span>取消请求已提交。执行引擎确认停止前，任务仍可能占用资源。</span></div>
        <div v-if="run.error" class="ops-error-card"><h3>{{ run.error.code }}</h3><pre>{{ run.error.message }}</pre></div>
        <h3 class="ops-section-label">最近运行事件</h3>
        <p v-if="eventsError" class="ops-inline-error" role="alert">{{ eventsError }}</p>
        <ol v-else-if="recentEvents.length" class="ops-event-list"><li v-for="event in recentEvents" :key="event.id" :class="`ops-event--${event.level}`"><i /><div><p>{{ event.message }}</p><time>{{ formatRunTime(event.created_at) }}</time></div></li></ol>
        <p v-else class="ops-muted">{{ eventsLoading ? '正在读取运行事件…' : '后台尚无运行事件。' }}</p>
        <button v-if="events.length" type="button" class="ops-text-button" @click="tab='events'">查看已读取事件（{{ events.length }}） <OperationsIcon name="chevron" /></button>
      </section>
      <aside class="ops-detail-aside"><section class="ops-panel"><h2>固定运行信息</h2><dl class="ops-key-values"><div><dt>任务编号</dt><dd class="ops-mono">{{ run.id }}</dd></div><div><dt>业务执行编号</dt><dd class="ops-mono">{{ run.external_ref.external_run_id }}</dd></div><div><dt>分析产品</dt><dd>{{ run.analysis_product?.analysis_code || '未绑定' }}</dd></div><div><dt>产品契约版本</dt><dd>{{ run.analysis_product?.contract_version || '未绑定' }}</dd></div><div><dt>流程修订</dt><dd>{{ run.workflow.version }}</dd></div><div><dt>排队耗时</dt><dd>{{ formatRunDuration(run.timing?.queue_seconds) }}</dd></div></dl><p v-if="metadataError" class="ops-inline-error">{{ metadataError }}</p></section>
        <section class="ops-panel"><h2>结果状态</h2><dl class="ops-key-values"><div><dt>分析执行</dt><dd><OperationsStatus :status="run.status" /></dd></div><div><dt>输出校验</dt><dd>{{ outputStatusLabels[run.output_status] || run.output_status || '未提供' }}</dd></div><div><dt>输出条目</dt><dd>{{ outputs.length }} 项</dd></div></dl><p class="ops-body-note">业务系统的结果接收、解析和报告状态，请在对应分析记录中查看。</p><a v-if="businessUrl" class="ops-text-button" :href="businessUrl" target="_blank" rel="noopener noreferrer">查看业务分析 <OperationsIcon name="chevron" /></a></section>
      </aside>
    </div>
    <section v-else-if="tab==='events'" class="ops-panel"><header class="ops-panel-heading"><h2>运行事件</h2><button type="button" class="ops-button ops-neutral" :disabled="!events.length" @click="emit('copy', eventText)"><OperationsIcon name="copy" />复制事件</button></header><p v-if="eventsError" class="ops-inline-error" role="alert">{{ eventsError }}</p><pre v-if="eventText" class="ops-event-log">{{ eventText }}</pre><p v-else-if="!eventsError" class="ops-body-note">{{ eventsLoading ? '正在读取…' : '尚无运行事件。' }}</p><button v-if="hasMoreEvents" type="button" class="ops-button ops-neutral" :disabled="eventsLoading" @click="emit('moreEvents')">{{ eventsLoading ? '读取中…' : '继续读取事件' }}</button></section>
    <section v-else class="ops-panel"><header class="ops-panel-heading"><h2>输出文件</h2><span class="ops-muted">{{ outputStatusLabels[run.output_status] || run.output_status }}</span></header><p v-if="run.output_status !== 'complete'" class="ops-body-note">仅展示后台已提供的输出；未通过校验的文件不提供下载。</p><div v-for="output in outputs" :key="output.key" class="ops-output-row"><OperationsIcon name="file" /><div><strong>{{ output.filename || output.name || output.key }}</strong><span>{{ output.semantic_type || output.key }} · {{ formatOutputSize(output.size) }}</span><p v-if="output.reason" class="ops-body-note">{{ output.reason }}</p><pre v-if="output.kind==='value'">{{ JSON.stringify(output.value, null, 2) }}</pre></div><a v-if="safeDownloadUrl(output.download_url, origin)" class="ops-button" :href="safeDownloadUrl(output.download_url, origin)" download><OperationsIcon name="download" />下载</a><span v-else class="ops-muted">{{ output.kind==='directory'?'目录条目':'不可下载' }}</span></div><div v-if="!outputs.length" class="ops-empty"><OperationsIcon name="file" /><strong>暂无可下载输出</strong><span>分析完成并通过输出校验后，结果会显示在这里。</span></div></section>
  </div>
</template>
