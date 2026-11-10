# 交接文档 · 2026-11-10

> 本文记录本轮工作的**完成状态、验证结论、以及下次开工的第一件事**。
> 接手时先读这一份，再读 `README.md` / `docs/ARCHITECTURE.md` / `docs/OSS_DIFF.md`。

---

## 一、这轮做了什么

以腾讯云文档 [大数据智能体工作台 DataBuddy 产品功能](https://cloud.tencent.com/document/product/1835/135579) 为能力清单，
审计 `OpenDataBuddy` 仓库并补齐缺失功能。目标是把官方 **8 大能力 / 24 个二级模块** 全部落地，
同时保持项目既有铁律：**零云依赖、零计费、零席位限制、DuckDB + SQLite、无 npm/构建步骤**。

版本从 `0.2.0` → `0.3.0`。

### 数字结果

| 指标 | 0.2.0 | 0.3.0 |
| --- | --- | --- |
| API 路径 | 约 120 | **220** |
| SQLite 表 | 31 | **44** |
| 扩展路由模块 | 0 | **10** |
| 前端页面模块 | 6 | **15**（24 个 js 文件） |
| Python 行数 | — | 26,908 |
| JS 行数 | — | 12,394 |
| **pip 依赖** | 5 | **5（未增加）** |

依赖没变是刻意维持的约束：YAML 解析、KS/PSI/Wasserstein/卡方/JS 散度、SVG 图表、DAG 布局全部手写。

---

## 二、新增文件清单

### 后端引擎（12 个）

| 文件 | 行数 | 能力 |
| --- | --- | --- |
| `databuddy/quality.py` | 1324 | 6 类规则引擎 + 纯标准库 YAML 导入导出 + 前置阻断 |
| `databuddy/profiling.py` | 619 | 三模式剖析（快照/时序/推理）+ 逐列统计 + 基线 |
| `databuddy/drift.py` | 807 | 5 个漂移指标纯 Python + 零配置异常巡检 |
| `databuddy/workflow.py` | 2066 | 9 种节点 × 12 种运行条件 + 两层参数 + 时区 cron |
| `databuddy/bundle.py` | — | 自研 YAML 解析 + CI/CD 六动作 + 幂等发布 |
| `databuddy/features.py` | 797 | Feature Store：CRUD / 版本 / 训练集 / 血缘 |
| `databuddy/mlops.py` | 905 | 注册 / 版本 / 冠军晋级 / 服务 / A-B / 伸缩 |
| `databuddy/lineage.py` | 1067 | 表级+字段级血缘 / BFS / 影响分析 / SQL 自动发现 |
| `databuddy/notebook.py` | 1333 | Notebook 内核 + 13 个魔法命令 |
| `databuddy/analysis.py` | ~1200 | 问数 / 归因 / AI 报告 / 生成看板 |
| `databuddy/reports.py` | ~800 | 报告 CRUD / 快照 / 定时推送 / 嵌出 token |
| `databuddy/ontology.py` | — | 本体实体/关系/术语表 + 同义词解析 |
| `databuddy/alerts.py` | — | 5 渠道告警 + 4 类事件 + 去重 |
| `databuddy/opsview.py` | — | 四层视图 / 健康度 / 规则推理诊断 |
| `databuddy/guardrails_ext.py` | — | 双向 Guardrails / OBO / OWASP LLM Top10 |

### 扩展路由（10 个，`databuddy/api/`）

`quality`(25) · `workflow`(13) · `cicd`(11) · `mlops`(25) · `catalog_ext`(22) · `ingest_ext`(6) · `notebook`(13) · `ops_ext`(22) · `analysis`(20) · `semantic_ext`(14) —— 合计 **171 条**。

### 前端页面（11 个新增）

`pages-quality.js` · `pages-workflow.js` · `pages-cicd.js` · `pages-mlops.js` · `pages-catalog.js` · `pages-ingest.js` · `pages-studio.js` · `pages-analysis.js` · `pages-ops.js` · `pages-security.js` · `pages-ontology.js`

### 修改的既有文件（8 个）

| 文件 | 改动 |
| --- | --- |
| `databuddy/store.py` | 新增 13 张表 + 索引，共 44 表 |
| `databuddy/main.py` | 版本 0.3.0；`CORE_ROUTERS` / `EXTRA_ROUTERS` 两档挂载；新增 `/api/capabilities` |
| `databuddy/flows.py` | `Scheduler.tick` 补 `reports.run_due()`（W8 指出，Lead 修） |
| `databuddy/harness/context.py` | 新增 `glossary_block()`，术语表注入 Buddy 上下文 |
| `databuddy/api/console.py` | 删掉与 `catalog_ext.py` 重复的 3 条 `/volumes` 路由 |
| `databuddy/api/catalog_ext.py` | `VolumeIn.schema` → `schema_name` + `Field(alias="schema")`，消除 pydantic 告警 |
| `web/js/app.js` | `lazyPage()` 动态导入 + 13 条路由接线 + 导航分组 |
| `web/js/icons.js` | 新增 `ontology` / `features` 图标 |
| `web/styles.css` | 新增 CI/CD 样式（+4.7 KB） |

---

## 三、验证状态

### 已通过 ✅

| 项目 | 结论 |
| --- | --- |
| 应用启动 | `LOADED_EXTRAS` 10/10 全加载，无失败 |
| OpenAPI | **220 条路径，0 条重复 (path, method)** |
| SQLite | 44 张表全部建成 |
| 全部 GET 路由 | 79 条无参 GET 全 200；6 条需参数的补参后也全 200 |
| 前端资源 | 16 个静态资源全部 200 |
| 前端模块导出 | 13 个 `lazyPage` 引用全部解析成功 |
| JS 语法 | 15 个模块 `node --check` 零错误 |
| CI/CD 六动作 | init → validate → deploy → 幂等重发 → summary → destroy 全通 |
| 幂等性 | 重复 deploy 返回 `{created:0, updated:0, unchanged:1}` |
| 工作流 | 建流程 → 运行 → `status: success`，节点级明细正确 |
| 质量 | `rules/run-all` → `total:3, passed:3, pass_rate:1.0` |
| 剖析 | `profiling/profile` snapshot 模式正常 |
| 漂移 | 5 个指标全部就绪 |
| 安全 | `check-input` 正确拦截 prompt injection（`action: block`）；OWASP 10/10 |
| 分析 | ask 返回 5 行真实数据；attribute 给出贡献度；report 4 章节；dashboard 3 卡片 |
| 报告定时推送 | 调度器接线后 tick 触发 → `report_runs` +1，同分钟幂等，错时不自增 |
| 告警 | 5 渠道元数据就绪 |
| Notebook | 13 个魔法命令就绪 |
| 本体 | overview 正常 |
| 路由遮蔽 | `/api/sources/templates` 未被 `/api/sources/{id}` 吃掉 |

### 未完成 / 下次接手第一件事 ⚠️

**1. 浏览器人工验收未做。**
所有验证都是 API 层 + 自建 DOM 垫片，**没有在真实浏览器里点过一遍**。
下次开工：

```bash
# 启动（用项目外的 venv，原因见「环境坑」）
$env:DATABUDDY_DATA_DIR = "$env:TEMP\odb-dev"
C:\Users\gayoshan\AppData\Local\Temp\odb-venv\Scripts\python.exe -m uvicorn databuddy.main:app --host 127.0.0.1 --port 8042
```

然后开 `http://127.0.0.1:8042`，账号 `admin@local` / `admin123`，重点看：
- 15 个页面逐个点开，控制台无报错
- 新增页面的**真实交互**：CI/CD 的 YAML 编辑与发布、工作流拖拽、质量四 Tab、目录 SVG 血缘图、Notebook 跨 cell 变量、分析 5 个 Tab
- `pages-mlops.js` 的 A/B 滑块用 `input[type=range]`，拖动没人工试过
- 图表在真实浏览器里的渲染（手写 SVG，未做过跨浏览器检查）

**2. 我的 e2e 脚本已删除**，其中有两处路径当时写错了（已确认正确路径）：
- 工作流是 `/api/workflows`（不是 `/api/flows`），响应包在 `{"workflow": {...}}` 里
- 质量跑批是 `/api/quality/rules/run-all`（不是 `/api/quality/run-all`）
- 剖析是 `/api/profiling/profile`（不是 `/api/profiling/snapshot`）
- 安全检查是 `/api/security/check-input`（不是 `/api/security/scan-input`）

下次要重跑完整 e2e 的话，按这四个正确路径写。

**3. `git` 尚未提交。** 47 个文件变更（8 改 + 39 新增）全在工作区，没有 commit。

---

## 四、环境坑（重要，别再踩）

| 坑 | 正确做法 |
| --- | --- |
| 项目内 `.venv` 建 SQLite 文件被拒（`WinError 5`，但 `os.access` 说可写） | **必须用项目外的 venv**：`C:\Users\gayoshan\AppData\Local\Temp\odb-venv\Scripts\python.exe` |
| uv 管理的 Python 是 externally-managed | 不要往里装包 |
| pip 直连超时 | 用镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 30 --retries 2` |
| 端口 8011 被外部进程占用（PID 25892） | **绝对不要 kill 25892**，换端口 |
| `Start-Process` 报 `NO_PROXY` 字典冲突 | 用后台 job 方式起服务，别用 `Start-Process` |
| PowerShell here-string 里带 `'''` 会断 | 测试脚本写成 `.py` 文件，别用 `-c` 内联 |
| 控制台中文显示乱码 | 只是 PowerShell 编码问题，逻辑正常，用 ASCII 标记验证 |
| 测试污染真实数据 | 每次设 `DATABUDDY_DATA_DIR` 指向临时目录 |

Node 路径：`C:\Users\gayoshan\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe`

---

## 五、架构决策备忘

### 为什么路由分两档

```python
CORE_ROUTERS  = ["auth", "data", "chat", "ops", "console"]   # 缺失 = 构建损坏
EXTRA_ROUTERS = ["quality", "workflow", "cicd", ...]          # 缺失 = 记日志跳过
```

10 个新能力域如果全塞核心路由，任何一个语法错误都会让控制台起不来。现在 `_mount_extras()` 用
`importlib` 逐个试，失败只 warning。`/api/capabilities` 返回实际挂载结果，前端可据此隐藏入口。

前端同理：`lazyPage(module, export, fallback)` 动态 `import()`，失败回退旧实现。

### 为什么表在各模块内建

扩展模块自己 `CREATE TABLE IF NOT EXISTS`，不集中到 `store.py`。删掉一个模块不留死表。

### 为什么这些算法自己写

| 需求 | 常见做法 | 这里 |
| --- | --- | --- |
| 解析 bundle.yaml | pyyaml | ~120 行子集解析器 |
| 漂移检测 | scipy.stats | 5 个指标纯 Python |
| 关系图 / DAG 图 | d3 / mermaid | 纯 SVG 分层布局 |
| 图表 | ECharts | 手写 SVG |

依赖数 5 → 5 不变。

---

## 六、已知限制（都不是 bug，是取舍）

| 限制 | 影响 |
| --- | --- |
| 告警发送是模拟投递 | 只落 `alert_deliveries`，不发真实 HTTP（防误触发外部通知） |
| 数据同步器未实现 | 外部库数据需先导出再上传 |
| 模型服务无真实推理后端 | 只有登记、流量切分、伸缩策略 |
| Notebook 内核是进程内内存态 | 多 worker 部署变量不跨进程 |
| 血缘为表级 + 字段级，但无自动采集 | 需 `auto_discover(sql)` 显式喂 SQL |
| 向量检索是关键词 | 同义改写召回弱 |

---

## 七、下次开工建议顺序

1. **浏览器验收**（上文第 1 项）—— 这是唯一真正没做的验证，且最容易暴露问题
2. 修浏览器里发现的问题
3. `git add -A && git commit`（47 个文件）
4. 可选：`docs/` 补一份「新增能力 API 速查」
5. 可选：给 `analysis` / `reports` 补 OpenAPI 示例

---

*生成于 2026-11-10 · 本轮共 8 个 teammate + Lead，最终由 Lead 完成接线、修缺陷与文档*
