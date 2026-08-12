<script setup lang="ts">
import ToolDraftEditor from '~/components/tools/ToolDraftEditor.vue'
import SoftwareLibraryPanel from '~/components/tools/SoftwareLibraryPanel.vue'
import ToolTestPanel from '~/components/tools/ToolTestPanel.vue'

interface RegistryTool {
  id: string
  name: string
  description: string
  version: string
  status: string
  isDraftOnly: boolean
  versionCount: number
}

interface ToolVersion {
  tool_id: string
  version: string
  name: string
  digest: string
  created_at: string
}

interface ToolOperationError {
  code: string
  message: string
}

const props = defineProps<{
  tools: RegistryTool[]
  registryLoaded: boolean
  selectedToolId: string
  selectedToolVersions: ToolVersion[]
  selectedToolVersion: string
  selectedToolSpec?: Record<string, any>
  selectedToolSoftwareLinks?: Array<{
    id: number
    role: string
    note: string
    software: { slug: string; name: string }
    release?: { id: number; version: string }
  }>
  toolDraftState: 'idle' | 'saving' | 'saved' | 'publishing' | 'published' | 'error'
  toolDraftValidationStatus?: string
  toolOperationError?: ToolOperationError
  toolCreateState: 'idle' | 'saving' | 'error'
  initialInspectorView: 'version' | 'draft'
}>()

const creatingTool = defineModel<boolean>('creatingTool', { required: true })
const newToolId = defineModel<string>('newToolId', { required: true })
const searchQuery = defineModel<string>('searchQuery', { required: true })
const toolDraft = defineModel<Record<string, any> | undefined>('toolDraft')

const viewMode = ref<'list' | 'cards'>('list')
const libraryMode = ref<'tools' | 'software'>('tools')
const inspectorView = ref<'version' | 'draft'>(props.initialInspectorView)
const versionDetailMode = ref<'details' | 'test'>('details')
const currentPage = ref(1)
const pageSize = 12
const pageCount = computed(() => Math.max(1, Math.ceil(props.tools.length / pageSize)))
const pagedTools = computed(() => {
  const start = (currentPage.value - 1) * pageSize
  return props.tools.slice(start, start + pageSize)
})
const paginationItems = computed<(number | 'ellipsis')[]>(() => {
  if (pageCount.value <= 7) return Array.from({ length: pageCount.value }, (_, index) => index + 1)
  const pages: (number | 'ellipsis')[] = [1]
  const start = Math.max(2, currentPage.value - 1)
  const end = Math.min(pageCount.value - 1, currentPage.value + 1)
  if (start > 2) pages.push('ellipsis')
  for (let page = start; page <= end; page += 1) pages.push(page)
  if (end < pageCount.value - 1) pages.push('ellipsis')
  pages.push(pageCount.value)
  return pages
})
const selectedToolSource = computed<Record<string, any> | undefined>(
  () => props.selectedToolSpec?.metadata?.source_wdl,
)
const selectedToolSourceLink = computed(() => {
  const source = selectedToolSource.value
  if (source?.package_slug) {
    const version = source.package_version
      ? `?version=${encodeURIComponent(source.package_version)}`
      : ''
    return `/wdl-packages/${encodeURIComponent(source.package_slug)}${version}`
  }
  if (source?.asset_slug) {
    const revision = source.revision ? `?revision=${encodeURIComponent(source.revision)}` : ''
    return `/wdl/${encodeURIComponent(source.asset_slug)}${revision}`
  }
  return ''
})
const selectedToolSourceLabel = computed(() => {
  const source = selectedToolSource.value
  if (source?.package_slug) return `${source.package_slug}@${source.package_version}`
  if (source?.asset_slug) return `${source.asset_slug} · WDL v${source.revision}`
  return ''
})
const inspectorPanel = ref<HTMLElement>()
const lastToolTrigger = ref<HTMLElement>()

watch(searchQuery, () => {
  currentPage.value = 1
})

watch(() => props.tools.length, () => {
  currentPage.value = Math.min(currentPage.value, pageCount.value)
})

function setViewMode(mode: 'list' | 'cards') {
  viewMode.value = mode
  currentPage.value = 1
}

function setLibraryMode(mode: 'tools' | 'software') {
  libraryMode.value = mode
  if (mode === 'software' && props.selectedToolId) emit('closeInspector')
}

function openToolInspector(event: MouseEvent, toolId: string) {
  lastToolTrigger.value = event.currentTarget as HTMLElement
  emit('selectTool', toolId)
}

function closeToolInspector() {
  emit('closeInspector')
  void nextTick(() => lastToolTrigger.value?.focus())
}

watch(() => props.selectedToolId, (toolId) => {
  if (!toolId) return
  inspectorView.value = props.initialInspectorView
  versionDetailMode.value = 'details'
  void nextTick(() => inspectorPanel.value?.focus({ preventScroll: true }))
})

watch(() => props.initialInspectorView, (view) => {
  inspectorView.value = view
})

watch([toolDraft, () => props.selectedToolVersions.length], ([draft, versionCount]) => {
  if (draft && versionCount === 0) inspectorView.value = 'draft'
})

function selectDraft() {
  inspectorView.value = 'draft'
  emit('selectDraft', props.selectedToolId)
}

function selectPublishedVersion(toolId: string, version: string) {
  inspectorView.value = 'version'
  versionDetailMode.value = 'details'
  emit('selectVersion', toolId, version)
}

const emit = defineEmits<{
  create: []
  import: []
  selectTool: [toolId: string]
  closeInspector: []
  selectVersion: [toolId: string, version: string]
  selectDraft: [toolId: string]
  useVersionAsDraft: []
  draftDirty: []
  draftSave: []
  draftPublish: []
}>()
</script>

<template>
  <header class="workspace-header">
    <div>
      <h1>工具库</h1>
      <p>{{ libraryMode === 'tools' ? '管理可复用的 Task，并对固定版本进行独立测试。' : '记录软件版本、使用注意事项和相关工具。' }}</p>
    </div>
    <div v-if="libraryMode === 'tools'" class="workspace-header__actions">
      <button class="button button--ghost" type="button" @click="creatingTool = !creatingTool">
        {{ creatingTool ? '取消新建' : '新建工具' }}
      </button>
      <button class="button button--primary" type="button" @click="emit('import')">导入 ToolSpec</button>
    </div>
  </header>

  <nav class="library-scope-tabs" aria-label="工具库范围">
    <button type="button" :class="{ 'is-active': libraryMode === 'tools' }" @click="setLibraryMode('tools')">工具</button>
    <button type="button" :class="{ 'is-active': libraryMode === 'software' }" @click="setLibraryMode('software')">软件知识库</button>
  </nav>

  <form v-if="libraryMode === 'tools' && creatingTool" class="tool-create-panel" @submit.prevent="emit('create')">
    <div>
      <strong>创建工具草稿</strong>
      <p>先建立可编辑草稿，校验通过后再发布不可变版本。</p>
    </div>
    <label class="field">
      <span>工具 ID</span>
      <input
        v-model="newToolId"
        type="text"
        autocomplete="off"
        placeholder="例如 samtools_sort"
        :aria-invalid="toolCreateState === 'error'"
      />
    </label>
    <button class="button button--primary" type="submit" :disabled="toolCreateState === 'saving'">
      {{ toolCreateState === 'saving' ? '创建中…' : '创建草稿' }}
    </button>
  </form>

  <div v-if="libraryMode === 'tools'" class="tool-library-browser" :class="{ 'tool-library-browser--selected': selectedToolId }">
    <section class="tool-library-catalog">
      <div class="workspace-toolbar">
    <label class="search-field workspace-search">
      <span aria-hidden="true">⌕</span>
      <input v-model="searchQuery" type="search" placeholder="搜索工具名称或版本" />
    </label>
    <div class="workspace-toolbar__summary">
      <span>{{ tools.length }} 个工具</span>
      <div class="view-switch" role="group" aria-label="工具展示方式">
        <button
          type="button"
          :class="{ 'is-active': viewMode === 'list' }"
          :aria-pressed="viewMode === 'list'"
          @click="setViewMode('list')"
        >
          <span class="view-switch__list-icon" aria-hidden="true" />
          列表
        </button>
        <button
          type="button"
          :class="{ 'is-active': viewMode === 'cards' }"
          :aria-pressed="viewMode === 'cards'"
          @click="setViewMode('cards')"
        >
          <span class="view-switch__card-icon" aria-hidden="true" />
          卡片
        </button>
      </div>
    </div>
      </div>

      <div
        class="registry-list"
        :class="{ 'registry-list--cards': viewMode === 'cards' }"
        role="list"
      >
    <article v-for="tool in pagedTools" :key="tool.id" class="registry-row" role="listitem">
      <span class="library-item__mark">{{ tool.name.slice(0, 2).toLowerCase() }}</span>
      <div>
        <strong>{{ tool.name }}</strong>
        <p>{{ tool.description }}</p>
      </div>
      <span class="registry-status">● {{ tool.status }}</span>
      <code>
        {{ tool.isDraftOnly ? tool.version : `v${tool.version} · ${tool.versionCount} 版` }}
      </code>
      <button
        class="button button--ghost"
        type="button"
        :aria-expanded="selectedToolId === tool.id"
        :aria-controls="selectedToolId === tool.id ? 'tool-version-inspector' : undefined"
        @click="openToolInspector($event, tool.id)"
      >
        {{ selectedToolId === tool.id ? '查看中' : '查看版本' }}
      </button>
    </article>
    <div v-if="registryLoaded && tools.length === 0" class="empty-state registry-empty">
      <strong>{{ searchQuery ? '没有匹配的工具' : '工具库为空' }}</strong>
      <p v-if="searchQuery">当前筛选词为“{{ searchQuery }}”，清除后可查看全部已发布工具。</p>
      <p v-else>可以导入 ToolSpec，或发布已有工具草稿。</p>
      <button
        v-if="searchQuery"
        class="button button--ghost"
        type="button"
        @click="searchQuery = ''"
      >
        清除搜索
      </button>
    </div>
      </div>

      <nav v-if="tools.length > pageSize" class="registry-pagination" aria-label="工具库分页">
    <button
      type="button"
      :disabled="currentPage === 1"
      aria-label="上一页"
      @click="currentPage -= 1"
    >
      上一页
    </button>
    <template v-for="(item, index) in paginationItems" :key="`${item}-${index}`">
      <span v-if="item === 'ellipsis'" aria-hidden="true">…</span>
      <button
        v-else
        type="button"
        :class="{ 'is-active': currentPage === item }"
        :aria-current="currentPage === item ? 'page' : undefined"
        :aria-label="`第 ${item} 页`"
        @click="currentPage = item"
      >
        {{ item }}
      </button>
    </template>
    <button
      type="button"
      :disabled="currentPage === pageCount"
      aria-label="下一页"
      @click="currentPage += 1"
    >
      下一页
    </button>
      </nav>
    </section>

    <button
      v-if="selectedToolId"
      class="tool-version-backdrop"
      type="button"
      aria-label="关闭版本详情"
      @click="closeToolInspector"
    />
    <aside
      v-if="selectedToolId"
      id="tool-version-inspector"
      ref="inspectorPanel"
      class="tool-version-panel"
      aria-label="工具版本检查器"
      tabindex="-1"
      @keydown.esc="closeToolInspector"
    >
      <header>
        <div>
          <span>工具检查器</span>
          <h2>{{ selectedToolId }}</h2>
        </div>
        <div class="tool-version-panel__header-actions">
          <strong>{{ selectedToolVersions.length }} 个版本</strong>
          <button type="button" @click="closeToolInspector">关闭</button>
        </div>
      </header>

      <div class="tool-version-panel__body">
        <section class="tool-version-rail" aria-label="草稿与已发布版本">
          <header>
            <strong>草稿与版本</strong>
            <small>选择后右侧立即查看</small>
          </header>
          <div v-if="toolDraft || selectedToolVersions.length" class="tool-version-list">
            <button
              v-if="toolDraft"
              type="button"
              class="tool-version-list__draft"
              :class="{ 'is-active': inspectorView === 'draft' }"
              @click="selectDraft"
            >
              <span>
                <strong>待发布草稿</strong>
                <small>{{ toolDraftValidationStatus === 'valid' ? '校验通过' : '待修正' }}</small>
              </span>
              <code>v{{ toolDraft.tool_version ?? '未指定版本' }}</code>
            </button>
            <button
              v-for="version in selectedToolVersions"
              :key="version.version"
              type="button"
              :class="{ 'is-active': inspectorView === 'version' && selectedToolVersion === version.version }"
              @click="selectPublishedVersion(version.tool_id, version.version)"
            >
              <span><strong>v{{ version.version }}</strong><small>{{ new Date(version.created_at).toLocaleDateString('zh-CN') }}</small></span>
              <code>{{ version.digest.slice(0, 18) }}…</code>
            </button>
          </div>
          <p v-else class="empty-state tool-version-panel__empty">尚无草稿或已发布版本</p>
        </section>

        <div class="tool-version-detail">
          <section
            v-if="inspectorView === 'draft' && toolDraft"
            class="tool-draft-workspace tool-draft-workspace--active"
          >
            <header>
              <div>
                <strong>待发布工具草稿</strong>
                <span>校验、审查并发布后形成新的固定版本。</span>
              </div>
              <small>{{ toolDraftValidationStatus === 'valid' ? '校验通过' : '需要修正' }}</small>
            </header>
            <ToolDraftEditor
              v-model:draft="toolDraft"
              :state="toolDraftState"
              :validation-status="toolDraftValidationStatus"
              :operation-error="toolOperationError"
              @dirty="emit('draftDirty')"
              @save="emit('draftSave')"
              @publish="emit('draftPublish')"
            />
          </section>
          <section v-else-if="selectedToolSpec" class="tool-version-snapshot">
            <header>
              <div>
                <strong>v{{ selectedToolVersion }} 固定内容</strong>
                <span>接口、容器和命令随版本锁定</span>
              </div>
              <button class="button button--ghost" type="button" @click="emit('useVersionAsDraft')">基于此版本修改</button>
            </header>
            <nav class="tool-version-tabs" aria-label="版本内容">
              <button type="button" :class="{ 'is-active': versionDetailMode === 'details' }" @click="versionDetailMode = 'details'">版本内容</button>
              <button type="button" :class="{ 'is-active': versionDetailMode === 'test' }" @click="versionDetailMode = 'test'">独立测试</button>
            </nav>
            <ToolTestPanel
              v-if="versionDetailMode === 'test'"
              :tool-id="selectedToolId"
              :version="selectedToolVersion"
              :tool-spec="selectedToolSpec"
            />
            <template v-else>
            <dl>
              <div><dt>容器</dt><dd><code>{{ selectedToolSpec.container?.image }}</code></dd></div>
              <div><dt>运行资源</dt><dd>{{ selectedToolSpec.runtime?.cpu ?? '—' }} CPU · {{ selectedToolSpec.runtime?.memory_gb ?? '—' }} GB 内存</dd></div>
              <div v-if="selectedToolSource">
                <dt>来源</dt>
                <dd>
                  <NuxtLink v-if="selectedToolSourceLink" :to="selectedToolSourceLink">
                    {{ selectedToolSourceLabel }}
                  </NuxtLink>
                  <span v-else>{{ selectedToolSourceLabel || 'WDL 导入' }}</span>
                </dd>
              </div>
            </dl>

            <section v-if="selectedToolSoftwareLinks?.length" class="tool-software-links">
              <header><strong>关联软件</strong></header>
              <ul>
                <li v-for="link in selectedToolSoftwareLinks" :key="link.id">
                  <span><strong>{{ link.software.name }}</strong><small>{{ link.role }}</small></span>
                  <code>{{ link.release ? `v${link.release.version}` : '未指定版本' }}</code>
                </li>
              </ul>
            </section>

            <div class="tool-version-interface">
              <section>
                <header><strong>输入</strong><span>{{ selectedToolSpec.inputs?.length ?? 0 }}</span></header>
                <ul v-if="selectedToolSpec.inputs?.length">
                  <li v-for="input in selectedToolSpec.inputs.slice(0, 8)" :key="input.name">
                    <code>{{ input.name }}</code><small>{{ input.wdl_type }}</small>
                  </li>
                </ul>
                <small v-else>无输入</small>
                <small v-if="(selectedToolSpec.inputs?.length ?? 0) > 8">还有 {{ selectedToolSpec.inputs.length - 8 }} 项</small>
              </section>
              <section>
                <header><strong>输出</strong><span>{{ selectedToolSpec.outputs?.length ?? 0 }}</span></header>
                <ul v-if="selectedToolSpec.outputs?.length">
                  <li v-for="output in selectedToolSpec.outputs.slice(0, 8)" :key="output.name">
                    <code>{{ output.name }}</code><small>{{ output.wdl_type }}</small>
                  </li>
                </ul>
                <small v-else>无输出</small>
                <small v-if="(selectedToolSpec.outputs?.length ?? 0) > 8">还有 {{ selectedToolSpec.outputs.length - 8 }} 项</small>
              </section>
            </div>

            <section v-if="selectedToolSpec.command?.template" class="tool-command-preview">
              <header><strong>命令模板</strong></header>
              <pre>{{ selectedToolSpec.command.template }}</pre>
            </section>
            </template>
          </section>
          <p v-else-if="inspectorView === 'version' && selectedToolVersions.length" class="empty-state tool-version-detail__empty">正在读取版本内容…</p>
          <p v-else class="empty-state tool-version-detail__empty">这个工具还没有可查看的草稿或已发布版本。</p>
        </div>
      </div>
    </aside>
  </div>
  <SoftwareLibraryPanel v-if="libraryMode === 'software'" />
</template>
