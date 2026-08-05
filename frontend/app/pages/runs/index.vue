<script setup lang="ts">
import AnalysisRunDetail from '~/components/analysis/AnalysisRunDetail.vue'
import AnalysisRunList from '~/components/analysis/AnalysisRunList.vue'
import AnalysisSetupPanel from '~/components/analysis/AnalysisSetupPanel.vue'
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { AnalysisCatalog, AnalysisRun } from '~/types/analysis'

type WorkspaceSection = 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help'

const { $api } = useNuxtApp()
const catalog = ref<AnalysisCatalog | null>(null)
const runs = ref<AnalysisRun[]>([])
const selectedRun = ref<AnalysisRun | null>(null)
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const submitting = ref(false)
const detailLoading = ref(false)
const submitError = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined

const activeRun = computed(() => selectedRun.value && ['queued', 'preparing', 'running'].includes(selectedRun.value.status))

function navigateSection(section: WorkspaceSection) {
  if (section === 'runs') return
  if (section === 'packages') {
    void navigateTo('/wdl-packages')
    return
  }
  if (section === 'wdl') {
    void navigateTo('/wdl')
    return
  }
  void navigateTo(`/?section=${section}`)
}

async function loadCatalog() {
  catalog.value = await $api<AnalysisCatalog>('/api/v1/analysis/catalog')
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
  try {
    const run = await $api<AnalysisRun>(`/api/v1/analysis-runs/${encodeURIComponent(id)}`)
    selectedRun.value = run
    mergeRun(run)
    schedulePoll()
  } finally {
    if (!quiet) detailLoading.value = false
  }
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
  reference: string
  panel: string
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
    if (runs.value[0]) await loadRun(runs.value[0].id)
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
            @submit="submitRun"
          />
          <AnalysisRunList
            :runs="runs"
            :selected-id="selectedRun?.id ?? ''"
            @select="loadRun"
          />
        </aside>
        <AnalysisRunDetail :run="selectedRun" :loading="detailLoading" />
      </div>

      <footer class="analysis-runs-footer" aria-label="版本信息">
        v1 · 开发版
      </footer>
    </main>
  </div>
</template>
