// HiveBid market — near-workspaces edge-paths: NO_SHOW / SUBMITTED_LATE / VOID_LATE.
// Post-audit semantics: HB-M19 (stranger no_show gate), P12–P14 late work path.
//
// Requirements (verified 2026-11-05):
//   near-sandbox >= protocol 77 — near-sdk 5.30 unconditionally imports
//   gas-key/global-contract host fns; older near-vm fails prepare with
//   `CompilationError::PrepareError::Deserialization` on ANY 5.30 wasm.
//   Env: NEAR_SANDBOX_BIN_PATH + NEAR_SANDBOX_BINARY_PATH = dir containing
//   the near-sandbox binary; HIVEBID_WASM = path to contract wasm.
//   Note: official near-sandbox >=2.1 binaries are x86-64-v3 — SIGILL on
//   pre-AVX2 CPUs; run on modern hardware or CI.
//
// Run: node edge-paths.js   ·   Wall time ≈ 25 s (real block timestamps).
const fs = require('fs');
const path = require('path');
const { Worker, NEAR } = require('near-workspaces');

const WASM = process.env.HIVEBID_WASM
  || path.resolve(__dirname, '..', 'wasm', 'hivebid_market.wasm');
const BUDGET = '50';
const PRICE = '30';
const STAKE = '10';

function assert(cond, msg) {
  if (!cond) throw new Error('ASSERT FAILED: ' + msg);
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function expectFail(promise, what) {
  try { await promise; } catch { return; }
  throw new Error('ASSERT FAILED: expected failure — ' + what);
}
const nowMs = () => Date.now();

// Часы цепи, а не стены: sandbox block_timestamp — реальное время, но
// blind sleep слипается/флачит. Ждём именно прошествие дедлайна по чейну.
let worker;
async function chainNowMs() {
  const b = await worker.provider.block({ finality: 'optimistic' });
  const ts = b.header.timestamp_nanosec ?? b.header.timestamp;
  return Number(BigInt(ts) / 1000000n);
}
async function waitPast(deadlineMs, label) {
  for (let i = 0; i < 200; i++) {
    if ((await chainNowMs()) > deadlineMs) return;
    await sleep(300);
  }
  throw new Error('ASSERT FAILED: chain time never passed ' + label);
}

// Возвращает дедлайны лота — окна сценариев строятся от них.
async function makeLot(market, manager, agent, lotId, opts) {
  const now = nowMs();
  const d = { bid: now + opts.bidIn, wd: now + opts.wdIn, vd: now + opts.vdIn };
  await manager.call(market, 'create_task', {
    lot_id: lotId,
    task_hash: 'T:' + 'b'.repeat(64),
    bid_deadline_ms: d.bid,
    work_deadline_ms: d.wd,
    verify_deadline_ms: d.vd,
  }, { attachedDeposit: NEAR.parse(`${BUDGET} N`) });
  await agent.call(market, 'submit_bid', {
    lot_id: lotId,
    price: NEAR.parse(`${PRICE} N`),
    eta_ms: opts.wdIn,
    signature: 'edge-sig',
  }, {});
  await waitPast(d.bid, 'bid_deadline');
  await manager.call(market, 'close_bidding', { lot_id: lotId }, {});
  await manager.call(market, 'award_best', { lot_id: lotId }, {});
  return d;
}

async function main() {
  worker = await Worker.init({ rm: true });
  try {
    const root = worker.rootAccount;
    const manager = await root.devCreateAccount('manager');
    const alice = await root.devCreateAccount('alice');
    const stranger = await root.devCreateAccount('stranger');
    const market = await root.createSubAccount('market');
    await market.deploy(fs.readFileSync(WASM));
    await market.call(market, 'new', { owner_id: manager.accountId }, {});
    await alice.call(market, 'register_agent', {}, { attachedDeposit: NEAR.parse(`${STAKE} N`) });
    console.log('setup OK (payable register_agent accepted 10 N stake)');

    // ---------- S1: NO_SHOW + HB-M19 gate ----------
    const s1 = await makeLot(market, manager, alice, 'edge-noshow', { bidIn: 1500, wdIn: 4000, vdIn: 16000 });
    await waitPast(s1.wd, 'S1 work_deadline'); // wd < now < vd (vd margin 12 s)

    // HB-M19: stranger in (wd, vd) is REJECTED — P12 window belongs to
    // the manager↔winner race, not to gas-only griefers.
    await expectFail(
      stranger.call(market, 'mark_no_show', { lot_id: 'edge-noshow' }, {}),
      'stranger mark_no_show in (wd, vd) must be rejected (HB-M19)');
    console.log('S1a HB-M19 gate OK — stranger rejected before verify_deadline');

    const stakeBefore = BigInt((await market.view('get_agent', { account: alice.accountId })).stake);
    await manager.call(market, 'mark_no_show', { lot_id: 'edge-noshow' }, {});
    let lot = await market.view('get_lot', { lot_id: 'edge-noshow' });
    assert(lot.status === 'Failed', 'S1 lot should be Failed');
    assert(String(await market.view('get_escrow')) === '0', 'S1 escrow released');
    const rec = await market.view('get_agent', { account: alice.accountId });
    assert(BigInt(rec.stake) === stakeBefore / 2n, 'S1 stake slashed 50%');
    assert(BigInt(await market.view('get_burned')) === stakeBefore / 2n, 'S1 burned == slash');
    assert(BigInt(rec.rating) < 500000n, 'S1 rating dropped below R0');
    console.log('S1 NO_SHOW OK — Failed, escrow 0, stake slashed 50%, burned == slash');

    // ---------- S2: SUBMITTED_LATE → accept_work_late ----------
    const s2 = await makeLot(market, manager, alice, 'edge-late', { bidIn: 1500, wdIn: 4000, vdIn: 24000 });
    // Strict on-time path is closed after wd:
    await waitPast(s2.wd, 'S2 work_deadline');
    await expectFail(
      alice.call(market, 'submit_work', { lot_id: 'edge-late', work_hash: 'W:late' }, {}),
      'submit_work after work_deadline must be rejected');
    await alice.call(market, 'late_submit_work', { lot_id: 'edge-late', work_hash: 'W:late' }, {});
    lot = await market.view('get_lot', { lot_id: 'edge-late' });
    assert(lot.status === 'SubmittedLate', 'S2 lot should be SubmittedLate');
    await manager.call(market, 'accept_work_late', { lot_id: 'edge-late' }, {});
    lot = await market.view('get_lot', { lot_id: 'edge-late' });
    assert(lot.status === 'Completed', 'S2 lot should be Completed');
    console.log('S2 P12–P13 OK — SubmittedLate → Completed via accept_work_late');

    // ---------- S3: SUBMITTED_LATE → void_late crank (anyone after vd) ----------
    const s3 = await makeLot(market, manager, alice, 'edge-void', { bidIn: 1500, wdIn: 4000, vdIn: 14000 });
    await waitPast(s3.wd, 'S3 work_deadline');
    await alice.call(market, 'late_submit_work', { lot_id: 'edge-void', work_hash: 'W:late2' }, {});
    // Manager silent; stranger can only act after verify_deadline:
    await expectFail(
      stranger.call(market, 'void_late', { lot_id: 'edge-void' }, {}),
      'stranger void_late before verify_deadline must be rejected');
    await waitPast(s3.vd, 'S3 verify_deadline'); // now > verify_deadline
    await stranger.call(market, 'void_late', { lot_id: 'edge-void' }, {});
    lot = await market.view('get_lot', { lot_id: 'edge-void' });
    assert(lot.status === 'Failed', 'S3 lot should be Failed');
    assert(String(await market.view('get_escrow')) === '0', 'S3 escrow released');
    console.log('S3 P14 OK — void_late crank by stranger after verify_deadline');

    console.log('EDGE PATHS OK (NO_SHOW + HB-M19 + SUBMITTED_LATE + VOID_LATE)');
  } finally {
    await worker.tearDown();
  }
}

main().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });
