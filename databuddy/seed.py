"""首次启动时生成默认工作空间 + 零售示例数仓 + 指标 + 知识库 + 示例看板。

目的：clone 下来直接就能问数、跑治理、看工作流，而不是面对一个空控制台。
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta

from . import config, security, store, warehouse

CITIES = ["上海", "北京", "深圳", "杭州", "成都", "武汉", "西安", "南京"]
CHANNELS = [
    ("ch_01", "天猫旗舰店", "电商平台"),
    ("ch_02", "京东自营", "电商平台"),
    ("ch_03", "抖音直播", "内容电商"),
    ("ch_04", "线下门店", "线下"),
    ("ch_05", "小程序商城", "私域"),
]
CATEGORIES = {
    "女装": ["连衣裙", "风衣", "针织衫", "阔腿裤"],
    "男装": ["夹克", "衬衫", "卫衣", "休闲裤"],
    "鞋靴": ["运动鞋", "乐福鞋", "短靴"],
    "配饰": ["围巾", "腰带", "棒球帽"],
    "箱包": ["双肩包", "托特包", "斜挎包"],
}
BRANDS = ["自有品牌", "AURORA", "NOAH", "MISTRAL"]


def _w(conn, sql: str, params=None) -> None:
    conn.execute(sql, params or [])


def seed_all() -> None:
    if store.scalar("SELECT count(*) FROM workspaces") or 0:
        return

    admin = store.row("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")
    if not admin:
        return
    admin_id = admin["id"]

    ws_id = store.new_id()
    store.insert(
        "workspaces",
        {
            "id": ws_id,
            "name": "零售数据智能",
            "description": "OpenDataBuddy 示例工作空间，内置零售数仓数据集",
            "owner_id": admin_id,
            "created_at": store.now(),
        },
    )
    store.insert(
        "workspace_members",
        {"workspace_id": ws_id, "user_id": admin_id, "role": "admin"},
        auto_id=False,
    )

    catalog_id = store.new_id()
    store.insert(
        "catalogs",
        {
            "id": catalog_id,
            "workspace_id": ws_id,
            "name": "demo",
            "description": "示例 Catalog，本地 DuckDB 承载",
            "created_at": store.now(),
        },
    )
    schema_id = store.new_id()
    store.insert(
        "schemas",
        {
            "id": schema_id,
            "catalog_id": catalog_id,
            "name": "retail",
            "description": "零售业务域",
            "created_at": store.now(),
        },
    )
    warehouse.ensure_namespace("demo", "retail")

    _build_tables(ws_id, schema_id)
    _build_metrics(ws_id)
    _build_knowledge(ws_id)
    _build_flow(ws_id)
    _build_dashboard(ws_id)
    _build_quality_rules(ws_id)

    store.set_setting("seeded_at", store.now())


# --------------------------------------------------------------------- 数据表


def _build_tables(ws_id: str, schema_id: str) -> None:
    conn = warehouse.connect()
    phys = warehouse.physical_name("demo", "retail")

    conn.execute(f'DROP TABLE IF EXISTS "{phys}".dim_customer')
    conn.execute(f'DROP TABLE IF EXISTS "{phys}".dim_product')
    conn.execute(f'DROP TABLE IF EXISTS "{phys}".dim_channel')
    conn.execute(f'DROP TABLE IF EXISTS "{phys}".fact_orders')

    conn.execute(
        f"""CREATE TABLE "{phys}".dim_channel (
            channel_id VARCHAR, channel_name VARCHAR, channel_type VARCHAR
        )"""
    )
    conn.executemany(f'INSERT INTO "{phys}".dim_channel VALUES (?, ?, ?)', CHANNELS)

    rnd = random.Random(42)
    surnames = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨"
    givens = "伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超"
    customers = []
    for i in range(1, 601):
        name = rnd.choice(surnames) + "".join(rnd.choice(givens) for _ in range(rnd.choice([1, 2])))
        customers.append(
            (
                f"C{i:05d}",
                name,
                rnd.choice(["男", "女"]),
                rnd.randint(18, 62),
                rnd.choice(CITIES),
                f"1{rnd.randint(3, 9)}{rnd.randint(100000000, 999999999)}",
                f"user{i:04d}@example.com",
                f"3101{rnd.randint(10, 99)}{rnd.randint(19000101, 20041231)}",
                date(2023, 1, 1) + timedelta(days=rnd.randint(0, 900)),
                rnd.choice(["黄金", "白银", "普通", "普通"]),
            )
        )
    conn.execute(
        f"""CREATE TABLE "{phys}".dim_customer (
            customer_id VARCHAR, customer_name VARCHAR, gender VARCHAR, age INTEGER,
            city VARCHAR, phone VARCHAR, email VARCHAR, id_card VARCHAR,
            register_date DATE, member_level VARCHAR
        )"""
    )
    conn.executemany(
        f'INSERT INTO "{phys}".dim_customer VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', customers
    )

    products = []
    pid = 1
    for category, names in CATEGORIES.items():
        for pname in names:
            base = rnd.choice([129, 199, 259, 329, 459, 699, 899, 1299])
            products.append(
                (
                    f"P{pid:04d}",
                    f"{pname}{rnd.randint(1, 9)}号",
                    category,
                    rnd.choice(BRANDS),
                    float(base),
                    float(round(base * rnd.uniform(0.72, 0.92), 2)),
                )
            )
            pid += 1
    conn.execute(
        f"""CREATE TABLE "{phys}".dim_product (
            product_id VARCHAR, product_name VARCHAR, category VARCHAR,
            brand VARCHAR, list_price DOUBLE, cost_price DOUBLE
        )"""
    )
    conn.executemany(
        f'INSERT INTO "{phys}".dim_product VALUES (?, ?, ?, ?, ?, ?)', products
    )

    orders = []
    start = date.today() - timedelta(days=179)
    for i in range(1, 5201):
        d = start + timedelta(days=rnd.randint(0, 179))
        # 制造一个近 7 天的下滑，便于演示「异动归因」
        if (date.today() - d).days < 7 and rnd.random() < 0.45:
            continue
        cust = rnd.choice(customers)
        prod = rnd.choice(products)
        qty = rnd.choice([1, 1, 1, 2, 2, 3])
        price = round(prod[4] * rnd.uniform(0.82, 1.0), 2)
        amount = round(price * qty, 2)
        status = "refund" if rnd.random() < 0.045 else "paid"
        orders.append(
            (
                f"O{i:07d}",
                d,
                cust[0],
                prod[0],
                rnd.choice(CHANNELS)[0],
                qty,
                amount,
                round(amount * rnd.uniform(0, 0.15), 2),
                status,
                "APP" if rnd.random() < 0.6 else "WEB",
            )
        )
    conn.execute(
        f"""CREATE TABLE "{phys}".fact_orders (
            order_id VARCHAR, order_date DATE, customer_id VARCHAR, product_id VARCHAR,
            channel_id VARCHAR, quantity INTEGER, amount DOUBLE, discount DOUBLE,
            status VARCHAR, terminal VARCHAR
        )"""
    )
    conn.executemany(
        f'INSERT INTO "{phys}".fact_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', orders
    )

    tables = [
        ("dim_channel", "渠道维度表：电商平台 / 内容电商 / 线下 / 私域", len(CHANNELS)),
        ("dim_customer", "客户维度表，含手机号、邮箱、身份证等敏感字段", len(customers)),
        ("dim_product", "商品维度表：品类、品牌、吊牌价与成本价", len(products)),
        ("fact_orders", "订单事实表：一行一个订单商品行，含金额、折扣、状态", len(orders)),
    ]
    for name, desc, count in tables:
        tid = store.new_id()
        store.insert(
            "tables",
            {
                "id": tid,
                "schema_id": schema_id,
                "name": name,
                "kind": "table",
                "description": desc,
                "row_count": count,
                "created_at": store.now(),
                "updated_at": store.now(),
            },
        )
        for i, col in enumerate(warehouse.describe("demo", "retail", name)):
            store.insert(
                "columns",
                {
                    "id": store.new_id(),
                    "table_id": tid,
                    "name": col["name"],
                    "data_type": col["data_type"],
                    "ordinal": i,
                    "is_nullable": 1,
                    "created_at": store.now(),
                },
            )

    for kind, up, down in [
        ("sql", "demo.retail.fact_orders", "demo.retail.dim_customer"),
        ("sql", "demo.retail.fact_orders", "demo.retail.dim_product"),
    ]:
        store.insert(
            "lineage",
            {
                "id": store.new_id(),
                "workspace_id": ws_id,
                "upstream": up,
                "downstream": down,
                "kind": kind,
                "detail": "订单事实表通过 customer_id 关联客户维度",
                "created_at": store.now(),
            },
        )


# ----------------------------------------------------------------------- 指标


def _build_metrics(ws_id: str) -> None:
    joins = [
        {
            "table": "demo.retail.dim_customer",
            "alias": "dc",
            "on": "dc.customer_id = f.customer_id",
            "columns": ["city", "gender", "member_level", "customer_name"],
        },
        {
            "table": "demo.retail.dim_product",
            "alias": "dp",
            "on": "dp.product_id = f.product_id",
            "columns": ["category", "brand", "product_name"],
        },
        {
            "table": "demo.retail.dim_channel",
            "alias": "dh",
            "on": "dh.channel_id = f.channel_id",
            "columns": ["channel_name", "channel_type"],
        },
    ]
    metrics = [
        (
            "gmv", "GMV", "成交总额，口径为订单金额之和，含未退款订单",
            "sum(f.amount)", "元", ["order_date", "channel_name", "category", "city", "gender"],
        ),
        (
            "order_cnt", "订单量", "去重订单数，一行订单商品明细折成一个订单",
            "count(DISTINCT f.order_id)", "单", ["order_date", "channel_name", "category", "city"],
        ),
        (
            "avg_order_value", "客单价", "GMV / 订单量，反映单笔成交能力",
            "sum(f.amount) / nullif(count(DISTINCT f.order_id), 0)", "元",
            ["order_date", "channel_name", "category"],
        ),
        (
            "customer_cnt", "成交客户数", "当期有成交的去重客户数",
            "count(DISTINCT f.customer_id)", "人", ["order_date", "city", "member_level"],
        ),
        (
            "refund_rate", "退款率", "退款订单数 / 订单总数",
            "sum(CASE WHEN f.status = 'refund' THEN 1 ELSE 0 END) * 1.0 / nullif(count(*), 0)",
            "%", ["order_date", "channel_name", "category"],
        ),
        (
            "qty", "销量", "售出商品件数",
            "sum(f.quantity)", "件", ["order_date", "category", "brand"],
        ),
    ]
    for name, display, desc, expr, unit, dims in metrics:
        store.insert(
            "metrics",
            {
                "id": store.new_id(),
                "workspace_id": ws_id,
                "name": name,
                "display_name": display,
                "description": desc,
                "expr": expr,
                "table_ref": "demo.retail.fact_orders",
                "unit": unit,
                "dimensions": json.dumps(dims, ensure_ascii=False),
                "joins": json.dumps(joins, ensure_ascii=False),
                "owner": "admin",
                "status": "published",
                "created_at": store.now(),
            },
        )


# --------------------------------------------------------------------- 知识库


def _build_knowledge(ws_id: str) -> None:
    docs = [
        (
            "数仓分层规范",
            "ODS：原始贴源层，结构与源系统一致，只做同步不做加工。\n"
            "DWD：明细层，完成清洗、去重、维度关联，保留业务过程粒度。\n"
            "DWS：汇总层，按主题聚合的宽表，供分析与看板使用。\n"
            "DIM：公共维度层，客户、商品、渠道等一致性维度。\n"
            "ADS：应用层，面向具体报表与 Agent 消费的结果表。\n"
            "命名规则：层级前缀 + 业务域 + 描述，例如 dwd_trade_order_detail。",
            "规范",
        ),
        (
            "指标口径说明",
            "GMV = sum(amount)，统计已支付与已退款订单，退款不回冲，另用退款率单独监控。\n"
            "订单量 = count(distinct order_id)，一行订单商品明细折成一个订单。\n"
            "客单价 = GMV / 订单量，保留两位小数。\n"
            "成交客户数 = count(distinct customer_id)，按自然月去重。\n"
            "退款率 = 退款订单数 / 订单总数，按订单数而非金额计算。",
            "指标",
        ),
        (
            "业务术语表",
            "渠道：指订单来源，分为电商平台（天猫、京东）、内容电商（抖音直播）、线下门店、私域（小程序商城）。\n"
            "会员等级：黄金 / 白银 / 普通，按近 12 个月累计消费划分。\n"
            "动销：统计周期内产生过订单的商品。\n"
            "连带率：单个订单平均包含的商品件数。",
            "术语",
        ),
        (
            "敏感数据与合规要求",
            "客户手机号、邮箱、身份证号属于个人敏感信息（PII），对外提供数据时必须脱敏。\n"
            "手机号保留前 3 后 4，中间用 * 掩码；身份证号保留前 6 后 4。\n"
            "涉及个人信息导出的需求需数据管理员审批，并记录审计日志。\n"
            "合规框架参考《个人信息保护法》（PIPL）与 GDPR 的数据最小化原则。",
            "合规",
        ),
    ]
    for title, content, tag in docs:
        store.insert(
            "knowledge_docs",
            {
                "id": store.new_id(),
                "workspace_id": ws_id,
                "title": title,
                "content": content,
                "source_type": "manual",
                "tags": tag,
                "scope": "workspace",
                "created_at": store.now(),
                "updated_at": store.now(),
            },
        )


# ------------------------------------------------------------------- 工作流


def _build_flow(ws_id: str) -> None:
    nodes = [
        {
            "id": "n1",
            "type": "sql",
            "name": "构建每日销售汇总",
            "config": {
                "sql": "SELECT order_date, sum(amount) AS gmv, count(DISTINCT order_id) AS order_cnt, "
                       "count(DISTINCT customer_id) AS customer_cnt "
                       "FROM demo.retail.fact_orders GROUP BY order_date ORDER BY order_date",
            },
        },
        {
            "id": "n2",
            "type": "quality",
            "name": "校验订单金额非空",
            "config": {
                "sql": "SELECT count(*) AS bad FROM demo.retail.fact_orders WHERE amount IS NULL",
            },
        },
        {
            "id": "n3",
            "type": "sql",
            "name": "输出品类贡献",
            "config": {
                "sql": "SELECT p.category, sum(o.amount) AS gmv FROM demo.retail.fact_orders o "
                       "JOIN demo.retail.dim_product p ON p.product_id = o.product_id "
                       "GROUP BY 1 ORDER BY gmv DESC",
            },
        },
    ]
    store.insert(
        "flows",
        {
            "id": store.new_id(),
            "workspace_id": ws_id,
            "name": "每日经营数据刷新",
            "description": "示例工作流：销售汇总 → 质量校验 → 品类贡献，每天 07:00 调度",
            "nodes": json.dumps(nodes, ensure_ascii=False),
            "edges": json.dumps([["n1", "n2"], ["n2", "n3"]], ensure_ascii=False),
            "cron": "0 7 * * *",
            "enabled": 0,
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )


# --------------------------------------------------------------------- 看板


def _build_dashboard(ws_id: str) -> None:
    spec = {
        "cards": [
            {
                "title": "近 30 天 GMV 趋势",
                "chart": {"type": "line", "x": 0, "y": 1},
                "sql": "SELECT * FROM (SELECT order_date, round(sum(amount), 2) AS gmv "
                       "FROM demo.retail.fact_orders GROUP BY 1 ORDER BY 1 DESC LIMIT 30) t ORDER BY order_date",
            },
            {
                "title": "渠道销售占比",
                "chart": {"type": "pie", "x": 0, "y": 1},
                "sql": "SELECT c.channel_name, round(sum(o.amount), 2) AS gmv FROM demo.retail.fact_orders o "
                       "JOIN demo.retail.dim_channel c ON c.channel_id = o.channel_id "
                       "GROUP BY 1 ORDER BY gmv DESC",
            },
            {
                "title": "品类排行",
                "chart": {"type": "bar", "x": 0, "y": 1},
                "sql": "SELECT p.category, round(sum(o.amount), 2) AS gmv FROM demo.retail.fact_orders o "
                       "JOIN demo.retail.dim_product p ON p.product_id = o.product_id "
                       "GROUP BY 1 ORDER BY gmv DESC",
            },
            {
                "title": "大区客户分布",
                "chart": {"type": "bar", "x": 0, "y": 1},
                "sql": "SELECT c.city, count(DISTINCT o.customer_id) AS customers FROM demo.retail.fact_orders o "
                       "JOIN demo.retail.dim_customer c ON c.customer_id = o.customer_id "
                       "GROUP BY 1 ORDER BY customers DESC",
            },
        ]
    }
    store.insert(
        "dashboards",
        {
            "id": store.new_id(),
            "workspace_id": ws_id,
            "name": "零售经营总览",
            "description": "示例看板，由语义层指标直接生成",
            "spec": json.dumps(spec, ensure_ascii=False),
            "created_at": store.now(),
            "updated_at": store.now(),
        },
    )


def _build_quality_rules(ws_id: str) -> None:
    rules = [
        ("demo.retail.fact_orders", "amount", "null_rate", {"max_rate": 0}),
        ("demo.retail.fact_orders", "order_id", "uniqueness", {"max_dup": 0}),
        ("demo.retail.fact_orders", "amount", "range", {"min": 0, "max": 1000000}),
    ]
    for table_ref, column, rule_type, cfg in rules:
        store.insert(
            "quality_rules",
            {
                "id": store.new_id(),
                "workspace_id": ws_id,
                "table_ref": table_ref,
                "column_name": column,
                "rule_type": rule_type,
                "config": json.dumps(cfg, ensure_ascii=False),
                "enabled": 1,
                "created_at": store.now(),
            },
        )


def ensure_admin() -> None:
    if store.scalar("SELECT count(*) FROM users") or 0:
        return
    store.insert(
        "users",
        {
            "id": store.new_id(),
            "email": config.ADMIN_EMAIL,
            "name": "管理员",
            "password_hash": security.hash_password(config.ADMIN_PASSWORD),
            "role": "admin",
            "created_at": store.now(),
        },
    )


def bootstrap() -> None:
    store.init_db()
    ensure_admin()
    seed_all()
