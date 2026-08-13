<script setup lang="ts">
import type { editor as MonacoEditor, IDisposable } from 'monaco-editor'
import 'monaco-editor/min/vs/editor/editor.main.css'

type MonacoApi = typeof import('monaco-editor/esm/vs/editor/editor.api')

const modelValue = defineModel<string>({ required: true })
const props = withDefaults(defineProps<{
  readOnly?: boolean
  ariaLabel?: string
  formatting?: boolean
}>(), {
  readOnly: false,
  ariaLabel: 'WDL 源码编辑器',
  formatting: false,
})
const emit = defineEmits<{
  export: []
  format: []
  comment: [line: number]
  cursorLine: [line: number]
}>()

const container = ref<HTMLElement>()
let editor: MonacoEditor.IStandaloneCodeEditor | undefined
let subscription: IDisposable | undefined
let formatAction: IDisposable | undefined
let exportAction: IDisposable | undefined
let commentAction: IDisposable | undefined
let cursorSubscription: IDisposable | undefined
let applyingExternalValue = false

function applyFormattedValue(value: string) {
  if (!editor) {
    modelValue.value = value
    return
  }
  const model = editor.getModel()
  if (!model || model.getValue() === value) {
    modelValue.value = value
    return
  }

  const viewState = editor.saveViewState()
  const hadTextFocus = editor.hasTextFocus()
  applyingExternalValue = true
  try {
    editor.pushUndoStop()
    editor.executeEdits('wdl-format', [{
      range: model.getFullModelRange(),
      text: value,
      forceMoveMarkers: true,
    }])
    editor.pushUndoStop()
    modelValue.value = value
    if (viewState) editor.restoreViewState(viewState)
    if (hadTextFocus) editor.focus()
  } finally {
    applyingExternalValue = false
  }
}

function revealLine(line: number) {
  if (!editor || !Number.isFinite(line)) return
  editor.revealLineInCenter(line)
  editor.setPosition({ lineNumber: Math.max(1, line), column: 1 })
  editor.focus()
}

defineExpose({ applyFormattedValue, revealLine })

function registerWdlLanguage(monaco: MonacoApi) {
  if (!monaco.languages.getLanguages().some(language => language.id === 'wdl')) {
    monaco.languages.register({ id: 'wdl', extensions: ['.wdl'] })
    monaco.languages.setMonarchTokensProvider('wdl', {
      keywords: [
        'version', 'import', 'as', 'task', 'workflow', 'input', 'output',
        'command', 'runtime', 'requirements', 'hints', 'meta', 'parameter_meta',
        'call', 'scatter', 'in', 'if', 'then', 'else', 'struct', 'object', 'after',
      ],
      typeKeywords: [
        'File', 'String', 'Int', 'Float', 'Boolean', 'Array', 'Map', 'Pair', 'Object',
      ],
      tokenizer: {
        root: [
          [/#.*$/, 'comment'],
          [/\b(?:true|false|None|null)\b/, 'constant'],
          [/\b\d+(?:\.\d+)?\b/, 'number'],
          [/[A-Za-z_][\w]*/, {
            cases: {
              '@keywords': 'keyword',
              '@typeKeywords': 'type',
              '@default': 'identifier',
            },
          }],
          [/"([^"\\]|\\.)*$/, 'string.invalid'],
          [/"/, 'string', '@stringDouble'],
          [/'([^'\\]|\\.)*$/, 'string.invalid'],
          [/'/, 'string', '@stringSingle'],
          [/~\{|\$\{/, 'delimiter.bracket'],
          [/[{}()[\]]/, '@brackets'],
          [/[=,:.?+\-*/!<>]+/, 'operator'],
        ],
        stringDouble: [
          [/[^\\"]+/, 'string'],
          [/\\./, 'string.escape'],
          [/"/, 'string', '@pop'],
        ],
        stringSingle: [
          [/[^\\']+/, 'string'],
          [/\\./, 'string.escape'],
          [/'/, 'string', '@pop'],
        ],
      },
    })
  }

  monaco.editor.defineTheme('bioworkflow-wdl', {
    base: 'vs',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '667C75', fontStyle: 'italic' },
      { token: 'keyword', foreground: '00695C', fontStyle: 'bold' },
      { token: 'type', foreground: '775600' },
      { token: 'string', foreground: '8A3B12' },
      { token: 'number', foreground: '4D5BB3' },
      { token: 'constant', foreground: '4D5BB3' },
      { token: 'delimiter.bracket', foreground: '00695C', fontStyle: 'bold' },
    ],
    colors: {
      'editor.background': '#F8FBFA',
      'editor.foreground': '#183B33',
      'editorLineNumber.foreground': '#71847F',
      'editorLineNumber.activeForeground': '#245B50',
      'editorCursor.foreground': '#006B5C',
      'editor.selectionBackground': '#CDEBE4',
      'editor.inactiveSelectionBackground': '#E3F2EE',
      'editor.lineHighlightBackground': '#EEF6F3',
      'editorIndentGuide.background1': '#DCE8E4',
      'editorIndentGuide.activeBackground1': '#9ABBB3',
    },
  })
}

onMounted(async () => {
  const monaco = await import('monaco-editor/esm/vs/editor/editor.api')
  if (!container.value) return
  registerWdlLanguage(monaco)
  editor = monaco.editor.create(container.value, {
    value: modelValue.value,
    language: 'wdl',
    theme: 'bioworkflow-wdl',
    readOnly: props.readOnly,
    ariaLabel: props.ariaLabel,
    automaticLayout: true,
    fontFamily: '"SFMono-Regular", Consolas, "Liberation Mono", monospace',
    fontSize: 13,
    lineHeight: 21,
    minimap: { enabled: false },
    padding: { top: 12, bottom: 16 },
    renderWhitespace: 'selection',
    scrollBeyondLastLine: false,
    smoothScrolling: true,
    tabSize: 2,
    insertSpaces: true,
    wordWrap: 'off',
  })
  subscription = editor.onDidChangeModelContent(() => {
    if (!applyingExternalValue) modelValue.value = editor?.getValue() ?? ''
  })
  cursorSubscription = editor.onDidChangeCursorPosition((event) => {
    emit('cursorLine', event.position.lineNumber)
  })
  emit('cursorLine', editor.getPosition()?.lineNumber ?? 1)
  formatAction = editor.addAction({
    id: 'bioworkflow.format-wdl',
    label: '格式化 WDL',
    keybindings: [
      monaco.KeyMod.Shift | monaco.KeyMod.Alt | monaco.KeyCode.KeyF,
    ],
    run: () => {
      if (!props.readOnly && !props.formatting) emit('format')
    },
  })
  exportAction = editor.addAction({
    id: 'bioworkflow.export-wdl',
    label: '导出 WDL',
    keybindings: [
      monaco.KeyMod.Shift | monaco.KeyMod.Alt | monaco.KeyCode.KeyE,
    ],
    run: () => emit('export'),
  })
  commentAction = editor.addAction({
    id: 'bioworkflow.add-wdl-review-comment',
    label: '添加行级评论',
    keybindings: [
      monaco.KeyMod.CtrlCmd | monaco.KeyMod.Alt | monaco.KeyCode.KeyM,
    ],
    contextMenuGroupId: 'navigation',
    contextMenuOrder: 1.5,
    run: () => emit('comment', editor?.getPosition()?.lineNumber ?? 1),
  })
})

watch(modelValue, (value) => {
  if (!editor || editor.getValue() === value) return
  applyingExternalValue = true
  editor.setValue(value)
  applyingExternalValue = false
})

watch(() => props.readOnly, value => editor?.updateOptions({ readOnly: value }))

onBeforeUnmount(() => {
  cursorSubscription?.dispose()
  commentAction?.dispose()
  exportAction?.dispose()
  formatAction?.dispose()
  subscription?.dispose()
  editor?.dispose()
})
</script>

<template>
  <div ref="container" class="wdl-code-editor" />
</template>
