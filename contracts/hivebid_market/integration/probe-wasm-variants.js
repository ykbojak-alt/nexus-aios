// Probe: deploy each wasm artifact, call `new`, report Prepare status.
const fs = require('fs');
const path = require('path');
const { Worker, NEAR } = require('near-workspaces');
const DIR = '/root/nexus/contracts/hivebid_market/wasm';
const FILES = ['hivebid_market.wasm', 'hivebid_market.d7.wasm', 'canonical-hivebid_market.wasm'];

(async () => {
  const worker = await Worker.init({ rm: true });
  try {
    const root = worker.rootAccount;
    const mgr = await root.devCreateAccount('manager');
    for (const f of FILES) {
      const p = path.join(DIR, f);
      if (!fs.existsSync(p)) { console.log(f, 'MISSING'); continue; }
      const buf = fs.readFileSync(p);
      const acc = await root.createSubAccount(f.replace(/\W+/g, '-').slice(0, 40));
      try {
        await acc.deploy(buf);
        const out = await acc.call(acc, 'new', { owner_id: mgr.accountId }, {});
        console.log(f, buf.length + 'B', '-> new OK');
      } catch (e) {
        const m = String(e.message || e);
        const kind = m.includes('Deserialization') ? 'Deserialization'
          : m.includes('FunctionCallError') ? 'FunctionCallError'
          : m.includes('CompilationError') ? 'CompilationError'
          : m.slice(0, 80);
        console.log(f, buf.length + 'B', '-> FAIL', kind);
      }
    }
  } finally {
    await worker.tearDown();
  }
  process.exit(0);
})();
