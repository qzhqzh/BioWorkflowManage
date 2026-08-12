<script setup lang="ts">
import AnalysisRunDetail from '~/components/analysis/AnalysisRunDetail.vue'
import AnalysisRunCompare from '~/components/analysis/AnalysisRunCompare.vue'
import AnalysisRunList from '~/components/analysis/AnalysisRunList.vue'
import AnalysisSetupPanel from '~/components/analysis/AnalysisSetupPanel.vue'
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { AnalysisCatalog, AnalysisRun } from '~/types/analysis'

const { $api } = useNuxtApp()
const { navigateSection } = useAppNavigation()
const route = useRoute()
const catalog = ref<AnalysisCatalog | null>(null)
const runs = ref<AnalysisRun[]>([])
const selectedRun = ref<AnalysisRun | null>(null)
const comparisonRun = ref<AnalysisRun | null>(null)
const comparisonPanel = ref<{ reveal: () => void } | null>(null)
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const submitting = ref(false)
const detailLoading = ref(false)
const comparisonLoading = ref(false)
const detailError = ref('')
const submitError = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined

const activeRun = computed(() => selectedRun.value && ['queued', 'preparing', 'running', 'cancel_requested'].includes(selectedRun.value.status))
const initialWorkflow = computed(() => typeof route.query.workflow === 'string' ? route.query.workflow : undefined)
const initialRevision = computed(() => {
  const value = Number(route.query.revision)
  return Number.isInteger(value) && value > 0 ? value : undefined
})
const initialRunId = computed(() => typeof route.query.run === 'string' ? route.query.run : undefined)
const initialComparisonId = computed(() => typeof route.query.compare === 'string' ? route.query.compare : undefined)
const scopedRuns = computed(() => {
  if (!initialWorkflow.value) return runs.value
  return runs.value.filter(run => (
    run.workflow.slug === initialWorkflow.value
    && (!initialRevision.value || run.workflow.revision === initialRevision.value)
  ))
})
const runScopeLabel = computed(() => {
  if (!initialWorkflow.value) return ''
  const workflow = catalog.value?.workflows.find(item => (
    item.source_slug === initialWorkflow.value
    && (!initialRevision.value || item.revision === initialRevision.value)
  ))
  const name = workflow?.name ?? initialWorkflow.value
  return `${name}${initialRevision.value ? ` · v${initialRevision.value}` : ''}`
})

async function loadCatalog() {
  const query: Record<string, string | number> = {}
  if (initialWorkflow.value) query.workflow = initialWorkflow.value
  if (initialRevision.value) query.revision = initialRevision.value
  catalog.value = Object.keys(query).length
    ? await $api<AnalysisCatalog>('/api/v1/analysis/catalog', { query })
    : await $api<AnalysisCatalog>('/api/v1/analysis/catalog')
}

async function loadRuns() {
  const response = await $api<{ results: AnalysisRun[] }>('/api/v1/analysis-runs')
  runs.value = response.results
}

function mergeRun(run: AnalysisRun) {
  const index = runs.value.findIndex(item => item.id === run.id)
  if (index === -1) runs.value = [run, ...runs.value]
  else runs.value[index] = run
}

async function loadRun(id: string, quiet = false) {
  if (!quiet) detailLoading.value = true
  if (!quiet) detailError.value = ''
  try {
    const run = await $api<AnalysisRun>(`/api/v1/analysis-runs/${encodeURIComponent(id)}`)
    selectedRun.value = run
    mergeRun(run)
    schedulePoll()
  } catch (error: any) {
    if (!quiet) {
      detailError.value = error?.data?.error?.message ?? '运行详情读取失败。'
    }
    throw error
  } finally {
    if (!quiet) detailLoading.value = false
  }
}

async function selectRun(id: string) {
  await loadRun(id)
  if (comparisonRun.value?.id === id) comparisonRun.value = null
  await navigateTo({
    path: '/runs',
    query: {
      ...route.query,
      run: id,
      compare: comparisonRun.value?.id,
    },
  }, { replace: true })
}

async function selectComparison(id: string, reveal = true) {
  if (comparisonRun.value?.id === id) {
    await closeComparison()
    return
  }
  if (selectedRun.value?.id === id) return
  comparisonLoading.value = true
  detailError.value = ''
  try {
    const run = await $api<AnalysisRun>(`/api/v1/analysis-runs/${encodeURIComponent(id)}`)
    comparisonRun.value = run
    mergeRun(run)
    await navigateTo({
      path: '/runs',
      query: { ...route.query, run: selectedRun.value?.id, compare: id },
    }, { replace: true })
    if (reveal) {
      await nextTick()
      comparisonPanel.value?.reveal()
    }
  } catch (error: any) {
    detailError.value = error?.data?.error?.message ?? '对比运行读取失败。'
  } finally {
    comparisonLoading.value = false
  }
}

async function closeComparison() {
  comparisonRun.value = null
  const query = { ...route.query }
  delete query.compare
  await navigateTo({ path: '/runs', query }, { replace: true })
}

async function clearRunScope() {
  comparisonRun.value = null
  const first = runs.value[0]
  if (first) {
    await loadRun(first.id)
    await navigateTo({ path: '/runs', query: { run: first.id } }, { replace: true })
    return
  }
  selectedRun.value = null
  await navigateTo('/runs', { replace: true })
}

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  if (!activeRun.value) return
  pollTimer = setTimeout(async () => {
    if (selectedRun.value) {
      try {
        await loadRun(selectedRun.value.id, true)
      } catch {
        schedulePoll()
      }
    }
  }, 2000)
}

async function submitRun(payload: {
  workflow: string
  dataset: string
  control_dataset?: string
  reference?: string
  panel?: string
  sample_id: string
  sample_name: string
  sample_type: string
  sample_gender: string
}) {
  submitting.value = true
  submitError.value = ''
  try {
    const run = await $api<AnalysisRun>('/api/v1/analysis-runs', {
      method: 'POST',
      body: payload,
    })
    selectedRun.value = run
    mergeRun(run)
    schedulePoll()
    await navigateTo({
      path: '/runs',
      query: { ...route.query, run: run.id },
    }, { replace: true })
  } catch (error: any) {
    submitError.value = error?.data?.error?.message ?? '运行提交失败。'
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  try {
    await Promise.all([loadCatalog(), loadRuns()])
    loadState.value = 'ready'
    if (initialRunId.value) {
      try {
        await loadRun(initialRunId.value)
      } catch {
        if (scopedRuns.value[0]) await selectRun(scopedRuns.value[0].id)
      }
    } else if (scopedRuns.value[0]) {
      await selectRun(scopedRuns.value[0].id)
    }
    if (initialComparisonId.value && initialComparisonId.value !== selectedRun.value?.id) {
      await selectComparison(initialComparisonId.value, false)
    }
  } catch {
    loadState.value = 'error'
  }
})

onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
})
</script>

<template>
  <div class="app-shell app-shell--workspace app-shell--analysis-runs">
    <AppTopbar section="运行中心" current="运行分析">
      <template v-if="activeRun" #status>
        <div class="save-state">
          <span class="status-dot" />
          <span>{{ selectedRun?.current_step }}</span>
        </div>
      </template>
    </AppTopbar>

    <AppRail active="runs" @select="navigateSection" />

    <main class="section-workspace analysis-runs-page">
      <header class="workspace-header">
        <div>
          <h1>运行分析</h1>
          <p>选择原始数据和受管 WDL，提交后持续查看 miniwdl 进度与结果。</p>
        </div>
        <div v-if="catalog" class="analysis-directory-state">
          <span><b>{{ catalog.datasets.length }}</b> 组原始数据</span>
          <span><b>{{ catalog.database.references.filter(item => item.ready).length }}</b> 个参考库就绪</span>
        </div>
      </header>

      <div v-if="loadState === 'loading'" class="analysis-page-state">正在读取运行环境…</div>
      <div v-else-if="loadState === 'error'" class="analysis-page-state analysis-page-state--error">
        <strong>运行环境暂时无法读取</strong>
        <button class="button button--ghost" type="button" @click="reloadNuxtApp()">重试</button>
      </div>
      <div v-else class="analysis-runs-layout">
        <aside class="analysis-runs-sidebar">
          <AnalysisSetupPanel
            :catalog="catalog"
            :busy="submitting"
            :error="submitError"
            :initial-workflow="initialWorkflow"
            :initial-revision="initialRevision"
            @submit="submitRun"
          />
          <AnalysisRunList
            :runs="scopedRuns"
            :selected-id="selectedRun?.id ?? ''"
            :comparison-id="comparisonRun?.id ?? ''"
            :scope-label="runScopeLabel"
            @select="selectRun"
            @compare="selectComparison"
            @clear-scope="clearRunScope"
          />
        </aside>
        <div v-if="detailError" class="analysis-page-state analysis-page-state--error" role="alert">
          <strong>{{ detailError }}</strong>
          <button
            v-if="selectedRun?.id"
            class="button button--ghost"
            type="button"
            @click="loadRun(selectedRun.id)"
          >重试</button>
        </div>
        <div v-else class="analysis-run-content">
          <AnalysisRunCompare
            v-if="selectedRun && comparisonRun"
            ref="comparisonPanel"
            :primary="selectedRun"
            :comparison="comparisonRun"
            :loading="comparisonLoading"
            @close="closeComparison"
          />
          <AnalysisRunDetail :run="selectedRun" :loading="detailLoading" />
        </div>
      </div>

      <footer class="analysis-runs-footer" aria-label="版本信息">
        v1 · 开发版
      </footer>
    </main>
  </div>
</template>
