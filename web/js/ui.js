// 控制台基础组件：表格 / 弹层 / 表单 / 标签 / Markdown
import { fmt } from './api.js';

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// ------------------------------------------------------------------ Markdown
function inline(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

export function md(src) {
  const lines = String(src ?? '').replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre class="code dark"${lang ? ` data-lang="${esc(lang)}"` : ''}>${esc(buf.join('\n'))}</pre>`);
      continue;
    }

    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const head = line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        rows.push(lines[i].trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim()));
        i++;
      }
      out.push(
        `<table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join('')}</tr></thead><tbody>${rows
          .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`)
          .join('')}</tbody></table>`
      );
      continue;
    }

    if (/^#{1,4}\s/.test(line)) {
      const level = line.match(/^#+/)[0].length;
      const tag = Math.min(level + 1, 4);
      out.push(`<h${tag}>${inline(line.replace(/^#+\s*/, ''))}</h${tag}>`);
      i++;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
      out.push(`<blockquote>${buf.map(inline).join('<br>')}</blockquote>`);
      continue;
    }

    if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      out.push('<hr>');
      i++;
      continue;
    }

    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, '');
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
          item += ' ' + lines[i].trim();
          i++;
        }
        items.push(item);
      }
      const tag = ordered ? 'ol' : 'ul';
      out.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join('')}</${tag}>`);
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|>|```|\s*([-*+]|\d+\.)\s|\s*\|)/.test(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    if (buf.length) out.push(`<p>${inline(buf.join(' '))}</p>`);
    else { out.push(`<p>${inline(line)}</p>`); i++; }
  }
  return out.join('\n');
}

// ------------------------------------------------------------------ 表格
export function tableHTML(columns, rows, opts = {}) {
  if (!columns || !columns.length) return `<div class="empty">没有返回列</div>`;
  const max = opts.max ?? rows.length;
  const raw = new Set(opts.raw || []);
  const numeric = columns.map((_, idx) =>
    rows.slice(0, 20).some((r) => typeof r[idx] === 'number' && !Number.isNaN(r[idx]))
  );
  const head = columns.map((c, idx) => `<th${numeric[idx] ? ' style="text-align:right"' : ''}>${esc(c)}</th>`).join('');
  const body = rows.slice(0, max).map((r) => {
    const tds = columns.map((_, idx) => {
      const v = r[idx];
      if (v === null || v === undefined) return '<td class="dim">-</td>';
      if (raw.has(idx)) return `<td>${v}</td>`;
      if (typeof v === 'number') return `<td class="num">${fmt.num(v, Number.isInteger(v) ? 0 : 2)}</td>`;
      const text = String(v);
      return `<td>${esc(text.length > 90 ? text.slice(0, 90) + '…' : text)}</td>`;
    });
    return `<tr>${tds.join('')}</tr>`;
  }).join('');
  const more = rows.length > max
    ? `<div class="table-foot"><span>显示 ${Math.min(max, rows.length)} / ${rows.length} 条</span></div>`
    : '';
  return `<div class="table-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${more}</div>`;
}

/**
 * 控制台数据表格
 * columns: [{ title, key?, width?, align?, render?(row) }]
 */
export function dataTable({ columns, rows, emptyText = '暂无数据', emptyHint = '', rowKey = 'id', onRowClick }) {
  if (!rows || !rows.length) return emptyState(emptyText, emptyHint);
  const head = columns.map((c) => `<th${c.width ? ` style="width:${c.width}"` : ''}>${esc(c.title)}</th>`).join('');
  const body = rows
    .map((row) => {
      const tds = columns
        .map((c) => {
          const cls = c.align === 'right' ? ' class="num"' : c.key === 'ops' ? ' class="ops"' : '';
          const content = c.render ? c.render(row) : esc(row[c.key] ?? '-');
          return `<td${cls}>${content}</td>`;
        })
        .join('');
      return `<tr data-key="${esc(row[rowKey] ?? '')}">${tds}</tr>`;
    })
    .join('');
  return `<div class="table-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

export function emptyState(text, hint = '') {
  return `<div class="empty">
    <div class="icon"><svg viewBox="0 0 48 48" width="40" height="40" fill="none" stroke="currentColor" stroke-width="2">
      <rect x="7" y="11" width="34" height="26" rx="3"/><path d="M7 19h34"/><path d="M15 27h8"/>
    </svg></div>
    <strong>${esc(text)}</strong>${hint ? `<div>${esc(hint)}</div>` : ''}</div>`;
}

export function loading(text = '加载中…') {
  return `<div style="padding:48px;text-align:center;color:var(--text-3);font-size:13px">
    <span class="thinking"><i></i><i></i><i></i><span style="margin-left:8px">${esc(text)}</span></span></div>`;
}

// ------------------------------------------------------------------ 展示件
export function statusTag(status) {
  const map = {
    running: ['green', '运行中'], ok: ['green', '通过'], success: ['green', '成功'], online: ['green', '已上线'],
    published: ['green', '已发布'], pass: ['green', '通过'], active: ['green', '启用'],
    stopped: ['', '已停止'], draft: ['', '草稿'], disabled: ['', '已停用'],
    creating: ['blue', '创建中'], runningtask: ['blue', '执行中'],
    failed: ['red', '失败'], fail: ['red', '未通过'], error: ['red', '异常'], offline: ['', '已下线'],
    pending: ['orange', '待确认'], warning: ['orange', '告警'],
  };
  const [tone, label] = map[status] || ['', status || '-'];
  return `<span class="tag ${tone}"><i class="dot" style="background:currentColor;width:5px;height:5px;border-radius:50%"></i>${esc(label)}</span>`;
}

export function tag(text, tone = '') {
  return `<span class="tag ${tone}">${esc(text)}</span>`;
}

export function kpi(label, value, foot = '', unit = '') {
  return `<div class="stat">
    <span class="label">${esc(label)}</span>
    <span class="value">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ''}</span>
    ${foot ? `<span class="foot">${foot}</span>` : ''}
  </div>`;
}

export function card(title, body, { sub = '', actions = '', flush = false, cls = '' } = {}) {
  return `<div class="card ${flush ? 'flush' : ''} ${cls}">
    ${title ? `<div class="card-head"><div><h3>${esc(title)}</h3>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>${actions}</div>` : ''}
    ${body}
  </div>`;
}

export function descList(items) {
  return `<dl class="desc-list">${items
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v && String(v).startsWith('<') ? v : esc(v ?? '-')}</dd>`)
    .join('')}</dl>`;
}

export function tabsHTML(items, active, name = 'tab') {
  return `<div class="tabs" data-tabs="${name}">${items
    .map((it) => `<button data-tab="${esc(it.key)}" class="${it.key === active ? 'active' : ''}">${esc(it.label)}${it.count != null ? ` (${it.count})` : ''}</button>`)
    .join('')}</div>`;
}

export function bindTabs(root, onChange) {
  root.querySelectorAll('[data-tabs] button').forEach((b) => {
    b.addEventListener('click', () => {
      root.querySelectorAll('[data-tabs] button').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      onChange(b.dataset.tab);
    });
  });
}

// ------------------------------------------------------------------ 反馈
let toastTimer;
export function toast(message, type = '') {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add('hidden'), 2800);
}

export function modal({ title, body, actions = [], wide = false, onMount }) {
  const root = $('#modal-root');
  const mask = el('div', { class: 'modal-mask' });
  const box = el('div', { class: `modal${wide ? ' wide' : ''}` });
  const head = el('div', { class: 'modal-head', html: `<h3>${esc(title)}</h3>` });
  const closeBtn = el('button', { class: 'icon-btn btn ghost sm', onclick: () => close(false) }, '✕');
  head.append(closeBtn);
  const content = el('div', { class: 'modal-body' });
  if (typeof body === 'string') content.innerHTML = body;
  else if (body) content.append(body);
  const foot = el('div', { class: 'modal-foot' });
  let settled = false;
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  const close = (value) => {
    if (settled) return;
    settled = true;
    mask.remove();
    resolve(value);
  };
  for (const action of actions) {
    foot.append(
      el('button', {
        class: `btn ${action.primary ? 'primary' : ''} ${action.danger ? 'danger' : ''}`,
        onclick: async () => {
          if (action.onClick) {
            action.btn = action.btn || null;
            const result = await action.onClick(content);
            if (result === false) return;
            close(result === undefined ? action.value ?? true : result);
            return;
          }
          close(action.value ?? true);
        },
      }, action.label)
    );
  }
  if (!actions.length) foot.append(el('button', { class: 'btn primary', onclick: () => close(false) }, '关闭'));
  box.append(head, content, foot);
  mask.append(box);
  mask.addEventListener('click', (e) => { if (e.target === mask) close(false); });
  root.append(mask);
  if (onMount) onMount(content, close);
  return promise;
}

export function confirmDialog(title, text, okLabel = '确认') {
  return modal({
    title,
    body: `<div style="font-size:13px;color:var(--text-2);line-height:1.8">${esc(text)}</div>`,
    actions: [
      { label: '取消', value: false },
      { label: okLabel, primary: true, value: true },
    ],
  });
}

export function drawer({ title, body, footer = '', wide = false }) {
  const root = $('#modal-root');
  const mask = el('div', { class: 'drawer-mask' });
  const panel = el('div', { class: `drawer${wide ? ' wide' : ''}` });
  const head = el('div', { class: 'drawer-head', html: `<h3>${esc(title)}</h3>` });
  head.append(el('button', { class: 'btn ghost sm', onclick: () => close() }, '✕'));
  const bodyNode = el('div', { class: 'drawer-body' });
  if (typeof body === 'string') bodyNode.innerHTML = body;
  else bodyNode.append(body);
  panel.append(head, bodyNode);
  if (footer) {
    const foot = el('div', { class: 'drawer-foot', html: footer });
    panel.append(foot);
  }
  mask.addEventListener('click', () => close());
  function close() { mask.remove(); panel.remove(); }
  root.append(mask, panel);
  return { close, body: bodyNode, panel };
}

// ------------------------------------------------------------------ 表单
function fieldHTML(f) {
  const req = f.required ? '<span class="req">*</span>' : '';
  const hint = f.hint ? `<div class="hint">${esc(f.hint)}</div>` : '';
  const val = f.value ?? '';
  let control = '';
  if (f.type === 'textarea') {
    control = `<textarea name="${f.name}" rows="${f.rows || 4}" placeholder="${esc(f.placeholder || '')}">${esc(val)}</textarea>`;
  } else if (f.type === 'select') {
    control = `<select name="${f.name}">${(f.options || [])
      .map((o) => {
        const opt = typeof o === 'string' ? { value: o, label: o } : o;
        return `<option value="${esc(opt.value)}"${String(opt.value) === String(val) ? ' selected' : ''}>${esc(opt.label)}</option>`;
      })
      .join('')}</select>`;
  } else if (f.type === 'checkbox') {
    return `<div class="field"><label class="checkbox"><input type="checkbox" name="${f.name}"${val ? ' checked' : ''}/>${esc(f.label)}</label>${hint}</div>`;
  } else if (f.type === 'number') {
    control = `<input type="number" name="${f.name}" value="${esc(val)}" min="${f.min ?? ''}" max="${f.max ?? ''}" placeholder="${esc(f.placeholder || '')}" />`;
  } else if (f.type === 'password') {
    control = `<input type="password" name="${f.name}" value="${esc(val)}" placeholder="${esc(f.placeholder || '')}" />`;
  } else {
    control = `<input type="text" name="${f.name}" value="${esc(val)}" placeholder="${esc(f.placeholder || '')}" />`;
  }
  return `<div class="field"><label>${req}${esc(f.label)}</label>${control}${hint}</div>`;
}

/** 表单弹窗：返回 Promise<data|null> */
export function formModal({ title, fields, submitText = '确定', wide = false, intro = '', onSubmit }) {
  const form = el('form', { html: intro + fields.map(fieldHTML).join('') });
  return modal({
    title,
    wide,
    body: form,
    actions: [
      { label: '取消', value: null },
      {
        label: submitText,
        primary: true,
        onClick: async () => {
          const data = {};
          for (const f of fields) {
            const node = form.querySelector(`[name="${f.name}"]`);
            if (!node) continue;
            data[f.name] = f.type === 'checkbox' ? node.checked : node.value;
            if (f.type === 'number') data[f.name] = Number(node.value || 0);
            if (f.required && !String(data[f.name] || '').trim()) {
              toast(`请填写「${f.label}」`, 'err');
              return false;
            }
          }
          if (onSubmit) {
            try {
              const ok = await onSubmit(data);
              if (ok === false) return false;
            } catch (err) {
              toast(err.message || '保存失败', 'err');
              return false;
            }
          }
          return data;
        },
      },
    ],
  });
}

export function debounce(fn, wait = 220) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
