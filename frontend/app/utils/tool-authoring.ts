export const WDL_IDENTIFIER_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

export function normalizeIdentifier(value: string, fallback = 'item') {
  const normalized = value
    .trim()
    .replace(/[^A-Za-z0-9_]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .replace(/^([0-9])/, '_$1')

  return normalized || fallback
}

export function createToolDraft(toolId: string) {
  const id = normalizeIdentifier(toolId, 'new_tool')
  return {
    schema_version: '1.0.0',
    id,
    name: id,
    display_name: id,
    tool_version: '0.1.0',
    description: '',
    container: {
      engine: 'docker',
      image: 'ubuntu:24.04',
    },
    inputs: [],
    outputs: [
      {
        name: 'result',
        label: 'Result',
        wdl_type: 'File',
        semantic_type: 'core.file.any',
        capture: {
          mode: 'path',
          value: 'outputs/result.txt',
        },
      },
    ],
    command: {
      shell: 'bash',
      strict_mode: true,
      template: 'mkdir -p outputs\necho "TODO" > outputs/result.txt\n',
    },
    runtime: {
      cpu: 1,
      memory_gb: 1,
      disk_gb: 10,
    },
  }
}

export function hydrateToolDraft(toolSpec: Record<string, any>, toolId: string) {
  const base = createToolDraft(toolId)
  return {
    ...base,
    ...toolSpec,
    id: toolId,
    container: {
      ...base.container,
      ...(toolSpec.container ?? {}),
    },
    command: {
      ...base.command,
      ...(toolSpec.command ?? {}),
    },
    runtime: {
      ...base.runtime,
      ...(toolSpec.runtime ?? {}),
    },
    inputs: (toolSpec.inputs ?? []).map((port: Record<string, any>) => ({
      required: true,
      ...port,
    })),
    outputs: (toolSpec.outputs ?? []).map((port: Record<string, any>) => ({
      ...port,
      capture: {
        mode: 'path',
        value: 'outputs/result.txt',
        ...(port.capture ?? {}),
      },
    })),
  }
}

export function coerceWdlValue(value: string | boolean, wdlType: string) {
  if (wdlType === 'Boolean') {
    return typeof value === 'boolean' ? value : value === 'true'
  }
  if (wdlType === 'Int') {
    const parsed = Number.parseInt(String(value), 10)
    return Number.isNaN(parsed) ? value : parsed
  }
  if (wdlType === 'Float') {
    const parsed = Number.parseFloat(String(value))
    return Number.isNaN(parsed) ? value : parsed
  }
  if (wdlType.startsWith('Array[')) {
    return String(value)
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
  }
  return value
}
