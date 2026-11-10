// 平台与系统：工作空间列表 / 控制台用户 / 平台设置 / 成员权限 / 工作空间设置
import { api, session, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  modal, confirmDialog, formModal, toast, drawer, esc, descList, el,
} from './ui.js';

/* ------------------------------------------------------------ 工作空间列表 */
export async function renderWorkspaces(page, ctx) {
  page.innerHTML = loading();
  const { workspaces } = await api.get('/console/workspaces');
  ctx.setHead({
    crumb: [{ text: '控制台' }, { text: '工作空间' }],
    title: '工作空间',
    desc: '工作空间是用户、资源与任务的最小集合',
    actions: el('button', { class: 'btn primary', onclick: () => newWorkspace(ctx) }, '＋ 新建工作空间'),
  });

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">工作空间</span><span class="value">${workspaces.length}</span><span class="foot">按团队 / 项目 / 环境划分</span></div>
      <div class="stat"><span class="label">数据表</span><span class="value">${workspaces.reduce((a, w) => a + w.tables, 0)}</span><span class="foot">跨空间合计</span></div>
      <div class="stat"><span class="label">成员</span><span class="value">${workspaces.reduce((a, w) => a + w.members, 0)}</span><span class="foot">无席位限制</span></div>
      <div class="stat"><span class="label">地域</span><span class="value" style="font-size:18px">本地</span><span class="foot"><span class="tag green">无地域锁定</span></span></div>
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">工作空间列表</span>
        <span class="dim tiny">删除工作空间不会删除 Catalog 中的数据资产</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      ${dataTable({
        rows: workspaces,
        columns: [
          { title: '名称', render: (w) => `<div class="cell-main">${esc(w.name)}</div><div class="cell-sub">${esc(w.description || '无描述')}</div>` },
          { title: '状态', render: () => statusTag('running') },
          { title: '数据表', align: 'right', width: '90px', render: (w) => w.tables },
          { title: '成员', align: 'right', width: '80px', render: (w) => w.members },
          { title: '我的角色', render: (w) => tag(w.role === 'admin' ? '空间管理员' : '空间成员', w.role === 'admin' ? 'blue' : 'outline') },
          { title: '创建时间', render: (w) => `<span class="dim">${fmt.dt(w.created_at)}</span>` },
          { title: '操作', key: 'ops', render: (w) => `<a class="btn-link" data-enter="${w.id}">进入</a>` },
        ],
        emptyText: '还没有工作空间', emptyHint: '点击右上角新建',
      })}
    </div>`;

  page.querySelectorAll('[data-enter]').forEach((b) =>
    b.addEventListener('click', () => {
      session.setWorkspace(b.dataset.enter);
      toast('已进入工作空间');
      location.hash = '#/overview';
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function newWorkspace(ctx) {
  const data = await formModal({
    title: '新建工作空间',
    fields: [
      { label: '工作空间名称', name: 'name', required: true, placeholder: '例如 demo_retail_dw', hint: '≤ 128 字符，会作为同名 Catalog 名称' },
      { label: '描述', name: 'description', placeholder: '例如 零售数仓 POC 环境' },
    ],
    onSubmit: async (d) => {
      const out = await api.post('/workspaces', d);
      const me = await api.get('/auth/me');
      session.workspaces = me.workspaces || [];
      session.setWorkspace(out.workspace.id);
      toast('工作空间已创建', 'ok');
    },
  });
  if (data) {
    await ctx.refresh();
    location.hash = '#/overview';
  }
}

/* ------------------------------------------------------------ 控制台用户 */
export async function renderConsoleUsers(page, ctx) {
  page.innerHTML = loading();
  const { users } = await api.get('/users');
  const canAdmin = session.user?.role === 'admin';
  ctx.setHead({
    crumb: [{ text: '控制台' }, { text: '用户管理' }],
    title: '用户管理',
    desc: '本地账号体系，替代云 CAM 子账号',
    actions: canAdmin ? el('button', { class: 'btn primary', onclick: () => newUser(ctx) }, '＋ 添加用户') : null,
  });

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">本地账号</span>
      不依赖云账号 / 企业实名 / CAM 策略。用户加一次即生效（原产品需要「控制台用户」与「工作空间用户」加两次）。
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">用户列表</span><span class="dim tiny">${users.length} 人</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      ${dataTable({
        rows: users,
        columns: [
          { title: '用户', render: (u) => `<div class="row" style="gap:8px"><span class="tb-avatar" style="margin:0;width:24px;height:24px;font-size:11px">${esc((u.name || u.email).slice(0, 1))}</span>
            <div><div class="cell-main">${esc(u.name)}</div><div class="cell-sub">${esc(u.email)}</div></div></div>` },
          { title: '全局角色', render: (u) => tag(u.role === 'admin' ? '管理员' : '成员', u.role === 'admin' ? 'blue' : 'outline') },
          { title: '创建时间', render: (u) => `<span class="dim">${fmt.dt(u.created_at)}</span>` },
          { title: '操作', key: 'ops', render: (u) => u.id === session.user.id
              ? '<span class="dim tiny">当前登录</span>'
              : `<a class="btn-link danger" data-del="${u.id}">移除</a>` },
        ],
        emptyText: '没有用户',
      })}
    </div>`;

  page.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('移除用户', '移除后该用户无法登录，确认？', '移除'))) return;
      await api.del(`/users/${b.dataset.del}`);
      ctx.refresh();
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function newUser(ctx) {
  const data = await formModal({
    title: '添加用户',
    fields: [
      { label: '邮箱', name: 'email', required: true, placeholder: 'name@company.com' },
      { label: '姓名', name: 'name', required: true },
      { label: '初始密码', name: 'password', value: 'databuddy123', hint: '建议首次登录后修改' },
      { label: '全局角色', name: 'role', type: 'select', options: [
        { value: 'member', label: '成员' }, { value: 'admin', label: '管理员' }] },
    ],
    onSubmit: async (d) => { await api.post('/users', d); toast('用户已创建并加入当前工作空间', 'ok'); },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- 平台设置 */
export async function renderPlatform(page, ctx) {
  page.innerHTML = loading();
  const [settings, usage, audit, toolsOut, trace] = await Promise.all([
    api.get('/settings'),
    api.get('/usage').catch(() => null),
    api.get('/ops/audit?limit=20').catch(() => ({ logs: [] })),
    api.get('/settings/tools').catch(() => ({ tools: [] })),
    api.get('/ops/traces').catch(() => null),
  ]);
  const canAdmin = session.user?.role === 'admin';
  const llm = settings.llm;

  ctx.setHead({
    crumb: [{ text: '控制台' }, { text: '平台设置' }],
    title: '平台设置',
    desc: '模型接入、本地用量与审计',
    actions: null,
  });

  page.innerHTML = `
    <div class="grid-2" style="grid-template-columns:1fr 380px;align-items:start">
      <div class="col" style="gap:16px">
        ${card('模型接入', `
          <div class="field"><label>服务商</label><select id="provider">
            ${(settings.providers || []).map((p) => `<option value="${p.id}" ${p.id === llm.provider ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}
          </select></div>
          <div class="field"><label>Base URL</label><input id="base" value="${esc(llm.base_url)}" /></div>
          <div class="field"><label>模型</label><input id="model" value="${esc(llm.model)}" /></div>
          <div class="field"><label>API Key</label><input id="key" type="password" placeholder="${esc(llm.api_key_masked || '未配置')}" />
            <div class="hint">DeepSeek 默认 https://api.deepseek.com/v1，模型 deepseek-chat / deepseek-reasoner</div></div>
          <div class="row">
            <button class="btn primary" id="save-llm" ${canAdmin ? '' : 'disabled'}>保存</button>
            <button class="btn" id="test-llm" ${canAdmin ? '' : 'disabled'}>测试连接</button>
            <span class="tag ${llm.configured ? 'green' : 'orange'}">${llm.configured ? '已配置' : '未配置，使用规则引擎'}</span>
          </div>
          <div id="llm-out" class="mt-12"></div>`, { sub: 'DeepSeek 为默认底座，兼容任意 OpenAI 协议端点' })}

        ${card('平台信息', descList([
          ['版本', settings.platform.version],
          ['数据目录', `<span class="mono tiny">${esc(settings.platform.data_dir)}</span>`],
          ['登录体系', '本地账号（无 CAM / 企业实名）'],
          ['计费', '<span class="tag green">已移除</span>'],
          ['席位限制', '<span class="tag green">无限制</span>'],
          ['地域锁定', '<span class="tag green">无</span>'],
          ['API 文档', '<a href="/api/docs" target="_blank">/api/docs</a>'],
        ]), { sub: '与原商业版的差异见 docs/OSS_DIFF.md' })}

        ${card('审计日志', audit.logs?.length
          ? tableHTML(['时间', '用户', '动作', '对象'], audit.logs.map((l) => [fmt.dt(l.created_at), l.user_name || '-', l.action, l.target || '-']), { raw: [] })
          : emptyState('暂无日志'), { flush: true })}
      </div>

      <div class="col" style="gap:16px">
        ${card('本地用量', usage ? `<div class="col" style="gap:12px">
          ${[
            ['成员', `${usage.seats.used} 人`, '无席位上限'],
            ['计算资源', `${usage.compute.clusters} 个 / ${usage.compute.cu_total} CU`, '本地运行'],
            ['存储占用', fmt.bytes(usage.storage.bytes), usage.storage.free_bytes == null ? '' : `剩余 ${fmt.bytes(usage.storage.free_bytes)}`],
            ['数据规模', `${usage.data.tables} 表 / ${fmt.num(usage.data.rows)} 行`, ''],
            ['Agent 调用', `${usage.agent.calls} 次`, `${usage.agent.conversations} 个会话`],
          ].map(([k, v, sub]) => `<div class="row" style="justify-content:space-between">
            <span class="dim tiny">${k}</span>
            <span style="text-align:right"><b>${v}</b>${sub ? `<div class="tiny dim">${sub}</div>` : ''}</span></div>`).join('')}
          <div class="divider"></div>
          <div class="row"><span class="tag green">计费已移除</span><span class="tiny dim">本平台不产生任何费用</span></div>
        </div>` : emptyState('无数据'), { sub: '服务订阅改为本地资源视图' })}

        ${card('轨迹评测', trace?.calls ? `<div class="col" style="gap:8px">
          <div class="row" style="justify-content:space-between"><span class="dim tiny">调用次数</span><b>${trace.calls}</b></div>
          <div class="row" style="justify-content:space-between"><span class="dim tiny">成功率</span><b>${fmt.pct(trace.success_rate)}</b></div>
          <div class="row" style="justify-content:space-between"><span class="dim tiny">平均耗时</span><b>${trace.avg_ms} ms</b></div>
          <div class="divider"></div>
          ${tableHTML(['工具', '调用', '成功率'], trace.tools.slice(0, 6).map((t) => [t.tool, t.calls, fmt.pct(t.success_rate)]), { raw: [] })}
        </div>` : emptyState('还没有工具调用', '和 Buddy 聊两句就会产生'), { flush: false })}

        ${card('Agent 工具', `<div class="row wrap" style="gap:6px">${(toolsOut.tools || []).map((t) =>
          `<span class="tag ${t.risk === 'read' ? 'outline' : 'orange'}" title="${esc(t.description)}">${esc(t.name)}</span>`).join('')}</div>`,
          { sub: `${(toolsOut.tools || []).length} 个工具，按场景模式下发` })}
      </div>
    </div>`;

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
      ctx.refresh();
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
        ? `<div class="tag green">连接成功 · ${esc(r.model)}</div><div class="tiny dim mt-8">${esc(r.reply)}</div>`
        : `<div class="tag red">失败</div><div class="tiny mt-8">${esc(r.error)}</div>`;
    } catch (err) {
      out.innerHTML = `<div class="tag red">失败</div><div class="tiny mt-8">${esc(err.message)}</div>`;
    } finally {
      e.target.disabled = false;
    }
  });
}

/* ------------------------------------------------------------ 成员与权限 */
export async function renderMembers(page, ctx) {
  page.innerHTML = loading();
  const wsId = session.wsId;
  const [{ members }, { users }] = await Promise.all([
    api.get(`/workspaces/${wsId}/members`),
    api.get('/users').catch(() => ({ users: [] })),
  ]);
  const canAdmin = session.user?.role === 'admin';

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">${esc(ctx.ws?.name || '')}</span>
      空间角色：<b>空间管理员</b>可管理成员与全部功能；<b>空间成员</b>可使用除空间管理外的功能。
      <span class="spacer"></span><span class="dim tiny">无 Agent 席位限制</span>
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">空间成员</span><span class="dim tiny">${members.length} 人</span>
        <span class="spacer"></span>
        ${canAdmin ? '<button class="btn sm primary" id="add-member">＋ 添加成员</button>' : ''}</div>
      ${dataTable({
        rows: members,
        columns: [
          { title: '成员', render: (m) => `<div class="row" style="gap:8px"><span class="tb-avatar" style="margin:0;width:24px;height:24px;font-size:11px">${esc((m.name || m.email).slice(0, 1))}</span>
            <div><div class="cell-main">${esc(m.name)}</div><div class="cell-sub">${esc(m.email)}</div></div></div>` },
          { title: '全局角色', render: (m) => tag(m.global_role === 'admin' ? '管理员' : '成员', 'outline') },
          { title: '空间角色', render: (m) => tag(m.space_role === 'admin' ? '空间管理员' : '空间成员', m.space_role === 'admin' ? 'blue' : '') },
          { title: '操作', key: 'ops', render: (m) => canAdmin && m.id !== session.user.id
              ? `<a class="btn-link danger" data-remove="${m.id}">移除</a>` : '<span class="dim tiny">-</span>' },
        ],
        emptyText: '还没有成员',
      })}
    </div>`;

  page.querySelector('#add-member')?.addEventListener('click', async () => {
    const candidates = users.filter((u) => !members.some((m) => m.id === u.id));
    if (!candidates.length) return toast('没有可添加的用户，请先在「用户管理」创建', 'err');
    const data = await formModal({
      title: '添加成员',
      fields: [
        { label: '用户', name: 'user_id', type: 'select', options: candidates.map((u) => ({ value: u.id, label: `${u.name}（${u.email}）` })) },
        { label: '空间角色', name: 'role', type: 'select', options: [
          { value: 'member', label: '空间成员' }, { value: 'admin', label: '空间管理员' }] },
      ],
      onSubmit: async (d) => { await api.post(`/workspaces/${wsId}/members`, d); toast('成员已添加', 'ok'); },
    });
    if (data) ctx.refresh();
  });
  page.querySelectorAll('[data-remove]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('移除成员', '移除后该成员将无法访问此工作空间，确认？', '移除'))) return;
      await api.del(`/workspaces/${wsId}/members/${b.dataset.remove}`);
      ctx.refresh();
    })
  );
}

/* -------------------------------------------------------- 工作空间设置 */
export async function renderSettings(page, ctx) {
  page.innerHTML = loading();
  const [settings, integ] = await Promise.all([
    api.get('/settings'),
    api.get('/settings/integrations').catch(() => ({ notify: {}, git: {} })),
  ]);
  const notify = integ.notify || {};
  const git = integ.git || {};

  page.innerHTML = `
    <div class="grid-2" style="align-items:start">
      <div class="col" style="gap:16px">
        ${card('通知渠道', `
          <div class="field"><label>企业微信机器人</label>
            <input id="n-wecom" value="${esc(notify.wecom || '')}" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." /></div>
          <div class="field"><label>Webhook</label>
            <input id="n-hook" value="${esc(notify.webhook || '')}" placeholder="https://your-domain/alert" /></div>
          <div class="field"><label>邮件</label>
            <input id="n-email" value="${esc(notify.email || '')}" placeholder="data-team@example.com" disabled />
            <div class="hint">邮件需配置 SMTP，v0.3 开放；当前可先用企业微信 / Webhook</div></div>
          <div class="row"><button class="btn primary" id="n-save">保存</button>
            <button class="btn" id="n-test">发送测试消息</button>
            <span id="n-out" class="tiny"></span></div>`,
          { sub: '任务失败、质量未通过、定时任务完成时推送' })}

        ${card('Git 连接', `
          <div class="field"><label>仓库地址</label>
            <input id="g-url" value="${esc(git.url || '')}" placeholder="https://github.com/your/repo.git" /></div>
          <div class="field"><label>分支</label><input id="g-branch" value="${esc(git.branch || 'main')}" /></div>
          <div class="row"><button class="btn primary" id="g-save">保存</button>
            <button class="btn" id="g-test">测试连接</button>
            <span id="g-out" class="tiny"></span></div>
          <div class="hint" style="margin-top:10px">用于 Studio 文件的版本管理；测试会用 <code class="inline">git ls-remote</code> 实际探测。</div>`)}
      </div>
      <div class="col" style="gap:16px">
        ${card('workspace_rule.md', `<div class="tiny dim" style="margin-bottom:8px">
          一次配置，全空间生效。会作为系统提示的一部分注入 Buddy 的上下文。</div>
          <textarea id="rule" rows="12" class="mono" style="font-family:var(--mono);font-size:12.5px">${esc(settings.workspace_rule || `# 空间规范

- 表命名：层级前缀 + 业务域 + 描述，例如 dwd_trade_order_detail
- 金额字段统一保留两位小数，单位元
- 涉及客户手机号、邮箱、身份证的查询必须脱敏
- 新增指标必须先注册语义层，不允许在 SQL 里重复实现口径`)}</textarea>
          <div class="row mt-12"><button class="btn primary" id="save-rule">保存</button></div>`)}
        ${card('工作空间信息', descList([
          ['名称', ctx.ws?.name || '-'],
          ['描述', ctx.ws?.description || '-'],
          ['我的角色', ctx.ws?.role === 'admin' ? '空间管理员' : '空间成员'],
          ['数据目录', `<span class="mono tiny">${esc(settings.platform.data_dir)}</span>`],
        ]))}
      </div>
    </div>`;

  const setOut = (id, ok, message) => {
    const node = page.querySelector(id);
    node.className = `tiny ${ok ? '' : ''}`;
    node.innerHTML = `<span class="tag ${ok ? 'green' : 'red'}">${ok ? '成功' : '失败'}</span>
      <span style="margin-left:6px">${esc(message)}</span>`;
  };

  page.querySelector('#n-save').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      await api.put('/settings/notify', {
        wecom: page.querySelector('#n-wecom').value.trim(),
        webhook: page.querySelector('#n-hook').value.trim(),
        email: page.querySelector('#n-email').value.trim(),
        enabled: true,
      });
      toast('通知渠道已保存', 'ok');
    } catch (err) { toast(err.message, 'err'); }
    finally { e.target.disabled = false; }
  });
  page.querySelector('#n-test').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const out = await api.post('/settings/notify/test', {
        wecom: page.querySelector('#n-wecom').value.trim(),
        webhook: page.querySelector('#n-hook').value.trim(),
        email: page.querySelector('#n-email').value.trim(),
        enabled: true,
      });
      setOut('#n-out', out.ok, out.message);
    } catch (err) { setOut('#n-out', false, err.message); }
    finally { e.target.disabled = false; }
  });

  page.querySelector('#g-save').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      await api.put('/settings/git', {
        url: page.querySelector('#g-url').value.trim(),
        branch: page.querySelector('#g-branch').value.trim() || 'main',
        username: '',
      });
      toast('Git 连接已保存', 'ok');
    } catch (err) { toast(err.message, 'err'); }
    finally { e.target.disabled = false; }
  });
  page.querySelector('#g-test').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      const out = await api.post('/settings/git/test', {
        url: page.querySelector('#g-url').value.trim(),
        branch: page.querySelector('#g-branch').value.trim() || 'main',
        username: '',
      });
      setOut('#g-out', out.ok, out.message);
    } catch (err) { setOut('#g-out', false, err.message); }
    finally { e.target.disabled = false; }
  });

  page.querySelector('#save-rule').addEventListener('click', async (e) => {
    e.target.disabled = true;
    try {
      await api.put('/settings', { workspace_rule: page.querySelector('#rule').value });
      toast('已保存', 'ok');
    } catch (err) { toast(err.message, 'err'); }
    finally { e.target.disabled = false; }
  });
}
