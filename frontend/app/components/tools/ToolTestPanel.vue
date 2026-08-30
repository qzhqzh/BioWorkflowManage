<script setup lang="ts">
interface ManagedResource {
  source: 'rawdata' | 'database'
  path: string
}

interface ToolTestRun {
  id: string
  label: string
  actor: string
  status: 'queued' | 'preparing' | 'running' | 'cancel_requested' | 'succeeded' | 'failed' | 'canceled'
  output_status: 'pending' | 'complete' | 'incomplete' | 'unavailable'
  progress: number
  current_step: string
  error?: string
  error_code: string
  error_details: Record<string, unknown>
  outputs: Array<{
    key: string
    label?: string
    kind: 'file' | 'value' | 'directory' | 'unverifiable'
    name?: string
    size_label?: string
    entry_count?: number
    value?: unknown
    reason?: string
    download_url?: string
  }>
  timing: { total_seconds?: number; execution_seconds?: number }
  events?: Array<{ id: number; level: string; message: string; created_at: string }>
  created_at: string
  finished_at?: string
}

interface ResourceOption {
  path: string
  name: string
  kind: 'file' | 'directory'
  size_label?: string
}

const props = defineProps<{
  toolId: string
  version: string
  toolSpec: Record<string, any>
}>()

const label = ref('')
const values = ref<Record<string, any>>({})
const submitState = ref<'idle' | 'submitting' | 'error'>('idle')
const errorMessage = ref('')
const runs = ref<ToolTestRun[]>([])
const activeRun = ref<ToolTestRun>()
const loadingRuns = ref(false)
const resourceOptions = ref<Record<string, ResourceOption[]>>({})
let pollTimer: ReturnType<typeof setTimeout> | undefined

const inputs = computed<Record<string, any>[]>(() => props.toolSpec.inputs ?? [])
const annotationSelector = computed(() => (
  props.toolSpec.task_kind === 'annotation'
    ? props.toolSpec.annotation?.selector_input
    : undefined
))
const annotationGroups = computed(() => {
  const groups = new Map<string, Record<string, any>[]>()
  for (const option of props.toolSpec.annotation?.options ?? []) {
    const group = option.group || '其他'
    groups.set(group, [...(groups.get(group) ?? []), option])
  }
  return [...groups.entries()].map(([name, options]) => ({ name, options }))
})
const isRunning = computed(() => activeRun.value && ['queued', 'preparing', 'running', 'cancel_requested'].includes(activeRun.value.status))
const outputErrorSummary = computed(() => {
  const details = activeRun.value?.error_details
  if (!details) return ''
  const describe = (value: unknown) => {
    if (typeof value === 'string') return value
    if (value && typeof value === 'object') {
      const item = value as Record<string, unknown>
      const key = String(item.key ?? item.name ?? '<unknown>')
      return item.reason ? `${key} (${String(item.reason)})` : key
    }
    return String(value)
  }
  const lines: string[] = []
  if (Array.isArray(details.missing) && details.missing.length) {
    lines.push(`缺失：${details.missing.map(describe).join('、')}`)
  }
  if (Array.isArray(details.unverifiable) && details.unverifiable.length) {
    lines.push(`无法验证：${details.unverifiable.map(describe).join('、')}`)
  }
  return lines.join('；')
})

function newResource(kind: string): ManagedResource {
  return { source: kind === 'Directory' ? 'database' : 'rawdata', path: '' }
}

function initializeValues() {
  const next: Record<string, any> = {}
  for (const input of inputs.value) {
    const type = String(input.wdl_type)
    if (type === 'File' || type === 'Directory') next[input.name] = newResource(type)
    else if (type === 'Pair[File,File]') next[input.name] = [newResource('File'), newResource('File')]
    else if (type === 'Array[File]') next[input.name] = [newResource('File')]
    else if (type === 'Boolean') next[input.name] = input.default === undefined ? '' : String(input.default)
    else if (type.startsWith('Array[')) next[input.name] = Array.isArray(input.default) ? [...input.default] : []
    else next[input.name] = input.default === undefined ? '' : String(input.default)
  }
  values.value = next
  label.value = `${props.toolSpec.display_name ?? props.toolSpec.name ?? props.toolId} 测试`
  activeRun.value = undefined
  errorMessage.value = ''
}

function resourceKey(source: string, kind: string) {
  return `${source}:${kind === 'Directory' ? 'directory' : 'file'}`
}

function optionsFor(resource: ManagedResource, wdlType: string) {
  return resourceOptions.value[resourceKey(resource.source, wdlType)] ?? []
}

async function loadResourceOptions() {
  const requests = [
    ['rawdata', 'file'],
    ['rawdata', 'directory'],
    ['database', 'file'],
    ['database', 'directory'],
  ] as const
  const responses = await Promise.all(requests.map(async ([source, kind]) => {
    try {
      const response = await $fetch<{ results: ResourceOption[] }>(
        `/api/v1/tool-test-resources?source=${source}&kind=${kind}`,
      )
      return [`${source}:${kind}`, response.results] as const
    }
    catch {
      return [`${source}:${kind}`, []] as const
    }
  }))
  resourceOptions.value = Object.fromEntries(responses)
}

function addResource(inputName: string) {
  values.value[inputName] = [...(values.value[inputName] ?? []), newResource('File')]
}

function removeResource(inputName: string, index: number) {
  const items = [...(values.value[inputName] ?? [])]
  items.splice(index, 1)
  values.value[inputName] = items
}

function updateArrayText(inputName: string, value: string) {
  values.value[inputName] = value.split(',').map(item => item.trim()).filter(Boolean)
}

function arrayText(inputName: string) {
  return Array.isArray(values.value[inputName]) ? values.value[inputName].join(', ') : ''
}

function requestInputs() {
  const result: Record<string, any> = {}
  for (const input of inputs.value) {
    const type = String(input.wdl_type)
    const value = values.value[input.name]
    if ((value === '' || value === null || value === undefined) && !input.required) continue
    if (type === 'Int') result[input.name] = value === '' ? null : Number.parseInt(value, 10)
    else if (type === 'Float') result[input.name] = value === '' ? null : Number.parseFloat(value)
    else if (type === 'Boolean') result[input.name] = value === '' ? null : value === 'true'
    else if (type === 'Array[Int]') result[input.name] = value.map((item: string) => Number.parseInt(item, 10))
    else if (type === 'Array[Float]') result[input.name] = value.map((item: string) => Number.parseFloat(item))
    else if (type === 'Array[Boolean]') result[input.name] = value.map((item: string) => item === 'true')
    else result[input.name] = value
  }
  return result
}

function apiError(error: any) {
  return error?.data?.error?.message ?? error?.response?._data?.error?.message ?? '工具测试提交失败。'
}

function statusLabel(run: ToolTestRun) {
  if (run.status === 'succeeded' && run.output_status === 'incomplete') return '输出不完整'
  return {
    queued: '排队中',
    preparing: '准备中',
    running: '运行中',
    cancel_requested: '取消中',
    succeeded: '已通过',
    failed: '失败',
    canceled: '已取消',
  }[run.status]
}

function statusClass(run: ToolTestRun) {
  return run.status === 'succeeded' && run.output_status === 'incomplete'
    ? 'incomplete'
    : run.status
}

function formatDuration(value?: number) {
  if (value === undefined) return '—'
  if (value < 60) return `${value.toFixed(1)} 秒`
  return `${Math.floor(value / 60)} 分 ${(value % 60).toFixed(0)} 秒`
}

async function loadRuns() {
  loadingRuns.value = true
  try {
    const response = await $fetch<{ results: ToolTestRun[] }>(
      `/api/v1/tool-test-runs?tool_id=${encodeURIComponent(props.toolId)}&version=${encodeURIComponent(props.version)}`,
    )
    runs.value = response.results
  }
  finally {
    loadingRuns.value = false
  }
}

async function refreshRun(runId: string) {
  try {
    const run = await $fetch<ToolTestRun>(`/api/v1/tool-test-runs/${runId}`)
    activeRun.value = run
    runs.value = [run, ...runs.value.filter(item => item.id !== run.id)]
    if (['queued', 'preparing', 'running', 'cancel_requested'].includes(run.status)) {
      pollTimer = setTimeout(() => void refreshRun(runId), 2200)
    }
  }
  catch {
    pollTimer = undefined
  }
}

async function submitRun() {
  submitState.value = 'submitting'
  errorMessage.value = ''
  if (pollTimer) clearTimeout(pollTimer)
  try {
    const run = await $fetch<ToolTestRun>('/api/v1/tool-test-runs', {
      method: 'POST',
      body: {
        tool_id: props.toolId,
        tool_version: props.version,
        label: label.value,
        inputs: requestInputs(),
      },
    })
    activeRun.value = run
    runs.value = [run, ...runs.value.filter(item => item.id !== run.id)]
    submitState.value = 'idle'
    pollTimer = setTimeout(() => void refreshRun(run.id), 1000)
  }
  catch (error) {
    submitState.value = 'error'
    errorMessage.value = apiError(error)
  }
}

function openRun(run: ToolTestRun) {
  activeRun.value = run
  if (pollTimer) clearTimeout(pollTimer)
  void refreshRun(run.id)
}

watch(() => [props.toolId, props.version], async () => {
  if (pollTimer) clearTimeout(pollTimer)
  initializeValues()
  await loadRuns()
}, { immediate: true })

onMounted(() => void loadResourceOptions())
onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
})
</script>

<template>
  <div class="tool-test-workspace">
    <form class="tool-test-form" @submit.prevent="submitRun">
      <div class="tool-test-heading">
        <div>
          <strong>独立测试 v{{ version }}</strong>
          <span>使用固定版本和小数据运行</span>
        </div>
        <span class="tool-test-image">{{ toolSpec.container?.image }}</span>
      </div>

      <label class="field">
        <span>测试名称</span>
        <input v-model="label" type="text" maxlength="256" />
      </label>

      <section class="tool-test-inputs">
        <header><strong>输入</strong><span>{{ inputs.length }} 项</span></header>
        <div v-if="inputs.length" class="tool-test-input-list">
          <article v-for="input in inputs" :key="input.name" class="tool-test-input">
            <header>
              <span><strong>{{ input.label || input.name }}</strong><code>{{ input.name }}</code></span>
              <small>{{ input.wdl_type }}{{ input.required ? ' · 必填' : '' }}</small>
            </header>
            <p v-if="input.description">{{ input.description }}</p>

            <fieldset v-if="input.name === annotationSelector" class="tool-test-annotation">
              <div v-for="group in annotationGroups" :key="group.name">
                <legend>{{ group.name }}</legend>
                <label v-for="option in group.options" :key="option.id">
                  <input v-model="values[input.name]" type="checkbox" :value="option.id" />
                  <span>{{ option.label }}</span>
                </label>
              </div>
            </fieldset>

            <div v-else-if="input.wdl_type === 'File' || input.wdl_type === 'Directory'" class="tool-resource-field">
              <select v-model="values[input.name].source" :aria-label="`${input.label || input.name} 来源`">
                <option value="rawdata">原始数据</option>
                <option value="database">数据库</option>
              </select>
              <input
                v-model="values[input.name].path"
                type="text"
                :list="`resource-${input.name}`"
                :placeholder="input.wdl_type === 'Directory' ? '选择目录' : '选择文件'"
              />
              <datalist :id="`resource-${input.name}`">
                <option v-for="option in optionsFor(values[input.name], input.wdl_type)" :key="option.path" :value="option.path">
                  {{ option.size_label || option.path }}
                </option>
              </datalist>
            </div>

            <div v-else-if="input.wdl_type === 'Pair[File,File]' || input.wdl_type === 'Array[File]'" class="tool-resource-array">
              <div v-for="(resource, resourceIndex) in values[input.name]" :key="resourceIndex" class="tool-resource-field">
                <select v-model="resource.source" :aria-label="`${input.label || input.name} ${Number(resourceIndex) + 1} 来源`">
                  <option value="rawdata">原始数据</option>
                  <option value="database">数据库</option>
                </select>
                <input
                  v-model="resource.path"
                  type="text"
                  :list="`resource-${input.name}-${resourceIndex}`"
                  :placeholder="`文件 ${Number(resourceIndex) + 1}`"
                />
                <datalist :id="`resource-${input.name}-${resourceIndex}`">
                  <option v-for="option in optionsFor(resource, 'File')" :key="option.path" :value="option.path">
                    {{ option.size_label || option.path }}
                  </option>
                </datalist>
                <button
                  v-if="input.wdl_type === 'Array[File]' && values[input.name].length > 1"
                  type="button"
                  @click="removeResource(input.name, Number(resourceIndex))"
                >移除</button>
              </div>
              <button v-if="input.wdl_type === 'Array[File]'" class="text-button" type="button" @click="addResource(input.name)">添加文件</button>
            </div>

            <select v-else-if="input.wdl_type === 'Boolean'" v-model="values[input.name]">
              <option value="">未设置</option>
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
            <input
              v-else-if="input.wdl_type === 'Int' || input.wdl_type === 'Float'"
              v-model="values[input.name]"
              type="number"
              :step="input.wdl_type === 'Float' ? 'any' : '1'"
            />
            <input
              v-else-if="String(input.wdl_type).startsWith('Array[')"
              :value="arrayText(input.name)"
              type="text"
              placeholder="多个值用逗号分隔"
              @input="updateArrayText(input.name, ($event.target as HTMLInputElement).value)"
            />
            <input v-else v-model="values[input.name]" type="text" />
          </article>
        </div>
        <p v-else class="empty-state">这个 Task 不需要输入。</p>
      </section>

      <p v-if="errorMessage" class="tool-test-error" role="alert">{{ errorMessage }}</p>
      <button class="button button--primary tool-test-submit" type="submit" :disabled="submitState === 'submitting' || Boolean(isRunning)">
        {{ submitState === 'submitting' ? '提交中…' : isRunning ? '测试运行中' : '开始测试' }}
      </button>
    </form>

    <section class="tool-test-results">
      <header><strong>测试记录</strong><span>{{ runs.length }}</span></header>
      <div v-if="activeRun" class="tool-test-current">
        <header>
          <div>
            <strong>{{ activeRun.label }}</strong>
            <span>{{ activeRun.current_step }}</span>
          </div>
          <span class="tool-run-status" :class="`is-${statusClass(activeRun)}`">{{ statusLabel(activeRun) }}</span>
        </header>
        <div v-if="['queued', 'preparing', 'running', 'cancel_requested'].includes(activeRun.status)" class="tool-run-progress" role="progressbar" :aria-valuenow="activeRun.progress" aria-valuemin="0" aria-valuemax="100">
          <span :style="{ width: `${activeRun.progress}%` }" />
        </div>
        <dl>
          <div><dt>提交人</dt><dd>{{ activeRun.actor }}</dd></div>
          <div><dt>总耗时</dt><dd>{{ formatDuration(activeRun.timing.total_seconds) }}</dd></div>
          <div><dt>执行耗时</dt><dd>{{ formatDuration(activeRun.timing.execution_seconds) }}</dd></div>
        </dl>
        <p v-if="activeRun.error" class="tool-test-error">
          <strong v-if="activeRun.status === 'succeeded' && activeRun.output_status === 'incomplete'">执行完成，但输出不完整：</strong>{{ activeRun.error }}<template v-if="outputErrorSummary"> {{ outputErrorSummary }}</template>
        </p>
        <section v-if="activeRun.outputs.length" class="tool-test-outputs">
          <strong>输出</strong>
          <ul>
            <li v-for="output in activeRun.outputs" :key="output.key">
              <span><strong>{{ output.label || output.key }}</strong><code v-if="output.label && output.label !== output.key">{{ output.key }}</code><small>{{ output.size_label }}</small></span>
              <a v-if="output.download_url" :href="output.download_url">下载 {{ output.name }}</a>
              <code v-else-if="output.kind === 'value'">{{ output.value }}</code>
              <code v-else-if="output.kind === 'directory'">{{ output.entry_count ?? 0 }} 项</code>
              <code v-else>{{ output.reason || '该输出无法验证。' }}</code>
            </li>
          </ul>
        </section>
        <ul v-if="activeRun.events?.length" class="tool-test-events">
          <li v-for="event in activeRun.events.slice(-8)" :key="event.id" :class="`is-${event.level}`">
            <span />
            <p>{{ event.message }}</p>
          </li>
        </ul>
      </div>
      <div v-if="runs.length" class="tool-test-run-list">
        <button v-for="run in runs" :key="run.id" type="button" :class="{ 'is-active': activeRun?.id === run.id }" @click="openRun(run)">
          <span><strong>{{ run.label }}</strong><small>{{ new Date(run.created_at).toLocaleString('zh-CN') }}</small></span>
          <span class="tool-run-status" :class="`is-${statusClass(run)}`">{{ statusLabel(run) }}</span>
        </button>
      </div>
      <p v-else class="empty-state">{{ loadingRuns ? '正在读取测试记录…' : '还没有运行记录。' }}</p>
    </section>
  </div>
</template>
