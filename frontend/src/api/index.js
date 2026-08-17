/**
 * api/index.js
 * All API calls to the Flask backend in one place.
 * Every function returns the parsed JSON or throws on error.
 */

const BASE = '';  // same origin in production; Vite proxy handles dev

async function request(method, path, body = null, { signal } = {}) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
    ...(signal ? { signal } : {}),
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(BASE + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const get  = (path, opts)        => request('GET',  path, null, opts);
const post = (path, body, opts)  => request('POST', path, body, opts);

// ── Auth ──────────────────────────────────────────────
export const fetchMe = () => get('/api/me');

// ── Sites ─────────────────────────────────────────────
export const fetchSites          = () => get('/api/sites');
export const submitSite          = (data) => post('/api/sites/submit', data);
export const fetchPendingSites   = () => get('/api/sites/pending');
export const reviewSite          = (id, approve) => post('/api/sites/review', { id, approve });
export const deleteSite          = (siteId) => post('/api/sites/delete', { site_id: siteId });
export const editSite            = (data) => post('/api/sites/edit', data);

// ── Stars ─────────────────────────────────────────────
export const fetchStars    = () => get('/api/stars');
export const addStar       = (siteId) => post('/api/stars/add',     { site_id: siteId });
export const removeStar    = (siteId) => post('/api/stars/remove',  { site_id: siteId });
export const reorderStars  = (ids)    => post('/api/stars/reorder', { site_ids: ids });

// ── Apps (SMB downloads) ───────────────────────────────
export const fetchApps = () => get('/api/apps');

// ── Scripts ───────────────────────────────────────────
export const fetchScripts       = () => get('/api/scripts');
export const submitScript       = (data) => post('/api/scripts/submit', data);
export const fetchPendingScripts = () => get('/api/scripts/pending');
export const approveScript      = (id) => post('/api/scripts/approve', { id });
export const rejectScript       = (id) => post('/api/scripts/reject',  { id });

export const uploadScript = (formData) =>
  fetch('/api/scripts/upload', { method: 'POST', body: formData })
    .then(r => r.json().then(d => { if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`); return d; }));

// Converts a .js file to its oneline-base64 value for a js_file run argument.
// Stateless -- nothing is stored server-side. Returns { value, filename, size };
// `value` is stored directly as the argument's value in the run form.
export const uploadScriptArgFile = (file) => {
  const formData = new FormData();
  formData.append('file', file);
  return fetch('/api/scripts/upload-arg-file', { method: 'POST', body: formData })
    .then(r => r.json().then(d => { if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`); return d; }));
};

// ── Admin ─────────────────────────────────────────────
export const fetchUsers    = () => get('/api/admin/users');
export const setUserAdmin  = (username, is_admin) => post('/api/admin/users/set', { username, is_admin });

// ── Banner options ──────────────────────────────────────────────────────────
export const fetchBannerOptions = () => get('/api/banner-options');

// ── Env colors (grouped-card environment rows) ────────────────────────────
export const fetchEnvColorOptions = () => get('/api/env-color-options');

// ── Downloads (S3 catalog) ────────────────────────────────────────────────────
export const fetchDownloadCategories = ({ signal } = {}) =>
  get('/api/downloads/categories', { signal });

export const fetchDownloads = ({ q = '', category = '', cursor = '', limit = 24, signal } = {}) => {
  const params = new URLSearchParams({ limit: String(limit), page_size: String(limit) });
  if (q)        params.set('q',        q);
  if (category) params.set('category', category);
  if (cursor)   params.set('cursor',   cursor);
  return get(`/api/downloads?${params.toString()}`, { signal });
};

export const fetchDownloadItem = (catalogId, { signal } = {}) =>
  get(`/api/downloads/${encodeURIComponent(catalogId)}`, { signal });

export const createDownloadUrl = (variantId) =>
  post(`/api/downloads/variants/${encodeURIComponent(variantId)}/url`, {});

export const uploadDownload = (formData, { signal } = {}) =>
  fetch(`${BASE}/api/downloads/upload`, {
    method: 'POST',
    body: formData,
    ...(signal ? { signal } : {}),
  }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  });
