// 产物渲染：工具结果 → 可视化卡片
import { fmt } from './api.js';
import { tableHTML, esc, levelBadge } from './ui.js';
import { chartHTML } from './charts.js';

function sqlBlock(sql) {
  if (!sql) return '';
  return `<details style="margin-top:8px"><summary class="tiny muted" style="cursor:pointer">查看生成的 SQL</summary>
    <pre class="code" style="margin-top:8px">${esc(sql)}</pre></details>`;
}

function wrap(title, bodyHTML, extraHead = '') {
  return `<div class="artifact">
    <div class="artifact-head"><strong>${esc(title)}</strong>${extraHead}</div>
    <div class="artifact-body">${bodyHTML}</div>
  </div>`;
}

export function artifactHTML(a) {
  if (!a || !a.type) return '';
  switch (a.type) {
    case 'query': {
      const chart = chartHTML(a.chart, a.columns, a.rows);
      const table = tableHTML(a.columns, a.rows, { max: 12 });
      return wrap(
        a.title ? `${a.title} · 查询结果` : '查询结果',
        `${chart}${table}${sqlBlock(a.sql)}`,
        `<span class="tiny muted" style="margin-left:auto">${a.rows ? a.rows.length : 0} 行</span>`
      );
    }
    case 'table_schema': {
      const rows = (a.columns || []).map((c) => [
        c.name,
        c.data_type,
        (c.tags || []).map((t) => `${t.tag}(${t.level})`).join('、') || '-',
        c.description || '-',
      ]);
      return wrap(`表结构 · ${a.table}`, tableHTML(['字段', '类型', '标签', '说明'], rows));
    }
    case 'table_list':
      return wrap(
        '数据资产',
        `<div class="grid-3">${(a.items || [])
          .map(
            (t) => `<div style="border:1px solid var(--line);border-radius:9px;padding:10px">
              <div class="mono" style="font-size:12px">${esc(t.ref)}</div>
              <div class="tiny muted">${fmt.num(t.rows)} 行 · ${esc(t.description || '无描述')}</div></div>`
          )
          .join('')}</div>`
      );
    case 'sensitive_scan': {
      const rows = (a.findings || []).map((f) => [
        f.column,
        `${f.tag} · ${f.level}`,
        f.confidence?.toFixed?.(2) ?? f.confidence,
        f.evidence,
        f.sample || '-',
      ]);
      const body = rows.length
        ? tableHTML(['字段', '分级', '置信度', '判定依据', '脱敏样例'], rows)
        : `<div class="empty" style="padding:20px">未发现敏感字段</div>`;
      return wrap(`敏感字段扫描 · ${a.table}`, body);
    }
    case 'quality_results': {
      const rows = (a.results || []).map((r) => [
        r.table || '-',
        r.column || '-',
        r.rule_type,
        r.status === 'pass' ? '通过' : r.status === 'fail' ? '失败' : r.status,
        r.detail || '',
      ]);
      const failed = (a.results || []).filter((r) => r.status !== 'pass').length;
      return wrap(
        '质量检查结果',
        tableHTML(['表', '字段', '规则', '结果', '详情'], rows),
        `<span class="badge ${failed ? 'err' : 'ok'}" style="margin-left:auto">${failed ? `${failed} 项未通过` : '全部通过'}</span>`
      );
    }
    case 'quality_suggestions': {
      const rows = (a.items || []).map((s) => [s.column_name, s.rule_type, JSON.stringify(s.config), s.reason]);
      return wrap(`质量规则建议 · ${a.table}`, tableHTML(['字段', '规则', '参数', '理由'], rows));
    }
    case 'profile': {
      const head = `<div class="stat-strip" style="margin-bottom:12px">
        <div class="item"><div class="kpi"><span class="label">行数</span><span class="value" style="font-size:20px">${fmt.num(a.row_count)}</span></div></div>
        <div class="item"><div class="kpi"><span class="label">字段数</span><span class="value" style="font-size:20px">${a.column_count}</span></div></div>
        <div class="item"><div class="kpi"><span class="label">敏感字段</span><span class="value" style="font-size:20px">${(a.pii_columns || []).length}</span></div></div>
        <div class="item"><div class="kpi"><span class="label">质量得分</span><span class="value" style="font-size:20px">${a.quality_score === null ? '-' : fmt.pct(a.quality_score)}</span></div></div>
      </div>`;
      const rows = (a.columns || []).map((c) => [
        c.name, c.data_type, fmt.num(c.total), fmt.num(c.nulls), `${((c.null_rate || 0) * 100).toFixed(2)}%`,
        fmt.num(c.distinct_cnt), (c.tags || []).join('、') || '-',
      ]);
      return wrap(`资产盘点 · ${a.table}`, head + tableHTML(['字段', '类型', '总行', '空值', '空值率', '去重', '标签'], rows, { max: 20 }));
    }
    case 'flow_run': {
      const rows = (a.log || []).map((e) => [
        e.name, e.status === 'success' ? '成功' : e.status === 'failed' ? '失败' : e.status,
        e.rows ?? '-', e.message || '', e.elapsed_ms ? `${e.elapsed_ms}ms` : '-',
      ]);
      return wrap(`工作流运行 · ${a.flow}`, tableHTML(['节点', '状态', '行数', '信息', '耗时'], rows));
    }
    case 'dashboard': {
      return wrap(
        `看板已生成 · ${a.name}`,
        `<div class="row" style="justify-content:space-between">
          <span class="tiny muted">${(a.cards || []).length} 张卡片</span>
          <a class="btn sm" href="#/dashboards" data-open-board="${esc(a.dashboard_id)}">打开看板 →</a>
        </div>
        <div class="tiny muted" style="margin-top:8px">${(a.cards || []).map((c) => esc(c.title)).join('、')}</div>`
      );
    }
    case 'knowledge':
      return wrap(
        '知识库命中',
        (a.items || [])
          .map((d) => `<div style="margin-bottom:10px"><strong style="font-size:12.8px">${esc(d.title)}</strong>
            <div class="tiny muted" style="margin-top:2px">${esc(d.snippet)}</div></div>`)
          .join('')
      );
    case 'notice': {
      const tone = a.level === 'warning' ? 'warn' : 'accent';
      return `<div class="approval" style="background:var(--${tone === 'warn' ? 'warn' : 'accent'}-soft);border-color:transparent">
        <h4 style="color:var(--${tone === 'warn' ? 'warn' : 'accent'})">${esc(a.title || '提示')}</h4>
        <div class="tiny" style="margin-top:4px">${esc(a.text || '')}</div></div>`;
    }
    default:
      return '';
  }
}

export function traceHTML(trace) {
  if (!trace || !trace.length) return '';
  return `<div class="trace">${trace
    .map(
      (t) => `<span class="trace-chip ${t.ok ? '' : 'fail'}" title="${esc(JSON.stringify(t.args || {}))}">
        <span class="dot"></span>${esc(t.tool)}${t.duration_ms ? `<span class="ms">${t.duration_ms}ms</span>` : ''}
      </span>`
    )
    .join('')}</div>`;
}

export function engineBadge(meta) {
  if (!meta) return '';
  if (meta.engine === 'rule') return `<span class="badge warn">规则引擎</span>`;
  if (meta.engine === 'system') return `<span class="badge">系统</span>`;
  const model = meta.model || 'LLM';
  const usage = meta.usage || {};
  const tokens = usage.total_tokens || (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
  return `<span class="badge accent">${esc(model)}</span>${tokens ? `<span class="tiny muted">${tokens} tokens</span>` : ''}`;
}
