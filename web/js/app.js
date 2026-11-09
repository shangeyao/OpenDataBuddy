// 控制台外壳：顶栏 / 侧边栏 / 路由 / 会话
import { api, session, fmt } from './api.js';
import { $, el, esc, toast, modal, confirmDialog, formModal } from './ui.js';
import { icon } from './icons.js';
import { renderBuddy } from './buddy.js';

import { renderOverview } from './pages-overview.js';
import { renderCatalog, renderIngest, renderSql, renderSources } from './pages-data.js';
import { renderStudio, renderFlows, renderCicd, renderOps, renderCompute } from './pages-eng.js';
import { renderExperiments, renderRegistry, renderServing, renderAgents, renderMcp, renderModelList } from './pages-ai.js';
import { renderSemantic, renderQuality, renderSecurity, renderAnalysis, renderDashboards } from './pages-gov.js';
import { renderWorkspaces, renderConsoleUsers, renderPlatform, renderMembers, renderSettings } from './pages-sys.js';

const WS_ROUTES = {
  overview: { title: '工作空间概览', desc: '资源概况、近期运行动态与快捷入口', icon: 'overview', render: renderOverview },
  buddy: { title: 'Buddy', desc: 'Agent 原生数据智能体 · 对话即操作', icon: 'buddy', render: renderBuddy, full: true },
  ingest: { title: '数据接入', desc: '文件到表、离线/实时同步与数据源接入', icon: 'ingest', render: renderIngest },
  studio: { title: '开发工作台', desc: 'Notebook、SQL IDE 与 AI 代码助手', icon: 'studio', render: renderStudio },
  flows: { title: '工作流编排', desc: '可视化 DAG、依赖与调度配置', icon: 'flows', render: renderFlows },
  cicd: { title: '发布管理', desc: '配置即代码，Bundle 校验与多环境发布', icon: 'cicd', render: renderCicd },
  ops: { title: '运维监控', desc: '四层运行视图、日志与 AI 诊断', icon: 'ops', render: renderOps },
  exp: { title: '模型实验', desc: '实验跟踪、指标对比与超参记录', icon: 'experiment', render: renderExperiments },
  registry: { title: '模型管理', desc: '模型注册、版本与血缘', icon: 'registry', render: renderRegistry },
  serving: { title: '模型服务', desc: '在线服务、副本与启停', icon: 'serving', render: renderServing },
  sql: { title: '数据探索', desc: 'SQL 直查与结果可视化', icon: 'explore', render: renderSql },
  dashboards: { title: '仪表盘', desc: '看板搭建、卡片渲染与分享', icon: 'dashboards', render: renderDashboards },
  analysis: { title: '分析空间', desc: '面向业务方的分析空间与资产绑定', icon: 'analysis', render: renderAnalysis },
  semantic: { title: '统一语义层', desc: '本体建模、指标口径与维度定义', icon: 'semantic', render: renderSemantic },
  quality: { title: '数据质量', desc: '规则引擎、执行结果与异常检测', icon: 'quality', render: renderQuality },
  catalog: { title: '数据目录', desc: 'Catalog.Schema.Table 三级资产与血缘', icon: 'catalog', render: renderCatalog },
  security: { title: 'Agent 安全防护', desc: '注入检测、三要素拦截与权限绑定', icon: 'security', render: renderSecurity },
  agents: { title: '智能体', desc: '内置与自定义 Agent', icon: 'agents', render: renderAgents },
  mcp: { title: 'MCP 服务', desc: 'Model Context Protocol 工具暴露', icon: 'mcp', render: renderMcp },
  modellist: { title: '模型列表', desc: '多模型接入与 Endpoint 管理', icon: 'modellist', render: renderModelList },
  sources: { title: '数据源管理', desc: '连接注册、连通性测试与关联任务', icon: 'sources', render: renderSources },
  compute: { title: '计算资源', desc: '四类集群、CU 配额与网络设置', icon: 'compute', render: renderCompute },
  members: { title: '成员与权限', desc: '工作空间成员与角色分配', icon: 'users', render: renderMembers },
  settings: { title: '工作空间设置', desc: '通知渠道、Git 连接与偏好', icon: 'settings', render: renderSettings },
};

const CONSOLE_ROUTES = {
  workspaces: { title: '工作空间', desc: '管理工作空间及其资源状态', icon: 'workspaces', render: renderWorkspaces },
  'console-users': { title: '用户管理', desc: '控制台用户与全局角色', icon: 'users', render: renderConsoleUsers },
  platform: { title: '平台设置', desc: '模型接入、平台信息与审计', icon: 'platform', render: renderPlatform },
};

const NAV_GROUPS = [
  { items: ['overview'] },
  { label: '智能体', items: ['buddy'] },
  { label: '数据接入', items: ['ingest'] },
  { label: '数据工程', items: ['studio', 'flows', 'cicd', 'ops'] },
  { label: '数据科学', items: ['exp', 'registry', 'serving'] },
  { label: '数据分析', items: ['sql', 'dashboards', 'analysis'] },
  { label: '数据治理', items: ['semantic', 'quality', 'catalog', 'security'] },
  { label: '数据应用', items: ['agents', 'mcp', 'modellist'] },
  { label: '平台管理', items: ['sources', 'compute', 'members', 'settings'] },
];

const CONSOLE_NAV = [{ items: ['workspaces', 'console-users', 'platform'] }];

let current = '';
let navKind = 'ws';

function routeOf(key) {
  return WS_ROUTES[key] || CONSOLE_ROUTES[key] || null;
}

function setHead({ crumb, title, desc, actions }) {
  const crumbHost = $('#crumb');
  crumbHost.innerHTML = (crumb || [])
    .map((c, i) =>
      i === crumb.length - 1
        ? `<span>${esc(c.text)}</span>`
        : `<a href="${esc(c.href || '#')}">${esc(c.text)}</a><span class="sep">/</span>`
    )
    .join('');
  $('#page-title').textContent = title || '';
  $('#page-desc').textContent = desc || '';
  const host = $('#page-actions');
  host.innerHTML = '';
  if (actions) host.append(actions);
}

function buildNav(kind) {
  navKind = kind;
  const host = $('#nav');
  const groups = kind === 'console' ? CONSOLE_NAV : NAV_GROUPS;
  const routes = kind === 'console' ? CONSOLE_ROUTES : WS_ROUTES;
  host.innerHTML = '';
  for (const g of groups) {
    const box = el('div', { class: 'nav-group' });
    if (g.label) box.append(el('div', { class: 'nav-label' }, g.label));
    for (const key of g.items) {
      const r = routes[key];
      if (!r) continue;
      const a = el('a', { href: `#/${key}`, dataset: { key } });
      a.innerHTML = `<span class="ico">${icon(r.icon, 16)}</span><span>${esc(r.title)}</span>`;
      box.append(a);
    }
    host.append(box);
  }
}

function markActive(key) {
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.key === key));
  $('#tb-console').classList.toggle('active', !!CONSOLE_ROUTES[key]);
  $('#tb-buddy').classList.toggle('active', key === 'buddy');
}

function currentWs() {
  return session.workspaces.find((w) => w.id === session.wsId) || session.workspaces[0] || null;
}

async function navigate(force = false) {
  const raw = (location.hash || '#/overview').replace(/^#\//, '') || 'overview';
  const seg = raw.split('/');
  let key = seg[0];
  if (!routeOf(key)) key = session.wsId ? 'overview' : 'workspaces';
  const isConsole = !!CONSOLE_ROUTES[key];
  if (!isConsole && !session.workspaces.length) key = 'workspaces';

  const kind = CONSOLE_ROUTES[key] ? 'console' : 'ws';
  if (kind !== navKind) buildNav(kind);
  if (!force && key === current && $('#page').dataset.rendered === key) {
    markActive(key);
    return;
  }
  current = key;
  markActive(key);

  const route = routeOf(key);
  const ws = currentWs();
  const crumb = kind === 'console'
    ? [{ text: '控制台', href: '#/workspaces' }, { text: route.title }]
    : [{ text: '工作空间', href: '#/workspaces' }, { text: ws ? ws.name : '-', href: '#/overview' }, { text: route.title }];

  setHead({ crumb, title: route.title, desc: route.desc });
  $('#page-head').style.display = route.full ? 'none' : '';
  const pageEl = $('#page');
  pageEl.dataset.rendered = '';
  pageEl.innerHTML = '';
  pageEl.style.padding = route.full ? '0' : '';

  const ctx = {
    key,
    ws,
    setHead: (cfg) => setHead({ crumb, ...cfg }),
    actions: (node) => setHead({ crumb, title: route.title, desc: route.desc, actions: node }),
    navigate,
    refresh: () => navigate(true),
  };
  try {
    await route.render(pageEl, ctx);
    pageEl.dataset.rendered = key;
  } catch (err) {
    console.error(err);
    pageEl.innerHTML = `<div class="card"><div class="tag red">页面加载失败</div>
      <pre class="code" style="margin-top:12px">${esc(err.stack || err.message || String(err))}</pre></div>`;
  }
  window.scrollTo({ top: 0 });
}

function renderUserInfo() {
  const user = session.user;
  $('#user-menu').textContent = (user.name || user.email).slice(0, 1).toUpperCase();
  const ws = currentWs();
  $('#ws-name').textContent = ws ? ws.name : '未选择空间';
  $('#tb-title').textContent = '大数据智能体工作台';
}

function bindTopbar() {
  $('#tb-docs').addEventListener('click', () => window.open('/api/docs', '_blank'));
  $('#tb-msg').addEventListener('click', async () => {
    try {
      const { conversations } = await api.get('/conversations');
      modal({
        title: '消息与提醒',
        body: conversations.length
          ? `<div class="col" style="gap:8px">${conversations
              .slice(0, 8)
              .map((c) => `<a class="list-item" href="#/buddy/${c.id}" style="text-decoration:none;color:inherit">
                <div class="meta"><strong>${esc(c.title || '新会话')}</strong><span>${esc(c.mode)} · ${c.message_count} 条消息</span></div>
                <span class="dim tiny">${fmt.ago(c.updated_at)}</span></a>`)
              .join('')}</div>`
          : '<div class="empty">暂无新消息</div>',
        actions: [{ label: '关闭', primary: true }],
      });
    } catch (e) {
      toast(e.message, 'err');
    }
  });
  $('#tb-help').addEventListener('click', () => {
    modal({
      title: '帮助',
      wide: true,
      body: `<div class="prose">
        <p><b>这是什么</b>：开源版大数据智能体工作台，能力对齐 DataBuddy，去掉商业限制与云依赖。</p>
        <ul>
          <li>数据落在本机 <code>data/</code>：元数据库 + DuckDB 湖仓文件</li>
          <li>模型接入在「平台设置 → 模型接入」，默认 DeepSeek</li>
          <li>Agent 写操作若触及敏感字段会先弹确认</li>
        </ul>
        <p><b>快捷操作</b>：<code>Ctrl/Cmd + Enter</code> 运行 SQL；Buddy 中 <code>Enter</code> 发送、<code>Shift + Enter</code> 换行。</p>
        <p>API 文档：<a href="/api/docs" target="_blank">/api/docs</a></p>
      </div>`,
      actions: [{ label: '知道了', primary: true }],
    });
  });
  $('#user-menu').addEventListener('click', () => {
    const user = session.user;
    modal({
      title: '账号',
      body: `<div class="col" style="gap:14px">
        <div class="row"><span class="dim" style="width:72px">姓名</span><span>${esc(user.name)}</span></div>
        <div class="row"><span class="dim" style="width:72px">邮箱</span><span>${esc(user.email)}</span></div>
        <div class="row"><span class="dim" style="width:72px">角色</span><span>${user.role === 'admin' ? '管理员' : '成员'}</span></div>
        <div class="divider"></div>
        <div class="row"><button class="btn sm" id="m-pwd">修改密码</button><button class="btn sm danger" id="m-logout">退出登录</button></div>
      </div>`,
      onMount: (root, close) => {
        root.querySelector('#m-pwd').addEventListener('click', async () => {
          close();
          const data = await formModal({
            title: '修改密码',
            fields: [
              { label: '当前密码', name: 'old', type: 'password', required: true },
              { label: '新密码', name: 'next', type: 'password', required: true, hint: '至少 6 位' },
            ],
            onSubmit: async (d) => {
              await api.post('/auth/password', { old_password: d.old, new_password: d.next });
              toast('密码已修改', 'ok');
            },
          });
          return data;
        });
        root.querySelector('#m-logout').addEventListener('click', async () => {
          close();
          try { await api.post('/auth/logout'); } catch { /* ignore */ }
          session.clear();
          location.hash = '';
          showLogin();
        });
      },
      actions: [{ label: '关闭' }],
    });
  });
  $('#ws-picker').addEventListener('click', openWsSwitcher);
}

async function openWsSwitcher() {
  let list = session.workspaces;
  try {
    const out = await api.get('/console/workspaces');
    list = out.workspaces;
  } catch { /* 用缓存 */ }
  const body = el('div');
  body.innerHTML = `<div class="col" style="gap:6px">${list
    .map(
      (w) => `<div class="list-item" data-ws="${esc(w.id)}" style="cursor:pointer">
        <div class="meta"><strong>${esc(w.name)}</strong><span>${w.tables ?? 0} 张表 · ${w.members ?? 0} 名成员 · ${esc(w.role)}</span></div>
        ${w.id === session.wsId ? '<span class="tag blue">当前</span>' : ''}
      </div>`
    )
    .join('')}</div>
    <div class="divider"></div>
    <a class="btn block" href="#/workspaces">管理工作空间</a>`;
  const d = modal({ title: '切换工作空间', body, actions: [{ label: '关闭' }] });
  body.querySelectorAll('[data-ws]').forEach((n) =>
    n.addEventListener('click', () => {
      session.setWorkspace(n.dataset.ws);
      document.querySelector('.modal-mask')?.remove();
      renderUserInfo();
      navigate(true);
      toast('已切换工作空间');
    })
  );
  return d;
}

function showLogin(message) {
  $('#app').classList.add('hidden');
  $('#login-screen').classList.remove('hidden');
  $('#login-error').textContent = message || '';
}

async function showApp() {
  $('#login-screen').classList.add('hidden');
  $('#app').classList.remove('hidden');
  renderUserInfo();
  buildNav('ws');
  await navigate(true);
}

async function boot() {
  bindTopbar();
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
      await showApp();
      toast('登录成功', 'ok');
    } catch (err) {
      $('#login-error').textContent = err.message;
    }
  });

  window.addEventListener('hashchange', () => navigate());
  window.addEventListener('odb:refresh', () => navigate(true));
  window.addEventListener('odb:logout', () => showLogin('会话已过期，请重新登录'));

  if (!session.token) return showLogin();
  try {
    const me = await api.get('/auth/me');
    session.user = me.user;
    session.workspaces = me.workspaces || [];
    if (!session.workspaces.length) {
      session.setWorkspace('');
      location.hash = '#/workspaces';
    } else if (!session.wsId) {
      session.setWorkspace(session.workspaces[0].id);
    }
    await showApp();
  } catch (err) {
    session.clear();
    showLogin(err.status === 401 ? '' : err.message);
  }
}

boot();
