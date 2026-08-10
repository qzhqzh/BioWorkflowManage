<script setup lang="ts">
import { coerceWdlValue } from '~/utils/tool-authoring'

const props = defineProps<{
  state: 'idle' | 'saving' | 'saved' | 'publishing' | 'published' | 'error'
  validationStatus?: string
  operationError?: {
    code: string
    message: string
  } | null
}>()

const emit = defineEmits<{
  dirty: []
  save: []
  publish: []
}>()

const draft = defineModel<Record<string, any>>('draft', { required: true })

const annotationConfig = computed(() =>
  draft.value.task_kind === 'annotation' && draft.value.annotation
    ? draft.value.annotation
    : undefined,
)

const annotationGroups = computed(() => {
  const groups = new Map<string, Record<string, any>[]>()
  for (const option of annotationConfig.value?.options ?? []) {
    const group = option.group || '其他'
    groups.set(group, [...(groups.get(group) ?? []), option])
  }
  return [...groups.entries()].map(([name, options]) => ({ name, options }))
})

const wdlTypes = [
  'File',
  'Directory',
  'String',
  'Int',
  'Float',
  'Boolean',
  'Array[File]',
  'Array[String]',
  'Array[Int]',
  'Array[Float]',
  'Array[Boolean]',
  'Pair[File,File]',
]

function addInput() {
  draft.value.inputs = [
    ...(draft.value.inputs ?? []),
    {
      name: `input_${(draft.value.inputs?.length ?? 0) + 1}`,
      label: 'New input',
      wdl_type: 'File',
      semantic_type: 'core.file.any',
      required: true,
    },
  ]
  emit('dirty')
}

function removeInput(index: number) {
  draft.value.inputs = draft.value.inputs.filter((_: unknown, itemIndex: number) => itemIndex !== index)
  emit('dirty')
}

function updateInputDefault(input: Record<string, any>, event: Event) {
  const rawValue = (event.target as HTMLInputElement | HTMLSelectElement).value
  if (rawValue === '') delete input.default
  else input.default = coerceWdlValue(rawValue, input.wdl_type)
  emit('dirty')
}

function isAnnotationSelectorInput(input: Record<string, any>) {
  return Boolean(annotationConfig.value?.selector_input === input.name)
}

function addOutput() {
  draft.value.outputs = [
    ...(draft.value.outputs ?? []),
    {
      name: `output_${(draft.value.outputs?.length ?? 0) + 1}`,
      label: 'New output',
      wdl_type: 'File',
      semantic_type: 'core.file.any',
      capture: { mode: 'path', value: 'outputs/result.txt' },
    },
  ]
  emit('dirty')
}

function removeOutput(index: number) {
  if ((draft.value.outputs?.length ?? 0) <= 1) return
  draft.value.outputs = draft.value.outputs.filter((_: unknown, itemIndex: number) => itemIndex !== index)
  emit('dirty')
}
</script>

<template>
  <form class="tool-editor" @input="emit('dirty')" @submit.prevent="emit('save')">
    <div class="tool-editor__heading">
      <div>
        <h3>编辑工具草稿</h3>
        <p>草稿可反复保存；发布后形成不可变版本，修改内容必须提升软件版本。</p>
      </div>
      <div class="tool-editor__actions">
        <span
          class="draft-validation"
          :class="`draft-validation--${validationStatus ?? 'unknown'}`"
        >
          {{ validationStatus === 'valid' ? '校验通过' : validationStatus === 'invalid' ? '需要修正' : '尚未校验' }}
        </span>
        <button
          class="button button--ghost"
          type="submit"
          :disabled="props.state === 'saving' || props.state === 'publishing'"
        >
          {{ props.state === 'saving' ? '保存中…' : props.state === 'saved' ? '草稿已保存' : '保存草稿' }}
        </button>
        <button
          class="button button--primary"
          type="button"
          :disabled="validationStatus !== 'valid' || props.state === 'saving' || props.state === 'publishing'"
          @click="emit('publish')"
        >
          {{ props.state === 'publishing' ? '发布中…' : props.state === 'published' ? '版本已发布' : '发布新版本' }}
        </button>
      </div>
    </div>

    <div v-if="props.operationError" class="tool-operation-error" role="alert">
      <strong>{{ props.operationError.code }}</strong>
      <p>{{ props.operationError.message }}</p>
    </div>

    <div class="form-grid">
      <label class="field">
        <span>标题</span>
        <input v-model="draft.display_name" type="text" />
      </label>
      <label class="field">
        <span>软件名称</span>
        <input v-model="draft.name" type="text" />
      </label>
      <label class="field">
        <span>软件版本</span>
        <input v-model="draft.tool_version" type="text" />
      </label>
      <label class="field">
        <span>Docker 镜像</span>
        <input v-model="draft.container.image" type="text" />
      </label>
    </div>

    <label class="field">
      <span>说明</span>
      <textarea v-model="draft.description" rows="3" />
    </label>

    <label class="field">
      <span>命令模板</span>
      <textarea v-model="draft.command.template" class="mono-input" rows="8" />
      <small>使用 <code v-pre>{{ inputs.port_name }}</code> 引用输入端口。</small>
    </label>

    <div class="port-editor-grid">
      <section>
        <div class="port-editor-heading">
          <div>
            <h4>输入端口</h4>
            <small>{{ draft.inputs?.length ?? 0 }} 个</small>
          </div>
          <button class="button button--ghost" type="button" @click="addInput">添加输入</button>
        </div>
        <div v-if="!draft.inputs?.length" class="port-editor-empty">
          没有输入端口。纯生成型工具可以保持为空。
        </div>
        <article
          v-for="(input, index) in draft.inputs"
          :key="`input-${index}`"
          class="port-editor-card"
        >
          <div class="port-editor-card__heading">
            <strong>{{ input.label || input.name || `输入 ${Number(index) + 1}` }}</strong>
            <button type="button" @click="removeInput(Number(index))">删除</button>
          </div>
          <div class="port-editor-fields">
            <label class="field">
              <span>字段名</span>
              <input v-model="input.name" aria-label="输入字段名" />
            </label>
            <label class="field">
              <span>标题</span>
              <input v-model="input.label" aria-label="输入标题" />
            </label>
            <label class="field">
              <span>WDL 类型</span>
              <select v-model="input.wdl_type" aria-label="输入 WDL 类型">
                <option v-for="wdlType in wdlTypes" :key="wdlType" :value="wdlType">{{ wdlType }}</option>
              </select>
            </label>
            <label class="field">
              <span>语义类型</span>
              <input v-model="input.semantic_type" aria-label="输入语义类型" />
            </label>
            <div v-if="isAnnotationSelectorInput(input)" class="annotation-default-editor">
              <div class="annotation-default-editor__heading">
                <span>可用注释项</span>
                <strong>{{ annotationConfig.options.length }} 项</strong>
              </div>
              <div class="annotation-default-editor__groups">
                <fieldset v-for="group in annotationGroups" :key="group.name">
                  <legend>{{ group.name }}</legend>
                  <label v-for="option in group.options" :key="option.id">
                    <input
                      type="checkbox"
                      checked
                      disabled
                    />
                    <span>
                      <strong>{{ option.label }}</strong>
                      <small v-if="option.description">{{ option.description }}</small>
                    </span>
                  </label>
                </fieldset>
              </div>
              <small>实际注释项在 Workflow 的注释节点中选择。</small>
            </div>
            <label v-else-if="!String(input.wdl_type).includes('File')" class="field">
              <span>默认值</span>
              <select
                v-if="input.wdl_type === 'Boolean'"
                :value="input.default === undefined ? '' : String(input.default)"
                aria-label="输入默认值"
                @change="updateInputDefault(input, $event)"
              >
                <option value="">无默认值</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
              <input
                v-else
                :value="Array.isArray(input.default) ? input.default.join(', ') : input.default"
                :type="input.wdl_type === 'Int' || input.wdl_type === 'Float' ? 'number' : 'text'"
                :step="input.wdl_type === 'Float' ? 'any' : undefined"
                :placeholder="String(input.wdl_type).startsWith('Array[') ? '多个值用逗号分隔' : '无默认值'"
                aria-label="输入默认值"
                @change="updateInputDefault(input, $event)"
              />
            </label>
            <label class="field port-description-field">
              <span>说明</span>
              <input v-model="input.description" aria-label="输入说明" />
            </label>
          </div>
          <label class="checkbox-field">
            <input v-model="input.required" type="checkbox" />
            <span>必填输入</span>
          </label>
        </article>
      </section>

      <section>
        <div class="port-editor-heading">
          <div>
            <h4>输出端口</h4>
            <small>{{ draft.outputs?.length ?? 0 }} 个，至少保留 1 个</small>
          </div>
          <button class="button button--ghost" type="button" @click="addOutput">添加输出</button>
        </div>
        <article
          v-for="(output, index) in draft.outputs"
          :key="`output-${index}`"
          class="port-editor-card"
        >
          <div class="port-editor-card__heading">
            <strong>{{ output.label || output.name || `输出 ${Number(index) + 1}` }}</strong>
            <button
              type="button"
              :disabled="draft.outputs.length <= 1"
              @click="removeOutput(Number(index))"
            >
              删除
            </button>
          </div>
          <div class="port-editor-fields">
            <label class="field">
              <span>字段名</span>
              <input v-model="output.name" aria-label="输出字段名" />
            </label>
            <label class="field">
              <span>标题</span>
              <input v-model="output.label" aria-label="输出标题" />
            </label>
            <label class="field">
              <span>WDL 类型</span>
              <select v-model="output.wdl_type" aria-label="输出 WDL 类型">
                <option v-for="wdlType in wdlTypes" :key="wdlType" :value="wdlType">{{ wdlType }}</option>
              </select>
            </label>
            <label class="field">
              <span>语义类型</span>
              <input v-model="output.semantic_type" aria-label="输出语义类型" />
            </label>
            <label class="field">
              <span>捕获方式</span>
              <select v-model="output.capture.mode" aria-label="输出捕获方式">
                <option value="path">固定路径</option>
                <option value="glob">Glob</option>
                <option value="expression">WDL 表达式</option>
              </select>
            </label>
            <label class="field">
              <span>{{ output.capture.mode === 'expression' ? 'WDL 表达式' : '产物路径' }}</span>
              <input v-model="output.capture.value" aria-label="输出捕获值" />
            </label>
            <label class="field port-description-field">
              <span>说明</span>
              <input v-model="output.description" aria-label="输出说明" />
            </label>
          </div>
          <label class="checkbox-field">
            <input v-model="output.optional" type="checkbox" />
            <span>可选输出</span>
          </label>
        </article>
      </section>
    </div>

    <fieldset class="runtime-editor">
      <legend>建议运行资源</legend>
      <label class="field">
        <span>CPU</span>
        <input v-model.number="draft.runtime.cpu" type="number" min="1" />
      </label>
      <label class="field">
        <span>内存 GB</span>
        <input v-model.number="draft.runtime.memory_gb" type="number" min="0.1" step="0.1" />
      </label>
      <label class="field">
        <span>磁盘 GB</span>
        <input v-model.number="draft.runtime.disk_gb" type="number" min="1" />
      </label>
    </fieldset>
  </form>
</template>

<style scoped>
.tool-editor__actions,
.port-editor-heading,
.port-editor-card__heading {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.tool-editor__actions {
  flex-wrap: wrap;
  justify-content: flex-end;
}

.draft-validation {
  color: var(--color-muted);
  font-size: var(--text-secondary);
}

.draft-validation--valid {
  color: var(--color-primary);
}

.draft-validation--invalid {
  color: var(--color-error);
}

.tool-operation-error {
  display: grid;
  gap: var(--space-1);
  border: 1px solid color-mix(in srgb, var(--color-error) 32%, var(--color-border));
  border-radius: var(--radius-md);
  background: color-mix(in srgb, var(--color-error) 7%, var(--color-surface));
  color: var(--color-error);
  padding: var(--space-3);
}

.tool-operation-error p {
  margin: 0;
  color: var(--color-ink-soft);
  font-size: var(--text-secondary);
}

.port-editor-heading,
.port-editor-card__heading {
  justify-content: space-between;
}

.port-editor-heading > div {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
}

.port-editor-heading small {
  color: var(--color-muted);
}

.port-editor-card {
  border-top: 1px solid var(--color-border);
  padding: var(--space-3) 0;
}

.port-editor-card__heading {
  margin-bottom: var(--space-3);
}

.port-editor-card__heading button {
  border: 0;
  background: transparent;
  color: var(--color-muted);
  font-size: var(--text-secondary);
}

.port-editor-card__heading button:hover {
  color: var(--color-error);
}

.port-editor-card__heading button:disabled {
  color: var(--color-border-strong);
}

.port-editor-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2) var(--space-3);
}

.port-description-field {
  grid-column: 1 / -1;
}

.checkbox-field {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  font-size: var(--text-secondary);
}

.port-editor-empty {
  border-top: 1px solid var(--color-border);
  color: var(--color-muted);
  padding: var(--space-4) 0;
  font-size: var(--text-secondary);
}

.runtime-editor {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--space-3);
  margin: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
}

.runtime-editor legend {
  color: var(--color-ink-soft);
  padding: 0 var(--space-1);
  font-size: var(--text-secondary);
  font-weight: 650;
}

.runtime-editor .field {
  margin-top: 0;
}

.annotation-default-editor {
  grid-column: 1 / -1;
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-1);
  border-top: 1px solid var(--color-border);
  padding-top: var(--space-3);
}

.annotation-default-editor__heading {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.annotation-default-editor__heading {
  justify-content: space-between;
  color: var(--color-ink-soft);
  font-size: var(--text-caption);
  font-weight: 600;
}

.annotation-default-editor__heading strong {
  color: var(--color-primary-hover);
  font-size: var(--text-caption);
}

.annotation-default-editor__groups {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}

.annotation-default-editor__groups fieldset {
  display: grid;
  align-content: start;
  gap: var(--space-1);
  margin: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-2);
}

.annotation-default-editor__groups legend {
  padding: 0 var(--space-1);
  color: var(--color-muted);
  font-size: var(--text-caption);
  font-weight: 600;
}

.annotation-default-editor__groups label {
  display: grid;
  grid-template-columns: 1rem minmax(0, 1fr);
  align-items: start;
  gap: var(--space-2);
  border-radius: var(--radius-sm);
  padding: var(--space-1);
  cursor: pointer;
}

.annotation-default-editor__groups label:hover {
  background: var(--color-surface);
}

.annotation-default-editor__groups input {
  width: 1rem;
  height: 1rem;
  margin: 0.1875rem 0 0;
  accent-color: var(--color-primary);
}

.annotation-default-editor__groups label > span {
  display: grid;
  gap: 0.0625rem;
}

.annotation-default-editor__groups strong {
  color: var(--color-ink);
  font-size: var(--text-secondary);
  font-weight: 600;
}

.annotation-default-editor__groups small {
  color: var(--color-muted);
  font-size: var(--text-caption);
  line-height: 1.4;
}

@media (max-width: 900px) {
  .annotation-default-editor__groups {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 64rem) {
  .port-editor-fields,
  .runtime-editor {
    grid-template-columns: 1fr;
  }
}
</style>
