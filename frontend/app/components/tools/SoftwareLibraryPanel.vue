<script setup lang="ts">
interface SoftwareRelease {
  id: number
  version: string
  description: string
  container_images: string[]
  metadata: Record<string, unknown>
  metadata_version: number
  created_at: string
}

interface ToolLink {
  id: number
  role: 'primary' | 'dependency' | 'runtime'
  note: string
  tool: { id: string; version: string; name: string; digest: string }
  release?: { id: number; version: string }
}

interface SoftwareAsset {
  slug: string
  name: string
  summary: string
  description: string
  homepage: string
  source_repository: string
  license: string
  notes: string
  tags: string[]
  metadata: Record<string, unknown>
  lifecycle: 'active' | 'archived'
  metadata_version: number
  release_count: number
  tool_count: number
  updated_by: string
  updated_at: string
  releases?: SoftwareRelease[]
  tool_links?: ToolLink[]
  audit_events?: Array<{ id: number; action: string; actor: string; created_at: string }>
}

interface RegistryTool {
  tool_id: string
  name: string
  latest_version?: string
}

const items = ref<SoftwareAsset[]>([])
const selected = ref<SoftwareAsset>()
const searchQuery = ref('')
const loading = ref(true)
const errorMessage = ref('')
const createOpen = ref(false)
const createState = ref<'idle' | 'saving'>('idle')
const editState = ref<'idle' | 'saving'>('idle')
const releaseState = ref<'idle' | 'saving'>('idle')
const linkState = ref<'idle' | 'saving'>('idle')
const createForm = reactive({ slug: '', name: '', summary: '' })
const editForm = reactive({
  name: '',
  summary: '',
  description: '',
  homepage: '',
  source_repository: '',
  license: '',
  notes: '',
  tags: '',
  metadata: '{}',
  lifecycle: 'active' as 'active' | 'archived',
})
const releaseForm = reactive({ version: '', description: '', container_images: '', metadata: '{}' })
const linkForm = reactive({ tool_id: '', tool_version: '', software_version: '', role: 'primary', note: '' })
const registryTools = ref<RegistryTool[]>([])
const toolVersions = ref<Array<{ version: string; name: string }>>([])

const filteredItems = computed(() => {
  const query = searchQuery.value.trim().toLocaleLowerCase()
  if (!query) return items.value
  return items.value.filter(item => `${item.name} ${item.slug} ${item.tags.join(' ')}`.toLocaleLowerCase().includes(query))
})

function apiError(error: any, fallback: string) {
  return error?.data?.error?.message ?? error?.response?._data?.error?.message ?? fallback
}

function parseMetadata(value: string) {
  const parsed = JSON.parse(value || '{}')
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('扩展信息必须是 JSON object。')
  return parsed
}

function hydrateEdit(item: SoftwareAsset) {
  Object.assign(editForm, {
    name: item.name,
    summary: item.summary,
    description: item.description,
    homepage: item.homepage,
    source_repository: item.source_repository,
    license: item.license,
    notes: item.notes,
    tags: item.tags.join(', '),
    metadata: JSON.stringify(item.metadata ?? {}, null, 2),
    lifecycle: item.lifecycle,
  })
}

async function loadItems() {
  loading.value = true
  try {
    const response = await $fetch<{ results: SoftwareAsset[] }>('/api/v1/software')
    items.value = response.results
    if (selected.value) {
      const exists = items.value.find(item => item.slug === selected.value?.slug)
      if (!exists) selected.value = undefined
    }
  }
  catch (error) {
    errorMessage.value = apiError(error, '软件知识库读取失败。')
  }
  finally {
    loading.value = false
  }
}

async function loadRegistryTools() {
  try {
    const response = await $fetch<{ results: RegistryTool[] }>('/api/v1/tools')
    registryTools.value = response.results.filter(item => item.latest_version)
  }
  catch {
    registryTools.value = []
  }
}

async function selectSoftware(slug: string) {
  errorMessage.value = ''
  try {
    const detail = await $fetch<SoftwareAsset>(`/api/v1/software/${encodeURIComponent(slug)}`)
    selected.value = detail
    hydrateEdit(detail)
  }
  catch (error) {
    errorMessage.value = apiError(error, '软件信息读取失败。')
  }
}

async function createSoftware() {
  createState.value = 'saving'
  errorMessage.value = ''
  try {
    const detail = await $fetch<SoftwareAsset>('/api/v1/software', {
      method: 'POST',
      body: createForm,
    })
    createOpen.value = false
    Object.assign(createForm, { slug: '', name: '', summary: '' })
    await loadItems()
    selected.value = detail
    hydrateEdit(detail)
  }
  catch (error) {
    errorMessage.value = apiError(error, '软件记录创建失败。')
  }
  finally {
    createState.value = 'idle'
  }
}

async function saveSoftware() {
  if (!selected.value) return
  editState.value = 'saving'
  errorMessage.value = ''
  try {
    const detail = await $fetch<SoftwareAsset>(`/api/v1/software/${encodeURIComponent(selected.value.slug)}`, {
      method: 'PATCH',
      body: {
        ...editForm,
        tags: editForm.tags.split(',').map(item => item.trim()).filter(Boolean),
        metadata: parseMetadata(editForm.metadata),
        base_metadata_version: selected.value.metadata_version,
      },
    })
    selected.value = detail
    hydrateEdit(detail)
    await loadItems()
  }
  catch (error) {
    errorMessage.value = error instanceof SyntaxError ? '扩展信息不是有效 JSON。' : apiError(error, '软件信息保存失败。')
  }
  finally {
    editState.value = 'idle'
  }
}

async function createRelease() {
  if (!selected.value) return
  releaseState.value = 'saving'
  errorMessage.value = ''
  try {
    await $fetch(`/api/v1/software/${encodeURIComponent(selected.value.slug)}/releases`, {
      method: 'POST',
      body: {
        version: releaseForm.version,
        description: releaseForm.description,
        container_images: releaseForm.container_images.split('\n').map(item => item.trim()).filter(Boolean),
        metadata: parseMetadata(releaseForm.metadata),
      },
    })
    Object.assign(releaseForm, { version: '', description: '', container_images: '', metadata: '{}' })
    await selectSoftware(selected.value.slug)
    await loadItems()
  }
  catch (error) {
    errorMessage.value = error instanceof SyntaxError ? '版本扩展信息不是有效 JSON。' : apiError(error, '软件版本添加失败。')
  }
  finally {
    releaseState.value = 'idle'
  }
}

async function loadToolVersions() {
  linkForm.tool_version = ''
  toolVersions.value = []
  if (!linkForm.tool_id) return
  try {
    const response = await $fetch<{ results: Array<{ version: string; name: string }> }>(
      `/api/v1/tools/${encodeURIComponent(linkForm.tool_id)}/versions`,
    )
    toolVersions.value = response.results
    linkForm.tool_version = response.results[0]?.version ?? ''
  }
  catch {
    toolVersions.value = []
  }
}

async function createLink() {
  if (!selected.value) return
  linkState.value = 'saving'
  errorMessage.value = ''
  try {
    await $fetch(`/api/v1/software/${encodeURIComponent(selected.value.slug)}/tool-links`, {
      method: 'POST',
      body: linkForm,
    })
    Object.assign(linkForm, { tool_id: '', tool_version: '', software_version: '', role: 'primary', note: '' })
    toolVersions.value = []
    await selectSoftware(selected.value.slug)
    await loadItems()
  }
  catch (error) {
    errorMessage.value = apiError(error, '工具关联失败。')
  }
  finally {
    linkState.value = 'idle'
  }
}

async function removeLink(linkId: number) {
  if (!selected.value) return
  errorMessage.value = ''
  try {
    await $fetch(`/api/v1/software/${encodeURIComponent(selected.value.slug)}/tool-links/${linkId}`, { method: 'DELETE' })
    await selectSoftware(selected.value.slug)
    await loadItems()
  }
  catch (error) {
    errorMessage.value = apiError(error, '工具关联移除失败。')
  }
}

onMounted(() => {
  void Promise.all([loadItems(), loadRegistryTools()])
})
</script>

<template>
  <div class="software-library">
    <section class="software-catalog">
      <div class="workspace-toolbar">
        <label class="search-field workspace-search">
          <span aria-hidden="true">⌕</span>
          <input v-model="searchQuery" type="search" placeholder="搜索软件、标签或 ID" />
        </label>
        <div class="workspace-toolbar__summary">
          <span>{{ filteredItems.length }} 个软件</span>
          <button class="button button--primary" type="button" @click="createOpen = !createOpen">{{ createOpen ? '取消' : '新建软件' }}</button>
        </div>
      </div>

      <form v-if="createOpen" class="software-create" @submit.prevent="createSoftware">
        <label class="field"><span>软件 ID</span><input v-model="createForm.slug" required placeholder="例如 samtools" /></label>
        <label class="field"><span>名称</span><input v-model="createForm.name" required /></label>
        <label class="field"><span>摘要</span><input v-model="createForm.summary" /></label>
        <button class="button button--primary" type="submit" :disabled="createState === 'saving'">{{ createState === 'saving' ? '创建中…' : '创建' }}</button>
      </form>

      <div class="software-list" role="list">
        <button
          v-for="item in filteredItems"
          :key="item.slug"
          type="button"
          role="listitem"
          :class="{ 'is-active': selected?.slug === item.slug }"
          @click="selectSoftware(item.slug)"
        >
          <span><strong>{{ item.name }}</strong><small>{{ item.slug }}</small></span>
          <p>{{ item.summary || '暂无摘要' }}</p>
          <span class="software-list__meta">{{ item.release_count }} 个版本 · {{ item.tool_count }} 个工具</span>
          <span v-if="item.tags.length" class="software-tags"><i v-for="tag in item.tags.slice(0, 4)" :key="tag">{{ tag }}</i></span>
        </button>
        <p v-if="!loading && filteredItems.length === 0" class="empty-state">{{ searchQuery ? '没有匹配的软件。' : '软件知识库为空。' }}</p>
      </div>
    </section>

    <aside class="software-detail">
      <p v-if="errorMessage" class="tool-test-error" role="alert">{{ errorMessage }}</p>
      <div v-if="selected" class="software-detail__content">
        <header>
          <div><h2>{{ selected.name }}</h2><code>{{ selected.slug }}</code></div>
          <span>v{{ selected.metadata_version }} · {{ selected.updated_by }}</span>
        </header>

        <form class="software-edit" @submit.prevent="saveSoftware">
          <div class="software-form-grid">
            <label class="field"><span>名称</span><input v-model="editForm.name" required /></label>
            <label class="field"><span>许可证</span><input v-model="editForm.license" /></label>
          </div>
          <label class="field"><span>摘要</span><input v-model="editForm.summary" /></label>
          <label class="field"><span>说明</span><textarea v-model="editForm.description" rows="4" /></label>
          <div class="software-form-grid">
            <label class="field"><span>官网</span><input v-model="editForm.homepage" type="url" /></label>
            <label class="field"><span>源码仓库</span><input v-model="editForm.source_repository" /></label>
          </div>
          <label class="field"><span>标签</span><input v-model="editForm.tags" placeholder="比对, BAM, 临床" /></label>
          <label class="field"><span>注意事项</span><textarea v-model="editForm.notes" rows="4" /></label>
          <details class="software-metadata">
            <summary>扩展信息</summary>
            <label class="field"><span>JSON</span><textarea v-model="editForm.metadata" class="mono-input" rows="5" /></label>
          </details>
          <div class="software-edit__actions">
            <label><span>状态</span><select v-model="editForm.lifecycle"><option value="active">使用中</option><option value="archived">已归档</option></select></label>
            <button class="button button--primary" type="submit" :disabled="editState === 'saving'">{{ editState === 'saving' ? '保存中…' : '保存信息' }}</button>
          </div>
        </form>

        <section class="software-section">
          <header><strong>软件版本</strong><span>{{ selected.releases?.length ?? 0 }}</span></header>
          <div v-if="selected.releases?.length" class="software-release-list">
            <article v-for="release in selected.releases" :key="release.id">
              <div><strong>v{{ release.version }}</strong><p>{{ release.description || '暂无说明' }}</p></div>
              <code v-for="image in release.container_images" :key="image">{{ image }}</code>
            </article>
          </div>
          <form class="software-inline-form" @submit.prevent="createRelease">
            <label class="field"><span>版本</span><input v-model="releaseForm.version" required /></label>
            <label class="field"><span>说明</span><input v-model="releaseForm.description" /></label>
            <label class="field field--wide"><span>容器镜像（每行一个）</span><textarea v-model="releaseForm.container_images" rows="2" /></label>
            <details class="field--wide software-metadata"><summary>版本扩展信息</summary><textarea v-model="releaseForm.metadata" class="mono-input" rows="3" /></details>
            <button class="button button--ghost" type="submit" :disabled="releaseState === 'saving'">{{ releaseState === 'saving' ? '添加中…' : '添加版本' }}</button>
          </form>
        </section>

        <section class="software-section">
          <header><strong>关联工具</strong><span>{{ selected.tool_links?.length ?? 0 }}</span></header>
          <ul v-if="selected.tool_links?.length" class="software-tool-links">
            <li v-for="link in selected.tool_links" :key="link.id">
              <span><strong>{{ link.tool.name }}</strong><code>{{ link.tool.id }}@{{ link.tool.version }}</code></span>
              <small>{{ link.role }}<template v-if="link.release"> · 软件 {{ link.release.version }}</template></small>
              <button type="button" @click="removeLink(link.id)">移除</button>
            </li>
          </ul>
          <form class="software-link-form" @submit.prevent="createLink">
            <label class="field"><span>工具</span><select v-model="linkForm.tool_id" required @change="loadToolVersions"><option value="">请选择</option><option v-for="tool in registryTools" :key="tool.tool_id" :value="tool.tool_id">{{ tool.name }}</option></select></label>
            <label class="field"><span>工具版本</span><select v-model="linkForm.tool_version" required><option v-for="version in toolVersions" :key="version.version" :value="version.version">v{{ version.version }}</option></select></label>
            <label class="field"><span>软件版本</span><select v-model="linkForm.software_version"><option value="">未指定</option><option v-for="release in selected.releases" :key="release.id" :value="release.version">v{{ release.version }}</option></select></label>
            <label class="field"><span>角色</span><select v-model="linkForm.role"><option value="primary">主要软件</option><option value="dependency">依赖</option><option value="runtime">运行环境</option></select></label>
            <button class="button button--ghost" type="submit" :disabled="linkState === 'saving'">{{ linkState === 'saving' ? '关联中…' : '关联工具' }}</button>
          </form>
        </section>

        <section v-if="selected.audit_events?.length" class="software-section">
          <header><strong>最近变更</strong></header>
          <ul class="software-audit-list">
            <li v-for="event in selected.audit_events.slice(0, 12)" :key="event.id"><span>{{ event.action }}</span><strong>{{ event.actor }}</strong><time>{{ new Date(event.created_at).toLocaleString('zh-CN') }}</time></li>
          </ul>
        </section>
      </div>
      <p v-else class="empty-state software-detail__empty">选择一个软件查看知识、版本和关联工具。</p>
    </aside>
  </div>
</template>
