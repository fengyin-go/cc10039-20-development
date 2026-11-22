import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type RulePackImportResult = {
  ok: boolean
  duplicate: boolean
  message: string
  pack: EntryRow | null
  lines: EntryRow[]
  summary: Record<string, number>
}

export async function importRulePack(
  fileName: string,
  content: string,
  defaultTable = '',
): Promise<RulePackImportResult> {
  const response = await request('/api/partition_rule_pack/import', {
    method: 'POST',
    body: JSON.stringify({
      file_name: fileName,
      content,
      default_table: defaultTable,
    }),
  })
  const payload = (await response.json()) as RulePackImportResult & { detail?: string }
  if (!response.ok) throw new Error(payload.detail ?? '分区策略文件导入失败')
  return payload
}

export async function fetchRulePackLines(packId: number): Promise<{
  pack: EntryRow
  summary: Record<string, number>
  items: EntryRow[]
}> {
  const response = await request(`/api/partition_rule_pack/${packId}/lines`)
  if (!response.ok) throw new Error('规则包明细读取失败')
  return await response.json()
}
