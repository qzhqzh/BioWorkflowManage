export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) return
  const nuxtApp = useNuxtApp()
  const { $api } = nuxtApp
  const auth = useAuth()
  if (!auth.ready.value) {
    try {
      const response = await $api<{ user: import('~/composables/useAuth').AuthUser }>('/api/v1/auth/me')
      auth.user.value = response.user
    } catch {
      auth.user.value = null
    } finally {
      auth.ready.value = true
    }
  }
  if (to.path === '/login') {
    if (auth.user.value) {
      return navigateTo(defaultRouteForUser(auth.user.value), { external: nuxtApp.isHydrating })
    }
    return
  }
  if (!auth.user.value) return navigateTo('/login', { external: nuxtApp.isHydrating })
  const section = routeSection(to.path, to.query.section)
  if (!auth.user.value.allowed_sections.includes(section)) {
    return navigateTo(defaultRouteForUser(auth.user.value), { external: nuxtApp.isHydrating })
  }
})
