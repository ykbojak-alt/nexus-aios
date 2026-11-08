// HiveBid market — testnet smoke probe (живой NEAR testnet, #359).
//
// Режимы CLI --plan/--run не отправляют транзакции: --plan печатает расписание,
// --run выполняет только read-only вызовы. Отдельная экспортируемая функция
// w31_rework_no_show — owner-gated manual step с внешним callManual-адаптером;
// она не вызывается из main и не содержит собственного кода подписи/отправки.
//
// Все u128 читаются exact-путём: base64 → сырые байты → (borsh | decimal-text).
// BigInt(JSON.parse(...)) запрещён — Number уже сжёг мантиссу (>2^53). Урок #351.
//
// Env:
//   NS, MKT, MGR, AGT          контракты/аккаунты (как в deploy-testnet-nearcli.sh)
//   NEAR_RPC_URL               default https://rpc.testnet.near.org
//   LOT, BUDGET_N, PRICE_N, STAKE_N, BID_IN_MS, WD_IN_MS, VD_IN_MS
//   NEAR_CRED_DIR              default ~/.near-credentials/testnet (только для show, не для подписи)
//   EXPECT_LOT=1               --run: требовать наличие лота LOT
//
// Run:
//   node testnet-smoke.js --plan     # расписание + аргументы, сети не касается
//   node testnet-smoke.js --run      # только views + инварианты
// W31: импортировать w31_rework_no_show и вызывать только вручную с callManual
// и отдельным crank-аккаунтом; требует свежего лота и ожидания дедлайнов.
const fs = require('fs');
const os = require('os');
const path = require('path');

// ------------------------------------------------------------------ config
const env = (k, d) => (process.env[k] !== undefined && process.env[k] !== '' ? process.env[k] : d);
const int = (k, d) => {
  const v = Number(process.env[k]);
  return Number.isSafeInteger(v) && v >= 0 ? v : d;
};

const CFG = {
  ns: env('NS', 'yksunder'),
  rpc: env('NEAR_RPC_URL', 'https://rpc.testnet.near.org'),
  lot: env('LOT', 'demo-1'),
  budgetN: env('BUDGET_N', '50'),
  priceN: env('PRICE_N', '30'),
  stakeN: env('STAKE_N', '10'),
  bidIn: int('BID_IN_MS', 300000),
  wdIn: int('WD_IN_MS', 1800000),
  vdIn: int('VD_IN_MS', 3600000),
  gas: env('HIVEBID_GAS', '30000000000000'),
  credDir: env('NEAR_CRED_DIR', path.join(os.homedir(), '.near-credentials', 'testnet')),
  expectLot: process.env.EXPECT_LOT === '1',
};
CFG.mkt = env('MKT', `hivebid.${CFG.ns}.testnet`);
CFG.mgr = env('MGR', `manager.${CFG.ns}.testnet`);
CFG.agt = env('AGT', `agent.${CFG.ns}.testnet`);

const YOCTO = 10n ** 24n;
const near = (s) => {
  const digits = String(s).trim().replace(/[^\d]/g, ''); // "50" | "50 N" | "50N"
  if (!digits) throw new Error(`near(): пустая сумма "${s}"`);
  return BigInt(digits) * YOCTO;
};
const SCALE = 1000000n; // рейтинг fixed-point 1e6
const R0 = 500000n;
const STATUS_TAG = ['Open', 'BidsClosed', 'Awarded', 'Submitted', 'Rework', 'Completed',
  'PartialSettled', 'Failed', 'Cancelled', 'SubmittedLate'];
const TERMINAL = new Set(['Completed', 'PartialSettled', 'Failed', 'Cancelled']);

// ------------------------------------------------------------------ exact readers
// BigInt-safe JSON: integer-литералы сверх 2^53 вычисляются в строку до parse.
function parseExactJson(text) {
  let out = '', i = 0, inStr = false;
  while (i < text.length) {
    const c = text[i];
    if (inStr) {
      out += c;
      if (c === '\\' && i + 1 < text.length) { out += text[i + 1]; i += 2; continue; }
      if (c === '"') inStr = false;
      i++; continue;
    }
    if (c === '"') { inStr = true; out += c; i++; continue; }
    const m = /^(?:-?\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(i));
    if (m && /^-?\d+$/.test(m[0]) && !/^0\d/.test(m[0]) &&
        m[0].replace('-', '').length > 15) {
      out += '"' + m[0] + '"'; i += m[0].length; continue;
    }
    out += c; i++;
  }
  return JSON.parse(out);
}

function big(v, what) {
  if (typeof v === 'bigint') return v;
  if (typeof v === 'string' && /^\d+$/.test(v)) return BigInt(v);
  if (typeof v === 'number' && Number.isSafeInteger(v)) return BigInt(v);
  if (v === null || v === undefined) return 0n; // Option<u128>=None / отсутствие записи
  throw new Error(`not exact u128 for ${what}: ${JSON.stringify(String(v))}`);
}

// Минимальный borsh-ридер (раскладка — src/lib.rs:63-200).
function borsh(buf) {
  let p = 0;
  const need = (n) => { if (p + n > buf.length) throw new Error('borsh short read'); };
  const u8 = () => { need(1); return buf[p++]; };
  const u32 = () => { need(4); let v = 0; for (let i = 3; i >= 0; i--) v = v * 256 + buf[p + i]; p += 4; return v; };
  const uint = (b) => { need(b); let v = 0n; for (let i = b - 1; i >= 0; i--) v = (v << 8n) | BigInt(buf[p + i]); p += b; return v; };
  const str = () => { const n = u32(); need(n); const s = buf.subarray(p, p + n).toString('utf8'); p += n; return s; };
  const opt = (rd) => (u8() === 1 ? rd() : null);
  const vec = (rd) => { const n = u32(); return Array.from({ length: n }, rd); };
  const self = {
    tell: () => p, done: () => p === buf.length,
    u8, u16: () => uint(2), u64: () => uint(8), u128: () => uint(16), str, opt, vec,
    bid: () => ({ agent: str(), price: uint(16), eta_ms: uint(8), ts_ms: uint(8), signature: str() }),
    payout: () => ({ executor: uint(16), manager: uint(16), fee: uint(16) }),
    submission: () => ({ work_hash: str(), attempt: u8(), ts_ms: uint(8) }),
    ratingEvent: () => ({ term: str(), q_scaled: uint(16), s_scaled: uint(16), q_final_scaled: uint(16), ts_ms: uint(8) }),
    status: () => {
      const t = u8();
      const name = STATUS_TAG[t];
      if (!name) throw new Error('unknown LotStatus tag ' + t);
      return name === 'Rework' ? `Rework(${u8()})` : name;
    },
    lot: () => ({
      lot_id: str(), manager: str(), task_hash: str(), budget: uint(16),
      bid_deadline_ms: uint(8), work_deadline_ms: uint(8), verify_deadline_ms: uint(8),
      status: self.status(), bids: vec(self.bid), winner: opt(str), winning_price: uint(16),
      payout: opt(self.payout), submission: opt(self.submission),
      rework_count: u8(), opened_ts_ms: uint(8), award_ts_ms: opt(() => uint(8)), settle_ts_ms: opt(() => uint(8)),
    }),
    agent: () => ({
      account: str(), stake: uint(16), tasks_completed: uint(8), tasks_failed: uint(8),
      rating: uint(16), sum_q: uint(16), n: uint(8), ledger: vec(self.ratingEvent),
    }),
  };
  return self;
}
const decodeLot = (buf) => { const r = borsh(buf); const lot = r.lot(); return { lot, consumed: r.tell(), exact: r.done() }; };
const decodeAgent = (buf) => { const r = borsh(buf); const agent = r.agent(); return { agent, consumed: r.tell(), exact: r.done() }; };
const decodeU128 = (buf) => borsh(buf).u128();

// serde: unit-варианты → "Awarded", Rework(u8) → {"Rework":3}. Нормализуем к строке.
const normStatus = (s) => (typeof s === 'string' ? s
  : s && typeof s === 'object' && s.Rework !== undefined ? `Rework(${s.Rework})` : String(s));
const baseStatus = (s) => normStatus(s).split('(')[0];

// borsh-ключ = u32 len + utf8 (AccountId == String).
const borshStr = (s) => {
  const b = Buffer.from(s, 'utf8');
  const len = Buffer.alloc(4);
  len.writeUInt32LE(b.length);
  return Buffer.concat([len, b]);
};

// ------------------------------------------------------------------ RPC (read-only)
let rpcSeq = 0;
async function rpc(method, params) {
  const res = await fetch(CFG.rpc, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: `smoke-${++rpcSeq}`, method, params }),
  });
  if (!res.ok) throw new Error(`RPC HTTP ${res.status} ${res.statusText}`);
  const j = await res.json();
  if (j.error) throw new Error(`RPC ${method}: ${j.error.message ?? JSON.stringify(j.error)}`);
  return j.result;
}

// exact view: base64 сырых байт возврата → BigInt (скаляр) либо BigInt-safe JSON.
async function viewExact(name, args, kind) {
  const r = await rpc('query', {
    request_type: 'call_function', account_id: CFG.mkt, method_name: name,
    args_base64: Buffer.from(JSON.stringify(args), 'utf8').toString('base64'),
    finality: env('FINALITY', 'final'),
  });
  const buf = Buffer.from(r.result ?? '', 'base64');
  const text = buf.toString('utf8');
  if (buf.length === 0) throw new Error(`${name}: пустой возврат — на ${CFG.mkt} нет контракта/метода (деплой не случился?)`);
  if (kind === 'u128') {
    if (!/^\d+$/.test(text.trim())) throw new Error(`${name}: не скалярный u128 — "${text.slice(0, 60)}"`);
    return BigInt(text.trim());
  }
  if (kind === 'optU128') return text.trim() === 'null' ? null : BigInt(text.trim());
  const v = parseExactJson(text);
  return kind === 'optLot' ? (v === null ? null : v) : v;
}

async function rawStorage() {
  const r = await rpc('query', {
    request_type: 'view_contract_state', account_id: CFG.mkt, finality: env('FINALITY', 'final'),
  });
  const map = new Map();
  for (const { key, value } of r.values ?? []) {
    map.set(Buffer.from(key, 'base64').toString('latin1'), Buffer.from(value, 'base64'));
  }
  return map;
}
// store::IterableMap/LookupMap: key = prefix-байт ++ borsh(K)
const storageKey = (prefix, keyStr) => Buffer.from(prefix, 'latin1').toString('latin1')
  + borshStr(keyStr).toString('latin1');

// ------------------------------------------------------------------ проверка инвариантов
const fails = [];
const checks = [];
const warnings = [];
function check(ok, id, detail) {
  checks.push({ id, ok, detail });
  if (!ok) fails.push(`${id}: ${detail}`);
  console.log(`${ok ? '  OK  ' : ' FAIL '} ${id} — ${detail}`);
}
function warn(id, detail) {
  warnings.push(`${id}: ${detail}`);
  console.warn(`  WARN ${id} — ${detail}`);
}

function exactUnsignedInteger(value, field) {
  if (typeof value === 'bigint' && value >= 0n) return value;
  if (typeof value === 'string' && /^\d+$/.test(value)) return BigInt(value);
  if (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) return BigInt(value);
  throw new Error(`${field}: ожидалось точное неотрицательное целое`);
}

function parseBidScoreRows(rows) {
  if (!Array.isArray(rows)) throw new Error('ответ не является массивом');
  const parsed = rows.map((row, i) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) {
      throw new Error(`элемент ${i}: ожидался объект BidScore`);
    }
    if (typeof row.agent !== 'string' || !row.agent) {
      throw new Error(`элемент ${i}: отсутствует agent`);
    }
    const score = {
      agent: row.agent,
      price: exactUnsignedInteger(row.price, `элемент ${i}.price`),
      eta_ms: exactUnsignedInteger(row.eta_ms, `элемент ${i}.eta_ms`),
      f_price: exactUnsignedInteger(row.f_price, `элемент ${i}.f_price`),
      f_rating: exactUnsignedInteger(row.f_rating, `элемент ${i}.f_rating`),
      f_eta: exactUnsignedInteger(row.f_eta, `элемент ${i}.f_eta`),
      score: exactUnsignedInteger(row.score, `элемент ${i}.score`),
    };
    if (score.eta_ms > (1n << 64n) - 1n) throw new Error(`элемент ${i}.eta_ms превышает u64`);
    return score;
  });
  if (new Set(parsed.map((row) => row.agent)).size !== parsed.length) {
    throw new Error('массив содержит повторный agent');
  }
  return parsed;
}

function isBidScoresUnavailable(error) {
  const message = String(error?.message ?? error).toLowerCase();
  return /methodnotfound|method not found|unknown method|no such method|method .*does not exist/.test(message)
    || (message.includes('пустой возврат') && message.includes('нет контракта/метода'));
}

function isMissingLotError(error) {
  const message = String(error?.message ?? error).toLowerCase();
  return /no such lot|lot not found|lot_id.*(?:unknown|not found)|несуществующ.*лот|лот.*не найден/.test(message);
}

// ------------------------------------------------------------------ план (без сети)
function writeSchedule() {
  const now = Date.now();
  const bid = now + CFG.bidIn, wd = now + CFG.wdIn, vd = now + CFG.vdIn;
  const taskHash = 'T:' + 'a'.repeat(64);
  const price = near(CFG.priceN);
  return [
    { i: 1, signer: CFG.mkt, method: 'new', args: { owner_id: CFG.mgr }, deposit: '0', note: 'deploy-фаза (уже в deploy-testnet-nearcli.sh)' },
    { i: 2, signer: CFG.agt, method: 'register_agent', args: {}, deposit: `${CFG.stakeN} N`, note: 'stake = deposit, ≥ min_stake' },
    { i: 3, signer: CFG.mgr, method: 'create_task', args: { lot_id: CFG.lot, task_hash: taskHash, bid_deadline_ms: bid, work_deadline_ms: wd, verify_deadline_ms: vd }, deposit: `${CFG.budgetN} N`, note: 'эскроу = бюджет' },
    { i: 4, signer: CFG.agt, method: 'submit_bid', args: `{"lot_id":"${CFG.lot}","price":${price},"eta_ms":${CFG.wdIn},"signature":"demo"}`, deposit: '0', note: `price = ${price} yocto raw-integer u128 (строка → deser unreachable) ≤ бюджет` },
    { i: 5, signer: null, method: '⏱ ждать bid_deadline', args: { until_ms: bid }, deposit: '0', note: `${CFG.bidIn} мс отnow` },
    { i: 6, signer: CFG.mgr, method: 'close_bidding', args: { lot_id: CFG.lot }, deposit: '0', note: 'bids заморожены' },
    { i: 7, signer: CFG.mgr, method: 'award_best', args: { lot_id: CFG.lot }, deposit: '0', note: 'AWARDED + bond резерв (HB-S18)' },
    { i: 8, signer: CFG.agt, method: 'submit_work', args: { lot_id: CFG.lot, work_hash: 'W:demo' }, deposit: '0', note: 'до work_deadline' },
    { i: 9, signer: CFG.mgr, method: 'accept_work', args: { lot_id: CFG.lot }, deposit: '0', note: 'COMPLETED + payout' },
  ];
}

function viewSchedule() {
  return [
    { method: 'get_escrow', args: {}, kind: 'u128', why: 'сумма живых бюджетов' },
    { method: 'get_treasury', args: {}, kind: 'u128', why: 'комиссии протокола' },
    { method: 'get_burned', args: {}, kind: 'u128', why: 'сожжённые слэши' },
    { method: 'get_total_staked', args: {}, kind: 'u128', why: 'Σ stake агентов' },
    { method: 'get_lot_ids', args: {}, kind: 'obj(list)', why: 'перечисление лотов' },
    { method: 'get_lot', args: { lot_id: CFG.lot }, kind: 'optLot', why: 'лот целиком (u128 budget/price/payout)' },
    { method: 'get_agent_ids', args: {}, kind: 'obj(list)', why: 'перечисление агентов' },
    { method: 'get_agent', args: { account: CFG.agt }, kind: 'obj', why: 'stake/rating/sum_q (u128)' },
    { method: 'get_rating', args: { account: CFG.agt }, kind: 'u128', why: 'R scaled' },
    { method: 'get_rating_eff', args: { account: CFG.agt }, kind: 'u128', why: 'R_eff scaled' },
    { method: 'view_account', args: {}, kind: 'balance', why: 'консервация баланса контракта' },
    { method: 'view_contract_state', args: {}, kind: 'raw', why: 'raw-байты storage → borsh cross-check' },
  ];
}

function printPlan() {
  console.log('=== testnet-smoke — РЕЖИМ --plan (только расписание, сеть не трогается) ===\n');
  console.log('config (env):');
  console.log(`  RPC      ${CFG.rpc}`);
  console.log(`  MKT      ${CFG.mkt}`);
  console.log(`  MGR      ${CFG.mgr}`);
  console.log(`  AGT      ${CFG.agt}`);
  console.log(`  LOT      ${CFG.lot}   gas ${CFG.gas}`);
  console.log(`  amounts  stake=${CFG.stakeN}N(${near(CFG.stakeN)} y) budget=${CFG.budgetN}N(${near(CFG.budgetN)} y) price=${CFG.priceN}N(${near(CFG.priceN)} y)`);
  console.log(`  creds    ${CFG.credDir} (read-only показ; подпись/отправка НЕ реализованы)\n`);
  for (const a of [CFG.mkt, CFG.mgr, CFG.agt]) {
    const f = path.join(CFG.credDir, `${a}.json`);
    let kind = 'нет файла';
    try { const j = JSON.parse(fs.readFileSync(f, 'utf8')); kind = `${fs.statSync(f).size}B ${j.key_id ?? ''}`.trim(); } catch { /* отсутствие — норма до деплоя */ }
    console.log(`  credential ${a.padEnd(34)} ${kind}`);
  }

  console.log('\n--- A. write-flow (справочное расписание; CLI-режимы его не исполняют) ---');
  for (const s of writeSchedule()) {
    console.log(` ${String(s.i).padStart(2)}. ${s.method}${s.signer ? `  signer=${s.signer}` : ''}  dep=${s.deposit}`);
    console.log(`     args   ${JSON.stringify(s.args)}`);
    console.log(`     note   ${s.note}`);
  }

  console.log('\n--- B. read-only probes, которые делает --run ---');
  for (const v of viewSchedule()) {
    console.log(` - ${v.method.padEnd(21)} ${JSON.stringify(v.args).padEnd(34)} → ${v.kind.padEnd(8)} // ${v.why}`);
  }

  console.log('\n--- C. инварианты (exact u128) ---');
  for (const t of [
    'I-A escrow == Σ budget живых лотов',
    'I-B get_total_staked == Σ get_agent.stake',
    'I-C balance >= escrow + treasury + burned + Σ stake (delta = faucet/storage — сообщается)',
    'I-D per агент: stake >= agent_bonds; agent_bonds == Σ lot_bonds по живым лотам',
    'I-E payout.executor + manager + fee == budget; winning_price == bid победителя',
    'I-F rating в [0, 1e6]; новый агент == R0 = 500000',
    'I-G borsh(raw storage) == JSON-view (кросс-проверка exact-чтения, байт в байт)',
  ]) console.log(' - ' + t);
  console.log('\nПЛАН ПОСТРОЕН. В режиме --plan транзакции не отправлялись.');
}

// ------------------------------------------------------------------ run (read-only)
async function run() {
  console.log(`=== testnet-smoke — --run, только views. RPC ${CFG.rpc}, MKT ${CFG.mkt} ===`);
  const escrow = await viewExact('get_escrow', {}, 'u128');
  const treasury = await viewExact('get_treasury', {}, 'u128');
  const burned = await viewExact('get_burned', {}, 'u128');
  const totalStaked = await viewExact('get_total_staked', {}, 'u128');
  const acc = await rpc('query', { request_type: 'view_account', account_id: CFG.mkt, finality: env('FINALITY', 'final') });
  const balance = BigInt(acc.amount); // RPC отдаёт строку — точная
  console.log(`views: escrow=${escrow} treasury=${treasury} burned=${burned} total_staked=${totalStaked} balance=${balance}`);

  const lotIds = (await viewExact('get_lot_ids', {}, 'obj')) ?? [];
  const agentIds = (await viewExact('get_agent_ids', {}, 'obj')) ?? [];
  console.log(`lots(${lotIds.length}): ${lotIds.join(', ') || '—'}`);
  console.log(`agents(${agentIds.length}): ${agentIds.join(', ') || '—'}`);

  // SCORES-VERIFY: только read-only; старый деплой без метода предупреждаем.
  const scoreLotId = 'smk-5-1794136566619';
  let scoresAvailable = false;
  let rawScores;
  try {
    rawScores = await viewExact('get_bid_scores', { lot_id: scoreLotId }, 'obj');
    scoresAvailable = true;
  } catch (e) {
    const detail = e?.message ?? String(e);
    if (isBidScoresUnavailable(e)) {
      warn('SCORES_METHOD', `get_bid_scores недоступен (возможно старый деплой): ${detail}`);
    } else {
      check(false, 'SCORES_METHOD', detail);
    }
  }

  if (scoresAvailable) {
    let bidScores = null;
    let shapeError = '';
    try { bidScores = parseBidScoreRows(rawScores); } catch (e) { shapeError = e.message; }
    check(bidScores !== null, 'SCORES_SHAPE', bidScores ? `${bidScores.length} элементов BidScore` : shapeError);
    if (bidScores !== null) {
      check(bidScores.length > 0, 'SCORES_KNOWN_LOT', `${scoreLotId}: bids=${bidScores.length}`);
      const inRange = bidScores.every((row) => row.price > 0n
        && row.f_price <= SCALE && row.f_rating <= SCALE && row.f_eta <= SCALE
        && row.score <= SCALE);
      check(inRange, 'SCORES_RANGE', 'price > 0; факторы и score в [0, 1e6]');

      if (bidScores.length > 0) {
        try {
          const scoredLot = await viewExact('get_lot', { lot_id: scoreLotId }, 'optLot');
          if (!scoredLot) {
            check(false, 'SCORES_LOT', `${scoreLotId} отсутствует в get_lot`);
          } else {
            const lotBids = Array.isArray(scoredLot.bids) ? scoredLot.bids : null;
            const sameBids = lotBids !== null && lotBids.length === bidScores.length
              && new Set(lotBids.map((bid) => bid.agent)).size === lotBids.length
              && lotBids.every((bid) => {
                const score = bidScores.find((row) => row.agent === bid.agent);
                return score !== undefined
                  && score.price === exactUnsignedInteger(bid.price, 'get_lot bid.price')
                  && score.eta_ms === exactUnsignedInteger(bid.eta_ms, 'get_lot bid.eta_ms');
              });
            check(sameBids, 'SCORES_BIDS_MATCH',
              `get_lot bids=${lotBids?.length ?? 'malformed'}; get_bid_scores=${bidScores.length}`);
            if (sameBids && lotBids.length > 1) {
              if (typeof scoredLot.winner !== 'string' || !scoredLot.winner) {
                warn('SCORES_WINNER', `${scoreLotId} имеет несколько bids, но ещё не выбран winner`);
              } else {
                const winnerScore = bidScores.find((row) => row.agent === scoredLot.winner);
                const maxScore = bidScores.reduce((max, row) => row.score > max ? row.score : max, 0n);
                check(winnerScore !== undefined && winnerScore.score === maxScore,
                  'SCORES_WINNER_MAX',
                  `winner=${scoredLot.winner} score=${winnerScore?.score ?? 'нет'} max=${maxScore}`);
              }
            } else if (sameBids) {
              check(true, 'SCORES_WINNER_MAX', 'не применяется: меньше двух bids');
            }
          }
        } catch (e) {
          check(false, 'SCORES_LOT', e?.message ?? String(e));
        }
      }
    }

    const missingLotId = `__codex_w15_missing_${Date.now()}__`;
    try {
      const missingScores = await viewExact('get_bid_scores', { lot_id: missingLotId }, 'obj');
      check(Array.isArray(missingScores) && missingScores.length === 0, 'SCORES_MISSING_LOT',
        `фактический ответ для отсутствующего lot_id: ${Array.isArray(missingScores) ? `[] (${missingScores.length})` : typeof missingScores}`);
    } catch (e) {
      const detail = e?.message ?? String(e);
      check(isMissingLotError(e), 'SCORES_MISSING_LOT', `ошибка для отсутствующего lot_id: ${detail}`);
    }
  }

  const storage = await rawStorage();
  const lots = [];
  for (const id of lotIds) {
    const j = await viewExact('get_lot', { lot_id: id }, 'optLot');
    if (!j) continue;
    lots.push({ id, j });
    const raw = storage.get(storageKey('l', id));
    if (raw) {
      const { lot, exact } = decodeLot(raw);
      check(exact && lot.budget === big(j.budget, 'budget') && lot.status === normStatus(j.status)
        && lot.winning_price === big(j.winning_price, 'winning_price'),
        `I-G lot ${id}`, `borsh(budget=${lot.budget}, status=${lot.status}) == JSON(${j.budget}, ${normStatus(j.status)}), exact-consume=${exact}`);
    } else check(false, `I-G lot ${id}`, 'нет ключа storage l++borsh(lot_id)');
  }

  // I-A escrow == Σ бюджетов живых лотов
  let liveBudget = 0n, liveLots = 0;
  for (const { j } of lots) {
    if (!TERMINAL.has(baseStatus(j.status))) { liveBudget += big(j.budget, 'budget'); liveLots++; }
  }
  check(liveBudget === escrow, 'I-A escrow==Σlive budgets', `escrow=${escrow} Σ=${liveBudget} (живых лотов ${liveLots})`);

  // I-B total_staked == Σ stake
  const agents = [];
  let sumStake = 0n;
  for (const id of agentIds) {
    const rec = await viewExact('get_agent', { account: id }, 'obj');
    if (!rec) { check(false, `I-B agent ${id}`, 'get_agent_ids вернул id без записи'); continue; }
    agents.push({ id, rec });
    sumStake += big(rec.stake, 'stake');
    const raw = storage.get(storageKey('a', id));
    if (raw) {
      const { agent, exact } = decodeAgent(raw);
      check(exact && agent.stake === big(rec.stake, 'stake') && agent.rating === big(rec.rating, 'rating'),
        `I-G agent ${id}`, `borsh(stake=${agent.stake}, rating=${agent.rating}) == JSON(${rec.stake},${rec.rating})`);
    }
  }
  check(sumStake === totalStaked, 'I-B total_staked==Σ stake', `onchain=${totalStaked} recomputed=${sumStake}`);

  // I-C консервация
  const buckets = escrow + treasury + burned + totalStaked;
  check(balance >= buckets, 'I-C conservation', `balance=${balance} buckets=${buckets} delta=${balance - buckets} (faucet float − storage)`);

  // I-D bonds: агентские и полотные
  const bondsByAgent = new Map();
  let sumLotBonds = 0n;
  for (const { id, j } of lots) {
    const raw = storage.get(storageKey('b', id));
    if (!raw) continue;
    const bond = decodeU128(raw);
    sumLotBonds += bond;
    const term = TERMINAL.has(baseStatus(j.status));
    if (!term && j.winner) bondsByAgent.set(j.winner, (bondsByAgent.get(j.winner) ?? 0n) + bond);
    if (term && bond === 0n) check(false, `I-D bond ${id}`, 'терминал, но bond не снят');
  }
  for (const { id, rec } of agents) {
    const raw = storage.get(storageKey('r', id));
    const abond = raw ? decodeU128(raw) : 0n;
    const expect = bondsByAgent.get(id) ?? 0n;
    check(abond === expect, `I-D agent_bonds ${id}`, `r-prefix=${abond} Σ lot_bonds(живые)=${expect}`);
    check(big(rec.stake, 'stake') >= abond, `I-D stake>=bond ${id}`, `stake=${rec.stake} bonded=${abond}`);
  }
  check(sumLotBonds <= totalStaked, 'I-D Σ bonds <= total_staked', `lot_bonds=${sumLotBonds} total_staked=${totalStaked}`);

  // I-E payout / winner price
  for (const { id, j } of lots) {
    if (!j.payout) continue;
    const sum = big(j.payout.executor, 'executor') + big(j.payout.manager, 'manager') + big(j.payout.fee, 'fee');
    check(sum === big(j.budget, 'budget'), `I-E payout ${id}`, `exec+mgr+fee=${sum} == budget=${j.budget}`);
    const bid = (j.bids ?? []).find((b) => b.agent === j.winner);
    if (bid) check(big(bid.price, 'price') === big(j.winning_price, 'wp'), `I-E winning_price ${id}`, `${j.winning_price} == bid ${bid.price}`);
  }

  // I-F рейтинг
  for (const { id, rec } of agents) {
    const r = big(rec.rating, 'rating');
    check(r >= 0n && r <= SCALE, `I-F rating ${id}`, `R=${r} в [0,1e6]`);
    if (big(rec.n, 'n') === 0n) check(r === R0, `I-F fresh R0 ${id}`, `n=0 → R=${r}, R0=${R0}`);
  }

  if (CFG.expectLot) {
    const j = await viewExact('get_lot', { lot_id: CFG.lot }, 'optLot');
    check(!!j, 'EXPECT_LOT', `лот ${CFG.lot} ${j ? 'присутствует' : 'отсутствует'}`);
  }

  console.log(`\n=== ИТОГ: ${checks.length - fails.length}/${checks.length} проверок OK ===`);
  if (warnings.length) console.log(`ПРЕДУПРЕЖДЕНИЙ: ${warnings.length} (smoke не завершён ошибкой только из-за них).`);
  if (fails.length) { console.log('ПРОВАЛЕНО:'); for (const f of fails) console.log('  - ' + f); return 1; }
  console.log('БЕЗ ТРАНЗАКЦИЙ — выполнены только query/view.');
  return 0;
}

// Ручной шаг W31: нужен новый лот, настроенные testnet-аккаунты и явный
// owner-provided transaction adapter. --plan/--run его не вызывают.
// callManual({ contract, signer, method, args: Buffer, depositNear, gas, networkId })
// отправляет одну транзакцию и отклоняет вызов при ошибке. Эта функция сама
// не подписывает и не отправляет транзакции.
async function w31_rework_no_show({ callManual, crank, lotId = `w31-${Date.now()}` } = {}) {
  if (typeof callManual !== 'function') throw new Error('W31: нужен явный callManual-адаптер владельца');
  if (typeof crank !== 'string' || !crank.trim() || crank === CFG.mgr || crank === CFG.agt) {
    throw new Error('W31: crank должен быть отдельным аккаунтом, не manager и не winner');
  }
  if (!lotId || lotId.length > 64) throw new Error('W31: lotId должен содержать 1..64 символа');

  const args = (value) => Buffer.from(JSON.stringify(value), 'utf8');
  const send = (signer, method, value, depositNear = '0') => callManual({
    contract: CFG.mkt, signer, method, args: Buffer.isBuffer(value) ? value : args(value),
    depositNear, gas: CFG.gas, networkId: 'testnet',
  });
  const assertW31 = (condition, label, detail) => {
    if (!condition) throw new Error(`W31 ASSERT FAIL ${label}: ${detail}`);
    console.log(`  OK W31 ${label} — ${detail}`);
  };
  const waitUntil = async (targetMs, label) => {
    while (Date.now() < targetMs) {
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000, targetMs - Date.now())));
    }
    console.log(`  дедлайн пройден: ${label}, запас 15 с`);
  };

  const existingLot = await viewExact('get_lot', { lot_id: lotId }, 'optLot');
  if (existingLot) throw new Error(`W31: lotId уже существует: ${lotId}`);
  const agentBefore = await viewExact('get_agent', { account: CFG.agt }, 'obj');
  if (!agentBefore) throw new Error(`W31: winner не зарегистрирован: ${CFG.agt}`);
  const stakeBefore = big(agentBefore.stake, 'W31 stake before');
  const burnedBefore = await viewExact('get_burned', {}, 'u128');

  const startedAt = Date.now();
  const bidDeadline = startedAt + 60_000;
  const workDeadline = startedAt + 170_000;
  const verifyDeadline = startedAt + 400_000;
  const price = near(CFG.priceN);
  const budget = near(CFG.budgetN);
  assertW31(stakeBefore > 0n, 'у зарегистрированного победителя есть стейк', `${CFG.agt} stake=${stakeBefore}`);
  assertW31(price > 0n && price <= budget, 'ставка в пределах бюджета', `price=${price} budget=${budget} yocto`);

  await send(CFG.mgr, 'create_task', {
    lot_id: lotId, task_hash: `T:${'a'.repeat(64)}`,
    bid_deadline_ms: bidDeadline, work_deadline_ms: workDeadline, verify_deadline_ms: verifyDeadline,
  }, CFG.budgetN);

  // price остаётся JSON-целым без кавычек: JSON.stringify(BigInt) запрещён,
  // строка вместо u128 приводит к wasm unreachable (причина W30).
  const bidArgs = Buffer.from(
    `{"lot_id":${JSON.stringify(lotId)},"price":${price},"eta_ms":${workDeadline - 1},"signature":"w31-manual"}`,
    'utf8',
  );
  await send(CFG.agt, 'submit_bid', bidArgs);
  await waitUntil(bidDeadline + 15_000, 'bid_deadline');
  await send(CFG.mgr, 'close_bidding', { lot_id: lotId });
  await send(CFG.mgr, 'award_lot', { lot_id: lotId, winner: CFG.agt });
  await send(CFG.agt, 'submit_work', { lot_id: lotId, work_hash: 'W:w31-rework-no-show' });
  await send(CFG.mgr, 'reject_work', { lot_id: lotId });

  const rework = await viewExact('get_lot', { lot_id: lotId }, 'optLot');
  if (!rework) throw new Error(`W31: лот не найден после reject_work: ${lotId}`);
  assertW31(normStatus(rework.status) === 'Rework(1)', 'reject_work устанавливает Rework(1)', normStatus(rework.status));
  assertW31(Number(rework.rework_count) === 1, 'rework_count равен единице', String(rework.rework_count));

  await waitUntil(workDeadline + 15_000, 'work_deadline');
  await send(crank, 'mark_no_show', { lot_id: lotId });

  const [failed, agentAfter, burnedAfter] = await Promise.all([
    viewExact('get_lot', { lot_id: lotId }, 'optLot'),
    viewExact('get_agent', { account: CFG.agt }, 'obj'),
    viewExact('get_burned', {}, 'u128'),
  ]);
  if (!failed || !agentAfter) throw new Error('W31: отсутствует итоговый get_lot/get_agent ответ');
  assertW31(normStatus(failed.status) === 'Failed', 'итоговый статус', normStatus(failed.status));
  assertW31(big(agentAfter.stake, 'W31 stake after') === stakeBefore, 'стейк победителя не изменился', String(agentAfter.stake));
  assertW31(burnedAfter === burnedBefore, 'get_burned не изменился', `${burnedBefore} → ${burnedAfter}`);
  assertW31(Number(failed.rework_count) === 1, 'итоговый rework_count равен единице', String(failed.rework_count));
  return { lotId, bidDeadline, workDeadline, verifyDeadline, stakeBefore: String(stakeBefore), burnedBefore: String(burnedBefore) };
}

// ------------------------------------------------------------------ main
if (require.main === module) (async () => {
  const mode = process.argv[2];
  if (mode === '--plan') { printPlan(); process.exit(0); }
  if (mode === '--run') { try { process.exit(await run()); } catch (e) { console.error('RUN FAIL:', e.message); process.exit(1); } }
  console.error('usage: node testnet-smoke.js --plan | --run   (оба CLI-режима не отправляют транзакции)');
  process.exit(2);
})();

module.exports = { parseExactJson, borsh, decodeLot, decodeAgent, decodeU128, big, storageKey, borshStr, near, normStatus, w31_rework_no_show };
