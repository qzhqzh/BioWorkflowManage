// Nuxt evaluates this config in Node; only the environment surface is needed here.
declare const process: { env: Record<string, string | undefined> }

export default defineNuxtConfig({
  compatibilityDate: '2026-07-23',
  css: ['~/assets/css/main.css'],
  devtools: { enabled: false },
  runtimeConfig: {
    apiBase: 'http://backend:8000',
    public: {
      apiBase: '',
      operationsBusinessLinks: '[]',
    },
  },
  nitro: {
    preset: 'node-server',
    devProxy: {
      '/api': {
        target: process.env.NUXT_DEV_API_PROXY || 'http://127.0.0.1:8082/api',
        // Preserve the browser origin/host pair so Django keeps enforcing CSRF.
        changeOrigin: false,
      },
    },
  },
  app: {
    head: {
      htmlAttrs: { lang: 'zh-CN' },
      title: 'BioWorkflowManage',
      meta: [
        {
          name: 'description',
          content: '可视化定义、校验并编译生物信息学 Workflow。',
        },
        {
          name: 'viewport',
          content: 'width=device-width, initial-scale=1, viewport-fit=cover',
        },
      ],
    },
  },
  typescript: {
    strict: true,
    typeCheck: false,
  },
})
