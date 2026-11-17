<!-- -*- coding: utf-8 -*- -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Delete, Edit, Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { adapterApi, type AdapterStatus, type QQBotPayload, type RuntimeConfigPayload } from '@/api'

const DEFAULT_INTENTS = 251658240
const DEFAULT_API_BASE = 'https://api.bot.qq.com'
const DEFAULT_AUTH_URL = 'https://api.bot.qq.com/app/getAppAccessToken'

interface IntentOption {
  bit: number
  label: string
  help: string
}

const INTENT_OPTIONS: IntentOption[] = [
  { bit: 1 << 24, label: '群成员变动', help: '群成员加入/退出、入群申请；入群验证和退群欢迎需要' },
  { bit: 1 << 25, label: '群与 C2C 消息', help: '群聊 @机器人、群消息和私聊消息；QQ 群机器人必需' },
  { bit: 1 << 26, label: '交互事件', help: '按钮、菜单等交互回调' },
  { bit: 1 << 27, label: '消息审核', help: '消息审核结果事件' },
]

const CHANNEL_INTENT_OPTIONS: IntentOption[] = [
  { bit: 1 << 0, label: '频道事件', help: '频道创建、更新、删除等' },
  { bit: 1 << 1, label: '频道成员', help: '频道成员加入/退出' },
  { bit: 1 << 9, label: '频道消息（私域）', help: '私域机器人的频道消息' },
  { bit: 1 << 10, label: '频道表情表态', help: '频道消息的表情表态' },
  { bit: 1 << 12, label: '频道私信', help: '频道私信消息' },
  { bit: 1 << 28, label: '论坛事件', help: '频道论坛主题和回复' },
  { bit: 1 << 30, label: '公域频道消息', help: '公域机器人的频道 @ 消息' },
]

const INTENT_OPTIONS_ALL = [...INTENT_OPTIONS, ...CHANNEL_INTENT_OPTIONS]
const INTENT_MASK = INTENT_OPTIONS_ALL.reduce((mask, option) => mask | option.bit, 0)

interface BotDraft {
  app_id: string
  secret: string
  mode: 'webhook' | 'websocket'
  bot_user_id: string
  intents: number
  shard_index: number | null
  shard_total: number | null
  gateway_url: string
  api_base: string
  auth_url: string
}

const loading = ref(false)
const saving = ref(false)
const loaded = ref(false)
const config = ref<RuntimeConfigPayload | null>(null)
const revision = ref('')
const online = ref<AdapterStatus[]>([])
const warning = ref('')
const applied = ref<RuntimeConfigPayload | null>(null)
const appliedToken = ref('')
const initialHost = ref('')
const initialPort = ref(0)
const botSaving = ref(false)

const dialogVisible = ref(false)
const editingAppId = ref<string | null>(null)
const draft = ref<BotDraft>(emptyDraft())

const qqBots = computed(() =>
  Object.entries(config.value?.qq_bots || {})
    .map(([appId, settings]) => ({ appId, ...settings }))
    .sort((a, b) => Number(a.appId) - Number(b.appId)),
)

const onlineKeys = computed(() => new Set(online.value.map((item) => `${item.adapter}:${item.self_id}`)))
const onebotBots = computed(() => online.value.filter((item) => item.adapter.startsWith('onebot')))
const isBotOnline = (appId: string) => onlineKeys.value.has(`qq:${appId}`)
const botStatusLabel = (appId: string, mode: string) =>
  isBotOnline(appId) ? '在线' : mode === 'webhook' ? '待事件' : '离线'
const dirty = computed(
  () => !!config.value && !!applied.value && JSON.stringify(config.value) !== JSON.stringify(applied.value),
)
const restartNeeded = computed(
  () =>
    !!config.value && (config.value.host !== initialHost.value || config.value.port !== initialPort.value),
)
const onebotEndpoint = computed(() =>
  config.value ? `ws://${config.value.host}:${config.value.port}/onebot/v11/ws` : '',
)

function emptyDraft(): BotDraft {
  return {
    app_id: '',
    secret: '',
    mode: 'webhook',
    bot_user_id: '',
    intents: DEFAULT_INTENTS,
    shard_index: null,
    shard_total: null,
    gateway_url: '',
    api_base: DEFAULT_API_BASE,
    auth_url: DEFAULT_AUTH_URL,
  }
}

watch(
  () => draft.value.mode,
  (mode) => {
    // Webhook 不接受分片；切换后清空，避免保存时被服务端拒绝。
    if (mode === 'webhook') {
      draft.value.shard_index = null
      draft.value.shard_total = null
    }
  },
)

const selectedIntents = computed<number[]>({
  get: () =>
    INTENT_OPTIONS_ALL.filter((option) => ((draft.value.intents || 0) & option.bit) !== 0).map(
      (option) => option.bit,
    ),
  set: (bits) => {
    // 未列出的权限位原样保留，避免控制台保存时丢掉自定义 intents。
    const residual = (draft.value.intents || 0) & ~INTENT_MASK
    draft.value.intents = residual | bits.reduce((mask, bit) => mask | bit, 0)
  },
})

const residualIntents = computed(() => (draft.value.intents || 0) & ~INTENT_MASK)

const resetIntents = () => {
  draft.value.intents = DEFAULT_INTENTS
}

const clearResidualIntents = () => {
  draft.value.intents &= INTENT_MASK
}

const maskSecret = (secret: string) => {
  if (!secret) return '未填写'
  if (secret.length <= 6) return '••••••'
  return `${secret.slice(0, 3)}••••${secret.slice(-3)}`
}

const modeLabel = (mode: string) => (mode === 'websocket' ? 'WebSocket' : 'Webhook')

const applyResponse = (
  data: { config: RuntimeConfigPayload; revision: string; online: AdapterStatus[]; warning?: string | null },
  preserveLocal = false,
) => {
  const local = config.value
  applied.value = data.config
  // 直接保存 QQ Bot 时只替换 Bot 列表，OneBot 侧尚未保存的编辑继续留在表单里。
  config.value =
    preserveLocal && local
      ? { ...data.config, token: local.token, host: local.host, port: local.port }
      : data.config
  revision.value = data.revision
  online.value = data.online || []
  warning.value = data.warning || ''
  appliedToken.value = data.config.token
}

const load = async () => {
  loading.value = true
  try {
    const data = await adapterApi.get()
    applyResponse(data)
    // 首次加载记录服务启动时的监听值；之后重新加载不覆盖，避免重启提示被误清除。
    if (!loaded.value) {
      initialHost.value = data.config.host
      initialPort.value = data.config.port
    }
    loaded.value = true
  } catch (error) {
    console.error('加载适配器配置失败', error)
    ElMessage.error('加载适配器配置失败，请重试')
  } finally {
    loading.value = false
  }
}

const save = async () => {
  if (!config.value || saving.value) return
  const normalized: RuntimeConfigPayload = { ...config.value, host: config.value.host.trim() }
  const invalid = validateConfig(normalized)
  if (invalid) {
    ElMessage.warning(invalid)
    return
  }
  const rotatesToken = normalized.token !== appliedToken.value
  saving.value = true
  try {
    const data = await adapterApi.save(normalized, revision.value)
    applyResponse(data)
    ElMessage.success('适配器配置已保存')
    if (rotatesToken) ElMessage.info('OneBot 令牌已更新；已有连接会被断开，客户端重连时需要使用新令牌')
    if (data.warning) ElMessage.warning(data.warning)
  } catch (error: any) {
    if (error?.response?.status === 409) {
      ElMessage.error('配置已在其他页面被修改，已重新加载最新配置')
      await load()
    } else {
      ElMessage.error('保存失败：' + (error?.response?.data?.detail || error?.message || error))
    }
  } finally {
    saving.value = false
  }
}

const saveBots = async (bots: Record<string, QQBotPayload>) => {
  if (!applied.value || botSaving.value) return false
  botSaving.value = true
  try {
    // 以服务端最近一次快照为底，只替换 Bot 列表，不把 OneBot 侧未保存的编辑带上去。
    const data = await adapterApi.save({ ...applied.value, qq_bots: bots }, revision.value)
    applyResponse(data, true)
    ElMessage.success('QQ Bot 配置已保存并生效')
    if (data.warning) ElMessage.warning(data.warning)
    return true
  } catch (error: any) {
    if (error?.response?.status === 409) {
      ElMessage.error('配置已在其他页面被修改，已重新加载最新配置')
      await load()
    } else {
      ElMessage.error('保存失败：' + (error?.response?.data?.detail || error?.message || error))
    }
    return false
  } finally {
    botSaving.value = false
  }
}

const validateConfig = (value: RuntimeConfigPayload) => {
  if (!value.host.trim()) return '监听地址不能为空'
  if (!Number.isInteger(value.port) || value.port <= 0 || value.port > 65535) return '监听端口必须在 1-65535 之间'
  if (value.token !== value.token.trim()) return 'OneBot 令牌不能包含首尾空白'
  for (const [appId, bot] of Object.entries(value.qq_bots)) {
    if (!/^[1-9]\d*$/.test(appId)) return `QQ AppID「${appId}」必须是正整数`
    if (!bot.secret || bot.secret !== bot.secret.trim()) return `AppID ${appId} 的 Secret 不能为空或包含首尾空白`
  }
  return ''
}

const openAdd = () => {
  editingAppId.value = null
  draft.value = emptyDraft()
  dialogVisible.value = true
}

const openEdit = (appId: string) => {
  const bot = config.value?.qq_bots[appId]
  if (!bot) return
  editingAppId.value = appId
  const shard = Array.isArray(bot.shard) ? bot.shard : null
  draft.value = {
    app_id: appId,
    secret: bot.secret,
    mode: bot.mode,
    bot_user_id: bot.bot_user_id || '',
    intents: bot.intents || DEFAULT_INTENTS,
    shard_index: shard ? shard[0] : null,
    shard_total: shard ? shard[1] : null,
    gateway_url: bot.gateway_url || '',
    api_base: bot.api_base || DEFAULT_API_BASE,
    auth_url: bot.auth_url || DEFAULT_AUTH_URL,
  }
  dialogVisible.value = true
}

const validateDraft = () => {
  const value = draft.value
  if (!config.value) return '配置尚未加载'
  if (!/^[1-9]\d*$/.test(value.app_id)) return 'AppID 必须是正整数'
  if (!value.secret || value.secret !== value.secret.trim()) return 'Secret 不能为空或包含首尾空白'
  if (!Number.isInteger(value.intents) || value.intents <= 0 || value.intents >= 2 ** 31) {
    return '至少选择一项功能权限'
  }
  if (value.mode === 'webhook' && (value.shard_index != null || value.shard_total != null)) {
    return 'Webhook 模式不能配置分片'
  }
  if (value.shard_index != null || value.shard_total != null) {
    if (value.shard_index == null || value.shard_total == null) return '分片序号和分片总数需要同时填写'
    if (
      !Number.isInteger(value.shard_index) ||
      !Number.isInteger(value.shard_total) ||
      value.shard_total < 1 ||
      value.shard_index < 0 ||
      value.shard_index >= value.shard_total
    ) {
      return '分片序号必须小于分片总数，且总分片数至少为 1'
    }
  }
  if (value.gateway_url && !value.gateway_url.startsWith('wss://')) return '自定义网关地址必须使用 wss://'
  if (!value.api_base.startsWith('https://') || !value.auth_url.startsWith('https://')) {
    return '接口地址和鉴权地址必须使用 https://'
  }
  return ''
}

const confirmBot = async () => {
  const invalid = validateDraft()
  if (invalid) {
    ElMessage.warning(invalid)
    return
  }
  const value = draft.value
  if (editingAppId.value === null && config.value?.qq_bots[value.app_id]) {
    ElMessage.warning('该 AppID 已存在')
    return
  }
  const payload: QQBotPayload = {
    secret: value.secret,
    mode: value.mode,
    bot_user_id: value.bot_user_id.trim(),
    intents: value.intents,
    api_base: value.api_base.trim(),
    auth_url: value.auth_url.trim(),
  }
  if (value.shard_index != null && value.shard_total != null) {
    payload.shard = [value.shard_index, value.shard_total]
  }
  if (value.gateway_url.trim()) payload.gateway_url = value.gateway_url.trim()
  if (!config.value) return
  const bots = { ...config.value.qq_bots }
  if (editingAppId.value !== null && editingAppId.value !== value.app_id) delete bots[editingAppId.value]
  bots[value.app_id] = payload
  if (await saveBots(bots)) dialogVisible.value = false
}

const removeBot = async (appId: string) => {
  try {
    await ElMessageBox.confirm(`确定删除 QQ Bot「${appId}」？删除后立即生效。`, '删除 QQ Bot', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch (action) {
    if (action === 'cancel' || action === 'close') return
    throw action
  }
  if (!config.value) return
  const bots = { ...config.value.qq_bots }
  delete bots[appId]
  await saveBots(bots)
}

const copyEndpoint = async () => {
  if (!onebotEndpoint.value) return
  try {
    await navigator.clipboard.writeText(onebotEndpoint.value)
    ElMessage.success('已复制 OneBot 连接地址')
  } catch (error) {
    console.error('复制失败', error)
    ElMessage.error('复制失败，请手动选择地址')
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="adapters-view">
    <PageHeader title="适配器配置" subtitle="OneBot 接入令牌与 QQ 官方 Bot 凭证">
      <template #actions>
        <el-button :icon="Refresh" :loading="loading" :disabled="saving || botSaving" @click="load">重新加载</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!loaded || !dirty || loading || botSaving"
          @click="save"
        >
          保存
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="warning" class="adapter-alert" type="warning" :closable="false" show-icon :title="warning" />
    <el-alert
      v-if="restartNeeded"
      class="adapter-alert"
      type="warning"
      :closable="false"
      show-icon
      title="监听地址或端口已修改，保存后需要重启 FxBot 才会使用新的监听地址。"
    />

    <div v-if="!loaded" v-loading="loading" class="adapter-loading" />

    <template v-else-if="config">
      <el-card class="adapter-card" shadow="never">
        <template #header>
          <div class="card-header">
            <div>
              <span class="card-title">OneBot 接入</span>
              <span class="card-desc">NapCat 等 OneBot V11/V12 实现通过反向 WebSocket 连接到本机</span>
            </div>
            <el-tag v-if="onebotBots.length" type="success" size="small">
              {{ onebotBots.length }} 个在线
            </el-tag>
            <el-tag v-else type="info" size="small">暂无在线连接</el-tag>
          </div>
        </template>

        <el-form label-position="top" class="adapter-form">
          <el-row :gutter="32">
            <el-col :xs="24" :sm="24" :md="12" :lg="8">
              <el-form-item>
                <template #label>
                  <div class="label-row">
                    <span>访问令牌</span>
                    <el-tooltip
                      content="OneBot V11/V12 连接时的 Authorization: Bearer 令牌。留空表示不鉴权，此时不要监听公网地址。保存新令牌会主动断开现有 OneBot 连接，客户端需要改用新令牌重连。"
                      placement="top"
                    >
                      <span class="help-dot">?</span>
                    </el-tooltip>
                  </div>
                </template>
                <el-input
                  v-model="config.token"
                  type="password"
                  show-password
                  clearable
                  placeholder="留空表示不校验"
                />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="6" :lg="5">
              <el-form-item label="监听地址">
                <el-input v-model="config.host" placeholder="127.0.0.1" />
              </el-form-item>
            </el-col>
            <el-col :xs="24" :sm="12" :md="6" :lg="5">
              <el-form-item label="监听端口">
                <el-input-number v-model="config.port" :min="1" :max="65535" controls-position="right" style="width: 100%" />
              </el-form-item>
            </el-col>
          </el-row>

          <el-form-item label="反向 WebSocket 地址">
            <div class="endpoint-row">
              <code class="endpoint">{{ onebotEndpoint }}</code>
              <el-button :icon="CopyDocument" size="small" @click="copyEndpoint">复制</el-button>
            </div>
            <div class="field-help">
              V12 使用 <code>/onebot/v12/{platform}/{bot_id}/ws</code>；连接时需要在请求头携带
              <code>Authorization: Bearer &lt;令牌&gt;</code>。
            </div>
          </el-form-item>

          <el-form-item v-if="onebotBots.length" label="在线 OneBot Bot">
            <div class="bot-chips">
              <el-tag v-for="bot in onebotBots" :key="`${bot.adapter}:${bot.self_id}`" size="small">
                {{ bot.adapter }} · {{ bot.self_id }}
              </el-tag>
            </div>
          </el-form-item>
        </el-form>
      </el-card>

      <el-card class="adapter-card" shadow="never">
        <template #header>
          <div class="card-header">
            <div>
              <span class="card-title">QQ 官方 Bot</span>
              <span class="card-desc">
                每个 AppID 只能选择 Webhook 或 WebSocket 一种接入方式；Secret 只用于本地验签和换取 access token
              </span>
            </div>
            <el-button type="primary" size="small" :icon="Plus" :disabled="botSaving" @click="openAdd">
              添加 Bot
            </el-button>
          </div>
        </template>

        <el-empty v-if="!qqBots.length" description="还没有配置 QQ 官方 Bot" :image-size="72" />

        <el-table v-else :data="qqBots" row-key="appId">
          <el-table-column prop="appId" label="AppID" min-width="130" />
          <el-table-column label="接入方式" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="row.mode === 'websocket' ? 'warning' : 'info'">
                {{ modeLabel(row.mode) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <span class="status-cell" :class="{ online: isBotOnline(row.appId) }">
                <span class="status-dot" />
                {{ botStatusLabel(row.appId, row.mode) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="Secret" min-width="150">
            <template #default="{ row }">{{ maskSecret(row.secret) }}</template>
          </el-table-column>
          <el-table-column prop="bot_user_id" label="Bot 用户 ID" min-width="140">
            <template #default="{ row }">{{ row.bot_user_id || '自动获取' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="170" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" :icon="Edit" :disabled="botSaving" @click="openEdit(row.appId)">
                编辑
              </el-button>
              <el-button link type="danger" :icon="Delete" :disabled="botSaving" @click="removeBot(row.appId)">
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </template>

    <el-dialog
      v-model="dialogVisible"
      :title="editingAppId === null ? '添加 QQ Bot' : `编辑 QQ Bot ${editingAppId}`"
      width="620"
      destroy-on-close
      append-to-body
      :show-close="!botSaving"
      :close-on-click-modal="false"
      :close-on-press-escape="!botSaving"
    >
      <el-form label-position="top" class="bot-form">
        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="AppID" required>
              <el-input v-model="draft.app_id" :disabled="editingAppId !== null" placeholder="QQ 开放平台 AppID" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="AppSecret" required>
              <el-input v-model="draft.secret" type="password" show-password placeholder="QQ 开放平台 AppSecret" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-form-item label="接入方式" required>
              <el-radio-group v-model="draft.mode">
                <el-radio value="webhook" border>Webhook</el-radio>
                <el-radio value="websocket" border>WebSocket</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Bot 用户 ID">
              <el-input v-model="draft.bot_user_id" placeholder="留空时自动查询" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item>
          <template #label>
            <div class="label-row">
              <span>功能权限（Intents）</span>
              <el-tooltip
                content="决定 Bot 订阅哪些 QQ 开放平台事件。权限值由勾选项自动合成，不需要手算。"
                placement="top"
              >
                <span class="help-dot">?</span>
              </el-tooltip>
              <el-tag size="small" type="info" class="intent-value">{{ draft.intents }}</el-tag>
            </div>
          </template>
          <el-checkbox-group v-model="selectedIntents" class="intent-options">
            <el-tooltip v-for="option in INTENT_OPTIONS" :key="option.bit" :content="option.help" placement="top">
              <el-checkbox :value="option.bit" border>{{ option.label }}</el-checkbox>
            </el-tooltip>
          </el-checkbox-group>
          <div class="field-help">
            默认只勾选群聊和私聊相关权限。QQ 群机器人至少勾选「群与 C2C 消息」；需要入群验证或
            退群处理时再勾选「群成员变动」。频道权限在高级设置里，默认不勾选。
            <el-button link type="primary" size="small" @click="resetIntents">恢复默认</el-button>
          </div>
          <div v-if="residualIntents" class="field-help">
            当前配置还包含未列出的权限位 {{ residualIntents }}，保存时会原样保留。
            <el-button link type="primary" size="small" @click="clearResidualIntents">清除</el-button>
          </div>
        </el-form-item>

        <el-collapse>
          <el-collapse-item title="高级设置" name="advanced">
            <el-form-item label="频道场景权限">
              <el-checkbox-group v-model="selectedIntents" class="intent-options">
                <el-tooltip
                  v-for="option in CHANNEL_INTENT_OPTIONS"
                  :key="option.bit"
                  :content="option.help"
                  placement="top"
                >
                  <el-checkbox :value="option.bit" border>{{ option.label }}</el-checkbox>
                </el-tooltip>
              </el-checkbox-group>
              <div class="field-help">仅在接入 QQ 频道时需要，默认不勾选；QQ 群机器人保持全不选即可。</div>
            </el-form-item>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="分片序号">
                  <el-input-number
                    v-model="draft.shard_index"
                    :min="0"
                    :step="1"
                    controls-position="right"
                    style="width: 100%"
                    :disabled="draft.mode === 'webhook'"
                    placeholder="留空自动"
                  />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="分片总数">
                  <el-input-number
                    v-model="draft.shard_total"
                    :min="1"
                    :step="1"
                    controls-position="right"
                    style="width: 100%"
                    :disabled="draft.mode === 'webhook'"
                    placeholder="留空自动"
                  />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="自定义网关地址（WSS）">
              <el-input v-model="draft.gateway_url" placeholder="留空使用官方网关" />
            </el-form-item>
            <el-row :gutter="20">
              <el-col :span="12">
                <el-form-item label="接口地址">
                  <el-input v-model="draft.api_base" placeholder="https://api.bot.qq.com" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="鉴权地址">
                  <el-input v-model="draft.auth_url" placeholder="https://api.bot.qq.com/app/getAppAccessToken" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-collapse-item>
        </el-collapse>
      </el-form>

      <template #footer>
        <el-button :disabled="botSaving" @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="botSaving" @click="confirmBot">
          {{ editingAppId === null ? '添加并生效' : '确定并生效' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.adapters-view {
  padding: 24px 28px 32px;
}

.adapter-alert {
  margin-bottom: 16px;
}

.adapter-loading {
  min-height: 240px;
}

.adapter-card {
  margin-bottom: 20px;
  border: 1px solid var(--color-border) !important;
  border-radius: var(--radius-lg) !important;
  box-shadow: none !important;

  :deep(.el-card__header) {
    padding: 14px 20px;
    border-bottom: 1px solid var(--color-border-light);
  }

  :deep(.el-card__body) {
    padding: 20px;
  }
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}

.card-title {
  color: var(--color-text);
  font-size: 15px;
  font-weight: 600;
}

.card-desc {
  margin-left: 10px;
  color: var(--color-text-tertiary);
  font-size: 12px;
}

.adapter-form {
  :deep(.el-form-item) {
    margin-bottom: 18px;
  }

  :deep(.el-form-item__label) {
    color: var(--color-text);
    font-size: 13px;
    font-weight: 600;
  }
}

.label-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.help-dot {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1;
  cursor: help;
}

.endpoint-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  width: 100%;
}

.endpoint {
  padding: 5px 10px;
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  background: var(--color-bg-tertiary);
  color: var(--color-text-secondary);
  font-size: 13px;
  word-break: break-all;
}

.field-help {
  margin-top: 6px;
  color: var(--color-text-tertiary);
  font-size: 12px;
  line-height: 1.6;

  code {
    padding: 1px 4px;
    border-radius: 3px;
    background: var(--color-bg-tertiary);
  }
}

.bot-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.intent-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 10px;
  width: 100%;

  :deep(.el-checkbox) {
    margin-right: 0;
  }
}

.intent-value {
  font-variant-numeric: tabular-nums;
}

.status-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--color-text-tertiary);
  font-size: 12px;

  .status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--color-text-tertiary);
  }

  &.online {
    color: var(--color-success);

    .status-dot {
      background: var(--color-success);
    }
  }
}

.bot-form {
  :deep(.el-form-item__label) {
    color: var(--color-text);
    font-size: 13px;
    font-weight: 600;
  }
}
</style>
