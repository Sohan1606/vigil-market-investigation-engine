/* The seven VIGIL experiences. Each concept has exactly ONE canonical home:
     PULSE        market state, attention, open cases, signal field
     INVESTIGATE  cases + evidence ledger + similar history
     FORECAST     prediction, trajectory, technical evidence, model consensus
     DECISION     gate checks, uncertainty, drift, calibration, risk, cost, verdict, stress, audit
     REPLAY       point-in-time reconstruction and decision grading
     LAB          tournament, experiments, backtest, decision quality, failures, cemetery, patterns
     OBSERVATORY  lake/MapReduce/Spark/stream/docstore topology, lineage, telemetry, health
   Cross-links are compact references only — never duplicated cards. */
import { api } from './api.js';
import { bar, barChart, degraded, drawer, el, gauge, isBad, kpi, lineChart, num, pct,
         reliabilityChart, section, sign, stateClass, table, tag, titleCase, verdictClass } from './ui.js';

const grid = (cols, ...kids) => el('div', { class: 'grid stagger', style: `grid-template-columns:${cols}` }, kids);
const auto = (min = '260px') => `repeat(auto-fit,minmax(${min},1fr))`;

/* ========================================================== PULSE */
export async function pulse(root) {
  const d = await api('/pulse');
  if (isBad(d)) return root.append(degraded(d));
  root.append(
    el('div', { class: 'view-head' },
      el('div', {},
        el('p', { class: 'meta' }, `MARKET PULSE · ${d.as_of} · ${d.data_mode}`),
        el('h1', { class: 'h1' }, 'What is happening?')),
      el('div', { class: 'row' },
        tag(d.market_state.replace('_', ' '), stateClass(d.market_state === 'TRANSITION' ? 'WATCH' : 'OK')),
        tag(`${d.open_case_count} open cases`, 'info'))),

    grid(auto('230px'),
      el('div', { class: 'card' }, kpi('MARKET STATE', d.market_state.replace('_', ' '),
        `regime confidence ${pct(Math.max(...Object.values(d.regime_probabilities)))}`)),
      el('div', { class: 'card' }, kpi('MARKET PRESSURE', `${d.market_pressure} / 100`),
        bar(d.market_pressure, d.market_pressure > 66 ? 'warn' : '')),
      el('div', { class: 'card' }, kpi('STABILITY', d.stability, `volatility ${num(d.volatility_20_pct)}% annualised`)),
      el('div', { class: 'card' }, kpi('PARTICIPATION', d.participation,
        `${num(d.participation_pct)}% of the universe advancing (20-session mean)`))),

    el('div', { class: 'grid', style: 'grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);margin-top:18px' },
      section('SIGNAL FIELD', 'Instruments clustered by measured co-movement. Size = centrality, ring = open verdict.',
        fieldCanvas(d.field)),
      el('div', { class: 'grid' },
        section("TODAY'S ATTENTION", 'Derived from streaming anomaly scores and regime statistics.',
          el('div', { class: 'list' }, (d.attention || []).length
            ? d.attention.map(a => el('div', { class: 'evidence' },
                el('div', {}, el('p', { style: 'margin:0 0 3px' }, a.headline),
                  el('p', { class: 'faint', style: 'margin:0;font-size:12px' }, a.detail))))
            : [el('p', { class: 'faint' }, 'No unusual activity in the current window.')])),
        section('OPEN CASES', 'Full investigation lives in INVESTIGATE.',
          el('div', { class: 'list' }, (d.open_cases || []).length
            ? d.open_cases.map(c => el('a', { class: 'evidence', href: `#investigate/${c.case_id}` },
                el('span', { class: 'ch' }, c.case_id),
                el('div', {}, el('p', { style: 'margin:0' }, c.title),
                  el('p', { class: 'faint', style: 'margin:2px 0 0;font-size:12px' },
                    `${c.sector} · opened ${c.opened_at} · severity ${num(c.severity, 1)}σ`))))
            : [el('p', { class: 'faint' }, 'No open cases.')])))),

    el('div', { style: 'margin-top:18px' },
      section('MARKET PRESSURE — LAST 30 SESSIONS', 'Pressure combines weak participation, volatility and regime ambiguity.',
        lineChart([{ values: d.sparkline.map(p => p.pressure) }], {
          height: 150, labels: d.sparkline.map((p, i) => i % 7 === 0 ? p.date.slice(5) : '') }))));
}

function fieldCanvas(field) {
  if (!field || field.available === false) return el('p', { class: 'faint' }, 'Graph artefact unavailable.');
  const wrap = el('div');
  const canvas = el('canvas', { class: 'field', role: 'img',
    'aria-label': `Market signal field with ${field.nodes.length} instruments and ${field.edges.length} correlation links` });
  const info = el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12px' },
    `${field.nodes.length} instruments · ${field.edges.length} links above the correlation threshold · modularity ${num(field.modularity)} · correlation structure ${titleCase(field.correlation_state || 'unknown')}`);
  const detail = el('div', { style: 'margin-top:8px;min-height:22px' });
  wrap.append(canvas, info, detail);
  requestAnimationFrame(() => drawField(canvas, field, detail));
  return wrap;
}

function drawField(canvas, field, detail) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ctx = canvas.getContext('2d');
  const sectors = [...new Set(field.nodes.map(n => n.sector))];
  const place = () => {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const W = canvas.width = canvas.clientWidth * dpr, H = canvas.height = canvas.clientHeight * dpr;
    const groups = {};
    sectors.forEach((sec, i) => {
      const a = (i / sectors.length) * Math.PI * 2;
      groups[sec] = { x: W / 2 + Math.cos(a) * W * 0.26, y: H / 2 + Math.sin(a) * H * 0.3 };
    });
    const counts = {};
    field.nodes.forEach(n => {
      const g = groups[n.sector]; counts[n.sector] = (counts[n.sector] || 0);
      const k = counts[n.sector]++;
      const a = k * 1.9;
      n._x = g.x + Math.cos(a) * (18 + k * 11) * dpr;
      n._y = g.y + Math.sin(a) * (18 + k * 11) * dpr;
      n._r = (4 + 11 * (n.eigen_centrality || 0.2)) * dpr;
    });
    return { W, H, dpr };
  };
  let { dpr } = place();
  const byId = Object.fromEntries(field.nodes.map(n => [n.symbol, n]));
  let hover = null, t = 0;
  function frame(time) {
    t = time;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    field.edges.forEach(e => {
      const a = byId[e.source], b = byId[e.target]; if (!a || !b) return;
      const act = hover && (hover === a || hover === b);
      ctx.strokeStyle = act ? 'rgba(166,146,255,.75)' : `rgba(150,140,255,${0.05 + 0.2 * (e.weight - 0.3)})`;
      ctx.lineWidth = (act ? 1.6 : 0.8) * dpr;
      ctx.beginPath(); ctx.moveTo(a._x, a._y); ctx.lineTo(b._x, b._y); ctx.stroke();
    });
    field.nodes.forEach(n => {
      const pulse = reduced ? 0 : 0.5 + 0.5 * Math.sin(time * 0.001 + n._x);
      const col = n.verdict === 'POSITIVE BIAS' ? '#3FB27F' : n.verdict === 'NEGATIVE BIAS' ? '#E0616A' : '#9FA6B8';
      ctx.beginPath(); ctx.arc(n._x, n._y, n._r + (hover === n ? 3 * dpr : 0), 0, Math.PI * 2);
      ctx.fillStyle = hover === n ? '#ECEDF2' : col; ctx.globalAlpha = hover && hover !== n ? .35 : .9;
      ctx.fill(); ctx.globalAlpha = 1;
      if (n.probability !== null && n.probability !== undefined && Math.abs(n.probability - .5) > .04) {
        ctx.beginPath(); ctx.arc(n._x, n._y, n._r + (4 + pulse * 2) * dpr, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(110,91,255,.45)'; ctx.lineWidth = dpr; ctx.stroke();
      }
      ctx.fillStyle = hover === n ? '#ECEDF2' : 'rgba(236,237,242,.55)';
      ctx.font = `${10 * dpr}px ui-monospace,monospace`;
      ctx.fillText(n.symbol.split('.')[0], n._x + n._r + 4 * dpr, n._y + 3 * dpr);
    });
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  canvas.addEventListener('mousemove', ev => {
    const r = canvas.getBoundingClientRect();
    const x = (ev.clientX - r.left) * (canvas.width / r.width), y = (ev.clientY - r.top) * (canvas.height / r.height);
    hover = field.nodes.find(n => Math.hypot(n._x - x, n._y - y) < n._r + 9 * dpr) || null;
    detail.innerHTML = hover
      ? `<span class="mono" style="font-size:12px">${hover.name} · ${hover.sector} · centrality ${num(hover.eigen_centrality)} · ${hover.verdict || 'no stored verdict'} ${hover.probability ? '· p(up) ' + pct(hover.probability) : ''}</span>`
      : '';
    canvas.style.cursor = hover ? 'pointer' : 'default';
  });
  canvas.addEventListener('click', () => { if (hover) location.hash = `#forecast/${hover.symbol}`; });
  addEventListener('resize', () => { ({ dpr } = place()); }, { passive: true });
}

/* ========================================================== INVESTIGATE */
export async function investigate(root, caseId) {
  const list = await api('/cases');
  if (isBad(list)) return root.append(degraded(list));
  const selected = caseId || (list.cases[0] && list.cases[0].case_id);
  const detail = selected ? await api(`/cases/${selected}`) : null;

  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, `CASE FILES · ${list.count} INVESTIGATIONS`),
        el('h1', { class: 'h1' }, 'Why is it happening?')),
      detail && !isBad(detail) ? el('div', { class: 'row' },
        el('a', { class: 'btn sm ghost', href: `/api/export/case.pdf?id=${detail.case_id}` }, 'Export case PDF'),
        el('a', { class: 'btn sm ghost', href: `/api/export/case.json?id=${detail.case_id}` }, 'JSON')) : null),
    el('div', { class: 'grid investigate-grid', style: 'grid-template-columns:minmax(220px,.5fr) minmax(0,1.5fr)' },
      el('div', { class: 'card', style: 'max-height:78vh;overflow:auto' },
        el('p', { class: 'meta', style: 'margin:0 0 10px' }, 'CASE INDEX'),
        el('div', { class: 'list' }, list.cases.map(c => el('a', {
          class: 'evidence' + (c.case_id === selected ? ' sup' : ''), href: `#investigate/${c.case_id}` },
          el('div', {}, el('p', { class: 'mono', style: 'margin:0;font-size:11.5px;color:var(--violet)' }, c.case_id),
            el('p', { style: 'margin:3px 0 0;font-size:13px' }, c.title),
            el('p', { class: 'faint', style: 'margin:3px 0 0;font-size:11.5px' },
              `${c.opened_at} · ${c.status} · ${num(c.severity, 1)}σ`))))))
      , detail && !isBad(detail) ? caseFile(detail) : el('div', { class: 'empty' }, 'No case selected.')));
}

function caseFile(c) {
  const ledger = c.evidence;
  const items = (kind, cls) => (ledger[kind] || []).map(e => el('div', { class: `evidence ${cls}` },
    el('span', { class: 'ch' }, e.channel),
    el('div', { style: 'flex:1' },
      el('p', { style: 'margin:0;font-size:13.5px' }, e.statement),
      el('div', { class: 'row', style: 'margin-top:6px' },
        el('span', { class: 'chip' }, e.information_state),
        el('span', { class: 'chip' }, `strength ${num(e.strength)}`),
        e.value !== null ? el('span', { class: 'chip' }, `value ${num(e.value, 3)}`) : null),
      el('div', { style: 'margin-top:7px' }, bar(e.strength * 100, cls === 'sup' ? 'pos' : cls === 'con' ? 'neg' : '')))));

  return el('div', { class: 'grid enter' },
    el('div', { class: 'card' },
      el('div', { class: 'between' },
        el('div', {}, el('p', { class: 'meta' }, `${c.case_id} · OPENED ${c.opened_at} · ${c.sector}`),
          el('h2', { class: 'h2', style: 'margin:4px 0 0' }, c.title)),
        tag(c.status, c.status === 'RESOLVED' ? 'pos' : 'warn')),
      el('p', { class: 'dim', style: 'margin:14px 0 0' }, el('strong', {}, 'Trigger: '), c.trigger),
      el('div', { class: 'row', style: 'margin-top:12px' }, (c.symbols || []).map(sym =>
        el('a', { class: 'chip on', href: `#forecast/${sym}` }, sym)))),

    el('div', { class: 'card' },
      el('p', { class: 'meta', style: 'margin:0 0 10px' }, 'LIFECYCLE'),
      el('div', { class: 'row' }, (c.lifecycle || []).map((stage, i) => {
        const reached = ['TRIGGERED', 'INVESTIGATING'].includes(stage) ||
          (c.status === 'RESOLVED' && i <= 5);
        return el('span', { class: 'chip' + (reached ? ' on' : '') }, `${i + 1} ${titleCase(stage)}`);
      }))),

    el('div', { class: 'grid', style: `grid-template-columns:${auto('300px')}` },
      section(`SUPPORTING EVIDENCE (${(ledger.supporting || []).length})`, 'Observations available at the cutoff.',
        el('div', { class: 'list' }, items('supporting', 'sup').length ? items('supporting', 'sup')
          : [el('p', { class: 'faint' }, 'None recorded.')])),
      section(`CONTRADICTING EVIDENCE (${(ledger.contradicting || []).length})`, 'Channels pointing the other way.',
        el('div', { class: 'list' }, items('contradicting', 'con').length ? items('contradicting', 'con')
          : [el('p', { class: 'faint' }, 'None recorded.')]))),

    (ledger.unavailable || []).length ? section('UNAVAILABLE INFORMATION',
      'Missing channels reduce trust — they are never assumed neutral.',
      el('div', { class: 'list' }, items('unavailable', 'unk'))) : null,

    section('VIGIL INTERPRETATION', c.causal_language_note,
      el('div', { class: 'row', style: 'margin-bottom:10px' },
        tag(c.interpretation.information_state, stateClass(c.interpretation.information_state)),
        el('span', { class: 'chip' }, `conflict index ${c.interpretation.conflict_index}`)),
      el('p', { style: 'margin:0' }, c.interpretation.text)),

    section('SIMILAR HISTORICAL SITUATIONS', 'Nearest market-state neighbours. Similarity is contextual, not predictive.',
      (c.similar_historical_cases || []).length
        ? table(['Date', 'State distance'], c.similar_historical_cases.map(s => [s.date, num(s.distance)]))
        : el('p', { class: 'faint' }, 'No comparable prior state within the window.')));
}

/* ========================================================== FORECAST */
export async function forecast(root, symbol) {
  const uni = await api('/universe');
  if (isBad(uni)) return root.append(degraded(uni));
  const sym = symbol || uni.instruments[0].symbol;
  const horizon = Number(sessionStorage.getItem('vigil.horizon') || 1);
  const [f, hist] = await Promise.all([
    api(`/forecast/${sym}?horizon=${horizon}`), api(`/forecast/${sym}/history?horizon=1`)]);
  if (isBad(f)) return root.append(degraded(f));

  const traj = (hist.points || []).slice(-60);
  const ev = f.evidence;

  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, `FORECAST · ${f.symbol} · AS OF ${f.as_of} · CUTOFF ${f.information_cutoff}`),
        el('h1', { class: 'h1' }, 'What could happen next?')),
      el('div', { class: 'row' },
        el('select', { class: 'chip', 'aria-label': 'Instrument', style: 'padding:8px',
          onchange: e => { location.hash = `#forecast/${e.target.value}`; } },
          uni.instruments.map(i => el('option', { value: i.symbol, selected: i.symbol === sym },
            `${i.name} — ${i.symbol}`))),
        ...[1, 3, 5, 20].map(h => el('button', {
          class: 'chip' + (h === horizon ? ' on' : ''),
          onclick: () => { sessionStorage.setItem('vigil.horizon', h); location.reload(); } },
          `${h} session${h > 1 ? 's' : ''}`)))),

    grid(auto('250px'),
      el('div', { class: 'card' }, kpi('DIRECTION', f.direction === 'UP' ? 'Positive' : 'Negative',
        `calibrated probability ${pct(f.probability_up)}`)),
      el('div', { class: 'card' }, kpi('EXPECTED MOVE', `±${num(f.expected_move_pct)}%`,
        f.interval_80.low_pct !== null
          ? `80% conformal range ${num(f.interval_80.low_pct)}% … ${num(f.interval_80.high_pct)}%`
          : 'interval unavailable')),
      el('div', { class: 'card' }, kpi('MODEL CONSENSUS', pct(f.model_agreement, 0),
        `${Object.keys(f.member_probabilities).length} models · spread ${num(f.uncertainty.spread, 3)}`)),
      el('div', { class: 'card' },
        el('span', { class: 'l', style: 'font-family:var(--ff-mono);font-size:10px;letter-spacing:.16em;color:var(--faint)' }, 'VERDICT'),
        el('div', { class: 'row', style: 'margin-top:8px' }, tag(f.verdict, verdictClass(f.verdict))),
        el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12px' },
          'Reliability and the verdict are owned by '),
        el('a', { class: 'btn sm', style: 'margin-top:6px', href: `#decision/${f.symbol}` }, 'Decision Gate →'))),

    el('div', { class: 'grid', style: 'grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);margin-top:18px' },
      section('FORECAST TRAJECTORY', 'Out-of-sample calibrated probability for the next session, last 60 observations.',
        traj.length ? lineChart([{ values: traj.map(p => p.probability) }], {
          height: 190, yDomain: [0.3, 0.7], zero: 0.5,
          labels: traj.map((p, i) => i % 12 === 0 ? p.date.slice(5) : ''),
          markers: traj.map((p, i) => p.outcome === null ? null :
            ({ i, v: p.probability, color: (p.probability >= .5) === (p.outcome === 1) ? '#3FB27F' : '#E0616A' })).filter(Boolean)
        }) : el('p', { class: 'faint' }, 'No out-of-sample history for this symbol yet.'),
        el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12px' },
          'Dots mark resolved sessions — green where the stated direction matched the outcome.')),
      section('MODEL DEBATE', 'Each member’s own probability before ensembling.',
        barChart(Object.entries(f.member_probabilities).filter(([, v]) => v !== null).map(([k, v]) => ({
          label: k.split('_').map(s => s.slice(0, 4)).join('·'), value: Number((v * 100).toFixed(1)),
          display: `${(v * 100).toFixed(0)}%`, color: v >= 50 / 100 ? '#3FB27F' : '#E0616A'
        })), { height: 180, zero: false }),
        el('div', { class: 'row', style: 'margin-top:10px' },
          Object.entries(f.member_weights || {}).map(([k, w]) =>
            el('span', { class: 'chip' }, `${k.split('_')[0]} w=${num(w, 2)}`))))),

    el('div', { style: 'margin-top:18px' },
      section('WHY — EVIDENCE CONTRIBUTION', `Conflict state: ${ev.conflict_state} (index ${ev.conflict_index}). ${ev.causal_note}`,
        el('div', { class: 'grid', style: `grid-template-columns:${auto('290px')}` },
          ev.items.map(item => el('div', {
            class: `evidence ${item.direction === 'SUPPORTS' ? 'sup' : item.direction === 'CONTRADICTS' ? 'con' : 'unk'}` },
            el('span', { class: 'ch' }, item.channel),
            el('div', { style: 'flex:1' },
              el('p', { style: 'margin:0;font-size:13px' }, item.statement),
              el('div', { class: 'row', style: 'margin-top:6px' },
                el('span', { class: 'chip' }, item.information_state),
                el('span', { class: 'chip' }, item.direction.toLowerCase())),
              el('div', { style: 'margin-top:7px' },
                bar(item.strength * 100, item.direction === 'SUPPORTS' ? 'pos' : item.direction === 'CONTRADICTS' ? 'neg' : '')))))))),

    el('div', { style: 'margin-top:18px' },
      el('details', {},
        el('summary', {}, 'Technical detail — price context, forecast contract and lineage'),
        el('div', { class: 'grid', style: `grid-template-columns:${auto('240px')};margin-top:14px` },
          el('div', { class: 'card tight' }, kpi('CLOSE', num(f.price_context.close), `${sign(f.price_context.ret_1_pct)}% session`)),
          el('div', { class: 'card tight' }, kpi('RSI(14)', num(f.price_context.rsi_14, 0))),
          el('div', { class: 'card tight' }, kpi('VOLATILITY', `${num(f.price_context.vol_20_pct)}%`, '20-session annualised')),
          el('div', { class: 'card tight' }, kpi('REL. VOLUME', `${num(f.price_context.rel_volume)}x`, 'vs 20-session mean'))),
        el('div', { style: 'margin-top:14px' },
          table(['Contract field', 'Value'], [
            ['forecast_id', f.forecast_id], ['target', f.target], ['horizon', `${f.horizon} session(s)`],
            ['information cutoff', f.information_cutoff], ['evaluation rule', f.evaluation_rule],
            ['model version', f.model_version], ['feature version', f.feature_version],
            ['dataset version', f.dataset_version], ['code fingerprint', f.code_fingerprint],
            ['status', f.status], ['served from', f.served_from || '—']])))));
}

/* ========================================================== DECISION GATE */
export async function decision(root, symbol) {
  const uni = await api('/universe');
  const sym = symbol || (uni.instruments && uni.instruments[0].symbol);
  const d = await api(`/decision/${sym}`);
  if (isBad(d)) return root.append(degraded(d));
  const g = d.gate;

  const checks = el('div', { class: 'gatelist' }, g.checks.map(c => el('div', {
    class: 'gatecheck' + (c.passed ? '' : ' fail') },
    el('span', { class: 'k' }, c.name),
    tag(c.passed ? 'PASS' : 'BLOCK', c.passed ? 'pos' : 'neg'),
    el('span', { class: 'd' }, `${c.explanation} — measured ${num(c.value, 3)} vs threshold ${num(c.threshold, 3)} (weight ${num(c.weight, 2)})`))));

  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, `DECISION GATE · ${d.symbol} · ${d.as_of}`),
        el('h1', { class: 'h1' }, 'Can this forecast be trusted?')),
      el('div', { class: 'row' },
        el('select', { class: 'chip', style: 'padding:8px', 'aria-label': 'Instrument',
          onchange: e => { location.hash = `#decision/${e.target.value}`; } },
          uni.instruments.map(i => el('option', { value: i.symbol, selected: i.symbol === sym }, i.symbol))),
        el('button', { class: 'btn sm', onclick: () => stress(sym) }, 'Stress test'),
        el('button', { class: 'btn sm', onclick: () => audit(d) }, 'VIGIL audit'))),

    el('div', { class: `verdict ${verdictClass(d.verdict) === 'pos' ? 'positive' : verdictClass(d.verdict) === 'neg' ? 'negative' : 'noaction'}` },
      el('p', { class: 'meta' }, 'VIGIL VERDICT'),
      el('p', { class: 'big', style: 'margin:8px 0 10px' }, d.verdict),
      el('p', { class: 'lead', style: 'margin:0' }, g.reason),
      el('div', { class: 'row', style: 'margin-top:18px' },
        el('span', { class: 'chip' }, `trust ${num(g.trust_score, 0)}/100`),
        el('span', { class: 'chip' }, `expected edge ${num(g.expected_edge_bps, 0)} bps`),
        el('span', { class: 'chip' }, `cost ${num(g.cost_bps, 0)} bps`),
        el('span', { class: 'chip' + (g.net_edge_bps > 0 ? ' on' : '') }, `net ${sign(g.net_edge_bps, 0)} bps`))),

    el('div', { class: 'grid decision-grid', style: 'grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);margin-top:18px' },
      section('GATE CHECKS', 'Every check must clear before a forecast becomes a bias.', checks),
      el('div', { class: 'grid' },
        section('THREE DIFFERENT QUESTIONS', 'Data reliability, forecast confidence and decision trust are not the same thing.',
          el('div', { class: 'gauge-cluster' },
            gauge(d.data_quality.score, 'DATA', stateClass(d.data_quality.score > 90 ? 'OK' : 'WATCH')),
            gauge(100 * (1 - d.uncertainty.uncertainty), 'CONFIDENCE', stateClass(d.uncertainty.label === 'LOW' ? 'OK' : d.uncertainty.label === 'MEDIUM' ? 'WATCH' : 'HIGH')),
            gauge(g.trust_score, 'TRUST', g.trust_score > 70 ? 'pos' : g.trust_score > 45 ? 'warn' : 'neg')),
          el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12px' },
            `Uncertainty ${d.uncertainty.label} — sharpness deficit ${num(d.uncertainty.components.sharpness_deficit)}, member spread ${num(d.uncertainty.components.member_spread)}, disagreement ${num(d.uncertainty.components.disagreement)}.`)),
        section('MODEL HEALTH & DRIFT', 'Degraded models are down-weighted or excluded.',
          el('div', { class: 'row' }, tag(d.drift?.health?.state || 'UNKNOWN', stateClass(d.drift?.health?.state))),
          el('ul', { class: 'dim', style: 'margin:10px 0 0;padding-left:18px;font-size:13px' },
            (d.drift?.health?.reasons || ['no drift report']).map(r => el('li', {}, r))),
          d.drift?.performance?.available ? el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12px' },
            `Recent Brier ${num(d.drift.performance.recent_brier, 4)} vs baseline ${num(d.drift.performance.baseline_brier, 4)} (Δ ${sign(d.drift.performance.brier_degradation, 4)}).`) : null),
        section('STABILITY · RISK · MODALITY', 'Inputs that modulate trust.',
          el('div', { class: 'grid', style: `grid-template-columns:${auto('150px')}` },
            el('div', {}, el('p', { class: 'meta' }, 'FORECAST STABILITY'),
              el('div', { class: 'row' }, tag(d.stability.label || 'UNKNOWN', stateClass(d.stability.label)),
                el('span', { class: 'chip' }, `score ${num(d.stability.stability_score)}`)),
              d.stability.trajectory ? lineChart([{ values: d.stability.trajectory }],
                { height: 90, yDomain: [0.35, 0.65], zero: 0.5 }) : null),
            el('div', {}, el('p', { class: 'meta' }, 'POSITION RISK'),
              el('div', { class: 'row' }, tag(d.risk.label, stateClass(d.risk.label)),
                el('span', { class: 'chip' }, `β ${num(d.risk.beta)}`),
                el('span', { class: 'chip' }, `vol ${num(d.risk.annualised_vol_pct)}%`),
                el('span', { class: 'chip' }, `VaR95 ${num(d.risk.var_95_pct)}%`))),
            el('div', {}, el('p', { class: 'meta' }, 'INFORMATION CHANNELS'),
              el('div', { class: 'row' }, Object.entries(d.modality.channels).map(([k, v]) =>
                el('span', { class: 'chip' + (v === 'AVAILABLE' ? ' on' : '') }, `${k} ${v === 'AVAILABLE' ? '✓' : '—'}`))),
              el('p', { class: 'faint', style: 'margin:8px 0 0;font-size:12px' }, d.modality.note)))))),

    el('div', { style: 'margin-top:18px' },
      section('HUMAN vs VIGIL', 'Record your own call. Stored for comparison once the horizon resolves — neither side is assumed correct.',
        el('div', { class: 'row' }, ['POSITIVE', 'NEGATIVE', 'NO ACTION'].map(call =>
          el('button', { class: 'btn sm', onclick: async (e) => {
            const r = await api('/human-vote', { method: 'POST', body: { symbol: sym, horizon: 1, call } });
            e.target.closest('.card').querySelector('.vote-out').textContent = r.__error
              ? r.what_failed : `Recorded. ${r.total_votes} vote(s) stored; agreement with VIGIL ${r.agreement_rate_pct}%.`;
          } }, call))),
        el('p', { class: 'vote-out faint', style: 'margin:10px 0 0;font-size:12.5px' }, ''))));

  async function stress(symbol) {
    const r = await api(`/forecast/${symbol}/stress`, { method: 'POST' });
    if (isBad(r)) return drawer('Stress test unavailable', 'FORECAST STRESS', degraded(r));
    drawer('Forecast stress test', `${symbol} · base verdict ${r.base_verdict} · robustness ${r.robustness_pct}%`,
      el('div', {},
        el('p', { class: 'dim' }, r.interpretation),
        el('div', { class: 'list', style: 'margin-top:14px' }, r.scenarios.map(sc =>
          el('div', { class: `evidence ${sc.verdict_changed ? 'con' : 'sup'}` },
            el('div', { style: 'flex:1' },
              el('div', { class: 'between' }, el('strong', {}, sc.label), tag(sc.verdict, verdictClass(sc.verdict))),
              el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:12.5px' }, sc.description),
              el('div', { class: 'row', style: 'margin-top:8px' },
                el('span', { class: 'chip' }, `p ${pct(sc.probability)}`),
                el('span', { class: 'chip' }, `trust ${num(sc.trust_score, 0)}`),
                el('span', { class: 'chip' }, `net ${sign(sc.net_edge_bps, 0)} bps`),
                sc.blocking_reasons.length ? el('span', { class: 'chip' }, `blocked: ${sc.blocking_reasons[0]}`) : null)))))));
  }

  async function audit(dec) {
    const fid = (await api(`/forecast/${dec.symbol}`)).forecast_id;
    const r = await api(`/audit/${fid}`);
    if (isBad(r)) return drawer('Audit unavailable', 'VIGIL AUDIT', degraded(r));
    drawer('VIGIL audit', `${fid} · ${r.verdict} · ${r.n_pass} passed / ${r.n_fail} failed / ${r.n_warn} warnings`,
      el('div', {},
        el('div', { class: 'list' }, r.checks.map(c => el('div', {
          class: `evidence ${c.result === 'PASS' ? 'sup' : c.result === 'WARN' ? 'unk' : 'con'}` },
          el('span', { class: 'ch' }, c.result),
          el('div', {}, el('strong', { style: 'font-size:13px' }, c.check),
            el('p', { class: 'faint', style: 'margin:4px 0 0;font-size:12.5px' }, c.detail))))),
        el('div', { style: 'margin-top:16px' },
          el('p', { class: 'meta' }, 'REPRODUCIBILITY CAPSULE'),
          table(['Field', 'Value'], Object.entries(r.reproducibility).map(([k, v]) => [k, String(v)])))));
  }
}

/* ========================================================== REPLAY */
export async function replayView(root) {
  const uni = await api('/universe');
  if (isBad(uni)) return root.append(degraded(uni));
  const state = { symbol: uni.instruments[0].symbol, date: '2024-06-14', horizon: 5 };
  const out = el('div', { style: 'margin-top:18px' });
  const timelineBox = el('div', { style: 'margin-top:18px' });

  const controls = el('div', { class: 'card' },
    el('div', { class: 'row' },
      el('label', { class: 'meta' }, 'INSTRUMENT'),
      el('select', { class: 'chip', style: 'padding:8px', onchange: e => state.symbol = e.target.value },
        uni.instruments.map(i => el('option', { value: i.symbol }, i.symbol))),
      el('label', { class: 'meta' }, 'HISTORICAL DATE'),
      el('input', { class: 'chip', type: 'date', value: state.date, style: 'padding:8px',
        onchange: e => state.date = e.target.value }),
      el('label', { class: 'meta' }, 'HORIZON'),
      el('select', { class: 'chip', style: 'padding:8px', onchange: e => state.horizon = Number(e.target.value) },
        [1, 3, 5, 20].map(h => el('option', { value: h, selected: h === 5 }, `${h} sessions`))),
      el('button', { class: 'btn primary sm', onclick: run }, 'Reconstruct'),
      el('button', { class: 'btn sm ghost', onclick: play }, 'Play 12 points')),
    el('p', { class: 'faint', style: 'margin:12px 0 0;font-size:12.5px' },
      'Each reconstruction retrains the live ensemble on data strictly older than the chosen session '
      + '(takes a few seconds — this is a real retrain, not a lookup).'));

  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, 'REPLAY · POINT-IN-TIME RECONSTRUCTION'),
        el('h1', { class: 'h1' }, 'Would VIGIL have known this then?'))),
    controls, out, timelineBox);

  async function run() {
    out.innerHTML = '';
    out.append(el('div', { class: 'card' }, el('p', { class: 'dim' }, 'Retraining on pre-cutoff data…')));
    const r = await api(`/replay?symbol=${state.symbol}&date=${state.date}&horizon=${state.horizon}`, { fresh: true });
    out.innerHTML = '';
    if (isBad(r)) return out.append(degraded(r));
    const f = r.forecast, o = r.outcome;
    out.append(el('div', { class: 'grid stagger' },
      el('div', { class: 'card' },
        el('p', { class: 'meta' }, `ANCHOR SESSION ${r.anchor_session} · CUTOFF ${r.information_cutoff}`),
        el('h2', { class: 'h2', style: 'margin:6px 0 14px' }, `${f.name} — what was knowable`),
        el('div', { class: 'grid', style: `grid-template-columns:${auto('210px')}` },
          el('div', {}, kpi('FORECAST', `${f.direction} ${pct(f.probability_up)}`)),
          el('div', {}, kpi('VERDICT', f.verdict)),
          el('div', {}, kpi('REGIME THEN', f.regime.state.replace('_', ' '), `${pct(f.regime.confidence, 0)} confidence`)),
          el('div', {}, kpi('TRUST', `${num(f.gate.trust_score, 0)}/100`)))),
      section('LEAK GUARD', 'The explicit statement of what was hidden.',
        table(['Guard', 'Value'], [
          ['training data ends', r.leak_guard.training_data_ends],
          ['sessions after anchor in dataset', r.leak_guard.sessions_after_anchor_in_dataset],
          ['sessions used after anchor', r.leak_guard.sessions_used_after_anchor],
          ['headlines visible at cutoff', r.leak_guard.news_visible],
          ['headlines hidden (published later)', r.leak_guard.news_hidden_because_later_than_cutoff]]),
        el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12.5px' }, r.leak_guard.statement)),
      r.visible_news.length ? section('HEADLINES VISIBLE AT THE CUTOFF', 'Only articles published at or before the cutoff.',
        el('div', { class: 'list' }, r.visible_news.map(n => el('div', { class: 'evidence' },
          el('div', {}, el('p', { style: 'margin:0;font-size:13px' }, n.headline),
            el('p', { class: 'faint', style: 'margin:4px 0 0;font-size:11.5px' },
              `${n.published_at} · ${n.provider} · sentiment ${num(n.sentiment)}`)))))) : null,
      o.resolved ? el('div', { class: `verdict ${o.forecast_correct ? 'positive' : 'negative'}` },
        el('p', { class: 'meta' }, `OUTCOME · RESOLVED ${o.resolution_date}`),
        el('p', { class: 'big', style: 'margin:8px 0' }, `${o.realised_direction} ${sign(o.realised_return_pct)}%`),
        el('div', { class: 'row' },
          tag(o.forecast_correct ? 'FORECAST CORRECT' : 'FORECAST WRONG', o.forecast_correct ? 'pos' : 'neg'),
          tag(titleCase(o.decision_quality), stateClass(o.decision_quality.includes('GOOD') ? 'OK' : 'WATCH')),
          el('span', { class: 'chip' }, `net P&L ${sign(o.net_pnl_pct)}%`),
          o.inside_interval_80 !== null ? el('span', { class: 'chip' },
            `inside 80% band: ${o.inside_interval_80 ? 'yes' : 'no'}`) : null),
        el('p', { class: 'dim', style: 'margin:14px 0 0' },
          o.acted ? 'VIGIL acted on this forecast.'
                  : 'VIGIL abstained. A wrong forecast with NO ACTION is still a defensible decision.'))
        : el('div', { class: 'empty' }, o.note)));
  }

  async function play() {
    timelineBox.innerHTML = '';
    timelineBox.append(el('div', { class: 'card' }, el('p', { class: 'dim' },
      'Replaying 12 historical decision points (each one retrains) — this takes a minute…')));
    const end = state.date;
    const start = new Date(new Date(end).getTime() - 1000 * 60 * 60 * 24 * 160).toISOString().slice(0, 10);
    const r = await api(`/replay/timeline?symbol=${state.symbol}&start=${start}&end=${end}&step=6&horizon=${state.horizon}`, { fresh: true });
    timelineBox.innerHTML = '';
    if (isBad(r)) return timelineBox.append(degraded(r));
    const line = el('div', { class: 'timeline' }, r.points.map((p, i) => el('span', {
      class: `pt ${p.correct === null || p.correct === undefined ? 'idle' : p.correct ? 'hit' : 'miss'}`,
      style: `left:${(i / Math.max(r.points.length - 1, 1)) * 96 + 2}%`,
      title: `${p.date} · ${p.verdict} · p=${pct(p.probability)} · realised ${sign(p.realised_pct)}%` })));
    timelineBox.append(section('REPLAY TIMELINE', `${r.points.length} reconstructions · hit rate ${r.hit_rate_pct ?? '—'}% · action rate ${r.action_rate_pct ?? '—'}%`,
      line,
      table(['Date', 'p(up)', 'Verdict', 'Realised', 'Decision quality'],
        r.points.map(p => [p.date, pct(p.probability), p.verdict, p.realised_pct === null ? '—' : `${sign(p.realised_pct)}%`,
          titleCase(p.decision_quality || '—')]))));
  }
}

/* ========================================================== RESEARCH LAB */
export async function lab(root) {
  const d = await api('/research/overview');
  if (isBad(d)) return root.append(degraded(d));
  const tabs = ['EXPERIMENTS', 'MODEL TOURNAMENT', 'RELIABILITY', 'ECONOMICS', 'FAILURES', 'PATTERNS'];
  let active = sessionStorage.getItem('vigil.lab') || tabs[0];
  const body = el('div');
  const tabbar = el('div', { class: 'tabs', role: 'tablist' }, tabs.map(t => el('button', {
    role: 'tab', 'aria-selected': String(t === active),
    onclick: () => { active = t; sessionStorage.setItem('vigil.lab', t); render(); } }, t)));

  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, 'RESEARCH LAB · ONE PROTOCOL, ONE SEED, PUBLISHED AS MEASURED'),
        el('h1', { class: 'h1' }, 'Does the system actually work?')),
      el('a', { class: 'btn sm ghost', href: '/api/export/experiments.pdf' }, 'Export research PDF')),
    tabbar, body);
  render();

  function render() {
    [...tabbar.children].forEach(b => b.setAttribute('aria-selected', String(b.textContent === active)));
    body.innerHTML = '';
    body.className = 'enter';
    if (active === 'EXPERIMENTS') return body.append(experiments(d.experiments));
    if (active === 'MODEL TOURNAMENT') return body.append(tournament(d.tournament));
    if (active === 'RELIABILITY') return body.append(reliability(d.calibration, d.uncertainty, d.drift));
    if (active === 'ECONOMICS') return body.append(economics(d.backtest, d.decision_quality));
    if (active === 'FAILURES') return body.append(failures(d.failure_lab));
    return body.append(patterns(d.patterns));
  }
}

function experiments(e) {
  if (isBad(e)) return degraded(e);
  const arms = e.experiments.filter(x => x.available);
  return el('div', { class: 'grid stagger' },
    el('p', { class: 'dim' }, e.integrity_note),
    ...arms.map(x => el('div', { class: 'card' },
      el('div', { class: 'between' },
        el('div', {}, el('p', { class: 'meta' }, x.experiment_id.replace(/_/g, ' ')),
          el('h3', { class: 'h3', style: 'margin:4px 0 0' }, x.research_question)),
        tag(x.conclusion?.verdict || '—', stateClass(x.conclusion?.verdict))),
      el('p', { class: 'dim', style: 'margin:12px 0 0;font-size:13.5px' },
        el('strong', {}, 'Hypothesis: '), x.hypothesis),
      el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:13px' },
        el('strong', {}, 'Manipulation: '), x.manipulation),
      x.metrics && x.metrics.brier ? el('div', { class: 'grid', style: `grid-template-columns:${auto('130px')};margin-top:14px` },
        el('div', {}, kpi('BRIER', num(x.metrics.brier, 5))),
        el('div', {}, kpi('ROC-AUC', num(x.metrics.roc_auc, 4))),
        el('div', {}, kpi('ACCURACY', pct(x.metrics.accuracy, 2))),
        el('div', {}, kpi('ECE', num(x.metrics.ece, 4))),
        el('div', {}, kpi('NET / TRADE', `${num(x.financial_metrics?.net_return_bps_per_trade, 1)} bps`,
          `${x.financial_metrics?.trades || 0} trades`))) : null,
      x.experiment_id === 'F_ABSTENTION_POLICY' ? el('div', { style: 'margin-top:12px' },
        table(['Policy', 'Trades', 'Gross bps/trade', 'Net bps/trade', 'Abstention'],
          Object.entries(x.financial_metrics).map(([k, v]) => [titleCase(k), v.trades,
            num(v.gross_return_bps_per_trade, 1), num(v.net_return_bps_per_trade, 1), `${v.abstention_rate_pct}%`]))) : null,
      el('p', { style: 'margin:14px 0 0;font-size:13.5px' }, x.conclusion?.statement || ''),
      x.dataset ? el('details', { style: 'margin-top:12px' },
        el('summary', {}, `Protocol & dataset — ${x.dataset.rows} rows, ${x.dataset.n_features} features`),
        el('p', { class: 'faint', style: 'margin:10px 0 0;font-size:12.5px' },
          `${x.dataset.period} · ${x.dataset.symbols} symbols · ${x.protocol.validation}, ${x.protocol.folds} folds, `
          + `train ${x.protocol.train_years}y / test ${x.protocol.test_months}m, purge ${x.protocol.purge_sessions} session(s) · model ${x.model.model_id}`),
        el('p', { class: 'faint', style: 'margin:8px 0 0;font-size:11.5px' }, (x.dataset.features || []).join(' · '))) : null)));
}

function tournament(t) {
  if (isBad(t)) return degraded(t);
  const rows = t.leaderboard.filter(r => r.available).sort((a, b) => a.metrics.brier - b.metrics.brier);
  const winner = rows[0];
  return el('div', { class: 'grid stagger' },
    el('div', { class: 'card' },
      el('p', { class: 'meta' }, `RANKED BY ${t.ranked_by.toUpperCase()}`),
      el('p', { class: 'dim', style: 'margin:10px 0 0' }, t.honest_note),
      el('div', { class: 'row', style: 'margin-top:12px' },
        el('span', { class: 'chip on' }, `winner: ${winner.name}`),
        el('span', { class: 'chip' }, `${t.protocol.validation}`),
        el('span', { class: 'chip' }, `purge ${t.protocol.purge_sessions} session(s)`),
        el('span', { class: 'chip' }, `random split used: ${t.protocol.random_split_used}`),
        el('span', { class: 'chip' }, `${t.n_features} features`))),
    section('LEADERBOARD', 'Identical folds, identical features, identical seed.',
      table(['Model', 'Family', 'Brier ↓', 'ROC-AUC ↑', 'Accuracy', 'vs majority', 'ECE', 'Fit (s)', 'Inference (ms/1k)'],
        rows.map(r => [r.name, r.family, num(r.metrics.brier, 5), num(r.metrics.roc_auc, 4),
          pct(r.metrics.accuracy, 2), sign(r.metrics.accuracy_vs_majority * 100, 2) + ' pp',
          num(r.metrics.ece, 4), num(r.mean_fit_seconds, 2), num(r.mean_inference_ms_per_1k, 2)]))),
    el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')}` },
      section('PROBABILITY QUALITY', 'Lower Brier is better; the baseline is the line to beat.',
        barChart(rows.map(r => ({ label: r.model_id.split('_')[0].slice(0, 6), value: Number((r.metrics.brier * 1000).toFixed(2)),
          display: r.metrics.brier.toFixed(4), color: r.model_id === t.winner ? '#A692FF' : 'rgba(255,255,255,.3)' })),
          { height: 190, zero: false })),
      section('PERFORMANCE BY REGIME', `${winner.name} broken down by market state.`,
        winner.by_regime?.length ? table(['Regime', 'n', 'Accuracy', 'ROC-AUC', 'Brier'],
          winner.by_regime.map(r => [r.regime, r.n, pct(r.accuracy, 2), num(r.roc_auc, 4), num(r.brier, 5)]))
          : el('p', { class: 'faint' }, 'Regime breakdown unavailable.'))),
    section('WALK-FORWARD FOLDS', 'Every fold trains on the past and tests on the future that follows it.',
      table(['Fold', 'Test window', 'Train rows', 'Test rows', 'Fit (s)'],
        winner.by_fold.map(f => [f.fold, `${f.test_start} → ${f.test_end}`, f.n_train, f.n_test, num(f.fit_seconds, 2)]))));
}

function reliability(cal, unc, drift) {
  if (isBad(cal)) return degraded(cal);
  return el('div', { class: 'grid stagger' },
    el('div', { class: 'grid', style: `grid-template-columns:${auto('300px')}` },
      section('CALIBRATION', cal.method,
        el('div', { class: 'grid', style: `grid-template-columns:${auto('120px')}` },
          el('div', {}, kpi('ECE BEFORE', num(cal.before.ece, 4))),
          el('div', {}, kpi('ECE AFTER', num(cal.after.ece, 4))),
          el('div', {}, kpi('BRIER BEFORE', num(cal.before.brier, 5))),
          el('div', {}, kpi('BRIER AFTER', num(cal.after.brier, 5)))),
        el('p', { class: 'dim', style: 'margin:12px 0 0' }, cal.verdict),
        el('div', { style: 'margin-top:12px' }, reliabilityChart(cal.reliability_after))),
      section('UNCERTAINTY — CONFORMAL COVERAGE', unc.method || '',
        unc.conformal?.available ? el('div', {},
          el('div', { class: 'grid', style: `grid-template-columns:${auto('130px')}` },
            el('div', {}, kpi('TARGET', pct(unc.conformal.target_coverage, 0))),
            el('div', {}, kpi('EMPIRICAL', pct(unc.conformal.empirical_coverage, 1))),
            el('div', {}, kpi('MEAN WIDTH', `${num(unc.conformal.mean_width_pct)}%`)),
            el('div', {}, kpi('EVALUATED', unc.conformal.n_evaluated))),
          el('p', { class: 'dim', style: 'margin:12px 0 0' }, unc.conformal.verdict))
          : el('p', { class: 'faint' }, 'Coverage test unavailable.'))),
    section('CONCEPT DRIFT', `Population Stability Index — ${drift.summary?.window || ''}`,
      el('div', { class: 'grid', style: `grid-template-columns:${auto('300px')}` },
        barChart((drift.features || []).slice(0, 10).map(f => ({ label: f.feature.slice(0, 7),
          value: Number(f.psi.toFixed(3)), display: f.psi.toFixed(2),
          color: f.level === 'HIGH' ? '#E0616A' : f.level === 'MODERATE' ? '#D9A441' : '#3FB27F' })), { height: 190, zero: false }),
        table(['Model', 'Health', 'Recent Brier', 'Δ vs baseline'],
          Object.entries(drift.models || {}).map(([k, v]) => [k,
            el('span', { class: `tag ${stateClass(v.health.state)}` }, v.health.state),
            num(v.performance?.recent_brier, 5), sign(v.performance?.brier_degradation, 5)])))));
}

function economics(bt, dq) {
  if (isBad(bt)) return degraded(bt);
  const names = Object.keys(bt.strategies).filter(k => bt.strategies[k].available);
  const curve = names.map(n => ({ values: bt.strategies[n].equity_curve.map(p => p.equity),
    className: n === 'BUY_AND_HOLD' ? 'dim' : n === 'ADAPTIVE_GATE' ? '' : 'alt' }));
  return el('div', { class: 'grid stagger' },
    el('div', { class: 'card' },
      el('p', { class: 'meta' }, `COST-AWARE BACKTEST · ${bt.window.start} → ${bt.window.end}`),
      el('p', { class: 'dim', style: 'margin:10px 0 0' }, bt.honest_note),
      el('div', { class: 'row', style: 'margin-top:12px' },
        Object.entries(bt.costs).map(([k, v]) => el('span', { class: 'chip' }, `${k.replace(/_/g, ' ')} ${v}`)))),
    section('EQUITY CURVES (NET OF COSTS)', 'Dashed = equal-weight buy & hold of the same universe.',
      lineChart(curve, { height: 230,
        labels: bt.strategies[names[0]].equity_curve.map((p, i) => i % 24 === 0 ? p.date.slice(0, 7) : '') }),
      el('div', { class: 'row', style: 'margin-top:10px' },
        names.map(n => el('span', { class: 'chip' }, titleCase(n))))),
    section('STRATEGY COMPARISON', 'Prediction quality, decision quality and economics are reported separately.',
      table(['Strategy', 'Annualised', 'Sharpe', 'Sortino', 'Max drawdown', 'VaR95', 'Hit rate', 'Abstention'],
        names.map(n => { const v = bt.strategies[n]; return [titleCase(n), `${num(v.annualised_return_pct)}%`,
          num(v.sharpe), num(v.sortino), `${num(v.max_drawdown_pct)}%`, `${num(v.var_95_pct)}%`,
          `${num(v.hit_rate_pct)}%`, `${num(v.abstention_rate_pct)}%`]; }))),
    !isBad(dq) ? section('DECISION QUALITY', dq.interpretation,
      el('div', { class: 'grid', style: `grid-template-columns:${auto('160px')}` },
        el('div', {}, kpi('FORECAST ACCURACY', `${num(dq.forecast_accuracy_pct)}%`)),
        el('div', {}, kpi('SELECTIVE ACCURACY', `${num(dq.selective_accuracy_pct)}%`)),
        el('div', {}, kpi('ACTION COVERAGE', `${num(dq.action_coverage_pct)}%`)),
        el('div', {}, kpi('ABSTENTION RATE', `${num(dq.abstention_rate_pct)}%`)),
        el('div', {}, kpi('NET WHEN ACTED', `${sign(dq.mean_net_return_when_acted_bps, 2)} bps`)),
        el('div', {}, kpi('DECISION UTILITY', `${sign(dq.decision_utility_bps, 3)} bps`)),
        el('div', {}, kpi('DOWNSIDE AVOIDED', `${sign(dq.downside_avoidance_bps, 1)} bps`))),
      el('p', { class: 'faint', style: 'margin:12px 0 0;font-size:12.5px' },
        'Decision utility = action coverage x net return when acted: value per evaluated '
        + 'opportunity, so abstaining contributes exactly zero. There is no single '
        + '"good decision rate" — abstention is reported, never scored.'),
      el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')};margin-top:14px` },
        table(['Outcome category', 'Count'], Object.entries(dq.categories).map(([k, v]) => [titleCase(k), v])),
        table(['Metric', 'Definition'], Object.entries(dq.metric_definitions || {}).map(([k, v]) => [titleCase(k), v])))) : null);
}

function failures(fl) {
  if (isBad(fl)) return degraded(fl);
  const box = el('div', { class: 'grid stagger' },
    el('div', { class: 'card' },
      el('p', { class: 'meta' }, 'PREDICTION FAILURE LAB'),
      el('div', { class: 'grid', style: `grid-template-columns:${auto('150px')};margin-top:10px` },
        el('div', {}, kpi('PREDICTIONS', fl.n_predictions)),
        el('div', {}, kpi('MISSES', fl.n_misses)),
        el('div', {}, kpi('MISS RATE', `${num(fl.miss_rate_pct)}%`)),
        el('div', {}, kpi('CEMETERY', fl.cemetery_size))),
      el('p', { class: 'faint', style: 'margin:12px 0 0;font-size:12.5px' }, fl.note)),
    el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')}` },
      section('FAILURE ATTRIBUTION', 'Conditions observable at prediction time (a miss can carry several tags).',
        barChart(Object.entries(fl.failure_attribution).map(([k, v]) => ({ label: k.split('_')[0].slice(0, 7), value: v })),
          { height: 190, zero: false }),
        el('div', { class: 'list', style: 'margin-top:12px' },
          Object.entries(fl.failure_attribution).map(([k, v]) => el('div', { class: 'evidence unk' },
            el('span', { class: 'ch' }, v), el('div', {}, el('strong', { style: 'font-size:13px' }, titleCase(k)),
              el('p', { class: 'faint', style: 'margin:4px 0 0;font-size:12.5px' }, fl.category_definitions[k])))))),
      el('div', { class: 'grid' },
        section('ACCURACY BY REGIME', 'Where the models work — and where they do not.',
          table(['Regime', 'n', 'Accuracy'], fl.accuracy_by_regime.map(r => [r.regime, r.n, pct(r.accuracy, 2)]))),
        section('NEWS COVERAGE EFFECT', `${fl.accuracy_with_news_vs_without.n_with_news} rows carried rolling 5-day news context (direct same-session coverage is far rarer — the two are reported separately in the Observatory).`,
          table(['Subset', 'Accuracy'], [['With news', pct(fl.accuracy_with_news_vs_without.with_news, 2)],
            ['Without news', pct(fl.accuracy_with_news_vs_without.without_news, 2)]])))),
    el('div', { id: 'cemetery' }, el('p', { class: 'dim' }, 'Loading prediction cemetery…')));
  api('/research/cemetery').then(c => {
    const host = box.querySelector('#cemetery');
    host.innerHTML = '';
    if (isBad(c)) return host.append(degraded(c));
    host.append(section('PREDICTION CEMETERY', 'The most confident wrong calls, kept as research objects.',
      el('div', { class: 'list' }, c.tombstones.map(t => el('div', { class: 'evidence con' },
        el('span', { class: 'ch' }, t.as_of),
        el('div', { style: 'flex:1' },
          el('div', { class: 'between' },
            el('strong', {}, `${t.symbol} — predicted ${t.predicted} at ${pct(t.stated_probability)}`),
            tag(`actual ${t.actual}`, 'neg')),
          el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:12.5px' },
            `realised ${sign(t.realised_return_pct)}% · regime ${t.regime} · agreement ${pct(t.model_agreement, 0)} · news ${t.news_available ? 'available' : 'unavailable'}`),
          el('p', { style: 'margin:8px 0 0;font-size:13px' }, el('strong', {}, 'Assumption that failed: '), t.assumption_that_failed),
          el('p', { style: 'margin:4px 0 0;font-size:13px;color:var(--violet)' }, el('strong', {}, 'Lesson: '), t.lesson),
          el('div', { class: 'row', style: 'margin-top:8px' }, t.failure_tags.map(tg => el('span', { class: 'chip' }, titleCase(tg))))))))));
  });
  return box;
}

function patterns(p) {
  if (isBad(p)) return degraded(p);
  return el('div', { class: 'grid stagger' },
    el('p', { class: 'dim' }, 'Market memory — historical pattern library. All forward statistics are descriptive '
      + 'summaries of past occurrences, never forecasts.'),
    ...p.map(x => el('div', { class: 'card' },
      el('div', { class: 'between' },
        el('div', {}, el('h3', { class: 'h3' }, titleCase(x.pattern_id)),
          el('p', { class: 'faint', style: 'margin:4px 0 0;font-size:12.5px' }, x.definition)),
        x.currently_active ? tag('ACTIVE NOW', 'info') : tag('dormant', 'idle')),
      el('div', { class: 'grid', style: `grid-template-columns:${auto('140px')};margin-top:14px` },
        el('div', {}, kpi('OCCURRENCES', x.occurrences, `${x.frequency_pct}% of sessions`)),
        el('div', {}, kpi('FWD MEAN', x.historical_forward_mean_pct === null ? '—' : `${sign(x.historical_forward_mean_pct)}%`,
          `${x.forward_horizon_sessions}-session horizon`)),
        el('div', {}, kpi('UP RATE', x.historical_up_rate_pct === null ? '—' : `${num(x.historical_up_rate_pct)}%`,
          `baseline ${num(x.baseline_up_rate_pct)}%`)),
        el('div', {}, kpi('LAST SEEN', x.last_seen || '—'))),
      el('p', { class: 'faint', style: 'margin:12px 0 0;font-size:12px' },
        el('span', { class: 'mono' }, x.detection_logic), x.sample_warning ? ` · ${x.sample_warning}` : ''))));
}

/* ========================================================== DATA OBSERVATORY */
export async function observatory(root) {
  const [d, health] = await Promise.all([api('/observatory'), api('/health')]);
  if (isBad(d)) return root.append(degraded(d));

  const topo = el('div', { class: 'topo' });
  d.architecture.forEach((n, i) => {
    topo.append(el('button', { class: 'node', onclick: () => explain(n) },
      el('span', { class: 't' }, n.id),
      el('span', { class: 's' }, n.title),
      el('div', { class: 'row', style: 'margin-top:9px' },
        Object.entries(n.metrics).slice(0, 2).map(([k, v]) =>
          el('span', { class: 'chip' }, `${k.replace(/_/g, ' ')}: ${Array.isArray(v) ? v.length : v}`)))));
    if (i < d.architecture.length - 1) topo.append(el('span', { class: 'flowline' }, '→'));
  });

  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, `DATA OBSERVATORY · ${health.data_mode}`),
        el('h1', { class: 'h1' }, 'How does the Big Data engine work?')),
      el('div', { class: 'row' }, tag(`VIGIL HEALTH: ${health.vigil_health}`, stateClass(health.vigil_health)))),

    (() => {
      const im = d.infrastructure_modes || {};
      const rows = [
        ['Storage', im.storage, im.storage && im.storage.active === 'HDFS' && im.storage.available],
        ['MapReduce', im.mapreduce, im.mapreduce && im.mapreduce.active === 'HADOOP'],
        ['Event bus', im.streaming, im.streaming && im.streaming.kafka_available],
        ['Document store', im.docstore, im.docstore && im.docstore.server_backed]];
      return section('INFRASTRUCTURE MODE — WHAT IS ACTUALLY RUNNING',
        (im.note || 'Probed at request time.'),
        el('div', { class: 'list' }, rows.filter(r => r[1]).map(([name, info, distributed]) => el('div', {
          class: `evidence ${distributed ? 'sup' : 'unk'}` },
          el('span', { class: 'ch' }, name),
          el('div', {},
            el('div', { class: 'row' },
              tag(info.label, distributed ? 'pos' : 'warn'),
              info.requested && info.requested !== info.active
                ? tag(`REQUESTED: ${info.requested}`, 'neg') : null),
            el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:12.5px' }, info.detail))))));
    })(),

    section('ARCHITECTURE FLOW', 'Click a component for what it does, why it exists and its syllabus connection.', topo),

    el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')};margin-top:18px` },
      section('SYSTEM HEALTH', 'System health is separate from forecast trust.',
        el('div', { class: 'list' }, health.components.map(c => el('div', {
          class: `evidence ${c.state === 'OK' ? 'sup' : c.state === 'DEGRADED' ? 'con' : 'unk'}` },
          el('span', { class: 'ch' }, c.component),
          el('div', {}, el('div', { class: 'row' }, tag(c.state, stateClass(c.state))),
            el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:12.5px' }, c.detail)))))),
      section('STREAM ALGORITHMS — MEASURED ERROR', `Bus mode ${d.streaming.bus_mode} · ${d.streaming.events_processed} events at ${num(d.streaming.throughput_eps, 0)} ev/s · p50 ${num(d.streaming.latency_p50_us)}µs / p95 ${num(d.streaming.latency_p95_us)}µs / p99 ${num(d.streaming.latency_p99_us)}µs`,
        table(['Sketch', 'State', 'Estimate', 'Exact', 'Error'], [
          ['Bloom filter', `${d.streaming.bloom.bits} bits · ${d.streaming.bloom.hashes} hashes`,
            `${d.streaming.duplicates_filtered} duplicates removed`, `${d.streaming.bloom.exact_unique_ids} unique ids`,
            `FP rate ${d.streaming.bloom.expected_fp_rate}`],
          ['Flajolet-Martin', `${d.streaming.flajolet_martin.registers} registers`,
            num(d.streaming.flajolet_martin.estimated_distinct, 0), d.streaming.flajolet_martin.exact_distinct,
            `${d.streaming.flajolet_martin.relative_error_pct}%`],
          ['DGIM', `${d.streaming.dgim.buckets} buckets / ${d.streaming.dgim.window} window`,
            d.streaming.dgim.estimated_ones, d.streaming.dgim.exact_ones, `${d.streaming.dgim.relative_error_pct}%`],
          ['Reservoir', `capacity ${d.streaming.reservoir.capacity}`, d.streaming.reservoir.sample_size,
            d.streaming.reservoir.stream_length, 'uniform sample']]))),

    el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')};margin-top:18px` },
      section('LAKE INVENTORY', `${(d.infrastructure_modes && d.infrastructure_modes.storage ? d.infrastructure_modes.storage.label : 'LOCAL DATA LAKE')} · Hive-partitioned Parquet; the same layout is written byte-for-byte to hdfs:// when STORAGE_MODE=hdfs.`,
        table(['Zone', 'Dataset', 'Rows', 'Blocks', 'MB', 'Partitions'],
          d.lake.map(i => [i.zone, i.dataset, i.rows ?? '—', i.blocks, num(i.bytes / 1e6, 2), (i.partitions || []).join(', ')]))),
      section('MAPREDUCE JOBS', `${(d.infrastructure_modes && d.infrastructure_modes.mapreduce ? d.infrastructure_modes.mapreduce.label : 'MapReduce Engine: LOCAL FALLBACK')} · map → shuffle/sort → reduce with measured counters.`,
        table(['Job', 'Splits', 'Map in', 'Map out', 'Keys', 'Reduce out', 'ms'],
          (d.mapreduce.jobs || []).map(j => [j.job_name, j.input_splits, j.map_input_records,
            j.map_output_records, j.shuffle_keys, j.reduce_output_records, num(j.total_ms, 0)])))),

    el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')};margin-top:18px` },
      section('DATA QUALITY TRIAGE', `Contract score ${d.data_quality.score}/100 · VALID / SUSPICIOUS → QUARANTINE / INVALID → REJECT`,
        el('div', { class: 'grid', style: `grid-template-columns:${auto('120px')}` },
          el('div', {}, kpi('ROWS IN', d.data_quality.rows_in)),
          el('div', {}, kpi('VALID', d.data_quality.rows_valid)),
          el('div', {}, kpi('QUARANTINED', d.data_quality.rows_quarantined)),
          el('div', {}, kpi('REJECTED', d.data_quality.rows_rejected))),
        el('div', { style: 'margin-top:14px' },
          table(['Check', 'Rows flagged'], Object.entries(d.data_quality.checks || {}).map(([k, v]) => [titleCase(k), v])))),
      section('DOCUMENT STORE', `${d.docstore.backend} · ${d.docstore.detail}`,
        table(['Collection', 'Documents'], Object.entries(d.docstore.collections).map(([k, v]) => [k, v])))),

    el('div', { class: 'grid', style: `grid-template-columns:${auto('320px')};margin-top:18px` },
      section('DATA LINEAGE', 'RAW → CLEANED → FEATURES → MODEL → FORECAST → DECISION → OUTCOME',
        el('div', { class: 'list' }, d.lineage.stages.map(s => el('div', { class: 'evidence' },
          el('span', { class: 'ch' }, s.stage),
          el('div', {}, el('p', { class: 'mono', style: 'margin:0;font-size:12px;color:var(--violet)' }, s.artifact),
            el('p', { class: 'faint', style: 'margin:4px 0 0;font-size:12.5px' },
              `${s.rows ?? '—'} records · ${s.detail}`)))))),
      section('MEASURED STAGE LATENCY', 'Wall-clock timings recorded during the last pipeline runs on this machine.',
        table(['Stage', 'Runs', 'Mean ms', 'Max ms'],
          Object.entries(d.latency_by_stage).map(([k, v]) => [k, v.runs, num(v.mean_ms, 0), num(v.max_ms, 0)])))),

    el('div', { style: 'margin-top:18px' },
      section('MLOPS RUN REGISTRY', 'Immutable run records: seed, versions, config fingerprint, git commit, artefacts.',
        table(['Run', 'Name', 'Created', 'Seed', 'Feature version', 'Config fingerprint'],
          d.runs.map(r => [r.run_id, r.name, r.created_at, r.seed, r.feature_version, r.config_fingerprint])))));

  function explain(n) {
    drawer(n.title, `${n.id} · ARCHITECTURE COMPONENT`, el('div', { class: 'grid' },
      el('div', {}, el('p', { class: 'meta' }, 'WHAT'), el('p', {}, n.what)),
      el('div', {}, el('p', { class: 'meta' }, 'WHY'), el('p', {}, n.why)),
      el('div', { class: 'grid', style: `grid-template-columns:${auto('140px')}` },
        el('div', {}, el('p', { class: 'meta' }, 'INPUT'), el('p', { style: 'font-size:13px' }, n.input)),
        el('div', {}, el('p', { class: 'meta' }, 'OUTPUT'), el('p', { style: 'font-size:13px' }, n.output))),
      el('div', {}, el('p', { class: 'meta' }, 'SYLLABUS CONNECTION'),
        el('p', { style: 'color:var(--violet)' }, n.syllabus)),
      el('div', {}, el('p', { class: 'meta' }, 'MEASURED STATE'),
        table(['Metric', 'Value'], Object.entries(n.metrics).map(([k, v]) =>
          [titleCase(k), Array.isArray(v) ? v.join(', ') : String(v)])))));
  }
}

/* ========================================================== WORKSPACE (pinned context) */
export async function workspace(root) {
  const pins = JSON.parse(localStorage.getItem('vigil.pins') || '[]');
  const rec = await api('/recommend?risk=BALANCED');
  root.append(
    el('div', { class: 'view-head' },
      el('div', {}, el('p', { class: 'meta' }, 'INVESTIGATION WORKSPACE · TEMPORARY CONTEXT'),
        el('h1', { class: 'h1' }, 'What am I working on?'))),
    section('PINNED', 'Temporary investigative context — cleared whenever you like. Not a second dashboard.',
      pins.length ? el('div', { class: 'list' }, pins.map(p => el('a', { class: 'evidence', href: p.route },
        el('span', { class: 'ch' }, p.type), el('div', {}, el('p', { style: 'margin:0' }, p.label)))))
        : el('p', { class: 'faint' }, 'Nothing pinned yet. Use the ✛ button in the top bar to pin the current view.'),
      pins.length ? el('button', { class: 'btn sm ghost', style: 'margin-top:12px',
        onclick: () => { localStorage.removeItem('vigil.pins'); location.reload(); } }, 'Clear workspace') : null),
    el('div', { style: 'margin-top:18px' },
      isBad(rec) ? degraded(rec) : section('RESEARCH SHORTLIST', rec.scoring,
        el('div', { class: 'row', style: 'margin-bottom:12px' },
          ['CONSERVATIVE', 'BALANCED', 'AGGRESSIVE'].map(p => el('button', {
            class: 'chip' + (p === rec.risk_profile ? ' on' : ''),
            onclick: async (e) => {
              const r = await api(`/recommend?risk=${p}`);
              const host = e.target.closest('.card');
              host.querySelector('.shortlist').replaceWith(shortlist(r));
              host.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c.textContent === p));
            } }, p))),
        shortlist(rec),
        el('p', { class: 'faint', style: 'margin:12px 0 0;font-size:12px' }, rec.disclaimer))));
}

function shortlist(rec) {
  if (isBad(rec)) return el('div', { class: 'shortlist' }, degraded(rec));
  return el('div', { class: 'shortlist list' }, rec.candidates.map(c => el('div', { class: 'evidence' },
    el('span', { class: 'ch' }, c.sector),
    el('div', { style: 'flex:1' },
      el('div', { class: 'between' },
        el('a', { href: `#forecast/${c.symbol}` }, el('strong', {}, `${c.name} (${c.symbol})`)),
        el('div', { class: 'row' }, tag(c.status, c.status === 'CANDIDATE' ? 'pos' : 'idle'),
          el('span', { class: 'chip' }, `score ${num(c.score, 1)}`))),
      el('p', { class: 'faint', style: 'margin:6px 0 0;font-size:12.5px' }, c.why)))));
}
