"""MLOps：模型注册、版本晋级、指标对比、在线服务、多版本流量与弹性伸缩。

设计要点
--------
* **注册**：从 `model_experiments` 注册为 `registered_models`，并顺手落一个 v1 版本
  （指标直接继承实验的 metrics），注册即可用，不需要先手动加版本。
* **版本**：版本号 `v1, v2, ...` 自增，按模型内最大序号 + 1 计算，删除版本不会回退。
* **晋级（冠军唯一）**：`promote(version, "production")` 会把同模型其他 production 版本
  降为 archived —— 任何时刻一个模型最多一个 production。staging 允许多个候选并存，
  archived 无限制。
* **对比**：`compare_versions(a, b)` 返回共有指标的差值与相对变化，给晋级决策用。
* **服务**：`model_services` 老表只有 id/name/model_ref/version/replicas/status/endpoint，
  traffic / min_replicas / max_replicas / target_qps / idle_timeout_seconds / last_active_at
  这些列在 `ensure_columns()` 里用 ALTER TABLE ADD COLUMN 补齐（幂等，不改 store.py）。
* **A/B**：`traffic` 是 JSON `{"v1": 80, "v2": 20}`，写入前归一化到合计 100，
  详情接口同时给出每条版本的权重、副本估算与健康度。
* **弹性伸缩**：`scale()` 按目标 QPS 与当前副本推算期望副本数，夹在 [min, max] 之间；
  `autoscale_evaluate()` 按 target_qps 推导；`auto_stop_check()` 按 idle_timeout 自动停服。
"""
from __future__ import annotations

import json
import math
from typing import Any

from . import store

STAGES = ("staging", "production", "archived")
STAGE_LABELS = {"staging": "候选", "production": "生产", "archived": "归档"}
SERVICE_STATUS = ("running", "stopped", "scaling")

# model_services 需要的新列（老库用 ALTER TABLE 补，幂等）
EXTRA_COLUMNS: dict[str, str] = {
    "traffic": "TEXT DEFAULT '{}'",
    "min_replicas": "INTEGER DEFAULT 1",
    "max_replicas": "INTEGER DEFAULT 4",
    "target_qps": "REAL DEFAULT 50",
    "idle_timeout_seconds": "INTEGER DEFAULT 0",
    "last_active_at": "TEXT DEFAULT ''",
    "description": "TEXT DEFAULT ''",
    "updated_at": "TEXT DEFAULT ''",
}

_columns_ready = False


# ------------------------------------------------------------------ 内部工具


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 建表 / 补列


def ensure_columns() -> list[str]:
    """给 model_services 补齐扩展列。幂等：列已存在时直接跳过。"""
    global _columns_ready
    added: list[str] = []
    with store.db() as conn:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(model_services)").fetchall()}
        for name, decl in EXTRA_COLUMNS.items():
            if name in existing:
                continue
            conn.execute(f'ALTER TABLE "model_services" ADD COLUMN "{name}" {decl}')
            added.append(name)
    _columns_ready = True
    return added


def _ensure() -> None:
    if not _columns_ready:
        ensure_columns()


# ------------------------------------------------------------------ 指标工具


def _flatten_metrics(metrics: Any) -> dict[str, float]:
    """把指标拍平成 name -> float，只保留可比较的数值。"""
    data = _loads(metrics, {}) if not isinstance(metrics, dict) else metrics
    out: dict[str, float] = {}
    for key, value in (data or {}).items():
        if isinstance(value, bool):
            out[str(key)] = float(value)
        elif isinstance(value, (int, float)):
            out[str(key)] = float(value)
        elif isinstance(value, str):
            try:
                out[str(key)] = float(value)
            except ValueError:
                continue
    return out


def _better(name: str, delta: float) -> bool:
    """指标方向判断：loss / mae / mse / rmse / mape / error / latency 越低越好。"""
    lowered = (name or "").lower()
    lower_is_better = any(
        token in lowered
        for token in ("loss", "mae", "mse", "rmse", "mape", "error", "latency", "cost", "size")
    )
    return delta < 0 if lower_is_better else delta > 0


# ------------------------------------------------------------------ 模型注册


def _shape_model(row: dict, versions: list[dict] | None = None) -> dict:
    row = dict(row)
    row["versions"] = versions if versions is not None else []
    champion = next((v for v in row["versions"] if v.get("stage") == "production"), None)
    row["production_version"] = champion.get("version") if champion else ""
    row["version_count"] = len(row["versions"])
    row["latest_version"] = row["versions"][0]["version"] if row["versions"] else ""
    row["stages"] = {
        stage: [v["version"] for v in row["versions"] if v.get("stage") == stage]
        for stage in STAGES
    }
    return row


def _versions_of(model_id: str) -> list[dict]:
    rows = store.rows(
        "SELECT * FROM model_versions WHERE model_id = ? ORDER BY created_at DESC", (model_id,)
    )
    for r in rows:
        r["metrics"] = _loads(r.get("metrics"), {})
    rows.sort(key=lambda r: (_version_ordinal(r.get("version")), r.get("created_at") or ""), reverse=True)
    return rows


def _version_ordinal(version: str) -> int:
    text = str(version or "").strip().lstrip("vV")
    try:
        return int(text)
    except ValueError:
        return 0


def list_models(workspace_id: str, keyword: str = "") -> list[dict]:
    rows = store.rows(
        "SELECT * FROM registered_models WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    )
    out = [_shape_model(r, _versions_of(r["id"])) for r in rows]
    if keyword:
        kw = keyword.lower()
        out = [
            m for m in out
            if kw in (m.get("name") or "").lower()
            or kw in (m.get("description") or "").lower()
            or kw in (m.get("framework") or "").lower()
        ]
    return out


def get_model(workspace_id: str, model_ref: str) -> dict | None:
    row = store.row(
        "SELECT * FROM registered_models WHERE workspace_id = ? AND id = ?", (workspace_id, model_ref)
    )
    if not row:
        row = store.row(
            "SELECT * FROM registered_models WHERE workspace_id = ? AND name = ?",
            (workspace_id, model_ref),
        )
    if not row:
        return None
    model = _shape_model(row, _versions_of(row["id"]))
    model["services"] = [
        _shape_service(s) for s in store.rows(
            "SELECT * FROM model_services WHERE workspace_id = ? AND model_ref = ?",
            (workspace_id, row["name"]),
        )
    ]
    return model


def register_model(workspace_id: str, payload: dict) -> dict:
    """从实验注册模型；不指定实验时按 payload 直接建。"""
    _ensure()
    experiment_ref = (payload.get("source_experiment") or payload.get("experiment") or "").strip()
    experiment: dict | None = None
    if experiment_ref:
        experiment = store.row(
            "SELECT * FROM model_experiments WHERE workspace_id = ? AND (id = ? OR name = ?)",
            (workspace_id, experiment_ref, experiment_ref),
        )
        if not experiment:
            raise ValueError(f"实验 {experiment_ref} 不存在")

    name = (payload.get("name") or (experiment or {}).get("name") or "").strip()
    if not name:
        raise ValueError("模型名不能为空")
    if store.row(
        "SELECT id FROM registered_models WHERE workspace_id = ? AND name = ?", (workspace_id, name)
    ):
        raise ValueError(f"模型 {name} 已注册")

    row = {
        "id": store.new_id("md"),
        "workspace_id": workspace_id,
        "name": name,
        "description": payload.get("description") or (experiment or {}).get("status") or "",
        "source_experiment": (experiment or {}).get("id") or experiment_ref,
        "framework": payload.get("framework") or (experiment or {}).get("framework") or "",
        "owner": payload.get("owner") or (experiment or {}).get("owner") or "",
        "created_at": store.now(),
    }
    store.insert("registered_models", row)

    # 注册即落 v1，指标继承实验
    metrics = _loads((experiment or {}).get("metrics"), {}) if experiment else (payload.get("metrics") or {})
    version = add_version(
        workspace_id,
        row["id"],
        {
            "metrics": metrics,
            "artifact_uri": payload.get("artifact_uri") or "",
            "run_id": (experiment or {}).get("id") or "",
            "description": payload.get("version_description") or "由实验注册自动创建",
            "stage": payload.get("stage") or "staging",
        },
    )
    store.audit(workspace_id, None, "register_model", name, {"experiment": experiment_ref})
    return {"model": get_model(workspace_id, row["id"]), "version": version}


# ------------------------------------------------------------------ 版本


def add_version(workspace_id: str, model_ref: str, payload: dict) -> dict:
    """新增版本，版本号按模型内最大序号 + 1 自增。"""
    _ensure()
    model = store.row(
        "SELECT * FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, model_ref, model_ref),
    )
    if not model:
        raise ValueError("模型不存在")

    existing = _versions_of(model["id"])
    next_ordinal = max([_version_ordinal(v.get("version")) for v in existing] + [0]) + 1
    version = f"v{next_ordinal}"

    stage = (payload.get("stage") or "staging").strip().lower()
    if stage not in STAGES:
        raise ValueError(f"阶段必须是 {' / '.join(STAGES)}")

    metrics = payload.get("metrics")
    if metrics is None and payload.get("experiment"):
        experiment = store.row(
            "SELECT metrics FROM model_experiments WHERE workspace_id = ? AND (id = ? OR name = ?)",
            (workspace_id, payload["experiment"], payload["experiment"]),
        )
        metrics = _loads((experiment or {}).get("metrics"), {})

    row = {
        "id": store.new_id("mv"),
        "model_id": model["id"],
        "workspace_id": workspace_id,
        "version": version,
        "stage": stage,
        "metrics": _dumps(_loads(metrics, {}) if not isinstance(metrics, dict) else metrics),
        "artifact_uri": payload.get("artifact_uri") or f"runs:/{model['name']}/{version}",
        "run_id": payload.get("run_id") or "",
        "description": payload.get("description") or "",
        "created_at": store.now(),
    }
    store.insert("model_versions", row)
    row["metrics"] = _loads(row["metrics"], {})

    if stage == "production":
        _demote_others(model["id"], row["id"], stage)
    store.audit(workspace_id, None, "add_model_version", f"{model['name']}:{version}")
    return row


def list_versions(workspace_id: str, model_ref: str) -> dict:
    model = get_model(workspace_id, model_ref)
    if not model:
        raise ValueError("模型不存在")
    return {
        "model": {k: v for k, v in model.items() if k != "versions"},
        "versions": model["versions"],
        "stages": model["stages"],
        "production_version": model["production_version"],
    }


def get_version(workspace_id: str, model_ref: str, version_ref: str) -> dict | None:
    model = store.row(
        "SELECT * FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, model_ref, model_ref),
    )
    if not model:
        return None
    row = store.row(
        "SELECT * FROM model_versions WHERE model_id = ? AND (id = ? OR version = ?)",
        (model["id"], version_ref, version_ref),
    )
    if not row:
        return None
    row["metrics"] = _loads(row.get("metrics"), {})
    row["model_name"] = model["name"]
    row["stage_label"] = STAGE_LABELS.get(row.get("stage") or "", row.get("stage") or "")
    return row


def _demote_others(model_id: str, keep_version_id: str, stage: str) -> list[str]:
    """冠军唯一：同模型其它 production 版本降为 archived。

    只有 production 是独占阶段（任何时刻一个模型最多一个冠军）。
    staging 允许多个候选版本并存 —— 把老候选直接归档会让人丢失「还在评估」的版本。
    """
    if stage != "production":
        return []
    demoted: list[str] = []
    others = store.rows(
        "SELECT id, version, stage FROM model_versions WHERE model_id = ? AND stage = 'production' AND id != ?",
        (model_id, keep_version_id),
    )
    for other in others:
        store.update("model_versions", other["id"], {"stage": "archived"})
        demoted.append(other["version"])
    return demoted


def promote_version(
    workspace_id: str, model_ref: str, version_ref: str, stage: str
) -> dict:
    """阶段流转：staging / production / archived。晋级 production 时同模型其它 production 降 archived。"""
    _ensure()
    stage = (stage or "").strip().lower()
    if stage not in STAGES:
        raise ValueError(f"阶段必须是 {' / '.join(STAGES)}")

    model = store.row(
        "SELECT * FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, model_ref, model_ref),
    )
    if not model:
        raise ValueError("模型不存在")
    version = store.row(
        "SELECT * FROM model_versions WHERE model_id = ? AND (id = ? OR version = ?)",
        (model["id"], version_ref, version_ref),
    )
    if not version:
        raise ValueError("版本不存在")

    previous_stage = version.get("stage") or "staging"
    demoted: list[str] = []
    if stage == "production":
        demoted = _demote_others(model["id"], version["id"], stage)
    store.update("model_versions", version["id"], {"stage": stage})

    store.audit(
        workspace_id,
        None,
        "promote_model",
        f"{model['name']}:{version['version']}",
        {"from": previous_stage, "to": stage, "demoted": demoted},
    )

    # 模型服务引用了被晋级版本时，把服务的主版本对齐到冠军
    synced_services: list[str] = []
    if stage == "production":
        for svc in store.rows(
            "SELECT * FROM model_services WHERE workspace_id = ? AND model_ref = ?",
            (workspace_id, model["name"]),
        ):
            patch: dict[str, Any] = {"version": version["version"], "updated_at": store.now()}
            traffic = _loads(svc.get("traffic"), {})
            if not traffic:
                patch["traffic"] = _dumps({version["version"]: 100})
            store.update("model_services", svc["id"], patch)
            synced_services.append(svc["name"])

    return {
        "ok": True,
        "model": model["name"],
        "version": version["version"],
        "previous_stage": previous_stage,
        "stage": stage,
        "demoted": demoted,
        "champion": version["version"] if stage == "production" else _champion_of(model["id"]),
        "synced_services": synced_services,
        "versions": _versions_of(model["id"]),
    }


def _champion_of(model_id: str) -> str:
    row = store.row(
        "SELECT version FROM model_versions WHERE model_id = ? AND stage = 'production' LIMIT 1",
        (model_id,),
    )
    return (row or {}).get("version") or ""


def compare_versions(workspace_id: str, model_ref: str, a: str, b: str) -> dict:
    """指标对比：返回共有指标的差值、相对变化与优劣判断。"""
    left = get_version(workspace_id, model_ref, a)
    right = get_version(workspace_id, model_ref, b)
    if not left:
        raise ValueError(f"版本 {a} 不存在")
    if not right:
        raise ValueError(f"版本 {b} 不存在")

    left_metrics = _flatten_metrics(left.get("metrics"))
    right_metrics = _flatten_metrics(right.get("metrics"))
    keys = sorted(set(left_metrics) | set(right_metrics))
    rows: list[dict] = []
    for key in keys:
        lv = left_metrics.get(key)
        rv = right_metrics.get(key)
        delta = None
        pct = None
        better = ""
        if lv is not None and rv is not None:
            delta = round(rv - lv, 6)
            pct = round((rv - lv) / abs(lv), 6) if lv else None
            if abs(delta) < 1e-12:
                better = "tie"
            else:
                better = "b" if _better(key, delta) else "a"
        rows.append(
            {
                "metric": key,
                "a": lv,
                "b": rv,
                "delta": delta,
                "delta_pct": pct,
                "better": better,
            }
        )

    wins = {"a": 0, "b": 0, "tie": 0}
    for r in rows:
        if r["better"]:
            wins[r["better"]] += 1
    recommendation = "b" if wins["b"] > wins["a"] else ("a" if wins["a"] > wins["b"] else "tie")

    return {
        "model": left.get("model_name") or right.get("model_name"),
        "a": {
            "version": left.get("version"),
            "id": left.get("id"),
            "stage": left.get("stage"),
            "metrics": left_metrics,
        },
        "b": {
            "version": right.get("version"),
            "id": right.get("id"),
            "stage": right.get("stage"),
            "metrics": right_metrics,
        },
        "metrics": rows,
        "wins": wins,
        "recommendation": recommendation,
        "recommendation_text": {
            "a": f"建议保留 {left.get('version')}",
            "b": f"建议晋级 {right.get('version')}",
            "tie": "两版本指标持平，可按成本或延迟决定",
        }[recommendation],
    }


# ------------------------------------------------------------------ 服务


def _shape_service(row: dict) -> dict:
    _ensure()
    row = dict(row)
    traffic = _loads(row.get("traffic"), {})
    traffic = {str(k): _int(v) for k, v in traffic.items()} if isinstance(traffic, dict) else {}
    if not traffic and row.get("version"):
        traffic = {str(row["version"]): 100}
    row["traffic"] = traffic
    row["min_replicas"] = _int(row.get("min_replicas"), 1)
    row["max_replicas"] = _int(row.get("max_replicas"), 4)
    row["target_qps"] = _num(row.get("target_qps"), 50)
    row["idle_timeout_seconds"] = _int(row.get("idle_timeout_seconds"), 0)
    row["replicas"] = _int(row.get("replicas"), 1)
    row["status_label"] = {"running": "运行中", "stopped": "已停止", "scaling": "扩缩容中"}.get(
        row.get("status") or "", row.get("status") or "-"
    )
    total = sum(traffic.values())
    row["traffic_total"] = total
    row["traffic_detail"] = [
        {
            "version": version,
            "weight": weight,
            "percent": round(weight / total * 100, 2) if total else 0.0,
            "replicas": _split_replicas(row["replicas"], weight, total),
        }
        for version, weight in sorted(traffic.items(), key=lambda kv: -kv[1])
    ]
    row["autoscale"] = {
        "enabled": row["max_replicas"] > row["min_replicas"],
        "min_replicas": row["min_replicas"],
        "max_replicas": row["max_replicas"],
        "target_qps": row["target_qps"],
        "capacity_qps": round(row["replicas"] * row["target_qps"], 2),
    }
    row["idle"] = {
        "timeout_seconds": row["idle_timeout_seconds"],
        "last_active_at": row.get("last_active_at") or "",
        "auto_stop": row["idle_timeout_seconds"] > 0,
    }
    row["ab_test"] = len([k for k, v in traffic.items() if v > 0]) > 1
    return row


def _split_replicas(replicas: int, weight: int, total: int) -> int:
    if not total:
        return 0
    return max(1, round(replicas * weight / total)) if weight > 0 else 0


def list_services(workspace_id: str) -> list[dict]:
    _ensure()
    rows = store.rows(
        "SELECT * FROM model_services WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    )
    return [_shape_service(r) for r in rows]


def get_service(workspace_id: str, service_ref: str) -> dict | None:
    _ensure()
    row = store.row(
        "SELECT * FROM model_services WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, service_ref, service_ref),
    )
    if not row:
        return None
    service = _shape_service(row)
    model = store.row(
        "SELECT * FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, row.get("model_ref") or "", row.get("model_ref") or ""),
    )
    if model:
        service["model"] = {
            "id": model["id"],
            "name": model["name"],
            "framework": model.get("framework"),
            "champion": _champion_of(model["id"]),
            "versions": [v["version"] for v in _versions_of(model["id"])],
        }
    else:
        service["model"] = None
    return service


def create_service(workspace_id: str, payload: dict) -> dict:
    _ensure()
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("服务名不能为空")
    if store.row(
        "SELECT id FROM model_services WHERE workspace_id = ? AND name = ?", (workspace_id, name)
    ):
        raise ValueError(f"服务 {name} 已存在")

    model_ref = (payload.get("model_ref") or payload.get("model") or "").strip()
    model = store.row(
        "SELECT * FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, model_ref, model_ref),
    )
    if not model:
        raise ValueError(f"模型 {model_ref} 未注册，请先在模型注册表注册")

    versions = _versions_of(model["id"])
    version = (payload.get("version") or "").strip() or _champion_of(model["id"]) or (
        versions[0]["version"] if versions else "v1"
    )
    if versions and version not in {v["version"] for v in versions}:
        raise ValueError(f"版本 {version} 不存在")

    replicas = max(1, _int(payload.get("replicas"), 1))
    min_replicas = max(1, _int(payload.get("min_replicas"), 1))
    max_replicas = max(min_replicas, _int(payload.get("max_replicas"), max(replicas, 4)))
    replicas = min(max(replicas, min_replicas), max_replicas)

    traffic = payload.get("traffic")
    if traffic:
        traffic = _normalize_traffic(traffic, versions)
    else:
        traffic = {version: 100}

    row = {
        "id": store.new_id("sv"),
        "workspace_id": workspace_id,
        "name": name,
        "model_ref": model["name"],
        "version": version,
        "replicas": replicas,
        "status": "running" if payload.get("start", True) else "stopped",
        "endpoint": payload.get("endpoint") or f"/svc/{name.replace('_', '-')}",
        "traffic": _dumps(traffic),
        "min_replicas": min_replicas,
        "max_replicas": max_replicas,
        "target_qps": _num(payload.get("target_qps"), 50),
        "idle_timeout_seconds": max(0, _int(payload.get("idle_timeout_seconds"), 0)),
        "last_active_at": store.now(),
        "description": payload.get("description") or "",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("model_services", row)
    store.audit(workspace_id, None, "create_service", name, {"model": model["name"], "version": version})
    return _shape_service(store.row("SELECT * FROM model_services WHERE id = ?", (row["id"],)) or row)


def _normalize_traffic(traffic: Any, versions: list[dict]) -> dict[str, int]:
    """流量归一化：校验版本存在、权重非负，合计归一到 100。"""
    data = _loads(traffic, {}) if not isinstance(traffic, dict) else traffic
    known = {v["version"] for v in versions}
    cleaned: dict[str, int] = {}
    for version, weight in (data or {}).items():
        key = str(version).strip()
        if known and key not in known:
            raise ValueError(f"版本 {key} 不存在，可选：{', '.join(sorted(known))}")
        value = max(0, _int(weight, 0))
        if value:
            cleaned[key] = value
    if not cleaned:
        raise ValueError("流量配置至少需要一个版本权重 > 0")
    total = sum(cleaned.values())
    if total == 100:
        return cleaned
    scaled = {k: max(1, round(v / total * 100)) for k, v in cleaned.items()}
    # 取整误差归到最大权重那条上，保证合计恰好 100
    diff = 100 - sum(scaled.values())
    if diff:
        top = max(scaled, key=lambda k: scaled[k])
        scaled[top] = max(1, scaled[top] + diff)
    return scaled


def update_service(workspace_id: str, service_ref: str, payload: dict) -> dict:
    _ensure()
    row = store.row(
        "SELECT * FROM model_services WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, service_ref, service_ref),
    )
    if not row:
        raise ValueError("服务不存在")

    patch: dict[str, Any] = {"updated_at": store.now()}
    if payload.get("description") is not None:
        patch["description"] = payload["description"]

    min_replicas = _int(payload.get("min_replicas"), _int(row.get("min_replicas"), 1))
    max_replicas = _int(payload.get("max_replicas"), _int(row.get("max_replicas"), 4))
    if max_replicas < min_replicas:
        raise ValueError("max_replicas 不能小于 min_replicas")
    patch["min_replicas"] = max(1, min_replicas)
    patch["max_replicas"] = max(1, max_replicas)

    if payload.get("target_qps") is not None:
        patch["target_qps"] = max(1.0, _num(payload["target_qps"], 50))
    if payload.get("idle_timeout_seconds") is not None:
        patch["idle_timeout_seconds"] = max(0, _int(payload["idle_timeout_seconds"], 0))

    if payload.get("replicas") is not None:
        replicas = _int(payload["replicas"], row.get("replicas") or 1)
        patch["replicas"] = min(max(replicas, patch["min_replicas"]), patch["max_replicas"])

    if payload.get("version"):
        model = store.row(
            "SELECT id FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
            (workspace_id, row.get("model_ref") or "", row.get("model_ref") or ""),
        )
        versions = _versions_of(model["id"]) if model else []
        if versions and payload["version"] not in {v["version"] for v in versions}:
            raise ValueError(f"版本 {payload['version']} 不存在")
        patch["version"] = payload["version"]

    if payload.get("traffic") is not None:
        model = store.row(
            "SELECT id FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
            (workspace_id, row.get("model_ref") or "", row.get("model_ref") or ""),
        )
        versions = _versions_of(model["id"]) if model else []
        traffic = _normalize_traffic(payload["traffic"], versions)
        patch["traffic"] = _dumps(traffic)

    store.update("model_services", row["id"], patch)
    return _shape_service(store.row("SELECT * FROM model_services WHERE id = ?", (row["id"],)))


def set_traffic(workspace_id: str, service_ref: str, traffic: Any) -> dict:
    """A/B 流量切分：写入前归一化到合计 100。"""
    _ensure()
    row = store.row(
        "SELECT * FROM model_services WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, service_ref, service_ref),
    )
    if not row:
        raise ValueError("服务不存在")
    model = store.row(
        "SELECT id FROM registered_models WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, row.get("model_ref") or "", row.get("model_ref") or ""),
    )
    versions = _versions_of(model["id"]) if model else []
    normalized = _normalize_traffic(traffic, versions)

    # 主版本对齐到权重最大的版本，保证单版本视图也正确
    primary = max(normalized, key=lambda k: normalized[k])
    store.update(
        "model_services",
        row["id"],
        {
            "traffic": _dumps(normalized),
            "version": primary,
            "updated_at": store.now(),
            "last_active_at": store.now(),
        },
    )
    store.audit(workspace_id, None, "set_service_traffic", row["name"], normalized)
    return _shape_service(store.row("SELECT * FROM model_services WHERE id = ?", (row["id"],)))


def service_action(workspace_id: str, service_ref: str, action: str, payload: dict | None = None) -> dict:
    """start / stop / scale。"""
    _ensure()
    payload = payload or {}
    action = (action or "").strip().lower()
    if action not in ("start", "stop", "scale"):
        raise ValueError("action 必须是 start / stop / scale")

    row = store.row(
        "SELECT * FROM model_services WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, service_ref, service_ref),
    )
    if not row:
        raise ValueError("服务不存在")

    patch: dict[str, Any] = {"updated_at": store.now()}
    detail: dict[str, Any] = {}

    if action == "start":
        patch["status"] = "running"
        patch["last_active_at"] = store.now()
        detail = {"status": "running"}
    elif action == "stop":
        patch["status"] = "stopped"
        detail = {"status": "stopped"}
    else:
        result = _scale(row, payload)
        patch.update(result["patch"])
        detail = result["detail"]

    store.update("model_services", row["id"], patch)
    store.audit(workspace_id, None, f"{action}_service", row["name"], detail)
    service = _shape_service(store.row("SELECT * FROM model_services WHERE id = ?", (row["id"],)))
    service["action"] = action
    service["detail"] = detail
    return service


def _scale(row: dict, payload: dict) -> dict:
    """弹性伸缩：按目标副本或目标 QPS 推算期望副本数，夹在 [min, max] 之间。"""
    min_replicas = max(1, _int(payload.get("min_replicas"), _int(row.get("min_replicas"), 1)))
    max_replicas = _int(payload.get("max_replicas"), _int(row.get("max_replicas"), 4))
    if max_replicas < min_replicas:
        raise ValueError("max_replicas 不能小于 min_replicas")
    target_qps = max(1.0, _num(payload.get("target_qps"), _num(row.get("target_qps"), 50)))
    current = _int(row.get("replicas"), 1)

    if payload.get("replicas") is not None:
        desired = _int(payload["replicas"], current)
        reason = "手动指定副本数"
    elif payload.get("qps") is not None:
        qps = max(0.0, _num(payload["qps"], 0))
        desired = max(1, math.ceil(qps / target_qps))
        reason = f"按 {qps:.0f} QPS / 单副本 {target_qps:.0f} QPS 推算"
    else:
        desired = current
        reason = "未提供扩缩容参数，保持当前副本"

    desired = min(max(desired, min_replicas), max_replicas)
    patch = {
        "replicas": desired,
        "min_replicas": min_replicas,
        "max_replicas": max_replicas,
        "target_qps": target_qps,
        # 扩缩容不改运行状态：停着的服务调副本数不应被隐式拉起
        "last_active_at": store.now(),
    }
    return {
        "patch": patch,
        "detail": {
            "from": current,
            "to": desired,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "target_qps": target_qps,
            "reason": reason,
        },
    }


def autoscale_evaluate(workspace_id: str, service_ref: str, qps: float = 0.0) -> dict:
    """按当前 QPS 评估期望副本数（不落库，只给建议）。"""
    row = store.row(
        "SELECT * FROM model_services WHERE workspace_id = ? AND (id = ? OR name = ?)",
        (workspace_id, service_ref, service_ref),
    )
    if not row:
        raise ValueError("服务不存在")
    target_qps = max(1.0, _num(row.get("target_qps"), 50))
    min_replicas = max(1, _int(row.get("min_replicas"), 1))
    max_replicas = max(min_replicas, _int(row.get("max_replicas"), 4))
    current = _int(row.get("replicas"), 1)
    desired = min(max(math.ceil(max(qps, 0) / target_qps), min_replicas), max_replicas)
    return {
        "service": row["name"],
        "current_replicas": current,
        "desired_replicas": desired,
        "qps": qps,
        "target_qps": target_qps,
        "min_replicas": min_replicas,
        "max_replicas": max_replicas,
        "action": "scale_out" if desired > current else ("scale_in" if desired < current else "hold"),
        "saturated": qps > current * target_qps,
    }


def auto_stop_check(workspace_id: str, now_iso: str | None = None) -> dict:
    """自动启停：空闲超过 idle_timeout_seconds 的运行中服务自动停掉。"""
    _ensure()
    from datetime import datetime, timezone

    reference = None
    if now_iso:
        try:
            reference = datetime.fromisoformat(now_iso)
        except ValueError:
            reference = None
    if reference is None:
        reference = datetime.now(timezone.utc).astimezone()

    stopped: list[str] = []
    for row in store.rows(
        "SELECT * FROM model_services WHERE workspace_id = ? AND status = 'running'", (workspace_id,)
    ):
        timeout = _int(row.get("idle_timeout_seconds"), 0)
        if timeout <= 0:
            continue
        last = row.get("last_active_at") or row.get("created_at") or ""
        try:
            last_dt = datetime.fromisoformat(last)
        except (ValueError, TypeError):
            continue
        idle = (reference - last_dt).total_seconds()
        if idle >= timeout:
            store.update(
                "model_services",
                row["id"],
                {"status": "stopped", "updated_at": store.now()},
            )
            stopped.append(row["name"])
    return {"checked_at": store.now(), "stopped": stopped, "count": len(stopped)}


# ------------------------------------------------------------------ 概览


def summary(workspace_id: str) -> dict:
    _ensure()
    models = list_models(workspace_id)
    services = list_services(workspace_id)
    versions = [v for m in models for v in m["versions"]]
    stage_count = {stage: 0 for stage in STAGES}
    for v in versions:
        stage_count[v.get("stage") or "staging"] = stage_count.get(v.get("stage") or "staging", 0) + 1
    return {
        "models": len(models),
        "versions": len(versions),
        "stages": stage_count,
        "champions": len([m for m in models if m["production_version"]]),
        "services": len(services),
        "running_services": len([s for s in services if s.get("status") == "running"]),
        "replicas": sum(s.get("replicas") or 0 for s in services),
        "ab_services": len([s for s in services if s.get("ab_test")]),
        "stage_labels": STAGE_LABELS,
    }


def describe_service(service: dict) -> str:
    traffic = service.get("traffic") or {}
    split = " / ".join(f"{k}:{v}%" for k, v in traffic.items()) or "-"
    return (
        f"{service.get('name')} → {service.get('model_ref')} 流量 {split}，"
        f"副本 {service.get('replicas')}，状态 {service.get('status_label')}"
    )
