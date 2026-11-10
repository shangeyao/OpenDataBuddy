// CI/CD：bundle 配置即代码。左侧 YAML 编辑器 + 动作按钮，右侧环境卡与部署历史。
import { api, fmt } from './api.js';
import {
  bindTabs, card, confirmDialog, descList, el, emptyState, esc,
  formModal, loading, statusTag, tabsHTML, tag, toast,
} from './ui.js';

const ENV_LABEL = { dev: '开发', staging: '预发', prod: '生产' };
const ENV_TONE = { dev: '#0052D9', staging: '#ED7B2F', prod: '#D54941' };

const ACTION_LABEL = {
  deploy: '发布',
  run: '运行',
  destroy: '销毁',
  validate: '校验',
  init: '初始化',
};

function envChip(env) {
  const color = ENV_TONE[env] || '#888';
  return `<span class="cd-env" style="--c:${color}">${esc(ENV_LABEL[env] || env)}</span>`;
}

export function renderCicd(page, ctx) {
  const root = ctx.root || page;
  root.innerHTML = '';
  const state = {
    bundles: [],
    current: null,
    envs: ['dev', 'staging', 'prod'],
    kinds: [],
    tab: 'editor',
    dirty: false,
  };

  const shell = el('div', { class: 'cd-wrap' });
  root.appendChild(shell);

  async function loadBundles(selectId) {
    shell.innerHTML = loading('加载 bundle…');
    try {
      const data = await api.get('/bundles');
      state.bundles = data.bundles || [];
      state.envs = data.envs || state.envs;
      state.kinds = data.kinds || [];
      if (!state.current && state.bundles.length) {
        state.current = state.bundles[0];
      } else if (selectId) {
        state.current = state.bundles.find((b) => b.id === selectId) || state.current;
      }
      render();
    } catch (err) {
      shell.innerHTML = emptyState('加载失败', err.message || String(err));
    }
  }

  async function selectBundle(id) {
    if (state.dirty && !(await confirmDialog('放弃未保存的修改？', '当前 YAML 尚未保存，切换后会丢失。'))) {
      return;
    }
    try {
      const data = await api.get(`/bundles/${id}`);
      state.current = data.bundle;
      state.dirty = false;
      render();
    } catch (err) {
      toast(err.message || '加载失败', 'err');
    }
  }

  function editorPane() {
    const b = state.current;
    const yamlText = b?.yaml || '';
    const wrap = el('div', { class: 'cd-editor-pane' });
    wrap.innerHTML = `
      <div class="cd-toolbar">
        <div class="cd-bundle-pick">
          <select id="cd-bundle">${state.bundles
            .map(
              (x) =>
                `<option value="${esc(x.id)}"${x.id === b?.id ? ' selected' : ''}>${esc(x.name)}</option>`
            )
            .join('')}</select>
          <button class="btn ghost" id="cd-new" title="新建 bundle">+ 新建</button>
        </div>
        <div class="cd-actions">
          <select id="cd-env">${state.envs
            .map((e) => `<option value="${esc(e)}">${esc(ENV_LABEL[e] || e)}</option>`)
            .join('')}</select>
          <button class="btn" id="cd-validate">校验</button>
          <button class="btn primary" id="cd-deploy">发布</button>
          <button class="btn" id="cd-run">运行</button>
          <button class="btn danger" id="cd-destroy">销毁</button>
        </div>
      </div>
      <textarea id="cd-yaml" class="cd-yaml" spellcheck="false"
        placeholder="bundle: my-bundle&#10;targets:&#10;  dev:&#10;    workspace: 默认工作空间&#10;resources:&#10;  flows: []">${esc(yamlText)}</textarea>
      <div class="cd-foot">
        <button class="btn primary" id="cd-save">保存 YAML</button>
        <span class="cd-hint" id="cd-hint">${b ? `最后更新 ${fmt.dt(b.updated_at)}` : ''}</span>
        <span class="cd-dirty" id="cd-dirty"></span>
      </div>
    `;
    return wrap;
  }

  function resultPane() {
    const wrap = el('div', { class: 'cd-result-pane' });
    wrap.innerHTML = `
      <div id="cd-envs" class="cd-env-cards"></div>
      <div id="cd-issues"></div>
      <div id="cd-history"></div>
    `;
    return wrap;
  }

  function envCards(summary) {
    const envs = summary?.envs || {};
    return Object.keys(envs)
      .map((env) => {
        const e = envs[env];
        const last = e.last_deploy;
        return `
        <div class="cd-env-card" style="--c:${ENV_TONE[env] || '#888'}">
          <div class="cd-env-head">${envChip(env)}
            <span class="cd-drift ${e.drift ? 'is-warn' : ''}">${e.drift ? `${e.drift} 项未发布` : '已同步'}</span>
          </div>
          <div class="cd-env-stat"><b>${e.present}</b><span>/ ${e.declared} 资源已就绪</span></div>
          <div class="cd-env-last">${
            last
              ? `最近${esc(ACTION_LABEL[last.action] || last.action)} ${fmt.dt(last.created_at)}`
              : '尚未发布'
          }</div>
        </div>`;
      })
      .join('');
  }

  function issuesHTML(issues) {
    if (!issues || !issues.length) {
      return `<div class="cd-ok-box">校验通过，未发现问题。</div>`;
    }
    const tone = { error: 'err', warning: 'warn', info: '' };
    return `<div class="cd-issue-list">${issues
      .map(
        (i) => `<div class="cd-issue cd-issue-${tone[i.level] || 'info'}">
          <span class="cd-issue-lv">${i.level === 'error' ? '错误' : i.level === 'warning' ? '警告' : '提示'}</span>
          <code>${esc(i.path || '')}</code>
          <span>${esc(i.message || '')}</span>
        </div>`
      )
      .join('')}</div>`;
  }

  function historyHTML(history) {
    if (!history || !history.length) {
      return `<div class="cd-empty">还没有部署记录。</div>`;
    }
    return `<div class="cd-timeline">${history
      .map((h) => {
        const log = Array.isArray(h.log) ? h.log : [];
        const counts = { created: 0, updated: 0, unchanged: 0, removed: 0 };
        log.forEach((l) => {
          if (l.action && counts[l.action] !== undefined) counts[l.action] += 1;
        });
        const detail = log
          .slice(0, 6)
          .map(
            (l) =>
              `<span class="cd-log-item">${esc(l.kind_label || l.kind || '')}·${esc(l.name || '')}<b class="cd-act-${esc(l.action || '')}">${esc(l.action || '')}</b></span>`
          )
          .join('');
        return `<div class="cd-tl-row">
          <div class="cd-tl-dot cd-tl-${h.status === 'success' ? 'ok' : 'bad'}"></div>
          <div class="cd-tl-body">
            <div class="cd-tl-head">${envChip(h.env)}
              <b>${esc(ACTION_LABEL[h.action] || h.action)}</b>
              ${statusTag(h.status)}
              <span class="cd-tl-time">${fmt.dt(h.created_at)}</span>
            </div>
            <div class="cd-tl-sum">${esc(h.summary || '')}</div>
            ${detail ? `<div class="cd-tl-detail">${detail}</div>` : ''}
          </div>
        </div>`;
      })
      .join('')}</div>`;
  }

  async function refreshSummary() {
    if (!state.current) return;
    const envBox = shell.querySelector('#cd-envs');
    const hisBox = shell.querySelector('#cd-history');
    try {
      const s = await api.get(`/bundles/${state.current.id}/summary`);
      if (envBox) envBox.innerHTML = envCards(s);
      if (hisBox) hisBox.innerHTML = historyHTML(s.history);
      const issBox = shell.querySelector('#cd-issues');
      if (issBox) issBox.innerHTML = issuesHTML(s.issues);
    } catch (err) {
      if (hisBox) hisBox.innerHTML = `<div class="cd-empty">${esc(err.message || '概览加载失败')}</div>`;
    }
  }

  function bindPane() {
    const ta = shell.querySelector('#cd-yaml');
    const dirtyMark = shell.querySelector('#cd-dirty');
    if (ta) {
      ta.addEventListener('input', () => {
        state.dirty = true;
        if (dirtyMark) dirtyMark.textContent = '● 未保存';
      });
      // Tab 缩进，YAML 手写更顺手
      ta.addEventListener('keydown', (e) => {
        if (e.key === 'Tab') {
          e.preventDefault();
          const s = ta.selectionStart;
          ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(ta.selectionEnd);
          ta.selectionStart = ta.selectionEnd = s + 2;
          state.dirty = true;
          if (dirtyMark) dirtyMark.textContent = '● 未保存';
        }
      });
    }

    const pick = shell.querySelector('#cd-bundle');
    pick?.addEventListener('change', () => selectBundle(pick.value));

    shell.querySelector('#cd-new')?.addEventListener('click', newBundle);
    shell.querySelector('#cd-save')?.addEventListener('click', saveYaml);
    shell.querySelector('#cd-validate')?.addEventListener('click', doValidate);
    shell.querySelector('#cd-deploy')?.addEventListener('click', doDeploy);
    shell.querySelector('#cd-run')?.addEventListener('click', doRun);
    shell.querySelector('#cd-destroy')?.addEventListener('click', doDestroy);
  }

  function currentEnv() {
    return shell.querySelector('#cd-env')?.value || 'dev';
  }

  function yamlValue() {
    return shell.querySelector('#cd-yaml')?.value || '';
  }

  async function newBundle() {
    formModal({
      title: '新建 Bundle',
      fields: [
        {
          name: 'name',
          label: 'Bundle 名称',
          placeholder: 'retail-dw',
          hint: '用于标识这组配置，建议用业务域命名',
        },
      ],
      submitText: '创建',
      onSubmit: async (vals) => {
        const name = (vals.name || '').trim();
        if (!name) {
          toast('请填写名称', 'err');
          return false;
        }
        try {
          const r = await api.post('/bundles', { name });
          toast('已创建', 'ok');
          state.dirty = false;
          await loadBundles(r.bundle?.id);
          return true;
        } catch (err) {
          toast(err.message || '创建失败', 'err');
          return false;
        }
      },
    });
  }

  async function saveYaml() {
    if (!state.current) return toast('请先创建 bundle', 'err');
    try {
      await api.patch(`/bundles/${state.current.id}`, { yaml: yamlValue() });
      state.dirty = false;
      const mark = shell.querySelector('#cd-dirty');
      if (mark) mark.textContent = '';
      toast('已保存', 'ok');
      await refreshSummary();
    } catch (err) {
      toast(err.message || '保存失败', 'err');
    }
  }

  async function doValidate() {
    if (!state.current) return toast('请先创建 bundle', 'err');
    // 先存再校验，保证校验的是屏幕上的内容
    try {
      await api.patch(`/bundles/${state.current.id}`, { yaml: yamlValue() });
      state.dirty = false;
    } catch (err) {
      return toast(err.message || '保存失败', 'err');
    }
    try {
      const r = await api.post(`/bundles/${state.current.id}/validate`, {});
      const box = shell.querySelector('#cd-issues');
      if (box) box.innerHTML = issuesHTML(r.issues);
      const errs = (r.issues || []).filter((i) => i.level === 'error').length;
      const warns = (r.issues || []).filter((i) => i.level === 'warning').length;
      if (r.ok) toast(`校验通过${warns ? `（${warns} 条警告）` : ''}`, 'ok');
      else toast(`校验未通过：${errs} 个错误`, 'err');
    } catch (err) {
      toast(err.message || '校验失败', 'err');
    }
  }

  async function doDeploy() {
    if (!state.current) return toast('请先创建 bundle', 'err');
    const env = currentEnv();
    try {
      await api.patch(`/bundles/${state.current.id}`, { yaml: yamlValue() });
      state.dirty = false;
    } catch (err) {
      return toast(err.message || '保存失败', 'err');
    }
    try {
      const r = await api.post(`/bundles/${state.current.id}/deploy`, { env });
      if (!r.ok) {
        const box = shell.querySelector('#cd-issues');
        if (box) box.innerHTML = issuesHTML(r.issues);
        return toast('校验未通过，发布中止', 'err');
      }
      const c = r.counts || {};
      toast(`发布完成：新建 ${c.created || 0}，更新 ${c.updated || 0}，未变 ${c.unchanged || 0}`, 'ok');
      await refreshSummary();
    } catch (err) {
      toast(err.message || '发布失败', 'err');
    }
  }

  async function doRun() {
    if (!state.current) return toast('请先创建 bundle', 'err');
    const env = currentEnv();
    try {
      const r = await api.post(`/bundles/${state.current.id}/run`, { env });
      const ok = (r.runs || []).filter((x) => x.ok).length;
      const total = (r.runs || []).length;
      if (!total) return toast(r.message || 'bundle 中没有可运行的工作流', '');
      toast(r.ok ? `已触发 ${total} 个工作流，全部成功` : `已触发 ${total} 个，成功 ${ok}`, r.ok ? 'ok' : 'err');
      await refreshSummary();
    } catch (err) {
      toast(err.message || '运行失败', 'err');
    }
  }

  async function doDestroy() {
    if (!state.current) return toast('请先创建 bundle', 'err');
    const env = currentEnv();
    const ok = await confirmDialog(
      `确认销毁 ${ENV_LABEL[env] || env} 环境的资源？`,
      '只会删除由本 bundle 发布创建的资源，手工创建的同名资源不受影响。此操作不可撤销。'
    );
    if (!ok) return;
    try {
      const r = await api.post(`/bundles/${state.current.id}/destroy`, { env });
      toast(r.summary || '销毁完成', 'ok');
      await refreshSummary();
    } catch (err) {
      toast(err.message || '销毁失败', 'err');
    }
  }

  function render() {
    shell.innerHTML = '';
    const layout = el('div', { class: 'cd-layout' });
    layout.appendChild(editorPane());
    layout.appendChild(resultPane());
    shell.appendChild(layout);

    if (!state.current) {
      const ta = shell.querySelector('#cd-yaml');
      if (ta) ta.value = '';
      const box = shell.querySelector('#cd-envs');
      if (box) box.innerHTML = `<div class="cd-empty">还没有 bundle，点「+ 新建」创建第一个。</div>`;
    }
    bindPane();
    refreshSummary();
  }

  loadBundles();

  return () => {
    root.innerHTML = '';
  };
}

export default renderCicd;
