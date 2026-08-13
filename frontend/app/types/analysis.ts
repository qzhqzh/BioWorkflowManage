export interface AnalysisFastqFile {
  mate: 1 | 2
  name: string
  relative_path: string
  size: number
  size_label: string
}

export interface AnalysisDataset {
  id: string
  name: string
  pair_key: string
  files: AnalysisFastqFile[]
  total_size: number
  total_size_label: string
}

export interface AnalysisWorkflowGraphSummary {
  node_count: number
  edge_count: number
  input_count: number
  tool_count: number
  subworkflow_count: number
  output_count: number
  tools: Array<{ id: string; name: string; version: string }>
  subworkflows: Array<{ slug: string; name: string; version: number | null }>
}

export interface AnalysisWorkflow {
  slug: string
  source_slug?: string
  name: string
  workflow_name: string
  mode: 'single' | 'paired'
  description: string
  asset_name: string
  revision: number | null
  digest: string
  ready: boolean
  diagnostic_count: number
  blockers: string[]
  source_type: 'wdl_asset' | 'workflow_version'
  requires_reference: boolean
  requires_panel: boolean
  required_reference?: string
  reference_status?: Record<string, AnalysisDatabaseOption>
  panel_status?: Record<string, AnalysisDatabaseOption>
  input_adapter_status?: {
    status: 'ready' | 'pending'
    unresolved_inputs: string[]
    external_resource_count: number
    external_resource_examples: string[]
  }
  graph_summary?: AnalysisWorkflowGraphSummary
}

export interface AnalysisRequirement {
  path: string
  label: string
  kind: 'file' | 'directory' | 'configuration'
  present: boolean
  binding?: string
  reason?: 'missing' | 'unconfigured' | 'checksum_mismatch' | 'constraint_mismatch' | ''
  expected?: string[]
}

export interface AnalysisDatabaseOption {
  id: string
  name: string
  reference?: string
  ref_version?: string
  workflow_ids?: string[]
  ready: boolean
  requirements: AnalysisRequirement[]
  missing: AnalysisRequirement[]
}

export interface AnalysisCatalog {
  rawdata_directory: string
  rawdata_scan?: {
    limited: boolean
    scanned_at: string
    issues: Array<{ code: string; message: string; path?: string }>
  }
  database_directory: string
  datasets: AnalysisDataset[]
  workflows: AnalysisWorkflow[]
  database: {
    schema_version: number
    references: AnalysisDatabaseOption[]
    panels: AnalysisDatabaseOption[]
    error: { code: string; message: string } | null
  }
}

export interface AnalysisRunOutput {
  key: string
  kind: 'file' | 'value'
  name?: string
  size?: number
  size_label?: string
  download_url?: string
  value?: unknown
}

export interface AnalysisRunEvent {
  id: number
  kind: string
  level: string
  message: string
  details: Record<string, unknown>
  created_at: string
}

export interface AnalysisTaskTiming {
  id: string
  name: string
  call: string
  status: 'running' | 'succeeded' | 'failed'
  cached: boolean
  offset_seconds: number
  duration_seconds: number
}

export interface AnalysisRunTiming {
  queue_seconds?: number
  total_seconds?: number
  execution_seconds?: number
  task_seconds?: number
  cached_tasks?: number
  tasks: AnalysisTaskTiming[]
}

export type AnalysisRunStatus = 'queued' | 'preparing' | 'running' | 'cancel_requested' | 'succeeded' | 'failed' | 'canceled'

export interface AnalysisRun {
  id: string
  workflow: {
    slug: string
    name: string
    workflow_name: string
    revision: number
    digest: string
    source_type: 'wdl_asset' | 'workflow_version'
    graph_summary?: AnalysisWorkflowGraphSummary
  }
  sample_id: string
  sample_name: string
  actor: string
  status: AnalysisRunStatus
  progress: number
  current_step: string
  request: {
    dataset_name: string
    control_dataset_name?: string | null
    reference_name?: string | null
    panel_name?: string | null
    sample_type?: string
    sample_gender?: string
  }
  error: string
  outputs: AnalysisRunOutput[]
  timing: AnalysisRunTiming
  events?: AnalysisRunEvent[]
  created_at: string
  started_at: string | null
  finished_at: string | null
  updated_at: string
}
