<script setup lang="ts">
import type { AppSection } from '~/composables/useAuth'

defineProps<{
  active: AppSection
}>()

const emit = defineEmits<{
  select: [section: AppSection]
}>()

const auth = useAuth()

const primaryItems = [
  { id: 'edit', glyph: '⌘', label: '编辑器' },
  { id: 'tools', glyph: 'T', label: '工具库' },
  { id: 'packages', glyph: 'P', label: 'WDL 工具包' },
  { id: 'artifacts', glyph: '{ }', label: '流程库' },
  { id: 'runs', glyph: '▶', label: '运行分析' },
  { id: 'wdl', glyph: 'W', label: 'WDL 工作台' },
] as const

const visiblePrimaryItems = computed(() => primaryItems.filter(
  item => auth.user.value?.allowed_sections.includes(item.id),
))
const showHelp = computed(() => auth.user.value?.allowed_sections.includes('help'))
</script>

<template>
  <aside class="rail" aria-label="主要导航">
    <button
      v-for="item in visiblePrimaryItems"
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
      v-if="showHelp"
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
