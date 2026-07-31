<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { WdlAsset, WdlTag } from '~/types/wdl'

type WorkspaceSection = 'edit' | 'tools' | 'artifacts' | 'wdl' | 'help'

const assets = ref<WdlAsset[]>([])
const availableTags = ref<WdlTag[]>([])
const searchQuery = ref('')
const selectedTags = ref<string[]>([])
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const showImport = ref(false)
const fileInput = ref<HTMLInputElement>()
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
})

function navigateSection(section: WorkspaceSection) {
  if (section === 'wdl') return
  void navigateTo(`/?section=${section}`)
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
  importDraft.value.filename = file.name
  importDraft.value.name = importDraft.value.name || file.name.replace(/\.wdl$/i, '')
  importDraft.value.content = await file.text()
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
  }
  if (fileInput.value) fileInput.value.value = ''
}

async function importAsset() {
  if (!importDraft.value.name.trim() || !importDraft.value.content.trim()) {
    importState.value = 'error'
    importError.value = '请选择 WDL 文件并填写资产名称。'
    return
  }
  importState.value = 'saving'
  importError.value = ''
  try {
    const asset = await $fetch<WdlAsset>('/api/v1/wdl-assets', {
      method: 'POST',
      body: {
        name: importDraft.value.name.trim(),
        description: importDraft.value.description.trim(),
        filename: importDraft.value.filename,
        content: importDraft.value.content,
        tags: importDraft.value.tags
          .split(',')
          .map(item => item.trim())
          .filter(Boolean),
        note: importDraft.value.note.trim(),
      },
    })
    await navigateTo(`/wdl/${encodeURIComponent(asset.slug)}`)
  } catch (error: any) {
    importState.value = 'error'
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
        <span class="save-state">
          <span class="status-dot" />
          {{ loadState === 'loading' ? '正在读取…' : `${assets.length} 个历史资产` }}
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
            accept=".wdl,text/plain"
            @change="selectImportFile"
          />
          <button class="button button--ghost" type="button" @click="fileInput?.click()">
            {{ importDraft.filename || '选择 .wdl 文件' }}
          </button>
          <small v-if="importDraft.content">
            {{ importDraft.content.split('\n').length }} 行 · {{ importDraft.content.length.toLocaleString('zh-CN') }} 字符
          </small>
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
          <input v-model="searchQuery" type="search" placeholder="搜索名称、文件或说明" />
        </label>
        <button class="button button--ghost" type="submit">查询</button>
        <span>{{ assets.length }} 个结果</span>
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

      <div v-else-if="assets.length" class="wdl-assets-table-wrap">
        <table class="wdl-assets-table">
          <thead>
            <tr>
              <th scope="col">资产</th>
              <th scope="col">标签</th>
              <th scope="col">结构</th>
              <th scope="col">检查</th>
              <th scope="col">版本</th>
              <th scope="col"><span class="visually-hidden">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="asset in assets" :key="asset.slug">
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
                </span>
              </td>
              <td>
                <span
                  class="analysis-status"
                  :class="`analysis-status--${asset.current_revision?.analysis.status ?? 'invalid'}`"
                >
                  {{ asset.current_revision?.analysis.status === 'valid' ? '通过' : '需处理' }}
                </span>
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
        <strong>{{ searchQuery || selectedTags.length ? '没有匹配的 WDL' : '还没有历史 WDL' }}</strong>
        <p>{{ searchQuery || selectedTags.length ? '调整搜索词或标签筛选。' : '从一个正在使用的 WDL 开始导入，系统会保留原始 v1。' }}</p>
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
