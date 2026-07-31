<script setup lang="ts">
type LibraryTab = 'tools' | 'subworkflows' | 'inputs' | 'outputs'

interface LibraryItem {
  id: string
  name: string
  description: string
}

const activeLibrary = defineModel<LibraryTab>('activeLibrary', { required: true })
const searchQuery = defineModel<string>('searchQuery', { required: true })
defineProps<{
  tools: Array<Record<string, any>>
  subworkflows: Array<Record<string, any>>
  workflowInputs: LibraryItem[]
  workflowOutputs: LibraryItem[]
  toolRegistryLoaded: boolean
  isWorkflowSwitching: boolean
}>()

const emit = defineEmits<{
  quickAdd: [payload: any]
  dragStart: [event: DragEvent, payload: any]
  dragEnd: []
  importToolSpec: []
}>()
</script>

<template>
  <aside class="library-panel">
    <div class="panel-tabs" role="tablist" aria-label="节点库">
      <button
        v-for="tab in (['tools', 'subworkflows', 'inputs', 'outputs'] as LibraryTab[])"
        :key="tab"
        type="button"
        role="tab"
        :aria-selected="activeLibrary === tab"
        :class="{ 'panel-tab--active': activeLibrary === tab }"
        @click="activeLibrary = tab"
      >
        {{ { tools: '工具', subworkflows: '子流程', inputs: '输入', outputs: '输出' }[tab] }}
      </button>
    </div>

    <div class="library-panel__body">
      <label class="search-field">
        <span class="visually-hidden">搜索节点</span>
        <span aria-hidden="true">⌕</span>
        <input v-model="searchQuery" type="search" placeholder="搜索工具或类型" />
        <kbd>⌘ K</kbd>
      </label>

      <div class="section-heading">
        <h2>
          {{
            activeLibrary === 'tools' ? '可用工具'
              : activeLibrary === 'subworkflows' ? '已发布子流程'
                : activeLibrary === 'inputs' ? 'Workflow 输入'
                  : 'Workflow 输出'
          }}
        </h2>
        <span>
          {{
            activeLibrary === 'tools' ? tools.length
              : activeLibrary === 'subworkflows' ? subworkflows.length
                : activeLibrary === 'inputs' ? workflowInputs.length
                  : workflowOutputs.length
          }}
        </span>
      </div>
      <p class="library-hint">单击快速添加到画布中心，也可以拖到指定位置。</p>

      <ul v-if="activeLibrary === 'tools'" class="library-list">
        <li v-for="tool in tools" :key="tool.id">
          <button
            type="button"
            class="library-item library-item--draggable"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动 ${tool.name} ${tool.version} 到画布`"
            @click="emit('quickAdd', { kind: 'tool', toolId: tool.id, version: tool.version, digest: tool.digest, label: tool.name })"
            @dragstart="emit('dragStart', $event, { kind: 'tool', toolId: tool.id, version: tool.version, digest: tool.digest, label: tool.name })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">{{ tool.name.slice(0, 2).toLowerCase() }}</span>
            <span class="library-item__content">
              <strong>{{ tool.name }}</strong>
              <small>{{ tool.description }}</small>
            </span>
            <span class="library-item__meta">
              <code>{{ tool.version }}</code>
              <small>{{ tool.status }}</small>
            </span>
          </button>
        </li>
      </ul>

      <ul v-else-if="activeLibrary === 'subworkflows'" class="library-list">
        <li v-for="subflow in subworkflows" :key="`${subflow.slug}@${subflow.version}`">
          <button
            type="button"
            class="library-item library-item--draggable library-item--subworkflow"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动子流程 ${subflow.name} v${subflow.version} 到画布`"
            @click="emit('quickAdd', { kind: 'subworkflow', slug: subflow.slug, version: subflow.version, digest: subflow.semantic_digest, label: subflow.name, description: subflow.description, interfaceContract: subflow.interface_contract })"
            @dragstart="emit('dragStart', $event, { kind: 'subworkflow', slug: subflow.slug, version: subflow.version, digest: subflow.semantic_digest, label: subflow.name, description: subflow.description, interfaceContract: subflow.interface_contract })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">SF</span>
            <span class="library-item__content">
              <strong>{{ subflow.name }}</strong>
              <small>{{ subflow.interface_contract.inputs?.length ?? 0 }} 输入 · {{ subflow.interface_contract.outputs?.length ?? 0 }} 输出</small>
            </span>
            <span class="library-item__meta">
              <code>v{{ subflow.version }}</code>
              <small>固定版本</small>
            </span>
          </button>
        </li>
      </ul>

      <ul v-else-if="activeLibrary === 'inputs'" class="library-list">
        <li v-for="input in workflowInputs" :key="input.id">
          <button
            type="button"
            class="library-item library-item--draggable"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动 ${input.name} 输入到画布`"
            @click="emit('quickAdd', { kind: 'input', wdlType: input.name, label: input.name })"
            @dragstart="emit('dragStart', $event, { kind: 'input', wdlType: input.name, label: input.name })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">{{ input.name.slice(0, 1) }}</span>
            <span class="library-item__content">
              <strong>{{ input.name }}</strong>
              <small>{{ input.description }}</small>
            </span>
          </button>
        </li>
      </ul>

      <ul v-else class="library-list">
        <li v-for="output in workflowOutputs" :key="output.id">
          <button
            type="button"
            class="library-item library-item--draggable"
            :draggable="!isWorkflowSwitching"
            :disabled="isWorkflowSwitching"
            :aria-label="`拖动 ${output.name} 输出到画布`"
            @click="emit('quickAdd', { kind: 'output', wdlType: output.name, label: output.name })"
            @dragstart="emit('dragStart', $event, { kind: 'output', wdlType: output.name, label: output.name })"
            @dragend="emit('dragEnd')"
          >
            <span class="library-item__mark">{{ output.name.slice(0, 1) }}</span>
            <span class="library-item__content">
              <strong>{{ output.name }}</strong>
              <small>{{ output.description }}</small>
            </span>
          </button>
        </li>
      </ul>

      <p v-if="activeLibrary === 'tools' && !toolRegistryLoaded" class="empty-state">
        正在载入已发布工具…
      </p>
      <p v-else-if="activeLibrary === 'tools' && tools.length === 0" class="empty-state">
        没有匹配的工具。可以清空搜索，或稍后导入 ToolSpec。
      </p>
      <p v-else-if="activeLibrary === 'subworkflows' && subworkflows.length === 0" class="empty-state">
        暂无已发布的子流程版本。先在流程库将流程类型设为“子流程”并发布版本。
      </p>
    </div>

    <footer class="library-panel__footer">
      <button type="button" class="text-button" @click="emit('importToolSpec')">导入 ToolSpec JSON</button>
    </footer>
  </aside>
</template>
