/* Thin API client. Every failure becomes a structured, explainable state — never a blank error. */
const cache = new Map();

export async function api(path, { method = 'GET', body, fresh = false } = {}) {
  const key = method + path + (body ? JSON.stringify(body) : '');
  if (!fresh && method === 'GET' && cache.has(key)) return cache.get(key);
  const t0 = performance.now();
  try {
    const res = await fetch(`/api${path}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await res.json();
    data.__ms = Math.round(performance.now() - t0);
    if (!res.ok) {
      return { __error: true, status: res.status, state: data.state || 'ERROR',
               what_failed: data.detail || data.what_failed || `HTTP ${res.status}`,
               affected: path, fallback: data.fallback || 'Try another VIGIL space; the rest of the system is unaffected.' };
    }
    if (method === 'GET') cache.set(key, data);
    return data;
  } catch (err) {
    return { __error: true, state: 'OFFLINE', what_failed: `the API did not respond (${err.message})`,
             affected: path, fallback: 'Start the server with `uvicorn vigil.api.app:app --port 8000`.' };
  }
}

export const clearCache = () => cache.clear();
