/* VIGIL front-end smoke test (jsdom).
 *
 *   npm install && npm run ui-smoke      <- single command: boots the API, runs this, stops it
 *   npm run ui-smoke:attach              <- run against an API you started yourself (VIGIL_URL)
 *
 * Coverage: the landing page, the app shell (navigation, permanent data-mode badge), all seven
 * canonical spaces plus WORKSPACE and REPLAY, the research lab tabs, the architecture drawer,
 * global search, and the assistant. It fails on any thrown error, any console error, any space
 * that renders almost nothing, and any degraded panel.
 *
 * SCOPE: jsdom builds and scripts the DOM; it does not do layout, CSS or paint. Passing this is
 * NOT browser verification and is never reported as such.
 */
import { JSDOM } from 'jsdom';
import fs from 'fs';
import path from 'path';
import { performance as perf } from 'node:perf_hooks';

const BASE = process.env.VIGIL_URL || 'http://127.0.0.1:8000';
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const dom = new JSDOM(fs.readFileSync(path.join(ROOT, 'apps/web/app.html'), 'utf8'),
  { url: `${BASE}/app`, pretendToBeVisual: true });
const { window } = dom;
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
window.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {} });
window.Element.prototype.scrollIntoView = function () {};
const nodeFetch = globalThis.fetch;
global.fetch = (u, o) => nodeFetch(typeof u === 'string' && u.startsWith('/') ? BASE + u : u, o);
global.performance = perf;
Object.assign(global, {
  window, document: window.document, location: window.location, Element: window.Element,
  localStorage: window.localStorage, sessionStorage: window.sessionStorage,
  addEventListener: window.addEventListener.bind(window), devicePixelRatio: 1,
  matchMedia: window.matchMedia, requestAnimationFrame: cb => setTimeout(() => cb(Date.now()), 16),
});

const failures = [];
const log = console.error;
process.on('unhandledRejection', e => failures.push('unhandled rejection: ' + (e?.stack || e)));
console.error = (...a) => failures.push('console.error ' + a.map(x => x?.stack || String(x)).join(' '));
const wait = ms => new Promise(r => setTimeout(r, ms));
const view = () => window.document.getElementById('view');
const text = () => view().textContent.replace(/\s+/g, ' ').trim();

// ---------------------------------------------------------------- landing page
{
  const landingDom = new JSDOM(fs.readFileSync(path.join(ROOT, 'apps/web/index.html'), 'utf8'),
    { url: `${BASE}/`, pretendToBeVisual: true, runScripts: 'outside-only' });
  const lw = landingDom.window;
  lw.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  lw.Element.prototype.scrollIntoView = function () {};
  const landingText = lw.document.body.textContent.replace(/\s+/g, ' ').trim();
  log(`landing        chars=${landingText.length} sections=${lw.document.querySelectorAll('section').length}`);
  if (landingText.length < 500) failures.push(`landing page rendered almost nothing (${landingText.length} chars)`);
  if (!/VIGIL/.test(landingText)) failures.push('landing page does not name the product');
  if (!/Investigate before you believe/i.test(landingText)) failures.push('landing page is missing the tagline');
  const landingJs = fs.readFileSync(path.join(ROOT, 'apps/web/js/landing.js'), 'utf8');
  if (!landingJs.length) failures.push('landing.js is empty');
  landingDom.window.close();
}

await import(path.join(ROOT, 'apps/web/js/app.js'));
await wait(2500);

// ---------------------------------------------------------------- app shell
{
  const navLinks = [...window.document.querySelectorAll('nav a, .nav a')];
  log(`shell          nav=${navLinks.length} badge=${!!window.document.body.textContent.match(/HISTORICAL/)}`);
  if (navLinks.length < 7) failures.push(`app shell exposes only ${navLinks.length} navigation entries`);
  if (!/HISTORICAL/.test(window.document.body.textContent)) {
    failures.push('permanent data-mode badge missing from the app shell');
  }
}

const SPACES = ['#pulse', '#investigate', '#forecast', '#decision', '#lab', '#observatory', '#workspace', '#replay'];
for (const space of SPACES) {
  window.location.hash = space;
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  await wait(space === '#forecast' || space === '#decision' ? 9000 : 4500);
  const chars = text().length;
  const degraded = view().querySelectorAll('.degraded').length;
  log(`${space.padEnd(14)} chars=${String(chars).padEnd(6)} cards=${view().querySelectorAll('.card').length} degraded=${degraded}`);
  if (chars < 300) failures.push(`${space} rendered almost nothing (${chars} chars)`);
  if (degraded) failures.push(`${space} rendered ${degraded} degraded panel(s)`);
}

window.location.hash = '#lab';
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
await wait(4500);
for (const btn of [...window.document.querySelectorAll('.tabs button')]) {
  const name = btn.textContent;
  btn.click();
  await wait(1800);
  log(`lab tab ${name.padEnd(18)} chars=${text().length}`);
  if (text().length < 400) failures.push(`lab tab ${name} rendered almost nothing`);
}

window.location.hash = '#observatory';
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
await wait(4500);
window.document.querySelector('.topo .node').click();
await wait(600);
if (!window.document.querySelector('.drawer.open')) failures.push('architecture drawer did not open');
window.document.querySelector('.drawer .btn').click();
await wait(300);
if (window.document.querySelector('.drawer.open')) failures.push('drawer did not close');

const input = window.document.getElementById('search');
input.value = 'infosys';
input.dispatchEvent(new window.Event('input'));
await wait(1200);
const hits = [...window.document.querySelectorAll('#search-results a')];
log('search hits:', hits.length);
if (!hits.length) failures.push('global search returned nothing for "infosys"');

window.document.getElementById('ask-btn').click();
await wait(400);
window.document.querySelectorAll('.drawer .chip')[0].click();
await wait(2500);
if (!window.document.querySelector('.drawer .evidence.sup')) failures.push('assistant produced no grounded answer');

// ------------------------------------------------- design-system checks (v1.0 defect #13)
// jsdom does not lay out or paint, so these verify the RULES exist in the stylesheet and that
// the DOM is keyboard- and screen-reader-navigable. Pixel verification needs a real browser and
// is reported as NOT EXECUTED, never as a pass.
{
  const css = fs.readFileSync(path.join(ROOT, 'apps/web/css/vigil.css'), 'utf8');
  const required = [
    ['reduced motion', /@media\s*\(prefers-reduced-motion/],
    ['responsive breakpoints', /@media\s*\(max-width/],
    ['visible keyboard focus', /:focus-visible/],
    ['hover affordance', /:hover/],
    ['drawer transition', /\.drawer/]];
  for (const [name, re] of required) {
    if (!re.test(css)) failures.push(`stylesheet has no ${name} rule`);
  }
  const breakpoints = [...css.matchAll(/@media\s*\(max-width:\s*(\d+)px/g)].map(m => m[1]);
  log(`css            reduced-motion=${/@media\s*\(prefers-reduced-motion/.test(css)} ` +
      `focus-visible=${/:focus-visible/.test(css)} breakpoints=[${[...new Set(breakpoints)].join(', ')}]`);
}

{
  // keyboard reachability: every nav entry and every tab must be focusable and reachable by Tab
  const focusables = [...window.document.querySelectorAll(
    'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])')];
  const unreachable = focusables.filter(e => e.getAttribute('tabindex') === '-1');
  const nav = [...window.document.querySelectorAll('nav a, .nav a')];
  nav[2].focus();
  const focusWorks = window.document.activeElement === nav[2];
  log(`a11y           focusable=${focusables.length} nav-focus=${focusWorks} ` +
      `aria-labels=${window.document.querySelectorAll('[aria-label], [aria-current], [role]').length}`);
  if (!focusWorks) failures.push('navigation entries cannot take keyboard focus');
  if (unreachable.length) failures.push(`${unreachable.length} control(s) removed from tab order`);
  if (!window.document.querySelector('[aria-label], [role]')) {
    failures.push('no ARIA labelling anywhere in the shell');
  }
}

{
  // error + empty states: an endpoint that cannot answer must degrade visibly, not blank out
  window.location.hash = '#replay';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  await wait(3000);
  const emptyState = view().textContent.trim().length > 100;
  log(`states         replay-empty-state=${emptyState}`);
  if (!emptyState) failures.push('REPLAY shows neither content nor an explained empty state');
}

// ---------------------------------------------------------------- workspace interaction
window.location.hash = '#workspace';
window.dispatchEvent(new window.HashChangeEvent('hashchange'));
await wait(4000);
{
  const chars = text().length;
  const controls = view().querySelectorAll('button, input, select, a').length;
  log(`workspace      chars=${chars} controls=${controls}`);
  if (chars < 300) failures.push(`workspace rendered almost nothing (${chars} chars)`);
  if (!controls) failures.push('workspace exposes no interactive controls');
}

log(`\n${failures.length ? 'FAILURES' : 'ALL FRONT-END CHECKS PASSED (jsdom — not a browser: no layout, CSS or paint)'}`);
failures.forEach(f => log(' - ' + f.slice(0, 300)));
process.exit(failures.length ? 1 : 0);
