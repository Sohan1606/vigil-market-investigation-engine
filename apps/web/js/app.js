/* VIGIL application shell: named spaces, hash router, global search, assistant. */
import { api, clearCache } from './api.js';
import { closeDrawer, degraded, drawer, el, isBad, num, pct, stateClass, tag } from './ui.js';
import { decision, forecast, investigate, lab, observatory, pulse, replayView, workspace } from './views.js';

const SPACES = [
  { id: 'pulse',        label: 'PULSE',          question: 'What is happening?',          glyph: '◉', view: pulse },
  { id: 'investigate',  label: 'INVESTIGATE',    question: 'Why is it happening?',        glyph: '◎', view: investigate },
  { id: 'forecast',     label: 'FORECAST',       question: 'What could happen next?',     glyph: '◈', view: forecast },
  { id: 'decision',     label: 'DECISION GATE',  question: 'Can it be trusted?',          glyph: '⬢', view: decision },
  { id: 'replay',       label: 'REPLAY',         question: 'Would we have known?',        glyph: '◁', view: replayView },
  { id: 'lab',          label: 'RESEARCH LAB',   question: 'Does it actually work?',      glyph: '⬡', view: lab },
  { id: 'observatory',  label: 'DATA OBSERVATORY', question: 'How is it engineered?',     glyph: '▣', view: observatory },
  { id: 'workspace',    label: 'WORKSPACE',      question: 'What am I working on?',       glyph: '✛', view: workspace }
];

const main = document.getElementById('view');
const railHost = document.getElementById('rail-nav');
const crumb = document.getElementById('crumb');

SPACES.forEach(s => railHost.append(el('a', { href: `#${s.id}`, id: `nav-${s.id}` },
  el('span', { class: 'n' }, `${s.glyph}  ${s.label}`),
  el('span', { class: 'q' }, s.question))));

async function route() {
  closeDrawer();
  const [id, arg] = (location.hash.replace('#', '') || 'pulse').split('/');
  const space = SPACES.find(s => s.id === id) || SPACES[0];
  document.querySelectorAll('#rail-nav a').forEach(a =>
    a.classList.toggle('active', a.id === `nav-${space.id}`));
  crumb.textContent = `${space.label} — ${space.question}`;
  document.title = `VIGIL · ${space.label}`;
  main.innerHTML = '';
  main.append(el('div', { class: 'card' }, el('p', { class: 'dim' }, `Loading ${space.label}…`)));
  try {
    const host = el('div', { class: 'enter' });
    main.innerHTML = '';
    main.append(host);
    await space.view(host, arg ? decodeURIComponent(arg) : undefined);
  } catch (err) {
    main.innerHTML = '';
    main.append(degraded({ state: 'VIEW ERROR', what_failed: `${space.label} could not render (${err.message})`,
      affected: `the ${space.label} space only`, fallback: 'Other spaces remain available; reload to retry.' }));
    console.error(err);
  }
  main.scrollIntoView({ block: 'start', behavior: 'instant' });
}

addEventListener('hashchange', route);

/* ---- top bar: health pill, mode badge, pin, search, assistant ---- */
async function bootHealth() {
  const h = await api('/health');
  const pill = document.getElementById('health-pill');
  if (isBad(h)) { pill.className = 'tag neg'; pill.textContent = 'VIGIL HEALTH: UNREACHABLE'; return; }
  pill.className = `tag ${stateClass(h.vigil_health)}`;
  pill.textContent = `VIGIL HEALTH: ${h.vigil_health}`;
  pill.onclick = () => drawer('VIGIL health', 'SYSTEM STATE — NOT FORECAST TRUST',
    el('div', { class: 'list' }, h.components.map(c => el('div', {
      class: `evidence ${c.state === 'OK' ? 'sup' : c.state === 'DEGRADED' ? 'con' : 'unk'}` },
      el('span', { class: 'ch' }, c.component),
      el('div', {}, el('div', { class: 'row' }, tag(c.state, stateClass(c.state))),
        el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:12.5px' }, c.detail))))));
  document.getElementById('mode-badge').textContent = h.data_mode;
}

document.getElementById('pin-btn').onclick = () => {
  const pins = JSON.parse(localStorage.getItem('vigil.pins') || '[]');
  const route = location.hash || '#pulse';
  if (!pins.some(p => p.route === route)) {
    pins.push({ route, label: crumb.textContent, type: route.replace('#', '').split('/')[0].slice(0, 4).toUpperCase() });
    localStorage.setItem('vigil.pins', JSON.stringify(pins));
  }
  location.hash = '#workspace';
};

const searchInput = document.getElementById('search');
const searchOut = document.getElementById('search-results');
let searchTimer;
searchInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) { searchOut.classList.remove('open'); return; }
  searchTimer = setTimeout(async () => {
    const r = await api(`/search?q=${encodeURIComponent(q)}`);
    searchOut.innerHTML = '';
    if (isBad(r) || !r.results.length) {
      searchOut.append(el('a', { href: '#' }, el('span', { class: 'faint' }, 'No matches in cases, instruments, models, experiments or architecture.')));
    } else {
      r.results.forEach(res => searchOut.append(el('a', { href: res.route,
        onclick: () => { searchOut.classList.remove('open'); searchInput.value = ''; } },
        el('span', { class: 'k' }, res.kind), el('span', {}, res.label),
        el('span', { class: 'faint', style: 'margin-left:auto;font-size:11.5px' }, res.detail || ''))));
    }
    searchOut.classList.add('open');
  }, 180);
});
searchInput.addEventListener('keydown', e => { if (e.key === 'Escape') { searchOut.classList.remove('open'); searchInput.blur(); } });
addEventListener('click', e => { if (!e.target.closest('.search')) searchOut.classList.remove('open'); });
addEventListener('keydown', e => {
  if ((e.key === '/' || (e.key === 'k' && (e.metaKey || e.ctrlKey))) && document.activeElement !== searchInput) {
    e.preventDefault(); searchInput.focus();
  }
});

document.getElementById('ask-btn').onclick = () => {
  const log = el('div', { class: 'list', style: 'max-height:52vh;overflow:auto' });
  const input = el('input', { class: 'input', placeholder: 'Ask about what VIGIL measured…', 'aria-label': 'Question' });
  const send = async () => {
    const q = input.value.trim(); if (!q) return;
    input.value = '';
    log.append(el('div', { class: 'evidence' }, el('span', { class: 'ch' }, 'YOU'), el('div', {}, q)));
    const r = await api('/assistant', { method: 'POST', body: { question: q } });
    if (isBad(r)) return log.append(degraded(r));
    log.append(el('div', { class: 'evidence sup' }, el('span', { class: 'ch' }, 'VIGIL'),
      el('div', { style: 'flex:1' }, el('p', { style: 'margin:0;white-space:pre-wrap' }, r.answer),
        el('div', { class: 'row', style: 'margin-top:8px' },
          el('span', { class: 'chip' }, `grounded in ${r.sources.length} artefact(s)`),
          ...r.sources.map(s => el('span', { class: 'chip' }, s)),
          r.route ? el('a', { class: 'chip on', href: r.route, onclick: closeDrawer }, 'open →') : null))));
    log.scrollTop = log.scrollHeight;
  };
  drawer('Ask VIGIL', 'ANSWERS ONLY FROM MEASURED ARTEFACTS — NO GENERATED NUMBERS',
    el('div', {},
      el('p', { class: 'faint', style: 'margin:0 0 12px;font-size:12.5px' },
        'This assistant retrieves from stored results. If a question is outside what VIGIL measured, it says so rather than guessing.'),
      log,
      el('div', { class: 'row', style: 'margin-top:14px' }, input,
        el('button', { class: 'btn primary sm', onclick: send }, 'Ask')),
      el('div', { class: 'row', style: 'margin-top:10px' },
        ['Does the model beat the baseline?', 'Why no action today?', 'How is leakage prevented?', 'What does the backtest say?']
          .map(q => el('button', { class: 'chip', onclick: () => { input.value = q; send(); } }, q)))));
  input.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
  setTimeout(() => input.focus(), 50);
};

document.getElementById('refresh-btn').onclick = () => { clearCache(); route(); };

bootHealth();
route();
