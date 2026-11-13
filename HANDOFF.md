# 交接记录 · 2026-11-13

## 本轮目标

1. 按腾讯云官方《DataBuddy 产品动态》2026-08 版逐条对照本仓库，把「文档声称已具备、代码实际未生效」的地方改成真实实现，并按差距清单补齐功能。
2. 按官方 Buddy 控制台截图重构前端：左侧任务栏、任务执行计划卡片、数据表芯片。

差距逐条证据见 [docs/OSS_DIFF.md](docs/OSS_DIFF.md) §6；本轮完成情况见同文件 §6.8 / §6.9，剩余 8 项待办也列在 §6.9。

## 实现状态

### 修掉的缺陷（P0，6 / 6）

- 外部目录探活与同步面板的前后端字段统一为 `checks` / `problems` / `issues` / `tables` / `suggestions` 与 `created` / `updated` / `target`，面板不再空白。
- 看板生成（`/api/analysis/dashboard/generate`）不再固定追加「每日GMV趋势」「品类GMV」两张演示卡。
- 补齐模型服务对外公布的 `POST /api/serving/services/{id}/predict`：模型版本在 `artifact_uri` 以 `sql:` 登记推理语句，`:名` 按实例绑定、`?` 按 `params` 顺序绑定，走只读护栏。
- 数据源模板注释改为真实的 60 个。
- Git 连接测试优先执行 `git ls-remote --heads` 取真实分支，未装 git 时退回 HTTP HEAD，并在响应里说明所用方法。
- OBO 面板有数据了：`assessPrincipal()` 按工作空间 `obo_policy.<ws>` 白名单判定（默认 deny），`assessTool()` 输出三要素与 OBO，并接入工具循环。

### 补齐的能力（P1，10 / 10）

- 三场景 Agent 名副其实：`modeGroups` / `modeBriefs` / `toolsForMode()` / `agentSystem()` 让会话 `mode` 真正分流工具与提示词；`agents.tools` 白名单进入运行时筛选。
- 工作流支持 `run_mode=once`（配 `run_at`，到期自动停用）与 `run_mode=event`（生成 `trigger_key`，公开端点 `POST /api/workflows/trigger/{key}`）。
- 告警真实投递：新增 `internal/platform/notify.go`，webhook / Teams / 企微 / Slack / 飞书走 HTTP POST，邮件走 SMTP；`DATABUDDY_ALERT_SIMULATE=1` 保留离线模拟开关；失败记录状态而不是静默成功。报告推送（`runReport`）也走同一条投递路径，渠道名与目标不再是空值。
- 敏感识别升级：`scan()` 用字段名规则 + 取值抽样（200 行）双重判定，输出 L1–L4 分级、命中比例与真实置信度。
- 审计覆盖读操作：`server.go` `auditRead()` 覆盖 SQL 执行、目录预览、问数、归因、扫描、推理、知识检索。
- 通知渠道补飞书：`metadata.json` `channel_types` 由 5 类增到 6 类。
- 质量规则智能推荐：`quality.go` `suggestQualityRules()` 按剖析统计生成阈值与理由，不再是「所有字段 not_null」占位；`GET /api/quality/suggest` + 质量页「智能推荐」入口 + Agent 侧 `quality_suggestions` 产物。

### 补齐的能力（P2，6 / 14）

- 离线同步任务：`sync_tasks` 实体 + `runSyncTask()` 全量 / 水位增量执行器 + 调度接入 Tick + 前端「同步与出湖」页签。
- AI 辅助建源建任务：`create_source` / `create_sync_task` 两个 Agent 工具（代码定义在 `sync.go` 的 `extraTools`，`Open()` 时注入元信息目录）。
- 数据对账与出湖：`POST /api/ingest/reconcile`、`POST /api/ingest/egress`、`GET /api/ingest/egress`（`internal/platform/egress.go`）。
- 看板卡片编辑与拖拽：卡片增删改、图表类型切换、指标引用或只读 SQL 数据源、HTML5 拖拽排序，落库走 `PATCH /api/dashboards/{id}`。

### Buddy 任务台（对标官方截图）

- 后端 `agent.go`：`planSteps` 按场景定义 5–6 步模板；`invoke()` 是工具执行的唯一入口，LLM 与规则兜底都经 `markPlanStep()` 推进步骤；`agentFinish()` 把计划写入 `meta.plan` 持久化进历史消息。
- 前端 `web/js/buddy.js`：左栏「Hi, Buddy! / ＋新建任务 / 场景分段控件 / 自动化任务·Skills·知识库·更多 / 最近任务 / 自动化任务列表」，交互由 `bindRailNav()` 与 `openMoreMenu()` 承担；主区新增「任务执行中 N/M」计划卡片与「分析数据表」芯片区；样式在 `web/styles.css`。

## 可重复验证

```bash
go build ./...
go vet ./...
go test ./... -count=1
ODB_HTTP_TEST=1 go test ./... -count=1
node --check web/js/<每个文件>.js
```

测试均使用临时目录，HTTP 测试使用随机本地端口，不读写真实 `data/`。

## 本轮新增测试

| 测试 | 覆盖 |
| --- | --- |
| `TestSyncTaskLandsInLake` | 离线同步任务入湖、行数与水位 |
| `TestPredictServesRegisteredModel` | `/predict` 真实推理端点与参数绑定 |
| `TestScenarioToolsAndPrincipalCheck` | 场景工具分流、智能体白名单、OBO 默认拒绝与放行、工具三要素 |
| `TestEventTriggerWorkflow` | 事件触发工作流的 trigger_key、公开路由与非法模式拒绝 |
| `TestSuggestQualityRulesUseProfile` | 规则推荐基于剖析统计（枚举 / 唯一 / 抽样不产出精确阈值）、产物契约 |
| `TestReportDeliveryUsesScheduledChannel` | 报告推送按定时配置的渠道真实投递并记录渠道与目标 |
| `TestHTTPNewCapabilities`（HTTP） | 同步 → 出湖 → 对账 → 推理 → OBO → 事件触发公开端点全链路 |

## 浏览器与最终回归

- 本轮未做新的浏览器人工验收；前端改动集中在 Buddy 任务台、质量页「智能推荐」入口与文案修正，逻辑均已被原生与 HTTP 测试覆盖。
- 上一轮（2026-11-10）的浏览器验收结论仍然有效：登录与 29 个页面加载复验无新增控制台错误，发布管理、工作流拖拽、Notebook 跨 cell、质量四标签、分析五标签、模型 A/B 分流均通过。
- 上传单文件上限实际为 128 MB（`ingest.go` 的 `128<<20`），前端提示与文档已同步修正（原写 200 MB）。

## 启动与数据

```bash
DATABUDDY_DATA_DIR=/tmp/odb-dev go run ./cmd/opendatabuddy --port 8042
```

打开 `http://127.0.0.1:8042`，默认 `admin@local` / `admin123`。迁移现有数据前先停止旧服务；SQLite 增量补列，原数据目录布局保留。不要让两个服务进程同时打开同一 DuckDB 文件。

出湖文件写在 `data/egress/<workspace_id>/`，按工作空间隔离。

## 实际边界

- 真实大模型供应商未使用个人密钥验收；协议和工具恢复使用本地模拟模型验证。
- 告警与报告渠道是真实出网投递，失败只记录状态、不自动重投；SMTP 需自行配置服务器；离线演练请设 `DATABUDDY_ALERT_SIMULATE=1`。
- `/predict` 的推理走登记的 SQL 推理语句，不是模型框架；没有鉴权、限流与预测质量监控。
- 离线同步任务只支持 SQLite 源；外部目录中其他类型数据源需导出后接入。
- 目标表仍是整表重建（`CREATE OR REPLACE`），不做增量列演进。
- 60 个数据源模板中只有 SQLite 有真实取数路径，其余为 TCP 探活，响应中明示未验认证。
- 权限模型仍是 admin / member 两角色，无行级 / 表级授权实体；检索仍为关键词 LIKE，无向量语义检索。
- XLSX 读取首个工作表；复杂 SQL 血缘为规则推断，需人工核对。
- Notebook 状态在内存，默认 30 秒执行超时，无内存隔离。
- 本机未安装 Python 解释器，Python 内核相关测试自动跳过，Notebook 执行能力未在本机实机验证。
- 编译需要 CGO 工具链；本次执行环境为 Windows x64，GCC 需使用与 DuckDB 静态库 ABI 匹配的 winlibs 版本。

这些边界不应当被描述为已验证的商业级完整能力。

---

## 追加轮次 · 2026-11-13（§6.9 剩余待办收口）

在 `12893c3` 之上，把 [docs/OSS_DIFF.md](docs/OSS_DIFF.md) §6.9 的「剩余待办（8 项，按依赖排序）」中**纯本地可做**的 6 项全部落地；第 7、8 项（数据科学补齐、外部依赖：CDC / BI / Ray / GPU）仍需外部系统或推理框架，保留在清单里。分支维持 `main`，不新建分支。

### 本轮实现（6 / 8 项可本地完成的待办）

| 项 | 能力 | 实现 | 新增文件 |
| --- | --- | --- | --- |
| 1 | 表结构演进 | 三处 `CREATE OR REPLACE` 重建路径（离线同步、外部目录同步、SQL / 文件导入）统一改走 `landing.go` 的共享落库路径：先 `DESCRIBE` diff，只对新增列执行 `ALTER TABLE ADD COLUMN`，再按「源列 ∩ 目标列」投影写入；全量导入清空保证幂等、增量同步追加。`drift.go` 检出字段数变化时归档 `trigger=drift` 的演进事件（不自动加列，需 `/api/catalog/evolve` 显式登记） | `evolution.go`、`landing.go` |
| 2 | 推理服务鉴权与限流 | `/predict` 改走 `predictGuarded`：先鉴权再限流最后推理。令牌实体 `predict_tokens`（库中只存 SHA-256 摘要，明文仅创建响应返回一次），`X-Predict-Token` / Bearer 校验 + 服务绑定 + scopes；按 `workspace:service` 的令牌桶限流，超配额 429。`require_token` 默认关闭以保持向后兼容 | `serveguard.go` |
| 3 | 看板自由布局 | 12 列栅格画布：卡片宽度下拉（半宽 / 全宽 / 三分之一）、右下角手柄拖拽调高（20px 吸附）、栅格显隐、自动排布、保存布局；布局随 `spec.layout` 与 `spec.cards` 一起落库 | `web/js/pages-gov.js`、`web/styles.css` |
| 4 | 语义检索 | 本地确定性「哈希 embedding」（256 维稀疏向量，CJK 二元组 + ASCII 词/前缀）+ 倒排词条索引 + TF-IDF，余弦与词面混合召回（0.65 / 0.35）；`knowledge-search` 升级为混合检索，纯词面查询仍兜底到原 IDF；文档增改删增量维护索引 | `semantic.go` |
| 5 | 行级 / 表级权限 | `grants` 授权实体（主体 user/role/workspace × 对象 table/schema/catalog/column × 动作 × allow/deny × 行谓词），显式 deny 优先、未配置策略时兼容模式零开销；行级谓词走严格文法白名单（挡注入），`queryAs()` 在 `guard/qualify` 之前求值并把谓词包成子查询过滤 | `grants.go` |
| 6 | 工作空间资源配额 | `workspace_quota` 表，按 6 类资源（数据表 / 工作流 / 看板 / 智能体 / 数据源 / 会话）限制新建数量，默认 `-1` 不限制；创建入口统一 `checkQuota`，超限 409 | `quota.go` |

**共享地基**：`foundation.go` 集中登记全部新路由与契约；`schema.sql` / `migrations.json` 新增 6 张表；`openapi.json` 补 13 个路径；`metadata.json` 补授权元数据并补齐 `freshness` 规则类型（§6.4 复核项）；`store.go` 增加限流桶字段。

### 可重复验证

```bash
CGO_ENABLED=1 go build ./...
CGO_ENABLED=1 go vet ./...
CGO_ENABLED=1 go test ./... -count=1
ODB_HTTP_TEST=1 CGO_ENABLED=1 go test ./... -count=1
node --check web/js/<每个文件>.js
```

本轮全部通过。新增 `extensions_test.go`，覆盖：
`TestTableEvolutionAddsColumnsWithoutDropping`（加列不丢数据 / 旧行为 NULL）、`TestTableEvolutionRejectsUnsafeType`（DDL 类型白名单）、`TestDriftRecordsEvolutionEvent`、`TestPredictTokenAuthAndRateLimit`（令牌摘要 / 401 / 令牌桶 429 / 吊销）、`TestSemanticIndexAndHybridRecall`（混合召回 + 增量索引）、`TestGrantPolicyEvaluationAndEnforcement`（deny 优先 / 行谓词注入 / 注入白名单）、`TestWorkspaceQuotaEnforcement`（限额 409 / 不限制）、`TestExtensionRouteInventory`（路由 / OpenAPI / 迁移 / 元数据契约）。

### 仍未完成（诚实边界）

- **数据科学补齐**：在线特征表与同步、实验类型 ml/dl/llm/agent、模型血缘、AutoML —— 需要训练 / 特征服务框架。
- **外部依赖项**：实时同步（CDC 需 Kafka/Pulsar 等消息中间件）、BI 对接（Power BI 等嵌入令牌与语义模型推送）、Ray / GPU 集群 —— 需要外部系统。
- 语义检索是**本地哈希向量 + 倒排**，不是 embedding 模型 / 向量数据库；对同义词与语义泛化的能力有限，纯词面查询仍走 IDF 兜底。
- 行级权限的谓词仅支持 `列 op 字面量` 文法（含 IN / LIKE），不支持子查询 / 函数 / 跨表关联；`admin` 在配置了任何策略后不再自动放行。
- `/predict` 推理仍走登记的 SQL 推理语句，不是模型框架；没有预测质量监控。
- 看板自由布局是「栅格列宽 + 高度吸附 + 排序」，不是像素级自由画布（无绝对定位 / 重叠 / 跨格拖拽）。
- 配额只统计数量，不统计存储体积 / CU / 行数。

