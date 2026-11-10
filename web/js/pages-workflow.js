// 数据工程 · 工作流开发：9 种节点 / 12 种运行条件 / 两层参数 / DAG 可视化 / 运行抽屉
import { api, fmt } from './api.js';
import {
  card, dataTable, descList, drawer, el, emptyState, esc, formModal,
  confirmDialog, loading, modal, statusTag, tag, toast,
} from './ui.js';

/* ------------------------------------------------------------------ 常量 */

const STATUS_COLOR = {
  success: '#2ba471',
  failed: '#d54941',
  running: '#0052d9',
  waiting: '#e37318',
  skipped: '#b5b9c0',
  idle: '#c9cdd4',
};

const STATUS_LABEL = {
  success: '成功',
  failed: '失败',
  running: '运行中',
  waiting: '等待审批',
  skipped: '已跳过',
  idle: '未运行',
};

const RUN_MODE_LABEL = { cron: '周期调度', continuous: '持续运行', manual: '仅手动' };

// ui.js 的 statusTag 不认识 skipped/waiting/idle，且 running 是绿色；
// 工作流状态语义不同，这里用本地映射保证颜色一致。
const STATUS_TONE = {
  success: 'green',
  failed: 'red',
  running: 'blue',
  waiting: 'orange',
  skipped: '',
  idle: '',
};

function wfStatusTag(status) {
  const key = String(status || 'idle');
  const tone = STATUS_TONE[key] ?? '';
  const label = STATUS_LABEL[key] || key;
  return `<span class="tag ${tone}"><i class="dot" style="background:currentColor;width:5px;height:5px;border-radius:50%"></i>${esc(label)}</span>`;
}

// 页面级状态（同一会话内切换视图复用）
const state = {
  view: 'list',
  flowId: '',
  flows: [],
  meta: null,
  detail: null,
};

/* ------------------------------------------------------------------ 工具 */

function jsonPretty(v, fallback = '{}') {
  if (v === null || v === undefined || v === '') return fallback;
  if (typeof v === 'string') {
    try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; }
  }
  try { return JSON.stringify(v, null, 2); } catch { return fallback; }
}

function parseJSON(text, label) {
  const raw = String(text ?? '').trim();
  if (!raw) return {};
  try {
    const out = JSON.parse(raw);
    if (out && typeof out === 'object' && !Array.isArray(out)) return out;
    throw new Error('需要是一个对象');
  } catch (e) {
    throw new Error(`${label} 不是合法 JSON：${e.message}`);
  }
}

function durationText(start, end) {
  if (!start || !end) return '-';
  const ms = new Date(end) - new Date(start);
  if (!Number.isFinite(ms) || ms < 0) return '-';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function truncate(s, n = 120) {
  const t = String(s ?? '');
  return t.length > n ? t.slice(0, n) + '…' : t;
}

/* ------------------------------------------------------------- DAG 布局 */

/**
 * 分层布局：按最长路径把节点分到不同列，同列按拓扑顺序纵向排布。
 * 返回 { width, height, boxes:[{id,node,x,y,w,h,layer}], links:[{from,to,path}] }
 */
function layoutDAG(nodes, edges) {
  const W = 178;
  const H = 62;
  const GAP_X = 74;
  const GAP_Y = 26;
  const PAD = 24;

  const ids = nodes.map((n) => n.id);
  const known = new Set(ids);
  const indeg = new Map(ids.map((id) => [id, 0]));
  const children = new Map(ids.map((id) => [id, []]));
  const cleanEdges = [];
  for (const edge of edges || []) {
    const s = String(edge?.[0] ?? '');
    const t = String(edge?.[1] ?? '');
    if (!known.has(s) || !known.has(t) || s === t) continue;
    cleanEdges.push([s, t]);
    indeg.set(t, (indeg.get(t) || 0) + 1);
    children.get(s).push(t);
  }

  const layer = new Map(ids.map((id) => [id, 0]));
  const order = [];
  const queue = ids.filter((id) => indeg.get(id) === 0);
  const pending = new Map(indeg);
  let guard = 0;
  while (queue.length && guard++ < 20000) {
    const id = queue.shift();
    order.push(id);
    for (const child of children.get(id)) {
      layer.set(child, Math.max(layer.get(child) ?? 0, (layer.get(id) ?? 0) + 1));
      pending.set(child, (pending.get(child) || 0) - 1);
      if (pending.get(child) === 0) queue.push(child);
    }
  }
  // 环里的节点兜底：追加到末尾，避免画不出来
  for (const id of ids) if (!order.includes(id)) order.push(id);

  const byLayer = new Map();
  for (const id of order) {
    const l = layer.get(id) ?? 0;
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l).push(id);
  }
  const maxLayer = Math.max(0, ...Array.from(byLayer.keys()));
  const maxRows = Math.max(1, ...Array.from(byLayer.values()).map((v) => v.length));

  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const boxes = [];
  for (let l = 0; l <= maxLayer; l += 1) {
    const column = byLayer.get(l) || [];
    const columnHeight = column.length * H + Math.max(0, column.length - 1) * GAP_Y;
    const totalHeight = maxRows * H + Math.max(0, maxRows - 1) * GAP_Y;
    const offsetY = PAD + (totalHeight - columnHeight) / 2;
    column.forEach((id, i) => {
      boxes.push({
        id,
        node: nodeById.get(id),
        layer: l,
        x: PAD + l * (W + GAP_X),
        y: offsetY + i * (H + GAP_Y),
        w: W,
        h: H,
      });
    });
  }

  for (const b of boxes) {
    const p = b.node?.position;
    if (p && Number.isFinite(p.x) && Number.isFinite(p.y)) { b.x = Math.max(PAD, Math.min(5000, p.x)); b.y = Math.max(PAD, Math.min(5000, p.y)); }
  }
  const boxById = new Map(boxes.map((b) => [b.id, b]));
  const links = [];
  for (const [s, t] of cleanEdges) {
    const a = boxById.get(s);
    const b = boxById.get(t);
    if (!a || !b) continue;
    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    let path;
    if (x2 >= x1 + 8) {
      const mid = (x1 + x2) / 2;
      path = `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
    } else {
      // 反向 / 同列边：绕一下
      const bulge = 40 + Math.abs(y2 - y1) * 0.15;
      path = `M ${a.x + a.w / 2} ${a.y + a.h} C ${a.x + a.w / 2 - bulge} ${a.y + a.h + bulge}, ${b.x + b.w / 2 + bulge} ${b.y + b.h + bulge}, ${b.x + b.w / 2} ${b.y + b.h}`;
    }
    links.push({ from: s, to: t, path, backward: x2 < x1 + 8 });
  }

  return {
    width: Math.max(PAD * 2 + (maxLayer + 1) * W + maxLayer * GAP_X, ...boxes.map((b) => b.x + b.w + PAD)),
    height: Math.max(PAD * 2 + maxRows * H + Math.max(0, maxRows - 1) * GAP_Y, ...boxes.map((b) => b.y + b.h + PAD)),
    boxes,
    links,
  };
}

function nodeStatusOf(nodeId, statusMap) {
  return (statusMap && statusMap[nodeId]) || 'idle';
}

/** 纯 SVG DAG：状态着色 + 可点击节点 */
function svgDAG(nodes, edges, statusMap, { selected = '', showStatus = false } = {}) {
  if (!nodes || !nodes.length) return emptyState('还没有节点', '点「添加节点」开始编排 DAG');
  const layout = layoutDAG(nodes, edges);
  const arrows = layout.links
    .map((l) => {
      const color = showStatus && statusMap ? STATUS_COLOR[nodeStatusOf(l.to, statusMap)] || STATUS_COLOR.idle : '#c9cdd4';
      return `<path data-edge-from="${esc(l.from)}" data-edge-to="${esc(l.to)}" d="${l.path}" fill="none" stroke="${color}" stroke-width="1.6" stroke-dasharray="${l.backward ? '4 3' : ''}" marker-end="url(#arrow-${l.backward ? 'back' : 'fwd'})" opacity="0.85"/>`;
    })
    .join('');

  const boxes = layout.boxes
    .map((b) => {
      const node = b.node || {};
      const status = showStatus ? nodeStatusOf(b.id, statusMap) : 'idle';
      const color = STATUS_COLOR[status] || STATUS_COLOR.idle;
      const isSel = selected === b.id;
      const cond = node.condition || 'all_success';
      const cfg = node.config || {};
      const hint = cfg.sql
        ? truncate(String(cfg.sql).replace(/\s+/g, ' '), 26)
        : cfg.command || cfg.url || cfg.table_ref || cfg.flow_id || cfg.code
          ? truncate(String(cfg.command || cfg.url || cfg.table_ref || cfg.flow_id || cfg.code).replace(/\s+/g, ' '), 26)
          : '未配置';
      return `<g class="wf-node" data-node="${esc(b.id)}" style="cursor:pointer">
        <rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="8"
          fill="#fff" stroke="${isSel ? '#0052d9' : color}" stroke-width="${isSel ? 2 : 1.4}"/>
        <rect x="${b.x}" y="${b.y}" width="4" height="${b.h}" rx="2" fill="${color}"/>
        <text x="${b.x + 14}" y="${b.y + 22}" font-size="12.5" font-weight="500" fill="#1f2329">${esc(truncate(node.name || b.id, 16))}</text>
        <text x="${b.x + 14}" y="${b.y + 39}" font-size="10.5" fill="#8b8f96">${esc(node.type || 'sql')} · ${esc(cond)}</text>
        <text x="${b.x + 14}" y="${b.y + 53}" font-size="10" fill="#b5b9c0">${esc(hint)}</text>
        ${showStatus
          ? `<circle cx="${b.x + b.w - 13}" cy="${b.y + 13}" r="5" fill="${color}"/>`
          : ''}
      </g>`;
    })
    .join('');

  return `<div class="wf-dag-wrap" style="overflow:auto;background:#fafbfc;border-radius:6px;padding:4px">
    <svg class="wf-dag" viewBox="0 0 ${layout.width} ${layout.height}" width="${layout.width}" height="${layout.height}"
      xmlns="http://www.w3.org/2000/svg" style="min-width:100%;display:block">
      <defs>
        <marker id="arrow-fwd" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#b5b9c0"/>
        </marker>
        <marker id="arrow-back" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="#d54941"/>
        </marker>
      </defs>
      ${arrows}
      ${boxes}
    </svg>
  </div>`;
}

function dagLegend() {
  return `<div class="row tiny dim" style="gap:14px;flex-wrap:wrap;margin-top:10px">
    ${Object.entries(STATUS_LABEL)
      .filter(([k]) => k !== 'idle')
      .map(([k, label]) => `<span class="row" style="gap:5px">
        <i style="width:9px;height:9px;border-radius:2px;background:${STATUS_COLOR[k]};display:inline-block"></i>${label}</span>`)
      .join('')}
    <span class="row" style="gap:5px"><i style="width:9px;height:9px;border-radius:2px;background:${STATUS_COLOR.idle};display:inline-block"></i>未运行</span>
  </div>`;
}

/* ------------------------------------------------------------------ 列表视图 */

export async function renderWorkflow(page, ctx) {
  page.innerHTML = loading();
  try {
    const [flowOut, metaOut] = await Promise.all([
      api.get('/workflows'),
      api.get('/workflow-meta').catch(() => null),
    ]);
    state.flows = flowOut.workflows || [];
    state.meta = metaOut || { node_types: [], conditions: [] };
  } catch (err) {
    page.innerHTML = `<div class="card"><div class="tag red">加载失败</div>
      <div class="tiny dim" style="margin-top:8px">${esc(err.message)}</div></div>`;
    return;
  }
  state.view = 'list';
  state.flowId = '';
  renderList(page, ctx);
}

function renderList(page, ctx) {
  const flows = state.flows;
  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => ctx.refresh() }, '刷新'),
      el('button', { class: 'btn primary', onclick: () => newWorkflow(page, ctx) }, '＋ 新建工作流'),
    ])
  );

  const totalNodes = flows.reduce((sum, f) => sum + (f.node_count || 0), 0);
  const scheduled = flows.filter((f) => f.enabled).length;
  const failed = flows.filter((f) => f.last_run && f.last_run.status === 'failed').length;

  page.innerHTML = `
    <div class="stat-row">
      ${statCard('工作流总数', flows.length, '个', '含草稿与已调度')}
      ${statCard('已启用调度', scheduled, '个', '周期 + 持续运行')}
      ${statCard('节点总数', totalNodes, '个', '9 种任务类型')}
      ${statCard('最近失败', failed, '个', failed ? '需要关注' : '运行健康')}
    </div>
    <div class="filter-bar mt-16">
      <div class="f-item"><span>名称</span><input id="wf-kw" placeholder="按名称 / 描述搜索" /></div>
      <div class="f-item"><span>调度状态</span><select id="wf-enabled">
        <option value="">全部</option><option value="1">已启用</option><option value="0">已停用</option>
      </select></div>
      <div class="f-item"><span>运行模式</span><select id="wf-mode">
        <option value="">全部</option><option value="cron">周期调度</option>
        <option value="continuous">持续运行</option><option value="manual">仅手动</option>
      </select></div>
      <span class="spacer"></span>
      <button class="btn" id="wf-reset">重置</button>
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">工作流列表</span>
        <span class="dim tiny">共 ${flows.length} 个 · 节点 ${totalNodes} 个</span>
        <span class="spacer"></span><button class="btn sm" id="wf-reload">刷新</button></div>
      <div id="wf-table"></div>
    </div>`;

  const draw = (rows) => {
    page.querySelector('#wf-table').innerHTML = dataTable({
      rows,
      columns: [
        {
          title: '工作流名称',
          render: (r) => `<div class="cell-main">${esc(r.name)}</div>
            <div class="cell-sub">${esc(r.description || '无描述')}</div>`,
        },
        {
          title: '调度',
          render: (r) => `${tag(RUN_MODE_LABEL[r.run_mode] || r.run_mode, r.run_mode === 'continuous' ? 'blue' : '')}
            <div class="cell-sub">${esc(r.schedule_text || '仅手动触发')}</div>`,
        },
        {
          title: '节点数', align: 'right', width: '80px',
          render: (r) => `${r.node_count}${r.edge_count ? `<div class="cell-sub">${r.edge_count} 条边</div>` : ''}`,
        },
        {
          title: '状态',
          render: (r) => statusTag(r.enabled ? 'active' : 'stopped'),
        },
        {
          title: '最近运行',
          render: (r) => (r.last_run
            ? `${wfStatusTag(r.last_run.status)}<div class="cell-sub">${fmt.ago(r.last_run.started_at)}</div>`
            : '<span class="dim">未运行</span>'),
        },
        {
          title: '操作', key: 'ops', width: '210px',
          render: (r) => `<a class="btn-link" data-detail="${esc(r.id)}">详情</a>
            <a class="btn-link" data-run="${esc(r.id)}">运行</a>
            <a class="btn-link" data-toggle="${esc(r.id)}">${r.enabled ? '停用' : '启用'}</a>
            <a class="btn-link danger" data-del="${esc(r.id)}">删除</a>`,
        },
      ],
      emptyText: '还没有工作流',
      emptyHint: '点右上角新建，或用模板快速生成',
    });

    page.querySelectorAll('[data-detail]').forEach((b) =>
      b.addEventListener('click', () => openDetail(page, ctx, b.dataset.detail))
    );
    page.querySelectorAll('[data-run]').forEach((b) =>
      b.addEventListener('click', async () => {
        const old = b.textContent;
        b.textContent = '运行中…';
        try {
          const out = await api.post(`/workflows/${b.dataset.run}/run`, { params: {} });
          const run = out.run || {};
          toast(`运行${run.status === 'success' ? '成功' : run.status === 'waiting' ? '等待审批' : '失败'}`,
            run.status === 'success' ? 'ok' : run.status === 'waiting' ? '' : 'err');
          showRunDrawer(run, state.meta);
          await reloadFlows(ctx);
        } catch (e) {
          toast(e.message, 'err');
        } finally {
          b.textContent = old;
        }
      })
    );
    page.querySelectorAll('[data-toggle]').forEach((b) =>
      b.addEventListener('click', async () => {
        const flow = state.flows.find((f) => f.id === b.dataset.toggle);
        if (!flow) return;
        try {
          await api.post(`/workflows/${flow.id}/${flow.enabled ? 'pause' : 'resume'}`, {});
          toast(flow.enabled ? '已停用调度' : '已启用调度', 'ok');
          await reloadFlows(ctx);
        } catch (e) {
          toast(e.message, 'err');
        }
      })
    );
    page.querySelectorAll('[data-del]').forEach((b) =>
      b.addEventListener('click', async () => {
        if (!(await confirmDialog('删除工作流', '会同时删除运行历史与节点记录，确认删除？', '删除'))) return;
        try {
          await api.del(`/workflows/${b.dataset.del}`);
          toast('已删除', 'ok');
          await reloadFlows(ctx);
        } catch (e) {
          toast(e.message, 'err');
        }
      })
    );
  };
  draw(flows);

  const filter = () => {
    const kw = page.querySelector('#wf-kw').value.trim().toLowerCase();
    const en = page.querySelector('#wf-enabled').value;
    const mode = page.querySelector('#wf-mode').value;
    draw(
      flows.filter(
        (f) =>
          (!kw || f.name.toLowerCase().includes(kw) || (f.description || '').toLowerCase().includes(kw)) &&
          (en === '' || String(f.enabled ? 1 : 0) === en) &&
          (!mode || f.run_mode === mode)
      )
    );
  };
  page.querySelector('#wf-kw').addEventListener('input', filter);
  page.querySelector('#wf-enabled').addEventListener('change', filter);
  page.querySelector('#wf-mode').addEventListener('change', filter);
  page.querySelector('#wf-reset').addEventListener('click', () => {
    page.querySelector('#wf-kw').value = '';
    page.querySelector('#wf-enabled').value = '';
    page.querySelector('#wf-mode').value = '';
    draw(flows);
  });
  page.querySelector('#wf-reload').addEventListener('click', () => ctx.refresh());
}

function statCard(label, value, unit, foot) {
  return `<div class="stat"><span class="label">${esc(label)}</span>
    <span class="value">${esc(value)}<small>${esc(unit)}</small></span>
    <span class="foot">${esc(foot)}</span></div>`;
}

async function reloadFlows(ctx) {
  try {
    const out = await api.get('/workflows');
    state.flows = out.workflows || [];
  } catch { /* ignore */ }
  ctx.refresh();
}

/* ------------------------------------------------------------------ 详情视图 */

async function openDetail(page, ctx, flowId) {
  page.innerHTML = loading('加载工作流…');
  let detail;
  try {
    const out = await api.get(`/workflows/${flowId}`);
    detail = out.workflow;
  } catch (err) {
    page.innerHTML = `<div class="card"><div class="tag red">加载失败</div>
      <div class="tiny dim" style="margin-top:8px">${esc(err.message)}</div></div>`;
    return;
  }
  state.view = 'detail';
  state.flowId = flowId;
  state.detail = detail;
  renderDetail(page, ctx, detail);
}

function renderDetail(page, ctx, flow) {
  const meta = state.meta || { node_types: [], conditions: [] };
  const runs = flow.runs || [];
  const lastRun = runs[0] || null;
  const statusMap = {};
  if (lastRun && Array.isArray(lastRun.log)) {
    lastRun.log.forEach((entry) => {
      if (entry && entry.node_id) statusMap[entry.node_id] = entry.status;
    });
  }

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => openDetail(page, ctx, flow.id) }, '刷新'),
      el('button', { class: 'btn', onclick: () => editWorkflow(page, ctx, flow) }, '编辑属性'),
      el('button', { class: 'btn', onclick: () => runWithParams(page, ctx, flow) }, '带参数运行'),
      el('button', {
        class: 'btn primary',
        onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          btn.textContent = '运行中…';
          try {
            const out = await api.post(`/workflows/${flow.id}/run`, { params: {} });
            toast(`运行${out.run?.status === 'success' ? '成功' : out.run?.status === 'waiting' ? '等待审批' : '失败'}`,
              out.run?.status === 'success' ? 'ok' : 'err');
            showRunDrawer(out.run, meta);
            openDetail(page, ctx, flow.id);
          } catch (err) {
            toast(err.message, 'err');
          } finally {
            btn.disabled = false;
            btn.textContent = '运行';
          }
        },
      }, '运行'),
    ])
  );

  page.innerHTML = `
    <div class="row" style="margin-bottom:14px;gap:10px;flex-wrap:wrap">
      <button class="btn sm" id="wf-back">← 返回列表</button>
      <span class="spacer"></span>
      ${statusTag(flow.enabled ? 'active' : 'stopped')}
      ${tag(RUN_MODE_LABEL[flow.run_mode] || flow.run_mode, 'blue')}
      ${tag(flow.schedule_text || '仅手动触发', 'outline')}
      ${flow.has_cycle ? tag('存在环，无法执行', 'red') : ''}
    </div>

    <div class="grid-2" style="grid-template-columns:minmax(0,1fr) 380px;align-items:start">
      <div class="col" style="gap:16px">
        ${card('DAG 视图', `
          <div class="row" style="gap:8px;margin-bottom:12px;flex-wrap:wrap">
            <button class="btn sm" id="wf-add-node">＋ 添加节点</button>
            <button class="btn sm" id="wf-add-edge">＋ 添加连线</button>
            <button class="btn sm" id="wf-validate">校验</button>
            <span class="spacer"></span>
            <span class="dim tiny">${flow.nodes.length} 节点 / ${flow.edges.length} 连线${lastRun ? ' · 显示最近一次运行状态' : ''}</span>
          </div>
          ${svgDAG(flow.nodes, flow.edges, statusMap, { showStatus: !!lastRun })}
          ${dagLegend()}
        `, { flush: false })}

        ${card('节点明细', dataTable({
          rows: flow.nodes,
          columns: [
            {
              title: '节点',
              render: (n) => `<div class="cell-main">${esc(n.name || n.id)}</div>
                <div class="cell-sub mono">${esc(n.id)}</div>`,
            },
            { title: '类型', render: (n) => tag(nodeTypeLabel(n.type, meta), 'blue') },
            {
              title: '运行条件',
              render: (n) => `<span class="tiny">${esc(conditionLabel(n.condition, meta))}</span>`,
            },
            {
              title: '配置摘要',
              render: (n) => `<span class="tiny dim">${esc(configSummary(n))}</span>`,
            },
            {
              title: '状态',
              render: (n) => wfStatusTag(statusMap[n.id] || 'idle'),
            },
            {
              title: '操作', key: 'ops', width: '110px',
              render: (n) => `<a class="btn-link" data-edit-node="${esc(n.id)}">编辑</a>
                <a class="btn-link danger" data-del-node="${esc(n.id)}">删除</a>`,
            },
          ],
          emptyText: '还没有节点',
        }), { flush: true })}

        ${card('连线', dataTable({
          rows: flow.edges.map((e, i) => ({ id: `e${i}`, from: e[0], to: e[1] })),
          columns: [
            { title: '上游', render: (e) => `<span class="mono tiny">${esc(e.from)}</span>` },
            { title: '', width: '40px', render: () => '<span class="dim">→</span>' },
            { title: '下游', render: (e) => `<span class="mono tiny">${esc(e.to)}</span>` },
            {
              title: '操作', key: 'ops', width: '80px',
              render: (e) => `<a class="btn-link danger" data-del-edge="${esc(e.id)}">删除</a>`,
            },
          ],
          emptyText: '没有连线，节点将按顺序串行执行',
        }), { flush: true })}
      </div>

      <div class="col" style="gap:16px">
        ${card('工作流属性', descList([
          ['名称', esc(flow.name)],
          ['描述', esc(flow.description || '-')],
          ['运行模式', esc(RUN_MODE_LABEL[flow.run_mode] || flow.run_mode)],
          ['cron', flow.cron ? `<span class="mono tiny">${esc(flow.cron)}</span>` : '<span class="dim">-</span>'],
          ['时区', esc(flow.timezone || '本地时区')],
          ['调度说明', esc(flow.schedule_text || '-')],
          ['最小间隔', flow.run_mode === 'continuous' ? `${flow.interval_seconds}s` : '-'],
          ['失败重试', `${flow.retries || 0} 次`],
          ['节点 / 连线', `${flow.nodes.length} / ${flow.edges.length}`],
          ['创建时间', fmt.dt(flow.created_at)],
        ]))}

        ${card('工作流级参数', Object.keys(flow.params || {}).length
          ? descList(Object.entries(flow.params || {}).map(([k, v]) => [k, `<span class="mono tiny">${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</span>`]))
          : '<div class="tiny dim">未定义参数。可在节点配置里用 <span class="mono">{{workflow.xxx}}</span> 引用。</div>',
          { sub: '模板：{{workflow.x}} / {{nodes.节点ID.output}}', flush: false })}

        ${card('最近运行', runs.length
          ? dataTable({
              rows: runs.slice(0, 8),
              columns: [
                { title: '状态', render: (r) => wfStatusTag(r.status) },
                {
                  title: '开始',
                  render: (r) => `<span class="tiny dim">${fmt.dt(r.started_at)}</span>
                    <div class="cell-sub">${esc(triggerLabel(r.trigger))}</div>`,
                },
                { title: '耗时', render: (r) => `<span class="tiny">${durationText(r.started_at, r.finished_at)}</span>` },
                {
                  title: '操作', key: 'ops', width: '70px',
                  render: (r) => `<a class="btn-link" data-open-run="${esc(r.id)}">明细</a>`,
                },
              ],
              emptyText: '还没有运行记录',
            })
          : emptyState('还没有运行记录', '点右上角「运行」触发一次'), { flush: true })}
      </div>
    </div>`;

  page.querySelector('#wf-back').addEventListener('click', () => {
    state.view = 'list';
    renderList(page, ctx);
  });
  page.querySelectorAll('[data-node]').forEach((g) =>
    g.addEventListener('click', () => {
      if (g.dataset.dragged) { delete g.dataset.dragged; return; }
      const node = flow.nodes.find((n) => n.id === g.dataset.node);
      if (node) openNodeEditor(page, ctx, flow, node);
    })
  );
  bindNodeDragging(page, ctx, flow);
  page.querySelectorAll('[data-edit-node]').forEach((b) =>
    b.addEventListener('click', () => {
      const node = flow.nodes.find((n) => n.id === b.dataset.editNode);
      if (node) openNodeEditor(page, ctx, flow, node);
    })
  );
  page.querySelectorAll('[data-del-node]').forEach((b) =>
    b.addEventListener('click', async () => {
      const nodeId = b.dataset.delNode;
      if (!(await confirmDialog('删除节点', `删除节点「${nodeId}」及其相关连线？`, '删除'))) return;
      const nodes = flow.nodes.filter((n) => n.id !== nodeId);
      const edges = flow.edges.filter((e) => e[0] !== nodeId && e[1] !== nodeId);
      await saveGraph(page, ctx, flow, nodes, edges);
    })
  );
  page.querySelectorAll('[data-del-edge]').forEach((b) =>
    b.addEventListener('click', async () => {
      const idx = Number(String(b.dataset.delEdge).slice(1));
      const edges = flow.edges.filter((_, i) => i !== idx);
      await saveGraph(page, ctx, flow, flow.nodes, edges);
    })
  );
  page.querySelectorAll('[data-open-run]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        const out = await api.get(`/workflow-runs/${b.dataset.openRun}`);
        showRunDrawer(out.run, meta);
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );

  page.querySelector('#wf-add-node').addEventListener('click', () => openNodeEditor(page, ctx, flow, null));
  page.querySelector('#wf-add-edge').addEventListener('click', () => openEdgeEditor(page, ctx, flow));
  page.querySelector('#wf-validate').addEventListener('click', async () => {
    try {
      const out = await api.post(`/workflows/${flow.id}/validate`, {});
      if (out.ok && !out.issues.length) {
        toast('校验通过', 'ok');
        return;
      }
      modal({
        title: out.ok ? '校验通过（含提示）' : '校验未通过',
        wide: true,
        body: out.issues.length
          ? `<div class="col" style="gap:8px">${out.issues
              .map((i) => `<div class="row" style="gap:8px;align-items:flex-start">
                ${tag(i.level === 'error' ? '错误' : '提示', i.level === 'error' ? 'red' : 'orange')}
                <span class="tiny">${esc(i.message)}</span></div>`)
              .join('')}</div>`
          : '<div class="tag green">没有发现问题</div>',
        actions: [{ label: '关闭', primary: true }],
      });
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

function triggerLabel(trigger) {
  return { manual: '手动触发', schedule: '定时触发', subflow: '子流程调用', agent: 'Agent 触发' }[trigger] || trigger || '-';
}

function nodeTypeLabel(type, meta) {
  const found = (meta?.node_types || []).find((t) => t.key === type);
  return found ? found.label : type;
}

function conditionLabel(cond, meta) {
  const found = (meta?.conditions || []).find((c) => c.key === cond);
  return found ? found.label : cond || 'all_success';
}

function configSummary(node) {
  const cfg = node.config || {};
  const parts = [];
  if (cfg.sql) parts.push(`SQL: ${truncate(String(cfg.sql).replace(/\s+/g, ' '), 60)}`);
  if (cfg.command) parts.push(`命令: ${truncate(cfg.command, 50)}`);
  if (cfg.code) parts.push(`代码: ${truncate(String(cfg.code).replace(/\s+/g, ' '), 50)}`);
  if (cfg.url) parts.push(`${(cfg.method || 'GET').toUpperCase()} ${cfg.url}`);
  if (cfg.table_ref) parts.push(`表: ${cfg.table_ref}`);
  if (cfg.flow_id) parts.push(`子流程: ${cfg.flow_id}`);
  if (cfg.channels) parts.push(`渠道: ${Array.isArray(cfg.channels) ? cfg.channels.join(',') : cfg.channels}`);
  if (cfg.cells) parts.push(`cells: ${Array.isArray(cfg.cells) ? cfg.cells.length : 1} 个`);
  const paramCount = Object.keys(node.params || {}).length;
  if (paramCount) parts.push(`参数 ${paramCount} 个`);
  return parts.join(' · ') || '未配置';
}

/* ------------------------------------------------------------------ 保存 DAG */

async function saveGraph(page, ctx, flow, nodes, edges) {
  try {
    const out = await api.patch(`/workflows/${flow.id}`, { nodes, edges });
    toast('已保存', 'ok');
    renderDetail(page, ctx, out.workflow || { ...flow, nodes, edges });
    return true;
  } catch (err) {
    toast(err.message, 'err');
    return false;
  }
}


// Persist node coordinates through the same DAG update API used by the editor.
function bindNodeDragging(page, ctx, flow) {
  const svg = page.querySelector('.wf-dag');
  if (!svg) return;
  svg.style.touchAction = 'none';
  const point = (event) => { const p = new DOMPoint(event.clientX, event.clientY); return p.matrixTransform(svg.getScreenCTM().inverse()); };
  page.querySelectorAll('.wf-node').forEach((group) => {
    group.style.cursor = 'grab';
    let drag = null;
    group.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const rect = group.querySelector('rect');
      drag = { start: point(event), x: Number(rect.getAttribute('x')), y: Number(rect.getAttribute('y')), dx: 0, dy: 0 };
      group.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    group.addEventListener('pointermove', (event) => {
      if (!drag) return;
      const p = point(event);
      drag.dx = Math.max(24 - drag.x, Math.min(5000 - drag.x, p.x - drag.start.x));
      drag.dy = Math.max(24 - drag.y, Math.min(5000 - drag.y, p.y - drag.start.y));
      if (Math.abs(drag.dx) + Math.abs(drag.dy) < 4) return;
      group.dataset.dragged = 'true';
      group.setAttribute('transform', `translate(${drag.dx},${drag.dy})`);
      group.style.cursor = 'grabbing';
      svg.querySelectorAll('[data-edge-from]').forEach((edge) => {
        const coords = (key, right) => {
          const g = Array.from(svg.querySelectorAll('.wf-node')).find((g) => g.dataset.node === key);
          const r = g.querySelector('rect');
          return { x: Number(r.getAttribute('x')) + (right ? Number(r.getAttribute('width')) : 0) + (g === group ? drag.dx : 0), y: Number(r.getAttribute('y')) + Number(r.getAttribute('height')) / 2 + (g === group ? drag.dy : 0) };
        };
        const a = coords(edge.dataset.edgeFrom, true), b = coords(edge.dataset.edgeTo, false), mid = (a.x + b.x) / 2;
        edge.setAttribute('d', `M ${a.x} ${a.y} C ${mid} ${a.y} ${mid} ${b.y} ${b.x} ${b.y}`);
      });
    });
    group.addEventListener('pointerup', async (event) => {
      if (!drag) return;
      const moved = Math.abs(drag.dx) + Math.abs(drag.dy) >= 4;
      const position = { x: Math.round(drag.x + drag.dx), y: Math.round(drag.y + drag.dy) };
      drag = null;
      group.releasePointerCapture(event.pointerId);
      group.style.cursor = 'grab';
      if (!moved) return;
      const nodes = flow.nodes.map((node) => node.id === group.dataset.node ? { ...node, position } : node);
      if (!(await saveGraph(page, ctx, flow, nodes, flow.edges))) renderDetail(page, ctx, flow);
    });
    group.addEventListener('pointercancel', () => { drag = null; renderDetail(page, ctx, flow); });
  });
}

/* ------------------------------------------------------------------ 节点编辑 */

function openNodeEditor(page, ctx, flow, node) {
  const meta = state.meta || { node_types: [], conditions: [] };
  const isNew = !node;
  const draft = node
    ? JSON.parse(JSON.stringify(node))
    : {
        id: `n${flow.nodes.length + 1}_${Math.random().toString(36).slice(2, 6)}`,
        type: 'sql',
        name: `节点${flow.nodes.length + 1}`,
        config: {},
        params: {},
        condition: 'all_success',
        condition_config: {},
      };

  const typeOptions = (meta.node_types || []).map((t) => ({ value: t.key, label: `${t.label} · ${t.desc}` }));
  const condOptions = (meta.conditions || []).map((c) => ({ value: c.key, label: `${c.label} · ${c.desc}` }));

  const body = el('div');
  body.innerHTML = `
    <div class="grid-2" style="gap:12px">
      <div class="field"><label><span class="req">*</span>节点名称</label>
        <input type="text" id="nd-name" value="${esc(draft.name)}" placeholder="例如 汇总订单" /></div>
      <div class="field"><label>节点 ID</label>
        <input type="text" id="nd-id" value="${esc(draft.id)}" ${isNew ? '' : 'disabled'} /></div>
    </div>
    <div class="field"><label>任务类型（9 种）</label>
      <select id="nd-type">${typeOptions
        .map((o) => `<option value="${esc(o.value)}"${o.value === draft.type ? ' selected' : ''}>${esc(o.label)}</option>`)
        .join('')}</select></div>
    <div class="field"><label>运行条件（12 种）</label>
      <select id="nd-cond">${condOptions
        .map((o) => `<option value="${esc(o.value)}"${o.value === draft.condition ? ' selected' : ''}>${esc(o.label)}</option>`)
        .join('')}</select>
      <div class="hint" id="nd-cond-hint"></div></div>
    <div id="nd-cond-cfg"></div>
    <div id="nd-fields"></div>
    <div class="field"><label>节点级参数（JSON 对象，可用 {{workflow.x}} / {{nodes.x.output}}）</label>
      <textarea id="nd-params" rows="3" class="mono" style="font-family:var(--mono);font-size:12px">${esc(jsonPretty(draft.params))}</textarea>
      <div class="hint">节点参数会覆盖工作流级同名参数，并注入到 SQL / 代码 / 命令的模板里。</div></div>
    <div class="field"><label>高级配置（JSON，直接覆盖上面的字段）</label>
      <textarea id="nd-config" rows="4" class="mono" style="font-family:var(--mono);font-size:12px">${esc(jsonPretty(draft.config))}</textarea></div>`;

  const renderTypeFields = () => {
    const kind = body.querySelector('#nd-type').value;
    const spec = (meta.node_types || []).find((t) => t.key === kind) || { fields: [] };
    const host = body.querySelector('#nd-fields');
    host.innerHTML = `<div class="divider"></div><div class="tiny dim" style="margin-bottom:8px">
      任务类型配置 · ${esc(spec.label || kind)}</div>` +
      (spec.fields || [])
        .map((f) => {
          const value = draft.config?.[f.key] ?? '';
          if (f.type === 'textarea') {
            return `<div class="field"><label>${esc(f.label)}</label>
              <textarea data-cfg="${esc(f.key)}" rows="3" class="mono" style="font-family:var(--mono);font-size:12px">${esc(typeof value === 'object' ? JSON.stringify(value, null, 2) : value)}</textarea></div>`;
          }
          if (f.type === 'select') {
            return `<div class="field"><label>${esc(f.label)}</label>
              <select data-cfg="${esc(f.key)}">${(f.options || [])
                .map((o) => `<option value="${esc(o)}"${String(o) === String(value) ? ' selected' : ''}>${esc(o)}</option>`)
                .join('')}</select></div>`;
          }
          if (f.type === 'checkbox') {
            return `<div class="field"><label class="checkbox">
              <input type="checkbox" data-cfg="${esc(f.key)}"${value ? ' checked' : ''}/>${esc(f.label)}</label></div>`;
          }
          return `<div class="field"><label>${esc(f.label)}</label>
            <input type="${f.type === 'number' ? 'number' : 'text'}" data-cfg="${esc(f.key)}"
              value="${esc(value ?? '')}" placeholder="${esc(f.placeholder || '')}" /></div>`;
        })
        .join('');
  };

  const renderCondCfg = () => {
    const cond = body.querySelector('#nd-cond').value;
    const spec = (meta.conditions || []).find((c) => c.key === cond) || {};
    body.querySelector('#nd-cond-hint').textContent = spec.desc || '';
    const host = body.querySelector('#nd-cond-cfg');
    const params = spec.params || [];
    if (!params.length) {
      host.innerHTML = '';
      return;
    }
    const cfg = draft.condition_config || {};
    host.innerHTML = `<div class="tiny dim" style="margin-bottom:8px">条件参数 · ${esc(spec.label || cond)}</div>` +
      params
        .map((p) => `<div class="field"><label>${esc(p.label)}</label>
          <input type="text" data-ccfg="${esc(p.key)}" value="${esc(cfg[p.key] ?? '')}"
            placeholder="${esc(p.placeholder || '')}" /></div>`)
        .join('');
  };

  renderTypeFields();
  renderCondCfg();
  body.querySelector('#nd-type').addEventListener('change', renderTypeFields);
  body.querySelector('#nd-cond').addEventListener('change', renderCondCfg);

  modal({
    title: isNew ? '添加节点' : `编辑节点 · ${node.name || node.id}`,
    wide: true,
    body,
    actions: [
      { label: '取消', value: null },
      {
        label: '保存',
        primary: true,
        onClick: async () => {
          const name = body.querySelector('#nd-name').value.trim();
          const nodeId = (isNew ? body.querySelector('#nd-id').value.trim() : draft.id) || '';
          if (!name) {
            toast('请填写节点名称', 'err');
            return false;
          }
          if (!nodeId) {
            toast('请填写节点 ID', 'err');
            return false;
          }
          const exists = flow.nodes.some((n) => n.id === nodeId && (!node || n.id !== node.id));
          if (exists) {
            toast(`节点 ID「${nodeId}」已存在`, 'err');
            return false;
          }
          let params;
          let advanced;
          try {
            params = parseJSON(body.querySelector('#nd-params').value, '节点参数');
            advanced = parseJSON(body.querySelector('#nd-config').value, '高级配置');
          } catch (e) {
            toast(e.message, 'err');
            return false;
          }
          const config = { ...advanced };
          body.querySelectorAll('[data-cfg]').forEach((input) => {
            const key = input.dataset.cfg;
            let value;
            if (input.type === 'checkbox') value = input.checked;
            else if (input.type === 'number') value = input.value === '' ? '' : Number(input.value);
            else value = input.value;
            if (value === '' || value === null) delete config[key];
            else config[key] = value;
          });
          const conditionConfig = {};
          body.querySelectorAll('[data-ccfg]').forEach((input) => {
            if (input.value !== '') conditionConfig[input.dataset.ccfg] = input.value;
          });
          const next = {
            id: nodeId,
            type: body.querySelector('#nd-type').value,
            name,
            config,
            params,
            condition: body.querySelector('#nd-cond').value,
            condition_config: conditionConfig,
          };
          const nodes = node ? flow.nodes.map((n) => (n.id === node.id ? next : n)) : [...flow.nodes, next];
          let edges = flow.edges;
          if (isNew && !edges.some((e) => e[1] === nodeId)) {
            // 新节点默认接到最后一个无后继节点后面，方便快速串起来
            const targets = new Set(edges.map((e) => e[1]));
            const tail = flow.nodes.filter((n) => !targets.has(n.id)).pop();
            if (tail) edges = [...edges, [tail.id, nodeId]];
          }
          await saveGraph(page, ctx, flow, nodes, edges);
          return true;
        },
      },
    ],
  });
}

function openEdgeEditor(page, ctx, flow) {
  if (flow.nodes.length < 2) {
    toast('至少需要两个节点才能连线', 'err');
    return;
  }
  const options = flow.nodes.map((n) => ({ value: n.id, label: `${n.name || n.id}（${n.type}）` }));
  formModal({
    title: '添加连线',
    fields: [
      { label: '上游节点', name: 'from', type: 'select', options },
      { label: '下游节点', name: 'to', type: 'select', options: options.slice(1) },
    ],
    onSubmit: async (d) => {
      if (d.from === d.to) throw new Error('上下游不能是同一个节点');
      if (flow.edges.some((e) => e[0] === d.from && e[1] === d.to)) throw new Error('该连线已存在');
      await saveGraph(page, ctx, flow, flow.nodes, [...flow.edges, [d.from, d.to]]);
    },
  });
}

/* ------------------------------------------------------------------ 工作流编辑 */

async function newWorkflow(page, ctx) {
  const meta = state.meta || { node_types: [], conditions: [] };
  const data = await formModal({
    title: '新建工作流',
    wide: true,
    fields: [
      { label: '工作流名称', name: 'name', required: true, placeholder: '例如 每日经营数据刷新' },
      { label: '描述', name: 'description', placeholder: '这条工作流做什么' },
      {
        label: '运行模式', name: 'run_mode', type: 'select',
        options: [
          { value: 'cron', label: '周期调度（cron）' },
          { value: 'continuous', label: '持续运行（最小间隔 60s）' },
          { value: 'manual', label: '仅手动触发' },
        ],
      },
      { label: 'cron 表达式', name: 'cron', placeholder: '0 7 * * *', hint: '5 段：分 时 日 月 周；留空则仅手动' },
      { label: '时区', name: 'timezone', placeholder: 'Asia/Shanghai', hint: '留空使用服务器本地时区' },
      { label: '持续运行间隔（秒）', name: 'interval_seconds', type: 'number', value: 60, hint: '最小 60 秒' },
      { label: '失败重试次数', name: 'retries', type: 'number', value: 0 },
      { label: '工作流级参数（JSON）', name: 'params', type: 'textarea', rows: 3, value: '{\n  "env": "prod"\n}' },
      {
        label: '初始节点（每行：名称 | 类型）', name: 'nodes', type: 'textarea', rows: 4,
        value: '汇总订单 | sql\n质量校验 | quality\n发送通知 | notify',
        hint: `可用类型：${(meta.node_types || []).map((t) => t.key).join(' / ')}`,
      },
    ],
    onSubmit: async (d) => {
      const nodes = String(d.nodes || '')
        .split('\n')
        .map((line) => line.split('|').map((s) => s.trim()))
        .filter((p) => p.length >= 1 && p[0])
        .map((p, i) => ({
          id: `n${i + 1}`,
          name: p[0],
          type: (meta.node_types || []).some((t) => t.key === p[1]) ? p[1] : 'sql',
          config: {},
          params: {},
          condition: 'all_success',
        }));
      if (!nodes.length) throw new Error('至少需要一个节点');
      const params = parseJSON(d.params, '工作流级参数');
      const out = await api.post('/workflows', {
        name: d.name,
        description: d.description,
        cron: d.cron,
        enabled: !!d.cron || d.run_mode === 'continuous',
        nodes,
        edges: nodes.slice(1).map((n, i) => [nodes[i].id, n.id]),
        params,
        retries: Number(d.retries || 0),
        run_mode: d.run_mode,
        interval_seconds: Math.max(60, Number(d.interval_seconds || 60)),
        timezone: d.timezone,
      });
      toast('工作流已创建', 'ok');
      return out;
    },
  });
  if (data) {
    await reloadFlows(ctx);
    if (data.workflow?.id) openDetail(page, ctx, data.workflow.id);
  }
}

async function editWorkflow(page, ctx, flow) {
  const data = await formModal({
    title: '编辑工作流属性',
    wide: true,
    fields: [
      { label: '工作流名称', name: 'name', required: true, value: flow.name },
      { label: '描述', name: 'description', value: flow.description || '' },
      {
        label: '运行模式', name: 'run_mode', type: 'select', value: flow.run_mode || 'cron',
        options: [
          { value: 'cron', label: '周期调度（cron）' },
          { value: 'continuous', label: '持续运行（最小间隔 60s）' },
          { value: 'manual', label: '仅手动触发' },
        ],
      },
      { label: 'cron 表达式', name: 'cron', value: flow.cron || '', placeholder: '0 7 * * *' },
      { label: '时区', name: 'timezone', value: flow.timezone || '', placeholder: 'Asia/Shanghai' },
      { label: '持续运行间隔（秒）', name: 'interval_seconds', type: 'number', value: flow.interval_seconds || 60 },
      { label: '失败重试次数', name: 'retries', type: 'number', value: flow.retries || 0 },
      { label: '启用调度', name: 'enabled', type: 'checkbox', value: !!flow.enabled },
      { label: '工作流级参数（JSON）', name: 'params', type: 'textarea', rows: 4, value: jsonPretty(flow.params) },
    ],
    onSubmit: async (d) => {
      const params = parseJSON(d.params, '工作流级参数');
      await api.patch(`/workflows/${flow.id}`, {
        name: d.name,
        description: d.description,
        cron: d.cron,
        enabled: !!d.enabled,
        run_mode: d.run_mode,
        interval_seconds: Math.max(60, Number(d.interval_seconds || 60)),
        retries: Number(d.retries || 0),
        timezone: d.timezone,
        params,
      });
      toast('已保存', 'ok');
    },
  });
  if (data) openDetail(page, ctx, flow.id);
}

async function runWithParams(page, ctx, flow) {
  const keys = Object.keys(flow.params || {});
  const data = await formModal({
    title: '带参数运行',
    wide: true,
    intro: `<div class="tiny dim" style="margin-bottom:12px">
      留空表示使用工作流级默认值。参数可在节点配置里用 <span class="mono">{{workflow.参数名}}</span> 引用。</div>`,
    fields: [
      {
        label: '运行参数（JSON）', name: 'params', type: 'textarea', rows: 6,
        value: jsonPretty(flow.params || {}),
        hint: keys.length ? `已定义参数：${keys.join(', ')}` : '工作流还没有定义参数',
      },
    ],
    onSubmit: async (d) => {
      const params = parseJSON(d.params, '运行参数');
      const out = await api.post(`/workflows/${flow.id}/run`, { params });
      const run = out.run || {};
      toast(`运行${run.status === 'success' ? '成功' : run.status === 'waiting' ? '等待审批' : '失败'}`,
        run.status === 'success' ? 'ok' : 'err');
      showRunDrawer(run, state.meta);
    },
  });
  if (data) openDetail(page, ctx, flow.id);
}

/* ------------------------------------------------------------------ 运行抽屉 */

function showRunDrawer(run, meta) {
  if (!run) return;
  const nodes = Array.isArray(run.nodes) ? run.nodes : Array.isArray(run.log) ? run.log : [];
  const stats = {
    success: nodes.filter((n) => n.status === 'success').length,
    failed: nodes.filter((n) => n.status === 'failed').length,
    skipped: nodes.filter((n) => n.status === 'skipped').length,
    waiting: nodes.filter((n) => n.status === 'waiting').length,
  };
  const total = durationText(run.started_at, run.finished_at);

  const body = `<div class="col" style="gap:16px">
    <div class="info-strip">
      ${wfStatusTag(run.status)}
      <span class="tiny dim">${esc(triggerLabel(run.trigger))}</span>
      <span class="tiny dim">${fmt.dt(run.started_at)} → ${run.finished_at ? fmt.dt(run.finished_at) : '进行中'}</span>
      <span class="spacer"></span>
      <span class="tiny dim">总耗时 ${esc(total)}</span>
    </div>
    <div class="row" style="gap:8px;flex-wrap:wrap">
      ${tag(`成功 ${stats.success}`, 'green')}
      ${tag(`失败 ${stats.failed}`, 'red')}
      ${tag(`跳过 ${stats.skipped}`, '')}
      ${stats.waiting ? tag(`等待 ${stats.waiting}`, 'orange') : ''}
      <span class="mono tiny dim">${esc(run.id || '')}</span>
    </div>
    ${card('节点执行明细', nodes.length
      ? `<div class="col" style="gap:10px">${nodes
          .map((n) => {
            const color = STATUS_COLOR[n.status] || STATUS_COLOR.idle;
            const dur = n.duration_ms != null
              ? `${n.duration_ms}ms`
              : durationText(n.started_at, n.finished_at);
            return `<div style="border:1px solid var(--border);border-left:3px solid ${color};border-radius:6px;padding:10px 12px">
              <div class="row" style="gap:8px;flex-wrap:wrap">
                ${wfStatusTag(n.status)}
                ${tag(nodeTypeLabel(n.type, meta), 'blue')}
                <strong style="font-size:13px">${esc(n.name || n.node_id || '-')}</strong>
                <span class="mono tiny dim">${esc(n.node_id || '')}</span>
                <span class="spacer"></span>
                <span class="tiny dim">${esc(dur)}</span>
              </div>
              <div class="tiny dim" style="margin-top:6px">
                条件：${esc(conditionLabel(n.condition, meta))}${n.attempts > 1 ? ` · 尝试 ${n.attempts} 次` : ''}
              </div>
              ${n.output ? `<pre class="code" style="margin-top:8px;max-height:180px;overflow:auto;white-space:pre-wrap">${esc(typeof n.output === 'string' ? n.output : JSON.stringify(n.output, null, 2))}</pre>` : ''}
              ${n.error ? `<div class="tag red" style="margin-top:8px;display:inline-block">${esc(n.error)}</div>` : ''}
              ${n.sql ? `<div class="tiny dim" style="margin-top:8px">SQL</div>
                <pre class="code" style="max-height:120px;overflow:auto;white-space:pre-wrap">${esc(n.sql)}</pre>` : ''}
              ${n.preview && n.preview.columns
                ? `<div class="tiny dim" style="margin-top:8px">输出预览</div>
                   ${previewTable(n.preview)}`
                : ''}
            </div>`;
          })
          .join('')}</div>`
      : emptyState('没有节点记录'), { flush: false })}
  </div>`;

  drawer({ title: `运行明细 · ${STATUS_LABEL[run.status] || run.status}`, body, wide: true });
}

function previewTable(preview) {
  const cols = preview.columns || [];
  const rows = preview.rows || [];
  if (!cols.length) return '<div class="tiny dim">无列信息</div>';
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join('');
  const body = rows
    .slice(0, 8)
    .map((r) => `<tr>${cols.map((_, i) => `<td>${esc(r[i] === null || r[i] === undefined ? '-' : String(r[i]).slice(0, 60))}</td>`).join('')}</tr>`)
    .join('');
  return `<div class="table-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export default renderWorkflow;
