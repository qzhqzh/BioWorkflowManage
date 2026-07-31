<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position, type NodeProps } from '@vue-flow/core'

type NodeKind = 'input' | 'tool' | 'subworkflow' | 'output'

interface PortSummary {
  name: string
  type: string
}

interface WorkflowNodeData {
  kind: NodeKind
  label: string
  identifier: string
  semanticType?: string
  version?: string
  inputs?: PortSummary[]
  outputs?: PortSummary[]
  layoutDirection?: 'horizontal' | 'vertical'
}

const props = defineProps<NodeProps>()
const node = computed(() => props.data as unknown as WorkflowNodeData)
const isVertical = computed(() => node.value.layoutDirection === 'vertical')
</script>

<template>
  <article
    class="flow-node"
    :class="[`flow-node--${node.kind}`, { 'flow-node--selected': selected }]"
  >
    <Handle
      v-if="node.kind === 'output'"
      id="in:value"
      type="target"
      :position="isVertical ? Position.Top : Position.Left"
      class="flow-handle"
    />

    <header class="flow-node__header">
      <span class="flow-node__kind">
        {{ node.kind === 'input' ? 'INPUT' : node.kind === 'tool' ? 'TOOL' : node.kind === 'subworkflow' ? 'SUBFLOW' : 'OUTPUT' }}
      </span>
      <span v-if="node.version" class="flow-node__version">v{{ node.version }}</span>
    </header>

    <div class="flow-node__body">
      <strong>{{ node.label }}</strong>
      <code>{{ node.identifier }}</code>
    </div>

    <dl v-if="node.kind === 'tool' || node.kind === 'subworkflow'" class="flow-node__ports">
      <div class="flow-node__port-column flow-node__port-column--inputs">
        <div v-for="port in node.inputs" :key="`input-${port.name}`" class="flow-node__port-row">
          <Handle
            :id="`in:${port.name}`"
            type="target"
            :position="Position.Left"
            class="flow-handle flow-handle--input"
          />
          <dt :title="port.name">{{ port.name }}</dt>
          <dd>{{ port.type }}</dd>
        </div>
      </div>
      <div class="flow-node__port-column flow-node__port-column--outputs">
        <div v-for="port in node.outputs" :key="`output-${port.name}`" class="flow-node__port-row">
          <dd>{{ port.type }}</dd>
          <dt :title="port.name">{{ port.name }}</dt>
          <Handle
            :id="`out:${port.name}`"
            type="source"
            :position="Position.Right"
            class="flow-handle flow-handle--output"
          />
        </div>
      </div>
    </dl>

    <footer v-else class="flow-node__footer">
      <span>{{ node.semanticType }}</span>
      <Handle
        v-if="node.kind === 'input'"
        id="out:value"
        type="source"
        :position="isVertical ? Position.Bottom : Position.Right"
        class="flow-handle"
      />
    </footer>
  </article>
</template>
