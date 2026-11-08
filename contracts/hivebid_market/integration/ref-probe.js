const fs = require('fs');
const { Worker } = require('near-workspaces');

async function main() {
  const worker = await Worker.init({ rm: true });
  try {
    const root = worker.rootAccount;
    const acc = await root.createSubAccount('probe');
    await acc.deploy(fs.readFileSync('/root/e2e-sandbox/attested_mint.wasm'));
    await acc.call(acc, 'new', { owner_id: root.accountId }, {});
    const supply = await acc.view('nft_total_supply');
    console.log('attested-mint OK, supply =', supply);
  } finally {
    await worker.tearDown();
  }
}
main().then(() => process.exit(0)).catch(e => { console.error(e); process.exit(1); });
