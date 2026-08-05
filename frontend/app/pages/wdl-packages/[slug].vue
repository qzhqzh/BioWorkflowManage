<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import WdlCodeEditor from '~/components/wdl/WdlCodeEditor.vue'
import WdlFileTree from '~/components/wdl/WdlFileTree.vue'
import type {
  WdlSourceFile,
  WdlTaskDefinition,
  WdlToolPackage,
  WdlToolPackageVersion,
} from '~/types/wdl'

const { $api: $fetch } = useNuxtApp()
const route = useRoute()
const slug = computed(() => String(route.params.slug))

type WorkspaceSection = 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help'
type InspectorTab = 'tasks' | 'diagnostics' | 'references' | 'history' | 'publish'
type EditorHandle = { revealLine: (line: number) => void }

const packageAsset = ref<WdlToolPackage>()
const selectedVersion = ref<WdlToolPackageVersion>()
const activeFilePath = ref('')
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const inspectorTab = ref<InspectorTab>('tasks')
const feedback = ref('')
const publishState = ref<'idle' | 'saving' | 'error'>('idle')
const publishError = ref('')
const publishFile = ref<File>()
const publishInput = ref<HTMLInputElement>()
const codeEditor = ref<EditorHandle>()
const publishDraft = ref({ version: '', sourceRepository: '', sourceRevision: '', note: '' })

const sourceFiles = computed<WdlSourceFile[]>(() =>
  (selectedVersion.value?.files ?? []).map(file => ({ ...file, is_entry: false })),
)
const content = computed({
  get: () => selectedVersion.value?.files.find(file => file.path === activeFilePath.value)?.content ?? '',
  set: () => undefined,
})
const analysis = computed(() => selectedVersion.value?.analysis)

function navigateSection(section: WorkspaceSection) {
  if (section === 'packages') {
    void navigateTo('/wdl-packages')
    return
  }
  if (section === 'wdl') {
    void navigateTo('/wdl')
    return
  }
  if (section === 'runs') {
    void navigateTo('/runs')
    return
  }
  void navigateTo(`/?section=${section}`)
}

async function loadVersion(version: string) {
  try {
    selectedVersion.value = await $fetch<WdlToolPackageVersion>(
      `/api/v1/wdl-packages/${encodeURIComponent(slug.value)}/versions/${encodeURIComponent(version)}`,
    )
    activeFilePath.value = selectedVersion.value.files[0]?.path ?? ''
    feedback.value = ''
  } catch {
    feedback.value = `版本 ${version} 读取失败。`
  }
}

async function loadPackage(preferredVersion?: string) {
  loadState.value = 'loading'
  try {
    packageAsset.value = await $fetch<WdlToolPackage>(
      `/api/v1/wdl-packages/${encodeURIComponent(slug.value)}`,
    )
    const version = preferredVersion || packageAsset.value.latest_version?.version
    if (version) await loadVersion(version)
    loadState.value = 'ready'
  } catch {
    loadState.value = 'error'
  }
}

function selectSourceFile(path: string, line?: number) {
  activeFilePath.value = path
  if (line) void nextTick(() => codeEditor.value?.revealLine(line))
}

function showTask(task: WdlTaskDefinition) {
  if (task.file_path) selectSourceFile(task.file_path, task.line)
}

function selectPublishFile(event: Event) {
  publishFile.value = (event.target as HTMLInputElement).files?.[0]
  publishError.value = ''
}

async function publishVersion() {
  if (!publishFile.value || !publishDraft.value.version.trim()) {
    publishState.value = 'error'
    publishError.value = '请选择 ZIP，并填写版本。'
    return
  }
  publishState.value = 'saving'
  publishError.value = ''
  try {
    const body = new FormData()
    body.append('archive', publishFile.value)
    body.append('version', publishDraft.value.version.trim())
    body.append('source_repository', publishDraft.value.sourceRepository.trim())
    body.append('source_revision', publishDraft.value.sourceRevision.trim())
    body.append('note', publishDraft.value.note.trim())
    const created = await $fetch<WdlToolPackageVersion>(
      `/api/v1/wdl-packages/${encodeURIComponent(slug.value)}/versions`,
      { method: 'POST', body },
    )
    publishState.value = 'idle'
    publishFile.value = undefined
    publishDraft.value = { version: '', sourceRepository: '', sourceRevision: '', note: '' }
    if (publishInput.value) publishInput.value.value = ''
    inspectorTab.value = 'tasks'
    await loadPackage(created.version)
    feedback.value = `已发布 ${created.version}`
  } catch (error: any) {
    publishState.value = 'error'
    publishError.value = error?.data?.error?.message ?? '版本发布失败。'
  }
}

async function exportPackage() {
  if (!packageAsset.value || !selectedVersion.value) return
  const blob = await $fetch<Blob>(
    `/api/v1/wdl-packages/${encodeURIComponent(slug.value)}/export`,
    { responseType: 'blob', query: { version: selectedVersion.value.version } },
  )
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${packageAsset.value.slug}-${selectedVersion.value.version}.zip`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
  feedback.value = `已导出 ${anchor.download}`
}

async function toggleArchive() {
  if (!packageAsset.value) return
  const lifecycle = packageAsset.value.lifecycle === 'active' ? 'archived' : 'active'
  try {
    packageAsset.value = await $fetch<WdlToolPackage>(
      `/api/v1/wdl-packages/${encodeURIComponent(slug.value)}`,
      { method: 'PATCH', body: { lifecycle, note: lifecycle === 'archived' ? '归档工具包' : '恢复工具包' } },
    )
    feedback.value = lifecycle === 'archived' ? '工具包已归档。' : '工具包已恢复。'
  } catch {
    feedback.value = '状态更新失败。'
  }
}

onMounted(() => void loadPackage())
</script>

<template>
  <div class="app-shell app-shell--workspace">
    <AppTopbar section="WDL 工具包" :current="packageAsset?.name ?? slug">
      <template #status>
        <span class="save-state">
          <span class="status-dot" :class="{ 'status-dot--warning': analysis?.status === 'invalid' }" />
          {{ selectedVersion ? `${selectedVersion.version} · ${selectedVersion.file_count} 文件` : '正在读取…' }}
        </span>
      </template>
      <template #actions>
        <button
          class="button button--primary"
          type="button"
          :disabled="packageAsset?.lifecycle === 'archived'"
          @click="inspectorTab = 'publish'"
        >
          发布新版本
        </button>
      </template>
    </AppTopbar>

    <AppRail active="packages" @select="navigateSection" />

    <main v-if="loadState === 'ready' && packageAsset && selectedVersion" class="section-workspace wdl-workbench wdl-package-workbench">
      <aside class="wdl-workbench__sidebar">
        <NuxtLink class="back-link" to="/wdl-packages">← 返回工具包</NuxtLink>
        <div class="wdl-asset-heading">
          <h1>{{ packageAsset.name }}</h1>
          <p v-if="packageAsset.description">{{ packageAsset.description }}</p>
          <div class="tag-list tag-list--compact wdl-package-heading__tags">
            <span v-for="tag in packageAsset.tags" :key="tag">{{ tag }}</span>
          </div>
        </div>

        <section class="wdl-sidebar-section wdl-source-files">
          <h2>文件 <span>{{ sourceFiles.length }}</span></h2>
          <WdlFileTree :files="sourceFiles" :active-path="activeFilePath" @select="selectSourceFile" />
        </section>

        <section class="wdl-sidebar-section wdl-revision-list">
          <h2>版本</h2>
          <button
            v-for="version in packageAsset.versions"
            :key="version.version"
            type="button"
            :class="{ 'wdl-revision-list__active': version.version === selectedVersion.version }"
            @click="loadVersion(version.version)"
          >
            <span>
              <strong>{{ version.version }}</strong>
              <small>{{ version.actor }} · {{ version.file_count }} 文件</small>
            </span>
            <time>{{ new Date(version.created_at).toLocaleDateString('zh-CN') }}</time>
          </button>
        </section>

        <section class="wdl-sidebar-section">
          <button class="button button--ghost" type="button" @click="toggleArchive">
            {{ packageAsset.lifecycle === 'active' ? '归档工具包' : '恢复工具包' }}
          </button>
        </section>
      </aside>

      <section class="wdl-workbench__editor">
        <header class="wdl-editor-toolbar">
          <div>
            <strong>{{ activeFilePath }}</strong>
            <small>
              WDL {{ selectedVersion.files.find(file => file.path === activeFilePath)?.analysis?.wdl_version ?? '未知版本' }}
              · {{ content.split('\n').length }} 行
            </small>
          </div>
          <div class="wdl-editor-toolbar__actions">
            <button class="button button--ghost" type="button" @click="exportPackage">导出 ZIP</button>
          </div>
        </header>
        <div class="wdl-editor-status" role="status" aria-live="polite">
          <div v-if="feedback" class="workbench-feedback">{{ feedback }}</div>
          <div v-else class="editor-ready-notice">不可变版本 · 只读</div>
        </div>
        <ClientOnly>
          <WdlCodeEditor
            ref="codeEditor"
            v-model="content"
            read-only
            :aria-label="`${packageAsset.name} ${activeFilePath}`"
          />
          <template #fallback><div class="wdl-editor-loading">正在载入源码编辑器…</div></template>
        </ClientOnly>
      </section>

      <aside class="wdl-workbench__inspector">
        <div class="panel-tabs panel-tabs--inspector" role="tablist" aria-label="工具包检查器">
          <button type="button" role="tab" :aria-selected="inspectorTab === 'tasks'" :class="{ 'panel-tab--active': inspectorTab === 'tasks' }" @click="inspectorTab = 'tasks'">
            Task <span class="tab-count">{{ analysis?.tasks.length ?? 0 }}</span>
          </button>
          <button type="button" role="tab" :aria-selected="inspectorTab === 'diagnostics'" :class="{ 'panel-tab--active': inspectorTab === 'diagnostics' }" @click="inspectorTab = 'diagnostics'">
            诊断 <span class="tab-count">{{ analysis?.diagnostics.length ?? 0 }}</span>
          </button>
          <button type="button" role="tab" :aria-selected="inspectorTab === 'references'" :class="{ 'panel-tab--active': inspectorTab === 'references' }" @click="inspectorTab = 'references'">
            引用 <span class="tab-count">{{ packageAsset.reference_count }}</span>
          </button>
          <button type="button" role="tab" :aria-selected="inspectorTab === 'history'" :class="{ 'panel-tab--active': inspectorTab === 'history' }" @click="inspectorTab = 'history'">历史</button>
        </div>

        <div v-if="inspectorTab === 'tasks'" class="wdl-inspector-content">
          <div class="analysis-summary">
            <span><strong>{{ analysis?.summary.task_count ?? 0 }}</strong><small>Task</small></span>
            <span><strong>{{ selectedVersion.file_count }}</strong><small>文件</small></span>
            <span><strong>{{ analysis?.summary.error_count ?? 0 }}</strong><small>诊断</small></span>
          </div>
          <div class="definition-group">
            <button v-for="task in analysis?.tasks" :key="task.id || `${task.file_path}:${task.name}`" class="wdl-package-task" type="button" @click="showTask(task)">
              <strong>{{ task.name }}</strong>
              <code>{{ task.file_path }}</code>
              <small>{{ task.inputs.length }} 输入 · {{ task.outputs.length }} 输出</small>
            </button>
          </div>
        </div>

        <div v-else-if="inspectorTab === 'diagnostics'" class="wdl-inspector-content">
          <div class="analysis-banner" :class="{ 'analysis-banner--invalid': analysis?.status === 'invalid' }">
            <strong>{{ analysis?.status === 'valid' ? '检查通过' : '需要处理' }}</strong>
          </div>
          <button
            v-for="diagnostic in analysis?.diagnostics"
            :key="`${diagnostic.file_path}:${diagnostic.location?.line}:${diagnostic.message}`"
            class="diagnostic wdl-package-diagnostic"
            type="button"
            @click="diagnostic.file_path && selectSourceFile(diagnostic.file_path, diagnostic.location?.line)"
          >
            <div><strong>{{ diagnostic.code }}</strong><small>{{ diagnostic.file_path }}:{{ diagnostic.location?.line ?? '—' }}</small></div>
            <p>{{ diagnostic.message }}</p>
          </button>
        </div>

        <div v-else-if="inspectorTab === 'references'" class="wdl-history-list">
          <NuxtLink
            v-for="reference in packageAsset.references"
            :key="`${reference.asset_slug}:${reference.revision}:${reference.mount_prefix}`"
            class="wdl-package-reference-asset"
            :to="`/wdl/${reference.asset_slug}`"
          >
            <span>
              <strong>{{ reference.asset_name }}</strong>
              <small>WDL v{{ reference.revision }} · {{ reference.mount_prefix || '根目录' }}</small>
            </span>
            <code>{{ reference.package_version }}</code>
          </NuxtLink>
          <p v-if="!packageAsset.references?.length" class="empty-state">暂无主流程引用。</p>
        </div>

        <div v-else-if="inspectorTab === 'history'" class="wdl-history-list">
          <article v-for="event in packageAsset.audit_events" :key="event.id" class="wdl-package-history-item">
            <strong>{{ event.action === 'publish_version' ? '发布版本' : event.action === 'create_package' ? '创建工具包' : '更新信息' }}</strong>
            <small>{{ event.actor }} · {{ new Date(event.created_at).toLocaleString('zh-CN') }}</small>
            <p v-if="event.note">{{ event.note }}</p>
          </article>
        </div>

        <form v-else class="wdl-package-publish" @submit.prevent="publishVersion">
          <h2>发布新版本</h2>
          <input ref="publishInput" class="visually-hidden" type="file" accept=".zip,application/zip" @change="selectPublishFile" />
          <button class="button button--ghost" type="button" @click="publishInput?.click()">
            {{ publishFile?.name || '选择 ZIP' }}
          </button>
          <label class="field"><span>版本</span><input v-model="publishDraft.version" required placeholder="1.1.0" /></label>
          <label class="field"><span>来源仓库</span><input v-model="publishDraft.sourceRepository" /></label>
          <label class="field"><span>来源版本</span><input v-model="publishDraft.sourceRevision" /></label>
          <label class="field"><span>备注</span><input v-model="publishDraft.note" /></label>
          <p v-if="publishError" class="inline-error" role="alert">{{ publishError }}</p>
          <button class="button button--primary" type="submit" :disabled="publishState === 'saving'">
            {{ publishState === 'saving' ? '正在分析…' : '发布版本' }}
          </button>
        </form>
      </aside>
    </main>

    <main v-else class="section-workspace wdl-workbench-state">
      <div class="empty-state">
        <strong>{{ loadState === 'error' ? '工具包无法读取' : '正在读取工具包…' }}</strong>
        <NuxtLink v-if="loadState === 'error'" class="button button--ghost button-link" to="/wdl-packages">返回工具包</NuxtLink>
      </div>
    </main>
  </div>
</template>

<style scoped>
.wdl-package-reference-asset {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-3);
  border-bottom: 1px solid var(--color-border);
  color: inherit;
  text-decoration: none;
}

.wdl-package-reference-asset:hover {
  background: var(--color-surface);
}

.wdl-package-reference-asset span {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.wdl-package-reference-asset small,
.wdl-package-reference-asset code {
  color: var(--color-muted);
  font-size: 11px;
}
</style>
