<script setup lang="ts">
import type { WdlSourceFile } from '~/types/wdl'

const props = defineProps<{
  files: WdlSourceFile[]
  activePath: string
  dirtyPaths?: string[]
}>()

const emit = defineEmits<{ select: [path: string] }>()
const collapsed = ref(new Set<string>())

type TreeRow = { kind: 'folder' | 'file'; path: string; name: string; depth: number; file?: WdlSourceFile }

const rows = computed<TreeRow[]>(() => {
  const result: TreeRow[] = []
  const emittedFolders = new Set<string>()
  for (const file of [...props.files].sort((left, right) => left.path.localeCompare(right.path))) {
    const parts = file.path.split('/')
    let hidden = false
    for (let index = 0; index < parts.length - 1; index += 1) {
      const path = parts.slice(0, index + 1).join('/')
      if (!emittedFolders.has(path) && !hidden) {
        result.push({ kind: 'folder', path, name: parts[index]!, depth: index })
        emittedFolders.add(path)
      }
      if (collapsed.value.has(path)) hidden = true
    }
    if (!hidden) {
      result.push({ kind: 'file', path: file.path, name: parts.at(-1)!, depth: parts.length - 1, file })
    }
  }
  return result
})

function toggle(path: string) {
  const next = new Set(collapsed.value)
  if (next.has(path)) next.delete(path)
  else next.add(path)
  collapsed.value = next
}
</script>

<template>
  <nav class="wdl-file-tree" aria-label="WDL 文件">
    <button
      v-for="row in rows"
      :key="`${row.kind}:${row.path}`"
      class="wdl-file-tree__row"
      :class="{ 'is-active': row.kind === 'file' && row.path === activePath }"
      :style="{ paddingInlineStart: `${10 + row.depth * 14}px` }"
      type="button"
      @click="row.kind === 'folder' ? toggle(row.path) : emit('select', row.path)"
    >
      <span class="wdl-file-tree__icon" aria-hidden="true">
        {{ row.kind === 'folder' ? (collapsed.has(row.path) ? '▸' : '▾') : '·' }}
      </span>
      <span class="wdl-file-tree__name">{{ row.name }}</span>
      <span v-if="row.file?.origin === 'package'" class="wdl-file-tree__package">包</span>
      <span v-if="row.file?.is_entry" class="wdl-file-tree__entry">入口</span>
      <span v-if="dirtyPaths?.includes(row.path)" class="wdl-file-tree__dirty" aria-label="未保存">●</span>
    </button>
  </nav>
</template>

<style scoped>
.wdl-file-tree {
  min-width: 0;
  padding: var(--space-1) 0;
}

.wdl-file-tree__row {
  width: 100%;
  min-height: 30px;
  display: flex;
  align-items: center;
  gap: var(--space-1);
  border: 0;
  background: transparent;
  color: var(--color-muted);
  cursor: pointer;
  padding-block: 4px;
  padding-inline-end: var(--space-2);
  text-align: left;
}

.wdl-file-tree__row:hover,
.wdl-file-tree__row.is-active {
  background: color-mix(in srgb, var(--color-primary) 8%, transparent);
  color: var(--color-text);
}

.wdl-file-tree__row.is-active {
  box-shadow: inset 2px 0 var(--color-primary);
}

.wdl-file-tree__icon {
  width: 12px;
  flex: 0 0 12px;
  color: var(--color-muted);
}

.wdl-file-tree__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 12px;
}

.wdl-file-tree__entry {
  margin-left: auto;
  color: var(--color-primary);
  font-size: 10px;
}

.wdl-file-tree__package {
  margin-left: auto;
  color: var(--color-muted);
  font-size: 10px;
}

.wdl-file-tree__package + .wdl-file-tree__entry {
  margin-left: 0;
}

.wdl-file-tree__dirty {
  margin-left: auto;
  color: var(--color-warning);
  font-size: 8px;
}
</style>
