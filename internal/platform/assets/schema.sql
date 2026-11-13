CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',          -- admin / member
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    owner_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',          -- admin / member
    PRIMARY KEY (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS catalogs (
    physical_prefix TEXT DEFAULT '',
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS schemas (
    id TEXT PRIMARY KEY,
    catalog_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (catalog_id, name)
);
CREATE TABLE IF NOT EXISTS tables (
    id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'table',                    -- table / view
    description TEXT DEFAULT '',
    row_count INTEGER DEFAULT 0,
    source_id TEXT,
    tags TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (schema_id, name)
);
CREATE TABLE IF NOT EXISTS columns (
    id TEXT PRIMARY KEY,
    table_id TEXT NOT NULL,
    name TEXT NOT NULL,
    data_type TEXT,
    description TEXT DEFAULT '',
    ordinal INTEGER DEFAULT 0,
    is_nullable INTEGER DEFAULT 1,
    created_at TEXT,
    UNIQUE (table_id, name)
);
CREATE TABLE IF NOT EXISTS column_tags (
    id TEXT PRIMARY KEY,
    column_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    level TEXT DEFAULT 'L1',                      -- L1 公开 / L2 内部 / L3 敏感 / L4 机密
    source TEXT DEFAULT 'manual',                 -- manual / ai / rule
    confidence REAL,
    evidence TEXT,
    created_at TEXT,
    UNIQUE (column_id, tag)
);
CREATE TABLE IF NOT EXISTS lineage (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    upstream TEXT,
    downstream TEXT,
    kind TEXT DEFAULT 'sql',
    detail TEXT,
    created_at TEXT
, "src_column" TEXT, "dst_column" TEXT, "transform" TEXT);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                           -- file / sqlite / postgres / mysql / http
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'unknown',                -- unknown / ok / failed
    last_test_at TEXT,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    display_name TEXT,
    description TEXT DEFAULT '',
    expr TEXT NOT NULL,                           -- 聚合表达式，如 sum(gmv)
    table_ref TEXT NOT NULL,                       -- catalog.schema.table
    filters TEXT DEFAULT '',
    unit TEXT DEFAULT '',
    dimensions TEXT DEFAULT '[]',
    joins TEXT DEFAULT '[]',
    owner TEXT DEFAULT '',
    status TEXT DEFAULT 'published',
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT DEFAULT 'manual',            -- manual / upload / catalog
    tags TEXT DEFAULT '',
    scope TEXT DEFAULT 'workspace',
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    key TEXT,
    value TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    mode TEXT DEFAULT 'engineering',              -- engineering / governance / analysis
    chat_mode TEXT DEFAULT 'agent',               -- agent / ask
    agent_id TEXT DEFAULT '',                     -- 可选：绑定的自定义智能体
    title TEXT DEFAULT '新会话',
    model TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    role TEXT,                                    -- user / assistant / system
    content TEXT,
    meta TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    tool TEXT,
    args TEXT,
    reason TEXT,
    status TEXT DEFAULT 'pending',                -- pending / approved / rejected
    created_at TEXT,
    decided_at TEXT,
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS flows (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    nodes TEXT DEFAULT '[]',
    edges TEXT DEFAULT '[]',
    cron TEXT,
    enabled INTEGER DEFAULT 0,
    retries INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
, "params" TEXT DEFAULT '{}', "run_mode" TEXT DEFAULT 'cron', "interval_seconds" INTEGER DEFAULT 60, "timezone" TEXT DEFAULT '', "run_at" TEXT DEFAULT '', "trigger_key" TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS flow_runs (
    id TEXT PRIMARY KEY,
    flow_id TEXT,
    status TEXT DEFAULT 'running',                -- running / success / failed
    trigger TEXT DEFAULT 'manual',                -- manual / schedule
    started_at TEXT,
    finished_at TEXT,
    log TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    spec TEXT DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS quality_rules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    table_ref TEXT NOT NULL,
    column_name TEXT,
    rule_type TEXT NOT NULL,                      -- null_rate / uniqueness / range / freshness / custom_sql
    config TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    last_status TEXT,
    last_value TEXT,
    last_run_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    action TEXT,
    target TEXT,
    detail TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    workspace_id TEXT,
    step INTEGER DEFAULT 0,
    tool TEXT,
    args TEXT,
    result TEXT,
    ok INTEGER DEFAULT 1,
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS compute_resources (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    type TEXT NOT NULL,                            -- job / interactive / platform / realtime
    engine TEXT DEFAULT '',
    compute_type TEXT DEFAULT 'CPU',
    image TEXT DEFAULT 'Standard-S 1.1',
    cu INTEGER DEFAULT 64,
    status TEXT DEFAULT 'running',                 -- running / stopped / creating / stopping
    network TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_experiments (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    framework TEXT DEFAULT 'PyTorch',
    status TEXT DEFAULT 'success',                 -- running / success / failed
    metrics TEXT DEFAULT '{}',
    owner TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS model_services (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    model_ref TEXT,
    version TEXT DEFAULT 'v1',
    replicas INTEGER DEFAULT 1,
    status TEXT DEFAULT 'running',                 -- running / stopped
    endpoint TEXT DEFAULT '',
    created_at TEXT
, "traffic" TEXT DEFAULT '{}', "min_replicas" INTEGER DEFAULT 1, "max_replicas" INTEGER DEFAULT 4, "target_qps" REAL DEFAULT 50, "idle_timeout_seconds" INTEGER DEFAULT 0, "last_active_at" TEXT DEFAULT '', "description" TEXT DEFAULT '', "updated_at" TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    mode TEXT DEFAULT 'engineering',
    description TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    tools TEXT DEFAULT '[]',
    status TEXT DEFAULT 'published',               -- draft / published
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS apps (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'chat',                      -- chat / dashboard / form
    description TEXT DEFAULT '',
    entry TEXT DEFAULT '',
    status TEXT DEFAULT 'online',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS buddy_tasks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    user_id TEXT,
    name TEXT NOT NULL,
    prompt TEXT DEFAULT '',
    cron TEXT,
    enabled INTEGER DEFAULT 1,
    last_run TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS analysis_spaces (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    tables TEXT DEFAULT '[]',
    metrics TEXT DEFAULT '[]',
    owner TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS buddy_task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    workspace_id TEXT,
    status TEXT DEFAULT 'success',                 -- success / failed
    summary TEXT DEFAULT '',
    conversation_id TEXT,
    error TEXT DEFAULT '',
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS volumes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    catalog TEXT NOT NULL,
    schema TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    file_count INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS quality_rule_runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    rule_id TEXT,
    table_ref TEXT,
    status TEXT,                                  -- pass / fail / error
    value TEXT,
    detail TEXT DEFAULT '',
    ran_at TEXT
);
CREATE TABLE IF NOT EXISTS profile_baselines (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    table_ref TEXT NOT NULL,
    column_name TEXT DEFAULT '',
    mode TEXT DEFAULT 'snapshot',                 -- snapshot / timeseries / inference
    stats TEXT DEFAULT '{}',                      -- 直方图 / 分位数 / 类别分布
    captured_at TEXT
);
CREATE TABLE IF NOT EXISTS anomaly_findings (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    table_ref TEXT,
    column_name TEXT DEFAULT '',
    kind TEXT DEFAULT 'schema',                   -- schema / volume / distribution / freshness
    severity TEXT DEFAULT 'warning',              -- info / warning / critical
    detail TEXT DEFAULT '',
    detected_at TEXT,
    acknowledged INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS flow_node_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    flow_id TEXT,
    node_id TEXT,
    name TEXT DEFAULT '',
    type TEXT DEFAULT 'sql',
    status TEXT DEFAULT 'running',                -- running / success / failed / skipped
    output TEXT DEFAULT '',
    error TEXT DEFAULT '',
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS bundles (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    yaml TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',                  -- draft / valid / invalid
    issues TEXT DEFAULT '[]',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bundle_deployments (
    id TEXT PRIMARY KEY,
    bundle_id TEXT,
    workspace_id TEXT,
    env TEXT DEFAULT 'dev',
    action TEXT DEFAULT 'deploy',                 -- init / validate / deploy / run / summary / destroy
    status TEXT DEFAULT 'success',                -- success / failed
    summary TEXT DEFAULT '',
    log TEXT DEFAULT '[]',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS features (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    entity TEXT DEFAULT '',                       -- 主体：customer / order / product
    dtype TEXT DEFAULT 'float',
    expr TEXT DEFAULT '',
    table_ref TEXT DEFAULT '',
    description TEXT DEFAULT '',
    stats TEXT DEFAULT '{}',
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'published',              -- draft / published
    created_at TEXT,
    updated_at TEXT,
    UNIQUE (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS registered_models (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    source_experiment TEXT DEFAULT '',
    framework TEXT DEFAULT '',
    owner TEXT DEFAULT '',
    created_at TEXT,
    UNIQUE (workspace_id, name)
);
CREATE TABLE IF NOT EXISTS model_versions (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    workspace_id TEXT,
    version TEXT NOT NULL,
    stage TEXT DEFAULT 'staging',                 -- staging / production / archived
    metrics TEXT DEFAULT '{}',
    artifact_uri TEXT DEFAULT '',
    run_id TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS functions (
    id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'udf',                      -- udf / view / macro
    body TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at TEXT,
    UNIQUE (schema_id, name)
);
CREATE TABLE IF NOT EXISTS external_catalogs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'postgres',                 -- postgres / mysql / sqlite / iceberg / hive
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'unknown',                -- unknown / ok / failed
    last_sync_at TEXT,
    description TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS ontology_entities (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                name TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                kind TEXT DEFAULT 'subject',
                description TEXT DEFAULT '',
                table_refs TEXT DEFAULT '[]',
                key_column TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                color TEXT DEFAULT '',
                created_at TEXT
            );
CREATE TABLE IF NOT EXISTS ontology_relations (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                from_entity TEXT NOT NULL,
                to_entity TEXT NOT NULL,
                verb TEXT DEFAULT 'references',
                cardinality TEXT DEFAULT '1:N',
                description TEXT DEFAULT '',
                join_hint TEXT DEFAULT '',
                created_at TEXT
            );
CREATE TABLE IF NOT EXISTS glossary_terms (
                id TEXT PRIMARY KEY,
                workspace_id TEXT,
                term TEXT NOT NULL,
                definition TEXT DEFAULT '',
                synonyms TEXT DEFAULT '[]',
                asset_type TEXT DEFAULT 'table',
                asset_ref TEXT DEFAULT '',
                entity_id TEXT DEFAULT '',
                owner TEXT DEFAULT '',
                status TEXT DEFAULT 'published',
                created_at TEXT
            );
CREATE TABLE IF NOT EXISTS feature_revisions (
            id TEXT PRIMARY KEY,
            feature_id TEXT,
            workspace_id TEXT,
            name TEXT,
            version INTEGER DEFAULT 1,
            entity TEXT DEFAULT '',
            dtype TEXT DEFAULT 'float',
            expr TEXT DEFAULT '',
            table_ref TEXT DEFAULT '',
            description TEXT DEFAULT '',
            stats TEXT DEFAULT '{}',
            status TEXT DEFAULT 'published',
            created_at TEXT
        );
CREATE TABLE IF NOT EXISTS notebooks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    cells TEXT DEFAULT '[]',                      -- [{id,type,source,output,status,elapsed_ms,...}]
    params TEXT DEFAULT '{}',                     -- notebook 级默认参数
    workdir TEXT DEFAULT '',                      -- 工作目录，空则用 data/notebooks
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_channels (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    type TEXT NOT NULL,                           -- email / webhook / teams / wecom / slack / feishu
    name TEXT DEFAULT '',
    config TEXT DEFAULT '{}',
    enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'unknown',                -- unknown / ok / failed
    last_test_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_rules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    event TEXT NOT NULL,                          -- flow_run_failed / quality_rule_failed / drift_detected / anomaly_detected
    channels TEXT DEFAULT '[]',                   -- JSON: 渠道 id 列表
    condition TEXT DEFAULT '{}',                  -- JSON: {min_severity, table_ref, flow_id, keywords}
    severity TEXT DEFAULT 'warning',
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_deliveries (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    rule_id TEXT,
    rule_name TEXT DEFAULT '',
    channel_id TEXT,
    channel_type TEXT DEFAULT '',
    channel_name TEXT DEFAULT '',
    event TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    severity TEXT DEFAULT 'warning',
    dedup_key TEXT DEFAULT '',
    status TEXT DEFAULT 'sent',                   -- sent / suppressed / failed
    simulated INTEGER DEFAULT 1,
    payload TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    topic TEXT DEFAULT '',
    description TEXT DEFAULT '',
    metrics TEXT DEFAULT '[]',                    -- JSON: 指标名列表
    period TEXT DEFAULT 'last_month',
    sections TEXT DEFAULT '[]',                   -- JSON: 最近一次章节快照
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',                  -- draft / ready
    owner TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS report_runs (
    id TEXT PRIMARY KEY,
    report_id TEXT,
    workspace_id TEXT,
    status TEXT DEFAULT 'success',                -- success / failed
    trigger TEXT DEFAULT 'manual',                -- manual / schedule
    title TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    sections TEXT DEFAULT '[]',
    snapshot TEXT DEFAULT '{}',                   -- JSON: 关键数值快照
    channels TEXT DEFAULT '[]',                   -- JSON: 推送渠道与结果
    error TEXT DEFAULT '',
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS report_schedules (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    report_id TEXT,
    cron TEXT DEFAULT '0 9 * * *',
    channels TEXT DEFAULT '[]',                   -- JSON: [{type, target}]
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    last_status TEXT,
    next_run_hint TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_tasks (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    name TEXT NOT NULL,
    source_id TEXT,
    target TEXT DEFAULT '',                    -- catalog.schema
    tables TEXT DEFAULT '[]',                 -- 待同步的源表名，空表示全部
    mode TEXT DEFAULT 'full',                 -- full / incremental
    watermark_column TEXT DEFAULT '',
    watermark_value TEXT DEFAULT '',
    cron TEXT DEFAULT '',
    enabled INTEGER DEFAULT 0,
    last_run_at TEXT DEFAULT '',
    last_status TEXT DEFAULT '',
    last_message TEXT DEFAULT '',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS analysis_embeds (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
    space_id TEXT,
    token TEXT NOT NULL,
    readonly_url TEXT DEFAULT '',
    scope TEXT DEFAULT 'readonly',                -- readonly / interactive
    expires_at TEXT,
    revoked INTEGER DEFAULT 0,
    created_at TEXT
);
-- ---- P2 剩余待办新增实体（见 docs/OSS_DIFF.md §6.9）----

-- P2-3 表结构演进记录：一次 DESCRIBE diff 落一条，记录新增列与累计演进次数
CREATE TABLE IF NOT EXISTS table_evolutions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    table_ref TEXT NOT NULL,
    added_columns TEXT DEFAULT '[]',       -- JSON: [{name,type}]
    removed_columns TEXT DEFAULT '[]',
    before_columns INTEGER DEFAULT 0,
    after_columns INTEGER DEFAULT 0,
    trigger TEXT DEFAULT 'manual',         -- manual / drift
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- P2-5 推理服务访问令牌（只存哈希，明文仅在创建响应里返回一次）
CREATE TABLE IF NOT EXISTS predict_tokens (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    masked TEXT DEFAULT '',
    service_id TEXT DEFAULT '',           -- 空表示工作空间全部服务
    scopes TEXT DEFAULT '["predict"]',    -- JSON 数组
    enabled INTEGER DEFAULT 1,
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT DEFAULT ''
);

-- P2-7 语义检索：文档 embedding 与倒排词条索引（本地哈希向量，无外部模型）
CREATE TABLE IF NOT EXISTS doc_embeddings (
    doc_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    vector TEXT DEFAULT '[]',             -- JSON: 归一化后的稀疏向量 [[idx,weight],...]
    dim INTEGER DEFAULT 256,
    indexed_at TEXT NOT NULL,
    content_hash TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS doc_terms (
    workspace_id TEXT NOT NULL,
    term TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    weight REAL DEFAULT 1,
    PRIMARY KEY (workspace_id, term, doc_id)
);

-- P2-8 行级 / 表级授权实体
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    subject_type TEXT NOT NULL DEFAULT 'user',   -- user / role / workspace
    subject TEXT NOT NULL,                      -- user id / role / '*'
    object_type TEXT NOT NULL DEFAULT 'table',   -- table / catalog / schema / column
    object_ref TEXT NOT NULL,                   -- 三段名或前缀，'*' 表示全部
    actions TEXT DEFAULT '["select"]',          -- select / insert / update / delete / all
    row_predicate TEXT DEFAULT '',              -- 行级谓词，如 region = '华东'
    effect TEXT NOT NULL DEFAULT 'allow',        -- allow / deny
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

-- P2-12 工作空间资源配额：按资源维度限制数量
CREATE TABLE IF NOT EXISTS workspace_quota (
    workspace_id TEXT NOT NULL,
    resource TEXT NOT NULL,                -- tables / flows / dashboards / agents / sources / conversations
    max_items INTEGER DEFAULT -1,          -- -1 表示不限制
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, resource)
);

CREATE INDEX IF NOT EXISTS idx_tables_schema ON tables(schema_id);
CREATE INDEX IF NOT EXISTS idx_columns_table ON columns(table_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_traces_conv ON traces(conversation_id, step);
CREATE INDEX IF NOT EXISTS idx_flow_runs_flow ON flow_runs(flow_id, started_at);
CREATE INDEX IF NOT EXISTS idx_compute_ws ON compute_resources(workspace_id);
CREATE INDEX IF NOT EXISTS idx_agents_ws ON agents(workspace_id);
CREATE INDEX IF NOT EXISTS idx_ont_ent_ws ON ontology_entities(workspace_id);
CREATE INDEX IF NOT EXISTS idx_ont_rel_ws ON ontology_relations(workspace_id);
CREATE INDEX IF NOT EXISTS idx_glossary_ws ON glossary_terms(workspace_id);
CREATE INDEX IF NOT EXISTS idx_notebooks_ws ON notebooks(workspace_id, name);
CREATE INDEX IF NOT EXISTS idx_rule_runs_rule ON quality_rule_runs(rule_id, ran_at);
CREATE INDEX IF NOT EXISTS idx_anomaly_ws ON anomaly_findings(workspace_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_ws ON alert_deliveries(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alert_deliveries_dedup ON alert_deliveries(channel_id, dedup_key, created_at);
CREATE INDEX IF NOT EXISTS idx_report_runs_report ON report_runs(report_id, started_at);
CREATE INDEX IF NOT EXISTS idx_report_sched_ws ON report_schedules(workspace_id);
CREATE INDEX IF NOT EXISTS idx_reports_ws ON reports(workspace_id);
CREATE INDEX IF NOT EXISTS idx_embeds_token ON analysis_embeds(token);
CREATE INDEX IF NOT EXISTS idx_baseline_ref ON profile_baselines(table_ref, column_name);
CREATE INDEX IF NOT EXISTS idx_evolutions_ws ON table_evolutions(workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_predict_tokens_ws ON predict_tokens(workspace_id);
CREATE INDEX IF NOT EXISTS idx_predict_tokens_hash ON predict_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_doc_terms_ws ON doc_terms(workspace_id, term);
CREATE INDEX IF NOT EXISTS idx_doc_embeddings_ws ON doc_embeddings(workspace_id);
CREATE INDEX IF NOT EXISTS idx_grants_ws ON grants(workspace_id, subject);