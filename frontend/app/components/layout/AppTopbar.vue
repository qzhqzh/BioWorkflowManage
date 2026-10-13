<script setup lang="ts">
defineProps<{
  section: string
  current: string
}>()

const { $api } = useNuxtApp()
const auth = useAuth()

async function logout() {
  try {
    await $api('/api/v1/auth/logout', { method: 'POST' })
  } finally {
    auth.clear()
    await navigateTo('/login')
  }
}
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
      <ClientOnly>
        <NuxtLink v-if="auth.user.value?.is_admin" class="topbar__operations" to="/operations">运维控制台</NuxtLink>
        <slot name="actions" />
      </ClientOnly>
      <ClientOnly>
        <span v-if="auth.user.value" class="topbar__user">{{ auth.user.value.username }}</span>
        <button v-if="auth.user.value" class="topbar__logout" type="button" @click="logout">退出</button>
      </ClientOnly>
    </div>
  </header>
</template>

<style scoped>
.topbar__operations {
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  border-radius: 5px;
  padding: 5px 9px;
  font-size: var(--text-caption);
  text-decoration: none;
  white-space: nowrap;
}

.topbar__user {
  color: var(--color-muted);
  font-size: var(--text-secondary);
}

.topbar__logout {
  border: 0;
  background: transparent;
  color: var(--color-muted);
  cursor: pointer;
  padding: var(--space-1);
}

.topbar__logout:hover {
  color: var(--color-text);
}
</style>
