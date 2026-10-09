<script setup lang="ts">
import { runStatusLabels } from '~/utils/operations'
defineProps<{ status: string }>()
</script>
<template>
  <span class="ops-status" :class="`ops-status--${status}`"><i />{{ runStatusLabels[status] || status }}</span>
</template>
