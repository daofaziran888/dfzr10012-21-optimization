import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/sink'
const columns = [
  "配置编号",
  "输出类型",
  "目标地址",
  "写入模式",
  "批处理大小",
  "刷新间隔",
  "重试策略",
  "配置状态"
]
const actions = [
  "启用输出",
  "暂停输出",
  "关闭输出"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "输出配置总数",
    "value": 0
  },
  {
    "label": "已启用输出",
    "value": 0
  },
  {
    "label": "已暂停输出",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type BackfillRow = Record<string, string | number | boolean | null>

export default function SinkView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [sharedRows, setSharedRows] = useState<BackfillRow[]>([])
  const [sharedSink, setSharedSink] = useState('')
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('输出配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流数据输出端列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function loadSharedBackfills(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/backfills`)
      if (!response.ok) {
        throw new Error('共享字段回填结果读取失败')
      }
      const payload = await response.json()
      setSharedRows(payload.items ?? [])
      setSharedSink(String(row['配置编号'] ?? row.id))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '共享字段回填结果读取失败')
    }
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('流数据输出端动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '流数据输出端操作失败')
    }
  }

  return (
    <section className="page" data-module="sink">
      <header className="page-head">
        <div>
          <h2>流数据输出端管理</h2>
          <p className="page-desc">维护流输出的目标地址、写入模式与批处理配置，为流数据输出端管理提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('输出配置登记入口尚未接入审批流')}>
            登记输出配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出流数据输出端清单
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

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

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
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void loadSharedBackfills(row)}>
                  共享回填结果
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无流数据输出端数据，可先登记输出配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {sharedSink && (
        <section className="page" style={{ marginTop: 16 }}>
          <header className="page-head">
            <div>
              <h3>{sharedSink} 共享的字段回填结果</h3>
              <p className="page-desc">输出端消费侧按记录编号关联同一份回填结果，不另建版本台账；失败列保留旧映射。</p>
            </div>
            <div className="page-actions">
              <button className="btn ghost" type="button" onClick={() => { setSharedRows([]); setSharedSink('') }}>
                关闭
              </button>
            </div>
          </header>
          <table className="data-table">
            <thead>
              <tr>
                {['记录编号', '回填批次', '字段名称', '原存储类型', '原默认值', '目标版本', '升级场景', '兼容判定', '状态'].map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sharedRows.map((row) => (
                <tr key={String(row.id)}>
                  {['记录编号', '回填批次', '字段名称', '原存储类型', '原默认值', '目标版本', '升级场景', '兼容判定', 'status'].map((column) => (
                    <td key={column}>{String(row[column] ?? '—')}</td>
                  ))}
                </tr>
              ))}
              {sharedRows.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={9}>该输出配置暂无共享回填结果</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条流数据输出端记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
