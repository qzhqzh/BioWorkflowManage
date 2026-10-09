import type { AnalysisRun, AnalysisRunEvent, AnalysisRunTiming } from './analysis'

export interface ProductReference {
  analysis_code: string
  contract_version: string
  contract_digest: string
}
export interface ProductPort {
  name: string
  label?: string
  required?: boolean
  wdl_type?: string
  semantic_type?: string
}
export interface ProductResource {
  name: string
  kind: string
  root_alias: string
  relative_path: string
  semantic_type?: string
}
export interface AnalysisProduct extends ProductReference {
  name: string
  description: string
  active: boolean
  ready: boolean
  blockers: string[]
  workflow: { version_id: number; slug: string; version: number; source_digest: string }
  interface: { resources?: ProductResource[] }
  input_contract: ProductPort[]
  output_contract: ProductPort[]
  created_at: string
}
export interface IntegrationOutput {
  key: string
  name?: string
  filename?: string
  kind: string
  semantic_type?: string
  size?: number
  sha256?: string
  available?: boolean
  download_url?: string
  value?: unknown
  reason?: string
}
export interface IntegrationRun {
  id: string
  external_ref: { client_id: string; external_run_id: string; external_analysis_id?: string }
  analysis_product: ProductReference | null
  workflow: { version_id?: number; slug?: string; version: number | string; source_digest: string }
  status: string
  status_version: number
  output_status: string
  current_step: string
  progress: number
  attempt: number
  retry_of: string | null
  actor: string
  error: { code: string; message: string; category?: string; details?: unknown; retryable?: boolean } | null
  outputs: IntegrationOutput[]
  timing?: AnalysisRunTiming
  created_at: string
  started_at: string | null
  finished_at: string | null
  updated_at: string
}
export interface ConsoleRun extends IntegrationRun {
  sampleLabel: string
  workflowLabel: string
  engineLabel: string
}
export interface RunMetadata extends AnalysisRun { execution_engine?: string }
export interface RunEvents {
  results: AnalysisRunEvent[]
  next_after_id: number
  status?: string
}
export interface BusinessLink {
  clientId: string
  name: string
  analysisUrlTemplate: string
}
