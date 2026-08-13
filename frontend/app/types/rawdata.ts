export interface RawdataIssue {
  code: string
  message: string
  path?: string
}

export interface RawdataFile {
  mate?: 1 | 2
  name: string
  relative_path: string
  size: number
  size_label: string
  modified_at: string
}

export interface RawdataDataset {
  id: string
  name: string
  pair_key: string
  directory: string
  status: 'ready' | 'issue' | 'scan_incomplete'
  missing_mates: Array<1 | 2>
  issues: RawdataIssue[]
  files: RawdataFile[]
  total_size: number
  total_size_label: string
  first_seen_at?: string
  last_seen_at?: string
  last_changed_at?: string
  run_count?: number
  recent_runs?: Array<{
    id: string
    status: string
    created_at: string
  }>
}

export interface RawdataDirectory {
  path: string
  dataset_count: number
  ready_count: number
  issue_count: number
  unrecognized_count: number
  total_size: number
  total_size_label: string
}

export interface RawdataCatalog {
  root_directory: string
  root_status: 'ready' | 'missing' | 'unreadable' | 'indexing'
  scanned_at?: string | null
  scan_limited: boolean
  scan_limit: number
  scan_entry_limit: number
  scanned_entry_count: number
  summary: {
    file_count: number
    dataset_count: number
    ready_dataset_count: number
    issue_dataset_count: number
    unrecognized_fastq_count: number
    total_size: number
    total_size_label: string
  }
  directories: RawdataDirectory[]
  datasets: RawdataDataset[]
  unrecognized_files: RawdataFile[]
  issues: RawdataIssue[]
  index: {
    latest_scan_id?: string | null
    latest_status?: 'succeeded' | 'limited' | 'failed' | null
    snapshot_scan_id?: string | null
    active_scan_id?: string | null
    active_status?: 'queued' | 'running' | null
    queued_at?: string | null
    started_at?: string | null
    finished_at?: string | null
    stale: boolean
    policy: {
      max_files: number
      max_entries: number
      max_depth: number
      batch_entries: number
    }
    repair_suggestions: string[]
  }
}
