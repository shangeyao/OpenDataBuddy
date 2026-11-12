# 与原商业版的差异清单

> v0.4：后端已迁移为 GoFrame v2.10.4，Python 仅作代码执行内核。以下能力表描述产品定位；具体实现边界与验收范围以 [HANDOFF](../HANDOFF.md) 为准。
>
> v0.4 复核与补全（2026-11-12）：按腾讯云官方「产品动态」2026-08 版逐条核对 7 大模块 37 项能力与本仓库代码，结果见下文 **§6**。复核发现的 6 处「文档声称已具备、代码实际未生效」已全部改为真实实现；30 项改进清单完成 P0 全部、P1 全部、P2 中的 6 项，剩余 8 项列在 §6.9。

> 目标：**能力保留，商业限制清空。**
> 本文逐条列出原产品的商业约束、云依赖与限额，以及本开源版的替代实现。

---

## 1. 账号与权限体系

| 原产品 | 开源版 | 说明 |
| --- | --- | --- |
| 腾讯云主账号登录 | 本地邮箱 + 密码登录 | PBKDF2-SHA256 加盐存储，会话 token 存本地库 |
| 企业实名认证（个人实名不支持） | **无** | 不需要任何实名信息 |
| CAM 子账号 / 预设策略 / 自定义策略 JSON | 本地用户表 + `admin`/`member` 两角色 | 不再需要 `cam:PassRole`、`finance:trade` 之类的授权步骤 |
| 「控制台用户」与「工作空间用户」双层添加 | 单一成员模型 | 原产品必须加两次，常见踩坑；这里加一次即生效 |
| Agent 席位（试用 2 个，198 元/人/月） | **无席位概念**，所有成员可用 | 不再为"能不能用 AI"单独付费 |
| 对象 ACL 五级（可管理/编辑/运行/查看/使用） | 工作空间隔离 + 角色 | 简化；细粒度 ACL 在 v0.2 视需求补 |
| CAM 角色与 STS 临时凭据 | 无 | 不存在跨云角色信任关系 |

> 保留：工作空间多租隔离、成员角色、审计日志。

---

## 2. 计费与额度

| 原产品 | 开源版 |
| --- | --- |
| CU 计量（1 CU ≈ 1 vCPU + 4GB） | 无计量，直接用本机 CPU/内存 |
| 计算资源按量计费 / 包年包月 | 无 |
| 创建集群冻结金额（16 CU ≈ 5.6 元） | 无 |
| 试用额度：30 天 / 600 CU·时 / 2 席位 / 5000 积分 / 100 GB 结果缓存 / 100 元存储券 | **全部删除**；不限时间、不限用量 |
| Agent 积分包消耗 | 无；只消耗你自己的模型 API 额度 |
| 结果缓存按量计费、存储按量计费 | 无；落本地磁盘 |
| 欠费 → 24h 隔离 → 15 天销毁 | 无；数据一直在，直到你删 |
| 升级正式版才能保住试用数据 | 无此概念 |
| SLA 与赔偿 | 无承诺 |

**唯一的外部成本**：如果你接 DeepSeek / OpenAI，产生的是模型 API 费用（由服务商计费，与本项目无关）。不接模型也能用，走内置规则引擎。

---

## 3. 云基础设施依赖

| 原产品 | 开源版替代 |
| --- | --- |
| TCLake 多模态数据湖托管 | **本地 DuckDB**（单文件，`data/warehouse.duckdb`） |
| TCCatalog 元数据服务 | 本地 SQLite `catalogs/schemas/tables/columns` |
| DLC 计算引擎（作业/交互式集群） | 进程内 DuckDB 执行，无集群概念 |
| 平台集群 / 实时集群（Flink） | 无实时链路；批处理由 DuckDB 承担 |
| WeData 调度 | 内置 cron 调度器（20 秒轮询，5 段表达式） |
| VPC / 子网 / 出站入站网络设置 | 无；本机直连文件系统 |
| NAT 网关、安全组、IP 白名单 | 无 |
| Git 连接（代码仓库集成） | 保存配置 + `git ls-remote --heads` 真实探测分支（`internal/platform/notify.go:202` `gitRemoteRefs()`，未装 git 时退回 HTTP HEAD 并回报所用方法），无 clone/push |
| 对象存储 COS | `data/uploads/` 本地目录 |
| 跨地域资源 | **无地域概念**，也因此没有"创建后不可修改"的坑 |
| 工单系统 | 无；提 Issue |

---

## 4. 硬性限额

| 原产品限制 | 开源版 |
| --- | --- |
| 单文件上传 5 GB | 单文件 128 MB（`internal/platform/ingest.go` 的 `128<<20` 限制） |
| 单次最多 100 个文件 | 无限制 |
| 分析空间最多 30 个表/指标模型 | 无限制 |
| CU 滑块 16~1024（刻度固定） | 无 |
| 资源名称 ≤ 50 字符、工作空间 ≤ 128 字符 | 标识符统一截断到 64 字符 |
| 交互式集群单实例并发 32 | 无（SQLite/DuckDB 单连接串行） |
| 自动停止 1~99999 分钟 | 无（无常驻计算资源） |
| 一次查询结果缓存 100 GB | 结果最多返回 500 行（保护前端） |

---

## 5. 能力对照：原产品有什么，这里怎么做的

按官方 8 大能力 / 24 个二级模块逐项对照。0.3.0 起 24 个二级模块的界面与存储均已落地；2026-11-12 按官方 2026-08 动态逐条复核后，发现其中若干处为「界面/登记齐备但行为未真正生效」，**本轮已把这些行为补成真实实现**（逐条证据见 §6）。

| 一级能力 | 二级模块 | 开源版 | 成熟度 |
| --- | --- | --- | --- |
| **数据接入** | 文件到表 | ✅ 本地解析建表（CSV/JSON/Parquet/XLSX） | 对齐 |
| | 文件到卷 | ✅ 上传落盘 + 卷清单 + 文件查看 + 体积刷新 | 对齐 |
| | 数据源模板与方案 | ✅ **60 个数据源模板**、5 类接入方案（全量/增量/CDC/文件/API）、Agent 连通诊断 | 部分（仅 SQLite 有真实取数路径，其余仅 TCP 探活） |
| | 离线 / 实时同步 | ⚠️ 界面、登记、诊断齐备；真实同步器需外部连接 | 部分 |
| **数据工程** | Studio（Notebook / SQL / Python IDE） | ✅ 文件树 + **13 个魔法命令**（%sql/%python/%md/%tables/%desc/%run/%load/%param/%time/%chart/%vars/%help/%clear）+ 参数化运行 + 跨文件引用 | 部分（官方 2026-08 新增的智能补全 / AI Coding 未实现） |
| | Workflow（DAG / 调度 / 9 种节点） | ✅ **9 种节点**（sql/python/shell/notebook/quality/http/subflow/notify/approval）× **12 种运行条件**；两层参数；时区 cron；持续运行 60s 下限；**周期 / 一次性 / 事件触发三种方式** | 部分（事件触发参数以请求体透传，未与 Studio / Bundle 打通） |
| | CI/CD（Bundle 发布） | ✅ **自研 YAML 解析**（零 pyyaml）+ init/validate/deploy/run/summary/destroy 六动作 + 多环境幂等 + drift + 归属追踪销毁 | 部分（无 Git 拉取/推送，Bundle 存本地 SQLite） |
| | OPS（四层监控 + AI 诊断） | ✅ **四层视图**（平台→工作空间→资源→任务）+ 健康度四因子 + 规则推理诊断（9 类信号）+ **6 渠道告警** + 5 分钟去重 | 部分（规则诊断，无 AI 自愈闭环；投递失败只记录状态，不自动重投） |
| **数据科学** | 模型实验 | ✅ 实验列表 + 指标 + 框架 | 部分（仅传统 ML 框架登记，无训练任务与日志） |
| | 模型管理 | ✅ 注册表 + 版本 + **冠军晋级**（多版本对比 promote） | 部分（无模型血缘） |
| | 模型服务 | ✅ 在线服务 + 副本 + 启停 + **A/B 流量切分 + 弹性伸缩 + 自动启停** + `POST /api/serving/services/{id}/predict` 真实推理端点 | 部分（推理走 SQL 推理语句，非模型框架；无鉴权、无限流、无预测质量监控） |
| | Feature Store | ✅ 特征定义 + 物化计算 + 按表反查 + 训练集构建 + 特征血缘 | 部分（仅离线特征，无在线特征表与同步） |
| **数据分析** | 智能问数 | ✅ 语义层编译 + NL2SQL + 规则引擎兜底 + 推理说明 | 对齐 |
| | 主动归因 | ✅ 指标+维度+周期 → 贡献度排序 + 结论 | 对齐 |
| | AI 报告 | ✅ 分章节生成 + 保存 + **定时推送（真实出网投递）** | 对齐 |
| | AI 生成看板 | ✅ 一句话 → 预览 → 一键创建 | 对齐（只返回提问对应的一张卡） |
| | 数据探索（SQL 直查） | ✅ | 对齐 |
| | 分析空间 | ✅ 绑定表与指标、空间隔离、**iframe 嵌出** | 对齐 |
| **数据治理** | 统一语义层 | ✅ 指标 / 维度 / join / 本体描述 | 对齐 |
| | **本体建模** | ✅ 实体（主体/事件/维度/指标组）× 关系（1:1/1:N/N:1/N:N）× 术语表；SVG 关系图；术语解析注入 Buddy 上下文 | 对齐 |
| | 数据质量 | ✅ **6 类规则**（空值/唯一/范围/枚举/正则/SQL）× 严重级别 × 阻断；**YAML 导入导出**；**前置阻断**接工作流；规则级告警；**零配置异常检测** | 对齐 |
| | 数据剖析与漂移 | ✅ 三模式剖析（快照/时序/推理）+ 基线管理 + **5 指标**（KS/PSI/Wasserstein/卡方/JS 散度），Go 原生实现 | 对齐 |
| | 数据目录 TCCatalog | ✅ 表 / **卷 / 模型 / 函数 / 外部目录** 五类资产 + **端到端血缘双视图**（上游影响 + 自动发现） | 对齐 |
| | Agent 安全防护 | ✅ **双向 Guardrails**（输入 15 类注入 + 输出 8 类 PII）+ 高危 SQL + 三要素 + **OWASP LLM Top10** 十项覆盖 | 部分（OBO 为工作空间策略白名单判定，非身份联邦） |
| **数据应用** | 智能体 | ✅ 自定义 Agent（模式 + 提示词 + 工具白名单字段） | 部分（工具白名单已接入运行时，仍是同一 Buddy 按场景分流，非独立实例） |
| | MCP | ✅ 独立 stdio 服务进程（initialize/tools/list/tools/call） | 对齐 |
| | Model List | ✅ 多 Endpoint + 切换 + 连通测试 | 对齐 |
| | **Buddy** | 三场景 + Agent/Ask | ✅ | 部分（三场景已按模式分流工具与提示词，仍非独立智能体实例） |
| | 知识库 / @ 引用 / 上传 / 产物 | ✅ | 部分（检索为关键词 LIKE，非语义检索） |
| | 定时任务 | ✅ cron 自动执行，建会话 + 记录结果 + 可回看 | 对齐 |
| | 上下文感知（Studio/Catalog 侧栏） | ⚠️ 仅主入口 | 待补 |
| **平台管理** | 工作空间管理 | ✅ 列表 / 新建 / 进入 / 成员 | 部分（无资源配额实体） |
| | 数据源管理 | ✅ 注册 + 连通性测试 + 批量诊断 | 部分（无直连取数，仅 SQLite 有真实路径） |
| | 计算资源管理 | ✅ 四类集群 / CU / 启停 / 网络 | 部分（无 Ray、无 GPU，网络为自由文本） |
| | 通知渠道 | ✅ **6 渠道**（邮件/Webhook/Teams/企微/Slack/飞书）+ 配置与测试 | 部分（真实出网投递，失败只记录状态不自动重投；SMTP 需自行配置） |
| | Git 连接 | ✅ 保存配置 + `git ls-remote` 真实探测分支 | 部分（仅探测，不 clone / push，未打通 Studio / Bundle） |

就官方 8 大能力 / 24 个二级模块而言，模块界面与存储均已落地；按官方 2026-08 动态的更细口径看，未达成的是**真实数据同步器**（需要外部数据库长连接）与**真实模型推理后端**（需要推理框架）两类外部连接能力，其余缺口均可在本仓库内补齐，逐条见 §6。

---

## 6. 对照官方 2026-08「产品动态」逐项差距

官方页面：[大数据智能体工作台 DataBuddy 产品动态](https://cloud.tencent.com/document/product/1835/135574)，2026-08 版列出 7 大模块共 37 项能力。复核方式为静态阅读代码（`internal/platform/*.go`、`web/js/*.js`）并比对路由与前端消费字段；未启动服务做运行时验证。

状态口径：

- **对齐** — 功能真实生效，行为与官方描述一致。
- **部分** — 界面 / 存储 / 登记齐备，但行为受限、为本地模拟或只覆盖官方口径的一部分。
- **缺失** — 无对应实现。
- **刻意不做** — 属开源版定位主动放弃（计费、实名认证）。

### 6.1 数据接入（6 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| 离线同步：文件 / 单表 / 批量同步，统一入湖 | 对齐 | `ingest.go:204-252` 文件入湖；`sync.go` `sync_tasks` 实体 + `runSyncTask()`（全量 / 增量水位）；`operations.go:124-193` 外部目录整库同步 | 同步任务可调度（cron）、可增量（水位列）、可立即运行；前端在「数据接入 → 同步与出湖」 |
| 实时同步：整库多表 / 分库分表 | 缺失 | `workflow.go` 节点类型仅 sql/python/shell/quality/notebook/subflow 等，无同步节点 | 无 CDC、无 Kafka/Pulsar 消费、无分库分表路由 |
| 数据源统一管理：47+ 类数据源统一配置与鉴权 | 部分 | `assets/metadata.json` `source_templates` = **60 个**；`ingest.go` `probe()` 真实读取 SQLite 表清单与行数，非 SQLite 仍为 TCP 探活 | 模板数超过官方 47+；只有 SQLite 有真实取数路径，其余类型未实现连接器 |
| AI 辅助建源建任务：自然语言建源 + 建同步任务 + 自动补全字段映射与调度 | 部分 | `sync.go` `createSource()` / `createSyncTask()` + `create_source` / `create_sync_task` 两个 Agent 工具（`tools.go` 代码定义，`applyExtraTools()` 注入目录） | 可用自然语言建源、建同步任务；字段映射自动补全未做（仍需填写连接参数） |
| 自动建表：源端结构变更自动感知 + 目标端结构演进 | 部分 | `ingest.go:246` `CREATE OR REPLACE TABLE` + `registerTable()` 用 `DESCRIBE` 回填列元数据 | 只有"建"没有"演进"：`sql.go` 禁 `alter`/`create`；`drift.go` 能检出字段数变化但只记 anomaly，不触发表结构变更 |
| 数据对账与出湖服务 | 对齐 | `egress.go` `POST /api/ingest/reconcile`（源端 vs 湖仓逐表比对、抽样与一致率）、`POST /api/ingest/egress`（CSV / Parquet 导出到 `data/egress/<workspace>`）、`GET /api/ingest/egress`（文件清单） | 出湖目录按工作空间隔离；对账以源表为主驱动，目标缺失记状态而非报错 |

### 6.2 数据工程（7 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| Studio 一体化开发环境（Notebook / SQL / Python） | 对齐 | `python.go:124` `runCell`、`:135-219` 魔法命令分派、`scripts/python_kernel.py:33-61` 真实内核 | 仅 Notebook 形态，无独立 SQL IDE 工程态 |
| 开发辅助：智能补全、AI Coding | 缺失 | 全仓无 completion / autocomplete 实现；`pages-eng.js:76-86`「AI 代码助手」仅跳转 Buddy；`app.js:36` 仍宣称"AI 代码助手" | 无补全、无模型驱动编码 |
| Git 源码管理：Studio 内置 Git 联动 | 缺失 | `operations.go:87` 仅存 settings；`notify.go:202` `gitRemoteRefs()` 执行 `git ls-remote --heads` 真实探测分支（未装 git 时退回 HTTP HEAD） | 无 clone / push / diff，无版本管理、无协作研发 |
| 工作流编排：周期 / 一次性 / 事件触发 + 依赖 + 参数 | 对齐 | 周期 `operations.go` `cronMatches`；一次性 `workflow.go` `run_mode="once"` + `run_at`；事件触发 `workflow.go` `POST /api/workflows/trigger/{key}`（公开端点，密钥鉴权）；依赖 `workflow.go` 拓扑 + 12 种条件；参数 `workflow.go` `renderValue` | 三种触发方式齐备；事件触发参数以请求体透传 |
| 运维监控告警：监控 + 告警 + 失败重试 | 部分 | 四层监控 `governance.go`；告警 `operations.go` `emit()` + 5 分钟去重；重试 `workflow.go` | 投递已改为真实出网（见 §6.7），失败仅记录不重投 |
| AI 诊断：任务失败 AI 诊断与自愈 | 部分 | `governance.go` `/ops/diagnose`，`engine="rules"` | 规则诊断，未接模型；无自动修复回写闭环 |
| CI/CD Bundle：多环境 + Git 联动自动化部署 | 部分 | `bundle.go:16-59` YAML 校验、`:86-155` 多环境幂等部署、`:258-286` 归属销毁 | Bundle 存本地 SQLite，无 Git 拉取/推送、无 CI 触发 |

### 6.3 数据科学（5 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| 特征管理：离线 / 在线特征表管理与同步 | 部分 | `features.go:62` `computeFeatures` 物化、`:99` 训练集、`:11` 版本、`:21` 血缘边 | 无在线特征表与在线存储，无离线→在线同步（`schema.sql:399-414` 无 online 字段） |
| 实验管理：ML / DL / 大模型 / Agent 多类型 | 部分 | `resources.go:231-238` 建实验（纯登记）；`pages-ai.js:71` 框架 PyTorch / LightGBM / XGBoost / Prophet / sklearn | 均为传统 ML，无 DL / LLM / Agent 类型；无训练任务、超参、日志（`schema.sql:245`） |
| AutoML：自动机器学习 | 缺失 | 全仓 `automl` / `AutoML` / `auto_ml` 零命中 | 无自动特征 / 超参搜索 / 自动训练入口 |
| 模型全生命周期：注册 / 版本 / 血缘追踪 | 部分 | `mlops.go:103-127` 注册、`:36-43` 版本、`:139-160` promote 冠军唯一化 | 模型血缘缺失：`source_experiment` / `run_id` 仅字符串（`schema.sql:415-437`），`lineage` 只记 sql / feature 边 |
| 在线推理服务：ABTest / 质量监控 / 鉴权流控 | 部分 | `mlops.go` `POST /api/serving/services/{id}/predict` 真实推理端点：模型版本在 `artifact_uri` 以 `sql:` 登记推理语句，`:名` 按实例绑定、`?` 按 `params` 顺序绑定；AB 流量 `normalizeTraffic` | 推理走 SQL 语句而非模型框架；无鉴权、无限流、无预测质量监控 |

### 6.4 数据治理（5 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| 多模态统一管理：Catalog / Schema / Table / Model / Volume / Function | 对齐 | `governance.go:370-400` `catalogTree()` 返回六类计数；`pages-catalog.js:1,136-175` 多模态目录树 | 六模态齐全，volume / function 仅 CRUD，无专项管理页 |
| AI 语义搜索：自然语言查询数据资产 | 部分 | `resources.go:239-241` `/api/knowledge-search` 为 SQL `LIKE '%q%'`；`heuristic.go:11-55` 二元切分 + IDF 加权（仍是词面）；`resources.go:242-252` rebuild-catalog 把表元数据灌进 knowledge_docs 供 LIKE 匹配 | 无 embedding / 向量库 / 余弦检索；NL 检索未命中指标直接 422（`analysis.go:176-178`） |
| 数据质量监控：规则配置 + 异常检测 + 剖析 | 对齐 | `quality.go:96-141` 规则引擎 7 类（`quality.go:50` 允许 not_null/unique/range/enum/regex/custom_sql/freshness）；`quality.go:219-338` 三模式剖析（空值率 / 基数 / p25–p99 / 直方图 / top_values）；`drift.go:121-238` 五种漂移指标 + 异常 + ack；规则推荐 `quality.go` `suggestQualityRules()` 基于剖析统计生成阈值与理由，`GET /api/quality/suggest` + 质量页「智能推荐」入口 | 前端规则目录 `metadata.json` `rule_types` 只暴露 6 类，缺 `freshness`；规则推荐只在 Agent 工具与质量页入口提供，未做自动下发 |
| 数据安全防护：表级 / 行级权限 + 敏感识别 + 审计 | 部分 | 敏感识别 `governance.go` `scan()`：字段名规则 + 取值抽样（200 行）双重判定，输出 L1–L4 分级、命中比例与真实置信度；输出脱敏 `checkOutput()` 8 类 PII；审计 `server.go` 覆盖全部写操作 + `auditRead()` 标记的读接口（SQL 执行、目录预览、问数、归因、扫描、推理、知识检索） | **表级 / 行级权限整体缺失**（`auth.go` 只有 admin/member 两角色，无授权模型实体）；敏感识别仍为规则 + 抽样，未做模型判定 |
| AI Ready 语义管理：指标 / 维度 / 语义建模 / 本体建模 | 对齐 | 指标 `resources.go:41` + `analysis.go:65-203` 统一口径编译；本体 `resources.go:50-52,253-283` 实体/关系（subject/event/dimension/metric_group）+ 术语表；`pages-ontology.js:91-322` CRUD + SVG 关系图 | 维度靠手填，未从血缘自动推导 |

### 6.5 数据应用（4 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| Dashboard 可视化搭建：拖拽式 + 丰富图表组件 | 部分 | 图表组件 `charts.js` 5 类（kpi/line/bar/pie/table，纯 SVG）；看板搭建 `pages-gov.js`：卡片增删改、图表类型切换、指标引用或只读 SQL 数据源、HTML5 拖拽排序，落库走 `PATCH /api/dashboards/{id}` | 拖拽只调整顺序，尚无自由画布布局（栅格 / 尺寸 / 吸附） |
| AI 自然语言生成图表 | 部分 | `agent.go` `create_dashboard` 工具 + 由 LLM 调工具建看板；`analysis.go` `/api/analysis/dashboard/generate` 走 `a.ask()`（硬编码关键词映射），只返回提问对应的一张卡 | 关键词映射是硬编码规则，不是模型理解；自由画布布局未做 |
| BI 产品对接：Power BI 等 | 缺失 | `assets/metadata.json` 中 powerbi / superset / tableau / quicksight / looker / connector 计数均为 0；`external-catalogs` 是数据源目录而非 BI 产品 | 完全未实现 |
| Agent 构建平台：创建 / 自主调优 / 部署运维 / MCP 接入 / 可观测性 | 部分 | 创建 `pages-ai.js:214-320`；部署运维 `bundle.go` + `pages-cicd.js`；MCP `mcp.go:15-84` stdio + `:88-137` BridgeMCP + `/api/mcp`；可观测 `agent.go:58` traces 表 + `auth.go:145-147` `/api/ops/traces` | **自主调优缺失**（全仓无 tuning / optimizer / feedback / 自优化）；可观测性是全局 trace 聚合，非按 Agent 实例 |

### 6.6 Agent（4 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| 工程 Agent：需求理解 / 数仓方案 / ETL 代码生成 / 智能运维 | 部分 | `agent.go` `create_flow` / `run_flow` / `diagnose_run`；`governance.go` 规则诊断；工程场景工具组含 `create_source` / `create_sync_task` | 无需求理解与数仓方案生成，ETL 仍靠人工写 SQL；诊断为规则引擎 |
| 治理 Agent：质量 / 安全 / 血缘多维问题发现与闭环治理 | 部分 | `agent.go` `scan_sensitive` / `apply_tags` / 质量工具；`governance.go` 扫描打标血缘；`resources.go` 本体实体/关系/术语 | 语义治理止于存储与图谱，无建模编排流程 |
| 分析 Agent：问数 / 归因 / 看板全链路 | 部分 | `agent.go` `query_metric` / `create_metric` / `create_dashboard`；`analysis.go` 归因 | 能力齐备但仍是同一个 Buddy（按场景分流工具与提示词，非独立智能体实例） |
| Agent 安全防护：多层防护 + 防护引擎 | 部分 | `governance.go` `checkInput()` 15 类注入 + 越权；`checkOutput()` 8 类 PII 脱敏；`agent.go` 入口拦截 + 工具审批；`assessTool()` 输出三要素与 OBO 主体校验并接入工具循环 | 无独立防护引擎；OBO 为策略白名单判定，非身份联邦 |

### 6.7 平台基础（6 项）

| 官方动态 | 状态 | 代码证据 | 差距 |
| --- | --- | --- | --- |
| 产品开通计费 | 刻意不做 | `auth.go:11,137` `"billing":"disabled"`；`resources.go:222` `"billing":{"enabled":false}`；schema 无 billing / order 表 | 无开通、计费、订单实体（与开源定位一致） |
| 工作空间管理：创建 / 成员 / 资源配额 | 部分 | `auth.go:70-77` 创建；`auth.go:86-111` 成员增删；`resources.go:195-203` | **无资源配额**：schema 无 quota 表，"资源配额"仅单集群 `cu` 字段（`schema.sql:241`） |
| 多渠道通知配置：邮件 / 企业微信 / 飞书 | 对齐 | `notify.go` 真实投递：webhook / Teams / 企微 / Slack / 飞书 走 HTTP POST，邮件走 SMTP；`metadata.json` `channel_types` = email/webhook/teams/wecom/slack/feishu；工作空间级 `notify.<ws>` 与告警规则渠道在 `emit()` 中打通 | SMTP 需自行配置服务器；投递失败只记录状态，不自动重投 |
| Git 连接管理：工作空间级 Git 连接 | 部分 | `operations.go` 保存 `git.<ws>` + `/test`；`notify.go` `gitRemoteRefs()` 优先执行 `git ls-remote` 真实探测分支，未安装 git 时退回 HTTP 探测 | 仍是只探测，不 clone / push，未打通 Studio / Bundle |
| 双层权限管理：RBAC + ACL | 部分 | 全局 `users.role` + `workspace_members.role`，**均只有 admin / member**（`auth.go:52-56,100-104`）；`server.go:37,90-105` | 无 RBAC 角色表 / 权限矩阵，无对象级 ACL；全局 admin 直接可见全部空间（`server.go:64-65`） |
| 计算资源统一管理：平台 / 实时 / Spark / Ray / GPU + 网络打通 | 部分 | `metadata.json` `cluster_types` = job/interactive(Spark)、platform、realtime(Flink)；`resources.go:44,68-82` | **无 Ray、无 GPU**（metadata/schema 中 gpu 命中为 0）；网络仅自由文本字段 `network`（`schema.sql:242`），无 VPC / 子网实体 |

### 6.8 文档与实现不符的 6 处（本轮全部修复）

下表是上一轮复核发现的「文档声称已有、代码实际没有」的不符项。本轮把每一处都改成了真实实现，而不是改文案。

| 原文档表述 | 修复前的代码实际 | 本轮处置 | 修复后证据 |
| --- | --- | --- | --- |
| `git ls-remote` 真实探测（§3、§5） | `operations.go:96` 为 HTTP HEAD 请求 | 新增 `gitRemoteRefs()`，优先执行 `git ls-remote --heads` 取真实分支；未装 git 时退回 HTTP HEAD 并在响应中说明所用方法 | `notify.go:202` `gitRemoteRefs()`；`web/js/pages-sys.js` 提示同步更新 |
| Agent 安全防护含 OBO 拦截（§5） | `governance.go:180-185` 无 obo 字段；`pages-security.js:480` 读 `res.obo` 恒为空 | 新增 `assessPrincipal()`（工作空间 `obo_policy.<ws>` 白名单，默认 deny）与 `assessTool()`，两者都返回 `obo`，并接入工具循环 | `governance.go:240` `assessPrincipal()`、`:277` `assessTool()`；`/api/security/assess-tool`、`/api/security/check-input`、`GET/PUT /api/security/obo` |
| 智能体支持「工具白名单」（§5） | `resources.go:45` 的 `agents.tools` 未被 agentLoop 读取 | 会话可绑定自定义智能体，`agents.tools` 真正参与运行时工具筛选 | `agent.go:48` `toolsForMode(mode, allow)`；`agent.go:174` `agentProfile()` |
| 通知渠道「真实发送测试」（§5） | `operations.go:35,39` 全部 `simulated: true` | 新增 `internal/platform/notify.go` 真实出网投递；`DATABUDDY_ALERT_SIMULATE=1` 保留离线模拟开关 | `notify.go:114` `deliverAlert()`、`:231` `deliverEmail()`；`operations.go:17` `emit()` 与工作空间 `notify.<ws>` 打通 |
| 数据源模板「对齐」（§5） | 60 个模板中仅 SQLite 有真实取数路径 | `probe()` 重写：SQLite 走只读连接读表清单与行数；非 SQLite 仍 TCP 探活但**明示未验认证**；§5 口径改为「部分」 | `ingest.go` `probe()`；`pages-catalog.js` 面板字段与后端对齐为 `checks/problems/issues/tables/suggestions` |
| AI 生成看板「对齐」（§5） | `analysis.go:348` 固定追加「每日GMV趋势」「品类GMV」两张演示卡 | 删除演示卡，只返回用户提问对应的一张卡 | `analysis.go` `/api/analysis/dashboard/generate` |

本轮另外修掉了三处同类问题，属于复核时新发现的「契约缺口」而非原清单条目：

- 外部目录面板前后端字段不匹配（前端读 `res.issues` / `res.tables`，后端返回 `checks` / `problems`），以及同步结果字段不匹配（前端读 `res.created` / `res.updated` / `res.target`，后端返回 `tables` / `count`）。现已统一为 `checks` / `problems` / `issues` / `tables` / `suggestions` 与 `created` / `updated` / `target`。
- 模型服务对外公布 `/predict` 推理端点但后端无此路由，前端 curl 示例必然 404。现已补真实路由（见 §6.3），并顺手把外部目录命名放宽为允许 Unicode 字母数字（`store.go` `lakeName()`），中文目录名不再被 `validName` 拒绝。
- 上传单文件上限实际是 128 MB（`ingest.go` 的 `128<<20`），而文档与前端提示都写 200 MB；报告推送（`runReport`）写死「本地模拟投递」且渠道名为空。两处都已改为与实现一致：报告推送改走真实投递路径并记录渠道与目标。

### 6.9 改进优先级与本轮完成情况

原清单共 30 项（P0 6 项、P1 10 项、P2 14 项）。本轮完成 P0 全部、P1 全部、P2 中 6 项；剩余 8 项因需要外部依赖或大改造而保留。

**P0 — 缺陷与误导（6 / 6 完成）**

| 原编号 | 事项 | 本轮结果 |
| --- | --- | --- |
| 1 | 外部目录面板字段不匹配 | 完成：后端 `probe()` 重写返回 `checks/problems/issues/tables/suggestions`，同步接口返回 `created/updated/target`，前端字段对齐 |
| 2 | 看板生成固定追加演示卡 | 完成：`analysis.go` 移除两个硬编码卡片 |
| 3 | `/predict` 端点不存在 | 完成：`mlops.go` 新增 `POST /api/serving/services/{id}/predict`，走 SQL 推理语句 + 只读护栏 |
| 4 | 模板数量注释过期 | 完成：`pages-ingest.js` 改为「60 个模板」 |
| 5 | Git 探测文案不实 | 完成：`notify.go` `gitRemoteRefs()` 真实执行 `git ls-remote`，响应中回报所用方法 |
| 6 | OBO 面板永不出现 | 完成：`governance.go` 输出 `obo` 字段，前端面板正常渲染，并补 `GET/PUT /api/security/obo` |

**P1 — 小改动高收益（10 / 10 完成）**

| 原编号 | 事项 | 本轮结果 |
| --- | --- | --- |
| 1 | 三场景 Agent 名副其实 | 完成：`agent.go` `modeGroups` / `modeBriefs` / `toolsForMode()` / `agentSystem()`，会话 `mode` 真正分流工具与提示词 |
| 2 | 工具白名单接入运行时 | 完成：`agents.tools` 进入 `toolsForMode(mode, allow)` |
| 3 | 补一次性 / 事件触发 | 完成：`workflow.go` `run_mode=once` + `run_at`、`run_mode=event` + `trigger_key` 与公开触发端点 `POST /api/workflows/trigger/{key}` |
| 4 | 告警真实投递 | 完成：`notify.go` webhook / Teams / 企微 / Slack / 飞书 走 HTTP POST，邮件走 SMTP；失败记录状态不静默成功 |
| 5 | 数据源真鉴权 | **部分完成**：SQLite 走只读连接读表清单与行数；其余 59 个模板仍为 TCP 探活，响应中明示未验认证 |
| 6 | 敏感识别升级 | 完成：`governance.go` `scan()` 字段名规则 + 取值抽样（200 行）双重判定，输出 L1–L4 分级、命中比例与真实置信度 |
| 7 | 审计覆盖读操作 | 完成：`server.go` `auditRead()` 覆盖 SQL 执行、目录预览、问数、归因、扫描、推理、知识检索 |
| 8 | 防护闭环 | 完成：`assessTool()` 接入 `agentLoop`，评估结果写入 trace 与 approval |
| 9 | 通知渠道补飞书 | 完成：`metadata.json` `channel_types` 增 `feishu`（5 → 6） |
| 10 | 规则建议智能化 | 完成：`quality.go` `suggestQualityRules()` 基于剖析统计生成阈值与理由；`GET /api/quality/suggest` + 质量页「智能推荐」入口；Agent 侧产出 `quality_suggestions` 产物 |

**P2 — 需要外部依赖或较大改造（6 / 14 完成）**

| 原编号 | 事项 | 本轮结果 |
| --- | --- | --- |
| 1 | AI 辅助建源建任务 | 完成：`sync.go` `createSource()` / `createSyncTask()` + `create_source` / `create_sync_task` 工具；字段映射自动补全未做 |
| 2 | 同步任务实体与执行器 | 完成：`sync_tasks` 表 + `runSyncTask()` 全量 / 水位增量 + 调度接入 Tick + 前端「同步与出湖」页签 |
| 3 | 目标表结构演进 | **未做**：仍是 `CREATE OR REPLACE`，缺 `ALTER TABLE ADD COLUMN` 的 DESCRIBE diff 与漂移触发 |
| 4 | 数据对账与出湖 | 完成：`egress.go` `POST /api/ingest/reconcile`、`POST /api/ingest/egress`、`GET /api/ingest/egress` |
| 5 | 在线推理服务 | **部分完成**：`/predict` 路由与真实推理语句齐备；鉴权、限流与预测质量监控未做 |
| 6 | 看板拖拽搭建 | 完成：`pages-gov.js` 卡片增删改、图表类型切换、指标引用或只读 SQL 数据源、HTML5 拖拽排序，落库 `PATCH /api/dashboards/{id}`；自由画布布局（栅格 / 吸附）未做 |
| 7 | 语义检索 | **未做**：仍为 `LIKE` + IDF 加权，无 embedding / 向量索引 |
| 8 | 行级 / 表级权限 | **未做**：仍只有 admin / member 两角色，无授权模型实体 |
| 9 | 数据科学补齐 | **未做**：无在线特征表、实验类型仍为传统 ML、无模型血缘、无 AutoML |
| 10 | 实时同步 | **未做**：CDC 链路与分库分表路由需外部消息中间件 |
| 11 | BI 对接 | **未做**：无 Power BI 等嵌入令牌与语义模型推送 |
| 12 | 工作空间资源配额 | **未做**：schema 无 quota 表 |
| 13 | Ray 集群与 GPU 资源 | **未做**：`cluster_types` 仍为 4 类 |
| 14 | 工程 Agent 的 ETL 代码生成与数仓方案 | **未做**：仍无需求理解与数仓方案生成 |

**剩余待办（8 项，按依赖排序）**

1. **表结构演进** — `ingest.go` 由 `CREATE OR REPLACE` 改为按 `DESCRIBE` diff 执行 `ALTER TABLE ADD COLUMN`，并由 `drift.go` 的 schema 变化触发。纯本地可做，不依赖外部系统。
2. **推理服务鉴权与限流** — 在现有 `/predict` 上补工作空间鉴权与令牌桶限流。
3. **看板自由布局** — 在现有拖拽排序上补栅格、尺寸与吸附。
4. **语义检索** — embedding 列 + 向量索引，替换 `resources.go` 的 `LIKE`。
5. **行级 / 表级权限** — 新增 grant 表与策略求值入口，支持行级谓词。
6. **工作空间资源配额** — schema 增 `workspace_quota` 表。
7. **数据科学补齐** — 在线特征表与同步、实验类型 ml/dl/llm/agent、模型血缘、AutoML。
8. **外部依赖项** — 实时同步（CDC 需消息中间件）、BI 对接、Ray / GPU 集群。

### 6.10 Buddy 任务台（对标官方控制台）

本轮按官方 Buddy 控制台截图重构了前端，任务计划由后端代码模板驱动，而非依赖模型自由输出结构。

**后端**（`internal/platform/agent.go`）

- `planSteps` 为每个场景定义 5–6 步固定计划模板，`newPlan()` 生成待执行计划。
- `invoke()` 是工具执行的唯一入口，无论走 LLM 还是规则兜底，工具调用都会经 `markPlanStep()` 推进对应步骤（running → done / failed）。
- `agentFinish()` 把最终计划写入 `meta.plan`，随消息持久化进历史，重新打开会话仍可回看。

**前端**（`web/js/buddy.js`、`web/styles.css`）

- 左栏按官方样式重构：`Hi, Buddy!` 问候区（含折叠按钮）、`＋ 新建任务`、场景分段控件、`自动化任务 / Skills / 知识库 / 更多` 导航、最近任务列表（带状态点）、可展开的自动化任务列表。交互由 `bindRailNav()` 与 `openMoreMenu()` 承担。
- 主区新增**任务执行计划卡片**：标题显示「任务执行中 N/M」，清单按 ✓ / ⟳ / ○ 三态标记步骤，可折叠，折叠状态记在 `state.foldedPlans`。
- 主区新增**分析数据表**区块：从 `table_list` / `table_schema` 产物渲染表芯片。
- 样式集中在 `web/styles.css` 新增的 `.rail-greet` / `.rail-new` / `.rail-nav` / `.rail-status` / `.plan-*` / `.analysis-block` / `.chip` 与 `@keyframes odb-spin`。

---

## 7. 保留的产品概念（刻意不改）

这些是产品价值的载体，改了就不是复刻了：

- **工作空间** 作为用户/资源/任务的最小集合
- **Catalog.Schema.Table** 三级命名空间（SQL 里怎么写就怎么用）
- **语义层** 收敛口径，指标是 Agent 的"选择题选项"
- **Agent 原生**：工具、轨迹、产物、人工确认，而不是一个聊天框
- **治理闭环**：识别 → 分级 → 打标 → 策略 → 质量监控

---

## 8. 明确不做的事

| 事项 | 原因 |
| --- | --- |
| 等保 / SOC2 / ISO 认证 | 开源项目无法承担认证成本；请自行评估合规要求 |
| 跨地域容灾、多副本 | 单机定位 |
| GPU 调度、模型训练 | 属于 MLOps 范畴，v0.4 再议 |
| 商业计费模块（哪怕可插拔） | 与"移除商业限制"的目标冲突 |
| 兼容原产品 API 协议 | 无公开协议可依，且无必要 |

---

## 9. 迁移指引（如果你手上有原产品的资产）

| 资产 | 迁移方式 |
| --- | --- |
| 表数据 | 导出为 CSV / Parquet → 「数据接入」上传建表 |
| 指标口径 | 在「语义层」重新注册（表达式需去掉云平台方言差异） |
| 质量规则 | 在「数据治理 → 质量规则」重建；类型一一对应 |
| 工作流 | 导出节点 SQL → 「工作流」新建，或让 Buddy 按 SQL 生成 |
| 知识库文档 | 复制文本 → 「知识库」新建 |
| 看板 | 无法直接迁移；用 Buddy 重新生成，或在「仪表盘」重建 |

没有自动迁移工具 —— 云上资产的导出接口不在公开文档里，硬做容易错。
