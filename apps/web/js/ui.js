/* Rendering primitives: elements, formatting, inline SVG charts, drawer, toast.
   Charts are hand-built SVG — no chart library — so every pixel is intentional and accessible. */

export const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'html') n.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  kids.flat().forEach(k => k !== null && k !== undefined && k !== false &&
    n.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k))));
  return n;
};

export const pct = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : `${(v * 100).toFixed(d)}%`;
export const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);
export const sign = (v, d = 2) => (v === null || v === undefined) ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(d)}`;
export const titleCase = s => String(s || '').replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, c => c.toUpperCase());

export const verdictClass = v => v === 'POSITIVE BIAS' ? 'pos' : v === 'NEGATIVE BIAS' ? 'neg' : 'idle';
export const stateClass = s => ({ HEALTHY: 'pos', OK: 'pos', PASS: 'pos', SUPPORTED: 'pos', LOW: 'pos',
  WATCH: 'warn', PARTIAL: 'warn', MEDIUM: 'warn', MIXED: 'warn', FALLBACK: 'warn', DRIFTING: 'warn',
  DEGRADED: 'neg', FAIL: 'neg', UNRELIABLE: 'neg', HIGH: 'neg', CONFLICTED: 'neg', UNSTABLE: 'neg',
  'NOT SUPPORTED': 'warn' }[s] || 'idle');

export function tag(text, kind = 'idle') { return el('span', { class: `tag ${kind}` }, text); }

export function kpi(label, value, sub) {
  return el('div', { class: 'kpi' },
    el('span', { class: 'l' }, label),
    el('span', { class: 'v' }, value),
    sub ? el('span', { class: 'faint', style: 'font-size:12px' }, sub) : null);
}

export function bar(valuePct, kind = '') {
  return el('div', { class: `bar ${kind}` }, el('span', { style: `width:${Math.max(0, Math.min(100, valuePct))}%` }));
}

export function degraded(payload) {
  return el('div', { class: 'degraded' },
    el('p', { class: 'meta', style: 'margin:0 0 6px' }, payload.state || 'DEGRADED'),
    el('p', { style: 'margin:0 0 6px' }, `What failed: ${payload.what_failed || 'unknown'}`),
    el('p', { class: 'dim', style: 'margin:0 0 6px;font-size:13px' }, `Affected: ${payload.affected || '—'}`),
    el('p', { class: 'faint', style: 'margin:0;font-size:13px' }, `Fallback: ${payload.fallback || '—'}`));
}

export const isBad = d => !d || d.__error || d.available === false;

/* ---------------- charts ---------------- */
const SVGNS = 'http://www.w3.org/2000/svg';
const s = (tag, attrs = {}) => { const n = document.createElementNS(SVGNS, tag);
  Object.entries(attrs).forEach(([k, v]) => n.setAttribute(k, v)); return n; };

export function lineChart(series, { height = 170, yDomain, labels = [], markers = [], band, zero } = {}) {
  const W = 600, H = height, pad = { l: 34, r: 12, t: 12, b: 20 };
  const svg = s('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img' });
  const all = series.flatMap(x => x.values).filter(v => v !== null && Number.isFinite(v));
  if (!all.length) return svg;
  let [lo, hi] = yDomain || [Math.min(...all), Math.max(...all)];
  if (lo === hi) { lo -= 0.01; hi += 0.01; }
  const n = Math.max(...series.map(x => x.values.length));
  const X = i => pad.l + (i / Math.max(n - 1, 1)) * (W - pad.l - pad.r);
  const Y = v => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  [0, .25, .5, .75, 1].forEach(f => {
    const y = pad.t + f * (H - pad.t - pad.b);
    svg.appendChild(s('line', { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: 'gridline' }));
    const t = s('text', { x: 4, y: y + 3, class: 'tick' });
    t.textContent = (hi - f * (hi - lo)).toFixed(2); svg.appendChild(t);
  });
  if (band) {
    const d = band.upper.map((v, i) => `${i ? 'L' : 'M'}${X(i)},${Y(v)}`).join(' ') + ' ' +
      band.lower.map((v, i) => `L${X(band.lower.length - 1 - i)},${Y(band.lower[band.lower.length - 1 - i])}`).join(' ') + ' Z';
    svg.appendChild(s('path', { d, fill: 'rgba(110,91,255,.14)' }));
  }
  if (zero !== undefined && zero >= lo && zero <= hi)
    svg.appendChild(s('line', { x1: pad.l, x2: W - pad.r, y1: Y(zero), y2: Y(zero),
      stroke: 'rgba(255,255,255,.26)', 'stroke-dasharray': '3 3' }));
  series.forEach(sr => {
    const d = sr.values.map((v, i) => (v === null || !Number.isFinite(v)) ? null : `${X(i)},${Y(v)}`)
      .filter(Boolean).map((p, i) => `${i ? 'L' : 'M'}${p}`).join(' ');
    svg.appendChild(s('path', { d, class: `series ${sr.className || ''}`, stroke: sr.color || '' }));
  });
  markers.forEach(m => {
    svg.appendChild(s('circle', { cx: X(m.i), cy: Y(m.v), r: 3.4, fill: m.color || '#A692FF' }));
  });
  labels.forEach((lab, i) => {
    if (!lab) return;
    const t = s('text', { x: X(i), y: H - 5, class: 'tick', 'text-anchor': i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle' });
    t.textContent = lab; svg.appendChild(t);
  });
  return svg;
}

export function barChart(items, { height = 170, color = '#A692FF', zero = true } = {}) {
  const W = 600, H = height, pad = { l: 34, r: 10, t: 10, b: 28 };
  const svg = s('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img' });
  const vals = items.map(i => i.value);
  const hi = Math.max(...vals, 0), lo = Math.min(...vals, 0);
  const Y = v => pad.t + (1 - (v - lo) / ((hi - lo) || 1)) * (H - pad.t - pad.b);
  const bw = (W - pad.l - pad.r) / Math.max(items.length, 1);
  if (zero) svg.appendChild(s('line', { x1: pad.l, x2: W - pad.r, y1: Y(0), y2: Y(0), class: 'axis' }));
  items.forEach((it, i) => {
    const x = pad.l + i * bw + bw * .18, w = bw * .64;
    const y0 = Y(0), y1 = Y(it.value);
    svg.appendChild(s('rect', { x, y: Math.min(y0, y1), width: w, height: Math.max(Math.abs(y1 - y0), 1.5),
      rx: 3, fill: it.color || color, opacity: it.opacity || .85 }));
    const t = s('text', { x: x + w / 2, y: H - 14, class: 'tick', 'text-anchor': 'middle' });
    t.textContent = it.label; svg.appendChild(t);
    const v = s('text', { x: x + w / 2, y: H - 3, class: 'tick', 'text-anchor': 'middle', fill: '#8E93A3' });
    v.textContent = it.display ?? it.value; svg.appendChild(v);
  });
  return svg;
}

export function reliabilityChart(rows, { height = 190 } = {}) {
  const W = 320, H = height, pad = 30;
  const svg = s('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Reliability diagram: predicted vs observed frequency' });
  const X = v => pad + v * (W - pad * 1.4), Y = v => H - pad - v * (H - pad * 1.5);
  svg.appendChild(s('line', { x1: X(0), y1: Y(0), x2: X(1), y2: Y(1), stroke: 'rgba(255,255,255,.25)', 'stroke-dasharray': '4 4' }));
  svg.appendChild(s('line', { x1: X(0), y1: Y(0), x2: X(1), y2: Y(0), class: 'axis' }));
  svg.appendChild(s('line', { x1: X(0), y1: Y(0), x2: X(0), y2: Y(1), class: 'axis' }));
  const pts = rows.map(r => `${X(r.predicted)},${Y(r.observed)}`).join(' L');
  if (pts) svg.appendChild(s('path', { d: `M${pts}`, class: 'series' }));
  rows.forEach(r => svg.appendChild(s('circle', { cx: X(r.predicted), cy: Y(r.observed),
    r: Math.max(2, Math.min(6, Math.sqrt(r.count) / 9)), fill: '#A692FF', opacity: .85 })));
  const t1 = s('text', { x: X(0.5), y: H - 6, class: 'tick', 'text-anchor': 'middle' }); t1.textContent = 'PREDICTED';
  const t2 = s('text', { x: 8, y: 14, class: 'tick' }); t2.textContent = 'OBSERVED';
  svg.append(t1, t2);
  return svg;
}

export function gauge(valuePct, label, kind = 'idle') {
  const W = 150, H = 92, cx = W / 2, cy = 78, r = 58;
  const svg = s('svg', { class: 'chart gauge-svg', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `${label}: ${valuePct}` });
  const arc = (frac, color, width) => {
    const a0 = Math.PI, a1 = Math.PI + Math.PI * frac;
    const x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0);
    const x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    return s('path', { d: `M${x0},${y0} A${r},${r} 0 ${frac > .5 ? 1 : 0} 1 ${x1},${y1}`,
      fill: 'none', stroke: color, 'stroke-width': width, 'stroke-linecap': 'round' });
  };
  svg.appendChild(arc(1, 'rgba(255,255,255,.1)', 8));
  const col = { pos: '#3FB27F', neg: '#E0616A', warn: '#D9A441', idle: '#A692FF' }[kind] || '#A692FF';
  svg.appendChild(arc(Math.max(0.01, Math.min(1, valuePct / 100)), col, 8));
  const t = s('text', { x: cx, y: cy - 10, 'text-anchor': 'middle', fill: '#ECEDF2',
    style: 'font-family:var(--ff-mono);font-size:22px' }); t.textContent = Math.round(valuePct);
  const l = s('text', { x: cx, y: cy + 8, 'text-anchor': 'middle', class: 'tick' }); l.textContent = label;
  svg.append(t, l);
  return svg;
}

/* ---------------- drawer ---------------- */
let drawerEl, bgEl, lastFocus;
export function drawer(title, subtitle, content) {
  if (!drawerEl) {
    bgEl = el('div', { class: 'drawer-bg', onclick: closeDrawer });
    drawerEl = el('aside', { class: 'drawer', role: 'dialog', 'aria-modal': 'true', tabindex: '-1' });
    document.body.append(bgEl, drawerEl);
    addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  }
  lastFocus = document.activeElement;
  drawerEl.innerHTML = '';
  drawerEl.append(
    el('header', {},
      el('div', {}, el('p', { class: 'meta', style: 'margin:0 0 4px' }, subtitle || ''),
        el('h2', { class: 'h2', style: 'margin:0' }, title)),
      el('button', { class: 'btn sm ghost', onclick: closeDrawer, 'aria-label': 'Close panel' }, 'Close')),
    content);
  bgEl.classList.add('open'); drawerEl.classList.add('open');
  drawerEl.focus();
}
export function closeDrawer() {
  if (!drawerEl) return;
  bgEl.classList.remove('open'); drawerEl.classList.remove('open');
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

export function section(title, subtitle, ...content) {
  return el('section', { class: 'card' },
    el('div', { class: 'between', style: 'margin-bottom:12px' },
      el('div', {}, el('h3', { class: 'h3' }, title),
        subtitle ? el('p', { class: 'faint', style: 'margin:3px 0 0;font-size:12.5px' }, subtitle) : null)),
    ...content);
}

export function table(headers, rows) {
  return el('div', { class: 'scroll' }, el('table', {},
    el('thead', {}, el('tr', {}, headers.map(h => el('th', {}, h)))),
    el('tbody', {}, rows.map(r => el('tr', {}, r.map(c => el('td', {}, c)))))));
}
