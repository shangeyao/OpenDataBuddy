// Studio 开发工作台：真·Notebook（多语法 cell + 13 个魔法命令 + 参数化 + 跨 cell 变量）
import { api, fmt } from './api.js';
import {
  card, tableHTML, statusTag, tag, loading, emptyState, modal,
  confirmDialog, formModal, toast, esc, el, md,
} from './ui.js';
import { chartHTML } from './charts.js';
import { icon } from './icons.js';

/* ------------------------------------------------------------------ 常量 */

const CELL_META = {
  sql: { label: 'SQL', tone: 'blue', hint: '直接写 SQL，支持 {{param}} 参数化' },
  python: { label: 'Python', tone: 'green', hint: '变量跨 cell 共享，可 import 同目录 .py' },
  markdown: { label: 'Markdown', tone: '', hint: '写文档与结论，%md 也可' },
};

const DEFAULT_SOURCES = {
  sql: 'SELECT\n  c.channel_name,\n  round(sum(o.amount), 2) AS gmv,\n  count(DISTINCT o.order_id) AS orders\nFROM demo.retail.fact_orders o\nJOIN demo.retail.dim_channel c ON c.channel_id = o.channel_id\nGROUP BY 1\nORDER BY gmv DESC',
  python: '# 变量在本 notebook 内跨 cell 共享\nrows = query("SELECT count(*) AS c FROM demo.retail.fact_orders")\ntotal = rows["rows"][0][0]\nprint("订单总数", total)\ntotal',
  markdown: '## 分析结论\n\n- 这里写 Markdown 文档\n- `%tables` 查看可用表',
};

/* -------------------------------------------------------------- 小工具 */

function cellTypeOf(cell) {
  const src = (cell.source || '').trim();
  const m = /^%([a-zA-Z_]+)/.exec(src);
  if (m) {
    const magic = m[1].toLowerCase();
    if (magic === 'md') return 'markdown';
    if (magic === 'python') return 'python';
  }
  return cell.type || 'sql';
}

function typeTag(cell) {
  const meta = CELL_META[cellTypeOf(cell)] || CELL_META.sql;
  return tag(meta.label, meta.tone);
}

function statusOf(cell) {
  if (cell.status === 'error' || cell.error) return statusTag('error');
  if (cell.status === 'success') return statusTag('success');
  return '<span class="tag outline">未运行</span>';
}

/** 输出区：表格 / 图表 / 文本 / 错误四种形态 */
function renderOutput(cell) {
  const parts = [];
  if (cell.error) {
    const tb = cell.traceback || cell.error;
    const where = cell.line_no
      ? `<div class="row" style="gap:6px;margin-bottom:6px">
           <span class="tag red">第 ${cell.line_no} 行</span>
           ${cell.source_line ? `<code class="inline">${esc(cell.source_line)}</code>` : ''}
         </div>`
      : '';
    parts.push(
      `<div style="border:1px solid #f0c4c0;background:var(--error-bg);border-radius:4px;padding:10px 12px">
        ${where}
        <div style="font-size:13px;color:var(--error);font-weight:500;margin-bottom:6px">${esc(cell.error)}</div>
        <pre class="code dark" style="margin:0;max-height:260px;overflow:auto;font-size:11.5px">${esc(tb)}</pre>
      </div>`
    );
  }
  const outText = (cell.output || '').trim();
  if (outText) {
    const isMd = cellTypeOf(cell) === 'markdown' || (cell.magic === '%md') || (cell.magic === '%help');
    parts.push(
      isMd
        ? `<div class="prose" style="padding:2px 0">${md(outText)}</div>`
        : `<pre class="code" style="margin:0;max-height:220px;overflow:auto;font-size:12px;white-space:pre-wrap">${esc(outText)}</pre>`
    );
  }
  if (cell.columns && cell.columns.length && cell.rows && cell.rows.length) {
    const chart = cell.chart && cell.chart.type !== 'table'
      ? chartHTML(cell.chart, cell.columns, cell.rows, { height: 200 })
      : '';
    if (chart) parts.push(`<div style="margin-top:8px">${chart}</div>`);
    parts.push(
      `<div style="margin-top:8px">${tableHTML(cell.columns, cell.rows, { max: 30 })}</div>`
    );
  }
  if (!parts.length) return '';
  return `<div style="padding:10px 14px;border-top:1px solid var(--border);background:var(--sub-2)">${parts.join('')}</div>`;
}

function cellHTML(cell, idx, total) {
  const ctype = cellTypeOf(cell);
  const meta = CELL_META[ctype] || CELL_META.sql;
  const elapsed = cell.elapsed_ms ? `${cell.elapsed_ms} ms` : '';
  const rowsInfo = cell.columns && cell.columns.length ? `${cell.rows?.length ?? 0} 行` : '';
  return `<div class="nb-cell" data-cell="${esc(cell.id)}" style="border:1px solid var(--border);border-radius:4px;background:#fff;overflow:hidden">
    <div class="row" style="gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);background:var(--sub)">
      <span class="tag outline" style="font-family:var(--mono);font-size:11px">[${idx + 1}]</span>
      ${typeTag(cell)}
      ${statusOf(cell)}
      <span class="tiny dim">${esc([elapsed, rowsInfo].filter(Boolean).join(' · '))}</span>
      <span class="spacer"></span>
      <button class="btn sm primary" data-act="run">▶ 运行</button>
      <button class="btn sm ghost" data-act="up" title="上移" ${idx === 0 ? 'disabled' : ''}>↑</button>
      <button class="btn sm ghost" data-act="down" title="下移" ${idx === total - 1 ? 'disabled' : ''}>↓</button>
      <button class="btn sm ghost danger" data-act="del" title="删除">✕</button>
    </div>
    <div style="padding:10px 12px 0">
      <textarea class="nb-src mono" rows="${Math.min(14, Math.max(2, (cell.source || '').split('\n').length))}"
        spellcheck="false"
        style="width:100%;font-family:var(--mono);font-size:12.5px;line-height:1.65;border:1px solid var(--border);border-radius:3px;padding:8px 10px;resize:vertical"
        placeholder="${esc(meta.hint)}">${esc(cell.source || '')}</textarea>
    </div>
    <div class="row" style="gap:8px;padding:6px 12px 10px">
      <span class="tiny dim">${esc(meta.hint)}</span>
      <span class="spacer"></span>
      <span class="tiny dim">Ctrl / Cmd + Enter 运行</span>
    </div>
    ${renderOutput(cell)}
  </div>`;
}

/* ------------------------------------------------------------ 魔法帮助 */

function magicHelpBody(magics, categories) {
  const groups = (categories && categories.length ? categories : ['全部']).map((cat) => {
    const items = magics.filter((m) => !categories?.length || m.category === cat);
    if (!items.length) return '';
    return `<div style="margin-bottom:14px">
      <div class="tiny dim" style="margin-bottom:6px">${esc(cat)}</div>
      <div class="col" style="gap:6px">${items
        .map(
          (m) => `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;background:#fff">
            <div class="row" style="gap:8px">
              <code class="inline" style="font-weight:500">${esc(m.usage)}</code>
              <span class="spacer"></span>
              <span class="tiny dim">${esc(m.name)}</span>
            </div>
            <div class="tiny muted" style="margin-top:4px">${esc(m.desc)}</div>
            ${m.example ? `<pre class="code" style="margin:6px 0 0;font-size:11px;padding:6px 8px">${esc(m.example)}</pre>` : ''}
          </div>`
        )
        .join('')}</div>
    </div>`;
  });
  return `<div class="prose" style="margin-bottom:10px">
      <p>魔法命令以 <code>%</code> 开头，写在 cell 第一行即可。共 <b>${magics.length}</b> 个。</p>
    </div>
    <div style="max-height:60vh;overflow:auto">${groups.join('')}</div>`;
}

/* -------------------------------------------------------------- 参数面板 */

async function openParamsPanel(notebook, params, onSaved) {
  const entries = Object.entries(params || {});
  const body = el('div');
  body.innerHTML = `
    <div class="tiny muted" style="margin-bottom:10px">
      参数用 <code class="inline">{{name}}</code> 在 SQL 中引用；带引号写法 <code class="inline">'{{name}}'</code> 与裸写法都支持。
    </div>
    <div class="col" style="gap:8px" id="param-rows">
      ${entries
        .map(
          ([k, v]) => `<div class="row" data-row style="gap:8px">
            <input data-k value="${esc(k)}" placeholder="参数名" style="flex:0 0 160px" />
            <input data-v value="${esc(v ?? '')}" placeholder="值" style="flex:1" />
            <button class="btn sm ghost" data-del>✕</button>
          </div>`
        )
        .join('')}
    </div>
    <button class="btn sm mt-12" id="add-param">＋ 添加参数</button>`;
  const rows = body.querySelector('#param-rows');
  const addRow = (k = '', v = '') => {
    const row = el('div', { class: 'row', style: 'gap:8px' });
    row.dataset.row = '';
    row.innerHTML = `<input data-k value="${esc(k)}" placeholder="参数名" style="flex:0 0 160px" />
      <input data-v value="${esc(v)}" placeholder="值" style="flex:1" />
      <button class="btn sm ghost" data-del>✕</button>`;
    row.querySelector('[data-del]').addEventListener('click', () => row.remove());
    rows.append(row);
  };
  body.querySelector('#add-param').addEventListener('click', () => addRow());
  body.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', () => b.closest('[data-row]').remove())
  );

  const saved = await modal({
    title: 'Notebook 参数',
    wide: true,
    body,
    actions: [
      { label: '取消', value: false },
      {
        label: '保存参数',
        primary: true,
        onClick: async () => {
          const next = {};
          body.querySelectorAll('[data-row]').forEach((row) => {
            const k = row.querySelector('[data-k]').value.trim();
            const v = row.querySelector('[data-v]').value;
            if (k) next[k] = v;
          });
          try {
            await api.patch(`/notebooks/${notebook.id}`, { params: next });
            toast('参数已保存', 'ok');
            if (onSaved) await onSaved(next);
          } catch (err) {
            toast(err.message || '保存失败', 'err');
            return false;
          }
          return true;
        },
      },
    ],
  });
  return saved;
}

/* ------------------------------------------------------------ 主渲染入口 */

export async function renderStudio2(page, ctx) {
  page.innerHTML = loading('加载工作台…');

  let state = {
    notebooks: [],
    summary: null,
    notebook: null,
    params: {},
    magics: [],
    categories: [],
    workdir: '',
    activeId: '',
    busy: false,
  };

  /* -------------------------------------------------------- 数据加载 */

  async function loadList() {
    const out = await api.get('/notebooks');
    state.notebooks = out.notebooks || [];
    state.summary = out.summary || null;
    if (!state.activeId && state.notebooks.length) state.activeId = state.notebooks[0].id;
    if (state.activeId && !state.notebooks.some((n) => n.id === state.activeId)) {
      state.activeId = state.notebooks[0]?.id || '';
    }
  }

  async function loadNotebook(id) {
    if (!id) {
      state.notebook = null;
      state.params = {};
      return;
    }
    const out = await api.get(`/notebooks/${id}`);
    state.notebook = out.notebook;
    state.params = out.params || {};
    state.workdir = out.workdir || '';
    if (!state.magics.length && out.magics) state.magics = out.magics;
    state.activeId = id;
  }

  async function loadMagics() {
    if (state.magics.length) return;
    try {
      const out = await api.get('/notebooks/magic-help');
      state.magics = out.magics || [];
      state.categories = out.categories || [];
    } catch {
      state.magics = [];
      state.categories = [];
    }
  }

  try {
    await Promise.all([loadList(), loadMagics()]);
    if (state.activeId) await loadNotebook(state.activeId);
  } catch (err) {
    page.innerHTML = card('工作台加载失败', `<pre class="code">${esc(err.message || String(err))}</pre>`);
    return;
  }

  /* -------------------------------------------------------- 视图渲染 */

  function treeHTML() {
    const groups = [
      { label: 'notebooks', icon: 'studio', items: state.notebooks },
    ];
    return `
      <div class="tp-head">
        <span>工作区文件</span>
        <button class="btn sm ghost" id="new-nb" title="新建 Notebook">＋ 新建</button>
      </div>
      <div class="tree">
        ${groups
          .map(
            (g) => `
          <div class="tree-node"><span class="ico">${icon('folder', 13)}</span>${esc(g.label)}
            <span class="cnt">${g.items.length}</span></div>
          <div class="tree-children">
            ${
              g.items.length
                ? g.items
                    .map(
                      (n) => `<div class="tree-node${n.id === state.activeId ? ' active' : ''}" data-nb="${esc(n.id)}"
                        title="${esc(n.description || n.name)}">
                        <span class="ico">${icon('studio', 13)}</span>
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(n.name)}</span>
                        <span class="cnt">${n.cell_count}</span>
                      </div>`
                    )
                    .join('')
                : '<div class="tiny dim" style="padding:8px 12px">还没有 notebook</div>'
            }
          </div>`
          )
          .join('')}
        <div class="tree-node" style="cursor:default"><span class="ico">${icon('folder', 13)}</span>工作目录</div>
        <div class="tree-children">
          <div class="tiny dim" style="padding:6px 12px;word-break:break-all;font-family:var(--mono);font-size:11px">
            ${esc(state.workdir || '-')}
          </div>
        </div>
      </div>`;
  }

  function toolbarHTML() {
    const nb = state.notebook;
    return `<div class="tbl-toolbar">
      <span class="title">${esc(nb?.name || '未选择 Notebook')}</span>
      ${nb ? `<span class="tag outline">${nb.cells.length} 个 cell</span>` : ''}
      <span class="spacer"></span>
      <button class="btn sm" id="btn-add-sql">＋ SQL</button>
      <button class="btn sm" id="btn-add-py">＋ Python</button>
      <button class="btn sm" id="btn-add-md">＋ Markdown</button>
      <button class="btn sm" id="btn-params">参数${Object.keys(state.params || {}).length ? ` (${Object.keys(state.params).length})` : ''}</button>
      <button class="btn sm" id="btn-help">魔法帮助</button>
      <button class="btn sm ghost" id="btn-reset" title="清空变量与参数">重置内核</button>
      <button class="btn sm primary" id="btn-run-all">▶ 运行全部</button>
    </div>`;
  }

  function cellsHTML() {
    const nb = state.notebook;
    if (!nb) {
      return card(
        '',
        emptyState('还没有 Notebook', '左侧点「＋ 新建」创建一个，或用示例快速开始'),
        { flush: false }
      );
    }
    if (!nb.cells.length) {
      return card('', emptyState('这个 Notebook 还没有 cell', '点上方「＋ SQL / ＋ Python / ＋ Markdown」添加'), {
        flush: false,
      });
    }
    return nb.cells.map((c, i) => cellHTML(c, i, nb.cells.length)).join('');
  }

  function statsHTML() {
    const s = state.summary || {};
    const paramsCount = Object.keys(state.params || {}).length;
    return `<div class="stat-row" style="grid-template-columns:repeat(4,minmax(0,1fr));margin-bottom:16px">
      <div class="stat"><span class="label">Notebook</span><span class="value">${fmt.num(s.notebooks ?? state.notebooks.length)}</span><span class="foot">个工作台文件</span></div>
      <div class="stat"><span class="label">Cell 总数</span><span class="value">${fmt.num(s.cells ?? 0)}</span><span class="foot">SQL / Python / Markdown</span></div>
      <div class="stat"><span class="label">魔法命令</span><span class="value">${fmt.num(state.magics.length)}</span><span class="foot">%sql %python %param …</span></div>
      <div class="stat"><span class="label">当前参数</span><span class="value">${fmt.num(paramsCount)}</span><span class="foot">${paramsCount ? '用 {{name}} 引用' : '点工具栏「参数」设置'}</span></div>
    </div>`;
  }

  function render() {
    page.innerHTML = `
      <div id="nb-stats">${statsHTML()}</div>
      <div class="split" style="grid-template-columns:232px 1fr">
        <div class="tree-panel" id="nb-tree">${treeHTML()}</div>
        <div class="col" style="gap:16px">
          <div class="card flush">
            ${toolbarHTML()}
            <div id="nb-cells" style="padding:14px;display:grid;gap:12px;background:var(--sub-2)">
              ${cellsHTML()}
            </div>
          </div>
        </div>
      </div>`;
    bind();
  }

  /* -------------------------------------------------------- 事件绑定 */

  function currentCell(cellId) {
    return state.notebook?.cells.find((c) => c.id === cellId) || null;
  }

  async function persistSource(cellId, source) {
    const cell = currentCell(cellId);
    if (!cell || cell.source === source) return;
    cell.source = source;
    try {
      await api.patch(`/notebooks/${state.notebook.id}/cells/${cellId}`, { source });
    } catch (err) {
      toast(err.message || '保存失败', 'err');
    }
  }

  async function runCell(cellId, { silent = false } = {}) {
    const cell = currentCell(cellId);
    if (!cell) return;
    const node = page.querySelector(`[data-cell="${cellId}"]`);
    if (node) node.style.opacity = '.65';
    try {
      const out = await api.post(`/notebooks/${state.notebook.id}/cells/${cellId}/run`, {
        params: state.params || {},
      });
      const updated = out.notebook?.cells?.find((c) => c.id === cellId);
      const idx = state.notebook.cells.findIndex((c) => c.id === cellId);
      if (updated && idx >= 0) state.notebook.cells[idx] = updated;
      state.notebook = out.notebook || state.notebook;
      repaintCells();
      if (updated?.error && !silent) toast('执行失败，见红色输出区', 'err');
      return out.result;
    } catch (err) {
      if (!silent) toast(err.message || '执行失败', 'err');
      return { error: err.message };
    }
  }

  function repaintCells() {
    const host = page.querySelector('#nb-cells');
    if (!host) return;
    host.innerHTML = cellsHTML();
    bindCells();
    const tb = page.querySelector('.tbl-toolbar');
    if (tb && state.notebook) {
      const titleNode = tb.querySelector('.title');
      if (titleNode) titleNode.textContent = state.notebook.name;
    }
  }

  async function runAll() {
    if (!state.notebook || state.busy) return;
    state.busy = true;
    const btn = page.querySelector('#btn-run-all');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '执行中…';
    }
    try {
      const out = await api.post(`/notebooks/${state.notebook.id}/run-all`, {
        params: state.params || {},
      });
      state.notebook = out.notebook || state.notebook;
      repaintCells();
      if (out.ok) toast(`全部 ${out.results.length} 个 cell 执行成功 · ${out.elapsed_ms} ms`, 'ok');
      else toast(`${out.failed} 个 cell 执行失败`, 'err');
      await loadList();
      repaintStats();
    } catch (err) {
      toast(err.message || '运行失败', 'err');
    } finally {
      state.busy = false;
      const b = page.querySelector('#btn-run-all');
      if (b) {
        b.disabled = false;
        b.textContent = '▶ 运行全部';
      }
    }
  }

  function repaintStats() {
    const host = page.querySelector('#nb-stats');
    if (host) host.innerHTML = statsHTML();
    const tree = page.querySelector('#nb-tree');
    if (tree) {
      tree.innerHTML = treeHTML();
      bindTree();
    }
    const tb = page.querySelector('.tbl-toolbar');
    if (tb && state.notebook) {
      const titleNode = tb.querySelector('.title');
      if (titleNode) titleNode.textContent = state.notebook.name;
    }
  }

  async function addCell(type) {
    if (!state.notebook) {
      toast('请先新建或选择一个 Notebook', 'err');
      return;
    }
    try {
      const out = await api.post(`/notebooks/${state.notebook.id}/cells`, {
        type,
        source: DEFAULT_SOURCES[type] || '',
      });
      state.notebook = out.notebook || state.notebook;
      repaintCells();
      const nodes = page.querySelectorAll('[data-cell]');
      const last = nodes[nodes.length - 1];
      if (last) {
        last.scrollIntoView({ behavior: 'smooth', block: 'center' });
        last.querySelector('.nb-src')?.focus();
      }
      await loadList();
      repaintStats();
    } catch (err) {
      toast(err.message || '添加失败', 'err');
    }
  }

  async function deleteCell(cellId) {
    const ok = await confirmDialog('删除 Cell', '删除后该 cell 的源码与输出都会丢失，确认继续？', '删除');
    if (!ok) return;
    try {
      const out = await api.del(`/notebooks/${state.notebook.id}/cells/${cellId}`);
      state.notebook = out.notebook || state.notebook;
      repaintCells();
      await loadList();
      repaintStats();
      toast('已删除', 'ok');
    } catch (err) {
      toast(err.message || '删除失败', 'err');
    }
  }

  async function moveCell(cellId, delta) {
    const cells = state.notebook.cells;
    const idx = cells.findIndex((c) => c.id === cellId);
    const target = idx + delta;
    if (idx < 0 || target < 0 || target >= cells.length) return;
    const [moved] = cells.splice(idx, 1);
    cells.splice(target, 0, moved);
    repaintCells();
    try {
      await api.patch(`/notebooks/${state.notebook.id}`, { cells });
    } catch (err) {
      toast(err.message || '排序保存失败', 'err');
    }
  }

  function bindCells() {
    page.querySelectorAll('[data-cell]').forEach((node) => {
      const cellId = node.dataset.cell;
      const ta = node.querySelector('.nb-src');
      if (ta) {
        ta.addEventListener('keydown', (e) => {
          if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            persistSource(cellId, ta.value).then(() => runCell(cellId));
          }
          if (e.key === 'Enter' && e.shiftKey) {
            e.preventDefault();
            persistSource(cellId, ta.value).then(() => runCell(cellId));
          }
        });
        ta.addEventListener('blur', () => persistSource(cellId, ta.value));
        ta.addEventListener('input', () => {
          const cell = currentCell(cellId);
          if (cell) cell.source = ta.value;
        });
      }
      node.querySelectorAll('[data-act]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const act = btn.dataset.act;
          if (act === 'run') {
            if (ta) await persistSource(cellId, ta.value);
            await runCell(cellId);
          } else if (act === 'del') {
            await deleteCell(cellId);
          } else if (act === 'up') {
            await moveCell(cellId, -1);
          } else if (act === 'down') {
            await moveCell(cellId, 1);
          }
        });
      });
    });
  }

  function bindTree() {
    page.querySelectorAll('[data-nb]').forEach((node) => {
      node.addEventListener('click', async () => {
        if (node.dataset.nb === state.activeId) return;
        try {
          await loadNotebook(node.dataset.nb);
          render();
        } catch (err) {
          toast(err.message || '打开失败', 'err');
        }
      });
    });
  }

  async function newNotebook() {
    const data = await formModal({
      title: '新建 Notebook',
      fields: [
        { label: '名称', name: 'name', required: true, placeholder: '例如 daily_gmv' },
        { label: '说明', name: 'description', placeholder: '这个 notebook 做什么' },
        {
          label: '初始内容',
          name: 'template',
          type: 'select',
          value: 'blank',
          options: [
            { value: 'blank', label: '空白（含 %tables 示例）' },
            { value: 'sql', label: 'SQL 分析模板' },
            { value: 'python', label: 'Python 特征工程模板' },
          ],
        },
      ],
      onSubmit: async (d) => {
        const cells = [];
        if (d.template === 'sql') {
          cells.push({ type: 'sql', source: DEFAULT_SOURCES.sql });
        } else if (d.template === 'python') {
          cells.push({ type: 'python', source: DEFAULT_SOURCES.python });
        }
        const out = await api.post('/notebooks', {
          name: d.name,
          description: d.description || '',
          cells: cells.length ? cells : undefined,
        });
        state.activeId = out.notebook.id;
        await loadList();
        await loadNotebook(out.notebook.id);
        render();
        toast('已创建', 'ok');
      },
    });
    return data;
  }

  function bind() {
    bindTree();
    bindCells();

    page.querySelector('#new-nb')?.addEventListener('click', newNotebook);
    page.querySelector('#btn-add-sql')?.addEventListener('click', () => addCell('sql'));
    page.querySelector('#btn-add-py')?.addEventListener('click', () => addCell('python'));
    page.querySelector('#btn-add-md')?.addEventListener('click', () => addCell('markdown'));
    page.querySelector('#btn-run-all')?.addEventListener('click', runAll);

    page.querySelector('#btn-params')?.addEventListener('click', async () => {
      if (!state.notebook) return toast('请先选择 Notebook', 'err');
      await openParamsPanel(state.notebook, state.params, async (next) => {
        state.params = next;
        repaintStats();
      });
    });

    page.querySelector('#btn-help')?.addEventListener('click', () => {
      modal({
        title: `魔法命令（${state.magics.length} 个）`,
        wide: true,
        body: magicHelpBody(state.magics, state.categories),
        actions: [{ label: '关闭', primary: true }],
      });
    });

    page.querySelector('#btn-reset')?.addEventListener('click', async () => {
      if (!state.notebook) return toast('请先选择 Notebook', 'err');
      const ok = await confirmDialog(
        '重置内核',
        '会清空当前 Notebook 的 Python 变量与参数（cell 源码保留），确认继续？',
        '重置'
      );
      if (!ok) return;
      try {
        await api.post(`/notebooks/${state.notebook.id}/reset`);
        await loadNotebook(state.notebook.id);
        render();
        toast('已重置内核', 'ok');
      } catch (err) {
        toast(err.message || '重置失败', 'err');
      }
    });
  }

  // 顶部动作：新建 / 删除当前 notebook
  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', {
        class: 'btn',
        onclick: async () => {
          if (!state.notebook) return;
          const ok = await confirmDialog('删除 Notebook', `确认删除「${state.notebook.name}」？该操作不可恢复。`, '删除');
          if (!ok) return;
          try {
            await api.del(`/notebooks/${state.notebook.id}`);
            state.notebook = null;
            state.activeId = '';
            await loadList();
            if (state.activeId) await loadNotebook(state.activeId);
            render();
            toast('已删除', 'ok');
          } catch (err) {
            toast(err.message || '删除失败', 'err');
          }
        },
      }, '删除当前 Notebook'),
      el('button', { class: 'btn primary', onclick: newNotebook }, '＋ 新建 Notebook'),
    ])
  );

  render();
}
