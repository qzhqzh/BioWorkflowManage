<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { WdlToolPackage, WdlToolPackageTag } from '~/types/wdl'

const { $api: $fetch } = useNuxtApp()

type WorkspaceSection = 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help'

const packages = ref<WdlToolPackage[]>([])
const tags = ref<WdlToolPackageTag[]>([])
const searchQuery = ref('')
const selectedTags = ref<string[]>([])
const lifecycle = ref<'active' | 'archived' | ''>('active')
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const showImport = ref(false)
const importState = ref<'idle' | 'saving' | 'error'>('idle')
const importError = ref('')
const fileInput = ref<HTMLInputElement>()
const selectedFile = ref<File>()
const draft = ref({
  name: '',
  version: '1.0.0',
  description: '',
  tags: '',
  sourceRepository: '',
  sourceRevision: '',
  note: '',
})

function navigateSection(section: WorkspaceSection) {
  if (section === 'packages') return
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
  importError.value = ''
}

function resetImport() {
  showImport.value = false
  importState.value = 'idle'
  importError.value = ''
  selectedFile.value = undefined
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

async function importPackage() {
  if (!selectedFile.value || !draft.value.name.trim() || !draft.value.version.trim()) {
    importState.value = 'error'
    importError.value = '请选择 ZIP，并填写名称和版本。'
    return
  }
  importState.value = 'saving'
  importError.value = ''
  try {
    const body = new FormData()
    body.append('archive', selectedFile.value)
    body.append('name', draft.value.name.trim())
    body.append('version', draft.value.version.trim())
    body.append('description', draft.value.description.trim())
    body.append('tags', JSON.stringify(draft.value.tags.split(',').map(item => item.trim()).filter(Boolean)))
    body.append('source_repository', draft.value.sourceRepository.trim())
    body.append('source_revision', draft.value.sourceRevision.trim())
    body.append('note', draft.value.note.trim())
    const created = await $fetch<WdlToolPackage>('/api/v1/wdl-packages', {
      method: 'POST',
      body,
    })
    await navigateTo(`/wdl-packages/${encodeURIComponent(created.slug)}`)
  } catch (error: any) {
    importState.value = 'error'
    importError.value = error?.data?.error?.message ?? '工具包导入失败。'
  }
}

onMounted(() => {
  void Promise.all([loadPackages(), loadTags()])
})
</script>

<template>
  <div class="app-shell app-shell--workspace">
    <AppTopbar section="工具库" current="WDL 工具包">
      <template #actions>
        <button class="button button--primary" type="button" @click="showImport = !showImport">
          {{ showImport ? '收起' : '导入工具包' }}
        </button>
      </template>
    </AppTopbar>

    <AppRail active="packages" @select="navigateSection" />

    <main class="section-workspace wdl-packages-page">
      <header class="workspace-header">
        <div><h1>WDL 工具包</h1></div>
      </header>

      <form v-if="showImport" class="wdl-import-panel" @submit.prevent="importPackage">
        <div class="wdl-import-panel__intro">
          <strong>导入工具包</strong>
          <input
            ref="fileInput"
            class="visually-hidden"
            type="file"
            accept=".zip,application/zip"
            @change="selectFile"
          />
          <button class="button button--ghost" type="button" @click="fileInput?.click()">
            {{ selectedFile?.name || '选择 ZIP' }}
          </button>
          <small v-if="selectedFile">{{ (selectedFile.size / 1024).toFixed(1) }} KB</small>
        </div>
        <div class="wdl-import-fields">
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
          <p v-if="importError" class="inline-error" role="alert">{{ importError }}</p>
          <div class="wdl-import-actions">
            <button class="button button--ghost" type="button" @click="resetImport">取消</button>
            <button class="button button--primary" type="submit" :disabled="importState === 'saving'">
              {{ importState === 'saving' ? '正在分析…' : '导入并分析' }}
            </button>
          </div>
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
        <span>{{ packages.length }} 个结果</span>
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
      <div v-else-if="packages.length" class="wdl-assets-table-wrap">
        <table class="wdl-assets-table wdl-packages-table">
          <thead>
            <tr>
              <th scope="col">工具包</th>
              <th scope="col">标签</th>
              <th scope="col">内容</th>
              <th scope="col">检查</th>
              <th scope="col">版本</th>
              <th scope="col">引用</th>
              <th scope="col"><span class="visually-hidden">操作</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in packages" :key="item.slug">
              <td>
                <NuxtLink :to="`/wdl-packages/${item.slug}`">{{ item.name }}</NuxtLink>
                <small>{{ item.slug }}</small>
              </td>
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
        <strong>{{ searchQuery || selectedTags.length ? '没有匹配的工具包' : '还没有工具包' }}</strong>
        <button v-if="!searchQuery && !selectedTags.length" class="button button--primary" type="button" @click="showImport = true">
          导入第一个工具包
        </button>
      </div>
    </main>
  </div>
</template>
