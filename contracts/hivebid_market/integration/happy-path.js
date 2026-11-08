// HiveBid market — near-workspaces happy-path: bid → escrow → execute → payout.
// Run against local NEAR sandbox: NEAR_SANDBOX_BIN=/path/to/near-sandbox
// WASM path: HIVEBID_WASM env var, else canonical artifact ../wasm/hivebid_market.wasm
const fs = require('fs');
const path = require('path');
const { Worker, NEAR } = require('near-workspaces');

const WASM = process.env.HIVEBID_WASM
  || path.resolve(__dirname, '..', 'wasm', 'hivebid_market.wasm');
const BUDGET = '100';
const PRICE = '40';

function assert(cond, msg) {
  if (!cond) throw new Error('ASSERT FAILED: ' + msg);
}

async function main() {
  const worker = await Worker.init({ rm: true });

  try {
    const root = worker.rootAccount;
    const manager = await root.devCreateAccount('manager');
    const alice = await root.devCreateAccount('alice');

    // Deploy the wasm contract under a subaccount and init.
    const market = await root.createSubAccount('market');
    await market.deploy(fs.readFileSync(WASM));
    await market.call(market, 'new', { owner_id: manager.accountId }, {});
    console.log('deployed + initialized market contract');

    // 1. register_agent (stake 10N)
    await alice.call(market, 'register_agent', {}, { attachedDeposit: NEAR.parse('10 N') });
    let rec = await market.view('get_agent', { account: alice.accountId });
    assert(rec && String(rec.stake) === NEAR.parse('10 N'), 'agent stake should be 10N');
    console.log('register_agent OK, stake =', rec.stake);

    // 2. create_task (escrow deposit = budget)
    const now_ms = Date.now();
    const lotId = 'integration-lot-1';
    const taskHash = 'T:' + 'a'.repeat(64);
    await manager.call(market, 'create_task', {
      lot_id: lotId,
      task_hash: taskHash,
      bid_deadline_ms: now_ms + 2000,
      work_deadline_ms: now_ms + 60000,
      verify_deadline_ms: now_ms + 120000,
    }, { attachedDeposit: NEAR.parse(`${BUDGET} N`) });

    assert(String(await market.view('get_escrow')) === NEAR.parse(`${BUDGET} N`),
      'escrow should hold budget after create_task');

    // 3. submit_bid (цена 40N ≤ бюджет 100N)
    await alice.call(market, 'submit_bid', {
      lot_id: lotId,
      price: NEAR.parse(`${PRICE} N`),
      eta_ms: 30000,
      signature: 'demo-signature',
    }, {});

    // 4. close_bidding (после bid_deadline)
    await new Promise(r => setTimeout(r, 2500));
    await manager.call(market, 'close_bidding', { lot_id: lotId }, {});

    const winner = await market.view('select_winner', { lot_id: lotId });
    assert(winner === alice.accountId, 'select_winner should be alice');

    // 5. award_best (аналитик → AWARDED, payout зафиксирован)
    const awarded = await manager.call(market, 'award_best', { lot_id: lotId }, {});
    assert(awarded === alice.accountId, 'award_best should return alice');
    let lot = await market.view('get_lot', { lot_id: lotId });
    assert(lot.status === 'Awarded', 'lot should be Awarded, got ' + lot.status);
    assert(lot.winner === alice.accountId, 'winner should be alice');
    assert(String(lot.payout.executor) === NEAR.parse(`${PRICE} N`), 'payout.executor = 40N');
    console.log('award_best OK, payout =', JSON.stringify(lot.payout));

    // 6. submit_work (winner)
    await alice.call(market, 'submit_work', { lot_id: lotId, work_hash: 'W:deadbeef' }, {});

    // 7. accept_work (manager → COMPLETED, payout выполнен)
    await manager.call(market, 'accept_work', { lot_id: lotId }, {});

    lot = await market.view('get_lot', { lot_id: lotId });
    assert(lot.status === 'Completed', 'lot should be Completed, got ' + lot.status);

    // Консервация: эскроу опустошено
    assert(String(await market.view('get_escrow')) === '0', 'escrow should be 0 after settle');

    // Рейтинг победителя вырос (R0 = 0.5 scaled = 500000)
    const r = BigInt(await market.view('get_rating', { account: alice.accountId }));
    assert(r > 500000n, 'rating should rise above 500000, got ' + r);

    console.log('HAPPY PATH OK');
    console.log('  status =', lot.status);
    console.log('  escrow =', await market.view('get_escrow'));
    console.log('  rating(alice) =', r);
  } finally {
    await worker.tearDown();
  }
}

main().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });
