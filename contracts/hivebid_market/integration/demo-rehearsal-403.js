// HIVEBID MARKET — полная e2e-репетиция демо (#403), judge-friendly.
// Local near-sandbox ТОЛЬКО — ничего не уходит на testnet.
//
// Сценарий (2 лота, общие дедлайны по chain-time):
//   SETUP   sandbox + accounts + deploy/new + register_agent ×2
//   LOTS    create_task lot-a (100N) + lot-b (80N) → escrow 180N
//   BIDS    alice→lot-a 40N ; alice→lot-b 45N + bob→lot-b 35N (select_winner)
//   CLOSE   waitPast(bid_deadline) → close_bidding ×2 → award_best ×2
//   HAPPY   lot-a: submit_work → accept_work → COMPLETED, payout 40/60, fee 0
//   REWORK  lot-b: submit_work → reject_work (Rework(1)) → resubmit (attempt 2)
//           → accept_work → COMPLETED, payout 35/45
//   FINAL   views: escrow=0, treasury/burned, ratings ledger, lot statuses
//
// Run (на sage-pl, AVX2+):
//   cd /root/nexus/contracts/hivebid_market/integration
//   HIVEBID_WASM=../wasm/hivebid_market.wasm \
//   NEAR_SANDBOX_BIN_PATH=/root/sandbox-neard \
//   NEAR_SANDBOX_BINARY_PATH=/root/sandbox-neard/near-sandbox \
//   NEAR_SANDBOX_BIN=/root/sandbox-neard/near-sandbox \
//   node demo-rehearsal-403.js
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { Worker, NEAR } = require('near-workspaces');

const WASM = process.env.HIVEBID_WASM
  || path.resolve(__dirname, '..', 'wasm', 'hivebid_market.wasm');

const sleep = ms => new Promise(r => setTimeout(r, ms));
const assert = (c, m) => { if (!c) throw new Error('ASSERT FAILED: ' + m); };

let stepN = 0, stepTotal = 0;
async function step(name, fn) {
  stepN++;
  const t0 = Date.now();
  process.stdout.write(`  [${String(stepN).padStart(2, '0')}/${stepTotal}] ${name} ... `);
  try {
    const out = await fn();
    console.log(`OK (${Date.now() - t0}ms)`);
    return out;
  } catch (e) {
    console.log(`FAIL — ${e.message}`);
    throw e;
  }
}
const view = (m, args) => market.view(m, args);
let market; // assigned in main

async function chainNowMs(worker) {
  const b = await worker.provider.block({ finality: 'optimistic' });
  const ts = b.header.timestamp_nanosec ?? b.header.timestamp;
  return Number(BigInt(ts) / 1000000n);
}
async function waitPast(worker, deadlineMs, label) {
  const t0 = Date.now();
  for (let i = 0; i < 2400; i++) {           // до ~10 мин chain-time
    if ((await chainNowMs(worker)) > deadlineMs) {
      console.log(`      chain passed ${label} (+${Date.now() - t0}ms wall)`);
      return;
    }
    await sleep(250);
  }
  throw new Error('ASSERT FAILED: chain never passed ' + label);
}
// u128-arg (price) идёт bare-числом в JSON — near-sdk парсит u128 из
// integer-литерала; строка "4000…" падает в deserialization→unreachable.
// call() сериализует BN→string, поэтому submit_bid — только через callRaw
// + ручная проверка outcome (callRaw НЕ бросает на Failure).
const submitBid = async (acct, lot, priceNEAR, etaMs, sig) => {
  const args = `{"lot_id":${JSON.stringify(lot)},"price":${yocto(priceNEAR)},"eta_ms":${etaMs},"signature":${JSON.stringify(sig)}}`;
  const out = await acct.callRaw(market.accountId, 'submit_bid', Buffer.from(args), {});
  const st = JSON.stringify(out && out.status !== undefined ? out.status : out);
  if (!st.includes('Success')) throw new Error(`submit_bid ${lot} failed: ${st.slice(0, 300)}`);
  return out;
};
const taskHash = tag => 'T:' + crypto.createHash('sha256').update(tag).digest('hex');
const N = x => NEAR.parse(`${x} N`).toString();          // "x N" -> yocto decimal string
const yocto = x => NEAR.parse(`${x} N`).toString();
// u128 views приходят как f64-экспонента ("1.8e+26") — сравниваем численно.
const num = v => Number(v);
const isYocto = (v, x) => num(v) === num(NEAR.parse(`${x} N`));

async function main() {
  const wasmBuf = fs.readFileSync(WASM);
  const wasmMd5 = crypto.createHash('md5').update(wasmBuf).digest('hex').slice(0, 12);
  console.log('='.repeat(64));
  console.log(' HIVEBID MARKET — DEMO REHEARSAL (#403)  [LOCAL SANDBOX ONLY]');
  console.log(` wasm=${path.basename(WASM)} ${wasmBuf.length}B md5:${wasmMd5}`);
  console.log('='.repeat(64));

  const worker = await Worker.init({ rm: true });
  try {
    const root = worker.rootAccount;
    stepTotal = 30;

    // ---------- SETUP ----------
    const MGR = await step('sandbox worker + root account', async () => root);
    const manager = await step('account manager.demo', () => root.devCreateAccount('manager'));
    const alice = await step('account alice.demo (agent)', () => root.devCreateAccount('alice'));
    const bob = await step('account bob.demo (agent)', () => root.devCreateAccount('bob'));
    market = await step('subaccount market.demo', () => root.createSubAccount('market'));
    await step('fund accounts (mgr 10000N, agents 3000N ea)', async () => {
      await root.transfer(manager.accountId, NEAR.parse('10000 N'));
      await root.transfer(alice.accountId, NEAR.parse('3000 N'));
      await root.transfer(bob.accountId, NEAR.parse('3000 N'));
    });
    await step(`deploy wasm + new(owner=${manager.accountId})`, async () => {
      await market.deploy(wasmBuf);
      await market.call(market, 'new', { owner_id: manager.accountId }, {});
    });
    await step('register_agent alice 10N', () =>
      alice.call(market, 'register_agent', {}, { attachedDeposit: NEAR.parse('10 N') }));
    await step('register_agent bob 10N', () =>
      bob.call(market, 'register_agent', {}, { attachedDeposit: NEAR.parse('10 N') }));

    // ---------- LOTS ----------
    const now = await chainNowMs(worker);
    const BID = now + 20000;       // bid-окно 20s — запас на 3 bid-tx (~6s) + close
    const WD = now + 300000;       // work deadline +5min (rework-цикл влезает)
    const VD = now + 600000;       // verify deadline +10min
    console.log(`      chain t=${now}  bid_deadline=+20s  work=+5m  verify=+10m`);

    await step('create_task lot-a budget=100N (manager)', () =>
      manager.call(market, 'create_task', {
        lot_id: 'lot-a', task_hash: taskHash('lot-a spec'),
        bid_deadline_ms: BID, work_deadline_ms: WD, verify_deadline_ms: VD,
      }, { attachedDeposit: NEAR.parse('100 N') }));
    await step('create_task lot-b budget=80N (manager)', () =>
      manager.call(market, 'create_task', {
        lot_id: 'lot-b', task_hash: taskHash('lot-b spec'),
        bid_deadline_ms: BID, work_deadline_ms: WD, verify_deadline_ms: VD,
      }, { attachedDeposit: NEAR.parse('80 N') }));
    await step('view get_escrow == 180N', async () => {
      const e = await view('get_escrow');
      assert(isYocto(e, 180), `escrow=${e}`);
    });

    // ---------- BIDS ----------
    // eta_ms обязан быть < work_deadline (HB-S15) — WD-1s.
    const ETA = WD - 1000;
    await step('submit_bid alice→lot-a price=40N', () =>
      submitBid(alice, 'lot-a', 40, ETA, 'orch-sig-a1'));
    await step('submit_bid alice→lot-b price=45N (дороже bob)', () =>
      submitBid(alice, 'lot-b', 45, ETA, 'orch-sig-b1'));
    await step('submit_bid bob→lot-b price=35N (дешевле → winner)', () =>
      submitBid(bob, 'lot-b', 35, ETA, 'orch-sig-b2'));

    // ---------- CLOSE + AWARD ----------
    await step('waitPast(bid_deadline) — deadline-aware, no blind sleep', () =>
      waitPast(worker, BID + 500, 'bid_deadline'));
    await step('close_bidding lot-a + lot-b', async () => {
      await manager.call(market, 'close_bidding', { lot_id: 'lot-a' });
      await manager.call(market, 'close_bidding', { lot_id: 'lot-b' });
    });
    await step('view select_winner: lot-a→alice, lot-b→bob', async () => {
      const wa = await view('select_winner', { lot_id: 'lot-a' });
      const wb = await view('select_winner', { lot_id: 'lot-b' });
      const la = await view('get_lot', { lot_id: 'lot-a' });
      const lb = await view('get_lot', { lot_id: 'lot-b' });
      console.log(`        lot-a winner=${wa} status=${la.status} bids=${la.bids?.length}`);
      console.log(`        lot-b winner=${wb} status=${lb.status} bids=${lb.bids?.length}`);
      if (la.bids?.length) console.log(`        lot-a bid0=${JSON.stringify(la.bids[0]).slice(0, 200)}`);
      assert(wa === alice.accountId, `lot-a winner=${wa} want=${alice.accountId}`);
      assert(wb === bob.accountId, `lot-b winner=${wb} want=${bob.accountId}`);
    });
    await step('award_best lot-a → alice', async () => {
      assert(await manager.call(market, 'award_best', { lot_id: 'lot-a' }) === alice.accountId);
      const l = await view('get_lot', { lot_id: 'lot-a' });
      assert(l.status === 'Awarded' && l.winner === alice.accountId, 'lot-a not Awarded');
      console.log(`        payout=${JSON.stringify(l.payout)}`);
    });
    await step('award_best lot-b → bob (price-compare сработал)', async () => {
      assert(await manager.call(market, 'award_best', { lot_id: 'lot-b' }) === bob.accountId);
      const l = await view('get_lot', { lot_id: 'lot-b' });
      assert(l.status === 'Awarded' && l.winner === bob.accountId, 'lot-b not Awarded');
      console.log(`        payout=${JSON.stringify(l.payout)}`);
    });

    // ---------- HAPPY PATH: lot-a ----------
    await step('lot-a: alice submit_work W:aaa (attempt 1)', () =>
      alice.call(market, 'submit_work', { lot_id: 'lot-a', work_hash: 'W:aaa-deliverable' }));
    await step('lot-a: view Submitted attempt=1', async () => {
      const l = await view('get_lot', { lot_id: 'lot-a' });
      assert(l.status === 'Submitted' && l.submission.attempt === 1, 'lot-a not Submitted');
    });
    await step('lot-a: manager accept_work → COMPLETED', async () => {
      await manager.call(market, 'accept_work', { lot_id: 'lot-a' });
      const l = await view('get_lot', { lot_id: 'lot-a' });
      assert(l.status === 'Completed', 'lot-a not Completed');
      assert(isYocto(l.payout.executor, 40) && isYocto(l.payout.manager, 60) && num(l.payout.fee) === 0,
        `payout=${JSON.stringify(l.payout)}`);
      console.log(`        payout executor=40N manager=60N fee=0`);
    });
    await step('lot-a: escrow=80N (lot-b ещё жив), rating alice > R0', async () => {
      const e = await view('get_escrow');
      assert(isYocto(e, 80), `escrow after lot-a=${e}`);
      const r = num(await view('get_rating', { account: alice.accountId }));
      assert(r > 500000, `rating=${r}`);
      console.log(`        rating(alice)=${r}  ledger=${JSON.stringify((await view('get_ledger', { account: alice.accountId })).map(e => e.term))}`);
    });

    // ---------- REWORK BRANCH: lot-b ----------
    await step('lot-b: bob submit_work W:bbb-v1', () =>
      bob.call(market, 'submit_work', { lot_id: 'lot-b', work_hash: 'W:bbb-v1' }));
    await step('lot-b: manager reject_work → Rework(1), submission cleared', async () => {
      await manager.call(market, 'reject_work', { lot_id: 'lot-b' });
      const l = await view('get_lot', { lot_id: 'lot-b' });
      // LotStatus::Rework(k) — tuple-вариант → сериализуется {"Rework":k}.
      assert(l.status && l.status.Rework === 1 && l.rework_count === 1,
        `status=${JSON.stringify(l.status)} rework_count=${l.rework_count}`);
      assert(l.submission === null || l.submission === undefined, 'submission not cleared');
      console.log(`        status=${JSON.stringify(l.status)} rework_count=${l.rework_count}`);
    });
    await step('lot-b: bob resubmit W:bbb-v2 (attempt 2)', async () => {
      await bob.call(market, 'submit_work', { lot_id: 'lot-b', work_hash: 'W:bbb-v2-fixed' });
      const l = await view('get_lot', { lot_id: 'lot-b' });
      assert(l.status === 'Submitted' && l.submission.attempt === 2, 'attempt!=2');
      console.log(`        attempt=${l.submission.attempt}`);
    });
    await step('lot-b: manager accept_work → COMPLETED payout 35/45', async () => {
      await manager.call(market, 'accept_work', { lot_id: 'lot-b' });
      const l = await view('get_lot', { lot_id: 'lot-b' });
      assert(l.status === 'Completed', 'lot-b not Completed');
      assert(isYocto(l.payout.executor, 35) && isYocto(l.payout.manager, 45),
        `payout=${JSON.stringify(l.payout)}`);
      console.log(`        payout executor=35N manager=45N`);
    });

    // ---------- FINAL ----------
    await step('FINAL views: escrow=0, lots Completed ×2', async () => {
      const e = await view('get_escrow');
      assert(num(e) === 0, 'escrow!=0');
      const la = await view('get_lot', { lot_id: 'lot-a' });
      const lb = await view('get_lot', { lot_id: 'lot-b' });
      assert(la.status === 'Completed' && lb.status === 'Completed', 'lots not terminal');
      console.log(`        escrow=0  treasury=${await view('get_treasury')}  burned=${await view('get_burned')}`);
      console.log(`        lot-a=${la.status}  lot-b=${lb.status}`);
    });
    await step('FINAL ratings: alice +1 COMPLETED, bob +1 COMPLETED', async () => {
      const la = await view('get_ledger', { account: alice.accountId });
      const lb = await view('get_ledger', { account: bob.accountId });
      assert(la.some(e => e.term === 'COMPLETED') && lb.some(e => e.term === 'COMPLETED'), 'ledger');
      console.log(`        rating(alice)=${await view('get_rating', { account: alice.accountId })}  rating(bob)=${await view('get_rating', { account: bob.accountId })}`);
    });

    console.log('='.repeat(64));
    console.log(' DEMO REHEARSAL #403: PASS — create→bid→award→work→accept');
    console.log('   + reject/rework/resubmit ветка подтверждена on-chain (sandbox)');
    console.log('='.repeat(64));
  } finally {
    await worker.tearDown();
  }
}

main().then(() => process.exit(0)).catch(e => { console.error('FATAL', e); process.exit(1); });
