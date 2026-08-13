<script setup lang="ts">
import { createTwoFilesPatch, diffArrays } from 'diff'
import AppRail from '~/components/layout/AppRail.vue'
import AppTopbar from '~/components/layout/AppTopbar.vue'
import WdlCodeEditor from '~/components/wdl/WdlCodeEditor.vue'
import WdlCollaborationPanel from '~/components/wdl/WdlCollaborationPanel.vue'
import WdlFileTree from '~/components/wdl/WdlFileTree.vue'
import WdlPackageReferencePanel from '~/components/wdl/WdlPackageReferencePanel.vue'
import type {
  WdlAsset,
  WdlAuditEvent,
  WdlSourceFile,
  WdlSourcePackageReference,
  WdlSourceRevision,
  WdlTag,
  WdlToolPackageVersion,
} from '~/types/wdl'

const { $api: $fetch } = useNuxtApp()
const { navigateSection } = useAppNavigation()

type InspectorTab = 'structure' | 'diagnostics' | 'collaboration' | 'diff' | 'history'
type WdlCodeEditorHandle = {
  applyFormattedValue: (value: string) => void
  revealLine: (line: number) => void
}
type MetadataField = 'name' | 'description'
type MetadataPatch = Partial<Pick<WdlAsset, 'name' | 'description' | 'tags'>> & {
  note: string
}
type RevisionConflict = {
  currentRevision: WdlSourceRevision
  actor?: string | null
  note?: string
  updatedAt?: string | null
}
type MetadataConflict = {
  actor?: string | null
  note?: string
  updatedAt?: string | null
}
type LineChange = {
  start: number
  end: number
  replacement: string[]
}

const route = useRoute()
const slug = computed(() => String(route.params.slug))
const asset = ref<WdlAsset>()
const availableTags = ref<WdlTag[]>([])
const selectedRevision = ref<WdlSourceRevision>()
const sourceFiles = ref<WdlSourceFile[]>([])
const packageReferences = ref<WdlSourcePackageReference[]>([])
const basePackageReferences = ref<WdlSourcePackageReference[]>([])
const fileContents = ref<Record<string, string>>({})
const baseFileContents = ref<Record<string, string>>({})
const activeFilePath = ref('')
const pendingOperation = ref<'edit' | 'format'>('edit')
const revisionNote = ref('')
const editingMetadataField = ref<MetadataField>()
const assetNameDraft = ref('')
const assetDescriptionDraft = ref('')
const tagDraft = ref('')
const inspectorTab = ref<InspectorTab>('structure')
const selectedEvent = ref<WdlAuditEvent>()
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const saveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
const metadataState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
const feedback = ref('')
const revisionConflict = ref<RevisionConflict>()
const metadataConflict = ref<MetadataConflict>()
const lifecycleLoadState = ref<'loading' | 'ready' | 'error'>('loading')
const directRunDestination = ref<{
  ready: boolean
  blockers: string[]
  revision: number
  digest: string
}>()
const derivedPackages = ref<Array<{
  slug: string
  name: string
  latest_version?: { version: string } | null
}>>([])
const codeEditor = ref<WdlCodeEditorHandle>()
const cursorLine = ref(1)
const collaborationComposerRequest = ref(0)
const assetNameInput = ref<HTMLInputElement>()
const assetDescriptionInput = ref<HTMLTextAreaElement>()
const revisionNoteInput = ref<HTMLInputElement>()
let metadataSaveQueue = Promise.resolve()
let metadataQueueGeneration = 0
const metadataQueueBlocked = ref(false)

const activeSourceFile = computed(() =>
  sourceFiles.value.find(file => file.path === activeFilePath.value),
)
const isActiveFileReadOnly = computed(() =>
  Boolean(isHistoricalRevision.value || activeSourceFile.value?.read_only),
)
const content = computed({
  get: () => fileContents.value[activeFilePath.value] ?? '',
  set: (value: string) => {
    if (activeFilePath.value && !isActiveFileReadOnly.value) {
      fileContents.value[activeFilePath.value] = value
    }
  },
})
const baseContent = computed(() => baseFileContents.value[activeFilePath.value] ?? '')
const latestVersion = computed(() => asset.value?.current_revision?.version)
const isHistoricalRevision = computed(
  () => selectedRevision.value?.version !== latestVersion.value,
)
const editablePaths = computed(() => new Set(
  sourceFiles.value.filter(file => !file.read_only).map(file => file.path),
))
const dirtyPaths = computed(() =>
  [...new Set([...Object.keys(baseFileContents.value), ...Object.keys(fileContents.value)])]
    .filter(path => editablePaths.value.has(path))
    .filter(path => baseFileContents.value[path] !== fileContents.value[path]),
)
const referencesDirty = computed(() =>
  JSON.stringify(packageReferences.value.map(referenceKey).sort())
  !== JSON.stringify(basePackageReferences.value.map(referenceKey).sort()),
)
const dirty = computed(() => dirtyPaths.value.length > 0 || referencesDirty.value)
const mergeConflictPaths = computed(() => sourceFiles.value
  .filter(file => !file.read_only)
  .map(file => file.path)
  .filter(path => (fileContents.value[path] ?? '').includes('<<<<<<< 本地草稿')))
const isRawImportedRevision = computed(
  () => selectedRevision.value?.operation === 'import' && !dirty.value,
)
const analysis = computed(() => selectedRevision.value?.analysis)
const auditEvents = computed(() => asset.value?.audit_events ?? [])
const popularTags = computed(() => availableTags.value.slice(0, 3))
const sourceWorkflowOrigin = computed(() => {
  const source = asset.value?.source_repository ?? ''
  const match = source.match(/^bioworkflow:\/\/editor\/workflows\/([A-Za-z_][A-Za-z0-9_]*)$/)
  if (!match) return undefined
  const versionMatch = (asset.value?.source_revision ?? '').match(/workflow-v(\d+)/)
  const version = versionMatch ? Number(versionMatch[1]) : undefined
  const wdlVersionMatch = (asset.value?.source_revision ?? '').match(/wdl-v(\d+)/)
  const wdlVersion = wdlVersionMatch ? Number(wdlVersionMatch[1]) : undefined
  return {
    slug: match[1]!,
    version,
    wdlVersion,
    to: {
      path: '/',
      query: {
        section: 'artifacts',
        workflow: match[1]!,
        ...(wdlVersion
          ? { wdlVersion: String(wdlVersion) }
          : version ? { workflowVersion: String(version) } : {}),
      },
    },
  }
})
const changeDiff = computed(() => {
  if (!dirty.value) return ''
  const version = selectedRevision.value?.version ?? '—'
  const sourceDiff = dirtyPaths.value.map(path => createTwoFilesPatch(
    `${path} (v${version})`,
    `${path} (working)`,
    baseFileContents.value[path] ?? '',
    fileContents.value[path] ?? '',
    '', '', { context: 3 },
  )).join('\n')
  if (!referencesDirty.value) return sourceDiff
  const before = basePackageReferences.value.map(referenceLabel)
  const after = packageReferences.value.map(referenceLabel)
  return [
    sourceDiff,
    '--- tool-packages (saved)',
    '+++ tool-packages (working)',
    ...before.filter(item => !after.includes(item)).map(item => `-${item}`),
    ...after.filter(item => !before.includes(item)).map(item => `+${item}`),
  ].filter(Boolean).join('\n')
})
const importedTasks = computed(() => new Set(
  auditEvents.value
    .filter(event => event.action === 'tool_import')
    .map(event => `${event.changes.file_path}::${event.changes.task_name}`),
))
const packageCreationLink = computed(() => ({
  path: '/wdl-packages',
  query: {
    from: 'wdl',
    asset: slug.value,
    revision: String(selectedRevision.value?.version ?? latestVersion.value ?? 1),
  },
}))
const directRunLink = computed(() => ({
  path: '/runs',
  query: {
    workflow: slug.value,
    revision: String(
      directRunDestination.value?.revision
      ?? selectedRevision.value?.version
      ?? latestVersion.value
      ?? 1,
    ),
  },
}))
const taskImportState = ref<Record<string, 'saving' | 'done' | 'error'>>({})
const operationLabels: Record<WdlSourceRevision['operation'], string> = {
  import: '导入',
  edit: '编辑',
  format: '格式化',
  package_link: '引用工具包',
}
const actionLabels: Record<string, string> = {
  import: '导入源码',
  edit: '保存修改',
  format: '格式化源码',
  package_link: '引用工具包',
  metadata_update: '更新信息与标签',
  tool_import: '导入工具草稿',
  review_requested: '发起评审',
  review_reassigned: '转交评审',
  review_approved: '评审通过',
  review_changes_requested: '要求修改',
  review_cancelled: '取消评审',
  review_comment_added: '添加评审评论',
  review_thread_resolve: '解决讨论',
  review_thread_reopen: '重新打开讨论',
  source_conflict: '记录源码冲突',
  source_conflict_resolved: '解决源码冲突',
}

function referenceKey(reference: WdlSourcePackageReference) {
  return [
    reference.package_slug,
    reference.version,
    reference.digest,
    reference.mount_prefix,
  ].join(':')
}

function referenceLabel(reference: WdlSourcePackageReference) {
  return `${reference.package_slug}@${reference.version} → ${reference.mount_prefix || '.'}`
}

function sourceFileReferenceKey(file: WdlSourceFile) {
  const reference = file.package_reference
  if (!reference) return ''
  return [
    reference.package_slug,
    reference.version,
    reference.digest,
    reference.mount_prefix,
  ].join(':')
}

function formatConflictTimestamp(value?: string | null) {
  if (!value) return ''
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function lineChanges(base: string[], next: string[]): LineChange[] {
  const changes: LineChange[] = []
  const parts = diffArrays(base, next)
  let baseIndex = 0
  let partIndex = 0
  while (partIndex < parts.length) {
    const part = parts[partIndex]!
    if (!part.added && !part.removed) {
      baseIndex += part.value.length
      partIndex += 1
      continue
    }
    const start = baseIndex
    const replacement: string[] = []
    while (partIndex < parts.length) {
      const changed = parts[partIndex]!
      if (!changed.added && !changed.removed) break
      if (changed.removed) baseIndex += changed.value.length
      if (changed.added) replacement.push(...changed.value)
      partIndex += 1
    }
    changes.push({ start, end: baseIndex, replacement })
  }
  return changes
}

function sameLineChange(left: LineChange, right: LineChange) {
  return left.start === right.start
    && left.end === right.end
    && JSON.stringify(left.replacement) === JSON.stringify(right.replacement)
}

function lineChangesOverlap(left: LineChange, right: LineChange) {
  if (left.start === left.end && right.start === right.end) {
    return left.start === right.start
  }
  if (left.start === left.end) {
    return left.start >= right.start && left.start < right.end
  }
  if (right.start === right.end) {
    return right.start >= left.start && right.start < left.end
  }
  return Math.max(left.start, right.start) < Math.min(left.end, right.end)
}

function mergeWdlContent(base: string, local: string, latest: string) {
  if (local === latest) return { content: local, conflicted: false }
  if (local === base) return { content: latest, conflicted: false }
  if (latest === base) return { content: local, conflicted: false }

  const baseLines = base.split('\n')
  const localChanges = lineChanges(baseLines, local.split('\n'))
  const latestChanges = lineChanges(baseLines, latest.split('\n'))
  const conflicted = localChanges.some(localChange => latestChanges.some(
    latestChange => lineChangesOverlap(localChange, latestChange)
      && !sameLineChange(localChange, latestChange),
  ))
  if (conflicted) {
    return {
      content: [
        '<<<<<<< 本地草稿',
        local,
        '||||||| 原基线',
        base,
        '=======',
        latest,
        '>>>>>>> 最新版本',
      ].join('\n'),
      conflicted: true,
    }
  }

  const merged = [...baseLines]
  const changes = [...localChanges, ...latestChanges]
    .filter((change, index, all) => all.findIndex(item => sameLineChange(item, change)) === index)
    .sort((left, right) => right.start - left.start)
  for (const change of changes) {
    merged.splice(change.start, change.end - change.start, ...change.replacement)
  }
  return { content: merged.join('\n'), conflicted: false }
}

function referenceRequestPayload() {
  return packageReferences.value.map(reference => ({
    package_slug: reference.package_slug,
    version: reference.version,
    digest: reference.digest,
    mount_prefix: reference.mount_prefix,
  }))
}

function localFileRequestPayload() {
  return sourceFiles.value
    .filter(file => !file.read_only && file.origin !== 'package')
    .map(file => ({ path: file.path, content: fileContents.value[file.path] ?? '' }))
}

function applyRevision(revision: WdlSourceRevision) {
  selectedRevision.value = revision
  const files = revision.files?.length
    ? revision.files
    : [{ path: asset.value?.source_filename || 'workflow.wdl', content: revision.content ?? '', digest: revision.digest, is_entry: true }]
  sourceFiles.value = files.map(file => ({ ...file }))
  packageReferences.value = (revision.package_references ?? []).map(reference => ({
    ...reference,
    files: reference.files.map(file => ({ ...file })),
  }))
  basePackageReferences.value = packageReferences.value.map(reference => ({
    ...reference,
    files: reference.files.map(file => ({ ...file })),
  }))
  const nextContents = Object.fromEntries(files.map(file => [file.path, file.content ?? '']))
  fileContents.value = { ...nextContents }
  baseFileContents.value = { ...nextContents }
  activeFilePath.value = revision.entrypoint
    || files.find(file => file.is_entry)?.path
    || files[0]?.path
    || ''
  pendingOperation.value = 'edit'
  revisionNote.value = ''
  revisionConflict.value = undefined
  feedback.value = ''
  if (inspectorTab.value === 'diff') inspectorTab.value = 'structure'
}

async function loadAsset() {
  loadState.value = 'loading'
  try {
    const [assetResponse, tagResponse] = await Promise.all([
      $fetch<WdlAsset>(`/api/v1/wdl-assets/${encodeURIComponent(slug.value)}`),
      $fetch<{ results: WdlTag[] }>('/api/v1/wdl-assets/tags'),
    ])
    asset.value = assetResponse
    availableTags.value = tagResponse.results
    if (assetResponse.current_revision) applyRevision(assetResponse.current_revision)
    const requestedRevision = Number(route.query.revision)
    if (
      Number.isInteger(requestedRevision)
      && requestedRevision > 0
      && requestedRevision !== assetResponse.current_revision?.version
    ) {
      await loadRevision(requestedRevision, false)
    }
    void loadLifecycleDestinations(
      assetResponse,
      selectedRevision.value?.version ?? assetResponse.current_revision?.version,
    )
    selectedEvent.value = assetResponse.audit_events?.[0]
    loadState.value = 'ready'
  } catch {
    loadState.value = 'error'
  }
}

let lifecycleLoadSequence = 0

async function loadLifecycleDestinations(
  currentAsset: WdlAsset,
  revisionVersion?: number,
) {
  const sequence = ++lifecycleLoadSequence
  lifecycleLoadState.value = 'loading'
  directRunDestination.value = undefined
  try {
    const [catalog, packageResponse] = await Promise.all([
      $fetch<{
        workflows: Array<{
          slug: string
          source_slug?: string
          source_type: 'wdl_asset' | 'workflow_version'
          revision: number
          digest: string
          ready: boolean
          blockers?: string[]
        }>
      }>('/api/v1/analysis/catalog', {
        query: {
          workflow: currentAsset.slug,
          ...(revisionVersion ? { revision: revisionVersion } : {}),
        },
      }),
      $fetch<{ results: Array<{
        slug: string
        name: string
        latest_version?: {
          version: string
          source_repository?: string
        } | null
      }> }>('/api/v1/wdl-packages', { query: { lifecycle: 'active' } }),
    ])
    if (sequence !== lifecycleLoadSequence) return
    const runDestination = catalog.workflows.find(
      item => item.source_type === 'wdl_asset'
        && (item.source_slug ?? item.slug) === currentAsset.slug
        && (!revisionVersion || item.revision === revisionVersion),
    )
    directRunDestination.value = runDestination
      ? {
          ready: runDestination.ready,
          blockers: runDestination.blockers ?? [],
          revision: runDestination.revision,
          digest: runDestination.digest,
        }
      : undefined
    const sourceRepository = `bioworkflow://wdl-assets/${currentAsset.slug}`
    derivedPackages.value = packageResponse.results.filter(
      item => item.latest_version?.source_repository === sourceRepository,
    )
    lifecycleLoadState.value = 'ready'
  } catch {
    if (sequence !== lifecycleLoadSequence) return
    lifecycleLoadState.value = 'error'
  }
}

async function loadRevision(version: number, refreshLifecycle = true) {
  if (dirty.value && !window.confirm('当前未保存的修改会丢失，继续切换版本吗？')) return
  try {
    const revision = await $fetch<WdlSourceRevision>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/revisions/${version}`,
    )
    applyRevision(revision)
    if (refreshLifecycle && asset.value) {
      void loadLifecycleDestinations(asset.value, revision.version)
    }
  } catch {
    feedback.value = `WDL v${version} 读取失败。`
  }
}

async function returnToLatest() {
  if (!latestVersion.value) return
  await loadRevision(latestVersion.value)
}

async function formatSource() {
  if (isActiveFileReadOnly.value || !content.value.trim()) return
  saveState.value = 'saving'
  feedback.value = ''
  try {
    const result = await $fetch<{
      content: string
      changed: boolean
      diff: string
    }>(`/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/format`, {
      method: 'POST',
      body: { content: content.value, filename: activeFilePath.value },
    })
    if (!result.changed) {
      feedback.value = '当前源码已经符合格式。'
      if (inspectorTab.value === 'diff') inspectorTab.value = 'structure'
      saveState.value = 'idle'
      return
    }
    if (codeEditor.value) {
      codeEditor.value.applyFormattedValue(result.content)
    } else {
      content.value = result.content
    }
    inspectorTab.value = 'diff'
    pendingOperation.value = 'format'
    revisionNote.value ||= '统一 WDL 格式'
    feedback.value = '格式化结果已应用到编辑器，保存后会形成新版本。'
    saveState.value = 'idle'
  } catch (error: any) {
    saveState.value = 'error'
    inspectorTab.value = 'diagnostics'
    feedback.value = error?.data?.error?.message ?? '格式化失败；请先修复语法错误。'
  }
}

async function saveRevision() {
  if (!dirty.value || isHistoricalRevision.value) return
  if (mergeConflictPaths.value.length) {
    saveState.value = 'error'
    feedback.value = `请先处理冲突标记：${mergeConflictPaths.value.join('、')}`
    return
  }
  if (!revisionNote.value.trim()) {
    saveState.value = 'error'
    feedback.value = '请先填写本次修改备注。'
    void nextTick(() => revisionNoteInput.value?.focus())
    return
  }
  saveState.value = 'saving'
  feedback.value = ''
  try {
    const revision = await $fetch<WdlSourceRevision>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/revisions`,
      {
        method: 'POST',
        body: {
          files: localFileRequestPayload(),
          package_references: referenceRequestPayload(),
          entrypoint: selectedRevision.value?.entrypoint || asset.value?.source_filename,
          operation: pendingOperation.value,
          note: revisionNote.value.trim(),
          base_version: selectedRevision.value?.version,
          base_digest: selectedRevision.value?.digest,
        },
      },
    )
    syncCurrentRevision(revision)
    applyRevision(revision)
    let visibleRevision = revision
    try {
      const refreshed = await $fetch<WdlAsset>(
        `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}`,
      )
      asset.value = refreshed
      selectedEvent.value = refreshed.audit_events?.[0]
      if (
        refreshed.current_revision
        && refreshed.current_revision.version >= revision.version
      ) {
        visibleRevision = refreshed.current_revision
        applyRevision(visibleRevision)
      }
    } catch {
      // The immutable revision is already saved; keep it usable if refresh fails.
    }
    saveState.value = 'saved'
    if (asset.value) {
      void loadLifecycleDestinations(asset.value, visibleRevision.version)
    }
    feedback.value = visibleRevision.version > revision.version
      ? `WDL v${revision.version} 已保存；已载入最新版本 v${visibleRevision.version}。`
      : `WDL v${revision.version} 已保存并写入操作历史。`
    window.setTimeout(() => {
      if (saveState.value === 'saved') saveState.value = 'idle'
    }, 2200)
  } catch (error: any) {
    saveState.value = 'error'
    if (
      error?.data?.error?.code === 'WDL_REVISION_CONFLICT'
      && error.data.current_revision
    ) {
      revisionConflict.value = {
        currentRevision: error.data.current_revision,
        actor: error.data.error.details?.actor,
        note: error.data.error.details?.note,
        updatedAt: error.data.error.details?.updated_at,
      }
      inspectorTab.value = 'diff'
      feedback.value = ''
      return
    }
    feedback.value = error?.data?.error?.message ?? 'WDL 版本保存失败。'
  }
}

function syncCurrentRevision(revision: WdlSourceRevision) {
  if (!asset.value) return
  const revisions = [
    revision,
    ...(asset.value.revisions ?? []).filter(item => item.version !== revision.version),
  ].sort((left, right) => right.version - left.version)
  asset.value = {
    ...asset.value,
    current_revision: revision,
    revision_count: Math.max(asset.value.revision_count, revision.version),
    revisions,
  }
}

async function refreshAssetAudit() {
  if (!asset.value) return
  try {
    const refreshed = await $fetch<WdlAsset>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}`,
    )
    asset.value = {
      ...asset.value,
      audit_events: refreshed.audit_events,
      attention: refreshed.attention,
      collaboration: refreshed.collaboration,
      latest_activity: refreshed.latest_activity,
    }
    selectedEvent.value = refreshed.audit_events?.[0]
  } catch {
    // Source state is already synchronized; history can refresh on the next load.
  }
}

function loadConflictRevision() {
  const latest = revisionConflict.value?.currentRevision
  if (!latest) return
  if (!window.confirm('载入最新版本会丢弃当前本地草稿，继续吗？')) return
  syncCurrentRevision(latest)
  applyRevision(latest)
  saveState.value = 'idle'
  feedback.value = `已载入最新版本 v${latest.version}。`
  void refreshAssetAudit()
}

function rebaseConflictDraft() {
  const latest = revisionConflict.value?.currentRevision
  if (!latest) return
  const draftFiles = sourceFiles.value.map(file => ({ ...file }))
  const draftContents = { ...fileContents.value }
  const draftBaseContents = { ...baseFileContents.value }
  const draftDirtyPaths = [...dirtyPaths.value]
  const draftReferences = packageReferences.value.map(reference => ({
    ...reference,
    files: reference.files.map(file => ({ ...file })),
  }))
  const draftBaseReferences = basePackageReferences.value.map(reference => ({
    ...reference,
    files: reference.files.map(file => ({ ...file })),
  }))
  const draftActivePath = activeFilePath.value
  const draftReferencesDirty = referencesDirty.value
  const draftOperation = pendingOperation.value
  const draftNote = revisionNote.value

  syncCurrentRevision(latest)
  applyRevision(latest)
  for (const path of draftDirtyPaths) {
    const draftFile = draftFiles.find(file => file.path === path)
    if (draftFile && !sourceFiles.value.some(file => file.path === path)) {
      sourceFiles.value = [...sourceFiles.value, draftFile]
    }
    const merged = mergeWdlContent(
      draftBaseContents[path] ?? '',
      draftContents[path] ?? '',
      fileContents.value[path] ?? '',
    )
    fileContents.value[path] = merged.content
  }
  if (draftReferencesDirty) {
    const draftReferenceKeys = new Set(draftReferences.map(referenceKey))
    const locallyRemovedKeys = new Set(
      draftBaseReferences
        .map(referenceKey)
        .filter(key => !draftReferenceKeys.has(key)),
    )
    if (locallyRemovedKeys.size) {
      packageReferences.value = packageReferences.value.filter(
        reference => !locallyRemovedKeys.has(referenceKey(reference)),
      )
      sourceFiles.value = sourceFiles.value.filter(
        file => !locallyRemovedKeys.has(sourceFileReferenceKey(file)),
      )
    }
    const occupiedFiles = new Map(sourceFiles.value.map(file => [file.path, file.digest]))
    const draftBaseReferenceKeys = new Set(draftBaseReferences.map(referenceKey))
    for (const reference of draftReferences) {
      if (draftBaseReferenceKeys.has(referenceKey(reference))) continue
      if (packageReferences.value.some(item => referenceKey(item) === referenceKey(reference))) {
        continue
      }
      const referenceFiles = draftFiles.filter(
        file => file.package_reference
          && file.package_reference.package_slug === reference.package_slug
          && file.package_reference.version === reference.version
          && file.package_reference.mount_prefix === reference.mount_prefix,
      )
      const pathConflict = referenceFiles.some(
        file => occupiedFiles.has(file.path) && occupiedFiles.get(file.path) !== file.digest,
      )
      if (pathConflict) {
        const entrypoint = selectedRevision.value?.entrypoint || activeFilePath.value
        fileContents.value[entrypoint] = [
          '<<<<<<< 本地草稿：工具包引用冲突',
          fileContents.value[entrypoint] ?? '',
          '=======',
          '# 请调整工具包挂载目录后删除冲突标记',
          '>>>>>>> 最新版本',
        ].join('\n')
        continue
      }
      const newReferenceFiles = referenceFiles.filter(file => !occupiedFiles.has(file.path))
      packageReferences.value = [...packageReferences.value, reference]
      sourceFiles.value = [...sourceFiles.value, ...newReferenceFiles]
      for (const file of newReferenceFiles) {
        fileContents.value[file.path] = file.content ?? ''
        occupiedFiles.set(file.path, file.digest)
      }
    }
  }
  activeFilePath.value = sourceFiles.value.some(file => file.path === draftActivePath)
    ? draftActivePath
    : activeFilePath.value
  pendingOperation.value = draftOperation
  revisionNote.value = draftNote
  revisionConflict.value = undefined
  inspectorTab.value = 'diff'
  saveState.value = 'idle'
  feedback.value = mergeConflictPaths.value.length
    ? `已合并 v${latest.version}；${mergeConflictPaths.value.join('、')} 需要手动处理冲突标记。`
    : `已合并 v${latest.version} 的更新并保留本地修改。请检查差异后保存。`
  void refreshAssetAudit()
}

function beginMetadataEdit(field: MetadataField) {
  if (!asset.value || metadataQueueBlocked.value) return
  editingMetadataField.value = field
  if (field === 'name') {
    assetNameDraft.value = asset.value.name
  } else {
    assetDescriptionDraft.value = asset.value.description
  }
  void nextTick(() => {
    const input = field === 'name' ? assetNameInput.value : assetDescriptionInput.value
    input?.focus()
    input?.select()
  })
}

function cancelMetadataEdit() {
  editingMetadataField.value = undefined
}

async function refreshTagPool() {
  try {
    const response = await $fetch<{ results: WdlTag[] }>('/api/v1/wdl-assets/tags')
    availableTags.value = response.results
  } catch {
    // Metadata is already saved; stale suggestions can refresh on the next page load.
  }
}

function queueMetadataPatch(
  buildBody: (current: WdlAsset) => MetadataPatch,
  successMessage: string,
) {
  const generation = metadataQueueGeneration
  let resolveResult: (saved: boolean) => void = () => undefined
  const result = new Promise<boolean>((resolve) => {
    resolveResult = resolve
  })
  metadataSaveQueue = metadataSaveQueue.then(async () => {
    if (!asset.value || metadataQueueBlocked.value || generation !== metadataQueueGeneration) {
      resolveResult(false)
      return
    }
    const saved = await saveMetadataPatch(
      buildBody(asset.value),
      successMessage,
    )
    resolveResult(saved)
  })
  return result
}

async function saveMetadataPatch(body: MetadataPatch, successMessage: string) {
  if (!asset.value) return false
  metadataState.value = 'saving'
  try {
    const updated = await $fetch<WdlAsset>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}`,
      {
        method: 'PATCH',
        body: {
          ...body,
          base_metadata_version: asset.value.metadata_version,
        },
      },
    )
    asset.value = updated
    selectedEvent.value = updated.audit_events?.[0]
    metadataConflict.value = undefined
    metadataState.value = 'saved'
    feedback.value = successMessage
    if (body.tags) await refreshTagPool()
    return true
  } catch (error: any) {
    metadataState.value = 'error'
    if (
      error?.data?.error?.code === 'WDL_METADATA_CONFLICT'
      && error.data.current_asset
    ) {
      asset.value = error.data.current_asset
      selectedEvent.value = error.data.current_asset.audit_events?.[0]
      metadataConflict.value = {
        actor: error.data.error.details?.actor,
        note: error.data.error.details?.note,
        updatedAt: error.data.error.details?.updated_at,
      }
      metadataQueueBlocked.value = true
      metadataQueueGeneration += 1
      feedback.value = ''
      return false
    }
    feedback.value = 'WDL 信息自动保存失败，请重试。'
    return false
  }
}

function resumeMetadataEditing() {
  metadataQueueBlocked.value = false
  metadataConflict.value = undefined
  metadataState.value = 'idle'
}

async function commitMetadataEdit(field: MetadataField) {
  if (editingMetadataField.value !== field || !asset.value) return
  editingMetadataField.value = undefined
  const original = asset.value[field]
  const value = (
    field === 'name' ? assetNameDraft.value : assetDescriptionDraft.value
  ).trim()
  if (field === 'name' && !value) {
    feedback.value = 'WDL 标题不能为空，已保留原值。'
    return
  }
  if (value === original) return

  const fieldLabel = field === 'name' ? '标题' : '说明'
  await queueMetadataPatch(
    () => ({ [field]: value, note: `自动更新${fieldLabel}` }),
    `${fieldLabel}已自动保存。`,
  )
}

function isTagSelected(name: string) {
  return asset.value?.tags.some(tag => tag.toLocaleLowerCase() === name.toLocaleLowerCase()) ?? false
}

async function addTag(name: string) {
  if (!asset.value || metadataQueueBlocked.value) return
  const trimmed = name.trim()
  tagDraft.value = ''
  if (!trimmed || isTagSelected(trimmed)) return
  const canonical = availableTags.value.find(
    tag => tag.name.toLocaleLowerCase() === trimmed.toLocaleLowerCase(),
  )?.name ?? trimmed
  await queueMetadataPatch(
    current => ({
      tags: current.tags.some(
        tag => tag.toLocaleLowerCase() === canonical.toLocaleLowerCase(),
      ) ? current.tags : [...current.tags, canonical],
      note: `添加标签 ${canonical}`,
    }),
    `标签“${canonical}”已自动保存。`,
  )
}

function commitTagDraft() {
  const value = tagDraft.value
  tagDraft.value = ''
  if (!value.trim()) return
  void addTag(value)
}

function blurCurrentTarget(event: Event) {
  if (event.currentTarget instanceof HTMLElement) event.currentTarget.blur()
}

async function removeTag(name: string) {
  if (!asset.value) return
  await queueMetadataPatch(
    current => ({
      tags: current.tags.filter(tag => tag !== name),
      note: `移除标签 ${name}`,
    }),
    `标签“${name}”已移除。`,
  )
}

function exportTimestamp(date = new Date()) {
  const pad = (value: number) => String(value).padStart(2, '0')
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
    '-',
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds()),
  ].join('')
}

function sanitizeDownloadName(value: string) {
  return value
    .normalize('NFKC')
    .replace(/\.wdl$/i, '')
    .replace(/[\\/:*?"<>|\u0000-\u001F]+/g, '-')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^[.-]+|[.-]+$/g, '')
    .slice(0, 120) || 'workflow'
}

async function exportWdl() {
  if (!asset.value || !content.value) return
  const version = selectedRevision.value?.version ?? 1
  const versionLabel = `v${version}${dirty.value ? '-draft' : ''}`
  const filename = [
    sanitizeDownloadName(asset.value.name),
    versionLabel,
    exportTimestamp(),
  ].join('-') + (sourceFiles.value.length > 1 ? '.zip' : '.wdl')
  const response = await $fetch<Blob>(
    `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/export`,
    {
      method: 'POST',
      responseType: 'blob',
      body: {
        version,
        files: localFileRequestPayload(),
        package_references: referenceRequestPayload(),
        entrypoint: selectedRevision.value?.entrypoint || asset.value.source_filename,
      },
    },
  )
  const url = URL.createObjectURL(response)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
  feedback.value = `已导出 ${filename}`
}

function relativeImportPath(fromPath: string, toPath: string) {
  const from = fromPath.split('/').slice(0, -1)
  const to = toPath.split('/')
  let common = 0
  while (common < from.length && common < to.length && from[common] === to[common]) {
    common += 1
  }
  return [
    ...Array.from({ length: from.length - common }, () => '..'),
    ...to.slice(common),
  ].join('/')
}

function importNamespace(path: string, used: Set<string>) {
  const base = (path.split('/').at(-1) ?? 'tools')
    .replace(/\.wdl$/i, '')
    .replace(/[^A-Za-z0-9_]+/g, '_')
    .replace(/^([0-9])/, '_$1') || 'tools'
  let candidate = base
  let suffix = 2
  while (used.has(candidate)) {
    candidate = `${base}_${suffix}`
    suffix += 1
  }
  used.add(candidate)
  return candidate
}

function insertPackageImports(reference: WdlSourcePackageReference, selectedPaths: string[]) {
  const entrypoint = selectedRevision.value?.entrypoint || asset.value?.source_filename || ''
  const entryContent = fileContents.value[entrypoint]
  if (!entrypoint || entryContent === undefined || !selectedPaths.length) return 0

  const existingUris = new Set(
    [...entryContent.matchAll(/^\s*import\s+"([^"]+)"/gm)].map(match => match[1]!),
  )
  const usedNamespaces = new Set(
    [...entryContent.matchAll(/^\s*import\s+"[^"]+"\s+as\s+([A-Za-z_][A-Za-z0-9_]*)/gm)]
      .map(match => match[1]!),
  )
  const imports = selectedPaths.flatMap((path) => {
    const mounted = reference.files.find(file => file.path === path)?.mounted_path
    if (!mounted) return []
    const uri = relativeImportPath(entrypoint, mounted)
    if (existingUris.has(uri)) return []
    existingUris.add(uri)
    return [`import "${uri}" as ${importNamespace(path, usedNamespaces)}`]
  })
  if (!imports.length) return 0

  const lines = entryContent.split('\n')
  const existingImportIndexes = lines.flatMap((line, index) =>
    /^\s*import\s+"/.test(line) ? [index] : [],
  )
  let insertAt = existingImportIndexes.length
    ? existingImportIndexes.at(-1)! + 1
    : Math.max(0, lines.findIndex(line => /^\s*version\s+/.test(line)) + 1)
  if (!existingImportIndexes.length) {
    while (insertAt < lines.length && !lines[insertAt]?.trim()) insertAt += 1
  }
  const insertion = existingImportIndexes.length ? imports : [...imports, '']
  lines.splice(insertAt, 0, ...insertion)
  fileContents.value[entrypoint] = lines.join('\n')
  return imports.length
}

function addPackageReference(payload: {
  reference: WdlSourcePackageReference
  version: WdlToolPackageVersion
  selectedPaths: string[]
}) {
  if (isHistoricalRevision.value) return
  const { reference, version, selectedPaths } = payload
  if (
    !reference.mount_prefix
    || reference.mount_prefix.split('/').some(part => !part || part === '.' || part === '..')
  ) {
    feedback.value = '挂载目录无效。'
    return
  }
  if (packageReferences.value.some(item => referenceKey(item) === referenceKey(reference))) {
    feedback.value = `${reference.package_name} ${reference.version} 已引用。`
    return
  }
  const occupied = new Set(sourceFiles.value.map(file => file.path))
  const conflict = reference.files.find(file => occupied.has(file.mounted_path))
  if (conflict) {
    feedback.value = `文件路径冲突：${conflict.mounted_path}`
    return
  }

  const packageFileByPath = new Map(version.files.map(file => [file.path, file]))
  const mountedFiles: WdlSourceFile[] = reference.files.map((file) => {
    const source = packageFileByPath.get(file.path)
    return {
      path: file.mounted_path,
      content: source?.content ?? '',
      digest: file.digest,
      is_entry: false,
      analysis: source?.analysis,
      origin: 'package',
      read_only: true,
      package_reference: {
        package_slug: reference.package_slug,
        package_name: reference.package_name,
        version: reference.version,
        digest: reference.digest,
        mount_prefix: reference.mount_prefix,
        package_file_path: file.path,
      },
    }
  })
  sourceFiles.value = [...sourceFiles.value, ...mountedFiles]
  for (const file of mountedFiles) {
    fileContents.value[file.path] = file.content ?? ''
    baseFileContents.value[file.path] = file.content ?? ''
  }
  packageReferences.value = [...packageReferences.value, reference]
  const importCount = insertPackageImports(reference, selectedPaths)
  pendingOperation.value = 'edit'
  revisionNote.value ||= `引用 ${reference.package_name} ${reference.version}`
  inspectorTab.value = 'diff'
  feedback.value = `已引用 ${reference.package_name} ${reference.version}${importCount ? `，补入 ${importCount} 条 import` : ''}。保存后生成新版本。`
}

function selectFile(path: string, line?: number) {
  activeFilePath.value = path
  if (line) {
    cursorLine.value = line
    void nextTick(() => codeEditor.value?.revealLine(line))
  }
}

function openCollaborationComposer(line: number) {
  cursorLine.value = line
  inspectorTab.value = 'collaboration'
  collaborationComposerRequest.value += 1
}

async function importTask(task: NonNullable<typeof analysis.value>['tasks'][number]) {
  if (!task.file_path || !selectedRevision.value || dirty.value) return
  const key = `${task.file_path}::${task.name}`
  taskImportState.value[key] = 'saving'
  try {
    const response = await $fetch<{ tool_id: string }>(
      `/api/v1/wdl-assets/${encodeURIComponent(slug.value)}/tasks/import`,
      {
        method: 'POST',
        body: {
          version: selectedRevision.value.version,
          file_path: task.file_path,
          task_name: task.name,
        },
      },
    )
    taskImportState.value[key] = 'done'
    feedback.value = `工具草稿 ${response.tool_id} 已创建。`
    await navigateTo({ path: '/', query: { section: 'tools', tool: response.tool_id } })
  } catch (error: any) {
    taskImportState.value[key] = 'error'
    feedback.value = error?.data?.error?.message ?? '工具草稿导入失败。'
  }
}

function diffLineClass(line: string) {
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-line--file'
  if (line.startsWith('+')) return 'diff-line--add'
  if (line.startsWith('-')) return 'diff-line--remove'
  if (line.startsWith('@@')) return 'diff-line--range'
  return ''
}

function handleSaveShortcut(event: KeyboardEvent) {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault()
    void saveRevision()
  } else if (event.shiftKey && event.altKey && event.key.toLowerCase() === 'e') {
    event.preventDefault()
    void exportWdl()
  } else if (event.shiftKey && event.altKey && event.key.toLowerCase() === 'f') {
    event.preventDefault()
    void formatSource()
  } else if ((event.ctrlKey || event.metaKey) && event.altKey && event.key.toLowerCase() === 'm') {
    event.preventDefault()
    openCollaborationComposer(cursorLine.value)
  }
}

function handleBeforeUnload(event: BeforeUnloadEvent) {
  if (!dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  void loadAsset()
  window.addEventListener('keydown', handleSaveShortcut)
  window.addEventListener('beforeunload', handleBeforeUnload)
})

watch(dirty, (value) => {
  if (!value && inspectorTab.value === 'diff') inspectorTab.value = 'structure'
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleSaveShortcut)
  window.removeEventListener('beforeunload', handleBeforeUnload)
})

onBeforeRouteLeave(() => {
  if (dirty.value && !window.confirm('当前 WDL 还有未保存的修改，确定离开吗？')) {
    return false
  }
})
</script>

<template>
  <div class="app-shell app-shell--workspace">
    <AppTopbar section="WDL 工作台" :current="asset?.name ?? slug">
      <template #status>
        <span class="save-state">
          <span class="status-dot" :class="{ 'status-dot--warning': dirty }" />
          {{
            loadState === 'loading' ? '正在读取…'
              : saveState === 'saving' ? '正在保存…'
                : dirty ? '有未保存修改'
                  : `WDL v${selectedRevision?.version ?? '—'}`
          }}
        </span>
      </template>
      <template #actions>
        <button
          class="button button--primary wdl-save-button"
          type="button"
          aria-label="保存新版本"
          title="保存新版本（Ctrl/⌘ + S）"
          :disabled="!dirty || isHistoricalRevision || saveState === 'saving'"
          @click="saveRevision"
        >
          <span>{{ saveState === 'saving' ? '保存中…' : '保存' }}</span>
          <kbd v-if="saveState !== 'saving'">Ctrl/⌘ S</kbd>
        </button>
      </template>
    </AppTopbar>

    <AppRail active="wdl" @select="navigateSection" />

    <main v-if="loadState === 'ready' && asset" class="section-workspace wdl-workbench">
      <aside class="wdl-workbench__sidebar">
        <NuxtLink class="back-link" to="/wdl">← 返回 WDL 资产</NuxtLink>
        <NuxtLink v-if="sourceWorkflowOrigin" class="wdl-source-workflow-link" :to="sourceWorkflowOrigin.to">
          来源流程 {{ sourceWorkflowOrigin.slug }}<template v-if="sourceWorkflowOrigin.wdlVersion"> · WDL v{{ sourceWorkflowOrigin.wdlVersion }}</template><template v-if="sourceWorkflowOrigin.version"> · 基于 v{{ sourceWorkflowOrigin.version }}</template>
        </NuxtLink>
        <div class="wdl-asset-heading">
          <h1
            v-if="editingMetadataField !== 'name'"
            class="inline-metadata-value inline-metadata-value--title"
            tabindex="0"
            title="双击修改标题"
            @dblclick="beginMetadataEdit('name')"
            @keydown.enter.prevent="beginMetadataEdit('name')"
          >
            {{ asset.name }}
          </h1>
          <input
            v-else
            ref="assetNameInput"
            v-model="assetNameDraft"
            class="inline-metadata-input inline-metadata-input--title"
            aria-label="WDL 标题"
            maxlength="256"
            @blur="commitMetadataEdit('name')"
            @keydown.enter.prevent="blurCurrentTarget"
            @keydown.esc.prevent="cancelMetadataEdit"
          />
          <p
            v-if="editingMetadataField !== 'description'"
            class="inline-metadata-value inline-metadata-value--description"
            tabindex="0"
            title="双击修改说明"
            @dblclick="beginMetadataEdit('description')"
            @keydown.enter.prevent="beginMetadataEdit('description')"
          >
            {{ asset.description || '暂无资产说明。' }}
          </p>
          <textarea
            v-else
            ref="assetDescriptionInput"
            v-model="assetDescriptionDraft"
            class="inline-metadata-input inline-metadata-input--description"
            aria-label="WDL 说明"
            rows="3"
            @blur="commitMetadataEdit('description')"
            @keydown.esc.prevent="cancelMetadataEdit"
          />
        </div>

        <section class="wdl-lifecycle-path" aria-label="WDL 迁移与使用路径">
          <article>
            <div>
              <strong>固定工具包</strong>
              <small v-if="derivedPackages[0]">已固定 · {{ derivedPackages[0].latest_version?.version }}</small>
              <small v-else>{{ lifecycleLoadState === 'loading' ? '正在检查…' : '尚未固定' }}</small>
            </div>
            <NuxtLink
              class="text-button"
              :to="derivedPackages[0] ? `/wdl-packages/${derivedPackages[0].slug}` : packageCreationLink"
            >{{ derivedPackages[0] ? '查看' : '创建' }}</NuxtLink>
          </article>
          <article>
            <div>
              <strong>拆解工具</strong>
              <small>{{ importedTasks.size }} / {{ analysis?.summary.task_count ?? 0 }} 已导入</small>
            </div>
            <button class="text-button" type="button" @click="inspectorTab = 'structure'">查看 Tasks</button>
          </article>
          <article>
            <div>
              <strong>直接运行</strong>
              <small>
                {{ directRunDestination
                  ? `v${directRunDestination.revision} · ${directRunDestination.ready ? '运行配置就绪' : directRunDestination.blockers[0] || '运行前检查未通过'}`
                  : (lifecycleLoadState === 'loading' ? '正在检查…' : '未配置受管运行') }}
              </small>
            </div>
            <NuxtLink
              v-if="directRunDestination"
              class="text-button"
              :to="directRunLink"
            >去运行</NuxtLink>
            <span v-else>—</span>
          </article>
        </section>

        <div
          v-if="metadataConflict"
          class="wdl-metadata-conflict"
          role="alert"
        >
          <strong>信息已更新</strong>
          <p>
            {{ metadataConflict.actor || '其他用户' }}
            <template v-if="metadataConflict.updatedAt">
              · {{ formatConflictTimestamp(metadataConflict.updatedAt) }}
            </template>
            已保存新内容；当前页面已载入最新版，本次修改未保存。
          </p>
          <small v-if="metadataConflict.note">备注：{{ metadataConflict.note }}</small>
          <button type="button" @click="resumeMetadataEditing">继续编辑</button>
        </div>

        <section class="wdl-sidebar-section">
          <h2>标签</h2>
          <div class="field wdl-tag-field">
            <div class="wdl-tag-editor">
              <div v-if="asset.tags.length" class="wdl-tag-editor__selected">
                <span v-for="tag in asset.tags" :key="tag" class="wdl-tag-editor__chip">
                  {{ tag }}
                  <button
                    type="button"
                    :aria-label="`移除标签 ${tag}`"
                    :disabled="metadataState === 'saving' || metadataQueueBlocked"
                    @click="removeTag(tag)"
                  >
                    ×
                  </button>
                </span>
              </div>
              <input
                v-model="tagDraft"
                aria-label="添加标签"
                maxlength="64"
                placeholder="输入一个标签"
                :disabled="metadataQueueBlocked"
                @blur="commitTagDraft"
                @keydown.enter.prevent="blurCurrentTarget"
                @keydown.esc.prevent="tagDraft = ''"
              />
            </div>
          </div>
          <div v-if="popularTags.length" class="tag-suggestions tag-suggestions--popular">
            <span>常用</span>
            <button
              v-for="tag in popularTags"
              :key="tag.name"
              type="button"
              :disabled="isTagSelected(tag.name) || metadataState === 'saving' || metadataQueueBlocked"
              @click="addTag(tag.name)"
            >
              {{ isTagSelected(tag.name) ? '✓' : '+' }} {{ tag.name }}
            </button>
          </div>
        </section>

        <section v-if="sourceFiles.length" class="wdl-sidebar-section wdl-source-files">
          <h2>文件 <span>{{ sourceFiles.length }}</span></h2>
          <WdlFileTree
            :files="sourceFiles"
            :active-path="activeFilePath"
            :dirty-paths="dirtyPaths"
            @select="selectFile"
          />
        </section>

        <section class="wdl-sidebar-section wdl-revision-list">
          <h2>源码版本</h2>
          <button
            v-for="revision in asset.revisions"
            :key="revision.version"
            type="button"
            :class="{ 'wdl-revision-list__active': revision.version === selectedRevision?.version }"
            @click="loadRevision(revision.version)"
          >
            <span>
              <strong>v{{ revision.version }}</strong>
              <small>{{ operationLabels[revision.operation] }} · {{ revision.actor }}</small>
            </span>
            <time>{{ new Date(revision.created_at).toLocaleDateString('zh-CN') }}</time>
          </button>
        </section>
      </aside>

      <section class="wdl-workbench__editor">
        <header class="wdl-editor-toolbar">
          <div>
            <strong>{{ activeFilePath || asset.source_filename }}</strong>
            <small>
              WDL {{ analysis?.wdl_version ?? '未知版本' }}
              · {{ content.split('\n').length }} 行
              <template v-if="activeSourceFile?.origin === 'package'"> · 工具包只读</template>
              <template v-if="analysis?.package"> · {{ analysis.package.reachable_file_count }}/{{ analysis.package.file_count }} 文件</template>
            </small>
          </div>
          <div class="wdl-editor-toolbar__actions">
            <button
              v-if="isHistoricalRevision"
              class="button button--ghost"
              type="button"
              @click="returnToLatest"
            >
              返回最新版本
            </button>
            <button
              class="button button--ghost wdl-export-button"
              type="button"
              aria-label="导出 WDL"
              title="导出 WDL（Shift + Alt/Option + E）"
              @click="exportWdl"
            >
              <span>导出</span>
              <kbd>⇧ Alt E</kbd>
            </button>
            <button
              class="button button--ghost wdl-comment-button"
              type="button"
              aria-label="评论当前行"
              title="添加行级评论（Ctrl/⌘ + Alt + M）"
              :disabled="dirty"
              @click="openCollaborationComposer(cursorLine)"
            >
              <span>评论</span>
              <kbd>Ctrl Alt M</kbd>
            </button>
            <button
              v-if="!isActiveFileReadOnly"
              class="button button--ghost wdl-format-button"
              type="button"
              aria-label="格式化"
              title="格式化 WDL（Shift + Alt/Option + F）"
              :disabled="saveState === 'saving'"
              @click="formatSource"
            >
              <span>格式化</span>
              <kbd>⇧ Alt F</kbd>
            </button>
          </div>
        </header>

        <div class="wdl-editor-status" role="status" aria-live="polite">
          <div v-if="isHistoricalRevision" class="historical-notice">
            正在查看不可变历史版本 v{{ selectedRevision?.version }}。返回最新版本后才能继续编辑。
          </div>
          <div v-else-if="activeSourceFile?.origin === 'package'" class="editor-ready-notice">
            {{ activeSourceFile.package_reference?.package_name }} {{ activeSourceFile.package_reference?.version }} · 只读
          </div>
          <div v-else-if="feedback" class="workbench-feedback" :class="{ 'workbench-feedback--error': saveState === 'error' || metadataState === 'error' }">
            {{ feedback }}
          </div>
          <div v-else-if="isRawImportedRevision" class="raw-import-notice">
            当前是保留原貌的导入版本，尚未格式化。
          </div>
          <div v-else class="editor-ready-notice">
            编辑器已就绪
          </div>
        </div>

        <section
          v-if="revisionConflict"
          class="wdl-revision-conflict"
          role="alert"
          aria-live="assertive"
        >
          <div>
            <strong>已有更新版本 v{{ revisionConflict.currentRevision.version }}</strong>
            <p>
              {{ revisionConflict.actor || revisionConflict.currentRevision.actor || '其他用户' }}
              <template v-if="revisionConflict.updatedAt">
                · {{ formatConflictTimestamp(revisionConflict.updatedAt) }}
              </template>
              已保存新版本。本地草稿仍在编辑器中，没有被覆盖。
            </p>
            <small v-if="revisionConflict.note">备注：{{ revisionConflict.note }}</small>
          </div>
          <div class="wdl-revision-conflict__actions">
            <button class="button button--ghost" type="button" @click="loadConflictRevision">
              载入最新版本
            </button>
            <button class="button button--primary" type="button" @click="rebaseConflictDraft">
              保留草稿并对比
            </button>
          </div>
        </section>

        <ClientOnly>
          <WdlCodeEditor
            ref="codeEditor"
            v-model="content"
            :read-only="isActiveFileReadOnly"
            :formatting="saveState === 'saving'"
            :aria-label="`${asset.name} WDL 源码`"
            @export="exportWdl"
            @format="formatSource"
            @comment="openCollaborationComposer"
            @cursor-line="cursorLine = $event"
          />
          <template #fallback>
            <div class="wdl-editor-loading">正在载入源码编辑器…</div>
          </template>
        </ClientOnly>

        <div v-if="!isHistoricalRevision" class="wdl-revision-note">
          <label class="field">
            <span>本次修改备注</span>
            <input
              ref="revisionNoteInput"
              v-model="revisionNote"
              :aria-invalid="dirty && !revisionNote.trim()"
              placeholder="说明改了什么、为什么改；保存后进入操作历史"
            />
            <small v-if="dirty && !revisionNote.trim()" class="field-error">
              保存新版本前需要填写备注。
            </small>
          </label>
        </div>
      </section>

      <aside class="wdl-workbench__inspector">
        <div class="panel-tabs panel-tabs--inspector" role="tablist" aria-label="WDL 工作台检查器">
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'collaboration'"
            :class="{ 'panel-tab--active': inspectorTab === 'collaboration' }"
            @click="inspectorTab = 'collaboration'"
          >
            协作 <span class="tab-count">{{ asset.collaboration?.open_thread_count ?? 0 }}</span>
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'structure'"
            :class="{ 'panel-tab--active': inspectorTab === 'structure' }"
            @click="inspectorTab = 'structure'"
          >
            结构
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'diagnostics'"
            :class="{ 'panel-tab--active': inspectorTab === 'diagnostics' }"
            @click="inspectorTab = 'diagnostics'"
          >
            诊断 <span class="tab-count">{{ analysis?.diagnostics.length ?? 0 }}</span>
          </button>
          <button
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'history'"
            :class="{ 'panel-tab--active': inspectorTab === 'history' }"
            @click="inspectorTab = 'history'"
          >
            历史 <span class="tab-count">{{ auditEvents.length }}</span>
          </button>
          <button
            v-if="dirty"
            type="button"
            role="tab"
            :aria-selected="inspectorTab === 'diff'"
            :class="{ 'panel-tab--active': inspectorTab === 'diff' }"
            @click="inspectorTab = 'diff'"
          >
            变更
          </button>
        </div>

        <div v-if="inspectorTab === 'structure'" class="wdl-inspector-content">
          <div class="analysis-summary">
            <span>
              <strong>{{ analysis?.summary.task_count ?? 0 }}</strong>
              <small>Tasks</small>
            </span>
            <span>
              <strong>{{ analysis?.summary.workflow_count ?? 0 }}</strong>
              <small>Workflows</small>
            </span>
            <span>
              <strong>{{ analysis?.summary.import_count ?? 0 }}</strong>
              <small>Imports</small>
            </span>
          </div>

          <WdlPackageReferencePanel
            :references="packageReferences"
            :read-only="isHistoricalRevision"
            @add="addPackageReference"
          />

          <section v-if="analysis?.imports.length" class="definition-group">
            <h2>Imports</h2>
            <article
              v-for="item in analysis.imports"
              :key="`${item.file_path}-${item.uri}`"
              class="definition-group__interactive"
              @click="item.target_path && selectFile(item.target_path)"
            >
              <strong>{{ item.namespace || item.uri }}</strong>
              <code>{{ item.uri }}</code>
              <small>
                {{ item.file_path }} · 第 {{ item.line ?? '—' }} 行
                · {{ item.status === 'resolved' ? '已解析' : item.status === 'external' ? '外部依赖' : '缺少文件' }}
              </small>
            </article>
          </section>

          <section class="definition-group">
            <h2>Tasks</h2>
            <article
              v-for="task in analysis?.tasks"
              :key="task.id || `${task.file_path}-${task.name}`"
              class="definition-group__interactive"
              @click="task.file_path && selectFile(task.file_path, task.line)"
            >
              <div class="definition-title-row">
                <strong>{{ task.name }}</strong>
                <button
                  class="task-import-button"
                  type="button"
                  :disabled="dirty || taskImportState[`${task.file_path}::${task.name}`] === 'saving'"
                  @click.stop="importTask(task)"
                >
                  {{
                    importedTasks.has(`${task.file_path}::${task.name}`)
                      || taskImportState[`${task.file_path}::${task.name}`] === 'done'
                      ? '已导入'
                      : taskImportState[`${task.file_path}::${task.name}`] === 'saving'
                        ? '导入中…'
                        : '导入工具'
                  }}
                </button>
              </div>
              <small>{{ task.file_path }} · 第 {{ task.line ?? '—' }}–{{ task.end_line ?? '—' }} 行</small>
              <code>{{ task.inputs.length }} inputs → {{ task.outputs.length }} outputs</code>
              <div v-if="task.runtime_keys.length" class="tag-list tag-list--compact">
                <span v-for="key in task.runtime_keys" :key="key">{{ key }}</span>
              </div>
            </article>
            <p v-if="analysis?.tasks.length === 0" class="empty-state">没有解析到 task。</p>
          </section>

          <section class="definition-group">
            <h2>Workflows</h2>
            <article v-for="workflow in analysis?.workflows" :key="workflow.name">
              <strong>{{ workflow.name }}</strong>
              <small>{{ workflow.file_path }} · 第 {{ workflow.line ?? '—' }}–{{ workflow.end_line ?? '—' }} 行</small>
              <code>
                {{ workflow.structure.call_count }} calls
                · {{ workflow.structure.scatter_count }} scatter
                · {{ workflow.structure.conditional_count }} if
              </code>
            </article>
            <p v-if="analysis?.workflows.length === 0" class="empty-state">没有解析到 workflow。</p>
          </section>
        </div>

        <div v-else-if="inspectorTab === 'diagnostics'" class="wdl-inspector-content">
          <div
            class="analysis-banner"
            :class="`analysis-banner--${analysis?.status ?? 'invalid'}`"
          >
            <strong>{{ analysis?.status === 'valid' ? '静态检查通过' : '需要处理' }}</strong>
            <p>
              {{
                analysis?.status === 'valid'
                  ? '源码可以继续格式化和拆解。'
                  : `${analysis?.summary.error_count ?? 0} 个解析或类型问题。`
              }}
            </p>
          </div>
          <article v-for="item in analysis?.diagnostics" :key="`${item.code}-${item.message}`" class="diagnostic">
            <div>
              <strong>{{ item.code }}</strong>
              <span>错误</span>
            </div>
            <p>{{ item.message }}</p>
            <small>{{ item.file_path || activeFilePath }}<template v-if="item.location"> · 第 {{ item.location.line ?? '—' }} 行，第 {{ item.location.column ?? '—' }} 列</template></small>
          </article>
          <p v-if="analysis?.diagnostics.length === 0" class="empty-state">当前版本没有诊断信息。</p>
        </div>

        <WdlCollaborationPanel
          v-else-if="inspectorTab === 'collaboration' && selectedRevision"
          :slug="slug"
          :revision="selectedRevision.version"
          :file-path="activeFilePath"
          :anchor-line="cursorLine"
          :can-comment="!dirty"
          :composer-request="collaborationComposerRequest"
          @reveal="selectFile"
          @changed="refreshAssetAudit"
        />

        <div v-else-if="inspectorTab === 'diff'" class="wdl-inspector-content wdl-format-diff-panel">
          <header>
            <div>
              <strong>未保存变更</strong>
              <small>相对 WDL v{{ selectedRevision?.version }}；保存后写入历史</small>
            </div>
            <button
              class="button button--ghost"
              type="button"
              @click="inspectorTab = 'structure'"
            >
              收起
            </button>
          </header>
          <pre class="audit-diff"><code><span
            v-for="(line, index) in changeDiff.split('\n')"
            :key="index"
            :class="diffLineClass(line)"
          >{{ line || ' ' }}
</span></code></pre>
        </div>

        <div v-else class="wdl-history-panel">
          <div class="wdl-history-list">
            <button
              v-for="event in auditEvents"
              :key="event.id"
              type="button"
              :class="{ 'wdl-history-list__active': event.id === selectedEvent?.id }"
              @click="selectedEvent = event"
            >
              <span>
                <strong>{{ actionLabels[event.action] ?? event.action }}</strong>
                <small>{{ event.actor }} · {{ new Date(event.created_at).toLocaleString('zh-CN') }}</small>
              </span>
              <em v-if="event.revision">v{{ event.revision }}</em>
            </button>
          </div>
          <article v-if="selectedEvent" class="wdl-history-detail">
            <header>
              <strong>{{ actionLabels[selectedEvent.action] ?? selectedEvent.action }}</strong>
              <small>{{ new Date(selectedEvent.created_at).toLocaleString('zh-CN') }}</small>
            </header>
            <dl>
              <div><dt>操作者</dt><dd>{{ selectedEvent.actor }}</dd></div>
              <div><dt>版本</dt><dd>{{ selectedEvent.revision ? `v${selectedEvent.revision}` : '仅元数据' }}</dd></div>
              <div><dt>备注</dt><dd>{{ selectedEvent.note || '未填写备注' }}</dd></div>
            </dl>
            <pre v-if="selectedEvent.diff" class="audit-diff"><code><span
              v-for="(line, index) in selectedEvent.diff.split('\n')"
              :key="index"
              :class="diffLineClass(line)"
            >{{ line || ' ' }}
</span></code></pre>
            <div v-else-if="Object.keys(selectedEvent.changes).length" class="metadata-changes">
              <strong>字段变化</strong>
              <dl>
                <div v-for="(change, field) in selectedEvent.changes" :key="field">
                  <dt>{{ field }}</dt>
                  <dd>{{ JSON.stringify(change) }}</dd>
                </div>
              </dl>
            </div>
          </article>
        </div>
      </aside>
    </main>

    <main v-else class="section-workspace wdl-workbench-state">
      <div v-if="loadState === 'loading'" class="wdl-editor-loading">正在打开 WDL 工作台…</div>
      <div v-else class="empty-state">
        <strong>WDL 资产无法打开</strong>
        <p>该资产不存在，或后端服务当前不可用。</p>
        <NuxtLink class="button button--ghost button-link" to="/wdl">返回资产台账</NuxtLink>
      </div>
    </main>
  </div>
</template>
