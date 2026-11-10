"""智能接入扩展：数据源模板库 / Agent 诊断 / 接入方案对比。

与 ``api/data.py`` 的 ``/sources`` 既有接口并存，只补充三类新能力：

* ``GET  /sources/templates``        —— 8 大分类、40+ 数据源模板，每项带 fields 定义
* ``POST /sources/{id}/diagnose``    —— Agent 诊断：配置完整性 + 修复建议清单
* ``GET  /ingest/plans``             —— 5 种接入方案（直连 / 定时 / 实时 / 文件 / Agent 采集）

模板只描述"需要填什么"，不绑定任何驱动：真正的取数仍走文件落地或本地 DuckDB，
因此不引入 psycopg2 / pymysql 等外部依赖。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import config, store
from .auth import ctx

router = APIRouter(prefix="/api", tags=["ingest-ext"])


# --------------------------------------------------------------------- 字段助手

def _f(key: str, label: str, typ: str = "text", required: bool = True, **extra: Any) -> dict:
    """构造一个字段定义。typ: text / password / number / select / textarea / checkbox。"""
    field = {"key": key, "label": label, "type": typ, "required": required}
    field.update(extra)
    return field


_HOST = _f("host", "主机地址", "text", True, placeholder="10.0.0.12")
_PORT = lambda default: _f("port", "端口", "number", True, value=default)  # noqa: E731
_DB = lambda label="数据库名": _f("database", label, "text", True)  # noqa: E731
_USER = _f("user", "用户名", "text", True)
_PASSWORD = _f("password", "密码", "password", True)
_SCHEMA = _f("schema", "Schema", "text", False, placeholder="public")
_SSL = _f("ssl", "启用 SSL", "checkbox", False)
_TABLE = _f("table", "表名 / 路径", "text", False)
_PATH = _f("path", "本地路径", "text", True, placeholder="D:/data/export.csv")
_BUCKET = _f("bucket", "Bucket", "text", True)
_AK = _f("access_key", "AccessKey", "text", True)
_SK = _f("secret_key", "SecretKey", "password", True)
_ENDPOINT = _f("endpoint", "Endpoint", "text", False)
_TOPIC = _f("topic", "Topic", "text", True)
_GROUP = _f("group_id", "消费组", "text", False)
_URL = _f("url", "接口地址", "text", True, placeholder="https://api.example.com/v1/orders")
_TOKEN = _f("token", "访问令牌", "password", False)
_APPID = _f("app_id", "AppID", "text", True)
_APPSECRET = _f("app_secret", "AppSecret", "password", True)
_SYNC = _f(
    "sync_mode", "同步方式", "select", True,
    options=["全量覆盖", "增量追加", "CDC 实时"],
)
_BATCH = _f("batch_size", "单批行数", "number", False, value=10000)


def _t(
    tid: str,
    name: str,
    category: str,
    icon: str,
    fields: list[dict],
    doc: str,
    *,
    popularity: int = 60,
    protocol: str = "",
) -> dict:
    return {
        "id": tid,
        "name": name,
        "category": category,
        "icon": icon,
        "fields": fields,
        "doc": doc,
        "popularity": popularity,
        "protocol": protocol or category,
    }


# --------------------------------------------------------------------- 分类

CATEGORIES: list[dict] = [
    {"key": "relational", "label": "关系型数据库", "icon": "catalog", "desc": "OLTP 业务库，走 JDBC 直连或导出文件落地"},
    {"key": "warehouse", "label": "数据仓库", "icon": "table", "desc": "OLAP 分析型引擎，适合大批量离线同步"},
    {"key": "nosql", "label": "NoSQL", "icon": "folder", "desc": "文档 / 键值 / 搜索 / 图数据库"},
    {"key": "object_storage", "label": "对象存储", "icon": "folder", "desc": "S3 兼容协议，适合湖仓分层落地"},
    {"key": "message_queue", "label": "消息队列", "icon": "ops", "desc": "流式接入，实时或准实时写入"},
    {"key": "api", "label": "API 接口", "icon": "mcp", "desc": "HTTP / GraphQL 拉取，适合第三方业务系统"},
    {"key": "saas", "label": "SaaS 应用", "icon": "agents", "desc": "开箱即用的商业应用连接器"},
    {"key": "file", "label": "文件与文件系统", "icon": "ingest", "desc": "上传或定时抓取，最短可用路径"},
]

CATEGORY_LABELS: dict[str, str] = {c["key"]: c["label"] for c in CATEGORIES}


# ----------------------------------------------------------------- 模板清单

SOURCE_TEMPLATES: list[dict] = [
    # ============================================================ 关系型（8）
    _t("mysql", "MySQL", "relational", "catalog",
       [_HOST, _PORT(3306), _DB(), _USER, _PASSWORD, _SCHEMA, _SSL, _SYNC, _BATCH],
       "MySQL 5.7/8.0 业务库接入。建议开只读账号，按 binlog 做 CDC 或按时间戳做增量。",
       popularity=96, protocol="jdbc"),
    _t("postgresql", "PostgreSQL", "relational", "catalog",
       [_HOST, _PORT(5432), _DB(), _USER, _PASSWORD, _SCHEMA, _SSL, _SYNC, _BATCH],
       "PostgreSQL 12+ 接入，支持 schema 级批量发现与逻辑复制槽增量同步。",
       popularity=92, protocol="jdbc"),
    _t("oracle", "Oracle", "relational", "catalog",
       [_HOST, _PORT(1521), _f("service_name", "Service Name", "text", True),
        _USER, _PASSWORD, _SCHEMA, _SYNC],
       "Oracle 11g+ 接入，需要 Service Name 而非 SID；建议用 LogMiner 做增量。",
       popularity=70, protocol="jdbc"),
    _t("sqlserver", "SQL Server", "relational", "catalog",
       [_HOST, _PORT(1433), _DB(), _USER, _PASSWORD, _SCHEMA, _SYNC],
       "Microsoft SQL Server 接入，支持 Change Tracking 增量。",
       popularity=68, protocol="jdbc"),
    _t("sqlite", "SQLite", "relational", "file",
       [_PATH, _TABLE, _SYNC],
       "本地 SQLite 文件，可直接读取表结构并做连通性验证（本平台唯一真连类型）。",
       popularity=80, protocol="file"),
    _t("mariadb", "MariaDB", "relational", "catalog",
       [_HOST, _PORT(3306), _DB(), _USER, _PASSWORD, _SYNC],
       "MariaDB 10.x 接入，协议与 MySQL 兼容。",
       popularity=55, protocol="jdbc"),
    _t("tidb", "TiDB", "relational", "catalog",
       [_HOST, _PORT(4000), _DB(), _USER, _PASSWORD, _SYNC],
       "TiDB 分布式 HTAP 数据库，MySQL 协议兼容，可直接批量拉取。",
       popularity=58, protocol="jdbc"),
    _t("db2", "IBM Db2", "relational", "catalog",
       [_HOST, _PORT(50000), _DB(), _USER, _PASSWORD, _SCHEMA, _SYNC],
       "IBM Db2 LUW 接入，需要授权 schema 的 SELECT 权限。",
       popularity=35, protocol="jdbc"),

    # ============================================================== 数仓（9）
    _t("clickhouse", "ClickHouse", "warehouse", "table",
       [_HOST, _PORT(9000), _DB(), _USER, _PASSWORD, _SYNC, _BATCH],
       "ClickHouse 列式数仓，适合大宽表拉取；注意单批行数别超过 100 万。",
       popularity=85, protocol="http"),
    _t("doris", "Apache Doris", "warehouse", "table",
       [_HOST, _PORT(9030), _DB(), _USER, _PASSWORD, _SYNC],
       "Doris / SelectDB 接入，走 MySQL 协议，支持 Broker Load。",
       popularity=72, protocol="jdbc"),
    _t("starrocks", "StarRocks", "warehouse", "table",
       [_HOST, _PORT(9030), _DB(), _USER, _PASSWORD, _SYNC],
       "StarRocks 极速分析引擎，MySQL 协议兼容。",
       popularity=66, protocol="jdbc"),
    _t("hive", "Apache Hive", "warehouse", "table",
       [_HOST, _PORT(10000), _DB(), _USER, _f("metastore_uris", "Metastore 地址", "text", False),
        _SYNC],
       "Hive Metastore + HiveServer2 接入，适合已有离线数仓的分区表同步。",
       popularity=74, protocol="thrift"),
    _t("greenplum", "Greenplum", "warehouse", "table",
       [_HOST, _PORT(5432), _DB(), _USER, _PASSWORD, _SCHEMA, _SYNC],
       "Greenplum MPP 数仓，PostgreSQL 协议兼容，建议 gpfdist 并行导出。",
       popularity=40, protocol="jdbc"),
    _t("snowflake", "Snowflake", "warehouse", "table",
       [_f("account", "Account 标识", "text", True), _DB(), _f("warehouse", "计算仓库", "text", True),
        _SCHEMA, _USER, _PASSWORD, _SYNC],
       "Snowflake 云数仓，需要 account 标识与计算仓库名。",
       popularity=62, protocol="http"),
    _t("bigquery", "Google BigQuery", "warehouse", "table",
       [_f("project_id", "Project ID", "text", True), _f("dataset", "Dataset", "text", True),
        _f("service_account_json", "服务账号 JSON", "textarea", True), _SYNC],
       "BigQuery 接入，用服务账号 JSON 认证；大表建议按分区表增量抽取。",
       popularity=60, protocol="http"),
    _t("redshift", "Amazon Redshift", "warehouse", "table",
       [_HOST, _PORT(5439), _DB(), _USER, _PASSWORD, _SCHEMA, _SYNC],
       "Redshift 云数仓，PostgreSQL 协议兼容，建议用 UNLOAD 到 S3 再落地。",
       popularity=52, protocol="jdbc"),
    _t("maxcompute", "阿里云 MaxCompute", "warehouse", "table",
       [_f("project", "Project", "text", True), _f("access_id", "AccessId", "text", True),
        _f("access_key", "AccessKey", "password", True), _f("endpoint", "Endpoint", "text", True),
        _SYNC],
       "阿里云 MaxCompute（ODPS）接入，需要 project 与 AK。",
       popularity=64, protocol="http"),

    # ============================================================= NoSQL（7）
    _t("mongodb", "MongoDB", "nosql", "folder",
       [_HOST, _PORT(27017), _DB(), _USER, _PASSWORD, _f("collection", "集合名", "text", False), _SYNC],
       "MongoDB 文档库接入，支持按集合拉取或全库发现。",
       popularity=78, protocol="mongodb"),
    _t("redis", "Redis", "nosql", "folder",
       [_HOST, _PORT(6379), _PASSWORD, _f("db_index", "DB 序号", "number", False, value=0),
        _f("key_pattern", "Key 匹配模式", "text", False, placeholder="order:*")],
       "Redis 键值库，适合小体量维表与缓存快照导出。",
       popularity=60, protocol="redis"),
    _t("cassandra", "Cassandra", "nosql", "folder",
       [_HOST, _PORT(9042), _f("keyspace", "Keyspace", "text", True), _USER, _PASSWORD, _SYNC],
       "Cassandra 宽列库，需要 keyspace；大批量建议按 token range 分片。",
       popularity=38, protocol="cql"),
    _t("hbase", "HBase", "nosql", "folder",
       [_f("zk_quorum", "ZooKeeper 地址", "text", True), _f("table", "表名", "text", True),
        _f("family", "列族", "text", False), _SYNC],
       "HBase 宽列库，通过 ZooKeeper 定位 RegionServer。",
       popularity=32, protocol="thrift"),
    _t("elasticsearch", "Elasticsearch", "nosql", "explore",
       [_f("hosts", "节点地址", "text", True, placeholder="http://10.0.0.1:9200"),
        _f("index", "索引名", "text", True), _USER, _PASSWORD, _SYNC],
       "Elasticsearch 搜索库，通过 _search + scroll 拉取文档。",
       popularity=58, protocol="http"),
    _t("neo4j", "Neo4j", "nosql", "folder",
       [_f("uri", "Bolt URI", "text", True, placeholder="bolt://10.0.0.1:7687"),
        _USER, _PASSWORD, _f("query", "Cypher 查询", "textarea", False), _SYNC],
       "Neo4j 图数据库，用 Cypher 导出节点与关系。",
       popularity=30, protocol="bolt"),
    _t("couchbase", "Couchbase", "nosql", "folder",
       [_HOST, _PORT(8091), _f("bucket", "Bucket", "text", True), _USER, _PASSWORD, _SYNC],
       "Couchbase 文档库，按 bucket 拉取。",
       popularity=26, protocol="http"),

    # ======================================================== 对象存储（7）
    _t("s3", "Amazon S3", "object_storage", "folder",
       [_BUCKET, _AK, _SK, _f("prefix", "对象前缀", "text", False), _f("region", "区域", "text", False),
        _f("file_format", "文件格式", "select", True, options=["CSV", "JSON", "Parquet", "ORC"])],
       "S3 兼容对象存储，按前缀批量发现对象后落地到本地湖仓。",
       popularity=88, protocol="s3"),
    _t("oss", "阿里云 OSS", "object_storage", "folder",
       [_BUCKET, _AK, _SK, _ENDPOINT, _f("prefix", "对象前缀", "text", False)],
       "阿里云 OSS，需要 endpoint 与 Bucket；建议按日期分区前缀同步。",
       popularity=70, protocol="s3"),
    _t("cos", "腾讯云 COS", "object_storage", "folder",
       [_BUCKET, _AK, _SK, _f("region", "地域", "text", True, placeholder="ap-shanghai"),
        _f("prefix", "对象前缀", "text", False)],
       "腾讯云 COS 对象存储，需要地域信息。",
       popularity=66, protocol="s3"),
    _t("minio", "MinIO", "object_storage", "folder",
       [_f("endpoint", "Endpoint", "text", True, placeholder="http://10.0.0.5:9000"),
        _BUCKET, _AK, _SK, _f("prefix", "对象前缀", "text", False)],
       "自建 MinIO，S3 协议兼容，适合内网湖仓。",
       popularity=56, protocol="s3"),
    _t("hdfs", "HDFS", "object_storage", "folder",
       [_f("namenode", "NameNode 地址", "text", True, placeholder="hdfs://10.0.0.1:8020"),
        _f("path", "目录路径", "text", True), _USER, _f("file_format", "文件格式", "select", False,
                                                       options=["Parquet", "ORC", "CSV", "JSON"])],
       "HDFS 目录接入，适合已有 Hadoop 集群的离线数据。",
       popularity=48, protocol="hdfs"),
    _t("azure_blob", "Azure Blob Storage", "object_storage", "folder",
       [_f("account_name", "存储账号", "text", True), _f("account_key", "账号密钥", "password", True),
        _f("container", "容器名", "text", True), _f("prefix", "Blob 前缀", "text", False)],
       "Azure Blob 存储，用账号 + 密钥认证。",
       popularity=34, protocol="http"),
    _t("gcs", "Google Cloud Storage", "object_storage", "folder",
       [_BUCKET, _f("service_account_json", "服务账号 JSON", "textarea", True),
        _f("prefix", "对象前缀", "text", False)],
       "GCS 对象存储，用服务账号 JSON 认证。",
       popularity=32, protocol="http"),

    # ======================================================== 消息队列（6）
    _t("kafka", "Apache Kafka", "message_queue", "ops",
       [_f("bootstrap_servers", "Bootstrap Servers", "text", True,
           placeholder="10.0.0.1:9092,10.0.0.2:9092"),
        _TOPIC, _GROUP, _f("format", "消息格式", "select", True, options=["JSON", "Avro", "Protobuf", "纯文本"]),
        _f("from_beginning", "从最早位点开始", "checkbox", False)],
       "Kafka 实时接入，按 topic 消费后落成微批写入。",
       popularity=86, protocol="kafka"),
    _t("pulsar", "Apache Pulsar", "message_queue", "ops",
       [_f("service_url", "Service URL", "text", True, placeholder="pulsar://10.0.0.1:6650"),
        _TOPIC, _f("subscription", "订阅名", "text", True), _f("format", "消息格式", "select", False,
                                                              options=["JSON", "Avro", "Protobuf"])],
       "Pulsar 多租户消息队列，需要订阅名做消费位点管理。",
       popularity=42, protocol="pulsar"),
    _t("rabbitmq", "RabbitMQ", "message_queue", "ops",
       [_HOST, _PORT(5672), _f("queue", "队列名", "text", True), _USER, _PASSWORD,
        _f("vhost", "VHost", "text", False, value="/")],
       "RabbitMQ 队列接入，适合业务事件类小流量数据。",
       popularity=44, protocol="amqp"),
    _t("rocketmq", "Apache RocketMQ", "message_queue", "ops",
       [_f("namesrv", "NameServer 地址", "text", True), _TOPIC, _GROUP, _AK, _SK],
       "RocketMQ 消息队列，阿里系业务系统常用。",
       popularity=50, protocol="rocketmq"),
    _t("mqtt", "MQTT", "message_queue", "ops",
       [_f("broker", "Broker 地址", "text", True, placeholder="tcp://10.0.0.1:1883"),
        _TOPIC, _f("client_id", "ClientID", "text", False), _USER, _PASSWORD],
       "MQTT 物联网消息接入，适合设备上报类数据。",
       popularity=30, protocol="mqtt"),
    _t("kinesis", "AWS Kinesis", "message_queue", "ops",
       [_f("stream_name", "Stream 名称", "text", True), _f("region", "区域", "text", True),
        _AK, _SK, _f("format", "消息格式", "select", False, options=["JSON", "CSV"])],
       "AWS Kinesis 流式数据，按分片消费。",
       popularity=28, protocol="http"),

    # ============================================================== API（5）
    _t("rest_api", "REST API", "api", "mcp",
       [_URL, _f("method", "请求方法", "select", True, options=["GET", "POST"]),
        _TOKEN, _f("headers", "自定义 Header（JSON）", "textarea", False),
        _f("pagination", "分页方式", "select", False, options=["无", "page/size", "cursor", "offset"]),
        _f("data_path", "数据字段路径", "text", False, placeholder="data.items")],
       "通用 REST 接口拉取，支持分页与嵌套字段定位。",
       popularity=82, protocol="http"),
    _t("graphql", "GraphQL", "api", "mcp",
       [_URL, _f("query", "GraphQL 查询", "textarea", True), _TOKEN, _f("variables", "变量（JSON）", "textarea", False)],
       "GraphQL 接口，按查询语句一次性拉取所需字段。",
       popularity=40, protocol="http"),
    _t("soap", "SOAP / WebService", "api", "mcp",
       [_URL, _f("wsdl", "WSDL 地址", "text", False), _f("action", "SOAPAction", "text", False),
        _f("body", "请求体 XML", "textarea", False), _USER, _PASSWORD],
       "传统 SOAP 接口，多见于 ERP / 政务系统。",
       popularity=24, protocol="http"),
    _t("webhook", "Webhook 推送", "api", "ops",
       [_f("path", "接收路径", "text", True, placeholder="/webhook/orders"),
        _f("secret", "签名密钥", "password", False),
        _f("format", "载荷格式", "select", False, options=["JSON", "Form"])],
       "反向接入：由外部系统推送到本平台，适合事件驱动的轻量同步。",
       popularity=36, protocol="http"),
    _t("grpc", "gRPC", "api", "mcp",
       [_f("target", "服务地址", "text", True, placeholder="10.0.0.1:50051"),
        _f("proto", "Proto 文件", "textarea", False), _f("method", "方法全名", "text", True),
        _TOKEN],
       "gRPC 服务拉取，需要 proto 描述与方法全名。",
       popularity=22, protocol="grpc"),

    # ============================================================= SaaS（10）
    _t("salesforce", "Salesforce", "saas", "agents",
       [_f("instance_url", "Instance URL", "text", True), _f("client_id", "Client ID", "text", True),
        _f("client_secret", "Client Secret", "password", True), _f("sobject", "对象名", "text", True),
        _f("since", "增量起始时间", "text", False)],
       "Salesforce CRM 接入，用 OAuth 客户端凭证拉取 SOQL 结果。",
       popularity=46, protocol="oauth"),
    _t("wecom", "企业微信", "saas", "agents",
       [_APPID, _APPSECRET, _f("agent_id", "AgentId", "text", False),
        _f("scope", "数据范围", "select", False, options=["通讯录", "审批", "客户联系", "会话存档"])],
       "企业微信通讯录与审批数据接入。",
       popularity=68, protocol="oauth"),
    _t("dingtalk", "钉钉", "saas", "agents",
       [_APPID, _APPSECRET, _f("scope", "数据范围", "select", False, options=["通讯录", "审批", "考勤", "日志"])],
       "钉钉开放平台数据接入，需要企业内部应用凭证。",
       popularity=62, protocol="oauth"),
    _t("feishu", "飞书", "saas", "agents",
       [_APPID, _APPSECRET, _f("scope", "数据范围", "select", False, options=["通讯录", "审批", "多维表格", "考勤"])],
       "飞书开放平台数据接入，支持多维表格直接同步成表。",
       popularity=64, protocol="oauth"),
    _t("shopify", "Shopify", "saas", "agents",
       [_f("shop", "店铺域名", "text", True, placeholder="mystore.myshopify.com"),
        _TOKEN, _f("resource", "资源", "select", True, options=["orders", "products", "customers", "inventory"])],
       "Shopify 电商订单与商品接入。",
       popularity=38, protocol="http"),
    _t("youzan", "有赞", "saas", "agents",
       [_APPID, _APPSECRET, _f("kdt_id", "店铺 ID", "text", True),
        _f("resource", "资源", "select", False, options=["订单", "商品", "客户", "退款"])],
       "有赞微商城订单与客户数据接入。",
       popularity=30, protocol="oauth"),
    _t("kingdee", "金蝶云星空", "saas", "agents",
       [_f("server_url", "服务地址", "text", True), _f("acct_id", "账套 ID", "text", True),
        _USER, _PASSWORD, _f("form_id", "业务对象", "text", False, placeholder="SAL_SaleOrder")],
       "金蝶云星空 ERP 接入，按业务对象（formId）拉取单据。",
       popularity=28, protocol="http"),
    _t("yonyou", "用友 YonSuite", "saas", "agents",
       [_f("server_url", "服务地址", "text", True), _f("tenant_id", "租户 ID", "text", True),
        _USER, _PASSWORD, _f("biz_type", "业务类型", "text", False)],
       "用友 ERP 接入，需要租户 ID 与业务类型。",
       popularity=26, protocol="http"),
    _t("ga4", "Google Analytics 4", "saas", "agents",
       [_f("property_id", "媒体资源 ID", "text", True), _f("service_account_json", "服务账号 JSON", "textarea", True),
        _f("metrics", "指标", "text", False, placeholder="sessions,conversions"),
        _f("dimensions", "维度", "text", False, placeholder="date,country")],
       "GA4 分析数据接入，走 Data API 批量报表。",
       popularity=34, protocol="oauth"),
    _t("sensors", "神策数据", "saas", "agents",
       [_f("api_url", "API 地址", "text", True), _f("project", "项目名", "text", True),
        _f("token", "API Token", "password", True), _f("event", "事件名", "text", False)],
       "神策用户行为分析平台事件数据接入。",
       popularity=32, protocol="http"),

    # ============================================================ 文件（8）
    _t("csv", "CSV 文件", "file", "ingest",
       [_PATH, _f("delimiter", "分隔符", "text", False, value=","),
        _f("encoding", "编码", "select", False, options=["UTF-8", "GBK", "UTF-8 BOM"]),
        _f("header", "首行为表头", "checkbox", False, value=True)],
       "最通用的落地方式：CSV 上传后自动推断字段类型并建表。",
       popularity=98, protocol="file"),
    _t("excel", "Excel 工作簿", "file", "ingest",
       [_PATH, _f("sheet", "工作表名", "text", False, placeholder="留空取第一个"),
        _f("header_row", "表头行号", "number", False, value=1)],
       "XLSX 工作簿接入，支持指定工作表与表头行。",
       popularity=90, protocol="file"),
    _t("parquet", "Parquet 文件", "file", "ingest",
       [_PATH, _f("columns", "指定列（逗号分隔）", "text", False)],
       "列式文件，湖仓场景首选；支持只读部分列以降低开销。",
       popularity=84, protocol="file"),
    _t("json", "JSON 文件", "file", "ingest",
       [_PATH, _f("data_path", "数据字段路径", "text", False, placeholder="data.items"),
        _f("array_mode", "数组展开", "checkbox", False)],
       "JSON / NDJSON 文件接入，支持嵌套数组展开成行。",
       popularity=76, protocol="file"),
    _t("xml", "XML 文件", "file", "ingest",
       [_PATH, _f("record_path", "记录节点路径", "text", True, placeholder="//order"),
        _f("namespace", "命名空间", "text", False)],
       "XML 报文接入，按记录节点路径抽取成行。",
       popularity=28, protocol="file"),
    _t("avro", "Avro 文件", "file", "ingest",
       [_PATH, _f("schema", "Schema 地址", "text", False)],
       "Avro 二进制文件，通常来自 Kafka 落盘。",
       popularity=24, protocol="file"),
    _t("orc", "ORC 文件", "file", "ingest",
       [_PATH, _f("columns", "指定列（逗号分隔）", "text", False)],
       "ORC 列式文件，Hive 生态常用。",
       popularity=26, protocol="file"),
    _t("sftp", "FTP / SFTP", "file", "folder",
       [_HOST, _PORT(22), _f("remote_path", "远端路径", "text", True), _USER, _PASSWORD,
        _f("file_pattern", "文件匹配", "text", False, placeholder="*.csv")],
       "定时从 FTP/SFTP 抓取文件后落地建表。",
       popularity=54, protocol="sftp"),
]


# ------------------------------------------------------------------ 接入方案

INGEST_PLANS: list[dict] = [
    {
        "id": "direct",
        "name": "直连查询",
        "tagline": "不搬数据，直接查",
        "icon": "explore",
        "description": "在 SQL 工作台直接对源库执行只读查询，结果即取即用，不落本地湖仓。",
        "scenarios": ["临时排查", "小表关联", "口径核对"],
        "pros": ["零冗余存储", "数据实时一致", "接入成本最低"],
        "cons": ["受源库性能影响", "不支持跨库大表关联", "无法做质量基线"],
        "latency": "秒级",
        "effort": "低",
        "cost": "低",
        "recommended_for": "开发调试与临时分析",
    },
    {
        "id": "scheduled",
        "name": "定时同步",
        "tagline": "按调度批量落地",
        "icon": "flows",
        "description": "用 cron 调度把源表按时间戳增量抽取到本地湖仓，形成可治理的 ODS 层。",
        "scenarios": ["T+1 报表", "数仓分层", "指标口径统一"],
        "pros": ["对源库压力可控", "支持断点续跑", "可接质量规则"],
        "cons": ["存在同步延迟", "需要维护水位字段", "首次全量较重"],
        "latency": "分钟 ~ 小时级",
        "effort": "中",
        "cost": "中",
        "recommended_for": "绝大多数离线分析场景",
    },
    {
        "id": "realtime",
        "name": "实时流接入",
        "tagline": "消息队列微批写入",
        "icon": "ops",
        "description": "消费 Kafka / Pulsar 等消息队列，按微批写入本地湖仓，支撑准实时看板。",
        "scenarios": ["实时大屏", "风控告警", "实时指标"],
        "pros": ["延迟最低", "天然解耦", "可回放重算"],
        "cons": ["运维复杂度高", "需要消息格式治理", "小文件问题明显"],
        "latency": "秒级",
        "effort": "高",
        "cost": "高",
        "recommended_for": "对时效敏感的业务监控",
    },
    {
        "id": "upload",
        "name": "文件上传",
        "tagline": "最短可用路径",
        "icon": "ingest",
        "description": "把 CSV / Excel / Parquet 上传或定时抓取到本地，自动推断字段类型并建表。",
        "scenarios": ["一次性导入", "外部数据补录", "离线对账"],
        "pros": ["零外部依赖", "立即可用", "支持结构化与非结构化卷"],
        "cons": ["需人工或脚本投递", "大文件受上传限制", "不适合高频更新"],
        "latency": "按需",
        "effort": "低",
        "cost": "低",
        "recommended_for": "开源版默认落地方式",
    },
    {
        "id": "agent",
        "name": "Agent 采集",
        "tagline": "让智能体自己接",
        "icon": "buddy",
        "description": "用自然语言描述来源与目标，Agent 自动生成连接配置、字段映射与调度，并给出诊断建议。",
        "scenarios": ["未知数据源探测", "批量接入", "非技术人员自助接入"],
        "pros": ["配置零门槛", "自动诊断与修复建议", "可解释的映射过程"],
        "cons": ["复杂源仍需人工确认", "依赖模型能力", "建议先在小表验证"],
        "latency": "按需",
        "effort": "低",
        "cost": "中",
        "recommended_for": "快速试错与批量登记",
    },
]


# ------------------------------------------------------------------ 辅助

def _jsonable(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _ws(ctxd: dict) -> str:
    ws = ctxd.get("workspace_id")
    if not ws:
        raise HTTPException(status_code=400, detail="请先选择工作空间")
    return ws


def _template_index() -> dict[str, dict]:
    return {t["id"]: t for t in SOURCE_TEMPLATES}


def _guess_template(source_type: str, name: str = "") -> dict | None:
    """按 sources.type 反查模板：支持别名与模糊匹配。"""
    index = _template_index()
    key = (source_type or "").strip().lower()
    aliases = {
        "file": "csv",
        "files": "csv",
        "upload": "csv",
        "postgres": "postgresql",
        "pg": "postgresql",
        "sqlserver": "sqlserver",
        "mssql": "sqlserver",
        "http": "rest_api",
        "api": "rest_api",
        "s3": "s3",
        "excel": "excel",
        "xlsx": "excel",
        "parquet": "parquet",
        "kafka": "kafka",
    }
    if key in index:
        return index[key]
    if key in aliases:
        return index.get(aliases[key])
    for tid, tpl in index.items():
        if key and (key in tid or key in tpl["name"].lower()):
            return tpl
    return None


def _local_path_ok(value: str) -> tuple[bool, str]:
    path = Path(value or "")
    if not value:
        return False, "路径为空"
    if not path.exists():
        return False, f"路径不存在：{value}"
    if path.is_dir():
        return True, f"目录可访问，包含 {len(list(path.glob('*')))} 个条目"
    return True, f"文件可访问，{path.stat().st_size} 字节"


# ------------------------------------------------------------------- 接口


@router.get("/sources/templates")
def source_templates(category: str = "", q: str = "") -> dict:
    """40+ 数据源模板，按 8 大分类返回；支持按分类过滤与关键字搜索。"""
    items = SOURCE_TEMPLATES
    if category:
        items = [t for t in items if t["category"] == category]
    if q:
        needle = q.strip().lower()
        items = [
            t for t in items
            if needle in t["name"].lower() or needle in t["id"].lower()
            or needle in (t["doc"] or "").lower()
        ]
    grouped: dict[str, list[dict]] = {}
    for t in items:
        grouped.setdefault(t["category"], []).append(t)
    groups = [
        {
            "key": c["key"],
            "label": c["label"],
            "icon": c["icon"],
            "desc": c["desc"],
            "count": len(grouped.get(c["key"], [])),
            "templates": sorted(grouped.get(c["key"], []), key=lambda x: -x["popularity"]),
        }
        for c in CATEGORIES
        if grouped.get(c["key"])
    ]
    return {
        "templates": items,
        "groups": groups,
        "categories": CATEGORIES,
        "count": len(items),
        "total": len(SOURCE_TEMPLATES),
    }


@router.get("/sources/templates/{template_id}")
def source_template_detail(template_id: str) -> dict:
    tpl = _template_index().get(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    related = [
        {"id": t["id"], "name": t["name"]}
        for t in SOURCE_TEMPLATES
        if t["category"] == tpl["category"] and t["id"] != tpl["id"]
    ]
    return {"template": tpl, "related": related[:6]}


@router.post("/sources/{source_id}/diagnose")
def diagnose_source(source_id: str, ctxd: dict = Depends(ctx)) -> dict:
    """Agent 诊断：检查配置完整性、给出问题清单与修复建议。"""
    ws = _ws(ctxd)
    row = store.row("SELECT * FROM sources WHERE id = ? AND workspace_id = ?", (source_id, ws))
    if not row:
        raise HTTPException(status_code=404, detail="数据源不存在")
    cfg = _jsonable(row.get("config"))
    tpl = _guess_template(row["type"], row["name"])
    problems: list[dict] = []
    fixes: list[str] = []
    checks: list[dict] = []

    def problem(level: str, code: str, message: str, fix: str, field: str = "") -> None:
        problems.append(
            {"level": level, "code": code, "message": message, "field": field, "fix": fix}
        )
        fixes.append(fix)

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    # 1) 类型是否在模板库里
    check("数据源类型已登记", tpl is not None, row["type"])
    if not tpl:
        problem(
            "warning", "unknown_type",
            f"类型 {row['type']} 不在模板库中，无法校验字段完整性",
            f"在数据源管理里改成模板库中的类型（共 {len(SOURCE_TEMPLATES)} 种），或先用「文件上传」落地",
            "type",
        )

    # 2) 必填字段完整性
    if tpl:
        missing = [
            f for f in tpl["fields"]
            if f.get("required") and not str(cfg.get(f["key"]) or "").strip()
        ]
        check("必填配置项齐全", not missing,
              "、".join(f["label"] for f in missing) if missing else "全部已填")
        for field in missing:
            problem(
                "critical", "missing_field",
                f"缺少必填配置：{field['label']}（{field['key']}）",
                f"补填「{field['label']}」，{tpl['doc'].split('。')[0]}",
                field["key"],
            )
        extra = [k for k in cfg if k not in {f["key"] for f in tpl["fields"]}]
        if extra:
            problem(
                "info", "extra_field",
                f"存在模板未定义的配置项：{'、'.join(extra)}",
                "确认这些额外参数是否被目标系统识别，否则可删除以简化配置",
            )

    # 3) 端口合法性
    port = cfg.get("port")
    if port not in (None, ""):
        try:
            port_int = int(port)
            ok = 0 < port_int < 65536
            check("端口范围合法", ok, str(port))
            if not ok:
                problem("critical", "bad_port", f"端口 {port} 超出 1-65535",
                        "改成目标数据库实际监听端口", "port")
        except (TypeError, ValueError):
            check("端口范围合法", False, str(port))
            problem("critical", "bad_port", f"端口必须是数字，当前为 {port!r}",
                    "填写数字端口，例如 5432 / 3306", "port")

    # 4) 本地路径可访问性（文件 / sqlite 类）
    path_value = cfg.get("path") or ""
    if path_value:
        ok, detail = _local_path_ok(str(path_value))
        check("本地路径可访问", ok, detail)
        if not ok:
            problem("critical", "path_missing", detail,
                    "确认路径存在且有读权限；Windows 下注意反斜杠转义", "path")

    # 5) 主机可达性（有 host 时做 TCP 探测，失败只告警不阻断）
    host = cfg.get("host")
    if host and not path_value:
        import socket

        target_port = int(port or 0) or 5432
        reachable = False
        try:
            with socket.create_connection((str(host), target_port), timeout=2):
                reachable = True
        except Exception as exc:  # noqa: BLE001
            check("主机端口可达", False, f"{host}:{target_port} 不可达（{type(exc).__name__}）")
            problem(
                "warning", "unreachable",
                f"{host}:{target_port} 无法建立 TCP 连接",
                "检查网络策略 / 安全组 / 防火墙；若为内网隔离环境，请改用「文件上传」或导出后落地",
                "host",
            )
        else:
            check("主机端口可达", True, f"{host}:{target_port}")

    # 6) 命名与描述
    if not (row.get("description") or "").strip():
        problem("info", "no_description", "数据源没有描述，后续难以检索与交接",
                "补一句用途说明，例如「生产库只读 · 订单域」", "description")

    # 7) 最近一次连通性测试
    if not row.get("last_test_at"):
        problem("warning", "never_tested", "从未做过连通性测试",
                "点击「连通性测试」先验证一次，确认账号与网络可用")
        check("已做过连通性测试", False, "未测试")
    else:
        check("已做过连通性测试", row.get("status") == "ok",
              f"最近状态 {row.get('status')}，测试于 {row.get('last_test_at')}")
        if row.get("status") == "failed":
            problem("critical", "last_test_failed", "最近一次连通性测试失败",
                    "按测试返回的错误信息修正配置后重测", "")

    # 8) 敏感信息提示
    if cfg.get("password") or cfg.get("secret_key") or cfg.get("app_secret"):
        problem("info", "secret_plain",
                "凭据以明文保存在元数据库中",
                "生产环境建议改用只读账号并限制权限，或通过环境变量注入凭据")

    score = 100
    for p in problems:
        score -= {"critical": 25, "warning": 12, "info": 3}.get(p["level"], 5)
    score = max(0, min(100, score))
    status = "healthy" if score >= 85 else "warning" if score >= 60 else "critical"

    # 推荐替代方案：本地文件类型且路径缺失时，引导到最短可用路径
    suggestions = []
    if any(p["code"] in ("unreachable", "path_missing", "missing_field") for p in problems):
        suggestions.append(
            {"plan": "upload", "name": "文件上传", "reason": "零外部依赖，导出成 CSV 后上传即可建表"}
        )
    if any(p["code"] == "never_tested" for p in problems):
        suggestions.append(
            {"plan": "direct", "name": "直连查询", "reason": "先用只读查询验证账号权限，再决定同步方式"}
        )
    if not suggestions:
        suggestions.append(
            {"plan": "scheduled", "name": "定时同步", "reason": "配置健康，可进入常规调度落地"}
        )

    store.audit(
        ws, ctxd["user"]["id"], "diagnose_source", row["name"],
        {"score": score, "problems": len(problems)},
    )
    return {
        "source": {"id": row["id"], "name": row["name"], "type": row["type"], "status": row.get("status")},
        "template": {"id": tpl["id"], "name": tpl["name"], "category": tpl["category"]} if tpl else None,
        "score": score,
        "status": status,
        "status_label": {"healthy": "健康", "warning": "需关注", "critical": "有阻断问题"}[status],
        "problems": problems,
        "fixes": fixes,
        "checks": checks,
        "suggestions": suggestions,
        "counts": {
            "critical": sum(1 for p in problems if p["level"] == "critical"),
            "warning": sum(1 for p in problems if p["level"] == "warning"),
            "info": sum(1 for p in problems if p["level"] == "info"),
        },
        "diagnosed_at": store.now(),
    }


@router.post("/sources/diagnose-all")
def diagnose_all(ctxd: dict = Depends(ctx)) -> dict:
    """批量诊断：一次跑完工作空间内所有数据源。"""
    ws = _ws(ctxd)
    rows = store.rows("SELECT id, name, type, status FROM sources WHERE workspace_id = ?", (ws,))
    out = []
    for row in rows:
        try:
            result = diagnose_source(row["id"], ctxd)
        except HTTPException:
            continue
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "type": row["type"],
                "score": result["score"],
                "status": result["status"],
                "problems": len(result["problems"]),
            }
        )
    out.sort(key=lambda x: x["score"])
    return {"sources": out, "count": len(out), "average_score": round(sum(o["score"] for o in out) / len(out)) if out else 0}


@router.get("/ingest/plans")
def ingest_plans() -> dict:
    """5 种接入方案对比。"""
    return {
        "plans": INGEST_PLANS,
        "count": len(INGEST_PLANS),
        "dimensions": [
            {"key": "latency", "label": "时效"},
            {"key": "effort", "label": "实施成本"},
            {"key": "cost", "label": "运行成本"},
        ],
    }


class DiagnoseInlineIn(BaseModel):
    type: str = "csv"
    config: dict = {}


@router.post("/ingest/validate")
def validate_config(payload: DiagnoseInlineIn) -> dict:
    """保存前的配置校验（不落库），前端表单实时调用。"""
    tpl = _guess_template(payload.type)
    cfg = payload.config or {}
    problems: list[dict] = []
    if not tpl:
        problems.append(
            {
                "level": "warning",
                "code": "unknown_type",
                "message": f"类型 {payload.type} 不在模板库中",
                "field": "type",
                "fix": "从模板画廊里选择一种类型",
            }
        )
        return {"ok": False, "problems": problems, "template": None}
    for field in tpl["fields"]:
        if field.get("required") and not str(cfg.get(field["key"]) or "").strip():
            problems.append(
                {
                    "level": "critical",
                    "code": "missing_field",
                    "message": f"缺少必填配置：{field['label']}",
                    "field": field["key"],
                    "fix": f"填写「{field['label']}」",
                }
            )
    path_value = cfg.get("path") or ""
    if path_value and payload.type in ("csv", "excel", "parquet", "json", "xml", "avro", "orc", "sqlite"):
        ok, detail = _local_path_ok(str(path_value))
        if not ok:
            problems.append(
                {"level": "critical", "code": "path_missing", "message": detail,
                 "field": "path", "fix": "确认路径存在且可读"}
            )
    return {
        "ok": not any(p["level"] == "critical" for p in problems),
        "problems": problems,
        "template": {"id": tpl["id"], "name": tpl["name"]},
        "required_fields": [f["key"] for f in tpl["fields"] if f.get("required")],
    }
