<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button v-if="meta.importable" class="btn primary" data-testid="open-import" type="button" @click="openImport">
          导入分区策略文件
        </button>
        <button v-else class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button v-if="meta.key === 'lake_table'" class="btn" data-testid="register-partitions" type="button" @click="registerPendingPartitions(null)">
          补录待登记分区
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-if="meta.importable"
              class="link"
              type="button"
              :data-testid="`detail-${row.id}`"
              @click="inspectPack(row)"
            >
              查看明细
            </button>
            <button
              v-if="meta.key === 'lake_table' && row.status === '已注册'"
              class="link"
              type="button"
              :data-testid="`partitions-${row.id}`"
              @click="registerPendingPartitions(row)"
            >
              补录分区
            </button>
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showImport" class="modal-backdrop" data-testid="import-modal">
      <form class="modal" @submit.prevent="submitImport">
        <h3>导入分区策略文件</h3>
        <p class="page-desc">
          支持 CSV / TSV / JSON / JSONL，需包含目标表、日期、分区粒度三列（可选分区字段）。
          逐行校验后按「表 + 日期」保留唯一生效规则。
        </p>
        <label class="create-field">
          <span>分区策略文件</span>
          <input data-testid="import-file" type="file" accept=".csv,.tsv,.txt,.json,.jsonl" @change="onFileSelected" />
        </label>
        <label class="create-field">
          <span>默认目标湖表（文件未写目标表时使用）</span>
          <input v-model="importDefaults.defaultTable" data-testid="import-default-table" />
        </label>
        <p v-if="importFileName" class="page-desc">已选择：{{ importFileName }}</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showImport = false">取消</button>
          <button class="btn primary" data-testid="submit-import" type="submit" :disabled="importing || !importFileName">
            {{ importing ? '导入中…' : '确认导入' }}
          </button>
        </div>
        <p v-if="importMessage" class="page-desc" data-testid="import-message">{{ importMessage }}</p>
      </form>
    </div>

    <div v-if="detailPack" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal">
        <h3>{{ detailPack['规则包名称'] }} · 规则明细</h3>
        <p class="page-desc">
          来源文件 {{ detailPack['来源文件'] }}，共 {{ detailLines.length }} 行。
          每行保留文件行号与生效状态，失败行不会进入湖表分区。
        </p>
        <table class="data-table">
          <thead>
            <tr><th>行号</th><th>目标湖表</th><th>日期</th><th>粒度</th><th>分区键</th><th>状态</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="line in detailLines" :key="String(line.id)">
              <td>{{ line['行号'] }}</td>
              <td>{{ line['目标湖表'] }}</td>
              <td>{{ line['日期'] }}</td>
              <td>{{ line['分区粒度'] }}</td>
              <td>{{ line['分区键'] || '—' }}</td>
              <td>{{ line.status }}</td>
              <td>{{ line['失败原因'] || line['状态说明'] }}</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions">
          <button class="btn" type="button" @click="detailPack = null">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  fetchRulePackLines,
  importRulePack,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import { request } from '@/api/client'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showImport = ref(false)
const importing = ref(false)
const importFileName = ref('')
const importContent = ref('')
const importMessage = ref('')
const importDefaults = ref({ defaultTable: '' })
const detailPack = ref<EntryRow | null>(null)
const detailLines = ref<EntryRow[]>([])
const stats = computed(() => {
  if (meta.importable) {
    return [
      { label: meta.metrics[0], value: total.value },
      { label: meta.metrics[1], value: rows.value.filter((row) => row.pending).length },
      { label: meta.metrics[2], value: rows.value.filter((row) => row.abnormal).length },
    ]
  }
  return [
    { label: meta.metrics[0], value: total.value },
    { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
    {
      label: meta.metrics[2],
      value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
    },
  ]
})
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

function openImport() {
  importFileName.value = ''
  importContent.value = ''
  importMessage.value = ''
  importDefaults.value.defaultTable = ''
  errorMessage.value = ''
  showImport.value = true
}

function onFileSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  importFileName.value = file?.name ?? ''
  importContent.value = ''
  importMessage.value = ''
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    importContent.value = String(reader.result ?? '')
  }
  reader.readAsText(file)
}

async function submitImport() {
  importing.value = true
  importMessage.value = ''
  errorMessage.value = ''
  try {
    if (!importContent.value) throw new Error('文件内容尚未读取完成，请重新选择文件')
    const result = await importRulePack(
      importFileName.value,
      importContent.value,
      importDefaults.value.defaultTable,
    )
    importMessage.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导入失败'
  } finally {
    importing.value = false
  }
}

async function inspectPack(row: EntryRow) {
  errorMessage.value = ''
  try {
    const payload = await fetchRulePackLines(Number(row.id))
    detailPack.value = payload.pack
    detailLines.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '规则包明细读取失败'
  }
}

async function registerPendingPartitions(row: EntryRow | null = null) {
  errorMessage.value = ''
  try {
    const id = row ? Number(row.id) : Number((prompt('要补录待登记分区的湖表 id') ?? '').trim())
    if (!id) return
    const response = await request(`/api/lake_table/${id}/register-partitions`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = (await response.json()) as { message?: string; detail?: string }
    if (!response.ok) throw new Error(payload.detail ?? '分区补录失败')
    errorMessage.value = payload.message ?? ''
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '分区补录失败'
  }
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
