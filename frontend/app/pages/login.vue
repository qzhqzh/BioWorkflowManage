<script setup lang="ts">
const { $api } = useNuxtApp()
const auth = useAuth()
const username = ref('')
const password = ref('')
const state = ref<'idle' | 'loading' | 'error'>('idle')

async function login() {
  if (!username.value.trim() || !password.value) return
  state.value = 'loading'
  try {
    await $api('/api/v1/auth/csrf')
    const response = await $api<{ user: import('~/composables/useAuth').AuthUser }>(
      '/api/v1/auth/login',
      { method: 'POST', body: { username: username.value.trim(), password: password.value } },
    )
    auth.user.value = response.user
    auth.ready.value = true
    await navigateTo(defaultRouteForUser(response.user))
  } catch {
    state.value = 'error'
  }
}
</script>

<template>
  <main class="login-page">
    <form class="login-panel" @submit.prevent="login">
      <div class="login-brand">
        <span class="brand__mark" aria-hidden="true"><i /><i /><i /></span>
        <strong>BioWorkflowManage</strong>
      </div>
      <h1>登录</h1>
      <label>
        <span>用户名</span>
        <input v-model="username" autocomplete="username" autofocus />
      </label>
      <label>
        <span>密码</span>
        <input v-model="password" type="password" autocomplete="current-password" />
      </label>
      <p v-if="state === 'error'" class="login-error" role="alert">用户名或密码不正确。</p>
      <button class="button button--primary" type="submit" :disabled="state === 'loading'">
        {{ state === 'loading' ? '登录中…' : '登录' }}
      </button>
    </form>
  </main>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: var(--color-canvas);
  padding: var(--space-6);
}

.login-panel {
  width: min(100%, 360px);
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

.login-panel h1 {
  margin: var(--space-2) 0 0;
  font-size: 24px;
}

.login-panel label {
  display: grid;
  gap: var(--space-2);
  color: var(--color-muted);
  font-size: var(--text-secondary);
}

.login-panel input {
  width: 100%;
}

.login-error {
  margin: 0;
  color: var(--color-error);
  font-size: var(--text-secondary);
}

.login-panel .button {
  min-height: 40px;
}
</style>
