<script setup lang="ts">
defineProps<{
  artifact: {
    name: string
    media_type: string
    content: string
  }
  compilationVersion?: string
}>()

defineEmits<{
  close: []
}>()
</script>

<template>
  <Teleport to="body">
    <div class="drawer-backdrop" @click.self="$emit('close')">
      <aside class="artifact-drawer" role="dialog" aria-modal="true" :aria-label="`预览 ${artifact.name}`">
        <header>
          <div>
            <strong>{{ artifact.name }}</strong>
            <small>{{ artifact.media_type }} · {{ compilationVersion ?? '当前编译' }}</small>
          </div>
          <button type="button" aria-label="关闭预览" @click="$emit('close')">×</button>
        </header>
        <pre><code>{{ artifact.content }}</code></pre>
      </aside>
    </div>
  </Teleport>
</template>
