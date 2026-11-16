import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/backfill'
const columns = [
  '记录编号',
  '回填批次',
  '定义编号',
  '字段名称',
  '配置编号',
  '原存储类型',
  '原默认值',
  '目标版本',
  '升级场景',
  '归档列',
  '目标类型',
  '兼容判定',
  '状态',
]
const statuses = ['待回填', '回填成功', '回填失败', '已回滚']
const scenarios = ['旧数据库', '新环境', '部署回滚', '依赖兼容']

type Row = Record<string, string | number | boolean | null>
type Filters = { keyword: string; status: string; batch: string; scene: string }

const emptyFilters: Filters = { keyword: '', status: '', batch: '', scene: '' }

export default function BackfillView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [batchInput, setBatchInput] = useState('schema_audit_v1')
  const [itemsInput, setItemsInput] = useState(
    JSON.stringify(
      [
        { 定义编号: 'SCHE-0004', 配置编号: 'SINK-0004', 字段名称: 'record-21a' },
        { 定义编号: 'SCHE-0005', 配置编号: 'SINK-0004', 字段名称: 'record-21b' },
        { 定义编号: 'SCHE-0006', 配置编号: 'SINK-0004', 字段名称: 'record-21c' },
      ],
      null,
      2,
    ),
  )
  const [sceneInput, setSceneInput] = useState('旧数据库')

  const stats = useMemo(() => {
    const success = rows.filter((row) => row.status === '回填成功').length
    const failed = rows.filter((row) => row.status === '回填失败').length
    return [
      { label: '本页回填结果', value: rows.length },
      { label: '回填成功', value: success },
      { label: '回填失败（可断点续做）', value: failed },
    ]
  }, [rows])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    if (filters.keyword) params.set('keyword', filters.keyword)
    if (filters.status) params.set('status', filters.status)
    if (filters.batch) params.set('batch', filters.batch)
    if (filters.scene) params.set('scene', filters.scene)
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) {
        throw new Error('字段回填结果读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段回填结果读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function postAction(path: string, values: Record<string, unknown>) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, {
        method: 'POST',
        body: JSON.stringify({ values }),
      })
      const payload = await response.json()
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message ?? '操作未生效，请稍后重试')
      }
      setNotice(payload.message ?? '操作已生效')
      await reload()
      return payload
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '字段回填操作失败')
      return null
    }
  }

  async function runBatch() {
    let items: unknown
    try {
      items = JSON.parse(itemsInput)
    } catch {
      setErrorMessage('回填明细不是合法 JSON，请检查 items 格式')
      return
    }
    if (!Array.isArray(items) || items.length === 0) {
      setErrorMessage('回填明细 items 必须是非空数组')
      return
    }
    await postAction(`${ENDPOINT}/batches`, {
      回填批次: batchInput.trim(),
      升级场景: sceneInput,
      items,
    })
  }

  async function resumeBatch() {
    const batch = window.prompt('输入要断点续做的回填批次', batchInput.trim())
    if (!batch) {
      return
    }
    await postAction(`${ENDPOINT}/batches/resume`, { 回填批次: batch, items: [] })
  }

  async function rollback() {
    const sinkCode = window.prompt('输入要部署回滚的输出配置编号，如 SINK-0005')
    if (!sinkCode) {
      return
    }
    await postAction(`${ENDPOINT}/rollback`, { 配置编号: sinkCode.trim() })
  }

  async function retryRow(row: Row) {
    await postAction(`${ENDPOINT}/${row.id}/actions`, { action: '重试回填' })
  }

  return (
    <section className="page" data-module="backfill">
      <header className="page-head">
        <div>
          <h2>字段回填结果</h2>
          <p className="page-desc">
            读取 SCHE 格式定义与 SINK 输出配置字段映射，按字段名称、存储类型和格式版本核对存量字段后批量升级；
            回填结果保留原存储类型、默认值和目标版本，两个业务入口共享同一份结果。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void runBatch()}>
            执行批量回填
          </button>
          <button className="btn" type="button" onClick={() => void resumeBatch()}>
            失败批次断点续做
          </button>
          <button className="btn" type="button" onClick={() => void rollback()}>
            部署回滚
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出回填结果
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form
        className="filter-bar"
        onSubmit={(event) => {
          event.preventDefault()
          void reload()
        }}
      >
        <label className="filter-item">
          <span>记录编号</span>
          <input
            value={filters.keyword}
            placeholder="按记录编号检索"
            onChange={(event) => setFilters((prev) => ({ ...prev, keyword: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>回填批次</span>
          <input
            value={filters.batch}
            placeholder="如 schema_audit_v1"
            onChange={(event) => setFilters((prev) => ({ ...prev, batch: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>升级场景</span>
          <select value={filters.scene} onChange={(event) => setFilters((prev) => ({ ...prev, scene: event.target.value }))}>
            <option value="">全部场景</option>
            {scenarios.map((scene) => (
              <option key={scene} value={scene}>{scene}</option>
            ))}
          </select>
        </label>
        <label className="filter-item">
          <span>状态</span>
          <select value={filters.status} onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}>
            <option value="">全部状态</option>
            {statuses.map((status) => (
              <option key={status} value={status}>{status}</option>
            ))}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters(emptyFilters)}>
          重置条件
        </button>
      </form>

      <div className="filter-bar" style={{ flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label className="filter-item">
          <span>回填批次</span>
          <input value={batchInput} onChange={(event) => setBatchInput(event.target.value)} />
        </label>
        <label className="filter-item">
          <span>升级场景</span>
          <select value={sceneInput} onChange={(event) => setSceneInput(event.target.value)}>
            {scenarios.map((scene) => (
              <option key={scene} value={scene}>{scene}</option>
            ))}
          </select>
        </label>
        <label className="filter-item" style={{ flex: 1, minWidth: 320 }}>
          <span>回填明细 items（同一字段并发升级只提交一个版本）</span>
          <textarea
            rows={6}
            style={{ width: '100%', fontFamily: 'monospace' }}
            value={itemsInput}
            onChange={(event) => setItemsInput(event.target.value)}
          />
        </label>
      </div>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{String(row[column] ?? '—')}</td>)}
              <td className="row-actions">
                {row.status === '回填失败' ? (
                  <button className="link" type="button" onClick={() => void retryRow(row)}>
                    重试回填
                  </button>
                ) : (
                  <span className="empty-state">—</span>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>
                暂无字段回填结果，可在上方提交批量回填
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条字段回填结果记录（消费侧按记录编号关联同一份结果）</span>
        {notice && <span className="stat-label">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
