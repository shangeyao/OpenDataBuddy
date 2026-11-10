// 数据目录 2.0：多模态目录树（目录→Schema→表/视图/函数/卷/模型）+ 端到端血缘
//
// 血缘可视化全部用纯 SVG 手绘：不引入 d3 / cytoscape 等任何图库。
// 布局思路（分层有向图，Sugiyama 简化版）：
//   1) 按 level 把节点分到「上游 / 中心 / 下游」三列（表级视图）；
//      字段级视图按「所属表」再分组成列，组内纵向排列。
//   2) 同列内按度数排序后均匀分布 y 坐标。
//   3) 边用三次贝塞尔曲线，控制点水平偏移，形成柔和的 S 形流向。
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  confirmDialog, formModal, toast, drawer, esc, descList, el,
} from './ui.js';
import { icon } from './icons.js';

const TYPE_ICON = {
  table: 'table', dashboard: 'dashboards', metric: 'semantic', model: 'registry',
  volume: 'folder', source: 'sources', column: 'dot', app: 'agents',
};
const TYPE_TONE = {
  table: 'blue', dashboard: 'green', metric: 'orange', model: 'red',
  volume: '', source: '', column: '', app: 'blue',
};
const KIND_OPTIONS = [
  { value: 'sql', label: 'SQL 加工' },
  { value: 'ctas', label: 'CTAS 建表' },
  { value: 'insert', label: 'INSERT 写入' },
  { value: 'merge', label: 'MERGE 合并' },
  { value: 'etl', label: 'ETL 作业' },
  { value: 'stream', label: '实时同步' },
  { value: 'cdc', label: 'CDC 捕获' },
  { value: 'dashboard', label: '看板引用' },
  { value: 'metric', label: '指标引用' },
  { value: 'model', label: '模型训练' },
  { value: 'manual', label: '手工登记' },
];
const FUNCTION_KINDS = [
  { value: 'udf', label: '自定义函数' },
  { value: 'view', label: '逻辑视图' },
  { value: 'macro', label: '宏' },
];
const EXTERNAL_TYPES = [
  { value: 'postgres', label: 'PostgreSQL', port: 5432, required: 'host, database, user' },
  { value: 'mysql', label: 'MySQL', port: 3306, required: 'host, database, user' },
  { value: 'sqlite', label: 'SQLite', port: 0, required: 'path' },
  { value: 'iceberg', label: 'Apache Iceberg', port: 8181, required: 'catalog_uri, warehouse' },
  { value: 'hive', label: 'Hive Metastore', port: 9083, required: 'host, port' },
];

const state = {
  tree: null,
  models: [],
  externals: [],
  counts: {},
  selected: null,          // { kind: 'table'|'function'|'volume'|'model'|'external', ... }
  tab: 'detail',
  lineageView: 'table',    // table | column
  direction: 'both',       // both | upstream | downstream
  depth: 3,
  graph: null,
};

/* ============================================================ 页面入口 */

export async function renderCatalog2(page, ctx) {
  page.innerHTML = loading('加载目录树…');
  await loadAll(ctx);

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => newExternalCatalog(ctx) }, '＋ 外部目录'),
      el('button', { class: 'btn', onclick: () => newVolume(ctx) }, '＋ 新建卷'),
      el('button', { class: 'btn primary', onclick: () => newFunction(ctx) }, '＋ 新建函数'),
    ])
  );

  const totals = state.counts;
  page.innerHTML = `
    <div class="stat-row" style="margin-bottom:16px">
      ${[
        ['目录', totals.catalogs || 0, 'Catalog'],
        ['表 / 视图', totals.tables || 0, '已登记资产'],
        ['函数 / 卷', `${totals.functions || 0} / ${totals.volumes || 0}`, 'UDF · 非结构化'],
        ['模型', totals.models || 0, '来自模型注册'],
      ].map(([label, value, foot]) => `<div class="card" style="padding:14px 18px">
          <div class="tiny dim">${esc(label)}</div>
          <div style="font-size:22px;font-weight:500;line-height:30px">${esc(String(value))}</div>
          <div class="tiny dim">${esc(foot)}</div>
        </div>`).join('')}
    </div>
    <div class="split" style="grid-template-columns:270px minmax(0,1fr)">
      <div class="tree-panel">
        <div class="tp-head">
          <span>资产目录</span>
          <span class="dim tiny">${totals.tables || 0} 表</span>
        </div>
        <div class="tree" id="c-tree">${renderTree()}</div>
      </div>
      <div id="c-detail">${emptyState('选择左侧任意资产', '查看字段、血缘、影响分析与外部目录', '▤')}</div>
    </div>`;

  bindTree(page, ctx);
  const first = page.querySelector('[data-asset]');
  if (first) {
    first.classList.add('active');
    selectAsset(page, first.dataset.asset, first.dataset.kind, ctx);
  }
}

async function loadAll(ctx) {
  const [tree, models, externals, lsummary] = await Promise.all([
    api.get('/catalog/tree-ext').catch(() => null),
    api.get('/catalog/models').catch(() => ({ models: [] })),
    api.get('/external-catalogs').catch(() => ({ external_catalogs: [] })),
    api.get('/lineage/summary').catch(() => null),
  ]);
  state.tree = tree || { catalogs: [], counts: {} };
  state.counts = { ...(tree?.counts || {}) };
  state.models = models.models || [];
  state.externals = externals.external_catalogs || [];
  state.counts.models = state.counts.models ?? state.models.length;
  state.lineageSummary = lsummary;
}

/* ============================================================== 目录树 */

function treeNode(label, { kind = '', value = '', iconName = 'dot', count = null, indent = 0, extra = '' } = {}) {
  return `<div class="tree-node" ${kind ? `data-kind="${esc(kind)}" data-asset="${esc(value)}"` : ''}
      style="padding-left:${10 + indent * 12}px${kind ? '' : ';cursor:default'}">
    <span class="ico">${icon(iconName, 13)}</span>
    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(label)}</span>
    ${count != null ? `<span class="cnt">${esc(String(count))}</span>` : ''}${extra}
  </div>`;
}

function renderTree() {
  const cats = state.tree.catalogs || [];
  const blocks = [];

  for (const cat of cats) {
    blocks.push(treeNode(cat.name, { iconName: 'catalog' }));
    for (const sch of cat.schemas || []) {
      const ref = `${cat.name}.${sch.name}`;
      const tables = sch.tables || [];
      const funcs = sch.functions || [];
      const vols = sch.volumes || [];
      blocks.push(`<div class="tree-children">`);
      blocks.push(treeNode(sch.name, { iconName: 'folder', indent: 1, count: tables.length }));
      blocks.push(`<div class="tree-children">`);
      for (const t of tables) {
        blocks.push(
          treeNode(t.name, {
            kind: 'table', value: `${ref}.${t.name}`, iconName: t.kind === 'view' ? 'explore' : 'table',
            count: t.row_count, indent: 2,
          })
        );
      }
      for (const f of funcs) {
        blocks.push(treeNode(f.name, { kind: 'function', value: f.id, iconName: 'sql', indent: 2, extra: `<span class="cnt">${esc(f.kind || 'udf')}</span>` }));
      }
      for (const v of vols) {
        blocks.push(treeNode(v.name, { kind: 'volume', value: v.id, iconName: 'folder', indent: 2, count: v.file_count }));
      }
      blocks.push(`</div></div>`);
    }
  }

  if (state.models.length) {
    blocks.push(treeNode(`模型目录`, { iconName: 'registry', count: state.models.length }));
    blocks.push(`<div class="tree-children">`);
    for (const m of state.models) {
      blocks.push(
        treeNode(m.name, {
          kind: 'model', value: m.id, iconName: 'registry', indent: 1,
          extra: `<span class="cnt">${esc(m.version || '-')}</span>`,
        })
      );
    }
    blocks.push(`</div>`);
  }

  if (state.externals.length) {
    blocks.push(treeNode('外部目录', { iconName: 'sources', count: state.externals.length }));
    blocks.push(`<div class="tree-children">`);
    for (const e of state.externals) {
      blocks.push(
        treeNode(e.name, {
          kind: 'external', value: e.id, iconName: 'sources', indent: 1,
          extra: `<span class="cnt">${esc(e.status === 'ok' ? '✓' : '·')}</span>`,
        })
      );
    }
    blocks.push(`</div>`);
  }

  return blocks.join('') || emptyState('还没有任何目录资产', '上传文件或新建 Catalog 后会出现在这里');
}

function bindTree(page, ctx) {
  page.querySelectorAll('[data-asset]').forEach((node) => {
    node.addEventListener('click', () => {
      page.querySelectorAll('.tree-node').forEach((n) => n.classList.remove('active'));
      node.classList.add('active');
      selectAsset(page, node.dataset.asset, node.dataset.kind, ctx);
    });
  });
}

function selectAsset(page, value, kind, ctx) {
  const host = page.querySelector('#c-detail');
  state.selected = { kind, value };
  if (kind === 'table') return showTable(host, value, ctx);
  if (kind === 'function') return showFunction(host, value, ctx);
  if (kind === 'volume') return showVolume(host, value, ctx);
  if (kind === 'model') return showModel(host, value, ctx);
  if (kind === 'external') return showExternal(host, value, ctx);
  return (host.innerHTML = emptyState('未知资产类型'));
}

/* ============================================================ 表详情 */

async function showTable(host, ref, ctx) {
  host.innerHTML = loading('加载表详情…');
  const [detail, preview] = await Promise.all([
    api.get(`/catalog/table?ref=${encodeURIComponent(ref)}`),
    api.get(`/catalog/preview?ref=${encodeURIComponent(ref)}&limit=10`).catch(() => null),
  ]);
  const table = detail.table;
  const pii = (table.columns || []).filter((c) =>
    (c.tags || []).some((t) => ['L3', 'L4'].includes(t.level))
  ).length;

  host.innerHTML = `<div class="col" style="gap:16px">
    <div class="card">
      <div class="card-head">
        <div>
          <h3 class="mono" style="font-size:15px">${esc(table.ref || ref)}</h3>
          <div class="sub">${esc(table.description || '无描述')}</div>
        </div>
        <div class="row">
          <button class="btn sm" id="t-prev">样例数据</button>
          <button class="btn sm" id="t-record">登记血缘</button>
          <button class="btn sm" id="t-discover">SQL 解析</button>
          <button class="btn sm" id="t-gov">敏感扫描</button>
        </div>
      </div>
      <div class="stat-row" style="grid-template-columns:repeat(4,1fr);gap:12px">
        ${[
          ['行数', fmt.num(table.row_count)],
          ['字段数', (table.columns || []).length],
          ['敏感字段', pii],
          ['样例', preview ? `${preview.row_count} 行` : '-'],
        ].map(([k, v]) => `<div style="border:1px solid var(--border);border-radius:4px;padding:10px 14px">
            <div class="tiny dim">${esc(k)}</div><div style="font-size:18px;font-weight:500">${esc(String(v))}</div></div>`).join('')}
      </div>
      <div class="tabs inline" style="margin-top:16px" data-tabs="ctable">
        <button class="active" data-tab="cols">字段</button>
        <button data-tab="lineage">血缘图</button>
        <button data-tab="impact">影响分析</button>
      </div>
      <div id="tab-cols" style="margin-top:8px">
        ${tableHTML(['字段', '类型', '可空', '标签', '说明'], (table.columns || []).map((c) => [
          c.name, c.data_type, c.is_nullable ? '是' : '否',
          (c.tags || []).map((t) => `${t.tag}(${t.level})`).join('、') || '-',
          c.description || '-',
        ]))}
      </div>
      <div id="tab-lineage" class="hidden" style="margin-top:8px"></div>
      <div id="tab-impact" class="hidden" style="margin-top:8px"></div>
    </div>
    <div id="t-prev-body"></div>
  </div>`;

  const tabs = host.querySelectorAll('[data-tabs="ctable"] button');
  const panes = {
    cols: host.querySelector('#tab-cols'),
    lineage: host.querySelector('#tab-lineage'),
    impact: host.querySelector('#tab-impact'),
  };
  let lineageLoaded = false;
  tabs.forEach((b) =>
    b.addEventListener('click', () => {
      tabs.forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      Object.entries(panes).forEach(([k, node]) => node.classList.toggle('hidden', k !== b.dataset.tab));
      if (b.dataset.tab === 'lineage' && !lineageLoaded) {
        lineageLoaded = true;
        renderLineagePanel(panes.lineage, table.ref || ref, ctx);
      }
      if (b.dataset.tab === 'impact') renderImpactPanel(panes.impact, table.ref || ref, ctx);
    })
  );

  host.querySelector('#t-prev').addEventListener('click', () => {
    host.querySelector('#t-prev-body').innerHTML = preview
      ? card('样例数据', tableHTML(preview.columns, preview.rows, { max: 10 }), {
          sub: `前 10 行 · ${preview.elapsed_ms}ms`, flush: true,
        })
      : card('样例数据', emptyState('无法读取样例'));
  });
  host.querySelector('#t-record').addEventListener('click', () => recordLineageDialog(table.ref || ref, ctx, () => showTable(host, ref, ctx)));
  host.querySelector('#t-discover').addEventListener('click', () => discoverDialog(ctx, () => showTable(host, ref, ctx)));
  host.querySelector('#t-gov').addEventListener('click', () => { location.hash = '#/security'; });
}

/* ======================================================== 血缘图（纯 SVG） */

async function renderLineagePanel(host, ref, ctx) {
  host.innerHTML = `
    <div class="row wrap" style="margin-bottom:12px;gap:8px">
      <div class="tabs inline" data-tabs="lview" style="border:none">
        <button class="${state.lineageView === 'table' ? 'active' : ''}" data-tab="table">表级</button>
        <button class="${state.lineageView === 'column' ? 'active' : ''}" data-tab="column">字段级</button>
      </div>
      <div class="divider" style="width:1px;height:18px;background:var(--border)"></div>
      <div class="tabs inline" data-tabs="ldir" style="border:none">
        <button class="${state.direction === 'both' ? 'active' : ''}" data-tab="both">双向</button>
        <button class="${state.direction === 'upstream' ? 'active' : ''}" data-tab="upstream">仅上游</button>
        <button class="${state.direction === 'downstream' ? 'active' : ''}" data-tab="downstream">仅下游</button>
      </div>
      <div class="divider" style="width:1px;height:18px;background:var(--border)"></div>
      <div class="row" style="gap:6px">
        <span class="tiny dim">深度</span>
        <select id="l-depth" style="width:76px;height:26px;font-size:12px">
          ${[1, 2, 3, 4, 5].map((d) => `<option value="${d}"${d === state.depth ? ' selected' : ''}>${d} 层</option>`).join('')}
        </select>
      </div>
      <span class="spacer" style="flex:1"></span>
      <button class="btn sm" id="l-refresh">刷新</button>
    </div>
    <div id="l-graph">${loading('构建血缘图…')}</div>
    <div id="l-legend" class="tiny dim" style="margin-top:10px"></div>`;

  host.querySelectorAll('[data-tabs="lview"] button').forEach((b) =>
    b.addEventListener('click', () => {
      host.querySelectorAll('[data-tabs="lview"] button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.lineageView = b.dataset.tab;
      drawGraph(host, ref, ctx);
    })
  );
  host.querySelectorAll('[data-tabs="ldir"] button').forEach((b) =>
    b.addEventListener('click', () => {
      host.querySelectorAll('[data-tabs="ldir"] button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.direction = b.dataset.tab;
      drawGraph(host, ref, ctx);
    })
  );
  host.querySelector('#l-depth').addEventListener('change', (e) => {
    state.depth = Number(e.target.value);
    drawGraph(host, ref, ctx);
  });
  host.querySelector('#l-refresh').addEventListener('click', () => drawGraph(host, ref, ctx));
  drawGraph(host, ref, ctx);
}

async function drawGraph(host, ref, ctx) {
  const target = host.querySelector('#l-graph');
  target.innerHTML = loading('构建血缘图…');
  let graph;
  try {
    graph = await api.get(
      `/lineage/graph?table_ref=${encodeURIComponent(ref)}&depth=${state.depth}` +
      `&view=${state.lineageView}&direction=${state.direction}`
    );
  } catch (err) {
    target.innerHTML = `<div class="tag red">加载失败</div><div class="tiny mt-8">${esc(err.message)}</div>`;
    return;
  }
  state.graph = graph;
  if (!graph.nodes.length) {
    target.innerHTML = emptyState('该资产还没有血缘', '执行 SQL 建表或手工登记后自动出现在这里');
    return;
  }
  target.innerHTML = svgLineage(graph, ref);
  host.querySelector('#l-legend').innerHTML =
    `共 ${graph.counts.nodes} 个节点 · ${graph.counts.edges} 条边` +
    (graph.view === 'column' ? ` · ${graph.counts.tables} 张表` : '') +
    ' · 点击节点可跳转，点击边可查看映射明细';

  bindGraphEvents(host, graph, ref, ctx);
}

/**
 * 纯 SVG 分层有向图。
 * 布局：按 level 分列（level<0 上游在左，level>0 下游在右，0 居中），
 *      字段级视图先按表分组，再组内排列。
 */
function svgLineage(graph, centerRef) {
  const nodes = graph.nodes;
  const edges = graph.edges;
  const isColumn = graph.view === 'column';

  // ------------------------------------------------------------ 分组
  const groups = new Map();   // key -> { title, level, nodes: [] }
  for (const node of nodes) {
    const groupKey = isColumn ? (node.table || node.ref) : String(node.level);
    const level = node.level;
    if (!groups.has(groupKey)) {
      groups.set(groupKey, {
        key: groupKey,
        title: isColumn ? shortRef(node.table || node.ref) : '',
        level,
        nodes: [],
      });
    }
    groups.get(groupKey).nodes.push(node);
  }
  const ordered = [...groups.values()].sort((a, b) => a.level - b.level);

  // ------------------------------------------------------- 坐标计算
  const COL_W = isColumn ? 168 : 190;      // 列宽（含间距）
  const NODE_H = isColumn ? 24 : 34;       // 节点高
  const NODE_GAP = isColumn ? 6 : 12;      // 节点间距
  const GROUP_GAP = 26;                    // 组间额外间距
  const PAD_X = 28;
  const PAD_Y = 24;
  const HEAD_H = isColumn ? 20 : 0;

  let maxColHeight = 0;
  const columns = ordered.map((group, ci) => {
    const innerH = group.nodes.length * NODE_H + Math.max(0, group.nodes.length - 1) * NODE_GAP;
    const h = innerH + HEAD_H;
    maxColHeight = Math.max(maxColHeight, h);
    return { ...group, x: PAD_X + ci * COL_W, height: h, innerH };
  });

  const canvasH = Math.max(maxColHeight + PAD_Y * 2, 160);
  const canvasW = Math.max(PAD_X * 2 + columns.length * COL_W - (COL_W - (isColumn ? 132 : 150)), 320);

  const pos = new Map();  // node.id -> {x,y,w,h,cx,cy}
  for (const col of columns) {
    const startY = PAD_Y + (canvasH - PAD_Y * 2 - col.height) / 2 + HEAD_H;
    col.nodes
      .slice()
      .sort((a, b) => (a.distance - b.distance) || String(a.label).localeCompare(String(b.label)))
      .forEach((node, i) => {
        const y = startY + i * (NODE_H + NODE_GAP);
        pos.set(node.id, {
          x: col.x,
          y,
          w: isColumn ? 132 : 150,
          h: NODE_H,
          cx: col.x + (isColumn ? 66 : 75),
          cy: y + NODE_H / 2,
          node,
        });
      });
    col.startY = startY;
  }

  // -------------------------------------------------------- 绘制 SVG
  const parts = [];
  parts.push(
    `<svg viewBox="0 0 ${canvasW} ${canvasH}" width="100%" height="${canvasH}" ` +
    `style="display:block;font-family:var(--font);background:#fff" xmlns="http://www.w3.org/2000/svg">`
  );
  parts.push(`<defs>
    <marker id="lg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#b9bcc4"/>
    </marker>
    <marker id="lg-arrow-hot" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#0052d9"/>
    </marker>
  </defs>`);

  // 列标题（字段级视图显示所属表）
  if (isColumn) {
    for (const col of columns) {
      parts.push(
        `<text x="${col.x}" y="${PAD_Y - 6}" font-size="11" fill="#8b8f99" ` +
        `font-weight="500">${esc(shortRef(col.title))}</text>`
      );
    }
  }

  // 边（先画，压在节点下面）
  for (const edge of edges) {
    const a = pos.get(edge.from);
    const b = pos.get(edge.to);
    if (!a || !b) continue;
    const forward = b.cx >= a.cx;
    const x1 = forward ? a.x + a.w : a.x;
    const x2 = forward ? b.x : b.x + b.w;
    const dx = Math.max(36, Math.abs(x2 - x1) * 0.42);
    const c1 = forward ? x1 + dx : x1 - dx;
    const c2 = forward ? x2 - dx : x2 + dx;
    const d = `M ${x1} ${a.cy} C ${c1} ${a.cy}, ${c2} ${b.cy}, ${x2} ${b.cy}`;
    const label = isColumn && edge.src_column ? `${edge.src_column}→${edge.dst_column}` : '';
    parts.push(
      `<g class="lg-edge" data-edge="${esc(edge.id)}" data-from="${esc(edge.from)}" ` +
      `data-to="${esc(edge.to)}" style="cursor:pointer">` +
      `<path d="${d}" fill="none" stroke="transparent" stroke-width="12"/>` +
      `<path d="${d}" fill="none" stroke="#d5d8de" stroke-width="1.4" marker-end="url(#lg-arrow)" ` +
      `data-line="1"/></g>`
    );
    if (label) {
      const mx = (x1 + x2) / 2;
      const my = (a.cy + b.cy) / 2 - 4;
      parts.push(
        `<text x="${mx}" y="${my}" font-size="9.5" fill="#8b8f99" text-anchor="middle" ` +
        `pointer-events="none">${esc(label.length > 22 ? label.slice(0, 22) + '…' : label)}</text>`
      );
    }
  }

  // 节点
  for (const p of pos.values()) {
    const { node, x, y, w, h } = p;
    const isCenter = node.ref === centerRef || node.is_center;
    const tone = TYPE_TONE[node.type] || '';
    const fill = isCenter ? '#e8f0fe' : '#fff';
    const stroke = isCenter ? '#0052d9' : tone === 'red' ? '#e34d59' : tone === 'green' ? '#2ba471'
      : tone === 'orange' ? '#ed7b2f' : '#dcdfe6';
    const textColor = isCenter ? '#0052d9' : '#1f2329';
    const label = String(node.label || node.ref);
    const short = label.length > 15 ? label.slice(0, 15) + '…' : label;
    parts.push(
      `<g class="lg-node" data-node="${esc(node.ref)}" data-ntype="${esc(node.type)}" style="cursor:pointer">` +
      `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${isColumn ? 5 : 7}" ` +
      `fill="${fill}" stroke="${stroke}" stroke-width="${isCenter ? 1.8 : 1.1}"/>` +
      (isColumn
        ? `<text x="${x + 10}" y="${y + h / 2 + 3.6}" font-size="10.5" fill="${textColor}" ` +
          `font-weight="${isCenter ? 600 : 400}">${esc(short)}</text>`
        : `<text x="${x + 11}" y="${y + 14}" font-size="11.5" fill="${textColor}" ` +
          `font-weight="${isCenter ? 600 : 500}">${esc(short)}</text>` +
          `<text x="${x + 11}" y="${y + 27}" font-size="9.5" fill="#8b8f99">${esc(node.type_label || '')}` +
          `${node.level ? ` · L${Math.abs(node.level)}` : ' · 当前'}</text>`) +
      `</g>`
    );
  }

  parts.push('</svg>');
  return `<div style="border:1px solid var(--border);border-radius:8px;overflow:auto;background:#fff">
    ${parts.join('')}</div>`;
}

function bindGraphEvents(host, graph, ref, ctx) {
  const svg = host.querySelector('#l-graph svg');
  if (!svg) return;

  svg.querySelectorAll('.lg-node').forEach((g) => {
    g.addEventListener('mouseenter', () => {
      const target = g.dataset.node;
      svg.querySelectorAll('.lg-edge').forEach((eg) => {
        const hot = eg.dataset.from === target || eg.dataset.to === target;
        const line = eg.querySelector('[data-line]');
        if (line) {
          line.setAttribute('stroke', hot ? '#0052d9' : '#d5d8de');
          line.setAttribute('stroke-width', hot ? '2' : '1.4');
          line.setAttribute('marker-end', hot ? 'url(#lg-arrow-hot)' : 'url(#lg-arrow)');
        }
      });
      const rect = g.querySelector('rect');
      if (rect) rect.setAttribute('stroke-width', '2');
    });
    g.addEventListener('mouseleave', () => {
      svg.querySelectorAll('.lg-edge [data-line]').forEach((line) => {
        line.setAttribute('stroke', '#d5d8de');
        line.setAttribute('stroke-width', '1.4');
        line.setAttribute('marker-end', 'url(#lg-arrow)');
      });
      const rect = g.querySelector('rect');
      if (rect) rect.setAttribute('stroke-width', rect.getAttribute('stroke') === '#0052d9' ? '1.8' : '1.1');
    });
    g.addEventListener('click', () => {
      const target = g.dataset.node;
      const ntype = g.dataset.ntype;
      if (ntype === 'table') {
        // 点击节点：在当前页切换中心表（而不是整页跳转，保持血缘面板状态）
        const page = host.closest('#page');
        if (page && target !== ref) {
          state.selected = { kind: 'table', value: target };
          page.querySelectorAll('.tree-node').forEach((n) => n.classList.remove('active'));
          showTable(page.querySelector('#c-detail'), target, ctx);
        }
      } else if (ntype === 'dashboard') {
        location.hash = '#/dashboards';
      } else if (ntype === 'metric') {
        location.hash = '#/semantic';
      } else if (ntype === 'model') {
        location.hash = '#/registry';
      } else {
        toast(`${target}（${ntype}）`);
      }
    });
  });

  svg.querySelectorAll('.lg-edge').forEach((g) => {
    g.addEventListener('click', () => {
      const edge = graph.edges.find((e) => e.id === g.dataset.edge);
      if (!edge) return;
      drawer({
        title: `血缘明细 · ${edge.kind_label || edge.kind}`,
        body: `<div class="col" style="gap:14px">
          ${descList([
            ['上游', `<span class="mono">${esc(edge.from)}</span>`],
            ['下游', `<span class="mono">${esc(edge.to)}</span>`],
            ['类型', tag(edge.kind_label || edge.kind, 'outline')],
            ['字段映射', String(edge.column_count || (edge.column_map || []).length) + ' 条'],
            ['说明', esc(edge.detail || '-')],
          ])}
          ${(edge.column_map || []).length
            ? card('字段级映射', tableHTML(
                ['上游字段', '下游字段', '转换表达式'],
                (edge.column_map || []).map((c) => [c.src_column || '-', c.dst_column || '-', c.transform || '-'])
              ), { flush: true })
            : '<div class="tiny dim">该边没有字段级映射，可在「登记血缘」里补充</div>'}
        </div>`,
      });
    });
  });
}

function shortRef(ref) {
  const parts = String(ref || '').split('.');
  return parts.length >= 2 ? `${parts[parts.length - 2]}.${parts[parts.length - 1]}` : String(ref || '');
}

/* ========================================================== 影响分析 */

async function renderImpactPanel(host, ref, ctx) {
  host.innerHTML = loading('分析下游影响…');
  let impact;
  try {
    impact = await api.get(`/lineage/impact?table_ref=${encodeURIComponent(ref)}&max_depth=5`);
  } catch (err) {
    host.innerHTML = `<div class="tag red">分析失败</div><div class="tiny mt-8">${esc(err.message)}</div>`;
    return;
  }
  if (!impact.has_impact) {
    host.innerHTML = emptyState('该资产没有下游影响', '它是血缘链路的叶子节点，变更不会波及其他资产');
    return;
  }
  const typeCards = [
    ['table', '下游表', impact.tables, 'blue'],
    ['dashboard', '仪表盘', impact.dashboards, 'green'],
    ['metric', '指标', impact.metrics, 'orange'],
    ['model', '模型', impact.models, 'red'],
  ];
  host.innerHTML = `<div class="col" style="gap:14px">
    <div class="grid-4" style="gap:12px">
      ${typeCards.map(([k, label, count, tone]) => `<div style="border:1px solid var(--border);border-radius:6px;padding:12px 14px">
        <div class="tiny dim">${esc(label)}</div>
        <div style="font-size:20px;font-weight:500">${esc(String(count || 0))}</div>
        <div class="tiny" style="color:var(--${tone === 'blue' ? 'brand' : tone === 'green' ? 'success' : tone === 'orange' ? 'warning' : 'error'})">
          ${count ? '受影响' : '无'}</div>
      </div>`).join('')}
    </div>
    <div class="info-strip">
      <span class="tag orange">影响分析</span>
      <span>修改 <span class="mono">${esc(impact.table_ref)}</span> 将影响 <b>${impact.total}</b> 个下游资产，
      最深 ${Math.max(0, ...impact.assets.map((a) => a.distance))} 跳。建议先通知相关看板与模型负责人。</span>
    </div>
    ${card('受影响资产清单', dataTable({
      rows: impact.assets,
      rowKey: 'ref',
      columns: [
        { title: '资产', render: (r) => `<div class="cell-main mono">${esc(r.ref)}</div>
          <div class="cell-sub">${esc(r.path_text)}</div>` },
        { title: '类型', render: (r) => tag(r.type_label, TYPE_TONE[r.type] || 'outline') },
        { title: '距离', align: 'right', render: (r) => `${r.distance} 跳` },
        { title: '操作', key: 'ops', render: (r) =>
          r.type === 'table' ? `<a class="btn-link" data-jump="${esc(r.ref)}">查看血缘</a>` : '<span class="dim tiny">-</span>' },
      ],
    }), { flush: true, sub: `共 ${impact.total} 项，按距离排序` })}
  </div>`;

  host.querySelectorAll('[data-jump]').forEach((b) =>
    b.addEventListener('click', () => {
      const page = host.closest('#page');
      state.selected = { kind: 'table', value: b.dataset.jump };
      showTable(page.querySelector('#c-detail'), b.dataset.jump, ctx);
    })
  );
}

/* ======================================================== 函数详情 */

async function showFunction(host, id, ctx) {
  host.innerHTML = loading();
  const { functions } = await api.get('/functions');
  const fn = functions.find((f) => f.id === id);
  if (!fn) return (host.innerHTML = emptyState('函数不存在'));
  host.innerHTML = card(`函数 · ${fn.name}`, `<div class="col" style="gap:16px">
    ${descList([
      ['完整引用', `<span class="mono">${esc(fn.ref || fn.name)}</span>`],
      ['类型', tag(fn.kind_label || fn.kind, 'outline')],
      ['所属 Schema', `${esc(fn.catalog_name || '')}.${esc(fn.schema_name || '')}`],
      ['说明', esc(fn.description || '-')],
      ['创建时间', fmt.dt(fn.created_at)],
    ])}
    ${fn.body ? `<div><div class="tiny dim" style="margin-bottom:6px">定义</div>
      <pre class="code" style="max-height:320px;overflow:auto">${esc(fn.body)}</pre></div>` : ''}
    <div class="row">
      <button class="btn sm danger" id="f-del">删除函数</button>
    </div>
  </div>`, { sub: fn.kind_label });

  host.querySelector('#f-del').addEventListener('click', async () => {
    if (!(await confirmDialog('删除函数', `确认删除 ${fn.name}？`, '删除'))) return;
    await api.del(`/functions/${fn.id}`);
    toast('函数已删除', 'ok');
    ctx.refresh();
  });
}

/* ======================================================== 卷详情 */

async function showVolume(host, id, ctx) {
  host.innerHTML = loading('读取卷文件…');
  let out;
  try {
    out = await api.get(`/volumes/${id}/files`);
  } catch (err) {
    return (host.innerHTML = `<div class="tag red">${esc(err.message)}</div>`);
  }
  const vol = out.volume;
  host.innerHTML = card(`卷 · ${vol.name}`, `<div class="col" style="gap:16px">
    ${descList([
      ['位置', `<span class="mono">${esc(vol.ref || `${vol.catalog}.${vol.schema}.${vol.name}`)}</span>`],
      ['物理路径', `<span class="mono tiny">${esc(vol.path)}</span>`],
      ['文件数', String(out.summary.count)],
      ['占用空间', fmt.bytes(out.summary.size_bytes)],
      ['创建时间', fmt.dt(vol.created_at)],
    ])}
    ${Object.keys(out.summary.by_ext || {}).length
      ? `<div class="row wrap" style="gap:6px">${Object.entries(out.summary.by_ext)
          .map(([ext, n]) => tag(`${ext} × ${n}`, 'outline')).join('')}</div>`
      : ''}
    ${out.files.length
      ? dataTable({
          rows: out.files,
          rowKey: 'relative_path',
          columns: [
            { title: '文件名', render: (r) => `<span class="mono">${esc(r.relative_path)}</span>` },
            { title: '大小', align: 'right', render: (r) => fmt.bytes(r.size) },
            { title: '修改时间', render: (r) => `<span class="dim">${fmt.dt(r.modified)}</span>` },
          ],
        })
      : emptyState('卷内还没有文件', '用「数据接入 → 文件到卷」上传图片、PDF、日志等非结构化文件')}
    <div class="row"><button class="btn sm danger" id="v-del">删除卷</button>
      <span class="tiny dim">删除会同时清理磁盘上的文件</span></div>
  </div>`, { sub: '非结构化文件存放位置' });

  host.querySelector('#v-del').addEventListener('click', async () => {
    if (!(await confirmDialog('删除卷', '卷内所有文件会被一并删除，确认继续？', '删除'))) return;
    await api.del(`/volumes/${vol.id}`);
    toast('卷已删除', 'ok');
    ctx.refresh();
  });
}

/* ======================================================== 模型详情 */

function showModel(host, id, ctx) {
  const model = state.models.find((m) => m.id === id);
  if (!model) return (host.innerHTML = emptyState('模型不存在'));
  host.innerHTML = card(`模型 · ${model.name}`, `<div class="col" style="gap:16px">
    ${descList([
      ['名称', esc(model.name)],
      ['框架', esc(model.framework || '-')],
      ['负责人', esc(model.owner || '-')],
      ['当前阶段', tag(model.stage === 'production' ? '生产' : model.stage, model.stage === 'production' ? 'green' : 'outline')],
      ['线上版本', `<span class="mono">${esc(model.version || '-')}</span>`],
      ['版本数', String(model.versions)],
      ['来源实验', esc(model.source_experiment || '-')],
      ['说明', esc(model.description || '-')],
    ])}
    ${Object.keys(model.metrics || {}).length
      ? card('线上版本指标', tableHTML(['指标', '值'],
          Object.entries(model.metrics).map(([k, v]) => [k, String(v)])))
      : ''}
    ${(model.version_list || []).length
      ? card('版本时间线', dataTable({
          rows: model.version_list,
          rowKey: 'id',
          columns: [
            { title: '版本', render: (r) => `<span class="mono">${esc(r.version)}</span>` },
            { title: '阶段', render: (r) => tag(
                r.stage === 'production' ? '生产' : r.stage === 'staging' ? '预发' : '归档',
                r.stage === 'production' ? 'green' : r.stage === 'staging' ? 'orange' : '') },
            { title: '创建时间', render: (r) => `<span class="dim">${fmt.dt(r.created_at)}</span>` },
          ],
        }), { flush: true })
      : ''}
    <div class="row"><button class="btn sm" id="m-go">前往模型管理</button></div>
  </div>`, { sub: '来自模型注册中心（W4 MLOps）' });

  host.querySelector('#m-go').addEventListener('click', () => { location.hash = '#/registry'; });
}

/* ==================================================== 外部目录详情 */

function showExternal(host, id, ctx) {
  const ext = state.externals.find((e) => e.id === id);
  if (!ext) return (host.innerHTML = emptyState('外部目录不存在'));
  const cfg = ext.config || {};
  host.innerHTML = card(`外部目录 · ${ext.name}`, `<div class="col" style="gap:16px">
    ${descList([
      ['类型', tag(ext.type_label || ext.type, 'outline')],
      ['状态', statusTag(ext.status === 'ok' ? 'ok' : ext.status === 'failed' ? 'failed' : 'draft')],
      ['配置校验', ext.config_ok ? tag('通过', 'green') : tag('有问题', 'red')],
      ['最近同步', ext.last_sync_at ? fmt.dt(ext.last_sync_at) : '从未同步'],
      ['说明', esc(ext.description || '-')],
    ])}
    ${card('连接配置', tableHTML(['配置项', '值'],
        Object.entries(cfg).map(([k, v]) => [k, String(v)])))}
    <div class="row wrap">
      <button class="btn sm primary" id="e-test">连通性测试</button>
      <button class="btn sm" id="e-sync">同步元数据</button>
      <button class="btn sm" id="e-edit">编辑配置</button>
    </div>
    <div id="e-result"></div>
  </div>`, { sub: EXTERNAL_TYPES.find((t) => t.value === ext.type)?.label || ext.type });

  const out = host.querySelector('#e-result');
  host.querySelector('#e-test').addEventListener('click', async (e) => {
    e.target.disabled = true;
    out.innerHTML = loading('测试连接中…');
    try {
      const res = await api.post(`/external-catalogs/${ext.id}/test`, {});
      out.innerHTML = `<div class="info-strip" style="background:${res.ok ? '#e8f8f2' : '#fdecee'};
          border-color:${res.ok ? '#bfe9d6' : '#f7c5c9'}">
        <span class="tag ${res.ok ? 'green' : 'red'}">${res.ok ? '连接成功' : '连接失败'}</span>
        <span>${esc(res.message)}</span></div>
        ${(res.issues || []).length
          ? `<div class="tiny mt-8" style="color:var(--error)">${res.issues.map((i) => `· ${esc(i)}`).join('<br>')}</div>`
          : ''}
        ${(res.tables || []).length
          ? card(`发现 ${res.tables.length} 张表`, tableHTML(
              ['表名', '字段数', '行数', '来源'],
              res.tables.map((t) => [t.name, String((t.columns || []).length), fmt.num(t.row_count),
                t.simulated ? '模拟抓取' : '真实读取'])), { flush: true })
          : ''}`;
      toast(res.message, res.ok ? 'ok' : 'err');
    } catch (err) {
      out.innerHTML = `<div class="tag red">${esc(err.message)}</div>`;
    } finally {
      e.target.disabled = false;
    }
  });
  host.querySelector('#e-sync').addEventListener('click', async (e) => {
    e.target.disabled = true;
    out.innerHTML = loading('同步元数据中…');
    try {
      const res = await api.post(`/external-catalogs/${ext.id}/sync`, {});
      out.innerHTML = `<div class="info-strip"><span class="tag green">同步完成</span>
        <span>新建 ${res.created} 张 · 更新 ${res.updated} 张 · 目标 ${esc(res.target)}</span></div>`;
      toast('元数据已同步', 'ok');
      setTimeout(() => ctx.refresh(), 900);
    } catch (err) {
      out.innerHTML = `<div class="tag red">${esc(err.message)}</div>`;
    } finally {
      e.target.disabled = false;
    }
  });
  host.querySelector('#e-edit').addEventListener('click', async () => {
    const data = await formModal({
      title: `编辑外部目录 · ${ext.name}`,
      wide: true,
      fields: [
        { label: '名称', name: 'name', value: ext.name, required: true },
        { label: '类型', name: 'type', type: 'select', value: ext.type,
          options: EXTERNAL_TYPES.map((t) => ({ value: t.value, label: t.label })) },
        { label: '配置（JSON）', name: 'config', type: 'textarea', rows: 8,
          value: JSON.stringify(cfg, null, 2),
          hint: '不同类型必填项不同：postgres/mysql 需 host+database+user；sqlite 需 path；iceberg 需 catalog_uri+warehouse；hive 需 host+port' },
        { label: '说明', name: 'description', value: ext.description || '' },
      ],
      onSubmit: async (d) => {
        let parsed;
        try {
          parsed = JSON.parse(d.config || '{}');
        } catch {
          throw new Error('配置不是合法 JSON');
        }
        await api.patch(`/external-catalogs/${ext.id}`, {
          name: d.name, type: d.type, config: parsed, description: d.description,
        });
        toast('已保存', 'ok');
      },
    });
    if (data) ctx.refresh();
  });
}

/* ========================================================== 新建动作 */

async function newVolume(ctx) {
  const cats = (state.tree.catalogs || []).map((c) => c.name);
  const targets = [];
  for (const c of state.tree.catalogs || []) {
    for (const s of c.schemas || []) targets.push(`${c.name}.${s.name}`);
  }
  const data = await formModal({
    title: '新建卷',
    fields: [
      { label: '卷名', name: 'name', required: true, placeholder: '例如 raw_files',
        hint: '卷用于存放图片、PDF、日志等非结构化文件' },
      { label: '所属位置', name: 'target', type: 'select',
        options: targets.length ? targets : ['demo.retail'] },
    ],
    onSubmit: async (d) => {
      const [cat, sch] = d.target.split('.');
      await api.post('/volumes', { name: d.name, catalog: cat, schema: sch });
      toast('卷已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

async function newFunction(ctx) {
  const targets = [];
  for (const c of state.tree.catalogs || []) {
    for (const s of c.schemas || []) targets.push(`${c.name}.${s.name}`);
  }
  if (!targets.length) return toast('请先创建 Catalog 与 Schema', 'err');
  const data = await formModal({
    title: '新建函数 / 视图',
    wide: true,
    fields: [
      { label: '所属 Schema', name: 'schema_ref', type: 'select', options: targets },
      { label: '名称', name: 'name', required: true, placeholder: '例如 calc_gmv' },
      { label: '类型', name: 'kind', type: 'select',
        options: FUNCTION_KINDS.map((k) => ({ value: k.value, label: k.label })) },
      { label: '定义', name: 'body', type: 'textarea', rows: 6,
        placeholder: 'amount * 1.0\n-- 或 SELECT * FROM demo.retail.fact_orders' },
      { label: '说明', name: 'description', placeholder: '可选' },
    ],
    onSubmit: async (d) => {
      await api.post('/functions', d);
      toast('函数已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

async function newExternalCatalog(ctx) {
  const data = await formModal({
    title: '新建外部目录',
    wide: true,
    intro: `<div class="info-strip" style="margin-bottom:16px">
      <span class="tag blue">提示</span>
      <span>SQLite 类型可真连读取表结构；其余类型只做配置校验 + 模拟元数据抓取，
      不需要安装任何数据库驱动。</span></div>`,
    fields: [
      { label: '名称', name: 'name', required: true, placeholder: '例如 生产PG只读' },
      { label: '类型', name: 'type', type: 'select',
        options: EXTERNAL_TYPES.map((t) => ({ value: t.value, label: t.label })) },
      { label: '配置（JSON）', name: 'config', type: 'textarea', rows: 8,
        value: JSON.stringify({ host: "127.0.0.1", port: 5432, database: "demo", user: "readonly" }, null, 2),
        hint: 'postgres/mysql 需 host+database+user；sqlite 需 {"path": "D:/data/x.db"}；iceberg 需 catalog_uri+warehouse；hive 需 host+port' },
      { label: '说明', name: 'description', placeholder: '可选' },
    ],
    onSubmit: async (d) => {
      let parsed;
      try {
        parsed = JSON.parse(d.config || '{}');
      } catch {
        throw new Error('配置不是合法 JSON');
      }
      const res = await api.post('/external-catalogs', {
        name: d.name, type: d.type, config: parsed, description: d.description,
      });
      if ((res.issues || []).length) {
        toast(`已创建，但配置有问题：${res.issues[0]}`, 'err');
      } else {
        toast('外部目录已创建', 'ok');
      }
    },
  });
  if (data) ctx.refresh();
}

/* ==================================================== 血缘登记弹窗 */

function recordLineageDialog(centerRef, ctx, onDone) {
  formModal({
    title: '登记血缘',
    wide: true,
    intro: `<div class="tiny dim" style="margin-bottom:14px">
      表级血缘只需填上下游；字段级血缘再填上游字段与下游字段。
      同一组上下游重复登记不会产生重复记录（幂等）。
    </div>`,
    fields: [
      { label: '上游资产', name: 'upstream', required: true, value: centerRef,
        hint: '表写 catalog.schema.table；资产可加前缀，如 dashboard:首页看板 / metric:gmv / model:churn_v2' },
      { label: '下游资产', name: 'downstream', required: true, placeholder: 'catalog.schema.table' },
      { label: '关系类型', name: 'kind', type: 'select',
        options: KIND_OPTIONS.map((k) => ({ value: k.value, label: k.label })) },
      { label: '上游字段', name: 'src_column', placeholder: '留空则为表级血缘' },
      { label: '下游字段', name: 'dst_column', placeholder: '留空则为表级血缘' },
      { label: '转换表达式', name: 'transform', placeholder: '例如 sum(amount)' },
      { label: '说明', name: 'detail', placeholder: '可选' },
    ],
    onSubmit: async (d) => {
      const body = {
        upstream: d.upstream, downstream: d.downstream, kind: d.kind,
        src_column: d.src_column || null, dst_column: d.dst_column || null,
        transform: d.transform || '', detail: d.detail || '',
      };
      const res = await api.post('/lineage/record', body);
      toast(res.created ? '血缘已登记' : '血缘已存在，已更新说明', 'ok');
    },
  }).then((d) => { if (d && onDone) onDone(); });
}

function discoverDialog(ctx, onDone) {
  formModal({
    title: '从 SQL 自动解析血缘',
    wide: true,
    intro: `<div class="tiny dim" style="margin-bottom:14px">
      支持 CREATE TABLE ... AS SELECT / CREATE VIEW / INSERT INTO / MERGE INTO，
      自动识别 FROM 与 JOIN 的源表；单源表投影还会推断字段级映射。
    </div>`,
    fields: [
      { label: 'SQL 语句', name: 'sql', type: 'textarea', rows: 10, required: true,
        value: `CREATE OR REPLACE TABLE demo.retail.dws_daily_gmv AS
SELECT
  order_date,
  sum(amount) AS gmv,
  count(DISTINCT order_id) AS order_cnt
FROM demo.retail.fact_orders
GROUP BY 1` },
      { label: '下游资产（可选）', name: 'downstream',
        hint: '留空则用 SQL 里解析出的目标表' },
      { label: '同时落库', name: 'save', type: 'checkbox', value: true,
        hint: '勾选后把识别到的血缘写入元数据库' },
    ],
    onSubmit: async (d) => {
      const res = await api.post('/lineage/discover', {
        sql: d.sql, downstream: d.downstream || null,
        save: !!d.save, record_columns: true,
      });
      const target = res.target || '(未识别目标)';
      toast(`解析完成：${res.sources?.length || 0} 个源表 → ${target}`, 'ok');
      if (res.sources?.length) {
        drawer({
          title: '解析结果',
          wide: true,
          body: `<div class="col" style="gap:14px">
            ${descList([
              ['目标资产', `<span class="mono">${esc(target)}</span>`],
              ['关系类型', tag(res.kind_label || res.kind, 'outline')],
              ['源表数', String(res.sources.length)],
              ['字段映射', String((res.columns || []).length)],
              ['落库结果', res.recorded != null ? `表级 ${res.recorded} 条 · 字段级 ${res.recorded_columns || 0} 条` : '未落库'],
            ])}
            ${(res.sources || []).length
              ? card('识别到的源表', tableHTML(['源表'], res.sources.map((s) => [s])))
              : ''}
            ${(res.columns || []).length
              ? card('推断出的字段映射', tableHTML(
                  ['上游字段', '下游字段', '转换表达式'],
                  res.columns.map((c) => [c.src_column, c.dst_column, c.transform || '-'])))
              : ''}
            ${(res.notes || []).length
              ? card('解析说明', `<ul style="margin:0;padding-left:18px;line-height:1.9">
                  ${res.notes.map((n) => `<li class="tiny">${esc(n)}</li>`).join('')}</ul>`)
              : ''}
          </div>`,
        });
      }
    },
  }).then((d) => { if (d && onDone) onDone(); });
}
