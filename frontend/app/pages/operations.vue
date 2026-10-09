<script setup lang="ts">
import type { AnalysisRunEvent } from '~/types/analysis'
import type { AnalysisProduct, ConsoleRun, IntegrationRun, RunEvents, RunMetadata } from '~/types/operations'
import OperationsIcon from '~/components/operations/OperationsIcon.vue'
import OperationsRunDetail from '~/components/operations/OperationsRunDetail.vue'
import OperationsRunTable from '~/components/operations/OperationsRunTable.vue'
import {
  apiErrorMessage, businessRunUrl, formatRunTime, isActiveRun, needsAttention,
  normalizeRun, parseBusinessLinks,
} from '~/utils/operations'

useHead({ title: 'BWM · 运维控制台' })
const { $api } = useNuxtApp()
const auth = useAuth()
const config = useRuntimeConfig()
const route = useRoute()
const router = useRouter()
const links = parseBusinessLinks(config.public.operationsBusinessLinks)
const nav = [
  { id: 'overview', label: '运行总览', icon: 'overview' },
  { id: 'runs', label: '运行记录', icon: 'file' },
  { id: 'workflows', label: '业务流程', icon: 'layers' },
  { id: 'resources', label: '资源依赖', icon: 'cube' },
]
const view = computed(() => nav.some(item => item.id === route.query.view) ? String(route.query.view) : 'overview')
const runId = computed(() => view.value === 'runs' && typeof route.query.run === 'string' ? route.query.run : '')
const runList = ref<IntegrationRun[]>([])
const runMetadata = ref<RunMetadata[]>([])
const products = ref<AnalysisProduct[]>([])
const runsLoaded = ref(false)
const productsLoaded = ref(false)
const loading = ref(true)
const refreshing = ref(false)
const productsLoading = ref(false)
const runError = ref('')
const productError = ref('')
const metadataError = ref('')
const refreshedAt = ref('')
const productCheckedAt = ref('')
const business = ref('all')
const filter = ref('all')
const search = ref('')
const mobileMenu = ref(false)
const selectedProductKey = ref('')
const detail = ref<IntegrationRun | null>(null)
const detailMetadata = ref<RunMetadata | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const detailMetadataError = ref('')
const events = ref<AnalysisRunEvent[]>([])
const eventsError = ref('')
const eventsLoading = ref(false)
const hasMoreEvents = ref(false)
const eventCursor = ref(0)
const toast = ref('')
const dialog = ref<HTMLDialogElement | null>(null)
const dialogType = ref<'cancel' | 'retry' | 'copy' | ''>('')
const operationTarget = ref<ConsoleRun | null>(null)
const operationBusy = ref(false)
const operationError = ref('')
const copyText = ref('')
let detailVersion = 0
let refreshBusy = false
let pollTimer: ReturnType<typeof setTimeout> | undefined
let toastTimer: ReturnType<typeof setTimeout> | undefined
let mounted = false
let trigger: HTMLElement | null = null

const businessNames = computed(() => Object.fromEntries(links.map(link => [link.clientId, link.name])))
const businesses = computed(() => Array.from(new Set([...links.map(link => link.clientId), ...runList.value.map(run => run.external_ref.client_id)])))
const businessLabel = computed(() => business.value === 'all' ? '全部业务' : businessNames.value[business.value] || business.value)
const allRuns = computed(() => runList.value.map(run => normalizeRun(run, runMetadata.value.find(item => item.id === run.id), products.value)))
const scopedRuns = computed(() => allRuns.value.filter(run => business.value === 'all' || run.external_ref.client_id === business.value))
const counts = computed(() => ({
  active: scopedRuns.value.filter(run => ['preparing', 'running', 'cancel_requested'].includes(run.status)).length,
  queued: scopedRuns.value.filter(run => run.status === 'queued').length,
  attention: scopedRuns.value.filter(needsAttention).length,
  succeeded: scopedRuns.value.filter(run => run.status === 'succeeded').length,
}))
const summaryItems = computed(() => [
  { id: 'active', label: '运行中', count: counts.value.active },
  { id: 'queued', label: '排队中', count: counts.value.queued },
  { id: 'attention', label: '需要关注', count: counts.value.attention },
  { id: 'succeeded', label: '分析完成', count: counts.value.succeeded },
])
const filteredRuns = computed(() => scopedRuns.value.filter(run => {
  const match = filter.value === 'all' || (filter.value === 'attention' ? needsAttention(run) : filter.value === 'active' ? ['preparing', 'running', 'cancel_requested'].includes(run.status) : run.status === filter.value)
  const text = [run.sampleLabel, run.id, run.workflowLabel, run.analysis_product?.analysis_code, run.analysis_product?.contract_version, run.external_ref.external_analysis_id, run.external_ref.external_run_id].join(' ').toLowerCase()
  return match && text.includes(search.value.trim().toLowerCase())
}))
const attentionRuns = computed(() => scopedRuns.value.filter(needsAttention).slice(0, 3))
const selectedRun = computed(() => detail.value ? normalizeRun(detail.value, detailMetadata.value || undefined, products.value) : null)
const selectedProduct = computed(() => products.value.find(item => productKey(item) === selectedProductKey.value) || products.value[0] || null)
const operationBusinessUrl = computed(() => businessRunUrl(operationTarget.value, links))
const listLimited = computed(() => runList.value.length >= 200)

function productKey(product: AnalysisProduct) { return `${product.analysis_code}@${product.contract_version}` }
function productClients(product: AnalysisProduct) {
  return Array.from(new Set(runList.value.filter(run => run.analysis_product?.analysis_code === product.analysis_code && run.analysis_product?.contract_version === product.contract_version).map(run => businessNames.value[run.external_ref.client_id] || run.external_ref.client_id)))
}
function announce(text: string) { toast.value = text; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.value = '' }, 4200) }
async function navigate(section: string) {
  mobileMenu.value = false
  await router.push({ path: '/operations', query: section === 'overview' ? {} : { view: section } })
  document.querySelector('.ops-content')?.scrollIntoView({ block: 'start' })
}
async function selectRun(id: string) {
  mobileMenu.value = false
  await router.push({ path: '/operations', query: { view: 'runs', run: id } })
}
async function goFiltered(status: string) { filter.value = status; search.value = ''; await navigate('runs') }
async function showProductRuns(product: AnalysisProduct) { search.value = product.analysis_code; filter.value = 'all'; await navigate('runs') }
async function loadProducts() {
  if (productsLoading.value) return
  productsLoading.value = true
  try {
    const response = await $api<{ results: AnalysisProduct[] }>('/api/v1/integration/analysis-products')
    products.value = response.results
    productsLoaded.value = true
    productError.value = ''
    productCheckedAt.value = new Date().toISOString()
  } catch (error) { productError.value = apiErrorMessage(error, '流程目录读取失败，请刷新后重试。') }
  finally { productsLoading.value = false }
}
async function loadRuns() {
  if (refreshBusy) return
  refreshBusy = true
  try {
    const responses = await Promise.allSettled([
      $api<{ results: IntegrationRun[] }>('/api/v1/integration/analysis-runs'),
      $api<{ results: RunMetadata[] }>('/api/v1/analysis-runs'),
    ])
    const records = responses[0]
    if (records.status === 'fulfilled') {
      runList.value = records.value.results
      runsLoaded.value = true
      runError.value = ''
      refreshedAt.value = new Date().toISOString()
    } else { runError.value = apiErrorMessage(records.reason, '运行记录读取失败，当前状态未能更新。') }
    const metadata = responses[1]
    if (metadata.status === 'fulfilled') { runMetadata.value = metadata.value.results; metadataError.value = '' }
    else { metadataError.value = '部分样本信息暂时无法读取，未取得名称的任务显示业务执行编号。' }
  } finally { refreshBusy = false; loading.value = false }
}
async function loadEvents(id: string, version: number, append = false) {
  eventsLoading.value = true
  try {
    const response = await $api<RunEvents>(`/api/v1/integration/analysis-runs/${encodeURIComponent(id)}/events`, { query: { after_id: append ? eventCursor.value : 0 } })
    if (version !== detailVersion || runId.value !== id) return
    events.value = append ? [...events.value, ...response.results.filter(item => !events.value.some(old => old.id === item.id))] : response.results
    eventCursor.value = response.next_after_id
    hasMoreEvents.value = response.results.length >= 500
    eventsError.value = ''
  } catch (error) { if (version === detailVersion && runId.value === id) eventsError.value = apiErrorMessage(error, '运行事件读取失败，请刷新重试。') }
  finally { if (version === detailVersion) eventsLoading.value = false }
}
async function loadDetail(quiet = false) {
  const id = runId.value
  if (!id) return
  const version = ++detailVersion
  if (!quiet) detailLoading.value = true
  try {
    const responses = await Promise.allSettled([
      $api<IntegrationRun>(`/api/v1/integration/analysis-runs/${encodeURIComponent(id)}`),
      $api<RunMetadata>(`/api/v1/analysis-runs/${encodeURIComponent(id)}`),
    ])
    if (version !== detailVersion || runId.value !== id) return
    const record = responses[0]
    if (record.status === 'fulfilled') {
      detail.value = record.value
      detailError.value = ''
      const index = runList.value.findIndex(item => item.id === id)
      if (index >= 0) runList.value[index] = record.value
    } else { detailError.value = apiErrorMessage(record.reason, '任务详情读取失败或任务不存在。') }
    const metadata = responses[1]
    if (metadata.status === 'fulfilled') { detailMetadata.value = metadata.value; detailMetadataError.value = '' }
    else { detailMetadataError.value = '样本与执行引擎信息暂时无法取得。' }
    await loadEvents(id, version)
  } finally { if (version === detailVersion) detailLoading.value = false }
}
async function refresh() {
  if (refreshing.value) return
  refreshing.value = true
  try { await Promise.all([loadRuns(), loadProducts(), runId.value ? loadDetail(true) : Promise.resolve()]) }
  finally { refreshing.value = false }
}
function schedulePoll() {
  clearTimeout(pollTimer)
  if (!mounted) return
  pollTimer = setTimeout(async () => {
    if (document.visibilityState === 'visible' && auth.user.value?.is_admin) {
      if (runId.value) { if (detail.value && isActiveRun(detail.value.status)) await loadDetail(true) }
      else if (view.value === 'overview' || view.value === 'runs') await loadRuns()
    }
    schedulePoll()
  }, 10000)
}
watch(runId, () => {
  detailVersion++
  detail.value = null; detailMetadata.value = null; detailError.value = ''; detailMetadataError.value = ''
  events.value = []; eventsError.value = ''; eventCursor.value = 0; hasMoreEvents.value = false
  if (mounted && runId.value) void loadDetail()
})
watch(business, () => { filter.value = 'all'; search.value = '' })
async function openDialog(type: 'cancel' | 'retry' | 'copy') {
  trigger = document.activeElement as HTMLElement
  operationTarget.value = selectedRun.value
  operationError.value = ''; dialogType.value = type
  await nextTick(); if (dialog.value && !dialog.value.open) dialog.value.showModal()
}
function closeDialog() { if (!operationBusy.value) dialog.value?.close() }
function onDialogClose() { dialogType.value = ''; trigger?.focus() }
async function cancelRun() {
  const target = operationTarget.value
  if (!target || operationBusy.value || !['queued', 'preparing', 'running'].includes(target.status)) return
  operationBusy.value = true; operationError.value = ''
  try {
    const result = await $api<IntegrationRun>(`/api/v1/integration/analysis-runs/${encodeURIComponent(target.id)}/cancel`, { method: 'POST', body: {} })
    if (runId.value === result.id) detail.value = result
    const index = runList.value.findIndex(item => item.id === result.id)
    if (index >= 0) runList.value[index] = result
    dialog.value?.close()
    announce(result.status === 'canceled' ? '任务已取消' : result.status === 'cancel_requested' ? '取消请求已提交，等待执行引擎确认' : '任务状态已更新，请查看当前状态')
    await loadDetail(true)
  } catch (error) { operationError.value = apiErrorMessage(error, '未能确认取消结果，请刷新任务状态后确认。') }
  finally { operationBusy.value = false }
}
async function copy(value: string) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(value); announce('已复制'); return }
    const field = document.createElement('textarea'); field.value = value; field.style.position = 'fixed'; field.style.opacity = '0'
    document.body.appendChild(field); field.select()
    const copied = document.execCommand('copy'); field.remove()
    if (copied) { announce('已复制'); return }
  } catch { /* Offer a selectable value when clipboard permissions are unavailable. */ }
  copyText.value = value
  await openDialog('copy')
}
async function logout() {
  try { await $api('/api/v1/auth/logout', { method: 'POST' }) }
  finally { auth.clear(); await navigateTo('/login') }
}
onMounted(async () => {
  mounted = true
  if (!auth.user.value?.is_admin) { loading.value = false; return }
  await Promise.all([loadRuns(), loadProducts(), runId.value ? loadDetail() : Promise.resolve()])
  schedulePoll()
})
onBeforeUnmount(() => { mounted = false; detailVersion++; clearTimeout(pollTimer); clearTimeout(toastTimer) })
</script>

<template>
  <div class="ops-console">
    <div v-if="mobileMenu" class="ops-scrim" @click="mobileMenu=false" />
    <aside class="ops-sidebar" :class="{ open: mobileMenu }" aria-label="运维导航">
      <div class="ops-brand"><strong>BWM</strong><span>运维控制台</span><button type="button" class="ops-icon-button ops-mobile-close" aria-label="关闭导航" @click="mobileMenu=false"><OperationsIcon name="close" /></button></div>
      <div class="ops-scope"><label for="ops-business">业务范围</label><select id="ops-business" v-model="business"><option value="all">全部业务</option><option v-for="client in businesses" :key="client" :value="client">{{ businessNames[client] || client }}</option></select></div>
      <nav><button v-for="item in nav" :key="item.id" type="button" :class="{ active:view===item.id }" :aria-current="view===item.id?'page':undefined" @click="navigate(item.id)"><OperationsIcon :name="item.icon" />{{ item.label }}</button></nav>
      <div class="ops-profile"><span class="ops-avatar">{{ auth.user.value?.username?.slice(0,1).toUpperCase() || 'B' }}</span><div><strong>{{ auth.user.value?.username }}</strong><span>{{ auth.user.value?.is_admin?'管理员':'当前账号' }}</span></div><button type="button" class="ops-text-button" @click="logout">退出</button></div>
    </aside>
    <div class="ops-main"><header class="ops-topbar"><button type="button" class="ops-icon-button ops-menu" aria-label="打开导航" @click="mobileMenu=true"><OperationsIcon name="menu" /></button><span>运维 <i>/</i> {{ businessLabel }}</span><span v-if="refreshedAt" class="ops-last-update">更新于 {{ formatRunTime(refreshedAt) }}</span></header>
      <main class="ops-content" tabindex="-1">
        <div v-if="!auth.user.value?.is_admin" class="ops-panel ops-empty"><OperationsIcon name="help" /><h1>当前账号无运维访问权限</h1><p>请使用已授权的 BWM 管理员账号登录。</p></div>
        <template v-else>
          <div v-if="runError" class="ops-notice ops-error-notice" role="alert"><OperationsIcon name="alert" /><span>{{ runError }}<template v-if="runsLoaded"> 当前保留上次成功读取的数据。</template></span><button type="button" class="ops-text-button" @click="refresh">重新读取</button></div>
          <template v-if="view==='overview'">
            <header class="ops-page-heading"><div><h1>运行总览</h1><p>关注业务分析进度、异常与结果状态。</p></div><div class="ops-heading-actions"><button type="button" class="ops-button ops-neutral" :disabled="refreshing" @click="refresh"><OperationsIcon name="refresh" :class="{ spinning:refreshing }" />刷新</button><button type="button" class="ops-button ops-primary" @click="navigate('runs')">查看运行记录</button></div></header>
            <section class="ops-summary" aria-label="运行状态汇总"><div class="ops-summary-grid"><button v-for="item in summaryItems" :key="item.id" type="button" @click="goFiltered(item.id)"><span>{{ item.label }}</span><strong :class="{ amber:item.id==='attention' }">{{ runsLoaded ? item.count : '—' }}</strong></button></div>
              <div v-if="loading && !runsLoaded" class="ops-summary-note">正在读取运行状态…</div>
              <template v-else-if="runsLoaded"><div v-for="run in attentionRuns" :key="run.id" class="ops-alert-strip"><OperationsIcon name="alert" /><strong>{{ run.status==='failed'?'分析执行失败':'结果需要检查' }}</strong><span>{{ run.sampleLabel }} · {{ run.current_step || run.output_status }}</span><button type="button" class="ops-text-button" @click="selectRun(run.id)">查看原因 <OperationsIcon name="chevron" /></button></div><div v-if="!attentionRuns.length" class="ops-summary-note"><OperationsIcon name="check" />已读取的记录中没有待处理异常。</div></template>
            </section>
            <p class="ops-data-scope">{{ listLimited?'统计范围：最近 200 条运行记录':'统计范围：本次读取的 '+(runsLoaded?scopedRuns.length:'—')+' 条运行记录' }}<span v-if="!runError"> · 每 10 秒刷新</span></p>
            <OperationsRunTable v-if="runsLoaded" :runs="filteredRuns" :filter="filter" :business-names="businessNames" overview @select="selectRun" @filter="filter=$event" @all="navigate('runs')" />
            <section class="ops-panel ops-published-panel"><header class="ops-panel-heading"><h2>本实例已发布流程</h2><button type="button" class="ops-text-button" @click="navigate('workflows')">查看流程 <OperationsIcon name="chevron" /></button></header><p v-if="productError" class="ops-inline-error" role="alert">{{ productError }}</p><div v-for="product in products" :key="productKey(product)" class="ops-workflow-row"><div class="ops-workflow-symbol"><OperationsIcon name="flow" /></div><div class="ops-workflow-label"><strong>{{ product.name }}</strong><span>{{ product.analysis_code }} · 固定流程修订 {{ product.workflow.version }}</span></div><span class="ops-tag">契约 {{ product.contract_version }}</span><span class="ops-tag" :class="product.ready?'ops-tag-green':'ops-tag-amber'">{{ product.ready?'契约可用':'契约需处理' }}</span></div><p v-if="!products.length && !productError" class="ops-body-note">{{ productsLoading?'正在读取流程目录…':'尚未发布分析产品。' }}</p></section>
          </template>
          <template v-else-if="view==='runs' && !runId">
            <header class="ops-page-heading"><div><h1>运行记录</h1><p>按业务、样本或任务编号查找分析记录。</p></div><button type="button" class="ops-button ops-neutral" :disabled="refreshing" @click="refresh"><OperationsIcon name="refresh" />刷新</button></header>
            <div class="ops-search-panel"><label for="ops-search">关键词</label><div class="ops-search-box"><OperationsIcon name="search" /><input id="ops-search" v-model="search" type="search" placeholder="样本、任务编号、业务执行编号或流程名称" /></div><button type="button" class="ops-button ops-neutral" @click="search='';filter='all'">重置</button></div><p v-if="metadataError" class="ops-inline-error">{{ metadataError }}</p><p v-if="listLimited" class="ops-data-scope">当前查询范围为最近 200 条任务。</p><OperationsRunTable v-if="runsLoaded" :runs="filteredRuns" :filter="filter" :business-names="businessNames" @select="selectRun" @filter="filter=$event" /><div v-else-if="loading" class="ops-panel ops-empty">正在读取运行记录…</div>
          </template>
          <template v-else-if="view==='runs' && runId"><div v-if="detailError" class="ops-notice ops-error-notice" role="alert"><OperationsIcon name="alert" /><span>{{ detailError }}</span><button type="button" class="ops-text-button" @click="loadDetail()">重新读取</button></div><OperationsRunDetail v-if="selectedRun" :key="selectedRun.id" :run="selectedRun" :metadata="detailMetadata" :metadata-error="detailMetadataError" :events="events" :events-error="eventsError" :events-loading="eventsLoading" :has-more-events="hasMoreEvents" :links="links" :business-name="businessNames[selectedRun.external_ref.client_id] || selectedRun.external_ref.client_id" @back="navigate('runs')" @refresh="loadDetail(true)" @copy="copy" @cancel="openDialog('cancel')" @retry="openDialog('retry')" @select="selectRun" @more-events="loadEvents(runId, detailVersion, true)" /><div v-else-if="detailLoading" class="ops-panel ops-empty">正在读取任务详情…</div><button v-else-if="detailError" type="button" class="ops-button ops-neutral" @click="navigate('runs')">返回运行记录</button></template>
          <template v-else-if="view==='workflows'">
            <header class="ops-page-heading"><div><h1>业务流程</h1><p>查看本实例已发布的分析产品、固定契约与实际调用来源。</p></div><button type="button" class="ops-button ops-neutral" :disabled="productsLoading" @click="loadProducts"><OperationsIcon name="refresh" />刷新目录</button></header>
            <div v-if="productError" class="ops-notice ops-error-notice" role="alert">{{ productError }}</div>
            <section v-for="product in products" :key="productKey(product)" class="ops-panel ops-product-panel"><header class="ops-product-heading"><div class="ops-workflow-symbol"><OperationsIcon name="flow" /></div><div><div class="ops-title-row"><h2>{{ product.name }}</h2><span class="ops-tag ops-tag-green">已发布</span></div><p>{{ product.analysis_code }} · 契约 {{ product.contract_version }}</p></div><button type="button" class="ops-text-button" @click="showProductRuns(product)">查看运行记录 <OperationsIcon name="chevron" /></button></header>
              <dl class="ops-product-facts"><div><dt>固定产品契约</dt><dd>{{ product.contract_version }}</dd></div><div><dt>对应流程修订</dt><dd>{{ product.workflow.version }}</dd></div><div><dt>已有调用来源</dt><dd>{{ productClients(product).join('、') || '已读取记录中暂无调用' }}</dd></div><div><dt>发布时间</dt><dd>{{ formatRunTime(product.created_at) }}</dd></div></dl>
              <p v-if="product.description" class="ops-body-note">{{ product.description }}</p><div v-if="product.blockers.length" class="ops-notice ops-warning-notice"><OperationsIcon name="alert" /><ul><li v-for="message in product.blockers" :key="message">{{ message }}</li></ul></div>
              <details class="ops-contract"><summary>输入与输出约定</summary><div class="ops-contract-grid"><div><h3>分析输入</h3><dl class="ops-key-values"><div v-for="port in product.input_contract" :key="port.name"><dt>{{ port.label || port.name }}</dt><dd>{{ port.wdl_type }} · {{ port.required?'必填':'可选' }}</dd></div></dl></div><div><h3>交付输出</h3><dl class="ops-key-values"><div v-for="port in product.output_contract" :key="port.name"><dt>{{ port.label || port.name }}</dt><dd>{{ port.semantic_type || port.wdl_type }}</dd></div></dl></div></div></details>
              <div class="ops-product-footer"><button type="button" class="ops-text-button" @click="selectedProductKey=productKey(product);navigate('resources')">查看资源依赖 <OperationsIcon name="chevron" /></button></div>
            </section><div v-if="!products.length && !productError" class="ops-panel ops-empty"><OperationsIcon name="layers" /><strong>{{ productsLoading?'正在读取目录…':'尚未发布分析产品' }}</strong></div>
          </template>
          <template v-else-if="view==='resources'">
            <header class="ops-page-heading"><div><h1>资源依赖</h1><p>查看已发布流程声明的资源路径与版本约定。</p></div><button type="button" class="ops-button ops-neutral" :disabled="productsLoading" @click="loadProducts"><OperationsIcon name="refresh" />刷新声明</button></header>
            <div v-if="productError" class="ops-notice ops-error-notice" role="alert">{{ productError }}</div><div v-if="products.length>1" class="ops-search-panel"><label for="ops-product">分析产品</label><select id="ops-product" v-model="selectedProductKey"><option v-for="product in products" :key="productKey(product)" :value="productKey(product)">{{ product.name }} · {{ product.contract_version }}</option></select></div>
            <section v-if="selectedProduct" class="ops-panel"><header class="ops-panel-heading"><h2>{{ selectedProduct.name }} · {{ selectedProduct.contract_version }}</h2><span class="ops-tag" :class="selectedProduct.ready?'ops-tag-green':'ops-tag-amber'">{{ selectedProduct.ready?'契约可用':'契约需处理' }}</span></header><p class="ops-body-note">以下是流程声明，不代表磁盘或工具镜像已检查通过。实际可用性在任务预检和执行前核对。</p><div v-if="selectedProduct.blockers.length" class="ops-notice ops-warning-notice"><ul><li v-for="message in selectedProduct.blockers" :key="message">{{ message }}</li></ul></div>
              <div class="ops-table-scroll ops-resource-table" tabindex="0" aria-label="流程声明的资源"><table class="ops-table"><thead><tr><th>资源名称</th><th>受管根目录</th><th>相对路径</th><th>类型</th><th class="ops-action-cell">操作</th></tr></thead><tbody><tr v-for="resource in selectedProduct.interface.resources || []" :key="resource.name"><td>{{ resource.name }}</td><td>{{ resource.root_alias }}</td><td><code>{{ resource.relative_path }}</code></td><td>{{ resource.kind==='directory'?'目录':'文件' }}</td><td class="ops-action-cell"><button type="button" class="ops-text-button" @click="copy(resource.relative_path)">复制路径</button></td></tr></tbody></table></div><p v-if="!selectedProduct.interface.resources?.length" class="ops-body-note">该产品未在契约中单独声明参考资源。</p><p class="ops-data-scope">声明读取时间：{{ formatRunTime(productCheckedAt) }}</p>
            </section><div v-else-if="!productError" class="ops-panel ops-empty">{{ productsLoading?'正在读取资源声明…':'尚无已发布流程的资源声明。' }}</div>
          </template>
        </template>
      </main>
    </div>
    <div v-if="toast" class="ops-toast" role="status"><OperationsIcon name="check" />{{ toast }}</div>
    <dialog ref="dialog" class="ops-modal" @close="onDialogClose" @cancel="operationBusy && $event.preventDefault()" @click="($event.target===dialog) && closeDialog()"><template v-if="dialogType"><header><h2>{{ dialogType==='cancel'?'取消分析任务':dialogType==='retry'?'从业务系统发起重跑':'复制内容' }}</h2><button type="button" class="ops-icon-button" aria-label="关闭弹窗" :disabled="operationBusy" @click="closeDialog"><OperationsIcon name="close" /></button></header><div class="ops-modal-body"><template v-if="dialogType==='cancel' && operationTarget"><div class="ops-operation-target"><strong>{{ operationTarget.sampleLabel }}</strong><code>{{ operationTarget.id }}</code></div><p>{{ operationTarget.status==='queued'?'任务尚未被领取。取消后退出队列，运行记录保留。':'将向执行引擎请求停止；确认停止前，任务显示为“取消中”。' }}</p><p>原始数据不会删除。业务系统通过状态同步获取取消结果。</p><p v-if="operationError" class="ops-inline-error" role="alert">{{ operationError }}</p><footer><button type="button" class="ops-button ops-neutral" :disabled="operationBusy" @click="closeDialog">返回</button><button type="button" class="ops-button ops-danger" :disabled="operationBusy" @click="cancelRun">{{ operationBusy?'提交中…':'确认取消任务' }}</button></footer></template>
      <template v-else-if="dialogType==='retry' && operationTarget"><div class="ops-operation-target"><strong>{{ operationTarget.sampleLabel }}</strong><span>{{ businessNames[operationTarget.external_ref.client_id] || operationTarget.external_ref.client_id }}</span></div><p>请在业务分析记录中确认重新分析。由业务系统创建新的执行记录，保持样本、分析结果和报告的关联。</p><p v-if="!operationBusinessUrl">当前业务尚未配置页面入口。请使用下方执行编号在对应业务系统中定位任务。</p><div class="ops-copyable"><code>{{ operationTarget.external_ref.external_run_id }}</code><button type="button" class="ops-icon-button" aria-label="复制业务执行编号" @click="copy(operationTarget.external_ref.external_run_id)"><OperationsIcon name="copy" /></button></div><footer><button type="button" class="ops-button ops-neutral" @click="closeDialog">返回</button><a v-if="operationBusinessUrl" class="ops-button ops-primary" :href="operationBusinessUrl" target="_blank" rel="noopener noreferrer">打开业务分析记录</a></footer></template>
      <template v-else-if="dialogType==='copy'"><p>请选择并复制下方内容：</p><textarea readonly :value="copyText" aria-label="待复制内容" @focus="($event.target as HTMLTextAreaElement).select()" /><footer><button type="button" class="ops-button ops-primary" @click="closeDialog">关闭</button></footer></template>
    </div></template></dialog>
  </div>
</template>

<style src="~/assets/css/operations.css" />
