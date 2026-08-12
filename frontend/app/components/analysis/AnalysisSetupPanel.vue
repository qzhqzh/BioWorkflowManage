<script setup lang="ts">
import type {
  AnalysisCatalog,
  AnalysisDatabaseOption,
  AnalysisDataset,
  AnalysisWorkflow,
} from '~/types/analysis'

const props = defineProps<{
  catalog: AnalysisCatalog | null
  busy: boolean
  error: string
  initialWorkflow?: string
  initialRevision?: number
}>()

const emit = defineEmits<{
  submit: [payload: {
    workflow: string
    dataset: string
    control_dataset?: string
    reference?: string
    panel?: string
    sample_id: string
    sample_name: string
    sample_type: string
    sample_gender: string
  }]
}>()

const workflowSlug = ref('')
const datasetId = ref('')
const controlDatasetId = ref('')
const referenceId = ref('')
const panelId = ref('')
const sampleId = ref('')
const sampleName = ref('')
const sampleType = ref('tissue')
const sampleGender = ref('女')
const auth = useAuth()

const datasets = computed<AnalysisDataset[]>(() => props.catalog?.datasets ?? [])
const workflows = computed<AnalysisWorkflow[]>(() => props.catalog?.workflows ?? [])
const references = computed<AnalysisDatabaseOption[]>(() => props.catalog?.database.references ?? [])
const selectedWorkflow = computed(() => workflows.value.find(item => item.slug === workflowSlug.value))
const panels = computed<AnalysisDatabaseOption[]>(() => (
  props.catalog?.database.panels.filter(item => (
    (!item.reference || item.reference === referenceId.value)
    && (!item.workflow_ids?.length || item.workflow_ids.includes(selectedWorkflow.value?.source_slug ?? selectedWorkflow.value?.slug ?? ''))
  )) ?? []
))
const selectedGraphSummary = computed(() => selectedWorkflow.value?.graph_summary)
const selectedWorkflowLink = computed(() => {
  const workflow = selectedWorkflow.value
  if (!workflow?.revision) return ''
  if (workflow.source_type === 'workflow_version') {
    return `/?section=artifacts&workflow=${encodeURIComponent(workflow.source_slug ?? workflow.slug)}&workflowVersion=${workflow.revision}`
  }
  return `/wdl/${encodeURIComponent(workflow.source_slug ?? workflow.slug)}?revision=${workflow.revision}`
})
const requiresReference = computed(() => selectedWorkflow.value?.requires_reference !== false)
const requiresPanel = computed(() => selectedWorkflow.value?.requires_panel !== false)
const selectedDataset = computed(() => datasets.value.find(item => item.id === datasetId.value))
const selectedReference = computed(() => references.value.find(item => item.id === referenceId.value))
const referenceStatus = (reference: AnalysisDatabaseOption) => (
  selectedWorkflow.value?.reference_status?.[reference.id] ?? reference
)
const panelStatus = (panel: AnalysisDatabaseOption) => (
  selectedWorkflow.value?.panel_status?.[panel.id] ?? panel
)
const selectedReferenceStatus = computed(() => (
  selectedReference.value ? referenceStatus(selectedReference.value) : undefined
))
const selectedPanel = computed(() => panels.value.find(item => item.id === panelId.value))
const selectedPanelStatus = computed(() => (
  selectedPanel.value ? panelStatus(selectedPanel.value) : undefined
))
const missingResources = computed(() => [
  ...(selectedReferenceStatus.value?.missing ?? []),
  ...(selectedPanelStatus.value?.missing ?? []),
])
const requiresControl = computed(() => selectedWorkflow.value?.mode === 'paired')
const canManageResources = computed(() => auth.user.value?.allowed_sections.includes('resources'))
const canSubmit = computed(() => Boolean(
  selectedWorkflow.value?.ready
  && selectedDataset.value
  && (!requiresReference.value || selectedReferenceStatus.value?.ready)
  && (!requiresPanel.value || selectedPanelStatus.value?.ready)
  && sampleId.value.trim()
  && sampleName.value.trim()
  && (!requiresControl.value || (controlDatasetId.value && controlDatasetId.value !== datasetId.value)),
))

watch(
  () => props.catalog,
  (catalog) => {
    if (!catalog) return
    if (!workflowSlug.value) {
      const requested = props.initialWorkflow
        ? catalog.workflows.find(item => (
            (item.slug === props.initialWorkflow || item.source_slug === props.initialWorkflow)
            && (!props.initialRevision || item.revision === props.initialRevision)
          ))
        : undefined
      workflowSlug.value = requested?.slug ?? (props.initialWorkflow ? '' : catalog.workflows[0]?.slug ?? '')
    }
    datasetId.value ||= catalog.datasets[0]?.id ?? ''
    referenceId.value ||= catalog.database.references[0]?.id ?? ''
    panelId.value ||= panels.value[0]?.id ?? ''
    if (!sampleId.value && catalog.datasets[0]) {
      sampleId.value = catalog.datasets[0].name
      sampleName.value = catalog.datasets[0].name
    }
  },
  { immediate: true },
)

watch(datasetId, (value, previous) => {
  const dataset = datasets.value.find(item => item.id === value)
  const oldDataset = datasets.value.find(item => item.id === previous)
  if (!dataset) return
  if (!sampleId.value || sampleId.value === oldDataset?.name) sampleId.value = dataset.name
  if (!sampleName.value || sampleName.value === oldDataset?.name) sampleName.value = dataset.name
  if (controlDatasetId.value === value) controlDatasetId.value = ''
})

watch(referenceId, () => {
  if (!panels.value.some(item => item.id === panelId.value)) {
    panelId.value = panels.value[0]?.id ?? ''
  }
})

watch(requiresControl, (required) => {
  if (!required) controlDatasetId.value = ''
})

watch(selectedWorkflow, (workflow) => {
  if (workflow?.required_reference && references.value.some(item => item.id === workflow.required_reference)) {
    referenceId.value = workflow.required_reference
  }
  if (!panels.value.some(item => item.id === panelId.value)) {
    panelId.value = panels.value[0]?.id ?? ''
  }
})

function missingResourceHint(item: AnalysisDatabaseOption['missing'][number]) {
  if (item.reason === 'checksum_mismatch') return 'SHA-256 与目录声明不一致'
  if (item.reason === 'constraint_mismatch') {
    return `文件名需包含 ${item.expected?.join(' / ') || '指定标识'}`
  }
  if (item.reason === 'unconfigured') return `未配置 ${item.binding}`
  return item.path
}

function submit() {
  if (!canSubmit.value) return
  emit('submit', {
    workflow: workflowSlug.value,
    dataset: datasetId.value,
    control_dataset: requiresControl.value ? controlDatasetId.value : undefined,
    reference: requiresReference.value ? referenceId.value : undefined,
    panel: requiresPanel.value ? panelId.value : undefined,
    sample_id: sampleId.value.trim(),
    sample_name: sampleName.value.trim(),
    sample_type: sampleType.value,
    sample_gender: sampleGender.value,
  })
}
</script>

<template>
  <form class="analysis-setup" @submit.prevent="submit">
    <header class="analysis-panel-header">
      <div>
        <span>新建运行</span>
        <h2>分析配置</h2>
      </div>
      <span v-if="catalog" class="analysis-source-count">{{ datasets.length }} 组数据</span>
    </header>

    <div v-if="!catalog" class="analysis-panel-state">正在读取数据和流程…</div>

    <template v-else>
      <section class="analysis-setup-section">
        <div class="analysis-step-heading">
          <span>1</span>
          <h3>原始数据</h3>
        </div>
        <label class="field">
          <span>分析样本</span>
          <select v-model="datasetId" aria-label="分析样本">
            <option value="" disabled>选择 R1/R2</option>
            <option v-for="dataset in datasets" :key="dataset.id" :value="dataset.id">
              {{ dataset.name }} · {{ dataset.total_size_label }}
            </option>
          </select>
        </label>
        <div v-if="selectedDataset" class="analysis-file-pair">
          <span v-for="file in selectedDataset.files" :key="file.mate">
            <b>R{{ file.mate }}</b>{{ file.name }}
          </span>
        </div>
        <p v-else-if="!datasets.length" class="analysis-inline-note analysis-inline-note--warning">
          rawdata 中没有识别到完整的 R1/R2。
        </p>
        <label v-if="requiresControl" class="field">
          <span>对照样本</span>
          <select v-model="controlDatasetId" aria-label="对照样本">
            <option value="" disabled>选择另一组 R1/R2</option>
            <option
              v-for="dataset in datasets.filter(item => item.id !== datasetId)"
              :key="dataset.id"
              :value="dataset.id"
            >
              {{ dataset.name }} · {{ dataset.total_size_label }}
            </option>
          </select>
        </label>
        <p v-if="requiresControl && datasets.length < 2" class="analysis-inline-note analysis-inline-note--warning">
          配对流程还需要一组对照 FASTQ。
        </p>
      </section>

      <section class="analysis-setup-section">
        <div class="analysis-step-heading">
          <span>2</span>
          <h3>流程</h3>
        </div>
        <div class="analysis-workflow-options" role="radiogroup" aria-label="分析流程">
          <label
            v-for="workflow in workflows"
            :key="workflow.slug"
            :class="{ 'analysis-workflow-option--selected': workflowSlug === workflow.slug }"
          >
            <input v-model="workflowSlug" type="radio" name="workflow" :value="workflow.slug" />
            <span>
              <strong>{{ workflow.name }}</strong>
              <small>
                v{{ workflow.revision ?? '—' }} ·
                {{ workflow.source_type === 'workflow_version' ? '流程库发布版' : workflow.mode === 'paired' ? '肿瘤 + 对照' : '历史 WDL' }}
              </small>
            </span>
            <i :class="workflow.ready ? 'is-ready' : 'is-blocked'">{{ workflow.ready ? '就绪' : '阻塞' }}</i>
          </label>
        </div>
        <details v-if="selectedWorkflow?.blockers.length" open class="analysis-workflow-blockers">
          <summary>{{ selectedWorkflow.blockers[0] }}</summary>
          <ul v-if="selectedWorkflow.blockers.length > 1">
            <li v-for="blocker in selectedWorkflow.blockers.slice(1)" :key="blocker">{{ blocker }}</li>
          </ul>
        </details>
        <p v-else-if="initialWorkflow && !selectedWorkflow" class="analysis-inline-note analysis-inline-note--warning">
          请求的流程版本当前不在可运行目录中，请重新选择。
        </p>
        <div v-if="selectedWorkflow" class="analysis-selected-workflow">
          <div>
            <p>{{ selectedWorkflow.description || '未填写流程说明' }}</p>
            <div v-if="selectedGraphSummary" class="analysis-selected-workflow__counts" aria-label="固定流程结构摘要">
              <span>{{ selectedGraphSummary.input_count }} 输入</span>
              <span>{{ selectedGraphSummary.tool_count }} 工具</span>
              <span v-if="selectedGraphSummary.subworkflow_count">{{ selectedGraphSummary.subworkflow_count }} 子流程</span>
              <span>{{ selectedGraphSummary.output_count }} 输出</span>
              <span>{{ selectedGraphSummary.edge_count }} 连接</span>
            </div>
            <details v-if="selectedGraphSummary" class="analysis-selected-workflow__details">
              <summary>核对固定内容</summary>
              <ul v-if="selectedGraphSummary.tools.length || selectedGraphSummary.subworkflows.length">
                <li v-for="tool in selectedGraphSummary.tools" :key="`tool:${tool.id}:${tool.version}`">
                  <span>{{ tool.name }}</span><code>工具 v{{ tool.version }}</code>
                </li>
                <li v-for="subworkflow in selectedGraphSummary.subworkflows" :key="`subworkflow:${subworkflow.slug}:${subworkflow.version}`">
                  <span>{{ subworkflow.name }}</span><code>子流程 v{{ subworkflow.version }}</code>
                </li>
              </ul>
              <code class="analysis-selected-workflow__digest">{{ selectedWorkflow.digest }}</code>
            </details>
            <code v-else>{{ selectedWorkflow.digest }}</code>
          </div>
          <NuxtLink
            v-if="selectedWorkflowLink"
            class="button button--ghost button-link"
            :to="selectedWorkflowLink"
            target="_blank"
            rel="noopener"
          >{{ selectedWorkflow.source_type === 'workflow_version' ? '查看发布版本' : '查看历史 WDL' }}</NuxtLink>
        </div>
      </section>

      <section v-if="requiresReference || requiresPanel" class="analysis-setup-section">
        <div class="analysis-step-heading">
          <span>3</span>
          <h3>数据库与 Panel</h3>
        </div>
        <div class="analysis-field-row">
          <label v-if="requiresReference" class="field">
            <span>参考版本</span>
            <select v-model="referenceId" aria-label="参考版本">
              <option v-for="reference in references" :key="reference.id" :value="reference.id">
                {{ reference.name }}{{ referenceStatus(reference).ready ? '' : ` · 缺 ${referenceStatus(reference).missing.length} 项` }}
              </option>
            </select>
          </label>
          <label v-if="requiresPanel" class="field">
            <span>Panel</span>
            <select v-model="panelId" aria-label="Panel">
              <option v-for="panel in panels" :key="panel.id" :value="panel.id">
                {{ panel.name }}{{ panelStatus(panel).ready ? '' : ` · 缺 ${panelStatus(panel).missing.length} 项` }}
              </option>
            </select>
          </label>
        </div>
        <details v-if="missingResources.length" open class="analysis-missing-resources">
          <summary>数据库还缺 {{ missingResources.length }} 项</summary>
          <ul>
            <li v-for="item in missingResources" :key="`${item.binding}-${item.path}-${item.label}`">
              <span>{{ item.label }}</span><code>{{ missingResourceHint(item) }}</code>
            </li>
          </ul>
        </details>
        <p v-else-if="requiresPanel && !panels.length" class="analysis-inline-note analysis-inline-note--warning">
          当前流程和参考版本还没有可用的 Panel。请在资源中心配置 BED、gene BED 与 CNV 基线。
          <NuxtLink v-if="canManageResources" to="/resources">打开资源中心</NuxtLink>
        </p>
        <p v-else-if="selectedReferenceStatus?.ready && (!requiresPanel || selectedPanelStatus?.ready)" class="analysis-inline-note analysis-inline-note--ready">
          数据库检查通过。
        </p>
      </section>

      <section class="analysis-setup-section">
        <div class="analysis-step-heading">
          <span>{{ requiresReference || requiresPanel ? 4 : 3 }}</span>
          <h3>样本信息</h3>
        </div>
        <div class="analysis-field-row">
          <label class="field">
            <span>样本编号</span>
            <input v-model="sampleId" maxlength="128" required />
          </label>
          <label class="field">
            <span>样本名称</span>
            <input v-model="sampleName" maxlength="128" required />
          </label>
          <label class="field">
            <span>样本类型</span>
            <select v-model="sampleType">
              <option value="tissue">组织</option>
              <option value="blood">血液</option>
              <option value="plasma">血浆</option>
            </select>
          </label>
          <label class="field">
            <span>性别</span>
            <select v-model="sampleGender">
              <option value="女">女</option>
              <option value="男">男</option>
            </select>
          </label>
        </div>
      </section>

      <div class="analysis-submit-row">
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <span v-else-if="canSubmit">
          {{ selectedWorkflow?.source_type === 'workflow_version'
            ? '将固定当前发布版本与编译产物后进入队列。'
            : '将固定当前 WDL revision 后进入队列。' }}
        </span>
        <span v-else>补齐上方必需项后可运行。</span>
        <button class="button button--primary" type="submit" :disabled="busy || !canSubmit">
          {{ busy ? '正在提交…' : '开始分析' }}
        </button>
      </div>
    </template>
  </form>
</template>
