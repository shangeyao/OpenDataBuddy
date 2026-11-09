# OpenDataBuddy

> 开源版大数据智能体工作台 —— 用对话完成取数、开发、治理与分析。
>
> 以 **DeepSeek Harness** 为 Agent 底座，本地 **DuckDB** 作湖仓，**零云依赖、零计费、零席位限制**。

![status](https://img.shields.io/badge/version-0.1.0-4f46e5) ![python](https://img.shields.io/badge/python-3.10%2B-3776ab) ![license](https://img.shields.io/badge/license-MIT-green)

---

## 这是什么

DataBuddy 类产品解决的核心问题是：**业务问一个数，中间要经过「找表 → 找口径 → 写 SQL → 校验 → 出图」五个环节**。智能体工作台把这条链路压缩成一句话。

本仓库是该形态的**开源重写**：保留产品骨架（工作空间 / Catalog / 语义层 / 工作流 / 治理 / 看板 / Agent 对话），去掉商业外壳（账号体系换成本地、算力换成本地湖仓、计费与席位全部删除）。

一句话概括差异：**能力表基本对齐，商业限制一个不留。**

详细的移除清单见 [docs/OSS_DIFF.md](docs/OSS_DIFF.md)，架构说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 30 秒跑起来

```bash
pip install -r requirements.txt
python run.py
```

浏览器打开 <http://127.0.0.1:8000>，用默认账号登录：

```
邮箱：admin@local
密码：admin123
```

首次启动会自动建库并灌入一套**零售示例数仓**（订单 5000+ 行、客户 600 行、商品、渠道），
所以你打开就能问数、看板、跑治理，而不是面对一个空控制台。

> 依赖只有 5 个：`fastapi` / `uvicorn` / `duckdb` / `python-multipart` / `httpx`。
> 前端是原生 ES Module，**没有 npm、没有构建步骤**。

### 接入大模型（可选，但强烈建议）

未配置模型时，Agent 会走**内置规则引擎**：问数、敏感扫描、质量检查、建任务都能用，
只是无法处理开放式追问。要解锁完整能力，在「设置 → 模型接入」填：

| 字段 | DeepSeek（默认） |
| --- | --- |
| Base URL | `https://api.deepseek.com/v1` |
| 模型 | `deepseek-chat`（或 `deepseek-reasoner`） |
| API Key | 你的 `sk-...` |

也可以换成任意 OpenAI 协议端点：Qwen、Kimi、硅基流动、vLLM、Ollama、LM Studio 都行。
Ollama 本地示例：`http://127.0.0.1:11434/v1` + 模型 `qwen2.5-coder:7b`，Key 随便填。

也可以用环境变量：

```bash
export DEEPSEEK_API_KEY=sk-xxxx
python run.py
```

---

## 能力地图

| 模块 | 你能做什么 | 实现要点 |
| --- | --- | --- |
| **Buddy** | 三种模式对话：数据开发 / 数据治理 / 数据分析；Agent 与 Ask 双模式；工具轨迹可见；高危操作弹确认 | DeepSeek function calling + Harness 六层 |
| **数据目录** | 浏览 `Catalog.Schema.Table`，看字段、类型、标签、样例行、血缘 | 元数据存 SQLite，物理表落 DuckDB schema |
| **数据接入** | 上传 CSV / JSON / Parquet / **XLSX** 自动建表；用 SQL 生成新表 | 类型推断；XLSX 用标准库解析（无 openpyxl） |
| **SQL 工作台** | 写只读查询，结果自动配图 | 单语句、只读、禁文件函数三重护栏 |
| **语义层** | 注册指标口径（表达式 + 主表 + 维度 + 单位），后续问数复用 | 指标 → SQL 确定性编译，避免口径漂移 |
| **工作流** | 顺序 DAG；手动/定时（cron）触发；节点级日志与重试 | 拓扑执行 + 20 秒轮询调度器 |
| **仪表盘** | 卡片 + 图表；支持 AI 生成整块看板 | 结果集自动推断 line / bar / pie / kpi |
| **数据治理** | 敏感字段扫描（分级 + 置信度 + 证据）、写入标注、质量规则、资产盘点 | 列名先验 + 数据抽样正则双证据 |
| **知识库** | 术语、口径、规范文档；元数据一键入库 | bigram + IDF 检索，无需向量库 |
| **设置** | 模型接入与连通性测试、成员管理、审计日志、工具清单、轨迹评测 | 全部落本地库 |

---

## 一次完整体验（3 分钟）

1. **问数** → 打开 Buddy，切到「数据分析」，输入 `近 30 天各渠道 GMV 趋势`
   → 得到结论 + 折线图 + 明细表 + 可展开的生成 SQL。
2. **治理** → 切到「数据治理」，输入 `扫描 dim_customer 的敏感字段`
   → 拿到手机号 / 邮箱 / 身份证的分级、置信度与脱敏样例，可一键写入字段标签。
3. **开发** → 切到「数据开发」，输入 `给 fact_orders 建一个每日销售汇总工作流，早上 7 点跑`
   → Agent 调用 `create_flow`，跑到「工作流」页点运行，看节点级日志。
4. **分析产出** → 输入 `帮我生成一份零售经营看板`
   → 生成带 4 张卡片的仪表盘，可继续在「仪表盘」里查看。

> 接模型后这些是 Agent 自主编排；没接模型时，问数 / 扫描 / 质量 / 表结构这几类走规则引擎同样能跑通。

---

## 目录结构

```
OpenDataBuddy/
├── run.py                     # 启动入口（自动开浏览器）
├── requirements.txt
├── .env.example               # 环境变量样例（全部可选）
├── databuddy/
│   ├── main.py                # FastAPI 应用 + 静态托管
│   ├── config.py              # 路径/端口/密钥
│   ├── store.py               # SQLite 元数据层（schema + 轻量迁移）
│   ├── security.py            # 本地账号：PBKDF2 + 会话 token + RBAC
│   ├── warehouse.py           # DuckDB 湖仓：三级命名空间 + SQL 护栏
│   ├── catalog.py             # 目录解析（逻辑名 ↔ 物理位置）
│   ├── ingest.py              # 文件接入：CSV/JSON/Parquet/XLSX + 类型推断
│   ├── semantic.py            # 指标注册与指标→SQL 编译
│   ├── governance.py          # 敏感识别 / 打标 / 质量规则 / 盘点
│   ├── flows.py               # DAG 拓扑执行 + cron 调度
│   ├── charts.py              # 结果集 → 图表规格推断
│   ├── heuristic.py           # 无模型时的规则引擎
│   ├── tools.py               # 领域工具集（Agent 可调用的 18 个工具）
│   ├── harness/               # ★ DeepSeek Harness
│   │   ├── providers.py       # DeepSeek / OpenAI 兼容 / Ollama
│   │   ├── context.py         # 上下文装配与裁剪
│   │   ├── knowledge.py       # 文档 + 元数据混合检索
│   │   ├── memory.py          # 长期记忆
│   │   ├── skills.py          # 工具注册表（function calling schema）
│   │   ├── guardrails.py      # 三要素判定 + 注入检测 + PII 脱敏
│   │   ├── trace.py           # 轨迹留痕与评测
│   │   └── runtime.py         # Agent 循环 + 审批中断/恢复
│   └── api/                   # auth / data / chat / ops 四组路由
├── web/                       # 无构建前端
│   ├── index.html
│   ├── styles.css
│   └── js/{app,api,ui,charts,artifact,buddy,pages,icons}.js
├── docs/
│   ├── ARCHITECTURE.md
│   └── OSS_DIFF.md
└── data/                      # 运行时生成：元数据库 / 湖仓文件 / 上传件
```

---

## 数据放在哪

全部在 `data/` 下，**不出本机**：

| 文件 | 内容 |
| --- | --- |
| `data/metadata.db` | 用户、工作空间、Catalog 元数据、指标、会话、审计 |
| `data/warehouse.duckdb` | 真实数据（各 schema 的物理表） |
| `data/uploads/` | 上传的原始文件 |
| `data/.secret` | 会话签名密钥（首次启动生成） |

想换位置：`DATABUDDY_DATA_DIR=/your/path python run.py`。
备份就是拷走整个 `data/`。

---

## 安全模型

- **SQL 护栏**：只允许单条 `SELECT/WITH/DESCRIBE/SHOW`，拦截 DDL/DML、`ATTACH`、`INSTALL`、
  文件读写函数（`read_csv` 等）与多语句，防止 NL2SQL 生成危险语句。
- **人工确认**：写操作若同时「改状态 + 触及 L3/L4 敏感字段」，Agent 会挂起并等待确认，
  确认前的多步进度完整保留。
- **PII 脱敏**：手机号 / 邮箱 / 身份证 / 银行卡在识别与展示时默认掩码。
- **注入防护**：检测「忽略以上指令」类表述，仍把输入当数据处理，并在回复中标注。
- **审计**：登录、建表、改口径、打标、跑工作流等动作全部落审计日志。

> 本机单用户场景默认 `127.0.0.1` 监听。要对外提供服务，请自行加反向代理与 HTTPS，
> 并修改默认管理员密码。

---

## 与原版的关系

本项目是**独立实现**，不包含任何腾讯云代码、SDK 或私有接口，也不连接任何云服务。
产品概念（工作空间、Catalog、语义层、Agent 场景模式等）参考了公开文档中的产品设计思路，
实现方式、代码与数据结构均为重写。

已移除：腾讯云 CAM / 子账号 / 企业实名、CU 与积分计费、席位限制、试用期与欠费隔离、
地域锁定、TCLake/DLC/WeData 依赖、各类额度上限。完整清单见 [docs/OSS_DIFF.md](docs/OSS_DIFF.md)。

---

## 开发

```bash
python run.py --reload          # 后端热重载
python -m uvicorn databuddy.main:app --port 8000
```

- API 文档（自动生成）：<http://127.0.0.1:8000/api/docs>
- 前端无打包：改完 `web/js/*.js` 刷新即可。
- 加一个 Agent 工具：在 `databuddy/tools.py` 里用 `@skills.tool(...)` 声明，
  写清 `parameters` 与 `risk`，运行时自动出现在 function calling 列表里。

## 路线图

- [ ] v0.2：Python / Notebook 节点、字段级血缘、DuckDB postgres/mysql 直连
- [ ] v0.2：会话内流式输出（SSE）
- [ ] v0.3：分析空间（空间级知识库隔离）、看板嵌出
- [ ] v0.3：OIDC / LDAP 单点登录

## License

MIT。随便用，出问题别找我。
