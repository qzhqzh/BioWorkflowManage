<script setup lang="ts">
interface WorkflowLibraryEntry {
  slug: string
  name: string
  description?: string
  kind?: 'workflow' | 'subworkflow'
  latest_version: number | null
  updated_at?: string
  created_by?: string
  is_mine?: boolean
}

interface CompilationVersion {
  id: string
  version: string
  createdAt: string
  status: 'succeeded' | 'failed'
}

interface WdlRevision {
  version: number
  source: 'system' | 'manual'
  artifact_role?: 'compiled_snapshot' | 'derived_draft'
  executable?: boolean
  workflow_version?: number | null
  base_workflow_version?: {
    version: number
    semantic_digest: string
    compiler_profile: string
  } | null
  run_source?: {
    type: 'workflow_version'
    version: number
    semantic_digest: string
  } | null
  base_wdl_revision?: number | null
  created_by?: string
  note?: string
}

interface WorkflowVersionSnapshot {
  version: number
  name: string
  description?: string
  semantic_digest: string
  compiler_profile: string
  workflow_graph?: Record<string, any>
}

interface WdlGraphProposalChange {
  kind: string
  subject: string
  detail: string
}

interface WdlGraphProposalFieldDiff {
  field: string
  label: string
  before: unknown
  after: unknown
}

interface WdlGraphProposalToolDraft {
  tool_id: string
  base_version: string | null
  proposed_version: string
  changed_fields: string[]
  field_diffs?: WdlGraphProposalFieldDiff[]
}

interface WdlGraphProposal {
  id: number
  status: 'ready' | 'blocked' | 'applied'
  proposal_digest: string
  base_document_version: number
  base_document_digest: string
  summary: {
    workflow_change_count: number
    tool_draft_count: number
    instance_change_count: number
  }
  changes: Record<string, WdlGraphProposalChange[]>
  required_confirmations: string[]
  warnings: string[]
  blocking_issues: string[]
  tool_drafts: WdlGraphProposalToolDraft[]
}

const props = defineProps<{
  workflowSlug: string
  workflowDocuments: WorkflowLibraryEntry[]
  libraryState: 'loading' | 'ready' | 'error'
  libraryError: string
  isWorkflowSwitching: boolean
  switchingWorkflowSlug: string
  saveState: 'loading' | 'saved' | 'saving' | 'error'
  compileState: 'idle' | 'running' | 'success' | 'error'
  compilationVersions: CompilationVersion[]
  selectedCompilationId: string
  selectedCompilationVersion?: string
  wdlRevisions: WdlRevision[]
  selectedWdlVersion?: number
  selectedWdlRevision?: WdlRevision
  activeWdlContent: string
  previewLines: string[]
  wdlSaveState: 'idle' | 'saving' | 'error'
  wdlRevisionLoadState: 'idle' | 'loading' | 'error'
  wdlAssetCreateState: 'idle' | 'saving' | 'error'
  wdlAssetCreateError: string
  wdlGraphProposal?: WdlGraphProposal
  wdlGraphProposalState: 'idle' | 'loading' | 'applying' | 'error'
  wdlGraphProposalError: string
  copiedArtifact: string
  createState: 'idle' | 'saving' | 'success' | 'error'
  createError: string
  createRequest?: 'workflow' | 'subworkflow'
  initialOwnerFilter?: 'all' | 'mine' | 'shared'
  initialKindFilter?: 'all' | 'workflow' | 'subworkflow'
  versionSnapshot?: WorkflowVersionSnapshot
  workflowGraph?: Record<string, any>
}>()

const showCreatePanel = ref(false)
const showWdlDestinations = ref(false)
const workflowQuery = ref('')
const workflowOwnerFilter = ref<'all' | 'mine' | 'shared'>('all')
const workflowKindFilter = ref<'all' | 'workflow' | 'subworkflow'>('all')
const workflowStatusFilter = ref<'all' | 'draft' | 'published'>('all')
const createDraft = ref({
  slug: '',
  name: '',
  description: '',
  kind: 'workflow' as 'workflow' | 'subworkflow',
})

function matchesWorkflowFacets(item: WorkflowLibraryEntry) {
  if (workflowOwnerFilter.value === 'mine' && !item.is_mine) return false
  if (workflowOwnerFilter.value === 'shared' && item.is_mine) return false
  if (workflowKindFilter.value !== 'all' && (item.kind ?? 'workflow') !== workflowKindFilter.value) return false
  if (workflowStatusFilter.value === 'draft' && item.latest_version) return false
  if (workflowStatusFilter.value === 'published' && !item.latest_version) return false
  return true
}

const filteredWorkflowDocuments = computed(() => {
  const query = workflowQuery.value.trim().toLocaleLowerCase()
  return props.workflowDocuments.filter((item) => {
    if (!matchesWorkflowFacets(item)) return false
    if (!query) return true
    return [item.name, item.slug, item.description, item.created_by]
      .some(value => value?.toLocaleLowerCase().includes(query))
  })
})

const selectedWorkflowMatchesFilters = computed(() => {
  const selected = props.workflowDocuments.find(item => item.slug === props.workflowSlug)
  return !selected || matchesWorkflowFacets(selected)
})

const workflowGroups = computed(() => {
  const groups = [
    {
      id: 'my-subworkflows',
      label: '我的子流程',
      owner: 'mine',
      items: filteredWorkflowDocuments.value.filter(item => item.is_mine && item.kind === 'subworkflow'),
    },
    {
      id: 'my-workflows',
      label: '我的流程',
      owner: 'mine',
      items: filteredWorkflowDocuments.value.filter(item => item.is_mine && item.kind !== 'subworkflow'),
    },
    {
      id: 'shared',
      label: '其他可用流程',
      owner: 'shared',
      items: filteredWorkflowDocuments.value.filter(item => !item.is_mine),
    },
  ]
  return groups.filter((group) => {
    if (workflowOwnerFilter.value !== 'all' && group.owner !== workflowOwnerFilter.value) return false
    if (group.items.length > 0) return true
    return group.id === 'my-subworkflows'
      && workflowOwnerFilter.value !== 'shared'
      && workflowKindFilter.value !== 'workflow'
      && !workflowQuery.value.trim()
  })
})

const hasWorkflowFilters = computed(() =>
  Boolean(workflowQuery.value.trim())
  || workflowOwnerFilter.value !== 'all'
  || workflowKindFilter.value !== 'all'
  || workflowStatusFilter.value !== 'all',
)

function resetWorkflowFilters() {
  workflowQuery.value = ''
  workflowOwnerFilter.value = 'all'
  workflowKindFilter.value = 'all'
  workflowStatusFilter.value = 'all'
}

function workflowUpdatedLabel(updatedAt?: string) {
  if (!updatedAt) return ''
  const date = new Date(updatedAt)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
  }).format(date)
}

const editingMetadata = defineModel<boolean>('editingMetadata', { required: true })
const workflowName = defineModel<string>('workflowName', { required: true })
const workflowKind = defineModel<'workflow' | 'subworkflow'>('workflowKind', { required: true })
const workflowDescription = defineModel<string>('workflowDescription', { required: true })
const editingWdl = defineModel<boolean>('editingWdl', { required: true })
const wdlDraft = defineModel<string>('wdlDraft', { required: true })
const wdlGraphConfirmations = defineModel<string[]>('wdlGraphConfirmations', { required: true })

const proposalSectionLabels: Record<string, string> = {
  workflow_structure: '画布结构',
  tool_versions: '工具固定内容',
  instance_parameters: '当前节点配置',
}

const proposalSectionDescriptions: Record<string, string> = {
  workflow_structure: '更新节点、连线或流程输入输出，只写入当前画布草稿。',
  tool_versions: '命令、容器、接口或运行规则变化会生成待发布工具草稿，不覆盖现有版本。',
  instance_parameters: '只调整本流程中的参数值或注释选择，工具版本保持不变。',
}

const allProposalSectionsConfirmed = computed(() =>
  props.wdlGraphProposal?.required_confirmations.every(
    section => wdlGraphConfirmations.value.includes(section),
  ) ?? false,
)

const runVersionLink = computed(() => {
  const version = props.versionSnapshot?.version
    ?? (props.selectedWdlRevision?.executable ? props.selectedWdlRevision.workflow_version : undefined)
  if (!version) return ''
  return `/runs?workflow=${encodeURIComponent(props.workflowSlug)}&revision=${version}`
})

const wdlDownloadName = computed(() => {
  const workflowVersion = props.versionSnapshot?.version
    ?? props.selectedWdlRevision?.workflow_version
  const revision = props.versionSnapshot ? undefined : props.selectedWdlRevision?.version
  return [
    props.workflowSlug || 'workflow',
    workflowVersion ? `v${workflowVersion}` : 'draft',
    revision ? `wdl-v${revision}` : '',
  ].filter(Boolean).join('-') + '.wdl'
})

const activeWorkflowGraph = computed(() => props.versionSnapshot?.workflow_graph ?? props.workflowGraph ?? {})
const workflowGraphNodes = computed<Record<string, any>[]>(() => (
  Array.isArray(activeWorkflowGraph.value?.nodes) ? activeWorkflowGraph.value.nodes : []
))
const workflowGraphEdges = computed<Record<string, any>[]>(() => (
  Array.isArray(activeWorkflowGraph.value?.edges) ? activeWorkflowGraph.value.edges : []
))
const canCreatePackageFromCanvas = computed(() => (
  !props.versionSnapshot && workflowGraphNodes.value.some(node => node.type === 'tool')
))
const workflowStages = computed(() => [
  {
    id: 'inputs',
    label: '输入',
    items: workflowGraphNodes.value.filter(node => node.type === 'workflow_input'),
  },
  {
    id: 'tools',
    label: '工具',
    items: workflowGraphNodes.value.filter(node => node.type === 'tool'),
  },
  {
    id: 'subworkflows',
    label: '子流程',
    items: workflowGraphNodes.value.filter(node => node.type === 'subworkflow'),
  },
  {
    id: 'outputs',
    label: '输出',
    items: workflowGraphNodes.value.filter(node => node.type === 'workflow_output'),
  },
].filter(stage => stage.items.length))

function workflowNodeLabel(node: Record<string, any>) {
  return node.label
    || node.tool_ref?.id
    || node.subworkflow_ref?.slug
    || node.port?.label
    || node.id
}

function workflowNodeVersion(node: Record<string, any>) {
  if (node.type === 'tool') return node.tool_ref?.tool_version ? `v${node.tool_ref.tool_version}` : ''
  if (node.type === 'subworkflow') return node.subworkflow_ref?.version ? `v${node.subworkflow_ref.version}` : ''
  return node.port?.wdl_type ?? ''
}

function proposalSectionLabel(section: string) {
  return proposalSectionLabels[section] ?? section
}

function proposalSectionDescription(section: string) {
  return proposalSectionDescriptions[section] ?? ''
}

function proposalDiffValue(value: unknown) {
  if (value === undefined || value === null || value === '') return '未设置'
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

function wdlRoleLabel(revision?: WdlRevision) {
  if (revision?.artifact_role === 'compiled_snapshot') return '编译快照'
  if (revision?.source === 'system' && revision.workflow_version) return '编译快照'
  return '派生稿'
}

const emit = defineEmits<{
  openEditor: []
  saveMetadata: []
  selectWorkflow: [slug: string]
  selectCompilation: [id: string]
  selectWdlVersion: [version: number]
  beginWdlEdit: []
  cancelWdlEdit: []
  createWdlAsset: []
  generateWdlGraphProposal: []
  applyWdlGraphProposal: []
  dismissWdlGraphProposal: []
  saveWdl: []
  copyWdl: []
  createWorkflow: [payload: {
    slug: string
    name: string
    description: string
    kind: 'workflow' | 'subworkflow'
  }]
  createRequestHandled: []
  retryLibrary: []
}>()

function beginDerivedWdlEdit() {
  showWdlDestinations.value = false
  emit('beginWdlEdit')
}

function beginCreate(kind: 'workflow' | 'subworkflow') {
  createDraft.value = { slug: '', name: '', description: '', kind }
  showCreatePanel.value = true
  void nextTick(() => document.querySelector<HTMLInputElement>('#workflow-create-name')?.focus())
}

function submitCreate() {
  emit('createWorkflow', {
    slug: createDraft.value.slug.trim(),
    name: createDraft.value.name.trim(),
    description: createDraft.value.description.trim(),
    kind: createDraft.value.kind,
  })
}

watch(() => props.createState, (state) => {
  if (state !== 'success') return
  showCreatePanel.value = false
  createDraft.value = { slug: '', name: '', description: '', kind: 'workflow' }
})

watch(() => props.createRequest, (kind) => {
  if (!kind) return
  beginCreate(kind)
  emit('createRequestHandled')
}, { immediate: true })

watch(() => props.initialOwnerFilter, (value) => {
  if (value) workflowOwnerFilter.value = value
}, { immediate: true })

watch(() => props.initialKindFilter, (value) => {
  if (value) workflowKindFilter.value = value
}, { immediate: true })

watch(
  () => [
    props.libraryState,
    workflowOwnerFilter.value,
    workflowKindFilter.value,
    workflowStatusFilter.value,
    props.workflowDocuments.length,
  ] as const,
  () => {
    if (
      props.libraryState !== 'ready'
      || props.isWorkflowSwitching
      || props.saveState === 'saving'
      || props.compileState === 'running'
      || editingWdl.value
    ) return
    const currentVisible = filteredWorkflowDocuments.value.some(item => item.slug === props.workflowSlug)
    const next = filteredWorkflowDocuments.value[0]
    if (!currentVisible && next) emit('selectWorkflow', next.slug)
  },
  { flush: 'post' },
)

watch(() => [props.workflowSlug, props.selectedWdlVersion], () => {
  showWdlDestinations.value = false
})
</script>

<template>
  <header class="workspace-header">
    <div>
      <h1>流程库</h1>
      <p>从画布创建流程和子流程；发布后保留固定版本与可追踪的 WDL。</p>
    </div>
    <div class="workspace-header__actions workflow-create-actions">
      <button class="button button--ghost" type="button" @click="beginCreate('subworkflow')">新建子流程</button>
      <button class="button button--ghost" type="button" @click="beginCreate('workflow')">新建流程</button>
      <button class="button button--primary" type="button" @click="emit('openEditor')">编辑当前流程</button>
    </div>
  </header>

  <form v-if="showCreatePanel" class="workflow-create-panel" @submit.prevent="submitCreate">
    <div class="workflow-create-panel__heading">
      <div>
        <strong>{{ createDraft.kind === 'subworkflow' ? '新建子流程' : '新建流程' }}</strong>
        <span>创建后直接进入画布。</span>
      </div>
      <button type="button" class="text-button" @click="showCreatePanel = false">取消</button>
    </div>
    <label class="field">
      <span>名称</span>
      <input id="workflow-create-name" v-model="createDraft.name" required autocomplete="off" placeholder="例如 质控与过滤" />
    </label>
    <label class="field">
      <span>流程 ID</span>
      <input v-model="createDraft.slug" required autocomplete="off" pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder="例如 qc_filter" />
      <small>用于 WDL 标识，创建后保持不变。</small>
    </label>
    <label class="field workflow-create-panel__description">
      <span>说明</span>
      <input v-model="createDraft.description" autocomplete="off" placeholder="这个流程解决什么问题" />
    </label>
    <p v-if="createError" class="inline-error" role="alert">{{ createError }}</p>
    <button class="button button--primary" type="submit" :disabled="createState === 'saving'">
      {{ createState === 'saving' ? '正在创建…' : '创建并打开画布' }}
    </button>
  </form>

  <div class="flow-library">
    <aside class="version-sidebar">
      <div class="version-sidebar__title">
        <div>
          <strong>{{ workflowName }}</strong>
          <small>{{ workflowSlug }}</small>
        </div>
        <span>{{ workflowKind === 'subworkflow' ? '子流程' : '流程' }}</span>
      </div>
      <button class="metadata-edit" type="button" @click="editingMetadata = !editingMetadata">
        {{ editingMetadata ? '收起信息' : '编辑名称与说明' }}
      </button>
      <form v-if="editingMetadata" class="metadata-form" @submit.prevent="emit('saveMetadata')">
        <label class="field"><span>名称</span><input v-model="workflowName" /></label>
        <label class="field">
          <span>类型</span>
          <select v-model="workflowKind">
            <option value="workflow">流程</option>
            <option value="subworkflow">子流程</option>
          </select>
        </label>
        <label class="field"><span>说明</span><textarea v-model="workflowDescription" rows="3" /></label>
        <button class="button button--primary" type="submit">保存信息</button>
      </form>

      <section v-if="libraryState === 'ready'" class="workflow-index-controls" aria-label="筛选流程">
        <label class="workflow-index-search">
          <span aria-hidden="true">⌕</span>
          <input v-model="workflowQuery" type="search" placeholder="搜索流程或 ID" aria-label="搜索流程或 ID" />
        </label>
        <div class="workflow-owner-filter" role="group" aria-label="流程归属">
          <button
            v-for="option in ([
              { value: 'all', label: '全部' },
              { value: 'mine', label: '我的' },
              { value: 'shared', label: '共享' },
            ] as const)"
            :key="option.value"
            type="button"
            :aria-pressed="workflowOwnerFilter === option.value"
            :class="{ 'is-active': workflowOwnerFilter === option.value }"
            @click="workflowOwnerFilter = option.value"
          >{{ option.label }}</button>
        </div>
        <div class="workflow-select-filters">
          <label>
            <span class="visually-hidden">流程类型</span>
            <select v-model="workflowKindFilter" aria-label="流程类型">
              <option value="all">全部类型</option>
              <option value="workflow">流程</option>
              <option value="subworkflow">子流程</option>
            </select>
          </label>
          <label>
            <span class="visually-hidden">发布状态</span>
            <select v-model="workflowStatusFilter" aria-label="发布状态">
              <option value="all">全部状态</option>
              <option value="draft">草稿</option>
              <option value="published">已发布</option>
            </select>
          </label>
        </div>
        <div class="workflow-index-controls__summary">
          <span>{{ filteredWorkflowDocuments.length }} / {{ workflowDocuments.length }}</span>
          <button v-if="hasWorkflowFilters" type="button" class="text-button" @click="resetWorkflowFilters">清除筛选</button>
        </div>
      </section>

      <div
        class="workflow-index"
        :aria-busy="libraryState === 'loading' || isWorkflowSwitching"
        aria-live="polite"
      >
        <div v-if="libraryState === 'loading'" class="workflow-index__empty workflow-index__empty--status">
          <strong>正在载入流程…</strong>
          <span>正在读取你可以维护的流程与子流程。</span>
        </div>
        <div v-else-if="libraryState === 'error'" class="workflow-index__empty workflow-index__empty--status" role="alert">
          <strong>流程列表载入失败</strong>
          <span>{{ libraryError || '请检查连接后重试。' }}</span>
          <button type="button" class="text-button" @click="emit('retryLibrary')">重新加载</button>
        </div>
        <template v-else>
        <section v-for="group in workflowGroups" :key="group.id" class="workflow-index__group">
          <h2>{{ group.label }} <span>{{ group.items.length }}</span></h2>
          <button
            v-for="document in group.items"
            :key="document.slug"
            type="button"
            :class="{ 'workflow-index__active': document.slug === workflowSlug }"
            :aria-current="document.slug === workflowSlug ? 'true' : undefined"
            :disabled="isWorkflowSwitching || saveState === 'saving' || compileState === 'running' || editingWdl || wdlGraphProposalState === 'applying'"
            @click="emit('selectWorkflow', document.slug)"
          >
            <span>
              <strong>{{ document.name }}</strong>
              <small>{{ document.description || document.slug }}</small>
            </span>
            <em>
              {{
                switchingWorkflowSlug === document.slug
                  ? '正在打开…'
                  : `${document.kind === 'subworkflow' ? '子流程' : '流程'} · ${document.latest_version ? `已发布 v${document.latest_version}` : '草稿'}${workflowUpdatedLabel(document.updated_at) ? ` · ${workflowUpdatedLabel(document.updated_at)}` : ''}`
              }}
            </em>
          </button>
          <div v-if="group.id === 'my-subworkflows' && group.items.length === 0" class="workflow-index__group-empty">
            <span>还没有自己的子流程</span>
            <button type="button" class="text-button" @click="beginCreate('subworkflow')">新建子流程</button>
          </div>
        </section>
        <div v-if="workflowGroups.length === 0" class="workflow-index__empty workflow-index__empty--filtered">
          <template v-if="hasWorkflowFilters">
            <strong>没有匹配的流程</strong>
            <span>调整搜索词或筛选条件。</span>
            <button type="button" class="text-button" @click="resetWorkflowFilters">清除筛选</button>
          </template>
          <template v-else>
            <strong>还没有自己的流程</strong>
            <span>先创建一个子流程，定义可复用的输入、工具与输出。</span>
            <button type="button" class="button button--ghost" @click="beginCreate('subworkflow')">新建子流程</button>
          </template>
        </div>
        </template>
      </div>

      <h2>编译版本</h2>
      <button
        v-for="version in compilationVersions"
        :key="version.id"
        type="button"
        class="version-row"
        :class="{ 'version-row--active': selectedCompilationId === version.id }"
        :disabled="editingWdl"
        @click="emit('selectCompilation', version.id)"
      >
        <span>
          <strong>{{ version.version }}</strong>
          <small>{{ version.createdAt }}</small>
        </span>
        <span>{{ version.status === 'succeeded' ? '通过' : '失败' }}</span>
      </button>
      <div v-if="compilationVersions.length === 0" class="version-empty">
        <strong>还没有编译版本</strong>
        <p>回到编辑器验证并编译，首个版本会出现在这里。</p>
        <button class="button button--ghost" type="button" @click="emit('openEditor')">去编译</button>
      </div>
    </aside>

    <section v-if="selectedWorkflowMatchesFilters" class="wdl-preview">
      <div v-if="versionSnapshot" class="published-version-banner">
        <div>
          <strong>v{{ versionSnapshot.version }} 只读发布快照</strong>
          <span>{{ versionSnapshot.name }}</span>
        </div>
        <code>{{ versionSnapshot.semantic_digest }}</code>
      </div>
      <section class="workflow-map-summary" aria-label="流程画布结构摘要">
        <header>
          <div>
            <span>{{ versionSnapshot ? `发布版本 v${versionSnapshot.version}` : '当前画布' }}</span>
            <h2>{{ workflowName }}</h2>
            <p>{{ workflowDescription || '尚未填写流程说明。' }}</p>
          </div>
          <div class="workflow-map-summary__actions">
            <NuxtLink
              v-if="canCreatePackageFromCanvas"
              class="button button--ghost button-link"
              :to="`/wdl-packages?from=editor&workflow=${encodeURIComponent(workflowSlug)}`"
            >创建工具包</NuxtLink>
            <NuxtLink
              v-if="runVersionLink"
              class="button button--ghost button-link"
              :to="runVersionLink"
            >运行与记录</NuxtLink>
            <button class="button button--primary" type="button" @click="emit('openEditor')">
              {{ versionSnapshot ? '打开当前画布' : '编辑画布' }}
            </button>
          </div>
        </header>
        <div v-if="workflowStages.length" class="workflow-map-summary__stages">
          <section v-for="stage in workflowStages" :key="stage.id">
            <header><strong>{{ stage.label }}</strong><span>{{ stage.items.length }}</span></header>
            <ul>
              <li v-for="node in stage.items.slice(0, 6)" :key="node.id">
                <span>{{ workflowNodeLabel(node) }}</span>
                <code v-if="workflowNodeVersion(node)">{{ workflowNodeVersion(node) }}</code>
              </li>
            </ul>
            <small v-if="stage.items.length > 6">另有 {{ stage.items.length - 6 }} 项</small>
          </section>
        </div>
        <div v-else class="workflow-map-summary__empty">
          画布还没有节点。打开编辑器，从输入、工具或子流程开始。
        </div>
        <footer>
          <span>{{ workflowGraphNodes.length }} 个节点 · {{ workflowGraphEdges.length }} 条连接</span>
          <span>结构以画布为准；WDL 修改需先确认变更提案。</span>
        </footer>
      </section>
      <header>
        <div>
          <strong>workflow.wdl</strong>
          <small>
            <template v-if="versionSnapshot">
              发布快照 v{{ versionSnapshot.version }} · {{ versionSnapshot.compiler_profile || '旧版编译器' }}
            </template>
            <template v-else>
              WDL 修订 v{{ selectedWdlRevision?.version ?? '—' }}
              <template v-if="selectedWdlRevision?.base_wdl_revision"> · 基于 WDL v{{ selectedWdlRevision.base_wdl_revision }}</template>
              · 基于流程 {{ selectedWdlRevision?.workflow_version ? `v${selectedWdlRevision.workflow_version}` : (selectedCompilationVersion ?? '未发布') }}
              · {{ wdlRoleLabel(selectedWdlRevision) }}{{ wdlRoleLabel(selectedWdlRevision) === '编译快照' ? '' : '（不参与运行）' }}
              <template v-if="selectedWdlRevision?.created_by"> · {{ selectedWdlRevision.created_by }}</template>
            </template>
          </small>
        </div>
        <div class="preview-actions">
          <select
            v-if="wdlRevisions.length && !versionSnapshot"
            :value="selectedWdlVersion"
            aria-label="选择 WDL 版本"
            :disabled="editingWdl || wdlRevisionLoadState === 'loading'"
            @change="emit('selectWdlVersion', Number(($event.target as HTMLSelectElement).value))"
          >
            <option v-for="revision in wdlRevisions" :key="revision.version" :value="revision.version">
              WDL v{{ revision.version }} · {{ wdlRoleLabel(revision) }}
            </option>
          </select>
          <span
            v-if="selectedWdlRevision && !versionSnapshot"
            class="source-tag"
            :class="wdlRoleLabel(selectedWdlRevision) === '编译快照' ? 'source-tag--system' : 'source-tag--manual'"
          >
            {{ wdlRoleLabel(selectedWdlRevision) }}
          </span>
          <button
            v-if="!editingWdl && !versionSnapshot && !showWdlDestinations"
            class="button button--ghost"
            type="button"
            :disabled="!activeWdlContent || wdlRevisionLoadState !== 'idle'"
            @click="showWdlDestinations = true"
          >
            修改 WDL
          </button>
          <button
            v-if="!editingWdl && !versionSnapshot && selectedWdlRevision?.artifact_role === 'derived_draft'"
            class="button button--ghost"
            type="button"
            :disabled="wdlGraphProposalState === 'loading' || wdlRevisionLoadState !== 'idle'"
            @click="emit('generateWdlGraphProposal')"
          >{{ wdlGraphProposalState === 'loading' ? '检查中…' : '检查画布影响' }}</button>
          <button
            v-if="editingWdl"
            class="button button--primary"
            type="button"
            :disabled="wdlSaveState === 'saving'"
            @click="emit('saveWdl')"
          >
            {{ wdlSaveState === 'saving' ? '保存中…' : '保存派生稿' }}
          </button>
          <button
            v-if="editingWdl"
            class="button button--ghost"
            type="button"
            :disabled="wdlSaveState === 'saving'"
            @click="emit('cancelWdlEdit')"
          >取消编辑</button>
          <button
            class="button button--ghost"
            type="button"
            :disabled="!activeWdlContent"
            @click="emit('copyWdl')"
          >
            {{ copiedArtifact ? '已复制' : '复制' }}
          </button>
          <a
            v-if="activeWdlContent"
            class="button button--ghost button-link"
            :href="`data:text/plain;charset=utf-8,${encodeURIComponent(activeWdlContent)}`"
            :download="wdlDownloadName"
          >下载 WDL</a>
        </div>
      </header>

      <section v-if="showWdlDestinations && !editingWdl" class="wdl-destination-panel" aria-label="选择 WDL 修改去向">
        <div class="wdl-destination-panel__heading">
          <div>
            <strong>这次 WDL 修改保存到哪里？</strong>
            <p>画布和已发布 WorkflowVersion 不会随手工 WDL 自动变化。</p>
          </div>
          <button class="text-button" type="button" @click="showWdlDestinations = false">取消</button>
        </div>
        <div class="wdl-destination-panel__actions">
          <button type="button" :disabled="wdlRevisionLoadState !== 'idle'" @click="beginDerivedWdlEdit">
            <strong>创建派生稿</strong>
            <span>留在当前流程下用于比较或试验，不参与运行。</span>
          </button>
          <button
            type="button"
            :disabled="wdlAssetCreateState === 'saving' || wdlRevisionLoadState !== 'idle'"
            @click="emit('createWdlAsset')"
          >
            <strong>{{ wdlAssetCreateState === 'saving' ? '正在创建历史资产…' : '转为历史 WDL 资产' }}</strong>
            <span>进入 WDL 工作台独立维护，并保留来源流程与版本。</span>
          </button>
        </div>
        <p v-if="wdlAssetCreateState === 'error'" class="inline-error" role="alert">
          {{ wdlAssetCreateError }}
        </p>
      </section>

      <textarea
        v-if="editingWdl"
        v-model="wdlDraft"
        class="wdl-editor"
        aria-label="编辑 WDL 内容"
        spellcheck="false"
      />
      <p v-if="editingWdl" class="wdl-edit-scope-note">
        派生稿不会更改画布、工具版本或运行使用的 WorkflowVersion。需要长期直接维护 WDL 时，请转为历史 WDL 资产。
      </p>
      <p v-else-if="selectedWdlRevision && wdlRoleLabel(selectedWdlRevision) === '派生稿'" class="wdl-edit-scope-note">
        当前内容是不可运行的派生稿；运行仍使用 WorkflowVersion v{{ selectedWdlRevision.workflow_version ?? '—' }} 的固定编译包。
      </p>
      <section
        v-if="wdlGraphProposal || wdlGraphProposalState === 'error'"
        class="wdl-graph-proposal"
        aria-label="WDL 对画布的变更提案"
      >
        <header>
          <div>
            <strong>WDL 对画布的影响</strong>
            <span v-if="wdlGraphProposal?.status === 'blocked'">需要人工处理</span>
            <span v-else-if="wdlGraphProposal?.required_confirmations.length === 0">与当前画布一致</span>
            <span v-else>确认后才会写入画布草稿</span>
          </div>
          <button class="text-button" type="button" @click="emit('dismissWdlGraphProposal')">关闭</button>
        </header>
        <p v-if="wdlGraphProposalState === 'error'" class="inline-error" role="alert">
          {{ wdlGraphProposalError }}
        </p>
        <ul v-if="wdlGraphProposal?.status === 'blocked'" class="wdl-graph-proposal__issues">
          <li v-for="issue in wdlGraphProposal.blocking_issues" :key="issue">{{ issue }}</li>
        </ul>
        <template v-else-if="wdlGraphProposal">
          <div class="wdl-graph-proposal__summary">
            <span>画布变更 {{ wdlGraphProposal.summary.workflow_change_count }}</span>
            <span>新工具草稿 {{ wdlGraphProposal.summary.tool_draft_count }}</span>
            <span>节点配置 {{ wdlGraphProposal.summary.instance_change_count }}</span>
          </div>
          <ul v-if="wdlGraphProposal.tool_drafts.length" class="wdl-graph-proposal__drafts">
            <li v-for="draft in wdlGraphProposal.tool_drafts" :key="draft.tool_id">
              <div>
                <strong>{{ draft.tool_id }}</strong>
                <code>{{ draft.base_version || '新工具' }} → {{ draft.proposed_version }}</code>
              </div>
              <details v-if="draft.field_diffs?.length">
                <summary>查看 {{ draft.field_diffs.length }} 项固定内容差异</summary>
                <section v-for="diff in draft.field_diffs ?? []" :key="diff.field">
                  <strong>{{ diff.label }}</strong>
                  <div class="wdl-graph-proposal__field-values">
                    <div><span>当前</span><pre>{{ proposalDiffValue(diff.before) }}</pre></div>
                    <div><span>WDL 提议</span><pre>{{ proposalDiffValue(diff.after) }}</pre></div>
                  </div>
                </section>
              </details>
            </li>
          </ul>
          <div
            v-for="section in wdlGraphProposal.required_confirmations"
            :key="section"
            class="wdl-graph-proposal__section"
          >
            <label>
              <input v-model="wdlGraphConfirmations" type="checkbox" :value="section" />
              <strong>确认{{ proposalSectionLabel(section) }}</strong>
            </label>
            <p>{{ proposalSectionDescription(section) }}</p>
            <ul>
              <li
                v-for="row in wdlGraphProposal.changes[section]"
                :key="`${row.kind}:${row.subject}:${row.detail}`"
              >
                <span>{{ row.subject }}</span>
                <small>{{ row.detail }}</small>
              </li>
            </ul>
          </div>
          <p v-for="warning in wdlGraphProposal.warnings" :key="warning" class="wdl-graph-proposal__warning">
            {{ warning }}
          </p>
          <button
            v-if="wdlGraphProposal.required_confirmations.length"
            class="button button--primary wdl-graph-proposal__apply"
            type="button"
            :disabled="!allProposalSectionsConfirmed || wdlGraphProposalState === 'applying'"
            @click="emit('applyWdlGraphProposal')"
          >{{ wdlGraphProposalState === 'applying' ? '正在应用…' : '应用到画布草稿' }}</button>
        </template>
      </section>
      <p v-if="wdlRevisionLoadState === 'loading'" class="preview-empty" role="status">正在读取 WDL 修订…</p>
      <p v-else-if="wdlRevisionLoadState === 'error'" class="inline-error" role="alert">WDL 修订读取失败，请重新选择该版本。</p>
      <div v-else-if="activeWdlContent" class="code-viewer" aria-label="WDL 只读预览" tabindex="0">
        <div v-for="(line, index) in previewLines" :key="index" class="code-line">
          <span aria-hidden="true">{{ index + 1 }}</span>
          <code>{{ line || ' ' }}</code>
        </div>
      </div>
      <p v-if="wdlSaveState === 'error'" class="inline-error">WDL 保存或语法校验失败，请检查后重试。</p>
      <div v-if="!editingWdl && !activeWdlContent && wdlRevisionLoadState === 'idle'" class="preview-empty">
        <template v-if="versionSnapshot">
          <strong>该历史版本没有归档编译包</strong>
          <p>这是编译包归档功能上线前发布的版本，无法恢复当时的精确 WDL。请从当前草稿重新发布新版本。</p>
        </template>
        <template v-else>
          <strong>WDL 预览将在编译后显示</strong>
          <p>这里会保留原始换行、提供行号，并支持复制和下载。</p>
        </template>
      </div>
    </section>
    <section v-else class="wdl-preview">
      <div class="preview-empty">
        <strong>当前筛选没有可打开的流程</strong>
        <p>从左侧选择一个匹配项，或调整归属、类型和发布状态。</p>
        <button
          v-if="workflowOwnerFilter !== 'shared' && workflowKindFilter !== 'workflow'"
          class="button button--primary"
          type="button"
          @click="beginCreate('subworkflow')"
        >新建子流程</button>
      </div>
    </section>
  </div>
</template>
