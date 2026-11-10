// 内联 SVG 图标：避免依赖系统字体字形（原方案用 ▤ 等字符在部分机器上缺字）
const P = {
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  buddy: '<path d="M12 3l1.8 4.9L19 9.7l-4.4 2.8L13.6 18 12 14.6 10.4 18l-1-5.5L5 9.7l5.2-1.8z"/>',
  catalog: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"/><path d="M4.5 12v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6"/>',
  ingest: '<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v3a2 2 0 002 2h12a2 2 0 002-2v-3"/>',
  sql: '<path d="M8 6l-5 6 5 6"/><path d="M16 6l5 6-5 6"/><path d="M13 4l-2 16"/>',
  flows: '<circle cx="5" cy="6" r="2.2"/><circle cx="19" cy="6" r="2.2"/><circle cx="12" cy="18" r="2.2"/><path d="M7.2 6h9.6"/><path d="M6.2 8l4.6 8"/><path d="M17.8 8l-4.6 8"/>',
  dashboards: '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
  semantic: '<path d="M18 5H7l6 7-6 7h11"/>',
  governance: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
  knowledge: '<path d="M4 5.5A2.5 2.5 0 016.5 3H20v16H6.5A2.5 2.5 0 004 21.5z"/><path d="M4 18.5A2.5 2.5 0 016.5 16H20"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  folder: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
  table: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 10v10"/>',
  dot: '<circle cx="12" cy="12" r="3.5"/>',
  spark: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18"/>',
  // 控制台二级模块
  studio: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/><path d="M8 13l-2 2 2 2"/><path d="M13 13l2 2-2 2"/>',
  cicd: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5V15a3 3 0 003 3h6"/><path d="M15 5.5h3v3"/>',
  ops: '<path d="M3 12h4l2.5-6 3 12 2.5-6h6"/>',
  experiment: '<path d="M9 3v6L4 19a1 1 0 00.9 1.4h14.2A1 1 0 0020 19l-5-10V3"/><path d="M8 3h8"/><path d="M6.5 14h11"/>',
  registry: '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
  serving: '<circle cx="12" cy="12" r="2.5"/><path d="M5.5 5.5a9 9 0 000 13M18.5 5.5a9 9 0 010 13"/><path d="M8.5 8.5a5 5 0 000 7M15.5 8.5a5 5 0 010 7"/>',
  explore: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l5 5"/>',
  analysis: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 15l3-4 3 2 4-6"/>',
  quality: '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
  security: '<rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/>',
  agents: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0114 0"/><path d="M2 12h2M20 12h2"/>',
  mcp: '<rect x="3" y="8" width="7" height="8" rx="1.5"/><rect x="14" y="8" width="7" height="8" rx="1.5"/><path d="M10 12h4"/>',
  modellist: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  workspaces: '<rect x="3" y="4" width="8" height="7" rx="1.5"/><rect x="13" y="4" width="8" height="7" rx="1.5"/><rect x="3" y="13" width="8" height="7" rx="1.5"/><rect x="13" y="13" width="8" height="7" rx="1.5"/>',
  users: '<circle cx="9" cy="8" r="3.2"/><path d="M2.5 20a6.5 6.5 0 0113 0"/><path d="M17 11a3 3 0 100-6"/><path d="M18 20a5.5 5.5 0 00-2-4.3"/>',
  platform: '<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="14" width="18" height="6" rx="1.5"/><path d="M7 7.5h.01M7 17h.01"/>',
  sources: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6"/>',
  compute: '<rect x="4" y="4" width="16" height="6" rx="1.5"/><rect x="4" y="14" width="16" height="6" rx="1.5"/><path d="M8 7h.01M8 17h.01"/>',
  subscription: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 10h18"/><path d="M7 15h4"/>',
  // 本体建模：实体 + 关系
  ontology: '<circle cx="6" cy="6" r="2.6"/><circle cx="18" cy="6" r="2.6"/><circle cx="12" cy="18" r="2.6"/><path d="M8.6 6h6.8"/><path d="M7.4 8.3l3.3 7.4"/><path d="M16.6 8.3l-3.3 7.4"/>',
  // 特征管理
  features: '<path d="M4 20V8l4-3 4 3v12"/><path d="M12 20V11l4-3 4 3v9"/><path d="M2 20h20"/>',
};

export function icon(name, size = 16) {
  const path = P[name] || P.dot;
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor"
    stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}
