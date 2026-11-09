"""数据接入：文件落盘 → 解析 → 建表 → 元数据登记。

XLSX 用 zipfile + ElementTree 直接解析（不引入 openpyxl），保证零额外依赖。
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import config, store, warehouse

SUPPORTED = {".csv", ".json", ".ndjson", ".jsonl", ".parquet", ".pq", ".xlsx"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def save_upload(filename: str, payload: bytes) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"暂不支持 {ext or '无扩展名'}，可用：{', '.join(sorted(SUPPORTED))}")
    config.ensure_dirs()
    safe = re.sub(r"[^\w.\-\u4e00-\u9fa5]", "_", Path(filename).stem)[:60] or "upload"
    target = config.UPLOAD_DIR / f"{store.new_id()}_{safe}{ext}"
    target.write_bytes(payload)
    return target


def sanitize_identifier(name: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5]", "_", (name or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "t_" + store.new_id()[:6]
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned[:64].lower()


# --------------------------------------------------------------------------- xlsx


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    idx = 0
    for ch in letters.group(1):
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def read_xlsx(path: Path) -> tuple[list[str], list[list[Any]]]:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise ValueError("工作簿里没有工作表")
        root = ET.fromstring(zf.read(sheets[0]))
        raw: list[list[Any]] = []
        for row in root.iter(f"{{{NS['m']}}}row"):
            cells: dict[int, Any] = {}
            for c in row.findall("m:c", NS):
                ref = c.get("r", "")
                ctype = c.get("t")
                v = c.find("m:v", NS)
                if ctype == "inlineStr":
                    is_el = c.find("m:is", NS)
                    text = "".join(t.text or "" for t in is_el.iter(f"{{{NS['m']}}}t")) if is_el is not None else ""
                    value: Any = text
                elif v is None or v.text is None:
                    value = None
                elif ctype == "s":
                    value = shared[int(v.text)] if int(v.text) < len(shared) else ""
                elif ctype in ("str", "e"):
                    value = v.text
                else:
                    try:
                        num = float(v.text)
                        value = int(num) if num.is_integer() else num
                    except ValueError:
                        value = v.text
                cells[_col_index(ref)] = value
            if cells:
                width = max(cells) + 1
                raw.append([cells.get(i) for i in range(width)])

    if not raw:
        raise ValueError("工作表为空")
    header = [str(h).strip() if h is not None else f"col_{i}" for i, h in enumerate(raw[0])]
    header = [sanitize_identifier(h) for h in header]
    body = [r + [None] * (len(header) - len(r)) for r in raw[1:]]
    return header, body


# --------------------------------------------------------------- type inference

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S")


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def infer_types(header: list[str], rows: list[list[Any]]) -> tuple[list[tuple[str, str]], list[list[Any]]]:
    """返回 (列定义, 转换后的行)。"""
    columns: list[tuple[str, str]] = []
    sample = rows[:2000]
    for i, name in enumerate(header):
        values = [r[i] for r in sample if i < len(r) and r[i] not in (None, "")]
        if not values:
            columns.append((name, "VARCHAR"))
            continue
        if all(isinstance(v, bool) for v in values):
            columns.append((name, "BOOLEAN"))
            continue
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            columns.append((name, "BIGINT"))
            continue
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            columns.append((name, "DOUBLE"))
            continue
        if all(_as_date(v) is not None for v in values):
            columns.append((name, "DATE"))
            continue
        if all(isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip() or "x") for v in values):
            columns.append((name, "BIGINT"))
            continue
        if all(isinstance(v, str) and re.fullmatch(r"-?\d*\.\d+", v.strip() or "x") for v in values):
            columns.append((name, "DOUBLE"))
            continue
        columns.append((name, "VARCHAR"))

    types = [t for _, t in columns]
    converted: list[list[Any]] = []
    for r in rows:
        out: list[Any] = []
        for i, dtype in enumerate(types):
            v = r[i] if i < len(r) else None
            if v == "":
                v = None
            if v is not None:
                try:
                    if dtype == "BIGINT":
                        v = int(str(v).strip()) if not isinstance(v, int) else v
                    elif dtype == "DOUBLE":
                        v = float(str(v).strip()) if not isinstance(v, (int, float)) else float(v)
                    elif dtype == "DATE":
                        v = _as_date(v)
                    elif dtype == "VARCHAR":
                        v = str(v)
                except (ValueError, TypeError):
                    v = None
            out.append(v)
        converted.append(out)
    return columns, converted


# ----------------------------------------------------------------------- ingest


def ingest_file(
    workspace_id: str,
    catalog: str,
    schema: str,
    table: str,
    path: Path,
    description: str = "",
    source_id: str | None = None,
) -> dict:
    table = sanitize_identifier(table)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        header, body = read_xlsx(path)
        columns, converted = infer_types(header, body)
        row_count = warehouse.create_from_rows(catalog, schema, table, columns, converted)
        file_columns = [{"name": n, "data_type": t} for n, t in columns]
    else:
        row_count = warehouse.create_from_file(catalog, schema, table, str(path))
        file_columns = warehouse.describe(catalog, schema, table)

    schema_row = store.row(
        "SELECT s.id FROM schemas s JOIN catalogs c ON c.id = s.catalog_id "
        "WHERE c.workspace_id = ? AND c.name = ? AND s.name = ?",
        (workspace_id, catalog, schema),
    )
    if not schema_row:
        raise ValueError("目标 schema 不存在")

    existing = store.row(
        "SELECT id FROM tables WHERE schema_id = ? AND name = ?", (schema_row["id"], table)
    )
    payload = {
        "schema_id": schema_row["id"],
        "name": table,
        "kind": "table",
        "description": description or f"由文件 {path.name} 导入",
        "row_count": row_count,
        "source_id": source_id,
        "created_at": store.now(),
        "updated_at": store.now(),
    }
    if existing:
        store.update("tables", existing["id"], payload)
        store.execute("DELETE FROM columns WHERE table_id = ?", (existing["id"],))
        table_id = existing["id"]
    else:
        payload["id"] = store.new_id()
        store.insert("tables", payload)
        table_id = payload["id"]

    for i, col in enumerate(file_columns):
        store.insert(
            "columns",
            {
                "id": store.new_id(),
                "table_id": table_id,
                "name": col["name"],
                "data_type": col.get("data_type") or col.get("type") or "VARCHAR",
                "ordinal": i,
                "is_nullable": 1 if col.get("is_nullable", True) else 0,
                "created_at": store.now(),
            },
        )
    return {"table": table, "row_count": row_count, "columns": len(file_columns), "table_id": table_id}
