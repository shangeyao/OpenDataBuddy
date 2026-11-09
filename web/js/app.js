// 应用外壳：登录、导航、路由
import { api, session, fmt } from './api.js';
import { $, el, esc, toast, modal, confirmDialog } from './ui.js';
import { renderBuddy } from './buddy.js';
import { icon } from './icons.js';
import * as pages from './pages.js';

const ROUTES = {
  overview: { title: '总览', sub: '工作空间健康度与最近动态', render: pages.renderOverview, group: '首页' },
  buddy: { title: 'Buddy', sub: '数据智能体 · 对话即操作', render: renderBuddy, group: '智能体' },
  catalog: { title: '数据目录', sub: 'Catalog / Schema / Table 三级资产', render: pages.renderCatalog, group: '数据' },
  ingest: { title: '数据接入', sub: '文件上传、SQL 建表、数据源连通性', render: pages.renderIngest, group: '数据' },
  sql: { title: 'SQL 工作台', sub: '只读查询与结果可视化', render: pages.renderSql, group: '数据' },
  flows: { title: '工作流', sub: 'DAG 编排、调度与运行监控', render: pages.renderFlows, group: '研发' },
  dashboards: { title: '仪表盘', sub: '看板搭建与卡片渲染', render: pages.renderDashboards, group: '分析' },
  semantic: { title: '语义层', sub: '指标口径、维度与统一算法', render: pages.renderSemantic, group: '分析' },
  governance: { title: '数据治理', sub: '敏感识别、分级打标、质量规则', render: pages.renderGovernance, group: '治理' },
  knowledge: { title: '知识库', sub: '术语、口径、规范与元数据文档', render: pages.renderKnowledge, group: '治理' },
  settings: { title: '设置', sub: '模型接入、成员、审计与平台信息', render: pages.renderSettings, group: '系统' },
};

const NAV = [
  { label: '首页', items: ['overview'] },
  { label: '智能体', items: ['buddy'] },
  { label: '数据', items: ['catalog', 'ingest', 'sql'] },
  { label: '研发', items: ['flows'] },
  { label: '分析', items: ['dashboards', 'semantic'] },
  { label: '治理', items: ['governance', 'knowledge'] },
  { label: '系统', items: ['settings'] },
];

const ICONS = {
  overview: 'overview', buddy: 'buddy', catalog: 'catalog', ingest: 'ingest', sql: 'sql',
  flows: 'flows', dashboards: 'dashboards', semantic: 'semantic', governance: 'governance',
  knowledge: 'knowledge', settings: 'settings',
};

let currentRoute = 'overview';

function setPage(title, sub, actionsNode) {
  $('#page-title').textContent = title;
  $('#page-sub').textContent = sub || '';
  const host = $('#page-actions');
  host.innerHTML = '';
  if (actionsNode) host.append(actionsNode);
}

function buildNav() {
  const nav = $('#nav');
  nav.innerHTML = '';
  for (const group of NAV) {
    const box = el('div', { class: 'nav-group' });
    box.append(el('div', { class: 'nav-label' }, group.label));
    for (const key of group.items) {
      const route = ROUTES[key];
      if (!route) continue;
      const a = el('a', { href: `#/${key}`, dataset: { route: key } });
      a.innerHTML = `<span class="nv-ico">${icon(ICONS[key] || 'dot', 15)}</span><span>${esc(route.title)}</span>`;
      box.append(a);
    }
    nav.append(box);
  }
}

async function navigate(force = false) {
  const raw = (location.hash || '#/overview').replace(/^#\//, '') || 'overview';
  const seg = raw.split('/');
  const key = ROUTES[seg[0]] ? seg[0] : 'overview';
  if (!force && key === currentRoute && $('#page').dataset.rendered === key) return;
  currentRoute = key;
  const route = ROUTES[key];
  document.querySelectorAll('#nav a').forEach((a) => {
    a.classList.toggle('active', a.dataset.route === key);
  });
  setPage(route.title, route.sub, null);
  const pageEl = $('#page');
  pageEl.dataset.rendered = '';
  pageEl.innerHTML = '';
  try {
    await route.render(pageEl, { setPage, actions: (node) => setPage(route.title, route.sub, node), navigate, refresh: () => navigate(true) });
    pageEl.dataset.rendered = key;
  } catch (err) {
    console.error(err);
    pageEl.innerHTML = `<div class="empty"><strong>页面加载失败</strong>${esc(err.message || err)}</div>`;
  }
}

function renderShell() {
  const user = session.user;
  $('#user-name').textContent = user.name || user.email;
  $('#user-role').textContent = user.role === 'admin' ? '管理员' : '成员';
  $('#user-avatar').textContent = (user.name || user.email).slice(0, 1).toUpperCase();
  const ws = session.workspaces.find((w) => w.id === session.wsId) || session.workspaces[0];
  if (ws && !session.wsId) session.setWorkspace(ws.id);
  $('#ws-name').textContent = ws ? ws.name : '未选择空间';
  $('#ws-current').textContent = ws ? ws.name : '未选择空间';
}

function showLogin(message) {
  $('#app').classList.add('hidden');
  $('#login-screen').classList.remove('hidden');
  $('#login-error').textContent = message || '';
}

function showApp() {
  $('#login-screen').classList.add('hidden');
  $('#app').classList.remove('hidden');
  buildNav();
  renderShell();
  navigate(true);
}

async function boot() {
  $('#logout').addEventListener('click', async () => {
    try { await api.post('/auth/logout'); } catch { /* ignore */ }
    session.clear();
    location.hash = '';
    showLogin('已退出登录');
  });

  $('#ws-switch').addEventListener('click', async () => {
    const workspaces = session.workspaces;
    const list = el('div', { class: 'col' });
    for (const w of workspaces) {
      const item = el('div', { class: `list-item ${w.id === session.wsId ? 'active' : ''}` });
      item.innerHTML = `<div class="meta"><strong>${esc(w.name)}</strong><span>${esc(w.description || '无描述')} · ${esc(w.role || 'member')}</span></div>`;
      item.addEventListener('click', () => {
        session.setWorkspace(w.id);
        document.querySelector('.modal-mask')?.remove();
        renderShell();
        navigate(true);
        toast(`已切换到 ${w.name}`);
      });
      list.append(item);
    }
    const form = el('div', { class: 'row', style: 'margin-top:12px;gap:8px' });
    const input = el('input', { placeholder: '新建工作空间名称' });
    const btn = el('button', { class: 'btn primary' }, '新建');
    btn.addEventListener('click', async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const out = await api.post('/workspaces', { name });
        const me = await api.get('/auth/me');
        session.workspaces = me.workspaces;
        session.setWorkspace(out.workspace.id);
        document.querySelector('.modal-mask')?.remove();
        renderShell();
        navigate(true);
        toast('工作空间已创建', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    });
    form.append(input, btn);
    list.append(el('div', { class: 'tiny muted', style: 'margin-top:14px' }, '切换工作空间会改变数据、指标与工作流的可见范围。'), form);
    modal({ title: '工作空间', body: list });
  });

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    $('#login-error').textContent = '';
    try {
      const out = await api.post('/auth/login', { email: fd.get('email'), password: fd.get('password') });
      session.setToken(out.token);
      session.user = out.user;
      session.workspaces = out.workspaces || [];
      if (!session.wsId && session.workspaces[0]) session.setWorkspace(session.workspaces[0].id);
      showApp();
      toast('欢迎回来', 'ok');
    } catch (err) {
      $('#login-error').textContent = err.message;
    }
  });

  window.addEventListener('odb:logout', () => showLogin('会话已过期，请重新登录'));
  window.addEventListener('odb:refresh', () => navigate(true));
  window.addEventListener('hashchange', () => navigate());

  if (!session.token) return showLogin();
  try {
    const me = await api.get('/auth/me');
    session.user = me.user;
    session.workspaces = me.workspaces || [];
    if (!session.workspaces.length) return showLogin('当前账号没有可访问的工作空间');
    showApp();
  } catch (err) {
    session.clear();
    showLogin(err.status === 401 ? '' : err.message);
  }
}

boot();
