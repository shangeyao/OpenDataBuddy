"""W8 数据分析引擎：智能问数 / 主动智能归因 / AI 报告 / AI 生成看板。

设计要点
--------
* **智能问数优先走语义层指标路径**：先认指标、再认维度、再认时间，最后交给
  ``semantic.build_metric_sql`` 编译成确定性 SQL。同一个问题永远问出同一个数，
  也不让任何模型自由拼 SQL。
* 指标命中不了才**降级**到「关键词 → 表 / 列」匹配 + 模板 SQL，并在 ``reasoning``
  里明确写出降级原因，避免把猜测包装成结论。
* 归因、报告、看板的结论全部由真实数据算出，纯模板填充，**不调用任何外部 LLM**。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from . import catalog, charts, semantic, store

# ------------------------------------------------------------------ 词表

# 指标别名：语义层里认不出名字时，用业务口语兜底（仍然落到真实指标上）
METRIC_ALIASES: dict[str, list[str]] = {
    "gmv": ["gmv", "成交额", "销售额", "销售金额", "营收", "收入", "营业额", "交易额", "卖了多少钱", "流水"],
    "order_cnt": ["订单量", "订单数", "订单笔数", "多少单", "单量", "成交单数"],
    "avg_order_value": ["客单价", "平均单价", "每单金额", "单均"],
    "customer_cnt": ["客户数", "用户数", "买家数", "成交人数", "多少人买", "买家"],
    "refund_rate": ["退款率", "退货率", "退款比例", "退货比例"],
    "qty": ["销量", "件数", "卖了多少件", "商品件数", "出货量"],
}

# 维度别名：中文口语 → 语义层维度名
DIM_ALIASES: dict[str, list[str]] = {
    "channel_name": ["渠道", "平台", "来源", "天猫", "京东", "抖音", "线下", "小程序", "私域"],
    "channel_type": ["渠道类型", "渠道大类"],
    "category": ["品类", "类目", "分类", "商品类", "商品分类"],
    "city": ["城市", "地域", "地区", "区域", "大区"],
    "gender": ["性别", "男女"],
    "brand": ["品牌"],
    "member_level": ["会员", "会员等级", "等级", "会员级别"],
}

DIM_LABELS: dict[str, str] = {
    "channel_name": "渠道",
    "channel_type": "渠道类型",
    "category": "品类",
    "city": "城市",
    "gender": "性别",
    "brand": "品牌",
    "member_level": "会员等级",
    "order_date": "日期",
    "__date__": "日期",
}

# 可选周期：key → 中文名。前端下拉直接读 /analysis/periods
PERIODS: dict[str, str] = {
    "today": "今天",
    "yesterday": "昨天",
    "last_7d": "近 7 天",
    "last_30d": "近 30 天",
    "last_90d": "近 90 天",
    "this_month": "本月",
    "last_month": "上月",
    "this_year": "今年",
}

TREND_WORDS = ("趋势", "走势", "变化", "每天", "每月", "逐日", "按天", "按周", "按月", "曲线")


def period_options() -> list[dict]:
    """给前端渲染周期下拉。"""
    return [{"value": k, "label": v} for k, v in PERIODS.items()]


# ------------------------------------------------------------------ 周期

def _month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


def _shift_month(day: date, months: int) -> date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    first, last = _month_bounds(date(year, month, 1))
    return date(year, month, min(day.day, last.day))


def resolve_period(period: str | None) -> dict:
    """把周期 key 解析成「本期 / 上期」两段时间窗。

    上期取等长的前一段窗口（月度按自然月，日度按天数），这样环比口径才可比。
    """
    today = date.today()
    key = (period or "").strip().lower()
    if key in ("", "auto", "default"):
        key = "last_month"
    # 允许直接传中文名
    if key not in PERIODS:
        for k, label in PERIODS.items():
            if key == label.replace(" ", ""):
                key = k
                break

    if key == "today":
        cur = (today, today)
    elif key == "yesterday":
        d = today - timedelta(days=1)
        cur = (d, d)
    elif key == "last_7d":
        cur = (today - timedelta(days=6), today)
    elif key == "last_30d":
        cur = (today - timedelta(days=29), today)
    elif key == "last_90d":
        cur = (today - timedelta(days=89), today)
    elif key == "this_month":
        cur = (today.replace(day=1), today)
    elif key == "this_year":
        cur = (today.replace(month=1, day=1), today)
    else:  # last_month 及未知值
        key = "last_month"
        first, last = _month_bounds(today.replace(day=1) - timedelta(days=1))
        cur = (first, last)

    start, end = cur
    span = (end - start).days + 1
    if key == "last_month":
        p_first, p_last = _month_bounds(start - timedelta(days=1))
        prev = (p_first, p_last)
    elif key == "this_month":
        p_first, p_last = _month_bounds(start - timedelta(days=1))
        prev = (p_first, p_last)
    elif key == "this_year":
        prev = (start.replace(year=start.year - 1), _shift_month(end, -12))
    else:
        prev = (start - timedelta(days=span), start - timedelta(days=1))

    return {
        "key": key,
        "label": PERIODS.get(key, key),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev_start": prev[0].isoformat(),
        "prev_end": prev[1].isoformat(),
        "days": span,
    }


def _window_text(win: dict | None, start: str | None, end: str | None) -> str:
    if win:
        return f"{win['label']}（{win['start']} ~ {win['end']}）"
    if start and end:
        return f"{start} ~ {end}" if start != end else start
    if start:
        return f"{start} 起"
    if end:
        return f"截至 {end}"
    return "全量数据"


# ------------------------------------------------------------------ 识别

def _pick_metric(workspace_id: str, text: str) -> dict | None:
    """从问句里认出指标：先精确名，再别名，最后子串。"""
    metrics = semantic.list_metrics(workspace_id)
    if not metrics:
        return None
    q = (text or "").lower()
    best, best_len = None, 0
    for m in metrics:
        for key in (m["name"], m.get("display_name") or ""):
            if key and key.lower() in q and len(key) > best_len:
                best, best_len = m, len(key)
    if best:
        return best
    for name, words in METRIC_ALIASES.items():
        if any(w in q for w in words):
            found = semantic.find_metric(workspace_id, name)
            if found:
                return found
    return None


def _pick_dimension(text: str, metric: dict | None = None) -> str | None:
    """从问句里认出维度；优先取指标声明过的可用维度。"""
    q = (text or "").lower()
    available = set((metric or {}).get("dimensions") or [])
    candidates = []
    for dim, words in DIM_ALIASES.items():
        if available and dim not in available:
            continue
        if any(w in q for w in words):
            candidates.append(dim)
    for dim in available:
        if dim and dim.lower() in q:
            candidates.append(dim)
    for dim in candidates:
        return dim
    return None


def _pick_dimensions(text: str, metric: dict | None = None) -> list[str]:
    q = (text or "").lower()
    available = set((metric or {}).get("dimensions") or [])
    out: list[str] = []
    for dim, words in DIM_ALIASES.items():
        if available and dim not in available:
            continue
        if any(w in q for w in words) and dim not in out:
            out.append(dim)
    return out


def resolve_dimension_name(dimension: str, metric: dict) -> str:
    """把用户传入的维度（可能是中文/简写，如 channel）解析成指标真实维度列。

    顺序：精确匹配 → 别名表 → 前缀/子串匹配 → 原样返回（交给下游报错）。
    """
    raw = (dimension or "").strip()
    available = list(metric.get("dimensions") or [])
    if not raw:
        return available[0] if available else ""
    low = raw.lower()
    for dim in available:
        if dim.lower() == low:
            return dim
    # 中文名或别名 → 维度列
    for dim in available:
        label = DIM_LABELS.get(dim, "")
        if label and (label == raw or label in raw or raw in label):
            return dim
        words = DIM_ALIASES.get(dim, [])
        if any(w == low or w in low or low in w for w in words):
            return dim
    # channel → channel_name 这类前缀补全
    for dim in available:
        if dim.lower().startswith(low) or low.startswith(dim.lower()):
            return dim
    return raw


def _want_trend(text: str) -> bool:
    return any(w in (text or "") for w in TREND_WORDS)


def _granularity(text: str, fallback: str | None) -> str:
    t = (text or "").lower()
    if re.search(r"按周|每周|周度", t):
        return "week"
    if re.search(r"按月|每月|月度", t):
        return "month"
    if re.search(r"按季|季度", t):
        return "quarter"
    return fallback or "day"


# ------------------------------------------------------------------ 数字与叙述

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt_num(value: Any, unit: str = "") -> str:
    """统一的数字格式化：万 / 亿 / 百分比。"""
    if value is None:
        return "-"
    if not _is_number(value):
        return str(value)
    if unit == "%":
        return f"{value * 100:.2f}%"
    tail = f" {unit}" if unit else ""
    if abs(value) >= 1e8:
        return f"{value / 1e8:,.2f} 亿{unit}"
    if abs(value) >= 1e4:
        return f"{value / 1e4:,.2f} 万{unit}"
    return f"{value:,.2f}{tail}"


def _growth(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    if not previous:
        return None
    return (current - previous) / abs(previous)


def _growth_text(rate: float | None) -> str:
    if rate is None:
        return "无可比基期"
    arrow = "增长" if rate >= 0 else "下降"
    return f"{arrow} {abs(rate) * 100:.1f}%"


def _narrate(result: dict, spec: dict, unit: str = "", subject: str = "", window: str = "") -> str:
    """把结果集讲成人话。所有数字都来自 result，不做任何编造。"""
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    head = f"**{subject or '结果'}**" + (f"（{window}）" if window else "")
    if not rows:
        return f"{head}：没有查到数据。可能是该时间段内没有记录，或筛选条件过窄。"

    kind = (spec or {}).get("type")
    if kind == "kpi" or len(rows) == 1 and len(cols) <= 3:
        idx = (spec or {}).get("value_index")
        if idx is None:
            idx = next((i for i in range(len(cols)) if _is_number(rows[0][i])), len(cols) - 1)
        return f"{head}：**{fmt_num(rows[0][idx], unit)}**。"

    x = (spec or {}).get("x", 0)
    y = (spec or {}).get("y")
    if y is None:
        y = next((i for i in range(len(cols)) if _is_number(rows[0][i])), len(cols) - 1)
    pairs = [(r[x], r[y]) for r in rows if _is_number(r[y])]
    if not pairs:
        return f"{head}：共返回 {len(rows)} 行。"

    total = sum(v for _, v in pairs)
    lines: list[str] = []
    if kind == "line":
        values = [v for _, v in pairs]
        lines.append(f"- 区间合计 **{fmt_num(total, unit)}**，共 {len(values)} 个时间点。")
        first, last = values[0], values[-1]
        lines.append(
            f"- 最新一期 **{fmt_num(last, unit)}**，较首期 {_growth_text(_growth(last, first))}。"
        )
        peak = max(range(len(values)), key=lambda i: values[i])
        lines.append(f"- 峰值出现在 **{pairs[peak][0]}**（{fmt_num(pairs[peak][1], unit)}）。")
        rate = _growth(last, first)
        trend = "上行" if (rate or 0) > 0.05 else ("下行" if (rate or 0) < -0.05 else "基本平稳")
        lines.append(f"- 区间整体**{trend}**。")
    else:
        ordered = sorted(pairs, key=lambda p: p[1], reverse=True)
        top = ordered[0]
        lines.append(f"- 合计 **{fmt_num(total, unit)}**，共 {len(ordered)} 个分组。")
        share = top[1] / total * 100 if total else 0
        lines.append(f"- 最高的是 **{top[0]}**（{fmt_num(top[1], unit)}，占比 {share:.1f}%）。")
        if len(ordered) >= 3:
            top3 = sum(v for _, v in ordered[:3])
            ratio = top3 / total * 100 if total else 0
            lines.append(f"- 前三名合计占比 **{ratio:.1f}%**，集中度{'较高' if ratio > 70 else '一般'}。")
        if len(ordered) > 1:
            bottom = ordered[-1]
            gap = (top[1] / bottom[1] - 1) * 100 if bottom[1] else 0
            lines.append(f"- 最低的是 {bottom[0]}（{fmt_num(bottom[1], unit)}），比最高低 {gap:.1f}%。")
    return "\n".join([head + "：", *lines])


def _chart_payload(result: dict, spec: dict | None = None) -> dict:
    """前端 chartHTML(spec, columns, rows) 需要的最小结构。"""
    return {
        "spec": spec or charts.infer(result),
        "columns": result.get("columns") or [],
        "rows": result.get("rows") or [],
        "row_count": result.get("row_count", len(result.get("rows") or [])),
        "elapsed_ms": result.get("elapsed_ms", 0),
    }


# ------------------------------------------------------------------ 智能问数

def ask(
    workspace_id: str,
    question: str,
    period: str | None = None,
    limit: int = 200,
) -> dict:
    """智能问数：指标路径优先，命中不了降级到表匹配。

    返回 ``{answer, sql, columns, rows, chart, reasoning}``，另附 engine / metric /
    dimension / window 等便于前端展示的元信息。
    """
    q = (question or "").strip()
    if not q:
        raise ValueError("问题不能为空")

    reasoning: list[str] = []
    metric = _pick_metric(workspace_id, q)
    start, end, gran = semantic.parse_time_range(q)
    win: dict | None = None
    if not start:
        if period:
            win = resolve_period(period)
            start, end = win["start"], win["end"]
    elif period:
        win = resolve_period(period)
        reasoning.append(
            f"问句自带时间范围（{start} ~ {end}），优先于页面选择的「{win['label']}」。"
        )
        win = None

    if metric:
        return _ask_metric(workspace_id, q, metric, start, end, gran, win, reasoning, limit)
    return _ask_fallback(workspace_id, q, start, end, win, reasoning, limit)


def _ask_metric(
    workspace_id: str,
    question: str,
    metric: dict,
    start: str | None,
    end: str | None,
    gran: str | None,
    win: dict | None,
    reasoning: list[str],
    limit: int,
) -> dict:
    label = metric.get("display_name") or metric["name"]
    unit = metric.get("unit") or ""
    dim = _pick_dimension(question, metric)
    trend = _want_trend(question)

    reasoning.append(
        f"命中语义层指标「{label}」（{metric['name']}），口径 `{metric['expr']}`，"
        f"数据源 `{metric['table_ref']}`，因此走指标编译而不是自由生成 SQL。"
    )

    dims: list[str] = []
    granularity: str | None = None
    if trend:
        granularity = _granularity(question, gran)
        dims = ["__date__"]
        reasoning.append(
            f"问句在问趋势，按「{granularity}」粒度做时间聚合。"
            + (f"（同时提到{ DIM_LABELS.get(dim, dim) if dim else '维度'}，趋势优先，维度未展开）" if dim else "")
        )
    elif dim:
        dims = [dim]
        reasoning.append(f"识别到分析维度「{DIM_LABELS.get(dim, dim)}」（{dim}），按该维度分组对比。")
    else:
        reasoning.append("没有识别到维度，先给汇总值（KPI）。")

    built = semantic.build_metric_sql(
        metric,
        dims,
        start,
        end,
        granularity,
        limit=limit,
        order="time" if "__date__" in dims else "auto",
    )
    window_text = _window_text(win, start, end)
    reasoning.append(f"时间范围：{window_text}。")

    result = catalog.run_workspace_sql(workspace_id, built["sql"], limit=limit)
    spec = charts.infer(result)
    answer = _narrate(result, spec, unit, label, window_text)
    if not result.get("rows"):
        reasoning.append("查询返回 0 行，结论里如实说明没有数据，不做推测。")

    return {
        "answer": answer,
        "sql": result.get("sql") or built["sql"],
        "columns": result.get("columns") or [],
        "rows": result.get("rows") or [],
        "chart": spec,
        "chart_data": _chart_payload(result, spec),
        "reasoning": "\n".join(f"{i + 1}. {line}" for i, line in enumerate(reasoning)),
        "engine": "metric",
        "row_count": result.get("row_count", 0),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "metric": {"name": metric["name"], "display_name": label, "unit": unit, "expr": metric["expr"]},
        "dimension": dims[0] if dims else "",
        "window": window_text,
        "period": win,
        "question": question,
    }


def _table_score(table: dict, question: str) -> tuple[int, list[str]]:
    """表匹配打分：表名命中权重最高，其次是列名命中。"""
    q = (question or "").lower()
    score = 0
    hits: list[str] = []
    name = (table.get("name") or "").lower()
    if name and name in q:
        score += 6
    for part in (table.get("description") or "").replace("，", ",").split(","):
        part = part.strip()
        if part and part.lower() in q:
            score += 1
    for token in re.split(r"[\s,，、_]+", name):
        if len(token) >= 3 and token in q:
            score += 2
    return score, hits


def _ask_fallback(
    workspace_id: str,
    question: str,
    start: str | None,
    end: str | None,
    win: dict | None,
    reasoning: list[str],
    limit: int,
) -> dict:
    """指标没命中时的降级路径：关键词 → 表/列 → 模板 SQL。"""
    reasoning.append("没有命中语义层指标，降级到「关键词 → 表 / 列」匹配 + 模板 SQL。")

    tables = catalog.all_tables(workspace_id)
    if not tables:
        return {
            "answer": "当前工作空间还没有任何数据表。可以先去「数据接入」上传 CSV / Excel，或到「开发工作台」用 SQL 建表。",
            "sql": "",
            "columns": [],
            "rows": [],
            "chart": {"type": "empty"},
            "reasoning": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(reasoning)),
            "engine": "none",
            "row_count": 0,
            "elapsed_ms": 0,
            "metric": None,
            "dimension": "",
            "window": "",
            "period": win,
            "question": question,
        }

    q = (question or "").lower()
    scored: list[tuple[int, dict, list[dict]]] = []
    for t in tables:
        score, _ = _table_score(t, question)
        detail = catalog.table_detail(workspace_id, t["ref"]) or {}
        cols = detail.get("columns") or []
        matched = [c for c in cols if c["name"] and c["name"].lower() in q]
        score += 3 * len(matched)
        if score > 0:
            scored.append((score, t, matched))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        refs = "、".join(f"`{t['ref']}`" for t in tables[:5])
        return {
            "answer": (
                "这个问题没有匹配到语义层指标，也没在表名 / 列名里找到关键词。\n\n"
                f"当前工作空间可用的表：{refs}。\n\n"
                "换个说法试试，比如「上个月各渠道 GMV」「近 30 天订单量趋势」；"
                "或者到「统一语义层」把常用口径注册成指标，问数准确率会明显提升。"
            ),
            "sql": "",
            "columns": [],
            "rows": [],
            "chart": {"type": "empty"},
            "reasoning": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(reasoning)),
            "engine": "none",
            "row_count": 0,
            "elapsed_ms": 0,
            "metric": None,
            "dimension": "",
            "window": "",
            "period": win,
            "question": question,
        }

    score, table, matched = scored[0]
    detail = catalog.table_detail(workspace_id, table["ref"]) or {}
    cols = detail.get("columns") or []
    reasoning.append(
        f"表匹配得分最高的是 `{table['ref']}`（{score} 分，命中 {len(matched)} 个列名）。"
    )

    text_cols = [c for c in cols if "CHAR" in str(c.get("data_type", "")).upper() or "TEXT" in str(c.get("data_type", "")).upper()]
    date_col = next(
        (c for c in cols if re.search(r"DATE|TIME", str(c.get("data_type", "")).upper())),
        None,
    )
    # 问的是「有多少 / 多少条 / 总数」时直接给行数，不要硬拆维度
    count_only = bool(re.search(r"多少条|多少行|几条|记录数|行数|总数|总量|有多少", question))
    dim = None
    for c in matched:
        if c in text_cols:
            dim = c["name"]
            break
    if not dim and not count_only:
        for c in text_cols:
            if c["name"].lower() in ("channel_name", "category", "city", "brand", "status", "member_level"):
                dim = c["name"]
                break
    if not dim and text_cols and not count_only:
        dim = text_cols[0]["name"]

    where = ""
    if date_col and start:
        where = f" WHERE {date_col['name']} >= DATE '{start}'"
        if end:
            where += f" AND {date_col['name']} <= DATE '{end}'"

    if dim:
        sql = (
            f'SELECT {dim} AS "{dim}", count(*) AS "记录数" FROM {table["ref"]}'
            f"{where} GROUP BY 1 ORDER BY 2 DESC LIMIT {int(limit)}"
        )
        reasoning.append(f"选 `{dim}` 当分组维度（文本列），用 `count(*)` 作为度量做模板聚合。")
    else:
        sql = f'SELECT count(*) AS "记录数" FROM {table["ref"]}{where}'
        reasoning.append(
            "问句在问记录数量，且没有点名分组维度，因此只做行数统计，不硬拆维度。"
            if count_only
            else "该表没有合适的文本维度列，退化成行数统计。"
        )

    result = catalog.run_workspace_sql(workspace_id, sql, limit=limit)
    spec = charts.infer(result)
    window_text = _window_text(win, start, end)
    if date_col and start:
        reasoning.append(f"按时间列 `{date_col['name']}` 过滤到 {window_text}。")
    else:
        reasoning.append("没有可用的时间列或时间范围，统计全量数据。")

    subject = f"{table['name']}" + (f" · {dim}" if dim else "")
    answer = _narrate(result, spec, "", subject, window_text)
    answer += "\n\n> 降级路径：本次没有命中语义层指标，结果来自模板聚合，口径可能较粗。建议把常用问题注册成指标。"
    return {
        "answer": answer,
        "sql": result.get("sql") or sql,
        "columns": result.get("columns") or [],
        "rows": result.get("rows") or [],
        "chart": spec,
        "chart_data": _chart_payload(result, spec),
        "reasoning": "\n".join(f"{i + 1}. {s}" for i, s in enumerate(reasoning)),
        "engine": "fallback",
        "row_count": result.get("row_count", 0),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "metric": None,
        "dimension": dim or "",
        "window": window_text,
        "period": win,
        "question": question,
    }


# ------------------------------------------------------------------ 主动归因

def _group_metric(
    workspace_id: str, metric: dict, dim: str, start: str, end: str, limit: int = 500
) -> tuple[dict[str, float], dict]:
    built = semantic.build_metric_sql(metric, [dim], start, end, None, limit=limit, order="auto")
    result = catalog.run_workspace_sql(workspace_id, built["sql"], limit=limit)
    values: dict[str, float] = {}
    for r in result.get("rows") or []:
        key = r[0]
        val = r[1]
        if key is None:
            key = "未知"
        values[str(key)] = float(val) if _is_number(val) else 0.0
    return values, result


def attribute(
    workspace_id: str,
    metric_name: str,
    dimension: str | None = None,
    period: str = "last_month",
    top_n: int = 8,
) -> dict:
    """主动智能归因：按维度算本期 / 上期差值，按贡献度排序，给中文结论。"""
    metric = semantic.find_metric(workspace_id, metric_name)
    if not metric:
        raise ValueError(f"指标 {metric_name} 不存在")
    dims = metric.get("dimensions") or []
    dim = resolve_dimension_name(dimension, metric) if dimension else (dims[0] if dims else "")
    if not dim:
        raise ValueError("该指标没有可用维度，无法做归因")
    if dim == "__date__":
        raise ValueError("归因需要一个业务维度，不能是时间维度")
    if dim not in dims:
        raise ValueError(f"指标 {metric['name']} 不支持维度 {dim}，可用维度：{'、'.join(dims) or '无'}")

    win = resolve_period(period)
    unit = metric.get("unit") or ""
    label = metric.get("display_name") or metric["name"]

    cur, cur_result = _group_metric(workspace_id, metric, dim, win["start"], win["end"])
    prev, _ = _group_metric(workspace_id, metric, dim, win["prev_start"], win["prev_end"])

    total_current = sum(cur.values())
    total_previous = sum(prev.values())
    total_delta = total_current - total_previous
    growth = _growth(total_current, total_previous)

    contributions: list[dict] = []
    for value in sorted(set(cur) | set(prev)):
        c = cur.get(value, 0.0)
        p = prev.get(value, 0.0)
        delta = c - p
        share = (delta / total_delta) if total_delta else 0.0
        contributions.append(
            {
                "dim_value": value,
                "current": round(c, 4),
                "previous": round(p, 4),
                "delta": round(delta, 4),
                "share": round(share, 6),
                "growth": round(_growth(c, p), 6) if _growth(c, p) is not None else None,
            }
        )
    contributions.sort(key=lambda x: abs(x["delta"]), reverse=True)
    top = contributions[: max(1, int(top_n))]
    for i, item in enumerate(top):
        item["rank"] = i + 1

    dim_label = DIM_LABELS.get(dim, dim)
    driver = top[0]["dim_value"] if top else ""
    conclusion = _attribution_conclusion(
        label, dim_label, unit, win, total_current, total_previous, total_delta, growth, top
    )

    columns = [dim_label, "本期", "上期", "差值", "贡献度"]
    rows = [
        [c["dim_value"], c["current"], c["previous"], c["delta"], round(c["share"], 4)]
        for c in top
    ]
    # 贡献度是 0~1 的小数，必须标成 pct，否则前端按普通数字渲染成 0.71 而不是 70.6%
    formats = {"4": "pct"}
    chart = {
        "type": "bar",
        "x": 0,
        "y": 3,
        "x_label": dim_label,
        "y_label": f"{label}差值",
    }

    return {
        "metric": {"name": metric["name"], "display_name": label, "unit": unit},
        "dimension": dim,
        "dimension_label": dim_label,
        "period": win,
        "total_current": round(total_current, 4),
        "total_previous": round(total_previous, 4),
        "total_delta": round(total_delta, 4),
        "growth": round(growth, 6) if growth is not None else None,
        "driver": driver,
        "contributions": top,
        "all_contributions": contributions,
        "conclusion": conclusion,
        "table": {"columns": columns, "rows": rows, "formats": formats},
        "chart": chart,
        "chart_data": {"spec": chart, "columns": columns, "rows": rows, "formats": formats},
    }


def _attribution_conclusion(
    label: str,
    dim_label: str,
    unit: str,
    win: dict,
    total_current: float,
    total_previous: float,
    total_delta: float,
    growth: float | None,
    top: list[dict],
) -> str:
    if not top:
        return f"{win['label']}没有可归因的维度数据。"
    direction = "增长" if total_delta >= 0 else "下降"
    lines = [
        f"**{label}** 在 **{win['label']}** 为 {fmt_num(total_current, unit)}，"
        f"上期 {fmt_num(total_previous, unit)}，{direction} {fmt_num(abs(total_delta), unit)}"
        f"（{_growth_text(growth)}）。"
    ]
    head = top[0]
    if total_delta:
        pct = abs(head["share"]) * 100
        lines.append(
            f"主要驱动来自 **{head['dim_value']}**（{dim_label}）：本期 {fmt_num(head['current'], unit)}、"
            f"上期 {fmt_num(head['previous'], unit)}，差值 {fmt_num(head['delta'], unit)}，"
            f"贡献了本次变化 {pct:.1f}% 的{'增量' if head['delta'] >= 0 else '减量'}。"
        )
    else:
        lines.append(f"整体变化为 0，{head['dim_value']} 的波动最大，需要单独确认口径或数据延迟。")

    gains = [c for c in top if c["delta"] > 0][:2]
    losses = [c for c in top if c["delta"] < 0][:2]
    if gains:
        lines.append(
            "拉动项：" + "、".join(f"{c['dim_value']}（+{fmt_num(c['delta'], unit)}）" for c in gains) + "。"
        )
    if losses:
        lines.append(
            "拖累项：" + "、".join(f"{c['dim_value']}（{fmt_num(c['delta'], unit)}）" for c in losses) + "。"
        )
    if len(top) >= 3 and total_delta:
        top3 = sum(abs(c["delta"]) for c in top[:3])
        if abs(total_delta) and top3 / abs(total_delta) >= 1.2:
            lines.append(
                f"注意：Top3 的绝对波动（{fmt_num(top3, unit)}）明显大于整体净变化，"
                "说明维度之间存在此消彼长，不能只看总量。"
            )
    return "\n".join(lines)


# ------------------------------------------------------------------ AI 报告

def _resolve_metric_list(workspace_id: str, metrics: Any) -> list[dict]:
    all_metrics = semantic.list_metrics(workspace_id)
    if not all_metrics:
        return []
    names: list[str] = []
    if isinstance(metrics, str):
        names = [s.strip() for s in re.split(r"[,，、\s]+", metrics) if s.strip()]
    elif isinstance(metrics, list):
        names = [str(s).strip() for s in metrics if str(s).strip()]
    picked: list[dict] = []
    for name in names:
        found = semantic.find_metric(workspace_id, name)
        if found and found["id"] not in {m["id"] for m in picked}:
            picked.append(found)
    if picked:
        return picked
    # 默认取前几个有数值口径的指标
    return all_metrics[:4]


def generate_report(
    workspace_id: str,
    topic: str = "经营分析",
    metrics: Any = None,
    period: str = "last_month",
) -> dict:
    """AI 报告：概览 → 指标拆解 → 异常与归因 → 建议。纯模板 + 真实数据。"""
    picked = _resolve_metric_list(workspace_id, metrics)
    if not picked:
        raise ValueError("当前工作空间还没有指标，无法生成报告")
    win = resolve_period(period)
    topic = (topic or "经营分析").strip()

    # ---------------- 概览：每个指标的本期 / 上期 / 环比
    overview_rows: list[list] = []
    overview_lines: list[str] = []
    for m in picked:
        unit = m.get("unit") or ""
        label = m.get("display_name") or m["name"]
        built_c = semantic.build_metric_sql(m, [], win["start"], win["end"])
        built_p = semantic.build_metric_sql(m, [], win["prev_start"], win["prev_end"])
        res_c = catalog.run_workspace_sql(workspace_id, built_c["sql"], limit=10)
        res_p = catalog.run_workspace_sql(workspace_id, built_p["sql"], limit=10)
        cur = res_c["rows"][0][0] if res_c.get("rows") else None
        prev = res_p["rows"][0][0] if res_p.get("rows") else None
        rate = _growth(cur if _is_number(cur) else None, prev if _is_number(prev) else None)
        overview_rows.append([label, cur, prev, round(rate, 6) if rate is not None else None])
        overview_lines.append(
            f"- **{label}**：{fmt_num(cur, unit)}，上期 {fmt_num(prev, unit)}，环比 {_growth_text(rate)}。"
        )
    overview_body = (
        f"报告周期：**{win['label']}**（{win['start']} ~ {win['end']}），"
        f"对比期 {win['prev_start']} ~ {win['prev_end']}。\n\n" + "\n".join(overview_lines)
    )

    # ---------------- 指标拆解：主指标按两个维度展开
    main = picked[0]
    main_unit = main.get("unit") or ""
    main_label = main.get("display_name") or main["name"]
    dims = [d for d in (main.get("dimensions") or []) if d not in ("order_date", "__date__")]
    breakdown_lines: list[str] = []
    breakdown_chart: dict | None = None
    for dim in dims[:2]:
        values, result = _group_metric(workspace_id, main, dim, win["start"], win["end"])
        if not values:
            continue
        ordered = sorted(values.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(values.values())
        dim_label = DIM_LABELS.get(dim, dim)
        breakdown_lines.append(
            f"\n**按{dim_label}拆解 {main_label}**（合计 {fmt_num(total, main_unit)}）："
        )
        for name, value in ordered[:5]:
            share = value / total * 100 if total else 0
            breakdown_lines.append(f"- {name}：{fmt_num(value, main_unit)}，占比 {share:.1f}%")
        if breakdown_chart is None:
            breakdown_chart = {
                "spec": {"type": "bar", "x": 0, "y": 1, "x_label": dim_label, "y_label": main_label},
                "columns": [dim_label, main_label],
                "rows": [[n, v] for n, v in ordered[:10]],
            }
    if not breakdown_lines:
        breakdown_lines.append(f"{main_label} 没有可用的业务维度，本期只做总量呈现。")

    # ---------------- 异常与归因：主指标归因 + 波动标记
    attr_dim = dims[0] if dims else ""
    attr: dict | None = None
    anomaly_lines: list[str] = []
    anomaly_chart: dict | None = None
    if attr_dim:
        attr = attribute(workspace_id, main["name"], attr_dim, win["key"], top_n=6)
        anomaly_lines.append(attr["conclusion"])
        spikes = [c for c in attr["contributions"] if abs(c.get("growth") or 0) >= 0.3]
        if spikes:
            anomaly_lines.append("\n**波动较大的分组（|环比| ≥ 30%）**：")
            for c in spikes[:5]:
                anomaly_lines.append(
                    f"- {c['dim_value']}：{fmt_num(c['previous'], main_unit)} → "
                    f"{fmt_num(c['current'], main_unit)}（{_growth_text(c.get('growth'))}）"
                )
        else:
            anomaly_lines.append("\n各分组环比波动都在 ±30% 以内，暂无明显异常。")
        anomaly_chart = {
            "spec": attr["chart"],
            "columns": attr["table"]["columns"],
            "rows": attr["table"]["rows"],
            "formats": attr["table"].get("formats") or {},
        }
    else:
        anomaly_lines.append(f"{main_label} 没有可用于归因的业务维度，本节仅做整体环比说明。")
        if overview_rows:
            anomaly_lines.append(f"- 整体环比：{_growth_text(overview_rows[0][3])}。")

    # ---------------- 建议：从数据里长出来的模板建议
    suggestions: list[str] = []
    if overview_rows:
        rate = overview_rows[0][3]
        if rate is not None and rate < -0.1:
            suggestions.append(
                f"1. **优先止跌**：{main_label} 环比 {_growth_text(rate)}，先把拖累最大的分组拉出来复盘，"
                "确认是投放、供给还是数据延迟导致。"
            )
        elif rate is not None and rate > 0.1:
            suggestions.append(
                f"1. **放大增长**：{main_label} 环比 {_growth_text(rate)}，把拉动最大的分组经验复制到同层其他分组。"
            )
        else:
            suggestions.append(f"1. **保持观察**：{main_label} 环比 {_growth_text(rate)}，当前处于平稳区间。")
    if attr and attr["contributions"]:
        head = attr["contributions"][0]
        suggestions.append(
            f"2. **盯住主驱动**：{head['dim_value']} 贡献了 {abs(head['share']) * 100:.1f}% 的变化，"
            "建议为它单独建一张监控卡片和告警规则。"
        )
    for m in picked[1:4]:
        unit = m.get("unit") or ""
        label = m.get("display_name") or m["name"]
        built_c = semantic.build_metric_sql(m, [], win["start"], win["end"])
        res_c = catalog.run_workspace_sql(workspace_id, built_c["sql"], limit=5)
        value = res_c["rows"][0][0] if res_c.get("rows") else None
        suggestions.append(f"3. **联动看**：{label} 本期 {fmt_num(value, unit)}，与主指标一起判断是量的问题还是价的问题。")
        break
    suggestions.append(
        "4. **固化口径**：把本次用到的指标与维度组合登记为语义层指标 / 看板，下次问数直接复用，避免口径漂移。"
    )
    suggestions.append(
        "5. **持续运行**：为该报告设置定时推送（每天 09:00），让经营变化在早会前就位。"
    )

    sections = [
        {
            "key": "overview",
            "heading": "一、概览",
            "body": overview_body,
            "chart": {
                "spec": {"type": "bar", "x": 0, "y": 1, "x_label": "指标", "y_label": "本期"},
                "columns": ["指标", "本期", "上期", "环比"],
                "rows": overview_rows,
                "formats": {"3": "pct"},
            },
        },
        {
            "key": "breakdown",
            "heading": "二、指标拆解",
            "body": "\n".join(breakdown_lines),
            "chart": breakdown_chart,
        },
        {
            "key": "attribution",
            "heading": "三、异常与归因",
            "body": "\n".join(anomaly_lines),
            "chart": anomaly_chart,
        },
        {
            "key": "advice",
            "heading": "四、建议",
            "body": "\n".join(suggestions),
            "chart": None,
        },
    ]

    summary = (
        f"{win['label']}{main_label} {fmt_num(overview_rows[0][1] if overview_rows else None, main_unit)}，"
        f"环比 {_growth_text(overview_rows[0][3] if overview_rows else None)}；"
        + (
            f"主要驱动是 {attr['driver']}（{attr['dimension_label']}）。"
            if attr and attr.get("driver")
            else "本期未识别到显著驱动维度。"
        )
    )

    return {
        "title": f"{topic} · {win['label']}分析报告",
        "topic": topic,
        "period": win,
        "metrics": [
            {"name": m["name"], "display_name": m.get("display_name") or m["name"], "unit": m.get("unit") or ""}
            for m in picked
        ],
        "sections": sections,
        "summary": summary,
        "attribution": attr,
        "generated_at": store.now(),
    }


# ------------------------------------------------------------------ AI 生成看板

def _dashboard_name(prompt: str, metrics: list[dict], dims: list[str]) -> str:
    metric_labels = [m.get("display_name") or m["name"] for m in metrics]
    dim_labels = [DIM_LABELS.get(d, d) for d in dims]
    core = "".join(metric_labels) or "经营"
    prefix = "".join(dim_labels[:2])
    if prefix:
        return f"{prefix}{core}看板"
    base = re.sub(r"^(帮我|请|给我|做|生成|创建|新建|搭|建)+\s*", "", (prompt or "").strip())
    base = re.sub(r"(看板|看版|仪表盘|大屏|报表|dashboard)\s*$", "", base, flags=re.IGNORECASE).strip()
    return f"{base or core}看板"


def generate_dashboard(workspace_id: str, prompt: str, period: str | None = None, limit: int = 200) -> dict:
    """从一句话里抽指标 + 维度，自动配卡片布局，并预跑出真实数据。"""
    text = (prompt or "").strip()
    if not text:
        raise ValueError("请先描述想看的看板")

    metrics = semantic.list_metrics(workspace_id)
    if not metrics:
        raise ValueError("当前工作空间还没有指标，无法生成看板")

    picked: list[dict] = []
    q = text.lower()
    for m in metrics:
        keys = [m["name"].lower(), (m.get("display_name") or "").lower()]
        keys += METRIC_ALIASES.get(m["name"], [])
        if any(k and k in q for k in keys):
            picked.append(m)
    if not picked:
        picked = metrics[:2]

    dims: list[str] = []
    for m in picked:
        for d in _pick_dimensions(text, m):
            if d not in dims:
                dims.append(d)
    if not dims:
        dims = ["channel_name"] if any("channel_name" in (m.get("dimensions") or []) for m in picked) else []

    start, end, gran = semantic.parse_time_range(text)
    win: dict | None = None
    if not start:
        win = resolve_period(period or "last_month")
        start, end = win["start"], win["end"]
    window_text = _window_text(win, start, end)

    cards: list[dict] = []
    for m in picked[:3]:
        unit = m.get("unit") or ""
        label = m.get("display_name") or m["name"]
        # 1) 总计 KPI
        built = semantic.build_metric_sql(m, [], start, end, limit=10)
        result = catalog.run_workspace_sql(workspace_id, built["sql"], limit=10)
        cards.append(
            {
                "title": f"{label} · 总计",
                "metric": m["name"],
                "metric_label": label,
                "dim": "",
                "dim_label": "总计",
                "chart_type": "kpi",
                "sql": result.get("sql") or built["sql"],
                "columns": result.get("columns") or [],
                "rows": result.get("rows") or [],
                "chart": {"type": "kpi", "value_index": 0, "label": label},
                "unit": unit,
            }
        )
        # 2) 维度拆解
        for d in dims[:2]:
            if d not in (m.get("dimensions") or []):
                continue
            built = semantic.build_metric_sql(m, [d], start, end, limit=limit)
            result = catalog.run_workspace_sql(workspace_id, built["sql"], limit=limit)
            spec = charts.infer(result)
            if spec.get("type") in ("empty", "table"):
                spec = {"type": "bar", "x": 0, "y": 1, "x_label": DIM_LABELS.get(d, d), "y_label": label}
            cards.append(
                {
                    "title": f"{label} · 按{DIM_LABELS.get(d, d)}",
                    "metric": m["name"],
                    "metric_label": label,
                    "dim": d,
                    "dim_label": DIM_LABELS.get(d, d),
                    "chart_type": spec.get("type", "bar"),
                    "sql": result.get("sql") or built["sql"],
                    "columns": result.get("columns") or [],
                    "rows": result.get("rows") or [],
                    "chart": spec,
                    "unit": unit,
                }
            )
        # 3) 时间趋势（最多补一张）
        if len(cards) < 6 and any("order_date" in (m.get("dimensions") or []) for _ in [0]):
            built = semantic.build_metric_sql(
                m, ["__date__"], start, end, _granularity(text, gran) or "day", limit=limit, order="time"
            )
            result = catalog.run_workspace_sql(workspace_id, built["sql"], limit=limit)
            spec = charts.infer(result)
            cards.append(
                {
                    "title": f"{label} · 趋势",
                    "metric": m["name"],
                    "metric_label": label,
                    "dim": "__date__",
                    "dim_label": "日期",
                    "chart_type": "line",
                    "sql": result.get("sql") or built["sql"],
                    "columns": result.get("columns") or [],
                    "rows": result.get("rows") or [],
                    "chart": spec,
                    "unit": unit,
                }
            )

    name = _dashboard_name(text, picked, dims)
    return {
        "name": name,
        "description": f"由一句话生成：{text}",
        "prompt": text,
        "period": win,
        "window": window_text,
        "metrics": [{"name": m["name"], "display_name": m.get("display_name") or m["name"]} for m in picked],
        "dimensions": [{"name": d, "label": DIM_LABELS.get(d, d)} for d in dims],
        "cards": cards[:8],
        "layout": "grid-2",
        "reasoning": (
            f"从描述里识别到指标 {('、'.join(m.get('display_name') or m['name'] for m in picked)) or '无'}，"
            f"维度 {('、'.join(DIM_LABELS.get(d, d) for d in dims)) or '无'}；"
            f"时间范围按「{window_text}」取数。"
            "每个指标配一张总计 KPI + 一张维度拆解，主指标再加一张趋势，卡片数据已预跑。"
        ),
    }
