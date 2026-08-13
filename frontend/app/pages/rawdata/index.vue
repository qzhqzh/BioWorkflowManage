<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type {
  RawdataCatalog,
  RawdataDataset,
  RawdataFile,
} from '~/types/rawdata'

const { $api } = useNuxtApp()
const { navigateSection } = useAppNavigation()

type StatusFilter = 'all' | 'ready' | 'issue' | 'unrecognized'
type ListItem =
  | { key: string; kind: 'dataset'; dataset: RawdataDataset }
  | { key: string; kind: 'unrecognized'; file: RawdataFile }

const catalog = ref<RawdataCatalog>()
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const scanState = ref<'idle' | 'queueing' | 'queued' | 'error'>('idle')
const errorMessage = ref('')
const search = ref('')
const selectedDirectory = ref('all')
const statusFilter = ref<StatusFilter>('all')
const selectedKey = ref('')

const allItems = computed<ListItem[]>(() => [
  ...(catalog.value?.datasets ?? []).map(dataset => ({
    key: `dataset:${dataset.id}`,
    kind: 'dataset' as const,
    dataset,
  })),
  ...(catalog.value?.unrecognized_files ?? []).map(file => ({
    key: `file:${file.relative_path}`,
    kind: 'unrecognized' as const,
    file,
  })),
])
const visibleItems = computed(() => {
  const query = search.value.trim().toLocaleLowerCase('zh-CN')
  return allItems.value.filter((item) => {
    const directory = item.kind === 'dataset'
      ? item.dataset.directory
      : directoryOf(item.file.relative_path)
    if (selectedDirectory.value !== 'all' && directory !== selectedDirectory.value) return false
    if (statusFilter.value === 'unrecognized' && item.kind !== 'unrecognized') return false
    if (statusFilter.value !== 'all' && statusFilter.value !== 'unrecognized') {
      if (item.kind !== 'dataset' || item.dataset.status !== statusFilter.value) return false
    }
    if (!query) return true
    const searchable = item.kind === 'dataset'
      ? `${item.dataset.name} ${item.dataset.pair_key} ${item.dataset.directory}`
      : `${item.file.name} ${item.file.relative_path}`
    return searchable.toLocaleLowerCase('zh-CN').includes(query)
  })
})
const selectedItem = computed(() => (
  allItems.value.find(item => item.key === selectedKey.value)
))
const selectedDataset = computed(() => (
  selectedItem.value?.kind === 'dataset' ? selectedItem.value.dataset : undefined
))
const selectedFile = computed(() => (
  selectedItem.value?.kind === 'unrecognized' ? selectedItem.value.file : undefined
))
const canRunSelected = computed(() => selectedDataset.value?.status === 'ready')

function directoryOf(relativePath: string) {
  const index = relativePath.lastIndexOf('/')
  return index === -1 ? '根目录' : relativePath.slice(0, index)
}

function ensureSelection() {
  if (!visibleItems.value.some(item => item.key === selectedKey.value)) {
    selectedKey.value = visibleItems.value[0]?.key ?? ''
  }
}

function selectDirectory(path: string) {
  selectedDirectory.value = path
  ensureSelection()
}

function changeStatus(status: StatusFilter) {
  statusFilter.value = status
  ensureSelection()
}

function selectItem(item: ListItem) {
  selectedKey.value = item.key
}

function itemName(item: ListItem) {
  return item.kind === 'dataset' ? item.dataset.name : item.file.name
}

function itemDirectory(item: ListItem) {
  return item.kind === 'dataset' ? item.dataset.directory : directoryOf(item.file.relative_path)
}

function itemSize(item: ListItem) {
  return item.kind === 'dataset' ? item.dataset.total_size_label : item.file.size_label
}

function statusLabel(item: ListItem) {
  if (item.kind === 'unrecognized') return '无法识别'
  if (item.dataset.status === 'ready') return '可运行'
  if (item.dataset.status === 'scan_incomplete') return '待完成扫描'
  if (item.dataset.missing_mates.length) {
    return `缺 ${item.dataset.missing_mates.map(mate => `R${mate}`).join('、')}`
  }
  return '需处理'
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

let catalogPollTimer: ReturnType<typeof setTimeout> | undefined

function scheduleCatalogPoll() {
  if (catalogPollTimer) window.clearTimeout(catalogPollTimer)
  if (!catalog.value?.index.active_status) return
  catalogPollTimer = window.setTimeout(() => void loadCatalog(true), 2000)
}

async function loadCatalog(silent = false) {
  if (!silent) loadState.value = 'loading'
  errorMessage.value = ''
  try {
    catalog.value = await $api<RawdataCatalog>('/api/v1/rawdata/catalog')
    loadState.value = 'ready'
    if (!catalog.value.index.active_status) scanState.value = 'idle'
    ensureSelection()
    scheduleCatalogPoll()
  }
  catch (error: any) {
    loadState.value = 'error'
    errorMessage.value = error?.data?.error?.message ?? '原始数据目录读取失败。'
  }
}

async function requestScan() {
  if (scanState.value === 'queueing' || catalog.value?.index.active_status) return
  scanState.value = 'queueing'
  errorMessage.value = ''
  try {
    await $api('/api/v1/rawdata/scans', { method: 'POST' })
    scanState.value = 'queued'
    await loadCatalog(true)
  }
  catch (error: any) {
    scanState.value = 'error'
    errorMessage.value = error?.data?.error?.message ?? '后台扫描排队失败。'
  }
}

watch([search, selectedDirectory, statusFilter], ensureSelection)
onMounted(() => void loadCatalog())
onBeforeUnmount(() => {
  if (catalogPollTimer) window.clearTimeout(catalogPollTimer)
})
</script>

<template>
  <div class="app-shell app-shell--workspace app-shell--rawdata">
    <!--
    THESIS: 原始数据中心是投递分析前的只读质检台，先暴露配对与文件问题，再提供运行入口。
    OWN-WORLD: 沿用白色工作面、淡青选区、细分隔线和深青操作色，状态始终包含明确文字。
    STORY: 用户先按目录缩小范围，再筛选可运行或异常数据，最后核对文件并带入运行分析。
    FIRST VIEWPORT: 左侧目录、中部数据清单、右侧质量详情；刷新和运行操作位于固定顶部。
    FORM: 三栏原始数据质检台，ordered candidate 6，seed a15bb867（降级模式）。
    -->
    <AppTopbar section="数据管理" current="原始数据">
      <template #status>
        <span v-if="catalog" class="save-state">
          <span class="status-dot" />
          {{ catalog.index.active_status
            ? '后台扫描中'
            : catalog.index.latest_status === 'failed'
              ? '最近扫描失败'
              : catalog.index.latest_status === 'limited'
                ? '清单扫描受限'
            : catalog.index.stale
              ? '清单需要更新'
              : catalog.scanned_at
                ? `清单更新于 ${formatTime(catalog.scanned_at)}`
                : '正在建立清单' }}
        </span>
      </template>
      <template #actions>
        <button
          class="button button--ghost"
          type="button"
          :disabled="loadState === 'loading' || scanState === 'queueing' || Boolean(catalog?.index.active_status)"
          @click="requestScan"
        >
          {{ catalog?.index.active_status
            ? '扫描进行中'
            : scanState === 'queueing'
              ? '正在排队…'
              : '更新清单' }}
        </button>
        <NuxtLink
          v-if="canRunSelected"
          class="button button--primary button--link"
          :to="{ path: '/runs', query: { dataset: selectedDataset?.id } }"
        >
          用此数据运行
        </NuxtLink>
      </template>
    </AppTopbar>

    <AppRail active="rawdata" @select="navigateSection" />

    <main class="section-workspace rawdata-page">
      <header class="workspace-header rawdata-header">
        <div>
          <h1>原始数据</h1>
          <p>只读检查 rawdata 目录中的 FASTQ 配对、空文件和命名问题，不移动或修改原始文件。</p>
        </div>
        <div v-if="catalog" class="rawdata-summary" aria-label="原始数据状态">
          <span><b>{{ catalog.summary.ready_dataset_count }}</b> 组可运行</span>
          <span :class="{ 'has-issue': catalog.summary.issue_dataset_count }"><b>{{ catalog.summary.issue_dataset_count }}</b> 组待处理</span>
          <span :class="{ 'has-issue': catalog.summary.unrecognized_fastq_count }"><b>{{ catalog.summary.unrecognized_fastq_count }}</b> 个未识别</span>
          <span><b>{{ catalog.summary.total_size_label }}</b></span>
        </div>
      </header>

      <div v-if="loadState === 'loading'" class="analysis-page-state">正在读取原始数据清单…</div>
      <div v-else-if="loadState === 'error'" class="analysis-page-state analysis-page-state--error" role="alert">
        <strong>{{ errorMessage }}</strong>
        <button class="button button--ghost" type="button" @click="loadCatalog()">重试</button>
      </div>
      <template v-else-if="catalog">
        <div v-if="catalog.issues.length" class="rawdata-global-issues" role="status">
          <p v-for="issue in catalog.issues" :key="`${issue.code}:${issue.path ?? ''}`">
            <strong>{{ issue.message }}</strong><code v-if="issue.path">{{ issue.path }}</code>
          </p>
        </div>

        <section class="rawdata-index-status" aria-label="原始数据索引状态">
          <div>
            <strong>{{ catalog.index.active_status
              ? '后台正在更新清单'
              : catalog.index.latest_status === 'failed'
                ? '最近一次扫描失败'
                : catalog.index.latest_status === 'limited'
                  ? '扫描达到安全预算'
                  : catalog.index.stale
                    ? '当前清单已过期'
                    : '清单已同步' }}</strong>
            <span>
              {{ catalog.index.active_status
                ? `已扫描 ${catalog.scanned_entry_count.toLocaleString('zh-CN')} 个目录项；页面继续使用上次成功快照。`
                : catalog.index.finished_at
                  ? `完成于 ${formatTime(catalog.index.finished_at)}`
                  : '首次扫描完成后会显示可运行数据。' }}
            </span>
          </div>
          <details>
            <summary>扫描策略</summary>
            <p>最多 {{ catalog.index.policy.max_entries.toLocaleString('zh-CN') }} 个目录项、{{ catalog.index.policy.max_files.toLocaleString('zh-CN') }} 个文件、{{ catalog.index.policy.max_depth }} 层目录；每批 {{ catalog.index.policy.batch_entries.toLocaleString('zh-CN') }} 项。</p>
          </details>
        </section>

        <div v-if="catalog.index.repair_suggestions.length" class="rawdata-repair-suggestions">
          <strong>管理员处理建议</strong>
          <p v-for="item in catalog.index.repair_suggestions" :key="item">{{ item }}</p>
        </div>

        <div v-if="catalog.root_status !== 'ready'" class="empty-state rawdata-root-empty">
          <strong>{{ catalog.root_status === 'indexing' ? '正在建立原始数据清单' : '原始数据目录尚未就绪' }}</strong>
          <p>{{ catalog.root_status === 'indexing' ? '后台索引器会分批扫描目录，页面无需保持打开。' : '请确认 Docker Compose 已将宿主机 rawdata 目录只读挂载到项目工作区。' }}</p>
        </div>
        <div v-else class="rawdata-layout">
          <aside class="rawdata-directories" aria-label="原始数据目录">
            <header>
              <h2>目录</h2>
              <span>{{ catalog.directories.length }}</span>
            </header>
            <div class="rawdata-directory-list" role="group" aria-label="按目录筛选">
              <button
                type="button"
                :aria-pressed="selectedDirectory === 'all'"
                :class="{ active: selectedDirectory === 'all' }"
                @click="selectDirectory('all')"
              >
                <span><strong>全部数据</strong><small>{{ catalog.root_directory }}</small></span>
                <b>{{ catalog.summary.dataset_count + catalog.summary.unrecognized_fastq_count }}</b>
              </button>
              <button
                v-for="directory in catalog.directories"
                :key="directory.path"
                type="button"
                :aria-pressed="selectedDirectory === directory.path"
                :class="{ active: selectedDirectory === directory.path }"
                @click="selectDirectory(directory.path)"
              >
                <span><strong>{{ directory.path }}</strong><small>{{ directory.total_size_label }}</small></span>
                <b>{{ directory.dataset_count + directory.unrecognized_count }}</b>
              </button>
            </div>
          </aside>

          <section class="rawdata-list-panel" aria-label="原始数据清单">
            <header class="rawdata-list-toolbar">
              <label class="search-field rawdata-search">
                <span aria-hidden="true">⌕</span>
                <span class="visually-hidden">搜索原始数据</span>
                <input v-model="search" type="search" placeholder="搜索样本、文件或目录" />
              </label>
              <div class="rawdata-filter" aria-label="数据状态筛选">
                <button type="button" :aria-pressed="statusFilter === 'all'" @click="changeStatus('all')">全部</button>
                <button type="button" :aria-pressed="statusFilter === 'ready'" @click="changeStatus('ready')">可运行</button>
                <button type="button" :aria-pressed="statusFilter === 'issue'" @click="changeStatus('issue')">待处理</button>
                <button type="button" :aria-pressed="statusFilter === 'unrecognized'" @click="changeStatus('unrecognized')">未识别</button>
              </div>
            </header>

            <div v-if="visibleItems.length" class="rawdata-table-wrap">
              <table class="rawdata-table">
                <thead>
                  <tr><th>样本 / 文件</th><th>目录</th><th>大小</th><th>状态</th></tr>
                </thead>
                <tbody>
                  <tr v-for="item in visibleItems" :key="item.key" :class="{ active: selectedKey === item.key }">
                    <td>
                      <button type="button" :aria-pressed="selectedKey === item.key" @click="selectItem(item)">
                        <span class="rawdata-item-heading">
                          <strong>{{ itemName(item) }}</strong>
                          <span class="rawdata-state rawdata-state--mobile" :class="`rawdata-state--${item.kind === 'dataset' ? item.dataset.status : 'unrecognized'}`">{{ statusLabel(item) }}</span>
                        </span>
                        <small>{{ item.kind === 'dataset' ? item.dataset.pair_key : item.file.relative_path }}</small>
                        <span class="rawdata-item-meta">{{ itemDirectory(item) }} · {{ itemSize(item) }}</span>
                      </button>
                    </td>
                    <td>{{ itemDirectory(item) }}</td>
                    <td>{{ itemSize(item) }}</td>
                    <td><span class="rawdata-state" :class="`rawdata-state--${item.kind === 'dataset' ? item.dataset.status : 'unrecognized'}`">{{ statusLabel(item) }}</span></td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="empty-state rawdata-list-empty">
              <strong>没有匹配的数据</strong>
              <p>调整目录、状态或搜索条件。</p>
            </div>
          </section>

          <aside class="rawdata-inspector" aria-label="原始数据详情">
            <template v-if="selectedDataset">
              <header>
                <div><span>数据集</span><h2>{{ selectedDataset.name }}</h2></div>
                <span class="rawdata-state" :class="`rawdata-state--${selectedDataset.status}`">{{ selectedDataset.status === 'ready' ? '可运行' : '需处理' }}</span>
              </header>
              <dl class="rawdata-facts">
                <div><dt>目录</dt><dd>{{ selectedDataset.directory }}</dd></div>
                <div><dt>总大小</dt><dd>{{ selectedDataset.total_size_label }}</dd></div>
                <div><dt>配对规则</dt><dd><code>{{ selectedDataset.pair_key }}</code></dd></div>
                <div v-if="selectedDataset.first_seen_at"><dt>首次发现</dt><dd>{{ formatTime(selectedDataset.first_seen_at) }}</dd></div>
                <div v-if="selectedDataset.last_changed_at"><dt>最近变化</dt><dd>{{ formatTime(selectedDataset.last_changed_at) }}</dd></div>
                <div><dt>运行引用</dt><dd>{{ selectedDataset.run_count ?? 0 }} 次</dd></div>
              </dl>
              <section v-if="selectedDataset.issues.length" class="rawdata-inspector-section">
                <h3>需要处理</h3>
                <ul class="rawdata-issue-list">
                  <li v-for="issue in selectedDataset.issues" :key="`${issue.code}:${issue.path ?? ''}`">
                    <strong>{{ issue.message }}</strong><code v-if="issue.path">{{ issue.path }}</code>
                  </li>
                </ul>
              </section>
              <section v-if="selectedDataset.recent_runs?.length" class="rawdata-inspector-section">
                <h3>最近运行</h3>
                <ul class="rawdata-run-list">
                  <li v-for="run in selectedDataset.recent_runs" :key="run.id">
                    <NuxtLink :to="`/runs?run=${encodeURIComponent(run.id)}`">{{ run.id.slice(0, 8) }}</NuxtLink>
                    <span>{{ run.status }} · {{ formatTime(run.created_at) }}</span>
                  </li>
                </ul>
              </section>
              <section class="rawdata-inspector-section">
                <h3>FASTQ 文件</h3>
                <ul class="rawdata-file-list">
                  <li v-for="file in selectedDataset.files" :key="file.relative_path">
                    <b>R{{ file.mate }}</b>
                    <span><strong>{{ file.name }}</strong><small>{{ file.size_label }} · {{ formatTime(file.modified_at) }}</small></span>
                  </li>
                  <li v-for="mate in selectedDataset.missing_mates" :key="mate" class="missing">
                    <b>R{{ mate }}</b><span><strong>文件缺失</strong><small>请补齐同名配对文件</small></span>
                  </li>
                </ul>
              </section>
              <NuxtLink
                v-if="selectedDataset.status === 'ready'"
                class="button button--primary button--link rawdata-run-action"
                :to="{ path: '/runs', query: { dataset: selectedDataset.id } }"
              >
                带入运行分析
              </NuxtLink>
              <p v-else class="rawdata-run-disabled">处理完上述问题后才会出现在运行分析的数据选择中。</p>
            </template>
            <template v-else-if="selectedFile">
              <header><div><span>未识别文件</span><h2>{{ selectedFile.name }}</h2></div></header>
              <p class="rawdata-inspector-note">文件是 FASTQ 压缩格式，但名称不含可识别的 R1 / R2 配对标记。</p>
              <dl class="rawdata-facts">
                <div><dt>路径</dt><dd><code>{{ selectedFile.relative_path }}</code></dd></div>
                <div><dt>大小</dt><dd>{{ selectedFile.size_label }}</dd></div>
                <div><dt>修改时间</dt><dd>{{ formatTime(selectedFile.modified_at) }}</dd></div>
              </dl>
              <section class="rawdata-naming-rule">
                <h3>建议命名</h3>
                <code>SAMPLE_R1.fastq.gz</code>
                <code>SAMPLE_R2.fastq.gz</code>
              </section>
            </template>
            <div v-else class="empty-state rawdata-inspector-empty">选择一项查看详情。</div>
          </aside>
        </div>
      </template>
      <footer class="workspace-version">v1.0 · 稳定版</footer>
    </main>
    <!-- FINISH REVIEW: critique this page against /home/zhuqin/.agents/skills/impeccable/reference/craft-floor.md -->
  </div>
</template>

<style scoped>
.rawdata-index-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg);
}

.rawdata-index-status > div {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.rawdata-index-status span,
.rawdata-index-status details,
.rawdata-repair-suggestions p,
.rawdata-run-list span {
  color: var(--color-muted);
  font-size: 0.75rem;
}

.rawdata-index-status summary {
  cursor: pointer;
  white-space: nowrap;
}

.rawdata-index-status details p {
  max-width: 440px;
  margin: 8px 0 0;
  line-height: 1.5;
}

.rawdata-repair-suggestions {
  margin-bottom: 12px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--color-warning) 45%, var(--color-border));
  border-radius: 10px;
  background: var(--color-warning-soft);
}

.rawdata-repair-suggestions p {
  margin: 4px 0 0;
}

.rawdata-run-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.rawdata-run-list li {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

@media (max-width: 720px) {
  .rawdata-index-status {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
