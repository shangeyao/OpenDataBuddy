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
│  登录 · 总览 · Buddy · 接入 · Studio · 工作流 · CI/CD · OPS · 特征 ·  │
│  模型 · 问数 · 报告 · 看板 · 语义层 · 本体 · 质量 · 目录 · 安全 · 应用 │
└───────────────┬────────────────────────────────────────────────────┘
                │  HTTP + Bearer token + X-Workspace-Id
┌───────────────▼────────────────────────────────────────────────────┐
│  FastAPI（databuddy/api/）                                          │
│  核心路由（缺失即视为损坏）                                           │
│  ├ auth   登录 / 用户 / 工作空间 / 设置 / 审计                        │
│  ├ data   目录 / 数据源 / 接入 / SQL / 语义层 / 知识库 / 记忆          │
│  ├ chat   会话 / 消息 / 审批                                          │
│  ├ ops    工作流 / 看板 / 治理 / 总览                                 │
│  └ console 卷 / 设置 / 集成                                          │
│  扩展路由（缺失只降级跳过，不阻断启动）                                │
│  ├ quality    质量 / 剖析 / 漂移         ├ workflow   9 节点 / 12 条件 │
│  ├ cicd       bundle 六动作              ├ mlops      特征/注册/服务   │
│  ├ catalog_ext 卷/函数/外部目录/血缘     ├ ingest_ext 60+ 源模板/诊断  │
│  ├ notebook   13 魔法命令                ├ ops_ext    告警/四层/安全   │
│  ├ analysis   问数/归因/报告/看板        └ semantic_ext 本体/术语表    │
└───────┬───────────────────────────────────┬────────────────────────┘
        │                                   │
┌───────▼─────────────────┐   ┌─────────────▼──────────────────────────┐
│  Harness（Agent 编排）   │   │  平台内核                                │
│  runtime  ─ 工具循环     │   │  catalog    逻辑名 ↔ 物理名              │
│  context  ─ 上下文装配   │   │  semantic   指标 → SQL 编译              │
│  knowledge─ 混合检索     │   │  ontology   实体/关系/术语表             │
│  memory   ─ 长期记忆     │   │  governance 敏感识别 / 质量              │
│  skills   ─ 工具注册表   │   │  quality    6 类规则 / YAML / 阻断       │
│  guardrails─ 三要素护栏  │   │  profiling  三模式剖析 / 基线            │
│  trace    ─ 轨迹评测     │   │  drift      KS/PSI/Wasserstein/χ²/JS    │
└───────┬─────────────────┘   │  workflow   DAG + 调度 + 持续运行        │
        │                     │  bundle     YAML 解析 + 幂等发布         │
        │                     │  features   特征物化 / 训练集            │
        │                     │  mlops      注册 / 晋级 / A/B 服务       │
        │                     │  alerts     5 渠道 / 4 事件 / 去重       │
        │                     │  opsview    四层视图 / 健康度 / 诊断      │
        │                     │  lineage    血缘双视图                   │
        │                     │  notebook   魔法命令执行引擎              │
        │                     │  analysis   问数 / 归因 / 报告           │
        │                     │  ingest     文件解析建表 / 源模板         │
        │                     │  charts     结果 → 图表规格              │
        │                     │  heuristic  无模型时的规则引擎            │
        └─────────────────────┴─────────────┬──────────────────────────┘
                                            │
┌──────────────────────┐        ┌───────────▼──────────────────────────┐
│ DeepSeek / OpenAI 兼容│        │  data/metadata.db   data/warehouse.db │
│ （HTTP，可换任意端点） │        │  SQLite（元数据）    DuckDB（真实数据） │
└──────────────────────┘        └──────────────────────────────────────┘
```

---

## 信息架构

控制台分两层，与官方文档的 8 大能力对齐：

```
控制台层（无工作空间上下文）
└── 工作空间列表 / 用户管理 / 平台设置

工作空间层（进入后）
├── 工作空间概览
├── 智能体        Buddy（三栏：历史会话 · 对话 · 产物预览）
├── 数据接入      文件到表 / SQL 建表 / 文件到卷 / 接入任务
├── 数据工程      开发工作台（Notebook）· 工作流编排 · 发布管理 · 运维监控
├── 数据科学      特征管理 · 模型实验 · 模型管理 · 模型服务
├── 数据分析      数据探索 · 仪表盘 · 分析空间 · 智能问数
├── 数据治理      统一语义层 · 本体建模 · 数据质量 · 数据目录 · Agent 安全防护
├── 数据应用      智能体 · MCP 服务 · 模型列表
└── 平台管理      数据源管理 · 计算资源 · 成员与权限 · 工作空间设置
```

**分层理由**：控制台层管"有哪些空间、谁能进"，工作空间层管"这个空间里有什么资产"。
原产品用「控制台用户 + 工作空间用户」两层实现同一件事，常被漏配；这里只保留一层成员关系，
但把页面拆成两层，视觉上仍然清晰。

**前端取舍**：26 个页面按域拆成 15 个 `pages-*.js`，共享 `ui.js` 里的表格/弹层/表单范式。
新增页面通过 `app.js` 的 `lazyPage()` 动态 `import()` 加载，加载失败时自动回退到旧实现 ——
这样任何一个页面模块出问题，都不会让整个控制台白屏。
每个页面都是"页头 + 筛选条 + 表格卡 + 抽屉详情"，靠一致性降低理解成本 ——
这也是控制台类产品真正的体验来源，而不是花哨的动效。

## 关键设计决策

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

### 4. 为什么扩展能力做成「可缺失」的模块

0.3.0 补齐 10 个能力域（质量、工作流、CI/CD、MLOps、目录扩展、接入扩展、Notebook、
运维安全、分析、语义扩展），如果全部塞进核心路由，任何一个模块的语法错误都会让整个
控制台起不来 —— 对一个要长期演进的复刻项目，这是不可接受的风险。

所以 `main.py` 把路由分成两档：

```python
CORE_ROUTERS  = ["auth", "data", "chat", "ops", "console"]   # 缺失 = 构建损坏，直接报错
EXTRA_ROUTERS = ["quality", "workflow", "cicd", ...]          # 缺失 = 记日志跳过
```

`_mount_extras()` 用 `importlib.import_module` 逐个尝试，失败只写一行 warning 并继续。
`/api/capabilities` 会返回实际挂载成功的模块名，前端可据此隐藏未就绪入口。

前端同理：`app.js` 的 `lazyPage(module, export, fallback)` 动态 `import()`，
失败时 `console.warn` 并回退到旧实现。**能力可以渐进交付，控制台永远可用。**

### 5. 为什么这些算法都自己写

新增能力里有几处"看起来该用库"的地方，最终都手写了，因为需求边界很窄：

| 需求 | 常见做法 | 这里的做法 | 理由 |
| --- | --- | --- | --- |
| 解析 bundle.yaml | pyyaml | ~120 行子集解析器 | 只需要映射/列表/标量/注释/块标量五类语法 |
| 漂移检测 | scipy.stats | KS/PSI/Wasserstein/χ²/JS 纯 Python | 每个指标核心公式不过 20 行，省一个重依赖 |
| 关系图 / DAG 图 | d3 / mermaid | 纯 SVG 分层布局 | 节点量小，避免引入前端依赖与构建步骤 |
| 图表 | ECharts | 手写 SVG | 同上；且需要精确控制 TDesign 配色 |

依赖数量从 0.2.0 到 0.3.0 **保持 5 个不变**，这是刻意维持的约束。

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

0.3.0 共 **44 张表**。核心表：

| 表 | 作用 |
| --- | --- |
| `users` / `sessions` | 本地账号与会话（替代 CAM） |
| `workspaces` / `workspace_members` | 工作空间与成员（无席位概念） |
| `catalogs` / `schemas` / `tables` / `columns` | 三级目录与字段元数据 |
| `column_tags` | 敏感分级标注（L1~L4 + 置信度 + 证据） |
| `lineage` | 血缘（`upstream` / `downstream`，表级 + 资产级） |
| `metrics` | 指标口径（表达式 / 主表 / 维度 / join） |
| `ontology_entities` / `ontology_relations` / `glossary_terms` | 本体实体、关系、业务术语表 |
| `knowledge_docs` / `memories` | 知识库与长期记忆 |
| `conversations` / `messages` / `approvals` | 会话、消息、待确认操作 |
| `flows` / `flow_runs` / `flow_node_runs` | 工作流定义、运行、逐节点明细 |
| `bundles` / `bundle_deployments` | CI/CD bundle 与部署记录（含资源归属追踪） |
| `dashboards` / `reports` / `report_schedules` | 看板、AI 报告、定时推送 |
| `quality_rules` / `quality_rule_runs` | 质量规则与每次执行结果 |
| `profile_baselines` / `anomaly_findings` | 剖析基线与异常发现 |
| `features` / `registered_models` / `model_versions` | 特征、模型注册、版本 |
| `functions` / `volumes` / `external_catalogs` | 函数、卷、外部目录 |
| `alert_channels` / `alert_rules` / `alert_deliveries` | 告警渠道、规则、投递记录 |
| `traces` | 工具调用轨迹（评测用） |
| `audit_logs` | 审计日志 |

迁移策略：`store._migrate()` 在启动时检查 `PRAGMA table_info`，缺列就 `ALTER TABLE ADD COLUMN`。
不引 ORM、不引 Alembic —— 单文件库 + 加列迁移足够撑到 v1。

扩展模块遵循同一约定：**表在自己模块内 `CREATE TABLE IF NOT EXISTS` 建**，不集中到 `store.py`。
这样删掉一个扩展模块，也不会留下没人用的表定义。

---

## 前端为什么不用框架

控制台类应用的复杂度在**布局与信息密度**，不在状态机。原生 ES Module 带来的好处：

- 零构建：clone 下来直接跑，没有 node_modules 与打包时长
- 零依赖：图表是手写 SVG（`charts.js`，约 200 行覆盖 line/bar/pie/kpi）
- 可读：`pages.js` 一个函数一个页面，改样式不用理解构建配置

代价：复杂交互要手写 DOM 更新。目前的取舍是**每次状态变更整块重渲染**——
消息量小、页面简单，性能足够，且不会出现状态不同步。

---

## 调度与外部接入

- **调度器**：`flows.Scheduler` 每 20 秒轮询一次，同时驱动两类任务 —— 工作流 DAG（`flows`）
  与 Buddy 定时任务（`buddy_tasks`）。后者会新建一条会话再跑 Agent，所以"早报"留下的是可回看的完整过程。
- **MCP**：`python -m databuddy.mcp` 起一个 stdio JSON-RPC 进程，复用同一份工具注册表与护栏。
  写操作在 stdio 下无法人工确认，因此直接拒绝 —— 宁可少做一步，也不越过审批。

## 已知限制

| 限制 | 影响 | 计划 |
| --- | --- | --- |
| 单机 DuckDB | 不支持多实例共享 | 定位即单机；需要集群时可换 Trino/StarRocks |
| 同步执行 | 长查询会占用一个线程 | v0.2 加异步任务队列 |
| 无流式输出 | 等模型整段返回 | v0.2 加 SSE |
| 向量检索为关键词 | 同义改写召回弱 | 可选接 pgvector |
| cron 为 5 段轮询 | 最小粒度 1 分钟 | 够用，不改进 |
| MCP 为 stdio 单进程 | 不支持多客户端共享 | 每客户端起一个进程即可 |
| Notebook 内核为进程内内存态 | 多 worker 部署时变量不跨进程 | 需外部内核，超出单机定位 |
| Python 节点无内存限制 | 恶意死循环会占住 worker | 已有线程超时（默认 30s）；内存需容器隔离 |
| 告警发送为模拟投递 | 只落 `alert_deliveries`，不发真实 HTTP | 防止误触发外部通知；接真实渠道需显式开启 |
| 数据同步器未实现 | 外部库数据需先导出再上传 | 需长期连接管理，属 v0.4 范围 |
| 模型服务无真实推理后端 | 只有服务登记、流量切分、伸缩策略 | 推理需引入框架，与"零依赖"约束冲突 |
