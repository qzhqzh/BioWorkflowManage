<script setup lang="ts">
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
  <main class="no-access-page">
    <section class="no-access-panel" aria-labelledby="no-access-title">
      <div class="login-brand">
        <span class="brand__mark" aria-hidden="true"><i /><i /><i /></span>
        <strong>BioWorkflowManage</strong>
      </div>
      <h1 id="no-access-title">暂无可访问的功能</h1>
      <p>当前账号尚未分配工作台权限，请联系管理员。</p>
      <button class="button button--primary" type="button" @click="logout">退出登录</button>
    </section>
  </main>
</template>

<style scoped>
.no-access-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: var(--color-canvas);
  padding: var(--space-6);
}

.no-access-panel {
  width: min(100%, 420px);
  display: grid;
  gap: var(--space-4);
  border-top: 3px solid var(--color-primary);
  background: var(--color-surface);
  box-shadow: var(--shadow-lg);
  padding: var(--space-6);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text);
}

.no-access-panel h1 {
  margin: var(--space-2) 0 0;
  font-size: 24px;
}

.no-access-panel p {
  margin: 0;
  color: var(--color-muted);
}

.no-access-panel .button {
  min-height: 40px;
}
</style>
