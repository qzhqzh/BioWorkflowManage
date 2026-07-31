<script setup lang="ts">
interface WorkflowLibraryEntry {
  slug: string
  name: string
  description?: string
  kind?: 'workflow' | 'subworkflow'
  latest_version: number | null
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
}

defineProps<{
  workflowSlug: string
  workflowDocuments: WorkflowLibraryEntry[]
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
  copiedArtifact: string
}>()

const editingMetadata = defineModel<boolean>('editingMetadata', { required: true })
const workflowName = defineModel<string>('workflowName', { required: true })
const workflowKind = defineModel<'workflow' | 'subworkflow'>('workflowKind', { required: true })
const workflowDescription = defineModel<string>('workflowDescription', { required: true })
const editingWdl = defineModel<boolean>('editingWdl', { required: true })
const wdlDraft = defineModel<string>('wdlDraft', { required: true })

const emit = defineEmits<{
  openEditor: []
  saveMetadata: []
  selectWorkflow: [slug: string]
  selectCompilation: [id: string]
  selectWdlVersion: [version: number]
  beginWdlEdit: []
  saveWdl: []
  copyWdl: []
}>()
</script>

<template>
  <header class="workspace-header">
    <div>
      <h1>流程库</h1>
      <p>已保存的流程版本与编译记录。每次编译都保留可追踪的 WDL 产物。</p>
    </div>
    <button class="button button--primary" type="button" @click="emit('openEditor')">打开编辑器</button>
  </header>

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

      <h2>流程与子流程</h2>
      <div class="workflow-index" :aria-busy="isWorkflowSwitching">
        <button
          v-for="document in workflowDocuments"
          :key="document.slug"
          type="button"
          :class="{ 'workflow-index__active': document.slug === workflowSlug }"
          :aria-current="document.slug === workflowSlug ? 'true' : undefined"
          :disabled="isWorkflowSwitching || saveState === 'saving' || compileState === 'running'"
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
                : `${document.kind === 'subworkflow' ? '子流程' : '流程'} · v${document.latest_version ?? '—'}`
            }}
          </em>
        </button>
      </div>

      <h2>编译版本</h2>
      <button
        v-for="version in compilationVersions"
        :key="version.id"
        type="button"
        class="version-row"
        :class="{ 'version-row--active': selectedCompilationId === version.id }"
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

    <section class="wdl-preview">
      <header>
        <div>
          <strong>workflow.wdl</strong>
          <small>
            WDL v{{ selectedWdlRevision?.version ?? '—' }}
            · 流程 {{ selectedCompilationVersion ?? '未发布' }}
            · {{ selectedWdlRevision?.source === 'manual' ? '人工编辑' : '系统生成' }}
          </small>
        </div>
        <div class="preview-actions">
          <select
            v-if="wdlRevisions.length"
            :value="selectedWdlVersion"
            aria-label="选择 WDL 版本"
            @change="emit('selectWdlVersion', Number(($event.target as HTMLSelectElement).value))"
          >
            <option v-for="revision in wdlRevisions" :key="revision.version" :value="revision.version">
              WDL v{{ revision.version }} · {{ revision.source === 'manual' ? '人工' : '系统' }}
            </option>
          </select>
          <span
            v-if="selectedWdlRevision"
            class="source-tag"
            :class="`source-tag--${selectedWdlRevision.source}`"
          >
            {{ selectedWdlRevision.source === 'manual' ? '人工' : '系统' }}
          </span>
          <button
            v-if="!editingWdl"
            class="button button--ghost"
            type="button"
            :disabled="!activeWdlContent"
            @click="emit('beginWdlEdit')"
          >
            编辑为新版本
          </button>
          <button
            v-else
            class="button button--primary"
            type="button"
            :disabled="wdlSaveState === 'saving'"
            @click="emit('saveWdl')"
          >
            {{ wdlSaveState === 'saving' ? '保存中…' : '保存人工版本' }}
          </button>
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
            download="workflow.wdl"
          >下载 WDL</a>
        </div>
      </header>

      <textarea
        v-if="editingWdl"
        v-model="wdlDraft"
        class="wdl-editor"
        aria-label="编辑 WDL 内容"
        spellcheck="false"
      />
      <div v-else-if="activeWdlContent" class="code-viewer" aria-label="WDL 只读预览" tabindex="0">
        <div v-for="(line, index) in previewLines" :key="index" class="code-line">
          <span aria-hidden="true">{{ index + 1 }}</span>
          <code>{{ line || ' ' }}</code>
        </div>
      </div>
      <p v-if="wdlSaveState === 'error'" class="inline-error">WDL 保存或语法校验失败，请检查后重试。</p>
      <div v-if="!editingWdl && !activeWdlContent" class="preview-empty">
        <strong>WDL 预览将在编译后显示</strong>
        <p>这里会保留原始换行、提供行号，并支持复制和下载。</p>
      </div>
    </section>
  </div>
</template>
