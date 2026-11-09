// 各功能页面
import { api, session, fmt } from './api.js';
import {
  $, $$, el, esc, md, toast, modal, confirmDialog, drawer, tableHTML,
  loading, empty, levelBadge,
} from './ui.js';
import { chartHTML } from './charts.js';
import { icon } from './icons.js';
import { artifactHTML } from './artifact.js';

const card = (title, body, sub = '', actions = '') => `
  <div class="card">
    <div class="card-head">
      <div><h3>${esc(title)}</h3>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>
      ${actions}
    </div>
    ${body}
  </div>`;

const kpi = (label, value, foot = '') => `
  <div class="card tight"><div class="kpi">
    <span class="label">${esc(label)}</span>
    <span class="value">${esc(value)}</span>
    ${foot ? `<span class="foot">${esc(foot)}</span>` : ''}
  </div></div>`;

function btn(label, cls = 'btn sm') {
  return `<button class="${cls}">${esc(label)}</button>`;
}

// ==================================================================== 总览
export async function renderOverview(page, ctx) {
  page.innerHTML = loading();
  const data = await api.get('/overview');
  const s = data.stats;
  const llmBanner = data.llm_ready
    ? ''
    : `<div class="card" style="border-color:#f0d9ae;background:#fffdf6">
        <div class="row" style="justify-content:space-between;gap:12px">
          <div><strong style="font-size:13.5px">尚未接入大模型</strong>
          <div class="tiny muted" style="margin-top:3px">当前使用内置规则引擎，问数、扫描、建任务可用；开放式追问需要模型。填入 DeepSeek API Key 即可解锁。</div></div>
          <a class="btn primary sm" href="#/settings">去配置</a>
        </div></div>`;

  const runRows = (data.recent_runs || []).map((r) => [
    r.flow_name,
    r.status === 'success' ? '成功' : r.status === 'failed' ? '失败' : r.status,
    r.trigger === 'schedule' ? '定时' : r.trigger === 'agent' ? 'Agent' : '手动',
    fmt.dt(r.started_at),
  ]);

  page.innerHTML = `
    <div class="col" style="gap:16px">
      ${llmBanner}
      <div class="grid-4">
        ${kpi('数据表', fmt.num(s.tables), `${fmt.num(s.rows)} 行数据`)}
        ${kpi('注册指标', fmt.num(s.metrics), '语义层口径')}
        ${kpi('工作流', fmt.num(s.flows), `${s.enabled_flows} 个已启用调度`)}
        ${kpi('敏感字段', fmt.num(s.sensitive_columns), 'L3/L4 分级')}
        ${kpi('质量通过率', s.quality_pass_rate === null ? '-' : fmt.pct(s.quality_pass_rate), '最近一次执行')}
        ${kpi('会话数', fmt.num(s.conversations), 'Agent 交互记录')}
        ${kpi('仪表盘', fmt.num(s.dashboards), '可分享看板')}
        ${kpi('工具调用', fmt.num(data.trace?.calls || 0), data.trace?.success_rate == null ? '尚无轨迹' : `成功率 ${fmt.pct(data.trace.success_rate)}`)}
      </div>

      <div class="grid-2">
        ${card('最近工作流运行', (runRows.length
          ? tableHTML(['工作流', '状态', '触发', '开始时间'], runRows)
          : empty('还没有运行记录', '到「工作流」里点一次运行试试')), '', '<a class="btn sm" href="#/flows">全部 →</a>')}
        ${card('工具调用分布', (data.trace?.tools?.length
          ? tableHTML(['工具', '调用', '成功率', '平均耗时'],
              data.trace.tools.map((t) => [t.tool, t.calls, fmt.pct(t.success_rate), `${t.avg_ms}ms`]))
          : empty('还没有工具调用轨迹', '和 Buddy 聊两句就会产生')), 'Harness 轨迹评测')}
      </div>

      <div class="grid-2">
        ${card('主要数据表', (data.tables?.length
          ? tableHTML(['表', '行数', '说明'],
              data.tables.map((t) => [t.ref, fmt.num(t.row_count), t.description || '-']))
          : empty('还没有数据', '到「数据接入」上传一份 CSV')), '', '<a class="btn sm" href="#/catalog">数据目录 →</a>')}
        ${card('快捷入口', `<div class="grid-2" style="gap:10px">
          ${[
            ['#/buddy', '问一个数', '用自然语言查指标'],
            ['#/ingest', '接入数据', 'CSV / Excel / Parquet'],
            ['#/governance', '做治理', '敏感字段扫描与打标'],
            ['#/semantic', '定口径', '注册指标避免口径漂移'],
          ].map(([href, t, d]) => `<a class="list-item" href="${href}" style="text-decoration:none">
            <div class="meta"><strong>${esc(t)}</strong><span>${esc(d)}</span></div><span class="muted">→</span></a>`).join('')}
        </div>`, '常用动作')}
      </div>
    </div>`;
}

// ================================================================== 数据目录
export async function renderCatalog(page, ctx) {
  page.innerHTML = loading();
  const [{ catalogs }, { tables }] = await Promise.all([
    api.get('/catalog/tree'),
    api.get('/catalog/tables'),
  ]);

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn sm', onclick: () => promptCatalog() }, '新建 Catalog'),
      el('button', { class: 'btn sm', onclick: () => promptSchema(catalogs) }, '新建 Schema'),
    ])
  );

  const treeHTML = (catalogs || [])
    .map(
      (c) => `<div class="tree-node" data-cat="${esc(c.name)}"><span class="ico">${icon('catalog', 14)}</span>${esc(c.name)}</div>
      <div class="tree-children">
        ${(c.schemas || [])
          .map(
            (s) => `<div class="tree-node" data-schema="${esc(c.name)}.${esc(s.name)}"><span class="ico">${icon('folder', 13)}</span>${esc(s.name)}</div>
            <div class="tree-children">
              ${(s.tables || [])
                .map(
                  (t) => `<div class="tree-node" data-table="${esc(c.name)}.${esc(s.name)}.${esc(t.name)}">
                    <span class="ico">${icon('table', 13)}</span><span style="flex:1">${esc(t.name)}</span>
                    <span class="tiny muted">${fmt.num(t.row_count)}</span></div>`
                )
                .join('')}
            </div>`
          )
          .join('')}
      </div>`
    )
    .join('');

  page.innerHTML = `<div class="grid-2" style="grid-template-columns:280px 1fr;align-items:start">
    <div class="card tight">
      <div class="card-head"><h3>目录树</h3><span class="tiny muted">${tables.length} 张表</span></div>
      <div class="tree" id="tree">${treeHTML || empty('还没有 Catalog')}</div>
    </div>
    <div id="detail">${empty('选择左侧任意表', '查看字段、标签、样例行与血缘')}</div>
  </div>`;

  const detail = page.querySelector('#detail');
  page.querySelectorAll('[data-table]').forEach((node) => {
    node.addEventListener('click', () => {
      page.querySelectorAll('.tree-node').forEach((n) => n.classList.remove('active'));
      node.classList.add('active');
      showTable(detail, node.dataset.table);
    });
  });
  const first = page.querySelector('[data-table]');
  if (first) {
    first.classList.add('active');
    showTable(detail, first.dataset.table);
  }
}

async function showTable(host, ref) {
  host.innerHTML = loading('加载表详情…');
  const { table } = await api.get(`/catalog/table?ref=${encodeURIComponent(ref)}`);
  const preview = await api.get(`/catalog/preview?ref=${encodeURIComponent(ref)}&limit=8`).catch(() => null);
  const tagRows = table.columns
    .filter((c) => (c.tags || []).length)
    .map((c) => [c.name, (c.tags || []).map((t) => `${t.tag} · ${t.level}`).join('、'), (c.tags || [])[0]?.evidence || '手工标注']);

  host.innerHTML = `
    <div class="col" style="gap:16px">
      ${card(table.ref, `<div class="stat-strip" style="margin-bottom:12px">
          <div class="item"><div class="kpi"><span class="label">行数</span><span class="value" style="font-size:18px">${fmt.num(table.row_count)}</span></div></div>
          <div class="item"><div class="kpi"><span class="label">字段</span><span class="value" style="font-size:18px">${table.columns.length}</span></div></div>
          <div class="item"><div class="kpi"><span class="label">敏感字段</span><span class="value" style="font-size:18px">${table.columns.filter((c) => (c.tags || []).some((t) => ['L3', 'L4'].includes(t.level))).length}</span></div></div>
          <div class="item" style="flex:1"><div class="kpi"><span class="label">描述</span><span style="font-size:12.5px;color:var(--ink-2)">${esc(table.description || '无描述')}</span></div></div>
        </div>
        ${tableHTML(['字段', '类型', '标签', '说明'], table.columns.map((c) => [
          c.name, c.data_type,
          (c.tags || []).map((t) => `${t.tag}(${t.level})`).join('、') || '-',
          c.description || '-',
        ]))}`, 'Catalog.Schema.Table', `<button class="btn sm" onclick="window.__dropTable('${esc(table.id)}')">删除表</button>`)}
      ${tagRows.length ? card('敏感标注明细', tableHTML(['字段', '标签', '依据'], tagRows), '用于脱敏与访问控制') : ''}
      ${preview ? card('样例数据', tableHTML(preview.columns, preview.rows, { max: 8 }), `前 8 行`) : ''}
      ${table.lineage?.length ? card('血缘', tableHTML(['上游', '下游', '类型'],
          table.lineage.map((l) => [l.upstream, l.downstream, l.kind])), '字段级血缘可在 v0.2 扩展') : ''}
    </div>`;
}

window.__dropTable = async (id) => {
  if (!(await confirmDialog('删除表', '删除后数据不可恢复，确认继续？', '删除'))) return;
  try {
    await api.del(`/catalog/tables/${id}`);
    toast('表已删除', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
};

async function promptCatalog() {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>Catalog 名称</label><input id="c-name" placeholder="例如 sales" /></div>
    <div class="field"><label>描述</label><input id="c-desc" placeholder="可选" /></div>`;
  const ok = await modal({ title: '新建 Catalog', body, actions: [{ label: '取消', value: false }, { label: '创建', primary: true, value: true }] });
  if (!ok) return;
  try {
    await api.post('/catalog/catalogs', { name: body.querySelector('#c-name').value, description: body.querySelector('#c-desc').value });
    toast('Catalog 已创建', 'ok');
    location.hash = '#/catalog';
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function promptSchema(catalogs) {
  if (!catalogs?.length) return toast('请先创建 Catalog', 'err');
  const body = el('div');
  body.innerHTML = `<div class="field"><label>所属 Catalog</label>
      <select id="s-cat">${catalogs.map((c) => `<option>${esc(c.name)}</option>`).join('')}</select></div>
    <div class="field"><label>Schema 名称</label><input id="s-name" placeholder="例如 dwd" /></div>`;
  const ok = await modal({ title: '新建 Schema', body, actions: [{ label: '取消', value: false }, { label: '创建', primary: true, value: true }] });
  if (!ok) return;
  try {
    await api.post('/catalog/schemas', { catalog: body.querySelector('#s-cat').value, name: body.querySelector('#s-name').value });
    toast('Schema 已创建', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ================================================================== 数据接入
export async function renderIngest(page, ctx) {
  const [{ tables }, { sources }] = await Promise.all([api.get('/catalog/tables'), api.get('/sources')]);
  const targets = [...new Set(tables.map((t) => `${t.catalog_name}.${t.schema_name}`))];
  if (!targets.length) targets.push('demo.retail');

  page.innerHTML = `
    <div class="tabs" id="tabs">
      <button class="active" data-tab="upload">文件上传</button>
      <button data-tab="sql">SQL 建表</button>
      <button data-tab="source">数据源</button>
    </div>
    <div id="tab-body"></div>`;

  const body = page.querySelector('#tab-body');
  const tabs = page.querySelectorAll('#tabs button');

  const views = {
    upload: () => {
      body.innerHTML = `<div class="grid-2" style="grid-template-columns:1fr 380px;align-items:start">
        ${card('上传文件自动建表', `
          <div class="field"><label>目标位置（Catalog.Schema）</label>
            <select id="target">${targets.map((t) => `<option>${esc(t)}</option>`).join('')}</select>
            <span class="hint">不存在会自动创建</span></div>
          <div class="field"><label>表名（留空用文件名）</label><input id="tname" placeholder="例如 orders_2026" /></div>
          <div class="field"><label>描述</label><input id="tdesc" placeholder="可选" /></div>
          <div class="field"><label>选择文件</label>
            <input type="file" id="file" accept=".csv,.json,.ndjson,.jsonl,.parquet,.xlsx" />
            <span class="hint">支持 CSV / JSON / NDJSON / Parquet / XLSX，单文件 ≤ 200MB，自动推断字段类型</span></div>
          <button class="btn primary" id="do-upload">上传并建表</button>
          <div id="upload-result" style="margin-top:14px"></div>`, '零依赖解析，XLSX 也能直接读')}
        ${card('当前数据表', tableHTML(['表', '行数'], tables.map((t) => [t.ref, fmt.num(t.row_count)])), `${tables.length} 张`)}
      </div>`;
      body.querySelector('#do-upload').addEventListener('click', async (e) => {
        const fileInput = body.querySelector('#file');
        if (!fileInput.files.length) return toast('请选择文件', 'err');
        const form = new FormData();
        form.append('file', fileInput.files[0]);
        form.append('target', body.querySelector('#target').value);
        form.append('table', body.querySelector('#tname').value);
        form.append('description', body.querySelector('#tdesc').value);
        e.target.disabled = true;
        e.target.textContent = '上传解析中…';
        const result = body.querySelector('#upload-result');
        try {
          const out = await api.upload('/ingest/upload', form);
          result.innerHTML = `<div class="badge ok">建表成功</div>
            <div class="tiny" style="margin-top:8px">${esc(out.ref)} · ${fmt.num(out.row_count)} 行 · ${out.columns} 个字段</div>`;
          toast('接入完成', 'ok');
          setTimeout(() => renderIngest(page, ctx), 900);
        } catch (err) {
          result.innerHTML = `<div class="badge err">失败</div><div class="tiny" style="margin-top:8px">${esc(err.message)}</div>`;
          e.target.disabled = false;
          e.target.textContent = '上传并建表';
        }
      });
    },
    sql: () => {
      body.innerHTML = card('用 SQL 建表 / 视图', `
        <div class="field"><label>目标位置</label><select id="s-target">${targets.map((t) => `<option>${esc(t)}</option>`).join('')}</select></div>
        <div class="field"><label>表名</label><input id="s-table" placeholder="例如 daily_gmv" /></div>
        <div class="field"><label>SELECT 语句</label>
          <textarea id="s-sql" rows="8" class="mono" style="font-family:var(--mono);font-size:12.5px">SELECT order_date, sum(amount) AS gmv
FROM demo.retail.fact_orders
GROUP BY 1</textarea></div>
        <button class="btn primary" id="do-sql">执行并建表</button>
        <div id="sql-result" style="margin-top:12px"></div>`);
      body.querySelector('#do-sql').addEventListener('click', async (e) => {
        e.target.disabled = true;
        try {
          const out = await api.post('/ingest/from-sql', {
            target: body.querySelector('#s-target').value,
            table: body.querySelector('#s-table').value,
            sql: body.querySelector('#s-sql').value,
          });
          body.querySelector('#sql-result').innerHTML = `<div class="badge ok">已生成 ${esc(out.ref)}</div>`;
          toast('建表成功', 'ok');
        } catch (err) {
          body.querySelector('#sql-result').innerHTML = `<div class="badge err">失败</div><div class="tiny" style="margin-top:6px">${esc(err.message)}</div>`;
          e.target.disabled = false;
        }
      });
    },
    source: () => {
      const rows = (sources || []).map((s) => [
        s.name,
        s.type,
        `<span class="badge ${s.status === 'ok' ? 'ok' : s.status === 'failed' ? 'err' : ''}">${s.status === 'ok' ? '连通' : s.status === 'failed' ? '不通' : '未测试'}</span>`,
        fmt.dt(s.last_test_at),
        `<button class="btn sm" data-test="${s.id}">测试</button> <button class="btn sm danger" data-del-src="${s.id}">删除</button>`,
      ]);
      body.innerHTML = `<div class="col" style="gap:16px">
        ${card('已注册数据源', rows.length
          ? tableHTML(['名称', '类型', '状态', '最近测试', '操作'], rows, { raw: [2, 4] })
          : empty('还没有数据源', '注册后可复用到多张表的接入'), '文件 / SQLite / PostgreSQL / MySQL',
          '<button class="btn primary sm" id="new-src">＋ 注册数据源</button>')}
        ${card('说明', `<div class="tiny muted" style="line-height:1.8">
          本开源版把数据落到本地 DuckDB 湖仓，所以「数据源」主要用于登记来源与做连通性巡检。<br>
          生产接入建议：先用导出或 CDC 把数据落到 CSV/Parquet，再用「文件上传」建表；需要直连时可在 v0.2 挂 DuckDB 的 postgres/mysql 扩展。</div>`)}
      </div>`;
      body.querySelector('#new-src').addEventListener('click', () => sourceModal());
      $$('[data-test]', body).forEach((b) =>
        b.addEventListener('click', async () => {
          b.disabled = true;
          try {
            const out = await api.post(`/sources/${b.dataset.test}/test`, {});
            toast(out.message, out.ok ? 'ok' : 'err');
            renderIngest(page, ctx);
          } catch (e) {
            toast(e.message, 'err');
            b.disabled = false;
          }
        })
      );
      $$('[data-del-src]', body).forEach((b) =>
        b.addEventListener('click', async () => {
          await api.del(`/sources/${b.dataset.delSrc}`);
          renderIngest(page, ctx);
        })
      );
    },
  };

  tabs.forEach((t) =>
    t.addEventListener('click', () => {
      tabs.forEach((x) => x.classList.remove('active'));
      t.classList.add('active');
      views[t.dataset.tab]();
    })
  );
  views.upload();
}

async function sourceModal() {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>名称</label><input id="n" placeholder="例如 生产库只读" /></div>
    <div class="field"><label>类型</label><select id="t">
      <option value="file">文件目录</option><option value="sqlite">SQLite</option>
      <option value="postgres">PostgreSQL</option><option value="mysql">MySQL</option></select></div>
    <div class="field"><label>路径 / 主机</label><input id="p" placeholder="D:/data 或 10.0.0.12" /></div>
    <div class="field"><label>端口（数据库类型需要）</label><input id="port" placeholder="5432" /></div>`;
  const ok = await modal({ title: '注册数据源', body, actions: [{ label: '取消', value: false }, { label: '保存', primary: true, value: true }] });
  if (!ok) return;
  const type = body.querySelector('#t').value;
  const p = body.querySelector('#p').value.trim();
  const config = type === 'file' || type === 'sqlite' ? { path: p } : { host: p, port: body.querySelector('#port').value || undefined };
  try {
    await api.post('/sources', { name: body.querySelector('#n').value, type, config, description: '' });
    toast('已注册', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ================================================================== SQL 工作台
export async function renderSql(page, ctx) {
  page.innerHTML = `<div class="grid-2" style="grid-template-columns:1fr 300px;align-items:start">
    ${card('编辑器', `<textarea id="sql" class="mono" rows="10" style="font-family:var(--mono);font-size:12.8px">SELECT
  c.channel_name,
  round(sum(o.amount), 2) AS gmv,
  count(DISTINCT o.order_id) AS orders
FROM demo.retail.fact_orders o
JOIN demo.retail.dim_channel c ON c.channel_id = o.channel_id
GROUP BY 1
ORDER BY gmv DESC</textarea>
      <div class="row" style="margin-top:10px">
        <button class="btn primary sm" id="run">运行 (Ctrl+Enter)</button>
        <span class="tiny muted">只允许只读 SELECT · 结果最多 500 行</span>
      </div>`, '表名支持 catalog.schema.table 或裸表名', '')}
    <div id="side"></div>
  </div>
  <div id="result" style="margin-top:16px"></div>`;

  const resultHost = page.querySelector('#result');
  const textarea = page.querySelector('#sql');
  const run = async () => {
    resultHost.innerHTML = loading('执行中…');
    try {
      const out = await api.post('/sql/run', { sql: textarea.value, limit: 500 });
      const chart = chartHTML(out.chart, out.columns, out.rows);
      resultHost.innerHTML = card(
        '查询结果',
        `${chart}${tableHTML(out.columns, out.rows, { max: 100 })}`,
        `${out.row_count} 行 · ${out.elapsed_ms}ms${out.truncated ? ' · 已截断' : ''}`,
        `<span class="badge">${esc(out.chart.type)}</span>`
      );
    } catch (err) {
      resultHost.innerHTML = `<div class="card" style="border-color:#f3cfcc"><div class="badge err">执行失败</div>
        <pre class="code" style="margin-top:10px">${esc(err.message)}</pre></div>`;
    }
  };
  page.querySelector('#run').addEventListener('click', run);
  textarea.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') run();
  });

  const { tables } = await api.get('/catalog/tables');
  page.querySelector('#side').innerHTML = card(
    '可用表',
    `<div class="tree">${tables.map((t) => `<div class="tree-node" data-ins="${esc(t.ref)}">
      <span class="ico">${icon('table', 13)}</span><span style="flex:1">${esc(t.name)}</span>
      <span class="tiny muted">${fmt.num(t.row_count)}</span></div>`).join('')}</div>`,
    '点击插入表名',
    ''
  );
  page.querySelectorAll('[data-ins]').forEach((n) =>
    n.addEventListener('click', () => {
      const pos = textarea.selectionStart;
      textarea.value = textarea.value.slice(0, pos) + n.dataset.ins + textarea.value.slice(pos);
      textarea.focus();
    })
  );
  run();
}

// =================================================================== 工作流
export async function renderFlows(page, ctx) {
  page.innerHTML = loading();
  const { flows } = await api.get('/flows');
  ctx.actions(el('button', { class: 'btn primary sm', onclick: () => flowModal() }, '＋ 新建工作流'));

  page.innerHTML = flows.length
    ? `<div class="col" style="gap:10px">${flows
        .map(
          (f) => `<div class="card tight">
            <div class="row" style="gap:12px">
              <div style="flex:1;min-width:0">
                <div class="row" style="gap:8px">
                  <strong style="font-size:14px">${esc(f.name)}</strong>
                  ${f.enabled ? '<span class="badge ok">调度中</span>' : '<span class="badge">未调度</span>'}
                  <span class="badge accent">${f.nodes} 节点</span>
                  ${f.last_run ? `<span class="badge ${f.last_run.status === 'success' ? 'ok' : f.last_run.status === 'failed' ? 'err' : ''}">上次 ${f.last_run.status === 'success' ? '成功' : f.last_run.status === 'failed' ? '失败' : f.last_run.status}</span>` : ''}
                </div>
                <div class="tiny muted" style="margin-top:4px">${esc(f.description || '无描述')} · ${esc(f.schedule_text)}${f.cron ? `（${esc(f.cron)}）` : ''}</div>
              </div>
              <button class="btn sm" data-detail="${f.id}">详情</button>
              <button class="btn sm primary" data-run="${f.id}">运行</button>
              <button class="btn sm danger" data-del="${f.id}">删除</button>
            </div>
          </div>`
        )
        .join('')}</div>`
    : empty('还没有工作流', '点右上角新建，或用 Buddy 一句话生成');

  $$('[data-run]', page).forEach((b) =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      b.textContent = '运行中…';
      try {
        const out = await api.post(`/flows/${b.dataset.run}/run`, {});
        const run = out.run;
        toast(`运行${run.status === 'success' ? '成功' : '失败'}`, run.status === 'success' ? 'ok' : 'err');
        showRunDetail(b.dataset.run, run);
      } catch (e) {
        toast(e.message, 'err');
      } finally {
        b.disabled = false;
        b.textContent = '运行';
      }
    })
  );
  $$('[data-detail]', page).forEach((b) => b.addEventListener('click', () => flowDetailDrawer(b.dataset.detail)));
  $$('[data-del]', page).forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除工作流', '会同时删除运行历史，确认？', '删除'))) return;
      await api.del(`/flows/${b.dataset.del}`);
      renderFlows(page, ctx);
    })
  );
}

async function flowDetailDrawer(id) {
  const { flow } = await api.get(`/flows/${id}`);
  const nodes = `<div class="col" style="gap:8px">${flow.nodes
    .map(
      (n, i) => `<div class="list-item"><span class="avatar" style="width:22px;height:22px;font-size:11px">${i + 1}</span>
      <div class="meta"><strong>${esc(n.name)}</strong><span>${esc(n.type)} · ${esc((n.config?.sql || n.config?.url || '').slice(0, 60))}</span></div></div>`
    )
    .join('')}</div>`;
  const runs = flow.runs?.length
    ? flow.runs
        .map(
          (r) => `<div class="list-item"><div class="meta">
            <strong>${r.status === 'success' ? '✓ 成功' : r.status === 'failed' ? '✕ 失败' : r.status} · ${esc(r.trigger)}</strong>
            <span>${fmt.dt(r.started_at)} → ${fmt.dt(r.finished_at)}</span></div>
            <button class="btn sm" data-show-run="${r.id}">日志</button></div>`
        )
        .join('')
    : empty('还没有运行记录');

  const host = el('div');
  host.innerHTML = `<div class="col" style="gap:16px">
    <div>${card('节点（按拓扑顺序执行）', nodes, esc(flow.description || ''))}</div>
    <div>${card('最近运行', runs, esc(flow.schedule_text))}</div>
  </div>`;
  const d = drawer({ title: flow.name, body: host });
  host.querySelectorAll('[data-show-run]').forEach((b) =>
    b.addEventListener('click', () => {
      const run = flow.runs.find((r) => r.id === b.dataset.showRun);
      showRunDetail(id, run);
    })
  );
}

function showRunDetail(flowId, run) {
  const log = run.log || [];
  const rows = log.map((e) => [
    e.name,
    e.status === 'success' ? '成功' : e.status === 'failed' ? '失败' : e.status,
    e.rows ?? '-',
    e.message || '',
    e.elapsed_ms ? `${e.elapsed_ms}ms` : '-',
  ]);
  const nodeCards = log
    .filter((e) => e.preview)
    .map((e) => `<div style="margin-top:12px"><div class="tiny muted" style="margin-bottom:6px">${esc(e.name)} 输出</div>
      ${tableHTML(e.preview.columns, e.preview.rows, { max: 6 })}</div>`)
    .join('');
  modal({
    title: `运行日志 · ${run.status === 'success' ? '成功' : '失败'}`,
    wide: true,
    body: `<div class="tiny muted" style="margin:6px 0 12px">${fmt.dt(run.started_at)}${run.finished_at ? ' → ' + fmt.dt(run.finished_at) : ''}</div>
      ${tableHTML(['节点', '状态', '行数', '信息', '耗时'], rows)}
      ${nodeCards}`,
    actions: [{ label: '关闭', primary: true }],
  });
}

async function flowModal() {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>名称</label><input id="f-name" placeholder="例如 每日经营刷新" /></div>
    <div class="field"><label>描述</label><input id="f-desc" placeholder="可选" /></div>
    <div class="field"><label>调度 cron（留空仅手动）</label><input id="f-cron" placeholder="0 7 * * *" /></div>
    <div class="field"><label>节点（每行一个，格式：名称 | SQL）</label>
      <textarea id="f-nodes" rows="6" class="mono" style="font-family:var(--mono);font-size:12px">汇总 | SELECT order_date, sum(amount) gmv FROM demo.retail.fact_orders GROUP BY 1
校验 | SELECT count(*) FROM demo.retail.fact_orders WHERE amount IS NULL</textarea>
      <span class="hint">按顺序串成 DAG，第二行起为「质量断言」，返回值需为 0</span></div>`;
  const ok = await modal({ title: '新建工作流', body, wide: true, actions: [{ label: '取消', value: false }, { label: '创建', primary: true, value: true }] });
  if (!ok) return;
  const nodes = body
    .querySelector('#f-nodes')
    .value.split('\n')
    .map((l) => l.split('|'))
    .filter((p) => p.length >= 2 && p[1].trim())
    .map((p, i) => ({ type: i === 0 ? 'sql' : 'quality', name: p[0].trim() || `步骤${i + 1}`, sql: p[1].trim() }));
  try {
    await api.post('/flows', {
      name: body.querySelector('#f-name').value,
      description: body.querySelector('#f-desc').value,
      cron: body.querySelector('#f-cron').value.trim(),
      enabled: !!body.querySelector('#f-cron').value.trim(),
      nodes,
    });
    toast('工作流已创建', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

// =================================================================== 仪表盘
export async function renderDashboards(page, ctx) {
  page.innerHTML = loading();
  const [{ dashboards }, { metrics }] = await Promise.all([api.get('/dashboards'), api.get('/metrics')]);
  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn sm', onclick: () => boardModal(metrics) }, '＋ 手工新建'),
      el('a', { class: 'btn primary sm', href: '#/buddy' }, '让 Buddy 生成'),
    ])
  );

  page.innerHTML = dashboards.length
    ? `<div class="col" style="gap:12px">${dashboards
        .map(
          (d) => `<div class="card tight"><div class="row">
            <div style="flex:1"><strong style="font-size:14px">${esc(d.name)}</strong>
              <div class="tiny muted" style="margin-top:3px">${esc(d.description || '无描述')} · ${d.card_count} 张卡片</div></div>
            <button class="btn sm" data-view="${d.id}">查看</button>
            <button class="btn sm danger" data-del-board="${d.id}">删除</button>
          </div></div>`
        )
        .join('')}</div>
      <div id="board" style="margin-top:18px"></div>`
    : `<div class="col">${empty('还没有仪表盘', '让 Buddy「生成一份经营看板」，或手工新建')}</div><div id="board"></div>`;

  $$('[data-view]', page).forEach((b) => b.addEventListener('click', () => viewBoard(page.querySelector('#board'), b.dataset.view)));
  $$('[data-del-board]', page).forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除看板', '确认删除？', '删除'))) return;
      await api.del(`/dashboards/${b.dataset.delBoard}`);
      renderDashboards(page, ctx);
    })
  );
  const first = page.querySelector('[data-view]');
  if (first) viewBoard(page.querySelector('#board'), first.dataset.view);
}

async function viewBoard(host, id) {
  host.innerHTML = loading('渲染看板…');
  const out = await api.get(`/dashboards/${id}`);
  const cards = (out.cards || [])
    .map((c) => {
      if (c.error) return `<div class="card"><div class="card-head"><h3>${esc(c.title)}</h3><span class="badge err">失败</span></div>
        <div class="tiny muted">${esc(c.error)}</div></div>`;
      const chart = chartHTML(c.chart, c.columns, c.rows);
      return `<div class="card">
        <div class="card-head"><div><h3>${esc(c.title)}</h3><div class="sub">${c.row_count} 行 · ${c.elapsed_ms}ms</div></div></div>
        ${chart}
        <details style="margin-top:8px"><summary class="tiny muted" style="cursor:pointer">明细</summary>
          <div style="margin-top:8px">${tableHTML(c.columns, c.rows, { max: 8 })}</div></details>
      </div>`;
    })
    .join('');
  host.innerHTML = `<div class="row" style="justify-content:space-between;margin-bottom:12px">
      <h3>${esc(out.dashboard.name)}</h3><span class="tiny muted">${esc(out.dashboard.description || '')}</span></div>
    <div class="grid-2">${cards}</div>`;
}

async function boardModal(metrics) {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>看板名称</label><input id="b-name" placeholder="例如 渠道日报" /></div>
    <div class="field"><label>指标</label><select id="b-metric">
      ${(metrics || []).map((m) => `<option value="${esc(m.name)}">${esc(m.display_name || m.name)}</option>`).join('')}</select></div>
    <div class="field"><label>维度</label><select id="b-dim">
      <option value="">总计</option><option value="__date__">按天趋势</option></select></div>
    <p class="tiny muted">会生成 2 张卡片：整体与按维度拆分。更复杂的看板建议直接用 Buddy 生成。</p>`;
  const ok = await modal({ title: '新建看板', body, actions: [{ label: '取消', value: false }, { label: '生成', primary: true, value: true }] });
  if (!ok) return;
  try {
    const name = body.querySelector('#b-name').value || '未命名看板';
    const metric = body.querySelector('#b-metric').value;
    const dim = body.querySelector('#b-dim').value;
    const preview = await api.post('/metrics/query', { metric, dimensions: [], granularity: null });
    const cards = [{ title: `${name} · 总计`, sql: preview.sql, chart: { type: 'kpi' } }];
    if (dim) {
      const p2 = await api.post('/metrics/query', { metric, dimensions: [dim], granularity: dim === '__date__' ? 'day' : null });
      cards.push({ title: `${name} · ${dim === '__date__' ? '趋势' : dim}`, sql: p2.sql, chart: p2.chart });
    }
    await api.post('/dashboards', { name, description: '由语义层指标生成', cards });
    toast('看板已生成', 'ok');
    renderDashboards(document.querySelector('#page'), { actions: () => {} });
  } catch (e) {
    toast(e.message, 'err');
  }
}

// =================================================================== 语义层
export async function renderSemantic(page, ctx) {
  page.innerHTML = loading();
  const { metrics } = await api.get('/metrics');
  ctx.actions(el('button', { class: 'btn primary sm', onclick: () => metricModal() }, '＋ 注册指标'));

  page.innerHTML = `<div class="col" style="gap:16px">
    ${card('已注册指标', metrics.length
      ? tableHTML(
          ['指标', '口径表达式', '单位', '可用维度', '负责人', '操作'],
          metrics.map((m) => [
            `${m.display_name || m.name}（${m.name}）`,
            m.expr,
            m.unit || '-',
            (m.dimensions || []).join('、') || '-',
            m.owner || '-',
            `<button class="btn sm" data-preview="${esc(m.name)}">预览</button> <button class="btn sm danger" data-del-m="${esc(m.id)}">删除</button>`,
          ]),
          { raw: [5] }
        )
      : empty('还没有指标', '注册后，问数与看板都会复用同一口径'), '口径收敛是准确率的前提')}
    <div id="preview"></div>
  </div>`;

  $$('[data-preview]', page).forEach((b) =>
    b.addEventListener('click', () => previewMetric(page.querySelector('#preview'), b.dataset.preview))
  );
  $$('[data-del-m]', page).forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除指标', '确认删除？', '删除'))) return;
      await api.del(`/metrics/${b.dataset.delM}`);
      renderSemantic(page, ctx);
    })
  );
  if (metrics.length) previewMetric(page.querySelector('#preview'), metrics[0].name);
}

async function previewMetric(host, name) {
  host.innerHTML = loading('按口径查询…');
  try {
    const out = await api.post('/metrics/query', { metric: name, dimensions: [], granularity: null });
    const trend = await api.post('/metrics/query', { metric: name, dimensions: ['__date__'], granularity: 'day' });
    host.innerHTML = `<div class="grid-2">
      ${card(`${out.metric.display_name || name} · 全量`, chartHTML(out.chart, out.columns, out.rows), `口径 ${out.metric.unit || ''}`)}
      ${card('按天趋势', chartHTML(trend.chart, trend.columns, trend.rows), '最近 500 个时间点')}
    </div>`;
  } catch (err) {
    host.innerHTML = `<div class="card"><div class="badge err">查询失败</div><div class="tiny" style="margin-top:6px">${esc(err.message)}</div></div>`;
  }
}

async function metricModal() {
  const { tables } = await api.get('/catalog/tables');
  const body = el('div');
  body.innerHTML = `<div class="field"><label>标识（英文）</label><input id="m-name" placeholder="例如 net_gmv" /></div>
    <div class="field"><label>中文名</label><input id="m-display" placeholder="例如 净GMV" /></div>
    <div class="field"><label>主表</label><select id="m-table">${tables.map((t) => `<option>${esc(t.ref)}</option>`).join('')}</select></div>
    <div class="field"><label>聚合表达式</label><input id="m-expr" class="mono" placeholder="sum(f.amount)" style="font-family:var(--mono)" />
      <span class="hint">别名固定为 f；需要维度表字段时系统会复用已有 join</span></div>
    <div class="field"><label>单位</label><input id="m-unit" placeholder="元 / % / 单" /></div>
    <div class="field"><label>可用维度（逗号分隔）</label><input id="m-dims" placeholder="order_date,channel_name,category" /></div>
    <div class="field"><label>说明</label><input id="m-desc" placeholder="口径说明，会进入知识库" /></div>`;
  const ok = await modal({ title: '注册指标', body, wide: true, actions: [{ label: '取消', value: false }, { label: '保存', primary: true, value: true }] });
  if (!ok) return;
  try {
    await api.post('/metrics', {
      name: body.querySelector('#m-name').value,
      display_name: body.querySelector('#m-display').value,
      table_ref: body.querySelector('#m-table').value,
      expr: body.querySelector('#m-expr').value,
      unit: body.querySelector('#m-unit').value,
      dimensions: body.querySelector('#m-dims').value.split(',').map((s) => s.trim()).filter(Boolean),
      description: body.querySelector('#m-desc').value,
    });
    toast('指标已注册', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

// =================================================================== 数据治理
export async function renderGovernance(page, ctx) {
  page.innerHTML = `
    <div class="tabs" id="tabs">
      <button class="active" data-tab="scan">敏感识别</button>
      <button data-tab="quality">质量规则</button>
      <button data-tab="policy">分级策略</button>
    </div><div id="tab-body"></div>`;
  const body = page.querySelector('#tab-body');
  const tabs = page.querySelectorAll('#tabs button');
  const { tables } = await api.get('/catalog/tables');
  const options = tables.map((t) => `<option value="${esc(t.ref)}">${esc(t.ref)}</option>`).join('');

  const views = {
    scan: () => {
      body.innerHTML = `<div class="grid-2" style="grid-template-columns:1fr 340px;align-items:start">
        <div id="scan-out">${empty('选择一张表开始扫描', '识别手机号、邮箱、身份证、银行卡等字段，给出分级与证据')}</div>
        ${card('扫描设置', `<div class="field"><label>目标表</label><select id="scan-table">${options}</select></div>
          <button class="btn primary" id="do-scan">开始扫描</button>
          <p class="tiny muted" style="margin-top:12px;line-height:1.7">判定逻辑：列名先验 + 数据抽样正则双证据，输出置信度与命中率，结论可复核。</p>`,
          '不依赖模型也能跑')}
      </div>`;
      body.querySelector('#do-scan').addEventListener('click', async (e) => {
        const out = body.querySelector('#scan-out');
        e.target.disabled = true;
        out.innerHTML = loading('扫描中…');
        try {
          const res = await api.post('/governance/scan', { table: body.querySelector('#scan-table').value });
          const rows = res.findings.map((f) => [
            f.column,
            `<span class="badge ${f.level.toLowerCase()}">${esc(f.tag)} · ${f.level}</span>`,
            (f.confidence * 100).toFixed(0) + '%',
            esc(f.evidence),
            `<span class="mono">${esc(f.sample || '-')}</span>`,
          ]);
          out.innerHTML = card(
            `扫描结果 · ${res.table}`,
            rows.length ? tableHTML(['字段', '分级', '置信度', '依据', '脱敏样例'], rows, { raw: [1, 4] }) : empty('未发现敏感字段'),
            `${res.columns} 个字段 / ${fmt.num(res.row_count)} 行`,
            `<button class="btn primary sm" id="apply-tags">写入标注</button>`
          );
          out.querySelector('#apply-tags')?.addEventListener('click', async () => {
            try {
              const r = await api.post('/governance/tags', { table: res.table, findings: res.findings });
              toast(`已写入 ${r.applied} 条标注`, 'ok');
            } catch (err) {
              toast(err.message, 'err');
            }
          });
        } catch (err) {
          out.innerHTML = `<div class="card"><div class="badge err">失败</div><div class="tiny" style="margin-top:6px">${esc(err.message)}</div></div>`;
        } finally {
          e.target.disabled = false;
        }
      });
    },
    quality: () => {
      body.innerHTML = loading();
      api.get('/governance/quality').then(({ rules }) => {
        body.innerHTML = `<div class="col" style="gap:16px">
          ${card('质量规则', rules.length
            ? tableHTML(['表', '字段', '规则', '配置', '最近结果', '操作'], rules.map((r) => [
                r.table_ref, r.column_name || '-', r.rule_type, r.config,
                r.last_status ? `<span class="badge ${r.last_status === 'pass' ? 'ok' : 'err'}">${r.last_status} ${r.last_value || ''}</span>` : '<span class="badge">未执行</span>',
                `<button class="btn sm" data-run-rule="${r.id}">执行</button>`,
              ]), { raw: [4, 5] })
            : empty('还没有质量规则', '可以点右上角新建，或让 Buddy 推荐'),
            '空值率 / 唯一性 / 值域 / 时效性 / 自定义 SQL',
            `<div class="row"><button class="btn sm" id="run-all">全部执行</button><button class="btn sm primary" id="new-rule">＋ 新建规则</button></div>`)}
        </div>`;
        body.querySelector('#run-all').addEventListener('click', async (e) => {
          e.target.disabled = true;
          const out = await api.post('/governance/quality/run-all', {});
          toast(`${out.passed}/${out.total} 通过`, out.passed === out.total ? 'ok' : 'err');
          views.quality();
        });
        body.querySelector('#new-rule').addEventListener('click', () => ruleModal(options));
        $$('[data-run-rule]', body).forEach((b) =>
          b.addEventListener('click', async () => {
            const out = await api.post(`/governance/quality/${b.dataset.runRule}/run`, {});
            toast(`${out.status === 'pass' ? '通过' : '未通过'}：${out.detail}`, out.status === 'pass' ? 'ok' : 'err');
            views.quality();
          })
        );
      });
    },
    policy: () => {
      body.innerHTML = loading();
      api.get('/governance/policies').then(({ policies }) => {
        body.innerHTML = card(
          '分级与处理建议',
          policies.length
            ? tableHTML(['对象', '分级', '建议动作', '置信度'], policies.map((p) => [
                p.subject,
                `<span class="badge ${p.level.toLowerCase()}">${esc(p.tag)} · ${p.level} ${p.level_name}</span>`,
                p.suggested_action,
                p.confidence ? (p.confidence * 100).toFixed(0) + '%' : '-',
              ]), { raw: [1] })
            : empty('还没有分级数据', '先做一次敏感识别并写入标注'),
          '基于已标注字段自动推导'
        );
      });
    },
  };
  tabs.forEach((t) =>
    t.addEventListener('click', () => {
      tabs.forEach((x) => x.classList.remove('active'));
      t.classList.add('active');
      views[t.dataset.tab]();
    })
  );
  views.scan();
}

async function ruleModal(options) {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>表</label><select id="r-table">${options}</select></div>
    <div class="field"><label>字段（自定义 SQL 可留空）</label><input id="r-col" placeholder="amount" /></div>
    <div class="field"><label>规则类型</label><select id="r-type">
      <option value="null_rate">空值率</option><option value="uniqueness">唯一性</option>
      <option value="range">值域</option><option value="freshness">时效性</option>
      <option value="custom_sql">自定义 SQL</option></select></div>
    <div class="field"><label>阈值（JSON）</label><input id="r-cfg" class="mono" value='{"max_rate":0.02}' style="font-family:var(--mono)" />
      <span class="hint">null_rate: max_rate；uniqueness: max_dup；range: min/max；freshness: max_days；custom_sql: sql</span></div>`;
  const ok = await modal({ title: '新建质量规则', body, actions: [{ label: '取消', value: false }, { label: '保存', primary: true, value: true }] });
  if (!ok) return;
  try {
    await api.post('/governance/quality', {
      table: body.querySelector('#r-table').value,
      column: body.querySelector('#r-col').value,
      rule_type: body.querySelector('#r-type').value,
      config: JSON.parse(body.querySelector('#r-cfg').value || '{}'),
    });
    toast('规则已创建', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

// =================================================================== 知识库
export async function renderKnowledge(page, ctx) {
  page.innerHTML = loading();
  const { docs } = await api.get('/knowledge');
  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn sm', onclick: () => rebuildDocs() }, '重建元数据文档'),
      el('button', { class: 'btn primary sm', onclick: () => docModal() }, '＋ 新建文档'),
    ])
  );

  page.innerHTML = `<div class="col" style="gap:16px">
    <div class="card tight">
      <div class="row"><input id="kb-search" placeholder="搜索知识库…" style="flex:1" />
      <button class="btn sm" id="kb-do">搜索</button></div>
      <div id="kb-results" style="margin-top:10px"></div>
    </div>
    ${card('文档列表', docs.length
      ? `<div class="col" style="gap:8px">${docs.map((d) => `<div class="list-item">
          <div class="meta"><strong>${esc(d.title)}</strong>
            <span>${esc(d.tags || '通用')} · ${esc(d.source_type)} · ${fmt.bytes(d.size)} · ${fmt.ago(d.created_at)}</span></div>
          <button class="btn sm" data-doc="${d.id}">查看</button>
          <button class="btn sm danger" data-del-doc="${d.id}">删除</button>
        </div>`).join('')}</div>`
      : empty('还没有知识文档', '沉淀术语、口径、规范，问数会更准'), `${docs.length} 篇`)}
  </div>`;

  page.querySelector('#kb-do').addEventListener('click', doSearch);
  page.querySelector('#kb-search').addEventListener('keydown', (e) => e.key === 'Enter' && doSearch());
  async function doSearch() {
    const q = page.querySelector('#kb-search').value.trim();
    const host = page.querySelector('#kb-results');
    if (!q) return (host.innerHTML = '');
    host.innerHTML = loading('检索中…');
    const { hits } = await api.get(`/knowledge-search?q=${encodeURIComponent(q)}`);
    host.innerHTML = hits.length
      ? hits.map((h) => `<div style="padding:8px 0;border-bottom:1px solid var(--line-2)">
          <strong style="font-size:12.8px">${esc(h.title)}</strong>
          <span class="badge" style="margin-left:6px">${h.score}</span>
          <div class="tiny muted" style="margin-top:3px">${esc(h.snippet)}</div></div>`).join('')
      : `<div class="tiny muted">没有命中</div>`;
  }
  $$('[data-doc]', page).forEach((b) =>
    b.addEventListener('click', async () => {
      const { doc } = await api.get(`/knowledge/${b.dataset.doc}`);
      modal({ title: doc.title, wide: true, body: `<div class="prose">${md(doc.content)}</div>`, actions: [{ label: '关闭', primary: true }] });
    })
  );
  $$('[data-del-doc]', page).forEach((b) =>
    b.addEventListener('click', async () => {
      await api.del(`/knowledge/${b.dataset.delDoc}`);
      renderKnowledge(page, ctx);
    })
  );
}

async function rebuildDocs() {
  const out = await api.post('/knowledge/rebuild-catalog', {});
  toast(`已生成 ${out.docs} 篇表结构文档`, 'ok');
  window.dispatchEvent(new Event('odb:refresh'));
}

async function docModal() {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>标题</label><input id="d-title" placeholder="例如 退款率口径" /></div>
    <div class="field"><label>标签</label><input id="d-tags" placeholder="指标 / 规范 / 合规" /></div>
    <div class="field"><label>内容</label><textarea id="d-content" rows="10" placeholder="写清口径、边界条件与例外"></textarea></div>`;
  const ok = await modal({ title: '新建知识文档', body, wide: true, actions: [{ label: '取消', value: false }, { label: '保存', primary: true, value: true }] });
  if (!ok) return;
  try {
    await api.post('/knowledge', {
      title: body.querySelector('#d-title').value,
      tags: body.querySelector('#d-tags').value,
      content: body.querySelector('#d-content').value,
    });
    toast('已保存', 'ok');
    window.dispatchEvent(new Event('odb:refresh'));
  } catch (e) {
    toast(e.message, 'err');
  }
}

// ===================================================================== 设置
export async function renderSettings(page, ctx) {
  page.innerHTML = loading();
  const [settings, users, audit, toolsOut] = await Promise.all([
    api.get('/settings'),
    api.get('/users').catch(() => ({ users: [] })),
    api.get('/ops/audit?limit=40').catch(() => ({ logs: [] })),
    api.get('/settings/tools').catch(() => ({ tools: [] })),
  ]);
  const llm = settings.llm;
  const canAdmin = session.user?.role === 'admin';

  page.innerHTML = `<div class="col" style="gap:16px">
    <div class="grid-2">
      ${card('模型接入', `
        <div class="field"><label>服务商</label><select id="provider">
          ${(settings.providers || []).map((p) => `<option value="${p.id}" ${p.id === llm.provider ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}
        </select></div>
        <div class="field"><label>Base URL</label><input id="base" value="${esc(llm.base_url)}" /></div>
        <div class="field"><label>模型</label><input id="model" value="${esc(llm.model)}" /></div>
        <div class="field"><label>API Key</label><input id="key" type="password" placeholder="${esc(llm.api_key_masked || '未配置')}" />
          <span class="hint">DeepSeek 默认 https://api.deepseek.com/v1 · 模型 deepseek-chat / deepseek-reasoner</span></div>
        <div class="row">
          <button class="btn primary sm" id="save-llm" ${canAdmin ? '' : 'disabled'}>保存</button>
          <button class="btn sm" id="test-llm" ${canAdmin ? '' : 'disabled'}>测试连接</button>
          <span class="badge ${llm.configured ? 'ok' : 'warn'}">${llm.configured ? '已配置' : '未配置，使用规则引擎'}</span>
        </div>
        <div id="llm-out" style="margin-top:10px"></div>`, 'DeepSeek 为默认底座，兼容任意 OpenAI 协议端点', '')}

      ${card('平台信息', `<div class="col" style="gap:10px">
        <div class="row"><span class="muted tiny" style="width:88px">版本</span><span>${esc(settings.platform.version)}</span></div>
        <div class="row"><span class="muted tiny" style="width:88px">数据目录</span><span class="mono tiny">${esc(settings.platform.data_dir)}</span></div>
        <div class="row"><span class="muted tiny" style="width:88px">登录体系</span><span>本地账号（无 CAM / 企业实名）</span></div>
        <div class="row"><span class="muted tiny" style="width:88px">计费</span><span class="badge ok">已移除</span></div>
        <div class="row"><span class="muted tiny" style="width:88px">席位限制</span><span class="badge ok">无限制</span></div>
        <div class="row"><span class="muted tiny" style="width:88px">地域锁定</span><span class="badge ok">无</span></div>
        <div class="row"><span class="muted tiny" style="width:88px">API 文档</span><a href="/api/docs" target="_blank">/api/docs</a></div>
      </div>`, '与原商业版的差异见 docs/OSS_DIFF.md', '')}
    </div>

    ${card('成员', users.users?.length
      ? tableHTML(['邮箱', '姓名', '角色', '创建时间'], users.users.map((u) => [u.email, u.name, u.role, fmt.dt(u.created_at)]))
      : empty('没有用户'), canAdmin ? '管理员可添加成员' : '只有管理员可管理', canAdmin ? '<button class="btn primary sm" id="new-user">＋ 添加成员</button>' : '')}

    <div class="grid-2">
      ${card('Agent 可用工具', `<div class="row wrap" style="gap:6px">${(toolsOut.tools || []).map((t) =>
          `<span class="badge ${t.risk === 'read' ? '' : 'warn'}" title="${esc(t.description)}">${esc(t.name)}${t.risk === 'read' ? '' : ' · 写'}</span>`).join('')}</div>`,
        `${(toolsOut.tools || []).length} 个工具，按模式下发`)}
      ${card('轨迹评测', `<div id="trace-stat">${loading()}</div>`, 'Harness 对每次工具调用留痕')}
    </div>

    ${card('审计日志', audit.logs?.length
      ? tableHTML(['时间', '用户', '动作', '对象'], audit.logs.map((l) => [fmt.dt(l.created_at), l.user_name || '-', l.action, l.target || '-']))
      : empty('暂无日志'))}
  </div>`;

  api.get('/ops/traces').then((t) => {
    const host = page.querySelector('#trace-stat');
    if (!t.calls) {
      host.innerHTML = `<div class="tiny muted">还没有工具调用记录</div>`;
      return;
    }
    host.innerHTML = `<div class="stat-strip" style="margin-bottom:10px">
        <div class="item"><div class="kpi"><span class="label">调用次数</span><span class="value" style="font-size:18px">${t.calls}</span></div></div>
        <div class="item"><div class="kpi"><span class="label">成功率</span><span class="value" style="font-size:18px">${fmt.pct(t.success_rate)}</span></div></div>
        <div class="item"><div class="kpi"><span class="label">平均耗时</span><span class="value" style="font-size:18px">${t.avg_ms}ms</span></div></div>
      </div>
      ${tableHTML(['工具', '调用', '成功率', '平均耗时'], t.tools.map((x) => [x.tool, x.calls, fmt.pct(x.success_rate), `${x.avg_ms}ms`]))}`;
  });

  page.querySelector('#save-llm').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      await api.put('/settings', {
        provider: page.querySelector('#provider').value,
        base_url: page.querySelector('#base').value,
        model: page.querySelector('#model').value,
        api_key: page.querySelector('#key').value,
      });
      toast('已保存', 'ok');
      renderSettings(page, ctx);
    } catch (err) {
      toast(err.message, 'err');
      e.target.disabled = false;
    }
  });
  page.querySelector('#test-llm').addEventListener('click', async (e) => {
    const out = page.querySelector('#llm-out');
    e.target.disabled = true;
    out.innerHTML = loading('连接中…');
    try {
      const r = await api.post('/settings/llm/test', {});
      out.innerHTML = r.ok
        ? `<div class="badge ok">连接成功 · ${esc(r.model)}</div><div class="tiny" style="margin-top:6px">${esc(r.reply)}</div>`
        : `<div class="badge err">失败</div><div class="tiny" style="margin-top:6px">${esc(r.error)}</div>`;
    } catch (err) {
      out.innerHTML = `<div class="badge err">失败</div><div class="tiny" style="margin-top:6px">${esc(err.message)}</div>`;
    } finally {
      e.target.disabled = false;
    }
  });
  page.querySelector('#new-user')?.addEventListener('click', () => userModal(ctx));
}

async function userModal(ctx) {
  const body = el('div');
  body.innerHTML = `<div class="field"><label>邮箱</label><input id="u-email" placeholder="name@company.com" /></div>
    <div class="field"><label>姓名</label><input id="u-name" /></div>
    <div class="field"><label>初始密码</label><input id="u-pass" value="databuddy123" /></div>
    <div class="field"><label>角色</label><select id="u-role"><option value="member">成员</option><option value="admin">管理员</option></select></div>`;
  const ok = await modal({ title: '添加成员', body, actions: [{ label: '取消', value: false }, { label: '创建', primary: true, value: true }] });
  if (!ok) return;
  try {
    await api.post('/users', {
      email: body.querySelector('#u-email').value,
      name: body.querySelector('#u-name').value,
      password: body.querySelector('#u-pass').value,
      role: body.querySelector('#u-role').value,
    });
    toast('成员已创建（已加入当前工作空间）', 'ok');
    renderSettings(document.querySelector('#page'), ctx);
  } catch (e) {
    toast(e.message, 'err');
  }
}
