// 数据治理与分析：统一语义层 / 数据质量 / Agent 安全防护 / 分析空间 / 仪表盘
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  modal, confirmDialog, formModal, toast, drawer, esc, descList, el,
} from './ui.js';
import { chartHTML } from './charts.js';

/* ------------------------------------------------------------ 统一语义层 */
export async function renderSemantic(page, ctx) {
  page.innerHTML = loading();
  const [{ metrics }, { tables }] = await Promise.all([api.get('/metrics'), api.get('/catalog/tables')]);
  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => location.hash = '#/buddy' }, '让 Buddy 生成'),
    el('button', { class: 'btn primary', onclick: () => newMetric(ctx, tables) }, '＋ 注册指标'),
  ]));

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">Unity Semantics</span>
      指标口径在语义层收敛后，Agent 只做「选指标 + 选维度 + 选时间」，SQL 由模板编译 —— 避免同一问题问出两个数。
      <span class="spacer"></span><span class="dim tiny">NL2SQL 准确率：无语义层 60~70%，完整语义层 90%+</span>
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">指标口径</span><span class="dim tiny">${metrics.length} 个已发布</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="metric-table"></div>
    </div>

    <div class="grid-2 mt-16">
      <div id="metric-preview"></div>
      ${card('本体建模（Ontology）', `<div class="tiny muted" style="line-height:2">
        实体：客户、商品、渠道、订单<br>
        关系：订单 → 客户（多对一）、订单 → 商品（多对一）、订单 → 渠道（多对一）<br>
        指标挂载在「订单」实体上，维度通过关系自动解析到维度表。<br><br>
        当前语义层配置了 ${(metrics[0]?.joins || []).length} 条 join 关系，新增指标会自动复用。</div>`)}
    </div>`;

  page.querySelector('#metric-table').innerHTML = dataTable({
    rows: metrics,
    columns: [
      { title: '指标', render: (m) => `<div class="cell-main">${esc(m.display_name || m.name)}</div><div class="cell-sub mono">${esc(m.name)}</div>` },
      { title: '口径表达式', render: (m) => `<span class="mono tiny">${esc(m.expr)}</span>` },
      { title: '数据源表', render: (m) => `<span class="mono tiny">${esc(m.table_ref)}</span>` },
      { title: '单位', width: '70px', render: (m) => m.unit || '-' },
      { title: '可用维度', render: (m) => (m.dimensions || []).slice(0, 2).map((d) => `<span class="tag outline" style="margin-right:4px">${esc(d)}</span>`).join('') + ((m.dimensions || []).length > 2 ? `<span class="dim tiny">+${m.dimensions.length - 2}</span>` : '') },
      { title: '负责人', render: (m) => `<span class="dim">${esc(m.owner || '-')}</span>` },
      { title: '操作', key: 'ops', render: (m) => `<a class="btn-link" data-preview="${esc(m.name)}">预览</a><a class="btn-link danger" data-del="${esc(m.id)}">删除</a>` },
    ],
    emptyText: '还没有指标', emptyHint: '注册指标后，问数与看板都会复用同一口径',
  });

  const preview = async (name) => {
    const host = page.querySelector('#metric-preview');
    host.innerHTML = card('指标预览', loading(), { flush: false });
    try {
      const out = await api.post('/metrics/query', { metric: name, dimensions: [], granularity: null });
      const trend = await api.post('/metrics/query', { metric: name, dimensions: ['__date__'], granularity: 'day' });
      host.innerHTML = card(`${out.metric.display_name || name} · 趋势`,
        `${chartHTML(trend.chart, trend.columns, trend.rows)}
         <div class="row mt-12" style="gap:16px">
           <div><div class="tiny dim">全量值</div><div style="font-size:20px;font-weight:500">${fmt.num(out.rows[0]?.[0], 2)}</div></div>
           <div><div class="tiny dim">数据点</div><div style="font-size:20px;font-weight:500">${trend.row_count}</div></div>
           <div><div class="tiny dim">单位</div><div style="font-size:20px;font-weight:500">${esc(out.metric.unit || '-')}</div></div>
         </div>`, { flush: false });
    } catch (e) {
      host.innerHTML = card('指标预览', `<div class="tag red">查询失败</div><div class="tiny mt-8">${esc(e.message)}</div>`);
    }
  };
  page.querySelectorAll('[data-preview]').forEach((b) => b.addEventListener('click', () => preview(b.dataset.preview)));
  page.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除指标', '删除后使用该口径的看板会失效，确认？', '删除'))) return;
      await api.del(`/metrics/${b.dataset.del}`);
      ctx.refresh();
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
  if (metrics.length) preview(metrics[0].name);
}

async function newMetric(ctx, tables) {
  const data = await formModal({
    title: '注册指标',
    wide: true,
    fields: [
      { label: '指标标识', name: 'name', required: true, placeholder: 'net_gmv', hint: '英文，唯一' },
      { label: '中文名', name: 'display_name', required: true, placeholder: '净GMV' },
      { label: '主表', name: 'table_ref', type: 'select', options: tables.map((t) => t.ref) },
      { label: '聚合表达式', name: 'expr', required: true, placeholder: 'sum(f.amount)', hint: '别名固定为 f；维度表字段会复用已有 join' },
      { label: '单位', name: 'unit', placeholder: '元 / % / 单' },
      { label: '可用维度', name: 'dimensions', placeholder: 'order_date,channel_name,category' },
      { label: '口径说明', name: 'description', type: 'textarea', rows: 3, placeholder: '写清边界条件，例如是否含退款' },
    ],
    onSubmit: async (d) => {
      await api.post('/metrics', {
        ...d,
        dimensions: d.dimensions.split(',').map((s) => s.trim()).filter(Boolean),
      });
      toast('指标已注册', 'ok');
    },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- 数据质量 */
export async function renderQuality(page, ctx) {
  page.innerHTML = loading();
  const [{ rules }, { tables }] = await Promise.all([api.get('/governance/quality'), api.get('/catalog/tables')]);
  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => runAll() }, '全部执行'),
    el('button', { class: 'btn primary', onclick: () => newRule(ctx, tables) }, '＋ 新建规则'),
  ]));

  const passed = rules.filter((r) => r.last_status === 'pass').length;
  const evaluated = rules.filter((r) => r.last_status).length;

  async function runAll() {
    const out = await api.post('/governance/quality/run-all', {});
    toast(`${out.passed}/${out.total} 通过`, out.passed === out.total ? 'ok' : 'err');
    ctx.refresh();
  }

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">规则总数</span><span class="value">${rules.length}</span><span class="foot">YAML 定义 / 界面新建</span></div>
      <div class="stat"><span class="label">已执行</span><span class="value">${evaluated}</span><span class="foot">可嵌入 ETL 前置阻断</span></div>
      <div class="stat"><span class="label">通过</span><span class="value" style="color:var(--success)">${passed}</span><span class="foot">最近一次结果</span></div>
      <div class="stat"><span class="label">未通过</span><span class="value" style="color:${evaluated - passed ? 'var(--error)' : 'inherit'}">${evaluated - passed}</span><span class="foot">建议接入告警</span></div>
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">质量规则</span>
        <span class="dim tiny">空值率 · 唯一性 · 值域 · 时效性 · 自定义 SQL</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="rule-table"></div>
    </div>

    <div class="grid-2 mt-16">
      ${card('异常检测', `<div class="tiny muted" style="line-height:1.9">
        零配置、自学习基线，检测指标漂移：KS / PSI / Wasserstein / 卡方 / JS 散度。<br>
        当前版本以规则引擎为主，漂移检测在 v0.2 提供。</div>`)}
      ${card('规则级告警', `<div class="tiny muted" style="line-height:1.9">
        支持嵌入工作流做前置阻断：节点类型选 <code class="inline">quality</code> 时，返回值必须为 0 才继续。<br>
        告警渠道：企业微信 / 邮件 / Webhook（v0.2）。</div>`)}
    </div>`;

  page.querySelector('#rule-table').innerHTML = dataTable({
    rows: rules,
    columns: [
      { title: '对象', render: (r) => `<span class="mono tiny">${esc(r.table_ref)}</span><div class="cell-sub">${esc(r.column_name || '整表')}</div>` },
      { title: '规则类型', render: (r) => tag({ null_rate: '空值率', uniqueness: '唯一性', range: '值域', freshness: '时效性', custom_sql: '自定义 SQL' }[r.rule_type] || r.rule_type, 'outline') },
      { title: '阈值配置', render: (r) => `<span class="mono tiny">${esc(String(r.config).slice(0, 42))}</span>` },
      { title: '最近结果', render: (r) => r.last_status ? `${statusTag(r.last_status)}<div class="cell-sub">${esc(r.last_value || '')}</div>` : '<span class="dim">未执行</span>' },
      { title: '最近执行', render: (r) => `<span class="dim tiny">${r.last_run_at ? fmt.dt(r.last_run_at) : '-'}</span>` },
      { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-run="${r.id}">执行</a><a class="btn-link danger" data-del="${r.id}">删除</a>` },
    ],
    emptyText: '还没有质量规则', emptyHint: '新建规则或让 Buddy 推荐',
  });

  page.querySelectorAll('[data-run]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        const out = await api.post(`/governance/quality/${b.dataset.run}/run`, {});
        toast(`${out.status === 'pass' ? '通过' : '未通过'}：${out.detail}`, out.status === 'pass' ? 'ok' : 'err');
        ctx.refresh();
      } catch (e) { toast(e.message, 'err'); }
    })
  );
  page.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async () => {
      await api.del(`/governance/quality/${b.dataset.del}`);
      ctx.refresh();
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function newRule(ctx, tables) {
  const data = await formModal({
    title: '新建质量规则',
    fields: [
      { label: '目标表', name: 'table', type: 'select', options: tables.map((t) => t.ref) },
      { label: '字段', name: 'column', placeholder: 'amount（自定义 SQL 可留空）' },
      { label: '规则类型', name: 'rule_type', type: 'select', options: [
        { value: 'null_rate', label: '空值率' }, { value: 'uniqueness', label: '唯一性' },
        { value: 'range', label: '值域' }, { value: 'freshness', label: '时效性' }, { value: 'custom_sql', label: '自定义 SQL' }] },
      { label: '阈值（JSON）', name: 'config', value: '{"max_rate":0.02}', hint: 'null_rate: max_rate；uniqueness: max_dup；range: min/max；freshness: max_days；custom_sql: sql' },
    ],
    onSubmit: async (d) => {
      let cfg = {};
      try { cfg = JSON.parse(d.config || '{}'); } catch { throw new Error('阈值不是合法 JSON'); }
      await api.post('/governance/quality', { ...d, config: cfg });
      toast('规则已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

/* ---------------------------------------------------------- Agent 安全防护 */
export async function renderSecurity(page, ctx) {
  page.innerHTML = loading();
  const [policies, tagsOut, audit] = await Promise.all([
    api.get('/governance/policies').catch(() => ({ policies: [] })),
    api.get('/governance/tags').catch(() => ({ tags: [] })),
    api.get('/ops/audit?limit=30').catch(() => ({ logs: [] })),
  ]);

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">注入检测</span><span class="value" style="font-size:18px">已启用</span><span class="foot">命中即标注，按数据处理</span></div>
      <div class="stat"><span class="label">三要素拦截</span><span class="value" style="font-size:18px">已启用</span><span class="foot">不可信输入 + 改状态 + 敏感数据</span></div>
      <div class="stat"><span class="label">高危 SQL 拦截</span><span class="value" style="font-size:18px">白名单</span><span class="foot">只读 / 单语句 / 禁文件函数</span></div>
      <div class="stat"><span class="label">敏感字段</span><span class="value">${tagsOut.tags.length}</span><span class="foot">L3 / L4 需脱敏</span></div>
    </div>

    <div class="grid-2 mt-16">
      ${card('拦截链路', `<div class="col" style="gap:10px">
        ${[
          ['① 输入侧', 'scan_user_input：检测「忽略以上指令」类注入表述', 'green'],
          ['② 工具侧', 'guardrails.assess：按 risk 与敏感表判定是否需人工确认', 'green'],
          ['③ 执行侧', 'warehouse.guard：只读白名单 + 单语句 + 禁文件函数', 'green'],
          ['④ 展示侧', 'PII 脱敏：手机号/邮箱/身份证/银行卡默认掩码', 'green'],
        ].map(([k, v, tone]) => `<div class="row" style="gap:10px">
          <span class="tag ${tone}">${k}</span><span class="tiny" style="flex:1">${v}</span></div>`).join('')}
      </div>`, { sub: '覆盖 OWASP LLM Top10 主要风险' })}
      ${card('分级策略', (policies.policies || []).length
        ? tableHTML(['对象', '分级', '建议动作'], policies.policies.slice(0, 8).map((p) => [
            p.subject, `${p.tag} · ${p.level}`, p.suggested_action,
          ]), { raw: [1] })
        : emptyState('还没有分级数据', '到「数据目录」对表做一次敏感扫描'), { flush: true })}
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">安全相关审计</span><span class="dim tiny">审批、打标、拦截都会留痕</span>
        <span class="spacer"></span><a class="btn sm" href="#/buddy">到 Buddy 验证</a></div>
      ${dataTable({
        rows: (audit.logs || []).filter((l) => ['approve_tool', 'apply_tags', 'drop_table', 'update_settings'].includes(l.action)),
        columns: [
          { title: '时间', render: (l) => `<span class="dim">${fmt.dt(l.created_at)}</span>` },
          { title: '动作', render: (l) => tag(l.action, 'outline') },
          { title: '操作人', render: (l) => esc(l.user_name || '-') },
          { title: '对象', render: (l) => `<span class="mono tiny">${esc(l.target || '-')}</span>` },
        ],
        emptyText: '暂无安全相关审计记录', emptyHint: '在 Buddy 里触发一次写操作或打标',
      })}
    </div>`;
}

/* -------------------------------------------------------------- 分析空间 */
export async function renderAnalysis(page, ctx) {
  page.innerHTML = loading();
  const [{ spaces }, { metrics }, { tables }] = await Promise.all([
    api.get('/analysis-spaces'), api.get('/metrics'), api.get('/catalog/tables'),
  ]);
  ctx.actions(el('button', { class: 'btn primary', onclick: () => newSpace(ctx, tables, metrics) }, '＋ 创建分析空间'));

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">Analysis Space</span>
      分析空间是业务方的工作台：绑定表与指标，Buddy 只在空间内检索。分享空间<b>不会</b>授予底层表权限，两套权限独立。
    </div>
    <div class="grid-2 mt-16">
      ${spaces.map((s) => `<div class="card">
        <div class="card-head">
          <div><h3>${esc(s.name)}</h3><div class="sub">${esc(s.description || '无描述')}</div></div>
          <span class="tag blue">${(s.tables || []).length} 表 / ${(s.metrics || []).length} 指标</span>
        </div>
        <div class="row" style="gap:6px;flex-wrap:wrap">
          ${(s.metrics || []).map((m) => `<span class="tag outline">${esc(m)}</span>`).join('')}
        </div>
        <div class="tiny dim mt-12">${(s.tables || []).map((t) => esc(t)).join(' · ')}</div>
        <div class="row mt-16" style="gap:8px">
          <a class="btn sm primary" href="#/buddy">进入空间问数</a>
          <button class="btn sm" data-view="${s.id}">资产</button>
          <button class="btn sm danger" data-del="${s.id}">删除</button>
        </div>
      </div>`).join('') || emptyState('还没有分析空间', '创建后把表与指标绑进去，业务方即可直接问数')}
    </div>`;

  page.querySelectorAll('[data-view]').forEach((b) =>
    b.addEventListener('click', () => {
      const s = spaces.find((x) => x.id === b.dataset.view);
      drawer({
        title: s.name,
        body: `<div class="col" style="gap:16px">
          ${descList([['负责人', s.owner || '-'], ['创建时间', fmt.dt(s.created_at)]])}
          <div><div class="tiny dim" style="margin-bottom:6px">绑定数据表</div>${(s.tables || []).map((t) => `<div class="mono tiny">${esc(t)}</div>`).join('') || '<span class="dim">未绑定</span>'}</div>
          <div><div class="tiny dim" style="margin-bottom:6px">绑定指标</div>${(s.metrics || []).map((t) => `<span class="tag blue" style="margin:0 4px 4px 0">${esc(t)}</span>`).join('') || '<span class="dim">未绑定</span>'}</div>
        </div>`,
      });
    })
  );
  page.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除分析空间', '空间内会话与产物会一并清理，确认？', '删除'))) return;
      await api.del(`/analysis-spaces/${b.dataset.del}`);
      ctx.refresh();
    })
  );
}

async function newSpace(ctx, tables, metrics) {
  const data = await formModal({
    title: '创建分析空间',
    wide: true,
    fields: [
      { label: '空间名称', name: 'name', required: true, placeholder: '例如 零售经营分析' },
      { label: '描述', name: 'description' },
      { label: '数据表', name: 'tables', placeholder: '逗号分隔，如 demo.retail.fact_orders', hint: tables.slice(0, 4).map((t) => t.ref).join(' / ') },
      { label: '指标', name: 'metrics', placeholder: '逗号分隔，如 gmv,order_cnt', hint: metrics.slice(0, 6).map((m) => m.name).join(' / ') },
    ],
    onSubmit: async (d) => {
      await api.post('/analysis-spaces', {
        name: d.name, description: d.description,
        tables: d.tables.split(',').map((s) => s.trim()).filter(Boolean),
        metrics: d.metrics.split(',').map((s) => s.trim()).filter(Boolean),
      });
      toast('分析空间已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- 仪表盘 */
const CARD_CHART_TYPES = [
  { value: 'kpi', label: '指标卡 (kpi)' },
  { value: 'line', label: '折线图 (line)' },
  { value: 'bar', label: '柱状图 (bar)' },
  { value: 'pie', label: '饼图 (pie)' },
  { value: 'table', label: '明细表 (table)' },
];

const CARD_PERIODS = [
  { value: '', label: '默认（上月）' },
  { value: 'today', label: '今天' },
  { value: 'yesterday', label: '昨天' },
  { value: 'last_7d', label: '近 7 天' },
  { value: 'last_30d', label: '近 30 天' },
  { value: 'last_90d', label: '近 90 天' },
  { value: 'this_month', label: '本月' },
  { value: 'this_year', label: '本年' },
];

// 图表轴推导，与后端 chart() 约定一致：x 取维度列，y 取数值列
function chartSpecFor(type, columns, rows, base) {
  const cols = columns || [];
  const sample = (rows || []).slice(0, 20);
  const isNum = (i) => sample.some((r) => typeof r[i] === 'number' && !Number.isNaN(r[i]));
  const numericIdx = cols.map((_, i) => i).filter(isNum);
  if (type === 'table') return { type: 'table' };
  if (type === 'kpi') {
    const idx = numericIdx.length ? numericIdx[0] : (base?.value_index ?? 1);
    return { type: 'kpi', value_index: idx, label: cols[idx] || base?.label || '' };
  }
  const x = base?.x ?? 0;
  let y = base?.y;
  if (y === undefined || y === null || y === x || !isNum(y)) y = numericIdx.find((i) => i !== x) ?? 1;
  return { type, x, y };
}

function chartTypeLabel(type) {
  const hit = CARD_CHART_TYPES.find((t) => t.value === type);
  return hit ? hit.label : (type || '表格');
}

// spec 里只保留卡片定义，查询结果由后端渲染时实时计算
function toStoredCard(c) {
  const out = { title: c.title || '未命名卡片', sql: c.sql || '', chart: c.chart || { type: 'table' } };
  if (c.source) out.source = c.source;
  return out;
}

// 拖拽排序：把 from 位置的卡片移动到 to 位置的前/后半区
function moveCard(cards, from, to, after) {
  const next = cards.slice();
  const [moved] = next.splice(from, 1);
  let target = after ? to + 1 : to;
  if (from < target) target -= 1;
  next.splice(target, 0, moved);
  return next;
}

// HTML5 拖拽排序：手柄 draggable，卡片容器作为放置目标
function bindCardDragging(root, onReorder) {
  const boxes = Array.from(root.querySelectorAll('[data-card-idx]'));
  let from = -1;
  root.querySelectorAll('[data-drag]').forEach((handle) => {
    handle.addEventListener('dragstart', (e) => {
      from = Number(handle.dataset.drag);
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(from));
      const box = handle.closest('[data-card-idx]');
      if (box) box.style.opacity = '.45';
    });
    handle.addEventListener('dragend', () => {
      from = -1;
      boxes.forEach((b) => {
        b.style.opacity = '';
        b.style.outline = '';
        delete b.dataset.dropAfter;
      });
    });
  });
  boxes.forEach((box, idx) => {
    box.addEventListener('dragover', (e) => {
      if (from < 0) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const rect = box.getBoundingClientRect();
      const after = e.clientY > rect.top + rect.height / 2;
      box.dataset.dropAfter = after ? '1' : '0';
      box.style.outline = `2px dashed ${after ? 'var(--brand)' : 'var(--border-strong)'}`;
    });
    box.addEventListener('dragleave', (e) => {
      if (e.relatedTarget && box.contains(e.relatedTarget)) return;
      box.style.outline = '';
    });
    box.addEventListener('drop', (e) => {
      e.preventDefault();
      const src = from >= 0 ? from : Number(e.dataTransfer.getData('text/plain'));
      const after = box.dataset.dropAfter === '1';
      box.style.outline = '';
      if (Number.isNaN(src) || src < 0) return;
      onReorder(src, idx, after);
    });
  });
}

export async function renderDashboards(page, ctx) {
  page.innerHTML = loading();
  const [{ dashboards }, { metrics }] = await Promise.all([api.get('/dashboards'), api.get('/metrics')]);
  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => newBoard(ctx, metrics) }, '＋ 手工新建'),
    el('a', { class: 'btn primary', href: '#/buddy' }, '让 Buddy 生成'),
  ]));

  page.innerHTML = `
    <div class="card flush">
      <div class="tbl-toolbar"><span class="title">仪表盘</span><span class="dim tiny">${dashboards.length} 个</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="board-list"></div>
    </div>
    <div id="board" class="mt-16"></div>`;

  const listHost = page.querySelector('#board-list');
  const boardHost = page.querySelector('#board');
  const state = { id: '', board: null, spec: {}, cards: [] };

  const renderList = (rows) => {
    listHost.innerHTML = dataTable({
      rows,
      columns: [
        { title: '名称', render: (d) => `<div class="cell-main">${esc(d.name)}</div><div class="cell-sub">${esc(d.description || '')}</div>` },
        { title: '卡片数', align: 'right', width: '90px', render: (d) => d.card_count },
        { title: '创建时间', render: (d) => `<span class="dim">${fmt.dt(d.created_at)}</span>` },
        { title: '操作', key: 'ops', render: (d) => `<a class="btn-link" data-view="${d.id}">查看</a><a class="btn-link danger" data-del="${d.id}">删除</a>` },
      ],
      emptyText: '还没有仪表盘', emptyHint: '让 Buddy「生成一份经营看板」，或手工新建',
    });
    listHost.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => view(b.dataset.view)));
    listHost.querySelectorAll('[data-del]').forEach((b) =>
      b.addEventListener('click', async () => {
        if (!(await confirmDialog('删除看板', '确认删除？', '删除'))) return;
        await api.del(`/dashboards/${b.dataset.del}`);
        ctx.refresh();
      })
    );
  };

  // 卡片定义：标题 + 只读 SQL + 图表类型
  const boardCard = (c, i) => {
    const type = (c.chart || {}).type || 'table';
    const rows = c.rows || [];
    const body = c.error
      ? `<div class="tag red">渲染失败</div><div class="tiny mt-8">${esc(c.error)}</div>`
      : type === 'table'
        ? tableHTML(c.columns, rows, { max: 8 })
        : `${chartHTML(c.chart, c.columns, rows)}
          <details class="mt-8"><summary class="tiny dim" style="cursor:pointer">明细</summary>
            <div class="mt-8">${tableHTML(c.columns, rows, { max: 8 })}</div></details>`;
    const ops = `<div class="row" style="gap:10px;align-items:center">
        <span draggable="true" data-drag="${i}" title="拖拽调整顺序" style="cursor:grab;color:var(--text-3);user-select:none;font-size:14px;line-height:1">⠿</span>
        <a class="btn-link" data-card-edit="${i}">编辑</a>
        <a class="btn-link danger" data-card-del="${i}">删除</a>
      </div>`;
    const sub = `${c.row_count ?? rows.length} 行${c.elapsed_ms != null ? ` · ${c.elapsed_ms} ms` : ''} · ${chartTypeLabel(type)}`;
    return `<div data-card-idx="${i}" style="display:flex;flex-direction:column">
      ${card(c.title || '未命名卡片', body, { sub, actions: ops })}
    </div>`;
  };

  const renderBoard = () => {
    const board = state.board || {};
    boardHost.innerHTML = `
      <div class="row" style="justify-content:space-between;margin-bottom:12px">
        <div><h3>${esc(board.name || '')}</h3><div class="tiny dim">${esc(board.description || '')}</div></div>
        <div class="row" style="gap:8px;align-items:center">
          <span class="dim tiny">拖动 ⠿ 可排序</span>
          <span class="tag">${state.cards.length} 张卡片</span>
          <button class="btn sm primary" id="board-add">＋ 添加卡片</button>
        </div>
      </div>
      <div class="grid-2">${state.cards.length
        ? state.cards.map((c, i) => boardCard(c, i)).join('')
        : `<div style="grid-column:1/-1"><div class="card">${emptyState('还没有卡片', '点右上角「添加卡片」，用一句话描述你想看的数据')}</div></div>`}
      </div>`;
    boardHost.querySelector('#board-add').addEventListener('click', addCard);
    boardHost.querySelectorAll('[data-card-edit]').forEach((b) =>
      b.addEventListener('click', () => openCardEditor(Number(b.dataset.cardEdit)))
    );
    boardHost.querySelectorAll('[data-card-del]').forEach((b) =>
      b.addEventListener('click', () => removeCard(Number(b.dataset.cardDel)))
    );
    bindCardDragging(boardHost, reorderCards);
  };

  const view = async (id) => {
    boardHost.innerHTML = loading('渲染看板…');
    let out;
    try {
      out = await api.get(`/dashboards/${id}`);
    } catch (err) {
      boardHost.innerHTML = card('看板加载失败', `<div class="tag red">${esc(err.message || '请求失败')}</div>`);
      return;
    }
    const board = out.dashboard || {};
    state.id = board.id || id;
    state.board = board;
    state.spec = board.spec && typeof board.spec === 'object' ? board.spec : {};
    state.cards = (out.cards || []).map((c) => ({ ...c }));
    renderBoard();
  };

  // 卡片定义变更统一走 PATCH：spec.cards 整体替换
  const saveCards = async (cards, message) => {
    const stored = cards.map(toStoredCard);
    await api.patch(`/dashboards/${state.id}`, { spec: { ...state.spec, cards: stored } });
    state.spec = { ...state.spec, cards: stored };
    state.cards = cards;
    if (message) toast(message, 'ok');
    renderBoard();
    await refreshList();
  };

  const refreshList = async () => {
    try {
      const out = await api.get('/dashboards');
      renderList(out.dashboards || []);
    } catch {
      // 列表刷新失败不影响看板编辑
    }
  };

  // 添加卡片：一句话描述 → 语义层生成 → 追加到看板
  const addCard = () => {
    formModal({
      title: '添加卡片',
      wide: true,
      submitText: '生成卡片',
      fields: [
        { label: '想看什么', name: 'prompt', type: 'textarea', rows: 3, required: true, placeholder: '例如：近 30 天按渠道的 GMV 趋势', hint: '按语义层指标口径编译 SQL，只读执行' },
        { label: '时间窗口', name: 'period', type: 'select', options: CARD_PERIODS },
      ],
      onSubmit: async (d) => {
        let res;
        try {
          res = await api.post('/analysis/dashboard/generate', { prompt: d.prompt, period: d.period });
        } catch (err) {
          throw new Error(err.message || '卡片生成失败');
        }
        const gen = (res.cards || [])[0];
        if (!gen || !gen.sql) throw new Error('没有生成可用的卡片，请调整描述后重试');
        await saveCards([...state.cards, cardFromGenerate(gen, d.prompt)], '卡片已添加');
      },
    });
  };

  const cardFromGenerate = (gen, prompt) => {
    const metric = gen.metric || {};
    const cols = gen.columns || [];
    const dim = cols.length > 1 ? cols[0] : '';
    const label = metric.display_name || metric.name || prompt || '新卡片';
    return {
      title: dim ? `${label} · 按${dim}` : label,
      sql: gen.sql,
      chart: chartSpecFor((gen.chart || {}).type || 'bar', cols, gen.rows, gen.chart),
      source: { type: 'metric', ...(metric.name ? { metric: metric.name } : {}), ...(dim ? { dimension: dim } : {}) },
    };
  };

  const dimOptions = (metricName, selected) => {
    const m = metrics.find((x) => x.name === metricName);
    const dims = (m && m.dimensions) || [];
    const opts = [{ value: '', label: '总计' }, { value: '__date__', label: '按天趋势' }];
    dims.filter((d) => d && d !== '__date__').forEach((d) => opts.push({ value: d, label: d }));
    return opts.map((o) => `<option value="${esc(o.value)}"${o.value === (selected || '') ? ' selected' : ''}>${esc(o.label)}</option>`).join('');
  };

  // 编辑卡片：标题 / 图表类型 / 数据来源（注册指标或只读 SQL）
  const openCardEditor = (idx) => {
    const cur = state.cards[idx];
    if (!cur) return;
    const source = cur.source || {};
    const metricName = source.metric || (cur.metric || {}).name || '';
    const dimValue = source.dimension || '';
    modal({
      title: '编辑卡片',
      wide: true,
      body: `
        <div class="col" style="gap:12px">
          <div class="field"><label>卡片标题</label>
            <input type="text" id="ce-title" value="${esc(cur.title || '')}" /></div>
          <div class="field"><label>图表类型</label>
            <select id="ce-type">${CARD_CHART_TYPES.map((t) => `<option value="${t.value}"${t.value === (cur.chart || {}).type ? ' selected' : ''}>${esc(t.label)}</option>`).join('')}</select></div>
          <div class="field"><label>数据来源</label>
            <select id="ce-source">
              <option value="metric"${source.type !== 'sql' ? ' selected' : ''}>引用注册指标</option>
              <option value="sql"${source.type === 'sql' ? ' selected' : ''}>只读 SQL</option>
            </select>
            <div class="hint">指标走统一语义层口径；SQL 仅允许单条只读查询，保存前会先试跑校验</div></div>
          <div id="ce-metric-box">
            <div class="field"><label>指标</label>
              <select id="ce-metric">${metrics.map((m) => `<option value="${esc(m.name)}"${m.name === metricName ? ' selected' : ''}>${esc(m.display_name || m.name)}</option>`).join('')}</select></div>
            <div class="field"><label>维度</label><select id="ce-dim">${dimOptions(metricName, dimValue)}</select></div>
          </div>
          <div id="ce-sql-box"><div class="field"><label>只读 SQL</label>
            <textarea id="ce-sql" rows="5" placeholder="select ...">${esc(cur.sql || '')}</textarea></div></div>
        </div>`,
      actions: [
        { label: '取消', value: false },
        {
          label: '保存',
          primary: true,
          onClick: async (content) => {
            const title = content.querySelector('#ce-title').value.trim();
            if (!title) {
              toast('请填写卡片标题', 'err');
              return false;
            }
            const type = content.querySelector('#ce-type').value;
            const src = content.querySelector('#ce-source').value;
            let next;
            try {
              if (src === 'metric') {
                const name = content.querySelector('#ce-metric').value;
                const dim = content.querySelector('#ce-dim').value;
                const q = await api.post('/metrics/query', { metric: name, dimensions: dim ? [dim] : [], granularity: dim === '__date__' ? 'day' : null });
                next = {
                  title,
                  sql: q.sql,
                  chart: chartSpecFor(type, q.columns, q.rows, { ...(cur.chart || {}), type }),
                  columns: q.columns,
                  rows: q.rows,
                  source: { type: 'metric', metric: name, dimension: dim },
                };
              } else {
                const sql = content.querySelector('#ce-sql').value.trim();
                if (!sql) {
                  toast('请填写只读 SQL', 'err');
                  return false;
                }
                const run = await api.post('/sql/run', { sql, limit: 200 });
                next = {
                  title,
                  sql,
                  chart: chartSpecFor(type, run.columns, run.rows, { ...(cur.chart || {}), type }),
                  columns: run.columns,
                  rows: run.rows,
                  source: { type: 'sql' },
                };
              }
            } catch (err) {
              toast(err.message || '数据源校验失败', 'err');
              return false;
            }
            const cards = state.cards.slice();
            cards[idx] = next;
            try {
              await saveCards(cards, '卡片已更新');
            } catch (err) {
              toast(err.message || '保存失败', 'err');
              return false;
            }
            return true;
          },
        },
      ],
      onMount: (content) => {
        const sourceSel = content.querySelector('#ce-source');
        const metricSel = content.querySelector('#ce-metric');
        const dimSel = content.querySelector('#ce-dim');
        const sync = () => {
          const isMetric = sourceSel.value === 'metric';
          content.querySelector('#ce-metric-box').style.display = isMetric ? '' : 'none';
          content.querySelector('#ce-sql-box').style.display = isMetric ? 'none' : '';
        };
        sourceSel.addEventListener('change', sync);
        metricSel.addEventListener('change', () => {
          dimSel.innerHTML = dimOptions(metricSel.value, '');
        });
        sync();
      },
    });
  };

  const removeCard = async (idx) => {
    const cur = state.cards[idx];
    if (!cur) return;
    if (!(await confirmDialog('删除卡片', `确认删除「${cur.title || '未命名卡片'}」？`, '删除'))) return;
    try {
      await saveCards(state.cards.filter((_, i) => i !== idx), '卡片已删除');
    } catch (err) {
      toast(err.message || '删除失败', 'err');
    }
  };

  const reorderCards = async (from, to, after) => {
    if (from === to) return;
    const next = moveCard(state.cards, from, to, after);
    if (next.every((c, i) => c === state.cards[i])) return;
    try {
      await saveCards(next, '顺序已保存');
    } catch (err) {
      toast(err.message || '顺序保存失败', 'err');
      renderBoard();
    }
  };

  renderList(dashboards);
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
  const first = listHost.querySelector('[data-view]');
  if (first) view(first.dataset.view);
}

async function newBoard(ctx, metrics) {
  const data = await formModal({
    title: '新建看板',
    fields: [
      { label: '看板名称', name: 'name', required: true },
      { label: '指标', name: 'metric', type: 'select', options: metrics.map((m) => ({ value: m.name, label: m.display_name || m.name })) },
      { label: '维度', name: 'dim', type: 'select', options: [
        { value: '', label: '总计' }, { value: '__date__', label: '按天趋势' }, { value: 'channel_name', label: '按渠道' }, { value: 'category', label: '按品类' }] },
    ],
    onSubmit: async (d) => {
      const preview = await api.post('/metrics/query', { metric: d.metric, dimensions: [], granularity: null });
      const cards = [{ title: `${d.name} · 总计`, sql: preview.sql, chart: { type: 'kpi' } }];
      if (d.dim) {
        const p2 = await api.post('/metrics/query', { metric: d.metric, dimensions: [d.dim], granularity: d.dim === '__date__' ? 'day' : null });
        cards.push({ title: `${d.name} · ${d.dim === '__date__' ? '趋势' : d.dim}`, sql: p2.sql, chart: p2.chart });
      }
      await api.post('/dashboards', { name: d.name, description: '由语义层指标生成', cards });
      toast('看板已生成', 'ok');
    },
  });
  if (data) ctx.refresh();
}
