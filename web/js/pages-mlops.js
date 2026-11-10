// MLOps：特征管理 / 模型注册与晋级 / 模型服务（A/B + 弹性伸缩）
import { api, fmt } from './api.js';
import {
  card, dataTable, statusTag, tag, loading, emptyState, tableHTML,
  modal, confirmDialog, formModal, toast, drawer, esc, descList, el,
} from './ui.js';

const STAGE_META = {
  staging: { label: '候选', tone: 'orange', hint: '已通过离线评估，待上线验证' },
  production: { label: '生产', tone: 'green', hint: '线上冠军版本，唯一' },
  archived: { label: '归档', tone: 'outline', hint: '历史版本，仅可回溯' },
};

const DTYPE_OPTIONS = [
  { value: 'float', label: 'float 浮点' },
  { value: 'int', label: 'int 整数' },
  { value: 'string', label: 'string 字符串' },
  { value: 'bool', label: 'bool 布尔' },
  { value: 'date', label: 'date 日期' },
];

function stageTag(stage) {
  const meta = STAGE_META[stage] || { label: stage || '-', tone: 'outline' };
  return tag(meta.label, meta.tone);
}

function pct(v, digits = 1) {
  if (v === null || v === undefined || v === '') return '-';
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

function num(v, digits = 3) {
  if (v === null || v === undefined || v === '') return '-';
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return Number.isInteger(n) ? String(n) : n.toFixed(digits);
}

/* ================================================================ 特征管理 */
export async function renderFeatures(page, ctx) {
  page.innerHTML = loading();
  const [{ features, summary }, { tables }] = await Promise.all([
    api.get('/features'),
    api.get('/catalog/tables').catch(() => ({ tables: [] })),
  ]);

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => buildTrainingSet(ctx, features, tables) }, '构建训练集'),
      el('button', { class: 'btn primary', onclick: () => newFeature(ctx, tables) }, '＋ 新建特征'),
    ])
  );

  const computed = features.filter((f) => f.stats && f.stats.count !== undefined).length;
  const avgNull = features.length
    ? features.reduce((acc, f) => acc + Number((f.stats || {}).null_rate || 0), 0) / features.length
    : 0;

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">特征总数</span><span class="value">${summary.total}</span>
        <span class="foot">${Object.keys(summary.entities || {}).length} 类主体</span></div>
      <div class="stat"><span class="label">已计算</span><span class="value">${computed}</span>
        <span class="foot">统计已落 stats</span></div>
      <div class="stat"><span class="label">平均空值率</span><span class="value">${pct(avgNull)}</span>
        <span class="foot">${avgNull > 0.3 ? '偏高，建议排查' : '在合理范围'}</span></div>
      <div class="stat"><span class="label">来源表</span><span class="value">${summary.tables}</span>
        <span class="foot">训练 / 推理共用同一份定义</span></div>
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">特征列表</span>
        <span class="dim tiny">expr 是 SQL 表达式，改定义时版本自增</span>
        <span class="spacer"></span>
        <input id="f-kw" placeholder="按名称 / 描述搜索" style="width:200px" />
        <button class="btn sm" id="reload">刷新</button></div>
      <div id="feature-table"></div>
    </div>

    <div id="feature-detail" class="mt-16"></div>`;

  const draw = (rows) => {
    page.querySelector('#feature-table').innerHTML = dataTable({
      rows,
      columns: [
        {
          title: '特征名称',
          render: (r) => `<div class="cell-main">${esc(r.name)}</div>
            <div class="cell-sub">${esc(r.description || r.expr)}</div>`,
        },
        {
          title: '主体',
          render: (r) => `${tag(r.entity || '-', 'outline')}
            ${r.is_aggregate ? tag('聚合', 'blue') : tag('行级', '')}`,
        },
        { title: '类型', render: (r) => `<span class="mono tiny">${esc(r.dtype)}</span>` },
        {
          title: '版本',
          render: (r) => `<span class="mono">v${r.version}</span>`,
        },
        {
          title: '空值率',
          render: (r) => {
            const s = r.stats || {};
            if (s.null_rate === undefined) return '<span class="dim">未计算</span>';
            const rate = Number(s.null_rate);
            return `<span class="${rate > 0.3 ? '' : 'dim'}" style="${rate > 0.3 ? 'color:var(--error)' : ''}">${pct(rate)}</span>
              <div class="cell-sub">${fmt.num(s.count)} 行 / ${fmt.num(s.distinct)} 基数</div>`;
          },
        },
        {
          title: '来源表',
          render: (r) => `<span class="mono tiny">${esc(r.table_ref)}</span>`,
        },
        {
          title: '操作',
          key: 'ops',
          render: (r) => `<a class="btn-link" data-compute="${r.id}">计算</a>
            <a class="btn-link" data-stats="${r.id}">统计</a>
            <a class="btn-link" data-versions="${r.id}">版本</a>
            <a class="btn-link" data-lineage="${r.id}">血缘</a>
            <a class="btn-link" data-edit="${r.id}">编辑</a>
            <a class="btn-link danger" data-del="${r.id}">删除</a>`,
        },
      ],
      emptyText: '还没有特征',
      emptyHint: '特征由「来源表 + SQL 表达式」定义，可被训练集与在线推理共用',
    });
    bindFeatureEvents(rows);
  };

  function bindFeatureEvents(rows) {
    const find = (id) => rows.find((x) => x.id === id);

    page.querySelectorAll('[data-compute]').forEach((b) =>
      b.addEventListener('click', async () => {
        b.textContent = '计算中…';
        try {
          const out = await api.post('/features/compute', { names: [find(b.dataset.compute).name], limit: 20 });
          const item = (out.features || [])[0] || {};
          toast(item.error ? `计算完成但有告警：${item.error}` : '计算完成，统计已落库', item.error ? '' : 'ok');
          ctx.refresh();
        } catch (e) {
          toast(e.message, 'err');
          b.textContent = '计算';
        }
      })
    );

    page.querySelectorAll('[data-stats]').forEach((b) =>
      b.addEventListener('click', () => {
        const f = find(b.dataset.stats);
        const s = f.stats || {};
        drawer({
          title: `特征统计 · ${f.name}`,
          body: `<div class="col" style="gap:16px">
            ${descList([
              ['特征名', `<span class="mono">${esc(f.name)}</span>`],
              ['表达式', `<code class="inline">${esc(f.expr)}</code>`],
              ['来源表', `<span class="mono tiny">${esc(f.table_ref)}</span>`],
              ['主体 / 类型', `${esc(f.entity || '-')} / ${esc(f.dtype)}`],
              ['当前版本', `v${f.version}`],
            ])}
            ${card('统计明细', Object.keys(s).length
              ? tableHTML(['指标', '数值'], Object.entries(s).map(([k, v]) => [k, String(v)]), { raw: [] })
              : emptyState('尚未计算', '点「计算」按钮跑一次物化统计'), { flush: true })}
          </div>`,
        });
      })
    );

    page.querySelectorAll('[data-versions]').forEach((b) =>
      b.addEventListener('click', async () => {
        const f = find(b.dataset.versions);
        try {
          const out = await api.get(`/features/${f.id}/versions`);
          drawer({
            title: `版本历史 · ${f.name}`,
            wide: true,
            body: `<div class="col" style="gap:16px">
              <div class="tiny muted">改 expr / 来源表 / 主体时版本自增，历史版本可用于复现训练集。</div>
              ${dataTable({
                rows: out.versions || [],
                columns: [
                  { title: '版本', render: (r) => `<span class="mono">v${r.version}</span>${r.current ? ' ' + tag('当前', 'blue') : ''}` },
                  { title: '表达式', render: (r) => `<code class="inline">${esc(r.expr)}</code>` },
                  { title: '类型', render: (r) => `<span class="mono tiny">${esc(r.dtype)}</span>` },
                  { title: '空值率', render: (r) => { const s = r.stats || {}; return s.null_rate === undefined ? '<span class="dim">-</span>' : pct(s.null_rate); } },
                  { title: '创建时间', render: (r) => `<span class="dim tiny">${fmt.dt(r.created_at)}</span>` },
                ],
                emptyText: '没有历史版本',
              })}</div>`,
          });
        } catch (e) { toast(e.message, 'err'); }
      })
    );

    page.querySelectorAll('[data-lineage]').forEach((b) =>
      b.addEventListener('click', async () => {
        const f = find(b.dataset.lineage);
        try {
          const out = await api.get(`/features/${f.id}/lineage`);
          const up = out.upstream || {};
          const down = (out.downstream || []).map((d) => d.downstream || d);
          drawer({
            title: `特征血缘 · ${f.name}`,
            body: `<div class="col" style="gap:16px">
              ${descList([
                ['上游表', `<span class="mono tiny">${esc(up.table_ref || '-')}</span>`],
                ['主体键', `<span class="mono">${esc(up.entity_key || '-')}</span>`],
                ['引用列', (up.columns || []).map((c) => tag(c, 'outline')).join(' ') || '-'],
              ])}
              <div>
                <div class="tiny muted" style="margin-bottom:8px">下游引用</div>
                ${down.length ? down.map((d) => `<div class="mono tiny">${esc(d)}</div>`).join('')
                  : '<div class="tiny dim">暂无下游（训练集 / 在线推理引用后会出现在这里）</div>'}
              </div>
              ${(out.related && out.related.metrics && out.related.metrics.length)
                ? `<div><div class="tiny muted" style="margin-bottom:8px">关联指标</div>
                    ${out.related.metrics.map((m) => tag(m.display_name || m.name, 'outline')).join(' ')}</div>`
                : ''}
            </div>`,
          });
        } catch (e) { toast(e.message, 'err'); }
      })
    );

    page.querySelectorAll('[data-edit]').forEach((b) =>
      b.addEventListener('click', () => editFeature(ctx, find(b.dataset.edit), tables))
    );

    page.querySelectorAll('[data-del]').forEach((b) =>
      b.addEventListener('click', async () => {
        const f = find(b.dataset.del);
        if (!(await confirmDialog('删除特征', `确认删除「${f.name}」？历史版本与血缘会一并清理。`, '删除'))) return;
        try {
          await api.del(`/features/${f.id}`);
          toast('已删除', 'ok');
          ctx.refresh();
        } catch (e) { toast(e.message, 'err'); }
      })
    );
  }

  draw(features);

  const kw = page.querySelector('#f-kw');
  kw.addEventListener('input', () => {
    const q = kw.value.trim().toLowerCase();
    draw(q
      ? features.filter((f) => `${f.name} ${f.description || ''} ${f.table_ref}`.toLowerCase().includes(q))
      : features);
  });
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());
}

async function newFeature(ctx, tables) {
  const data = await formModal({
    title: '新建特征',
    wide: true,
    fields: [
      { label: '特征名', name: 'name', required: true, placeholder: 'order_amount_30d' },
      { label: '来源表', name: 'table_ref', type: 'select', options: tables.map((t) => t.ref) },
      { label: '主体', name: 'entity', placeholder: 'customer（留空则按 *_id 自动推断）' },
      { label: '数据类型', name: 'dtype', type: 'select', options: DTYPE_OPTIONS },
      { label: 'SQL 表达式', name: 'expr', type: 'textarea', required: true, rows: 3,
        placeholder: 'amount * 0.8 或 sum(amount) 或 count(*)',
        hint: '行级表达式直接对列计算；含聚合函数（sum/avg/count…）时按主体键 GROUP BY。' },
      { label: '描述', name: 'description', placeholder: '这个特征刻画什么业务含义' },
    ],
    submitText: '创建',
    onSubmit: async (d) => {
      await api.post('/features', d);
      toast('特征已创建', 'ok');
      ctx.refresh();
    },
  });
  return data;
}

async function editFeature(ctx, feature, tables) {
  await formModal({
    title: `编辑特征 · ${feature.name}`,
    wide: true,
    intro: `<div class="tiny muted" style="margin-bottom:12px">改动 expr / 来源表 / 主体会让版本自增，历史统计会清空需重算。</div>`,
    fields: [
      { label: '来源表', name: 'table_ref', type: 'select', options: tables.map((t) => t.ref), value: feature.table_ref },
      { label: '主体', name: 'entity', value: feature.entity || '' },
      { label: '数据类型', name: 'dtype', type: 'select', options: DTYPE_OPTIONS, value: feature.dtype },
      { label: 'SQL 表达式', name: 'expr', type: 'textarea', rows: 3, value: feature.expr },
      { label: '描述', name: 'description', value: feature.description || '' },
    ],
    submitText: '保存',
    onSubmit: async (d) => {
      const out = await api.patch(`/features/${feature.id}`, d);
      const bumped = out.feature && out.feature.version > feature.version;
      toast(bumped ? `已保存，版本升到 v${out.feature.version}` : '已保存', 'ok');
      ctx.refresh();
    },
  });
}

async function buildTrainingSet(ctx, features, tables) {
  if (!features.length) {
    toast('请先创建特征', 'err');
    return;
  }
  const featureOptions = features.map((f) => ({ value: f.name, label: `${f.name}（v${f.version} · ${f.entity || '-'}）` }));

  modal({
    title: '构建训练集',
    wide: true,
    body: `<div class="col" style="gap:14px">
      <div class="tiny muted">训练集把多个特征按主体键拼成宽表，表达式与在线推理共用同一份定义 —— 这是训练 / 推理一致性的保证。</div>
      <div class="field"><label><span class="req">*</span>选择特征</label>
        <select id="ts-features" multiple size="6" style="min-height:132px">
          ${featureOptions.map((o) => `<option value="${esc(o.value)}" selected>${esc(o.label)}</option>`).join('')}
        </select>
        <div class="hint">按住 Ctrl / Cmd 可多选</div>
      </div>
      <div class="field"><label>基准表</label>
        <select id="ts-table">${tables.map((t) => `<option value="${esc(t.ref)}">${esc(t.ref)}</option>`).join('')}</select>
      </div>
      <div class="field"><label>标签表达式</label>
        <textarea id="ts-label" rows="2" placeholder="CASE WHEN sum(amount) > 200 THEN 1 ELSE 0 END（可留空）"></textarea>
        <div class="hint">含聚合函数时会自动把宽表收敛到「一行一个主体」</div>
      </div>
      <div class="field inline" style="grid-template-columns:120px 1fr"><label>行数上限</label>
        <input type="number" id="ts-limit" value="200" min="1" max="500" /></div>
      <div id="ts-result"></div>
    </div>`,
    actions: [
      { label: '取消', value: false },
      {
        label: '生成',
        primary: true,
        onClick: async (body) => {
          const names = Array.from(body.querySelector('#ts-features').selectedOptions).map((o) => o.value);
          if (!names.length) {
            toast('至少选择一个特征', 'err');
            return false;
          }
          const host = body.querySelector('#ts-result');
          host.innerHTML = loading('生成中…');
          try {
            const out = await api.post('/features/training-set', {
              feature_names: names,
              table_ref: body.querySelector('#ts-table').value,
              label_expr: body.querySelector('#ts-label').value.trim(),
              limit: Number(body.querySelector('#ts-limit').value || 200),
            });
            host.innerHTML = `<div class="col" style="gap:12px">
              ${out.warnings && out.warnings.length
                ? `<div class="tag orange">${esc(out.warnings.join('；'))}</div>` : ''}
              ${descList([
                ['粒度', out.granularity === 'entity' ? '一行一个主体' : '一行一条源记录'],
                ['主体键', `<span class="mono">${esc(out.entity_key)}</span>`],
                ['行数', fmt.num(out.row_count)],
                ['列数', String((out.columns || []).length)],
                ['耗时', out.elapsed_ms != null ? `${out.elapsed_ms} ms` : '-'],
              ])}
              <div>
                <div class="tiny muted" style="margin-bottom:6px">生成的 SQL</div>
                <pre class="code">${esc(out.sql)}</pre>
              </div>
              ${card('预览数据', tableHTML(out.columns || [], out.rows || [], { max: 20 }), { flush: true })}
            </div>`;
            return false;
          } catch (e) {
            host.innerHTML = `<pre class="code">${esc(e.message)}</pre>`;
            return false;
          }
        },
      },
    ],
  });
}

/* ========================================================== 模型注册与晋级 */
export async function renderRegistry2(page, ctx) {
  page.innerHTML = loading();
  const { models, summary } = await api.get('/registry/models');

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => ctx.refresh() }, '刷新'),
      el('button', { class: 'btn primary', onclick: () => registerModel(ctx) }, '＋ 从实验注册'),
    ])
  );

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">已注册模型</span><span class="value">${summary.models}</span>
        <span class="foot">${summary.champions} 个有生产冠军</span></div>
      <div class="stat"><span class="label">版本总数</span><span class="value">${summary.versions}</span>
        <span class="foot">v1 / v2 / v3 自增</span></div>
      <div class="stat"><span class="label">生产版本</span><span class="value" style="color:var(--success)">${summary.stages.production || 0}</span>
        <span class="foot">每模型唯一（冠军唯一）</span></div>
      <div class="stat"><span class="label">候选 / 归档</span>
        <span class="value" style="font-size:20px">${summary.stages.staging || 0} / ${summary.stages.archived || 0}</span>
        <span class="foot">staging / archived</span></div>
    </div>

    <div class="card flush mt-16">
      <div class="tbl-toolbar"><span class="title">模型列表</span>
        <span class="dim tiny">晋级 production 时同模型其他生产版本会自动归档</span>
        <span class="spacer"></span><button class="btn sm" id="reload">刷新</button></div>
      <div id="model-table"></div>
    </div>

    <div id="model-detail" class="mt-16"></div>`;

  page.querySelector('#model-table').innerHTML = dataTable({
    rows: models,
    columns: [
      {
        title: '模型名称',
        render: (r) => `<div class="cell-main">${esc(r.name)}</div>
          <div class="cell-sub">${esc(r.framework || '-')} · ${esc(r.owner || '-')}</div>`,
      },
      {
        title: '版本',
        render: (r) => `<span class="mono">${r.version_count}</span> 个
          <div class="cell-sub">最新 ${esc(r.latest_version || '-')}</div>`,
      },
      {
        title: '生产冠军',
        render: (r) => (r.production_version
          ? `${tag(r.production_version, 'green')}`
          : '<span class="dim">未指定</span>'),
      },
      {
        title: '阶段分布',
        render: (r) => ['staging', 'production', 'archived']
          .filter((s) => (r.stages[s] || []).length)
          .map((s) => `${stageTag(s)}<span class="tiny dim"> ${r.stages[s].join(' ')}</span>`)
          .join('<br>') || '<span class="dim">-</span>',
      },
      { title: '来源实验', render: (r) => `<span class="mono tiny">${esc(r.source_experiment || '-')}</span>` },
      { title: '创建时间', render: (r) => `<span class="dim tiny">${fmt.dt(r.created_at)}</span>` },
      {
        title: '操作',
        key: 'ops',
        render: (r) => `<a class="btn-link" data-versions="${r.id}">版本看板</a>
          <a class="btn-link" data-addver="${r.id}">加版本</a>
          <a class="btn-link" data-compare="${r.id}">对比</a>`,
      },
    ],
    emptyText: '还没有注册模型',
    emptyHint: '从模型实验注册后即可管理版本与晋级',
  });

  page.querySelectorAll('[data-versions]').forEach((b) =>
    b.addEventListener('click', () => showVersionBoard(b.dataset.versions))
  );
  page.querySelectorAll('[data-addver]').forEach((b) =>
    b.addEventListener('click', () => addVersion(ctx, b.dataset.addver))
  );
  page.querySelectorAll('[data-compare]').forEach((b) =>
    b.addEventListener('click', () => compareVersions(b.dataset.compare))
  );
  page.querySelector('#reload').addEventListener('click', () => ctx.refresh());

  async function showVersionBoard(modelId) {
    const { model } = await api.get(`/registry/models/${modelId}`);
    const cols = ['staging', 'production', 'archived'];
    const board = cols.map((stage) => {
      const list = (model.versions || []).filter((v) => (v.stage || 'staging') === stage);
      return `<div class="card" style="flex:1;min-width:0">
        <div class="card-head" style="margin-bottom:10px">
          <div><h3>${stageTag(stage)} <span class="tiny dim">${list.length}</span></h3>
            <div class="sub">${esc(STAGE_META[stage].hint)}</div></div>
        </div>
        <div class="col" style="gap:8px">
          ${list.length ? list.map((v) => `
            <div style="border:1px solid var(--border);border-radius:8px;padding:10px">
              <div class="row" style="justify-content:space-between">
                <span class="mono" style="font-weight:500">${esc(v.version)}</span>
                ${stage !== 'production' ? `<a class="btn-link" data-promote="${v.version}" data-stage="production">晋级生产</a>` : ''}
                ${stage === 'production' ? `<a class="btn-link" data-promote="${v.version}" data-stage="archived">归档</a>` : ''}
              </div>
              <div class="tiny dim" style="margin-top:6px">${esc(v.description || '-')}</div>
              <div class="tiny" style="margin-top:6px">${Object.entries(v.metrics || {}).slice(0, 3)
                .map(([k, val]) => `<span class="tag outline" style="margin-right:4px">${esc(k)} <b>${num(val)}</b></span>`)
                .join('') || '<span class="dim">无指标</span>'}</div>
              <div class="tiny dim" style="margin-top:6px">${fmt.dt(v.created_at)}</div>
            </div>`).join('')
            : '<div class="tiny dim" style="padding:12px 0">暂无版本</div>'}
        </div>
      </div>`;
    }).join('');

    const d = drawer({
      title: `版本看板 · ${model.name}`,
      wide: true,
      body: `<div class="col" style="gap:16px">
        <div class="tiny muted">晋级 production 会自动把同模型其他生产版本降为 archived —— 冠军唯一。</div>
        <div style="display:flex;gap:12px;align-items:flex-start">${board}</div>
      </div>`,
    });

    d.body.querySelectorAll('[data-promote]').forEach((b) =>
      b.addEventListener('click', async () => {
        const stage = b.dataset.stage;
        const ver = b.dataset.promote;
        const label = stage === 'production' ? '晋级为生产' : '归档';
        const ok = await confirmDialog(
          label,
          stage === 'production'
            ? `确认把 ${ver} 晋级为生产版本？同模型当前生产版本会自动归档，线上流量会切换。`
            : `确认把 ${ver} 归档？归档后不再参与线上流量。`,
          label
        );
        if (!ok) return;
        try {
          const out = await api.post(`/registry/models/${model.id}/versions/${ver}/promote`, { stage });
          const extra = (out.demoted || []).length ? `，${out.demoted.join('、')} 已自动归档` : '';
          toast(`${ver} → ${STAGE_META[stage].label}${extra}`, 'ok');
          d.close();
          ctx.refresh();
        } catch (e) { toast(e.message, 'err'); }
      })
    );
  }

  async function compareVersions(modelId) {
    const { model } = await api.get(`/registry/models/${modelId}`);
    const versions = model.versions || [];
    if (versions.length < 2) {
      toast('至少需要两个版本才能对比', 'err');
      return;
    }
    const opts = versions.map((v) => ({ value: v.version, label: `${v.version}（${STAGE_META[v.stage] ? STAGE_META[v.stage].label : v.stage}）` }));
    modal({
      title: `版本对比 · ${model.name}`,
      wide: true,
      body: `<div class="col" style="gap:14px">
        <div class="row" style="gap:12px">
          <div class="field" style="flex:1;margin:0"><label>基线 A</label>
            <select id="cmp-a">${opts.map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}</select></div>
          <div class="field" style="flex:1;margin:0"><label>对比 B</label>
            <select id="cmp-b">${opts.slice(1).map((o) => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join('')}</select></div>
        </div>
        <div id="cmp-result"></div>
      </div>`,
      actions: [
        { label: '关闭', value: false },
        {
          label: '对比',
          primary: true,
          onClick: async (body) => {
            const a = body.querySelector('#cmp-a').value;
            const b = body.querySelector('#cmp-b').value;
            if (a === b) {
              toast('请选择两个不同版本', 'err');
              return false;
            }
            const host = body.querySelector('#cmp-result');
            host.innerHTML = loading('对比中…');
            try {
              const out = await api.get(`/registry/models/${modelId}/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
              host.innerHTML = `<div class="col" style="gap:12px">
                <div class="tag ${out.recommendation === 'tie' ? 'outline' : 'blue'}">${esc(out.recommendation_text)}</div>
                ${dataTable({
                  rows: out.metrics || [],
                  columns: [
                    { title: '指标', render: (r) => `<span class="mono">${esc(r.metric)}</span>` },
                    { title: `A · ${out.a.version}`, align: 'right', render: (r) => num(r.a) },
                    { title: `B · ${out.b.version}`, align: 'right', render: (r) => num(r.b) },
                    {
                      title: '差值',
                      align: 'right',
                      render: (r) => {
                        if (r.delta === null || r.delta === undefined) return '<span class="dim">-</span>';
                        const better = r.better === 'b';
                        const worse = r.better === 'a';
                        const color = better ? 'var(--success)' : worse ? 'var(--error)' : 'inherit';
                        return `<span style="color:${color}">${r.delta > 0 ? '+' : ''}${num(r.delta)}</span>`;
                      },
                    },
                    {
                      title: '变化',
                      align: 'right',
                      render: (r) => (r.delta_pct === null || r.delta_pct === undefined
                        ? '<span class="dim">-</span>'
                        : `${r.delta_pct > 0 ? '+' : ''}${(r.delta_pct * 100).toFixed(1)}%`),
                    },
                    {
                      title: '更优',
                      render: (r) => (r.better === 'b' ? tag('B', 'green') : r.better === 'a' ? tag('A', 'blue') : tag('持平', 'outline')),
                    },
                  ],
                  emptyText: '两版本没有可比较的数值指标',
                })}
              </div>`;
              return false;
            } catch (e) {
              host.innerHTML = `<pre class="code">${esc(e.message)}</pre>`;
              return false;
            }
          },
        },
      ],
    });
  }

  async function addVersion(context, modelId) {
    await formModal({
      title: '新增版本',
      wide: true,
      intro: `<div class="tiny muted" style="margin-bottom:12px">版本号自动自增（v1、v2…），指标可手填或从实验继承。</div>`,
      fields: [
        { label: '指标 JSON', name: 'metrics', type: 'textarea', rows: 4, value: '{\n  "auc": 0.0,\n  "f1": 0.0\n}',
          hint: '键值对，值必须是数字才能参与版本对比' },
        { label: '产物路径', name: 'artifact_uri', placeholder: 'runs:/模型名/v3（留空自动生成）' },
        { label: '阶段', name: 'stage', type: 'select', options: [
          { value: 'staging', label: '候选 staging' },
          { value: 'archived', label: '归档 archived' },
        ] },
        { label: '说明', name: 'description', placeholder: '这次版本改了什么' },
      ],
      submitText: '创建版本',
      onSubmit: async (d) => {
        let metrics = {};
        try {
          metrics = d.metrics ? JSON.parse(d.metrics) : {};
        } catch {
          toast('指标 JSON 格式不正确', 'err');
          return false;
        }
        const out = await api.post(`/registry/models/${modelId}/versions`, {
          metrics,
          artifact_uri: d.artifact_uri,
          stage: d.stage,
          description: d.description,
        });
        toast(`已创建 ${out.version.version}`, 'ok');
        context.refresh();
      },
    });
  }

  async function registerModel(context) {
    const { experiments } = await api.get('/models');
    if (!experiments || !experiments.length) {
      toast('还没有模型实验，请先到「模型实验」创建', 'err');
      return;
    }
    await formModal({
      title: '从实验注册模型',
      wide: true,
      intro: `<div class="tiny muted" style="margin-bottom:12px">注册后会自动创建 v1，指标继承实验。</div>`,
      fields: [
        { label: '来源实验', name: 'source_experiment', type: 'select', required: true,
          options: experiments.map((e) => ({ value: e.name, label: `${e.name}（${e.framework}）` })) },
        { label: '模型名', name: 'name', required: true, placeholder: 'churn_model' },
        { label: '描述', name: 'description', placeholder: '这个模型解决什么业务问题' },
      ],
      submitText: '注册',
      onSubmit: async (d) => {
        const out = await api.post('/registry/models', d);
        toast(`已注册，自动创建 ${out.version.version}`, 'ok');
        context.refresh();
      },
    });
  }
}

/* ================================================================ 模型服务 */
export async function renderServing2(page, ctx) {
  page.innerHTML = loading();
  const { services, summary } = await api.get('/serving/services');
  const { models } = await api.get('/registry/models').catch(() => ({ models: [] }));

  ctx.actions(
    el('div', { class: 'row' }, [
      el('button', { class: 'btn', onclick: () => autoStopCheck(ctx) }, '空闲巡检'),
      el('button', { class: 'btn primary', onclick: () => createService(ctx, models) }, '＋ 创建服务'),
    ])
  );

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">服务总数</span><span class="value">${summary.services}</span>
        <span class="foot">${summary.running_services} 个运行中</span></div>
      <div class="stat"><span class="label">副本总数</span><span class="value">${summary.replicas}</span>
        <span class="foot">按 target_qps 弹性伸缩</span></div>
      <div class="stat"><span class="label">A/B 服务</span><span class="value">${summary.ab_services}</span>
        <span class="foot">多版本流量切分</span></div>
      <div class="stat"><span class="label">生产冠军</span><span class="value">${summary.champions}</span>
        <span class="foot">每模型唯一</span></div>
    </div>

    <div class="mt-16" id="service-cards">
      ${services.length ? '' : card('', emptyState('还没有模型服务', '从已注册模型创建服务，支持多版本 A/B 与弹性伸缩'))}
    </div>`;

  const host = page.querySelector('#service-cards');
  if (services.length) {
    host.innerHTML = `<div class="grid-2">${services.map(serviceCard).join('')}</div>`;
    bindServiceEvents(services);
  }

  function serviceCard(s) {
    const traffic = s.traffic || {};
    const total = Object.values(traffic).reduce((a, b) => a + Number(b || 0), 0) || 1;
    const barColors = ['var(--brand)', '#12b76a', '#f79009', '#f04438'];
    const segments = Object.entries(traffic).map(([ver, weight], i) => {
      const w = (Number(weight) / total) * 100;
      return `<i style="width:${w}%;background:${barColors[i % barColors.length]}" title="${esc(ver)} ${weight}%"></i>`;
    }).join('');

    return `<div class="card" data-svc="${esc(s.id)}">
      <div class="card-head">
        <div>
          <div class="card-title-row"><h3>${esc(s.name)}</h3>${statusTag(s.status)}${s.ab_test ? tag('A/B', 'blue') : ''}</div>
          <div class="sub">${esc(s.model_ref || '-')} · ${esc(s.endpoint || '-')}</div>
        </div>
        <div class="row">
          <button class="btn sm" data-toggle="${s.id}" data-to="${s.status === 'running' ? 'stop' : 'start'}">
            ${s.status === 'running' ? '停止' : '启动'}</button>
          <button class="btn sm" data-ab="${s.id}">流量</button>
        </div>
      </div>

      <div class="progress" style="height:8px;display:flex;gap:2px">${segments}</div>
      <div class="tiny" style="margin-top:8px">
        ${Object.entries(traffic).map(([ver, weight], i) =>
          `<span class="tag outline" style="margin-right:6px"><i class="dot" style="background:${barColors[i % barColors.length]}"></i>${esc(ver)} ${weight}%</span>`
        ).join('') || '<span class="dim">未配置流量</span>'}
      </div>

      <div class="grid-3 mt-16" style="gap:10px">
        <div><div class="tiny dim">副本</div><div style="font-size:16px">${s.replicas}
          <span class="tiny dim">/ ${s.min_replicas}–${s.max_replicas}</span></div></div>
        <div><div class="tiny dim">单副本 QPS</div><div style="font-size:16px">${num(s.target_qps, 0)}</div></div>
        <div><div class="tiny dim">空闲超时</div><div style="font-size:16px">${s.idle_timeout_seconds ? `${s.idle_timeout_seconds}s` : '不自动停'}</div></div>
      </div>

      <div class="row mt-16" style="gap:8px;flex-wrap:wrap">
        <button class="btn sm" data-scale="${s.id}">扩缩容</button>
        <button class="btn sm" data-detail="${s.id}">详情</button>
        <span class="spacer" style="flex:1"></span>
        <span class="tiny dim">${s.traffic_detail && s.traffic_detail.length > 1 ? '多版本分流中' : '单版本'}</span>
      </div>
    </div>`;
  }

  function bindServiceEvents(rows) {
    const find = (id) => rows.find((x) => x.id === id);

    page.querySelectorAll('[data-toggle]').forEach((b) =>
      b.addEventListener('click', async () => {
        try {
          await api.post(`/serving/services/${b.dataset.toggle}/action`, { action: b.dataset.to });
          toast(b.dataset.to === 'start' ? '已启动' : '已停止', 'ok');
          ctx.refresh();
        } catch (e) { toast(e.message, 'err'); }
      })
    );

    page.querySelectorAll('[data-ab]').forEach((b) =>
      b.addEventListener('click', () => abDialog(ctx, find(b.dataset.ab)))
    );

    page.querySelectorAll('[data-scale]').forEach((b) =>
      b.addEventListener('click', () => scaleDialog(ctx, find(b.dataset.scale)))
    );

    page.querySelectorAll('[data-detail]').forEach((b) =>
      b.addEventListener('click', async () => {
        const { service } = await api.get(`/serving/services/${b.dataset.detail}`);
        drawer({
          title: `服务详情 · ${service.name}`,
          wide: true,
          body: `<div class="col" style="gap:16px">
            ${descList([
              ['状态', statusTag(service.status)],
              ['模型', `<span class="mono">${esc(service.model_ref)}</span>`],
              ['主版本', tag(service.version, 'green')],
              ['Endpoint', `<code class="inline">${esc(service.endpoint)}</code>`],
              ['副本', `${service.replicas}（${service.min_replicas}–${service.max_replicas}）`],
              ['单副本 QPS', num(service.target_qps, 0)],
              ['容量', `${num(service.autoscale && service.autoscale.capacity_qps, 0)} QPS`],
              ['空闲超时', service.idle_timeout_seconds ? `${service.idle_timeout_seconds} 秒` : '不自动停'],
            ])}
            ${card('版本流量', tableHTML(['版本', '权重', '占比', '估算副本'],
              (service.traffic_detail || []).map((t) => [t.version, `${t.weight}%`, `${t.percent}%`, String(t.replicas)]),
              { raw: [0, 1, 2, 3] }), { flush: true })}
            ${service.model ? card('所属模型', descList([
              ['模型名', `<span class="mono">${esc(service.model.name)}</span>`],
              ['框架', esc(service.model.framework || '-')],
              ['生产冠军', service.model.champion ? tag(service.model.champion, 'green') : '<span class="dim">未指定</span>'],
              ['全部版本', (service.model.versions || []).map((v) => tag(v, 'outline')).join(' ')],
            ])) : ''}
            <div>
              <div class="tiny muted" style="margin-bottom:6px">调用示例</div>
              <pre class="code">curl -X POST http://127.0.0.1:8000${esc(service.endpoint)} \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"instances": [{"customer_id": "C00001"}]}'</pre>
            </div>
          </div>`,
        });
      })
    );
  }

  async function abDialog(context, service) {
    const { models: all } = await api.get('/registry/models');
    const model = all.find((m) => m.name === service.model_ref);
    const versions = model ? (model.versions || []).map((v) => v.version) : [service.version];
    if (versions.length < 2) {
      toast('该服务所属模型只有一个版本，先加一个版本再做 A/B', 'err');
      return;
    }
    const current = { ...(service.traffic || {}) };
    versions.forEach((v) => { if (current[v] === undefined) current[v] = 0; });

    modal({
      title: `A/B 流量切分 · ${service.name}`,
      wide: true,
      body: `<div class="col" style="gap:14px">
        <div class="tiny muted">拖动滑块调整各版本流量占比，保存后自动归一化到 100%。</div>
        <div id="ab-sliders" class="col" style="gap:12px">
          ${versions.map((v) => `
            <div class="field" style="margin:0">
              <label>${esc(v)} <span class="tiny dim" id="ab-val-${esc(v)}">${current[v] || 0}%</span></label>
              <input type="range" min="0" max="100" step="5" value="${current[v] || 0}"
                data-ab-slider="${esc(v)}" style="width:100%" />
            </div>`).join('')}
        </div>
        <div class="progress" style="height:8px;display:flex;gap:2px" id="ab-bar"></div>
        <div class="tiny dim" id="ab-sum"></div>
      </div>`,
      actions: [
        { label: '取消', value: false },
        {
          label: '保存流量',
          primary: true,
          onClick: async (body) => {
            const traffic = {};
            body.querySelectorAll('[data-ab-slider]').forEach((n) => {
              traffic[n.dataset.abSlider] = Number(n.value);
            });
            if (!Object.values(traffic).some((v) => v > 0)) {
              toast('至少给一个版本分配流量', 'err');
              return false;
            }
            try {
              const out = await api.post(`/serving/services/${service.id}/traffic`, { traffic });
              toast(`流量已生效：${Object.entries(out.service.traffic).map(([k, v]) => `${k} ${v}%`).join(' / ')}`, 'ok');
              context.refresh();
            } catch (e) {
              toast(e.message, 'err');
              return false;
            }
          },
        },
      ],
      onMount: (body) => {
        const bar = body.querySelector('#ab-bar');
        const sum = body.querySelector('#ab-sum');
        const sliders = Array.from(body.querySelectorAll('[data-ab-slider]'));
        const colors = ['var(--brand)', '#12b76a', '#f79009', '#f04438'];
        const sync = () => {
          const vals = sliders.map((n) => Number(n.value));
          const total = vals.reduce((a, b) => a + b, 0) || 1;
          bar.innerHTML = sliders.map((n, i) => {
            const w = (Number(n.value) / total) * 100;
            return `<i style="width:${w}%;background:${colors[i % colors.length]}"></i>`;
          }).join('');
          sliders.forEach((n) => {
            const label = body.querySelector(`#ab-val-${CSS.escape(n.dataset.abSlider)}`);
            if (label) label.textContent = `${n.value}%`;
          });
          sum.textContent = total === 100
            ? `合计 ${total}%（保存时按比例归一化）`
            : `合计 ${total}%，保存时会按比例归一化到 100%`;
        };
        sliders.forEach((n) => n.addEventListener('input', sync));
        sync();
      },
    });
  }

  async function scaleDialog(context, service) {
    const out = await api.post(`/serving/services/${service.id}/autoscale`, { qps: service.replicas * service.target_qps })
      .catch(() => null);
    await formModal({
      title: `扩缩容 · ${service.name}`,
      wide: true,
      intro: `<div class="tiny muted" style="margin-bottom:12px">
        弹性伸缩按 target_qps 推算：期望副本 = ceil(流量 QPS / 单副本 QPS)，并夹在 [min, max] 之间。
        ${out ? `<br>当前容量 ${num(out.current_replicas * out.target_qps, 0)} QPS，建议 ${out.desired_replicas} 副本。` : ''}
      </div>`,
      fields: [
        { label: '最小副本', name: 'min_replicas', type: 'number', value: service.min_replicas, min: 1 },
        { label: '最大副本', name: 'max_replicas', type: 'number', value: service.max_replicas, min: 1 },
        { label: '单副本 QPS', name: 'target_qps', type: 'number', value: service.target_qps, min: 1 },
        { label: '当前流量 QPS', name: 'qps', type: 'number', value: service.replicas * service.target_qps, min: 0,
          hint: '按这个 QPS 推算期望副本数' },
      ],
      submitText: '应用',
      onSubmit: async (d) => {
        try {
          const res = await api.post(`/serving/services/${service.id}/action`, {
            action: 'scale',
            min_replicas: d.min_replicas,
            max_replicas: d.max_replicas,
            target_qps: d.target_qps,
            qps: d.qps,
          });
          const detail = res.service.detail || {};
          toast(`副本 ${detail.from ?? '?'} → ${res.service.replicas}`, 'ok');
          context.refresh();
        } catch (e) {
          toast(e.message, 'err');
          return false;
        }
      },
    });
  }

  async function createService(context, registered) {
    if (!registered || !registered.length) {
      toast('请先在模型注册表注册模型', 'err');
      return;
    }
    await formModal({
      title: '创建模型服务',
      wide: true,
      intro: `<div class="tiny muted" style="margin-bottom:12px">默认使用模型的生产冠军版本；可配置副本区间与空闲自动停服。</div>`,
      fields: [
        { label: '服务名', name: 'name', required: true, placeholder: 'churn_score_api' },
        { label: '模型', name: 'model_ref', type: 'select', required: true,
          options: registered.map((m) => ({ value: m.name, label: `${m.name}（冠军 ${m.production_version || '未指定'}）` })) },
        { label: '副本数', name: 'replicas', type: 'number', value: 1, min: 1 },
        { label: '最小副本', name: 'min_replicas', type: 'number', value: 1, min: 1 },
        { label: '最大副本', name: 'max_replicas', type: 'number', value: 4, min: 1 },
        { label: '单副本 QPS', name: 'target_qps', type: 'number', value: 50, min: 1 },
        { label: '空闲超时（秒）', name: 'idle_timeout_seconds', type: 'number', value: 0, min: 0,
          hint: '0 表示不自动停服；设置后空闲超过该时长会自动停止' },
      ],
      submitText: '创建',
      onSubmit: async (d) => {
        try {
          const out = await api.post('/serving/services', d);
          toast(`服务已创建，Endpoint ${out.service.endpoint}`, 'ok');
          context.refresh();
        } catch (e) {
          toast(e.message, 'err');
          return false;
        }
      },
    });
  }

  async function autoStopCheck(context) {
    try {
      const out = await api.post('/serving/auto-stop-check', {});
      toast(out.count ? `已自动停止 ${out.stopped.join('、')}` : '没有需要停止的空闲服务', out.count ? 'ok' : '');
      if (out.count) context.refresh();
    } catch (e) { toast(e.message, 'err'); }
  }
}
