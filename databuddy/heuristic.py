"""无模型时的规则引擎：用语义层 + 模板把常见问题兜住。

它不是"假回答" —— 生成的 SQL 是真在本地湖仓里跑，结论由数据算出。
做不到的是开放式追问与复杂多跳，那种场景会明确提示去配置模型。
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import catalog, charts, governance, semantic, store
from .harness import knowledge

TABLE_HINT = re.compile(r"(demo\.[\w]+\.[\w]+|[\w]+_[\w]+)")


def _fmt(value: Any, unit: str = "") -> str:
    if isinstance(value, (int, float)):
        if unit == "%":
            return f"{value * 100:.2f}%"
        if abs(value) >= 10000:
            return f"{value / 10000:,.2f} 万{unit}"
        return f"{value:,.2f}{unit}"
    return f"{value}{unit}"


def _analyze_rows(title: str, result: dict, unit: str = "") -> str:
    cols = result["columns"]
    rows = result["rows"]
    if not rows:
        return f"{title}：未查到数据，可能是该时间段没有记录或筛选条件过严。"

    spec = charts.infer(result)
    if spec["type"] == "kpi":
        return f"{title} 为 **{_fmt(rows[0][spec['value_index']], unit)}**（共 {len(cols)} 个字段）。"

    y = spec.get("y", len(cols) - 1)
    x = spec.get("x", 0)
    values = [r[y] for r in rows if isinstance(r[y], (int, float))]
    if not values:
        return f"{title}：查询返回 {len(rows)} 行。"

    lines = []
    if spec["type"] == "line":
        total = sum(values)
        latest = values[-1]
        prev = values[-2] if len(values) > 1 else None
        lines.append(f"- 区间合计 **{_fmt(total, unit)}**，共 {len(rows)} 个时间点。")
        lines.append(
            f"- 最新一期 **{_fmt(latest, unit)}**"
            + (f"，较上一期 {'+' if latest >= prev else ''}{((latest - prev) / prev * 100):.1f}%。" if prev else "。")
        )
        peak = max(range(len(values)), key=lambda i: values[i])
        lines.append(f"- 峰值出现在 **{rows[peak][x]}**（{_fmt(values[peak], unit)}）。")
        change = (values[-1] - values[0]) / values[0] if values[0] else 0
        trend = "上行" if change > 0.05 else ("下行" if change < -0.05 else "基本平稳")
        lines.append(
            f"- 区间整体**{trend}**：由 {_fmt(values[0], unit)} 变为 {_fmt(values[-1], unit)}"
            f"（{'%+.1f%%' % (change * 100)}）。"
        )
    else:
        total = sum(values) or 1
        ordered = sorted(zip(values, [r[x] for r in rows]), reverse=True)
        top = ordered[0]
        lines.append(f"- 合计 **{_fmt(total, unit)}**，共 {len(rows)} 个分组。")
        lines.append(f"- 最高的是 **{top[1]}**（{_fmt(top[0], unit)}，占比 {top[0] / total * 100:.1f}%）。")
        if len(ordered) >= 3:
            top3 = sum(v for v, _ in ordered[:3])
            lines.append(f"- 前三名合计占比 **{top3 / total * 100:.1f}%**，集中度{'较高' if top3 / total > 0.7 else '一般'}。")
        bottom = ordered[-1]
        if len(ordered) > 1:
            gap = (top[0] / max(bottom[0], 1e-9) - 1) * 100
            lines.append(f"- 最低的是 {bottom[1]}（{_fmt(bottom[0], unit)}），比最高低 {gap:.1f}%。")
    return "\n".join(lines)


def _pick_metric(workspace_id: str, question: str) -> dict | None:
    metrics = semantic.list_metrics(workspace_id)
    q = question.lower()
    best, best_len = None, 0
    for m in metrics:
        for key in (m["name"], m.get("display_name") or ""):
            if key and key.lower() in q and len(key) > best_len:
                best, best_len = m, len(key)
    if best:
        return best
    aliases = {
        "gmv": ["gmv", "销售额", "成交额", "营收", "收入", "卖了多少钱"],
        "order_cnt": ["订单量", "订单数", "多少单", "单量"],
        "avg_order_value": ["客单价", "平均单价", "每单"],
        "customer_cnt": ["客户数", "用户数", "多少人买", "买家"],
        "refund_rate": ["退款率", "退货率"],
        "qty": ["销量", "件数", "卖了多少件"],
    }
    for name, words in aliases.items():
        if any(w in q for w in words):
            return semantic.find_metric(workspace_id, name)
    return None


def _pick_dimension(question: str, metric: dict) -> str | None:
    dims = metric.get("dimensions") or []
    q = question.lower()
    alias = {
        "channel_name": ["渠道", "平台", "天猫", "京东", "抖音", "线下"],
        "category": ["品类", "类目", "分类"],
        "city": ["城市", "地域", "地区", "大区"],
        "gender": ["性别", "男女"],
        "brand": ["品牌"],
        "member_level": ["会员", "等级"],
    }
    for dim, words in alias.items():
        if dim in dims and any(w in q for w in words):
            return dim
    return None


def respond(workspace_id: str, user: dict, question: str, mode: str) -> dict:
    started = time.perf_counter()
    trace: list[dict] = []
    artifacts: list[dict] = []
    q = (question or "").strip()
    lower = q.lower()

    def note(tool: str, args: dict, result: Any, ms: int = 0) -> None:
        trace.append({"tool": tool, "args": args, "ok": not (isinstance(result, dict) and result.get("error")), "duration_ms": ms,
                      "summary": str(result)[:300]})

    tables = catalog.all_tables(workspace_id)
    if not tables:
        return {
            "content": "当前工作空间还没有数据表。\n\n你可以：\n1. 到「数据接入」上传 CSV / Excel 文件，系统会自动建表；\n2. 或到「目录」新建 Catalog/Schema 后用 SQL 建表。",
            "artifacts": [],
            "trace": trace,
            "engine": "rule",
        }

    # 1) 表清单 / 表结构
    if re.search(r"有哪些表|表清单|哪些数据|有什么数据|数据资产", q):
        lines = [f"当前工作空间共 **{len(tables)} 张表**：", ""]
        for t in tables:
            lines.append(f"- `{t['ref']}`（{t['row_count']:,} 行）{('— ' + t['description']) if t.get('description') else ''}")
        note("list_tables", {}, {"count": len(tables)})
        artifacts.append(
            {
                "type": "table_list",
                "items": [{"ref": t["ref"], "rows": t["row_count"], "description": t.get("description")} for t in tables],
            }
        )
        return {"content": "\n".join(lines), "artifacts": artifacts, "trace": trace, "engine": "rule"}

    if re.search(r"表结构|有哪些列|schema", q, re.IGNORECASE) and not re.search(r"敏感|质量|脱敏|校验|治理", q):
        ref = None
        for t in tables:
            if t["name"] in q or t["ref"] in q:
                ref = t["ref"]
                break
        ref = ref or tables[0]["ref"]
        detail = catalog.table_detail(workspace_id, ref)
        note("describe_table", {"table": ref}, {"columns": len(detail["columns"])})
        lines = [f"`{ref}` 共 {len(detail['columns'])} 个字段、{detail['row_count']:,} 行。", "", "| 字段 | 类型 | 标签 |", "| --- | --- | --- |"]
        for c in detail["columns"]:
            tags = "、".join(f"{t['tag']}({t['level']})" for t in c.get("tags", [])) or "-"
            lines.append(f"| {c['name']} | {c['data_type']} | {tags} |")
        artifacts.append({"type": "table_schema", "table": ref, "columns": detail["columns"]})
        return {"content": "\n".join(lines), "artifacts": artifacts, "trace": trace, "engine": "rule"}

    # 2) 治理类
    if re.search(r"敏感|脱敏|隐私|pii|个人信息", lower):
        ref = next((t["ref"] for t in tables if t["name"] in q), "demo.retail.dim_customer")
        scan = governance.scan_table(workspace_id, ref)
        note("scan_sensitive", {"table": ref}, {"findings": len(scan["findings"])})
        lines = [f"`{scan['table']}` 扫描完成，发现 **{len(scan['findings'])} 个敏感字段**：", ""]
        lines.append("| 字段 | 分级 | 置信度 | 依据 | 脱敏样例 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in scan["findings"]:
            lines.append(f"| {f['column']} | {f['tag']} {f['level']} | {f['confidence']:.2f} | {f['evidence']} | {f['sample']} |")
        lines.append("")
        lines.append("建议：L3 及以上字段默认脱敏展示，导出需审批；可将结论写入字段标签固化为策略。")
        artifacts.append({"type": "sensitive_scan", **scan})
        return {"content": "\n".join(lines), "artifacts": artifacts, "trace": trace, "engine": "rule"}

    if re.search(r"质量|校验|空值|重复|异常值", lower):
        ref = next((t["ref"] for t in tables if t["name"] in q), "demo.retail.fact_orders")
        rules = store.rows(
            "SELECT * FROM quality_rules WHERE workspace_id = ? AND table_ref = ?", (workspace_id, ref)
        )
        results = []
        for rule in rules:
            res = governance.run_rule(workspace_id, rule)
            results.append({"table": ref, "column": rule["column_name"], "rule_type": rule["rule_type"], **res})
        note("run_quality_rules", {"table": ref}, {"count": len(results)})
        if not results:
            suggestions = governance.suggest_rules(workspace_id, ref)
            lines = [f"`{ref}` 还没有质量规则。以下是推荐的规则：", ""]
            for s in suggestions[:8]:
                lines.append(f"- {s['column_name']}：{s['rule_type']}（{s['reason']}）")
            artifacts.append({"type": "quality_suggestions", "table": ref, "items": suggestions})
        else:
            passed = sum(1 for r in results if r["status"] == "pass")
            lines = [f"`{ref}` 质量检查：**{passed}/{len(results)} 通过**。", "", "| 字段 | 规则 | 结果 | 详情 |", "| --- | --- | --- | --- |"]
            for r in results:
                flag = "通过" if r["status"] == "pass" else "失败"
                lines.append(f"| {r['column']} | {r['rule_type']} | {flag} | {r['detail']} |")
            artifacts.append({"type": "quality_results", "results": results})
        return {"content": "\n".join(lines), "artifacts": artifacts, "trace": trace, "engine": "rule"}

    # 3) 指标问数
    metric = _pick_metric(workspace_id, q)
    start, end, gran = semantic.parse_time_range(q)

    # 3.1) 口径/定义类问题优先讲清算法，而不是直接甩数
    if metric and re.search(r"口径|定义|怎么算|如何计算|含义|是什么意思|代表什么", q):
        dims = "、".join(metric.get("dimensions") or []) or "无"
        lines = [
            f"**{metric.get('display_name') or metric['name']}** 的口径定义：",
            "",
            f"- 计算方式：`{metric['expr']}`",
            f"- 数据来源：`{metric['table_ref']}`",
            f"- 单位：{metric.get('unit') or '无'}",
            f"- 可用维度：{dims}",
        ]
        if metric.get("description"):
            lines.insert(2, f"- 说明：{metric['description']}")
        docs = knowledge.search(workspace_id, metric.get("display_name") or metric["name"] + " 口径", 2)
        if docs:
            lines += ["", "知识库补充："]
            lines += [f"- 《{d['title']}》：{d['snippet']}" for d in docs]
        note("list_metrics", {"metric": metric["name"]}, {"found": True})
        return {"content": "\n".join(lines), "artifacts": artifacts, "trace": trace, "engine": "rule"}


    if metric:
        dim = _pick_dimension(q, metric)
        want_trend = bool(re.search(r"趋势|走势|变化|每天|每月|逐日|按天|按周|按月", q))
        dims: list[str] = []
        if want_trend:
            dims = ["__date__"]
            gran = gran or ("month" if re.search(r"按月|每月", q) else "day")
        elif dim:
            dims = [dim]
        built = semantic.build_metric_sql(metric, dims, start, end, gran if want_trend else None, order="time" if want_trend else "auto")
        res = catalog.run_workspace_sql(workspace_id, built["sql"], limit=400)
        note("query_metric", {"metric": metric["name"], "dims": dims}, {"rows": res["row_count"]}, res["elapsed_ms"])
        label = metric.get("display_name") or metric["name"]
        period = f"{start} ~ {end}" if start else "全部时间"
        analysis = _analyze_rows(label, res, metric.get("unit") or "")
        content = [
            f"**{label}**（{period}）",
            "",
            analysis,
            "",
            f"> 口径：`{metric['expr']}`，取自 `{metric['table_ref']}`"
            + (f"，按 {dim} 拆分" if dims and dims[0] != "__date__" else ""),
        ]
        if not dims and not want_trend:
            content.append("")
            content.append("如需按渠道/品类/城市拆分，或看时间趋势，直接说明维度即可。")
        artifacts.append(
            {
                "type": "query",
                "title": label,
                "sql": res["sql"],
                "columns": res["columns"],
                "rows": res["rows"][:50],
                "chart": charts.infer(res),
            }
        )
        return {"content": "\n".join(content), "artifacts": artifacts, "trace": trace, "engine": "rule"}

    # 4) 口径/概念问答 → 知识库
    docs = knowledge.search(workspace_id, q, top_k=3)
    if docs:
        note("search_knowledge", {"query": q}, {"hits": len(docs)})
        lines = ["根据知识库，", ""]
        for d in docs:
            lines.append(f"**{d['title']}**")
            lines.append(d["snippet"])
            lines.append("")
        artifacts.append({"type": "knowledge", "items": [{"title": d["title"], "snippet": d["snippet"]} for d in docs]})
        return {"content": "\n".join(lines), "artifacts": artifacts, "trace": trace, "engine": "rule"}

    # 5) 兜底
    metric_names = "、".join(f"{m.get('display_name') or m['name']}" for m in semantic.list_metrics(workspace_id))
    content = (
        "这条问题需要更强的语义理解，当前未配置大模型，只能用规则引擎回答。\n\n"
        "现在可以做这些事：\n"
        f"- **问数**：例如「近 7 天 GMV 趋势」「各渠道 GMV 排行」（已注册指标：{metric_names}）\n"
        "- **看资产**：「有哪些表」「fact_orders 的字段结构」\n"
        "- **做治理**：「扫描敏感字段」「fact_orders 质量检查」\n\n"
        "想让它处理开放式问题，到「设置 → 模型」填入 DeepSeek API Key 即可（`deepseek-chat`，OpenAI 兼容协议）。"
    )
    return {"content": content, "artifacts": [], "trace": trace, "engine": "rule"}
