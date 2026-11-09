// Buddy：Agent 原生三栏工作台
import { api, session, fmt } from './api.js';
import { el, esc, md, toast, modal, formModal, confirmDialog, emptyState } from './ui.js';
import { artifactHTML, traceHTML, engineBadge } from './artifact.js';
import { icon } from './icons.js';

const MODES = [
  { id: 'engineering', label: '数据开发', expert: '数据工程专家', desc: '数据接入、ETL 开发、工作流编排、运维诊断' },
  { id: 'governance', label: '数据治理', expert: '数据治理专家', desc: '数据建模、敏感识别、质量规则、资产盘点' },
  { id: 'analysis', label: '数据分析', expert: '数据分析专家', desc: '问数、异动归因、报告生成、看板搭建' },
];

const SUGGESTIONS = {
  engineering: [
    ['建一条每日汇总工作流', '给 fact_orders 建每日销售汇总，早上 7 点调度'],
    ['排查上次失败', '工作流「每日经营数据刷新」上次失败的原因是什么'],
    ['看看有哪些表', '列出当前空间的数据表、行数与用途'],
  ],
  governance: [
    ['扫描敏感字段', '扫描 dim_customer 的敏感字段，给出分级、置信度与依据'],
    ['推荐质量规则', '给 fact_orders 推荐数据质量规则并说明理由'],
    ['资产盘点', '盘点 fact_orders 的行数、空值率与质量得分'],
  ],
  analysis: [
    ['近 30 天 GMV 趋势', '按天看 GMV 走势，指出拐点与环比变化'],
    ['渠道对比', '各渠道 GMV 排名与占比'],
    ['生成经营看板', '帮我生成一份零售经营的 AI 看板'],
  ],
};

const TOOL_ICON = { folder: 'folder', upload: 'ingest', ref: 'sources', skill: 'settings' };

const state = {
  mode: 'analysis',
  chatMode: 'agent',
  convId: null,
  conversations: [],
  messages: [],
  pending: null,
  sending: false,
  model: '',
  llmReady: false,
  tasks: [],
  spaces: [],
  artifacts: [],
  previewIndex: 0,
  workdir: '工作空间根目录',
  refs: [],
  skills: [],
  root: null,
  ctx: null,
};

export async function renderBuddy(page, ctx) {
  state.root = page;
  state.ctx = ctx;
  state.convId = null;
  state.messages = [];
  state.pending = null;
  state.artifacts = [];

  page.innerHTML = `
    <div class="chat-layout" id="chat-layout">
      <aside class="chat-rail">
        <div class="rail-section">
          <div class="segmented" id="mode-switch"></div>
        </div>
        <div class="rail-section" style="padding-top:0">
          <button class="btn primary block sm" id="new-conv">＋ 新建会话</button>
        </div>
        <div class="rail-label">历史会话<span class="dim">${''}</span></div>
        <div class="rail-list" id="conv-list" style="flex:1"></div>
        <div class="rail-label">定时任务</div>
        <div class="rail-list" id="task-list" style="flex:none;max-height:150px"></div>
        <div class="rail-label">知识库</div>
        <div class="rail-list" style="flex:none;padding-bottom:12px">
          <a class="rail-item" href="#/semantic"><span class="ico">${icon('knowledge', 14)}</span><span>指标口径与术语</span></a>
          <a class="rail-item" href="#/catalog"><span class="ico">${icon('catalog', 14)}</span><span>元数据目录</span></a>
        </div>
      </aside>

      <section class="chat-main">
        <div class="chat-body" id="chat-body"></div>
        <div class="composer">
          <div class="composer-box">
            <textarea id="input" rows="2" placeholder="描述你的需求，或直接问一个数…"></textarea>
            <div class="composer-tools">
              <button class="tool-btn" id="btn-dir"><span>${icon('folder', 13)}</span>工作目录</button>
              <button class="tool-btn" id="btn-upload"><span>${icon('ingest', 13)}</span>上传</button>
              <button class="tool-btn" id="btn-ref"><span>${icon('sources', 13)}</span>@ 引用</button>
              <span class="spacer"></span>
              <select class="sel" id="chat-mode">
                <option value="agent">Agent · 可操作</option>
                <option value="ask">Ask · 仅问答</option>
              </select>
              <select class="sel" id="model-sel"></select>
              <button class="tool-btn" id="btn-skills"><span>${icon('settings', 13)}</span>Skills</button>
              <button class="btn primary sm" id="send">发送</button>
            </div>
          </div>
          <div class="row mt-8" style="justify-content:space-between">
            <span class="tiny dim" id="ctx-hint"></span>
            <span class="tiny dim">Enter 发送 · Shift + Enter 换行</span>
          </div>
        </div>
      </section>

      <aside class="preview-pane" id="preview-pane">
        <div class="preview-head">
          <h4>产物预览</h4>
          <span class="tag" id="artifact-count">0</span>
        </div>
        <div class="preview-body" id="preview-body"></div>
      </aside>
    </div>`;

  buildModeSwitch();
  bindComposer();
  await Promise.all([loadSettings(), loadConversations(), loadTasks(), loadSpaces()]);
  updateCtxHint();
  renderMessages();
  renderPreview();

  const deep = (location.hash.split('/')[2] || '').trim();
  if (deep) await openConversation(deep);
}

/* ------------------------------------------------------------------ 左栏 */
function buildModeSwitch() {
  const host = state.root.querySelector('#mode-switch');
  host.innerHTML = '';
  for (const m of MODES) {
    const b = el('button', { class: state.mode === m.id ? 'active' : '', title: m.desc }, m.label);
    b.addEventListener('click', async () => {
      if (state.mode === m.id) return;
      state.mode = m.id;
      state.convId = null;
      state.messages = [];
      state.pending = null;
      state.artifacts = [];
      buildModeSwitch();
      renderMessages();
      renderPreview();
      await loadConversations();
    });
    host.append(b);
  }
}

async function loadConversations() {
  try {
    const out = await api.get(`/conversations?mode=${state.mode}`);
    state.conversations = out.conversations || [];
  } catch { state.conversations = []; }
  const host = state.root.querySelector('#conv-list');
  host.innerHTML = state.conversations.length
    ? state.conversations.map((c) => `<div class="rail-item ${c.id === state.convId ? 'active' : ''}" data-conv="${c.id}">
        <span class="ico">${icon('buddy', 13)}</span><span>${esc(c.title || '新会话')}</span>
        <span class="x" data-del-conv="${c.id}">✕</span></div>`).join('')
    : `<div class="tiny dim" style="padding:6px 8px">还没有会话</div>`;
  host.querySelectorAll('[data-conv]').forEach((n) =>
    n.addEventListener('click', (e) => {
      if (e.target.dataset.delConv) return;
      openConversation(n.dataset.conv);
    })
  );
  host.querySelectorAll('[data-del-conv]').forEach((x) =>
    x.addEventListener('click', async (e) => {
      e.stopPropagation();
      await api.del(`/conversations/${x.dataset.delConv}`);
      if (state.convId === x.dataset.delConv) {
        state.convId = null; state.messages = []; state.artifacts = []; renderMessages(); renderPreview();
      }
      await loadConversations();
    })
  );
}

async function openConversation(id) {
  try {
    const out = await api.get(`/conversations/${id}`);
    state.convId = id;
    state.mode = out.conversation.mode || state.mode;
    state.chatMode = out.conversation.chat_mode || 'agent';
    state.messages = out.messages || [];
    state.pending = (out.pending_approvals || [])[0] || null;
    const sel = state.root.querySelector('#chat-mode');
    if (sel) sel.value = state.chatMode;
    buildModeSwitch();
    updateCtxHint();
    collectArtifacts();
    if (state.artifacts.length) state.previewIndex = state.artifacts.length - 1;
    renderMessages();
    renderPreview();
    await loadConversations();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function loadTasks() {
  try {
    const out = await api.get('/buddy-tasks');
    state.tasks = out.tasks || [];
  } catch { state.tasks = []; }
  const host = state.root.querySelector('#task-list');
  host.innerHTML = state.tasks.length
    ? state.tasks.map((t) => `<div class="rail-item" data-task="${t.id}">
        <span class="ico">${icon('ops', 13)}</span><span>${esc(t.name)}</span>
        <span class="x">${t.enabled ? '●' : '○'}</span></div>`).join('')
    : `<div class="tiny dim" style="padding:6px 8px">暂无定时任务</div>`;
  host.querySelectorAll('[data-task]').forEach((n) =>
    n.addEventListener('click', () => openTask(n.dataset.task))
  );
}

async function openTask(taskId) {
  const task = state.tasks.find((x) => x.id === taskId);
  if (!task) return;
  let runs = [];
  try {
    const out = await api.get(`/buddy-tasks/${taskId}/runs`);
    runs = out.runs || [];
  } catch { runs = []; }

  const body = el('div');
  body.innerHTML = `<div class="col" style="gap:14px">
    <div class="info-strip"><span class="tag ${task.enabled ? 'green' : ''}">${task.enabled ? '已启用' : '已停用'}</span>
      <span class="mono tiny">${esc(task.cron || '')}</span>
      <span class="spacer"></span><span class="dim tiny">最近运行 ${task.last_run ? fmt.ago(task.last_run) : '从未'}</span></div>
    <div><div class="tiny dim" style="margin-bottom:4px">提示词</div><div class="tiny">${esc(task.prompt)}</div></div>
    <div><div class="tiny dim" style="margin-bottom:6px">运行记录</div>
      ${runs.length ? `<div class="col" style="gap:6px">${runs.map((r) => `<div class="list-item">
        <div class="meta"><strong>${r.status === 'success' ? '成功' : r.status === 'failed' ? '失败' : r.status === 'pending' ? '待确认' : '运行中'}</strong>
          <span>${fmt.dt(r.started_at)}${r.error ? ' · ' + esc(r.error) : ''}</span></div>
        ${r.conversation_id ? `<a class="btn-link" href="#/buddy/${r.conversation_id}">查看会话</a>` : ''}
      </div>`).join('')}</div>` : '<div class="tiny dim">还没有运行记录</div>'}
    </div>
    ${runs[0] && runs[0].summary ? `<div><div class="tiny dim" style="margin-bottom:4px">最近结果</div>
      <div class="prose" style="font-size:12.5px">${md(runs[0].summary)}</div></div>` : ''}
  </div>`;

  modal({
    title: task.name,
    wide: true,
    body,
    onMount: (root) => {
      root.querySelectorAll('a[href^="#/buddy/"]').forEach((a) =>
        a.addEventListener('click', () => document.querySelector('.modal-mask')?.remove())
      );
    },
    actions: [
      { label: '关闭' },
      {
        label: task.enabled ? '停用' : '启用',
        onClick: async () => {
          await api.patch(`/buddy-tasks/${task.id}`, { enabled: !task.enabled });
          toast(task.enabled ? '已停用' : '已启用', 'ok');
          await loadTasks();
        },
      },
      {
        label: '立即运行',
        primary: true,
        onClick: async (root) => {
          toast('任务已提交，执行中…');
          try {
            const out = await api.post(`/buddy-tasks/${task.id}/run`, {});
            toast(`执行${out.run.status === 'success' ? '成功' : '结束'}`, 'ok');
            await loadTasks();
            await loadConversations();
            if (out.run.conversation_id) {
              document.querySelector('.modal-mask')?.remove();
              state.convId = out.run.conversation_id;
              location.hash = `#/buddy/${out.run.conversation_id}`;
            }
          } catch (e) {
            toast(e.message, 'err');
          }
        },
      },
    ],
  });
}

async function loadSpaces() {
  try {
    const out = await api.get('/analysis-spaces');
    state.spaces = out.spaces || [];
  } catch { state.spaces = []; }
}

/* ------------------------------------------------------------------ 消息 */
function renderMessages() {
  const body = state.root.querySelector('#chat-body');
  if (!state.messages.length && !state.sending) {
    const mode = MODES.find((m) => m.id === state.mode);
    body.innerHTML = `<div class="empty-chat">
      <h2>你好，我是 Buddy</h2>
      <p class="sub">当前场景：<b>${esc(mode.label)}</b> · ${esc(mode.desc)}。
        ${state.llmReady ? '已接入大模型，可多步调用工具完成任务。' : '尚未配置模型，将使用内置规则引擎（问数、扫描、建任务可用）。'}</p>
      <div class="expert-cards">
        ${MODES.map((m) => `<button class="expert-card" data-mode="${m.id}">
          <div class="ec-title"><span style="color:var(--brand)">${icon('agents', 16)}</span>${esc(m.expert)}</div>
          <div class="ec-desc">${esc(m.desc)}</div>
          <div class="ec-tags">${(m.id === 'engineering' ? ['接入', 'ETL', 'DAG'] : m.id === 'governance' ? ['分级', '打标', '质量'] : ['问数', '归因', '看板']).map((t) => `<span class="tag outline">${t}</span>`).join('')}</div>
        </button>`).join('')}
      </div>
      <div class="suggest-list">
        ${(SUGGESTIONS[state.mode] || []).map(([t, d]) => `<div class="suggest-item" data-fill="${esc(d)}">
          <span class="dim">›</span><span>${esc(t)}</span><span class="dim" style="margin-left:auto;font-size:12px">${esc(d)}</span></div>`).join('')}
      </div>
      <div class="tiny dim mt-16">我会先看数据再回答；涉及改口径、建表、打标等写操作时，会先请你确认。</div>
    </div>`;
    body.querySelectorAll('[data-fill]').forEach((n) =>
      n.addEventListener('click', () => {
        const input = state.root.querySelector('#input');
        input.value = n.dataset.fill;
        input.focus();
      })
    );
    body.querySelectorAll('[data-mode]').forEach((n) =>
      n.addEventListener('click', () => {
        state.mode = n.dataset.mode;
        buildModeSwitch();
        renderMessages();
        loadConversations();
      })
    );
    return;
  }

  body.innerHTML = state.messages.map(messageHTML).join('');
  if (state.pending) {
    body.insertAdjacentHTML('beforeend', approvalHTML(state.pending));
    bindApproval(body);
  }
  if (state.sending) {
    body.insertAdjacentHTML('beforeend', `<div class="msg assistant">
      <span class="avatar">B</span><div class="body">
        <div class="msg-head"><strong>Buddy</strong></div>
        <div class="thinking"><i></i><i></i><i></i><span style="margin-left:8px">正在思考并调用工具…</span></div>
      </div></div>`);
    body.scrollTop = body.scrollHeight;
    return;
  }
  const answers = body.querySelectorAll('.msg.assistant');
  const last = answers[answers.length - 1];
  if (last && last.scrollHeight > body.clientHeight * 0.75) last.scrollIntoView({ block: 'start' });
  else body.scrollTop = body.scrollHeight;
  body.querySelectorAll('[data-artifact]').forEach((n) =>
    n.addEventListener('click', () => selectArtifact(Number(n.dataset.artifact)))
  );
}

function messageHTML(m) {
  const meta = m.meta || {};
  const isUser = m.role === 'user';
  const artifacts = (meta.artifacts || []).map((a) => artifactHTML(a, { compact: true, index: state.artifacts.indexOf(a) })).join('');
  const note = meta.note ? `<div class="tiny mt-8" style="color:var(--warning)">${esc(meta.note)}</div>` : '';
  if (isUser) {
    return `<div class="msg user">
      <span class="avatar">${esc((session.user?.name || 'U').slice(0, 1))}</span>
      <div class="body"><div class="bubble">${esc(m.content)}</div></div>
    </div>`;
  }
  return `<div class="msg assistant">
    <span class="avatar">B</span>
    <div class="body">
      <div class="msg-head"><strong>Buddy</strong><span>${esc(fmt.ago(m.created_at))}</span>${engineBadge(meta)}</div>
      <div class="prose">${md(m.content)}</div>
      ${traceHTML(meta.trace)}
      ${artifacts}
      ${note}
    </div>
  </div>`;
}

function approvalHTML(a) {
  return `<div class="approval" id="approval-box">
    <h4>⚠ 需要确认后执行</h4>
    <div class="tiny" style="margin-top:4px">${esc(a.reason || '该操作会修改平台资产')}</div>
    <ul class="factors">${(a.factors || []).map((f) => `<li>${esc(f)}</li>`).join('')}</ul>
    <div class="tiny dim">工具：<span class="mono">${esc(a.tool)}</span></div>
    <div class="args">${esc(JSON.stringify(a.args || {}, null, 2))}</div>
    <div class="acts">
      <button class="btn primary sm" data-approve>确认执行</button>
      <button class="btn sm" data-reject>取消这一步</button>
    </div>
  </div>`;
}

function bindApproval(root) {
  const box = root.querySelector('#approval-box');
  if (!box) return;
  box.querySelector('[data-approve]').addEventListener('click', async () => {
    const id = state.pending?.id;
    if (!id) return;
    box.querySelectorAll('button').forEach((b) => (b.disabled = true));
    state.sending = true;
    renderMessages();
    try {
      const out = await api.post(`/approvals/${id}/approve`, {});
      state.pending = null;
      state.sending = false;
      state.messages.push({ role: 'assistant', content: out.content, meta: out.meta, created_at: new Date().toISOString() });
      if (out.approval) state.pending = out.approval;
      collectArtifacts();
      renderMessages();
      renderPreview();
      await loadConversations();
    } catch (err) {
      state.sending = false;
      renderMessages();
      toast(err.message, 'err');
    }
  });
  box.querySelector('[data-reject]').addEventListener('click', async () => {
    const id = state.pending?.id;
    if (!id) return;
    try {
      const out = await api.post(`/approvals/${id}/reject`, {});
      state.pending = null;
      state.messages.push({ role: 'assistant', content: out.content, meta: out.meta, created_at: new Date().toISOString() });
      collectArtifacts();
      renderMessages();
      renderPreview();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

/* ------------------------------------------------------------------ 右栏 */
function collectArtifacts() {
  state.artifacts = [];
  for (const m of state.messages) {
    for (const a of (m.meta?.artifacts || [])) state.artifacts.push(a);
  }
  if (state.previewIndex >= state.artifacts.length) state.previewIndex = Math.max(0, state.artifacts.length - 1);
}

function renderPreview() {
  const pane = state.root.querySelector('#preview-pane');
  if (!pane) return;
  const body = pane.querySelector('#preview-body');
  pane.querySelector('#artifact-count').textContent = state.artifacts.length;
  if (!state.artifacts.length) {
    body.innerHTML = emptyState('还没有产物', '对话产生的表格、图表、报告会显示在这里', '◫');
    return;
  }
  const current = state.artifacts[state.previewIndex] || state.artifacts[0];
  body.innerHTML = `
    <div class="row wrap" style="gap:4px;margin-bottom:12px">
      ${state.artifacts.map((a, i) => `<button class="tool-btn ${i === state.previewIndex ? 'on' : ''}" data-art="${i}">
        ${esc(artifactTitle(a))}</button>`).join('')}
    </div>
    <div class="artifact" style="margin-top:0">${artifactHTML(current)}</div>`;
  body.querySelectorAll('[data-art]').forEach((b) =>
    b.addEventListener('click', () => {
      state.previewIndex = Number(b.dataset.art);
      renderPreview();
    })
  );
}

function artifactTitle(a) {
  return a.title || { query: '查询结果', sensitive_scan: '敏感扫描', quality_results: '质量结果', dashboard: '看板', profile: '资产盘点', table_schema: '表结构', table_list: '资产列表', knowledge: '知识命中', flow_run: '工作流' }[a.type] || '产物';
}

function selectArtifact(i) {
  state.previewIndex = i;
  renderPreview();
  state.root.querySelector('#preview-pane')?.scrollIntoView({ block: 'nearest' });
}

/* ------------------------------------------------------------------ 输入区 */
function bindComposer() {
  const input = state.root.querySelector('#input');
  const modeSel = state.root.querySelector('#chat-mode');
  modeSel.value = state.chatMode;
  modeSel.addEventListener('change', () => {
    state.chatMode = modeSel.value;
    updateCtxHint();
  });
  state.root.querySelector('#model-sel').addEventListener('change', (e) => { state.model = e.target.value; });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
      return;
    }
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  });
  state.root.querySelector('#send').addEventListener('click', send);
  state.root.querySelector('#new-conv').addEventListener('click', () => {
    state.convId = null; state.messages = []; state.pending = null; state.artifacts = [];
    renderMessages(); renderPreview();
    input.focus();
  });
  state.root.querySelector('#btn-dir').addEventListener('click', () => {
    modal({
      title: '选择工作目录',
      body: `<div class="col" style="gap:6px">
        ${['工作空间根目录', 'demo.retail', 'notebooks', 'python'].map((d) => `<div class="list-item" data-dir="${d}" style="cursor:pointer">
          <span style="color:var(--text-3)">${icon('folder', 16)}</span><div class="meta"><strong>${d}</strong></div></div>`).join('')}
        <div class="tiny dim mt-8">模型会优先读取该目录理解项目，产物默认落盘此处。不限制访问其他路径。</div>
      </div>`,
      onMount: (root) => {
        root.querySelectorAll('[data-dir]').forEach((n) =>
          n.addEventListener('click', () => {
            state.workdir = n.dataset.dir;
            updateCtxHint();
            document.querySelector('.modal-mask')?.remove();
            toast(`工作目录：${state.workdir}`);
          })
        );
      },
      actions: [{ label: '取消' }],
    });
  });
  state.root.querySelector('#btn-upload').addEventListener('click', () => uploadFile());
  state.root.querySelector('#btn-ref').addEventListener('click', () => openRefPicker(input));
  state.root.querySelector('#btn-skills').addEventListener('click', () => openSkills());
}

function updateCtxHint() {
  const hint = state.root.querySelector('#ctx-hint');
  if (!hint) return;
  const parts = [`工作目录：${state.workdir}`];
  if (state.refs.length) parts.push(`引用 ${state.refs.length} 项`);
  if (state.skills.length) parts.push(`Skills ${state.skills.length} 个`);
  parts.push(state.chatMode === 'ask' ? 'Ask 模式不改数据' : 'Agent 模式');
  hint.textContent = parts.join(' · ');
}

async function loadSettings() {
  try {
    const s = await api.get('/settings');
    state.llmReady = s.llm.configured;
    const sel = state.root.querySelector('#model-sel');
    const models = new Set(['']);
    (s.providers || []).forEach((p) => p.model && models.add(p.model));
    if (s.llm.model) models.add(s.llm.model);
    models.add('deepseek-reasoner');
    sel.innerHTML = [...models].map((m) => `<option value="${esc(m)}">${m ? esc(m) : `默认 · ${esc(s.llm.model || '未配置')}`}</option>`).join('');
  } catch { /* ignore */ }
}

async function openRefPicker(input) {
  const [{ tables }, { docs }] = await Promise.all([
    api.get('/catalog/tables').catch(() => ({ tables: [] })),
    api.get('/knowledge').catch(() => ({ docs: [] })),
  ]);
  const body = el('div');
  body.innerHTML = `<div class="col" style="gap:14px">
    <div><div class="tiny dim" style="margin-bottom:6px">数据表</div>
      <div class="col" style="gap:4px">${tables.slice(0, 8).map((t) => `<div class="list-item" data-ref="${esc(t.ref)}" style="cursor:pointer">
        <span style="color:var(--text-3)">${icon('table', 15)}</span>
        <div class="meta"><strong class="mono" style="font-size:12.5px">${esc(t.ref)}</strong><span>${fmt.num(t.row_count)} 行</span></div></div>`).join('') || '<div class="dim tiny">暂无</div>'}</div></div>
    <div><div class="tiny dim" style="margin-bottom:6px">知识库文档</div>
      <div class="col" style="gap:4px">${docs.slice(0, 6).map((d) => `<div class="list-item" data-ref-doc="${esc(d.title)}" style="cursor:pointer">
        <span style="color:var(--text-3)">${icon('knowledge', 15)}</span>
        <div class="meta"><strong>${esc(d.title)}</strong><span>${esc(d.tags || '通用')}</span></div></div>`).join('') || '<div class="dim tiny">暂无</div>'}</div></div>
  </div>`;
  modal({
    title: '引用上下文',
    wide: true,
    body,
    onMount: (root) => {
      root.querySelectorAll('[data-ref], [data-ref-doc]').forEach((n) =>
        n.addEventListener('click', () => {
          const ref = n.dataset.ref || `知识库《${n.dataset.refDoc}》`;
          state.refs.push(ref);
          input.value = `${input.value}${input.value ? ' ' : ''}@${ref} `;
          updateCtxHint();
          document.querySelector('.modal-mask')?.remove();
          input.focus();
        })
      );
    },
    actions: [{ label: '关闭' }],
  });
}

async function openSkills() {
  const { tools } = await api.get('/settings/tools').catch(() => ({ tools: [] }));
  const body = el('div');
  body.innerHTML = `<div class="col" style="gap:6px">
    ${tools.map((t) => `<label class="list-item" style="cursor:pointer">
      <input type="checkbox" style="width:14px;height:14px;accent-color:var(--brand)" data-skill="${esc(t.name)}" ${state.skills.includes(t.name) ? 'checked' : ''} />
      <div class="meta"><strong class="mono" style="font-size:12.5px">${esc(t.name)}</strong><span>${esc(t.description)}</span></div>
      <span class="tag ${t.risk === 'read' ? 'outline' : 'orange'}">${t.risk === 'read' ? '只读' : '写'}</span>
    </label>`).join('')}
    <div class="tiny dim mt-8">勾选后本次会话只向模型下发这些工具。不勾选表示全部可用。</div>
  </div>`;
  modal({
    title: 'Skills 选择',
    wide: true,
    body,
    onMount: (root) => {
      root.querySelectorAll('[data-skill]').forEach((n) =>
        n.addEventListener('change', () => {
          state.skills = [...root.querySelectorAll('[data-skill]:checked')].map((x) => x.dataset.skill);
          updateCtxHint();
        })
      );
    },
    actions: [{ label: '完成', primary: true }],
  });
}

async function uploadFile() {
  const form = document.createElement('form');
  form.innerHTML = `<div class="field"><label>选择文件</label>
      <input type="file" name="file" accept=".csv,.json,.ndjson,.jsonl,.parquet,.xlsx" />
      <div class="hint">上传后自动建表到 demo.raw，并在对话中引用</div></div>
    <div class="field"><label>目标位置</label><input name="target" value="demo.raw" /></div>`;
  modal({
    title: '上传文件',
    body: form,
    actions: [
      { label: '取消' },
      {
        label: '上传并建表',
        primary: true,
        onClick: async () => {
          const file = form.querySelector('[name=file]').files[0];
          if (!file) return false;
          const fd = new FormData();
          fd.append('file', file);
          fd.append('target', form.querySelector('[name=target]').value);
          try {
            const out = await api.upload('/ingest/upload', fd);
            state.refs.push(out.ref);
            const input = state.root.querySelector('#input');
            input.value = `${input.value}${input.value ? ' ' : ''}@${out.ref} `;
            updateCtxHint();
            toast(`已接入 ${out.ref}（${out.row_count} 行）`, 'ok');
          } catch (e) {
            toast(e.message, 'err');
            return false;
          }
        },
      },
    ],
  });
}

/* ------------------------------------------------------------------ 发送 */
async function send() {
  const input = state.root.querySelector('#input');
  const text = input.value.trim();
  if (!text || state.sending) return;

  if (!state.convId) {
    try {
      const out = await api.post('/conversations', { mode: state.mode, chat_mode: state.chatMode });
      state.convId = out.conversation.id;
    } catch (err) {
      return toast(err.message, 'err');
    }
  }

  input.value = '';
  input.style.height = 'auto';
  state.messages.push({ role: 'user', content: text, meta: {}, created_at: new Date().toISOString() });
  state.refs = [];
  state.sending = true;
  renderMessages();

  try {
    const out = await api.post(`/conversations/${state.convId}/messages`, {
      content: text, mode: state.mode, chat_mode: state.chatMode, model: state.model || null,
    });
    state.sending = false;
    state.messages.push({ role: 'assistant', content: out.content, meta: out.meta, created_at: new Date().toISOString() });
    state.pending = out.approval || null;
    collectArtifacts();
    if (state.artifacts.length) state.previewIndex = state.artifacts.length - 1;
    renderMessages();
    renderPreview();
    await loadConversations();
  } catch (err) {
    state.sending = false;
    state.messages.push({ role: 'assistant', content: `请求失败：${err.message}`, meta: { engine: 'system' }, created_at: new Date().toISOString() });
    renderMessages();
  }
}
