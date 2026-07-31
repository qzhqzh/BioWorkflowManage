export interface WdlDiagnostic {
  code: string
  stage: string
  severity: 'error' | 'warning'
  message: string
  location?: { line?: number; column?: number }
}

export interface WdlDeclaration {
  name: string
  type: string
  line?: number
  end_line?: number
}

export interface WdlTaskDefinition {
  name: string
  line?: number
  end_line?: number
  inputs: WdlDeclaration[]
  outputs: WdlDeclaration[]
  runtime_keys: string[]
}

export interface WdlWorkflowDefinition {
  name: string
  line?: number
  end_line?: number
  inputs: WdlDeclaration[]
  outputs: WdlDeclaration[]
  structure: {
    call_count: number
    scatter_count: number
    conditional_count: number
  }
}

export interface WdlAnalysis {
  status: 'valid' | 'invalid'
  parsed: boolean
  wdl_version?: string | null
  summary: {
    task_count: number
    workflow_count: number
    import_count: number
    error_count: number
  }
  imports: Array<{
    uri: string
    namespace?: string
    line?: number
  }>
  tasks: WdlTaskDefinition[]
  workflows: WdlWorkflowDefinition[]
  diagnostics: WdlDiagnostic[]
}

export interface WdlSourceRevision {
  version: number
  operation: 'import' | 'edit' | 'format'
  digest: string
  diff: string
  note: string
  actor: string
  analysis: WdlAnalysis
  created_at: string
  content?: string
}

export interface WdlAuditEvent {
  id: number
  action: string
  actor: string
  note: string
  changes: Record<string, any>
  diff: string
  revision?: number | null
  created_at: string
}

export interface WdlAsset {
  slug: string
  name: string
  description: string
  source_filename: string
  lifecycle: 'active' | 'frozen' | 'migrating' | 'retired'
  tags: string[]
  created_by: string
  created_at: string
  updated_at: string
  revision_count: number
  current_revision?: WdlSourceRevision | null
  revisions?: WdlSourceRevision[]
  audit_events?: WdlAuditEvent[]
}

export interface WdlTag {
  id: number
  name: string
  asset_count: number
}
