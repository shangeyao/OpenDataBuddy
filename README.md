# OpenDataBuddy

> 开源版大数据智能体工作台 —— 用对话完成取数、开发、治理与分析。
>
> **DeepSeek Harness** 作 Agent 底座，本地 **DuckDB** 作湖仓，控制台按腾讯云 / TDesign 规范重建，**零云依赖、零计费、零席位限制**。

![version](https://img.shields.io/badge/version-0.3.0-0052d9) ![python](https://img.shields.io/badge/python-3.10%2B-3776ab) ![license](https://img.shields.io/badge/license-MIT-green)

---

## 这是什么

DataBuddy 类产品解决的核心问题是：**业务问一个数，中间要经过「找表 → 找口径 → 写 SQL → 校验 → 出图」五个环节**。智能体工作台把这条链路压缩成一句话。

本仓库是该形态的**开源重写**：能力结构对齐官方 8 大能力 / 24 个二级模块，去掉商业外壳（账号换本地、算力换本地湖仓、计费与席位全部删除）。

一句话概括差异：**能力表基本对齐，商业限制一个不留。**

详细移除清单见 [docs/OSS_DIFF.md](docs/OSS_DIFF.md)，架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 30 秒跑起来

```bash
pip install -r requirements.txt
python run.py
```

打开 <http://127.0.0.1:8000>，默认账号：

```
邮箱：admin@local
密码：admin123
```

首次启动会自动建库，并灌入**零售示例数仓**（订单 5000+ 行、客户 600 行、商品、渠道）+ 4 个计算资源 + 6 个指标 + 3 个智能体 + 1 份看板 + 已完成的敏感标注。打开就有内容，不是空控制台。

> 依赖只有 5 个：`fastapi` / `uvicorn` / `duckdb` / `python-multipart` / `httpx`。
> 前端是原生 ES Module，**没有 npm、没有构建步骤**。
>
> 0.3.0 补齐了官方文档里的全部二级模块，**依赖数量没有增加一个** —— YAML 解析、KS/PSI/Wasserstein/卡方/JS 散度、SVG 图表、DAG 布局全部自己实现。

### 接入大模型（可选，但强烈建议）

未配置模型时，Agent 走**内置规则引擎**：问数、敏感扫描、质量检查、建任务都能用，只是处理不了开放式追问。要解锁完整能力，在「平台设置 → 模型接入」填：

| 字段 | DeepSeek（默认） |
| --- | --- |
| Base URL | `https://api.deepseek.com/v1` |
| 模型 | `deepseek-chat`（或 `deepseek-reasoner`） |
| API Key | 你的 `sk-...` |

也可以换成任意 OpenAI 协议端点：Qwen、Kimi、硅基流动、vLLM、Ollama、LM Studio。Ollama 示例：`http://127.0.0.1:11434/v1` + `qwen2.5-coder:7b`。

---

## 界面

控制台按腾讯云 / TDesign 规范实现：

- 顶栏 48px：产品名、工作空间、Buddy 快捷入口、地域、文档、消息、帮助、账号
- 侧边栏 200px：按 8 大能力分组，当前项浅蓝底 + 左侧 3px 指示条
- 页头：面包屑 → 标题 → 描述 → 右侧操作按钮
- 内容区 `#F3F3F3`，卡片白底 4px 圆角 + `#E7E7E7` 描边
- 主色 `#0052D9`，正文 14px，控件 32px 高，表格行 48px

### 模块一览（26 个页面）

| 分组 | 模块 |
| --- | --- |
| — | 工作空间概览 |
| 智能体 | **Buddy**（三栏工作台：历史会话 / 对话 / 产物预览） |
| 数据接入 | 数据接入（文件到表 / SQL 建表 / 文件到卷 / 接入任务） |
| 数据工程 | 开发工作台（Notebook）、工作流编排、发布管理（CI/CD）、运维监控 |
| 数据科学 | 特征管理、模型实验、模型管理、模型服务 |
| 数据分析 | 数据探索、仪表盘、分析空间、智能问数 |
| 数据治理 | 统一语义层、**本体建模**、数据质量、数据目录、Agent 安全防护 |
| 数据应用 | 智能体、MCP 服务、模型列表 |
| 平台管理 | 数据源管理、计算资源、成员与权限、工作空间设置 |
| 控制台级 | 工作空间列表、用户管理、平台设置 |

---

## 能力地图

| 能力 | 你能做什么 | 实现要点 |
| --- | --- | --- |
| **Buddy** | 三场景（开发/治理/分析）× Agent/Ask 双模式；工具轨迹、产物预览、高危操作确认、@ 引用、Skills 选择、定时任务 | DeepSeek function calling + Harness 六层 |
| **数据接入** | CSV / JSON / Parquet / **XLSX** 上传自动建表；SQL 建表；**文件到卷**（非结构化落盘）；**60+ 数据源模板** + 5 类接入方案 + Agent 连通诊断 | 类型推断；XLSX 标准库解析；模板驱动 |
| **开发工作台** | 文件树 + Notebook（**13 个魔法命令** `%sql %python %md %tables %desc %run %load %param %time %chart %vars %help %clear`）+ SQL/Python 编辑器 + 参数化运行 + 跨文件引用 | 复用 SQL 引擎；受限命名空间执行 |
| **工作流编排** | **9 种节点**（sql/python/shell/notebook/quality/http/subflow/notify/approval）× **12 种运行条件**；两层参数 `{{workflow.x}}` / `{{nodes.x.output}}`；时区感知 cron；持续运行（60s 下限）；条件跳过 | 拓扑排序 + 环检测 + 节点级落库；20 秒轮询调度 |
| **发布管理（CI/CD）** | bundle.yaml 自研解析（零 pyyaml）；init / validate / deploy / run / summary / destroy 六动作；多环境（dev/staging/prod）幂等发布；drift 对比；销毁只回收自己建的资源 | 配置即代码；deployment 记录追踪归属 |
| **运维监控** | **四层视图**（平台 → 工作空间 → 资源 → 任务）；健康度四因子加权；**规则推理 AI 诊断**（9 类信号）；**5 渠道告警**（邮件/Webhook/Teams/企微/钉钉式 Slack）+ 5 分钟去重 | flow_runs + 错误特征规则，不依赖外部 LLM |
| **特征管理** | 特征定义与物化、按表反查、训练集构建、特征血缘 | 落 DuckDB，版本可追溯 |
| **模型实验/管理/服务** | 实验指标、模型注册、**冠军晋级**（多版本对比 promote）、在线服务启停、**A/B 流量切分 + 弹性伸缩 + 自动启停** | 本地实体，可建可改 |
| **数据探索** | 只读 SQL 直查 + 自动配图 | 单语句、只读、禁文件函数三重护栏 |
| **智能问数** | 一句话出 SQL + 结果表格/图表 + 推理说明；**主动归因**（贡献度排序）；**AI 报告**（分章节 + 定时推送）；**一句话生成看板** | 语义层编译 + charts 自动配图 |
| **仪表盘** | 卡片渲染、AI 生成看板 | 结果集自动推断 line/bar/pie/kpi |
| **分析空间** | 绑定表与指标，Buddy 只在空间内检索；**空间嵌出**（iframe 代码片段） | 空间权限与表权限分离 |
| **统一语义层** | 指标口径注册 → 确定性 SQL 编译；本体关系 | 避免同问两数 |
| **本体建模** | 实体（主体/事件/维度/指标组）× 关系（1:1/1:N/N:1/N:N）× 业务术语表；SVG 关系图；术语同义词解析并注入 Buddy 上下文 | 最长匹配优先；零外部依赖 |
| **数据质量** | **6 类规则**（空值/唯一/范围/枚举/正则/自定义 SQL）× 严重级别 × 阻断标记；**YAML 导入导出**；**前置阻断**接入工作流；规则级告警；**零配置异常检测**（schema/行数/分布/新鲜度） | 纯标准库 YAML 子集解析 |
| **数据剖析与漂移** | 剖析三模式（快照/时序/推理采样）；基线管理；**5 个漂移指标**：KS / PSI / Wasserstein / 卡方 / JS 散度，全部纯 Python 实现 | 无 scipy 依赖 |
| **数据目录** | Catalog.Schema.Table、字段、标签、样例；**表 / 卷 / 模型 / 函数 / 外部目录** 五类资产；**端到端血缘双视图**（上游影响分析 + 自动发现） | 元数据 SQLite + DuckDB schema |
| **Agent 安全防护** | **双向 Guardrails**（输入 15 类注入 + 输出 8 类 PII）；**OBO 代操作拦截**；高危 SQL 判定；**三要素矩阵**；**OWASP LLM Top10** 十项覆盖清单 | 四道防线可逐层追溯 |
| **智能体/应用** | 自定义 Agent（模式 + 提示词 + 工具白名单）、低代码应用 | 与 Buddy 共用工具集 |
| **MCP 服务** | 工具清单、接入配置、安全约束 | **独立 stdio 服务进程**，13 个只读/写入工具 |
| **模型列表** | 多模型 Endpoint、切换与连通测试 | OpenAI 兼容协议 |
| **数据源管理** | 注册连接、连通性测试、关联任务、批量诊断 | 文件/SQLite/PG/MySQL 探测 |
| **计算资源** | 四类集群、CU 配额、启停、网络设置 | 本地实体，无计费 |
| **成员与权限** | 空间成员、双角色（管理员/成员） | 无席位限制 |
| **平台设置** | 模型接入、本地用量、审计、轨迹评测、工具清单 | 替代原「服务订阅」 |
| **工作空间设置** | 通知渠道（企微/Webhook）、Git 连接（真实 ls-remote 探测）、workspace_rule.md | 配置持久化并可用 |
| **Buddy 定时任务** | cron 到期自动建会话并跑 Agent，记录状态与摘要，可回看会话 | 与工作流共用调度器 |

---

## MCP 接入（让外部 Agent 复用同一套工具）

平台把 13 个工具暴露成标准 MCP 工具，外部 Agent 与人在同一条调用链路上，共用护栏与审计。

```bash
# 先登录控制台，从浏览器 localStorage 取 odb.token
export DATABUDDY_TOKEN=你的token
export DATABUDDY_WORKSPACE_ID=可选，默认取第一个空间
python -m databuddy.mcp        # stdio JSON-RPC 2.0
```

Claude Desktop / Cursor 配置：

```json
{
  "mcpServers": {
    "opendatabuddy": {
      "command": "python",
      "args": ["-m", "databuddy.mcp"],
      "cwd": "D:/Project/OpenDataBuddy",
      "env": { "DATABUDDY_TOKEN": "你的token" }
    }
  }
}
```

已实现 `initialize` / `tools/list` / `tools/call`。写操作若命中「三要素」（不可信输入 + 改状态 + 敏感数据），
stdio 场景无法人工确认，服务会直接拒绝并提示到控制台操作 —— 宁可不做，也不越权。


---

## 一次完整体验（3 分钟）

1. **问数** → 打开 Buddy，切「数据分析」，输入 `近 30 天 GMV 趋势`
   → 得到结论 + 折线图 + 明细表 + 可展开 SQL，右侧产物面板同步预览。
2. **治理** → 切「数据治理」，输入 `扫描 dim_customer 的敏感字段`
   → 手机号 / 邮箱 / 身份证的分级、置信度、脱敏样例，可一键写入标签。
3. **开发** → 切「数据开发」，输入 `给 fact_orders 建每日销售汇总工作流，早上 7 点跑`
   → Agent 调 `create_flow`；到「工作流编排」运行，看节点级日志；再到「运维监控」点 AI 诊断。
4. **产出** → 输入 `帮我生成一份零售经营看板` → 生成 4 卡片仪表盘。

> 接模型后这些是 Agent 自主编排；没接模型时，问数 / 扫描 / 质量 / 表结构走规则引擎同样跑通，且轨迹一样留痕。

---

## 目录结构

```
OpenDataBuddy/
├── run.py / start.sh / start.bat
├── requirements.txt
├── databuddy/
│   ├── main.py                # FastAPI 应用 + 静态托管
│   ├── store.py               # SQLite 元数据层（schema + 轻量迁移）
│   ├── security.py            # 本地账号：PBKDF2 + 会话 token + RBAC
│   ├── warehouse.py           # DuckDB 湖仓：三级命名空间 + SQL 护栏
│   ├── catalog.py             # 逻辑名 ↔ 物理位置
│   ├── ingest.py              # CSV/JSON/Parquet/XLSX 接入 + 类型推断
│   ├── semantic.py            # 指标注册与指标→SQL 编译
│   ├── governance.py          # 敏感识别 / 打标 / 质量 / 盘点
│   ├── flows.py               # DAG 拓扑执行 + cron 调度
│   ├── charts.py              # 结果集 → 图表规格
│   ├── heuristic.py           # 无模型时的规则引擎
│   ├── tools.py               # 20 个领域工具
│   ├── harness/               # ★ DeepSeek Harness
│   │   ├── providers.py       # DeepSeek / OpenAI 兼容 / Ollama
│   │   ├── context.py         # 上下文装配与裁剪
│   │   ├── knowledge.py       # 文档 + 元数据混合检索
│   │   ├── memory.py          # 长期记忆
│   │   ├── skills.py          # 工具注册表
│   │   ├── guardrails.py      # 三要素判定 + 注入检测 + PII 脱敏
│   │   ├── trace.py           # 轨迹留痕与评测
│   │   └── runtime.py         # Agent 循环 + 审批中断/恢复
│   └── api/                   # auth / data / chat / ops / console
├── web/
│   ├── index.html             # 顶栏 + 侧边栏 + 页头骨架
│   ├── styles.css             # TDesign 控制台设计系统
│   └── js/
│       ├── app.js             # 路由 + 导航 + 控制台页
│       ├── ui.js              # 表格/弹层/表单/Markdown
│       ├── charts.js          # 自研 SVG 图表
│       ├── buddy.js           # 三栏 Agent 工作台
│       ├── artifact.js        # 产物渲染
│       ├── pages-overview.js  # 概览
│       ├── pages-data.js      # 目录 / 接入 / 探索 / 数据源
│       ├── pages-eng.js       # Studio / 工作流 / 发布 / 运维 / 计算
│       ├── pages-ai.js        # 实验 / 模型 / 服务 / 智能体 / MCP / 模型列表
│       ├── pages-gov.js       # 语义层 / 质量 / 安全 / 分析空间 / 看板
│       └── pages-sys.js       # 工作空间 / 用户 / 平台 / 成员 / 设置
├── docs/
│   ├── ARCHITECTURE.md
│   └── OSS_DIFF.md
└── data/                      # 运行时生成
```

---

## 数据放在哪

全部在 `data/` 下，**不出本机**：

| 文件 | 内容 |
| --- | --- |
| `data/metadata.db` | 用户、工作空间、Catalog 元数据、指标、会话、审计 |
| `data/warehouse.duckdb` | 真实数据（各 schema 的物理表） |
| `data/uploads/` | 上传的原始文件 |
| `data/.secret` | 会话签名密钥 |

换位置：`DATABUDDY_DATA_DIR=/your/path python run.py`。备份 = 拷走整个 `data/`。

---

## 安全模型

- **SQL 护栏**：只允许单条 `SELECT/WITH/DESCRIBE/SHOW`，拦截 DDL/DML、`ATTACH`、`INSTALL`、文件读写函数与多语句。
- **人工确认**：写操作若同时「改状态 + 触及 L3/L4 敏感字段」，Agent 挂起等待确认，确认前的多步进度完整保留。
- **PII 脱敏**：手机号 / 邮箱 / 身份证 / 银行卡默认掩码。
- **注入防护**：检测「忽略以上指令」类表述，仍把输入当数据处理并在回复中标注。
- **审计**：登录、建表、改口径、打标、跑工作流全部落审计日志。

> 默认 `127.0.0.1` 监听。对外提供服务请加反向代理与 HTTPS，并修改默认管理员密码。

---

## 与原版的关系

本项目是**独立实现**，不含任何腾讯云代码、SDK 或私有接口，也不连接任何云服务。
产品概念（工作空间、Catalog、语义层、Agent 场景模式等）参考公开文档的产品设计思路，实现方式、代码与数据结构均为重写。

已移除：CAM / 子账号 / 企业实名、CU 与积分计费、席位限制、试用期与欠费隔离、地域锁定、TCLake/DLC/WeData 依赖、各类额度上限。完整清单见 [docs/OSS_DIFF.md](docs/OSS_DIFF.md)。

---

## 开发

```bash
python run.py --reload          # 后端热重载
```

- API 文档：<http://127.0.0.1:8000/api/docs>
- 前端无打包：改完 `web/js/*.js` 刷新即可。
- 加一个 Agent 工具：`databuddy/tools.py` 里用 `@skills.tool(...)` 声明，写清 `parameters` 与 `risk`，运行时自动出现在 function calling 列表。

## 路线图

- [x] v0.2：独立 MCP server 进程、卷管理、通知渠道、Git 连接、Buddy 定时任务执行
- [ ] v0.3：流式输出（SSE）、字段级血缘、邮件 SMTP、漂移检测
- [ ] v0.4：OIDC / LDAP 单点登录、多工作空间资源隔离

## License

MIT。
