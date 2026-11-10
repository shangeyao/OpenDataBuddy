"""配置即代码（Bundle）：把工作空间资源写进 YAML，按环境幂等发布。

为什么值得单独做一层：手工在 UI 里点出来的资源配置无法 review、无法回滚、
也无法在 dev/staging/prod 之间保持一致。Bundle 把「声明」和「落地」分开：
YAML 是唯一事实来源，deploy 负责把它对齐到某个环境。

刻意不引入 pyyaml：本项目的解析需求只有「映射 / 列表 / 标量 / 注释 / 多行块」
这几类，自己写一个 100 行的解析器换来零依赖，比多一个 C 扩展更划算。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from . import store

ENVS = ["dev", "staging", "prod"]

# 资源类型注册表：每种资源的字段约定 + 读写钩子
RESOURCE_KINDS: dict[str, dict[str, Any]] = {
    "flows": {
        "label": "工作流",
        "table": "flows",
        "key": "name",
        "fields": ["name", "description", "cron", "nodes", "enabled"],
    },
    "metrics": {
        "label": "指标",
        "table": "metrics",
        "key": "name",
        "fields": ["name", "display_name", "expr", "table_ref", "unit"],
    },
    "dashboards": {
        "label": "仪表盘",
        "table": "dashboards",
        "key": "name",
        "fields": ["name", "description", "spec"],
    },
    "quality_rules": {
        "label": "质量规则",
        "table": "quality_rules",
        "key": "name",
        "fields": ["name", "table_ref", "column_name", "rule_type", "config"],
    },
    "knowledge": {
        "label": "知识库",
        "table": "knowledge_docs",
        "key": "title",
        "fields": ["title", "content", "source_type"],
    },
    "agents": {
        "label": "智能体",
        "table": "agents",
        "key": "name",
        "fields": ["name", "mode", "description", "prompt", "tools"],
    },
}


# ------------------------------------------------------------------ YAML 解析
# 只支持 bundle 需要的子集：嵌套映射、`- ` 列表、标量、# 注释、引号、| 与 > 块。
# 明确不支持锚点/别名/流式集合 —— 遇到就当普通标量，避免半吊子语义。


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但保留引号内的 #。"""
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_yaml(text: str) -> Any:
    """把 YAML 子集解析成 Python 对象。缩进用空格，不支持 tab。"""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[tuple[int, str]] = []
    block: list[str] | None = None
    block_indent = 0
    block_style = ""
    block_key_indent = 0

    for raw in raw_lines:
        if block is not None:
            indent = len(raw) - len(raw.lstrip(" "))
            if raw.strip() == "" or indent > block_key_indent:
                block.append(raw[block_indent:] if len(raw) >= block_indent else "")
                continue
            # 块结束
            lines.append((-1, block_style + "\x00" + "\n".join(block)))
            block = None
        if raw.strip() == "":
            continue
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        body = stripped.strip()
        if re.search(r":\s*[|>][+-]?$", body):
            block = []
            block_indent = indent + 2
            block_key_indent = indent
            block_style = body[-1]
            lines.append((indent, body))
            continue
        lines.append((indent, body))
    if block is not None:
        lines.append((-1, block_style + "\x00" + "\n".join(block)))

    pos = 0

    def consume_block() -> str | None:
        """吃掉紧跟在键后面的块标量行（解析阶段用 -1 标记）。"""
        nonlocal pos
        if pos < len(lines) and lines[pos][0] == -1:
            marker, payload = lines[pos][1][0], lines[pos][1][2:]
            pos += 1
            if marker == ">":
                return " ".join(x.strip() for x in payload.split("\n") if x.strip())
            return payload
        return None

    def parse_block(indent: int) -> Any:
        nonlocal pos
        if pos >= len(lines):
            return None
        if lines[pos][0] == -1:
            block_text = consume_block()
            return block_text if block_text is not None else ""

        # 列表
        if lines[pos][0] == indent and lines[pos][1].startswith("- "):
            items: list[Any] = []
            while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("- "):
                item_body = lines[pos][1][2:].strip()
                pos += 1
                if ":" in item_body and not item_body.startswith(("'", '"')):
                    # 列表项是映射：把首行当作映射的第一对，后续同级缩进继续读
                    key, _, val = item_body.partition(":")
                    obj: dict[str, Any] = {}
                    val = val.strip()
                    if val and val[0] in "|>":
                        block_text = consume_block()
                        obj[key.strip()] = block_text if block_text is not None else ""
                    elif val:
                        obj[key.strip()] = _scalar(val)
                    else:
                        nxt = pos
                        if nxt < len(lines) and (lines[nxt][0] > indent or lines[nxt][0] == -1):
                            obj[key.strip()] = parse_block(lines[nxt][0])
                        else:
                            obj[key.strip()] = None
                    # 继续吸收同项的其余键
                    while pos < len(lines) and lines[pos][0] > indent and not lines[pos][1].startswith("- "):
                        cur_indent = lines[pos][0]
                        line = lines[pos][1]
                        pos += 1
                        k2, _, v2 = line.partition(":")
                        v2 = v2.strip()
                        if v2 and v2[0] in "|>":
                            block_text = consume_block()
                            obj[k2.strip()] = block_text if block_text is not None else ""
                        elif v2:
                            obj[k2.strip()] = _scalar(v2)
                        else:
                            if pos < len(lines) and (lines[pos][0] > cur_indent or lines[pos][0] == -1):
                                obj[k2.strip()] = parse_block(lines[pos][0])
                            else:
                                obj[k2.strip()] = None
                    items.append(obj)
                else:
                    items.append(_scalar(item_body))
            return items

        # 映射
        mapping: dict[str, Any] = {}
        while pos < len(lines) and lines[pos][0] == indent:
            line = lines[pos][1]
            if line.startswith("- "):
                break
            key, sep, val = line.partition(":")
            if not sep:
                pos += 1
                continue
            key = key.strip().strip("'\"")
            val = val.strip()
            pos += 1
            if val and val[0] in "|>":
                block_text = consume_block()
                mapping[key] = block_text if block_text is not None else ""
            elif val:
                mapping[key] = _scalar(val)
            else:
                if pos < len(lines) and (lines[pos][0] > indent or lines[pos][0] == -1):
                    mapping[key] = parse_block(lines[pos][0])
                else:
                    mapping[key] = None
        return mapping

    if not lines:
        return {}
    result = parse_block(lines[0][0])
    return result if result is not None else {}


def dump_yaml(obj: Any, indent: int = 0) -> str:
    """把对象写回 YAML，用于 init 模板与前端回显。"""
    pad = " " * indent
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            if _needs_block(v):
                out.append(f"{pad}{k}: {_dump_block(v, pad)}")
            elif isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out.append(dump_yaml(v, indent + 2))
            elif isinstance(v, dict):
                out.append(f"{pad}{k}: {{}}")
            elif isinstance(v, list):
                out.append(f"{pad}{k}: []")
            else:
                out.append(f"{pad}{k}: {_dump_scalar(v)}")
        return "\n".join(out)
    if isinstance(obj, list):
        out = []
        for v in obj:
            if _needs_block(v):
                out.append(f"{pad}- {_dump_block(v, pad)}")
            elif isinstance(v, dict):
                inner = dump_yaml(v, indent + 2)
                first, *rest = inner.split("\n")
                out.append(f"{pad}- {first.strip()}")
                out.extend(rest)
            elif isinstance(v, list):
                out.append(f"{pad}-")
                out.append(dump_yaml(v, indent + 2))
            else:
                out.append(f"{pad}- {_dump_scalar(v)}")
        return "\n".join(out)
    return f"{pad}{_dump_scalar(obj)}"


def _dump_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    text = str(v)
    if text == "" or any(c in text for c in ":#\n\"'") or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def _dump_block(value: str, pad: str) -> str:
    """多行字符串写成 `|` 块标量，保持换行语义。"""
    body = "\n".join(f"{pad}  {line}" for line in value.split("\n"))
    return f"|\n{body}"


def _needs_block(v: Any) -> bool:
    return isinstance(v, str) and "\n" in v


# ------------------------------------------------------------------ Bundle


TEMPLATE = {
    "bundle": "my-bundle",
    "targets": {
        "dev": {"workspace": "默认工作空间"},
        "staging": {"workspace": "默认工作空间"},
        "prod": {"workspace": "默认工作空间"},
    },
    "resources": {
        "flows": [],
        "metrics": [],
        "dashboards": [],
        "quality_rules": [],
        "agents": [],
    },
}


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _record(bundle_id: str, workspace_id: str, env: str, action: str, status: str,
            summary: str, log: list[dict]) -> dict:
    row = {
        "id": store.new_id(),
        "bundle_id": bundle_id,
        "workspace_id": workspace_id,
        "env": env,
        "action": action,
        "status": status,
        "summary": summary,
        "log": json.dumps(log, ensure_ascii=False, default=str),
        "created_at": store.now(),
    }
    store.insert("bundle_deployments", row)
    return row


# ------------------------------------------------------------------ 六个动作


def init(workspace_id: str, name: str) -> dict:
    """创建一个 bundle 骨架并落库。"""
    name = (name or "").strip() or "my-bundle"
    if store.row(
        "SELECT id FROM bundles WHERE workspace_id = ? AND name = ?", (workspace_id, name)
    ):
        raise ValueError(f"Bundle {name} 已存在")
    tpl = json.loads(json.dumps(TEMPLATE))
    tpl["bundle"] = name
    text = dump_yaml(tpl)
    row = {
        "id": store.new_id(),
        "workspace_id": workspace_id,
        "name": name,
        "yaml": text,
        "status": "draft",
        "issues": "[]",
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    store.insert("bundles", row)
    return row


def save_yaml(bundle_id: str, text: str) -> dict:
    row = store.row("SELECT * FROM bundles WHERE id = ?", (bundle_id,))
    if not row:
        raise ValueError("Bundle 不存在")
    store.update("bundles", bundle_id, {"yaml": text, "status": "draft", "updated_at": store.now()})
    return store.row("SELECT * FROM bundles WHERE id = ?", (bundle_id,)) or row


def get_bundle(bundle_id: str) -> dict | None:
    row = store.row("SELECT * FROM bundles WHERE id = ?", (bundle_id,))
    if row:
        row["issues"] = _loads(row.get("issues"), [])
    return row


def list_bundles(workspace_id: str) -> list[dict]:
    rows = store.rows(
        "SELECT * FROM bundles WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,)
    )
    for r in rows:
        r["issues"] = _loads(r.get("issues"), [])
        last = store.row(
            "SELECT action, env, status, created_at FROM bundle_deployments "
            "WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 1",
            (r["id"],),
        )
        r["last_action"] = last
    return rows


def validate(text: str, workspace_id: str | None = None) -> dict:
    """解析 + 语义校验。返回 {ok, issues, parsed}。"""
    issues: list[dict] = []

    def add(level: str, path: str, message: str) -> None:
        issues.append({"level": level, "path": path, "message": message})

    try:
        doc = parse_yaml(text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "issues": [{"level": "error", "path": "/", "message": f"YAML 解析失败：{exc}"}], "parsed": {}}

    if not isinstance(doc, dict):
        add("error", "/", "根节点必须是映射")
        return {"ok": False, "issues": issues, "parsed": doc}

    if not doc.get("bundle"):
        add("error", "/bundle", "缺少 bundle 字段（bundle 名称）")
    if not isinstance(doc.get("targets"), dict) or not doc.get("targets"):
        add("error", "/targets", "缺少 targets 字段（环境 → 工作空间映射）")
    else:
        for env in doc["targets"]:
            if env not in ENVS:
                add("warning", f"/targets/{env}", f"环境 {env} 不在推荐列表 {ENVS} 中")
    if not isinstance(doc.get("resources"), dict):
        add("error", "/resources", "缺少 resources 字段（资源声明）")
    else:
        for kind in doc["resources"]:
            if kind not in RESOURCE_KINDS:
                add("error", f"/resources/{kind}", f"未知资源类型 {kind}，支持：{', '.join(RESOURCE_KINDS)}")

        # 引用完整性：同一 bundle 内资源重名
        for kind, items in doc["resources"].items():
            if kind not in RESOURCE_KINDS or not isinstance(items, list):
                continue
            key = RESOURCE_KINDS[kind]["key"]
            seen: set[str] = set()
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    add("error", f"/resources/{kind}[{idx}]", "资源项必须是映射")
                    continue
                name = item.get(key)
                if not name:
                    add("error", f"/resources/{kind}[{idx}]", f"缺少必填字段 {key}")
                    continue
                if name in seen:
                    add("error", f"/resources/{kind}[{idx}]", f"资源 {name} 在同一 bundle 内重复")
                seen.add(name)

                # 工作流节点引用校验
                if kind == "flows" and isinstance(item.get("nodes"), list):
                    node_ids = {n.get("id") for n in item["nodes"] if isinstance(n, dict) and n.get("id")}
                    for n in item["nodes"]:
                        if not isinstance(n, dict):
                            continue
                        for dep in n.get("depends_on") or []:
                            if dep not in node_ids:
                                add(
                                    "error",
                                    f"/resources/flows/{name}/nodes/{n.get('id')}",
                                    f"依赖的节点 {dep} 不存在",
                                )

        # 指标引用的表是否存在（有 workspace 上下文时才查）
        if workspace_id:
            for kind in ("metrics", "quality_rules"):
                for item in doc["resources"].get(kind) or []:
                    if not isinstance(item, dict):
                        continue
                    ref = item.get("table_ref")
                    if ref:
                        found = store.row(
                            "SELECT t.id FROM tables t JOIN schemas s ON s.id = t.schema_id "
                            "JOIN catalogs c ON c.id = s.catalog_id "
                            "WHERE c.workspace_id = ? AND (t.name = ? OR (c.name || '.' || s.name || '.' || t.name) = ?)",
                            (workspace_id, str(ref).split(".")[-1], ref),
                        )
                        if not found:
                            add("warning", f"/resources/{kind}/{item.get('name') or item.get('title')}",
                                f"引用的表 {ref} 不存在")

    ok = not any(i["level"] == "error" for i in issues)
    return {"ok": ok, "issues": issues, "parsed": doc}


def _existing(workspace_id: str, kind: str, name: str) -> dict | None:
    spec = RESOURCE_KINDS[kind]
    return store.row(
        f'SELECT * FROM "{spec["table"]}" WHERE workspace_id = ? AND "{spec["key"]}" = ?',
        (workspace_id, name),
    )


def _same(a: Any, b: Any) -> bool:
    """SQLite 里布尔存成 0/1、JSON 存成字符串，比较前先归一化。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if a is None and b is None:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


def _apply(workspace_id: str, kind: str, item: dict) -> tuple[str, dict]:
    """幂等 upsert 一条资源，返回 (动作, 记录)。"""
    spec = RESOURCE_KINDS[kind]
    key = spec["key"]
    name = item.get(key)
    current = _existing(workspace_id, kind, name)
    payload: dict[str, Any] = {}
    for f in spec["fields"]:
        if f in item:
            val = item[f]
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            payload[f] = val

    if current:
        changed = {k: v for k, v in payload.items() if not _same(current.get(k), v)}
        if not changed:
            return "unchanged", current
        store.update(spec["table"], current["id"], changed)
        return "updated", {**current, **changed}

    payload["id"] = store.new_id()
    payload["workspace_id"] = workspace_id
    if kind == "flows":
        payload.setdefault("enabled", 1)
        payload.setdefault("nodes", "[]")
        payload.setdefault("created_at", store.now())
    elif kind == "metrics":
        payload.setdefault("display_name", name)
        payload.setdefault("status", "published")
        payload.setdefault("dimensions", "[]")
        payload.setdefault("joins", "[]")
        payload.setdefault("created_at", store.now())
    else:
        payload.setdefault("created_at", store.now())
    store.insert(spec["table"], payload)
    return "created", payload


def deploy(bundle_id: str, env: str = "dev") -> dict:
    """把 bundle 声明的资源对齐到指定环境（以工作空间为落点）。"""
    bundle = get_bundle(bundle_id)
    if not bundle:
        raise ValueError("Bundle 不存在")
    result = validate(bundle["yaml"], bundle["workspace_id"])
    if not result["ok"]:
        _record(bundle_id, bundle["workspace_id"], env, "deploy", "failed", "校验未通过，发布中止", result["issues"])
        store.update("bundles", bundle_id, {"status": "invalid", "issues": json.dumps(result["issues"], ensure_ascii=False)})
        return {"ok": False, "issues": result["issues"], "detail": []}

    doc = result["parsed"]
    targets = doc.get("targets") or {}
    if env not in targets:
        raise ValueError(f"targets 中未声明环境 {env}")
    # 目标工作空间：按名字找，找不到就用当前工作空间
    target_name = (targets.get(env) or {}).get("workspace")
    target_ws = bundle["workspace_id"]
    if target_name:
        found = store.row("SELECT id FROM workspaces WHERE name = ?", (target_name,))
        if found:
            target_ws = found["id"]

    detail: list[dict] = []
    for kind, items in (doc.get("resources") or {}).items():
        if kind not in RESOURCE_KINDS or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            action, record = _apply(target_ws, kind, item)
            detail.append(
                {
                    "kind": kind,
                    "kind_label": RESOURCE_KINDS[kind]["label"],
                    "name": item.get(RESOURCE_KINDS[kind]["key"]),
                    "action": action,
                    "id": record.get("id"),
                }
            )

    counts = {
        "created": sum(1 for d in detail if d["action"] == "created"),
        "updated": sum(1 for d in detail if d["action"] == "updated"),
        "unchanged": sum(1 for d in detail if d["action"] == "unchanged"),
    }
    summary = f"{env} 发布完成：新建 {counts['created']}，更新 {counts['updated']}，未变 {counts['unchanged']}"
    _record(bundle_id, target_ws, env, "deploy", "success", summary, detail)
    store.update(
        "bundles",
        bundle_id,
        {"status": "valid", "issues": json.dumps(result["issues"], ensure_ascii=False), "updated_at": store.now()},
    )
    return {"ok": True, "env": env, "counts": counts, "detail": detail, "summary": summary}


def run(bundle_id: str, env: str = "dev", resource: str | None = None) -> dict:
    """触发 bundle 中声明的工作流（真实调用工作流引擎；缺失时降级）。"""
    bundle = get_bundle(bundle_id)
    if not bundle:
        raise ValueError("Bundle 不存在")
    doc = parse_yaml(bundle["yaml"]) or {}
    flows = ((doc.get("resources") or {}).get("flows") or []) if isinstance(doc, dict) else []
    names = [f.get("name") for f in flows if isinstance(f, dict) and f.get("name")]
    if resource:
        names = [n for n in names if n == resource]
    if not names:
        _record(bundle_id, bundle["workspace_id"], env, "run", "failed", "bundle 中没有可运行的工作流", [])
        return {"ok": False, "message": "bundle 中没有可运行的工作流", "runs": []}

    results: list[dict] = []
    for name in names:
        row = store.row(
            "SELECT id FROM flows WHERE workspace_id = ? AND name = ?", (bundle["workspace_id"], name)
        )
        if not row:
            results.append({"flow": name, "ok": False, "error": "工作流尚未发布到该环境"})
            continue
        try:
            from . import workflow as wf

            out = wf.run_flow(row["id"], trigger="cicd", params={"env": env})
            results.append({"flow": name, "ok": bool(out.get("ok", True)), "run_id": out.get("run_id")})
        except Exception as exc:  # noqa: BLE001
            results.append({"flow": name, "ok": False, "error": str(exc)})

    ok = all(r["ok"] for r in results)
    summary = f"{env} 触发 {len(results)} 个工作流，成功 {sum(1 for r in results if r['ok'])}"
    _record(bundle_id, bundle["workspace_id"], env, "run", "success" if ok else "failed", summary, results)
    return {"ok": ok, "env": env, "runs": results, "summary": summary}


def summary(bundle_id: str) -> dict:
    """各环境资源差异（drift）与部署历史。"""
    bundle = get_bundle(bundle_id)
    if not bundle:
        raise ValueError("Bundle 不存在")
    result = validate(bundle["yaml"], bundle["workspace_id"])
    doc = result.get("parsed") or {}
    declared: dict[str, set[str]] = {}
    for kind, items in (doc.get("resources") or {}).items():
        if kind in RESOURCE_KINDS and isinstance(items, list):
            key = RESOURCE_KINDS[kind]["key"]
            declared[kind] = {i.get(key) for i in items if isinstance(i, dict) and i.get(key)}

    envs: dict[str, dict] = {}
    for env in (doc.get("targets") or {}) or {e: {} for e in ENVS}:
        missing: list[dict] = []
        present = 0
        for kind, names in declared.items():
            for name in names:
                if _existing(bundle["workspace_id"], kind, name):
                    present += 1
                else:
                    missing.append({"kind": kind, "kind_label": RESOURCE_KINDS[kind]["label"], "name": name})
        last = store.row(
            "SELECT action, status, summary, created_at FROM bundle_deployments "
            "WHERE bundle_id = ? AND env = ? ORDER BY created_at DESC LIMIT 1",
            (bundle_id, env),
        )
        envs[env] = {
            "env": env,
            "declared": sum(len(v) for v in declared.values()),
            "present": present,
            "missing": missing,
            "drift": len(missing),
            "last_deploy": last,
        }

    history = store.rows(
        "SELECT * FROM bundle_deployments WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 30",
        (bundle_id,),
    )
    for h in history:
        h["log"] = _loads(h.get("log"), [])
    return {"bundle": {"id": bundle["id"], "name": bundle["name"], "status": bundle["status"]},
            "issues": result["issues"], "envs": envs, "history": history}


def destroy(bundle_id: str, env: str = "dev") -> dict:
    """删除该 bundle 在目标环境创建过的资源。

    只删「由 deploy 记录证明是本 bundle 建的」资源，避免误删人工创建的同名资源。
    """
    bundle = get_bundle(bundle_id)
    if not bundle:
        raise ValueError("Bundle 不存在")
    created: dict[tuple[str, str], str] = {}
    for row in store.rows(
        "SELECT * FROM bundle_deployments WHERE bundle_id = ? AND env = ? AND action = 'deploy' "
        "ORDER BY created_at",
        (bundle_id, env),
    ):
        for entry in _loads(row.get("log"), []):
            if entry.get("action") == "created" and entry.get("id"):
                created[(entry.get("kind"), entry.get("name"))] = entry["id"]

    removed: list[dict] = []
    for (kind, name), rid in created.items():
        spec = RESOURCE_KINDS.get(kind or "")
        if not spec:
            continue
        if store.row(f'SELECT id FROM "{spec["table"]}" WHERE id = ?', (rid,)):
            store.delete(spec["table"], rid)
            removed.append({"kind": kind, "kind_label": spec["label"], "name": name})

    summary_text = f"{env} 销毁完成：移除 {len(removed)} 项由本 bundle 创建的资源"
    _record(bundle_id, bundle["workspace_id"], env, "destroy", "success", summary_text, removed)
    return {"ok": True, "env": env, "removed": removed, "summary": summary_text}


def deployments(bundle_id: str) -> list[dict]:
    rows = store.rows(
        "SELECT * FROM bundle_deployments WHERE bundle_id = ? ORDER BY created_at DESC LIMIT 100",
        (bundle_id,),
    )
    for r in rows:
        r["log"] = _loads(r.get("log"), [])
    return rows
