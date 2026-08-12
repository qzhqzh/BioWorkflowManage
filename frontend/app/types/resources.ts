export interface ResourceRequirement {
  path: string
  label: string
  kind: 'file' | 'directory' | 'configuration'
  alternatives?: string[]
  sha256?: string
  present?: boolean
  reason?: 'missing' | 'unconfigured' | 'checksum_mismatch' | 'constraint_mismatch' | ''
  binding?: string
  expected?: string[]
}

export interface ResourceBindingDefinition {
  key: string
  label: string
  kind: 'file' | 'directory'
  basename_includes?: string[]
}

export interface ResourceCatalogEntry {
  id: string
  name: string
  description?: string
  ref_version?: string
  resource_version?: string
  reference?: string
  workflow_ids?: string[]
  directories?: Record<string, string>
  bindings?: Record<string, string | string[]>
  required_bindings?: ResourceBindingDefinition[]
  required: ResourceRequirement[]
  ready?: boolean
  requirements?: ResourceRequirement[]
  missing?: ResourceRequirement[]
}

export interface ResourceCatalogDocument {
  schema_version: 1
  references: ResourceCatalogEntry[]
  panels: ResourceCatalogEntry[]
}

export interface ResourceCatalogRevision {
  version: number
  digest: string
  actor: string
  note: string
  changes: Record<string, {
    created: string[]
    updated: string[]
    deleted: string[]
  }>
  created_at: string
}

export interface ResourceCatalogPayload {
  document: ResourceCatalogDocument
  references: ResourceCatalogEntry[]
  panels: ResourceCatalogEntry[]
  version: number
  digest: string
  source: 'file' | 'managed'
  updated_by: string
  updated_at?: string | null
  summary: {
    reference_count: number
    ready_reference_count: number
    panel_count: number
    ready_panel_count: number
    missing_count: number
  }
  revisions: ResourceCatalogRevision[]
}
