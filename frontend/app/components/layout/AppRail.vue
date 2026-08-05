<script setup lang="ts">
defineProps<{
  active: 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help'
}>()

const emit = defineEmits<{
  select: [section: 'edit' | 'tools' | 'packages' | 'artifacts' | 'runs' | 'wdl' | 'help']
}>()

const primaryItems = [
  { id: 'edit', glyph: '⌘', label: '编辑器' },
  { id: 'tools', glyph: 'T', label: '工具库' },
  { id: 'packages', glyph: 'P', label: 'WDL 工具包' },
  { id: 'artifacts', glyph: '{ }', label: '流程库' },
  { id: 'runs', glyph: '▶', label: '运行分析' },
  { id: 'wdl', glyph: 'W', label: 'WDL 工作台' },
] as const
</script>

<template>
  <aside class="rail" aria-label="主要导航">
    <button
      v-for="item in primaryItems"
      :key="item.id"
      class="rail__item"
      :class="{ 'rail__item--active': active === item.id }"
      type="button"
      :aria-current="active === item.id ? 'page' : undefined"
      @click="emit('select', item.id)"
    >
      <span class="rail__glyph" aria-hidden="true">{{ item.glyph }}</span>
      <span>{{ item.label }}</span>
    </button>
    <button
      class="rail__item rail__item--bottom"
      :class="{ 'rail__item--active': active === 'help' }"
      type="button"
      :aria-current="active === 'help' ? 'page' : undefined"
      @click="emit('select', 'help')"
    >
      <span class="rail__glyph" aria-hidden="true">?</span>
      <span>帮助</span>
    </button>
  </aside>
</template>
