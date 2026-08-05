export default defineNuxtConfig({
  compatibilityDate: '2026-07-23',
  css: ['~/assets/css/main.css'],
  devtools: { enabled: false },
  runtimeConfig: {
    apiBase: 'http://backend:8000',
    public: {
      apiBase: '',
    },
  },
  nitro: {
    preset: 'node-server',
    devProxy: {
      '/api': {
        target: 'http://127.0.0.1:8082/api',
        changeOrigin: true,
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
