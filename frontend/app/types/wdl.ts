export interface WdlDiagnostic {
  code: string
  stage: string
  severity: 'error' | 'warning'
  message: string
  file_path?: string
  location?: { line?: number; column?: number }
}

export interface WdlDeclaration {
  name: string
  type: string
  line?: number
  end_line?: number
  optional?: boolean
  has_default?: boolean
}

export interface WdlTaskDefinition {
  id?: string
  name: string
  file_path?: string
  source_digest?: string
  line?: number
  end_line?: number
  inputs: WdlDeclaration[]
  outputs: WdlDeclaration[]
  runtime_keys: string[]
}

export interface WdlWorkflowDefinition {
  name: string
  file_path?: string
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
    file_path?: string
    uri: string
    namespace?: string
    target_path?: string | null
    status?: 'resolved' | 'missing' | 'external'
    line?: number
  }>
  tasks: WdlTaskDefinition[]
  workflows: WdlWorkflowDefinition[]
  diagnostics: WdlDiagnostic[]
  package?: {
    entrypoint?: string
    file_count: number
    module_count?: number
    reachable_file_count?: number
    orphan_file_count?: number
    resolved_import_count: number
    missing_import_count: number
    external_import_count?: number
  }
  files?: Array<{
    path: string
    digest: string
    status: 'valid' | 'invalid'
    reachable: boolean
    task_count: number
    workflow_count: number
    import_count: number
  }>
}

export interface WdlSourceFile {
  path: string
  digest: string
  is_entry: boolean
  content?: string
  analysis?: Record<string, any>
  origin?: 'asset' | 'package'
  read_only?: boolean
  package_reference?: {
    package_slug: string
    package_name: string
    version: string
    digest: string
    mount_prefix: string
    package_file_path: string
  }
}

export interface WdlSourcePackageReference {
  package_slug: string
  package_name: string
  package_lifecycle: 'active' | 'archived'
  version: string
  digest: string
  mount_prefix: string
  file_count: number
  files: Array<{
    path: string
    digest: string
    mounted_path: string
  }>
}

export interface WdlSourceRevision {
  version: number
  operation: 'import' | 'edit' | 'format' | 'package_link'
  digest: string
  diff: string
  note: string
  actor: string
  analysis: WdlAnalysis
  created_at: string
  content?: string
  entrypoint?: string
  files?: WdlSourceFile[]
  package_references?: WdlSourcePackageReference[]
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
  source_repository: string
  source_revision: string
  lifecycle: 'active' | 'frozen' | 'migrating' | 'retired'
  tags: string[]
  created_by: string
  created_at: string
  updated_at: string
  revision_count: number
  file_count: number
  current_revision?: WdlSourceRevision | null
  revisions?: WdlSourceRevision[]
  audit_events?: WdlAuditEvent[]
}

export interface WdlTag {
  id: number
  name: string
  asset_count: number
}

export interface WdlToolPackageVersion {
  version: string
  digest: string
  source_repository: string
  source_revision: string
  note: string
  actor: string
  analysis: WdlAnalysis
  file_count: number
  files: WdlSourceFile[]
  created_at: string
}

export interface WdlToolPackageAuditEvent {
  id: number
  action: string
  actor: string
  note: string
  changes: Record<string, any>
  version?: string | null
  created_at: string
}

export interface WdlToolPackage {
  slug: string
  name: string
  description: string
  lifecycle: 'active' | 'archived'
  tags: string[]
  created_by: string
  created_at: string
  updated_at: string
  version_count: number
  reference_count: number
  latest_version?: WdlToolPackageVersion | null
  versions?: WdlToolPackageVersion[]
  audit_events?: WdlToolPackageAuditEvent[]
  references?: WdlToolPackageReference[]
}

export interface WdlToolPackageReference {
  asset_slug: string
  asset_name: string
  asset_lifecycle: WdlAsset['lifecycle']
  revision: number
  package_version: string
  mount_prefix: string
  digest: string
  created_at: string
}

export interface WdlToolPackageTag {
  id: number
  name: string
  package_count: number
}
