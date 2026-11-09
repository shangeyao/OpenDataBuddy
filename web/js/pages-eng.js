// 数据工程：开发工作台 / 工作流编排 / 发布管理 / 运维监控 / 计算资源
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  modal, confirmDialog, formModal, toast, drawer, esc, descList, bindTabs, el,
} from './ui.js';
import { chartHTML } from './charts.js';
import { icon } from './icons.js';

/* ------------------------------------------------------------ 开发工作台 */
export async function renderStudio(page, ctx) {
  const { tables } = await api.get('/catalog/tables');
  page.innerHTML = `
    <div class="split" style="grid-template-columns:232px 1fr">
      <div class="tree-panel">
        <div class="tp-head"><span>工作区文件</span><span class="dim tiny">+ 新建</span></div>
        <div class="tree">
          <div class="tree-node"><span class="ico">${icon('folder', 13)}</span>notebooks</div>
          <div class="tree-children">
            <div class="tree-node active" data-file="daily_gmv.sql"><span class="ico">${icon('studio', 13)}</span>daily_gmv.sql</div>
            <div class="tree-node" data-file="channel_report.sql"><span class="ico">${icon('studio', 13)}</span>channel_report.sql</div>
          </div>
          <div class="tree-node"><span class="ico">${icon('folder', 13)}</span>python</div>
          <div class="tree-children">
            <div class="tree-node" data-file="feature_eng.py"><span class="ico">${icon('studio', 13)}</span>feature_eng.py</div>
          </div>
          <div class="tree-node"><span class="ico">${icon('folder', 13)}</span>tables</div>
          <div class="tree-children">
            ${tables.slice(0, 6).map((t) => `<div class="tree-node" data-ins="${esc(t.ref)}"><span class="ico">${icon('table', 13)}</span>
              <span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(t.name)}</span></div>`).join('')}
          </div>
        </div>
      </div>
      <div class="col" style="gap:16px">
        <div class="card flush">
          <div class="tbl-toolbar">
            <span class="title">daily_gmv.sql</span>
            <span class="tag outline">SQL</span>
            <span class="spacer"></span>
            <span class="tiny dim">交互式集群 demo_interactive（已停止）</span>
            <button class="btn sm" id="ai-assist">AI 助手</button>
            <button class="btn sm primary" id="run">运行</button>
          </div>
          <div style="padding:16px">
            <textarea id="code" rows="12" class="mono" style="font-family:var(--mono);font-size:12.5px;min-height:220px">-- 每日 GMV 汇总：按渠道拆分
SELECT
  o.order_date,
  c.channel_name,
  round(sum(o.amount), 2) AS gmv,
  count(DISTINCT o.order_id) AS order_cnt
FROM demo.retail.fact_orders o
JOIN demo.retail.dim_channel c ON c.channel_id = o.channel_id
WHERE o.order_date >= DATE '2026-11-01'
GROUP BY 1, 2
ORDER BY 1, 3 DESC</textarea>
          </div>
        </div>
        <div id="studio-result">${card('运行结果', emptyState('点击运行查看结果', 'Ctrl / Cmd + Enter 也可运行', '▶'), { flush: false })}</div>
      </div>
    </div>`;

  const ta = page.querySelector('#code');
  const run = async () => {
    const host = page.querySelector('#studio-result');
    host.innerHTML = loading('执行中…');
    try {
      const out = await api.post('/sql/run', { sql: ta.value.replace(/--.*$/gm, ''), limit: 200 });
      host.innerHTML = card('运行结果', `${chartHTML(out.chart, out.columns, out.rows)}${tableHTML(out.columns, out.rows, { max: 50 })}`, {
        sub: `${out.row_count} 行 · ${out.elapsed_ms} ms`, flush: true,
      });
    } catch (err) {
      host.innerHTML = card('运行失败', `<pre class="code">${esc(err.message)}</pre>`);
    }
  };
  page.querySelector('#run').addEventListener('click', run);
  page.querySelector('#ai-assist').addEventListener('click', () => {
    modal({
      title: 'AI 代码助手',
      body: `<div class="col" style="gap:12px">
        <div class="tiny muted">Buddy 已关联当前文件与运行历史，可直接提问或让它改代码。</div>
        <textarea rows="4" placeholder="例如：把日期过滤改成参数化，并加上退款单剔除"></textarea>
        <div class="row"><button class="btn primary sm" onclick="location.hash='#/buddy'">到 Buddy 继续 →</button></div>
      </div>`,
      actions: [{ label: '关闭' }],
    });
  });
  ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') run(); });
  page.querySelectorAll('[data-ins]').forEach((n) =>
    n.addEventListener('click', () => {
      const pos = ta.selectionStart;
      ta.value = ta.value.slice(0, pos) + n.dataset.ins + ta.value.slice(pos);
      ta.focus();
    })
  );
  page.querySelectorAll('[data-file]').forEach((n) =>
    n.addEventListener('click', () => {
      page.querySelectorAll('[data-file]').forEach((x) => x.classList.remove('active'));
      n.classList.add('active');
      page.querySelector('.tbl-toolbar .title').textContent = n.dataset.file;
      ta.value = n.dataset.file.endsWith('.py')
        ? `import duckdb\n\ncon = duckdb.connect('data/warehouse.duckdb')\ndf = con.sql("SELECT * FROM demo__retail.fact_orders LIMIT 100").df()\nprint(df.head())`
        : `SELECT * FROM demo.retail.${n.dataset.file.replace('.sql', '')} LIMIT 100`;
    })
  );
  run();
}

/* ------------------------------------------------------------ 工作流编排 */
export async function renderFlows(page, ctx) {
  page.innerHTML = loading();
  const { flows } = await api.get('/flows');
  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => { location.hash = '#/ops'; } }, '运行监控'),
    el('button', { class: 'btn primary', onclick: () => newFlow(ctx) }, '＋ 新建工作流'),
  ]));

  page.innerHTML = `
    <div class="filter-bar">
      <div class="f-item"><span>工作流</span><input id="f-kw" placeholder="按名称搜索" /></div>
      <div class="f-item"><span>调度状态</span><select id="f-enabled"><option value="">全部</option><option value="1">调度中</option><option value="0">未调度</option></select></div>
      <span class="spacer"></span>
      <button class="btn" id="f-reset">重置</button>
    </div>
    <div class="card flush">
      <div class="tbl-toolbar"><span class="title">工作流列表</span><span class="dim tiny">共 ${flows.length} 个</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="flow-table"></div>
    </div>`;

  const draw = (rows) => {
    page.querySelector('#flow-table').innerHTML = dataTable({
      rows,
      columns: [
        { title: '工作流名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div><div class="cell-sub">${esc(r.description || '无描述')}</div>` },
        { title: '节点数', align: 'right', width: '80px', render: (r) => r.nodes },
        { title: '调度周期', render: (r) => r.cron ? `<span class="mono tiny">${esc(r.cron)}</span><div class="cell-sub">${esc(r.schedule_text)}</div>` : '<span class="dim">手动触发</span>' },
        { title: '状态', render: (r) => statusTag(r.enabled ? 'running' : 'stopped') },
        { title: '最近运行', render: (r) => r.last_run ? `${statusTag(r.last_run.status)}<div class="cell-sub">${fmt.ago(r.last_run.started_at)}</div>` : '<span class="dim">未运行</span>' },
        { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-detail="${r.id}">详情</a><a class="btn-link" data-run="${r.id}">运行</a><a class="btn-link danger" data-del="${r.id}">删除</a>` },
      ],
      emptyText: '还没有工作流', emptyHint: '点右上角新建，或让 Buddy 一句话生成',
    });
    page.querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', async () => {
      b.textContent = '运行中…';
      try {
        const out = await api.post(`/flows/${b.dataset.run}/run`, {});
        toast(`运行${out.run.status === 'success' ? '成功' : '失败'}`, out.run.status === 'success' ? 'ok' : 'err');
        showRun(out.run);
        ctx.refresh();
      } catch (e) { toast(e.message, 'err'); b.textContent = '运行'; }
    }));
    page.querySelectorAll('[data-detail]').forEach((b) => b.addEventListener('click', () => openFlow(b.dataset.detail)));
    page.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除工作流', '会同时删除运行历史，确认？', '删除'))) return;
      await api.del(`/flows/${b.dataset.del}`);
      ctx.refresh();
    }));
  };
  draw(flows);

  const filter = () => {
    const kw = page.querySelector('#f-kw').value.trim().toLowerCase();
    const en = page.querySelector('#f-enabled').value;
    draw(flows.filter((f) => (!kw || f.name.toLowerCase().includes(kw)) && (en === '' || String(f.enabled ? 1 : 0) === en)));
  };
  page.querySelector('#f-kw').addEventListener('input', filter);
  page.querySelector('#f-enabled').addEventListener('change', filter);
  page.querySelector('#f-reset').addEventListener('click', () => {
    page.querySelector('#f-kw').value = ''; page.querySelector('#f-enabled').value = ''; draw(flows);
  });
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

function dagHTML(nodes) {
  if (!nodes || !nodes.length) return emptyState('没有节点');
  return `<div class="dag-canvas"><div class="dag-flow">${nodes
    .map((n, i) => `${i ? '<span class="dag-arrow">→</span>' : ''}
      <div class="dag-node"><div class="n-title"><span class="tag blue">${i + 1}</span>${esc(n.name)}</div>
        <div class="n-meta">${esc(n.type)}${n.config?.sql ? ' · ' + esc(String(n.config.sql).slice(0, 40)) + '…' : ''}</div>
      </div>`)
    .join('')}</div></div>`;
}

async function openFlow(id) {
  const { flow } = await api.get(`/flows/${id}`);
  const body = `<div class="col" style="gap:16px">
    <div class="info-strip">
      <span class="tag ${flow.enabled ? 'green' : ''}">${flow.enabled ? '调度中' : '未调度'}</span>
      <span>${esc(flow.schedule_text)}${flow.cron ? `（<span class="mono">${esc(flow.cron)}</span>）` : ''}</span>
      <span class="spacer"></span><span class="dim tiny">${flow.nodes.length} 个节点</span>
    </div>
    <div>${card('DAG 视图', dagHTML(flow.nodes), { flush: true })}</div>
    <div>${card('节点明细', tableHTML(['节点', '类型', 'SQL / 配置'], flow.nodes.map((n) => [
      n.name, n.type, (n.config?.sql || n.config?.url || '-').slice(0, 80),
    ])), { flush: true })}</div>
    <div>${card('最近运行', flow.runs?.length
      ? tableHTML(['状态', '触发', '开始', '结束', '操作'], flow.runs.map((r) => [
          r.status, r.trigger, fmt.dt(r.started_at), r.finished_at ? fmt.dt(r.finished_at) : '-',
          `<a href="javascript:void 0" class="btn-link" data-run-id="${r.id}">日志</a>`,
        ]), { raw: [0, 4] })
      : emptyState('还没有运行记录'), { flush: true })}</div>
  </div>`;
  const d = drawer({ title: flow.name, body, wide: true });
  d.body.querySelectorAll('[data-run-id]').forEach((b) =>
    b.addEventListener('click', () => {
      const run = flow.runs.find((r) => r.id === b.dataset.runId);
      showRun(run);
    })
  );
}

function showRun(run) {
  const log = run.log || [];
  const rows = log.map((e) => [
    e.name, e.status === 'success' ? '成功' : e.status === 'failed' ? '失败' : e.status,
    e.rows ?? '-', e.message || '', e.elapsed_ms ? `${e.elapsed_ms}ms` : '-',
  ]);
  const previews = log.filter((e) => e.preview).map((e) => `
    <div class="mt-16"><div class="tiny dim" style="margin-bottom:6px">${esc(e.name)} 输出预览</div>
    ${tableHTML(e.preview.columns, e.preview.rows, { max: 6 })}</div>`).join('');
  modal({
    title: `运行日志 · ${run.status === 'success' ? '成功' : '失败'}`,
    wide: true,
    body: `<div class="tiny dim" style="margin-bottom:12px">${fmt.dt(run.started_at)}${run.finished_at ? ' → ' + fmt.dt(run.finished_at) : ''}</div>
      ${tableHTML(['节点', '状态', '行数', '信息', '耗时'], rows)}${previews}`,
    actions: [{ label: '关闭', primary: true }],
  });
}

async function newFlow(ctx) {
  const data = await formModal({
    title: '新建工作流',
    wide: true,
    fields: [
      { label: '工作流名称', name: 'name', required: true, placeholder: '例如 每日经营数据刷新' },
      { label: '描述', name: 'description' },
      { label: '调度 cron', name: 'cron', placeholder: '0 7 * * *', hint: '5 段表达式，留空则只支持手动触发' },
      { label: '节点（每行：名称 | SQL）', name: 'nodes', type: 'textarea', rows: 6,
        value: '汇总 | SELECT order_date, sum(amount) gmv FROM demo.retail.fact_orders GROUP BY 1\n校验 | SELECT count(*) FROM demo.retail.fact_orders WHERE amount IS NULL',
        hint: '按顺序串成 DAG；第二个节点起为质量断言，返回值需为 0' },
    ],
    onSubmit: async (d) => {
      const nodes = d.nodes.split('\n').map((l) => l.split('|')).filter((p) => p.length >= 2 && p[1].trim())
        .map((p, i) => ({ type: i === 0 ? 'sql' : 'quality', name: p[0].trim() || `步骤${i + 1}`, sql: p[1].trim() }));
      if (!nodes.length) throw new Error('至少需要一个节点');
      await api.post('/flows', { name: d.name, description: d.description, cron: d.cron, enabled: !!d.cron, nodes });
      toast('工作流已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- 发布管理 */
export async function renderCicd(page, ctx) {
  const { flows } = await api.get('/flows');
  page.innerHTML = `
    <div class="grid-2" style="grid-template-columns:1fr 380px;align-items:start">
      <div class="col" style="gap:16px">
        ${card('bundle.yaml', `<textarea id="yaml" rows="16" class="mono" style="font-family:var(--mono);font-size:12.5px;min-height:300px">bundle: retail-dw
targets:
  dev:
    workspace: 零售数据智能
  prod:
    workspace: 零售数据智能
resources:
  flows:
    - name: 每日经营数据刷新
      schedule: "0 7 * * *"
  metrics:
    - gmv
    - order_cnt
  dashboards:
    - 零售经营总览
</textarea>
          <div class="row mt-12">
            <button class="btn" id="validate">校验 bundle</button>
            <button class="btn primary" id="deploy">发布到环境</button>
            <select id="env" style="width:140px"><option value="dev">dev</option><option value="staging">staging</option><option value="prod">prod</option></select>
          </div>
          <div id="cicd-out" class="mt-16"></div>`, { sub: '配置即代码：YAML 描述资源，CLI 执行 init / validate / deploy' })}
      </div>
      <div class="col" style="gap:16px">
        ${card('环境', tableHTML(['环境', '状态', '最近发布'], [
          ['dev', '可用', '今天 09:12'],
          ['staging', '可用', '昨天 18:40'],
          ['prod', '可用', '11-07 21:05'],
        ], { raw: [] }), { flush: true })}
        ${card('当前资源', tableHTML(['资源', '数量'], [
          ['工作流', String(flows.length)],
          ['指标', '—'],
          ['仪表盘', '—'],
        ]), { flush: true })}
      </div>
    </div>`;

  page.querySelector('#validate').addEventListener('click', () => {
    const text = page.querySelector('#yaml').value;
    const issues = [];
    if (!/bundle:/.test(text)) issues.push('缺少 bundle 字段');
    if (!/targets:/.test(text)) issues.push('缺少 targets 字段');
    if (!/resources:/.test(text)) issues.push('缺少 resources 字段');
    const out = page.querySelector('#cicd-out');
    out.innerHTML = issues.length
      ? `<div class="tag red">校验失败</div><ul class="tiny mt-8" style="color:var(--error)">${issues.map((i) => `<li>${esc(i)}</li>`).join('')}</ul>`
      : `<div class="tag green">校验通过</div><div class="tiny dim mt-8">3 个资源配置可被解析，可执行发布。</div>`;
  });
  page.querySelector('#deploy').addEventListener('click', async () => {
    const env = page.querySelector('#env').value;
    await confirmDialog('发布确认', `将把 bundle 中声明的资源配置发布到 ${env} 环境。开源版本地执行，不会影响外部系统。`, '发布');
    page.querySelector('#cicd-out').innerHTML = `<div class="tag green">已发布到 ${env}</div>
      <div class="tiny dim mt-8">${new Date().toLocaleString('zh-CN')} · 资源 3 项 · 耗时 0.4s</div>`;
    toast('发布完成', 'ok');
  });
}

/* -------------------------------------------------------------- 运维监控 */
export async function renderOps(page, ctx) {
  page.innerHTML = loading();
  const { flows } = await api.get('/flows');
  const runs = [];
  for (const f of flows.slice(0, 8)) {
    try {
      const out = await api.get(`/flows/${f.id}/runs`);
      (out.runs || []).forEach((r) => runs.push({ ...r, flow_name: f.name, flow_id: f.id }));
    } catch { /* ignore */ }
  }
  runs.sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)));
  const trace = await api.get('/ops/traces').catch(() => null);

  page.innerHTML = `
    <div class="stat-row">
      ${['运行实例', '成功', '失败', '平均耗时'].map((label, i) => {
        const total = runs.length;
        const ok = runs.filter((r) => r.status === 'success').length;
        const fail = runs.filter((r) => r.status === 'failed').length;
        const vals = [total, ok, fail, trace?.avg_ms ? `${trace.avg_ms} ms` : '-'];
        return `<div class="stat"><span class="label">${label}</span><span class="value">${vals[i]}</span>
          <span class="foot">${i === 3 ? '工具调用平均耗时' : '最近 20 次运行'}</span></div>`;
      }).join('')}
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">运行实例</span>
        <span class="dim tiny">四层视图：实例 → 记录 → 详情 → 任务</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="runs"></div>
    </div>

    <div class="grid-2 mt-16">
      ${card('工具调用轨迹', trace?.tools?.length
        ? tableHTML(['工具', '调用', '成功率', '平均耗时'], trace.tools.map((t) => [t.tool, t.calls, fmt.pct(t.success_rate), `${t.avg_ms} ms`]), { raw: [] })
        : emptyState('暂无轨迹'), { sub: '用于定位慢工具与失败点', flush: true })}
      ${card('AI 诊断', `<div class="tiny muted" style="line-height:1.9">
        选择左侧任一条失败实例，点击「AI 诊断」可得到：<br>
        · 根因定位（对象不存在 / 类型不匹配 / 绑定失败）<br>
        · 影响范围评估<br>
        · 修复建议（重跑 / 终止 / 修改后重跑）<br><br>
        未接入模型时，由内置规则引擎按错误特征匹配给出建议。</div>`)}
    </div>`;

  page.querySelector('#runs').innerHTML = dataTable({
    rows: runs,
    columns: [
      { title: '实例 ID', render: (r) => `<span class="mono tiny">${esc(r.id)}</span>` },
      { title: '工作流', render: (r) => `<span class="cell-main">${esc(r.flow_name)}</span>` },
      { title: '状态', render: (r) => statusTag(r.status) },
      { title: '触发', render: (r) => ({ schedule: '定时', manual: '手动', agent: 'Agent' }[r.trigger] || r.trigger) },
      { title: '开始时间', render: (r) => `<span class="dim">${fmt.dt(r.started_at)}</span>` },
      { title: '耗时', render: (r) => {
          if (!r.finished_at) return '<span class="dim">-</span>';
          const ms = new Date(r.finished_at) - new Date(r.started_at);
          return ms > 0 ? `${(ms / 1000).toFixed(2)}s` : '-';
        } },
      { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-log="${r.id}">日志</a><a class="btn-link" data-diagnose="${r.id}">AI 诊断</a>` },
    ],
    emptyText: '还没有运行实例', emptyHint: '到「工作流编排」运行一次工作流',
  });

  page.querySelectorAll('[data-log]').forEach((b) =>
    b.addEventListener('click', () => showRun(runs.find((r) => r.id === b.dataset.log)))
  );
  page.querySelectorAll('[data-diagnose]').forEach((b) =>
    b.addEventListener('click', async () => {
      const run = runs.find((r) => r.id === b.dataset.diagnose);
      const failed = (run.log || []).find((e) => e.status === 'failed');
      if (!failed) {
        return modal({ title: 'AI 诊断', body: `<div class="tag green">本次运行没有失败节点</div>`, actions: [{ label: '关闭', primary: true }] });
      }
      modal({
        title: 'AI 诊断',
        wide: true,
        body: `<div class="col" style="gap:12px">
          <div class="info-strip"><span class="tag orange">根因</span>${esc(failed.message || '节点执行失败')}</div>
          <div><div class="tiny dim">失败节点</div><div class="cell-main">${esc(failed.name)}</div></div>
          <div><div class="tiny dim">影响范围</div><div class="tiny">下游 ${run.log.length - 1} 个节点未执行</div></div>
          <div><div class="tiny dim">建议</div><ul class="tiny" style="line-height:1.9">
            <li>核对表名 / 字段名拼写，确认 catalog.schema.table 完整路径</li>
            <li>用「数据目录」确认字段类型后再比较</li>
            <li>修复后重跑该实例（幂等写入）</li></ul></div>
          ${failed.sql ? `<pre class="code">${esc(failed.sql)}</pre>` : ''}
        </div>`,
        actions: [{ label: '关闭' }, { label: '重跑', primary: true, onClick: async () => { await api.post(`/flows/${run.flow_id}/run`, {}); toast('已重新提交', 'ok'); } }],
      });
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

/* -------------------------------------------------------------- 计算资源 */
export async function renderCompute(page, ctx) {
  page.innerHTML = loading();
  const { resources, summary, types } = await api.get('/compute-resources');
  ctx.actions(el('div', { class: 'row' }, [el('button', { class: 'btn primary', onclick: () => newResource(ctx, types) }, '＋ 创建计算资源')]));

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">计算资源</span><span class="value">${summary.count}</span><span class="foot">四类集群</span></div>
      <div class="stat"><span class="label">运行中</span><span class="value">${summary.running}</span><span class="foot">含常驻集群</span></div>
      <div class="stat"><span class="label">CU 配额</span><span class="value">${summary.total_cu}<small>CU</small></span><span class="foot">1 CU ≈ 1 vCPU + 4 GB</span></div>
      <div class="stat"><span class="label">计费</span><span class="value" style="font-size:18px">本地运行</span><span class="foot"><span class="tag green">不产生费用</span></span></div>
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">计算资源列表</span>
        <span class="dim tiny">连通性测试仅支持平台集群 / 实时集群</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="res-table"></div>
    </div>

    <div class="grid-2 mt-16">
      ${card('集群类型与用途', tableHTML(['类型', '引擎', '适用任务'], Object.entries(types).map(([, v]) => [v.label, v.engine, v.use]), { raw: [] }), { flush: true })}
      ${card('网络设置', `<div class="tiny muted" style="line-height:1.9">
        平台集群与实时集群需要绑定 VPC 与子网；开源版本地运行，不需要网络打通。<br>
        出站：计算资源 → 数据源（数据接入、External Catalog）<br>
        入站：外部应用 → 计算资源（JDBC / ODBC）</div>`)}
    </div>`;

  page.querySelector('#res-table').innerHTML = dataTable({
    rows: resources,
    columns: [
      { title: '资源名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div><div class="cell-sub">${esc(r.description || '')}</div>` },
      { title: '类型', render: (r) => tag(r.type_label, 'outline') },
      { title: '引擎 / 镜像', render: (r) => `${esc(r.engine)}<div class="cell-sub mono">${esc(r.image)}</div>` },
      { title: 'CU', align: 'right', width: '80px', render: (r) => r.cu },
      { title: '网络', render: (r) => r.network ? `<span class="mono tiny">${esc(r.network)}</span>` : '<span class="dim">-</span>' },
      { title: '状态', render: (r) => statusTag(r.status) },
      { title: '操作', key: 'ops', render: (r) => {
          const ops = [`<a class="btn-link" data-edit="${r.id}">编辑</a>`];
          if (r.type === 'interactive') {
            ops.push(r.status === 'running'
              ? `<a class="btn-link" data-stop="${r.id}">停止</a>`
              : `<a class="btn-link" data-start="${r.id}">启动</a>`);
          }
          ops.push(`<a class="btn-link danger" data-del="${r.id}">删除</a>`);
          return ops.join('');
        } },
    ],
    emptyText: '还没有计算资源', emptyHint: '创建作业集群后即可跑 Notebook 与 SQL 任务',
  });

  const act = (id, action) => async () => {
    try {
      await api.post(`/compute-resources/${id}/action`, { action });
      toast(action === 'start' ? '已启动' : '已停止', 'ok');
      ctx.refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
  page.querySelectorAll('[data-start]').forEach((b) => b.addEventListener('click', act(b.dataset.start, 'start')));
  page.querySelectorAll('[data-stop]').forEach((b) => b.addEventListener('click', act(b.dataset.stop, 'stop')));
  page.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', async () => {
    if (!(await confirmDialog('删除计算资源', '删除后该资源不可恢复，确认？', '删除'))) return;
    await api.del(`/compute-resources/${b.dataset.del}`);
    ctx.refresh();
  }));
  page.querySelectorAll('[data-edit]').forEach((b) => b.addEventListener('click', () => {
    const r = resources.find((x) => x.id === b.dataset.edit);
    editResource(r, ctx);
  }));
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function newResource(ctx, types) {
  const data = await formModal({
    title: '创建计算资源',
    wide: true,
    fields: [
      { label: '资源名称', name: 'name', required: true, placeholder: 'demo_job_cluster', hint: '中英文、数字、- 与 _，≤ 50 字符' },
      { label: '资源描述', name: 'description' },
      { label: '资源类型', name: 'type', type: 'select', options: Object.entries(types).map(([k, v]) => ({ value: k, label: v.label })) },
      { label: '资源配额', name: 'cu', type: 'number', value: 64, min: 16, max: 1024, hint: '16 ~ 1024 CU，步长 1' },
      { label: '镜像版本', name: 'image', type: 'select', options: ['Standard-S 1.1', 'Standard-L 1.1', 'ML-S 2.0'] },
      { label: '网络（VPC / 子网）', name: 'network', placeholder: '平台 / 实时集群必填', hint: '开源版本地运行，可留空' },
    ],
    onSubmit: async (d) => { await api.post('/compute-resources', d); toast('创建成功', 'ok'); },
  });
  if (data) ctx.refresh();
}

async function editResource(r, ctx) {
  const data = await formModal({
    title: `编辑 ${r.name}`,
    fields: [
      { label: '资源名称', name: 'name', value: r.name, required: true },
      { label: '资源描述', name: 'description', value: r.description },
      { label: '资源配额', name: 'cu', type: 'number', value: r.cu, min: 1, max: 1024 },
      { label: '镜像版本', name: 'image', value: r.image },
      { label: '网络', name: 'network', value: r.network },
    ],
    onSubmit: async (d) => {
      await api.patch(`/compute-resources/${r.id}`, { ...d, type: r.type });
      toast('已保存', 'ok');
    },
  });
  if (data) ctx.refresh();
}
