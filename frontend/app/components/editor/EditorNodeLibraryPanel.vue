<script setup lang="ts">
type LibraryTab = 'tools' | 'subworkflows' | 'inputs' | 'outputs'

interface LibraryItem {
  id: string
  name: string
  description: string
}

interface OwnedSubworkflow {
  slug: string
  name: string
  description?: string
  latest_version: number | null
}

const activeLibrary = defineModel<LibraryTab>('activeLibrary', { required: true })
const searchQuery = defineModel<string>('searchQuery', { required: true })
const props = defineProps<{
  currentWorkflowName: string
  currentWorkflowKind: 'workflow' | 'subworkflow'
  currentWorkflowPublished: boolean
  canCreateToolPackage: boolean
  subworkflowGuide: {
    inputReady: boolean
    implementationReady: boolean
    outputReady: boolean
    connected: boolean
  }
  ownedSubworkflows: OwnedSubworkflow[]
  tools: Array<Record<string, any>>
  subworkflows: Array<Record<string, any>>
  workflowInputs: LibraryItem[]
  workflowOutputs: LibraryItem[]
  toolRegistryLoaded: boolean
  isWorkflowSwitching: boolean
  createState: 'idle' | 'saving' | 'success' | 'error'
  createError: string
}>()

const emit = defineEmits<{
  quickAdd: [payload: any]
  dragStart: [event: DragEvent, payload: any]
  dragEnd: []
  importToolSpec: []
  openToolPackages: []
  prepareCreate: []
  openOwnedSubworkflows: []
  openWorkflow: [slug: string]
  createWorkflow: [payload: {
    slug: string
    name: string
    description: string
    kind: 'workflow' | 'subworkflow'
  }]
  validateWorkflow: []
}>()

const showCreatePanel = ref(false)
const createDraft = ref({
  slug: '',
  name: '',
  description: '',
  kind: 'workflow' as 'workflow' | 'subworkflow',
})

const unpublishedSubworkflows = computed(() =>
  props.ownedSubworkflows.filter(item => !item.latest_version),
)
const showOnlyMySubworkflows = ref(false)
const ownedSubworkflowSlugs = computed(() =>
  new Set(props.ownedSubworkflows.map(item => item.slug)),
)
const publishedOwnedSubworkflowCount = computed(() =>
  new Set(
    props.subworkflows
      .filter(item => ownedSubworkflowSlugs.value.has(item.slug))
      .map(item => item.slug),
  ).size,
)
const visibleSubworkflows = computed(() =>
  showOnlyMySubworkflows.value
    ? props.subworkflows.filter(item => ownedSubworkflowSlugs.value.has(item.slug))
    : props.subworkflows,
)
const subworkflowGuideCompleted = computed(() => Object.values(props.subworkflowGuide).filter(Boolean).length)

function beginCreate(kind: 'workflow' | 'subworkflow') {
  emit('prepareCreate')
  createDraft.value = { slug: '', name: '', description: '', kind }
  showCreatePanel.value = true
  void nextTick(() => document.querySelector<HTMLInputElement>('#canvas-workflow-create-name')?.focus())
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
</script>

<template>
  <aside class="library-panel">
    <section class="canvas-workflow-context" aria-label="当前流程">
      <div>
        <span>{{ currentWorkflowKind === 'subworkflow' ? '当前子流程' : '当前流程' }}</span>
        <strong>{{ currentWorkflowName }}</strong>
      </div>
      <div class="canvas-workflow-context__actions">
        <button
          type="button"
          class="text-button"
          :class="{ 'canvas-workflow-context__library': canCreateToolPackage }"
          @click="emit('openOwnedSubworkflows')"
        >
          我的子流程 {{ ownedSubworkflows.length }}
        </button>
        <button type="button" class="text-button" @click="beginCreate('subworkflow')">新建子流程</button>
        <button
          v-if="canCreateToolPackage"
          type="button"
          class="text-button"
          @click="emit('openToolPackages')"
        >创建工具包</button>
      </div>
    </section>

    <section
      v-if="currentWorkflowKind === 'subworkflow' && !currentWorkflowPublished"
      class="canvas-subworkflow-guide"
      aria-label="子流程画布准备"
    >
      <header>
        <strong>子流程草稿</strong>
        <span>{{ subworkflowGuideCompleted }}/4</span>
      </header>
      <ol>
        <li :class="{ 'is-complete': subworkflowGuide.inputReady }">
          <button type="button" @click="activeLibrary = 'inputs'">
            <span aria-hidden="true">{{ subworkflowGuide.inputReady ? '✓' : '1' }}</span>定义输入
          </button>
        </li>
        <li :class="{ 'is-complete': subworkflowGuide.implementationReady }">
          <button type="button" @click="activeLibrary = 'tools'">
            <span aria-hidden="true">{{ subworkflowGuide.implementationReady ? '✓' : '2' }}</span>添加处理节点
          </button>
        </li>
        <li :class="{ 'is-complete': subworkflowGuide.outputReady }">
          <button type="button" @click="activeLibrary = 'outputs'">
            <span aria-hidden="true">{{ subworkflowGuide.outputReady ? '✓' : '3' }}</span>定义输出
          </button>
        </li>
        <li :class="{ 'is-complete': subworkflowGuide.connected }">
          <button type="button" @click="emit('validateWorkflow')">
            <span aria-hidden="true">{{ subworkflowGuide.connected ? '✓' : '4' }}</span>连接并验证
          </button>
        </li>
      </ol>
    </section>

    <form v-if="showCreatePanel" class="canvas-workflow-create" @submit.prevent="submitCreate">
      <div class="canvas-workflow-create__heading">
        <strong>{{ createDraft.kind === 'subworkflow' ? '新建子流程' : '新建流程' }}</strong>
        <button type="button" class="text-button" @click="showCreatePanel = false">取消</button>
      </div>
      <div class="canvas-workflow-create__kind" role="group" aria-label="流程类型">
        <button
          type="button"
          :aria-pressed="createDraft.kind === 'workflow'"
          :class="{ 'is-active': createDraft.kind === 'workflow' }"
          @click="createDraft.kind = 'workflow'"
        >流程</button>
        <button
          type="button"
          :aria-pressed="createDraft.kind === 'subworkflow'"
          :class="{ 'is-active': createDraft.kind === 'subworkflow' }"
          @click="createDraft.kind = 'subworkflow'"
        >子流程</button>
      </div>
      <label class="field">
        <span>名称</span>
        <input id="canvas-workflow-create-name" v-model="createDraft.name" required autocomplete="off" placeholder="例如 质控与过滤" />
      </label>
      <label class="field">
        <span>流程 ID</span>
        <input v-model="createDraft.slug" required autocomplete="off" pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder="例如 qc_filter" />
      </label>
      <label class="field">
        <span>说明</span>
        <input v-model="createDraft.description" autocomplete="off" placeholder="这个流程解决什么问题" />
      </label>
      <p v-if="createError" class="inline-error" role="alert">{{ createError }}</p>
      <button class="button button--primary" type="submit" :disabled="createState === 'saving' || isWorkflowSwitching">
        {{ createState === 'saving' ? '正在创建…' : '创建并打开画布' }}
      </button>
    </form>

    <div class="panel-tabs" role="tablist" aria-label="节点库">
      <button
        v-for="tab in (['tools', 'subworkflows', 'inputs', 'outputs'] as LibraryTab[])"
        :key="tab"
        type="button"
        role="tab"
        :aria-selected="activeLibrary === tab"
        :class="{ 'panel-tab--active': activeLibrary === tab }"
        @click="activeLibrary = tab"
      >
        {{ { tools: '工具', subworkflows: '子流程', inputs: '输入', outputs: '输出' }[tab] }}
      </button>
    </div>

    <div class="library-panel__body">
      <label class="search-field">
        <span class="visually-hidden">搜索节点</span>
        <span aria-hidden="true">⌕</span>
        <input v-model="searchQuery" type="search" placeholder="搜索工具或类型" />
        <kbd>⌘ K</kbd>
      </label>

      <div class="section-heading">
        <h2>
          {{
            activeLibrary === 'tools' ? '可用工具'
              : activeLibrary === 'subworkflows' ? '已发布子流程'
                : activeLibrary === 'inputs' ? 'Workflow 输入'
                  : 'Workflow 输出'
          }}
        </h2>
        <span>
          {{
            activeLibrary === 'tools' ? tools.length
              : activeLibrary === 'subworkflows' ? visibleSubworkflows.length
                : activeLibrary === 'inputs' ? workflowInputs.length
                  : workflowOutputs.length
          }}
        </span>
      </div>
      <p class="library-hint">单击快速添加到画布中心，也可以拖到指定位置。</p>

      <section v-if="activeLibrary === 'subworkflows'" class="owned-subworkflow-summary">
        <div>
          <span>我的子流程</span>
          <strong>{{ ownedSubworkflows.length }}</strong>
          <small>{{ publishedOwnedSubworkflowCount }} 个已发布 · {{ unpublishedSubworkflows.length }} 个草稿</small>
        </div>
        <div class="owned-subworkflow-summary__actions">
          <button
            type="button"
            class="text-button"
            :class="{ 'is-active': showOnlyMySubworkflows }"
            :aria-pressed="showOnlyMySubworkflows"
            @click="showOnlyMySubworkflows = !showOnlyMySubworkflows"
          >{{ showOnlyMySubworkflows ? '显示全部' : '只看我的' }}</button>
          <button type="button" class="text-button" @click="emit('openOwnedSubworkflows')">管理</button>
        </div>
      </section>

      <ul v-if="activeLibrary === 'subworkflows' && unpublishedSubworkflows.length" class="subworkflow-draft-list">
        <li v-for="subflow in unpublishedSubworkflows" :key="subflow.slug">
          <button type="button" :disabled="isWorkflowSwitching" @click="emit('openWorkflow', subflow.slug)">
            <span><strong>{{ subflow.name }}</strong><small>{{ subflow.description || subflow.slug }}</small></span>
            <em>继续编辑</em>
          </button>
        </li>
      </ul>

      <ul v-if="activeLibrary === 'tools'" class="library-list">
        <li v-for="tool in tools" :key="tool.id">
          <button
            type="button"
            class="library-item library-item--draggable"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动 ${tool.name} ${tool.version} 到画布`"
            @click="emit('quickAdd', { kind: 'tool', toolId: tool.id, version: tool.version, digest: tool.digest, label: tool.name })"
            @dragstart="emit('dragStart', $event, { kind: 'tool', toolId: tool.id, version: tool.version, digest: tool.digest, label: tool.name })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">{{ tool.name.slice(0, 2).toLowerCase() }}</span>
            <span class="library-item__content">
              <strong>{{ tool.name }}</strong>
              <small>{{ tool.description }}</small>
            </span>
            <span class="library-item__meta">
              <code>{{ tool.version }}</code>
              <small>{{ tool.status }}</small>
            </span>
          </button>
        </li>
      </ul>

      <ul v-else-if="activeLibrary === 'subworkflows'" class="library-list">
        <li v-for="subflow in visibleSubworkflows" :key="`${subflow.slug}@${subflow.version}`">
          <button
            type="button"
            class="library-item library-item--draggable library-item--subworkflow"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动子流程 ${subflow.name} v${subflow.version} 到画布`"
            @click="emit('quickAdd', { kind: 'subworkflow', slug: subflow.slug, version: subflow.version, digest: subflow.semantic_digest, label: subflow.name, description: subflow.description, interfaceContract: subflow.interface_contract })"
            @dragstart="emit('dragStart', $event, { kind: 'subworkflow', slug: subflow.slug, version: subflow.version, digest: subflow.semantic_digest, label: subflow.name, description: subflow.description, interfaceContract: subflow.interface_contract })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">SF</span>
            <span class="library-item__content">
              <strong>{{ subflow.name }}</strong>
              <small>{{ subflow.interface_contract.inputs?.length ?? 0 }} 输入 · {{ subflow.interface_contract.outputs?.length ?? 0 }} 输出</small>
            </span>
            <span class="library-item__meta">
              <code>v{{ subflow.version }}</code>
              <small>{{ ownedSubworkflowSlugs.has(subflow.slug) ? '我的 · 固定版本' : '共享 · 固定版本' }}</small>
            </span>
          </button>
        </li>
      </ul>

      <ul v-else-if="activeLibrary === 'inputs'" class="library-list">
        <li v-for="input in workflowInputs" :key="input.id">
          <button
            type="button"
            class="library-item library-item--draggable"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动 ${input.name} 输入到画布`"
            @click="emit('quickAdd', { kind: 'input', wdlType: input.name, label: input.name })"
            @dragstart="emit('dragStart', $event, { kind: 'input', wdlType: input.name, label: input.name })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">{{ input.name.slice(0, 1) }}</span>
            <span class="library-item__content">
              <strong>{{ input.name }}</strong>
              <small>{{ input.description }}</small>
            </span>
          </button>
        </li>
      </ul>

      <ul v-else class="library-list">
        <li v-for="output in workflowOutputs" :key="output.id">
          <button
            type="button"
            class="library-item library-item--draggable"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动 ${output.name} 输出到画布`"
            @click="emit('quickAdd', { kind: 'output', wdlType: output.name, label: output.name })"
            @dragstart="emit('dragStart', $event, { kind: 'output', wdlType: output.name, label: output.name })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">{{ output.name.slice(0, 1) }}</span>
            <span class="library-item__content">
              <strong>{{ output.name }}</strong>
              <small>{{ output.description }}</small>
            </span>
          </button>
        </li>
      </ul>

      <p v-if="activeLibrary === 'tools' && !toolRegistryLoaded" class="empty-state">
        正在载入已发布工具…
      </p>
      <p v-else-if="activeLibrary === 'tools' && tools.length === 0" class="empty-state">
        没有匹配的工具。可以清空搜索，或稍后导入 ToolSpec。
      </p>
      <div v-else-if="activeLibrary === 'subworkflows' && visibleSubworkflows.length === 0" class="empty-state">
        <p>{{ showOnlyMySubworkflows ? '你还没有已发布的子流程。' : '暂无已发布的子流程版本。' }}</p>
        <button class="button button--ghost" type="button" @click="beginCreate('subworkflow')">新建子流程</button>
      </div>
    </div>

    <footer class="library-panel__footer">
      <button v-if="activeLibrary === 'subworkflows' && subworkflows.length > 0" type="button" class="text-button" @click="beginCreate('subworkflow')">新建子流程</button>
      <button v-if="activeLibrary === 'tools'" type="button" class="text-button" @click="emit('openToolPackages')">创建 WDL 工具包</button>
      <button type="button" class="text-button" @click="emit('importToolSpec')">导入 ToolSpec JSON</button>
    </footer>
  </aside>
</template>
