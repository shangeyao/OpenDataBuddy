// DOM / Markdown / 弹层 基础件
import { fmt } from './api.js';

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

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
      out.push(`<pre class="code"${lang ? ` data-lang="${esc(lang)}"` : ''}>${esc(buf.join('\n'))}</pre>`);
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
      out.push(`<h${Math.min(level + 1, 4)}>${inline(line.replace(/^#+\s*/, ''))}</h${Math.min(level + 1, 4)}>`);
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

    if (!line.trim()) {
      i++;
      continue;
    }

    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|>|```|\s*([-*+]|\d+\.)\s|\s*\|)/.test(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    if (buf.length) out.push(`<p>${inline(buf.join(' '))}</p>`);
    else out.push(`<p>${inline(line)}</p>`), i++;
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
      if (v === null || v === undefined) return '<td class="muted">-</td>';
      if (raw.has(idx)) return `<td>${v}</td>`;
      if (typeof v === 'number') return `<td class="num">${fmt.num(v, Number.isInteger(v) ? 0 : 2)}</td>`;
      const text = String(v);
      return `<td>${esc(text.length > 80 ? text.slice(0, 80) + '…' : text)}</td>`;
    });
    return `<tr>${tds.join('')}</tr>`;
  }).join('');
  const more = rows.length > max ? `<div class="tiny muted" style="padding:8px 10px">仅显示前 ${max} 行，共 ${rows.length} 行</div>` : '';
  return `<div class="table-wrap"><table class="tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${more}</div>`;
}

// ------------------------------------------------------------------ 反馈
let toastTimer;
export function toast(message, type = '') {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add('hidden'), 2600);
}

export function modal({ title, body, actions = [], wide = false, onMount }) {
  const root = $('#modal-root');
  const mask = el('div', { class: 'modal-mask' });
  const box = el('div', { class: `modal${wide ? ' wide' : ''}` });
  box.innerHTML = `<h3>${esc(title)}</h3>`;
  const content = el('div');
  if (typeof body === 'string') content.innerHTML = body;
  else if (body) content.append(body);
  box.append(content);
  const foot = el('div', { class: 'modal-foot' });
  let settled = false;
  const close = (value) => {
    if (settled) return;
    settled = true;
    mask.remove();
    resolve(value);
  };
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  for (const action of actions) {
    foot.append(
      el('button', {
        class: `btn ${action.primary ? 'primary' : ''} ${action.danger ? 'danger' : ''}`,
        onclick: async () => {
          if (action.onClick) {
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
  if (!actions.length) foot.append(el('button', { class: 'btn', onclick: () => close(false) }, '关闭'));
  box.append(foot);
  mask.append(box);
  mask.addEventListener('click', (e) => {
    if (e.target === mask) close(false);
  });
  root.append(mask);
  if (onMount) onMount(content, close);
  return promise;
}

export function confirmDialog(title, text, okLabel = '确认') {
  return modal({
    title,
    body: `<p class="muted" style="font-size:13px">${esc(text)}</p>`,
    actions: [
      { label: '取消', value: false },
      { label: okLabel, primary: true, value: true },
    ],
  });
}

export function drawer({ title, body, width }) {
  const root = $('#modal-root');
  const mask = el('div', { class: 'drawer-mask' });
  const panel = el('div', { class: 'drawer' });
  if (width) panel.style.width = width;
  const head = el('div', { class: 'drawer-head' });
  head.innerHTML = `<h3 style="flex:1">${esc(title)}</h3>`;
  const closeBtn = el('button', { class: 'icon-btn', onclick: () => close() }, '✕');
  head.append(closeBtn);
  const bodyNode = el('div', { class: 'drawer-body' });
  if (typeof body === 'string') bodyNode.innerHTML = body;
  else bodyNode.append(body);
  panel.append(head, bodyNode);
  mask.addEventListener('click', () => close());
  function close() {
    mask.remove();
    panel.remove();
  }
  root.append(mask, panel);
  return { close, body: bodyNode, head };
}

export function loading(text = '加载中…') {
  return `<div class="row" style="padding:28px;justify-content:center;color:var(--muted);font-size:13px">
    <span class="thinking"><i></i><i></i><i></i></span>${esc(text)}</div>`;
}

export function empty(text, hint = '') {
  return `<div class="empty"><strong>${esc(text)}</strong>${hint ? esc(hint) : ''}</div>`;
}

export function levelBadge(level, tag) {
  const map = { L1: 'l1', L2: 'l2', L3: 'l3', L4: 'l4' };
  return `<span class="badge ${map[level] || ''}">${esc(tag || level)}${level ? ` · ${level}` : ''}</span>`;
}

export function debounce(fn, wait = 220) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
