<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type {
  WdlAnalysis,
  WdlAsset,
  WdlSourceFile,
  WdlSourceRevision,
  WdlToolPackage,
  WdlToolPackageTag,
} from '~/types/wdl'

const { $api: $fetch } = useNuxtApp()
const { navigateSection } = useAppNavigation()
const route = useRoute()

interface WorkflowPackageSourceFile {
  path: string
  content: string
  tool_id: string
  tool_version: string
  tool_digest: string
}

interface WorkflowPackageSource {
  workflow: {
    slug: string
    name: string
    document_version: number
    document_digest: string
  }
  files: WorkflowPackageSourceFile[]
  preview_digest: string
  can_publish: boolean
  analysis: WdlAnalysis
}

const packages = ref<WdlToolPackage[]>([])
const tags = ref<WdlToolPackageTag[]>([])
const searchQuery = ref('')
const selectedTags = ref<string[]>([])
const lifecycle = ref<'active' | 'archived' | ''>('active')
const mineOnly = ref(false)
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const showImport = ref(false)
const importState = ref<'idle' | 'previewing' | 'ready' | 'saving' | 'error'>('idle')
const importError = ref('')
const packagePreview = ref<{
  preview_digest: string
  can_publish: boolean
  analysis: WdlAnalysis
}>()
const sourceMode = ref<'template' | 'zip' | 'asset' | 'workflow'>('template')
const assetSourceFiles = ref<WdlSourceFile[]>([])
const workflowSource = ref<WorkflowPackageSource>()
const selectedWorkflowDigests = ref<string[]>([])
const fileInput = ref<HTMLInputElement>()
const selectedFile = ref<File>()
const sourcePath = ref('tasks/example_task.wdl')
const sourceContent = ref(`version 1.0

task ExampleTask {
  input {
    File input_file
  }

  command <<<
    cp "~{input_file}" output.txt
  >>>

  output {
    File output_file = "output.txt"
  }

  runtime {
    docker: "ubuntu:24.04"
  }
}
`)
const draft = ref({
  name: '',
  version: '1.0.0',
  description: '',
  tags: '',
  sourceRepository: '',
  sourceRevision: '',
  note: '',
})
const visiblePackages = computed(() =>
  mineOnly.value ? packages.value.filter(item => item.is_mine) : packages.value,
)
const openedFromEditor = computed(() => route.query.from === 'editor')
const openedFromWdl = computed(() => route.query.from === 'wdl' && typeof route.query.asset === 'string')
const initialWorkflowNodeId = computed(() =>
  typeof route.query.node === 'string' ? route.query.node : '',
)
const selectedWorkflowFiles = computed(() => {
  const selected = new Set(selectedWorkflowDigests.value)
  return (workflowSource.value?.files ?? []).filter(file => selected.has(file.tool_digest))
})

function returnToEditor() {
  const workflow = typeof route.query.workflow === 'string' ? route.query.workflow : undefined
  void navigateTo({
    path: '/',
    query: { section: 'edit', ...(workflow ? { workflow } : {}) },
  })
}

function returnToWdl() {
  if (typeof route.query.asset !== 'string') return
  const revision = typeof route.query.revision === 'string' ? route.query.revision : undefined
  void navigateTo({
    path: `/wdl/${encodeURIComponent(route.query.asset)}`,
    query: revision ? { revision } : {},
  })
}

async function loadWdlAssetSource() {
  if (!openedFromWdl.value || typeof route.query.asset !== 'string') return
  try {
    const asset = await $fetch<WdlAsset>(
      `/api/v1/wdl-assets/${encodeURIComponent(route.query.asset)}`,
    )
    const requestedRevision = Number(route.query.revision)
    let revision = asset.current_revision
    if (
      Number.isInteger(requestedRevision)
      && requestedRevision > 0
      && requestedRevision !== revision?.version
    ) {
      revision = await $fetch<WdlSourceRevision>(
        `/api/v1/wdl-assets/${encodeURIComponent(asset.slug)}/revisions/${requestedRevision}`,
      )
    }
    assetSourceFiles.value = (revision?.files ?? []).filter(
      file => !file.read_only && file.origin !== 'package',
    )
    sourceMode.value = 'asset'
    showImport.value = true
    draft.value = {
      name: `${asset.name} 工具包`,
      version: '1.0.0',
      description: `从历史 WDL ${asset.name} 固定可复用 task。`,
      tags: asset.tags.join(', '),
      sourceRepository: `bioworkflow://wdl-assets/${asset.slug}`,
      sourceRevision: `wdl-v${revision?.version ?? 1}`,
      note: `由历史 WDL ${asset.slug} 的 revision v${revision?.version ?? 1} 创建。${asset.source_repository ? ` 原始来源：${asset.source_repository}` : ''}`,
    }
    if (!assetSourceFiles.value.length) {
      importState.value = 'error'
      importError.value = '当前 WDL revision 没有可打包的源文件。'
    }
  } catch (error: any) {
    importState.value = 'error'
    importError.value = error?.data?.error?.message ?? '历史 WDL 读取失败。'
  }
}

async function loadWorkflowSource() {
  if (!openedFromEditor.value || typeof route.query.workflow !== 'string') return
  showImport.value = true
  sourceMode.value = 'workflow'
  importState.value = 'previewing'
  importError.value = ''
  try {
    const workflow = await $fetch<{
      slug: string
      name: string
      description: string
      workflow_graph: Record<string, any>
      tool_specs: Record<string, any>[]
      document_version: number
      document_digest: string
    }>(`/api/v1/editor/workflows/${encodeURIComponent(route.query.workflow)}`)
    const usedDigests = Array.from(new Set(
      (workflow.workflow_graph.nodes ?? [])
        .filter((node: Record<string, any>) => node.type === 'tool')
        .map((node: Record<string, any>) => node.tool_ref?.digest)
        .filter((digest: unknown): digest is string => typeof digest === 'string' && Boolean(digest)),
    ))
    if (!usedDigests.length || !workflow.tool_specs.length) {
      throw new Error('当前画布还没有可打包的工具版本。')
    }
    const requestedNodeDigest = initialWorkflowNodeId.value
      ? (workflow.workflow_graph.nodes ?? []).find(
          (node: Record<string, any>) => node.id === initialWorkflowNodeId.value && node.type === 'tool',
        )?.tool_ref?.digest
      : undefined
    if (initialWorkflowNodeId.value && !requestedNodeDigest) {
      throw new Error('当前节点不是可打包的工具节点。')
    }
    const source = await $fetch<WorkflowPackageSource>(
      `/api/v1/editor/workflows/${encodeURIComponent(workflow.slug)}/tool-package-source`,
      {
        method: 'POST',
        body: {
          base_document_version: workflow.document_version,
          base_document_digest: workflow.document_digest,
          tool_digests: usedDigests,
        },
      },
    )
    if (
      requestedNodeDigest
      && !source.files.some(file => file.tool_digest === requestedNodeDigest)
    ) {
      throw new Error('当前节点引用的工具版本无法打包。')
    }
    workflowSource.value = source
    selectedWorkflowDigests.value = requestedNodeDigest
      ? [requestedNodeDigest]
      : source.files.map(file => file.tool_digest)
    packagePreview.value = requestedNodeDigest
      ? undefined
      : {
          preview_digest: source.preview_digest,
          can_publish: source.can_publish,
          analysis: source.analysis,
        }
    draft.value = {
      name: `${workflow.name} 工具包`,
      version: '1.0.0',
      description: `固定画布流程 ${workflow.name} 当前引用的工具版本。`,
      tags: '',
      sourceRepository: `bioworkflow://editor/workflows/${workflow.slug}`,
      sourceRevision: `draft-v${workflow.document_version}`,
      note: `由画布流程 ${workflow.slug} 的文档版本 v${workflow.document_version} 创建。`,
    }
    importState.value = requestedNodeDigest ? 'idle' : 'ready'
  } catch (error: any) {
    importState.value = 'error'
    importError.value = error?.data?.error?.message ?? error?.message ?? '当前画布工具读取失败。'
  }
}

async function loadPackages() {
  loadState.value = 'loading'
  try {
    const response = await $fetch<{ results: WdlToolPackage[] }>('/api/v1/wdl-packages', {
      query: {
        q: searchQuery.value.trim() || undefined,
        tag: selectedTags.value.length ? selectedTags.value : undefined,
        lifecycle: lifecycle.value || undefined,
      },
    })
    packages.value = response.results
    loadState.value = 'ready'
  } catch {
    loadState.value = 'error'
  }
}

async function loadTags() {
  try {
    const response = await $fetch<{ results: WdlToolPackageTag[] }>('/api/v1/wdl-packages/tags')
    tags.value = response.results
  } catch {
    tags.value = []
  }
}

function toggleTag(name: string) {
  selectedTags.value = selectedTags.value.includes(name)
    ? selectedTags.value.filter(item => item !== name)
    : [...selectedTags.value, name]
  void loadPackages()
}

function addImportTag(name: string) {
  const current = draft.value.tags.split(',').map(item => item.trim()).filter(Boolean)
  if (!current.includes(name)) draft.value.tags = [...current, name].join(', ')
}

function selectFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  selectedFile.value = file
  draft.value.name ||= file.name.replace(/\.zip$/i, '')
  invalidatePackagePreview()
}

function selectSourceMode(mode: 'template' | 'zip' | 'asset' | 'workflow') {
  if (sourceMode.value === mode) return
  sourceMode.value = mode
  invalidatePackagePreview()
}

function toggleWorkflowTool(digest: string) {
  selectedWorkflowDigests.value = selectedWorkflowDigests.value.includes(digest)
    ? selectedWorkflowDigests.value.filter(item => item !== digest)
    : [...selectedWorkflowDigests.value, digest]
  invalidatePackagePreview()
}

function selectAllWorkflowTools() {
  selectedWorkflowDigests.value = (workflowSource.value?.files ?? []).map(file => file.tool_digest)
  invalidatePackagePreview()
}

function clearWorkflowTools() {
  selectedWorkflowDigests.value = []
  invalidatePackagePreview()
}

function invalidatePackagePreview() {
  packagePreview.value = undefined
  if (importState.value !== 'saving') importState.value = 'idle'
  importError.value = ''
}

function resetImport() {
  showImport.value = false
  importState.value = 'idle'
  importError.value = ''
  packagePreview.value = undefined
  selectedFile.value = undefined
  sourceMode.value = 'template'
  sourcePath.value = 'tasks/example_task.wdl'
  sourceContent.value = `version 1.0

task ExampleTask {
  input {
    File input_file
  }

  command <<<
    cp "~{input_file}" output.txt
  >>>

  output {
    File output_file = "output.txt"
  }

  runtime {
    docker: "ubuntu:24.04"
  }
}
`
  draft.value = {
    name: '',
    version: '1.0.0',
    description: '',
    tags: '',
    sourceRepository: '',
    sourceRevision: '',
    note: '',
  }
  if (fileInput.value) fileInput.value.value = ''
}

function validatePackageSource() {
  if (sourceMode.value === 'zip' && !selectedFile.value) {
    importState.value = 'error'
    importError.value = '请选择包含 WDL task 的 ZIP。'
    return false
  }
  if (sourceMode.value === 'template' && (!sourcePath.value.trim() || !sourceContent.value.trim())) {
    importState.value = 'error'
    importError.value = '请填写 WDL 文件路径和内容。'
    return false
  }
  if (sourceMode.value === 'asset' && !assetSourceFiles.value.length) {
    importState.value = 'error'
    importError.value = '当前 WDL revision 没有可打包的源文件。'
    return false
  }
  if (sourceMode.value === 'workflow' && !selectedWorkflowFiles.value.length) {
    importState.value = 'error'
    importError.value = '请至少选择一个当前画布中的工具版本。'
    return false
  }
  return true
}

function packageRequestBody(includeMetadata = false) {
  const tagNames = draft.value.tags.split(',').map(item => item.trim()).filter(Boolean)
  if (sourceMode.value === 'zip') {
    const form = new FormData()
    form.append('archive', selectedFile.value!)
    if (includeMetadata) {
      form.append('name', draft.value.name.trim())
      form.append('version', draft.value.version.trim())
      form.append('description', draft.value.description.trim())
      form.append('tags', JSON.stringify(tagNames))
      form.append('source_repository', draft.value.sourceRepository.trim())
      form.append('source_revision', draft.value.sourceRevision.trim())
      form.append('note', draft.value.note.trim())
      form.append('preview_digest', packagePreview.value?.preview_digest ?? '')
      form.append('confirm_preview', 'true')
    }
    return form
  }
  const files = sourceMode.value === 'asset'
    ? assetSourceFiles.value.map(file => ({ path: file.path, content: file.content }))
    : sourceMode.value === 'workflow'
      ? selectedWorkflowFiles.value.map(file => ({ path: file.path, content: file.content }))
      : [{ path: sourcePath.value.trim(), content: sourceContent.value }]
  return {
    ...(includeMetadata
      ? {
          name: draft.value.name.trim(),
          version: draft.value.version.trim(),
          description: draft.value.description.trim(),
          tags: tagNames,
          source_repository: draft.value.sourceRepository.trim(),
          source_revision: draft.value.sourceRevision.trim(),
          note: draft.value.note.trim(),
          preview_digest: packagePreview.value?.preview_digest,
          confirm_preview: true,
        }
      : {}),
    files,
  }
}

async function previewPackage() {
  if (!validatePackageSource()) return
  importState.value = 'previewing'
  importError.value = ''
  try {
    packagePreview.value = await $fetch<{
      preview_digest: string
      can_publish: boolean
      analysis: WdlAnalysis
    }>('/api/v1/wdl-packages/preview', {
      method: 'POST',
      body: packageRequestBody(),
    })
    importState.value = 'ready'
    if (!packagePreview.value.can_publish) {
      importError.value = packagePreview.value.analysis.summary.task_count
        ? '修复下方诊断后重新分析。'
        : '工具包中没有可发布的 task。'
    }
  } catch (error: any) {
    importState.value = 'error'
    importError.value = error?.data?.error?.message ?? '工具包分析失败。'
  }
}

async function importPackage() {
  if (!packagePreview.value?.can_publish) {
    importState.value = 'error'
    importError.value = '请先完成内容分析并修复诊断。'
    return
  }
  if (!draft.value.name.trim() || !draft.value.version.trim()) {
    importState.value = 'error'
    importError.value = '请填写名称和版本。'
    return
  }
  importState.value = 'saving'
  importError.value = ''
  try {
    const created = await $fetch<WdlToolPackage>('/api/v1/wdl-packages', {
      method: 'POST',
      body: packageRequestBody(true),
    })
    await navigateTo(`/wdl-packages/${encodeURIComponent(created.slug)}`)
  } catch (error: any) {
    importState.value = 'error'
    importError.value = error?.data?.error?.message ?? '工具包导入失败。'
  }
}

watch([sourcePath, sourceContent], () => {
  if (packagePreview.value) invalidatePackagePreview()
})

onMounted(() => {
  if (route.query.create === '1') showImport.value = true
  void Promise.all([loadPackages(), loadTags(), loadWdlAssetSource(), loadWorkflowSource()])
})
</script>

<template>
  <div class="app-shell app-shell--workspace">
    <AppTopbar section="工具库" current="WDL 工具包">
      <template #actions>
        <button v-if="openedFromEditor" class="button button--ghost" type="button" @click="returnToEditor">
          返回编辑器
        </button>
        <button v-else-if="openedFromWdl" class="button button--ghost" type="button" @click="returnToWdl">
          返回 WDL
        </button>
        <button class="button button--primary" type="button" @click="showImport = !showImport">
          {{ showImport ? '收起' : '创建工具包' }}
        </button>
      </template>
    </AppTopbar>

    <AppRail active="packages" @select="navigateSection" />

    <main class="section-workspace wdl-packages-page">
      <header class="workspace-header">
        <div>
          <h1>WDL 工具包</h1>
          <p>上传一组可复用的 task WDL，创建工具包并发布第一个固定版本。</p>
        </div>
      </header>

      <form v-if="showImport" class="wdl-import-panel" @submit.prevent="importPackage">
        <div class="wdl-import-panel__intro">
          <strong>创建工具包</strong>
          <ol class="wdl-package-create-steps" aria-label="创建步骤">
            <li :class="{ 'is-active': !packagePreview, 'is-complete': packagePreview }"><span>1</span> 分析内容</li>
            <li :class="{ 'is-active': packagePreview?.can_publish }"><span>2</span> 填写信息并创建</li>
          </ol>
          <div class="wdl-package-source-switch" role="group" aria-label="工具包来源">
            <button
              type="button"
              :class="{ 'is-active': sourceMode === 'template' }"
              :aria-pressed="sourceMode === 'template'"
              @click="selectSourceMode('template')"
            >从模板开始</button>
            <button
              type="button"
              :class="{ 'is-active': sourceMode === 'zip' }"
              :aria-pressed="sourceMode === 'zip'"
              @click="selectSourceMode('zip')"
            >上传 ZIP</button>
            <button
              v-if="openedFromWdl"
              type="button"
              :class="{ 'is-active': sourceMode === 'asset' }"
              :aria-pressed="sourceMode === 'asset'"
              @click="selectSourceMode('asset')"
            >当前历史 WDL</button>
            <button
              v-if="openedFromEditor"
              type="button"
              :class="{ 'is-active': sourceMode === 'workflow' }"
              :aria-pressed="sourceMode === 'workflow'"
              @click="selectSourceMode('workflow')"
            >当前画布</button>
          </div>
          <input
            ref="fileInput"
            class="visually-hidden"
            type="file"
            accept=".zip,application/zip"
            @change="selectFile"
          />
        </div>
        <div v-if="packagePreview?.can_publish" class="wdl-import-fields">
          <label class="field">
            <span>名称</span>
            <input v-model="draft.name" required placeholder="例如 solid-tumor-tools" />
          </label>
          <label class="field">
            <span>版本</span>
            <input v-model="draft.version" required placeholder="1.0.0" />
          </label>
          <label class="field field--wide">
            <span>说明</span>
            <input v-model="draft.description" placeholder="工具包用途和维护范围" />
          </label>
          <label class="field field--wide">
            <span>标签</span>
            <input v-model="draft.tags" placeholder="实体瘤, hg38" />
          </label>
          <div v-if="tags.length" class="tag-suggestions">
            <button v-for="tag in tags.slice(0, 8)" :key="tag.id" type="button" @click="addImportTag(tag.name)">
              + {{ tag.name }}
            </button>
          </div>
          <label class="field field--wide">
            <span>来源仓库</span>
            <input v-model="draft.sourceRepository" placeholder="可选" />
          </label>
          <label class="field">
            <span>来源版本</span>
            <input v-model="draft.sourceRevision" placeholder="Git commit 或 tag" />
          </label>
          <label class="field field--wide">
            <span>备注</span>
            <input v-model="draft.note" placeholder="本次导入说明" />
          </label>
        </div>
        <section
          v-if="sourceMode === 'template'"
          class="wdl-package-source-editor"
          :class="{ 'wdl-package-source-editor--first-step': !packagePreview }"
        >
          <label class="field">
            <span>文件路径</span>
            <input v-model="sourcePath" required autocomplete="off" pattern="[^\\]+\.wdl" placeholder="tasks/example_task.wdl" />
          </label>
          <label>
            <span>WDL 内容</span>
            <textarea v-model="sourceContent" required spellcheck="false" aria-label="WDL 内容" />
          </label>
        </section>
        <section
          v-else-if="sourceMode === 'zip'"
          class="wdl-package-archive-picker"
          :class="{ 'wdl-package-archive-picker--first-step': !packagePreview }"
        >
          <button class="wdl-package-archive-picker__button" type="button" @click="fileInput?.click()">
            <strong>{{ selectedFile?.name || '选择 WDL 工具包 ZIP' }}</strong>
            <span>{{ selectedFile ? `${(selectedFile.size / 1024).toFixed(1)} KB` : '支持包含多个 WDL 文件及相对 import' }}</span>
          </button>
        </section>
        <section v-else-if="sourceMode === 'asset'" class="wdl-package-asset-source" aria-label="历史 WDL 源文件">
          <strong>将当前 revision 固定为工具包版本</strong>
          <ul>
            <li v-for="file in assetSourceFiles" :key="file.path">
              <code>{{ file.path }}</code>
            </li>
          </ul>
        </section>
        <section v-else class="wdl-package-workflow-source" aria-label="当前画布工具版本">
          <header>
            <div>
              <strong>{{ workflowSource?.workflow.name ?? '当前画布' }}</strong>
              <span>{{ selectedWorkflowFiles.length }} / {{ workflowSource?.files.length ?? 0 }} 个工具版本将进入工具包</span>
            </div>
            <div>
              <button type="button" @click="selectAllWorkflowTools">全选</button>
              <button type="button" @click="clearWorkflowTools">清空</button>
            </div>
          </header>
          <div v-if="workflowSource?.files.length" class="wdl-package-workflow-source__tools">
            <label v-for="file in workflowSource.files" :key="file.tool_digest">
              <input
                type="checkbox"
                :checked="selectedWorkflowDigests.includes(file.tool_digest)"
                @change="toggleWorkflowTool(file.tool_digest)"
              />
              <span>
                <strong>{{ file.tool_id }}</strong>
                <small>v{{ file.tool_version }} · {{ file.path }}</small>
              </span>
              <code>{{ file.tool_digest.slice(0, 18) }}…</code>
            </label>
          </div>
          <p v-else>{{ importError || '正在读取当前画布工具…' }}</p>
        </section>
        <section v-if="packagePreview" class="wdl-package-preview" aria-label="工具包分析预览">
          <header>
            <div>
              <strong>{{ packagePreview.can_publish ? '内容检查通过' : '内容需要修复' }}</strong>
              <span>{{ packagePreview.analysis.package?.file_count ?? packagePreview.analysis.files?.length ?? 0 }} 文件 · {{ packagePreview.analysis.summary.task_count }} task · {{ packagePreview.analysis.summary.import_count }} import</span>
            </div>
            <span class="analysis-status" :class="`analysis-status--${packagePreview.analysis.status}`">
              {{ packagePreview.analysis.status === 'valid' ? '可以创建' : `${packagePreview.analysis.summary.error_count} 项问题` }}
            </span>
          </header>
          <div v-if="packagePreview.analysis.tasks.length" class="wdl-package-preview__tasks">
            <article v-for="task in packagePreview.analysis.tasks" :key="task.id ?? `${task.file_path}:${task.name}`">
              <span aria-hidden="true">✓</span>
              <div><strong>{{ task.name }}</strong><small>{{ task.file_path }}</small></div>
              <small>{{ task.inputs.length }} 输入 · {{ task.outputs.length }} 输出</small>
            </article>
          </div>
          <ul v-if="packagePreview.analysis.diagnostics.length" class="wdl-package-preview__diagnostics">
            <li v-for="item in packagePreview.analysis.diagnostics" :key="`${item.code}:${item.file_path}:${item.location?.line}:${item.message}`">
              <strong>{{ item.code }}</strong>
              <span>{{ item.message }}</span>
              <small v-if="item.file_path">{{ item.file_path }}<template v-if="item.location?.line">:{{ item.location.line }}</template></small>
            </li>
          </ul>
          <details v-if="packagePreview.analysis.imports.length || packagePreview.analysis.files?.length">
            <summary>查看文件与 import</summary>
            <ul>
              <li v-for="file in packagePreview.analysis.files" :key="file.path">{{ file.path }}</li>
              <li v-for="item in packagePreview.analysis.imports" :key="`${item.file_path}:${item.uri}`">
                {{ item.file_path }} → {{ item.uri }} · {{ item.status === 'resolved' ? '已解析' : item.status === 'external' ? '外部引用' : '缺失' }}
              </li>
            </ul>
          </details>
        </section>
        <p v-if="importError" class="inline-error wdl-package-create-error" role="alert">{{ importError }}</p>
        <div class="wdl-import-actions">
          <button class="button button--ghost" type="button" @click="resetImport">取消</button>
          <button
            v-if="!packagePreview?.can_publish"
            class="button button--primary"
            type="button"
            :disabled="importState === 'previewing'"
            @click="previewPackage"
          >{{ importState === 'previewing' ? '正在分析…' : packagePreview ? '重新分析' : '分析内容' }}</button>
          <button v-else class="button button--primary" type="submit" :disabled="importState === 'saving'">
            {{ importState === 'saving' ? '正在创建…' : '创建固定版本' }}
          </button>
        </div>
      </form>

      <form class="wdl-assets-toolbar" role="search" @submit.prevent="loadPackages">
        <label class="search-field workspace-search">
          <span aria-hidden="true">⌕</span>
          <input v-model="searchQuery" type="search" placeholder="搜索名称、说明或来源" />
        </label>
        <select v-model="lifecycle" aria-label="状态" @change="loadPackages">
          <option value="active">使用中</option>
          <option value="archived">已归档</option>
          <option value="">全部</option>
        </select>
        <button class="button button--ghost" type="submit">查询</button>
        <button
          class="button button--ghost"
          type="button"
          :aria-pressed="mineOnly"
          :class="{ 'is-active': mineOnly }"
          @click="mineOnly = !mineOnly"
        >
          {{ mineOnly ? '正在看我的' : '只看我的' }}
        </button>
        <span>{{ visiblePackages.length }} 个结果</span>
      </form>

      <div v-if="tags.length" class="wdl-tag-filter" aria-label="按标签筛选">
        <button
          v-for="tag in tags"
          :key="tag.id"
          class="wdl-tag-filter__name"
          :class="{ 'wdl-tag-filter__active': selectedTags.includes(tag.name) }"
          type="button"
          :aria-pressed="selectedTags.includes(tag.name)"
          @click="toggleTag(tag.name)"
        >
          {{ tag.name }} <span class="wdl-tag-filter__count">{{ tag.package_count }}</span>
        </button>
      </div>

      <div v-if="loadState === 'error'" class="empty-state wdl-assets-empty" role="alert">
        <strong>工具包暂时无法读取</strong>
        <button class="button button--ghost" type="button" @click="loadPackages">重新加载</button>
      </div>
      <div v-else-if="loadState === 'loading'" class="wdl-assets-loading" aria-label="正在加载工具包">
        <span v-for="index in 5" :key="index" />
      </div>
      <div v-else-if="visiblePackages.length" class="wdl-assets-table-wrap">
        <table class="wdl-assets-table wdl-packages-table">
          <thead>
            <tr>
              <th scope="col">工具包</th>
              <th scope="col">创建人</th>
              <th scope="col">标签</th>
              <th scope="col">内容</th>
              <th scope="col">检查</th>
              <th scope="col">版本</th>
              <th scope="col">引用</th>
              <th scope="col"><span class="visually-hidden">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in visiblePackages" :key="item.slug">
              <td>
                <NuxtLink :to="`/wdl-packages/${item.slug}`">{{ item.name }}</NuxtLink>
                <small>{{ item.slug }}</small>
              </td>
              <td><span>{{ item.is_mine ? '我' : item.created_by }}</span></td>
              <td>
                <div class="tag-list tag-list--compact">
                  <span v-for="tag in item.tags" :key="tag">{{ tag }}</span>
                  <small v-if="!item.tags.length">未分类</small>
                </div>
              </td>
              <td>
                <span class="structure-summary">
                  {{ item.latest_version?.analysis.summary.task_count ?? 0 }} task
                  · {{ item.latest_version?.file_count ?? 0 }} 文件
                </span>
              </td>
              <td>
                <span class="analysis-status" :class="`analysis-status--${item.latest_version?.analysis.status ?? 'invalid'}`">
                  {{ item.latest_version?.analysis.status === 'valid' ? '通过' : `${item.latest_version?.analysis.summary.error_count ?? 0} 项` }}
                </span>
              </td>
              <td>
                <strong>{{ item.latest_version?.version ?? '—' }}</strong>
                <small>{{ item.version_count }} 个版本</small>
              </td>
              <td>{{ item.reference_count }}</td>
              <td>
                <NuxtLink class="button button--ghost button-link" :to="`/wdl-packages/${item.slug}`">打开</NuxtLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="empty-state wdl-assets-empty">
        <strong>{{ searchQuery || selectedTags.length || mineOnly ? '没有匹配的工具包' : '还没有工具包' }}</strong>
        <button v-if="!searchQuery && !selectedTags.length" class="button button--primary" type="button" @click="showImport = true">
          创建第一个工具包
        </button>
      </div>
    </main>
  </div>
</template>
