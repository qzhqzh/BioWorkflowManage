<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { AnalysisCatalog, AnalysisRun } from '~/types/analysis'
import type { WdlAsset, WdlToolPackage } from '~/types/wdl'

interface WorkflowEntry {
  slug: string
  name: string
  description?: string
  kind: 'workflow' | 'subworkflow'
  latest_version: number | null
  updated_at: string
  created_by?: string
  is_mine?: boolean
}

interface ToolEntry {
  tool_id: string
  name?: string | null
  latest_version?: string | null
  version_count: number
  draft_status?: string | null
}

const { $api } = useNuxtApp()
const { navigateSection } = useAppNavigation()

const workflows = ref<WorkflowEntry[]>([])
const tools = ref<ToolEntry[]>([])
const packages = ref<WdlToolPackage[]>([])
const wdlAssets = ref<WdlAsset[]>([])
const runs = ref<AnalysisRun[]>([])
const catalog = ref<AnalysisCatalog>()
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const resourceState = ref<'loading' | 'ready' | 'error'>('loading')
const loadFailures = ref(0)

const myWorkflows = computed(() => workflows.value.filter(item => item.is_mine))
const mySubworkflows = computed(() => myWorkflows.value.filter(item => item.kind === 'subworkflow'))
const draftSubworkflows = computed(() => mySubworkflows.value.filter(item => !item.latest_version))
const publishedSubworkflows = computed(() => mySubworkflows.value.filter(item => item.latest_version))
const draftWorkflows = computed(() => myWorkflows.value.filter(item => item.kind === 'workflow' && !item.latest_version))
const publishedTools = computed(() => tools.value.filter(item => item.latest_version))
const myPackages = computed(() => packages.value.filter(item => item.is_mine && item.lifecycle === 'active'))
const activeRuns = computed(() => runs.value.filter(item => ['queued', 'preparing', 'running'].includes(item.status)))
const failedRuns = computed(() => runs.value.filter(item => item.status === 'failed'))
const readyRunWorkflows = computed(() => catalog.value?.workflows.filter(item => item.ready) ?? [])
const blockedRunWorkflows = computed(() => catalog.value?.workflows.filter(item => !item.ready) ?? [])
const readyReferences = computed(() => catalog.value?.database.references.filter(item => item.ready) ?? [])
const recentWorkflows = computed(() => myWorkflows.value
  .toSorted((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
  .slice(0, 5))
const recentRuns = computed(() => runs.value.slice(0, 4))
const latestCanvas = computed(() => recentWorkflows.value[0])

function formatTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

function runStatusLabel(status: AnalysisRun['status']) {
  return {
    queued: '排队中',
    preparing: '准备中',
    running: '运行中',
    succeeded: '已完成',
    failed: '失败',
  }[status]
}

async function loadOverview() {
  loadState.value = 'loading'
  resourceState.value = 'loading'
  const results = await Promise.allSettled([
    $api<{ results: WorkflowEntry[] }>('/api/v1/editor/workflows'),
    $api<{ results: ToolEntry[] }>('/api/v1/tools'),
    $api<{ results: WdlToolPackage[] }>('/api/v1/wdl-packages', { query: { lifecycle: 'active' } }),
    $api<{ results: WdlAsset[] }>('/api/v1/wdl-assets'),
    $api<{ results: AnalysisRun[] }>('/api/v1/analysis-runs'),
  ])

  if (results[0].status === 'fulfilled') workflows.value = results[0].value.results
  if (results[1].status === 'fulfilled') tools.value = results[1].value.results
  if (results[2].status === 'fulfilled') packages.value = results[2].value.results
  if (results[3].status === 'fulfilled') wdlAssets.value = results[3].value.results
  if (results[4].status === 'fulfilled') runs.value = results[4].value.results

  loadFailures.value = results.filter(result => result.status === 'rejected').length
  loadState.value = loadFailures.value === results.length ? 'error' : 'ready'
  void loadRunResources()
}

async function loadRunResources() {
  try {
    catalog.value = await $api<AnalysisCatalog>('/api/v1/analysis/catalog')
    resourceState.value = 'ready'
  } catch {
    resourceState.value = 'error'
  }
}

onMounted(() => {
  void loadOverview()
})
</script>

<template>
  <div class="app-shell app-shell--workspace app-shell--overview">
    <AppTopbar section="总览" current="产品工作台">
      <template #status>
        <span v-if="loadState === 'ready'" class="save-state">
          <span class="status-dot" />
          {{ activeRuns.length ? `${activeRuns.length} 个任务运行中` : '运行队列空闲' }}
        </span>
      </template>
    </AppTopbar>

    <AppRail active="overview" @select="navigateSection" />

    <main id="product-overview" class="section-workspace product-overview">
      <header class="workspace-header product-overview__header">
        <div>
          <h1>从画布继续</h1>
          <p>先沉淀可复用子流程，再组装并发布主流程；历史 WDL 只在迁移时使用。</p>
        </div>
        <div class="workspace-header__actions">
          <NuxtLink
            class="button button--ghost button--link"
            to="/?section=artifacts&owner=mine&kind=subworkflow"
          >我的子流程</NuxtLink>
          <NuxtLink
            class="button button--primary button--link"
            to="/?section=artifacts&owner=mine&kind=subworkflow&create=subworkflow"
          >新建子流程</NuxtLink>
        </div>
      </header>

      <div v-if="loadState === 'loading'" class="overview-state" aria-live="polite">正在整理你的资产与运行状态…</div>
      <div v-else-if="loadState === 'error'" class="overview-state overview-state--error" role="alert">
        <strong>总览暂时无法读取</strong>
        <button class="button button--ghost" type="button" @click="loadOverview">重试</button>
      </div>
      <div v-else class="product-overview__body">
        <p v-if="loadFailures" class="overview-partial" role="status">
          {{ loadFailures }} 项数据暂未读取，其他入口仍可使用。
        </p>

        <section class="overview-continuation" aria-labelledby="continue-heading">
          <header>
            <div>
              <h2 id="continue-heading">继续工作</h2>
              <p>最近维护的流程与子流程。</p>
            </div>
            <NuxtLink class="text-button text-button--link" to="/?section=artifacts&owner=mine">查看全部</NuxtLink>
          </header>
          <ul v-if="recentWorkflows.length" class="overview-work-list">
            <li v-for="workflow in recentWorkflows" :key="workflow.slug">
              <NuxtLink :to="`/?section=edit&workflow=${encodeURIComponent(workflow.slug)}`">
                <span>
                  <strong>{{ workflow.name }}</strong>
                  <small>{{ workflow.description || workflow.slug }}</small>
                </span>
                <span class="overview-work-list__meta">
                  <em>{{ workflow.kind === 'subworkflow' ? '子流程' : '流程' }}</em>
                  <small>{{ workflow.latest_version ? `已发布 v${workflow.latest_version}` : '草稿' }} · {{ formatTime(workflow.updated_at) }}</small>
                </span>
              </NuxtLink>
            </li>
          </ul>
          <div v-else class="overview-empty">
            <strong>还没有自己的流程</strong>
            <span>创建一个子流程，直接进入画布定义输入、工具和输出。</span>
            <NuxtLink class="button button--primary button--link" to="/?section=artifacts&create=subworkflow">新建子流程</NuxtLink>
          </div>
        </section>

        <section class="overview-lifecycle" aria-labelledby="lifecycle-heading">
          <header>
            <div>
              <h2 id="lifecycle-heading">从可复用节点到运行</h2>
              <p>每一步都保留固定版本和来源。</p>
            </div>
          </header>
          <ol>
            <li class="overview-lifecycle__step overview-lifecycle__step--primary">
              <span class="overview-lifecycle__index">1</span>
              <div>
                <strong>子流程</strong>
                <p>{{ mySubworkflows.length }} 个属于你 · {{ publishedSubworkflows.length }} 个已发布 · {{ draftSubworkflows.length }} 个待完善</p>
              </div>
              <div class="overview-lifecycle__actions">
                <NuxtLink class="text-button text-button--link" to="/?section=artifacts&owner=mine&kind=subworkflow">管理</NuxtLink>
                <NuxtLink class="button button--ghost button--link" to="/?section=artifacts&owner=mine&kind=subworkflow&create=subworkflow">新建</NuxtLink>
              </div>
            </li>
            <li class="overview-lifecycle__step">
              <span class="overview-lifecycle__index">2</span>
              <div>
                <strong>工具与工具包</strong>
                <p>{{ publishedTools.length }} 个已发布工具版本可用于画布 · {{ myPackages.length }} 个我的工具包</p>
              </div>
              <div class="overview-lifecycle__actions">
                <NuxtLink class="text-button text-button--link" to="/?section=tools">查看工具版本</NuxtLink>
                <NuxtLink
                  class="button button--ghost button--link"
                  :to="latestCanvas ? `/wdl-packages?from=editor&workflow=${encodeURIComponent(latestCanvas.slug)}` : '/wdl-packages?create=1'"
                >从画布创建工具包</NuxtLink>
              </div>
            </li>
            <li class="overview-lifecycle__step">
              <span class="overview-lifecycle__index">3</span>
              <div>
                <strong>主流程</strong>
                <p>{{ myWorkflows.length - mySubworkflows.length }} 个属于你 · {{ draftWorkflows.length }} 个草稿待发布</p>
              </div>
              <div class="overview-lifecycle__actions">
                <NuxtLink class="text-button text-button--link" to="/?section=artifacts&owner=mine&kind=workflow">流程库</NuxtLink>
                <NuxtLink class="button button--ghost button--link" to="/?section=artifacts&owner=mine&kind=workflow&create=workflow">新建流程</NuxtLink>
              </div>
            </li>
            <li class="overview-lifecycle__step">
              <span class="overview-lifecycle__index">4</span>
              <div>
                <strong>运行验证</strong>
                <p v-if="resourceState === 'loading'">正在检查流程与数据库资源…</p>
                <p v-else-if="resourceState === 'error'">资源状态暂不可用；可以进入运行分析重新检查。</p>
                <p v-else>{{ readyRunWorkflows.length }} 个流程可投递 · {{ readyReferences.length }} 个参考库就绪<span v-if="blockedRunWorkflows.length"> · {{ blockedRunWorkflows.length }} 个流程被资源阻断</span></p>
              </div>
              <div class="overview-lifecycle__actions">
                <NuxtLink class="button button--ghost button--link" to="/runs">进入运行分析</NuxtLink>
              </div>
            </li>
          </ol>
        </section>

        <section class="overview-run-state" aria-labelledby="run-state-heading">
          <header>
            <div>
              <h2 id="run-state-heading">最近运行</h2>
              <p>{{ activeRuns.length }} 个进行中 · {{ failedRuns.length }} 个失败记录</p>
            </div>
            <NuxtLink class="text-button text-button--link" to="/runs">查看与对比</NuxtLink>
          </header>
          <ul v-if="recentRuns.length" class="overview-run-list">
            <li v-for="run in recentRuns" :key="run.id">
              <NuxtLink :to="`/runs?run=${encodeURIComponent(run.id)}`">
                <span><strong>{{ run.sample_name || run.sample_id }}</strong><small>{{ run.workflow.name }} · v{{ run.workflow.revision }}</small></span>
                <em :class="`is-${run.status}`">{{ runStatusLabel(run.status) }}</em>
              </NuxtLink>
            </li>
          </ul>
          <div v-else class="overview-empty overview-empty--compact">
            <span>还没有运行记录。</span>
            <NuxtLink class="text-button text-button--link" to="/runs">提交小数据验证</NuxtLink>
          </div>
        </section>

        <section class="overview-legacy" aria-label="历史 WDL 迁移入口">
          <div>
            <strong>历史 WDL 迁移</strong>
            <span>{{ wdlAssets.length }} 个历史资产；用于拆解旧流程、整理工具包与审计差异。</span>
          </div>
          <NuxtLink class="text-button text-button--link" to="/wdl">打开历史 WDL</NuxtLink>
        </section>
      </div>
    </main>
  </div>
</template>
