# 实时流数据处理中心

面向实时数据处理团队的流式计算与管理中心。覆盖流数据源接入、流处理算子编排、窗口计算配置、状态后端管理、检查点恢复、反压监控、延迟追踪与流作业运维的完整实时计算链路。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流数据源接入 | `streamin` | 流数据源 | 源编号、源名称、消息队列 |
| 流处理算子 | `operator` | 算子定义 | 算子编号、算子名称、输入流 |
| 窗口计算配置 | `window` | 窗口定义 | 窗口编号、数据流名称、窗口类型 |
| 状态后端管理 | `state` | 后端配置 | 配置编号、后端类型、存储路径 |
| 检查点恢复 | `checkpoint` | 检查点记录 | 记录编号、流作业名、检查点ID |
| 反压监控面板 | `backpress` | 反压指标 | 指标编号、流作业名、反压级别 |
| 延迟追踪管理 | `latency` | 延迟记录 | 记录编号、流作业名、端到端延迟 |
| 流作业运维 | `jobops` | 作业实例 | 实例编号、作业名称、运行节点 |
| 流数据输出端 | `sink` | 输出配置 | 配置编号、输出类型、目标地址 |
| 流数据格式定义 | `schema` | 格式定义 | 定义编号、流名称、字段名称 |
| 字段回填结果 | `backfill` | 回填结果 | 记录编号、回填批次、目标版本 |
| 水位线管理 | `watermark` | 水位线配置 | 配置编号、流名称、时间字段 |
| 并行度调优 | `parallel` | 调优记录 | 记录编号、流算子名、当前并行度 |
| 流作业指标采集 | `metric` | 指标采集器 | 采集器编号、作业名称、指标名称 |
| 流告警规则 | `alarm` | 告警规则 | 规则编号、监控指标、告警条件 |
| 自定义函数库 | `udf` | 函数定义 | 函数编号、函数名称、输入参数 |
| 流处理日志中心 | `log` | 日志记录 | 记录编号、流作业名、日志级别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 存量格式定义批量升级与字段回填

`backfill` 模块是存量格式定义批量升级的唯一编排入口，收口版本重复与兼容判定不一致问题：

- 读取 `schema`（SCHE 格式定义，按字段名称、存储类型、格式版本核对存量）与 `sink`（SINK 输出配置字段映射），校验通过后才写入字段回填结果；回填结果保留**原存储类型、默认值和目标版本**。
- 升级流程覆盖四类场景：旧数据库、新环境、部署回滚、依赖兼容，全平台共用一份类型兼容矩阵。
- 同一字段并发升级按「定义编号+字段名称+配置编号+目标版本」自然键去重，只提交一个版本；失败批次保留旧映射，按记录编号断点续做。
- 回填结果与输出配置契约在同一批次内一起更新：批次内仍有失败列时契约不切换；旧归档列与新目标类型混写同一批次按冲突拦截。
- 格式定义侧与输出端消费侧共享同一份回填结果，消费侧用记录编号关联（`/api/sink/{id}/backfills`、`/api/sink/backfills/record/{记录编号}`）。

验证脚本（独立于应用代码目录）：

```bash
python3 tools/verify_backfill.py   # 服务层 8 个场景 55 项断言
python3 tools/verify_http.py       # HTTP 链路（需先起后端，默认 127.0.0.1:8011）
```

验收样例：`schema_audit_v1` 的 record-21a 至 record-21c 回填后仍对应 `sink-archive` 的旧写入契约 v1，其中 record-21c 因 `string→varchar` 兼容不通过保留原映射（BFR-0003），断点续做成功后契约才一起切到 v2。

领域：数据工程
子方向：数仓与数据湖
