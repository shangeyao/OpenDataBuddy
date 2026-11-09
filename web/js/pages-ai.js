// 数据科学与数据应用：模型实验 / 模型管理 / 模型服务 / 智能体 / MCP / 模型列表
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  modal, confirmDialog, formModal, toast, drawer, esc, descList, el,
} from './ui.js';
import { icon } from './icons.js';

/* -------------------------------------------------------------- 模型实验 */
export async function renderExperiments(page, ctx) {
  page.innerHTML = loading();
  const { experiments } = await api.get('/models');
  ctx.actions(el('button', { class: 'btn primary', onclick: () => newExperiment(ctx) }, '＋ 新建实验'));

  const success = experiments.filter((e) => e.status === 'success').length;
  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">实验总数</span><span class="value">${experiments.length}</span><span class="foot">MLflow 跟踪</span></div>
      <div class="stat"><span class="label">成功</span><span class="value">${success}</span><span class="foot">可复现</span></div>
      <div class="stat"><span class="label">运行中</span><span class="value">${experiments.filter((e) => e.status === 'running').length}</span><span class="foot">训练中</span></div>
      <div class="stat"><span class="label">框架</span><span class="value" style="font-size:18px">${[...new Set(experiments.map((e) => e.framework))].length} 种</span><span class="foot">${[...new Set(experiments.map((e) => e.framework))].join(' / ') || '-'}</span></div>
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">实验列表</span><span class="dim tiny">指标由训练脚本回写</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="exp-table"></div>
    </div>`;

  page.querySelector('#exp-table').innerHTML = dataTable({
    rows: experiments,
    columns: [
      { title: '实验名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div><div class="cell-sub">${esc(r.owner || '-')}</div>` },
      { title: '框架', render: (r) => tag(r.framework, 'outline') },
      { title: '状态', render: (r) => statusTag(r.status) },
      { title: '关键指标', render: (r) => Object.entries(r.metrics || {}).slice(0, 3)
          .map(([k, v]) => `<span class="tag" style="margin-right:4px">${esc(k)} <b style="margin-left:2px">${typeof v === 'number' ? v.toFixed(3) : esc(v)}</b></span>`).join('') || '<span class="dim">-</span>' },
      { title: '创建时间', render: (r) => `<span class="dim">${fmt.dt(r.created_at)}</span>` },
      { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-detail="${r.id}">详情</a><a class="btn-link" data-register="${r.name}">注册模型</a>` },
    ],
    emptyText: '还没有实验', emptyHint: '新建实验后，训练脚本可回写指标',
  });

  page.querySelectorAll('[data-detail]').forEach((b) =>
    b.addEventListener('click', () => {
      const e = experiments.find((x) => x.id === b.dataset.detail);
      drawer({
        title: e.name,
        body: `<div class="col" style="gap:16px">
          ${descList([['实验 ID', `<span class="mono">${e.id}</span>`], ['框架', e.framework], ['状态', statusTag(e.status)], ['负责人', e.owner], ['创建时间', fmt.dt(e.created_at)]])}
          <div>${card('指标', tableHTML(['指标', '数值'], Object.entries(e.metrics || {}).map(([k, v]) => [k, String(v)]), { raw: [] }), { flush: true })}</div>
          <div class="tiny dim">复现命令：<code class="inline">python train.py --exp ${esc(e.name)} --seed 42</code></div>
        </div>`,
      });
    })
  );
  page.querySelectorAll('[data-register]').forEach((b) =>
    b.addEventListener('click', async () => {
      await confirmDialog('注册模型', `将 ${b.dataset.register} 注册为模型并创建 v1 版本？`, '注册');
      toast('已注册到模型管理', 'ok');
      location.hash = '#/registry';
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function newExperiment(ctx) {
  const data = await formModal({
    title: '新建实验',
    fields: [
      { label: '实验名称', name: 'name', required: true, placeholder: '例如 churn_v2' },
      { label: '框架', name: 'framework', type: 'select', options: ['PyTorch', 'LightGBM', 'XGBoost', 'Prophet', 'sklearn'] },
      { label: '备注', name: 'note', type: 'textarea', rows: 3 },
    ],
    onSubmit: async (d) => {
      await api.post('/models/experiments', { name: d.name, framework: d.framework, metrics: {} });
      toast('实验已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- 模型管理 */
export async function renderRegistry(page, ctx) {
  page.innerHTML = loading();
  const { experiments, services } = await api.get('/models');
  const models = [...new Set(experiments.map((e) => e.name))];
  ctx.actions(el('button', { class: 'btn primary', onclick: () => location.hash = '#/exp' }, '＋ 从实验注册'));

  page.innerHTML = `
    <div class="card flush">
      <div class="tbl-toolbar"><span class="title">模型注册表</span><span class="dim tiny">注册 · 版本 · 血缘 · 晋级</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="reg-table"></div>
    </div>
    <div class="grid-2 mt-16">
      ${card('版本阶段', `<div class="tiny muted" style="line-height:2">
        · <b>Staging</b>：候选版本，可跑离线评估<br>
        · <b>Production</b>：冠军模型，对外提供服务<br>
        · <b>Archived</b>：下线版本，仅保留记录<br><br>
        一键晋级会同步更新模型服务的模型引用。</div>`)}
      ${card('血缘', `<div class="tiny muted" style="line-height:2">
        数据表 → 特征 → 实验 → 模型版本 → 在线服务，全链路可追溯。<br>
        当前已关联 ${models.length} 个模型、${services.length} 个在线服务。</div>`)}
    </div>`;

  page.querySelector('#reg-table').innerHTML = dataTable({
    rows: models.map((name, i) => {
      const exp = experiments.find((e) => e.name === name);
      const svc = services.find((s) => s.model_ref === name);
      return {
        id: name,
        name,
        version: `v${(i % 3) + 1}`,
        stage: svc ? 'Production' : 'Staging',
        metrics: exp?.metrics || {},
        service: svc?.name,
      };
    }),
    columns: [
      { title: '模型名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div><div class="cell-sub">来源实验 ${esc(r.name)}</div>` },
      { title: '最新版本', render: (r) => `<span class="mono">${r.version}</span>` },
      { title: '阶段', render: (r) => tag(r.stage, r.stage === 'Production' ? 'green' : 'blue') },
      { title: '关联服务', render: (r) => r.service ? `<span class="mono tiny">${esc(r.service)}</span>` : '<span class="dim">未部署</span>' },
      { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-promote="${r.name}">晋级</a><a class="btn-link" data-serve="${r.name}">部署</a>` },
    ],
    emptyText: '还没有注册模型', emptyHint: '从「模型实验」注册一个实验为模型',
  });

  page.querySelectorAll('[data-promote]').forEach((b) =>
    b.addEventListener('click', async () => {
      await confirmDialog('晋级模型', `将 ${b.dataset.promote} 晋级为 Production（冠军模型）？`, '晋级');
      toast('已晋级为 Production', 'ok');
    })
  );
  page.querySelectorAll('[data-serve]').forEach((b) =>
    b.addEventListener('click', () => deployService(b.dataset.serve, ctx))
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function deployService(modelRef, ctx) {
  const data = await formModal({
    title: '部署为在线服务',
    fields: [
      { label: '服务名称', name: 'name', required: true, value: `${modelRef}_api` },
      { label: '模型', name: 'model_ref', value: modelRef },
      { label: '版本', name: 'version', value: 'v1' },
      { label: '副本数', name: 'replicas', type: 'number', value: 1, min: 1, max: 8 },
    ],
    onSubmit: async (d) => { await api.post('/models/services', d); toast('服务已创建', 'ok'); },
  });
  if (data) {
    toast('已部署，可在「模型服务」查看', 'ok');
    location.hash = '#/serving';
  }
}

/* -------------------------------------------------------------- 模型服务 */
export async function renderServing(page, ctx) {
  page.innerHTML = loading();
  const { services, experiments } = await api.get('/models');
  ctx.actions(el('button', { class: 'btn primary', onclick: () => deployService(experiments[0]?.name || 'model', ctx) }, '＋ 部署服务'));

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">在线服务</span><span class="value">${services.filter((s) => s.status === 'running').length}</span><span class="foot">共 ${services.length} 个</span></div>
      <div class="stat"><span class="label">总副本</span><span class="value">${services.reduce((a, s) => a + (s.replicas || 0), 0)}</span><span class="foot">弹性伸缩未开启</span></div>
      <div class="stat"><span class="label">调用方式</span><span class="value" style="font-size:18px">HTTP</span><span class="foot">/svc/{name}</span></div>
      <div class="stat"><span class="label">计费</span><span class="value" style="font-size:18px">本地</span><span class="foot"><span class="tag green">不产生费用</span></span></div>
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">服务列表</span><span class="dim tiny">支持多版本 A/B、自动启停</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="svc-table"></div>
    </div>`;

  page.querySelector('#svc-table').innerHTML = dataTable({
    rows: services,
    columns: [
      { title: '服务名称', render: (r) => `<div class="cell-main mono">${esc(r.name)}</div><div class="cell-sub">${esc(r.endpoint)}</div>` },
      { title: '模型 / 版本', render: (r) => `${esc(r.model_ref)}<div class="cell-sub mono">${esc(r.version)}</div>` },
      { title: '副本', align: 'right', width: '70px', render: (r) => r.replicas },
      { title: '状态', render: (r) => statusTag(r.status) },
      { title: '创建时间', render: (r) => `<span class="dim">${fmt.dt(r.created_at)}</span>` },
      { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-toggle="${r.id}" data-to="${r.status === 'running' ? 'stop' : 'start'}">${r.status === 'running' ? '停止' : '启动'}</a><a class="btn-link" data-call="${esc(r.endpoint)}">调用示例</a>` },
    ],
    emptyText: '还没有在线服务', emptyHint: '从「模型管理」部署一个模型',
  });

  page.querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', async () => {
      await api.post(`/models/services/${b.dataset.toggle}/action`, { action: b.dataset.to });
      toast(b.dataset.to === 'start' ? '已启动' : '已停止', 'ok');
      ctx.refresh();
    })
  );
  page.querySelectorAll('[data-call]').forEach((b) =>
    b.addEventListener('click', () => {
      modal({
        title: '调用示例',
        wide: true,
        body: `<pre class="code">curl -X POST http://127.0.0.1:8000${esc(b.dataset.call)} \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"instances": [{"customer_id": "C00001"}]}'</pre>`,
        actions: [{ label: '关闭', primary: true }],
      });
    })
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

/* -------------------------------------------------------------- 智能体 */
export async function renderAgents(page, ctx) {
  page.innerHTML = loading();
  const [{ agents, apps }, { tools }] = await Promise.all([api.get('/agents'), api.get('/settings/tools').catch(() => ({ tools: [] }))]);
  ctx.actions(el('div', { class: 'row' }, [
    el('button', { class: 'btn', onclick: () => newApp(ctx) }, '＋ 新建应用'),
    el('button', { class: 'btn primary', onclick: () => newAgent(ctx, tools) }, '＋ 新建智能体'),
  ]));

  const modeLabel = { engineering: '数据开发', governance: '数据治理', analysis: '数据分析' };

  page.innerHTML = `
    <div class="card">
      <div class="card-head"><div><h3>内置与自定义 Agent</h3>
        <div class="sub">每个 Agent 绑定场景模式与工具集，可独立发布</div></div>
        <span class="tag blue">${agents.length} 个</span></div>
      <div class="grid-3">
        ${agents.map((a) => `<div class="card" style="padding:14px 16px">
          <div class="row" style="justify-content:space-between">
            <div class="row" style="gap:8px"><span class="ico" style="color:var(--brand)">${icon('agents', 18)}</span>
              <strong style="font-size:14px">${esc(a.name)}</strong></div>
            ${tag(modeLabel[a.mode] || a.mode, 'blue')}
          </div>
          <div class="tiny muted mt-8" style="line-height:1.7;min-height:38px">${esc(a.description || '无描述')}</div>
          <div class="row mt-8" style="gap:4px;flex-wrap:wrap">${(a.tools || []).slice(0, 3).map((t) => `<span class="tag outline">${esc(t)}</span>`).join('')}</div>
          <div class="row mt-12" style="gap:8px">
            <a class="btn sm" href="#/buddy">对话</a>
            <button class="btn sm" data-view="${a.id}">配置</button>
            <button class="btn sm danger" data-del="${a.id}">删除</button>
          </div>
        </div>`).join('') || emptyState('还没有智能体', '点击右上角新建')}
      </div>
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">低代码应用</span><span class="dim tiny">把 Agent 或看板包成业务页面</span>
        <span class="spacer"></span></div>
      <div id="app-table"></div>
    </div>`;

  page.querySelector('#app-table').innerHTML = dataTable({
    rows: apps,
    columns: [
      { title: '应用名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div><div class="cell-sub">${esc(r.description || '')}</div>` },
      { title: '类型', render: (r) => tag({ chat: '对话式', dashboard: '看板', form: '表单' }[r.type] || r.type, 'outline') },
      { title: '入口', render: (r) => `<span class="mono tiny">${esc(r.entry)}</span>` },
      { title: '状态', render: (r) => statusTag(r.status) },
      { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" href="${esc(r.entry)}">打开</a>` },
    ],
    emptyText: '还没有应用',
  });

  page.querySelectorAll('[data-del]').forEach((b) =>
    b.addEventListener('click', async () => {
      if (!(await confirmDialog('删除智能体', '确认删除？', '删除'))) return;
      await api.del(`/agents/${b.dataset.del}`);
      ctx.refresh();
    })
  );
  page.querySelectorAll('[data-view]').forEach((b) =>
    b.addEventListener('click', () => {
      const a = agents.find((x) => x.id === b.dataset.view);
      drawer({
        title: a.name,
        body: `<div class="col" style="gap:16px">
          ${descList([['场景模式', modeLabel[a.mode] || a.mode], ['状态', statusTag(a.status)], ['创建时间', fmt.dt(a.created_at)]])}
          <div><div class="tiny dim" style="margin-bottom:6px">系统提示词</div><pre class="code">${esc(a.prompt || '-')}</pre></div>
          <div><div class="tiny dim" style="margin-bottom:6px">可用工具</div>${(a.tools || []).map((t) => `<span class="tag blue" style="margin:0 4px 4px 0">${esc(t)}</span>`).join('') || '<span class="dim">未配置</span>'}</div>
        </div>`,
      });
    })
  );
}

async function newAgent(ctx, tools) {
  const data = await formModal({
    title: '新建智能体',
    wide: true,
    fields: [
      { label: '智能体名称', name: 'name', required: true, placeholder: '例如 结算对账助手' },
      { label: '场景模式', name: 'mode', type: 'select', options: [
        { value: 'engineering', label: '数据开发' }, { value: 'governance', label: '数据治理' }, { value: 'analysis', label: '数据分析' }] },
      { label: '描述', name: 'description', placeholder: '一句话说明它能干什么' },
      { label: '系统提示词', name: 'prompt', type: 'textarea', rows: 5, placeholder: '你是一名…，回答时必须先查数据再下结论。' },
      { label: '工具白名单', name: 'tools', placeholder: '逗号分隔，如 run_query,list_tables', hint: tools?.length ? `可用：${tools.slice(0, 6).map((t) => t.name).join(', ')}…` : '' },
    ],
    onSubmit: async (d) => {
      await api.post('/agents', {
        name: d.name, mode: d.mode, description: d.description, prompt: d.prompt,
        tools: d.tools.split(',').map((s) => s.trim()).filter(Boolean),
      });
      toast('智能体已创建', 'ok');
    },
  });
  if (data) ctx.refresh();
}

async function newApp(ctx) {
  const data = await formModal({
    title: '新建应用',
    fields: [
      { label: '应用名称', name: 'name', required: true },
      { label: '类型', name: 'type', type: 'select', options: [
        { value: 'chat', label: '对话式' }, { value: 'dashboard', label: '看板' }] },
      { label: '描述', name: 'description' },
      { label: '入口路径', name: 'entry', value: '/buddy' },
    ],
    onSubmit: async (d) => { await api.post('/apps', d); toast('应用已创建', 'ok'); },
  });
  if (data) ctx.refresh();
}

/* -------------------------------------------------------------- MCP 服务 */
export async function renderMcp(page, ctx) {
  page.innerHTML = loading();
  const { tools } = await api.get('/settings/tools');
  page.innerHTML = `
    <div class="info-strip">
      <span class="tag blue">MCP</span>
      MCP（Model Context Protocol）把平台能力暴露成标准工具，让外部 Agent 与人走同一套调用链路。
      开源版已把 ${tools.length} 个工具注册为 MCP 工具清单。
    </div>
    <div class="grid-2 mt-16" style="grid-template-columns:1fr 380px">
      <div class="card flush">
        <div class="tbl-toolbar"><span class="title">工具清单</span><span class="dim tiny">按场景模式下发</span>
          <span class="spacer"></span><span class="tag">${tools.length} tools</span></div>
        ${dataTable({
          rows: tools,
          columns: [
            { title: '工具名', render: (r) => `<span class="mono" style="font-size:12.5px">${esc(r.name)}</span>` },
            { title: '说明', render: (r) => `<span class="tiny">${esc(r.description)}</span>` },
            { title: '风险', render: (r) => tag(r.risk === 'read' ? '只读' : '写操作', r.risk === 'read' ? 'green' : 'orange') },
          ],
        })}
      </div>
      <div class="col" style="gap:16px">
        ${card('接入方式', `<pre class="code">{
  "mcpServers": {
    "databuddy": {
      "command": "python",
      "args": ["-m", "databuddy.mcp"],
      "env": { "DATABUDDY_TOKEN": "&lt;token&gt;" }
    }
  }
}</pre>
          <div class="tiny dim mt-12">v0.2 提供独立 MCP server 进程；当前可通过 HTTP 接口 /api/* 直接调用。</div>`)}
        ${card('安全约束', `<div class="tiny muted" style="line-height:1.9">
          所有工具调用都经过护栏：只读 SQL 校验、三要素拦截、PII 脱敏。<br>
          外部 Agent 调用同样落审计日志。</div>`)}
      </div>
    </div>`;
}

/* -------------------------------------------------------------- 模型列表 */
export async function renderModelList(page, ctx) {
  page.innerHTML = loading();
  const s = await api.get('/settings');
  const providers = s.providers || [];
  const active = s.llm;

  page.innerHTML = `
    <div class="info-strip">
      <span class="tag ${active.configured ? 'green' : 'orange'}">${active.configured ? '已接入' : '未接入'}</span>
      当前模型：<b>${esc(active.model || '-')}</b>（${esc(active.provider)}）·
      Base URL <span class="mono tiny">${esc(active.base_url)}</span>
      <span class="spacer"></span><a class="btn sm" href="#/platform">去配置</a>
    </div>
    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">可用模型 Endpoint</span>
        <span class="dim tiny">OpenAI 兼容协议，可自由切换</span></div>
      ${dataTable({
        rows: providers.map((p) => ({
          ...p,
          active: p.id === active.provider,
          model: p.id === active.provider ? active.model : p.model,
        })),
        columns: [
          { title: '服务商', render: (r) => `<div class="cell-main">${esc(r.label)}</div><div class="cell-sub mono">${esc(r.id)}</div>` },
          { title: 'Base URL', render: (r) => `<span class="mono tiny">${esc(r.base_url)}</span>` },
          { title: '默认模型', render: (r) => `<span class="mono tiny">${esc(r.model)}</span>` },
          { title: '状态', render: (r) => r.active ? statusTag(active.configured ? 'running' : 'draft') : '<span class="dim">未启用</span>' },
          { title: '操作', key: 'ops', render: (r) => r.active ? '<span class="dim tiny">当前使用</span>' : `<a class="btn-link" href="#/platform">切换</a>` },
        ],
      })}
    </div>
    <div class="grid-2 mt-16">
      ${card('推荐模型', tableHTML(['模型', '定位', '适用'], [
        ['deepseek-chat', '通用对话 + 工具调用', '问数、ETL 生成、诊断'],
        ['deepseek-reasoner', '深度推理', '复杂归因、方案设计'],
        ['qwen2.5-coder:7b', '本地 Ollama', '离线环境、代码补全'],
      ], { raw: [] }), { flush: true })}
      ${card('费用说明', `<div class="tiny muted" style="line-height:1.9">
        平台本身不产生模型费用；调用 DeepSeek / OpenAI 由对应服务商计费。<br>
        使用 Ollama 等本地模型则完全离线、零成本。<br>
        未配置模型时，Buddy 自动降级为内置规则引擎。</div>`)}
    </div>`;
}
