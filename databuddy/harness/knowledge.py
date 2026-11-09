"""知识检索：文档知识库 + Catalog 元数据混合召回。

不引向量库 —— 中文用 bigram 切分 + IDF 加权的倒排，本地够用且零依赖。
接向量库时只需替换 `search()`，其余调用方不用改。
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .. import catalog, store

CJK = re.compile(r"[\u4e00-\u9fa5]+")
LATIN = re.compile(r"[a-z0-9_]{2,}")


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set(LATIN.findall(text))
    for chunk in CJK.findall(text):
        if len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


def search(workspace_id: str, query: str, top_k: int = 5) -> list[dict]:
    docs = store.rows(
        "SELECT * FROM knowledge_docs WHERE workspace_id = ?", (workspace_id,)
    )
    if not docs or not (query or "").strip():
        return []
    q_tokens = tokenize(query)
    if not q_tokens:
        return []

    df: Counter = Counter()
    doc_tokens: dict[str, set[str]] = {}
    for d in docs:
        toks = tokenize(d["title"] + " " + d["content"] + " " + (d.get("tags") or ""))
        doc_tokens[d["id"]] = toks
        for t in toks:
            df[t] += 1

    total = len(docs)
    scored = []
    for d in docs:
        toks = doc_tokens[d["id"]]
        score = 0.0
        for t in q_tokens:
            if t in toks:
                score += math.log(1 + total / (1 + df[t]))
        if score <= 0:
            continue
        # 标题命中加权
        title_tokens = tokenize(d["title"])
        score += 1.2 * len(q_tokens & title_tokens)
        scored.append({**d, "score": round(score, 3)})

    scored.sort(key=lambda x: -x["score"])
    results = scored[:top_k]
    for r in results:
        r["snippet"] = _snippet(r["content"], q_tokens)
    return results


def _snippet(content: str, q_tokens: set[str], width: int = 160) -> str:
    best_pos = 0
    best_hits = -1
    for i in range(0, max(len(content) - 2, 1), 20):
        window = content[i : i + 60]
        hits = len(tokenize(window) & q_tokens)
        if hits > best_hits:
            best_hits, best_pos = hits, i
    start = max(0, best_pos - 20)
    return content[start : start + width].replace("\n", " ")


def search_tables(workspace_id: str, query: str, top_k: int = 6) -> list[dict]:
    tables = catalog.all_tables(workspace_id)
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    scored = []
    for t in tables:
        text = f"{t['name']} {t.get('description') or ''}"
        cols = " ".join(c["name"] + " " + (c.get("description") or "") for c in catalog.columns_of(t["id"]))
        toks = tokenize(text + " " + cols)
        score = len(q_tokens & toks)
        if score:
            scored.append({**t, "score": score})
    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


def context_block(workspace_id: str, query: str, top_k: int = 3) -> str:
    """给 system prompt 用的知识片段。"""
    parts: list[str] = []
    docs = search(workspace_id, query, top_k)
    if docs:
        parts.append("【知识库检索结果】")
        for d in docs:
            parts.append(f"- 《{d['title']}》({d.get('tags') or '通用'}): {d['snippet']}")
    tables = search_tables(workspace_id, query, top_k=4)
    if tables:
        parts.append("【相关数据表】")
        for t in tables:
            parts.append(f"- {t['ref']}：{t.get('description') or '无描述'}（{t['row_count']} 行）")
    return "\n".join(parts)


def rebuild_catalog_docs(workspace_id: str) -> int:
    """把表结构生成知识条目，便于「这张表是干什么的」这类问题。"""
    store.execute(
        "DELETE FROM knowledge_docs WHERE workspace_id = ? AND source_type = 'catalog'",
        (workspace_id,),
    )
    count = 0
    for t in catalog.all_tables(workspace_id):
        cols = catalog.columns_of(t["id"])
        col_text = "，".join(
            f"{c['name']}({c['data_type']}{'，' + c['description'] if c.get('description') else ''})"
            for c in cols
        )
        store.insert(
            "knowledge_docs",
            {
                "id": store.new_id(),
                "workspace_id": workspace_id,
                "title": f"表结构：{t['ref']}",
                "content": f"{t.get('description') or '无描述'}。字段：{col_text}。"
                           f"当前约 {t['row_count']} 行。",
                "source_type": "catalog",
                "tags": "元数据",
                "scope": "workspace",
                "created_at": store.now(),
                "updated_at": store.now(),
            },
        )
        count += 1
    return count
