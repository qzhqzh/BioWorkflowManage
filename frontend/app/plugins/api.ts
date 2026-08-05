function cookieValue(name: string) {
  if (!import.meta.client) return ''
  const prefix = `${name}=`
  return document.cookie
    .split(';')
    .map(item => item.trim())
    .find(item => item.startsWith(prefix))
    ?.slice(prefix.length) ?? ''
}

export default defineNuxtPlugin(() => {
  const config = useRuntimeConfig()
  const auth = useAuth()
  const requestHeaders = import.meta.server ? useRequestHeaders(['cookie']) : {}
  const api = $fetch.create({
    baseURL: import.meta.server ? config.apiBase : config.public.apiBase,
    credentials: 'include',
    onRequest({ options }) {
      const method = String(options.method ?? 'GET').toUpperCase()
      options.headers = new Headers(options.headers)
      if (requestHeaders.cookie) options.headers.set('cookie', requestHeaders.cookie)
      if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
        const token = cookieValue('csrftoken')
        if (token) options.headers.set('X-CSRFToken', decodeURIComponent(token))
      }
    },
    onResponseError({ response }) {
      if (response.status === 401) auth.clear()
    },
  })

  return { provide: { api } }
})
