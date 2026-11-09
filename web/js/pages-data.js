// 数据域：数据目录 / 数据接入 / 数据探索 / 数据源管理
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  modal, confirmDialog, formModal, toast, drawer, esc, descList, bindTabs, el,
} from './ui.js';
import { chartHTML } from './charts.js';
import { icon } from './icons.js';

/* -------------------------------------------------------------- 数据目录 */
export async function renderCatalog(page, ctx) {
  page.innerHTML = loading();
  const [{ catalogs }, { tables }] = await Promise.all([api.get('/catalog/tree'), api.get('/catalog/tables')]);

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => newCatalog(ctx) }, '新建 Catalog'),
      el('button', { class: 'btn', onclick: () => newSchema(catalogs, ctx) }, '新建 Schema'),
      el('button', { class: 'btn primary', onclick: () => newSchema(catalogs, ctx) }, '新建表'),
    ])
  );

  const tree = (catalogs || [])
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
                    <span class="ico">${icon('table', 13)}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(t.name)}</span>
                    <span class="cnt">${fmt.num(t.row_count)}</span></div>`
                )
                .join('')}
            </div>`
          )
          .join('')}
      </div>`
    )
    .join('');

  page.innerHTML = `
    <div class="split">
      <div class="tree-panel">
        <div class="tp-head"><span>目录树</span><span class="dim tiny">${tables.length} 张表</span></div>
        <div class="tree" id="tree">${tree || emptyState('还没有 Catalog', '点击右上角新建', '▤')}</div>
      </div>
      <div id="detail">${emptyState('选择左侧任意表', '查看字段、标签、样例数据与血缘', '▤')}</div>
    </div>`;

  const detail = page.querySelector('#detail');
  page.querySelectorAll('[data-table]').forEach((node) => {
    node.addEventListener('click', () => {
      page.querySelectorAll('.tree-node').forEach((n) => n.classList.remove('active'));
      node.classList.add('active');
      showTable(detail, node.dataset.table, ctx);
    });
  });
  const first = page.querySelector('[data-table]');
  if (first) { first.classList.add('active'); showTable(detail, first.dataset.table, ctx); }
}

async function showTable(host, ref, ctx) {
  host.innerHTML = loading('加载表详情…');
  const [{ table }, preview] = await Promise.all([
    api.get(`/catalog/table?ref=${encodeURIComponent(ref)}`),
    api.get(`/catalog/preview?ref=${encodeURIComponent(ref)}&limit=10`).catch(() => null),
  ]);
  const pii = table.columns.filter((c) => (c.tags || []).some((t) => ['L3', 'L4'].includes(t.level))).length;

  host.innerHTML = `<div class="col" style="gap:16px">
    <div class="card">
      <div class="card-head">
        <div>
          <h3 class="mono" style="font-size:15px">${esc(table.ref)}</h3>
          <div class="sub">${esc(table.description || '无描述')}</div>
        </div>
        <div class="row">
          <button class="btn sm" id="t-preview">样例数据</button>
          <button class="btn sm" id="t-govern">敏感扫描</button>
          <button class="btn sm danger" id="t-drop">删除</button>
        </div>
      </div>
      <div class="stat-row" style="grid-template-columns:repeat(4,1fr);gap:12px">
        ${[
          ['行数', fmt.num(table.row_count)],
          ['字段数', table.columns.length],
          ['敏感字段', pii],
          ['样例', preview ? `${preview.row_count} 行` : '-'],
        ].map(([k, v]) => `<div style="border:1px solid var(--border);border-radius:4px;padding:10px 14px">
            <div class="tiny dim">${k}</div><div style="font-size:18px;font-weight:500">${v}</div></div>`).join('')}
      </div>
      <div class="tabs inline" style="margin-top:16px" data-tabs="catalog">
        <button class="active" data-tab="cols">字段</button>
        <button data-tab="lineage">血缘</button>
      </div>
      <div id="tab-cols" style="margin-top:8px">
        ${tableHTML(['字段', '类型', '标签', '说明'], table.columns.map((c) => [
          c.name, c.data_type,
          (c.tags || []).map((t) => `${t.tag}(${t.level})`).join('、') || '-',
          c.description || '-',
        ]), { raw: [] })}
      </div>
      <div id="tab-lineage" class="hidden" style="margin-top:8px">
        ${table.lineage?.length
          ? tableHTML(['上游', '下游', '类型', '说明'], table.lineage.map((l) => [l.upstream, l.downstream, l.kind, l.detail]))
          : emptyState('暂无血缘信息', '执行 ETL 或 SQL 建表后自动采集', '⇄')}
      </div>
    </div>
    <div id="t-preview-body"></div>
  </div>`;

  host.querySelectorAll('[data-tabs="catalog"] button').forEach((b) =>
    b.addEventListener('click', () => {
      host.querySelectorAll('[data-tabs="catalog"] button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      host.querySelector('#tab-cols').classList.toggle('hidden', b.dataset.tab !== 'cols');
      host.querySelector('#tab-lineage').classList.toggle('hidden', b.dataset.tab !== 'lineage');
    })
  );
  host.querySelector('#t-preview').addEventListener('click', () => {
    host.querySelector('#t-preview-body').innerHTML = preview
      ? card('样例数据', tableHTML(preview.columns, preview.rows, { max: 10 }), { sub: `前 10 行 · ${preview.elapsed_ms}ms`, flush: true })
      : card('样例数据', emptyState('无法读取样例'));
  });
  host.querySelector('#t-govern').addEventListener('click', () => {
    location.hash = '#/security';
  });
  host.querySelector('#t-drop').addEventListener('click', async () => {
    if (!(await confirmDialog('删除表', `删除 ${table.ref} 后数据不可恢复，确认继续？`, '删除'))) return;
    await api.del(`/catalog/tables/${table.id}`);
    toast('表已删除', 'ok');
    ctx.refresh();
  });
}

async function newCatalog(ctx) {
  const data = await formModal({
    title: '新建 Catalog',
    fields: [
      { label: 'Catalog 名称', name: 'name', required: true, placeholder: '例如 sales' },
      { label: '描述', name: 'description', placeholder: '可选' },
    ],
    onSubmit: async (d) => { await api.post('/catalog/catalogs', d); toast('Catalog 已创建', 'ok'); },
  });
  if (data) ctx.refresh();
}

async function newSchema(catalogs, ctx) {
  if (!catalogs.length) return toast('请先创建 Catalog', 'err');
  const data = await formModal({
    title: '新建 Schema',
    fields: [
      { label: '所属 Catalog', name: 'catalog', type: 'select', options: catalogs.map((c) => c.name) },
      { label: 'Schema 名称', name: 'name', required: true, placeholder: '例如 dwd' },
      { label: '描述', name: 'description' },
    ],
    onSubmit: async (d) => { await api.post('/catalog/schemas', d); toast('Schema 已创建', 'ok'); },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- 数据接入 */
export async function renderIngest(page, ctx) {
  page.innerHTML = loading();
  const [{ tables }, { sources }] = await Promise.all([api.get('/catalog/tables'), api.get('/sources')]);
  const targets = [...new Set(tables.map((t) => `${t.catalog_name}.${t.schema_name}`))];
  if (!targets.length) targets.push('demo.retail');

  page.innerHTML = `
    <div class="card flush">
      <div style="padding:0 20px"><div class="tabs" data-tabs="ingest">
        <button class="active" data-tab="file">文件到表</button>
        <button data-tab="sql">SQL 建表</button>
        <button data-tab="volume">文件到卷</button>
        <button data-tab="tasks">接入任务</button>
      </div></div>
      <div style="padding:20px" id="ingest-body"></div>
    </div>`;

  const body = page.querySelector('#ingest-body');
  const views = {
    file: () => {
      body.innerHTML = `<div class="grid-2" style="grid-template-columns:1fr 340px;align-items:start">
        <div class="col" style="gap:16px">
          ${card('上传文件并自动建表', `
            <div class="field"><label><span class="req">*</span>目标位置（Catalog.Schema）</label>
              <select id="target">${targets.map((t) => `<option>${esc(t)}</option>`).join('')}</select>
              <div class="hint">不存在时会自动创建 Catalog 与 Schema</div></div>
            <div class="field"><label>表名</label><input id="tname" placeholder="留空则使用文件名" /></div>
            <div class="field"><label>表描述</label><input id="tdesc" placeholder="可选，便于后续检索" /></div>
            <div class="field"><label><span class="req">*</span>选择文件</label>
              <input type="file" id="file" accept=".csv,.json,.ndjson,.jsonl,.parquet,.xlsx" />
              <div class="hint">支持 CSV / JSON / NDJSON / Parquet / XLSX，单文件 ≤ 200MB，自动推断字段类型</div></div>
            <div class="row"><button class="btn primary" id="do-upload">上传并建表</button>
              <span class="tiny dim">解析在本地完成，文件不会离开本机</span></div>
            <div id="upload-result" class="mt-16"></div>`, { flush: false })}
        </div>
        <div class="col" style="gap:16px">
          ${card('已接入表', `<div class="col" style="gap:6px">${tables.slice(0, 10).map((t) => `
            <div class="row" style="justify-content:space-between">
              <span class="mono tiny" style="overflow:hidden;text-overflow:ellipsis">${esc(t.name)}</span>
              <span class="dim tiny">${fmt.num(t.row_count)} 行</span>
            </div>`).join('') || '<div class="dim tiny">暂无</div>'}</div>`, { sub: `${tables.length} 张` })}
          ${card('接入方式说明', `<div class="tiny muted" style="line-height:1.9">
            离线同步、实时 CDC、文件到卷等方案依赖外部系统连接。开源版把数据落地到本地 DuckDB，
            因此「文件到表」是最短可用路径；生产接入建议先用导出或 CDC 落成 CSV/Parquet。</div>`)}
        </div>
      </div>`;
      body.querySelector('#do-upload').addEventListener('click', async (e) => {
        const fileInput = body.querySelector('#file');
        if (!fileInput.files.length) return toast('请选择文件', 'err');
        const form = new FormData();
        form.append('file', fileInput.files[0]);
        form.append('target', body.querySelector('#target').value);
        form.append('table', body.querySelector('#tname').value);
        form.append('description', body.querySelector('#tdesc').value);
        const btn = e.target;
        btn.disabled = true; btn.textContent = '上传解析中…';
        const out = body.querySelector('#upload-result');
        try {
          const res = await api.upload('/ingest/upload', form);
          out.innerHTML = `<div class="info-strip" style="background:#e8f8f2;border-color:#bfe9d6">
            <span class="tag green">建表成功</span>
            <span class="mono">${esc(res.ref)}</span> · ${fmt.num(res.row_count)} 行 · ${res.columns} 个字段</div>`;
          toast('接入完成', 'ok');
          setTimeout(() => ctx.refresh(), 900);
        } catch (err) {
          out.innerHTML = `<div class="tag red">失败</div><div class="tiny mt-8">${esc(err.message)}</div>`;
          btn.disabled = false; btn.textContent = '上传并建表';
        }
      });
    },
    sql: () => {
      body.innerHTML = card('用 SQL 生成新表', `
        <div class="grid-2" style="gap:16px">
          <div class="field"><label>目标位置</label><select id="s-target">${targets.map((t) => `<option>${esc(t)}</option>`).join('')}</select></div>
          <div class="field"><label>新表名</label><input id="s-table" placeholder="例如 daily_gmv" /></div>
        </div>
        <div class="field"><label>SELECT 语句</label>
          <textarea id="s-sql" rows="8" class="mono" style="font-family:var(--mono);font-size:12.5px">SELECT order_date,
       sum(amount) AS gmv,
       count(DISTINCT order_id) AS order_cnt
FROM demo.retail.fact_orders
GROUP BY 1</textarea>
          <div class="hint">只允许只读 SELECT；结果会物化为新表</div></div>
        <button class="btn primary" id="do-sql">执行并建表</button>
        <div id="sql-result" class="mt-16"></div>`);
      body.querySelector('#do-sql').addEventListener('click', async (e) => {
        e.target.disabled = true;
        try {
          const out = await api.post('/ingest/from-sql', {
            target: body.querySelector('#s-target').value,
            table: body.querySelector('#s-table').value,
            sql: body.querySelector('#s-sql').value,
          });
          body.querySelector('#sql-result').innerHTML = `<div class="tag green">已生成 ${esc(out.ref)}</div>`;
          toast('建表成功', 'ok');
        } catch (err) {
          body.querySelector('#sql-result').innerHTML = `<div class="tag red">失败</div><div class="tiny mt-8">${esc(err.message)}</div>`;
          e.target.disabled = false;
        }
      });
    },
    volume: () => {
      body.innerHTML = card('文件到卷 / 批量到卷', `<div class="tiny muted" style="line-height:2">
        用于存放图片、PDF、日志等非结构化文件。开源版把 Volume 落成本地目录 <span class="mono">data/uploads/</span>。<br>
        · 文件到卷：单个或多个文件直接入卷，保留原文件名<br>
        · 批量到卷：从 FTP / S3 / 本地目录同步，保留目录结构<br><br>
        当前版本未开放在线卷管理界面，可先用「文件到表」把结构化数据落地。</div>`);
    },
    tasks: () => {
      body.innerHTML = card('接入任务', emptyState('还没有接入任务', '文件上传建表不产生调度任务；离线/实时同步任务需要外部数据源连接'), { flush: false });
    },
  };
  bindTabs(page, (key) => views[key]());
  views.file();
}

/* -------------------------------------------------------------- 数据探索 */
export async function renderSql(page, ctx) {
  const { tables } = await api.get('/catalog/tables');
  page.innerHTML = `
    <div class="split" style="grid-template-columns:1fr 260px">
      <div class="col" style="gap:16px">
        <div class="card flush">
          <div class="tbl-toolbar">
            <span class="title">SQL 编辑器</span>
            <span class="tag blue">Spark SQL 兼容</span>
            <span class="spacer"></span>
            <span class="tiny dim">${tables.length} 张可用表 · 结果上限 500 行</span>
          </div>
          <div style="padding:16px">
            <textarea id="sql" rows="10" class="mono" style="font-family:var(--mono);font-size:12.5px;min-height:200px">SELECT
  c.channel_name,
  round(sum(o.amount), 2) AS gmv,
  count(DISTINCT o.order_id) AS orders
FROM demo.retail.fact_orders o
JOIN demo.retail.dim_channel c ON c.channel_id = o.channel_id
GROUP BY 1
ORDER BY gmv DESC</textarea>
            <div class="row mt-12">
              <button class="btn primary" id="run">运行</button>
              <button class="btn" id="fmt">格式化</button>
              <span class="tiny dim">Ctrl / Cmd + Enter 运行</span>
            </div>
          </div>
        </div>
        <div id="result"></div>
      </div>
      <div class="tree-panel">
        <div class="tp-head"><span>数据表</span><span class="dim tiny">点击插入</span></div>
        <div class="tree">${tables.map((t) => `<div class="tree-node" data-ins="${esc(t.ref)}">
          <span class="ico">${icon('table', 13)}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis">${esc(t.name)}</span>
          <span class="cnt">${fmt.num(t.row_count)}</span></div>`).join('')}</div>
      </div>
    </div>`;

  const ta = page.querySelector('#sql');
  const result = page.querySelector('#result');
  const run = async () => {
    result.innerHTML = loading('执行中…');
    try {
      const out = await api.post('/sql/run', { sql: ta.value, limit: 500 });
      result.innerHTML = card(`查询结果`, `${chartHTML(out.chart, out.columns, out.rows)}${tableHTML(out.columns, out.rows, { max: 100 })}`, {
        sub: `${out.row_count} 行 · ${out.elapsed_ms} ms${out.truncated ? ' · 已截断' : ''}`,
        actions: `<span class="tag">${out.chart.type}</span>`,
        flush: true,
      });
    } catch (err) {
      result.innerHTML = card('执行失败', `<pre class="code">${esc(err.message)}</pre>`, { flush: false });
    }
  };
  page.querySelector('#run').addEventListener('click', run);
  page.querySelector('#fmt').addEventListener('click', () => {
    ta.value = ta.value.replace(/\s+FROM\s+/gi, '\nFROM ').replace(/\s+WHERE\s+/gi, '\nWHERE ')
      .replace(/\s+GROUP BY\s+/gi, '\nGROUP BY ').replace(/\s+ORDER BY\s+/gi, '\nORDER BY ')
      .replace(/\s+JOIN\s+/gi, '\nJOIN ');
  });
  ta.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') run(); });
  page.querySelectorAll('[data-ins]').forEach((n) =>
    n.addEventListener('click', () => {
      const pos = ta.selectionStart;
      ta.value = ta.value.slice(0, pos) + n.dataset.ins + ta.value.slice(pos);
      ta.focus();
    })
  );
  run();
}

/* -------------------------------------------------------------- 数据源 */
export async function renderSources(page, ctx) {
  page.innerHTML = loading();
  const { sources } = await api.get('/sources');
  ctx.actions(el('div', { class: 'row' }, [el('button', { class: 'btn primary', onclick: () => newSource(ctx) }, '＋ 新建数据源')]));

  const typeLabel = { file: '文件目录', sqlite: 'SQLite', postgres: 'PostgreSQL', mysql: 'MySQL', http: 'HTTP API' };

  page.innerHTML = `
    <div class="filter-bar">
      <div class="f-item"><span>名称</span><input id="f-kw" placeholder="搜索数据源名称" /></div>
      <div class="f-item"><span>类型</span><select id="f-type"><option value="">全部</option>
        ${Object.entries(typeLabel).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}</select></div>
      <span class="spacer"></span>
      <button class="btn" id="f-reset">重置</button>
    </div>
    <div class="card flush">
      <div class="tbl-toolbar">
        <span class="title">数据源列表</span>
        <span class="dim tiny">共 ${sources.length} 个</span>
        <span class="spacer"></span>
        <button class="btn sm" id="t-refresh">刷新</button>
      </div>
      <div id="src-table"></div>
    </div>`;

  const draw = (rows) => {
    page.querySelector('#src-table').innerHTML = dataTable({
      rows,
      columns: [
        { title: '数据源名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div><div class="cell-sub">${esc(r.description || '无描述')}</div>` },
        { title: '类型', render: (r) => tag(typeLabel[r.type] || r.type, 'outline') },
        { title: '连接信息', render: (r) => `<span class="mono tiny">${esc(JSON.stringify(r.config || {}).slice(0, 48))}</span>` },
        { title: '连通状态', render: (r) => statusTag(r.status === 'ok' ? 'ok' : r.status === 'failed' ? 'failed' : 'draft') },
        { title: '最近测试', render: (r) => `<span class="dim">${r.last_test_at ? fmt.dt(r.last_test_at) : '-'}</span>` },
        { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-test="${r.id}">连通性测试</a><a class="btn-link danger" data-del="${r.id}">删除</a>` },
      ],
      emptyText: '还没有数据源',
      emptyHint: '点击右上角新建，登记来源并做连通性巡检',
    });
    page.querySelectorAll('[data-test]').forEach((b) =>
      b.addEventListener('click', async () => {
        b.textContent = '测试中…';
        try {
          const out = await api.post(`/sources/${b.dataset.test}/test`, {});
          toast(out.message, out.ok ? 'ok' : 'err');
          ctx.refresh();
        } catch (e) { toast(e.message, 'err'); b.textContent = '连通性测试'; }
      })
    );
    page.querySelectorAll('[data-del]').forEach((b) =>
      b.addEventListener('click', async () => {
        if (!(await confirmDialog('删除数据源', '确认删除该连接登记？', '删除'))) return;
        await api.del(`/sources/${b.dataset.del}`);
        ctx.refresh();
      })
    );
  };
  draw(sources);

  page.querySelector('#f-kw').addEventListener('input', (e) => {
    const kw = e.target.value.trim().toLowerCase();
    const type = page.querySelector('#f-type').value;
    draw(sources.filter((s) => (!kw || s.name.toLowerCase().includes(kw)) && (!type || s.type === type)));
  });
  page.querySelector('#f-type').addEventListener('change', () => {
    page.querySelector('#f-kw').dispatchEvent(new Event('input'));
  });
  page.querySelector('#f-reset').addEventListener('click', () => {
    page.querySelector('#f-kw').value = '';
    page.querySelector('#f-type').value = '';
    draw(sources);
  });
  page.querySelector('#t-refresh').addEventListener('click', () => ctx.refresh());
}

async function newSource(ctx) {
  const data = await formModal({
    title: '新建数据源',
    fields: [
      { label: '数据源名称', name: 'name', required: true, placeholder: '例如 生产库只读' },
      { label: '类型', name: 'type', type: 'select', options: [
        { value: 'file', label: '文件目录' }, { value: 'sqlite', label: 'SQLite' },
        { value: 'postgres', label: 'PostgreSQL' }, { value: 'mysql', label: 'MySQL' },
      ] },
      { label: '路径 / 主机', name: 'host', required: true, placeholder: 'D:/data 或 10.0.0.12', hint: '文件类型填本地路径；数据库类型填主机地址' },
      { label: '端口', name: 'port', type: 'number', hint: '数据库类型需要，PostgreSQL 5432 / MySQL 3306' },
      { label: '描述', name: 'description' },
    ],
    onSubmit: async (d) => {
      const config = ['file', 'sqlite'].includes(d.type) ? { path: d.host } : { host: d.host, port: d.port };
      await api.post('/sources', { name: d.name, type: d.type, description: d.description, config });
      toast('数据源已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}
