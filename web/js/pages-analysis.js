// W8 数据分析：智能问数 / 主动归因 / AI 报告（定时推送）/ AI 生成看板 / 分析空间嵌出
import { api, fmt } from './api.js';
import {
  card, dataTable, tag, loading, emptyState, tableHTML,
  confirmDialog, formModal, toast, drawer, esc, descList, el, md,
  tabsHTML, bindTabs, kpi,
} from './ui.js';
import { chartHTML } from './charts.js';

/* ------------------------------------------------------------------ 工具 */

const TABS = [
  { key: 'ask', label: '智能问数' },
  { key: 'attr', label: '主动归因' },
  { key: 'report', label: 'AI 报告' },
  { key: 'board', label: 'AI 生成看板' },
  { key: 'embed', label: '分析空间嵌出' },
];

const EXAMPLES = [
  '上个月各渠道GMV',
  '近 30 天 GMV 趋势',
  '上月各品类订单量',
  '上个月各城市成交客户数',
  '上月各渠道退款率',
];

// styles.css 里只有 .stat .foot .up/.down 有配色，表格内用行内样式，避免改别人的样式文件
const trendColor = (v) => (v > 0 ? 'var(--success)' : v < 0 ? 'var(--error)' : 'var(--text-2)');
const trendStyle = (v) => `color:${trendColor(v)};font-variant-numeric:tabular-nums`;
const arrow = (v) => (v > 0 ? '▲' : v < 0 ? '▼' : '—');
const signed = (v, digits = 2) => `${v >= 0 ? '+' : ''}${fmt.num(v, digits)}`;

/** 报告 / 运行状态的中文标签（ui.js 的 statusTag 没有 ready/draft/skipped 这几种） */
const STATUS_LABELS = {
  ready: ['blue', '就绪'],
  draft: ['', '草稿'],
  success: ['green', '成功'],
  failed: ['red', '失败'],
  running: ['blue', '运行中'],
  skipped: ['', '已跳过'],
  disabled: ['', '已停用'],
};
function reportStatus(status) {
  const [tone, label] = STATUS_LABELS[status] || ['', status || '-'];
  return `<span class="tag ${tone}">${esc(label)}</span>`;
}

/** 百分比展示（后端给的是小数） */
function pct(v, digits = 1) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '-';
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

/** 周期 key → 中文标签（报告里存的是 key） */
function periodLabel(key, meta) {
  const hit = (meta?.periods || []).find((p) => p.value === key);
  return hit ? hit.label : key || '-';
}

/**
 * 按后端给的 formats 把单元格转成展示文本。
 * tableHTML 只会把数字按千分位渲染，比率列（贡献度 / 环比）是 0~1 小数，
 * 必须在这里先转成 "70.6%"，否则会显示成 0.71。
 */
function cellText(v, format) {
  if (v === null || v === undefined) return '-';
  if (format === 'pct') return pct(v);
  if (format === 'money') return fmt.money(v);
  if (format === 'int') return fmt.num(v, 0);
  return v;
}

/** 明细表格：把 formats 指定的列转成字符串，其余交给 tableHTML 原样渲染 */
function detailTable(columns, rows, formats, opts) {
  const idx = Object.keys(formats || {}).map(Number);
  if (!idx.length) return tableHTML(columns, rows, opts);
  const projected = rows.map((r) => {
    const copy = r.slice();
    for (const i of idx) copy[i] = cellText(copy[i], formats[i]);
    return copy;
  });
  // 已是字符串，禁用数字右对齐逻辑
  return tableHTML(columns, projected, { ...opts, raw: idx });
}

/** 渲染一张「图表 + 可展开明细」的卡片体 */
function chartBody(chartData, { maxRows = 8, height } = {}) {
  if (!chartData) return '<div class="dim tiny">无图表数据</div>';
  const { spec, columns, rows, formats } = chartData;
  const svg = chartHTML(spec, columns, rows, height ? { height } : {});
  const detail = rows && rows.length
    ? `<details class="mt-8"><summary class="tiny dim" style="cursor:pointer">查看明细（${rows.length} 行）</summary>
        <div class="mt-8">${detailTable(columns, rows, formats, { max: maxRows })}</div></details>`
    : '';
  return `${svg || '<div class="dim tiny">该结果不适合可视化</div>'}${detail}`;
}

/** 结果集 → 后端 chart_data 结构（兼容只有 columns/rows 的场景） */
function asChartData(obj) {
  if (!obj) return null;
  if (obj.chart_data) return obj.chart_data;
  if (obj.spec && obj.columns) return obj;
  return null;
}

/* ================================================================ 主入口 */

export async function renderAnalysis2(page, ctx) {
  page.innerHTML = loading('加载分析元数据…');
  let meta;
  try {
    meta = await api.get('/analysis/meta');
  } catch (err) {
    page.innerHTML = card('分析模块不可用', `<div class="tag red">加载失败</div>
      <div class="tiny mt-8">${esc(err.message || String(err))}</div>`);
    return;
  }

  const metrics = meta.metrics || [];
  const periods = meta.periods || [];
  const dims = meta.dimensions || [];
  const summary = meta.summary || {};

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn sm', onclick: () => ctx.refresh() }, '刷新'),
      el('button', { class: 'btn primary', onclick: () => quickReport(meta, ctx) }, '＋ 新建报告'),
    ])
  );

  if (!metrics.length) {
    page.innerHTML = `
      <div class="info-strip">
        <span class="tag blue">AI 分析</span>
        分析能力全部建立在<b>语义层指标</b>之上：指标口径统一，问数与报告才不会一次一个数。
      </div>
      <div class="mt-16">${card('还没有可用指标', emptyState(
        '语义层里还没有指标',
        '先到「统一语义层」注册指标（如 gmv = sum(amount)），再回来问数'
      ), { flush: true })}</div>`;
    return;
  }

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">AI 分析</span>
      问数、归因、报告、看板全部走<b>语义层指标编译</b>，结论由真实数据算出，不调外部大模型，不会编数字。
      <span class="spacer"></span>
      <span class="dim tiny">${metrics.length} 个指标 · ${dims.length} 个维度 · 已生成 ${summary.reports || 0} 份报告</span>
    </div>

    <div class="grid-4 mt-16">
      ${kpi('可用指标', String(metrics.length), '<span class="dim">语义层已发布</span>')}
      ${kpi('可用维度', String(dims.length), '<span class="dim">支持归因与拆解</span>')}
      ${kpi('报告总数', String(summary.reports || 0), `<span class="dim">运行 ${summary.runs || 0} 次</span>`)}
      ${kpi('定时推送', String(summary.schedules || 0), summary.alerts_module
        ? '<span class="dim">告警渠道已接通</span>'
        : '<span class="tag orange" style="font-size:11px">告警模块未就绪，仅落库</span>')}
    </div>

    <div class="mt-16" id="an-tabs">${tabsHTML(TABS, 'ask', 'analysis')}</div>
    <div class="mt-16" id="an-body"></div>`;

  const host = page.querySelector('#an-body');
  const views = {
    ask: () => renderAsk(host, meta),
    attr: () => renderAttribute(host, meta),
    report: () => renderReports(host, meta, ctx),
    board: () => renderDashboardGen(host, meta, ctx),
    embed: () => renderEmbed(host, ctx),
  };
  bindTabs(page.querySelector('#an-tabs'), (key) => {
    if (views[key]) views[key]();
  });
  views.ask();
}

/* ============================================================ 1) 智能问数 */

function renderAsk(host, meta) {
  host.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>智能问数</h3><div class="sub">用业务语言提问，系统先认指标再编译 SQL</div></div>
        <span class="tag outline">指标路径优先</span>
      </div>
      <div class="row" style="gap:8px">
        <input id="ask-q" class="grow" placeholder="例如：上个月各渠道GMV"
          style="flex:1;height:36px;padding:0 12px;border:1px solid var(--border);border-radius:4px;font-size:13px" />
        <select id="ask-period" style="height:36px;padding:0 8px;border:1px solid var(--border);border-radius:4px">
          <option value="">自动识别时间</option>
          ${(meta.periods || []).map((p) => `<option value="${esc(p.value)}">${esc(p.label)}</option>`).join('')}
        </select>
        <button class="btn primary" id="ask-go">提问</button>
      </div>
      <div class="row mt-12" style="gap:6px;flex-wrap:wrap">
        <span class="dim tiny">试试：</span>
        ${EXAMPLES.map((q) => `<button class="btn sm ghost" data-example="${esc(q)}">${esc(q)}</button>`).join('')}
      </div>
    </div>
    <div class="mt-16" id="ask-out"></div>`;

  const input = host.querySelector('#ask-q');
  const out = host.querySelector('#ask-out');
  const periodSel = host.querySelector('#ask-period');

  host.querySelectorAll('[data-example]').forEach((b) =>
    b.addEventListener('click', () => {
      input.value = b.dataset.example;
      run();
    })
  );
  host.querySelector('#ask-go').addEventListener('click', run);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') run();
  });

  async function run() {
    const question = input.value.trim();
    if (!question) {
      toast('请先输入问题', 'err');
      return;
    }
    out.innerHTML = card('', loading('正在识别指标并编译 SQL…'), { flush: true });
    let res;
    try {
      res = await api.post('/analysis/ask', {
        question,
        period: periodSel.value || null,
      });
    } catch (err) {
      out.innerHTML = card('问数失败', `<div class="tag red">${esc(err.message || '请求失败')}</div>`);
      return;
    }
    renderAskResult(out, res, question);
  }

  // 首次进入自动跑一个示例，避免空页
  input.value = EXAMPLES[0];
  run();
}

function renderAskResult(out, res, question) {
  const engineTag =
    res.engine === 'metric'
      ? '<span class="tag green">语义层指标</span>'
      : res.engine === 'fallback'
        ? '<span class="tag orange">降级匹配</span>'
        : '<span class="tag">未匹配</span>';

  const chartData = asChartData(res);
  const hasRows = (res.rows || []).length > 0;

  out.innerHTML = `
    <div class="grid-2">
      <div class="col" style="gap:16px">
        ${card('答案', `<div class="prose">${md(res.answer || '（无结论）')}</div>`, {
          sub: question,
          actions: engineTag,
        })}
        ${card('推理过程', `<div class="prose tiny" style="line-height:1.9">${md(res.reasoning || '-')}</div>`, {
          sub: '为什么这样查：命中了什么指标 / 维度 / 时间范围',
        })}
      </div>
      <div class="col" style="gap:16px">
        ${card('结果', hasRows ? chartBody(chartData, { maxRows: 12 }) : emptyState('没有查到数据', '换个时间范围或换个问法试试'), {
          sub: res.row_count != null ? `${res.row_count} 行 · ${res.elapsed_ms || 0} ms` : '',
        })}
        ${card('生成的 SQL', `<pre class="code dark" style="white-space:pre-wrap;word-break:break-all;margin:0">${esc(res.sql || '（未生成 SQL）')}</pre>`, {
          sub: '只读查询 · 已过安全护栏',
        })}
      </div>
    </div>`;
}

/* ============================================================ 2) 主动归因 */

function renderAttribute(host, meta) {
  const metricOpts = (meta.metrics || []).map((m) => ({
    value: m.name,
    label: `${m.display_name || m.name}（${m.unit || '无单位'}）`,
  }));

  host.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>主动智能归因</h3><div class="sub">按维度拆本期 / 上期差值，贡献度排序告诉你「变化是谁带来的」</div></div>
        <span class="tag outline">本期 vs 上期</span>
      </div>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <select id="at-metric" style="height:36px;padding:0 8px;border:1px solid var(--border);border-radius:4px">
          ${metricOpts.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}
        </select>
        <select id="at-dim" style="height:36px;padding:0 8px;border:1px solid var(--border);border-radius:4px"></select>
        <select id="at-period" style="height:36px;padding:0 8px;border:1px solid var(--border);border-radius:4px">
          ${(meta.periods || []).map((p) => `<option value="${esc(p.value)}"${p.value === 'last_month' ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}
        </select>
        <button class="btn primary" id="at-go">开始归因</button>
      </div>
    </div>
    <div class="mt-16" id="at-out"></div>`;

  const metricSel = host.querySelector('#at-metric');
  const dimSel = host.querySelector('#at-dim');
  const periodSel = host.querySelector('#at-period');
  const out = host.querySelector('#at-out');

  const fillDims = () => {
    const m = (meta.metrics || []).find((x) => x.name === metricSel.value);
    const dims = (m?.dimensions || []).filter((d) => d !== 'order_date' && d !== '__date__');
    dimSel.innerHTML = dims.length
      ? dims.map((d) => {
          const label = (meta.dimensions || []).find((x) => x.name === d)?.label || d;
          return `<option value="${esc(d)}">按${esc(label)}</option>`;
        }).join('')
      : '<option value="">（该指标无业务维度）</option>';
  };
  fillDims();
  metricSel.addEventListener('change', fillDims);
  host.querySelector('#at-go').addEventListener('click', run);

  async function run() {
    if (!dimSel.value) {
      toast('该指标没有可用于归因的业务维度', 'err');
      return;
    }
    out.innerHTML = loading('正在按维度对比本期与上期…');
    let res;
    try {
      res = await api.post('/analysis/attribute', {
        metric: metricSel.value,
        dimension: dimSel.value,
        period: periodSel.value,
      });
    } catch (err) {
      out.innerHTML = card('归因失败', `<div class="tag red">${esc(err.message || '请求失败')}</div>`);
      return;
    }
    renderAttributeResult(out, res);
  }

  run();
}

function renderAttributeResult(out, res) {
  const growth = res.growth;
  const contribs = res.contributions || [];
  const label = res.metric?.display_name || res.metric?.name || '指标';
  const unit = res.metric?.unit || '';
  const dimLabel = res.dimension_label || '维度';

  const bars = contribs
    .map((c) => {
      const pctText = pct(c.share);
      const width = Math.min(Math.abs(c.share) * 100, 100);
      const positive = c.delta >= 0;
      return `<div style="margin-bottom:10px">
        <div class="row" style="justify-content:space-between;font-size:12px;margin-bottom:3px">
          <span><b>${esc(c.dim_value)}</b>
            <span class="dim tiny" style="margin-left:6px">${fmt.money(c.previous)} → ${fmt.money(c.current)}</span></span>
          <span style="${trendStyle(c.delta)}">
            ${signed(c.delta)} ${esc(unit)}（贡献 ${pctText}）</span>
        </div>
        <div style="height:8px;background:var(--sub);border-radius:4px;overflow:hidden">
          <div style="height:100%;width:${width}%;border-radius:4px;background:${positive ? 'var(--success)' : 'var(--error)'};opacity:.8"></div>
        </div>
      </div>`;
    })
    .join('');

  out.innerHTML = `
    <div class="grid-4">
      ${kpi('本期 ' + label, fmt.money(res.total_current), `<span class="dim">${esc(res.period?.label || '')}</span>`, unit)}
      ${kpi('上期 ' + label, fmt.money(res.total_previous), `<span class="dim">${esc(res.period?.prev_start || '')} ~ ${esc(res.period?.prev_end || '')}</span>`, unit)}
      ${kpi('变化量', signed(res.total_delta),
        `<span class="foot"><span style="${trendStyle(growth)}">${arrow(growth)} ${pct(growth)}</span></span>`, unit)}
      ${kpi('主要驱动', res.driver || '-', `<span class="dim">按${esc(dimLabel)}</span>`)}
    </div>

    <div class="grid-2 mt-16">
      ${card(`贡献度排序（按${esc(dimLabel)}）`, bars || emptyState('没有可归因的分组'), {
        sub: `条形长度 = |贡献度|，绿色为拉动、红色为拖累`,
      })}
      ${card('归因结论', `<div class="prose">${md(res.conclusion || '-')}</div>`, {
        sub: `${esc(res.period?.start || '')} ~ ${esc(res.period?.end || '')} vs 上期`,
      })}
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">贡献度明细</span>
        <span class="dim tiny">贡献度 = 该维差值 / 整体差值</span></div>
      ${dataTable({
        rows: contribs,
        rowKey: 'dim_value',
        columns: [
          { title: '#', width: '44px', render: (c) => `<span class="dim">${c.rank}</span>` },
          { title: dimLabel, render: (c) => `<b>${esc(c.dim_value)}</b>` },
          { title: '本期', align: 'right', render: (c) => fmt.num(c.current, 2) },
          { title: '上期', align: 'right', render: (c) => `<span class="dim">${fmt.num(c.previous, 2)}</span>` },
          {
            title: '差值', align: 'right',
            render: (c) => `<span style="${trendStyle(c.delta)}">${signed(c.delta)}</span>`,
          },
          { title: '贡献度', align: 'right', render: (c) => pct(c.share) },
          { title: '分组环比', align: 'right', render: (c) => (c.growth == null ? '-' : `<span style="${trendStyle(c.growth)}">${arrow(c.growth)} ${pct(c.growth)}</span>`) },
        ],
        emptyText: '没有可归因的分组',
      })}
    </div>`;
}

/* ============================================================ 3) AI 报告 */

function renderReports(host, meta, ctx) {
  host.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>AI 报告</h3><div class="sub">概览 → 指标拆解 → 异常归因 → 建议，全部由真实数据填充</div></div>
        <button class="btn primary sm" id="rp-new">＋ 生成新报告</button>
      </div>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <input id="rp-topic" placeholder="报告主题，如 零售经营分析"
          style="flex:1;min-width:200px;height:36px;padding:0 12px;border:1px solid var(--border);border-radius:4px;font-size:13px" />
        <select id="rp-period" style="height:36px;padding:0 8px;border:1px solid var(--border);border-radius:4px">
          ${(meta.periods || []).map((p) => `<option value="${esc(p.value)}"${p.value === 'last_month' ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}
        </select>
        <button class="btn primary" id="rp-gen">生成报告</button>
      </div>
      <div class="mt-12">
        <div class="tiny dim" style="margin-bottom:6px">选择指标（可多选，默认取前 4 个）</div>
        <div class="row" style="gap:6px;flex-wrap:wrap" id="rp-metrics">
          ${(meta.metrics || []).map((m, i) => `
            <label class="tag outline" style="cursor:pointer;padding:4px 8px">
              <input type="checkbox" value="${esc(m.name)}"${i < 3 ? ' checked' : ''} style="margin-right:4px" />
              ${esc(m.display_name || m.name)}
            </label>`).join('')}
        </div>
      </div>
    </div>
    <div class="mt-16" id="rp-out"></div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">已保存的报告</span>
        <span class="dim tiny" id="rp-count"></span>
        <span class="spacer"></span>
        <button class="btn sm" id="rp-reload">刷新</button></div>
      <div id="rp-list">${loading()}</div>
    </div>`;

  const out = host.querySelector('#rp-out');
  const listHost = host.querySelector('#rp-list');
  const topicInput = host.querySelector('#rp-topic');
  const periodSel = host.querySelector('#rp-period');
  const metricsHost = host.querySelector('#rp-metrics');

  const pickedMetrics = () =>
    Array.from(metricsHost.querySelectorAll('input:checked')).map((n) => n.value);

  host.querySelector('#rp-new').addEventListener('click', () => {
    topicInput.value = topicInput.value || '经营分析';
    host.querySelector('#rp-gen').click();
  });
  host.querySelector('#rp-reload').addEventListener('click', () => loadList());
  host.querySelector('#rp-gen').addEventListener('click', generate);
  loadList();

  async function generate() {
    const metrics = pickedMetrics();
    if (!metrics.length) {
      toast('至少选择一个指标', 'err');
      return;
    }
    out.innerHTML = loading('正在计算各指标本期 / 上期并生成章节…');
    let res;
    try {
      res = await api.post('/analysis/report/generate', {
        topic: topicInput.value.trim() || '经营分析',
        metrics,
        period: periodSel.value,
      });
    } catch (err) {
      out.innerHTML = card('生成失败', `<div class="tag red">${esc(err.message || '请求失败')}</div>`);
      return;
    }
    renderReportPreview(out, res, { onSave: () => saveReport(res), onSchedule: () => scheduleReport(res, meta) });
  }

  async function saveReport(res) {
    try {
      const created = await api.post('/reports', {
        name: res.title,
        topic: res.topic,
        metrics: (res.metrics || []).map((m) => m.name),
        period: res.period?.key || 'last_month',
        sections: res.sections,
        summary: res.summary,
        description: '由 AI 报告生成',
      });
      toast('报告已保存', 'ok');
      loadList();
      return created.report;
    } catch (err) {
      toast(err.message || '保存失败', 'err');
      return null;
    }
  }

  async function scheduleReport(res, meta) {
    const saved = await saveReport(res);
    if (!saved) return;
    newSchedule(saved, meta, loadList);
  }

  async function loadList() {
    listHost.innerHTML = loading();
    let data;
    try {
      data = await api.get('/reports');
    } catch (err) {
      listHost.innerHTML = `<div class="tag red">加载失败：${esc(err.message || '')}</div>`;
      return;
    }
    const reports = data.reports || [];
    host.querySelector('#rp-count').textContent = `${reports.length} 份`;
    listHost.innerHTML = dataTable({
      rows: reports,
      columns: [
        {
          title: '报告',
          render: (r) => `<div class="cell-main">${esc(r.name)}</div>
            <div class="cell-sub">${esc(r.topic || '')} · ${(r.metrics || []).map((m) => esc(m)).join(' / ')}</div>`,
        },
        { title: '周期', width: '90px', render: (r) => tag(periodLabel(r.period, meta), 'outline') },
        { title: '状态', width: '90px', render: (r) => reportStatus(r.status) },
        {
          title: '最近运行',
          render: (r) => (r.last_run
            ? `<div>${reportStatus(r.last_run.status)}<span class="dim tiny" style="margin-left:6px">${fmt.ago(r.last_run.started_at)}</span></div>`
            : '<span class="dim">未运行</span>'),
        },
        {
          title: '定时',
          width: '130px',
          render: (r) => (r.schedule
            ? `<span class="tag ${r.schedule.enabled ? 'green' : ''}" title="${esc(r.schedule.cron)}">${esc(r.schedule.schedule_text || r.schedule.cron)}</span>`
            : '<span class="dim tiny">未设置</span>'),
        },
        {
          title: '操作',
          key: 'ops',
          render: (r) => `<a class="btn-link" data-view="${esc(r.id)}">查看</a>
            <a class="btn-link" data-run="${esc(r.id)}">运行</a>
            <a class="btn-link" data-sched="${esc(r.id)}">定时</a>
            <a class="btn-link danger" data-del="${esc(r.id)}">删除</a>`,
        },
      ],
      emptyText: '还没有保存的报告',
      emptyHint: '先生成一份报告，再点「保存」或「定时推送」',
    });

    listHost.querySelectorAll('[data-view]').forEach((b) =>
      b.addEventListener('click', () => viewReport(b.dataset.view))
    );
    listHost.querySelectorAll('[data-run]').forEach((b) =>
      b.addEventListener('click', async () => {
        b.textContent = '运行中…';
        try {
          const res = await api.post(`/reports/${b.dataset.run}/run`, {});
          toast(`报告已生成（${(res.run.sections || []).length} 章节）`, 'ok');
          loadList();
          viewReport(b.dataset.run);
        } catch (err) {
          toast(err.message || '运行失败', 'err');
          b.textContent = '运行';
        }
      })
    );
    listHost.querySelectorAll('[data-sched]').forEach((b) =>
      b.addEventListener('click', () => {
        const report = reports.find((r) => r.id === b.dataset.sched);
        newSchedule(report, meta, loadList);
      })
    );
    listHost.querySelectorAll('[data-del]').forEach((b) =>
      b.addEventListener('click', async () => {
        if (!(await confirmDialog('删除报告', '报告及其运行历史会一并删除，确认？', '删除'))) return;
        await api.del(`/reports/${b.dataset.del}`);
        toast('已删除', 'ok');
        loadList();
      })
    );
  }

  async function viewReport(id) {
    let data;
    try {
      data = await api.get(`/reports/${id}`);
    } catch (err) {
      toast(err.message || '加载失败', 'err');
      return;
    }
    const r = data.report;
    const runs = r.runs || [];
    const schedules = r.schedules || [];
    drawer({
      title: r.name,
      wide: true,
      body: `<div class="col" style="gap:16px">
        ${descList([
          ['主题', r.topic || '-'],
          ['周期', periodLabel(r.period, meta)],
          ['状态', reportStatus(r.status)],
          ['创建人', r.owner || '-'],
          ['创建时间', fmt.dt(r.created_at)],
          ['运行次数', String(runs.length)],
        ])}
        <div>${(r.sections || []).map((s) => renderSection(s)).join('') || '<div class="dim">该报告还没有内容，点「运行」生成一次</div>'}</div>
        ${schedules.length ? `<div>
          <div class="tiny dim" style="margin-bottom:8px">定时推送</div>
          <div class="row" style="gap:6px;flex-wrap:wrap">
            ${schedules.map((s) => `<span class="tag ${s.enabled ? 'green' : ''}">
              ${esc(s.schedule_text || s.cron)} · ${esc((s.channels || []).map((c) => c.name || c.type).join('/') || '无渠道')}
            </span>`).join('')}
          </div>
        </div>` : ''}
        ${runs.length ? `<div>
          <div class="tiny dim" style="margin-bottom:8px">运行历史</div>
          ${dataTable({
            rows: runs,
            columns: [
              { title: '时间', render: (x) => `<span class="dim">${fmt.dt(x.started_at)}</span>` },
              { title: '触发', render: (x) => tag(x.trigger === 'schedule' ? '定时' : '手动', 'outline') },
              { title: '状态', render: (x) => reportStatus(x.status) },
              { title: '章节', align: 'right', render: (x) => (x.sections || []).length },
              {
                title: '渠道',
                render: (x) => (x.channels || []).length
                  ? (x.channels || []).map((c) => `<span class="tag ${c.ok ? 'green' : 'orange'}" style="margin-right:4px" title="${esc(c.detail || '')}">${esc(c.channel)}${c.simulated ? '(模拟)' : ''}</span>`).join('')
                  : '<span class="dim tiny">未推送</span>',
              },
            ],
            emptyText: '没有运行记录',
          })}
        </div>` : ''}
      </div>`,
    });
  }
}

function renderSection(s) {
  return `<div class="card" style="margin-bottom:12px">
    <div class="card-head"><div><h3>${esc(s.heading || '')}</h3></div></div>
    <div class="prose">${md(s.body || '')}</div>
    ${s.chart && (s.chart.rows || []).length ? `<div class="mt-12">${chartBody(s.chart, { maxRows: 10 })}</div>` : ''}
  </div>`;
}

function renderReportPreview(out, res, { onSave, onSchedule }) {
  out.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>${esc(res.title)}</h3><div class="sub">${esc(res.summary || '')}</div></div>
        <div class="row" style="gap:8px">
          <button class="btn sm" id="rp-save">保存报告</button>
          <button class="btn sm primary" id="rp-sched">设定时推送</button>
        </div>
      </div>
      <div class="row" style="gap:6px;flex-wrap:wrap">
        ${(res.metrics || []).map((m) => `<span class="tag blue">${esc(m.display_name || m.name)}</span>`).join('')}
        <span class="tag outline">${esc(res.period?.label || '')}（${esc(res.period?.start || '')} ~ ${esc(res.period?.end || '')}）</span>
        <span class="dim tiny">生成于 ${fmt.dt(res.generated_at)}</span>
      </div>
    </div>
    <div class="mt-16">${(res.sections || []).map((s) => renderSection(s)).join('')}</div>`;

  out.querySelector('#rp-save').addEventListener('click', onSave);
  out.querySelector('#rp-sched').addEventListener('click', onSchedule);
}

function newSchedule(report, meta, onDone) {
  if (!report) {
    toast('请先选择报告', 'err');
    return;
  }
  formModal({
    title: '设定时推送',
    intro: `<div class="tiny dim" style="margin-bottom:12px">报告：${esc(report.name)}</div>`,
    fields: [
      {
        label: '推送频率', name: 'cron', type: 'select', value: '0 9 * * *',
        options: [
          { value: '0 9 * * *', label: '每天 09:00' },
          { value: '30 8 * * 1', label: '每周一 08:30' },
          { value: '0 9 1 * *', label: '每月 1 日 09:00' },
          { value: '0 18 * * 5', label: '每周五 18:00' },
        ],
      },
      {
        label: '推送渠道', name: 'channel', type: 'select', value: 'email',
        options: [
          { value: 'email', label: '邮件' },
          { value: 'wecom', label: '企业微信' },
          { value: 'slack', label: 'Slack' },
          { value: 'teams', label: 'Microsoft Teams' },
          { value: 'webhook', label: 'Webhook' },
        ],
      },
      { label: '接收目标', name: 'target', placeholder: '邮箱 / 群机器人 key / webhook 地址', hint: '留空则只落库不推送' },
      { label: '启用', name: 'enabled', type: 'checkbox', value: true },
    ],
    onSubmit: async (d) => {
      const channels = d.target ? [{ type: d.channel, target: d.target }] : [];
      await api.post('/report-schedules', {
        report_id: report.id,
        cron: d.cron,
        channels,
        enabled: !!d.enabled,
      });
      toast('定时推送已设定', 'ok');
      if (onDone) onDone();
    },
  });
}

function quickReport(meta, ctx) {
  formModal({
    title: '新建报告',
    wide: true,
    fields: [
      { label: '报告主题', name: 'topic', required: true, placeholder: '例如 零售经营分析' },
      {
        label: '统计周期', name: 'period', type: 'select', value: 'last_month',
        options: (meta.periods || []).map((p) => ({ value: p.value, label: p.label })),
      },
      {
        label: '指标', name: 'metrics', placeholder: '逗号分隔，留空取默认',
        hint: (meta.metrics || []).slice(0, 6).map((m) => m.name).join(' / '),
      },
    ],
    onSubmit: async (d) => {
      const metrics = String(d.metrics || '').split(/[,，、\s]+/).filter(Boolean);
      const res = await api.post('/analysis/report/generate', {
        topic: d.topic, metrics, period: d.period,
      });
      await api.post('/reports', {
        name: res.title, topic: res.topic,
        metrics: (res.metrics || []).map((m) => m.name),
        period: res.period?.key || d.period,
        sections: res.sections, summary: res.summary,
        description: '由 AI 报告生成',
      });
      toast('报告已生成并保存', 'ok');
      ctx.refresh();
    },
  });
}

/* ======================================================== 4) AI 生成看板 */

function renderDashboardGen(host, meta, ctx) {
  host.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>AI 生成看板</h3><div class="sub">一句话描述想看什么，自动抽指标与维度并配好卡片</div></div>
        <span class="tag outline">预跑真实数据</span>
      </div>
      <div class="row" style="gap:8px">
        <input id="bd-p" placeholder="例如：做一个渠道GMV看板"
          style="flex:1;height:36px;padding:0 12px;border:1px solid var(--border);border-radius:4px;font-size:13px" />
        <select id="bd-period" style="height:36px;padding:0 8px;border:1px solid var(--border);border-radius:4px">
          ${(meta.periods || []).map((p) => `<option value="${esc(p.value)}"${p.value === 'last_month' ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}
        </select>
        <button class="btn primary" id="bd-go">生成看板</button>
      </div>
      <div class="row mt-12" style="gap:6px;flex-wrap:wrap">
        <span class="dim tiny">试试：</span>
        ${['做一个渠道GMV看板', '各品类订单量看板', '渠道客单价看板', '城市成交客户数看板'].map((q) =>
          `<button class="btn sm ghost" data-bd-example="${esc(q)}">${esc(q)}</button>`).join('')}
      </div>
    </div>
    <div class="mt-16" id="bd-out"></div>`;

  const input = host.querySelector('#bd-p');
  const periodSel = host.querySelector('#bd-period');
  const out = host.querySelector('#bd-out');

  host.querySelectorAll('[data-bd-example]').forEach((b) =>
    b.addEventListener('click', () => {
      input.value = b.dataset.bdExample;
      run();
    })
  );
  host.querySelector('#bd-go').addEventListener('click', run);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') run();
  });

  async function run() {
    const prompt = input.value.trim();
    if (!prompt) {
      toast('请先描述想看什么', 'err');
      return;
    }
    out.innerHTML = loading('正在识别指标与维度…');
    let res;
    try {
      res = await api.post('/analysis/dashboard/generate', { prompt, period: periodSel.value });
    } catch (err) {
      out.innerHTML = card('生成失败', `<div class="tag red">${esc(err.message || '请求失败')}</div>`);
      return;
    }
    renderBoardPreview(out, res, ctx);
  }

  input.value = '做一个渠道GMV看板';
  run();
}

function renderBoardPreview(out, res, ctx) {
  const cards = res.cards || [];
  out.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h3>${esc(res.name)}</h3><div class="sub">${esc(res.reasoning || '')}</div></div>
        <div class="row" style="gap:8px">
          <span class="tag outline">${cards.length} 张卡片</span>
          <button class="btn primary sm" id="bd-create">一键创建看板</button>
        </div>
      </div>
      <div class="row" style="gap:6px;flex-wrap:wrap">
        ${(res.metrics || []).map((m) => `<span class="tag blue">${esc(m.display_name || m.name)}</span>`).join('')}
        ${(res.dimensions || []).map((d) => `<span class="tag outline">按${esc(d.label || d.name)}</span>`).join('')}
        <span class="dim tiny">${esc(res.window || '')}</span>
      </div>
    </div>
    <div class="grid-2 mt-16">
      ${cards.map((c) => card(c.title, chartBody(
        { spec: c.chart, columns: c.columns, rows: c.rows },
        { maxRows: 8 }
      ), { sub: `${(c.rows || []).length} 行 · ${esc(c.chart_type || '')}` })).join('') || emptyState('没有生成卡片', '换个描述试试')}
    </div>`;

  out.querySelector('#bd-create').addEventListener('click', async () => {
    try {
      await api.post('/dashboards', {
        name: res.name,
        description: res.description || '由 AI 生成',
        cards: cards.map((c) => ({ title: c.title, sql: c.sql, chart: c.chart })),
      });
      toast('看板已创建，可在「仪表盘」查看', 'ok');
      if (ctx?.navigate) ctx.navigate('#/dashboards');
    } catch (err) {
      toast(err.message || '创建失败', 'err');
    }
  });
}

/* ==================================================== 5) 分析空间嵌出 */

function renderEmbed(host, ctx) {
  host.innerHTML = loading('加载分析空间…');

  (async () => {
    let spaces = [];
    try {
      const data = await api.get('/analysis-spaces');
      spaces = data.spaces || [];
    } catch (err) {
      host.innerHTML = card('加载失败', `<div class="tag red">${esc(err.message || '')}</div>`);
      return;
    }
    if (!spaces.length) {
      host.innerHTML = card('还没有分析空间', emptyState('没有可嵌出的分析空间', '先到「分析空间」创建一个，再回来生成嵌出链接'), { flush: true });
      return;
    }

    host.innerHTML = `
      <div class="info-strip">
        <span class="tag blue">Embed</span>
        嵌出用 <b>token</b> 做只读鉴权：分享空间不会授予底层表权限，token 可随时撤销。
      </div>
      <div class="grid-2 mt-16">
        ${spaces.map((s) => `<div class="card" data-space="${esc(s.id)}">
          <div class="card-head">
            <div><h3>${esc(s.name)}</h3><div class="sub">${esc(s.description || '无描述')}</div></div>
            <span class="tag blue">${(s.tables || []).length} 表 / ${(s.metrics || []).length} 指标</span>
          </div>
          <div id="embed-${esc(s.id)}" class="mt-12">${loading('生成嵌出配置…')}</div>
        </div>`).join('')}
      </div>`;

    for (const s of spaces) {
      loadEmbed(s);
    }
  })();

  async function loadEmbed(space) {
    const box = host.querySelector(`#embed-${space.id}`);
    if (!box) return;
    let res;
    try {
      res = await api.get(`/analysis-spaces/${space.id}/embed`);
    } catch (err) {
      box.innerHTML = `<div class="tag red">${esc(err.message || '生成失败')}</div>`;
      return;
    }
    const embed = res.embed || {};
    box.innerHTML = `
      ${descList([
        ['只读地址', `<span class="mono tiny">${esc(embed.readonly_url || '-')}</span>`],
        ['Token', `<span class="mono tiny">${esc((embed.token || '').slice(0, 18))}…</span>`],
        ['范围', tag(embed.scope === 'readonly' ? '只读' : embed.scope, 'outline')],
      ])}
      <div class="tiny dim" style="margin:12px 0 6px">iframe 嵌出代码</div>
      <pre class="code dark" id="code-${esc(space.id)}" style="white-space:pre-wrap;word-break:break-all;margin:0;font-size:11px">${esc(embed.iframe || '')}</pre>
      <div class="row mt-12" style="gap:8px">
        <button class="btn sm primary" data-copy="${esc(space.id)}">复制代码</button>
        <button class="btn sm" data-rotate="${esc(space.id)}">轮换 token</button>
        <button class="btn sm danger" data-revoke="${esc(space.id)}">撤销嵌出</button>
      </div>`;

    box.querySelector(`[data-copy="${space.id}"]`).addEventListener('click', async () => {
      const code = embed.iframe || '';
      try {
        await navigator.clipboard.writeText(code);
        toast('嵌出代码已复制', 'ok');
      } catch {
        // 剪贴板不可用时退化为选中，让用户手动复制
        const pre = box.querySelector(`#code-${space.id}`);
        const range = document.createRange();
        range.selectNodeContents(pre);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        toast('已选中代码，请按 Ctrl+C 复制', '');
      }
    });
    box.querySelector(`[data-rotate="${space.id}"]`).addEventListener('click', async () => {
      if (!(await confirmDialog('轮换 token', '旧 token 会立即失效，已分享的链接将无法访问，确认？', '轮换'))) return;
      await api.post(`/analysis-spaces/${space.id}/embed`, {});
      toast('token 已轮换', 'ok');
      loadEmbed(space);
    });
    box.querySelector(`[data-revoke="${space.id}"]`).addEventListener('click', async () => {
      if (!(await confirmDialog('撤销嵌出', '撤销后该空间所有嵌出链接立即失效，确认？', '撤销'))) return;
      await api.del(`/analysis-spaces/${space.id}/embed`);
      toast('嵌出已撤销', 'ok');
      loadEmbed(space);
    });
  }
}
