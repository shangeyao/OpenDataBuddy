# 架构说明

## 一句话

**一层 FastAPI + 一个 SQLite 元数据库 + 一个 DuckDB 湖仓 + 一个无构建前端**，
Agent 能力由自实现的 Harness 六层提供，模型层默认接 DeepSeek。

没有消息队列、没有微服务、没有容器编排 —— 单机跑得动，才谈得上"开源版"。

---

## 整体结构

```
┌────────────────────────────────────────────────────────────────────┐
│  浏览器（web/，原生 ES Module，无构建）                              │
│  登录 · 总览 · Buddy 对话 · 目录 · 接入 · SQL · 工作流 · 看板 · 治理   │
└───────────────┬────────────────────────────────────────────────────┘
                │  HTTP + Bearer token + X-Workspace-Id
┌───────────────▼────────────────────────────────────────────────────┐
│  FastAPI（databuddy/api/）                                          │
│  ├ auth   登录 / 用户 / 工作空间 / 设置 / 审计                        │
│  ├ data   目录 / 数据源 / 接入 / SQL / 语义层 / 知识库 / 记忆          │
│  ├ chat   会话 / 消息 / 审批                                          │
│  └ ops    工作流 / 看板 / 治理 / 总览                                 │
└───────┬───────────────────────────────────┬────────────────────────┘
        │                                   │
┌───────▼─────────────────┐   ┌─────────────▼──────────────────────────┐
│  Harness（Agent 编排）   │   │  平台内核                                │
│  runtime  ─ 工具循环     │   │  catalog    逻辑名 ↔ 物理名              │
│  context  ─ 上下文装配   │   │  semantic   指标 → SQL 编译              │
│  knowledge─ 混合检索     │   │  governance 敏感识别 / 质量              │
│  memory   ─ 长期记忆     │   │  flows      DAG + cron                   │
│  skills   ─ 工具注册表   │   │  ingest     文件解析建表                 │
│  guardrails─ 三要素护栏  │   │  charts     结果 → 图表规格              │
│  trace    ─ 轨迹评测     │   │  heuristic  无模型时的规则引擎            │
└───────┬─────────────────┘   └─────────────┬──────────────────────────┘
        │                                   │
┌───────▼──────────────┐        ┌───────────▼──────────────────────────┐
│ DeepSeek / OpenAI 兼容│        │  data/metadata.db   data/warehouse.db │
│ （HTTP，可换任意端点） │        │  SQLite（元数据）    DuckDB（真实数据） │
└──────────────────────┘        └──────────────────────────────────────┘
```

---

## 三个关键设计决策

### 1. 为什么用 DuckDB 而不是接一个真湖仓

原产品的 Catalog 由云上服务托管。开源版要复刻「三层命名空间 + 列式分析 + 零运维」，
DuckDB 是最短路径：

- 逻辑名 `demo.retail.fact_orders` → 物理名 `"demo__retail"."fact_orders"`
  （一个 DuckDB 文件里，每个 `catalog.schema` 对应一个 schema）
- 查询前把 SQL 里的三段名重写成物理名，裸表名在**全局唯一**时才允许（避免歧义）
- 单文件即数据库，备份=拷文件，没有服务要运维

代价：并发写弱、无分布式。对"单机智能体工作台"这个定位，这是合理取舍。

### 2. 为什么先做语义层，再让模型写 SQL

让模型直接写 SQL，同一个问题两次可能问出两个数。语义层的做法是：

- 人先把口径固化成指标：`gmv = sum(f.amount) on demo.retail.fact_orders`
- 模型只负责**选择题**：选指标、选维度、选时间范围、选粒度
- 系统用确定性模板编译成 SQL

好处是准确率与可解释性同时上升，坏处是覆盖面受已注册指标限制。
所以 Agent 同时保留了 `run_query` 逃生通道：指标不够用时写原始 SQL，
但如果涉及口径类问题，prompt 会要求优先走语义层。

### 3. 为什么 Harness 自己写，而不是套 LangChain

需要的能力很具体，且都不复杂：

| 层 | 实现 | 为什么不用框架 |
| --- | --- | --- |
| providers | httpx 直连 `/chat/completions` | function calling 协议很简单，少一层抽象少一类 bug |
| context | 字符串拼装 + 字符预算裁剪 | 需要精确控制注入顺序与内容 |
| knowledge | bigram + IDF 倒排 | 单机文档量小，向量库是过度设计 |
| memory | 一张表，键值 | 同上 |
| skills | 装饰器注册 → OpenAI schema | 需要同时携带 risk 元信息给护栏 |
| guardrails | 三要素规则 | 业务规则，框架给不了 |
| trace | 每次调用落库 | 用于算成功率与定位慢工具 |

框架的价值在生态；这里的价值在**可控**。所以只借协议，不借框架。

---

## Agent 一次回合的数据流

```
用户输入 "各渠道 GMV 排行"
   │
   ├─ guardrails.scan_user_input      检测注入（命中则标注但不阻断）
   ├─ context.system_prompt           角色 + 硬约束 + 表清单 + 指标口径 + 知识 + 记忆 + 合规
   ├─ 历史裁剪（12k 字符预算）
   │
   ▼
DeepSeek /chat/completions  ──►  tool_calls: [query_metric(metric=gmv, dimensions=[channel_name])]
   │
   ├─ guardrails.assess               读操作 → 放行
   ├─ skills.invoke → tools.query_metric
   │     ├─ semantic.build_metric_sql  生成确定性 SQL
   │     ├─ catalog.run_workspace_sql  重写三段名 + 护栏 + 执行
   │     └─ charts.infer               推断图表类型
   ├─ trace.record                     落库：工具名/耗时/成败
   ├─ 结果裁剪（行数截断到 20）后回填 tool 消息
   │
   ▼
第二轮模型调用 → 自然语言结论
   │
   ├─ artifacts 收集（表格 + 图表 spec）
   └─ 落 messages 表，前端渲染
```

写操作若命中护栏（改状态 + 触敏感），流程在此中断：

```
approvals 表 ← { state: 完整 messages 快照, call: {name, args, id} }
前端渲染确认卡 → POST /api/approvals/{id}/approve
   → 恢复 state → 执行工具 → 继续同一个循环
```

这样**用户确认前后的上下文完全一致**，不需要重跑已完成的步骤。

---

## 护栏的三要素模型

```
① 输入不可信   用户消息 / 知识库 / 工具返回都可能夹带指令
② 会改变状态   建表、改口径、打标、跑工作流、发外部请求
③ 触及敏感数据 命中 L3/L4 字段或涉及导出
```

三个条件**同时成立**才升级为人工确认。只满足 ① 或 ② 时仅记审计日志 ——
否则每个写操作都弹窗，Agent 就没法用了。

另外三道防线不依赖模型自觉：

1. `warehouse.guard()` —— SQL 白名单式校验（只读、单语句、禁文件函数）
2. `skills` 的 `risk` 声明 —— 工具自带风险等级，护栏据此判定
3. 展示层脱敏 —— 敏感字段在样例与结果里默认掩码

---

## 数据模型（主要表）

| 表 | 作用 |
| --- | --- |
| `users` / `sessions` | 本地账号与会话（替代 CAM） |
| `workspaces` / `workspace_members` | 工作空间与成员（无席位概念） |
| `catalogs` / `schemas` / `tables` / `columns` | 三级目录与字段元数据 |
| `column_tags` | 敏感分级标注（L1~L4 + 置信度 + 证据） |
| `lineage` | 表级血缘 |
| `metrics` | 指标口径（表达式 / 主表 / 维度 / join） |
| `knowledge_docs` / `memories` | 知识库与长期记忆 |
| `conversations` / `messages` / `approvals` | 会话、消息、待确认操作 |
| `flows` / `flow_runs` | 工作流定义与运行日志 |
| `dashboards` | 看板规格（卡片 + SQL + 图表 spec） |
| `quality_rules` | 质量规则与最近结果 |
| `traces` | 工具调用轨迹（评测用） |
| `audit_logs` | 审计日志 |

迁移策略：`store._migrate()` 在启动时检查 `PRAGMA table_info`，缺列就 `ALTER TABLE ADD COLUMN`。
不引 ORM、不引 Alembic —— 单文件库 + 加列迁移足够撑到 v1。

---

## 前端为什么不用框架

控制台类应用的复杂度在**布局与信息密度**，不在状态机。原生 ES Module 带来的好处：

- 零构建：clone 下来直接跑，没有 node_modules 与打包时长
- 零依赖：图表是手写 SVG（`charts.js`，约 200 行覆盖 line/bar/pie/kpi）
- 可读：`pages.js` 一个函数一个页面，改样式不用理解构建配置

代价：复杂交互要手写 DOM 更新。目前的取舍是**每次状态变更整块重渲染**——
消息量小、页面简单，性能足够，且不会出现状态不同步。

---

## 已知限制

| 限制 | 影响 | 计划 |
| --- | --- | --- |
| 单机 DuckDB | 不支持多实例共享 | 定位即单机；需要集群时可换 Trino/StarRocks |
| 同步执行 | 长查询会占用一个线程 | v0.2 加异步任务队列 |
| 无流式输出 | 等模型整段返回 | v0.2 加 SSE |
| 向量检索为关键词 | 同义改写召回弱 | 可选接 pgvector |
| cron 为 5 段轮询 | 最小粒度 1 分钟 | 够用，不改进 |
