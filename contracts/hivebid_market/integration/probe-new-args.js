// Probe: which `new` arg-shape does the deployed wasm accept?
const fs = require('fs');
const path = require('path');
const { Worker, NEAR } = require('near-workspaces');
const WASM = process.env.HIVEBID_WASM
  || path.resolve(__dirname, '..', 'wasm', 'hivebid_market.wasm');

async function tryNew(label, argsObj, rawJson) {
  const worker = await Worker.init({ rm: true });
  try {
    const root = worker.rootAccount;
    const mgr = await root.devCreateAccount('manager');
    const market = await root.createSubAccount('market');
    await market.deploy(fs.readFileSync(WASM));
    let out;
    if (rawJson !== undefined) {
      out = await market.callRaw(market.accountId, 'new', Buffer.from(rawJson), {});
    } else {
      out = await market.call(market, 'new', argsObj, {});
    }
    const st = out && out.status ? JSON.stringify(out.status).slice(0, 120) : 'ok?';
    console.log(`${label}: CALL-RETURNED ${st}`);
  } catch (e) {
    const m = String(e.message || e).match(/PrepareError[^}]*}|FunctionCallError[^}]*}|Success/g);
    console.log(`${label}: FAIL ${m ? m[0].slice(0, 140) : String(e).slice(0, 140)}`);
  } finally {
    await worker.tearDown();
  }
}

(async () => {
  console.log('wasm =', WASM);
  await tryNew('A {owner_id:mgr}', { owner_id: 'placeholder' });
  await tryNew('B {} empty', {});
  await tryNew('C raw {"owner_id":"x.test.near"}', undefined, '{"owner_id":"x.test.near"}');
  await tryNew('D raw {}', undefined, '{}');
  process.exit(0);
})();
