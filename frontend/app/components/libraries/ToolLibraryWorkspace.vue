<script setup lang="ts">
import ToolDraftEditor from '~/components/tools/ToolDraftEditor.vue'

interface RegistryTool {
  id: string
  name: string
  description: string
  version: string
  status: string
  isDraftOnly: boolean
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

defineProps<{
  tools: RegistryTool[]
  registryLoaded: boolean
  selectedToolId: string
  selectedToolVersions: ToolVersion[]
  toolDraftState: 'idle' | 'saving' | 'saved' | 'publishing' | 'published' | 'error'
  toolDraftValidationStatus?: string
  toolOperationError?: ToolOperationError
  toolCreateState: 'idle' | 'saving' | 'error'
}>()

const creatingTool = defineModel<boolean>('creatingTool', { required: true })
const newToolId = defineModel<string>('newToolId', { required: true })
const searchQuery = defineModel<string>('searchQuery', { required: true })
const toolDraft = defineModel<Record<string, any> | undefined>('toolDraft')

const emit = defineEmits<{
  create: []
  import: []
  selectTool: [toolId: string]
  selectVersion: [toolId: string, version: string]
  draftDirty: []
  draftSave: []
  draftPublish: []
}>()
</script>

<template>
  <header class="workspace-header">
    <div>
      <h1>工具库</h1>
      <p>管理可复用的 ToolSpec。每个版本固定工具接口、容器与命令模板。</p>
    </div>
    <div class="workspace-header__actions">
      <button class="button button--ghost" type="button" @click="creatingTool = !creatingTool">
        {{ creatingTool ? '取消新建' : '新建工具' }}
      </button>
      <button class="button button--primary" type="button" @click="emit('import')">导入 ToolSpec</button>
    </div>
  </header>

  <form v-if="creatingTool" class="tool-create-panel" @submit.prevent="emit('create')">
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

  <div class="workspace-toolbar">
    <label class="search-field workspace-search">
      <span aria-hidden="true">⌕</span>
      <input v-model="searchQuery" type="search" placeholder="搜索工具名称或版本" />
    </label>
    <span>{{ tools.length }} 个工具</span>
  </div>

  <div class="registry-list" role="list">
    <article v-for="tool in tools" :key="tool.id" class="registry-row" role="listitem">
      <span class="library-item__mark">{{ tool.name.slice(0, 2).toLowerCase() }}</span>
      <div>
        <strong>{{ tool.name }}</strong>
        <p>{{ tool.description }}</p>
      </div>
      <span class="registry-status">● {{ tool.status }}</span>
      <code>{{ tool.isDraftOnly ? tool.version : `v${tool.version}` }}</code>
      <button class="button button--ghost" type="button" @click="emit('selectTool', tool.id)">
        {{ selectedToolId === tool.id ? '已展开' : '查看版本' }}
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

  <section v-if="selectedToolId" class="tool-version-panel">
    <header>
      <div>
        <span>工具详情与版本</span>
        <h2>{{ selectedToolId }}</h2>
      </div>
      <strong>{{ selectedToolVersions.length }} 个版本</strong>
    </header>
    <div class="tool-detail-layout">
      <div v-if="selectedToolVersions.length" class="tool-version-list">
        <article v-for="version in selectedToolVersions" :key="version.version">
          <div>
            <button type="button" @click="emit('selectVersion', version.tool_id, version.version)">
              v{{ version.version }}
            </button>
            <small>{{ new Date(version.created_at).toLocaleString('zh-CN') }}</small>
          </div>
          <code>{{ version.digest }}</code>
          <span>不可变快照</span>
        </article>
      </div>
      <p v-else class="empty-state">该工具还没有已发布版本。</p>

      <ToolDraftEditor
        v-if="toolDraft"
        v-model:draft="toolDraft"
        :state="toolDraftState"
        :validation-status="toolDraftValidationStatus"
        :operation-error="toolOperationError"
        @dirty="emit('draftDirty')"
        @save="emit('draftSave')"
        @publish="emit('draftPublish')"
      />
    </div>
  </section>
</template>
