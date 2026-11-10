"""漂移检测：KS / PSI / Wasserstein / 卡方 / JS 散度 + 零配置异常检测。

设计要点
--------
* 5 个指标全部纯 Python 实现，不依赖 scipy —— 这些公式本身只有十几行，
  引入一个几十 MB 的科学计算栈只为算一个 CDF 差，性价比太低。
* 数值列默认 KS（对位置和形状都敏感），类别列默认卡方（对类别占比变化敏感），
  用户可显式指定任意指标，但指标与列类型不匹配时会自动降级并说明原因。
* 阈值按指标给默认值（PSI 的经典经验值是 0.1/0.25，KS 取 0.1/0.2），
  阈值可覆盖，因为不同业务对「漂移」的容忍度差异很大。
* detect_anomalies 是零配置的：没有基线就自动现采一条，再与当前快照对比；
  行数突增突减、schema 增删列、分布漂移、新鲜度全部自己判断，不要求用户配规则。
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Sequence

from . import catalog, profiling, quality, store

METRICS = ("ks", "psi", "wasserstein", "chi2", "js")

METRIC_LABELS = {
    "ks": "KS 统计量",
    "psi": "PSI 群体稳定性",
    "wasserstein": "Wasserstein 距离",
    "chi2": "卡方统计量",
    "js": "JS 散度",
}

METRIC_DESC = {
    "ks": "两条经验 CDF 的最大差值，0~1，越大差异越显著",
    "psi": "分桶后 sum((a-b)·ln(a/b))，<0.1 稳定，>0.25 显著漂移",
    "wasserstein": "按分位数近似的一阶推土机距离，与量纲同单位",
    "chi2": "类别分布卡方统计量，越大越说明占比结构变化",
    "js": "对称化 KL 散度，底数 2，范围 0~1，1 表示完全不重叠",
}

# 默认阈值：低于 warn 视为稳定，warn~crit 为告警，超过 crit 为严重
DEFAULT_THRESHOLDS = {
    "ks": {"warn": 0.10, "crit": 0.20},
    "psi": {"warn": 0.10, "crit": 0.25},
    "wasserstein": {"warn": 0.10, "crit": 0.25},  # 归一化后使用
    "chi2": {"warn": 0.10, "crit": 0.20},         # 归一化为 Cramér's V 后使用
    "js": {"warn": 0.10, "crit": 0.20},
}

LEVELS = ("stable", "warning", "critical")
LEVEL_LABELS = {"stable": "稳定", "warning": "告警", "critical": "严重"}

KIND_DEFAULT_METRIC = {
    "numeric": "ks",
    "date": "ks",
    "string": "chi2",
    "boolean": "chi2",
    "other": "chi2",
}

EPS = 1e-9
_EPS_BIN = 1e-4
PSI_BINS = 10


# --------------------------------------------------------------------- 小工具


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        try:
            out = float(value)
        except (TypeError, ValueError):
            return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _clean(values: Sequence[Any]) -> list[float]:
    out: list[float] = []
    for v in values:
        f = _num(v)
        if f is not None:
            out.append(f)
    return out


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _round(value: Any, digits: int = 6) -> float | None:
    out = _num(value)
    return None if out is None else round(out, digits)


# --------------------------------------------------------------------- 核心指标


def ks_statistic(baseline: Sequence[Any], current: Sequence[Any]) -> float:
    """KS 统计量：两条经验 CDF 的最大绝对差（0~1）。"""
    a = sorted(_clean(baseline))
    b = sorted(_clean(current))
    if not a or not b:
        return 0.0
    values = sorted(set(a) | set(b))
    i = j = 0
    na, nb = len(a), len(b)
    best = 0.0
    for v in values:
        while i < na and a[i] <= v:
            i += 1
        while j < nb and b[j] <= v:
            j += 1
        diff = abs(i / na - j / nb)
        if diff > best:
            best = diff
    return min(1.0, max(0.0, best))


def _edges_from(baseline: Sequence[float], bins: int = PSI_BINS) -> list[float] | None:
    values = sorted(_clean(baseline))
    if len(values) < 2:
        return None
    n = len(values)
    edges = [values[0] - EPS]
    for k in range(1, bins):
        # 等频切点，避免长尾把桶挤成 0
        pos = (n - 1) * k / bins
        lo_idx = int(math.floor(pos))
        hi_idx = min(lo_idx + 1, n - 1)
        frac = pos - lo_idx
        edges.append(values[lo_idx] + (values[hi_idx] - values[lo_idx]) * frac)
    edges.append(values[-1] + EPS)
    # 去重并保证严格递增
    out: list[float] = []
    for edge in edges:
        if not out or edge > out[-1]:
            out.append(edge)
    return out if len(out) >= 3 else None


def _bucket_counts(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    counts = [0.0] * (len(edges) - 1)
    for v in values:
        placed = False
        for idx in range(len(edges) - 1):
            if edges[idx] <= v < edges[idx + 1]:
                counts[idx] += 1.0
                placed = True
                break
        if not placed:
            if v <= edges[0]:
                counts[0] += 1.0
            else:
                counts[-1] += 1.0
    return counts


def _normalize(counts: Sequence[float]) -> list[float]:
    total = sum(counts)
    if total <= 0:
        return [1.0 / len(counts)] * len(counts) if counts else []
    return [c / total for c in counts]


def _smoothed(counts: Sequence[float], bins: int) -> list[float]:
    """空桶补一个极小数，避免 ln(0) 与除零把 PSI 打成 inf。"""
    out = [max(float(c), _EPS_BIN) for c in counts]
    if len(out) < bins:
        out.extend([_EPS_BIN] * (bins - len(out)))
    return _normalize(out)


def psi(baseline: Sequence[Any], current: Sequence[Any], bins: int = PSI_BINS) -> float:
    """PSI = sum((a_i - b_i) · ln(a_i / b_i))，分桶用基线的等频切点。"""
    a = _clean(baseline)
    b = _clean(current)
    if not a or not b:
        return 0.0
    edges = _edges_from(a, bins)
    if edges is None:
        # 基线值太少，退化为「等宽」单桶，用值本身构造两条边缘
        lo = min(min(a), min(b))
        hi = max(max(a), max(b))
        if hi <= lo:
            return 0.0
        edges = [lo - EPS + (hi - lo + 2 * EPS) * i / bins for i in range(bins + 1)]
    base_counts = _bucket_counts(a, edges)
    cur_counts = _bucket_counts(b, edges)
    pa = _smoothed(base_counts, bins)
    pb = _smoothed(cur_counts, bins)
    total = 0.0
    for x, y in zip(pa, pb):
        total += (x - y) * math.log(x / y)
    return max(0.0, total)


def wasserstein(baseline: Sequence[Any], current: Sequence[Any]) -> float:
    """一阶 Wasserstein 距离：用分位数近似，再按基线量纲归一化到 0~1。

    原始距离与量纲同单位（金额、天数），直接比阈值没有意义，所以同时给出
    ``raw``（真实距离）与归一化值（除以基线值域跨度）。调用方拿到的 score
    统一是归一化后的，便于跨列比较。
    """
    a = sorted(_clean(baseline))
    b = sorted(_clean(current))
    if not a or not b:
        return 0.0
    steps = max(len(a), len(b), 2)
    total = 0.0
    for i in range(steps):
        pos = (len(a) - 1) * i / (steps - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(a) - 1)
        frac = pos - lo
        qa = a[lo] + (a[hi] - a[lo]) * frac

        pos = (len(b) - 1) * i / (steps - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(b) - 1)
        frac = pos - lo
        qb = b[lo] + (b[hi] - b[lo]) * frac
        total += abs(qa - qb)
    distance = total / steps
    return distance


def wasserstein_normalized(baseline: Sequence[Any], current: Sequence[Any]) -> tuple[float, float]:
    """返回 (归一化距离, 原始距离)。"""
    a = _clean(baseline)
    if not a:
        return 0.0, 0.0
    distance = wasserstein(a, current)
    lo, hi = min(a), max(a)
    span = hi - lo
    if span <= 0:
        scale = max(abs(hi), 1.0)
    else:
        scale = span
    return min(1.0, max(0.0, distance / scale)), distance


def _count_map(values: Sequence[Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for v in values:
        if v is None:
            continue
        key = str(v)
        out[key] = out.get(key, 0.0) + 1.0
    return out


def chi_square(baseline: Sequence[Any], current: Sequence[Any]) -> float:
    """类别分布卡方统计量（原始值，未归一化）。

    缺失的类别按 0 计数，但不做空桶平滑 —— 卡方对「新出现的类别」本来就该敏感。
    分母为 0 时该类别跳过（基线没有、当前也没有，不会进 map）。
    """
    a = _count_map(baseline)
    b = _count_map(current)
    if not a or not b:
        return 0.0
    total_a = sum(a.values())
    total_b = sum(b.values())
    if total_a <= 0 or total_b <= 0:
        return 0.0
    keys = set(a) | set(b)
    stat = 0.0
    for key in keys:
        ea = a.get(key, 0.0) / total_a
        eb = b.get(key, 0.0) / total_b
        expected = (ea + eb) / 2.0
        if expected <= 0:
            continue
        stat += ((ea - expected) ** 2) / expected
    return max(0.0, stat)


def chi_square_normalized(
    baseline: Sequence[Any], current: Sequence[Any]
) -> tuple[float, float]:
    """把卡方归一化成 Cramér's V（0~1），便于统一阈值与跨列比较。"""
    stat = chi_square(baseline, current)
    a = _count_map(baseline)
    b = _count_map(current)
    n = min(sum(a.values()), sum(b.values()))
    k = len(set(a) | set(b))
    if n <= 0 or k <= 1:
        return 0.0, stat
    v = math.sqrt(stat / (n * (k - 1)))
    return min(1.0, max(0.0, v)), stat


def js_divergence(baseline: Sequence[Any], current: Sequence[Any]) -> float:
    """JS 散度（底数 2，范围 0~1）：数值列按直方图，类别列按占比。"""
    a_num = _clean(baseline)
    b_num = _clean(current)
    numeric = bool(a_num) and bool(b_num) and len(a_num) >= 2 and len(b_num) >= 2
    if numeric:
        edges = _edges_from(a_num, PSI_BINS)
        if edges is None:
            return 0.0
        pa = _normalize(_bucket_counts(a_num, edges))
        pb = _normalize(_bucket_counts(b_num, edges))
    else:
        ca = _count_map(baseline)
        cb = _count_map(current)
        keys = sorted(set(ca) | set(cb))
        if not keys:
            return 0.0
        total_a = sum(ca.values()) or 1.0
        total_b = sum(cb.values()) or 1.0
        pa = [ca.get(k, 0.0) / total_a for k in keys]
        pb = [cb.get(k, 0.0) / total_b for k in keys]
    if not pa or not pb or len(pa) != len(pb):
        return 0.0
    total = 0.0
    for x, y in zip(pa, pb):
        m = (x + y) / 2.0
        if x > 0 and m > 0:
            total += 0.5 * x * math.log2(x / m)
        if y > 0 and m > 0:
            total += 0.5 * y * math.log2(y / m)
    return min(1.0, max(0.0, total))


def compute_metric(metric: str, baseline: Sequence[Any], current: Sequence[Any]) -> dict:
    """统一入口：返回 {metric, score, raw, normalized}。score 一律 0~1 可比。"""
    metric = str(metric or "ks").lower()
    if metric not in METRICS:
        raise ValueError(f"不支持的漂移指标：{metric}（可选：{'/'.join(METRICS)}）")
    if metric == "ks":
        score = ks_statistic(baseline, current)
        return {"metric": "ks", "score": score, "raw": score, "normalized": True}
    if metric == "psi":
        score = psi(baseline, current)
        return {"metric": "psi", "score": score, "raw": score, "normalized": True}
    if metric == "wasserstein":
        score, raw = wasserstein_normalized(baseline, current)
        return {"metric": "wasserstein", "score": score, "raw": raw, "normalized": True}
    if metric == "chi2":
        score, raw = chi_square_normalized(baseline, current)
        return {"metric": "chi2", "score": score, "raw": raw, "normalized": True}
    score = js_divergence(baseline, current)
    return {"metric": "js", "score": score, "raw": score, "normalized": True}


def _level(score: float, metric: str, thresholds: dict | None = None) -> tuple[str, float, float]:
    limits = dict(DEFAULT_THRESHOLDS.get(metric) or DEFAULT_THRESHOLDS["ks"])
    if thresholds:
        for key in ("warn", "crit"):
            if thresholds.get(key) is not None:
                limits[key] = float(thresholds[key])
    warn = float(limits["warn"])
    crit = float(limits["crit"])
    if score >= crit:
        return "critical", warn, crit
    if score >= warn:
        return "warning", warn, crit
    return "stable", warn, crit


# --------------------------------------------------------------------- 取数


def _resolve(ws: str, table_ref: str) -> dict:
    found = catalog.resolve(ws, table_ref)
    if not found:
        raise ValueError(f"表 {table_ref} 不存在")
    return found


def fetch_column(
    ws: str, table_ref: str, column: str, limit: int = 20000
) -> list[Any]:
    """取一列样本值（不含 NULL）。上限兜底，避免把整表拉进内存。"""
    ref = table_ref
    sql = f"SELECT {_q(column)} FROM {ref} WHERE {_q(column)} IS NOT NULL LIMIT {int(limit)}"
    rows = catalog.run_workspace_sql(ws, sql, limit=limit)["rows"]
    return [r[0] for r in rows if r]


def _column_kind(ws: str, found: dict, column: str) -> str:
    for col in profiling._describe(found):  # noqa: SLF001 —— 同包内复用真实类型探测
        if col["name"] == column:
            return profiling.kind_of(col.get("data_type"))
    return "other"


def _workspace_of(table_ref: str) -> str:
    hit = store.row(
        "SELECT workspace_id FROM profile_baselines WHERE table_ref = ? LIMIT 1", (table_ref,)
    )
    if hit and hit.get("workspace_id"):
        return hit["workspace_id"]
    return profiling._workspace_of(table_ref)  # noqa: SLF001


def _baseline_values(
    ws: str,
    ref: str,
    column: str,
    stats: dict,
    limit: int = 20000,
) -> list[Any]:
    """优先从基线的直方图/类别分布重建分布；没有则现采。"""
    cols = (stats or {}).get("columns") or {}
    entry = cols.get(column) if isinstance(cols, dict) else None
    if entry:
        hist = entry.get("histogram")
        if hist and hist.get("counts"):
            lo, hi = _num(hist.get("min")), _num(hist.get("max"))
            counts = hist.get("counts") or []
            if lo is not None and hi is not None and hi > lo and counts:
                width = (hi - lo) / len(counts)
                out: list[float] = []
                for idx, count in enumerate(counts):
                    c = int(_num(count) or 0)
                    if c <= 0:
                        continue
                    center = lo + width * (idx + 0.5)
                    out.extend([center] * min(c, limit))
                    if len(out) >= limit:
                        break
                if out:
                    return out[:limit]
        tops = entry.get("top_values")
        if tops:
            out = []
            for item in tops:
                count = int(_num(item.get("count")) or 0)
                value = item.get("value")
                out.extend([value] * min(count, limit))
                if len(out) >= limit:
                    break
            if out:
                return out[:limit]
    return fetch_column(ws, ref, column, limit)


# --------------------------------------------------------------------- 单列漂移


def detect_drift(
    table_ref: str,
    column: str,
    baseline_id: str | None = None,
    metric: str | None = None,
    workspace_id: str | None = None,
    thresholds: dict | None = None,
    limit: int = 20000,
) -> dict:
    """检测单列漂移。

    返回 {table_ref, column, metric, score, raw, threshold, thresholds, drifted, level, ...}
    """
    profiling.ensure_tables()
    ws = workspace_id or _workspace_of(table_ref)
    found = _resolve(ws, table_ref)
    ref = found["ref"]

    baseline = profiling.get_baseline(ref, column_name=column, baseline_id=baseline_id, workspace_id=ws)
    if not baseline:
        baseline = profiling.get_baseline(ref, baseline_id=baseline_id, workspace_id=ws)
    if not baseline:
        raise ValueError(
            f"表 {ref} 还没有剖析基线，请先调用 POST /api/profiling/profile 或 /api/profiling/baselines 建立基线"
        )

    kind = _column_kind(ws, found, column)
    chosen = str(metric or KIND_DEFAULT_METRIC.get(kind, "ks")).lower()
    auto_metric = metric is None
    notes: list[str] = []
    if chosen not in METRICS:
        notes.append(f"未知指标 {chosen}，已回退到 {KIND_DEFAULT_METRIC.get(kind, 'ks')}")
        chosen = KIND_DEFAULT_METRIC.get(kind, "ks")

    baseline_values = _baseline_values(ws, ref, column, baseline.get("stats") or {}, limit)
    current_values = fetch_column(ws, ref, column, limit)
    if not baseline_values:
        raise ValueError(f"基线中缺少列 {column} 的可用统计，请重新采集基线")
    if not current_values:
        return {
            "table_ref": ref,
            "column": column,
            "kind": kind,
            "metric": chosen,
            "score": 0.0,
            "raw": 0.0,
            "threshold": None,
            "thresholds": DEFAULT_THRESHOLDS.get(chosen),
            "drifted": False,
            "level": "stable",
            "baseline_id": baseline["id"],
            "baseline_captured_at": baseline.get("captured_at"),
            "notes": notes + ["当前列无数据，跳过漂移判定"],
            "auto_metric": auto_metric,
        }

    out = compute_metric(chosen, baseline_values, current_values)
    level, warn, crit = _level(out["score"], chosen, thresholds)
    return {
        "table_ref": ref,
        "column": column,
        "kind": kind,
        "metric": chosen,
        "metric_label": METRIC_LABELS.get(chosen, chosen),
        "score": _round(out["score"]),
        "raw": _round(out["raw"]),
        "threshold": warn,
        "thresholds": {"warn": warn, "crit": crit},
        "drifted": level != "stable",
        "level": level,
        "level_label": LEVEL_LABELS.get(level, level),
        "baseline_id": baseline["id"],
        "baseline_captured_at": baseline.get("captured_at"),
        "baseline_rows": len(baseline_values),
        "current_rows": len(current_values),
        "auto_metric": auto_metric,
        "notes": notes,
    }


def detect_drift_batch(
    table_ref: str,
    columns: list[str] | None = None,
    workspace_id: str | None = None,
    limit: int = 20000,
) -> dict:
    """批量检测一张表的所有列（或指定列）。"""
    ws = workspace_id or _workspace_of(table_ref)
    found = _resolve(ws, table_ref)
    ref = found["ref"]
    baseline = profiling.get_baseline(ref, workspace_id=ws)
    if not baseline:
        raise ValueError(f"表 {ref} 还没有剖析基线，请先建立基线")
    stats_cols = (baseline.get("stats") or {}).get("columns") or {}
    targets = columns or sorted(stats_cols.keys())
    if not targets:
        raise ValueError("基线中没有列信息，请重新采集")
    results = []
    for column in targets:
        try:
            results.append(detect_drift(ref, column, workspace_id=ws, limit=limit))
        except Exception as exc:  # noqa: BLE001 —— 单列失败不影响其它列
            results.append(
                {
                    "table_ref": ref,
                    "column": column,
                    "metric": None,
                    "score": None,
                    "drifted": False,
                    "level": "unknown",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    drifted = [r for r in results if r.get("drifted")]
    return {
        "table_ref": ref,
        "baseline_id": baseline["id"],
        "total": len(results),
        "drifted_count": len(drifted),
        "critical_count": sum(1 for r in results if r.get("level") == "critical"),
        "columns": results,
    }


# --------------------------------------------------------------------- 零配置异常检测


def _ensure_baseline(ws: str, ref: str) -> tuple[dict, bool]:
    """没有基线就先采一条。返回 (baseline, created)。"""
    baseline = profiling.get_baseline(ref, workspace_id=ws)
    if baseline:
        return baseline, False
    profile = profiling.profile_table(ref, "snapshot", workspace_id=ws)
    saved = profiling.save_baseline(ref, "", "snapshot", profile, workspace_id=ws)
    return profiling.get_baseline(ref, baseline_id=saved["id"], workspace_id=ws), True


def _freshness_anomaly(ws: str, found: dict, ref: str) -> list[dict]:
    """新鲜度：最大时间列距今天数超过阈值就报。"""
    out: list[dict] = []
    columns = profiling._describe(found)  # noqa: SLF001
    time_column = profiling.detect_time_column(columns)
    if not time_column:
        return out
    row = catalog.run_workspace_sql(
        ws,
        f"SELECT max({_q(time_column)}), "
        f"date_diff('day', max({_q(time_column)}), current_date) FROM {ref}",
        limit=1,
    )["rows"]
    if not row or not row[0]:
        return out
    latest, lag = row[0][0], row[0][1]
    lag_days = int(_num(lag) or 0)
    if lag_days <= 7:
        return out
    severity = "critical" if lag_days > 30 else "warning"
    out.append(
        {
            "column": time_column,
            "kind": "freshness",
            "severity": severity,
            "detail": f"时间列 {time_column} 最新值 {latest}，已滞后 {lag_days} 天（阈值 7 天）",
        }
    )
    return out


def detect_anomalies(
    table_ref: str,
    workspace_id: str | None = None,
    volume_threshold: float = 0.3,
    freshness_days: int = 7,
    limit: int = 20000,
    persist: bool = True,
) -> dict:
    """零配置异常检测：schema 漂移 / 行数突增突减 / 分布漂移 / 新鲜度。

    没有基线会自动现采一条（第一次运行相当于「建立参照」），
    后续运行才有对比意义 —— 这一点会在返回的 ``baseline_created`` 里说明。
    """
    profiling.ensure_tables()
    ws = workspace_id or _workspace_of(table_ref)
    found = _resolve(ws, table_ref)
    ref = found["ref"]
    baseline, created = _ensure_baseline(ws, ref)
    baseline_stats = baseline.get("stats") or {}
    current_profile = profiling.profile_table(ref, "snapshot", workspace_id=ws)

    findings: list[dict] = []

    # 1) schema 漂移：增列 / 删列 / 类型变化
    old_cols = baseline_stats.get("columns") or {}
    new_cols = {c["name"]: c for c in current_profile.get("columns") or []}
    old_names, new_names = set(old_cols), set(new_cols)
    for name in sorted(new_names - old_names):
        findings.append(
            {
                "column": name,
                "kind": "schema",
                "severity": "warning",
                "detail": f"新增列 {name}（{new_cols[name].get('data_type')}）",
            }
        )
    for name in sorted(old_names - new_names):
        findings.append(
            {
                "column": name,
                "kind": "schema",
                "severity": "critical",
                "detail": f"列 {name} 已消失，下游引用可能断裂",
            }
        )
    for name in sorted(old_names & new_names):
        old_type = str((old_cols.get(name) or {}).get("data_type") or "").upper()
        new_type = str((new_cols.get(name) or {}).get("data_type") or "").upper()
        if old_type and new_type and old_type != new_type:
            findings.append(
                {
                    "column": name,
                    "kind": "schema",
                    "severity": "critical",
                    "detail": f"列 {name} 类型变更 {old_type} → {new_type}",
                }
            )

    # 2) 行数突增突减
    base_rows = int(_num(baseline_stats.get("row_count")) or 0)
    cur_rows = int(_num(current_profile.get("row_count")) or 0)
    if base_rows > 0:
        delta = (cur_rows - base_rows) / base_rows
        if abs(delta) >= volume_threshold:
            direction = "突增" if delta > 0 else "突减"
            severity = "critical" if abs(delta) >= 0.5 else "warning"
            findings.append(
                {
                    "column": "",
                    "kind": "volume",
                    "severity": severity,
                    "detail": (
                        f"行数{direction} {abs(delta):.1%}：基线 {base_rows} → 当前 {cur_rows}"
                        f"（阈值 ±{volume_threshold:.0%}）"
                    ),
                }
            )

    # 3) 分布漂移：数值列 KS，类别列卡方
    for name, col in new_cols.items():
        kind = col.get("kind")
        if kind not in ("numeric", "string", "boolean"):
            continue
        metric = KIND_DEFAULT_METRIC.get(kind, "ks")
        try:
            out = detect_drift(
                ref, name, workspace_id=ws, metric=metric, baseline_id=baseline["id"], limit=limit
            )
        except Exception:  # noqa: BLE001
            continue
        if out.get("drifted"):
            findings.append(
                {
                    "column": name,
                    "kind": "distribution",
                    "severity": out["level"] if out["level"] == "critical" else "warning",
                    "detail": (
                        f"{METRIC_LABELS.get(out['metric'], out['metric'])} = {out['score']}"
                        f"（阈值 {out['threshold']}），列 {name} 分布{'严重' if out['level'] == 'critical' else ''}漂移"
                    ),
                }
            )

    # 4) 新鲜度
    for item in _freshness_anomaly(ws, found, ref):
        if item["detail"].endswith("（阈值 7 天）") and freshness_days != 7:
            item["detail"] = item["detail"].replace("（阈值 7 天）", f"（阈值 {freshness_days} 天）")
        findings.append(item)

    # 落库
    persisted = 0
    if persist:
        for item in findings:
            fid = quality.record_anomaly(
                ws,
                ref,
                item.get("column") or "",
                item["kind"],
                item["severity"],
                item["detail"],
            )
            if fid:
                persisted += 1
        if findings:
            store.audit(
                ws,
                None,
                "anomaly_detect",
                ref,
                {"findings": len(findings), "persisted": persisted},
            )

    return {
        "table_ref": ref,
        "baseline_id": baseline["id"],
        "baseline_captured_at": baseline.get("captured_at"),
        "baseline_created": created,
        "baseline_row_count": base_rows,
        "current_row_count": cur_rows,
        "findings": findings,
        "finding_count": len(findings),
        "persisted": persisted,
        "by_kind": _count(findings, "kind"),
        "by_severity": _count(findings, "severity"),
        "checked_at": store.now(),
    }


def _count(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for item in items:
        k = str(item.get(key) or "unknown")
        out[k] = out.get(k, 0) + 1
    return out


# --------------------------------------------------------------------- 异常列表


def list_anomalies(
    workspace_id: str,
    table_ref: str = "",
    acknowledged: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    quality.ensure_tables()
    sql = "SELECT * FROM anomaly_findings WHERE workspace_id = ?"
    params: list[Any] = [workspace_id]
    if table_ref:
        sql += " AND table_ref = ?"
        params.append(table_ref)
    if acknowledged is not None:
        sql += " AND acknowledged = ?"
        params.append(1 if acknowledged else 0)
    sql += " ORDER BY detected_at DESC LIMIT ?"
    params.append(limit)
    rows = store.rows(sql, params)
    for row in rows:
        row["severity_label"] = {"info": "提示", "warning": "告警", "critical": "严重"}.get(
            row.get("severity"), row.get("severity")
        )
        row["kind_label"] = {
            "schema": "结构漂移",
            "volume": "行数异常",
            "distribution": "分布漂移",
            "freshness": "新鲜度",
            "rule": "规则失败",
        }.get(row.get("kind"), row.get("kind"))
        row["acknowledged"] = bool(row.get("acknowledged"))
    return rows


def acknowledge(anomaly_id: str, workspace_id: str, user_id: str | None = None) -> bool:
    quality.ensure_tables()
    row = store.row(
        "SELECT id FROM anomaly_findings WHERE id = ? AND workspace_id = ?", (anomaly_id, workspace_id)
    )
    if not row:
        return False
    store.update("anomaly_findings", anomaly_id, {"acknowledged": 1})
    store.audit(workspace_id, user_id, "anomaly_ack", anomaly_id)
    return True


def describe_metrics() -> list[dict]:
    return [
        {
            "metric": m,
            "label": METRIC_LABELS[m],
            "desc": METRIC_DESC[m],
            "thresholds": DEFAULT_THRESHOLDS[m],
        }
        for m in METRICS
    ]
