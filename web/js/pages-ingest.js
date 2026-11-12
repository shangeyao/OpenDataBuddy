// 智能接入 2.0：数据源模板画廊（8 大分类 · 60 个模板）+ 5 方案对比 + Agent 诊断
//
// 交互主线：选模板 → 按 fields 定义动态生成配置表单 → 保存 → 一键诊断 → 按修复建议改进。
// 全部复用 ui.js 的既有组件，不引入任何前端框架或构建步骤。
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState,
  toast, drawer, esc, el, modal, formModal,
} from './ui.js';
import { icon } from './icons.js';

const state = {
  templates: [],
  groups: [],
  categories: [],
  plans: [],
  sources: [],
  activeCategory: 'all',
  keyword: '',
  activePlan: 'upload',
  diagnoses: {},   // source_id -> diagnose result
};

const PLAN_TONE = { direct: 'blue', scheduled: 'green', realtime: 'orange', upload: '', agent: 'blue' };
const LEVEL_TONE = { critical: 'red', warning: 'orange', info: 'blue' };
const LEVEL_LABEL = { critical: '阻断', warning: '告警', info: '提示' };

/* ============================================================ 页面入口 */

export async function renderIngest2(page, ctx) {
  page.innerHTML = loading('加载数据源模板…');
  await loadAll();

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', {
        class: 'btn',
        onclick: () => {
          if (state.activateTab) state.activateTab('diagnose');
          runAllDiagnose(ctx, state.diagnosePane);
        },
      }, '批量诊断'),
      el('button', { class: 'btn', onclick: () => { location.hash = '#/sources'; } }, '数据源管理'),
      el('button', { class: 'btn primary', onclick: () => openTemplatePicker(ctx) }, '＋ 新建数据源'),
    ])
  );

  const diagnosed = Object.keys(state.diagnoses).length;
  const healthy = Object.values(state.diagnoses).filter((d) => d.status === 'healthy').length;

  page.innerHTML = `
    <div class="stat-row" style="margin-bottom:16px">
      ${[
        ['数据源模板', state.templates.length, `${state.categories.length} 大分类`],
        ['已注册数据源', state.sources.length, state.sources.length ? '点击可诊断' : '还没有数据源'],
        ['接入方案', state.plans.length, '直连 / 定时 / 实时 / 文件 / Agent'],
        ['诊断健康', diagnosed ? `${healthy} / ${diagnosed}` : '-', diagnosed ? '已诊断的数据源' : '点「批量诊断」'],
      ].map(([label, value, foot]) => `<div class="card" style="padding:14px 18px">
          <div class="tiny dim">${esc(label)}</div>
          <div style="font-size:22px;font-weight:500;line-height:30px">${esc(String(value))}</div>
          <div class="tiny dim">${esc(foot)}</div>
        </div>`).join('')}
    </div>

    <div class="tabs" data-tabs="ingest" style="margin-bottom:16px">
      <button class="active" data-tab="gallery">模板画廊</button>
      <button data-tab="plans">接入方案</button>
      <button data-tab="diagnose">Agent 诊断</button>
      <button data-tab="sync">同步与出湖</button>
    </div>

    <div id="tab-gallery"></div>
    <div id="tab-plans" class="hidden"></div>
    <div id="tab-diagnose" class="hidden"></div>
    <div id="tab-sync" class="hidden"></div>`;

  const panes = {
    gallery: page.querySelector('#tab-gallery'),
    plans: page.querySelector('#tab-plans'),
    diagnose: page.querySelector('#tab-diagnose'),
    sync: page.querySelector('#tab-sync'),
  };
  state.diagnosePane = panes.diagnose;
  const buttons = page.querySelectorAll('[data-tabs="ingest"] button');
  let plansLoaded = false;
  let diagnoseLoaded = false;

  const activate = (key) => {
    buttons.forEach((b) => b.classList.toggle('active', b.dataset.tab === key));
    Object.entries(panes).forEach(([k, node]) => node.classList.toggle('hidden', k !== key));
    if (key === 'gallery') renderGallery(panes.gallery, ctx);
    if (key === 'plans' && !plansLoaded) { plansLoaded = true; renderPlans(panes.plans, ctx); }
    if (key === 'diagnose' && !diagnoseLoaded) { diagnoseLoaded = true; renderDiagnosePane(panes.diagnose, ctx); }
    if (key === 'sync') renderSyncPane(panes.sync, ctx);
  };
  state.activateTab = activate;
  buttons.forEach((b) => b.addEventListener('click', () => activate(b.dataset.tab)));
  activate('gallery');
}

async function loadAll() {
  const [tpl, plans, sources] = await Promise.all([
    api.get('/sources/templates').catch(() => ({ templates: [], groups: [], categories: [] })),
    api.get('/ingest/plans').catch(() => ({ plans: [] })),
    api.get('/sources').catch(() => ({ sources: [] })),
  ]);
  state.templates = tpl.templates || [];
  state.groups = tpl.groups || [];
  state.categories = tpl.categories || [];
  state.plans = plans.plans || [];
  state.sources = sources.sources || [];
}

/* ======================================================== 模板画廊 */

function renderGallery(host, ctx) {
  const counts = {};
  for (const t of state.templates) counts[t.category] = (counts[t.category] || 0) + 1;
  const cats = [
    { key: 'all', label: '全部', count: state.templates.length },
    ...state.categories.map((c) => ({ ...c, count: counts[c.key] || 0 })),
  ];
  host.innerHTML = `
    <div class="row wrap" style="gap:8px;margin-bottom:16px">
      ${cats.map((c) => `<button class="btn sm ${state.activeCategory === c.key ? 'primary' : ''}"
        data-cat="${esc(c.key)}">${esc(c.label)} (${c.count})</button>`).join('')}
      <span style="flex:1"></span>
      <input id="tpl-search" type="text" placeholder="搜索数据源名称…"
        value="${esc(state.keyword)}" style="width:220px;height:30px;font-size:12.5px" />
    </div>
    <div id="tpl-groups"></div>`;

  host.querySelectorAll('[data-cat]').forEach((b) =>
    b.addEventListener('click', () => {
      state.activeCategory = b.dataset.cat;
      renderGallery(host, ctx);
    })
  );
  const search = host.querySelector('#tpl-search');
  search.addEventListener('input', () => {
    state.keyword = search.value.trim();
    renderGalleryGroups(host.querySelector('#tpl-groups'), ctx);
  });
  search.addEventListener('keydown', (e) => { if (e.key === 'Escape') { search.value = ''; state.keyword = ''; renderGalleryGroups(host.querySelector('#tpl-groups'), ctx); } });
  renderGalleryGroups(host.querySelector('#tpl-groups'), ctx);
}

function renderGalleryGroups(host, ctx) {
  const keyword = state.keyword.toLowerCase();
  const groups = state.groups
    .filter((g) => state.activeCategory === 'all' || g.key === state.activeCategory)
    .map((g) => ({
      ...g,
      templates: g.templates.filter((t) =>
        !keyword || t.name.toLowerCase().includes(keyword) || t.id.includes(keyword)
        || (t.doc || '').toLowerCase().includes(keyword)
      ),
    }))
    .filter((g) => g.templates.length);

  if (!groups.length) {
    host.innerHTML = emptyState('没有匹配的数据源模板', '换个关键字或点击「全部」分类');
    return;
  }

  host.innerHTML = groups.map((g) => `
    <div style="margin-bottom:24px">
      <div class="row" style="margin-bottom:10px">
        <span class="ico" style="color:var(--brand);display:inline-flex">${icon(g.icon || 'dot', 15)}</span>
        <h3 style="font-size:14px;font-weight:500">${esc(g.label)}</h3>
        <span class="tag outline">${g.templates.length}</span>
        <span class="tiny dim">${esc(g.desc || '')}</span>
      </div>
      <div class="grid-3" style="gap:12px">
        ${g.templates.map((t) => templateCard(t)).join('')}
      </div>
    </div>`).join('');

  host.querySelectorAll('[data-tpl]').forEach((node) =>
    node.addEventListener('click', () => {
      const tpl = state.templates.find((t) => t.id === node.dataset.tpl);
      if (tpl) openTemplateForm(tpl, ctx);
    })
  );
}

function templateCard(t) {
  const required = (t.fields || []).filter((f) => f.required).length;
  return `<div class="card" data-tpl="${esc(t.id)}" style="padding:14px 16px;cursor:pointer;
      transition:border-color .15s, box-shadow .15s"
      onmouseover="this.style.borderColor='var(--brand)'"
      onmouseout="this.style.borderColor='var(--border)'">
    <div class="row" style="gap:10px;align-items:flex-start">
      <span style="width:30px;height:30px;border-radius:8px;background:var(--brand-bg);color:var(--brand);
        display:inline-flex;align-items:center;justify-content:center;flex:none">${icon(t.icon || 'dot', 16)}</span>
      <div style="min-width:0;flex:1">
        <div class="row" style="gap:6px">
          <span style="font-weight:500;font-size:13px">${esc(t.name)}</span>
          ${t.popularity >= 80 ? '<span class="tag blue tiny">热门</span>' : ''}
        </div>
        <div class="tiny dim" style="margin-top:3px;line-height:1.5;
          display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">
          ${esc(t.doc || '')}</div>
        <div class="row tiny dim" style="gap:8px;margin-top:8px">
          <span>${(t.fields || []).length} 项配置</span>
          <span>·</span>
          <span>${required} 项必填</span>
          ${t.protocol ? `<span>·</span><span class="mono">${esc(t.protocol)}</span>` : ''}
        </div>
      </div>
    </div>
  </div>`;
}

/* ==================================================== 模板配置表单 */

function fieldControl(f) {
  const name = esc(f.key);
  const value = f.value ?? '';
  if (f.type === 'select') {
    const opts = (f.options || []).map((o) => {
      const opt = typeof o === 'string' ? { value: o, label: o } : o;
      return `<option value="${esc(opt.value)}"${String(opt.value) === String(value) ? ' selected' : ''}>${esc(opt.label)}</option>`;
    }).join('');
    return `<select name="${name}" data-field="${name}">${opts}</select>`;
  }
  if (f.type === 'textarea') {
    return `<textarea name="${name}" data-field="${name}" rows="3"
      placeholder="${esc(f.placeholder || '')}">${esc(value)}</textarea>`;
  }
  if (f.type === 'checkbox') {
    return `<label class="checkbox"><input type="checkbox" name="${name}" data-field="${name}"
      ${value ? 'checked' : ''}/> ${esc(f.label)}</label>`;
  }
  if (f.type === 'number') {
    return `<input type="number" name="${name}" data-field="${name}" value="${esc(value)}"
      placeholder="${esc(f.placeholder || '')}" />`;
  }
  return `<input type="${f.type === 'password' ? 'password' : 'text'}" name="${name}"
    data-field="${name}" value="${esc(value)}" placeholder="${esc(f.placeholder || '')}" />`;
}

/** 单个字段的完整表单项；checkbox 自带文案，不再套外层 label。 */
function fieldBlock(f) {
  if (f.type === 'checkbox') {
    return `<div class="field">${fieldControl(f)}
      ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}</div>`;
  }
  return `<div class="field">
    <label>${f.required ? '<span class="req">*</span>' : ''}${esc(f.label)}</label>
    ${fieldControl(f)}
    ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}
  </div>`;
}

function openTemplateForm(tpl, ctx) {
  const fields = tpl.fields || [];
  const requiredFields = fields.filter((f) => f.required);
  const body = `<div class="col" style="gap:16px">
    <div class="info-strip">
      <span class="tag blue">${esc(tpl.name)}</span>
      <span>${esc(tpl.doc || '')}</span>
    </div>
    <div class="grid-2" style="gap:14px">
      <div class="field"><label><span class="req">*</span>数据源名称</label>
        <input type="text" id="ing-name" placeholder="例如 生产库只读" /></div>
      <div class="field"><label>说明</label>
        <input type="text" id="ing-desc" placeholder="可选，便于后续检索" /></div>
    </div>
    <div>
      <div class="tiny dim" style="margin-bottom:8px">
        连接配置（${requiredFields.length} 项必填 · 共 ${fields.length} 项）
      </div>
      <div class="grid-2" style="gap:14px">
        ${fields.map(fieldBlock).join('')}
      </div>
    </div>
    <div id="ing-validate"></div>
  </div>`;

  const handle = drawer({
    title: `配置数据源 · ${tpl.name}`,
    wide: true,
    body,
    footer: `<div class="row">
      <button class="btn" id="ing-check">配置校验</button>
      <span class="spacer" style="flex:1"></span>
      <button class="btn" id="ing-cancel">取消</button>
      <button class="btn primary" id="ing-save">保存并测试</button>
    </div>`,
  });
  const panel = handle.panel;

  const collect = () => {
    const config = {};
    fields.forEach((f) => {
      const node = panel.querySelector(`[data-field="${f.key}"]`);
      if (!node) return;
      if (f.type === 'checkbox') config[f.key] = node.checked;
      else if (f.type === 'number') config[f.key] = node.value === '' ? null : Number(node.value);
      else config[f.key] = node.value.trim();
    });
    return {
      name: (panel.querySelector('#ing-name').value || '').trim(),
      description: (panel.querySelector('#ing-desc').value || '').trim(),
      config,
    };
  };

  const result = panel.querySelector('#ing-validate');
  panel.querySelector('#ing-cancel').addEventListener('click', () => handle.close());

  panel.querySelector('#ing-check').addEventListener('click', async (e) => {
    const payload = collect();
    e.target.disabled = true;
    result.innerHTML = loading('校验中…');
    try {
      const res = await api.post('/ingest/validate', { type: tpl.id, config: payload.config });
      result.innerHTML = problemsHTML(res.problems, res.ok ? '配置校验通过' : '发现配置问题');
    } catch (err) {
      result.innerHTML = `<div class="tag red">${esc(err.message)}</div>`;
    } finally {
      e.target.disabled = false;
    }
  });

  panel.querySelector('#ing-save').addEventListener('click', async (e) => {
    const payload = collect();
    if (!payload.name) return toast('请填写数据源名称', 'err');
    const missing = requiredFields.filter((f) => {
      if (f.type === 'checkbox') return false;
      const v = payload.config[f.key];
      return v === undefined || v === null || String(v).trim() === '';
    });
    if (missing.length) return toast(`请填写「${missing[0].label}」`, 'err');

    e.target.disabled = true;
    result.innerHTML = loading('保存中…');
    try {
      const created = await api.post('/sources', {
        name: payload.name,
        type: tpl.id,
        description: payload.description,
        config: payload.config,
      });
      const sourceId = created.source?.id || created.id;
      result.innerHTML = `<div class="info-strip"><span class="tag green">已保存</span>
        <span>数据源已创建，正在做连通性测试与诊断…</span></div>`;
      if (sourceId) {
        const [tested, diag] = await Promise.all([
          api.post(`/sources/${sourceId}/test`, {}).catch(() => null),
          api.post(`/sources/${sourceId}/diagnose`, {}).catch(() => null),
        ]);
        if (diag) state.diagnoses[sourceId] = diag;
        result.innerHTML = `
          <div class="info-strip" style="margin-bottom:12px">
            <span class="tag ${tested?.ok ? 'green' : 'orange'}">
              连通性 ${tested?.ok ? '通过' : '未通过'}</span>
            <span>${esc(tested?.message || '未执行测试')}</span>
          </div>
          ${diag ? problemsHTML(diag.problems, `诊断评分 ${diag.score} · ${diag.status_label}`) : ''}`;
        toast(diag ? `保存成功，诊断评分 ${diag.score}` : '数据源已保存', 'ok');
        setTimeout(() => ctx.refresh(), 1600);
      } else {
        toast('数据源已保存', 'ok');
        setTimeout(() => ctx.refresh(), 900);
      }
    } catch (err) {
      result.innerHTML = `<div class="tag red">${esc(err.message)}</div>`;
      toast(err.message, 'err');
    } finally {
      e.target.disabled = false;
    }
  });
}

function problemsHTML(problems, title) {
  const list = problems || [];
  if (!list.length) {
    return `<div class="info-strip" style="background:#e8f8f2;border-color:#bfe9d6">
      <span class="tag green">${esc(title || '无问题')}</span>
      <span>配置完整，可以正常接入。</span></div>`;
  }
  return `<div style="border:1px solid var(--border);border-radius:6px;overflow:hidden">
    <div style="padding:10px 14px;background:var(--sub);font-size:12.5px;font-weight:500">
      ${esc(title || '发现配置问题')} · ${list.length} 项</div>
    <div style="padding:6px 0">
      ${list.map((p) => `<div style="padding:8px 14px;border-top:1px solid var(--border)">
        <div class="row" style="gap:8px;align-items:flex-start">
          ${tag(LEVEL_LABEL[p.level] || p.level, LEVEL_TONE[p.level] || '')}
          <div style="flex:1;min-width:0">
            <div style="font-size:12.5px">${esc(p.message)}</div>
            ${p.fix ? `<div class="tiny dim" style="margin-top:3px">
              <span style="color:var(--brand)">修复建议：</span>${esc(p.fix)}</div>` : ''}
          </div>
          ${p.field ? `<span class="tiny dim mono">${esc(p.field)}</span>` : ''}
        </div>
      </div>`).join('')}
    </div></div>`;
}

function openTemplatePicker(ctx) {
  const grouped = state.categories.map((c) => ({
    ...c,
    items: state.templates.filter((t) => t.category === c.key),
  })).filter((c) => c.items.length);

  const body = `<div class="col" style="gap:14px">
    <div class="tiny dim">选择一种数据源类型开始配置，共 ${state.templates.length} 种可用模板。</div>
    ${grouped.map((c) => `<div>
      <div class="row" style="gap:8px;margin-bottom:8px">
        <span class="ico" style="color:var(--brand);display:inline-flex">${icon(c.icon || 'dot', 14)}</span>
        <span style="font-size:13px;font-weight:500">${esc(c.label)}</span>
        <span class="tag outline">${c.items.length}</span>
      </div>
      <div class="row wrap" style="gap:6px">
        ${c.items.map((t) => `<button class="btn sm" data-pick="${esc(t.id)}">${esc(t.name)}</button>`).join('')}
      </div>
    </div>`).join('')}
  </div>`;

  modal({
    title: '选择数据源类型',
    wide: true,
    body,
    onMount: (content, close) => {
      content.querySelectorAll('[data-pick]').forEach((b) =>
        b.addEventListener('click', () => {
          const tpl = state.templates.find((t) => t.id === b.dataset.pick);
          close(false);
          if (tpl) openTemplateForm(tpl, ctx);
        })
      );
    },
  });
}

/* ======================================================== 接入方案 */

function renderPlans(host, ctx) {
  const plans = state.plans;
  const active = plans.find((p) => p.id === state.activePlan) || plans[0];
  host.innerHTML = `
    <div class="grid-3" style="gap:12px;margin-bottom:20px">
      ${plans.map((p) => `<div class="card" data-plan="${esc(p.id)}" style="padding:16px 18px;cursor:pointer;
          border-color:${p.id === active?.id ? 'var(--brand)' : 'var(--border)'};
          box-shadow:${p.id === active?.id ? '0 0 0 2px var(--brand-bg)' : 'none'}">
        <div class="row" style="gap:10px;margin-bottom:8px">
          <span style="width:28px;height:28px;border-radius:7px;background:var(--brand-bg);color:var(--brand);
            display:inline-flex;align-items:center;justify-content:center;flex:none">${icon(p.icon || 'dot', 15)}</span>
          <div>
            <div style="font-size:13px;font-weight:500">${esc(p.name)}</div>
            <div class="tiny dim">${esc(p.tagline)}</div>
          </div>
        </div>
        <div class="tiny dim" style="line-height:1.6">${esc(p.description)}</div>
        <div class="row tiny" style="gap:6px;margin-top:10px">
          ${tag(`时效 ${p.latency}`, 'outline')}
          ${tag(`成本 ${p.cost}`, 'outline')}
        </div>
      </div>`).join('')}
    </div>
    <div id="plan-detail">${active ? planDetailHTML(active) : emptyState('没有接入方案')}</div>`;

  host.querySelectorAll('[data-plan]').forEach((node) =>
    node.addEventListener('click', () => {
      state.activePlan = node.dataset.plan;
      renderPlans(host, ctx);
    })
  );
}

function planDetailHTML(p) {
  return `<div class="col" style="gap:16px">
    <div class="card">
      <div class="card-head">
        <div>
          <h3>${esc(p.name)} · ${esc(p.tagline)}</h3>
          <div class="sub">${esc(p.description)}</div>
        </div>
        ${tag(`推荐：${p.recommended_for}`, 'blue')}
      </div>
      <div class="grid-4" style="gap:12px;margin-bottom:16px">
        ${[['时效', p.latency], ['实施成本', p.effort], ['运行成本', p.cost], ['适用场景数', String((p.scenarios || []).length)]]
          .map(([k, v]) => `<div style="border:1px solid var(--border);border-radius:6px;padding:10px 14px">
            <div class="tiny dim">${esc(k)}</div>
            <div style="font-size:15px;font-weight:500">${esc(String(v))}</div></div>`).join('')}
      </div>
      <div class="grid-2" style="gap:16px">
        <div>
          <div class="tiny dim" style="margin-bottom:6px">适用场景</div>
          <div class="row wrap" style="gap:6px">${(p.scenarios || []).map((s) => tag(s, 'outline')).join('')}</div>
        </div>
        <div>
          <div class="tiny dim" style="margin-bottom:6px">优势</div>
          <div class="col" style="gap:4px">${(p.pros || []).map((s) =>
            `<div class="tiny"><span style="color:var(--success)">✓</span> ${esc(s)}</div>`).join('')}</div>
        </div>
      </div>
      <div style="margin-top:14px">
        <div class="tiny dim" style="margin-bottom:6px">局限</div>
        <div class="col" style="gap:4px">${(p.cons || []).map((s) =>
          `<div class="tiny"><span style="color:var(--warning)">!</span> ${esc(s)}</div>`).join('')}</div>
      </div>
    </div>
    ${card('方案对比', dataTable({
      rows: state.plans,
      rowKey: 'id',
      columns: [
        { title: '方案', render: (r) => `<b>${esc(r.name)}</b><div class="cell-sub">${esc(r.tagline)}</div>` },
        { title: '时效', render: (r) => esc(r.latency) },
        { title: '实施成本', render: (r) => esc(r.effort) },
        { title: '运行成本', render: (r) => esc(r.cost) },
        { title: '推荐场景', render: (r) => `<span class="tiny dim">${esc(r.recommended_for)}</span>` },
      ],
    }), { flush: true, sub: '五条路径按需组合，多数场景建议「文件上传」起步再升级到「定时同步」' })}
  </div>`;
}

/* ======================================================== Agent 诊断 */

function renderDiagnosePane(host, ctx) {
  host.innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <div class="tiny dim">诊断会检查：必填配置完整性 · 端口合法性 · 本地路径可访问性 ·
        主机连通性 · 测试记录 · 凭据风险</div>
      <span style="flex:1"></span>
      <button class="btn sm primary" id="diag-all">批量诊断全部</button>
    </div>
    <div id="diag-list"></div>
    <div id="diag-detail" style="margin-top:16px"></div>`;

  host.querySelector('#diag-all').addEventListener('click', () => runAllDiagnose(ctx, host));
  renderDiagnoseList(host, ctx);
}

function renderDiagnoseList(host, ctx) {
  const list = host.querySelector('#diag-list');
  if (!state.sources.length) {
    list.innerHTML = emptyState('还没有注册数据源', '从「模板画廊」选一种类型开始接入');
    return;
  }
  list.innerHTML = card('已注册数据源', dataTable({
    rows: state.sources,
    rowKey: 'id',
    columns: [
      { title: '名称', render: (r) => `<div class="cell-main">${esc(r.name)}</div>
        <div class="cell-sub">${esc(r.type)}</div>` },
      { title: '状态', render: (r) => statusTag(r.status === 'ok' ? 'ok' : r.status === 'failed' ? 'failed' : 'draft') },
      { title: '最近测试', render: (r) => `<span class="dim tiny">${fmt.ago(r.last_test_at)}</span>` },
      { title: '诊断评分', render: (r) => {
          const d = state.diagnoses[r.id];
          if (!d) return '<span class="dim tiny">未诊断</span>';
          const tone = d.score >= 85 ? 'green' : d.score >= 60 ? 'orange' : 'red';
          return `<span class="tag ${tone}">${d.score}</span>`;
        } },
      { title: '操作', key: 'ops', render: (r) =>
          `<a class="btn-link" data-diag="${esc(r.id)}">诊断</a>` },
    ],
  }), { flush: true, sub: `共 ${state.sources.length} 个数据源` });

  list.querySelectorAll('[data-diag]').forEach((b) =>
    b.addEventListener('click', () => diagnoseOne(b.dataset.diag, host, ctx))
  );
}

async function diagnoseOne(sourceId, host, ctx) {
  const detail = host.querySelector('#diag-detail');
  detail.innerHTML = loading('Agent 诊断中…');
  try {
    const d = await api.post(`/sources/${sourceId}/diagnose`, {});
    state.diagnoses[sourceId] = d;
    detail.innerHTML = diagnoseResultHTML(d);
    renderDiagnoseList(host, ctx);
  } catch (err) {
    detail.innerHTML = `<div class="tag red">${esc(err.message)}</div>`;
  }
}

async function runAllDiagnose(ctx, host) {
  const results = [];
  for (const src of state.sources) {
    try {
      const d = await api.post(`/sources/${src.id}/diagnose`, {});
      state.diagnoses[src.id] = d;
      results.push({ src, d });
    } catch {
      /* 单个失败不影响整体 */
    }
  }
  if (!results.length) {
    if (host) host.innerHTML = emptyState('没有可诊断的数据源', '先创建一个数据源');
    return toast('没有可诊断的数据源', 'err');
  }
  const critical = results.filter((r) => r.d.counts.critical > 0).length;
  toast(`诊断完成：${results.length} 个数据源，${critical} 个有阻断问题`, critical ? 'err' : 'ok');

  // 从顶栏触发时还没有渲染诊断面板，先切过去再填充
  const pane = host || state.diagnosePane;
  if (!pane) return;
  renderDiagnoseList(pane, ctx);
  const detail = pane.querySelector('#diag-detail');
  if (detail) {
    detail.innerHTML = results
      .map(({ d }) => `<div style="margin-bottom:16px">${diagnoseResultHTML(d)}</div>`)
      .join('');
  }
}

function diagnoseResultHTML(d) {
  const tone = d.score >= 85 ? 'green' : d.score >= 60 ? 'orange' : 'red';
  const statusIcon = d.status === 'healthy' ? '✓' : d.status === 'warning' ? '!' : '✕';
  return `<div class="card">
    <div class="card-head">
      <div>
        <h3>${esc(d.source.name)} <span class="mono tiny dim">${esc(d.source.type)}</span></h3>
        <div class="sub">${d.template ? `模板：${esc(d.template.name)}` : '未匹配到模板'} ·
          诊断于 ${fmt.dt(d.diagnosed_at)}</div>
      </div>
      <div class="row" style="gap:10px">
        <div style="text-align:right">
          <div class="tiny dim">健康评分</div>
          <div style="font-size:22px;font-weight:500;color:var(--${tone === 'green' ? 'success' : tone === 'orange' ? 'warning' : 'error'})">
            ${d.score}</div>
        </div>
        ${tag(`${statusIcon} ${d.status_label}`, tone)}
      </div>
    </div>

    <div class="grid-3" style="gap:12px;margin-bottom:14px">
      ${[['阻断问题', d.counts.critical, 'error'], ['告警', d.counts.warning, 'warning'], ['提示', d.counts.info, 'brand']]
        .map(([k, v, c]) => `<div style="border:1px solid var(--border);border-radius:6px;padding:10px 14px">
          <div class="tiny dim">${esc(k)}</div>
          <div style="font-size:18px;font-weight:500;color:var(--${c})">${esc(String(v))}</div></div>`).join('')}
    </div>

    ${(d.checks || []).length
      ? `<div class="row wrap" style="gap:6px;margin-bottom:14px">
          ${d.checks.map((c) => `<span class="tag ${c.ok ? 'green' : 'orange'}">
            ${c.ok ? '✓' : '!'} ${esc(c.name)}</span>`).join('')}
        </div>`
      : ''}

    ${problemsHTML(d.problems, `问题清单 · ${(d.problems || []).length} 项`)}

    ${(d.suggestions || []).length
      ? `<div style="margin-top:14px">
          <div class="tiny dim" style="margin-bottom:8px">推荐接入方案</div>
          <div class="col" style="gap:8px">
            ${d.suggestions.map((s) => `<div class="info-strip">
              <span class="tag blue">${esc(s.name)}</span>
              <span>${esc(s.reason)}</span></div>`).join('')}
          </div>
        </div>`
      : ''}
  </div>`;
}

/* ============================================================ 同步与出湖 */
async function renderSyncPane(host, ctx) {
  host.innerHTML = loading('加载同步任务…');
  const [tasks, egress, catalogs, tables] = await Promise.all([
    api.get('/sync/tasks').catch(() => ({ tasks: [] })),
    api.get('/ingest/egress').catch(() => ({ files: [] })),
    api.get('/external-catalogs').catch(() => ({ external_catalogs: [] })),
    api.get('/catalog/tables').catch(() => ({ tables: [] })),
  ]);
  const taskList = tasks.tasks || [];
  const files = egress.files || [];
  const sources = (catalogs.external_catalogs || []).filter((c) => c.type === 'sqlite');
  const tableList = tables.tables || [];

  host.innerHTML = `
    <div class="grid-2" style="align-items:start">
      <div class="col" style="gap:16px">
        ${card('同步任务', taskList.length ? dataTable({
          rows: taskList,
          rowKey: 'id',
          columns: [
            { title: '任务', render: (r) => `<div class="cell-main">${esc(r.name)}</div>
              <div class="cell-sub">${esc(r.mode === 'incremental' ? '增量' : '全量')} · ${esc(r.cron || '仅手动')}</div>` },
            { title: '状态', render: (r) => statusTag(r.last_status === 'success' ? 'ok' : r.last_status === 'failed' ? 'failed' : 'draft') },
            { title: '最近运行', render: (r) => `<span class="dim tiny">${fmt.ago(r.last_run_at)}</span>` },
            { title: '操作', key: 'ops', render: (r) => `
              <a class="btn-link" data-run="${esc(r.id)}">运行</a>
              <a class="btn-link" data-toggle="${esc(r.id)}">${r.enabled ? '停用' : '启用'}</a>
              <a class="btn-link danger" data-drop="${esc(r.id)}">删除</a>` },
          ],
        }) : emptyState('还没有同步任务', '新建一个任务，把数据源按调度同步进湖仓'), { flush: true, sub: `共 ${taskList.length} 个任务` })}
        <div class="row"><button class="btn primary sm" id="sync-new">＋ 新建同步任务</button></div>

        ${card('出湖文件', files.length ? dataTable({
          rows: files,
          rowKey: 'path',
          columns: [
            { title: '文件', render: (r) => `<span class="mono tiny">${esc(r.name)}</span>` },
            { title: '大小', render: (r) => `<span class="tiny">${esc(r.size || '')}</span>` },
            { title: '导出时间', render: (r) => `<span class="dim tiny">${fmt.ago(r.modified_at)}</span>` },
          ],
        }) : emptyState('还没有出湖文件', '把工作空间表导出为 CSV / Parquet'), { flush: true, sub: `目录 ${esc(egress.directory || '')}` })}
        <div class="row"><button class="btn sm" id="egress-export">导出数据表</button></div>
      </div>

      <div class="col" style="gap:16px">
        ${card('数据对账', sources.length ? `
          <div class="tiny dim" style="margin-bottom:10px">比对外部目录与湖仓的行数与内容，定位入湖差异。</div>
          <div class="col" style="gap:8px">
            ${sources.map((s) => `<div class="list-item" style="cursor:pointer" data-reconcile="${esc(s.id)}">
              <span style="color:var(--text-3)">${icon('sources', 15)}</span>
              <div class="meta"><strong>${esc(s.name)}</strong><span>${esc(s.type)}</span></div>
              <span class="btn-link">对账</span>
            </div>`).join('')}
          </div>
          <div id="reconcile-out" class="mt-12"></div>
        ` : emptyState('还没有外部目录', '在「目录」页登记一个 SQLite 外部目录后可做对账'), { sub: '源端与湖仓逐表比对' })}
      </div>
    </div>`;

  host.querySelector('#sync-new')?.addEventListener('click', async () => {
    const sqliteSources = (state.sources || []).filter((s) => s.type === 'sqlite');
    if (!sqliteSources.length) {
      toast('请先注册一个 SQLite 数据源', 'err');
      return;
    }
    const data = await formModal({
      title: '新建同步任务',
      fields: [
        { name: 'name', label: '任务名称', required: true, placeholder: '每日订单入库' },
        { name: 'source_id', label: '数据源', type: 'select', options: sqliteSources.map((s) => ({ value: s.id, label: s.name })) },
        { name: 'target', label: '目标 Schema', placeholder: 'catalog.schema，留空则写入 synced.main' },
        { name: 'cron', label: '调度表达式', placeholder: '0 2 * * *' },
        { name: 'mode', label: '同步方式', type: 'select', options: [{ value: 'full', label: '全量' }, { value: 'incremental', label: '增量（需水位列）' }] },
        { name: 'watermark_column', label: '水位列', placeholder: 'updated_at，增量时必填' },
      ],
      submitText: '创建',
    });
    if (!data) return;
    try {
      await api.post('/sync/tasks', { ...data, enabled: true });
      toast('同步任务已创建', 'ok');
      renderSyncPane(host, ctx);
    } catch (err) {
      toast(err.message, 'err');
    }
  });

  host.querySelectorAll('[data-run]').forEach((b) =>
    b.addEventListener('click', async () => {
      b.textContent = '运行中…';
      try {
        const out = await api.post(`/sync/tasks/${b.dataset.run}/run`, {});
        toast(`同步完成：${out.run?.total_rows ?? 0} 行`, 'ok');
      } catch (err) {
        toast(err.message, 'err');
      }
      renderSyncPane(host, ctx);
    })
  );
  host.querySelectorAll('[data-toggle]').forEach((b) =>
    b.addEventListener('click', async () => {
      const t = taskList.find((x) => x.id === b.dataset.toggle);
      try {
        await api.patch(`/sync/tasks/${b.dataset.toggle}`, { enabled: !t.enabled });
        renderSyncPane(host, ctx);
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );
  host.querySelectorAll('[data-drop]').forEach((b) =>
    b.addEventListener('click', async () => {
      try {
        await api.del(`/sync/tasks/${b.dataset.drop}`);
        toast('同步任务已删除', 'ok');
        renderSyncPane(host, ctx);
      } catch (err) {
        toast(err.message, 'err');
      }
    })
  );

  host.querySelector('#egress-export')?.addEventListener('click', async () => {
    if (!tableList.length) {
      toast('当前空间还没有数据表', 'err');
      return;
    }
    const data = await formModal({
      title: '导出数据表',
      fields: [
        { name: 'table', label: '数据表', type: 'select', options: tableList.map((t) => ({ value: t.ref, label: t.ref })) },
        { name: 'format', label: '格式', type: 'select', options: [{ value: 'csv', label: 'CSV' }, { value: 'parquet', label: 'Parquet' }] },
      ],
      submitText: '导出',
    });
    if (!data) return;
    try {
      const out = await api.post('/ingest/egress', data);
      toast(`已导出 ${out.name}（${out.rows} 行）`, 'ok');
      renderSyncPane(host, ctx);
    } catch (err) {
      toast(err.message, 'err');
    }
  });

  host.querySelectorAll('[data-reconcile]').forEach((n) =>
    n.addEventListener('click', async () => {
      const out = host.querySelector('#reconcile-out');
      out.innerHTML = loading('对账中…');
      try {
        const r = await api.post('/ingest/reconcile', { source: n.dataset.reconcile });
        out.innerHTML = dataTable({
          rows: r.results || [],
          rowKey: 'table',
          columns: [
            { title: '表', render: (x) => `<span class="mono tiny">${esc(x.table)}</span>` },
            { title: '源 / 目标', render: (x) => `<span class="tiny">${esc(String(x.source_rows))} / ${esc(String(x.target_rows))}</span>` },
            { title: '一致率', render: (x) => `<span class="tiny">${Math.round((x.consistency || 0) * 100)}%</span>` },
            { title: '结论', render: (x) => tag(x.status, x.status === '一致' ? 'green' : x.status === '不一致' ? 'red' : 'orange') },
          ],
        });
      } catch (err) {
        out.innerHTML = `<div class="tag red">${esc(err.message)}</div>`;
      }
    })
  );
}
