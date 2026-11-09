// 轻量 SVG 图表：line / bar / pie / kpi / table，零外部依赖
const PALETTE = ['#4f46e5', '#7c3aed', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#14b8a6'];

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function compact(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '-';
  const v = Number(n);
  const abs = Math.abs(v);
  if (abs >= 1e8) return `${(v / 1e8).toFixed(1)}亿`;
  if (abs >= 1e4) return `${(v / 1e4).toFixed(1)}万`;
  if (abs >= 1000) return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  if (Number.isInteger(v)) return String(v);
  return v.toFixed(2);
}

const num = (v) => {
  const n = typeof v === 'number' ? v : parseFloat(v);
  return Number.isFinite(n) ? n : null;
};

function niceTicks(min, max, count = 4) {
  if (min === max) {
    if (min === 0) return [0, 1];
    min = Math.min(0, min);
    max = max * 1.2;
  }
  const span = max - min || 1;
  const step = Math.pow(10, Math.floor(Math.log10(span / count)));
  const err = (span / count) / step;
  const mult = err >= 7.5 ? 10 : err >= 3 ? 5 : err >= 1.5 ? 2 : 1;
  const s = step * mult;
  const lo = Math.floor(min / s) * s;
  const hi = Math.ceil(max / s) * s;
  const ticks = [];
  for (let v = lo; v <= hi + s / 2; v += s) ticks.push(Number(v.toFixed(10)));
  return ticks;
}

function axes(ticks, x0, x1, y0, y1) {
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const sy = (v) => y1 - ((v - lo) / (hi - lo || 1)) * (y1 - y0);
  return ticks
    .map(
      (t) =>
        `<line x1="${x0}" x2="${x1}" y1="${sy(t).toFixed(1)}" y2="${sy(t).toFixed(1)}" stroke="#eef0f7" stroke-width="1"/>` +
        `<text x="${x0 - 8}" y="${(sy(t) + 3.5).toFixed(1)}" text-anchor="end" font-size="10" fill="#9aa0b0">${esc(compact(t))}</text>`
    )
    .join('');
}

function lineChart(xs, ys, height = 210) {
  const W = 660, H = height, padL = 56, padR = 14, padT = 14, padB = 26;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const values = ys.map(num).filter((v) => v !== null);
  const ticks = niceTicks(Math.min(0, ...values), Math.max(...values));
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const step = xs.length > 1 ? (x1 - x0) / (xs.length - 1) : 0;
  const px = (i) => x0 + i * step;
  const py = (v) => y1 - ((v - lo) / (hi - lo || 1)) * (y1 - y0);

  const pts = values.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`);
  const area = `M${px(0).toFixed(1)},${y1} L${pts.join(' L')} L${px(values.length - 1).toFixed(1)},${y1} Z`;
  const labelIdx = new Set();
  if (xs.length <= 8) xs.forEach((_, i) => labelIdx.add(i));
  else [0, Math.floor(xs.length / 3), Math.floor((2 * xs.length) / 3), xs.length - 1].forEach((i) => labelIdx.add(i));

  const labels = [...labelIdx]
    .sort((a, b) => a - b)
    .map(
      (i) =>
        `<text x="${px(i).toFixed(1)}" y="${H - 8}" text-anchor="middle" font-size="10" fill="#9aa0b0">${esc(
          String(xs[i]).slice(5, 10) || xs[i]
        )}</text>`
    )
    .join('');

  const dots = values.length <= 24
    ? values.map((v, i) => `<circle cx="${px(i).toFixed(1)}" cy="${py(v).toFixed(1)}" r="2.6" fill="#fff" stroke="${PALETTE[0]}" stroke-width="2"><title>${esc(xs[i])}: ${esc(compact(v))}</title></circle>`).join('')
    : '';

  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img">
    <defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${PALETTE[0]}" stop-opacity=".22"/>
      <stop offset="100%" stop-color="${PALETTE[0]}" stop-opacity="0"/>
    </linearGradient></defs>
    ${axes(ticks, x0, x1, y0, y1)}
    <path d="${area}" fill="url(#ag)"/>
    <polyline points="${pts.join(' ')}" fill="none" stroke="${PALETTE[0]}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
    ${dots}${labels}
  </svg></div>`;
}

function barChart(xs, ys, height = 220) {
  const items = xs.map((x, i) => ({ x, y: num(ys[i]) })).filter((d) => d.y !== null);
  const W = 660, H = height, padL = 56, padR = 14, padT = 14, padB = 42;
  const x0 = padL, x1 = W - padR, y0 = padT, y1 = H - padB;
  const ticks = niceTicks(0, Math.max(...items.map((d) => d.y), 1));
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const py = (v) => y1 - ((v - lo) / (hi - lo || 1)) * (y1 - y0);
  const slot = (x1 - x0) / Math.max(items.length, 1);
  const bw = Math.min(46, slot * 0.62);

  const bars = items
    .map((d, i) => {
      const cx = x0 + slot * (i + 0.5);
      const top = py(d.y);
      const h = Math.max(y1 - top, 1);
      const label = String(d.x ?? '');
      const showLabel = items.length <= 14 || i % Math.ceil(items.length / 12) === 0;
      return `<g><rect x="${(cx - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}"
        rx="4" fill="${PALETTE[0]}" opacity=".88"><title>${esc(label)}: ${esc(compact(d.y))}</title></rect>
        <text x="${cx.toFixed(1)}" y="${(top - 6).toFixed(1)}" text-anchor="middle" font-size="10" fill="#6b7280">${esc(compact(d.y))}</text>
        ${showLabel ? `<text x="${cx.toFixed(1)}" y="${H - 22}" text-anchor="end" font-size="10" fill="#9aa0b0" transform="rotate(-22 ${cx.toFixed(1)} ${H - 22})">${esc(label.length > 8 ? label.slice(0, 8) : label)}</text>` : ''}
      </g>`;
    })
    .join('');

  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img">
    ${axes(ticks, x0, x1, y0, y1)}${bars}
  </svg></div>`;
}

function pieChart(labels, values) {
  const items = labels
    .map((label, i) => ({ label: String(label ?? ''), value: num(values[i]) || 0 }))
    .filter((d) => d.value > 0);
  const total = items.reduce((s, d) => s + d.value, 0) || 1;
  const R = 92, r = 56, cx = 110, cy = 105;
  let angle = -Math.PI / 2;
  const arcs = items
    .map((d, i) => {
      const sweep = (d.value / total) * Math.PI * 2;
      const a0 = angle, a1 = angle + sweep;
      angle = a1;
      const large = sweep > Math.PI ? 1 : 0;
      const p = (rad, a) => [cx + rad * Math.cos(a), cy + rad * Math.sin(a)];
      const [x0, y0] = p(R, a0), [x1, y1] = p(R, a1);
      const [x2, y2] = p(r, a1), [x3, y3] = p(r, a0);
      return `<path d="M${x0.toFixed(1)},${y0.toFixed(1)} A${R},${R} 0 ${large} 1 ${x1.toFixed(1)},${y1.toFixed(1)}
        L${x2.toFixed(1)},${y2.toFixed(1)} A${r},${r} 0 ${large} 0 ${x3.toFixed(1)},${y3.toFixed(1)} Z"
        fill="${PALETTE[i % PALETTE.length]}" opacity=".9"><title>${esc(d.label)}: ${esc(compact(d.value))}（${((d.value / total) * 100).toFixed(1)}%）</title></path>`;
    })
    .join('');

  const legend = items
    .map(
      (d, i) =>
        `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(d.label)} <span class="muted">${((d.value / total) * 100).toFixed(1)}%</span></span>`
    )
    .join('');

  return `<div class="row" style="gap:18px;align-items:center;flex-wrap:wrap">
    <svg viewBox="0 0 220 210" style="width:220px;height:210px;flex:none">${arcs}</svg>
    <div class="chart-legend" style="flex-direction:column;gap:6px">${legend}</div>
  </div>`;
}

export function chartHTML(spec, columns, rows, opts = {}) {
  const s = spec || { type: 'table' };
  if (!columns || !columns.length || !rows || !rows.length) {
    return `<div class="empty" style="padding:22px">没有数据可展示</div>`;
  }
  try {
    if (s.type === 'kpi') {
      const idx = s.value_index ?? 1;
      const value = rows[0][idx];
      const numeric = typeof value === 'number';
      return `<div class="kpi-big"><span class="v">${esc(
        numeric ? compact(value) : value ?? '-'
      )}</span><span class="l">${esc(columns[idx] || s.label || '')}</span></div>`;
    }
    if (s.type === 'line') {
      const xs = rows.map((r) => r[s.x ?? 0]);
      const ys = rows.map((r) => r[s.y ?? 1]);
      return lineChart(xs, ys, opts.height);
    }
    if (s.type === 'bar') {
      const xs = rows.map((r) => r[s.x ?? 0]);
      const ys = rows.map((r) => r[s.y ?? 1]);
      return barChart(xs, ys, opts.height);
    }
    if (s.type === 'pie') {
      const labels = rows.map((r) => r[s.x ?? 0]);
      const values = rows.map((r) => r[s.y ?? 1]);
      return pieChart(labels, values);
    }
  } catch (err) {
    console.warn('chart render failed', err);
  }
  return '';
}

export function miniBar(values, color = '#4f46e5') {
  const max = Math.max(...values, 1);
  const w = 60, h = 18, step = w / Math.max(values.length, 1);
  return `<svg viewBox="0 0 ${w} ${h}" style="width:${w}px;height:${h}px">${values
    .map((v, i) => {
      const bh = Math.max((v / max) * h, 1.5);
      return `<rect x="${(i * step).toFixed(1)}" y="${(h - bh).toFixed(1)}" width="${Math.max(step - 1.5, 1).toFixed(1)}" height="${bh.toFixed(1)}" rx="1" fill="${color}" opacity=".75"/>`;
    })
    .join('')}</svg>`;
}
