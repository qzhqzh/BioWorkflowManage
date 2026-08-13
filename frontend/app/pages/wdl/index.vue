<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { WdlAsset, WdlTag } from '~/types/wdl'

const { $api: $fetch } = useNuxtApp()

const assets = ref<WdlAsset[]>([])
const { navigateSection } = useAppNavigation()
const availableTags = ref<WdlTag[]>([])
const searchQuery = ref('')
const selectedTags = ref<string[]>([])
const maintenanceFilter = ref<'all' | 'attention' | 'ready'>('all')
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const showImport = ref(false)
const fileInput = ref<HTMLInputElement>()
const selectedImportFile = ref<File>()
const entrypointCandidates = ref<string[]>([])
const importState = ref<'idle' | 'saving' | 'error'>('idle')
const importError = ref('')
const tagPoolState = ref<'idle' | 'saving' | 'error'>('idle')
const tagPoolError = ref('')
const editingTagId = ref<number>()
const tagNameDraft = ref('')
let tagClickTimer: ReturnType<typeof setTimeout> | undefined
const importDraft = ref({
  name: '',
  description: '',
  filename: '',
  content: '',
  tags: '',
  note: '',
  entrypoint: '',
  sourceRepository: '',
  sourceRevision: '',
})
const visibleAssets = computed(() => assets.value.filter((asset) => {
  if (maintenanceFilter.value === 'attention') return maintenanceStatus(asset) !== 'ready'
  if (maintenanceFilter.value === 'ready') return maintenanceStatus(asset) === 'ready'
  return true
}))

const attentionCount = computed(() => assets.value.filter(
  asset => maintenanceStatus(asset) !== 'ready',
).length)

function maintenanceStatus(asset: WdlAsset) {
  return asset.maintenance_status
    ?? (asset.current_revision?.analysis.status === 'valid' ? 'ready' : 'error')
}

function maintenanceLabel(asset: WdlAsset) {
  if (maintenanceStatus(asset) === 'ready') return '检查通过'
  if (asset.maintenance_counts?.errors) return `${asset.maintenance_counts.errors} 个错误`
  if (asset.maintenance_counts?.warnings) return `${asset.maintenance_counts.warnings} 个提醒`
  return '需处理'
}

function activityLabel(action: string) {
  return ({
    import: '导入源码',
    edit: '修改源码',
    format: '格式化',
    metadata_update: '修改信息',
    package_link: '更新工具包引用',
    tool_import: '提取工具',
  } as Record<string, string>)[action] ?? action
}

function formatActivityTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function loadAssets() {
  loadState.value = 'loading'
  try {
    const response = await $fetch<{ results: WdlAsset[] }>('/api/v1/wdl-assets', {
      query: {
        q: searchQuery.value.trim() || undefined,
        tag: selectedTags.value.length ? selectedTags.value : undefined,
      },
    })
    assets.value = response.results
    loadState.value = 'ready'
  } catch {
    loadState.value = 'error'
  }
}

async function loadTags() {
  try {
    const response = await $fetch<{ results: WdlTag[] }>('/api/v1/wdl-assets/tags')
    availableTags.value = response.results
  } catch {
    availableTags.value = []
  }
}

function focusTagRenameInput() {
  void nextTick(() => {
    const input = document.querySelector<HTMLInputElement>('.wdl-tag-filter__input')
    input?.focus()
    input?.select()
  })
}

function beginTagRename(tag: WdlTag) {
  if (tagClickTimer) window.clearTimeout(tagClickTimer)
  editingTagId.value = tag.id
  tagNameDraft.value = tag.name
  tagPoolError.value = ''
  focusTagRenameInput()
}

function cancelTagRename() {
  editingTagId.value = undefined
  tagNameDraft.value = ''
}

function handleTagClick(tag: WdlTag, event: MouseEvent) {
  if (event.detail !== 1) return
  if (tagClickTimer) window.clearTimeout(tagClickTimer)
  tagClickTimer = window.setTimeout(() => {
    toggleTag(tag.name)
    tagClickTimer = undefined
  }, 220)
}

async function commitTagRename(tag: WdlTag) {
  if (editingTagId.value !== tag.id) return
  const name = tagNameDraft.value.trim()
  if (!name || name === tag.name) {
    cancelTagRename()
    return
  }
  tagPoolState.value = 'saving'
  tagPoolError.value = ''
  try {
    const updated = await $fetch<WdlTag>(
      `/api/v1/wdl-assets/tags/${tag.id}`,
      {
        method: 'PATCH',
        body: { name },
      },
    )
    availableTags.value = availableTags.value
      .map(item => item.id === tag.id ? updated : item)
      .sort((left, right) =>
        right.asset_count - left.asset_count || left.name.localeCompare(right.name, 'zh-CN'),
      )
    selectedTags.value = selectedTags.value.map(
      item => item === tag.name ? updated.name : item,
    )
    cancelTagRename()
    tagPoolState.value = 'idle'
    await loadAssets()
  } catch (error: any) {
    tagPoolState.value = 'error'
    tagPoolError.value = error?.data?.error?.code === 'WDL_TAG_CONFLICT'
      ? '标签名称已存在。'
      : '标签重命名失败，请重试。'
  }
}

async function deleteUnusedTag(tag: WdlTag) {
  if (tag.asset_count !== 0 || tagPoolState.value === 'saving') return
  tagPoolState.value = 'saving'
  tagPoolError.value = ''
  try {
    await $fetch(`/api/v1/wdl-assets/tags/${tag.id}`, { method: 'DELETE' })
    availableTags.value = availableTags.value.filter(item => item.id !== tag.id)
    selectedTags.value = selectedTags.value.filter(item => item !== tag.name)
    tagPoolState.value = 'idle'
    await loadAssets()
  } catch (error: any) {
    tagPoolState.value = 'error'
    tagPoolError.value = error?.data?.error?.code === 'WDL_TAG_IN_USE'
      ? '该标签正在使用，不能删除。'
      : '标签删除失败，请重试。'
  }
}

function toggleTag(tag: string) {
  selectedTags.value = selectedTags.value.includes(tag)
    ? selectedTags.value.filter(item => item !== tag)
    : [...selectedTags.value, tag]
  void loadAssets()
}

async function selectImportFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  selectedImportFile.value = file
  entrypointCandidates.value = []
  importDraft.value.filename = file.name
  importDraft.value.name = importDraft.value.name || file.name.replace(/\.(?:wdl|zip)$/i, '')
  importDraft.value.content = file.name.toLowerCase().endsWith('.zip') ? '' : await file.text()
  importError.value = ''
}

function addImportTag(tag: string) {
  const tags = importDraft.value.tags
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
  if (!tags.includes(tag)) importDraft.value.tags = [...tags, tag].join(', ')
}

function resetImport() {
  showImport.value = false
  importState.value = 'idle'
  importError.value = ''
  importDraft.value = {
    name: '',
    description: '',
    filename: '',
    content: '',
    tags: '',
    note: '',
    entrypoint: '',
    sourceRepository: '',
    sourceRevision: '',
  }
  selectedImportFile.value = undefined
  entrypointCandidates.value = []
  if (fileInput.value) fileInput.value.value = ''
}

async function importAsset() {
  if (!importDraft.value.name.trim() || !selectedImportFile.value) {
    importState.value = 'error'
    importError.value = '请选择 WDL 文件并填写资产名称。'
    return
  }
  importState.value = 'saving'
  importError.value = ''
  try {
    const body = new FormData()
    body.append('archive', selectedImportFile.value)
    body.append('name', importDraft.value.name.trim())
    body.append('description', importDraft.value.description.trim())
    body.append('entrypoint', importDraft.value.entrypoint)
    body.append('tags', JSON.stringify(importDraft.value.tags
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)))
    body.append('note', importDraft.value.note.trim())
    body.append('source_repository', importDraft.value.sourceRepository.trim())
    body.append('source_revision', importDraft.value.sourceRevision.trim())
    const asset = await $fetch<WdlAsset>('/api/v1/wdl-assets', {
      method: 'POST',
      body,
    })
    await navigateTo(`/wdl/${encodeURIComponent(asset.slug)}`)
  } catch (error: any) {
    importState.value = 'error'
    entrypointCandidates.value = error?.data?.error?.details?.candidates ?? []
    importError.value = error?.data?.error?.message ?? 'WDL 导入失败，请检查文件后重试。'
  }
}

onMounted(() => {
  void Promise.all([loadAssets(), loadTags()])
})

onBeforeUnmount(() => {
  if (tagClickTimer) window.clearTimeout(tagClickTimer)
})
</script>

<template>
  <div class="app-shell app-shell--workspace">
    <AppTopbar section="WDL 工作台" current="资产台账">
      <template #status>
        <span class="save-state" :class="{ 'save-state--error': loadState === 'error' }">
          <span class="status-dot" />
          {{ loadState === 'loading' ? '正在读取…' : loadState === 'error' ? '读取失败' : `${assets.length} 个历史资产` }}
        </span>
      </template>
      <template #actions>
        <button class="button button--primary" type="button" @click="showImport = !showImport">
          {{ showImport ? '收起导入' : '导入 WDL' }}
        </button>
      </template>
    </AppTopbar>

    <AppRail active="wdl" @select="navigateSection" />

    <main class="section-workspace wdl-assets-page">
      <header class="workspace-header">
        <div>
          <h1>历史 WDL 资产</h1>
          <p>保留原始源码、标签、结构分析和每次修改记录；确认后再转换到标准工具库或流程库。</p>
        </div>
      </header>

      <form v-if="showImport" class="wdl-import-panel" @submit.prevent="importAsset">
        <div class="wdl-import-panel__intro">
          <strong>导入历史 WDL</strong>
          <p>原始内容会保存为不可变 v1。即使语法暂时有问题，也可以先纳入台账后修复。</p>
          <input
            ref="fileInput"
            class="visually-hidden"
            type="file"
            accept=".wdl,.zip,text/plain,application/zip"
            @change="selectImportFile"
          />
          <button class="button button--ghost" type="button" @click="fileInput?.click()">
            {{ importDraft.filename || '选择 .wdl 或 .zip' }}
          </button>
          <small v-if="importDraft.content">
            {{ importDraft.content.split('\n').length }} 行 · {{ importDraft.content.length.toLocaleString('zh-CN') }} 字符
          </small>
          <small v-else-if="selectedImportFile">{{ (selectedImportFile.size / 1024).toFixed(1) }} KB</small>
        </div>
        <div class="wdl-import-fields">
          <label class="field">
            <span>资产名称</span>
            <input v-model="importDraft.name" required placeholder="例如 实体瘤 WES hg38" />
          </label>
          <label class="field">
            <span>标签（逗号分隔）</span>
            <input v-model="importDraft.tags" placeholder="实体瘤, hg38" />
          </label>
          <div v-if="availableTags.length" class="tag-suggestions" aria-label="已有标签">
            <button
              v-for="tag in availableTags.slice(0, 12)"
              :key="tag.name"
              type="button"
              @click="addImportTag(tag.name)"
            >
              + {{ tag.name }}
            </button>
          </div>
          <label class="field field--wide">
            <span>说明</span>
            <input v-model="importDraft.description" placeholder="该流程的用途、来源或维护范围" />
          </label>
          <label v-if="entrypointCandidates.length" class="field field--wide">
            <span>入口 WDL</span>
            <select v-model="importDraft.entrypoint" required>
              <option value="" disabled>选择工作流入口</option>
              <option v-for="path in entrypointCandidates" :key="path" :value="path">{{ path }}</option>
            </select>
          </label>
          <label class="field field--wide">
            <span>来源仓库</span>
            <input v-model="importDraft.sourceRepository" placeholder="可选，例如 easygene/tumor_wdl" />
          </label>
          <label class="field">
            <span>来源版本</span>
            <input v-model="importDraft.sourceRevision" placeholder="可选，Git commit 或 tag" />
          </label>
          <label class="field field--wide">
            <span>导入备注</span>
            <input v-model="importDraft.note" placeholder="例如：从生产 Cromwell 仓库迁移" />
          </label>
          <p v-if="importError" class="inline-error" role="alert">{{ importError }}</p>
          <div class="wdl-import-actions">
            <button class="button button--ghost" type="button" @click="resetImport">取消</button>
            <button class="button button--primary" type="submit" :disabled="importState === 'saving'">
              {{ importState === 'saving' ? '正在导入…' : '导入并分析' }}
            </button>
          </div>
        </div>
      </form>

      <form class="wdl-assets-toolbar" role="search" @submit.prevent="loadAssets">
        <label class="search-field workspace-search">
          <span aria-hidden="true">⌕</span>
          <span class="visually-hidden">搜索历史 WDL</span>
          <input v-model="searchQuery" type="search" placeholder="搜索名称、文件或说明" />
        </label>
        <button class="button button--ghost" type="submit">查询</button>
        <div class="wdl-maintenance-filter" aria-label="维护状态筛选">
          <button type="button" :aria-pressed="maintenanceFilter === 'all'" @click="maintenanceFilter = 'all'">全部</button>
          <button type="button" :aria-pressed="maintenanceFilter === 'attention'" @click="maintenanceFilter = 'attention'">待处理 {{ attentionCount }}</button>
          <button type="button" :aria-pressed="maintenanceFilter === 'ready'" @click="maintenanceFilter = 'ready'">已通过</button>
        </div>
        <span>{{ visibleAssets.length }} 个结果</span>
      </form>

      <div v-if="availableTags.length" class="wdl-tag-filter" aria-label="按标签筛选">
        <div
          v-for="tag in availableTags"
          :key="tag.id"
          class="wdl-tag-filter__item"
          :class="{
            'wdl-tag-filter__active': selectedTags.includes(tag.name),
            'wdl-tag-filter__editing': editingTagId === tag.id,
          }"
        >
          <template v-if="editingTagId === tag.id">
            <input
              v-model="tagNameDraft"
              class="wdl-tag-filter__input"
              :aria-label="`重命名标签 ${tag.name}`"
              maxlength="64"
              @blur="commitTagRename(tag)"
              @keydown.enter.prevent="($event.currentTarget as HTMLInputElement).blur()"
              @keydown.esc.prevent="cancelTagRename"
            />
            <span class="wdl-tag-filter__count">{{ tag.asset_count }}</span>
          </template>
          <template v-else>
            <button
              class="wdl-tag-filter__name"
              type="button"
              :title="`双击重命名标签 ${tag.name}`"
              :aria-pressed="selectedTags.includes(tag.name)"
              @click="handleTagClick(tag, $event)"
              @dblclick.prevent="beginTagRename(tag)"
              @keydown.enter.prevent="toggleTag(tag.name)"
            >
              {{ tag.name }} <span class="wdl-tag-filter__count">{{ tag.asset_count }}</span>
            </button>
            <button
              v-if="tag.asset_count === 0"
              class="wdl-tag-filter__delete"
              type="button"
              :aria-label="`删除未使用标签 ${tag.name}`"
              :disabled="tagPoolState === 'saving'"
              @click="deleteUnusedTag(tag)"
            >
              ×
            </button>
          </template>
        </div>
        <p v-if="tagPoolError" class="wdl-tag-filter__error" role="alert">
          {{ tagPoolError }}
        </p>
      </div>

      <div v-if="loadState === 'error'" class="empty-state wdl-assets-empty" role="alert">
        <strong>历史 WDL 暂时无法读取</strong>
        <p>后端服务或数据库不可用，请恢复后重试。</p>
        <button class="button button--ghost" type="button" @click="loadAssets">重新加载</button>
      </div>

      <div v-else-if="loadState === 'loading'" class="wdl-assets-loading" aria-label="正在加载历史 WDL">
        <span v-for="index in 5" :key="index" />
      </div>

      <div v-else-if="visibleAssets.length" class="wdl-assets-table-wrap">
        <table class="wdl-assets-table">
          <colgroup>
            <col class="wdl-assets-col--asset" />
            <col class="wdl-assets-col--tags" />
            <col class="wdl-assets-col--structure" />
            <col class="wdl-assets-col--status" />
            <col class="wdl-assets-col--activity" />
            <col class="wdl-assets-col--version" />
            <col class="wdl-assets-col--action" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">资产</th>
              <th scope="col">标签</th>
              <th scope="col">结构</th>
              <th scope="col">检查</th>
              <th scope="col">最近协作</th>
              <th scope="col">版本</th>
              <th scope="col"><span class="visually-hidden">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="asset in visibleAssets" :key="asset.slug">
              <td>
                <NuxtLink :to="`/wdl/${asset.slug}`">{{ asset.name }}</NuxtLink>
                <small>{{ asset.source_filename }} · {{ asset.slug }}</small>
              </td>
              <td>
                <div class="tag-list tag-list--compact">
                  <span v-for="tag in asset.tags" :key="tag">{{ tag }}</span>
                  <small v-if="asset.tags.length === 0">未分类</small>
                </div>
              </td>
              <td>
                <span class="structure-summary">
                  {{ asset.current_revision?.analysis.summary.task_count ?? 0 }} task
                  · {{ asset.current_revision?.analysis.summary.workflow_count ?? 0 }} workflow
                  <template v-if="asset.file_count > 1"> · {{ asset.file_count }} 文件</template>
                </span>
              </td>
              <td>
                <span
                  class="analysis-status"
                  :class="`analysis-status--${maintenanceStatus(asset)}`"
                >
                  {{ maintenanceLabel(asset) }}
                </span>
              </td>
              <td class="wdl-latest-activity">
                <template v-if="asset.latest_activity">
                  <strong>{{ asset.latest_activity.actor }} · {{ activityLabel(asset.latest_activity.action) }}</strong>
                  <small>{{ asset.latest_activity.note || `WDL v${asset.latest_activity.revision ?? asset.current_revision?.version ?? '—'}` }}</small>
                  <time :datetime="asset.latest_activity.created_at">{{ formatActivityTime(asset.latest_activity.created_at) }}</time>
                </template>
                <small v-else>暂无记录</small>
              </td>
              <td>
                <strong>v{{ asset.current_revision?.version ?? '—' }}</strong>
                <small>{{ new Date(asset.updated_at).toLocaleString('zh-CN') }}</small>
              </td>
              <td>
                <NuxtLink class="button button--ghost button-link" :to="`/wdl/${asset.slug}`">打开工作台</NuxtLink>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-else class="empty-state wdl-assets-empty">
        <strong>{{ searchQuery || selectedTags.length || maintenanceFilter !== 'all' ? '没有匹配的 WDL' : '还没有历史 WDL' }}</strong>
        <p>{{ searchQuery || selectedTags.length || maintenanceFilter !== 'all' ? '调整搜索词、标签或维护状态。' : '从一个正在使用的 WDL 开始导入，系统会保留原始 v1。' }}</p>
        <button
          v-if="!searchQuery && selectedTags.length === 0"
          class="button button--primary"
          type="button"
          @click="showImport = true"
        >
          导入第一个 WDL
        </button>
      </div>
    </main>
  </div>
</template>
