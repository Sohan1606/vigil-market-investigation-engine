/* Landing choreography: entrance reveals + an art-directed market signal field.
   The field is a quiet lattice of instruments: nodes drift, correlated pairs breathe,
   and a slow "investigation sweep" passes through the structure. Not a force graph. */
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

const io = new IntersectionObserver((entries) => {
  entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
}, { threshold: 0.18 });
document.querySelectorAll('.reveal').forEach(el => io.observe(el));
if (reduced) document.querySelectorAll('.reveal').forEach(el => el.classList.add('in'));

const canvas = document.getElementById('field');
if (canvas && !reduced) {
  const ctx = canvas.getContext('2d');
  let w, h, dpr, nodes = [], links = [], t = 0;
  const CLUSTERS = [
    { x: .22, y: .34, n: 6 }, { x: .52, y: .62, n: 6 },
    { x: .78, y: .30, n: 5 }, { x: .40, y: .22, n: 4 }, { x: .68, y: .74, n: 4 }
  ];
  function build() {
    dpr = Math.min(devicePixelRatio || 1, 2);
    w = canvas.width = canvas.clientWidth * dpr;
    h = canvas.height = canvas.clientHeight * dpr;
    nodes = [];
    CLUSTERS.forEach((c, ci) => {
      for (let i = 0; i < c.n; i++) {
        const a = (i / c.n) * Math.PI * 2 + ci;
        const r = (28 + Math.random() * 62) * dpr;
        nodes.push({
          cx: c.x * w, cy: c.y * h, a, r, cluster: ci,
          sp: 0.00008 + Math.random() * 0.00022,
          m: 0.6 + Math.random() * 1.5, ph: Math.random() * Math.PI * 2
        });
      }
    });
    links = [];
    for (let i = 0; i < nodes.length; i++)
      for (let j = i + 1; j < nodes.length; j++)
        if (nodes[i].cluster === nodes[j].cluster || Math.random() < 0.035) links.push([i, j]);
  }
  function pos(n, time) {
    const a = n.a + time * n.sp * 1000;
    return [n.cx + Math.cos(a) * n.r, n.cy + Math.sin(a) * n.r * 0.72];
  }
  function frame(time) {
    t = time;
    ctx.clearRect(0, 0, w, h);
    const sweep = ((time * 0.00006) % 1.4) - 0.2;          // investigation sweep
    const pts = nodes.map(n => pos(n, time));
    ctx.lineWidth = 1 * dpr;
    links.forEach(([i, j]) => {
      const [x1, y1] = pts[i], [x2, y2] = pts[j];
      const d = Math.hypot(x2 - x1, y2 - y1);
      if (d > 260 * dpr) return;
      const mid = ((x1 + x2) / 2) / w;
      const near = Math.max(0, 1 - Math.abs(mid - sweep) * 7);
      ctx.strokeStyle = `rgba(150,140,255,${0.04 + 0.22 * near})`;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    });
    pts.forEach(([x, y], i) => {
      const n = nodes[i];
      const pulse = 0.5 + 0.5 * Math.sin(time * 0.0012 + n.ph);
      const near = Math.max(0, 1 - Math.abs(x / w - sweep) * 7);
      const r = (1.3 + n.m * 0.9 + pulse * 0.7 + near * 1.6) * dpr;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${210 + 40 * near},${205 + 30 * near},255,${0.3 + 0.45 * near})`;
      ctx.fill();
      if (near > 0.72) {
        ctx.beginPath(); ctx.arc(x, y, r * 3.4, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(110,91,255,${0.22 * near})`; ctx.stroke();
      }
    });
    requestAnimationFrame(frame);
  }
  build(); requestAnimationFrame(frame);
  addEventListener('resize', build, { passive: true });
}

/* Chapter art: small, meaningful diagrams drawn as inline SVG (no external assets). */
const art = {
  'art-observe': `
    <svg viewBox="0 0 420 220" class="chart" role="img" aria-label="Event stream passing through bounded-memory sketches">
      <defs><linearGradient id="g1" x1="0" x2="1"><stop offset="0" stop-color="#6E5BFF" stop-opacity=".0"/><stop offset=".5" stop-color="#A692FF" stop-opacity=".9"/><stop offset="1" stop-color="#6E5BFF" stop-opacity="0"/></linearGradient></defs>
      <line x1="10" y1="110" x2="410" y2="110" stroke="url(#g1)" stroke-width="2"/>
      ${Array.from({length: 26}, (_, i) => `<circle cx="${18 + i * 15}" cy="110" r="${2 + (i % 5 === 0 ? 2.4 : 0)}" fill="${i % 5 === 0 ? '#A692FF' : 'rgba(255,255,255,.35)'}"/>`).join('')}
      <rect x="120" y="54" width="80" height="34" rx="8" fill="rgba(110,91,255,.12)" stroke="rgba(166,146,255,.5)"/>
      <text x="160" y="76" text-anchor="middle" class="tick" fill="#A692FF">BLOOM</text>
      <rect x="215" y="132" width="80" height="34" rx="8" fill="rgba(110,91,255,.12)" stroke="rgba(166,146,255,.5)"/>
      <text x="255" y="154" text-anchor="middle" class="tick" fill="#A692FF">DGIM</text>
      <rect x="300" y="54" width="92" height="34" rx="8" fill="rgba(110,91,255,.12)" stroke="rgba(166,146,255,.5)"/>
      <text x="346" y="76" text-anchor="middle" class="tick" fill="#A692FF">FLAJOLET–MARTIN</text>
    </svg>`,
  'art-investigate': `
    <svg viewBox="0 0 420 220" class="chart" role="img" aria-label="Evidence ledger with supporting and contradicting channels">
      ${['PRICE','VOLUME','SECTOR','BREADTH','NEWS'].map((c, i) => {
        const sup = [1,1,1,0,-1][i]; const y = 30 + i * 36;
        const col = sup > 0 ? '#3FB27F' : sup < 0 ? '#5A5F70' : '#E0616A';
        const wdt = sup === 0 ? 150 : 90 + i * 34;
        return `<g><text x="14" y="${y+4}" class="tick">${c}</text>
          <rect x="96" y="${y - 8}" width="${wdt}" height="12" rx="6" fill="${col}" opacity="${sup === 0 ? .25 : .55}"/>
          <text x="${96 + wdt + 10}" y="${y+4}" class="tick">${sup > 0 ? 'SUPPORTS' : sup < 0 ? 'UNKNOWN' : 'CONTRADICTS'}</text></g>`;
      }).join('')}
    </svg>`,
  'art-forecast': `
    <svg viewBox="0 0 420 220" class="chart" role="img" aria-label="Forecast trajectory with an uncertainty band">
      <defs><linearGradient id="vg2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6E5BFF" stop-opacity=".35"/><stop offset="1" stop-color="#6E5BFF" stop-opacity="0"/></linearGradient></defs>
      <path d="M10,150 C70,140 90,120 140,124 C190,128 210,96 260,104 C310,112 340,86 410,80" class="series"/>
      <path d="M10,150 C70,140 90,120 140,124 C190,128 210,96 260,104 C310,112 340,86 410,80 L410,180 C340,170 310,186 260,176 C210,166 190,186 140,182 C90,178 70,186 10,186 Z" fill="url(#vg2)"/>
      <line x1="10" y1="186" x2="410" y2="186" class="axis"/>
      <text x="10" y="205" class="tick">PROBABILITY TRAJECTORY · 80% CONFORMAL BAND</text>
    </svg>`
};
Object.entries(art).forEach(([id, svg]) => { const el = document.getElementById(id); if (el) el.innerHTML = svg; });
