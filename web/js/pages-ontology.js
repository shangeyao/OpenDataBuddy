// 统一语义层扩展：本体建模（实体 / 关系 / 术语表）
import { api } from './api.js';
import {
  card, dataTable, tag, loading, emptyState, toast, esc, el,
  formModal, confirmDialog, bindTabs, tabsHTML,
} from './ui.js';

/* 纯 SVG 画本体关系图：分层布局，不引入任何图表库 */
function ontologyGraph(graph, { width = 720, height = 360 } = {}) {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  if (!nodes.length) return emptyState('还没有本体实体', '先创建实体并建立关系，这里会画出业务关系图');

  // 按入度分层：无入边的在第 0 层
  const incoming = new Map(nodes.map((n) => [n.id, 0]));
  for (const e of edges) {
    if (incoming.has(e.to)) incoming.set(e.to, incoming.get(e.to) + 1);
  }
  const level = new Map();
  const byLevel = [];
  for (const n of nodes) {
    const lv = incoming.get(n.id) === 0 ? 0 : 1;
    level.set(n.id, lv);
    (byLevel[lv] ||= []).push(n);
  }
  // 一层放不下的挪到第二层，避免重叠
  if ((byLevel[0] || []).length > 4) {
    byLevel[1] = [...(byLevel[1] || []), ...byLevel[0].slice(4)];
    byLevel[0] = byLevel[0].slice(0, 4);
    byLevel[1].forEach((n) => level.set(n.id, 1));
  }

  const NW = 132;
  const NH = 44;
  const pos = new Map();
  byLevel.forEach((group, lv) => {
    const list = group || [];
    const gap = width / (list.length + 1);
    list.forEach((n, i) => {
      pos.set(n.id, { x: gap * (i + 1) - NW / 2, y: 40 + lv * 130 });
    });
  });

  const palette = { subject: '#0052D9', event: '#0594FA', dimension: '#00A870', metric_group: '#ED7B2F' };
  const edgeSvg = edges
    .map((e) => {
      const a = pos.get(e.from);
      const b = pos.get(e.to);
      if (!a || !b) return '';
      const x1 = a.x + NW / 2;
      const y1 = a.y + NH;
      const x2 = b.x + NW / 2;
      const y2 = b.y;
      const midY = (y1 + y2) / 2;
      return `<g>
        <path d="M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}"
          stroke="#C5C5C5" stroke-width="1.5" fill="none" marker-end="url(#arrow)"/>
        <text x="${(x1 + x2) / 2}" y="${midY - 4}" text-anchor="middle"
          font-size="10" fill="#888">${esc(e.label || '')}${e.cardinality ? ` · ${esc(e.cardinality)}` : ''}</text>
      </g>`;
    })
    .join('');

  const nodeSvg = nodes
    .map((n) => {
      const p = pos.get(n.id);
      if (!p) return '';
      const color = palette[n.kind] || '#0052D9';
      return `<g>
        <rect x="${p.x}" y="${p.y}" width="${NW}" height="${NH}" rx="6"
          fill="#fff" stroke="${color}" stroke-width="1.5"/>
        <rect x="${p.x}" y="${p.y}" width="4" height="${NH}" rx="2" fill="${color}"/>
        <text x="${p.x + NW / 2 + 2}" y="${p.y + 19}" text-anchor="middle"
          font-size="12.5" font-weight="500" fill="#000000E6">${esc(n.label)}</text>
        <text x="${p.x + NW / 2 + 2}" y="${p.y + 33}" text-anchor="middle"
          font-size="10" fill="#888">${esc(n.kind_label || '')}</text>
      </g>`;
    })
    .join('');

  return `<svg viewBox="0 0 ${width} ${height}" width="100%" style="max-height:${height}px;background:var(--sub-2);border-radius:6px">
    <defs>
      <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
        <path d="M0,0 L0,6 L7,3 z" fill="#C5C5C5"/>
      </marker>
    </defs>
    ${edgeSvg}${nodeSvg}
  </svg>`;
}

export async function renderOntology(page, ctx) {
  page.innerHTML = loading();
  const [ov, { entities }, { relations }, { terms }] = await Promise.all([
    api.get('/ontology/overview'),
    api.get('/ontology/entities'),
    api.get('/ontology/relations'),
    api.get('/glossary'),
  ]);
  const graph = await api.get('/ontology/graph');

  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => newTerm(ctx) }, '＋ 术语'),
    el('button', { class: 'btn primary', onclick: () => newEntity(ctx) }, '＋ 实体'),
  ]));

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">Unity Semantics</span>
      本体层把业务概念固化为 实体 / 关系 / 术语：指标解决「口径唯一」，本体解决「概念可解释」。
      <span class="spacer"></span>
      <span class="dim tiny">表覆盖 ${ov.covered_tables}/${ov.total_tables || '—'} · 覆盖率 ${(ov.coverage * 100).toFixed(0)}%</span>
    </div>

    <div class="stat-row mt-16">
      ${[['实体', ov.entities, '业务对象 / 事件 / 维度'], ['关系', ov.relations, '带基数与语义动词'],
         ['术语', ov.terms, '含同义词与资产绑定'], ['表覆盖率', `${(ov.coverage * 100).toFixed(0)}%`, `${ov.covered_tables} 张表已建模`]]
        .map(([label, value, sub]) => `<div class="stat">
          <div class="label">${esc(label)}</div>
          <div class="value">${esc(String(value))}</div>
          <div class="sub">${esc(sub)}</div>
        </div>`).join('')}
    </div>

    <div class="mt-16" id="ont-tabs">${tabsHTML([
      { key: 'graph', label: '本体图' },
      { key: 'entities', label: '实体', count: entities.length },
      { key: 'relations', label: '关系', count: relations.length },
      { key: 'glossary', label: '术语表', count: terms.length },
    ], 'graph')}</div>
    <div id="ont-body" class="mt-16"></div>`;

  const tabsRoot = page.querySelector('#ont-tabs');
  bindTabs(tabsRoot, (key) => paint(key));
  paint('graph');

  const body = page.querySelector('#ont-body');

  function paint(key) {
    if (key === 'graph') {
      body.innerHTML = `
        <div class="card">
          <div class="card-head"><div><h3>业务关系图</h3>
            <div class="sub">节点颜色区分实体类型，边上的文字是语义动词与基数</div></div>
            <span class="dim tiny">${graph.nodes.length} 节点 · ${graph.edges.length} 边</span>
          </div>
          ${ontologyGraph(graph)}
        </div>
        <div class="grid-2 mt-16">
          ${card('实体类型', `<div class="tiny muted" style="line-height:2">${Object.entries(ov.kinds)
            .map(([k, v]) => `· <b>${esc(v)}</b>（<span class="mono">${esc(k)}</span>）`).join('<br>')}</div>`)}
          ${card('关系动词', `<div class="tiny muted" style="line-height:2">${Object.entries(ov.verbs)
            .map(([k, v]) => `· <b>${esc(v)}</b>（<span class="mono">${esc(k)}</span>）`).join('<br>')}</div>`)}
        </div>`;
      return;
    }
    if (key === 'entities') {
      body.innerHTML = `<div class="card flush">
        <div class="tbl-toolbar"><span class="title">本体实体</span>
          <span class="dim tiny">${entities.length} 个</span><span class="spacer"></span>
          <button class="btn sm primary" id="add-ent">＋ 新建实体</button></div>
        <div id="ent-table"></div></div>`;
      body.querySelector('#ent-table').innerHTML = dataTable({
        rows: entities,
        columns: [
          { title: '实体', render: (e) => `<div class="cell-main">${esc(e.display_name || e.name)}</div><div class="cell-sub mono">${esc(e.name)}</div>` },
          { title: '类型', width: '100px', render: (e) => tag(e.kind_label || e.kind, 'blue') },
          { title: '绑定表', render: (e) => (e.table_refs || []).length
            ? e.table_refs.map((t) => `<span class="tag outline mono" style="margin-right:4px">${esc(t)}</span>`).join('')
            : '<span class="dim">未绑定</span>' },
          { title: '主键', width: '120px', render: (e) => `<span class="mono tiny">${esc(e.key_column || '-')}</span>` },
          { title: '关系数', width: '80px', render: (e) => String(e.relation_count ?? 0) },
          { title: '操作', key: 'ops', width: '90px', render: (e) => `<a class="btn-link danger" data-del-ent="${esc(e.id)}">删除</a>` },
        ],
        emptyText: '还没有实体', emptyHint: '先建「客户」「订单」这类业务对象',
      });
      body.querySelector('#add-ent').addEventListener('click', () => newEntity(ctx));
      body.querySelectorAll('[data-del-ent]').forEach((b) =>
        b.addEventListener('click', async () => {
          await confirmDialog('删除实体', '删除后与它相关的所有关系也会一并移除，确认继续？', '删除');
          await api.del(`/ontology/entities/${b.dataset.delEnt}`);
          toast('已删除', 'ok');
          ctx.refresh();
        })
      );
      return;
    }
    if (key === 'relations') {
      body.innerHTML = `<div class="card flush">
        <div class="tbl-toolbar"><span class="title">实体关系</span>
          <span class="dim tiny">${relations.length} 条</span><span class="spacer"></span>
          <button class="btn sm primary" id="add-rel">＋ 新建关系</button></div>
        <div id="rel-table"></div></div>`;
      body.querySelector('#rel-table').innerHTML = dataTable({
        rows: relations,
        columns: [
          { title: '起点', render: (r) => `<span class="cell-main">${esc(r.from_name)}</span>` },
          { title: '关系', width: '160px', render: (r) => `${tag(r.verb_label || r.verb, 'green')} <span class="dim tiny">${esc(r.cardinality_label || r.cardinality || '')}</span>` },
          { title: '终点', render: (r) => `<span class="cell-main">${esc(r.to_name)}</span>` },
          { title: '说明', render: (r) => `<span class="dim tiny">${esc(r.description || '-')}</span>` },
          { title: '操作', key: 'ops', width: '80px', render: (r) => `<a class="btn-link danger" data-del-rel="${esc(r.id)}">删除</a>` },
        ],
        emptyText: '还没有关系', emptyHint: '例如「订单 隶属于 客户」（多对一）',
      });
      body.querySelector('#add-rel').addEventListener('click', () => newRelation(ctx, entities, ov));
      body.querySelectorAll('[data-del-rel]').forEach((b) =>
        b.addEventListener('click', async () => {
          await confirmDialog('删除关系', '确认删除这条关系？', '删除');
          await api.del(`/ontology/relations/${b.dataset.delRel}`);
          toast('已删除', 'ok');
          ctx.refresh();
        })
      );
      return;
    }
    // glossary
    body.innerHTML = `<div class="card flush">
      <div class="tbl-toolbar"><span class="title">业务术语表</span>
        <span class="dim tiny">${terms.length} 条 · 问句解析时优先命中术语</span><span class="spacer"></span>
        <input id="glossary-search" class="input" placeholder="搜索术语…" style="width:180px">
        <button class="btn sm primary" id="add-term">＋ 新建术语</button></div>
      <div id="term-table"></div></div>`;
    const paintTerms = (list) => {
      body.querySelector('#term-table').innerHTML = dataTable({
        rows: list,
        columns: [
          { title: '术语', render: (t) => `<div class="cell-main">${esc(t.term)}</div>
            <div class="cell-sub">${(t.synonyms || []).map((s) => esc(s)).join('、') || '无同义词'}</div>` },
          { title: '定义', render: (t) => `<span class="tiny">${esc(t.definition || '-')}</span>` },
          { title: '绑定资产', render: (t) => `<span class="tag outline">${esc(t.asset_type)}</span> <span class="mono tiny">${esc(t.asset_ref || '-')}</span>` },
          { title: '状态', width: '80px', render: (t) => tag(t.status === 'published' ? '已发布' : '草稿', t.status === 'published' ? 'green' : '') },
          { title: '操作', key: 'ops', width: '80px', render: (t) => `<a class="btn-link danger" data-del-term="${esc(t.id)}">删除</a>` },
        ],
        emptyText: '还没有术语', emptyHint: '把「销售额」「活跃客户」这类业务黑话登记进来',
      });
      body.querySelectorAll('[data-del-term]').forEach((b) =>
        b.addEventListener('click', async () => {
          await confirmDialog('删除术语', '确认删除该术语？', '删除');
          await api.del(`/glossary/${b.dataset.delTerm}`);
          toast('已删除', 'ok');
          ctx.refresh();
        })
      );
    };
    paintTerms(terms);
    body.querySelector('#add-term').addEventListener('click', () => newTerm(ctx));
    let timer;
    body.querySelector('#glossary-search').addEventListener('input', (e) => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const out = await api.get(`/glossary?keyword=${encodeURIComponent(e.target.value)}`);
        paintTerms(out.terms || []);
      }, 250);
    });
  }
}

function newEntity(ctx) {
  return formModal({
    title: '新建本体实体',
    fields: [
      { label: '实体标识', name: 'name', required: true, placeholder: 'customer' },
      { label: '显示名称', name: 'display_name', placeholder: '客户' },
      { label: '实体类型', name: 'kind', type: 'select', options: [
        { value: 'subject', label: '业务对象' }, { value: 'event', label: '业务事件' },
        { value: 'dimension', label: '分析维度' }, { value: 'metric_group', label: '指标集合' },
      ] },
      { label: '绑定表', name: 'table_refs', placeholder: 'demo.retail.dim_customer（多个用逗号分隔）' },
      { label: '主键列', name: 'key_column', placeholder: 'customer_id' },
      { label: '说明', name: 'description', type: 'textarea' },
    ],
    onSubmit: async (d) => {
      await api.post('/ontology/entities', {
        ...d,
        table_refs: (d.table_refs || '').split(',').map((s) => s.trim()).filter(Boolean),
      });
      toast('实体已创建', 'ok');
      ctx.refresh();
    },
  });
}

function newRelation(ctx, entities, ov) {
  if (entities.length < 2) {
    toast('至少需要 2 个实体才能建立关系', 'err');
    return;
  }
  const opts = entities.map((e) => ({ value: e.id, label: e.display_name || e.name }));
  return formModal({
    title: '新建实体关系',
    fields: [
      { label: '起点实体', name: 'from_entity', type: 'select', options: opts, required: true },
      { label: '关系动词', name: 'verb', type: 'select', options: Object.entries(ov.verbs).map(([v, l]) => ({ value: v, label: l })) },
      { label: '终点实体', name: 'to_entity', type: 'select', options: opts, required: true },
      { label: '基数', name: 'cardinality', type: 'select', options: Object.entries(ov.cardinalities).map(([v, l]) => ({ value: v, label: l })) },
      { label: '说明', name: 'description', type: 'textarea' },
    ],
    onSubmit: async (d) => {
      await api.post('/ontology/relations', d);
      toast('关系已创建', 'ok');
      ctx.refresh();
    },
  });
}

function newTerm(ctx) {
  return formModal({
    title: '新建业务术语',
    fields: [
      { label: '术语', name: 'term', required: true, placeholder: '销售额' },
      { label: '同义词', name: 'synonyms', placeholder: 'GMV, 成交额（逗号分隔）' },
      { label: '定义', name: 'definition', type: 'textarea' },
      { label: '资产类型', name: 'asset_type', type: 'select', options: [
        { value: 'table', label: '数据表' }, { value: 'metric', label: '指标' },
        { value: 'column', label: '字段' }, { value: 'dashboard', label: '仪表盘' },
      ] },
      { label: '资产引用', name: 'asset_ref', placeholder: 'gmv 或 demo.retail.fact_orders' },
    ],
    onSubmit: async (d) => {
      await api.post('/glossary', {
        ...d,
        synonyms: (d.synonyms || '').split(',').map((s) => s.trim()).filter(Boolean),
      });
      toast('术语已创建', 'ok');
      ctx.refresh();
    },
  });
}
