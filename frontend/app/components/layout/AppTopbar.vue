<script setup lang="ts">
defineProps<{
  section: string
  current: string
}>()
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <span class="brand__mark" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <strong>BioWorkflowManage</strong>
    </div>

    <nav class="breadcrumb" aria-label="当前位置">
      <span>{{ section }}</span>
      <span aria-hidden="true">/</span>
      <strong>{{ current }}</strong>
    </nav>

    <slot name="status" />
    <div class="topbar__actions">
      <slot name="actions" />
    </div>
  </header>
</template>
