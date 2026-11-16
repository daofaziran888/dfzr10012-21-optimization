import { useEffect, useState } from 'react'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const fallbackCards = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
const fallbackModules = [{"name": "流数据源接入", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流处理算子", "created": 0, "pending": 0, "abnormal": 0}, {"name": "窗口计算配置", "created": 0, "pending": 0, "abnormal": 0}, {"name": "状态后端管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "检查点恢复", "created": 0, "pending": 0, "abnormal": 0}, {"name": "反压监控面板", "created": 0, "pending": 0, "abnormal": 0}, {"name": "延迟追踪管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流作业运维", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流数据输出端", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流数据格式定义", "created": 0, "pending": 0, "abnormal": 0}, {"name": "字段回填结果", "created": 0, "pending": 0, "abnormal": 0}, {"name": "水位线管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "并行度调优", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流作业指标采集", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流告警规则", "created": 0, "pending": 0, "abnormal": 0}, {"name": "自定义函数库", "created": 0, "pending": 0, "abnormal": 0}, {"name": "流处理日志中心", "created": 0, "pending": 0, "abnormal": 0}]

export default function Overview() {
  const [cards, setCards] = useState<Overview['cards']>(fallbackCards)
  const [moduleRows, setModuleRows] = useState<Overview['modules']>(fallbackModules)

  useEffect(() => {
    fetchJson<Overview>('/api/overview')
      .then((payload) => {
        setCards(payload.cards)
        setModuleRows(payload.modules)
      })
      .catch(() => undefined)
  }, [])

  return (
    <section className="page">
      <header className="page-head">
        <div>
          <h2>运营概览</h2>
          <p className="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
        </div>
      </header>
      <div className="stat-row">
        {cards.map((card) => (
          <article key={card.label} className="stat-card">
            <span className="stat-label">{card.label}</span>
            <strong className="stat-value">{card.value}</strong>
          </article>
        ))}
      </div>
      <table className="data-table">
        <thead>
          <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
        </thead>
        <tbody>
          {moduleRows.map((row) => (
            <tr key={row.name}>
              <td>{row.name}</td>
              <td>{row.created}</td>
              <td>{row.pending}</td>
              <td>{row.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
