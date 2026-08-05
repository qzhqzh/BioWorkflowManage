<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import {
  MarkerType,
  VueFlow,
  type Connection,
  type Edge,
  type GraphNode,
  type Node,
  type NodeDragEvent,
  type VueFlowStore,
  type XYPosition,
} from '@vue-flow/core'
import WorkflowNode from '~/components/workflow/WorkflowNode.vue'
import {
  coerceWdlValue,
  createToolDraft,
  hydrateToolDraft,
  normalizeIdentifier,
  WDL_IDENTIFIER_PATTERN,
} from '~/utils/tool-authoring'
import {
  layoutWorkflow,
  type WorkflowLayoutDirection,
} from '~/utils/workflow-layout'
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import ArtifactPreviewDrawer from '~/components/editor/ArtifactPreviewDrawer.vue'
import EditorNodeLibraryPanel from '~/components/editor/EditorNodeLibraryPanel.vue'
import WorkflowInspectorPanel from '~/components/editor/WorkflowInspectorPanel.vue'
import HelpWorkspace from '~/components/libraries/HelpWorkspace.vue'
import ToolLibraryWorkspace from '~/components/libraries/ToolLibraryWorkspace.vue'
import WorkflowLibraryWorkspace from '~/components/libraries/WorkflowLibraryWorkspace.vue'

const { $api: $fetch } = useNuxtApp()

type CompileState = 'idle' | 'running' | 'success' | 'error'
type LibraryTab = 'tools' | 'subworkflows' | 'inputs' | 'outputs'
type LayoutState = 'idle' | 'running' | 'success' | 'error'
type RailSection = 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help'
type SaveState = 'loading' | 'saved' | 'saving' | 'error'

interface Diagnostic {
  code: string
  stage: string
  severity: 'error' | 'warning'
  message: string
  location?: { node_id?: string; edge_id?: string; port?: string }
}

interface Artifact {
  name: string
  media_type: string
  digest: string
  content: string
}

interface ToolRegistryEntry {
  tool_id: string
  name: string | null
  latest_version: string | null
  latest_digest: string | null
  version_count: number
  draft_status?: string | null
  draft_updated_at?: string | null
}

interface ToolVersionEntry {
  tool_id: string
  version: string
  name: string
  digest: string
  created_at: string
  tool_spec?: Record<string, any>
}
interface ToolOperationError {
  code: string
  message: string
}
interface WorkflowLibraryEntry {
  slug: string
  name: string
  description?: string
  kind?: 'workflow' | 'subworkflow'
  latest_version: number | null
  updated_at: string
}
interface WorkflowInterfacePort {
  name: string
  label?: string
  wdl_type: string
  semantic_type?: string
  required?: boolean
}
interface WorkflowInterfaceContract {
  contract_version?: string
  inputs?: WorkflowInterfacePort[]
  outputs?: WorkflowInterfacePort[]
}
interface WorkflowVersionEntry {
  slug: string
  version: number
  name: string
  description?: string
  kind: 'workflow' | 'subworkflow'
  semantic_digest: string
  interface_contract: WorkflowInterfaceContract
}
interface WorkflowDocumentPayload {
  slug: string
  name: string
  description?: string
  kind: 'workflow' | 'subworkflow'
  workflow_graph: Record<string, any>
  tool_specs: Record<string, any>[]
  editor_document: {
    nodes?: Array<{ id: string; position: XYPosition }>
    viewport?: { x: number; y: number; zoom: number }
  }
}
interface CompilationHistoryRecord {
  id: number
  status: string
  workflow_version: number | null
  artifacts: Artifact[]
  created_at: string
}
interface WdlRevision {
  id?: number
  version: number
  source: 'system' | 'manual'
  content: string
  workflow_version?: number | null
  validation?: { status?: string; diagnostics?: Diagnostic[] }
  created_at: string
}
interface CompilationVersion {
  id: string
  version: string
  createdAt: string
  status: 'succeeded' | 'failed'
  artifacts: Artifact[]
}
type AlignmentAction =
  | 'left'
  | 'center-x'
  | 'right'
  | 'top'
  | 'center-y'
  | 'bottom'
  | 'distribute-x'
  | 'distribute-y'

type NodeOrientation = 'horizontal' | 'vertical'

interface WorkflowNodeData {
  kind: 'input' | 'tool' | 'subworkflow' | 'output'
  label: string
  identifier: string
  semanticType?: string
  version?: string
  description?: string
  dockerImage?: string
  command?: string
  toolId?: string
  toolDigest?: string
  specVersion?: string
  parameterValues?: Record<string, any>
  subworkflowSlug?: string
  subworkflowDigest?: string
  interfaceContract?: WorkflowInterfaceContract
  inputs?: Array<{ name: string; type: string; semanticType?: string }>
  outputs?: Array<{ name: string; type: string; semanticType?: string }>
  layoutDirection?: NodeOrientation
}

interface NodeLayoutSnapshot {
  activeDirection: WorkflowLayoutDirection
  nodes: Array<{
    id: string
    position: XYPosition
    orientation: NodeOrientation
  }>
}

interface NodeBounds {
  left: number
  right: number
  top: number
  bottom: number
  centerX: number
  centerY: number
  width: number
  height: number
}

const route = useRoute()
const activeLibrary = ref<LibraryTab>('tools')
const activeRail = ref<RailSection>('edit')
const searchQuery = ref('')
const registrySearchQuery = ref('')
const selectedNodeId = ref('fastp_1')
const selectedNodeIds = ref<string[]>([])
const compileState = ref<CompileState>('idle')
const saveState = ref<SaveState>('loading')
const inspectorTab = ref<'properties' | 'diagnostics' | 'artifacts'>('properties')
const diagnostics = ref<Diagnostic[]>([])
const artifacts = ref<Artifact[]>([])
const compilationVersions = ref<CompilationVersion[]>([])
const selectedCompilationId = ref('')
const copiedArtifact = ref('')
const registryTools = ref<ToolRegistryEntry[]>([])
const toolRegistryLoaded = ref(false)
const selectedToolId = ref('')
const selectedToolVersions = ref<ToolVersionEntry[]>([])
const selectedToolSpec = ref<Record<string, any>>()
const toolDraft = ref<Record<string, any>>()
const toolDraftState = ref<'idle' | 'saving' | 'saved' | 'publishing' | 'published' | 'error'>('idle')
const toolDraftValidationStatus = ref<string>()
const toolOperationError = ref<ToolOperationError>()
const creatingTool = ref(false)
const newToolId = ref('')
const toolCreateState = ref<'idle' | 'saving' | 'error'>('idle')
const workflowDocuments = ref<WorkflowLibraryEntry[]>([])
const subworkflowVersions = ref<WorkflowVersionEntry[]>([])
const subworkflowUpgradeTargetVersion = ref<number>()
const subworkflowUpgradeState = ref<'idle' | 'saving' | 'success' | 'warning' | 'error'>('idle')
const subworkflowUpgradeMessage = ref('')
const subworkflowUpgradeNodeId = ref('')
const selectedWorkflowSlug = ref('fastp_bwa_demo')
const workflowSwitchState = ref<'idle' | 'loading' | 'error'>('idle')
const switchingWorkflowSlug = ref('')
const workflowDescription = ref('')
const workflowKind = ref<'workflow' | 'subworkflow'>('workflow')
const editingWorkflowMetadata = ref(false)
const wdlRevisions = ref<WdlRevision[]>([])
const selectedWdlVersion = ref<number>()
const editingWdl = ref(false)
const wdlDraft = ref('')
const wdlSaveState = ref<'idle' | 'saving' | 'error'>('idle')
const previewDrawerArtifact = ref<Artifact>()
const lastSavedAt = ref<Date>()
const workflowGraph = ref<Record<string, any>>()
const toolSpecs = ref<Record<string, any>[]>([])
const importInput = ref<HTMLInputElement>()
const showInteractiveCanvas = ref(false)
const isLibraryDragging = ref(false)
const isCanvasDropActive = ref(false)
const vueFlowStore = shallowRef<VueFlowStore>()
const layoutDirection = ref<WorkflowLayoutDirection>('RIGHT')
const activeLayoutDirection = ref<WorkflowLayoutDirection>('RIGHT')
const layoutState = ref<LayoutState>('idle')
const layoutMessage = ref('')
const alignmentGuides = ref<{ x: number | null; y: number | null }>({
  x: null,
  y: null,
})
const undoStack = ref<NodeLayoutSnapshot[]>([])
const redoStack = ref<NodeLayoutSnapshot[]>([])
let narrowViewport: MediaQueryList | undefined
let dragStartSnapshot: NodeLayoutSnapshot | undefined
let layoutFeedbackTimer: number | undefined
let positionSaveTimer: number | undefined
let autosaveTimer: number | undefined
let workflowLoadSequence = 0

const workflowInputs = [
  { id: 'file', name: 'File', description: '文件或对象存储路径' },
  { id: 'string', name: 'String', description: '文本参数' },
  { id: 'integer', name: 'Int', description: '整数参数' },
  { id: 'boolean', name: 'Boolean', description: '布尔开关' },
]

const workflowOutputs = [
  { id: 'file', name: 'File', description: '导出文件或对象存储路径' },
  { id: 'string', name: 'String', description: '导出文本结果' },
  { id: 'integer', name: 'Int', description: '导出整数结果' },
  { id: 'boolean', name: 'Boolean', description: '导出布尔结果' },
]
const workflowPortWdlTypes = ['File', 'String', 'Int', 'Float', 'Boolean']

const visibleTools = computed(() => registryTools.value.map((tool) => ({
  id: tool.tool_id,
  name: tool.name || tool.tool_id,
  description: `${tool.version_count} 个不可变版本`,
  version: tool.latest_version ?? '草稿',
  digest: tool.latest_digest ?? undefined,
  status: tool.latest_version
    ? '已发布'
    : tool.draft_status === 'valid'
      ? '有效草稿'
      : '草稿待修正',
  isDraftOnly: !tool.latest_version,
})))

const filteredTools = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  const publishedTools = visibleTools.value.filter((tool) => !tool.isDraftOnly)
  if (!query) return publishedTools
  return publishedTools.filter((tool) =>
    `${tool.name} ${tool.description} ${tool.version}`.toLowerCase().includes(query),
  )
})
const filteredRegistryTools = computed(() => {
  const query = registrySearchQuery.value.trim().toLowerCase()
  if (!query) return visibleTools.value
  return visibleTools.value.filter((tool) =>
    `${tool.name} ${tool.description} ${tool.version}`.toLowerCase().includes(query),
  )
})
const filteredSubworkflows = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  if (!query) return subworkflowVersions.value
  return subworkflowVersions.value.filter((item) =>
    `${item.name} ${item.description ?? ''} ${item.slug} ${item.version}`.toLowerCase().includes(query),
  )
})
const currentWorkflowName = computed(() =>
  workflowGraph.value?.name
  ?? workflowDocuments.value.find((item) => item.slug === selectedWorkflowSlug.value)?.name
  ?? selectedWorkflowSlug.value,
)
const isWorkflowSwitching = computed(() => workflowSwitchState.value === 'loading')

const nodes = shallowRef<Node<WorkflowNodeData>[]>([
  {
    id: 'input_reads_1',
    type: 'workflow',
    position: { x: 36, y: 98 },
    data: {
      kind: 'input',
      label: 'Read 1 FASTQ',
      identifier: 'input_reads_1',
      semanticType: 'bio.fastq.gz.r1',
    },
  },
  {
    id: 'input_reads_2',
    type: 'workflow',
    position: { x: 36, y: 272 },
    data: {
      kind: 'input',
      label: 'Read 2 FASTQ',
      identifier: 'input_reads_2',
      semanticType: 'bio.fastq.gz.r2',
    },
  },
  {
    id: 'fastp_1',
    type: 'workflow',
    position: { x: 330, y: 148 },
    data: {
      kind: 'tool',
      label: 'fastp',
      identifier: 'fastp_1',
      version: '0.23.4',
      inputs: [
        { name: 'reads_1', type: 'File' },
        { name: 'reads_2', type: 'File' },
      ],
      outputs: [
        { name: 'clean_reads_1', type: 'File' },
        { name: 'clean_reads_2', type: 'File' },
        { name: 'html_report', type: 'File' },
      ],
    },
  },
  {
    id: 'bwa_1',
    type: 'workflow',
    position: { x: 680, y: 148 },
    data: {
      kind: 'tool',
      label: 'BWA-MEM',
      identifier: 'bwa_1',
      version: '0.7.17',
      inputs: [
        { name: 'reads_1', type: 'File' },
        { name: 'reads_2', type: 'File' },
        { name: 'reference', type: 'File' },
      ],
      outputs: [{ name: 'aligned_bam', type: 'File' }],
    },
  },
  {
    id: 'input_reference',
    type: 'workflow',
    position: { x: 330, y: 414 },
    data: {
      kind: 'input',
      label: 'Reference genome',
      identifier: 'input_reference',
      semanticType: 'bio.reference.fasta',
    },
  },
  {
    id: 'output_aligned_bam',
    type: 'workflow',
    position: { x: 1050, y: 148 },
    data: {
      kind: 'output',
      label: 'Aligned BAM',
      identifier: 'output_aligned_bam',
      semanticType: 'bio.alignment.bam',
    },
  },
  {
    id: 'output_html_report',
    type: 'workflow',
    position: { x: 680, y: 414 },
    data: {
      kind: 'output',
      label: 'HTML report',
      identifier: 'output_html_report',
      semanticType: 'bio.report.html',
    },
  },
])

const edges = shallowRef<Edge[]>([
  {
    id: 'edge_reads_1_fastp',
    source: 'input_reads_1',
    sourceHandle: 'out:value',
    target: 'fastp_1',
    targetHandle: 'in:reads_1',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
  {
    id: 'edge_reads_2_fastp',
    source: 'input_reads_2',
    sourceHandle: 'out:value',
    target: 'fastp_1',
    targetHandle: 'in:reads_2',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
  {
    id: 'edge_fastp_bwa_reads_1',
    source: 'fastp_1',
    sourceHandle: 'out:clean_reads_1',
    target: 'bwa_1',
    targetHandle: 'in:reads_1',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
  {
    id: 'edge_fastp_bwa_reads_2',
    source: 'fastp_1',
    sourceHandle: 'out:clean_reads_2',
    target: 'bwa_1',
    targetHandle: 'in:reads_2',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
  {
    id: 'edge_reference_bwa',
    source: 'input_reference',
    sourceHandle: 'out:value',
    target: 'bwa_1',
    targetHandle: 'in:reference',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
  {
    id: 'edge_bwa_bam',
    source: 'bwa_1',
    sourceHandle: 'out:aligned_bam',
    target: 'output_aligned_bam',
    targetHandle: 'in:value',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
  {
    id: 'edge_fastp_html',
    source: 'fastp_1',
    sourceHandle: 'out:html_report',
    target: 'output_html_report',
    targetHandle: 'in:value',
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  },
])

const selectedCompilation = computed(() =>
  compilationVersions.value.find((item) => item.id === selectedCompilationId.value)
    ?? compilationVersions.value[0],
)
const previewArtifact = computed(() =>
  (selectedCompilation.value?.artifacts ?? artifacts.value).find((item) => item.name.endsWith('.wdl')),
)
const selectedWdlRevision = computed(() =>
  wdlRevisions.value.find((revision) => revision.version === selectedWdlVersion.value)
    ?? wdlRevisions.value[0],
)
const activeWdlContent = computed(() =>
  selectedWdlRevision.value?.content ?? previewArtifact.value?.content ?? '',
)
const previewLines = computed(() => activeWdlContent.value.split('\n'))
const lastSavedLabel = computed(() => {
  if (!lastSavedAt.value) return '尚未自动保存'
  return `上次保存 ${new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(lastSavedAt.value)}`
})
const selectedNode = computed(() =>
  nodes.value.find((node) => node.id === selectedNodeId.value),
)

const selectedData = computed(() => selectedNode.value?.data as WorkflowNodeData | undefined)

function contractFromNodeData(data: WorkflowNodeData): WorkflowInterfaceContract {
  return data.interfaceContract ?? {
    contract_version: '1.0.0',
    inputs: (data.inputs ?? []).map((port) => ({
      name: port.name,
      wdl_type: port.type,
      semantic_type: port.semanticType,
    })),
    outputs: (data.outputs ?? []).map((port) => ({
      name: port.name,
      wdl_type: port.type,
      semantic_type: port.semanticType,
    })),
  }
}

function diffContractPorts(
  currentPorts: WorkflowInterfacePort[] = [],
  nextPorts: WorkflowInterfacePort[] = [],
) {
  const current = new Map(currentPorts.map((port) => [port.name, port]))
  const next = new Map(nextPorts.map((port) => [port.name, port]))
  const added = nextPorts.filter((port) => !current.has(port.name))
  const removed = currentPorts.filter((port) => !next.has(port.name))
  const changed = nextPorts.filter((port) => {
    const previous = current.get(port.name)
    return previous && (
      previous.wdl_type !== port.wdl_type
      || (previous.semantic_type ?? '') !== (port.semantic_type ?? '')
      || Boolean(previous.required) !== Boolean(port.required)
    )
  })
  const breaking = [
    ...removed,
    ...changed.filter((port) => {
      const previous = current.get(port.name)
      return previous?.wdl_type !== port.wdl_type
        || (previous?.semantic_type ?? '') !== (port.semantic_type ?? '')
        || (!previous?.required && Boolean(port.required))
    }),
    ...added.filter((port) => Boolean(port.required)),
  ]
  return { added, removed, changed, breaking }
}

const availableSubworkflowUpgrades = computed(() => {
  const data = selectedData.value
  if (data?.kind !== 'subworkflow' || !data.subworkflowSlug) return []
  const currentVersion = Number(data.version ?? 0)
  return subworkflowVersions.value
    .filter((item) =>
      item.slug === data.subworkflowSlug
      && item.kind === 'subworkflow'
      && item.version > currentVersion,
    )
    .toSorted((a, b) => b.version - a.version)
})

const selectedSubworkflowUpgrade = computed(() =>
  availableSubworkflowUpgrades.value.find(
    (item) => item.version === subworkflowUpgradeTargetVersion.value,
  ) ?? availableSubworkflowUpgrades.value[0],
)

const selectedSubworkflowUpgradeInfo = computed(() => {
  const data = selectedData.value
  const target = selectedSubworkflowUpgrade.value
  if (data?.kind !== 'subworkflow' || !target) return undefined
  const current = contractFromNodeData(data)
  const inputs = diffContractPorts(current.inputs, target.interface_contract.inputs)
  const outputs = diffContractPorts(current.outputs, target.interface_contract.outputs)
  const details = [
    inputs.added.length ? `新增输入 ${inputs.added.map((port) => port.name).join('、')}` : '',
    inputs.removed.length ? `移除输入 ${inputs.removed.map((port) => port.name).join('、')}` : '',
    inputs.changed.length ? `变更输入 ${inputs.changed.map((port) => port.name).join('、')}` : '',
    outputs.added.length ? `新增输出 ${outputs.added.map((port) => port.name).join('、')}` : '',
    outputs.removed.length ? `移除输出 ${outputs.removed.map((port) => port.name).join('、')}` : '',
    outputs.changed.length ? `变更输出 ${outputs.changed.map((port) => port.name).join('、')}` : '',
  ].filter(Boolean)
  return {
    inputs,
    outputs,
    details,
    likelyCompatible: inputs.breaking.length === 0 && outputs.breaking.length === 0,
  }
})

const selectedToolSpecForNode = computed(() => {
  const data = selectedData.value
  if (data?.kind !== 'tool') return undefined
  return toolSpecs.value.find((spec) =>
    spec.id === data.toolId
    && (!data.version || spec.tool_version === data.version),
  ) ?? toolSpecs.value.find((spec) =>
    spec.id === data.label.toLowerCase() || spec.name === data.label,
  )
})

const selectedToolInputs = computed<Record<string, any>[]>(() =>
  selectedToolSpecForNode.value?.inputs ?? [],
)
const selectedToolParameters = computed(() =>
  selectedToolInputs.value.filter((port) => !String(port.wdl_type).includes('File')),
)

const selectedGraphNodes = computed(() => {
  const ids = new Set(selectedNodeIds.value)
  return (vueFlowStore.value?.getNodes.value ?? []).filter((node) => ids.has(node.id))
})
const selectedNodeCount = computed(() => selectedGraphNodes.value.length)
const canAlign = computed(() =>
  selectedNodeCount.value >= 2
  && layoutState.value !== 'running'
  && !isWorkflowSwitching.value,
)
const canDistribute = computed(() =>
  selectedNodeCount.value >= 3
  && layoutState.value !== 'running'
  && !isWorkflowSwitching.value,
)
const autoLayoutLabel = computed(() =>
  selectedNodeCount.value >= 2
    ? `整理选中 ${selectedNodeCount.value} 项`
    : '自动整理',
)
const layoutDirectionLabel = computed(() =>
  layoutDirection.value === 'RIGHT' ? '从左到右' : '从上到下',
)
const verticalGuideStyle = computed(() => {
  const guide = alignmentGuides.value.x
  const viewport = vueFlowStore.value?.viewport.value
  if (guide === null || !viewport) return undefined

  return {
    transform: `translateX(${guide * viewport.zoom + viewport.x}px)`,
  }
})
const horizontalGuideStyle = computed(() => {
  const guide = alignmentGuides.value.y
  const viewport = vueFlowStore.value?.viewport.value
  if (guide === null || !viewport) return undefined

  return {
    transform: `translateY(${guide * viewport.zoom + viewport.y}px)`,
  }
})

function handleNodeClick({ node, event }: { node: Node; event: MouseEvent | TouchEvent }) {
  const multiSelect = event instanceof MouseEvent
    && (event.shiftKey || event.ctrlKey || event.metaKey)
  selectedNodeIds.value = multiSelect
    ? selectedNodeIds.value.includes(node.id)
      ? selectedNodeIds.value.filter((id) => id !== node.id)
      : [...selectedNodeIds.value, node.id]
    : [node.id]
  selectedNodeId.value = node.id
  inspectorTab.value = 'properties'
}

function handleCanvasDeleteKey(event: KeyboardEvent) {
  if (
    !['Delete', 'Backspace'].includes(event.key)
    || activeRail.value !== 'edit'
    || isWorkflowSwitching.value
  ) return
  const target = event.target as HTMLElement | null
  if (
    target?.matches('input, textarea, select, [contenteditable="true"]')
    || target?.closest('input, textarea, select, [contenteditable="true"]')
  ) return

  const selectedIds = new Set(selectedGraphNodes.value.map((node) => node.id))
  const focusedNodeId = target?.closest<HTMLElement>('.vue-flow__node')?.dataset.id
  if (selectedIds.size === 0 && focusedNodeId) selectedIds.add(focusedNodeId)
  if (
    selectedIds.size === 0
    && selectedNodeId.value
    && target?.closest('.canvas-panel')
  ) selectedIds.add(selectedNodeId.value)
  if (selectedIds.size === 0) return

  event.preventDefault()
  nodes.value = nodes.value.filter((node) => !selectedIds.has(node.id))
  edges.value = edges.value.filter(
    (edge) => !selectedIds.has(edge.source) && !selectedIds.has(edge.target),
  )
  if (selectedIds.has(selectedNodeId.value)) {
    selectedNodeId.value = nodes.value[0]?.id ?? ''
  }
  selectedNodeIds.value = selectedNodeIds.value.filter((id) => !selectedIds.has(id))
  void nextTick().then(() => saveWorkflow(true))
  showLayoutFeedback(
    `已删除 ${selectedIds.size} 个节点及其关联连线`,
    'success',
  )
}

function handleFlowInit(store: VueFlowStore) {
  vueFlowStore.value = store
}

function getNodeBounds(node: GraphNode): NodeBounds {
  const data = node.data as WorkflowNodeData
  const fallbackWidth = ['tool', 'subworkflow'].includes(data.kind) ? 288 : 216
  const fallbackHeight = ['tool', 'subworkflow'].includes(data.kind) ? 160 : 112
  const width = node.dimensions.width || (typeof node.width === 'number' ? node.width : fallbackWidth)
  const height = node.dimensions.height || (typeof node.height === 'number' ? node.height : fallbackHeight)

  return {
    left: node.position.x,
    right: node.position.x + width,
    top: node.position.y,
    bottom: node.position.y + height,
    centerX: node.position.x + width / 2,
    centerY: node.position.y + height / 2,
    width,
    height,
  }
}

function getGroupBounds(group: GraphNode[]): NodeBounds {
  const bounds = group.map(getNodeBounds)
  const left = Math.min(...bounds.map((node) => node.left))
  const right = Math.max(...bounds.map((node) => node.right))
  const top = Math.min(...bounds.map((node) => node.top))
  const bottom = Math.max(...bounds.map((node) => node.bottom))

  return {
    left,
    right,
    top,
    bottom,
    centerX: (left + right) / 2,
    centerY: (top + bottom) / 2,
    width: right - left,
    height: bottom - top,
  }
}

function createLayoutSnapshot(): NodeLayoutSnapshot {
  const currentNodes = vueFlowStore.value?.getNodes.value ?? []

  return {
    activeDirection: activeLayoutDirection.value,
    nodes: currentNodes.map((node) => ({
      id: node.id,
      position: { ...node.position },
      orientation:
        ((node.data as WorkflowNodeData).layoutDirection ?? 'horizontal'),
    })),
  }
}

function snapshotKey(snapshot: NodeLayoutSnapshot) {
  return snapshot.nodes
    .map(({ id, position, orientation }) =>
      `${id}:${position.x.toFixed(2)},${position.y.toFixed(2)},${orientation}`,
    )
    .join('|')
}

function pushUndo(snapshot: NodeLayoutSnapshot) {
  undoStack.value = [...undoStack.value.slice(-29), snapshot]
  redoStack.value = []
}

async function applySnapshot(snapshot: NodeLayoutSnapshot) {
  const nodeState = new Map(snapshot.nodes.map((node) => [node.id, node]))
  nodes.value = nodes.value.map((node) => {
    const state = nodeState.get(node.id)
    if (!state) return node

    return {
      ...node,
      position: { ...state.position },
      data: {
        ...(node.data as WorkflowNodeData),
        layoutDirection: state.orientation,
      },
    }
  })
  activeLayoutDirection.value = snapshot.activeDirection
  layoutDirection.value = snapshot.activeDirection

  await nextTick()
  vueFlowStore.value?.updateNodeInternals(snapshot.nodes.map((node) => node.id))
}

async function undoLayoutChange() {
  const previous = undoStack.value.at(-1)
  if (!previous) return

  const current = createLayoutSnapshot()
  undoStack.value = undoStack.value.slice(0, -1)
  redoStack.value = [...redoStack.value, current]
  await applySnapshot(previous)
  showLayoutFeedback('已撤销上一步画布调整', 'success')
}

async function redoLayoutChange() {
  const next = redoStack.value.at(-1)
  if (!next) return

  const current = createLayoutSnapshot()
  redoStack.value = redoStack.value.slice(0, -1)
  undoStack.value = [...undoStack.value, current]
  await applySnapshot(next)
  showLayoutFeedback('已重做画布调整', 'success')
}

function showLayoutFeedback(message: string, state: Exclude<LayoutState, 'running'>) {
  window.clearTimeout(layoutFeedbackTimer)
  layoutMessage.value = message
  layoutState.value = state
  layoutFeedbackTimer = window.setTimeout(() => {
    layoutState.value = 'idle'
    layoutMessage.value = ''
  }, 2600)
}

function toggleLayoutDirection() {
  layoutDirection.value = layoutDirection.value === 'RIGHT' ? 'DOWN' : 'RIGHT'
}

async function runAutoLayout() {
  const store = vueFlowStore.value
  if (!store || layoutState.value === 'running') return

  const allNodes = [...store.getNodes.value]
  if (allNodes.length === 0) return

  const directionChanged = activeLayoutDirection.value !== layoutDirection.value
  const selectedNodes = [...store.getSelectedNodes.value]
  const layoutNodes =
    !directionChanged && selectedNodes.length >= 2
      ? selectedNodes
      : allNodes
  const layoutNodeIds = new Set(layoutNodes.map((node) => node.id))
  const layoutEdges = edges.value.filter(
    (edge) => layoutNodeIds.has(edge.source) && layoutNodeIds.has(edge.target),
  )
  const anchor = getGroupBounds(layoutNodes)
  const before = createLayoutSnapshot()

  window.clearTimeout(layoutFeedbackTimer)
  layoutState.value = 'running'
  layoutMessage.value = `正在按${layoutDirectionLabel.value}整理…`

  try {
    const positions = await layoutWorkflow(layoutNodes, layoutEdges, layoutDirection.value)
    if (positions.size !== layoutNodes.length) {
      throw new Error('布局引擎未返回完整节点坐标')
    }

    const resultPositions = [...positions.values()]
    const resultMinX = Math.min(...resultPositions.map((position) => position.x))
    const resultMinY = Math.min(...resultPositions.map((position) => position.y))
    const offsetX = anchor.left - resultMinX
    const offsetY = anchor.top - resultMinY
    const isFullLayout = layoutNodes.length === allNodes.length
    const orientation: NodeOrientation =
      layoutDirection.value === 'RIGHT' ? 'horizontal' : 'vertical'

    nodes.value = nodes.value.map((node) => {
      const position = positions.get(node.id)
      const nextData =
        isFullLayout
          ? {
              ...(node.data as WorkflowNodeData),
              layoutDirection: orientation,
            }
          : node.data

      if (!position) {
        return {
          ...node,
          data: nextData,
        }
      }

      return {
        ...node,
        position: {
          x: Math.round(position.x + offsetX),
          y: Math.round(position.y + offsetY),
        },
        data: nextData,
      }
    })

    if (isFullLayout) {
      activeLayoutDirection.value = layoutDirection.value
    }

    await nextTick()
    if (isFullLayout) {
      store.updateNodeInternals(allNodes.map((node) => node.id))
    }
    await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()))
    await store.fitView({
      padding: 0.18,
      duration: 260,
      interpolate: 'smooth',
    })

    pushUndo(before)
    const scopeLabel = isFullLayout ? `${layoutNodes.length} 个节点` : `${layoutNodes.length} 个选中节点`
    showLayoutFeedback(`已按${layoutDirectionLabel.value}整理 ${scopeLabel}`, 'success')
  } catch (error) {
    console.error('Automatic workflow layout failed', error)
    showLayoutFeedback('自动整理失败，请重试', 'error')
  }
}

function commitPositions(positions: Map<string, XYPosition>, message: string) {
  if (positions.size === 0) return

  const before = createLayoutSnapshot()
  const currentPositions = new Map(before.nodes.map((node) => [node.id, node.position]))
  const hasChanges = [...positions].some(([id, position]) => {
    const current = currentPositions.get(id)
    return !current || current.x !== position.x || current.y !== position.y
  })
  if (!hasChanges) return

  nodes.value = nodes.value.map((node) => {
    const position = positions.get(node.id)
    return position
      ? {
          ...node,
          position: {
            x: Math.round(position.x),
            y: Math.round(position.y),
          },
        }
      : node
  })
  pushUndo(before)
  showLayoutFeedback(message, 'success')
}

function alignSelection(action: AlignmentAction) {
  const selection = [...selectedGraphNodes.value]
  if (selection.length < 2 || layoutState.value === 'running') return
  if ((action === 'distribute-x' || action === 'distribute-y') && selection.length < 3) return

  const selectionBounds = getGroupBounds(selection)
  const positions = new Map<string, XYPosition>()

  if (action === 'distribute-x') {
    const ordered = selection.toSorted((a, b) => a.position.x - b.position.x)
    const nodeBounds = ordered.map(getNodeBounds)
    const totalWidth = nodeBounds.reduce((sum, bounds) => sum + bounds.width, 0)
    const availableGap = (selectionBounds.width - totalWidth) / (ordered.length - 1)

    if (availableGap >= 0) {
      let cursor = selectionBounds.left
      ordered.forEach((node, index) => {
        positions.set(node.id, { x: cursor, y: node.position.y })
        cursor += nodeBounds[index]!.width + availableGap
      })
    } else {
      const firstCenter = nodeBounds[0]!.centerX
      const lastCenter = nodeBounds.at(-1)!.centerX
      const centerStep = (lastCenter - firstCenter) / (ordered.length - 1)
      ordered.forEach((node, index) => {
        positions.set(node.id, {
          x: firstCenter + centerStep * index - nodeBounds[index]!.width / 2,
          y: node.position.y,
        })
      })
    }
    commitPositions(positions, '已水平等距分布选中节点')
    return
  }

  if (action === 'distribute-y') {
    const ordered = selection.toSorted((a, b) => a.position.y - b.position.y)
    const nodeBounds = ordered.map(getNodeBounds)
    const totalHeight = nodeBounds.reduce((sum, bounds) => sum + bounds.height, 0)
    const availableGap = (selectionBounds.height - totalHeight) / (ordered.length - 1)

    if (availableGap >= 0) {
      let cursor = selectionBounds.top
      ordered.forEach((node, index) => {
        positions.set(node.id, { x: node.position.x, y: cursor })
        cursor += nodeBounds[index]!.height + availableGap
      })
    } else {
      const firstCenter = nodeBounds[0]!.centerY
      const lastCenter = nodeBounds.at(-1)!.centerY
      const centerStep = (lastCenter - firstCenter) / (ordered.length - 1)
      ordered.forEach((node, index) => {
        positions.set(node.id, {
          x: node.position.x,
          y: firstCenter + centerStep * index - nodeBounds[index]!.height / 2,
        })
      })
    }
    commitPositions(positions, '已垂直等距分布选中节点')
    return
  }

  for (const node of selection) {
    const bounds = getNodeBounds(node)
    let x = node.position.x
    let y = node.position.y

    if (action === 'left') x = selectionBounds.left
    if (action === 'center-x') x = selectionBounds.centerX - bounds.width / 2
    if (action === 'right') x = selectionBounds.right - bounds.width
    if (action === 'top') y = selectionBounds.top
    if (action === 'center-y') y = selectionBounds.centerY - bounds.height / 2
    if (action === 'bottom') y = selectionBounds.bottom - bounds.height

    positions.set(node.id, { x, y })
  }

  const actionLabel: Record<Exclude<AlignmentAction, 'distribute-x' | 'distribute-y'>, string> = {
    left: '左对齐',
    'center-x': '水平居中',
    right: '右对齐',
    top: '顶部对齐',
    'center-y': '垂直居中',
    bottom: '底部对齐',
  }
  commitPositions(
    positions,
    `已${actionLabel[action as Exclude<AlignmentAction, 'distribute-x' | 'distribute-y'>]}选中节点`,
  )
}

function findGuideMatch(
  movingValues: number[],
  targetValues: number[],
  threshold: number,
) {
  let best: { delta: number; guide: number; distance: number } | undefined

  for (const moving of movingValues) {
    for (const target of targetValues) {
      const delta = target - moving
      const distance = Math.abs(delta)
      if (distance <= threshold && (!best || distance < best.distance)) {
        best = { delta, guide: target, distance }
      }
    }
  }

  return best
}

function handleNodeDragStart() {
  dragStartSnapshot = createLayoutSnapshot()
}

function handleNodeDrag({ nodes: draggedNodes }: NodeDragEvent) {
  const store = vueFlowStore.value
  if (!store || draggedNodes.length === 0) return

  const draggedIds = new Set(draggedNodes.map((node) => node.id))
  const stationaryNodes = store.getNodes.value.filter((node) => !draggedIds.has(node.id))
  if (stationaryNodes.length === 0) {
    alignmentGuides.value = { x: null, y: null }
    return
  }

  const movingBounds = getGroupBounds(draggedNodes)
  const targetBounds = stationaryNodes.map(getNodeBounds)
  const viewportZoom = store.viewport.value.zoom
  const threshold = 6 / viewportZoom
  const matchX = findGuideMatch(
    [movingBounds.left, movingBounds.centerX, movingBounds.right],
    targetBounds.flatMap((bounds) => [bounds.left, bounds.centerX, bounds.right]),
    threshold,
  )
  const matchY = findGuideMatch(
    [movingBounds.top, movingBounds.centerY, movingBounds.bottom],
    targetBounds.flatMap((bounds) => [bounds.top, bounds.centerY, bounds.bottom]),
    threshold,
  )

  alignmentGuides.value = {
    x: matchX?.guide ?? null,
    y: matchY?.guide ?? null,
  }

  if (!matchX && !matchY) return

  for (const node of draggedNodes) {
    node.position = {
      x: node.position.x + (matchX?.delta ?? 0),
      y: node.position.y + (matchY?.delta ?? 0),
    }
  }
}

function handleNodeDragStop() {
  alignmentGuides.value = { x: null, y: null }
  if (
    dragStartSnapshot
    && snapshotKey(dragStartSnapshot) !== snapshotKey(createLayoutSnapshot())
  ) {
    pushUndo(dragStartSnapshot)
  }
  dragStartSnapshot = undefined
  schedulePositionSave()
}

function schedulePositionSave() {
  window.clearTimeout(positionSaveTimer)
  positionSaveTimer = window.setTimeout(() => {
    void saveWorkflow(true)
  }, 1200)
}

type LibraryDragPayload =
  | { kind: 'tool'; toolId: string; version: string; digest?: string; label: string }
  | {
      kind: 'subworkflow'
      slug: string
      version: number
      digest: string
      label: string
      description?: string
      interfaceContract: WorkflowVersionEntry['interface_contract']
    }
  | { kind: 'input' | 'output'; wdlType: string; label: string }

function startLibraryDrag(event: DragEvent, payload: LibraryDragPayload) {
  if (!event.dataTransfer) return
  event.dataTransfer.effectAllowed = 'copy'
  event.dataTransfer.setData('application/x-bioworkflow-node', JSON.stringify(payload))
  event.dataTransfer.setData('text/plain', payload.label)
  isLibraryDragging.value = true
}

function finishLibraryDrag() {
  isLibraryDragging.value = false
  isCanvasDropActive.value = false
}

function uniqueNodeId(prefix: string) {
  const normalized = prefix.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_|_$/g, '') || 'node'
  let index = 1
  let candidate = `${normalized}_${index}`
  const ids = new Set(nodes.value.map((node) => node.id))
  while (ids.has(candidate)) candidate = `${normalized}_${++index}`
  return candidate
}

function uniqueEdgeId(prefix: string) {
  const normalized = prefix.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^_|_$/g, '') || 'edge'
  let index = 1
  let candidate = `${normalized}_${index}`
  const ids = new Set(edges.value.map((edge) => edge.id))
  while (ids.has(candidate)) candidate = `${normalized}_${++index}`
  return candidate
}

function refreshSelectedNode(nodeIds: string[]) {
  void nextTick().then(() => {
    vueFlowStore.value?.updateNodeInternals(nodeIds)
  })
  schedulePositionSave()
}

function updateSelectedNodeIdentifier(event: Event) {
  if (!selectedNode.value || selectedData.value?.kind === 'subworkflow') return
  const input = event.target as HTMLInputElement
  const currentId = selectedNode.value.id
  const nextId = input.value.trim()
  if (!WDL_IDENTIFIER_PATTERN.test(nextId)) {
    input.value = currentId
    showLayoutFeedback('节点 ID 必须是合法的 WDL identifier', 'error')
    return
  }
  if (nextId !== currentId && nodes.value.some((node) => node.id === nextId)) {
    input.value = currentId
    showLayoutFeedback(`节点 ID ${nextId} 已存在`, 'error')
    return
  }
  if (nextId === currentId) return

  nodes.value = nodes.value.map((node) =>
    node.id === currentId
      ? {
          ...node,
          id: nextId,
          data: { ...(node.data as WorkflowNodeData), identifier: nextId },
        }
      : node,
  )
  edges.value = edges.value.map((edge) => ({
    ...edge,
    source: edge.source === currentId ? nextId : edge.source,
    target: edge.target === currentId ? nextId : edge.target,
  }))
  selectedNodeId.value = nextId
  selectedNodeIds.value = selectedNodeIds.value.map((id) => id === currentId ? nextId : id)
  refreshSelectedNode([nextId])
}

function updateSelectedNodeLabel(event: Event) {
  const data = selectedData.value
  if (!selectedNode.value || !data || data.kind === 'subworkflow') return
  const input = event.target as HTMLInputElement
  const label = input.value.trim()
  if (!label) {
    input.value = data.label
    showLayoutFeedback('显示名称不能为空', 'error')
    return
  }
  nodes.value = nodes.value.map((node) =>
    node.id === selectedNodeId.value
      ? { ...node, data: { ...(node.data as WorkflowNodeData), label } }
      : node,
  )
  refreshSelectedNode([selectedNodeId.value])
}

function updateSelectedNodeWdlType(event: Event) {
  if (!selectedNode.value || !['input', 'output'].includes(selectedData.value?.kind ?? '')) return
  const wdlType = (event.target as HTMLSelectElement).value
  nodes.value = nodes.value.map((node) => {
    if (node.id !== selectedNodeId.value) return node
    const data = node.data as WorkflowNodeData
    return {
      ...node,
      data: {
        ...data,
        inputs: data.kind === 'output' ? [{ name: 'value', type: wdlType }] : undefined,
        outputs: data.kind === 'input' ? [{ name: 'value', type: wdlType }] : undefined,
      },
    }
  })
  refreshSelectedNode([selectedNodeId.value])
}

function updateSelectedNodeSemanticType(event: Event) {
  if (!selectedNode.value || !['input', 'output'].includes(selectedData.value?.kind ?? '')) return
  const semanticType = (event.target as HTMLInputElement).value.trim()
  nodes.value = nodes.value.map((node) =>
    node.id === selectedNodeId.value
      ? { ...node, data: { ...(node.data as WorkflowNodeData), semanticType } }
      : node,
  )
  refreshSelectedNode([selectedNodeId.value])
}

function isParameterConnected(portName: string) {
  return edges.value.some((edge) =>
    edge.target === selectedNodeId.value && edge.targetHandle === `in:${portName}`,
  )
}

function parameterDisplayValue(port: Record<string, any>) {
  const value = selectedData.value?.parameterValues?.[port.name] ?? port.default ?? ''
  return Array.isArray(value) ? value.join(', ') : value
}

function updateToolParameter(port: Record<string, any>, event: Event) {
  if (!selectedNode.value || selectedData.value?.kind !== 'tool' || isParameterConnected(port.name)) return
  const rawValue = (event.target as HTMLInputElement | HTMLSelectElement).value
  nodes.value = nodes.value.map((node) => {
    if (node.id !== selectedNodeId.value) return node
    const data = node.data as WorkflowNodeData
    const parameterValues = { ...(data.parameterValues ?? {}) }
    if (rawValue === '') delete parameterValues[port.name]
    else parameterValues[port.name] = coerceWdlValue(rawValue, port.wdl_type)
    return { ...node, data: { ...data, parameterValues } }
  })
  refreshSelectedNode([selectedNodeId.value])
}

function graphNodeToCanvasNode(
  graphNode: Record<string, any>,
  position: XYPosition,
  specs = toolSpecs.value,
): Node<WorkflowNodeData> {
  if (graphNode.type === 'tool') {
    const spec = specs.find((item) =>
      item.id === graphNode.tool_ref?.id && item.tool_version === graphNode.tool_ref?.tool_version,
    )
    return {
      id: graphNode.id,
      type: 'workflow',
      position,
      data: {
        kind: 'tool',
        label: graphNode.label ?? spec?.display_name ?? spec?.name ?? graphNode.tool_ref?.id,
        identifier: graphNode.id,
        version: graphNode.tool_ref?.tool_version,
        toolId: graphNode.tool_ref?.id,
        toolDigest: graphNode.tool_ref?.digest,
        specVersion: graphNode.tool_ref?.spec_version,
        parameterValues: { ...(graphNode.parameter_values ?? {}) },
        description: spec?.description,
        dockerImage: spec?.container?.image,
        command: spec?.command?.template,
        inputs: (spec?.inputs ?? []).map((port: any) => ({
          name: port.name, type: port.wdl_type, semanticType: port.semantic_type,
        })),
        outputs: (spec?.outputs ?? []).map((port: any) => ({
          name: port.name, type: port.wdl_type, semanticType: port.semantic_type,
        })),
      },
    }
  }
  if (graphNode.type === 'subworkflow') {
    const reference = graphNode.subworkflow_ref ?? graphNode.workflow_ref ?? {}
    const contract = graphNode.interface_contract ?? {}
    return {
      id: graphNode.id,
      type: 'workflow',
      position,
      data: {
        kind: 'subworkflow',
        label: graphNode.label ?? reference.name ?? reference.slug ?? graphNode.id,
        identifier: graphNode.id,
        version: String(reference.version ?? ''),
        description: graphNode.description,
        subworkflowSlug: reference.slug,
        subworkflowDigest: reference.digest ?? reference.semantic_digest,
        interfaceContract: contract,
        inputs: (contract.inputs ?? []).map((port: any) => ({
          name: port.name,
          type: port.wdl_type ?? port.type,
          semanticType: port.semantic_type ?? port.semanticType,
        })),
        outputs: (contract.outputs ?? []).map((port: any) => ({
          name: port.name,
          type: port.wdl_type ?? port.type,
          semanticType: port.semantic_type ?? port.semanticType,
        })),
      },
    }
  }
  const kind = graphNode.type === 'workflow_output' ? 'output' : 'input'
  return {
    id: graphNode.id,
    type: 'workflow',
    position,
    data: {
      kind,
      label: graphNode.label ?? graphNode.id,
      identifier: graphNode.id,
      semanticType: graphNode.port?.semantic_type,
      inputs: kind === 'output' ? [{ name: 'value', type: graphNode.port?.wdl_type ?? 'File' }] : undefined,
      outputs: kind === 'input' ? [{ name: 'value', type: graphNode.port?.wdl_type ?? 'File' }] : undefined,
    },
  }
}

function graphEdgeToCanvasEdge(edge: Record<string, any>): Edge {
  return {
    id: edge.id,
    source: edge.source.node_id,
    sourceHandle: `out:${edge.source.port}`,
    target: edge.target.node_id,
    targetHandle: `in:${edge.target.port}`,
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  }
}

async function resolveToolSpec(payload: Extract<LibraryDragPayload, { kind: 'tool' }>) {
  let spec = toolSpecs.value.find((item) =>
    item.id === payload.toolId && item.tool_version === payload.version,
  )
  if (spec) return spec
  const version = await $fetch<ToolVersionEntry>(
    `/api/v1/tools/${encodeURIComponent(payload.toolId)}/versions/${encodeURIComponent(payload.version)}`,
  )
  spec = version.tool_spec
  if (spec) toolSpecs.value = [...toolSpecs.value, spec]
  return spec
}

async function addLibraryNode(payload: LibraryDragPayload, position: XYPosition) {
  if (isWorkflowSwitching.value) return
  try {
    let graphNode: Record<string, any>

    if (payload.kind === 'tool') {
      const spec = await resolveToolSpec(payload)
      if (!spec) throw new Error(`找不到工具 ${payload.toolId} ${payload.version} 的 ToolSpec`)
      const registry = registryTools.value.find((item) => item.tool_id === payload.toolId)
      const id = uniqueNodeId(payload.toolId)
      graphNode = {
        id,
        type: 'tool',
        label: payload.label,
        tool_ref: {
          id: payload.toolId,
          tool_version: payload.version,
          spec_version: spec.schema_version,
          digest: payload.digest ?? registry?.latest_digest,
        },
        parameter_values: Object.fromEntries(
          (spec.inputs ?? [])
            .filter((port: any) => port.default !== undefined)
            .map((port: any) => [port.name, port.default]),
        ),
      }
    } else if (payload.kind === 'subworkflow') {
      const id = uniqueNodeId(payload.slug)
      graphNode = {
        id,
        type: 'subworkflow',
        label: payload.label,
        description: payload.description ?? '',
        subworkflow_ref: {
          slug: payload.slug,
          version: payload.version,
          digest: payload.digest,
        },
        interface_contract: payload.interfaceContract,
      }
    } else {
      const id = uniqueNodeId(`${payload.kind}_${payload.wdlType}`)
      graphNode = {
        id,
        type: payload.kind === 'input' ? 'workflow_input' : 'workflow_output',
        label: `${payload.label} ${payload.kind === 'input' ? 'input' : 'output'}`,
        port: {
          name: 'value',
          wdl_type: payload.wdlType,
          semantic_type: payload.wdlType === 'File' ? 'core.file.any' : `core.${payload.wdlType.toLowerCase()}`,
          ...(payload.kind === 'input' ? { required: true } : {}),
        },
      }
    }

    const nextNode = graphNodeToCanvasNode(graphNode, position)
    nodes.value = [...nodes.value, nextNode]
    selectedNodeId.value = nextNode.id
    await nextTick()
    await saveWorkflow(true)
    showLayoutFeedback(`已添加 ${(nextNode.data as WorkflowNodeData).label}`, 'success')
  } catch (error) {
    console.error('Failed to add library node', error)
    showLayoutFeedback(error instanceof Error ? error.message : '节点添加失败', 'error')
  }
}

async function quickAddLibraryNode(payload: LibraryDragPayload) {
  const store = vueFlowStore.value
  const flowElement = document.querySelector<HTMLElement>('.workflow-flow')
  if (!store || !flowElement) {
    showLayoutFeedback('画布尚未准备好，请稍后重试', 'error')
    return
  }
  const bounds = flowElement.getBoundingClientRect()
  const offset = (nodes.value.length % 5) * 18
  const position = store.screenToFlowCoordinate({
    x: bounds.left + bounds.width / 2 + offset,
    y: bounds.top + bounds.height / 2 + offset,
  })
  await addLibraryNode(payload, position)
}

async function handleCanvasDrop(event: DragEvent) {
  event.preventDefault()
  finishLibraryDrag()
  const raw = event.dataTransfer?.getData('application/x-bioworkflow-node')
  const store = vueFlowStore.value
  if (!raw || !store) return

  try {
    const payload = JSON.parse(raw) as LibraryDragPayload
    const position = store.screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
    await addLibraryNode(payload, position)
  } catch (error) {
    console.error('Failed to parse library node', error)
    showLayoutFeedback('节点数据无效，请刷新后重试', 'error')
  }
}

function handleCanvasDragOver(event: DragEvent) {
  if (!event.dataTransfer?.types.includes('application/x-bioworkflow-node')) return
  event.preventDefault()
  event.dataTransfer.dropEffect = 'copy'
  isCanvasDropActive.value = true
}

function handleCanvasDragLeave(event: DragEvent) {
  const current = event.currentTarget as HTMLElement
  if (event.relatedTarget instanceof Node && current.contains(event.relatedTarget)) return
  isCanvasDropActive.value = false
}

interface ConnectionPort {
  name: string
  type: string
  semanticType?: string
}

function portsAreCompatible(source?: ConnectionPort, target?: ConnectionPort) {
  if (!source || !target || source.type !== target.type) return false
  return !source.semanticType
    || !target.semanticType
    || source.semanticType === target.semanticType
    || [source.semanticType, target.semanticType].includes('core.file.any')
}

function handleConnect(connection: Connection) {
  if (isWorkflowSwitching.value) return
  if (!connection.source || !connection.target || !connection.sourceHandle || !connection.targetHandle) return
  const targetAlreadyConnected = edges.value.some((edge) =>
    edge.target === connection.target && edge.targetHandle === connection.targetHandle,
  )
  if (targetAlreadyConnected) {
    showLayoutFeedback('这个输入端口已经有连接', 'error')
    return
  }
  const sourceData = nodes.value.find((node) => node.id === connection.source)?.data as WorkflowNodeData
  const targetData = nodes.value.find((node) => node.id === connection.target)?.data as WorkflowNodeData
  const sourcePortName = connection.sourceHandle.replace(/^out:/, '')
  const targetPortName = connection.targetHandle.replace(/^in:/, '')
  const sourcePort = sourceData.kind === 'input'
    ? { type: sourceData.outputs?.[0]?.type ?? 'File', semanticType: sourceData.semanticType }
    : sourceData.outputs?.find((port) => port.name === sourcePortName)
  const targetPort = targetData.kind === 'output'
    ? { type: targetData.inputs?.[0]?.type ?? 'File', semanticType: targetData.semanticType }
    : targetData.inputs?.find((port) => port.name === targetPortName)
  if (!portsAreCompatible(sourcePort && { name: sourcePortName, ...sourcePort }, targetPort && { name: targetPortName, ...targetPort })) {
    showLayoutFeedback('端口类型或语义类型不兼容', 'error')
    return
  }
  const id = uniqueEdgeId(`edge_${connection.source}_${connection.target}`)
  edges.value = [...edges.value, {
    ...connection,
    id,
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  } as Edge]
  void saveWorkflow(true)
}

function autoConnectSelection() {
  if (isWorkflowSwitching.value) return
  const selection = [...selectedGraphNodes.value]
  if (selection.length !== 2) return

  const ordered = selection.toSorted((a, b) =>
    activeLayoutDirection.value === 'RIGHT'
      ? a.position.x - b.position.x
      : a.position.y - b.position.y,
  )
  const sourceNode = ordered[0]!
  const targetNode = ordered[1]!
  const sourceData = sourceNode.data as WorkflowNodeData
  const targetData = targetNode.data as WorkflowNodeData
  const sourcePorts: ConnectionPort[] = sourceData.kind === 'input'
    ? [{ name: 'value', type: sourceData.outputs?.[0]?.type ?? 'File', semanticType: sourceData.semanticType }]
    : (sourceData.outputs ?? [])
  const targetPorts: ConnectionPort[] = targetData.kind === 'output'
    ? [{ name: 'value', type: targetData.inputs?.[0]?.type ?? 'File', semanticType: targetData.semanticType }]
    : (targetData.inputs ?? [])

  const candidates = sourcePorts.flatMap((source, sourceIndex) =>
    targetPorts
      .map((target, targetIndex) => ({ source, target, sourceIndex, targetIndex }))
      .filter(({ source, target }) => portsAreCompatible(source, target))
      .filter(({ target }) => !edges.value.some(
        (edge) => edge.target === targetNode.id && edge.targetHandle === `in:${target.name}`,
      ))
      .map((candidate) => ({
        ...candidate,
        score:
          (candidate.source.semanticType === candidate.target.semanticType ? 100 : 0)
          + (candidate.source.name === candidate.target.name ? 20 : 0),
      })),
  ).toSorted((a, b) =>
    b.score - a.score
    || a.sourceIndex - b.sourceIndex
    || a.targetIndex - b.targetIndex,
  )

  const match = candidates[0]
  if (!match) {
    showLayoutFeedback('两个节点之间没有可用的兼容端口', 'error')
    return
  }

  const id = uniqueEdgeId(`edge_${sourceNode.id}_${targetNode.id}`)
  edges.value = [...edges.value, {
    id,
    source: sourceNode.id,
    sourceHandle: `out:${match.source.name}`,
    target: targetNode.id,
    targetHandle: `in:${match.target.name}`,
    type: 'smoothstep',
    markerEnd: MarkerType.ArrowClosed,
  }]
  void saveWorkflow(true)
  showLayoutFeedback(
    `已连接 ${match.source.name} → ${match.target.name}${candidates.length > 1 ? '（按固定优先级选择）' : ''}`,
    'success',
  )
}

async function compileWorkflow() {
  if (
    compileState.value === 'running'
    || isWorkflowSwitching.value
    || saveState.value === 'saving'
  ) return
  compileState.value = 'running'
  const workflowSlug = selectedWorkflowSlug.value
  try {
    await saveWorkflow()
    if (workflowSlug !== selectedWorkflowSlug.value) {
      compileState.value = 'idle'
      return
    }
    const publishedVersion = await $fetch<{ version: number }>(
      `/api/v1/editor/workflows/${encodeURIComponent(workflowSlug)}/versions`,
      {
        method: 'POST',
        body: { reuse_unchanged: true },
      },
    )
    const response = await $fetch<{
      status: string
      validation: { diagnostics: Diagnostic[] }
      artifacts: Artifact[]
    }>('/api/v1/compilations', {
      method: 'POST',
      body: {
        request_version: '1.0.0',
        workflow_graph: graphWithCurrentLayout(),
        tool_specs: toolSpecs.value,
        workflow_version: publishedVersion.version,
      },
    })
    diagnostics.value = response.validation.diagnostics
    artifacts.value = response.artifacts
    compileState.value = response.status === 'succeeded' ? 'success' : 'error'
    const version: CompilationVersion = {
      id: `compile-${Date.now()}`,
      version: `v${publishedVersion.version}`,
      createdAt: new Intl.DateTimeFormat('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      }).format(new Date()),
      status: response.status === 'succeeded' ? 'succeeded' : 'failed',
      artifacts: response.artifacts,
    }
    compilationVersions.value = [version, ...compilationVersions.value]
    selectedCompilationId.value = version.id
    await loadWdlRevisions()
    inspectorTab.value = response.status === 'succeeded' ? 'artifacts' : 'diagnostics'
    activeRail.value = response.status === 'succeeded' ? 'artifacts' : 'edit'
  } catch (error: any) {
    const data = error?.data
    diagnostics.value = data?.validation?.diagnostics ?? [{
      code: 'SYS001',
      stage: 'system',
      severity: 'error',
      message: data?.error?.message ?? '编译服务暂时不可用。',
    }]
    compileState.value = 'error'
    inspectorTab.value = 'diagnostics'
  }
}

function graphWithCurrentLayout() {
  const graph = JSON.parse(JSON.stringify(workflowGraph.value ?? {}))
  const existingGraphNodes = new Map(
    (graph.nodes ?? []).map((node: Record<string, any>) => [node.id, node]),
  )
  graph.nodes = nodes.value.map((node) => {
    const data = node.data as WorkflowNodeData
    const existing = existingGraphNodes.get(node.id) as Record<string, any> | undefined
    if (data.kind === 'tool') {
      const spec = toolSpecs.value.find((item) =>
        item.id === data.toolId && item.tool_version === data.version,
      )
      return {
        ...(existing ?? {}),
        id: node.id,
        type: 'tool',
        label: data.label,
        tool_ref: {
          ...(existing?.tool_ref ?? {}),
          id: data.toolId,
          tool_version: data.version,
          spec_version: data.specVersion ?? spec?.schema_version,
          digest: data.toolDigest,
        },
        parameter_values: { ...(data.parameterValues ?? {}) },
      }
    }
    if (data.kind === 'subworkflow') {
      return {
        ...(existing ?? {}),
        id: node.id,
        type: 'subworkflow',
        label: data.label,
        description: data.description ?? '',
        subworkflow_ref: {
          slug: data.subworkflowSlug,
          version: Number(data.version),
          digest: data.subworkflowDigest,
        },
        interface_contract: contractFromNodeData(data),
      }
    }
    return {
      ...(existing ?? {}),
      id: node.id,
      type: data.kind === 'input' ? 'workflow_input' : 'workflow_output',
      label: data.label,
      port: {
        ...(existing?.port ?? {}),
        name: 'value',
        wdl_type: data.inputs?.[0]?.type ?? data.outputs?.[0]?.type ?? 'File',
        semantic_type: data.semanticType ?? 'core.file.any',
        ...(data.kind === 'input' ? { required: true } : {}),
      },
    }
  })
  graph.edges = edges.value.map((edge) => ({
    id: edge.id,
    source: {
      node_id: edge.source,
      port: edge.sourceHandle?.replace(/^out:/, '') ?? 'value',
    },
    target: {
      node_id: edge.target,
      port: edge.targetHandle?.replace(/^in:/, '') ?? 'value',
    },
  }))
  graph.layout = graph.layout ?? {}
  graph.layout.nodes = Object.fromEntries(
    nodes.value.map((node) => [node.id, { x: Math.round(node.position.x), y: Math.round(node.position.y) }]),
  )
  const viewport = vueFlowStore.value?.viewport.value
  if (viewport) graph.layout.viewport = { ...viewport }
  return graph
}

function mapCompilationHistory(records: CompilationHistoryRecord[]): CompilationVersion[] {
  return records.map((record, index) => ({
    id: String(record.id),
    version: record.workflow_version ? `v${record.workflow_version}` : `编译 ${records.length - index}`,
    createdAt: new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(record.created_at)),
    status: record.status === 'succeeded' ? 'succeeded' : 'failed',
    artifacts: record.artifacts,
  }))
}

async function loadWorkflow(slug = selectedWorkflowSlug.value) {
  if (
    !slug
    || isWorkflowSwitching.value
    || saveState.value === 'saving'
    || compileState.value === 'running'
  ) return false
  const sequence = ++workflowLoadSequence
  const hadActiveDocument = Boolean(workflowGraph.value)
  workflowSwitchState.value = 'loading'
  switchingWorkflowSlug.value = slug
  if (!hadActiveDocument) saveState.value = 'loading'
  try {
    const [document, history, revisionIndex] = await Promise.all([
      $fetch<WorkflowDocumentPayload>(
        `/api/v1/editor/workflows/${encodeURIComponent(slug)}`,
      ),
      $fetch<{ results: CompilationHistoryRecord[] }>(
        `/api/v1/editor/workflows/${encodeURIComponent(slug)}/compilations`,
      ),
      $fetch<{ results: WdlRevision[] }>(
        `/api/v1/editor/workflows/${encodeURIComponent(slug)}/wdl-versions`,
      ),
    ])
    const firstRevision = revisionIndex.results[0]
      ? await $fetch<WdlRevision>(
          `/api/v1/editor/workflows/${encodeURIComponent(slug)}/wdl-versions/${revisionIndex.results[0].version}`,
        )
      : undefined
    if (sequence !== workflowLoadSequence) return false

    const positions = new Map(document.editor_document.nodes?.map((item) => [item.id, item.position]))
    const nextNodes: Node<WorkflowNodeData>[] = (document.workflow_graph.nodes ?? []).map(
      (node: Record<string, any>, index: number) =>
        graphNodeToCanvasNode(
          node,
          positions.get(node.id)
            ?? document.workflow_graph.layout?.nodes?.[node.id]
            ?? { x: 48 + (index % 3) * 320, y: 80 + Math.floor(index / 3) * 200 },
          document.tool_specs,
        ),
    )
    const nextEdges = (document.workflow_graph.edges ?? []).map(graphEdgeToCanvasEdge)
    const nextCompilations = mapCompilationHistory(history.results)
    const nextRevisions = firstRevision
      ? revisionIndex.results.map((revision) =>
          revision.version === firstRevision.version
            ? { ...revision, ...firstRevision }
            : revision,
        )
      : []

    selectedWorkflowSlug.value = slug
    workflowGraph.value = document.workflow_graph
    toolSpecs.value = document.tool_specs
    workflowDescription.value = document.description ?? ''
    workflowKind.value = document.kind ?? 'workflow'
    nodes.value = nextNodes
    edges.value = nextEdges
    compilationVersions.value = nextCompilations
    selectedCompilationId.value = nextCompilations[0]?.id ?? ''
    artifacts.value = nextCompilations[0]?.artifacts ?? []
    wdlRevisions.value = nextRevisions
    selectedWdlVersion.value = nextRevisions[0]?.version
    selectedNodeId.value = nextNodes[0]?.id ?? ''
    selectedNodeIds.value = []
    diagnostics.value = []
    compileState.value = 'idle'
    editingWdl.value = false
    editingWorkflowMetadata.value = false
    undoStack.value = []
    redoStack.value = []
    saveState.value = 'saved'
    lastSavedAt.value = new Date()
    workflowSwitchState.value = 'idle'
    switchingWorkflowSlug.value = ''
    await nextTick()
    vueFlowStore.value?.updateNodeInternals(nextNodes.map((node) => node.id))
    void loadWorkflowLibrary()
    return true
  } catch (error) {
    console.error('Failed to load workflow document', error)
    if (sequence === workflowLoadSequence) {
      workflowSwitchState.value = 'error'
      switchingWorkflowSlug.value = ''
      if (!hadActiveDocument) saveState.value = 'error'
      showLayoutFeedback(`无法打开流程 ${slug}，当前编辑内容未受影响`, 'error')
    }
    return false
  }
}

async function saveWorkflow(silent = false) {
  if (
    !workflowGraph.value
    || saveState.value === 'saving'
    || isWorkflowSwitching.value
  ) return
  const workflowSlug = selectedWorkflowSlug.value
  saveState.value = 'saving'
  try {
    const graph = graphWithCurrentLayout()
    await $fetch(`/api/v1/editor/workflows/${encodeURIComponent(workflowSlug)}`, {
      method: 'PUT',
      body: {
        name: graph.name,
        description: workflowDescription.value,
        kind: workflowKind.value,
        subworkflow_references: graph.nodes
          .filter((node: Record<string, any>) => node.type === 'subworkflow')
          .map((node: Record<string, any>) => node.subworkflow_ref),
        workflow_graph: graph,
        tool_specs: toolSpecs.value,
        editor_document: {
          nodes: nodes.value.map((node) => ({ id: node.id, position: node.position })),
          viewport: graph.layout.viewport,
        },
      },
    })
    if (workflowSlug !== selectedWorkflowSlug.value) return
    workflowGraph.value = graph
    saveState.value = 'saved'
    lastSavedAt.value = new Date()
    if (!silent) editingWorkflowMetadata.value = false
  } catch (error) {
    console.error('Failed to save workflow document', error)
    saveState.value = 'error'
    throw error
  }
}

async function validateWorkflow() {
  if (isWorkflowSwitching.value) return
  try {
    const response = await $fetch<{ validation: { diagnostics: Diagnostic[] } }>(
      '/api/v1/validations/workflow-graph',
      {
        method: 'POST',
        body: {
          request_version: '1.0.0',
          workflow_graph: graphWithCurrentLayout(),
          tool_specs: toolSpecs.value,
        },
      },
    )
    diagnostics.value = response.validation.diagnostics
  } catch {
    diagnostics.value = [{
      code: 'SYS001',
      stage: 'system',
      severity: 'error',
      message: '无法连接验证服务。',
    }]
  }
  inspectorTab.value = 'diagnostics'
  activeRail.value = 'edit'
}

function selectRail(section: RailSection) {
  if (section === 'packages') {
    void navigateTo('/wdl-packages')
    return
  }
  if (section === 'runs') {
    void navigateTo('/runs')
    return
  }
  if (section === 'wdl') {
    void navigateTo('/wdl')
    return
  }
  activeRail.value = section
  if (section === 'tools') {
    activeLibrary.value = 'tools'
    loadToolRegistry()
    nextTick(() => document.querySelector<HTMLInputElement>('.search-field input')?.focus())
  }
  if (section === 'artifacts') inspectorTab.value = 'artifacts'
  if (section === 'help') {
    diagnostics.value = [{
      code: 'HELP',
      stage: 'system',
      severity: 'warning',
      message: '先从左侧添加输入与工具，连接端口后点击“验证”，通过后再编译为 WDL。',
    }]
    inspectorTab.value = 'diagnostics'
  }
}

function updateWorkflowName(name: string) {
  if (workflowGraph.value) workflowGraph.value.name = name
}

async function openSubworkflowDetail() {
  const slug = selectedData.value?.subworkflowSlug
  if (!slug) return
  if (await loadWorkflow(slug)) activeRail.value = 'edit'
}

function selectSubworkflowUpgrade(event: Event) {
  subworkflowUpgradeTargetVersion.value = Number((event.target as HTMLSelectElement).value)
  subworkflowUpgradeState.value = 'idle'
  subworkflowUpgradeMessage.value = ''
}

async function upgradeSelectedSubworkflow() {
  const node = selectedNode.value
  const data = selectedData.value
  const target = selectedSubworkflowUpgrade.value
  if (
    !node
    || data?.kind !== 'subworkflow'
    || !target
    || saveState.value === 'saving'
    || isWorkflowSwitching.value
  ) return

  const previousNodes = nodes.value
  subworkflowUpgradeNodeId.value = node.id
  subworkflowUpgradeState.value = 'saving'
  subworkflowUpgradeMessage.value = `正在升级到 v${target.version} 并保存父流程…`
  nodes.value = nodes.value.map((item) => {
    if (item.id !== node.id) return item
    return {
      ...item,
      data: {
        ...(item.data as WorkflowNodeData),
        version: String(target.version),
        subworkflowDigest: target.semantic_digest,
        interfaceContract: target.interface_contract,
        inputs: (target.interface_contract.inputs ?? []).map((port) => ({
          name: port.name,
          type: port.wdl_type,
          semanticType: port.semantic_type,
        })),
        outputs: (target.interface_contract.outputs ?? []).map((port) => ({
          name: port.name,
          type: port.wdl_type,
          semanticType: port.semantic_type,
        })),
      },
    }
  })

  try {
    await nextTick()
    vueFlowStore.value?.updateNodeInternals([node.id])
    await saveWorkflow(true)
  } catch {
    nodes.value = previousNodes
    await nextTick()
    vueFlowStore.value?.updateNodeInternals([node.id])
    subworkflowUpgradeState.value = 'error'
    subworkflowUpgradeMessage.value = `升级到 v${target.version} 未保存，当前固定版本保持不变。`
    showLayoutFeedback('子流程升级保存失败', 'error')
    return
  }

  try {
    const response = await $fetch<{
      validation: { status: string; diagnostics: Diagnostic[] }
    }>('/api/v1/validations/workflow-graph', {
      method: 'POST',
      body: {
        request_version: '1.0.0',
        workflow_graph: graphWithCurrentLayout(),
        tool_specs: toolSpecs.value,
      },
    })
    diagnostics.value = response.validation.diagnostics
    const errorCount = diagnostics.value.filter((item) => item.severity === 'error').length
    subworkflowUpgradeState.value = errorCount ? 'warning' : 'success'
    subworkflowUpgradeMessage.value = errorCount
      ? `已固定到 v${target.version} 并保存；验证发现 ${errorCount} 个问题，请查看诊断。`
      : `已固定到 v${target.version}，父流程保存且验证通过。`
    showLayoutFeedback(
      errorCount ? `子流程已升级，验证发现 ${errorCount} 个问题` : `子流程已升级到 v${target.version}`,
      errorCount ? 'error' : 'success',
    )
  } catch {
    diagnostics.value = [{
      code: 'SYS001',
      stage: 'system',
      severity: 'error',
      message: '子流程版本已保存，但无法连接验证服务。',
    }]
    subworkflowUpgradeState.value = 'warning'
    subworkflowUpgradeMessage.value = `已固定到 v${target.version} 并保存，但验证服务暂不可用。`
    showLayoutFeedback('子流程已升级，但验证服务不可用', 'error')
  }
}

async function selectWorkflowDocument(slug: string) {
  if (
    slug === selectedWorkflowSlug.value
    || isWorkflowSwitching.value
    || saveState.value === 'saving'
    || compileState.value === 'running'
  ) return
  await loadWorkflow(slug)
}

async function loadToolRegistry() {
  try {
    const response = await $fetch<{ results: ToolRegistryEntry[] }>('/api/v1/tools')
    registryTools.value = response.results
    if (!selectedToolId.value && response.results[0]) {
      await loadToolVersions(response.results[0].tool_id)
    }
  } catch (error) {
    console.error('Failed to load tool registry', error)
  } finally {
    toolRegistryLoaded.value = true
  }
}

async function loadToolVersions(toolId: string) {
  selectedToolId.value = toolId
  selectedToolVersions.value = []
  selectedToolSpec.value = undefined
  toolDraft.value = undefined
  toolDraftState.value = 'idle'
  toolDraftValidationStatus.value = undefined
  toolOperationError.value = undefined
  try {
    const response = await $fetch<{ results: ToolVersionEntry[] }>(
      `/api/v1/tools/${encodeURIComponent(toolId)}/versions`,
    )
    selectedToolVersions.value = response.results
    const draftLoaded = await loadToolDraft(toolId)
    const latest = response.results[0]
    if (!draftLoaded && latest) await loadToolVersionDetail(toolId, latest.version)
  } catch (error) {
    console.error('Failed to load tool versions', error)
    selectedToolVersions.value = []
  }
}

async function loadToolDraft(toolId: string) {
  try {
    const response = await $fetch<{
      draft_spec: Record<string, any>
      validation: { status?: string; diagnostics?: Diagnostic[] }
    }>(`/api/v1/tools/${encodeURIComponent(toolId)}/drafts`)
    toolDraft.value = hydrateToolDraft(response.draft_spec, toolId)
    toolDraftValidationStatus.value = response.validation.status
    toolDraftState.value = 'idle'
    toolOperationError.value = undefined
    return true
  } catch (error: any) {
    if (error?.statusCode !== 404 && error?.status !== 404) {
      console.error('Failed to load tool draft', error)
    }
    return false
  }
}

async function loadToolVersionDetail(toolId: string, version: string) {
  try {
    const detail = await $fetch<ToolVersionEntry>(
      `/api/v1/tools/${encodeURIComponent(toolId)}/versions/${encodeURIComponent(version)}`,
    )
    selectedToolSpec.value = detail.tool_spec
    toolDraft.value = detail.tool_spec ? hydrateToolDraft(detail.tool_spec, toolId) : undefined
    toolDraftState.value = 'idle'
    toolDraftValidationStatus.value = undefined
    toolOperationError.value = undefined
  } catch (error) {
    console.error('Failed to load tool version detail', error)
  }
}

async function saveToolDraft() {
  if (!selectedToolId.value || !toolDraft.value) return false
  toolDraftState.value = 'saving'
  toolOperationError.value = undefined
  try {
    const response = await $fetch<{
      draft_spec: Record<string, any>
      validation: { status: string; diagnostics: Diagnostic[] }
    }>(`/api/v1/tools/${encodeURIComponent(selectedToolId.value)}/drafts`, {
      method: 'PUT',
      body: { tool_spec: toolDraft.value },
    })
    toolDraft.value = hydrateToolDraft(response.draft_spec, selectedToolId.value)
    toolDraftValidationStatus.value = response.validation.status
    diagnostics.value = response.validation.diagnostics ?? []
    toolDraftState.value = 'saved'
    await loadToolRegistry()
    if (response.validation.status !== 'valid') {
      showLayoutFeedback('草稿已保存，但还有校验问题需要修正', 'error')
    }
    return response.validation.status === 'valid'
  } catch (error) {
    console.error('Failed to save tool draft', error)
    toolDraftState.value = 'error'
    showLayoutFeedback('工具草稿保存失败', 'error')
    return false
  }
}

function markToolDraftDirty() {
  if (toolDraftState.value === 'saving' || toolDraftState.value === 'publishing') return
  toolDraftState.value = 'idle'
  toolDraftValidationStatus.value = undefined
  toolOperationError.value = undefined
}

async function publishToolDraft() {
  if (!selectedToolId.value || !toolDraft.value) return
  const valid = await saveToolDraft()
  if (!valid) return

  toolDraftState.value = 'publishing'
  try {
    const published = await $fetch<ToolVersionEntry>(
      `/api/v1/tools/${encodeURIComponent(selectedToolId.value)}/publish`,
      { method: 'POST' },
    )
    toolDraftState.value = 'published'
    selectedToolSpec.value = published.tool_spec
    toolOperationError.value = undefined
    showLayoutFeedback(`已发布 ${selectedToolId.value} v${published.version}`, 'success')
    await loadToolRegistry()
    await loadToolVersions(selectedToolId.value)
    toolDraftState.value = 'published'
  } catch (error: any) {
    const response = error?.data
    const code = response?.error?.code ?? 'TOOL_PUBLISH_FAILED'
    toolOperationError.value = {
      code,
      message: code === 'TOOL_VERSION_IMMUTABLE'
        ? '该工具版本已发布且内容不可修改；请提升软件版本后重新发布。'
        : response?.error?.message ?? '工具版本发布失败。',
    }
    diagnostics.value = response?.validation?.diagnostics ?? [{
      code,
      stage: 'tool',
      severity: 'error',
      message: toolOperationError.value.message,
    }]
    toolDraftState.value = 'error'
    showLayoutFeedback('工具版本发布失败，请查看校验信息', 'error')
  }
}

async function createTool() {
  const toolId = normalizeIdentifier(newToolId.value, '')
  if (!toolId || !WDL_IDENTIFIER_PATTERN.test(toolId)) {
    toolCreateState.value = 'error'
    showLayoutFeedback('请输入合法的 WDL 工具 ID', 'error')
    return
  }
  if (registryTools.value.some((tool) => tool.tool_id === toolId)) {
    toolCreateState.value = 'error'
    showLayoutFeedback(`工具 ${toolId} 已存在，请直接打开编辑`, 'error')
    return
  }

  toolCreateState.value = 'saving'
  selectedToolId.value = toolId
  selectedToolVersions.value = []
  selectedToolSpec.value = undefined
  toolDraft.value = createToolDraft(toolId)
  toolDraftValidationStatus.value = undefined
  toolOperationError.value = undefined
  const saved = await saveToolDraft()
  if (!saved) {
    toolCreateState.value = 'error'
    return
  }

  toolCreateState.value = 'idle'
  creatingTool.value = false
  newToolId.value = ''
  showLayoutFeedback(`已创建工具草稿 ${toolId}`, 'success')
}

async function loadWorkflowLibrary() {
  try {
    const response = await $fetch<{ results: WorkflowLibraryEntry[] }>('/api/v1/editor/workflows')
    workflowDocuments.value = response.results
    const subflows = response.results.filter((item) => item.kind === 'subworkflow' && item.latest_version)
    const versionResponses = await Promise.all(subflows.map((item) =>
      $fetch<{ results: WorkflowVersionEntry[] }>(
        `/api/v1/editor/workflows/${encodeURIComponent(item.slug)}/versions`,
      ),
    ))
    subworkflowVersions.value = versionResponses
      .flatMap((response) => response.results)
      .filter((item) => item.kind === 'subworkflow')
      .toSorted((a, b) => a.name.localeCompare(b.name) || b.version - a.version)
  } catch (error) {
    console.error('Failed to load workflow library', error)
  }
}

async function loadWdlRevisions() {
  const workflowSlug = selectedWorkflowSlug.value
  try {
    const response = await $fetch<{ results: WdlRevision[] }>(
      `/api/v1/editor/workflows/${encodeURIComponent(workflowSlug)}/wdl-versions`,
    )
    if (workflowSlug !== selectedWorkflowSlug.value) return
    wdlRevisions.value = response.results
    selectedWdlVersion.value = response.results[0]?.version
    if (response.results[0]) await loadWdlRevisionDetail(response.results[0].version)
  } catch {
    wdlRevisions.value = previewArtifact.value
      ? [{
          version: Number(selectedCompilation.value?.version.replace(/\D/g, '')) || 1,
          source: 'system',
          content: previewArtifact.value.content,
          workflow_version: Number(selectedCompilation.value?.version.replace(/\D/g, '')) || null,
          created_at: new Date().toISOString(),
        }]
      : []
  }
}

async function loadWdlRevisionDetail(version: number) {
  selectedWdlVersion.value = version
  const existing = wdlRevisions.value.find((revision) => revision.version === version)
  if (existing?.content) return
  try {
    const detail = await $fetch<WdlRevision>(
      `/api/v1/editor/workflows/${encodeURIComponent(selectedWorkflowSlug.value)}/wdl-versions/${version}`,
    )
    wdlRevisions.value = wdlRevisions.value.map((revision) =>
      revision.version === version ? { ...revision, ...detail } : revision,
    )
  } catch (error) {
    console.error('Failed to load WDL revision detail', error)
  }
}

function beginWdlEdit() {
  wdlDraft.value = activeWdlContent.value
  editingWdl.value = true
  wdlSaveState.value = 'idle'
}

async function saveManualWdl() {
  if (!wdlDraft.value.trim()) return
  wdlSaveState.value = 'saving'
  try {
    const revision = await $fetch<WdlRevision>(
      `/api/v1/editor/workflows/${encodeURIComponent(selectedWorkflowSlug.value)}/wdl-versions`,
      {
        method: 'POST',
        body: {
          content: wdlDraft.value,
          source: 'manual',
          workflow_version: selectedCompilation.value?.version.replace(/\D/g, '') || null,
        },
      },
    )
    wdlRevisions.value = [revision, ...wdlRevisions.value]
    selectedWdlVersion.value = revision.version
    editingWdl.value = false
  } catch {
    wdlSaveState.value = 'error'
  }
}

function openArtifactPreview(artifact: Artifact) {
  previewDrawerArtifact.value = artifact
}

async function copyWdl() {
  if (!activeWdlContent.value) return
  await navigator.clipboard.writeText(activeWdlContent.value)
  copiedArtifact.value = 'workflow.wdl'
  window.setTimeout(() => {
    copiedArtifact.value = ''
  }, 1800)
}

async function importToolSpec(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  try {
    const tool = JSON.parse(await file.text())
    const response = await $fetch<{ validation: { status: string; diagnostics: Diagnostic[] } }>(
      '/api/v1/validations/tool-spec',
      { method: 'POST', body: { request_version: '1.0.0', tool_spec: tool } },
    )
    diagnostics.value = response.validation.diagnostics
    if (response.validation.status === 'valid') {
      toolSpecs.value = [...toolSpecs.value.filter((item) => item.id !== tool.id), tool]
      await $fetch(`/api/v1/tools/${encodeURIComponent(tool.id)}/versions`, {
        method: 'POST',
        body: { tool_spec: tool },
      })
      await saveWorkflow()
      await loadToolRegistry()
    }
    inspectorTab.value = 'diagnostics'
  } catch {
    diagnostics.value = [{ code: 'PARSE001', stage: 'parse', severity: 'error', message: 'ToolSpec JSON 无法解析。' }]
    inspectorTab.value = 'diagnostics'
  } finally {
    if (importInput.value) importInput.value.value = ''
  }
}

function syncCanvasMode() {
  showInteractiveCanvas.value = !narrowViewport?.matches
}

onMounted(() => {
  narrowViewport = window.matchMedia('(max-width: 48rem)')
  syncCanvasMode()
  narrowViewport.addEventListener('change', syncCanvasMode)
  void Promise.all([loadWorkflow(), loadToolRegistry()]).then(() => {
    const requestedTool = route.query.tool
    if (typeof requestedTool === 'string' && requestedTool) {
      void loadToolVersions(requestedTool)
    }
  })
  const requestedSection = route.query.section
  if (
    typeof requestedSection === 'string'
    && ['edit', 'tools', 'artifacts', 'help'].includes(requestedSection)
  ) {
    selectRail(requestedSection as RailSection)
  }
  window.addEventListener('keydown', handleCanvasDeleteKey)
  autosaveTimer = window.setInterval(() => {
    void saveWorkflow(true)
  }, 60_000)
})

onBeforeUnmount(() => {
  narrowViewport?.removeEventListener('change', syncCanvasMode)
  window.removeEventListener('keydown', handleCanvasDeleteKey)
  window.clearTimeout(layoutFeedbackTimer)
  window.clearTimeout(positionSaveTimer)
  window.clearInterval(autosaveTimer)
})
</script>

<template>
  <div class="app-shell">
    <a class="skip-link" href="#workflow-canvas">跳到工作流画布</a>
    <input ref="importInput" class="visually-hidden" type="file" accept="application/json,.json" @change="importToolSpec" />

    <AppTopbar section="流程库" :current="currentWorkflowName">
      <template #status>
        <button
          class="save-state save-state--button"
          type="button"
          aria-label="保存草稿"
          :disabled="isWorkflowSwitching"
          @click="saveWorkflow()"
        >
          <span class="status-dot" />
          {{
            isWorkflowSwitching ? `正在打开 ${switchingWorkflowSlug}…`
              : saveState === 'loading' ? '正在载入…'
              : saveState === 'saving' ? '正在保存…'
                : saveState === 'error' ? '保存失败，重试'
                  : '草稿已保存'
          }}
        </button>
      </template>
      <template #actions>
        <button class="button button--ghost" type="button" :disabled="isWorkflowSwitching" @click="validateWorkflow">验证</button>
        <button
          class="button button--primary"
          type="button"
          :disabled="compileState === 'running' || isWorkflowSwitching || saveState === 'saving'"
          @click="compileWorkflow"
        >
          {{
            compileState === 'running'
              ? '正在编译…'
              : compileState === 'success'
                ? '编译完成'
                : compileState === 'error'
                  ? '编译失败'
                : '编译流程'
          }}
        </button>
      </template>
    </AppTopbar>

    <AppRail :active="activeRail" @select="selectRail" />

    <section
      v-if="activeRail !== 'edit'"
      class="section-workspace"
      :class="{ 'section-workspace--flow-library': activeRail === 'artifacts' }"
    >
      <ToolLibraryWorkspace
        v-if="activeRail === 'tools'"
        v-model:creating-tool="creatingTool"
        v-model:new-tool-id="newToolId"
        v-model:search-query="registrySearchQuery"
        v-model:tool-draft="toolDraft"
        :tools="filteredRegistryTools"
        :registry-loaded="toolRegistryLoaded"
        :selected-tool-id="selectedToolId"
        :selected-tool-versions="selectedToolVersions"
        :tool-draft-state="toolDraftState"
        :tool-draft-validation-status="toolDraftValidationStatus"
        :tool-operation-error="toolOperationError"
        :tool-create-state="toolCreateState"
        @create="createTool"
        @import="importInput?.click()"
        @select-tool="loadToolVersions"
        @select-version="loadToolVersionDetail"
        @draft-dirty="markToolDraftDirty"
        @draft-save="saveToolDraft"
        @draft-publish="publishToolDraft"
      />

      <WorkflowLibraryWorkspace
        v-else-if="activeRail === 'artifacts'"
        v-model:editing-metadata="editingWorkflowMetadata"
        :workflow-name="currentWorkflowName"
        @update:workflow-name="updateWorkflowName"
        v-model:workflow-kind="workflowKind"
        v-model:workflow-description="workflowDescription"
        v-model:editing-wdl="editingWdl"
        v-model:wdl-draft="wdlDraft"
        :workflow-slug="selectedWorkflowSlug"
        :workflow-documents="workflowDocuments"
        :is-workflow-switching="isWorkflowSwitching"
        :switching-workflow-slug="switchingWorkflowSlug"
        :save-state="saveState"
        :compile-state="compileState"
        :compilation-versions="compilationVersions"
        :selected-compilation-id="selectedCompilation?.id ?? ''"
        :selected-compilation-version="selectedCompilation?.version"
        :wdl-revisions="wdlRevisions"
        :selected-wdl-version="selectedWdlVersion"
        :selected-wdl-revision="selectedWdlRevision"
        :active-wdl-content="activeWdlContent"
        :preview-lines="previewLines"
        :wdl-save-state="wdlSaveState"
        :copied-artifact="copiedArtifact"
        @open-editor="selectRail('edit')"
        @save-metadata="saveWorkflow()"
        @select-workflow="selectWorkflowDocument"
        @select-compilation="selectedCompilationId = $event"
        @select-wdl-version="loadWdlRevisionDetail"
        @begin-wdl-edit="beginWdlEdit"
        @save-wdl="saveManualWdl"
        @copy-wdl="copyWdl"
      />

      <HelpWorkspace v-else />
    </section>

    <EditorNodeLibraryPanel
      v-if="activeRail === 'edit'"
      v-model:active-library="activeLibrary"
      v-model:search-query="searchQuery"
      :tools="filteredTools"
      :subworkflows="filteredSubworkflows"
      :workflow-inputs="workflowInputs"
      :workflow-outputs="workflowOutputs"
      :tool-registry-loaded="toolRegistryLoaded"
      :is-workflow-switching="isWorkflowSwitching"
      @quick-add="quickAddLibraryNode"
      @drag-start="startLibraryDrag"
      @drag-end="finishLibraryDrag"
      @import-tool-spec="importInput?.click()"
    />

    <main
      v-if="activeRail === 'edit'"
      id="workflow-canvas"
      class="canvas-panel"
      :class="{ 'canvas-panel--dragging': isLibraryDragging, 'canvas-panel--drop-active': isCanvasDropActive }"
      :aria-busy="isWorkflowSwitching"
      tabindex="-1"
      @dragover="handleCanvasDragOver"
      @dragleave="handleCanvasDragLeave"
      @drop="handleCanvasDrop"
    >
      <div class="canvas-toolbar" aria-label="画布工具">
        <button type="button" aria-label="选择工具" class="canvas-tool canvas-tool--active">↖</button>
        <button type="button" aria-label="添加便签" class="canvas-tool">A</button>
        <span class="canvas-toolbar__divider" />
        <button
          type="button"
          aria-label="撤销画布调整"
          class="canvas-tool"
          :disabled="undoStack.length === 0 || layoutState === 'running' || isWorkflowSwitching"
          @click="undoLayoutChange"
        >
          ↶
        </button>
        <button
          type="button"
          aria-label="重做画布调整"
          class="canvas-tool"
          :disabled="redoStack.length === 0 || layoutState === 'running' || isWorkflowSwitching"
          @click="redoLayoutChange"
        >
          ↷
        </button>
        <span class="canvas-toolbar__divider" />
        <button
          type="button"
          class="canvas-tool canvas-tool--direction"
          :aria-label="`切换布局方向，当前为${layoutDirectionLabel}`"
          :title="`布局方向：${layoutDirectionLabel}`"
          :disabled="layoutState === 'running' || isWorkflowSwitching"
          @click="toggleLayoutDirection"
        >
          {{ layoutDirection === 'RIGHT' ? '↔' : '↕' }}
        </button>
        <button
          type="button"
          class="canvas-arrange"
          :disabled="layoutState === 'running' || isWorkflowSwitching"
          :aria-label="`${autoLayoutLabel}，${layoutDirectionLabel}`"
          @click="runAutoLayout"
        >
          <span v-if="layoutState === 'running'" class="canvas-arrange__spinner" aria-hidden="true" />
          <svg v-else viewBox="0 0 16 16" aria-hidden="true">
            <rect x="1.5" y="2" width="4" height="4" rx="1" />
            <rect x="10.5" y="1" width="4" height="4" rx="1" />
            <rect x="10.5" y="11" width="4" height="4" rx="1" />
            <path d="M5.5 4h2.25A2.25 2.25 0 0 1 10 6.25v5" />
          </svg>
          <span>{{ layoutState === 'running' ? '整理中' : autoLayoutLabel }}</span>
        </button>
      </div>

      <div
        v-if="canAlign"
        class="selection-toolbar"
        role="toolbar"
        :aria-label="`${selectedNodeCount} 个选中节点的对齐工具`"
      >
        <span class="selection-toolbar__count">{{ selectedNodeCount }} 项</span>
        <button
          v-if="selectedNodeCount === 2"
          type="button"
          class="selection-toolbar__connect"
          title="按类型和语义自动选择端口"
          aria-label="自动连接两个选中节点"
          @click="autoConnectSelection"
        >
          自动连接
        </button>
        <span class="canvas-toolbar__divider" />
        <button type="button" aria-label="左对齐" title="左对齐" @click="alignSelection('left')">
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M3 2v14" />
            <rect x="5.5" y="4" width="8" height="3" rx=".5" />
            <rect x="5.5" y="11" width="5" height="3" rx=".5" />
          </svg>
        </button>
        <button
          type="button"
          aria-label="水平居中"
          title="水平居中"
          @click="alignSelection('center-x')"
        >
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M9 2v14" />
            <rect x="4" y="4" width="10" height="3" rx=".5" />
            <rect x="5.5" y="11" width="7" height="3" rx=".5" />
          </svg>
        </button>
        <button type="button" aria-label="右对齐" title="右对齐" @click="alignSelection('right')">
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M15 2v14" />
            <rect x="4.5" y="4" width="8" height="3" rx=".5" />
            <rect x="7.5" y="11" width="5" height="3" rx=".5" />
          </svg>
        </button>
        <span class="canvas-toolbar__divider" />
        <button type="button" aria-label="顶部对齐" title="顶部对齐" @click="alignSelection('top')">
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M2 3h14" />
            <rect x="4" y="5.5" width="3" height="8" rx=".5" />
            <rect x="11" y="5.5" width="3" height="5" rx=".5" />
          </svg>
        </button>
        <button
          type="button"
          aria-label="垂直居中"
          title="垂直居中"
          @click="alignSelection('center-y')"
        >
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M2 9h14" />
            <rect x="4" y="4" width="3" height="10" rx=".5" />
            <rect x="11" y="5.5" width="3" height="7" rx=".5" />
          </svg>
        </button>
        <button type="button" aria-label="底部对齐" title="底部对齐" @click="alignSelection('bottom')">
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M2 15h14" />
            <rect x="4" y="4.5" width="3" height="8" rx=".5" />
            <rect x="11" y="7.5" width="3" height="5" rx=".5" />
          </svg>
        </button>
        <span class="canvas-toolbar__divider" />
        <button
          type="button"
          aria-label="水平等距分布"
          title="水平等距分布"
          :disabled="!canDistribute"
          @click="alignSelection('distribute-x')"
        >
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M2 3v12M16 3v12" />
            <rect x="4" y="6" width="3" height="6" rx=".5" />
            <rect x="11" y="5" width="3" height="8" rx=".5" />
          </svg>
        </button>
        <button
          type="button"
          aria-label="垂直等距分布"
          title="垂直等距分布"
          :disabled="!canDistribute"
          @click="alignSelection('distribute-y')"
        >
          <svg viewBox="0 0 18 18" aria-hidden="true">
            <path d="M3 2h12M3 16h12" />
            <rect x="6" y="4" width="6" height="3" rx=".5" />
            <rect x="5" y="11" width="8" height="3" rx=".5" />
          </svg>
        </button>
      </div>

      <div class="canvas-meta">
        <div>
          <strong>{{ currentWorkflowName }}</strong>
          <span>{{ nodes.length }} 个节点 · {{ edges.length }} 条连接</span>
        </div>
        <span class="autosave-label">{{ saveState === 'saving' ? '正在自动保存…' : lastSavedLabel }}</span>
        <span class="target-badge">WDL 1.0</span>
      </div>

      <section class="mobile-overview" aria-label="工作流只读摘要">
        <header>
          <div>
            <span>只读预览</span>
            <h1>{{ currentWorkflowName }}</h1>
          </div>
          <span class="mobile-overview__status">Graph 有效</span>
        </header>
        <p>完整拖拽编辑器适用于桌面端。当前可查看流程结构、类型和校验状态。</p>
        <ol class="mobile-flow">
          <li>
            <span>2 个输入</span>
            <strong>Paired-end FASTQ</strong>
            <code>bio.fastq.gz.r1 / r2</code>
          </li>
          <li class="mobile-flow__tool">
            <span>工具</span>
            <strong>fastp → BWA-MEM</strong>
            <code>v0.23.4 → v0.7.17</code>
          </li>
          <li>
            <span>3 个输出</span>
            <strong>Aligned BAM + report</strong>
            <code>BAM / HTML</code>
          </li>
        </ol>
        <dl class="mobile-overview__meta">
          <div>
            <dt>目标</dt>
            <dd>WDL 1.0</dd>
          </div>
          <div>
            <dt>错误</dt>
            <dd>0</dd>
          </div>
          <div>
            <dt>提醒</dt>
            <dd>1</dd>
          </div>
        </dl>
      </section>

      <ClientOnly v-if="showInteractiveCanvas">
        <VueFlow
          v-model:nodes="nodes"
          v-model:edges="edges"
          class="workflow-flow"
          :class="{ 'workflow-flow--arranging': layoutState === 'running' }"
          :fit-view-on-init="true"
          :min-zoom="0.55"
          :max-zoom="1.6"
          :snap-to-grid="true"
          :snap-grid="[16, 16]"
          :elevate-nodes-on-select="true"
          :default-edge-options="{ style: { strokeWidth: 1.5 } }"
          @init="handleFlowInit"
          @node-click="handleNodeClick"
          @node-drag-start="handleNodeDragStart"
          @node-drag="handleNodeDrag"
          @node-drag-stop="handleNodeDragStop"
          @selection-drag-start="handleNodeDragStart"
          @selection-drag="handleNodeDrag"
          @selection-drag-stop="handleNodeDragStop"
          @connect="handleConnect"
        >
          <template #node-workflow="nodeProps">
            <WorkflowNode
              v-bind="nodeProps"
              :selected="selectedNodeIds.includes(nodeProps.id)"
            />
          </template>
        </VueFlow>
        <template #fallback>
          <div class="canvas-loading" aria-live="polite">
            <span />
            正在加载工作流画布…
          </div>
        </template>
      </ClientOnly>

      <div v-if="isWorkflowSwitching" class="workflow-switch-overlay" role="status" aria-live="polite">
        <span aria-hidden="true" />
        <div>
          <strong>正在打开流程</strong>
          <small>{{ switchingWorkflowSlug }}</small>
        </div>
      </div>

      <div
        v-if="alignmentGuides.x !== null"
        class="alignment-guide alignment-guide--vertical"
        :style="verticalGuideStyle"
        aria-hidden="true"
      />
      <div
        v-if="alignmentGuides.y !== null"
        class="alignment-guide alignment-guide--horizontal"
        :style="horizontalGuideStyle"
        aria-hidden="true"
      />

      <div
        v-if="layoutMessage"
        class="layout-feedback"
        :class="`layout-feedback--${layoutState}`"
        role="status"
        aria-live="polite"
      >
        <span v-if="layoutState === 'running'" class="layout-feedback__spinner" aria-hidden="true" />
        <span v-else class="layout-feedback__dot" aria-hidden="true" />
        {{ layoutMessage }}
      </div>

      <div class="canvas-status">
        <span class="status-dot" />
        <strong>{{ diagnostics.some((item) => item.severity === 'error') ? 'Graph 有错误' : 'Graph 已验证' }}</strong>
        <span>{{ diagnostics.filter((item) => item.severity === 'error').length }} 个错误</span>
        <span>{{ diagnostics.filter((item) => item.severity === 'warning').length }} 个提醒</span>
        <button type="button" @click="inspectorTab = 'diagnostics'">查看诊断</button>
      </div>
    </main>

    <WorkflowInspectorPanel
      v-if="activeRail === 'edit'"
      v-model:inspector-tab="inspectorTab"
      :selected-data="selectedData"
      :selected-node-id="selectedNode?.id"
      :selected-tool-spec="selectedToolSpecForNode"
      :workflow-port-wdl-types="workflowPortWdlTypes"
      :available-subworkflow-upgrades="availableSubworkflowUpgrades"
      :selected-subworkflow-upgrade="selectedSubworkflowUpgrade"
      :selected-subworkflow-upgrade-info="selectedSubworkflowUpgradeInfo"
      :subworkflow-upgrade-state="subworkflowUpgradeState"
      :subworkflow-upgrade-message="subworkflowUpgradeMessage"
      :subworkflow-upgrade-node-id="subworkflowUpgradeNodeId"
      :save-state="saveState"
      :is-workflow-switching="isWorkflowSwitching"
      :selected-tool-parameters="selectedToolParameters"
      :diagnostics="diagnostics"
      :artifacts="artifacts"
      :parameter-display-value="parameterDisplayValue"
      :is-parameter-connected="isParameterConnected"
      @update-identifier="updateSelectedNodeIdentifier"
      @update-label="updateSelectedNodeLabel"
      @update-wdl-type="updateSelectedNodeWdlType"
      @update-semantic-type="updateSelectedNodeSemanticType"
      @open-subworkflow="openSubworkflowDetail"
      @select-subworkflow-upgrade="selectSubworkflowUpgrade"
      @upgrade-subworkflow="upgradeSelectedSubworkflow"
      @update-tool-parameter="updateToolParameter"
      @open-artifact="openArtifactPreview"
    />

    <ArtifactPreviewDrawer
      v-if="previewDrawerArtifact"
      :artifact="previewDrawerArtifact"
      :compilation-version="selectedCompilation?.version"
      @close="previewDrawerArtifact = undefined"
    />
  </div>
</template>
