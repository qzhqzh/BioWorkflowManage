<script setup lang="ts">
import type {
  WdlSourcePackageReference,
  WdlToolPackage,
  WdlToolPackageVersion,
} from '~/types/wdl'

defineProps<{
  references: WdlSourcePackageReference[]
  readOnly?: boolean
}>()

const emit = defineEmits<{
  add: [payload: {
    reference: WdlSourcePackageReference
    version: WdlToolPackageVersion
    selectedPaths: string[]
  }]
}>()

const { $api: $fetch } = useNuxtApp()
const packages = ref<WdlToolPackage[]>([])
const packageDetail = ref<WdlToolPackage>()
const versionDetail = ref<WdlToolPackageVersion>()
const packageSlug = ref('')
const version = ref('')
const mountPrefix = ref('')
const selectedPaths = ref<string[]>([])
const expanded = ref(false)
const loading = ref(false)
const errorMessage = ref('')

async function loadPackages() {
  try {
    const response = await $fetch<{ results: WdlToolPackage[] }>(
      '/api/v1/wdl-packages?lifecycle=active',
    )
    packages.value = response.results
  } catch {
    errorMessage.value = '工具包读取失败。'
  }
}

async function selectPackage() {
  packageDetail.value = undefined
  versionDetail.value = undefined
  selectedPaths.value = []
  errorMessage.value = ''
  if (!packageSlug.value) return
  loading.value = true
  try {
    packageDetail.value = await $fetch<WdlToolPackage>(
      `/api/v1/wdl-packages/${encodeURIComponent(packageSlug.value)}`,
    )
    version.value = packageDetail.value.latest_version?.version ?? ''
    mountPrefix.value = version.value
      ? `packages/${packageSlug.value}/${version.value}`
      : `packages/${packageSlug.value}`
    await loadVersion()
  } catch {
    errorMessage.value = '工具包详情读取失败。'
  } finally {
    loading.value = false
  }
}

async function loadVersion() {
  versionDetail.value = undefined
  selectedPaths.value = []
  errorMessage.value = ''
  if (!packageSlug.value || !version.value) return
  loading.value = true
  try {
    versionDetail.value = await $fetch<WdlToolPackageVersion>(
      `/api/v1/wdl-packages/${encodeURIComponent(packageSlug.value)}/versions/${encodeURIComponent(version.value)}`,
    )
    mountPrefix.value = `packages/${packageSlug.value}/${version.value}`
    selectedPaths.value = versionDetail.value.files.map(file => file.path)
  } catch {
    errorMessage.value = '工具包版本读取失败。'
  } finally {
    loading.value = false
  }
}

function mountedPath(path: string) {
  const prefix = mountPrefix.value.trim().replace(/^\/+|\/+$/g, '')
  return prefix ? `${prefix}/${path}` : path
}

function addReference() {
  if (!packageDetail.value || !versionDetail.value || !mountPrefix.value.trim()) return
  const reference: WdlSourcePackageReference = {
    package_slug: packageDetail.value.slug,
    package_name: packageDetail.value.name,
    package_lifecycle: packageDetail.value.lifecycle,
    version: versionDetail.value.version,
    digest: versionDetail.value.digest,
    mount_prefix: mountPrefix.value.trim().replace(/^\/+|\/+$/g, ''),
    file_count: versionDetail.value.file_count,
    files: versionDetail.value.files.map(file => ({
      path: file.path,
      digest: file.digest,
      mounted_path: mountedPath(file.path),
    })),
  }
  emit('add', {
    reference,
    version: versionDetail.value,
    selectedPaths: [...selectedPaths.value],
  })
  expanded.value = false
}

onMounted(() => void loadPackages())
</script>

<template>
  <section class="wdl-package-reference-panel">
    <header>
      <h2>工具包</h2>
      <button
        v-if="!readOnly"
        class="wdl-package-reference-panel__toggle"
        type="button"
        @click="expanded = !expanded"
      >
        {{ expanded ? '收起' : '引用工具包' }}
      </button>
    </header>

    <NuxtLink
      v-for="reference in references"
      :key="`${reference.package_slug}:${reference.version}:${reference.mount_prefix}`"
      class="wdl-package-reference"
      :to="`/wdl-packages/${reference.package_slug}`"
    >
      <span>
        <strong>{{ reference.package_name }}</strong>
        <small>{{ reference.file_count }} 文件 · {{ reference.mount_prefix || '根目录' }}</small>
      </span>
      <code>{{ reference.version }}</code>
    </NuxtLink>

    <div v-if="expanded && !readOnly" class="wdl-package-reference-picker">
      <label>
        <span>工具包</span>
        <select v-model="packageSlug" @change="selectPackage">
          <option value="">选择工具包</option>
          <option v-for="item in packages" :key="item.slug" :value="item.slug">
            {{ item.name }}
          </option>
        </select>
      </label>
      <label v-if="packageDetail">
        <span>版本</span>
        <select v-model="version" @change="loadVersion">
          <option v-for="item in packageDetail.versions" :key="item.version" :value="item.version">
            {{ item.version }}
          </option>
        </select>
      </label>
      <label v-if="versionDetail">
        <span>挂载目录</span>
        <input v-model="mountPrefix" spellcheck="false" />
      </label>

      <div v-if="versionDetail" class="wdl-package-reference-picker__files">
        <label v-for="file in versionDetail.files" :key="file.path">
          <input v-model="selectedPaths" type="checkbox" :value="file.path" />
          <code>{{ file.path }}</code>
        </label>
      </div>

      <p v-if="errorMessage" class="inline-error" role="alert">{{ errorMessage }}</p>
      <button
        v-if="versionDetail"
        class="button button--primary"
        type="button"
        :disabled="loading"
        @click="addReference"
      >
        确认引用
      </button>
    </div>
  </section>
</template>

<style scoped>
.wdl-package-reference-panel {
  display: grid;
  gap: var(--space-2);
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--color-border);
}

.wdl-package-reference-panel > header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.wdl-package-reference-panel h2 {
  margin: 0;
}

.wdl-package-reference-panel__toggle {
  border: 0;
  background: transparent;
  color: var(--color-primary);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
}

.wdl-package-reference {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  color: inherit;
  text-decoration: none;
}

.wdl-package-reference:hover {
  border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
}

.wdl-package-reference span {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.wdl-package-reference small,
.wdl-package-reference code {
  color: var(--color-muted);
  font-size: 11px;
}

.wdl-package-reference-picker {
  display: grid;
  gap: var(--space-2);
  padding: var(--space-2);
  background: var(--color-surface);
  border-radius: var(--radius-sm);
}

.wdl-package-reference-picker > label {
  display: grid;
  gap: 4px;
  color: var(--color-muted);
  font-size: 11px;
}

.wdl-package-reference-picker select,
.wdl-package-reference-picker input:not([type='checkbox']) {
  width: 100%;
  min-height: 32px;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  color: var(--color-ink);
  padding: 0 8px;
}

.wdl-package-reference-picker__files {
  max-height: 180px;
  overflow: auto;
  display: grid;
  gap: 6px;
}

.wdl-package-reference-picker__files label {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  cursor: pointer;
}

.wdl-package-reference-picker__files code {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
}
</style>
