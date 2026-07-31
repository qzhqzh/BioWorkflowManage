<script setup lang="ts">
type InspectorTab = 'properties' | 'diagnostics' | 'artifacts'

const inspectorTab = defineModel<InspectorTab>('inspectorTab', { required: true })
defineProps<{
  selectedData?: Record<string, any>
  selectedNodeId?: string
  selectedToolSpec?: Record<string, any>
  workflowPortWdlTypes: string[]
  availableSubworkflowUpgrades: Array<Record<string, any>>
  selectedSubworkflowUpgrade?: Record<string, any>
  selectedSubworkflowUpgradeInfo?: Record<string, any>
  subworkflowUpgradeState: string
  subworkflowUpgradeMessage: string
  subworkflowUpgradeNodeId: string
  saveState: string
  isWorkflowSwitching: boolean
  selectedToolParameters: Array<Record<string, any>>
  diagnostics: Array<Record<string, any>>
  artifacts: Array<Record<string, any>>
  parameterDisplayValue: (port: Record<string, any>) => unknown
  isParameterConnected: (portName: string) => boolean
}>()

const emit = defineEmits<{
  updateIdentifier: [event: Event]
  updateLabel: [event: Event]
  updateWdlType: [event: Event]
  updateSemanticType: [event: Event]
  openSubworkflow: []
  selectSubworkflowUpgrade: [event: Event]
  upgradeSubworkflow: []
  updateToolParameter: [port: Record<string, any>, event: Event]
  openArtifact: [artifact: any]
}>()
</script>

<template>
  <aside class="inspector-panel">
    <header class="inspector-panel__header">
      <div>
        <span>INSPECTOR</span>
        <h2>{{ selectedData?.label ?? '未选择节点' }}</h2>
      </div>
      <button type="button" aria-label="关闭检查器">×</button>
    </header>

    <div class="panel-tabs panel-tabs--inspector" role="tablist" aria-label="检查器内容">
      <button
        type="button"
        role="tab"
        :aria-selected="inspectorTab === 'properties'"
        :class="{ 'panel-tab--active': inspectorTab === 'properties' }"
        @click="inspectorTab = 'properties'"
      >
        属性
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="inspectorTab === 'diagnostics'"
        :class="{ 'panel-tab--active': inspectorTab === 'diagnostics' }"
        @click="inspectorTab = 'diagnostics'"
      >
        诊断 <span class="tab-count">{{ diagnostics.length }}</span>
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="inspectorTab === 'artifacts'"
        :class="{ 'panel-tab--active': inspectorTab === 'artifacts' }"
        @click="inspectorTab = 'artifacts'"
      >
        产物 <span class="tab-count">{{ artifacts.length }}</span>
      </button>
    </div>

    <div v-if="inspectorTab === 'properties' && selectedData" class="inspector-content">
      <section class="inspector-section">
        <h3>标识</h3>
        <label class="field">
          <span>节点 ID</span>
          <input
            :value="selectedData.identifier"
            type="text"
            :readonly="selectedData.kind === 'subworkflow'"
            @change="emit('updateIdentifier', $event)"
          />
          <small>{{ selectedData.kind === 'subworkflow' ? '子流程实例保持固定标识' : '合法且唯一的 WDL identifier' }}</small>
        </label>
        <label class="field">
          <span>显示名称</span>
          <input
            :value="selectedData.label"
            type="text"
            :readonly="selectedData.kind === 'subworkflow'"
            @change="emit('updateLabel', $event)"
          />
        </label>
      </section>

      <section v-if="selectedData.kind === 'input' || selectedData.kind === 'output'" class="inspector-section">
        <h3>接口定义</h3>
        <label class="field">
          <span>WDL 类型</span>
          <select
            :value="selectedData.inputs?.[0]?.type ?? selectedData.outputs?.[0]?.type ?? 'File'"
            @change="emit('updateWdlType', $event)"
          >
            <option v-for="wdlType in workflowPortWdlTypes" :key="wdlType" :value="wdlType">
              {{ wdlType }}
            </option>
          </select>
        </label>
        <label class="field">
          <span>语义类型</span>
          <input
            :value="selectedData.semanticType"
            type="text"
            placeholder="例如 bio.fastq.gz.r1"
            @change="emit('updateSemanticType', $event)"
          />
          <small>连线会同时检查 WDL 类型和语义类型。</small>
        </label>
      </section>

      <section v-if="selectedData.kind === 'tool'" class="inspector-section">
        <h3>工具说明</h3>
        <p class="inspector-description">
          {{ selectedToolSpec?.description ?? selectedData.description ?? '该工具暂未填写说明。' }}
        </p>
        <dl class="property-list">
          <div><dt>工具版本</dt><dd><code>v{{ selectedData.version }}</code></dd></div>
          <div><dt>Docker</dt><dd><code>{{ selectedToolSpec?.container?.image ?? selectedData.dockerImage ?? '未定义' }}</code></dd></div>
        </dl>
      </section>

      <section v-if="selectedData.kind === 'subworkflow'" class="inspector-section">
        <div class="subworkflow-lock">
          <span aria-hidden="true">◆</span>
          <div><strong>固定版本黑盒</strong><small>内部结构在父流程中只读</small></div>
        </div>
        <p class="inspector-description">
          {{ selectedData.description || '该子流程暂未填写说明。' }}
        </p>
        <dl class="property-list">
          <div><dt>子流程</dt><dd><code>{{ selectedData.subworkflowSlug }}</code></dd></div>
          <div><dt>固定版本</dt><dd><code>v{{ selectedData.version }}</code></dd></div>
          <div><dt>语义摘要</dt><dd><code :title="selectedData.subworkflowDigest">{{ selectedData.subworkflowDigest?.slice(0, 18) }}…</code></dd></div>
        </dl>
        <div class="subworkflow-actions">
          <button type="button" class="secondary-button" @click="emit('openSubworkflow')">查看内部流程</button>
          <span>如需修改，请打开子流程编辑器并发布新版本；父流程需主动升级。</span>
        </div>
        <div class="subworkflow-upgrade">
          <div class="subworkflow-upgrade__heading">
            <div>
              <strong>版本升级</strong>
              <small>当前固定 v{{ selectedData.version }}</small>
            </div>
            <span v-if="availableSubworkflowUpgrades.length">
              {{ availableSubworkflowUpgrades.length }} 个可选版本
            </span>
            <span v-else class="subworkflow-upgrade__current">已是最新</span>
          </div>

          <template v-if="selectedSubworkflowUpgrade && selectedSubworkflowUpgradeInfo">
            <label class="subworkflow-upgrade__selector">
              <span>目标版本</span>
              <select
                :value="selectedSubworkflowUpgrade.version"
                :disabled="subworkflowUpgradeState === 'saving'"
                @change="emit('selectSubworkflowUpgrade', $event)"
              >
                <option
                  v-for="version in availableSubworkflowUpgrades"
                  :key="`${version.slug}-${version.version}`"
                  :value="version.version"
                >
                  v{{ version.version }} · {{ version.semantic_digest.slice(0, 12) }}
                </option>
              </select>
            </label>

            <div class="subworkflow-upgrade__diff">
              <div>
                <span>输入</span>
                <code>
                  +{{ selectedSubworkflowUpgradeInfo.inputs.added.length }}
                  −{{ selectedSubworkflowUpgradeInfo.inputs.removed.length }}
                  · {{ selectedSubworkflowUpgradeInfo.inputs.changed.length }} 变更
                </code>
              </div>
              <div>
                <span>输出</span>
                <code>
                  +{{ selectedSubworkflowUpgradeInfo.outputs.added.length }}
                  −{{ selectedSubworkflowUpgradeInfo.outputs.removed.length }}
                  · {{ selectedSubworkflowUpgradeInfo.outputs.changed.length }} 变更
                </code>
              </div>
              <p>
                {{ selectedSubworkflowUpgradeInfo.details.join('；') || '接口字段未发生变化。' }}
              </p>
            </div>

            <p
              class="subworkflow-upgrade__compatibility"
              :class="{
                'subworkflow-upgrade__compatibility--warning':
                  !selectedSubworkflowUpgradeInfo.likelyCompatible,
              }"
            >
              <span aria-hidden="true">
                {{ selectedSubworkflowUpgradeInfo.likelyCompatible ? '✓' : '!' }}
              </span>
              {{
                selectedSubworkflowUpgradeInfo.likelyCompatible
                  ? '未发现破坏性接口变化；保存后仍会验证父流程。'
                  : '接口可能影响现有连接；升级会保存新的固定引用，再验证父流程。'
              }}
            </p>

            <button
              type="button"
              class="button button--primary subworkflow-upgrade__button"
              :disabled="
                subworkflowUpgradeState === 'saving'
                || saveState === 'saving'
                || isWorkflowSwitching
              "
              @click="emit('upgradeSubworkflow')"
            >
              {{
                subworkflowUpgradeState === 'saving'
                  && subworkflowUpgradeNodeId === selectedNodeId
                  ? '正在保存并验证…'
                  : `升级到 v${selectedSubworkflowUpgrade.version}`
              }}
            </button>
          </template>
          <p v-else class="subworkflow-upgrade__empty">
            没有更高的已发布版本；当前引用不会自动变化。
          </p>

          <p
            v-if="
              subworkflowUpgradeMessage
              && subworkflowUpgradeNodeId === selectedNodeId
            "
            class="subworkflow-upgrade__feedback"
            :class="`subworkflow-upgrade__feedback--${subworkflowUpgradeState}`"
            aria-live="polite"
          >
            {{ subworkflowUpgradeMessage }}
          </p>
        </div>
      </section>

      <section v-if="selectedData.kind === 'tool' || selectedData.kind === 'subworkflow'" class="inspector-section">
        <div class="section-heading"><h3>输入契约</h3><span>{{ selectedData.inputs?.length ?? 0 }}</span></div>
        <div class="contract-list">
          <div v-for="port in (selectedData.kind === 'tool' ? selectedToolSpec?.inputs : undefined) ?? selectedData.inputs" :key="`inspector-input-${port.name}`">
            <span class="port-direction port-direction--input">输入</span>
            <strong>{{ port.label ?? port.name }}</strong>
            <code>{{ port.wdl_type ?? port.type }} · {{ port.semantic_type ?? port.semanticType ?? '未声明语义类型' }}</code>
          </div>
        </div>
        <div class="section-heading"><h3>输出契约</h3><span>{{ selectedData.outputs?.length ?? 0 }}</span></div>
        <div class="contract-list">
          <div v-for="port in (selectedData.kind === 'tool' ? selectedToolSpec?.outputs : undefined) ?? selectedData.outputs" :key="`inspector-output-${port.name}`">
            <span class="port-direction port-direction--output">输出</span>
            <strong>{{ port.label ?? port.name }}</strong>
            <code>{{ port.wdl_type ?? port.type }} · {{ port.semantic_type ?? port.semanticType ?? '未声明语义类型' }}</code>
          </div>
        </div>
        <p class="contract-help">连线时输出与输入的 WDL 类型及语义类型必须兼容；端口名称对应画布节点两侧的连接点。</p>
      </section>

      <section v-if="selectedData.kind === 'tool'" class="inspector-section">
        <div class="section-heading">
          <h3>参数</h3>
          <span>{{ selectedToolParameters.length }}</span>
        </div>
        <p v-if="selectedToolParameters.length === 0" class="contract-help">
          该工具没有可直接填写的标量参数；文件类输入请通过画布端口连接。
        </p>
        <label
          v-for="port in selectedToolParameters"
          :key="`parameter-${port.name}`"
          class="field parameter-field"
        >
          <span>
            {{ port.label ?? port.name }}
            <code>{{ port.wdl_type }}</code>
          </span>
          <select
            v-if="port.wdl_type === 'Boolean'"
            :value="String(parameterDisplayValue(port))"
            :disabled="isParameterConnected(port.name)"
            @change="emit('updateToolParameter', port, $event)"
          >
            <option value="">使用工具默认值</option>
            <option value="true">true</option>
            <option value="false">false</option>
          </select>
          <input
            v-else
            :value="parameterDisplayValue(port)"
            :type="port.wdl_type === 'Int' || port.wdl_type === 'Float' ? 'number' : 'text'"
            :step="port.wdl_type === 'Float' ? 'any' : undefined"
            :min="port.constraints?.minimum"
            :max="port.constraints?.maximum"
            :disabled="isParameterConnected(port.name)"
            :placeholder="port.wdl_type.startsWith('Array[') ? '多个值用逗号分隔' : undefined"
            @change="emit('updateToolParameter', port, $event)"
          />
          <small v-if="isParameterConnected(port.name)">该参数由上游端口提供，断开连线后才能手工填写。</small>
          <small v-else-if="port.constraints?.minimum !== undefined || port.constraints?.maximum !== undefined">
            允许范围 {{ port.constraints?.minimum ?? '不限' }}–{{ port.constraints?.maximum ?? '不限' }}
          </small>
          <small v-else-if="port.default !== undefined">工具默认值：{{ Array.isArray(port.default) ? port.default.join(', ') : port.default }}</small>
        </label>
      </section>

      <section class="inspector-section">
        <h3>类型</h3>
        <dl class="property-list">
          <div>
            <dt>节点类型</dt>
            <dd>{{ selectedData.kind }}</dd>
          </div>
          <div v-if="selectedData.semanticType">
            <dt>语义类型</dt>
            <dd><code>{{ selectedData.semanticType }}</code></dd>
          </div>
          <div v-if="selectedData.version">
            <dt>{{ selectedData.kind === 'subworkflow' ? '子流程版本' : '工具版本' }}</dt>
            <dd><code>{{ selectedData.version }}</code></dd>
          </div>
        </dl>
      </section>
    </div>

    <div v-else-if="inspectorTab === 'diagnostics'" class="inspector-content">
      <section v-if="diagnostics.length === 0" class="diagnostic diagnostic--success">
        <div class="diagnostic__icon" aria-hidden="true">✓</div>
        <div>
          <strong>验证通过</strong>
          <p>当前 Workflow Graph 没有发现错误或提醒。</p>
        </div>
      </section>
      <template v-else>
        <section v-for="item in diagnostics" :key="`${item.code}-${item.message}`" class="diagnostic">
          <div class="diagnostic__icon" aria-hidden="true">!</div>
          <div>
            <strong>{{ item.code }}</strong>
            <p>{{ item.message }}</p>
            <code>{{ item.stage }}<template v-if="item.location?.node_id"> · {{ item.location.node_id }}</template></code>
          </div>
        </section>
      </template>
    </div>

    <div v-else-if="inspectorTab === 'artifacts'" class="inspector-content">
      <p v-if="artifacts.length === 0" class="empty-state empty-state--inspector">
        尚无编译产物。验证通过后点击“编译流程”。
      </p>
      <template v-else>
        <section v-for="artifact in artifacts" :key="artifact.name" class="artifact-card">
          <div>
            <strong>{{ artifact.name }}</strong>
            <small>{{ artifact.media_type }}</small>
          </div>
          <code>{{ artifact.digest.slice(0, 19) }}…</code>
          <div class="artifact-card__actions">
            <button type="button" @click="emit('openArtifact', artifact)">预览</button>
            <a
              :href="`data:${artifact.media_type};charset=utf-8,${encodeURIComponent(artifact.content)}`"
              :download="artifact.name"
            >下载</a>
          </div>
        </section>
      </template>
    </div>

    <div v-else class="empty-state empty-state--inspector">
      在画布中选择一个节点查看属性。
    </div>
  </aside>
</template>
