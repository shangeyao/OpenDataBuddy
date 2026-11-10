// 数据质量：规则引擎 / 数据剖析 / 漂移检测 / 异常处理
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  confirmDialog, formModal, toast, drawer, esc, el, bindTabs,
} from './ui.js';

const SEVERITY_TONE = { info: 'blue', warning: 'orange', critical: 'red' };
const SEVERITY_LABEL = { info: '提示', warning: '告警', critical: '严重' };
const LEVEL_TONE = { stable: 'green', warning: 'orange', critical: 'red', unknown: '' };
const LEVEL_LABEL = { stable: '稳定', warning: '告警', critical: '严重', unknown: '未知' };
const KIND_TONE = { schema: 'orange', volume: 'red', distribution: 'orange', freshness: 'blue', rule: 'red' };
const KIND_LABEL = {
  schema: '结构漂移', volume: '行数异常', distribution: '分布漂移', freshness: '新鲜度', rule: '规则失败',
};

const severityTag = (s) => tag(SEVERITY_LABEL[s] || s || '-', SEVERITY_TONE[s] || '');
const levelTag = (l) => tag(LEVEL_LABEL[l] || l || '-', LEVEL_TONE[l] ?? '');
const kindTag = (k) => tag(KIND_LABEL[k] || k || '-', KIND_TONE[k] || '');

/* -------------------------------------------------------------- 小工具 */
function statCard(label, value, foot, unit = '') {
  return `<div class="stat"><span class="label">${esc(label)}</span>
    <span class="value">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ''}</span>
    <span class="foot">${foot || ''}</span></div>`;
}

function sparkline(points) {
  const vals = (points || []).map((p) => (p === null || p === undefined ? null : Number(p)));
  const known = vals.filter((v) => v !== null && Number.isFinite(v));
  if (!known.length) return '<span class="dim tiny">暂无趋势</span>';
  const W = 132, H = 30, pad = 3;
  const max = Math.max(...known, 0.0001);
  const step = vals.length > 1 ? (W - pad * 2) / (vals.length - 1) : 0;
  const seg = [];
  let started = false;
  vals.forEach((v, i) => {
    if (v === null || !Number.isFinite(v)) return;
    const x = pad + i * step;
    const y = H - pad - (v / max) * (H - pad * 2);
    seg.push(`${started ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`);
    started = true;
  });
  if (seg.length < 2) return '<span class="dim tiny">数据不足</span>';
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" style="display:block">
    <path d="${seg.join(' ')}" fill="none" stroke="#4f46e5" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
}

function barRow(label, value, max, tone = '') {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return `<div style="display:grid;grid-template-columns:96px 1fr 56px;gap:10px;align-items:center;margin-bottom:7px">
    <span class="tiny dim" style="text-align:right">${esc(label)}</span>
    <div style="height:8px;background:var(--sub);border-radius:4px;overflow:hidden">
      <div style="height:100%;width:${pct.toFixed(1)}%;background:${tone || 'var(--brand)'};border-radius:4px"></div></div>
    <span class="tiny mono" style="text-align:right">${esc(value)}</span></div>`;
}

/* -------------------------------------------------------------- 主入口 */
export async function renderQuality(page, ctx) {
  page.innerHTML = loading('加载质量概览…');

  const [summary, meta, ruleResp] = await Promise.all([
    api.get('/quality/summary'),
    api.get('/quality/meta').catch(() => ({ rule_types: [], severities: [], profile_modes: [], drift_metrics: [] })),
    api.get('/quality/rules'),
  ]);
  const tables = await api.get('/catalog/tables').catch(() => ({ tables: [] }));
  const tableList = tables.tables || [];

  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => importYaml(ctx) }, '导入 YAML'),
    el('button', { class: 'btn', onclick: () => exportYaml(ctx) }, '导出 YAML'),
    el('button', { class: 'btn primary', onclick: () => ruleForm(ctx, tableList, meta) }, '＋ 新建规则'),
  ]));

  const passRate = summary.today_pass_rate;
  const trendPoints = (summary.trend || []).map((t) => t.pass_rate);

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">Quality First</span>
      规则失败会写入异常清单与审计日志；标记为「阻断」的规则可被工作流作为前置检查调用，不通过就停下游。
      <span class="spacer"></span>
      <span class="dim tiny">6 类规则 · 3 种剖析模式 · 5 个漂移指标</span>
    </div>

    <div class="stat-row mt-16">
      ${statCard('规则总数', fmt.num(summary.rule_count), `启用 ${summary.enabled_count} 条 · 今日执行 ${summary.today_runs} 次`)}
      ${statCard('今日通过率', passRate === null || passRate === undefined ? '-' : fmt.pct(passRate),
        `通过 ${summary.today_passed} / 失败 ${summary.today_failed} / 异常 ${summary.today_errors}`)}
      ${statCard('失败规则', fmt.num(summary.failing_count), '最近一次执行未通过', '条')}
      ${statCard('未处理异常', fmt.num(summary.unacked_anomalies), '规则失败 + 自动巡检发现', '条')}
    </div>

    <div class="grid-2 mt-16">
      ${card('通过率趋势（近 7 天）', `
        <div class="row" style="gap:16px;align-items:flex-end">
          <div style="flex:1">${sparkline(trendPoints)}</div>
          <div style="text-align:right">
            <div class="tiny dim">最新</div>
            <div style="font-size:20px;font-weight:500">${passRate === null || passRate === undefined ? '-' : fmt.pct(passRate)}</div>
          </div>
        </div>
        <div class="tiny dim mt-12">${(summary.trend || []).map((t) => `${esc(t.date.slice(5))} ${t.pass_rate === null ? '-' : fmt.pct(t.pass_rate)}`).join(' · ')}</div>`,
      { sub: '按天统计 quality_rule_runs' })}
      ${card('规则类型分布', `
        <div>${Object.entries(summary.by_type || {}).map(([k, v]) => {
          const label = (meta.rule_types || []).find((t) => t.type === k)?.label || k;
          const max = Math.max(...Object.values(summary.by_type || { x: 1 }));
          return barRow(label, v, max);
        }).join('') || emptyState('还没有规则', '点击右上角「新建规则」开始')}</div>`,
      { sub: 'null / unique / range / enum / regex / sql' })}
    </div>

    <div class="card flush mt-16">
      <div class="tabs" data-tabs="quality">
        <button class="active" data-tab="rules">规则</button>
        <button data-tab="profile">剖析</button>
        <button data-tab="drift">漂移</button>
        <button data-tab="anomaly">异常</button>
      </div>
      <div id="q-tab-body" style="padding:16px 20px"></div>
    </div>`;

  const body = page.querySelector('#q-tab-body');
  const views = {
    rules: () => viewRules(body, ctx, tableList, meta, ruleResp),
    profile: () => viewProfile(body, ctx, tableList),
    drift: () => viewDrift(body, ctx, tableList, meta),
    anomaly: () => viewAnomaly(body, ctx),
  };
  bindTabs(page, (key) => views[key]());
  views.rules();
}

/* -------------------------------------------------------------- 规则 Tab */
async function viewRules(host, ctx, tableList, meta, prefetched) {
  host.innerHTML = loading('加载规则…');
  let resp = prefetched;
  if (!resp) resp = await api.get('/quality/rules');
  const rules = resp.rules || [];
  const types = resp.types || meta.rule_types || [];

  host.innerHTML = `
    <div class="row" style="margin-bottom:12px">
      <input class="input" id="q-rule-filter" placeholder="按表名 / 字段筛选" style="width:240px" />
      <span class="spacer"></span>
      <button class="btn sm" id="q-run-all">全部执行</button>
      <button class="btn sm" id="q-precheck">前置检查</button>
    </div>
    <div id="q-rule-table"></div>`;

  const draw = (keyword = '') => {
    const kw = keyword.trim().toLowerCase();
    const rows = rules.filter(
      (r) => !kw || String(r.table_ref).toLowerCase().includes(kw) || String(r.column_name || '').toLowerCase().includes(kw)
    );
    host.querySelector('#q-rule-table').innerHTML = dataTable({
      rows,
      columns: [
        {
          title: '目标',
          render: (r) => `<div class="cell-main mono">${esc(r.table_ref)}</div>
            <div class="cell-sub">${esc(r.column_name || '（整表）')}</div>`,
        },
        { title: '规则类型', render: (r) => tag(r.rule_type_label || r.rule_type, 'blue') },
        { title: '配置', render: (r) => `<span class="mono tiny">${esc(JSON.stringify(r.config || {}))}</span>` },
        { title: '严重级别', render: (r) => severityTag(r.severity) },
        { title: '阻断', render: (r) => (r.blocking ? tag('阻断', 'red') : '<span class="dim tiny">-</span>') },
        { title: '最近结果', render: (r) => (r.last_status ? statusTag(r.last_status) : '<span class="dim tiny">未执行</span>') },
        { title: '最近值', render: (r) => `<span class="mono tiny">${esc(r.last_value || '-')}</span>` },
        { title: '最近执行', render: (r) => `<span class="dim tiny">${fmt.ago(r.last_run_at)}</span>` },
        { title: '启用', render: (r) => (r.enabled ? tag('启用', 'green') : tag('停用')) },
        {
          title: '操作',
          key: 'ops',
          render: (r) => `<a class="btn-link" data-run="${esc(r.id)}">执行</a>
            <a class="btn-link" data-runs="${esc(r.id)}">历史</a>
            <a class="btn-link" data-toggle="${esc(r.id)}">${r.enabled ? '停用' : '启用'}</a>
            <a class="btn-link danger" data-del="${esc(r.id)}">删除</a>`,
        },
      ],
      emptyText: '还没有质量规则',
      emptyHint: '可以新建规则，或从 YAML 批量导入',
    });
  };
  draw();

  host.querySelector('#q-rule-filter').addEventListener('input', (e) => draw(e.target.value));

  host.querySelector('#q-run-all').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const out = await api.post('/quality/rules/run-all', {});
      toast(`执行完成：${out.passed}/${out.total} 通过`, out.failed || out.errors ? 'err' : 'ok');
      ctx.refresh();
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      e.target.disabled = false;
    }
  });

  host.querySelector('#q-precheck').addEventListener('click', () => precheckForm(ctx, tableList));

  host.querySelectorAll('[data-run]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        const out = await api.post(`/quality/rules/${b.dataset.run}/run`, {});
        toast(`${out.status === 'pass' ? '通过' : '未通过'}：${out.value || out.detail}`, out.status === 'pass' ? 'ok' : 'err');
        ctx.refresh();
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );

  host.querySelectorAll('[data-runs]').forEach((b) =>
    b.addEventListener('click', async () => {
      const rule = rules.find((r) => r.id === b.dataset.runs);
      const out = await api.get(`/quality/rules/${b.dataset.runs}/runs`);
      drawer({
        title: `执行历史 · ${rule?.table_ref || ''}.${rule?.column_name || ''}`,
        body: dataTable({
          rows: out.runs || [],
          columns: [
            { title: '时间', render: (r) => fmt.dt(r.ran_at) },
            { title: '结果', render: (r) => statusTag(r.status) },
            { title: '值', render: (r) => `<span class="mono tiny">${esc(r.value || '-')}</span>` },
            { title: '说明', render: (r) => `<span class="tiny">${esc(r.detail || '')}</span>` },
          ],
          emptyText: '还没有执行记录',
        }),
      });
    })
  );

  host.querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', async () => {
      const rule = rules.find((r) => r.id === b.dataset.toggle);
      await api.patch(`/quality/rules/${b.dataset.toggle}`, { enabled: !rule.enabled });
      toast(rule.enabled ? '规则已停用' : '规则已启用', 'ok');
      ctx.refresh();
    })
  );

  host.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除规则', '删除后该规则的历史执行记录会保留，但不再参与检查。确认删除？', '删除'))) return;
      await api.del(`/quality/rules/${b.dataset.del}`);
      toast('规则已删除', 'ok');
      ctx.refresh();
    })
  );
}

function ruleForm(ctx, tableList, meta) {
  const types = (meta.rule_types || []).map((t) => ({ value: t.type, label: `${t.label}（${t.type}）` }));
  formModal({
    title: '新建质量规则',
    wide: true,
    fields: [
      {
        label: '目标表', name: 'table_ref', type: 'select', required: true,
        options: [{ value: '', label: '请选择表' }, ...tableList.map((t) => ({ value: t.ref, label: t.ref }))],
      },
      { label: '字段名', name: 'column_name', placeholder: '整表级规则（如自定义 SQL）可留空' },
      { label: '规则类型', name: 'rule_type', type: 'select', options: types.length ? types : [{ value: 'null', label: '空值（null）' }] },
      {
        label: '配置 JSON', name: 'config_text', type: 'textarea', rows: 6,
        value: '{"max_rate": 0.02, "severity": "warning", "blocking": false}',
        hint: '示例：null → {"max_rate":0.02}；range → {"min":0,"max":1000}；enum → {"values":["a","b"]}；regex → {"pattern":"^1\\d{10}$"}；sql → {"sql":"SELECT count(*) FROM t WHERE x<0"}',
      },
    ],
    onSubmit: async (d) => {
      let config = {};
      try {
        config = d.config_text ? JSON.parse(d.config_text) : {};
      } catch (err) {
        toast('配置不是合法 JSON', 'err');
        return false;
      }
      await api.post('/quality/rules', {
        table_ref: d.table_ref,
        column_name: d.column_name,
        rule_type: d.rule_type,
        config,
      });
      toast('规则已创建', 'ok');
      ctx.refresh();
    },
  });
}

function precheckForm(ctx, tableList) {
  formModal({
    title: '前置阻断检查',
    fields: [
      {
        label: '目标表', name: 'table_ref', type: 'select', required: true,
        options: [{ value: '', label: '请选择表' }, ...tableList.map((t) => ({ value: t.ref, label: t.ref }))],
      },
    ],
    submitText: '开始检查',
    onSubmit: async (d) => {
      const out = await api.post('/quality/precheck', { table_ref: d.table_ref });
      const rows = [...(out.reasons || []), ...(out.warnings || [])];
      drawer({
        title: `前置检查 · ${out.table_ref}`,
        body: `
          <div class="row" style="margin-bottom:14px;gap:12px">
            <span>${out.blocked ? tag('已阻断', 'red') : tag('可放行', 'green')}</span>
            <span class="dim tiny">检查 ${out.checked} 条阻断级规则，通过 ${out.passed} 条</span>
          </div>
          ${rows.length
            ? tableHTML(['类型', '字段', '结果', '级别', '说明'], rows.map((r) => [
                r.rule_type, r.column_name || '-', r.status, r.severity, r.detail,
              ]), { raw: [2] })
            : emptyState('没有阻断级规则', '给规则加上 "blocking": true 才会参与前置检查')}`,
      });
    },
  });
}

/* -------------------------------------------------------------- YAML */
function importYaml(ctx) {
  formModal({
    title: '导入 YAML 规则',
    wide: true,
    fields: [
      {
        label: 'YAML 内容', name: 'yaml', type: 'textarea', rows: 14, required: true,
        value: `version: 1
rules:
  - table: demo.retail.fact_orders
    column: amount
    type: range
    severity: warning
    blocking: true
    config:
      min: 0
      max: 1000000
  - table: demo.retail.fact_orders
    column: order_id
    type: unique
    config: {max_dup: 0}`,
      },
      { label: '覆盖同名表的旧规则', name: 'replace', type: 'checkbox' },
    ],
    submitText: '导入',
    onSubmit: async (d) => {
      const out = await api.post('/quality/rules/import', { yaml: d.yaml, replace: !!d.replace });
      toast(`导入 ${out.created} 条规则${out.skipped?.length ? `，跳过 ${out.skipped.length} 条` : ''}`, out.skipped?.length ? 'err' : 'ok');
      if (out.skipped?.length) {
        drawer({
          title: '导入跳过明细',
          body: tableHTML(['表', '原因'], out.skipped.map((s) => [s.table_ref, s.reason])),
        });
      }
      ctx.refresh();
    },
  });
}

async function exportYaml(ctx) {
  try {
    const out = await api.get('/quality/rules/export');
    drawer({
      title: '导出 YAML',
      wide: true,
      body: `<div class="tiny dim" style="margin-bottom:8px">共 ${(out.yaml || '').split('\n').filter((l) => l.trim().startsWith('- table')).length} 条规则，可直接复制或保存为 .yaml 再导入</div>
        <pre class="code" style="max-height:60vh;overflow:auto">${esc(out.yaml || '')}</pre>`,
      footer: '<button class="btn" id="q-copy-yaml">复制</button>',
    });
    const btn = document.querySelector('#q-copy-yaml');
    if (btn) {
      btn.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(out.yaml || '');
          toast('已复制到剪贴板', 'ok');
        } catch {
          toast('复制失败，请手动选择', 'err');
        }
      });
    }
  } catch (err) {
    toast(err.message, 'err');
  }
}

/* -------------------------------------------------------------- 剖析 Tab */
async function viewProfile(host, ctx, tableList) {
  const modes = [
    { mode: 'snapshot', label: '单次快照', desc: '全表统计，最准' },
    { mode: 'timeseries', label: '时间分桶', desc: '按时间列观察趋势' },
    { mode: 'inference', label: '推理采样', desc: '只采样不落全量' },
  ];
  host.innerHTML = `
    <div class="row" style="margin-bottom:14px;gap:8px;flex-wrap:wrap">
      <select class="input" id="q-prof-table" style="width:280px">
        <option value="">选择要剖析的表</option>
        ${tableList.map((t) => `<option value="${esc(t.ref)}">${esc(t.ref)}</option>`).join('')}
      </select>
      <select class="input" id="q-prof-mode" style="width:150px">
        ${modes.map((m) => `<option value="${m.mode}">${m.label}</option>`).join('')}
      </select>
      <button class="btn primary" id="q-prof-run">开始剖析</button>
      <span class="spacer"></span>
      <button class="btn sm" id="q-baseline-save" disabled>存为基线</button>
      <button class="btn sm" id="q-baseline-list">基线列表</button>
    </div>
    <div class="tiny dim" style="margin-bottom:12px">${modes.map((m) => `<b>${m.label}</b>：${m.desc}`).join(' · ')}</div>
    <div id="q-prof-result">${emptyState('选择一张表开始剖析', '剖析结果可用于建立基线，供漂移检测对比')}</div>`;

  let lastProfile = null;

  host.querySelector('#q-prof-run').addEventListener('click', async (e) => {
    const table_ref = host.querySelector('#q-prof-table').value;
    const mode = host.querySelector('#q-prof-mode').value;
    if (!table_ref) {
      toast('请先选择表', 'err');
      return;
    }
    const result = host.querySelector('#q-prof-result');
    result.innerHTML = loading('正在剖析…');
    e.target.disabled = true;
    try {
      const out = await api.post('/profiling/profile', { table_ref, mode });
      lastProfile = out;
      host.querySelector('#q-baseline-save').disabled = false;
      result.innerHTML = renderProfile(out);
    } catch (err) {
      result.innerHTML = card('剖析失败', `<div class="tag red">${esc(err.message)}</div>`);
    } finally {
      e.target.disabled = false;
    }
  });

  host.querySelector('#q-baseline-save').addEventListener('click', async () => {
    if (!lastProfile) return;
    await api.post('/profiling/baselines', {
      table_ref: lastProfile.table_ref,
      mode: lastProfile.mode,
      stats: lastProfile,
    });
    toast('基线已保存', 'ok');
  });

  host.querySelector('#q-baseline-list').addEventListener('click', async () => {
    const out = await api.get('/profiling/baselines');
    drawer({
      title: '剖析基线',
      wide: true,
      body: dataTable({
        rows: out.baselines || [],
        columns: [
          { title: '表', render: (b) => `<span class="mono tiny">${esc(b.table_ref)}</span>` },
          { title: '模式', render: (b) => tag(b.mode, 'blue') },
          { title: '行数', align: 'right', render: (b) => fmt.num(b.row_count) },
          { title: '列数', align: 'right', render: (b) => fmt.num(b.column_count) },
          { title: '列预览', render: (b) => `<span class="tiny dim">${esc((b.column_names || []).join(', '))}</span>` },
          { title: '采集时间', render: (b) => `<span class="dim tiny">${fmt.dt(b.captured_at)}</span>` },
          { title: '操作', key: 'ops', render: (b) => `<a class="btn-link danger" data-bdel="${esc(b.id)}">删除</a>` },
        ],
        emptyText: '还没有基线',
        emptyHint: '剖析完成后点击「存为基线」',
      }),
    });
    document.querySelectorAll('[data-bdel]').forEach((btn) =>
      btn.addEventListener('click', async () => {
        await api.del(`/profiling/baselines/${btn.dataset.bdel}`);
        toast('基线已删除', 'ok');
        btn.closest('tr')?.remove();
      })
    );
  });
}

function renderProfile(out) {
  const hist = (col) => {
    if (!col.histogram?.bins?.length) return '';
    const max = Math.max(...col.histogram.bins.map((b) => b.count), 1);
    return `<div style="display:flex;align-items:flex-end;gap:1px;height:26px;margin-top:6px">
      ${col.histogram.bins.map((b) => `<div title="${fmt.num(b.lo, 2)} ~ ${fmt.num(b.hi, 2)}：${b.count}"
        style="flex:1;height:${Math.max(2, (b.count / max) * 26)}px;background:var(--brand);opacity:.55;border-radius:1px"></div>`).join('')}
    </div>`;
  };

  const colRow = (c) => {
    const q = c.quantiles
      ? ['p25', 'p50', 'p75', 'p90', 'p99'].map((k) => `${k}=${fmt.num(c.quantiles[k], 2)}`).join(' · ')
      : '';
    const top = (c.top_values || []).slice(0, 3).map((t) => `${esc(t.value)}(${fmt.pct(t.ratio)})`).join(' · ');
    return `<tr>
      <td><div class="cell-main mono">${esc(c.name)}</div><div class="cell-sub">${esc(c.data_type || '')}</div></td>
      <td>${tag(c.kind, 'blue')}</td>
      <td class="num">${fmt.num(c.count)}</td>
      <td class="num">${c.null_rate === null || c.null_rate === undefined ? '-' : fmt.pct(c.null_rate)}</td>
      <td class="num">${fmt.num(c.distinct)}</td>
      <td class="mono tiny">${c.min !== undefined ? `${fmt.num(c.min, 2)} ~ ${fmt.num(c.max, 2)}` : '-'}</td>
      <td class="mono tiny">${c.mean !== undefined ? `${fmt.num(c.mean, 2)} ± ${fmt.num(c.std, 2)}` : '-'}</td>
      <td class="mono tiny">${esc(q) || '-'}</td>
      <td class="tiny">${top || '-'}${hist(c)}</td>
    </tr>`;
  };

  const series = out.series?.length
    ? `<div class="mt-16">${card(`时间分桶（${esc(out.time_column || '')} · ${esc(out.granularity || '')}）`, tableHTML(
        ['时间桶', '行数', ...(out.numeric_columns || []).slice(0, 4).map((c) => `${c} 均值`)],
        out.series.slice(0, 40).map((s) => [
          s.bucket, fmt.num(s.total), ...(out.numeric_columns || []).slice(0, 4).map((c) => fmt.num(s.columns?.[c]?.avg, 2)),
        ]),
        { max: 40 }
      ), { sub: `共 ${out.bucket_count} 个桶`, flush: false })}</div>`
    : '';

  return `
    <div class="row" style="margin-bottom:12px;gap:10px;flex-wrap:wrap">
      <span class="mono">${esc(out.table_ref)}</span>
      ${tag(out.mode_label || out.mode, 'blue')}
      ${out.sampled ? tag(`采样 ${out.sample_rows} 行`, 'orange') : tag('全量', 'green')}
      <span class="dim tiny">行数 ${fmt.num(out.row_count)} · 列数 ${out.column_count} · 耗时 ${out.elapsed_ms} ms</span>
    </div>
    ${(out.warnings || []).length ? `<div class="tag orange" style="margin-bottom:10px">${esc(out.warnings.join('；'))}</div>` : ''}
    <div class="table-wrap"><table class="tbl">
      <thead><tr>
        <th>字段</th><th>类型</th><th class="num">行数</th><th class="num">空值率</th>
        <th class="num">去重值</th><th>范围</th><th>均值 ± 标准差</th><th>分位数</th><th>TopN / 直方图</th>
      </tr></thead>
      <tbody>${out.columns.map(colRow).join('')}</tbody>
    </table></div>
    ${series}`;
}

/* -------------------------------------------------------------- 漂移 Tab */
async function viewDrift(host, ctx, tableList, meta) {
  const metrics = meta.drift_metrics || [];
  host.innerHTML = `
    <div class="row" style="margin-bottom:14px;gap:8px;flex-wrap:wrap">
      <select class="input" id="q-drift-table" style="width:260px">
        <option value="">选择表</option>
        ${tableList.map((t) => `<option value="${esc(t.ref)}">${esc(t.ref)}</option>`).join('')}
      </select>
      <select class="input" id="q-drift-metric" style="width:180px">
        <option value="">自动（按列类型）</option>
        ${metrics.map((m) => `<option value="${m.metric}">${m.label}</option>`).join('')}
      </select>
      <button class="btn primary" id="q-drift-batch">批量检测所有列</button>
      <span class="spacer"></span>
      <button class="btn sm" id="q-drift-anomaly">零配置巡检</button>
    </div>
    <div class="grid-2" style="margin-bottom:14px">
      ${card('5 个漂移指标', `<div class="tiny" style="line-height:1.9">
        ${(metrics.length ? metrics : [
          { label: 'KS 统计量', desc: '经验 CDF 最大差', thresholds: { warn: 0.1, crit: 0.2 } },
          { label: 'PSI', desc: '群体稳定性', thresholds: { warn: 0.1, crit: 0.25 } },
          { label: 'Wasserstein', desc: '推土机距离', thresholds: { warn: 0.1, crit: 0.25 } },
          { label: '卡方', desc: '类别分布差异', thresholds: { warn: 0.1, crit: 0.2 } },
          { label: 'JS 散度', desc: '对称 KL', thresholds: { warn: 0.1, crit: 0.2 } },
        ]).map((m) => `<div><b>${esc(m.label)}</b>：${esc(m.desc || '')}
          <span class="dim">阈值 ${m.thresholds?.warn} / ${m.thresholds?.crit}</span></div>`).join('')}
      </div>`, { sub: 'Go 原生实现' })}
      ${card('怎么用', `<div class="tiny muted" style="line-height:1.9">
        1. 先在「剖析」Tab 给表存一条基线<br>
        2. 回到这里批量检测，或对单列指定指标<br>
        3. 分数超过阈值即判定漂移，严重级别分 stable / warning / critical<br>
        4. 「零配置巡检」会顺带检查结构变更、行数突增突减、分布漂移与数据新鲜度</div>`)}
    </div>
    <div id="q-drift-result">${emptyState('选择表后开始检测', '漂移检测需要先有剖析基线')}</div>`;

  const runBatch = async () => {
    const table_ref = host.querySelector('#q-drift-table').value;
    if (!table_ref) {
      toast('请先选择表', 'err');
      return;
    }
    const result = host.querySelector('#q-drift-result');
    result.innerHTML = loading('正在对比基线…');
    try {
      const out = await api.post('/drift/batch', { table_ref });
      result.innerHTML = `
        <div class="row" style="margin-bottom:12px;gap:10px">
          ${out.drifted_count ? tag(`${out.drifted_count} 列漂移`, 'orange') : tag('全部稳定', 'green')}
          ${out.critical_count ? tag(`${out.critical_count} 列严重`, 'red') : ''}
          <span class="dim tiny">共 ${out.total} 列 · 基线 ${esc(String(out.baseline_id).slice(0, 8))}</span>
        </div>
        ${dataTable({
          rows: out.columns || [],
          columns: [
            { title: '字段', render: (r) => `<span class="mono">${esc(r.column)}</span>` },
            { title: '类型', render: (r) => (r.kind ? tag(r.kind, 'blue') : '-') },
            { title: '指标', render: (r) => esc(r.metric_label || r.metric || '-') },
            { title: '分数', align: 'right', render: (r) => `<span class="mono">${r.score === null || r.score === undefined ? '-' : fmt.num(r.score, 4)}</span>` },
            { title: '阈值', align: 'right', render: (r) => `<span class="dim mono tiny">${r.threshold ?? '-'}</span>` },
            { title: '结论', render: (r) => (r.error ? tag('检测失败', 'red') : levelTag(r.level)) },
            { title: '说明', render: (r) => `<span class="tiny dim">${esc(r.error || (r.notes || []).join('；') || '-')}</span>` },
          ],
          emptyText: '基线中没有可检测的列',
        })}`;
    } catch (err) {
      result.innerHTML = card('检测失败', `<div class="tag red">${esc(err.message)}</div>
        <div class="tiny dim mt-12">漂移检测依赖剖析基线，请先到「剖析」Tab 采集并保存基线。</div>`);
    }
  };
  host.querySelector('#q-drift-batch').addEventListener('click', runBatch);

  host.querySelector('#q-drift-anomaly').addEventListener('click', async () => {
    const table_ref = host.querySelector('#q-drift-table').value;
    if (!table_ref) {
      toast('请先选择表', 'err');
      return;
    }
    const result = host.querySelector('#q-drift-result');
    result.innerHTML = loading('零配置巡检中…');
    try {
      const out = await api.post('/drift/anomalies', { table_ref });
      result.innerHTML = `
        <div class="row" style="margin-bottom:12px;gap:10px;flex-wrap:wrap">
          ${out.finding_count ? tag(`发现 ${out.finding_count} 项异常`, 'red') : tag('未发现异常', 'green')}
          ${out.baseline_created ? tag('已自动建立基线', 'blue') : ''}
          <span class="dim tiny">基线 ${fmt.num(out.baseline_row_count)} 行 → 当前 ${fmt.num(out.current_row_count)} 行</span>
        </div>
        ${out.findings.length
          ? tableHTML(['类型', '级别', '字段', '说明'], out.findings.map((f) => [
              KIND_LABEL[f.kind] || f.kind, SEVERITY_LABEL[f.severity] || f.severity, f.column || '（整表）', f.detail,
            ]), { raw: [0, 1] })
          : emptyState('未发现异常', '结构、行数、分布、新鲜度都正常')}`;
      if (out.finding_count) toast(`巡检完成，发现 ${out.finding_count} 项异常`, 'err');
    } catch (err) {
      result.innerHTML = card('巡检失败', `<div class="tag red">${esc(err.message)}</div>`);
    }
  });
}

/* -------------------------------------------------------------- 异常 Tab */
async function viewAnomaly(host, ctx) {
  host.innerHTML = loading('加载异常…');
  const load = async () => {
    const out = await api.get('/anomalies');
    const rows = out.anomalies || [];
    host.innerHTML = `
      <div class="row" style="margin-bottom:12px;gap:10px">
        <span class="dim tiny">共 ${rows.length} 条，其中未处理 ${out.unacked} 条</span>
        <span class="spacer"></span>
        <select class="input" id="q-anom-filter" style="width:140px">
          <option value="">全部</option>
          <option value="0">未处理</option>
          <option value="1">已处理</option>
        </select>
        <button class="btn sm" id="q-anom-reload">刷新</button>
      </div>
      <div id="q-anom-table"></div>`;

    const draw = (filter = '') => {
      const list = filter === '' ? rows : rows.filter((a) => String(a.acknowledged ? 1 : 0) === filter);
      host.querySelector('#q-anom-table').innerHTML = dataTable({
        rows: list,
        columns: [
          { title: '发现时间', render: (a) => `<span class="dim tiny">${fmt.dt(a.detected_at)}</span>` },
          { title: '类型', render: (a) => kindTag(a.kind) },
          { title: '级别', render: (a) => severityTag(a.severity) },
          { title: '表', render: (a) => `<span class="mono tiny">${esc(a.table_ref)}</span>` },
          { title: '字段', render: (a) => esc(a.column_name || '（整表）') },
          { title: '说明', render: (a) => `<span class="tiny">${esc(a.detail || '')}</span>` },
          {
            title: '状态',
            render: (a) => (a.acknowledged ? tag('已处理', 'green') : tag('未处理', 'orange')),
          },
          {
            title: '操作',
            key: 'ops',
            render: (a) => (a.acknowledged ? '<span class="dim tiny">-</span>' : `<a class="btn-link" data-ack="${esc(a.id)}">标记已处理</a>`),
          },
        ],
        emptyText: '没有异常记录',
        emptyHint: '规则失败或零配置巡检发现的问题都会出现在这里',
      });

      host.querySelectorAll('[data-ack]').forEach((b) =>
        b.addEventListener('click', async () => {
          await api.post(`/anomalies/${b.dataset.ack}/ack`, {});
          toast('已标记为处理', 'ok');
          await load();
        })
      );
    };
    draw();
    host.querySelector('#q-anom-filter').addEventListener('change', (e) => draw(e.target.value));
    host.querySelector('#q-anom-reload').addEventListener('click', load);
  };
  await load();
}
