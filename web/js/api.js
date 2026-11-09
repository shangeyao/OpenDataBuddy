// 接口封装 + 会话状态
const TOKEN_KEY = 'odb.token';
const WS_KEY = 'odb.workspace';

export const session = {
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: null,
  workspaces: [],
  wsId: localStorage.getItem(WS_KEY) || '',
  setToken(t) {
    this.token = t || '';
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  },
  setWorkspace(id) {
    this.wsId = id || '';
    if (id) localStorage.setItem(WS_KEY, id);
  },
  clear() {
    this.setToken('');
    this.user = null;
    this.workspaces = [];
    this.setWorkspace('');
  },
};

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = 'GET', body, form, raw = false } = {}) {
  const headers = {};
  if (session.token) headers['Authorization'] = `Bearer ${session.token}`;
  if (session.wsId) headers['X-Workspace-Id'] = session.wsId;
  let payload;
  if (form) {
    payload = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const resp = await fetch(`/api${path}`, { method, headers, body: payload });
  if (resp.status === 401) {
    session.clear();
    window.dispatchEvent(new CustomEvent('odb:logout'));
    throw new ApiError('会话已过期，请重新登录', 401);
  }
  if (raw) return resp;
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!resp.ok) {
    const detail = (data && (data.detail || data.message)) || `请求失败 (${resp.status})`;
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), resp.status);
  }
  return data;
}

export const api = {
  get: (p) => request(p),
  post: (p, body) => request(p, { method: 'POST', body }),
  patch: (p, body) => request(p, { method: 'PATCH', body }),
  put: (p, body) => request(p, { method: 'PUT', body }),
  del: (p) => request(p, { method: 'DELETE' }),
  upload: (p, form) => request(p, { method: 'POST', form }),
};

// ---------------------------------------------------------------- 格式化
export const fmt = {
  num(v, digits = 0) {
    if (v === null || v === undefined || v === '') return '-';
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  money(v) {
    if (v === null || v === undefined || v === '') return '-';
    const n = Number(v);
    if (Number.isNaN(n)) return String(v);
    if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`;
    if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(2)} 万`;
    return n.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  },
  pct(v) {
    if (v === null || v === undefined) return '-';
    return `${(Number(v) * 100).toFixed(1)}%`;
  },
  dt(s) {
    if (!s) return '-';
    return String(s).replace('T', ' ').slice(0, 19);
  },
  ago(s) {
    if (!s) return '-';
    const t = new Date(s).getTime();
    const diff = (Date.now() - t) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`;
    return String(s).slice(0, 10);
  },
  bytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return `${n} B`;
    if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 ** 2).toFixed(1)} MB`;
  },
};
