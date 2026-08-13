<script setup lang="ts">
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import type { ResourceCatalogEntry, ResourceCatalogPayload, ResourceRequirement } from '~/types/resources'

const { $api } = useNuxtApp()
const { navigateSection } = useAppNavigation()

type ResourceKind = 'references' | 'panels'
type SaveState = 'idle' | 'saving' | 'saved' | 'error' | 'conflict'

const catalog = ref<ResourceCatalogPayload>()
const draft = ref<ResourceCatalogPayload['document']>()
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const saveState = ref<SaveState>('idle')
const errorMessage = ref('')
const selectedKind = ref<ResourceKind>('panels')
const selectedId = ref('')
const search = ref('')
const note = ref('')
const verifyState = ref<'idle' | 'running' | 'done'>('idle')
const verifiedEntry = ref('')

const currentEntries = computed(() => draft.value?.[selectedKind.value] ?? [])
const currentStatuses = computed(() => catalog.value?.[selectedKind.value] ?? [])
const selectedEntry = computed(() => currentEntries.value.find(item => item.id === selectedId.value))
const selectedStatus = computed(() => currentStatuses.value.find(item => item.id === selectedId.value))
const visibleEntries = computed(() => {
  const query = search.value.trim().toLocaleLowerCase('zh-CN')
  if (!query) return currentEntries.value
  return currentEntries.value.filter(item => (
    `${item.name} ${item.id} ${item.description ?? ''}`.toLocaleLowerCase('zh-CN').includes(query)
  ))
})
const isDirty = computed(() => Boolean(
  catalog.value && draft.value
  && JSON.stringify(catalog.value.document) !== JSON.stringify(draft.value),
))
const canSave = computed(() => Boolean(
  catalog.value && (isDirty.value || catalog.value.source === 'file'),
))
const readyLabel = computed(() => {
  if (isDirty.value) return '待保存检查'
  if (!selectedStatus.value) return ''
  if (selectedStatus.value.ready) return '资源完整'
  return `缺 ${selectedStatus.value.missing?.length ?? 0} 项`
})
const isSelectedVerified = computed(() => (
  verifiedEntry.value === `${selectedKind.value}:${selectedId.value}`
))

function cloneDocument(value: ResourceCatalogPayload['document']) {
  return JSON.parse(JSON.stringify(value)) as ResourceCatalogPayload['document']
}

function ensureSelection() {
  const exists = currentEntries.value.some(item => item.id === selectedId.value)
  if (!exists) selectedId.value = currentEntries.value[0]?.id ?? ''
}

async function loadCatalog() {
  loadState.value = 'loading'
  errorMessage.value = ''
  try {
    catalog.value = await $api<ResourceCatalogPayload>('/api/v1/resource-catalog')
    draft.value = cloneDocument(catalog.value.document)
    ensureSelection()
    loadState.value = 'ready'
  }
  catch (error: any) {
    loadState.value = 'error'
    errorMessage.value = error?.data?.error?.message ?? '资源目录读取失败。'
  }
}

function changeKind(kind: ResourceKind) {
  selectedKind.value = kind
  selectedId.value = draft.value?.[kind][0]?.id ?? ''
  verifyState.value = 'idle'
}

function selectEntry(id: string) {
  selectedId.value = id
  verifyState.value = 'idle'
}

function bindingValue(entry: ResourceCatalogEntry, key: string) {
  const value = entry.bindings?.[key]
  return Array.isArray(value) ? value.join('\n') : String(value ?? '')
}

function setBinding(entry: ResourceCatalogEntry, key: string, value: string) {
  entry.bindings ??= {}
  entry.bindings[key] = value.trim()
}

function requirementState(item: ResourceRequirement) {
  if (item.present) return '已就绪'
  if (item.reason === 'unconfigured') return '未配置路径'
  if (item.reason === 'checksum_mismatch') return '校验值不一致'
  if (item.reason === 'constraint_mismatch') return '文件名不匹配'
  return '文件缺失'
}

function requirementPath(item: ResourceRequirement) {
  return item.path || `绑定字段：${item.binding}`
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function changeCount(revision: ResourceCatalogPayload['revisions'][number]) {
  return Object.values(revision.changes).reduce((total, item) => (
    total + item.created.length + item.updated.length + item.deleted.length
  ), 0)
}

function addRequirement() {
  selectedEntry.value?.required.push({ path: '', label: '', kind: 'file' })
}

function removeRequirement(index: number) {
  selectedEntry.value?.required.splice(index, 1)
}

function addPanel() {
  if (!draft.value) return
  const id = `panel-${Date.now()}`
  draft.value.panels.push({
    id,
    name: '新 Panel',
    description: '',
    reference: draft.value.references[0]?.id ?? '',
    workflow_ids: [],
    bindings: {},
    required_bindings: [
      { key: 'bed', label: '捕获区域 BED', kind: 'file' },
      { key: 'gene_bed', label: '基因区域 BED', kind: 'file' },
    ],
    required: [],
  })
  selectedKind.value = 'panels'
  selectedId.value = id
}

function addReference() {
  if (!draft.value) return
  const id = `reference-${Date.now()}`
  draft.value.references.push({
    id,
    name: '新参考库',
    description: '',
    ref_version: '',
    bindings: {},
    required_bindings: [
      { key: 'fasta', label: '参考基因组 FASTA', kind: 'file' },
      { key: 'fasta_fai', label: 'FASTA 索引', kind: 'file' },
    ],
    required: [],
  })
  selectedKind.value = 'references'
  selectedId.value = id
}

function discardChanges() {
  if (!catalog.value) return
  draft.value = cloneDocument(catalog.value.document)
  ensureSelection()
  saveState.value = 'idle'
  errorMessage.value = ''
}

async function saveCatalog() {
  if (!catalog.value || !draft.value || !canSave.value) return
  saveState.value = 'saving'
  errorMessage.value = ''
  try {
    const saved = await $api<ResourceCatalogPayload>('/api/v1/resource-catalog', {
      method: 'PUT',
      body: {
        document: draft.value,
        base_version: catalog.value.version,
        base_digest: catalog.value.digest,
        note: note.value.trim() || '更新资源配置。',
      },
    })
    catalog.value = saved
    draft.value = cloneDocument(saved.document)
    note.value = ''
    saveState.value = 'saved'
    setTimeout(() => {
      if (saveState.value === 'saved') saveState.value = 'idle'
    }, 1800)
  }
  catch (error: any) {
    const code = error?.data?.error?.code
    saveState.value = code === 'RESOURCE_CATALOG_CONFLICT' ? 'conflict' : 'error'
    errorMessage.value = error?.data?.error?.message ?? '资源目录保存失败。'
  }
}

async function verifySelectedResource() {
  if (!catalog.value || !selectedEntry.value || isDirty.value) return
  verifyState.value = 'running'
  const verifyKey = `${selectedKind.value}:${selectedEntry.value.id}`
  errorMessage.value = ''
  try {
    catalog.value = await $api<ResourceCatalogPayload>('/api/v1/resource-catalog', {
      query: {
        verify_kind: selectedKind.value,
        verify_id: selectedEntry.value.id,
      },
    })
    verifiedEntry.value = verifyKey
    verifyState.value = 'done'
  }
  catch (error: any) {
    verifyState.value = 'idle'
    errorMessage.value = error?.data?.error?.message ?? '完整性校验失败。'
  }
}

onBeforeRouteLeave(() => {
  if (!isDirty.value) return true
  return window.confirm('资源配置还有未保存修改，确认离开？')
})

onMounted(() => void loadCatalog())
</script>

<template>
  <div class="app-shell app-shell--workspace app-shell--resources">
    <!--
    THESIS: 资源中心是一张可检查、可修订的实验准备台，拒绝用统计卡片掩盖缺失文件。
    OWN-WORLD: 沿用白色工作面、淡青分区、细分隔线和深青操作色，状态同时使用文字与符号。
    STORY: 用户先定位 Reference 或 Panel，再补齐语义绑定，逐项核对物理文件并留下修改记录。
    FIRST VIEWPORT: 左侧资源索引，中部配置和缺失清单，右侧修订轨迹；保存始终位于右上方。
    FORM: 三栏资源检查台，ordered candidate 6，seed 6669d935。
    -->
    <AppTopbar section="资源中心" current="分析资源">
      <template #status>
        <span v-if="catalog" class="save-state">
          <span class="status-dot" />
          {{ catalog.source === 'managed' ? `目录 v${catalog.version}` : '尚未纳管' }}
        </span>
      </template>
      <template #actions>
        <button v-if="isDirty" class="button button--ghost" type="button" @click="discardChanges">放弃修改</button>
        <button
          class="button button--primary"
          type="button"
          :disabled="!canSave || saveState === 'saving'"
          @click="saveCatalog"
        >
          {{ saveState === 'saving' ? '正在保存…' : saveState === 'saved' ? '已保存' : '保存资源目录' }}
        </button>
      </template>
    </AppTopbar>

    <AppRail active="resources" @select="navigateSection" />

    <main class="section-workspace resource-catalog-page">
      <header class="workspace-header resource-catalog-header">
        <div>
          <h1>分析资源</h1>
          <p>统一维护参考基因组、Panel、BED 和 CNV 基线；路径均相对数据库根目录。</p>
        </div>
        <div v-if="catalog" class="resource-catalog-summary" aria-label="资源状态">
          <span><b>{{ catalog.summary.ready_reference_count }}</b>/{{ catalog.summary.reference_count }} 参考库就绪</span>
          <span><b>{{ catalog.summary.ready_panel_count }}</b>/{{ catalog.summary.panel_count }} Panel 就绪</span>
          <span :class="{ 'has-missing': catalog.summary.missing_count }"><b>{{ catalog.summary.missing_count }}</b> 项待补</span>
        </div>
      </header>

      <div v-if="loadState === 'loading'" class="analysis-page-state">正在读取资源目录…</div>
      <div v-else-if="loadState === 'error'" class="analysis-page-state analysis-page-state--error" role="alert">
        <strong>{{ errorMessage }}</strong>
        <button class="button button--ghost" type="button" @click="loadCatalog">重试</button>
      </div>
      <div v-else-if="draft && catalog" class="resource-catalog-layout">
        <aside class="resource-index" aria-label="资源索引">
          <div class="resource-index-tabs">
            <button type="button" :class="{ active: selectedKind === 'references' }" @click="changeKind('references')">
              参考库 <span>{{ draft.references.length }}</span>
            </button>
            <button type="button" :class="{ active: selectedKind === 'panels' }" @click="changeKind('panels')">
              Panel <span>{{ draft.panels.length }}</span>
            </button>
          </div>
          <label class="search-field resource-search">
            <span aria-hidden="true">⌕</span>
            <input v-model="search" type="search" placeholder="搜索名称或 ID" />
          </label>
          <div class="resource-index-list" role="listbox">
            <button
              v-for="entry in visibleEntries"
              :key="entry.id"
              type="button"
              role="option"
              :aria-selected="entry.id === selectedId"
              :class="{ active: entry.id === selectedId }"
              @click="selectEntry(entry.id)"
            >
              <span>
                <strong>{{ entry.name }}</strong>
                <small>{{ entry.id }}</small>
              </span>
              <i :class="{ ready: currentStatuses.find(item => item.id === entry.id)?.ready }">
                {{ currentStatuses.find(item => item.id === entry.id)?.ready ? '就绪' : `缺 ${currentStatuses.find(item => item.id === entry.id)?.missing?.length ?? 0}` }}
              </i>
            </button>
            <p v-if="!visibleEntries.length" class="empty-state">没有匹配的资源。</p>
          </div>
          <button
            class="button button--ghost resource-add"
            type="button"
            @click="selectedKind === 'panels' ? addPanel() : addReference()"
          >
            {{ selectedKind === 'panels' ? '新建 Panel' : '新建参考库' }}
          </button>
        </aside>

        <section v-if="selectedEntry" class="resource-editor" aria-label="资源配置">
          <header class="resource-editor-header">
            <div>
              <input v-model="selectedEntry.name" class="resource-title-input" aria-label="资源名称" />
              <code>{{ selectedEntry.id }}</code>
            </div>
            <span class="resource-ready-state" :class="{ ready: !isDirty && selectedStatus?.ready, pending: isDirty }">{{ readyLabel }}</span>
          </header>

          <div class="resource-editor-fields">
            <label class="field field--wide">
              <span>说明</span>
              <textarea v-model="selectedEntry.description" rows="2" placeholder="用途、适用项目及维护注意事项" />
            </label>
            <label v-if="selectedKind === 'panels'" class="field">
              <span>参考版本</span>
              <select v-model="selectedEntry.reference">
                <option v-for="reference in draft.references" :key="reference.id" :value="reference.id">{{ reference.name }}</option>
              </select>
            </label>
            <label v-if="selectedKind === 'panels'" class="field">
              <span>资源版本</span>
              <input v-model="selectedEntry.resource_version" placeholder="例如：2026.08" />
            </label>
            <label v-else class="field">
              <span>WDL ref_version</span>
              <input v-model="selectedEntry.ref_version" />
            </label>
          </div>

          <section v-if="selectedEntry.required_bindings?.length" class="resource-editor-section">
            <header>
              <div><h2>流程绑定</h2><p>配置 WDL 语义输入对应的实际资源。</p></div>
            </header>
            <div class="resource-binding-grid">
              <label v-for="binding in selectedEntry.required_bindings" :key="binding.key" class="field">
                <span>{{ binding.label }}</span>
                <input
                  :value="bindingValue(selectedEntry, binding.key)"
                  :placeholder="`${binding.kind === 'directory' ? '目录' : '文件'}相对路径`"
                  @change="setBinding(selectedEntry, binding.key, ($event.target as HTMLInputElement).value)"
                />
                <small>
                  <code>{{ binding.key }}</code>
                  <span v-if="binding.basename_includes?.length">
                    · 文件名须包含 {{ binding.basename_includes.join(' / ') }}
                  </span>
                </small>
              </label>
            </div>
          </section>

          <section class="resource-editor-section resource-validation">
            <header>
              <div>
                <h2>资源检查</h2>
                <p>{{ isSelectedVerified ? '已完成所选资源的 SHA-256 完整校验。' : '日常检查路径与类型；SHA-256 由完整校验读取。' }}</p>
              </div>
              <div class="resource-validation-actions">
                <span>{{ selectedStatus?.requirements?.length ?? 0 }} 项</span>
                <button
                  class="button button--ghost"
                  type="button"
                  :disabled="isDirty || verifyState === 'running'"
                  @click="verifySelectedResource"
                >{{ verifyState === 'running' ? '正在校验…' : '完整校验' }}</button>
              </div>
            </header>
            <ul v-if="selectedStatus?.requirements?.length" class="resource-requirement-list">
              <li v-for="item in selectedStatus.requirements" :key="`${item.binding}-${item.path}-${item.label}`" :class="{ ready: item.present }">
                <span class="resource-check-mark" aria-hidden="true">{{ item.present ? '✓' : '!' }}</span>
                <span><strong>{{ item.label }}</strong><code>{{ requirementPath(item) }}</code></span>
                <em>{{ requirementState(item) }}</em>
              </li>
            </ul>
            <p v-else class="empty-state">尚未声明需要检查的资源。</p>
          </section>

          <section class="resource-editor-section resource-extra-files">
            <header>
              <div><h2>附加文件</h2><p>记录该资源运行时还必须存在的数据库文件。</p></div>
              <button class="text-button" type="button" @click="addRequirement">添加</button>
            </header>
            <div v-if="selectedEntry.required.length" class="resource-file-editor">
              <div v-for="(item, index) in selectedEntry.required" :key="index">
                <input v-model="item.label" aria-label="资源标签" placeholder="名称" />
                <input v-model="item.path" aria-label="资源路径" placeholder="相对路径" />
                <input v-model="item.sha256" aria-label="SHA-256" placeholder="SHA-256（可选）" />
                <select v-model="item.kind" aria-label="资源类型"><option value="file">文件</option><option value="directory">目录</option></select>
                <button type="button" aria-label="删除资源" @click="removeRequirement(index)">删除</button>
              </div>
            </div>
            <p v-else class="empty-state">没有附加文件。</p>
          </section>

          <p v-if="errorMessage" class="inline-error" role="alert">{{ errorMessage }}</p>
          <button
            v-if="saveState === 'conflict'"
            class="button button--ghost resource-reload"
            type="button"
            @click="loadCatalog"
          >刷新并查看最新目录</button>
          <label class="field resource-save-note">
            <span>本次修改备注</span>
            <input v-model="note" placeholder="例如：补充 84 Panel BED 路径" />
          </label>
        </section>

        <aside class="resource-history" aria-label="资源目录修订记录">
          <header><h2>修订记录</h2><span>{{ catalog.revisions.length }}</span></header>
          <ol v-if="catalog.revisions.length">
            <li v-for="revision in catalog.revisions" :key="revision.version">
              <span>v{{ revision.version }}</span>
              <div><strong>{{ revision.actor }}</strong><p>{{ revision.note || '更新资源目录。' }}</p><small>{{ formatTime(revision.created_at) }} · {{ changeCount(revision) }} 项变更</small></div>
            </li>
          </ol>
          <div v-else class="resource-history-empty">
            <strong>尚未纳管</strong>
            <p>第一次保存会把当前 catalog.json 固化为 v1。</p>
          </div>
        </aside>
      </div>

      <footer class="analysis-runs-footer" aria-label="版本信息">v1.0 · 稳定版</footer>
    </main>
  </div>
</template>
