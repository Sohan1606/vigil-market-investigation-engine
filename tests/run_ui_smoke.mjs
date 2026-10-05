/* Single-command VIGIL front-end smoke test.
 *
 *     npm install        # once: installs jsdom
 *     npm run ui-smoke   # starts the API, renders the whole app, stops the API
 *
 * The runner boots `uvicorn vigil.api.app:app` on a free port, waits for /api/health, runs
 * tests/ui_smoke.mjs against it and shuts the server down again. Use `npm run ui-smoke:attach`
 * with VIGIL_URL=... to test an API you started yourself.
 *
 * SCOPE: this is jsdom, not a browser. It verifies DOM construction, routing, data binding,
 * interaction handlers and the absence of console errors. It does NOT execute layout, CSS or
 * paint, so it is never reported as browser verification.
 */
import { spawn } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';
import net from 'node:net';

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');

const freePort = () => new Promise((resolve, reject) => {
  const srv = net.createServer();
  srv.on('error', reject);
  srv.listen(0, '127.0.0.1', () => {
    const { port } = srv.address();
    srv.close(() => resolve(port));
  });
});

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitForHealth(base, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${base}/api/health`);
      if (res.ok) return true;
    } catch { /* not up yet */ }
    await sleep(500);
  }
  return false;
}

const python = process.env.PYTHON || 'python3';
const port = process.env.VIGIL_PORT || await freePort();
const base = `http://127.0.0.1:${port}`;

console.log(`[ui-smoke] starting API: ${python} -m uvicorn vigil.api.app:app --port ${port}`);
const server = spawn(python, ['-m', 'uvicorn', 'vigil.api.app:app', '--host', '127.0.0.1',
                              '--port', String(port), '--log-level', 'warning'],
                     { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
let serverLog = '';
server.stdout.on('data', d => { serverLog += d; });
server.stderr.on('data', d => { serverLog += d; });

const stop = () => { try { server.kill('SIGTERM'); } catch { /* already gone */ } };
process.on('exit', stop);
process.on('SIGINT', () => { stop(); process.exit(130); });

if (!await waitForHealth(base)) {
  console.error('[ui-smoke] API did not become healthy. Server output:\n' + serverLog.slice(-2000));
  stop();
  process.exit(1);
}
console.log(`[ui-smoke] API healthy at ${base}`);

const smoke = spawn(process.execPath, [path.join(ROOT, 'tests', 'ui_smoke.mjs')],
                    { cwd: ROOT, stdio: 'inherit', env: { ...process.env, VIGIL_URL: base } });
const code = await new Promise(resolve => smoke.on('close', resolve));
stop();
await sleep(300);
console.log(`[ui-smoke] finished with exit code ${code}`);
process.exit(code);
