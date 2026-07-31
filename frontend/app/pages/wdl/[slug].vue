<script setup lang="ts">
import { createTwoFilesPatch } from 'diff'
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import WdlCodeEditor from '~/components/wdl/WdlCodeEditor.vue'
import type {
  WdlAsset,
  WdlAuditEvent,
  WdlSourceRevision,
  WdlTag,
} from '~/types/wdl'

type WorkspaceSection = 'edit' | 'tools' | 'artifacts' | 'wdl' | 'help'
type InspectorTab = 'structure' | 'diagnostics' | 'diff' | 'history'
type WdlCodeEditorHandle = {
  applyFormattedValue: (value: string) => void
}
type MetadataField = 'name' | 'description'
type MetadataPatch = Partial<Pick<WdlAsset, 'name' | 'description' | 'tags'>> & {
  note: string
}

const route = useRoute()
const slug = computed(() => String(route.params.slug))
const asset = ref<WdlAsset>()
const availableTags = ref<WdlTag[]>([])
const selectedRevision = ref<WdlSourceRevision>()
const content = ref('')
const baseContent = ref('')
const pendingOperation = ref<'edit' | 'format'>('edit')
const revisionNote = ref('')
const editingMetadataField = ref<MetadataField>()
const assetNameDraft = ref('')
const assetDescriptionDraft = ref('')
const tagDraft = ref('')
const inspectorTab = ref<InspectorTab>('structure')
const selectedEvent = ref<WdlAuditEvent>()
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const saveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
const metadataState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
const feedback = ref('')
const codeEditor = ref<WdlCodeEditorHandle>()
const assetNameInput = ref<HTMLInputElement>()
const assetDescriptionInput = ref<HTMLTextAreaElement>()
let metadataSaveQueue = Promise.resolve()

const dirty = computed(() => content.value !== baseContent.value)
const latestVersion = computed(() => asset.value?.current_revision?.version)
const isHistoricalRevision = computed(
  () => selectedRevision.value?.version !== latestVersion.value,
)
const isRawImportedRevision = computed(
  () => selectedRevision.value?.operation === 'import' && !dirty.value,
)
const analysis = computed(() => selectedRevision.value?.analysis)
const auditEvents = computed(() => asset.value?.audit_events ?? [])
const popularTags = computed(() => availableTags.value.slice(0, 3))
const changeDiff = computed(() => {
  if (!dirty.value) return ''
  const filename = asset.value?.source_filename || 'workflow.wdl'
  const version = selectedRevision.value?.version ?? '—'
  return createTwoFilesPatch(
    `${filename} (v${version})`,
    `${filename} (working)`,
    baseContent.value,
    content.value,
    '',
    '',
    { context: 3 },
  )
})
const operationLabels: Record<WdlSourceRevision['operation'], string> = {
  import: '导入',
  edit: '编辑',
  format: '格式化',
}
const actionLabels: Record<string, string> = {
  import: '导入源码',
  edit: '保存修改',
  format: '格式化源码',
  metadata_update: '更新信息与标签',
}

function navigateSection(section: WorkspaceSection) {
  if (section === 'wdl') {
    void navigateTo('/wdl')
    return
  }
  void navigateTo(`/?section=${section}`)
}

function applyRevision(revision: WdlSourceRevision) {
  selectedRevision.value = revision
  content.value = revision.content ?? ''
  baseContent.value = revision.content ?? ''
  pendingOperation.value = 'edit'
  revisionNote.value = ''
  feedback.value = ''
  if (inspectorTab.value === 'diff') inspectorTab.value = 'structure'
}

async function loadAsset() {
  loadState.value = 'loading'
  try {
    const [assetResponse, tagResponse] = await Promise.all([
      $fetch<WdlAsset>(`/api/v1/wdl-assets/${encodeURIComponent(slug.value)}`),
      $fetch<{ results: WdlTag[] }>('/api/v1/wdl-assets/tags'),
    ])
    asset.value = assetResponse
    availableTags.value = tagResponse.results
    if (assetResponse.current_revision) applyRevision(assetResponse.current_revision)
    selectedEvent.value = assetResponse.audit_events?.[0]
    loadState.value = 'ready'
  } catch {
    loadState.value = 'error'
  }
}

async function loadRevision(version: number) {
  if (dirty.value && !window.confirm('当前未保存的修改会丢失，继续切换版本吗？')) return
  try {
    const revision = await $fetch<WdlSourceRevision>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/revisions/${version}`,
    )
    applyRevision(revision)
  } catch {
    feedback.value = `WDL v${version} 读取失败。`
  }
}

async function returnToLatest() {
  if (!latestVersion.value) return
  await loadRevision(latestVersion.value)
}

async function formatSource() {
  if (isHistoricalRevision.value || !content.value.trim()) return
  saveState.value = 'saving'
  feedback.value = ''
  try {
    const result = await $fetch<{
      content: string
      changed: boolean
      diff: string
    }>(`/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/format`, {
      method: 'POST',
      body: { content: content.value },
    })
    if (!result.changed) {
      feedback.value = '当前源码已经符合格式。'
      if (inspectorTab.value === 'diff') inspectorTab.value = 'structure'
      saveState.value = 'idle'
      return
    }
    if (codeEditor.value) {
      codeEditor.value.applyFormattedValue(result.content)
    } else {
      content.value = result.content
    }
    inspectorTab.value = 'diff'
    pendingOperation.value = 'format'
    revisionNote.value ||= '统一 WDL 格式'
    feedback.value = '格式化结果已应用到编辑器，保存后会形成新版本。'
    saveState.value = 'idle'
  } catch (error: any) {
    saveState.value = 'error'
    inspectorTab.value = 'diagnostics'
    feedback.value = error?.data?.error?.message ?? '格式化失败；请先修复语法错误。'
  }
}

async function saveRevision() {
  if (!dirty.value || isHistoricalRevision.value) return
  saveState.value = 'saving'
  feedback.value = ''
  try {
    const revision = await $fetch<WdlSourceRevision>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/revisions`,
      {
        method: 'POST',
        body: {
          content: content.value,
          operation: pendingOperation.value,
          note: revisionNote.value.trim(),
        },
      },
    )
    await loadAsset()
    selectedRevision.value = {
      ...revision,
      content: revision.content,
    }
    content.value = revision.content ?? ''
    baseContent.value = revision.content ?? ''
    saveState.value = 'saved'
    feedback.value = `WDL v${revision.version} 已保存并写入操作历史。`
    window.setTimeout(() => {
      if (saveState.value === 'saved') saveState.value = 'idle'
    }, 2200)
  } catch (error: any) {
    saveState.value = 'error'
    feedback.value = error?.data?.error?.message ?? 'WDL 版本保存失败。'
  }
}

function beginMetadataEdit(field: MetadataField) {
  if (!asset.value) return
  editingMetadataField.value = field
  if (field === 'name') {
    assetNameDraft.value = asset.value.name
  } else {
    assetDescriptionDraft.value = asset.value.description
  }
  void nextTick(() => {
    const input = field === 'name' ? assetNameInput.value : assetDescriptionInput.value
    input?.focus()
    input?.select()
  })
}

function cancelMetadataEdit() {
  editingMetadataField.value = undefined
}

async function refreshTagPool() {
  try {
    const response = await $fetch<{ results: WdlTag[] }>('/api/v1/wdl-assets/tags')
    availableTags.value = response.results
  } catch {
    // Metadata is already saved; stale suggestions can refresh on the next page load.
  }
}

function queueMetadataPatch(body: MetadataPatch, successMessage: string) {
  let resolveResult: (saved: boolean) => void = () => undefined
  const result = new Promise<boolean>((resolve) => {
    resolveResult = resolve
  })
  metadataSaveQueue = metadataSaveQueue.then(async () => {
    const saved = await saveMetadataPatch(body, successMessage)
    resolveResult(saved)
  })
  return result
}

async function saveMetadataPatch(body: MetadataPatch, successMessage: string) {
  if (!asset.value) return false
  metadataState.value = 'saving'
  try {
    const updated = await $fetch<WdlAsset>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}`,
      {
        method: 'PATCH',
        body,
      },
    )
    asset.value = updated
    selectedEvent.value = updated.audit_events?.[0]
    metadataState.value = 'saved'
    feedback.value = successMessage
    if (body.tags) await refreshTagPool()
    return true
  } catch {
    metadataState.value = 'error'
    feedback.value = 'WDL 信息自动保存失败，请重试。'
    return false
  }
}

async function commitMetadataEdit(field: MetadataField) {
  if (editingMetadataField.value !== field || !asset.value) return
  editingMetadataField.value = undefined
  const original = asset.value[field]
  const value = (
    field === 'name' ? assetNameDraft.value : assetDescriptionDraft.value
  ).trim()
  if (field === 'name' && !value) {
    feedback.value = 'WDL 标题不能为空，已保留原值。'
    return
  }
  if (value === original) return

  asset.value = { ...asset.value, [field]: value }
  const fieldLabel = field === 'name' ? '标题' : '说明'
  const saved = await queueMetadataPatch(
    { [field]: value, note: `自动更新${fieldLabel}` },
    `${fieldLabel}已自动保存。`,
  )
  if (!saved && asset.value) asset.value = { ...asset.value, [field]: original }
}

function isTagSelected(name: string) {
  return asset.value?.tags.some(tag => tag.toLocaleLowerCase() === name.toLocaleLowerCase()) ?? false
}

async function addTag(name: string) {
  if (!asset.value) return
  const trimmed = name.trim()
  tagDraft.value = ''
  if (!trimmed || isTagSelected(trimmed)) return
  const canonical = availableTags.value.find(
    tag => tag.name.toLocaleLowerCase() === trimmed.toLocaleLowerCase(),
  )?.name ?? trimmed
  const previousTags = [...asset.value.tags]
  const tags = [...previousTags, canonical]
  asset.value = { ...asset.value, tags }
  const saved = await queueMetadataPatch(
    { tags, note: `添加标签 ${canonical}` },
    `标签“${canonical}”已自动保存。`,
  )
  if (!saved && asset.value) asset.value = { ...asset.value, tags: previousTags }
}

function commitTagDraft() {
  const value = tagDraft.value
  tagDraft.value = ''
  if (!value.trim()) return
  void addTag(value)
}

function blurCurrentTarget(event: Event) {
  if (event.currentTarget instanceof HTMLElement) event.currentTarget.blur()
}

async function removeTag(name: string) {
  if (!asset.value) return
  const previousTags = [...asset.value.tags]
  const tags = previousTags.filter(tag => tag !== name)
  asset.value = { ...asset.value, tags }
  const saved = await queueMetadataPatch(
    { tags, note: `移除标签 ${name}` },
    `标签“${name}”已移除。`,
  )
  if (!saved && asset.value) asset.value = { ...asset.value, tags: previousTags }
}

function exportTimestamp(date = new Date()) {
  const pad = (value: number) => String(value).padStart(2, '0')
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
    '-',
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds()),
  ].join('')
}

function sanitizeDownloadName(value: string) {
  return value
    .normalize('NFKC')
    .replace(/\.wdl$/i, '')
    .replace(/[\\/:*?"<>|\u0000-\u001F]+/g, '-')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '')
    .slice(0, 120) || 'workflow'
}

function exportWdl() {
  if (!asset.value || !content.value) return
  const version = selectedRevision.value?.version ?? 1
  const versionLabel = `v${version}${dirty.value ? '-draft' : ''}`
  const filename = [
    sanitizeDownloadName(asset.value.name),
    versionLabel,
    exportTimestamp(),
  ].join('-') + '.wdl'
  const url = URL.createObjectURL(new Blob([content.value], {
    type: 'text/plain;charset=utf-8',
  }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
  feedback.value = `已导出 ${filename}`
}

function diffLineClass(line: string) {
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-line--file'
  if (line.startsWith('+')) return 'diff-line--add'
  if (line.startsWith('-')) return 'diff-line--remove'
  if (line.startsWith('@@')) return 'diff-line--range'
  return ''
}

function handleSaveShortcut(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void saveRevision()
  }
}

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  void loadAsset()
  window.addEventListener('keydown', handleSaveShortcut)
  window.addEventListener('beforeunload', handleBeforeUnload)
})

watch(dirty, (value) => {
  if (!value && inspectorTab.value === 'diff') inspectorTab.value = 'structure'
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleSaveShortcut)
  window.removeEventListener('beforeunload', handleBeforeUnload)
})

onBeforeRouteLeave(() => {
  if (dirty.value && !window.confirm('当前 WDL 还有未保存的修改，确定离开吗？')) {
    return false
  }
})
</script>

<template>
  <div class="app-shell app-shell--workspace">
    <AppTopbar section="WDL 工作台" :current="asset?.name ?? slug">
      <template #status>
        <span class="save-state">
          <span class="status-dot" :class="{ 'status-dot--warning': dirty }" />
          {{
            loadState === 'loading' ? '正在读取…'
              : saveState === 'saving' ? '正在保存…'
                : dirty ? '有未保存修改'
                  : `WDL v${selectedRevision?.version ?? '—'}`
          }}
        </span>
      </template>
      <template #actions>
        <button
          class="button button--primary wdl-save-button"
          type="button"
          aria-label="保存新版本"
          title="保存新版本（Ctrl/⌘ + S）"
          :disabled="!dirty || isHistoricalRevision || saveState === 'saving'"
          @click="saveRevision"
        >
          <span>{{ saveState === 'saving' ? '保存中…' : '保存' }}</span>
          <kbd v-if="saveState !== 'saving'">Ctrl/⌘ S</kbd>
        </button>
      </template>
    </AppTopbar>

    <AppRail active="wdl" @select="navigateSection" />

    <main v-if="loadState === 'ready' && asset" class="section-workspace wdl-workbench">
      <aside class="wdl-workbench__sidebar">
        <NuxtLink class="back-link" to="/wdl">← 返回 WDL 资产</NuxtLink>
        <div class="wdl-asset-heading">
          <h1
            v-if="editingMetadataField !== 'name'"
            class="inline-metadata-value inline-metadata-value--title"
            tabindex="0"
            title="双击修改标题"
            @dblclick="beginMetadataEdit('name')"
            @keydown.enter.prevent="beginMetadataEdit('name')"
          >
            {{ asset.name }}
          </h1>
          <input
            v-else
            ref="assetNameInput"
            v-model="assetNameDraft"
            class="inline-metadata-input inline-metadata-input--title"
            aria-label="WDL 标题"
            maxlength="256"
            @blur="commitMetadataEdit('name')"
            @keydown.enter.prevent="blurCurrentTarget"
            @keydown.esc.prevent="cancelMetadataEdit"
          />
          <p
            v-if="editingMetadataField !== 'description'"
            class="inline-metadata-value inline-metadata-value--description"
            tabindex="0"
            title="双击修改说明"
            @dblclick="beginMetadataEdit('description')"
            @keydown.enter.prevent="beginMetadataEdit('description')"
          >
            {{ asset.description || '暂无资产说明。' }}
          </p>
          <textarea
            v-else
            ref="assetDescriptionInput"
            v-model="assetDescriptionDraft"
            class="inline-metadata-input inline-metadata-input--description"
            aria-label="WDL 说明"
            rows="3"
            @blur="commitMetadataEdit('description')"
            @keydown.esc.prevent="cancelMetadataEdit"
          />
        </div>

        <section class="wdl-sidebar-section">
          <h2>标签</h2>
          <div class="field wdl-tag-field">
            <div class="wdl-tag-editor">
              <div v-if="asset.tags.length" class="wdl-tag-editor__selected">
                <span v-for="tag in asset.tags" :key="tag" class="wdl-tag-editor__chip">
                  {{ tag }}
                  <button
                    type="button"
                    :aria-label="`移除标签 ${tag}`"
                    :disabled="metadataState === 'saving'"
                    @click="removeTag(tag)"
                  >
                    ×
                  </button>
                </span>
              </div>
              <input
                v-model="tagDraft"
                aria-label="添加标签"
                maxlength="64"
                placeholder="输入一个标签"
                @blur="commitTagDraft"
                @keydown.enter.prevent="blurCurrentTarget"
                @keydown.esc.prevent="tagDraft = ''"
              />
            </div>
          </div>
          <div v-if="popularTags.length" class="tag-suggestions tag-suggestions--popular">
            <span>常用</span>
            <button
              v-for="tag in popularTags"
              :key="tag.name"
              type="button"
              :disabled="isTagSelected(tag.name) || metadataState === 'saving'"
              @click="addTag(tag.name)"
            >
              {{ isTagSelected(tag.name) ? '✓' : '+' }} {{ tag.name }}
            </button>
          </div>
        </section>

        <section class="wdl-sidebar-section wdl-revision-list">
          <h2>源码版本</h2>
          <button
            v-for="revision in asset.revisions"
            :key="revision.version"
            type="button"
            :class="{ 'wdl-revision-list__active': revision.version === selectedRevision?.version }"
            @click="loadRevision(revision.version)"
          >
            <span>
              <strong>v{{ revision.version }}</strong>
              <small>{{ operationLabels[revision.operation] }} · {{ revision.actor }}</small>
            </span>
            <time>{{ new Date(revision.created_at).toLocaleDateString('zh-CN') }}</time>
          </button>
        </section>
      </aside>

      <section class="wdl-workbench__editor">
        <header class="wdl-editor-toolbar">
          <div>
            <strong>{{ asset.source_filename }}</strong>
            <small>
              WDL {{ analysis?.wdl_version ?? '未知版本' }}
              · {{ content.split('\n').length }} 行
              · sha256 {{ selectedRevision?.digest.slice(-10) }}
            </small>
          </div>
          <div class="wdl-editor-toolbar__actions">
            <button
              v-if="isHistoricalRevision"
              class="button button--ghost"
              type="button"
              @click="returnToLatest"
            >
              返回最新版本
            </button>
            <button
              class="button button--ghost wdl-export-button"
              type="button"
              aria-label="导出 WDL"
              title="导出 WDL（Shift + Alt/Option + E）"
              @click="exportWdl"
            >
              <span>导出</span>
              <kbd>⇧ Alt E</kbd>
            </button>
            <button
              v-if="!isHistoricalRevision"
              class="button button--ghost wdl-format-button"
              type="button"
              aria-label="格式化"
              title="格式化 WDL（Shift + Alt/Option + F）"
              :disabled="saveState === 'saving'"
              @click="formatSource"
            >
              <span>格式化</span>
              <kbd>⇧ Alt F</kbd>
            </button>
          </div>
        </header>

        <div class="wdl-editor-status" role="status" aria-live="polite">
          <div v-if="isHistoricalRevision" class="historical-notice">
            正在查看不可变历史版本 v{{ selectedRevision?.version }}。返回最新版本后才能继续编辑。
          </div>
          <div v-else-if="feedback" class="workbench-feedback" :class="{ 'workbench-feedback--error': saveState === 'error' || metadataState === 'error' }">
            {{ feedback }}
          </div>
          <div v-else-if="isRawImportedRevision" class="raw-import-notice">
            当前是保留原貌的导入版本，尚未格式化。
          </div>
          <div v-else class="editor-ready-notice">
            编辑器已就绪
          </div>
        </div>

        <ClientOnly>
          <WdlCodeEditor
            ref="codeEditor"
            v-model="content"
            :read-only="isHistoricalRevision"
            :formatting="saveState === 'saving'"
            :aria-label="`${asset.name} WDL 源码`"
            @export="exportWdl"
            @format="formatSource"
          />
          <template #fallback>
            <div class="wdl-editor-loading">正在载入源码编辑器…</div>
          </template>
        </ClientOnly>

        <div v-if="!isHistoricalRevision" class="wdl-revision-note">
          <label class="field">
            <span>本次修改备注</span>
            <input
              v-model="revisionNote"
              placeholder="说明改了什么、为什么改；保存后进入操作历史"
            />
          </label>
        </div>
      </section>

      <aside class="wdl-workbench__inspector">
        <div class="panel-tabs panel-tabs--inspector" role="tablist" aria-label="WDL 工作台检查器">
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'structure'"
            :class="{ 'panel-tab--active': inspectorTab === 'structure' }"
            @click="inspectorTab = 'structure'"
          >
            结构
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'diagnostics'"
            :class="{ 'panel-tab--active': inspectorTab === 'diagnostics' }"
            @click="inspectorTab = 'diagnostics'"
          >
            诊断 <span class="tab-count">{{ analysis?.diagnostics.length ?? 0 }}</span>
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'history'"
            :class="{ 'panel-tab--active': inspectorTab === 'history' }"
            @click="inspectorTab = 'history'"
          >
            历史 <span class="tab-count">{{ auditEvents.length }}</span>
          </button>
          <button
            v-if="dirty"
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'diff'"
            :class="{ 'panel-tab--active': inspectorTab === 'diff' }"
            @click="inspectorTab = 'diff'"
          >
            变更
          </button>
        </div>

        <div v-if="inspectorTab === 'structure'" class="wdl-inspector-content">
          <div class="analysis-summary">
            <span>
              <strong>{{ analysis?.summary.task_count ?? 0 }}</strong>
              <small>Tasks</small>
            </span>
            <span>
              <strong>{{ analysis?.summary.workflow_count ?? 0 }}</strong>
              <small>Workflows</small>
            </span>
            <span>
              <strong>{{ analysis?.summary.import_count ?? 0 }}</strong>
              <small>Imports</small>
            </span>
          </div>

          <section v-if="analysis?.imports.length" class="definition-group">
            <h2>Imports</h2>
            <article v-for="item in analysis.imports" :key="item.uri">
              <strong>{{ item.namespace || item.uri }}</strong>
              <code>{{ item.uri }}</code>
              <small>第 {{ item.line ?? '—' }} 行 · 当前按单文件资产记录，依赖包将在后续版本补齐</small>
            </article>
          </section>

          <section class="definition-group">
            <h2>Tasks</h2>
            <article v-for="task in analysis?.tasks" :key="task.name">
              <strong>{{ task.name }}</strong>
              <small>第 {{ task.line ?? '—' }}–{{ task.end_line ?? '—' }} 行</small>
              <code>{{ task.inputs.length }} inputs → {{ task.outputs.length }} outputs</code>
              <div v-if="task.runtime_keys.length" class="tag-list tag-list--compact">
                <span v-for="key in task.runtime_keys" :key="key">{{ key }}</span>
              </div>
            </article>
            <p v-if="analysis?.tasks.length === 0" class="empty-state">没有解析到 task。</p>
          </section>

          <section class="definition-group">
            <h2>Workflows</h2>
            <article v-for="workflow in analysis?.workflows" :key="workflow.name">
              <strong>{{ workflow.name }}</strong>
              <small>第 {{ workflow.line ?? '—' }}–{{ workflow.end_line ?? '—' }} 行</small>
              <code>
                {{ workflow.structure.call_count }} calls
                · {{ workflow.structure.scatter_count }} scatter
                · {{ workflow.structure.conditional_count }} if
              </code>
            </article>
            <p v-if="analysis?.workflows.length === 0" class="empty-state">没有解析到 workflow。</p>
          </section>
        </div>

        <div v-else-if="inspectorTab === 'diagnostics'" class="wdl-inspector-content">
          <div
            class="analysis-banner"
            :class="`analysis-banner--${analysis?.status ?? 'invalid'}`"
          >
            <strong>{{ analysis?.status === 'valid' ? '静态检查通过' : '需要处理' }}</strong>
            <p>
              {{
                analysis?.status === 'valid'
                  ? '源码可以继续格式化和拆解。'
                  : `${analysis?.summary.error_count ?? 0} 个解析或类型问题。`
              }}
            </p>
          </div>
          <article v-for="item in analysis?.diagnostics" :key="`${item.code}-${item.message}`" class="diagnostic">
            <div>
              <strong>{{ item.code }}</strong>
              <span>错误</span>
            </div>
            <p>{{ item.message }}</p>
            <small v-if="item.location">第 {{ item.location.line ?? '—' }} 行，第 {{ item.location.column ?? '—' }} 列</small>
          </article>
          <p v-if="analysis?.diagnostics.length === 0" class="empty-state">当前版本没有诊断信息。</p>
        </div>

        <div v-else-if="inspectorTab === 'diff'" class="wdl-inspector-content wdl-format-diff-panel">
          <header>
            <div>
              <strong>未保存变更</strong>
              <small>相对 WDL v{{ selectedRevision?.version }}；保存后写入历史</small>
            </div>
            <button
              class="button button--ghost"
              type="button"
              @click="inspectorTab = 'structure'"
            >
              收起
            </button>
          </header>
          <pre class="audit-diff"><code><span
            v-for="(line, index) in changeDiff.split('\n')"
            :key="index"
            :class="diffLineClass(line)"
          >{{ line || ' ' }}
</span></code></pre>
        </div>

        <div v-else class="wdl-history-panel">
          <div class="wdl-history-list">
            <button
              v-for="event in auditEvents"
              :key="event.id"
              type="button"
              :class="{ 'wdl-history-list__active': event.id === selectedEvent?.id }"
              @click="selectedEvent = event"
            >
              <span>
                <strong>{{ actionLabels[event.action] ?? event.action }}</strong>
                <small>{{ event.actor }} · {{ new Date(event.created_at).toLocaleString('zh-CN') }}</small>
              </span>
              <em v-if="event.revision">v{{ event.revision }}</em>
            </button>
          </div>
          <article v-if="selectedEvent" class="wdl-history-detail">
            <header>
              <strong>{{ actionLabels[selectedEvent.action] ?? selectedEvent.action }}</strong>
              <small>{{ new Date(selectedEvent.created_at).toLocaleString('zh-CN') }}</small>
            </header>
            <dl>
              <div><dt>操作者</dt><dd>{{ selectedEvent.actor }}</dd></div>
              <div><dt>版本</dt><dd>{{ selectedEvent.revision ? `v${selectedEvent.revision}` : '仅元数据' }}</dd></div>
              <div><dt>备注</dt><dd>{{ selectedEvent.note || '未填写备注' }}</dd></div>
            </dl>
            <pre v-if="selectedEvent.diff" class="audit-diff"><code><span
              v-for="(line, index) in selectedEvent.diff.split('\n')"
              :key="index"
              :class="diffLineClass(line)"
            >{{ line || ' ' }}
</span></code></pre>
            <div v-else-if="Object.keys(selectedEvent.changes).length" class="metadata-changes">
              <strong>字段变化</strong>
              <dl>
                <div v-for="(change, field) in selectedEvent.changes" :key="field">
                  <dt>{{ field }}</dt>
                  <dd>{{ JSON.stringify(change) }}</dd>
                </div>
              </dl>
            </div>
          </article>
        </div>
      </aside>
    </main>

    <main v-else class="section-workspace wdl-workbench-state">
      <div v-if="loadState === 'loading'" class="wdl-editor-loading">正在打开 WDL 工作台…</div>
      <div v-else class="empty-state">
        <strong>WDL 资产无法打开</strong>
        <p>该资产不存在，或后端服务当前不可用。</p>
        <NuxtLink class="button button--ghost button-link" to="/wdl">返回资产台账</NuxtLink>
      </div>
    </main>
  </div>
</template>
