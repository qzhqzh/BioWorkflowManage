<script setup lang="ts">
import type {
  WdlCollaboration,
  WdlReleaseCheckKey,
  WdlReviewRequest,
  WdlReviewThread,
} from '~/types/wdl'

const props = defineProps<{
  slug: string
  revision: number
  filePath: string
  anchorLine: number
  canComment: boolean
  composerRequest: number
}>()

const emit = defineEmits<{
  reveal: [filePath: string, line: number]
  changed: []
}>()

const { $api } = useNuxtApp()
const collaboration = ref<WdlCollaboration>()
const loadState = ref<'loading' | 'ready' | 'error'>('loading')
const mutationState = ref<'idle' | 'saving' | 'error'>('idle')
const feedback = ref('')
const assignee = ref('')
const requestNote = ref('')
const conclusion = ref('')
const commentBody = ref('')
const replyDrafts = ref<Record<number, string>>({})
const commentInput = ref<HTMLTextAreaElement>()
const releaseVersion = ref(`r${props.revision}`)
const releaseNote = ref('')
const policyChecks = ref<WdlReleaseCheckKey[]>([])
const maxInputMiB = ref(1024)

const releaseCheckOptions: Array<{ key: WdlReleaseCheckKey; label: string }> = [
  { key: 'syntax', label: '语法与静态检查' },
  { key: 'imports', label: 'imports 完整' },
  { key: 'package_pins', label: '工具包版本固定' },
  { key: 'approved_review', label: '版本评审通过' },
  { key: 'resolved_threads', label: '讨论全部解决' },
  { key: 'small_data_run', label: '小数据运行成功' },
]

const latestReview = computed(() => collaboration.value?.reviews[0])
const pendingReview = computed(() => collaboration.value?.reviews.find(
  item => item.status === 'pending',
))
const openThreads = computed(() => collaboration.value?.threads.filter(
  item => item.status === 'open',
) ?? [])
const resolvedThreads = computed(() => collaboration.value?.threads.filter(
  item => item.status === 'resolved',
) ?? [])
const isAssignedReviewer = computed(() =>
  pendingReview.value?.assignee === collaboration.value?.me,
)
const latestReleaseCheck = computed(() => collaboration.value?.governance.checks[0])
const currentRelease = computed(() => collaboration.value?.governance.releases.find(
  item => item.revision === props.revision,
))

function reviewStatusLabel(review: WdlReviewRequest) {
  return ({
    pending: '等待评审',
    approved: '已通过',
    changes_requested: '需修改',
    cancelled: '已取消',
  } as const)[review.status]
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

async function loadCollaboration() {
  loadState.value = 'loading'
  feedback.value = ''
  try {
    collaboration.value = await $api<WdlCollaboration>(
      `/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/collaboration`,
      { query: { revision: props.revision } },
    )
    assignee.value ||= collaboration.value.assignees[0]?.username ?? ''
    policyChecks.value = [...collaboration.value.governance.policy.enabled_checks]
    maxInputMiB.value = Math.round(
      collaboration.value.governance.policy.max_input_bytes / 1024 / 1024,
    )
    loadState.value = 'ready'
  }
  catch {
    loadState.value = 'error'
  }
}

async function runReleaseCheck() {
  if (mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(`/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/release-checks`, {
      method: 'POST',
      body: { revision: props.revision },
    })
    await loadCollaboration()
    feedback.value = latestReleaseCheck.value?.status === 'passed'
      ? '发布检查已通过。'
      : '发布检查完成，请处理未通过项。'
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '发布检查失败，请重试。'
    return
  }
  mutationState.value = 'idle'
}

async function publishRelease() {
  const check = latestReleaseCheck.value
  if (!check || check.status !== 'passed' || mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(`/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/releases`, {
      method: 'POST',
      body: {
        release_check_id: check.id,
        base_version: props.revision,
        base_digest: check.revision_digest,
        version: releaseVersion.value.trim(),
        note: releaseNote.value.trim(),
      },
    })
    releaseNote.value = ''
    await loadCollaboration()
    feedback.value = '稳定版本已发布。'
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '发布失败，请重新检查。'
    await loadCollaboration()
    return
  }
  mutationState.value = 'idle'
}

async function saveReleasePolicy() {
  const policy = collaboration.value?.governance.policy
  if (!policy || !policyChecks.value.length || mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api('/api/v1/wdl-release-policy', {
      method: 'PATCH',
      body: {
        base_policy_version: policy.version,
        enabled_checks: policyChecks.value,
        max_input_bytes: Math.round(maxInputMiB.value * 1024 * 1024),
      },
    })
    await loadCollaboration()
    feedback.value = '发布检查模板已更新。'
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '检查模板保存失败。'
    await loadCollaboration()
    return
  }
  mutationState.value = 'idle'
}

async function requestReview() {
  if (!assignee.value || mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(`/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/reviews`, {
      method: 'POST',
      body: {
        revision: props.revision,
        assignee: assignee.value,
        note: requestNote.value.trim(),
      },
    })
    requestNote.value = ''
    feedback.value = '评审已发起。'
    await loadCollaboration()
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '评审发起失败，请重试。'
    return
  }
  mutationState.value = 'idle'
}

async function concludeReview(action: 'approve' | 'request_changes') {
  const review = pendingReview.value
  if (!review || !conclusion.value.trim() || mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(
      `/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/reviews/${review.id}`,
      {
        method: 'PATCH',
        body: {
          action,
          conclusion: conclusion.value.trim(),
          base_review_version: review.version,
        },
      },
    )
    conclusion.value = ''
    feedback.value = action === 'approve' ? '评审已通过。' : '修改意见已提交。'
    await loadCollaboration()
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '评审状态更新失败，请重试。'
    await loadCollaboration()
    return
  }
  mutationState.value = 'idle'
}

async function createThread() {
  if (!props.canComment || !commentBody.value.trim() || mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(
      `/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/review-threads`,
      {
        method: 'POST',
        body: {
          revision: props.revision,
          file_path: props.filePath,
          line: props.anchorLine,
          body: commentBody.value.trim(),
        },
      },
    )
    commentBody.value = ''
    feedback.value = '行级评论已添加。'
    await loadCollaboration()
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '评论添加失败，请重试。'
    return
  }
  mutationState.value = 'idle'
}

async function reply(thread: WdlReviewThread) {
  const body = replyDrafts.value[thread.id]?.trim()
  if (!body || mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(
      `/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/review-threads/${thread.id}/comments`,
      { method: 'POST', body: { body } },
    )
    replyDrafts.value[thread.id] = ''
    await loadCollaboration()
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '回复失败，请重试。'
    return
  }
  mutationState.value = 'idle'
}

async function toggleThread(thread: WdlReviewThread) {
  if (mutationState.value === 'saving') return
  mutationState.value = 'saving'
  feedback.value = ''
  try {
    await $api(
      `/api/v1/wdl-assets/${encodeURIComponent(props.slug)}/review-threads/${thread.id}`,
      {
        method: 'PATCH',
        body: {
          action: thread.status === 'open' ? 'resolve' : 'reopen',
          base_thread_version: thread.version,
        },
      },
    )
    await loadCollaboration()
    emit('changed')
  }
  catch (error: any) {
    mutationState.value = 'error'
    feedback.value = error?.data?.error?.message ?? '讨论状态更新失败，请重试。'
    await loadCollaboration()
    return
  }
  mutationState.value = 'idle'
}

watch(
  () => [props.slug, props.revision],
  () => {
    releaseVersion.value = `r${props.revision}`
    void loadCollaboration()
  },
  { immediate: true },
)

watch(() => props.composerRequest, () => {
  void nextTick(() => commentInput.value?.focus())
})
</script>

<template>
  <div class="wdl-collaboration">
    <div v-if="loadState === 'loading'" class="collaboration-state" role="status">
      正在读取协作记录…
    </div>
    <div v-else-if="loadState === 'error'" class="collaboration-state collaboration-state--error" role="alert">
      <strong>协作记录读取失败</strong>
      <button type="button" @click="loadCollaboration">重新加载</button>
    </div>
    <template v-else-if="collaboration">
      <p v-if="feedback" class="collaboration-feedback" :class="{ 'collaboration-feedback--error': mutationState === 'error' }" role="status">
        {{ feedback }}
      </p>

      <section class="collaboration-section">
        <header>
          <h2>版本评审</h2>
          <span>v{{ revision }}</span>
        </header>

        <article v-if="latestReview" class="review-summary">
          <div>
            <strong>{{ reviewStatusLabel(latestReview) }}</strong>
            <span>{{ latestReview.requester }} → {{ latestReview.assignee }}</span>
          </div>
          <p v-if="latestReview.request_note">{{ latestReview.request_note }}</p>
          <p v-if="latestReview.conclusion" class="review-conclusion">{{ latestReview.conclusion }}</p>
          <time :datetime="latestReview.updated_at">{{ formatTime(latestReview.updated_at) }}</time>
        </article>

        <form v-if="!pendingReview" class="collaboration-form" @submit.prevent="requestReview">
          <label>
            <span>指派评审人</span>
            <select v-model="assignee" required>
              <option value="" disabled>选择维护者</option>
              <option v-for="item in collaboration.assignees" :key="item.username" :value="item.username">
                {{ item.username }}
              </option>
            </select>
          </label>
          <label>
            <span>交接说明</span>
            <textarea v-model="requestNote" rows="3" maxlength="10000" placeholder="希望重点检查什么" />
          </label>
          <button class="button button--primary" type="submit" :disabled="!assignee || mutationState === 'saving'">
            发起评审
          </button>
        </form>

        <form v-else-if="isAssignedReviewer" class="collaboration-form" @submit.prevent>
          <label>
            <span>评审结论</span>
            <textarea v-model="conclusion" rows="3" maxlength="10000" placeholder="说明通过依据或需要修改的内容" />
          </label>
          <div class="review-actions">
            <button type="button" :disabled="!conclusion.trim() || mutationState === 'saving'" @click="concludeReview('request_changes')">
              需要修改
            </button>
            <button class="button button--primary" type="button" :disabled="!conclusion.trim() || mutationState === 'saving'" @click="concludeReview('approve')">
              通过评审
            </button>
          </div>
        </form>
      </section>

      <section class="collaboration-section release-governance">
        <header>
          <h2>稳定发布</h2>
          <span v-if="currentRelease">{{ currentRelease.version }}</span>
          <span v-else>revision {{ revision }}</span>
        </header>

        <article v-if="currentRelease" class="review-summary release-summary">
          <div><strong>已发布</strong><time :datetime="currentRelease.created_at">{{ formatTime(currentRelease.created_at) }}</time></div>
          <p>{{ currentRelease.actor }}<template v-if="currentRelease.note"> · {{ currentRelease.note }}</template></p>
        </article>

        <template v-else>
          <ul v-if="latestReleaseCheck" class="release-check-list">
            <li v-for="item in latestReleaseCheck.checks" :key="item.key" :class="{ failed: !item.passed }">
              <span aria-hidden="true">{{ item.passed ? '✓' : '!' }}</span>
              <span>{{ item.label }}</span>
            </li>
          </ul>
          <p v-else class="collaboration-empty">尚未执行当前版本的发布检查。</p>
          <button class="button button--ghost" type="button" :disabled="mutationState === 'saving'" @click="runReleaseCheck">
            {{ latestReleaseCheck ? '重新检查' : '运行发布检查' }}
          </button>
          <form v-if="latestReleaseCheck?.status === 'passed' && collaboration.is_latest" class="collaboration-form" @submit.prevent="publishRelease">
            <label>
              <span>版本</span>
              <input v-model="releaseVersion" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._-]*" />
            </label>
            <label>
              <span>发布备注</span>
              <textarea v-model="releaseNote" rows="2" maxlength="10000" />
            </label>
            <button class="button button--primary" type="submit" :disabled="!releaseVersion.trim() || mutationState === 'saving'">发布稳定版本</button>
          </form>
        </template>

        <details v-if="collaboration.governance.can_manage_policy" class="release-policy">
          <summary>检查模板</summary>
          <label v-for="item in releaseCheckOptions" :key="item.key">
            <input v-model="policyChecks" type="checkbox" :value="item.key" />
            <span>{{ item.label }}</span>
          </label>
          <label class="release-limit">
            <span>小数据上限</span>
            <input v-model.number="maxInputMiB" type="number" min="1" max="102400" />
            <span>MiB</span>
          </label>
          <button type="button" :disabled="!policyChecks.length || mutationState === 'saving'" @click="saveReleasePolicy">保存模板</button>
        </details>
      </section>

      <section class="collaboration-section">
        <header>
          <h2>行级讨论</h2>
          <span>{{ openThreads.length }} 个未解决</span>
        </header>
        <form class="collaboration-form" @submit.prevent="createThread">
          <div class="comment-anchor">
            <code>{{ filePath || '未选择文件' }}</code>
            <span>第 {{ anchorLine }} 行</span>
          </div>
          <label>
            <span class="visually-hidden">添加行级评论</span>
            <textarea
              ref="commentInput"
              v-model="commentBody"
              rows="3"
              maxlength="10000"
              :disabled="!canComment"
              :placeholder="canComment ? '针对当前行提出问题或说明' : '请先保存当前源码变更'"
            />
          </label>
          <button class="button button--primary" type="submit" :disabled="!canComment || !commentBody.trim() || mutationState === 'saving'">
            添加评论
          </button>
        </form>

        <div v-if="openThreads.length" class="thread-list">
          <article v-for="thread in openThreads" :key="thread.id" class="review-thread">
            <header>
              <button type="button" @click="emit('reveal', thread.file_path, thread.line)">
                <code>{{ thread.file_path }}</code> · 第 {{ thread.line }} 行
              </button>
              <span v-if="thread.stale">历史版本</span>
            </header>
            <div v-for="comment in thread.comments" :key="comment.id" class="review-comment">
              <p>{{ comment.body }}</p>
              <small>{{ comment.author }} · {{ formatTime(comment.created_at) }}</small>
            </div>
            <form class="thread-reply" @submit.prevent="reply(thread)">
              <input v-model="replyDrafts[thread.id]" maxlength="10000" aria-label="回复讨论" placeholder="回复" />
              <button type="submit" :disabled="!replyDrafts[thread.id]?.trim() || mutationState === 'saving'">发送</button>
              <button type="button" :disabled="mutationState === 'saving'" @click="toggleThread(thread)">解决</button>
            </form>
          </article>
        </div>
        <p v-else class="collaboration-empty">当前版本没有待解决讨论。</p>

        <details v-if="resolvedThreads.length" class="resolved-threads">
          <summary>已解决 {{ resolvedThreads.length }}</summary>
          <article v-for="thread in resolvedThreads" :key="thread.id" class="review-thread review-thread--resolved">
            <header>
              <button type="button" @click="emit('reveal', thread.file_path, thread.line)">
                <code>{{ thread.file_path }}</code> · 第 {{ thread.line }} 行
              </button>
              <button type="button" :disabled="mutationState === 'saving'" @click="toggleThread(thread)">重新打开</button>
            </header>
            <p>{{ thread.comments.at(-1)?.body }}</p>
            <small>{{ thread.resolved_by }} · {{ thread.resolved_at ? formatTime(thread.resolved_at) : '' }}</small>
          </article>
        </details>
      </section>
    </template>
  </div>
</template>

<style scoped>
.wdl-collaboration {
  display: grid;
  min-width: 0;
}

.collaboration-state,
.collaboration-feedback,
.collaboration-empty {
  margin: 0;
  padding: 16px;
  color: var(--color-muted);
}

.collaboration-state--error,
.collaboration-feedback--error {
  color: var(--color-error);
}

.collaboration-state button {
  margin-inline-start: 8px;
}

.collaboration-section {
  display: grid;
  gap: 12px;
  padding: 16px;
  border-bottom: 1px solid var(--color-border);
}

.collaboration-section > header,
.review-summary > div,
.review-actions,
.comment-anchor,
.review-thread > header,
.thread-reply {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.collaboration-section h2 {
  margin: 0;
  font-size: 0.8125rem;
}

.collaboration-section header > span,
.review-summary time,
.review-comment small,
.review-thread > small {
  color: var(--color-muted);
  font-size: 0.75rem;
}

.review-summary {
  display: grid;
  gap: 8px;
  padding-block: 10px;
  border-block: 1px solid var(--color-border);
}

.review-summary p,
.review-comment p,
.review-thread > p {
  margin: 0;
  overflow-wrap: anywhere;
  line-height: 1.55;
}

.review-summary span {
  color: var(--color-muted);
  font-size: 0.75rem;
}

.review-conclusion {
  padding: 8px 10px;
  background: var(--color-primary-soft);
}

.collaboration-form {
  display: grid;
  gap: 10px;
}

.collaboration-form label {
  display: grid;
  gap: 5px;
  min-width: 0;
  color: var(--color-muted);
  font-size: 0.75rem;
}

.collaboration-form select,
.collaboration-form textarea,
.collaboration-form input,
.thread-reply input {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--color-border-strong);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-ink);
  font: inherit;
}

.collaboration-form select,
.collaboration-form input,
.thread-reply input {
  min-height: 36px;
  padding-inline: 10px;
}

.collaboration-form textarea {
  resize: vertical;
  min-height: 72px;
  padding: 9px 10px;
  line-height: 1.5;
}

.review-actions button,
.thread-reply button,
.review-thread header button,
.collaboration-state button {
  border: 0;
  background: transparent;
  color: var(--color-primary-hover);
  cursor: pointer;
  font: inherit;
}

.review-actions .button,
.collaboration-form > .button {
  border: 1px solid transparent;
  color: var(--color-bg);
}

.review-actions .button--primary,
.collaboration-form > .button--primary {
  background: var(--color-primary);
}

.comment-anchor code,
.review-thread header code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.comment-anchor span {
  flex: 0 0 auto;
  color: var(--color-muted);
  font-size: 0.75rem;
}

.thread-list,
.resolved-threads {
  display: grid;
  gap: 12px;
}

.review-thread {
  display: grid;
  gap: 10px;
  padding-block: 12px;
  border-top: 1px solid var(--color-border);
}

.review-thread > header button:first-child {
  display: flex;
  min-width: 0;
  padding: 0;
  text-align: start;
}

.review-comment {
  display: grid;
  gap: 4px;
  padding-inline-start: 10px;
  border-inline-start: 1px solid var(--color-border-strong);
}

.thread-reply {
  align-items: stretch;
}

.thread-reply input {
  flex: 1 1 auto;
}

.thread-reply button {
  flex: 0 0 auto;
  padding-inline: 4px;
}

.resolved-threads summary {
  cursor: pointer;
  color: var(--color-muted);
}

.review-thread--resolved {
  opacity: 0.82;
}

.release-check-list {
  display: grid;
  gap: 7px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.release-check-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-primary);
  font-size: 0.8125rem;
}

.release-check-list li.failed {
  color: var(--color-error);
}

.release-check-list li > span:first-child {
  display: inline-grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border: 1px solid currentcolor;
  border-radius: 50%;
  font-weight: 700;
  font-size: 0.6875rem;
}

.release-policy {
  padding-top: 8px;
  border-top: 1px solid var(--color-border);
  color: var(--color-muted);
  font-size: 0.75rem;
}

.release-policy summary {
  margin-bottom: 8px;
  cursor: pointer;
}

.release-policy > label {
  display: flex;
  align-items: center;
  gap: 7px;
  min-height: 28px;
}

.release-policy > button {
  margin-top: 8px;
  border: 0;
  background: transparent;
  color: var(--color-primary-hover);
  cursor: pointer;
}

.release-limit input {
  width: 88px;
  border: 1px solid var(--color-border-strong);
  border-radius: 6px;
  padding: 5px 7px;
}

@media (pointer: coarse) {
  .review-actions button,
  .thread-reply button,
  .review-thread header button,
  .collaboration-state button {
    min-height: 44px;
  }
}
</style>
