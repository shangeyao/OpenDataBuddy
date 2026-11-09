// Buddy：数据智能体对话页
import { api, session, fmt } from './api.js';
import { el, esc, md, toast, empty } from './ui.js';
import { artifactHTML, traceHTML, engineBadge } from './artifact.js';

const MODES = [
  { id: 'engineering', label: '数据开发', desc: '接入、ETL、工作流、排障' },
  { id: 'governance', label: '数据治理', desc: '敏感识别、打标、质量' },
  { id: 'analysis', label: '数据分析', desc: '问数、归因、报告、看板' },
];

const SUGGESTIONS = {
  engineering: [
    { t: '建一条每日汇总工作流', d: '把订单明细聚合成每日销售汇总并配置 07:00 调度' },
    { t: '排查上次失败', d: '工作流「每日经营数据刷新」上次失败的原因是什么' },
    { t: '看看有哪些表', d: '列出当前空间的数据表与行数' },
  ],
  governance: [
    { t: '扫描敏感字段', d: '扫描 dim_customer 的敏感字段并给出分级与依据' },
    { t: '推荐质量规则', d: '给 fact_orders 推荐数据质量规则' },
    { t: '资产盘点', d: '盘点 fact_orders 的行数、空值率与质量得分' },
  ],
  analysis: [
    { t: '近 30 天 GMV 趋势', d: '按天看 GMV 走势，指出拐点' },
    { t: '渠道对比', d: '各渠道 GMV 排名与占比' },
    { t: '生成经营看板', d: '帮我生成一份零售经营 AI 看板' },
  ],
};

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
  modelOptions: [],
  container: null,
  ctx: null,
};

export async function renderBuddy(container, ctx) {
  state.container = container;
  state.ctx = ctx;
  container.innerHTML = `
    <div class="buddy">
      <div class="buddy-rail">
        <button class="btn primary block sm" id="new-conv">＋ 新建会话</button>
        <div class="mode-switch" id="mode-switch"></div>
        <div class="conv-list" id="conv-list"></div>
      </div>
      <div class="chat">
        <div class="chat-body" id="chat-body"></div>
        <div class="composer">
          <div class="composer-box">
            <textarea id="input" rows="2" placeholder="问数据、让我建任务、做治理…（Enter 发送 / Shift+Enter 换行）"></textarea>
            <div class="composer-foot">
              <select class="sel" id="chat-mode" title="对话模式">
                <option value="agent">Agent · 可操作</option>
                <option value="ask">Ask · 仅问答</option>
              </select>
              <select class="sel" id="model-sel" title="模型"></select>
              <span class="spacer"></span>
              <span class="hint-key" id="hint">Enter 发送</span>
              <button class="btn primary sm" id="send">发送</button>
            </div>
          </div>
        </div>
      </div>
    </div>`;

  buildModeSwitch();
  bindComposer();
  await loadSettings();
  await loadConversations();
  const deepLink = (location.hash.split('/')[2] || '').trim();
  if (deepLink) {
    await openConversation(deepLink);
  } else {
    renderMessages();
  }
}

function buildModeSwitch() {
  const host = state.container.querySelector('#mode-switch');
  host.innerHTML = '';
  for (const m of MODES) {
    const b = el('button', { title: m.desc, class: state.mode === m.id ? 'active' : '' }, m.label);
    b.addEventListener('click', async () => {
      if (state.mode === m.id) return;
      state.mode = m.id;
      state.convId = null;
      state.messages = [];
      state.pending = null;
      buildModeSwitch();
      renderMessages();
      await loadConversations();
    });
    host.append(b);
  }
}

function bindComposer() {
  const input = state.container.querySelector('#input');
  const sendBtn = state.container.querySelector('#send');
  const modeSel = state.container.querySelector('#chat-mode');
  modeSel.value = state.chatMode;
  modeSel.addEventListener('change', () => {
    state.chatMode = modeSel.value;
    state.container.querySelector('#hint').textContent =
      state.chatMode === 'ask' ? 'Ask 模式不会改数据' : 'Enter 发送';
  });
  const modelSel = state.container.querySelector('#model-sel');
  modelSel.addEventListener('change', () => {
    state.model = modelSel.value;
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
  });
  sendBtn.addEventListener('click', send);
  state.container.querySelector('#new-conv').addEventListener('click', () => {
    state.convId = null;
    state.messages = [];
    state.pending = null;
    renderMessages();
    input.focus();
  });
}

async function loadSettings() {
  try {
    const s = await api.get('/settings');
    state.llmReady = s.llm.configured;
    const sel = state.container.querySelector('#model-sel');
    const models = new Set(['']);
    (s.providers || []).forEach((p) => p.model && models.add(p.model));
    if (s.llm.model) models.add(s.llm.model);
    models.add('deepseek-reasoner');
    sel.innerHTML = [...models]
      .map((m) => `<option value="${esc(m)}">${m ? esc(m) : `默认（${esc(s.llm.model || '未配置')}）`}</option>`)
      .join('');
  } catch {
    /* 忽略：不影响对话 */
  }
}

async function loadConversations() {
  try {
    const out = await api.get(`/conversations?mode=${state.mode}`);
    state.conversations = out.conversations || [];
  } catch {
    state.conversations = [];
  }
  renderConvList();
}

function renderConvList() {
  const host = state.container.querySelector('#conv-list');
  host.innerHTML = '';
  if (!state.conversations.length) {
    host.innerHTML = `<div class="tiny muted" style="padding:8px 4px">还没有会话</div>`;
    return;
  }
  for (const c of state.conversations) {
    const item = el('div', { class: `conv-item ${c.id === state.convId ? 'active' : ''}` });
    item.innerHTML = `<span>${esc(c.title || '新会话')}</span>`;
    const del = el('button', { class: 'icon-btn del', title: '删除' }, '✕');
    del.addEventListener('click', async (e) => {
      e.stopPropagation();
      await api.del(`/conversations/${c.id}`);
      if (state.convId === c.id) {
        state.convId = null;
        state.messages = [];
        renderMessages();
      }
      await loadConversations();
    });
    item.append(del);
    item.addEventListener('click', () => openConversation(c.id));
    host.append(item);
  }
}

async function openConversation(id) {
  try {
    const out = await api.get(`/conversations/${id}`);
    state.convId = id;
    state.mode = out.conversation.mode || state.mode;
    state.chatMode = out.conversation.chat_mode || 'agent';
    state.messages = out.messages || [];
    state.pending = (out.pending_approvals || [])[0] || null;
    state.container.querySelector('#chat-mode').value = state.chatMode;
    buildModeSwitch();
    renderConvList();
    renderMessages();
  } catch (err) {
    toast(err.message, 'err');
  }
}

// ------------------------------------------------------------------ 渲染
function renderMessages() {
  const body = state.container.querySelector('#chat-body');
  if (!state.messages.length && !state.sending) {
    body.innerHTML = welcomeHTML();
    body.querySelectorAll('[data-fill]').forEach((node) => {
      node.addEventListener('click', () => {
        const input = state.container.querySelector('#input');
        input.value = node.dataset.fill;
        input.focus();
      });
    });
    return;
  }
  body.innerHTML = state.messages.map(messageHTML).join('');
  if (state.pending) {
    body.insertAdjacentHTML('beforeend', approvalHTML(state.pending));
    bindApproval(body);
  }
  if (state.sending) {
    body.insertAdjacentHTML(
      'beforeend',
      `<div class="msg"><span class="avatar">B</span><div class="body">
        <div class="msg-head">Buddy</div>
        <div class="thinking"><i></i><i></i><i></i><span style="margin-left:6px">正在思考并调用工具…</span></div>
      </div></div>`
    );
    body.scrollTop = body.scrollHeight;
    return;
  }
  // 长回答从开头读：把最后一条助手消息滚到视口顶部
  const answers = body.querySelectorAll('.msg:not(.user)');
  const last = answers[answers.length - 1];
  if (last && last.scrollHeight > body.clientHeight * 0.8) {
    last.scrollIntoView({ block: 'start' });
  } else {
    body.scrollTop = body.scrollHeight;
  }
}

function welcomeHTML() {
  const meta = MODES.find((m) => m.id === state.mode);
  const cards = SUGGESTIONS[state.mode] || [];
  return `<div style="max-width:760px;margin:8px auto 0">
    <h3 style="font-size:19px">你好，我是 Buddy</h3>
    <p class="muted" style="margin-top:6px;font-size:13px">
      当前模式：<b>${esc(meta.label)}</b> · ${esc(meta.desc)}。
      ${state.llmReady ? '已接入大模型，可多步调用工具完成任务。' : '尚未配置模型，将使用内置规则引擎（可问数、扫描、建任务）。'}
    </p>
    <div class="suggest-grid" style="margin-top:18px">
      ${cards
        .map(
          (c) => `<button class="suggest" data-fill="${esc(c.d)}">
            <b>${esc(c.t)}</b><span>${esc(c.d)}</span></button>`
        )
        .join('')}
    </div>
    <p class="tiny muted" style="margin-top:18px">
      提示：我会先看数据再回答；涉及改口径、建表、打标等写操作时，会先请你确认。
    </p>
  </div>`;
}

function messageHTML(m) {
  const isUser = m.role === 'user';
  const meta = m.meta || {};
  const artifacts = (meta.artifacts || []).map(artifactHTML).join('');
  const notice = meta.note
    ? `<div class="tiny" style="margin-top:8px;color:var(--warn)">${esc(meta.note)}</div>`
    : '';
  return `<div class="msg ${isUser ? 'user' : ''}">
    <span class="avatar">${isUser ? esc((session.user?.name || 'U').slice(0, 1)) : 'B'}</span>
    <div class="body">
      <div class="msg-head">${isUser ? esc(session.user?.name || '我') : 'Buddy'}
        <span>· ${esc(fmt.ago(m.created_at))}</span>
        ${isUser ? '' : engineBadge(meta)}
      </div>
      <div class="prose">${md(m.content)}</div>
      ${traceHTML(meta.trace)}
      ${artifacts}
      ${notice}
    </div>
  </div>`;
}

function approvalHTML(a) {
  return `<div class="approval" id="approval-box">
    <h4>⚠ 需要你确认后才能继续</h4>
    <p class="tiny" style="margin-top:6px">${esc(a.reason || '该操作会修改平台资产')}</p>
    <ul class="factors">${(a.factors || []).map((f) => `<li>${esc(f)}</li>`).join('')}</ul>
    <div class="tiny muted">将执行：<span class="mono">${esc(a.tool)}</span></div>
    <div class="args mono">${esc(JSON.stringify(a.args || {}, null, 2))}</div>
    <div class="acts">
      <button class="btn primary sm" data-approve="1">确认执行</button>
      <button class="btn sm" data-reject="1">取消这一步</button>
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
      renderMessages();
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
      renderMessages();
    } catch (err) {
      toast(err.message, 'err');
    }
  });
}

// ------------------------------------------------------------------ 发送
async function send() {
  const input = state.container.querySelector('#input');
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
  state.sending = true;
  renderMessages();

  try {
    const out = await api.post(`/conversations/${state.convId}/messages`, {
      content: text,
      mode: state.mode,
      chat_mode: state.chatMode,
      model: state.model || null,
    });
    state.sending = false;
    state.messages.push({
      role: 'assistant',
      content: out.content,
      meta: out.meta,
      created_at: new Date().toISOString(),
    });
    state.pending = out.approval || null;
    renderMessages();
    await loadConversations();
  } catch (err) {
    state.sending = false;
    state.messages.push({
      role: 'assistant',
      content: `请求失败：${err.message}`,
      meta: { engine: 'system' },
      created_at: new Date().toISOString(),
    });
    renderMessages();
  }
}
