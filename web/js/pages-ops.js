// W7 · OPS 运维监控（四层视图 + 5 渠道告警 + AI 诊断）
// 对应后端：internal/platform/operations.go、governance.go
import { api, fmt } from './api.js';
import {
  card, dataTable, descList, emptyState, esc, formModal, loading,
  statusTag, tag, toast, bindTabs, tabsHTML, confirmDialog, drawer, modal,
} from './ui.js';

const LEVELS = [
  { key: 'platform', label: '平台层', desc: '全局任务吞吐与资源总量', icon: '◉' },
  { key: 'workspace', label: '工作空间层', desc: '按空间拆分任务与资产', icon: '▣' },
  { key: 'resource', label: '资源层', desc: '工作流与计算资源', icon: '▤' },
  { key: 'task', label: '任务层', desc: '运行实例明细', icon: '▫' },
];

const SEVERITY_TONE = { info: '', warning: 'orange', critical: 'red' };
const SEVERITY_LABEL = { info: '提示', warning: '警告', critical: '严重' };

function healthTone(score) {
  if (score >= 90) return 'green';
  if (score >= 70) return 'orange';
  if (score >= 50) return 'orange';
  return 'red';
}

function healthRing(score, size = 44) {
  const r = size / 2 - 4;
  const c = 2 * Math.PI * r;
  const dash = (c * Math.max(0, Math.min(100, score))) / 100;
  const color = score >= 90 ? 'var(--success,#2ba471)' : score >= 70 ? 'var(--warning,#e37318)' : 'var(--error,#d54941)';
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="flex:none">
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--line,#e7e7e7)" stroke-width="4"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="4"
      stroke-linecap="round" stroke-dasharray="${dash} ${c}" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    <text x="50%" y="50%" dominant-baseline="central" text-anchor="middle"
      font-size="${size * 0.26}" fill="${color}" font-weight="600">${Math.round(score)}</text>
  </svg>`;
}

/* ================================================================ 主入口 */
export async function renderOps2(page, ctx) {
  page.innerHTML = loading();

  const [levelsRes, alertsMeta] = await Promise.all([
    api.get('/ops/levels?depth=all').catch(() => null),
    api.get('/alerts/meta').catch(() => null),
  ]);

  if (!levelsRes) {
    page.innerHTML = emptyState('OPS 视图不可用', '请确认后端 /api/ops/levels 已注册');
    return;
  }

  const tree = levelsRes.tree;
  const health = tree.health || {};
  const metrics = tree.metrics || {};
  const stats = alertsMeta?.stats || {};
  let activeLevel = 'platform';
  let diagnosis = null;

  page.innerHTML = `
    <div class="stat-row">
      <div class="stat"><span class="label">平台健康度</span>
        <span class="value">${health.score ?? '-'}<small>分</small></span>
        <span class="foot">${tag(health.label || '-', healthTone(health.score ?? 0))} 四因子加权</span></div>
      <div class="stat"><span class="label">任务总数</span><span class="value">${metrics.total ?? 0}</span>
        <span class="foot">成功 ${metrics.success ?? 0} / 失败 ${metrics.failed ?? 0} / 运行 ${metrics.running ?? 0}</span></div>
      <div class="stat"><span class="label">成功率</span>
        <span class="value">${metrics.total ? ((metrics.success_rate ?? 0) * 100).toFixed(1) : '-'}<small>%</small></span>
        <span class="foot">最近 ${metrics.total ?? 0} 次运行</span></div>
      <div class="stat"><span class="label">告警渠道</span>
        <span class="value">${stats.enabled_channels ?? 0}<small>/${stats.channels ?? 0}</small></span>
        <span class="foot">24h 投递 ${stats.last_24h ?? 0} · 抑制 ${stats.suppressed ?? 0}</span></div>
    </div>

    <div class="mt-16">${tabsHTML([
      { key: 'levels', label: '四层视图' },
      { key: 'alerts', label: '告警渠道与规则', count: stats.rules ?? 0 },
      { key: 'diagnose', label: 'AI 诊断' },
    ], 'levels', 'ops')}</div>

    <div id="ops-body" class="mt-16"></div>`;

  const body = page.querySelector('#ops-body');

  const renderLevels = () => {
    body.innerHTML = `
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-bottom:12px">
        ${LEVELS.map((l) => `<button class="btn sm ${l.key === activeLevel ? 'primary' : ''}" data-level="${l.key}">
          ${l.icon} ${l.label}</button>`).join('')}
        <span class="spacer" style="flex:1"></span>
        <button class="btn sm" id="ops-expand">展开全部</button>
        <button class="btn sm" id="ops-flat">表格视图</button>
      </div>
      <div class="tiny dim" style="margin-bottom:12px">
        健康度 = 100 × (0.50·成功率 + 0.25·(1-失败率) + 0.15·(1-排队率) + 0.10·运行负载因子)
      </div>
      <div id="ops-tree"></div>`;

    const treeBox = body.querySelector('#ops-tree');

    const renderNodes = () => {
      const levelIndex = LEVELS.findIndex((l) => l.key === activeLevel);
      const depthKeys = LEVELS.slice(0, levelIndex + 1).map((l) => l.key);

      const nodeHTML = (node, depth) => {
        if (!depthKeys.includes(node.level)) return '';
        const canExpand = node.children && node.children.length && depthKeys.indexOf(node.level) < levelIndex;
        const h = node.health || {};
        return `<div style="margin-left:${depth * 20}px" class="ops-node">
          <div class="card" style="padding:12px 14px;margin-bottom:8px">
            <div class="row" style="gap:12px;align-items:center">
              ${healthRing(h.score ?? 100, 40)}
              <div style="flex:1;min-width:0">
                <div class="row" style="gap:8px;align-items:center">
                  <span class="cell-main">${esc(node.name)}</span>
                  ${tag(node.level === 'platform' ? '平台层' : node.level === 'workspace' ? '工作空间' : node.level === 'resource' ? '资源' : '任务',
                        node.level === 'task' ? 'outline' : 'blue')}
                  ${h.label ? tag(h.label, healthTone(h.score ?? 100)) : ''}
                </div>
                <div class="tiny dim" style="margin-top:4px">${esc(h.basis || '')}</div>
              </div>
              <div class="row" style="gap:16px;flex:none">
                ${node.metrics?.status
                  ? statusTag(node.metrics.status)
                  : `<div style="text-align:right"><div class="tiny dim">成功/失败</div>
                     <div class="tiny">${node.metrics?.success ?? 0} / ${node.metrics?.failed ?? 0}</div></div>`}
                ${node.level !== 'task'
                  ? `<div style="text-align:right"><div class="tiny dim">子节点</div>
                     <div class="tiny">${(node.children || []).length}</div></div>` : ''}
                ${node.level === 'task'
                  ? `<a class="btn-link" data-task="${esc(node.id)}">详情</a>` : ''}
              </div>
            </div>
          </div>
          ${canExpand ? (node.children || []).map((c) => nodeHTML(c, depth + 1)).join('') : ''}
        </div>`;
      };

      treeBox.innerHTML = nodeHTML(tree, 0) || emptyState('该层暂无节点');

      treeBox.querySelectorAll('[data-task]').forEach((b) =>
        b.addEventListener('click', () => showTaskDetail(b.dataset.task))
      );
    };

    renderNodes();

    body.querySelectorAll('[data-level]').forEach((b) =>
      b.addEventListener('click', () => {
        activeLevel = b.dataset.level;
        renderLevels();
      })
    );
    body.querySelector('#ops-expand').addEventListener('click', () => {
      activeLevel = 'task';
      renderLevels();
    });
    body.querySelector('#ops-flat').addEventListener('click', () => showFlatView(activeLevel));
  };

  const renderAlerts = async () => {
    body.innerHTML = loading('加载告警配置…');
    const [chRes, ruleRes, dlRes, meta] = await Promise.all([
      api.get('/alerts/channels').catch(() => ({ channels: [] })),
      api.get('/alerts/rules').catch(() => ({ rules: [] })),
      api.get('/alerts/deliveries?limit=60').catch(() => ({ deliveries: [] })),
      api.get('/alerts/meta').catch(() => null),
    ]);

    const channelTypes = meta?.channels || chRes.types || [];
    const events = meta?.events || ruleRes.events || [];
    const channels = chRes.channels || [];
    const rules = ruleRes.rules || [];
    const deliveries = dlRes.deliveries || [];

    // 每种渠道一张卡片：已配置的显示状态，未配置的显示「未配置」
    const channelCards = channelTypes.map((t) => {
      const configured = channels.filter((c) => c.type === t.type);
      const ch = configured[0];
      return `<div class="card" style="padding:14px">
        <div class="row" style="gap:10px;align-items:flex-start">
          <div style="width:32px;height:32px;border-radius:8px;background:var(--bg-2,#f3f3f3);
            display:flex;align-items:center;justify-content:center;font-size:15px;flex:none">${esc(t.icon || '•')}</div>
          <div style="flex:1;min-width:0">
            <div class="row" style="gap:6px;align-items:center">
              <span class="cell-main">${esc(t.label)}</span>
              ${ch
                ? tag(ch.enabled ? '已启用' : '已停用', ch.enabled ? 'green' : '')
                : tag('未配置', 'outline')}
              ${ch && !ch.valid ? tag('配置有问题', 'red') : ''}
            </div>
            <div class="tiny dim" style="margin-top:4px">${esc(t.desc)}</div>
            ${ch ? `<div class="tiny dim" style="margin-top:6px;font-family:var(--font-mono,monospace)">
              ${esc(ch.name)}${ch.last_test_at ? ` · 最近测试 ${fmt.ago(ch.last_test_at)}` : ''}</div>` : ''}
          </div>
        </div>
        <div class="row" style="gap:6px;margin-top:12px">
          ${ch
            ? `<button class="btn sm" data-test="${ch.id}">发送测试</button>
               <button class="btn sm" data-edit-ch="${ch.id}">编辑</button>
               <button class="btn sm danger" data-del-ch="${ch.id}">删除</button>`
            : `<button class="btn sm primary" data-new-ch="${t.type}">＋ 配置</button>`}
        </div>
      </div>`;
    }).join('');

    body.innerHTML = `
      <div class="card-head" style="padding:0 0 10px">
        <div><h3>告警渠道</h3>
          <div class="sub">${channelTypes.length || 6} 种渠道 · 真实出网投递（设 DATABUDDY_ALERT_SIMULATE=1 可退回离线模拟）· 同一事件 5 分钟内去重抑制</div></div>
        <button class="btn sm" id="emit-test">手动触发一次事件</button>
      </div>
      <div class="grid-3" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">
        ${channelCards}
      </div>

      <div class="card flush mt-16">
        <div class="tbl-toolbar"><span class="title">告警规则</span>
          <span class="dim tiny">4 类事件：${events.map((e) => esc(e.label)).join(' / ')}</span>
          <span class="spacer"></span><button class="btn sm primary" id="new-rule">＋ 新建规则</button></div>
        ${dataTable({
          rows: rules,
          columns: [
            { title: '规则名称', render: (r) => `<span class="cell-main">${esc(r.name)}</span>` },
            { title: '事件', render: (r) => tag(r.event_label || r.event, 'blue') },
            { title: '级别', render: (r) => tag(r.severity_label || r.severity, SEVERITY_TONE[r.severity] || '') },
            { title: '渠道', render: (r) => (r.channel_names || []).length
                ? (r.channel_names || []).map((n) => tag(n, 'outline')).join(' ')
                : '<span class="dim tiny">未绑定</span>' },
            { title: '投递次数', align: 'right', render: (r) => fmt.num(r.delivery_count || 0) },
            { title: '状态', render: (r) => tag(r.enabled ? '启用' : '停用', r.enabled ? 'green' : '') },
            { title: '操作', key: 'ops', render: (r) => `
              <a class="btn-link" data-toggle-rule="${r.id}">${r.enabled ? '停用' : '启用'}</a>
              <a class="btn-link" data-del-rule="${r.id}" style="color:var(--error)">删除</a>` },
          ],
          emptyText: '还没有告警规则', emptyHint: '新建规则后，工作流失败/质量失败/漂移/异常会自动触发告警',
        })}
      </div>

      <div class="card flush mt-16">
        <div class="tbl-toolbar"><span class="title">投递记录</span>
          <span class="dim tiny">suppressed = 被 5 分钟去重抑制</span>
          <span class="spacer"></span><span class="tiny dim">共 ${deliveries.length} 条</span></div>
        ${dataTable({
          rows: deliveries,
          columns: [
            { title: '时间', render: (r) => `<span class="dim">${fmt.dt(r.created_at)}</span>` },
            { title: '事件', render: (r) => tag(r.event || '-', 'outline') },
            { title: '渠道', render: (r) => `${esc(r.channel_name || r.channel_type || '-')}
                <span class="tiny dim">${esc(r.channel_type || '')}</span>` },
            { title: '主题', render: (r) => `<span class="tiny">${esc((r.subject || '').slice(0, 48))}</span>` },
            { title: '级别', render: (r) => tag(SEVERITY_LABEL[r.severity] || r.severity || '-',
                SEVERITY_TONE[r.severity] || '') },
            { title: '状态', render: (r) => r.status === 'sent'
                ? tag('已投递', 'green')
                : r.status === 'suppressed' ? tag('已抑制', 'orange') : tag('失败', 'red') },
            { title: '模拟', render: (r) => r.simulated ? tag('simulated', 'outline') : tag('真实', 'blue') },
            { title: '操作', key: 'ops', render: (r) => `<a class="btn-link" data-dl="${r.id}">报文</a>` },
          ],
          emptyText: '暂无投递记录', emptyHint: '点「发送测试」或「手动触发一次事件」',
        })}
      </div>`;

    // ---- 事件绑定
    body.querySelectorAll('[data-new-ch]').forEach((b) =>
      b.addEventListener('click', () => newChannel(ctx, b.dataset.newCh, channelTypes, renderAlerts))
    );
    body.querySelectorAll('[data-edit-ch]').forEach((b) =>
      b.addEventListener('click', () => {
        const ch = channels.find((c) => c.id === b.dataset.editCh);
        newChannel(ctx, ch.type, channelTypes, renderAlerts, ch);
      })
    );
    body.querySelectorAll('[data-del-ch]').forEach((b) =>
      b.addEventListener('click', async () => {
        if (!(await confirmDialog('删除告警渠道', '删除后绑定该渠道的规则将无法投递，确认？', '删除'))) return;
        await api.del(`/alerts/channels/${b.dataset.delCh}`);
        toast('渠道已删除', 'ok');
        renderAlerts();
      })
    );
    body.querySelectorAll('[data-test]').forEach((b) =>
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          const res = await api.post(`/alerts/channels/${b.dataset.test}/test`, {});
          showPayloadModal('渠道测试结果', res.payload, res.issues);
          toast(res.ok ? '测试投递完成（模拟）' : '测试失败：配置有问题', res.ok ? 'ok' : 'err');
        } catch (err) {
          toast(err.message || '测试失败', 'err');
        } finally {
          b.disabled = false;
        }
      })
    );
    body.querySelectorAll('[data-dl]').forEach((b) =>
      b.addEventListener('click', () => {
        const d = deliveries.find((x) => x.id === b.dataset.dl);
        showPayloadModal('投递报文', d.payload, [], d);
      })
    );
    body.querySelectorAll('[data-del-rule]').forEach((b) =>
      b.addEventListener('click', async () => {
        if (!(await confirmDialog('删除告警规则', '确认删除该规则？', '删除'))) return;
        await api.del(`/alerts/rules/${b.dataset.delRule}`);
        toast('规则已删除', 'ok');
        renderAlerts();
      })
    );
    body.querySelectorAll('[data-toggle-rule]').forEach((b) =>
      b.addEventListener('click', async () => {
        const r = rules.find((x) => x.id === b.dataset.toggleRule);
        await api.patch(`/alerts/rules/${r.id}`, { enabled: !r.enabled });
        toast(r.enabled ? '规则已停用' : '规则已启用', 'ok');
        renderAlerts();
      })
    );
    body.querySelector('#new-rule').addEventListener('click', () =>
      newRule(channels, events, renderAlerts)
    );
    body.querySelector('#emit-test').addEventListener('click', () =>
      emitTest(events, renderAlerts)
    );
  };

  const renderDiagnose = async () => {
    body.innerHTML = loading('规则引擎推理中…');
    diagnosis = await api.get('/ops/diagnose?scope=platform').catch(() => null);
    if (!diagnosis) {
      body.innerHTML = emptyState('诊断不可用');
      return;
    }
    const h = diagnosis.health || {};
    const sevTone = (s) => SEVERITY_TONE[s] || '';
    body.innerHTML = `
      <div class="card">
        <div class="card-head">
          <div><h3>AI 诊断结论</h3>
            <div class="sub">基于规则推理（失败率 / 连续失败 / 超时 / 排队 / 资源 / 质量 / 异常），不调用外部 LLM</div></div>
          <div class="row" style="gap:8px">
            ${tag(`引擎 ${diagnosis.engine}`, 'outline')}
            ${tag(`健康度 ${h.score}`, healthTone(h.score ?? 100))}
          </div>
        </div>
        <div class="row" style="gap:16px;align-items:flex-start">
          ${healthRing(h.score ?? 100, 64)}
          <div style="flex:1;font-size:13px;line-height:1.9">${esc(diagnosis.summary)}</div>
        </div>
        <div class="row mt-16" style="gap:8px;flex-wrap:wrap">
          <button class="btn sm primary" id="diag-platform">平台层</button>
          <button class="btn sm" id="diag-workspace">工作空间层</button>
          <button class="btn sm" id="diag-flow">按工作流</button>
        </div>
      </div>

      <div class="grid-2 mt-16">
        ${card('根因定位', (diagnosis.root_causes || []).length
          ? `<div class="col" style="gap:10px">${(diagnosis.root_causes || []).map((c) => `
            <div class="info-strip" style="display:block">
              <div class="row" style="gap:8px;margin-bottom:4px">
                ${tag(SEVERITY_LABEL[c.severity] || c.severity, sevTone(c.severity))}
                <span class="cell-main">${esc(c.title)}</span>
              </div>
              <div class="tiny" style="line-height:1.8">${esc(c.detail)}</div>
            </div>`).join('')}</div>`
          : emptyState('未发现异常信号', '当前运行状态健康'),
          { sub: `${(diagnosis.root_causes || []).length} 条信号` })}
        ${card('修复建议', (diagnosis.suggestions || []).length
          ? `<div class="col" style="gap:10px">${(diagnosis.suggestions || []).map((s, i) => `
            <div style="display:flex;gap:10px">
              <span class="tag outline" style="flex:none">${i + 1}</span>
              <div><div class="cell-main tiny">${esc(s.title)}</div>
                <div class="tiny dim" style="line-height:1.8;margin-top:2px">${esc(s.action)}</div></div>
            </div>`).join('')}</div>`
          : emptyState('暂无建议'),
          { sub: '按优先级排序' })}
      </div>

      <div class="card flush mt-16">
        <div class="tbl-toolbar"><span class="title">诊断阈值</span>
          <span class="dim tiny">规则引擎的全部判据，可复现、可解释</span></div>
        ${dataTable({
          rows: diagnosis.rules_applied || [],
          columns: [
            { title: '规则', render: (r) => `<span class="mono tiny">${esc(r.id)}</span>` },
            { title: '说明', render: (r) => esc(r.label) },
            { title: '阈值', align: 'right', render: (r) => fmt.num(r.threshold) },
          ],
        })}
      </div>`;

    body.querySelector('#diag-platform').addEventListener('click', () => renderDiagnose());
    body.querySelector('#diag-workspace').addEventListener('click', () => renderDiagnose());
    body.querySelector('#diag-flow').addEventListener('click', () => pickFlowDiagnose(body));
  };

  const render = (key) => {
    if (key === 'levels') renderLevels();
    else if (key === 'alerts') renderAlerts();
    else renderDiagnose();
  };

  bindTabs(page, render);
  render('levels');

  // ---------------------------------------------------------------- 内部件
  async function showFlatView(depth) {
    const out = await api.get(`/ops/tree-flat?depth=${depth}`).catch(() => null);
    if (!out) return;
    drawer({
      title: `四层视图 · ${LEVELS.find((l) => l.key === depth)?.label || depth}（拍平）`,
      wide: true,
      body: dataTable({
        rows: out.nodes || [],
        columns: [
          { title: '层级', render: (r) => tag(r.level, 'outline') },
          { title: '名称', render: (r) => `<span class="cell-main">${esc(r.name)}</span>` },
          { title: '路径', render: (r) => `<span class="mono tiny">${esc(r.path)}</span>` },
          { title: '健康度', align: 'right', render: (r) => tag(String(r.health_score),
              healthTone(r.health_score ?? 100)) },
          { title: '子节点', align: 'right', render: (r) => fmt.num(r.child_count || 0) },
        ],
      }),
    });
  }

  async function showTaskDetail(nodeId) {
    const runId = String(nodeId).replace(/^task:/, '');
    const out = await api.get(`/ops/diagnose?scope=task:${encodeURIComponent(runId)}`).catch(() => null);
    const res = out || { summary: '无法获取诊断', root_causes: [], suggestions: [] };
    modal({
      title: '任务详情与诊断',
      wide: true,
      body: `<div class="col" style="gap:12px">
        ${descList([
          ['运行实例', `<span class="mono tiny">${esc(runId)}</span>`],
          ['诊断范围', esc(res.scope_label || '-')],
          ['引擎', esc(res.engine || '-')],
        ])}
        <div class="info-strip" style="display:block"><div class="tiny" style="line-height:1.9">${esc(res.summary)}</div></div>
        ${(res.root_causes || []).length
          ? `<div><div class="tiny dim" style="margin-bottom:6px">根因</div>${(res.root_causes || []).map((c) => `
            <div class="row" style="gap:8px;margin-bottom:6px">
              ${tag(SEVERITY_LABEL[c.severity] || c.severity, SEVERITY_TONE[c.severity] || '')}
              <span class="tiny" style="flex:1">${esc(c.title)} — ${esc(c.detail)}</span></div>`).join('')}</div>`
          : '<div class="tag green">未发现异常信号</div>'}
      </div>`,
      actions: [{ label: '关闭' }],
    });
  }

  async function pickFlowDiagnose(box) {
    const out = await api.get('/ops/levels?depth=resource').catch(() => null);
    const flows = [];
    const walk = (node) => {
      if (node.kind === 'flow') flows.push(node);
      (node.children || []).forEach(walk);
    };
    if (out?.tree) walk(out.tree);
    if (!flows.length) {
      toast('还没有工作流资源', 'err');
      return;
    }
    modal({
      title: '按工作流诊断',
      body: `<div class="col" style="gap:8px">${flows.map((f) => `
        <button class="btn" data-pick-flow="${esc(f.flow_id)}" style="justify-content:flex-start;text-align:left">
          <span style="flex:1">${esc(f.name)}</span>
          ${tag(String(f.health_score), healthTone(f.health_score ?? 100))}
        </button>`).join('')}</div>`,
      onMount: (content, close) => {
        content.querySelectorAll('[data-pick-flow]').forEach((b) =>
          b.addEventListener('click', async () => {
            close(false);
            const res = await api.get(`/ops/diagnose?scope=flow:${b.dataset.pickFlow}`).catch(() => null);
            if (!res) return toast('诊断失败', 'err');
            diagnosis = res;
            body.innerHTML = `<div class="card">
              <div class="card-head"><div><h3>${esc(res.scope_label)} · 诊断</h3>
                <div class="sub">规则引擎推理</div></div>
                ${tag(`健康度 ${res.health?.score}`, healthTone(res.health?.score ?? 100))}</div>
              <div style="font-size:13px;line-height:1.9">${esc(res.summary)}</div>
              <div class="col mt-16" style="gap:10px">
                ${(res.root_causes || []).map((c) => `<div class="info-strip" style="display:block">
                  <div class="row" style="gap:8px"><span class="cell-main">${esc(c.title)}</span>
                    ${tag(SEVERITY_LABEL[c.severity] || c.severity, SEVERITY_TONE[c.severity] || '')}</div>
                  <div class="tiny" style="line-height:1.8">${esc(c.detail)}</div></div>`).join('')
                  || '<div class="tag green">未发现异常信号</div>'}
              </div>
              <div class="col mt-16" style="gap:8px">
                ${(res.suggestions || []).map((s, i) => `<div class="tiny" style="line-height:1.8">
                  ${i + 1}. ${esc(s.action)}</div>`).join('')}
              </div>
            </div>`;
          })
        );
      },
    });
  }
}

/* ------------------------------------------------------------ 渠道配置弹窗 */
async function newChannel(ctx, type, types, refresh, existing) {
  const spec = types.find((t) => t.type === type);
  if (!spec) return toast('未知渠道类型', 'err');
  const fields = [
    { label: '渠道名称', name: 'name', required: true, value: existing?.name || spec.label },
    ...(spec.fields || []).map((f) => ({
      label: f.label + (f.required ? '' : '（可选）'),
      name: f.key,
      type: f.type === 'select' ? 'select' : f.type === 'json' ? 'textarea' : 'text',
      options: f.options,
      hint: f.hint,
      required: f.required,
      value: existing?.config?.[f.key] ?? '',
    })),
    { label: '启用', name: 'enabled', type: 'checkbox', value: existing ? existing.enabled : true },
  ];

  const data = await formModal({
    title: `${existing ? '编辑' : '配置'}告警渠道 · ${spec.label}`,
    wide: true,
    intro: `<div class="info-strip" style="margin-bottom:12px">
      <span class="tag blue">${esc(spec.label)}</span>
      告警与报告推送会<b>真实出网投递</b>到该目标；如需离线演练，请设置环境变量
      <code>DATABUDDY_ALERT_SIMULATE=1</code>，此时只生成应发报文并落库。</div>`,
    fields,
    submitText: existing ? '保存' : '创建',
    onSubmit: async (d) => {
      const config = {};
      for (const f of spec.fields || []) {
        const raw = d[f.key];
        if (raw === '' || raw === undefined) continue;
        if (f.type === 'json') {
          try { config[f.key] = JSON.parse(raw); }
          catch { throw new Error(`「${f.label}」不是合法 JSON`); }
        } else config[f.key] = raw;
      }
      if (existing) {
        const res = await api.patch(`/alerts/channels/${existing.id}`, {
          name: d.name, config, enabled: d.enabled,
        });
        if (res.channel?.issues?.length) toast('已保存，但配置仍有问题', 'err');
        else toast('渠道已更新', 'ok');
      } else {
        const res = await api.post('/alerts/channels', {
          type, name: d.name, config, enabled: d.enabled,
        });
        if (res.issues?.length) toast('已创建，但配置有问题：' + res.issues[0], 'err');
        else toast('渠道已创建', 'ok');
      }
    },
  });
  if (data) refresh();
}

/* -------------------------------------------------------------- 规则弹窗 */
async function newRule(channels, events, refresh) {
  if (!channels.length) {
    toast('请先配置至少一个告警渠道', 'err');
    return;
  }
  const data = await formModal({
    title: '新建告警规则',
    wide: true,
    intro: `<div class="info-strip" style="margin-bottom:12px">
      规则命中后向绑定渠道投递；同一事件 <b>5 分钟</b>内只发一次，重复的记为 suppressed。</div>`,
    fields: [
      { label: '规则名称', name: 'name', required: true, placeholder: '例如 每日同步失败告警' },
      {
        label: '事件类型', name: 'event', type: 'select', required: true,
        options: events.map((e) => ({ value: e.event, label: `${e.label} — ${e.desc}` })),
      },
      {
        label: '严重级别', name: 'severity', type: 'select',
        options: [
          { value: 'info', label: '提示' },
          { value: 'warning', label: '警告' },
          { value: 'critical', label: '严重' },
        ],
      },
      {
        label: '绑定渠道', name: 'channels', type: 'select', required: true,
        options: channels.filter((c) => c.enabled).map((c) => ({
          value: c.id, label: `${c.name}（${c.type}）`,
        })),
        hint: '当前仅支持单渠道选择；多渠道可在创建后通过 API 追加',
      },
      { label: '启用', name: 'enabled', type: 'checkbox', value: true },
    ],
    submitText: '创建规则',
    onSubmit: async (d) => {
      await api.post('/alerts/rules', {
        name: d.name,
        event: d.event,
        channels: d.channels ? [d.channels] : [],
        severity: d.severity,
        enabled: d.enabled,
        condition: {},
      });
      toast('规则已创建', 'ok');
    },
  });
  if (data) refresh();
}

/* ------------------------------------------------------- 手动触发事件弹窗 */
async function emitTest(events, refresh) {
  const data = await formModal({
    title: '手动触发一次告警事件',
    wide: true,
    intro: `<div class="info-strip" style="margin-bottom:12px">
      用于验证「规则匹配 → 渠道渲染 → 投递落库」整条链路。</div>`,
    fields: [
      {
        label: '事件类型', name: 'event', type: 'select', required: true,
        options: events.map((e) => ({ value: e.event, label: e.label })),
      },
      { label: '主题', name: 'subject', required: true, value: '手动测试告警' },
      { label: '正文', name: 'body', type: 'textarea', value: '这是一条手动触发的测试告警。' },
      {
        label: '严重级别', name: 'severity', type: 'select',
        options: [
          { value: '', label: '跟随事件默认' },
          { value: 'info', label: '提示' },
          { value: 'warning', label: '警告' },
          { value: 'critical', label: '严重' },
        ],
      },
      { label: '强制发送（跳过去重抑制）', name: 'force', type: 'checkbox', value: false },
    ],
    submitText: '触发',
    onSubmit: async (d) => {
      const res = await api.post('/alerts/emit', {
        event: d.event, subject: d.subject, body: d.body,
        severity: d.severity, force: !!d.force,
      });
      modal({
        title: '事件分发结果',
        wide: true,
        body: `<div class="col" style="gap:12px">
          ${descList([
            ['事件', esc(res.event_label || res.event)],
            ['匹配规则', fmt.num(res.matched || 0)],
            ['已投递', fmt.num((res.sent || []).length)],
            ['已抑制', fmt.num((res.suppressed || []).length)],
            ['失败', fmt.num((res.failed || []).length)],
            ['跳过', fmt.num((res.skipped || []).length)],
          ])}
          ${(res.sent || []).length ? `<div><div class="tiny dim" style="margin-bottom:6px">已投递</div>
            ${(res.sent || []).map((s) => `<div class="tiny">✓ ${esc(s.channel)} · ${esc(s.rule)}</div>`).join('')}</div>` : ''}
          ${(res.suppressed || []).length ? `<div><div class="tiny dim" style="margin-bottom:6px">被抑制（5 分钟内重复）</div>
            ${(res.suppressed || []).map((s) => `<div class="tiny dim">⊘ ${esc(s.channel)} — ${esc(s.reason)}</div>`).join('')}</div>` : ''}
          ${(res.failed || []).length ? `<div><div class="tiny dim" style="margin-bottom:6px">失败</div>
            ${(res.failed || []).map((s) => `<div class="tiny" style="color:var(--error)">✕ ${esc(s.channel)} — ${esc(s.error)}</div>`).join('')}</div>` : ''}
          ${(res.skipped || []).length ? `<div><div class="tiny dim" style="margin-bottom:6px">跳过</div>
            ${(res.skipped || []).map((s) => `<div class="tiny dim">- ${esc(s.rule || s.channel || '')}：${esc(s.reason)}</div>`).join('')}</div>` : ''}
        </div>`,
        actions: [{ label: '关闭', primary: true }],
      });
    },
  });
  if (data) refresh();
}

/* ------------------------------------------------------------ 报文查看弹窗 */
function showPayloadModal(title, payload, issues, delivery) {
  const body = `<div class="col" style="gap:12px">
    ${issues && issues.length ? `<div class="info-strip" style="display:block;border-color:var(--error)">
      <div class="tiny" style="color:var(--error)">配置问题：</div>
      ${issues.map((i) => `<div class="tiny" style="color:var(--error)">· ${esc(i)}</div>`).join('')}</div>` : ''}
    ${delivery ? descList([
      ['渠道', esc(delivery.channel_name || delivery.channel_type || '-')],
      ['事件', esc(delivery.event || '-')],
      ['状态', esc(delivery.status || '-')],
      ['去重键', `<span class="mono tiny">${esc(delivery.dedup_key || '-')}</span>`],
      ['时间', fmt.dt(delivery.created_at)],
      ['模拟', delivery.simulated ? '是（未真实外发）' : '否'],
    ]) : ''}
    <div><div class="tiny dim" style="margin-bottom:6px">应发报文（JSON）</div>
      <pre class="code" style="max-height:400px;overflow:auto">${esc(JSON.stringify(payload || {}, null, 2))}</pre></div>
    <div class="info-strip"><span class="tag green">simulated</span>
      开源版本不发起真实网络请求；该报文已落库到 alert_deliveries。</div>
  </div>`;
  modal({ title, wide: true, body, actions: [{ label: '关闭', primary: true }] });
}
