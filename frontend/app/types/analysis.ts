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

export interface AnalysisWorkflow {
  slug: string
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
}

export interface AnalysisRequirement {
  path: string
  label: string
  kind: 'file' | 'directory'
  present: boolean
}

export interface AnalysisDatabaseOption {
  id: string
  name: string
  reference?: string
  ref_version?: string
  ready: boolean
  requirements: AnalysisRequirement[]
  missing: AnalysisRequirement[]
}

export interface AnalysisCatalog {
  rawdata_directory: string
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

export type AnalysisRunStatus = 'queued' | 'preparing' | 'running' | 'succeeded' | 'failed'

export interface AnalysisRun {
  id: string
  workflow: {
    slug: string
    name: string
    workflow_name: string
    revision: number
    digest: string
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
    reference_name: string
    panel_name: string
    sample_type: string
    sample_gender: string
  }
  error: string
  outputs: AnalysisRunOutput[]
  events?: AnalysisRunEvent[]
  created_at: string
  started_at: string | null
  finished_at: string | null
  updated_at: string
}
