//! HiveBid Market contract — аукцион задач между роями + эскроу + выплаты
//! + on-chain рейтинг (по `docs/ROY_MARKET_PROTOCOL.md`).
//!
//! Сводит к минимуму расхождения с протоколом, которые были в контракте-
//! аналоге (`nexus_escrow`), и закрывает ловушку «Submitted», G-3/G-4/G-12:
//! - эскроу вносится реальным депозитом при `create_task` (G-1 → вариант a);
//! - три раздельных дедлайна: bid / work / verify (G-2);
//! - `reject_work`/`settle_partial` дают заказчику путь кроме «заплатить»
//!   (G-3); состояние `REWORK(k)`, k ≤ 3 (G-4);
//! - слэш стейка — только за `NO_SHOW`, только долей, вызвать может manager
//!   (G-12); само-release по `verify_deadline_ms` (G-6);
//! - рейтинг — ledger + аддитивное правило B (6.4), on-chain.
//!
//! `award_best` выбирает победителя детерминированно по взвешенному счёту
//! S = w_price·f_price + w_rating·f_rating + w_eta·f_eta (5) на замороженном
//! множестве bids (`BIDS_CLOSED`).

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::store::{IterableMap, LookupMap};
use near_sdk::{env, near, AccountId, NearToken, PanicOnDefault, Promise};
use serde::{Deserialize, Serialize};

/// Масштаб фиксированной точки для рейтинга/счёта (1e6).
const SCALE: u128 = 1_000_000;
const HALF_SCALE: u128 = 500_000;

/// Псевдонаблюдения холодного старта (k_0 = 3).
const K0: u128 = 3;
const R0: u128 = HALF_SCALE; // 0.5

/// δ_up / δ_dn (вариант B, 6.4).
const DELTA_UP: u128 = 100_000; // 0.10
const DELTA_DN: u128 = 200_000; // 0.20

/// λ = 0.25 за каждый rework-раунд (6.1).
const LAMBDA: u128 = 250_000; // 0.25

/// Пол скорости (6.3).
const S_MIN: u128 = HALF_SCALE; // 0.5

/// τ0 = 0.5 (6.2).
const TAU0: u128 = HALF_SCALE;

/// Веса аналитика (5): цена/рейтинг/срок.
const W_PRICE: u128 = 400_000; // 0.4
const W_RATING: u128 = 400_000; // 0.4
const W_ETA: u128 = 200_000; // 0.2

/// Максимум переделок (spec:15).
const MAX_REWORK: u8 = 3;

/// HB-M17: верхняя граница ставок на лот (gas-DoS через неограниченный
/// bids — award/рerescoring упирался бы в лимит газа).
const MAX_BIDS: usize = 64;

/// HB-S10: верхняя граница рейтинг-ledger'а на агента — кольцевой буфер;
/// иначе запись растёт бесплатно для агента за счёт storage контракта.
const MAX_LEDGER_EVENTS: usize = 512;

/// Доля стейка, сгорающая при NO_SHOW (G-12; штраф, не весь стейк).
const NO_SHOW_SLASH_BPS: u16 = 5000; // 50%

#[derive(BorshDeserialize, BorshSerialize, Clone, Copy, PartialEq, Debug, Serialize, Deserialize)]
pub enum LotStatus {
    Open,
    BidsClosed,
    Awarded,
    Submitted,
    Rework(u8),
    Completed,
    PartialSettled,
    Failed,
    Cancelled,
    /// P12: сдача после work_deadline — ждёт решения менеджера
    /// (accept_work_late / void_late), не терминал.
    SubmittedLate,
}

#[cfg(test)]
impl LotStatus {
    fn is_terminal(self) -> bool {
        matches!(self, LotStatus::Completed | LotStatus::PartialSettled
                | LotStatus::Failed | LotStatus::Cancelled)
    }
    fn is_live(self) -> bool {
        !self.is_terminal()
    }
}

#[derive(BorshDeserialize, BorshSerialize, Clone, Serialize, Deserialize)]
pub struct Bid {
    pub agent: AccountId,
    /// Цена в yoctoNEAR.
    pub price: u128,
    /// Обещанный срок (ms) — для f_eta.
    pub eta_ms: u64,
    pub ts_ms: u64,
    /// Подпись оркестратора (внечейн-атрибуция для аудита). HB-M10:
    /// on-chain верификация сознательно НЕ реализована — ставка и так
    /// привязана к predecessor_account_id (подделать чужую нельзя);
    /// поле хранится для корреляции с внечейн-реестром оркестратора.
    pub signature: String,
}

/// Декомпозиция score для read-only демо-панели (все факторы и score scaled до SCALE).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct BidScore {
    pub agent: AccountId,
    pub price: u128,
    pub eta_ms: u64,
    pub f_price: u128,
    pub f_rating: u128,
    pub f_eta: u128,
    pub score: u128,
}

#[derive(BorshDeserialize, BorshSerialize, Clone, Serialize, Deserialize)]
pub struct Submission {
    pub work_hash: String,
    pub attempt: u8,
    pub ts_ms: u64,
}

/// Вектор выплат (все суммы — yoctoNEAR). Сумма executor + manager == budget
/// при fee = 0; при fee > 0 остаток уходит в казну контракта (treasury).
#[derive(BorshDeserialize, BorshSerialize, Clone, Serialize, Deserialize)]
pub struct Payout {
    pub executor: u128,
    pub manager: u128,
    pub fee: u128,
}

#[derive(BorshDeserialize, BorshSerialize, Clone, Serialize, Deserialize)]
pub struct Lot {
    pub lot_id: String,
    pub manager: AccountId,
    /// sha256(task) → "T:<64hex>".
    pub task_hash: String,
    pub budget: u128,
    pub bid_deadline_ms: u64,
    pub work_deadline_ms: u64,
    pub verify_deadline_ms: u64,
    pub status: LotStatus,
    pub bids: Vec<Bid>,
    pub winner: Option<AccountId>,
    pub winning_price: u128,
    pub payout: Option<Payout>,
    pub submission: Option<Submission>,
    pub rework_count: u8,
    pub opened_ts_ms: u64,
    pub award_ts_ms: Option<u64>,
    pub settle_ts_ms: Option<u64>,
}

#[cfg(test)]
impl Lot {
    fn is_terminal(&self) -> bool {
        self.status.is_terminal()
    }
    fn is_live(&self) -> bool {
        self.status.is_live()
    }
}

#[derive(BorshDeserialize, BorshSerialize, Clone, Serialize, Deserialize)]
pub struct RatingEvent {
    pub term: String, // COMPLETED / PARTIAL / FAILED / NO_SHOW
    pub q_scaled: u128,
    pub s_scaled: u128,
    pub q_final_scaled: u128,
    pub ts_ms: u64,
}

#[derive(BorshDeserialize, BorshSerialize, Clone, Serialize, Deserialize)]
pub struct AgentRecord {
    pub account: AccountId,
    pub stake: u128,
    pub tasks_completed: u64,
    pub tasks_failed: u64,
    /// Текущий рейтинг R (scaled).
    pub rating: u128,
    /// Сумма Q по всем терминалам (для R_eff).
    pub sum_q: u128,
    pub n: u64,
    pub ledger: Vec<RatingEvent>,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Contract {
    pub owner: AccountId,
    pub lots: IterableMap<String, Lot>,
    pub agents: IterableMap<AccountId, AgentRecord>,
    /// Сумма бюджетов живых лотов (консервация I-1/I-9).
    pub escrow: u128,
    /// Сожжённые стейки (I-2).
    pub burned: u128,
    /// Казн: комиссии протокола (fee).
    pub treasury: u128,
    /// Комиссия протокола в bps (0 = нет). По умолчанию 0.
    pub protocol_fee_bps: u16,
    pub min_stake: NearToken,
    /// HB-S18: замороженный slash-bond по лоту (lot_id → yocto). Пишется
    /// при award как `stake_winner_at_award × NO_SHOW_SLASH_BPS / 10_000`,
    /// ограниченный свободным стейком; снимается на терминале тем же
    /// значением — иммунен к set_min_stake (D6-1/D6-2). Отдельный префикс
    /// `b`, чтобы не менять borsh-раскладку Lot/AgentRecord (D6-4).
    pub lot_bonds: LookupMap<String, u128>,
    /// Сумма bonds агента по живым лотам (account → yocto) — O(1)-гейт
    /// withdraw_stake/submit_bid. Префикс `r`.
    pub agent_bonds: LookupMap<AccountId, u128>,
}

#[near]
impl Contract {
    #[init]
    pub fn new(owner_id: AccountId) -> Self {
        Self {
            owner: owner_id,
            lots: IterableMap::new(b"l"),
            agents: IterableMap::new(b"a"),
            escrow: 0,
            burned: 0,
            treasury: 0,
            protocol_fee_bps: 0,
            min_stake: NearToken::from_near(1),
            lot_bonds: LookupMap::new(b"b"),
            agent_bonds: LookupMap::new(b"r"),
        }
    }

    // ------------------------------------------------------------------
    // Администрирование
    // ------------------------------------------------------------------

    pub fn set_protocol_fee(&mut self, fee_bps: u16) {
        assert_eq!(env::predecessor_account_id(), self.owner, "owner only");
        assert!(fee_bps <= 5000, "fee_bps too large (≤5000)");
        self.protocol_fee_bps = fee_bps;
    }

    pub fn set_min_stake(&mut self, min_stake: NearToken) {
        assert_eq!(env::predecessor_account_id(), self.owner, "owner only");
        // HB-S01/S08: порог ограничен governance-диапазоном — иначе owner
        // может задизейблить регистрацию (2^128−1) или обнулить порог и
        // выключить слэш (slash = stake/2 → 0 при stake < 2 yocto).
        let amt = min_stake.as_yoctonear();
        assert!(
            amt >= NearToken::from_near(1).as_yoctonear()
                && amt <= NearToken::from_near(1_000).as_yoctonear(),
            "min_stake out of bounds [1..1000 N]"
        );
        self.min_stake = min_stake;
    }

    // ------------------------------------------------------------------
    // Агенты и рейтинг
    // ------------------------------------------------------------------

    /// Регистрация агента; attached deposit = стейк.
    #[payable]
    pub fn register_agent(&mut self) {
        let account = env::predecessor_account_id();
        let deposit = env::attached_deposit();
        assert!(
            deposit >= self.min_stake,
            "stake below minimum: need {}",
            self.min_stake
        );
        let stake = deposit.as_yoctonear();
        match self.agents.get(&account).cloned() {
            Some(mut rec) => {
                rec.stake += stake;
                self.agents.insert(account, rec);
            }
            None => {
                self.agents.insert(
                    account.clone(),
                    AgentRecord {
                        account,
                        stake,
                        tasks_completed: 0,
                        tasks_failed: 0,
                        rating: R0,
                        sum_q: 0,
                        n: 0,
                        ledger: Vec::new(),
                    },
                );
            }
        }
    }

    pub fn get_agent(&self, account: &AccountId) -> Option<AgentRecord> {
        self.agents.get(account).cloned()
    }

    /// Все зарегистрированные агенты (для инвариант-чекера/демо).
    pub fn get_agent_ids(&self) -> Vec<AccountId> {
        self.agents.keys().cloned().collect()
    }

    /// Σ stake всех агентов — консервационный инвариант on-chain:
    /// `balance == escrow + treasury + burned + Σ stakes`.
    pub fn get_total_staked(&self) -> u128 {
        self.agents.values().map(|a| a.stake).sum()
    }

    /// Текущий рейтинг R (scaled, 1e6).
    pub fn get_rating(&self, account: &AccountId) -> u128 {
        self.agents.get(account).map_or(R0, |a| a.rating)
    }

    /// R_eff (объёмно взвешенный, 5).
    pub fn get_rating_eff(&self, account: &AccountId) -> u128 {
        let r = self.get_rating(account);
        let n = self.agents.get(account).map_or(0, |a| a.n) as u128;
        (n * r + K0 * R0) / (n + K0)
    }

    pub fn get_ledger(&self, account: &AccountId) -> Vec<RatingEvent> {
        self.agents
            .get(account)
            .map_or_else(Vec::new, |a| a.ledger.clone())
    }

    // ------------------------------------------------------------------
    // Лоты
    // ------------------------------------------------------------------

    /// create_task: заказчик вносит депозит (= бюджет), лот → OPEN (P1).
    /// #[payable] обязателен: без него near-sdk отклоняет депозит на
    /// dispatch — unit-тесты это не видят (обёртка обходится), на чейне
    /// register/create_task падали бы всегда.
    #[payable]
    pub fn create_task(
        &mut self,
        lot_id: String,
        task_hash: String,
        bid_deadline_ms: u64,
        work_deadline_ms: u64,
        verify_deadline_ms: u64,
    ) {
        let manager = env::predecessor_account_id();
        let budget = env::attached_deposit().as_yoctonear();
        assert!(budget > 0, "budget must be positive");
        // HB-N04: P1 требует 'T:<64hex>' — не только префикс и длину,
        // но и hex-алфавит.
        assert!(
            task_hash.len() == 66
                && task_hash.starts_with("T:")
                && task_hash[2..].chars().all(|c| c.is_ascii_hexdigit()),
            "task_hash must be 'T:<64hex>'"
        );
        // HB-S11: идентификатор лота ограничен — пустые/гигантские id
        // засоряют storage и ключи.
        assert!(
            !lot_id.is_empty() && lot_id.len() <= 64,
            "lot_id must be 1..64 chars"
        );
        assert!(!self.lots.contains_key(&lot_id), "lot_id already exists");
        assert!(
            bid_deadline_ms < work_deadline_ms && work_deadline_ms < verify_deadline_ms,
            "deadlines must be ordered bid < work < verify"
        );
        let now = self._now_ms();
        // HB-M13: окно торгов не может быть уже закрыто в момент создания
        // (иначе мгновенный close+award+no_show за один блок).
        assert!(bid_deadline_ms > now, "bid_deadline must be in the future");
        self.lots.insert(
            lot_id.clone(),
            Lot {
                lot_id: lot_id.clone(),
                manager,
                task_hash,
                budget,
                bid_deadline_ms,
                work_deadline_ms,
                verify_deadline_ms,
                status: LotStatus::Open,
                bids: Vec::new(),
                winner: None,
                winning_price: 0,
                payout: None,
                submission: None,
                rework_count: 0,
                opened_ts_ms: now,
                award_ts_ms: None,
                settle_ts_ms: None,
            },
        );
        self.escrow += budget; // I-9
        env::log_str(&format!("HIVEBID create_task {} budget {}", lot_id, budget));
    }

    /// submit_bid (P2): только OPEN, до дедлайна, зарегистрированный агент,
    /// 0 < price ≤ budget, eta_ms < work_deadline (HB-S15: обещание ровно на
    /// границе невыполнимо он-тайм — сдача требует `now < wd`).
    pub fn submit_bid(&mut self, lot_id: String, price: u128, eta_ms: u64, signature: String) {
        let agent = env::predecessor_account_id();
        // HB-M11: не просто «зарегистрирован» — стейк ≥ min_stake (P2);
        // слэшенный до нуля агент перестаёт bid-ить.
        let rec = self.agents.get(&agent).cloned().expect("agent not registered");
        // HB-S18: bid-гейт по свободному стейку (stake − bonds) — полностью
        // занятый bond'ами агент не получает бесплатных торгов (D6-3).
        let bonded = self.agent_bonds.get(&agent).copied().unwrap_or(0);
        let free_stake = rec.stake.saturating_sub(bonded);
        assert!(
            free_stake >= self.min_stake.as_yoctonear(),
            "stake below minimum: {}",
            self.min_stake
        );
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(lot.status == LotStatus::Open, "lot not open");
        assert!(now < lot.bid_deadline_ms, "bidding closed");
        assert!(price > 0 && price <= lot.budget, "price out of bounds");
        assert!(eta_ms < lot.work_deadline_ms, "eta_ms must be before work deadline");
        // HB-M03: self-bid — менеджер не исполняет свою же задачу
        // (эскроу возвращался бы ему же + фарм рейтинга COMPLETED).
        assert!(agent != lot.manager, "self-bid not allowed");
        // HB-S11: подпись ограничена разумной длиной (внечейн-атрибуция).
        assert!(signature.len() <= 256, "signature too long");
        // HB-M17: граница числа ставок — иначе award упирается в газ.
        assert!(lot.bids.len() < MAX_BIDS, "bid limit reached");
        // Дубль-ставка агента запрещена: иначе скоринг идёт по одной bid,
        // а award_lot фиксирует payout по ПЕРВОЙ (probe_2 — pay разрыв).
        assert!(
            !lot.bids.iter().any(|b| b.agent == agent),
            "duplicate bid from agent"
        );
        lot.bids.push(Bid {
            agent: agent.clone(),
            price,
            eta_ms,
            ts_ms: now,
            signature,
        });
        self.lots.insert(lot_id, lot);
    }

    /// close_bidding (P3): после bid_deadline → BIDS_CLOSED (freeze bids).
    pub fn close_bidding(&mut self, lot_id: String) {
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(lot.status == LotStatus::Open, "lot not open");
        assert!(now >= lot.bid_deadline_ms, "bidding still open");
        lot.status = LotStatus::BidsClosed;
        self.lots.insert(lot_id, lot);
    }

    /// Победитель (view) на замороженном наборе bids.
    pub fn select_winner(&self, lot_id: String) -> Option<AccountId> {
        let lot = self.lots.get(&lot_id)?;
        if lot.status != LotStatus::BidsClosed || lot.bids.is_empty() {
            return None;
        }
        let now = self._now_ms();
        self._pick_best(lot, now).map(|b| b.agent.clone())
    }

    /// award_lot (P5): аналитик/менеджер выбирает победителя → AWARDED,
    /// фиксируется payout_vector {executor: P, manager: B−P} (деньги НЕ
    /// двигаются).
    pub fn award_lot(&mut self, lot_id: String, winner: AccountId) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(lot.status == LotStatus::BidsClosed, "lot must be BIDS_CLOSED");
        // HB-M01: P5 — награждать может менеджер лота или owner (аналитик),
        // не посторонний stranger.
        assert!(
            caller == lot.manager || caller == self.owner,
            "award: manager or analyst(owner) only"
        );
        // HB-M14: награждение после work_deadline → гарантированный слэш
        // через мгновенный mark_no_show.
        assert!(now < lot.work_deadline_ms, "work deadline already passed");
        // HB-M03/G-1 (HERETIC #405 §5): менеджер не может выиграть собственный
        // лот. submit_bid уже не пускает его в bid-набор, поэтому этот гейт —
        // инвариант (defense-in-depth), а не единственная защита: award
        // обязан отказывать и в том случае, если набор когда-нибудь будет
        // испорчен. Проверяется до поиска bid, чтобы сообщение не протекало
        // состав набора.
        assert!(winner != lot.manager, "self-dealing: manager cannot win own lot");
        let bid = lot
            .bids
            .iter()
            .find(|b| b.agent == winner)
            .expect("winner is not in bid set");
        lot.winner = Some(winner.clone());
        // HB-S18 (дельта 7): slash-bond замораживается при award как доля
        // СТОЙКИ на момент award — не min_stake (изменяемый глобал, D6-1)
        // и не весь стейк (D6-3). Ёмкость проверяется строго: bond не
        // больше свободного стейка (stake − уже занятых bonds).
        {
            let rec = self.agents.get(&winner).cloned().expect("winner agent not registered");
            let already = self.agent_bonds.get(&winner).copied().unwrap_or(0);
            let slash_base = rec.stake * NO_SHOW_SLASH_BPS as u128 / 10_000;
            let free = rec.stake.saturating_sub(already);
            // D7-1b: bid-гейт проверял free_stake на момент ставки, но
            // withdraw_stake мог увести stake ниже min_stake после неё —
            // тогда slash_base→0 и no_show слэшил бы ~0. Fail-closed:
            // award только при stake >= min_stake (симметрично bid-гейту).
            assert!(
                rec.stake >= self.min_stake.as_yoctonear(),
                "winner stake below min_stake at award"
            );
            // D7-1: частичный/нулевой bond не принимаем — award только при
            // полной slash-ёмкости, иначе лот получает усечённую защиту
            // (гонка: между bid и award стейк мог уйти в bonds других лотов).
            assert!(
                free >= slash_base,
                "winner free stake below slash bond"
            );
            self.lot_bonds.insert(lot_id.clone(), slash_base);
            self.agent_bonds.insert(winner.clone(), already + slash_base);
        }
        lot.winning_price = bid.price;
        lot.payout = Some(Payout {
            executor: bid.price,
            manager: lot.budget.saturating_sub(bid.price),
            fee: 0,
        });
        lot.award_ts_ms = Some(now);
        lot.status = LotStatus::Awarded;
        env::log_str(&format!("HIVEBID award {} <- {}", lot_id, winner));
        self.lots.insert(lot_id, lot);
    }

    /// award_best: аналитик — автоматический выбор победителя по счёту.
    pub fn award_best(&mut self, lot_id: String) -> AccountId {
        let now = self._now_ms();
        let lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(lot.status == LotStatus::BidsClosed, "lot must be BIDS_CLOSED");
        let best = self._pick_best(&lot, now).expect("no bids").agent.clone();
        drop(lot);
        self.award_lot(lot_id, best.clone());
        best
    }

    /// cancel_lot (P4): manager (или owner) до award → CANCELLED, возврат
    /// бюджета. HB-M18: после work_deadline не-награждённый лот может
    /// отменить ЛЮБОЙ — иначе эскроу зависает навсегда (refund всё равно
    /// уходит manager'у, выгоды для crank-вызывающего нет).
    pub fn cancel_lot(&mut self, lot_id: String) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        let timeout_rescue = now >= lot.work_deadline_ms;
        assert!(
            caller == lot.manager || caller == self.owner || timeout_rescue,
            "cancel: manager/owner, or anyone after work deadline"
        );
        assert!(lot.winner.is_none(), "cannot cancel after award");
        assert!(
            lot.status == LotStatus::Open || lot.status == LotStatus::BidsClosed,
            "lot not cancellable"
        );
        let refund = lot.budget;
        lot.status = LotStatus::Cancelled;
        lot.settle_ts_ms = Some(now);
        self.escrow = self.escrow.saturating_sub(refund);
        let manager = lot.manager.clone();
        self.lots.insert(lot_id.clone(), lot);
        Promise::new(manager).transfer(NearToken::from_yoctonear(refund)).detach();
        env::log_str(&format!("HIVEBID cancel {} refund {}", lot_id, refund));
    }

    /// submit_work (P6): только победитель, из AWARDED → SUBMITTED,
    /// attempt = rework_count + 1, до work_deadline.
    pub fn submit_work(&mut self, lot_id: String, work_hash: String) {
        let agent = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(
            matches!(lot.status, LotStatus::Awarded | LotStatus::Rework(_)),
            "lot not awaiting work"
        );
        assert_eq!(lot.winner.as_ref(), Some(&agent), "winner only");
        assert!(now < lot.work_deadline_ms, "work deadline passed");
        // HB-M16: после REWORK(3) сдачи больше нет — терминалы отсюда
        // только settle_partial/FAILED (иначе полная цена с q=0.25).
        assert!(lot.rework_count < MAX_REWORK, "rework limit reached");
        // HB-S11: «доказательство работы» обязано быть непустым и ограниченным.
        assert!(
            !work_hash.is_empty() && work_hash.len() <= 128,
            "work_hash must be 1..128 chars"
        );
        lot.submission = Some(Submission {
            work_hash,
            attempt: lot.rework_count + 1,
            ts_ms: now,
        });
        lot.status = LotStatus::Submitted;
        self.lots.insert(lot_id, lot);
    }

    /// accept_work (P7): только manager ИЛИ auto-release по verify_deadline →
    /// COMPLETED; payout + рейтинг.
    pub fn accept_work(&mut self, lot_id: String) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        // HB-M02/M16: приём только из SUBMITTED — Rework(k) ждёт
        // пересдачи (submission в нём уже очищена reject_work).
        assert!(lot.status == LotStatus::Submitted, "lot not submitted");
        let is_manager = lot.manager == caller;
        let timeout = now >= lot.verify_deadline_ms;
        assert!(is_manager || timeout, "manager only (or verify deadline passed)");
        if !is_manager {
            // P7: auto-release требует реальную сабмиссию (attempt ≥ 1) —
            // исполнитель не платит сам себе через timeout-ветку.
            let attempt = lot.submission.as_ref().map(|s| s.attempt).unwrap_or(0);
            assert!(attempt >= 1, "auto-release requires submission");
        }
        lot.status = LotStatus::Completed;
        lot.settle_ts_ms = Some(now);
        self._settle(&lot);
        self.lots.insert(lot_id.clone(), lot);
    }

    /// reject_work (P8/P9): только manager; k<3 → REWORK(k+1); k==3 → FAILED
    /// (исполнителю 0, весь бюджет заказчику; рейтинг FAILED → q=0).
    pub fn reject_work(&mut self, lot_id: String) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert_eq!(lot.manager, caller, "manager only");
        // Отклоняется живая сабмиссия (SUBMITTED); из Rework(3) прямой
        // вызов = финальный отказ → FAILED (P9, сабмиссий больше нет).
        assert!(
            lot.status == LotStatus::Submitted
                || matches!(lot.status, LotStatus::Rework(k) if k == MAX_REWORK),
            "lot not submitted"
        );
        assert!(now < lot.verify_deadline_ms, "verify deadline passed (auto-release)");
        if lot.rework_count >= MAX_REWORK {
            // P9: FAILED
            lot.status = LotStatus::Failed;
            lot.settle_ts_ms = Some(now);
            self.escrow = self.escrow.saturating_sub(lot.budget);
            Promise::new(lot.manager.clone()).transfer(NearToken::from_yoctonear(lot.budget)).detach();
            if let Some(winner) = &lot.winner {
                self._release_bond(&lot_id, winner);
                self._apply_rating(winner, 0, SCALE, "FAILED", now);
            }
            env::log_str(&format!("HIVEBID fail {} (rework exhausted)", lot_id));
        } else {
            lot.rework_count += 1;
            lot.status = LotStatus::Rework(lot.rework_count);
            lot.submission = None;
            env::log_str(&format!("HIVEBID rework {} k={}", lot_id, lot.rework_count));
        }
        self.lots.insert(lot_id, lot);
    }

    /// settle_partial (P10): из REWORK(3); α·P исполнителю, остаток заказчику;
    /// α задаётся в bps (0..9999). Рейтинг: PARTIAL (q по k=3 → 0.25).
    pub fn settle_partial(&mut self, lot_id: String, alpha_bps: u16) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert_eq!(lot.manager, caller, "manager only");
        assert!(
            matches!(lot.status, LotStatus::Rework(k) if k == MAX_REWORK),
            "partial settle only from REWORK(3)"
        );
        assert!(alpha_bps <= 9999, "α < 1");
        let payout = lot.payout.clone().expect("payout not set");
        let paid = payout.executor * alpha_bps as u128 / 10_000;
        // HB-S03: fee удерживается и здесь — та же экономика, что в _release
        // (fee из доли исполнителя; доля менеджера не трогается).
        let fee = paid * self.protocol_fee_bps as u128 / 10_000;
        let executor_paid = paid.saturating_sub(fee);
        lot.status = LotStatus::PartialSettled;
        lot.settle_ts_ms = Some(now);
        self.escrow = self.escrow.saturating_sub(lot.budget);
        if executor_paid > 0 {
            Promise::new(lot.winner.clone().expect("winner")).transfer(NearToken::from_yoctonear(executor_paid)).detach();
        }
        if fee > 0 {
            self.treasury += fee;
        }
        Promise::new(lot.manager.clone()).transfer(NearToken::from_yoctonear(lot.budget.saturating_sub(paid))).detach();
        if let Some(winner) = &lot.winner {
            self._release_bond(&lot_id, winner);
            // HB-M12: P10 — PARTIAL(q = 0, α); рейтинг падает, а не растёт.
            self._apply_rating(winner, 0, SCALE, "PARTIAL", now);
        }
        self.lots.insert(lot_id.clone(), lot);
        env::log_str(&format!("HIVEBID partial {} executor {}", lot_id, paid));
    }

    /// mark_no_show (P11): AWARDED + просрочена работа + нет сабмиссии →
    /// FAILED; слэш стейка победителя долей (G-12). HB-M18: crank открыт
    /// — переход детерминирован меткой времени, дискреции нет. HB-M19:
    /// до verify_deadline crank только у менеджера (его эскроу и его
    /// гонка с P12-окном победителя); посторонний раньше vd мог за газ
    /// срезать легальное late-окно и спалить чужой стейк — гриф без
    /// выгоды, убран. После vd живых путей у лота нет — терминализирует
    /// любой.
    pub fn mark_no_show(&mut self, lot_id: String) {
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        // I-8: Rework без пересдачи к work_deadline — тот же no_show
        // (submission в REWORK очищена reject_work, обязательство не снято).
        assert!(
            lot.status == LotStatus::Awarded || matches!(lot.status, LotStatus::Rework(_)),
            "lot not awarded"
        );
        // HB-S06: `>=` закрывает 1-ms мёртвую зону на границе — submit_work
        // уже запрещён (`now < wd`), раньше и no_show не имел ребра.
        // HB-W31 (6.2/6.3): из Rework исполнитель уже сдавал работу —
        // а) слэш выключается (rework-grind реджектов менеджером не должен
        //    конвертироваться в слэш честного стейка);
        // б) после work_deadline у агента живого хода нет (submit_work
        //    требует now < wd, late_submit — только Awarded), значит окно
        //    (wd, vd) под Rework не защищает никакой гонки — crank открыт
        //    любому сразу после wd, что разблокирует и hold-up
        //    settle_partial'ом: bond не может висеть вечно.
        let was_rework = matches!(lot.status, LotStatus::Rework(_));
        assert!(now >= lot.work_deadline_ms, "work deadline not passed");
        // HB-M19: окно (wd, vd) принадлежит гонке manager↔winner (P11/P12);
        // третьи лица допускаются только после vd (комплементарно к
        // late_submit `< vd` — мёртвой зоны нет). Для Rework — любой после wd.
        assert!(
            env::predecessor_account_id() == lot.manager
                || now >= lot.verify_deadline_ms
                || was_rework,
            "no_show: manager until verify deadline, anyone after"
        );
        assert!(lot.submission.is_none(), "work was submitted");
        lot.status = LotStatus::Failed;
        lot.settle_ts_ms = Some(now);
        self.escrow = self.escrow.saturating_sub(lot.budget);
        let manager = lot.manager.clone();
        Promise::new(manager).transfer(NearToken::from_yoctonear(lot.budget)).detach();
        if let Some(winner) = &lot.winner {
            // HB-S04: тихий `if let` терял бы слэш без следа; запись агента
            // гарантирована регистрацией при bid — отсутствие = коррупция.
            let mut rec = self.agents.get(winner).cloned().expect("winner agent not registered");
            // HB-S18 (дельта 7): слэш = замороженный bond этого лота
            // (50% стейка на момент award), не текущий stake×bps и не
            // живой min_stake — увод стейка не сжимает его (D6-5/D6-1).
            // HB-W31 (6.2): слэш только при истинном no-show из Awarded —
            // из Rework bond возвращается исполнителю целиком.
            let slash = if was_rework {
                0
            } else {
                let bond = self.lot_bonds.get(&lot_id).copied().unwrap_or(0);
                bond.min(rec.stake)
            };
            rec.stake = rec.stake.saturating_sub(slash);
            self.burned += slash;
            self.agents.insert(winner.clone(), rec);
            self._release_bond(&lot_id, winner);
            self._apply_rating(winner, 0, 0, "NO_SHOW", now);
        }
        self.lots.insert(lot_id.clone(), lot);
        env::log_str(&format!("HIVEBID no_show {}", lot_id));
    }

    // ------------------------------------------------------------------
    // Поздняя сдача (P12–P14, HB-S07 / G-7 / I-8)
    // ------------------------------------------------------------------

    /// late_submit_work (P12): поздняя сдача с work_deadline включительно
    /// (HB-S15: `now >= wd`, комплементарно `now < wd` у submit_work —
    /// миллисекундной дыры между двумя ветками нет) — из AWARDED в
    /// SUBMITTED_LATE; окно до verify_deadline (после него void_late и так
    /// крэнкается любым). Гонка с mark_no_show решается порядком вызовов —
    /// кто первый, тот переход и стоит.
    pub fn late_submit_work(&mut self, lot_id: String, work_hash: String) {
        let agent = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(lot.status == LotStatus::Awarded, "lot not awaiting work");
        assert_eq!(lot.winner.as_ref(), Some(&agent), "winner only");
        assert!(now >= lot.work_deadline_ms, "work deadline not passed");
        assert!(
            now < lot.verify_deadline_ms,
            "verify deadline passed (void_late)"
        );
        assert!(
            !work_hash.is_empty() && work_hash.len() <= 128,
            "work_hash must be 1..128 chars"
        );
        lot.submission = Some(Submission {
            work_hash,
            attempt: lot.rework_count + 1,
            ts_ms: now,
        });
        lot.status = LotStatus::SubmittedLate;
        self.lots.insert(lot_id.clone(), lot);
        env::log_str(&format!("HIVEBID late_submit {}", lot_id));
    }

    /// accept_work_late (P13): manager принимает позднюю сдачу → COMPLETED.
    /// рейтинг идёт через _apply_rating/_speed_factor (HB-S17), как он-тайм
    /// путь; для просроченной сдачи кривая даёт s = 0, поэтому итог прежний:
    /// q_final = q·S_MIN.
    pub fn accept_work_late(&mut self, lot_id: String) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert_eq!(lot.manager, caller, "manager only");
        assert!(
            lot.status == LotStatus::SubmittedLate,
            "lot not late-submitted"
        );
        lot.status = LotStatus::Completed;
        lot.settle_ts_ms = Some(now);
        self._release(&lot);
        if let Some(winner) = &lot.winner {
            let k = lot.rework_count as u128;
            let q = SCALE.saturating_sub(k * LAMBDA);
            // HB-S17: s берётся из той же кривой, что и он-тайм путь, —
            // отдельная константа была бы вторым источником правды. Для
            // просроченной сдачи кривая даёт ровно 0 (elapsed за окном ⇒
            // late ≥ span ⇒ SCALE − span·SCALE/span), поэтому поведение не
            // меняется. Это безопасно ровно потому, что award больше не
            // возможен на самой границе (`now < wd` ⇒ window ≥ 1 мс): при
            // window == 0 _speed_factor возвращает SCALE, а не 0.
            let submit_ts = lot.submission.as_ref().map(|s| s.ts_ms).unwrap_or(now);
            let s = self._speed_factor(&lot, submit_ts);
            self._apply_rating(winner, q, s, "COMPLETED", now);
        }
        self.lots.insert(lot_id.clone(), lot);
        env::log_str(&format!("HIVEBID accept_late {}", lot_id));
    }

    /// void_late (P14): позднюю сдачу аннулирует manager — либо ЛЮБОЙ с
    /// verify_deadline (`now >= vd`, HB-S14 — тот же канон, что у accept_work
    /// и mark_no_show; I-8: каждый лот имеет авто-терминал). → FAILED,
    /// весь бюджет менеджеру.
    pub fn void_late(&mut self, lot_id: String) {
        let caller = env::predecessor_account_id();
        let now = self._now_ms();
        let mut lot = self.lots.get(&lot_id).expect("no such lot").clone();
        assert!(
            lot.status == LotStatus::SubmittedLate,
            "lot not late-submitted"
        );
        let timeout = now >= lot.verify_deadline_ms;
        assert!(
            caller == lot.manager || timeout,
            "manager only (or verify deadline passed)"
        );
        lot.status = LotStatus::Failed;
        lot.settle_ts_ms = Some(now);
        self.escrow = self.escrow.saturating_sub(lot.budget);
        let manager = lot.manager.clone();
        Promise::new(manager).transfer(NearToken::from_yoctonear(lot.budget)).detach();
        if let Some(winner) = &lot.winner {
            self._release_bond(&lot_id, winner);
            self._apply_rating(winner, 0, 0, "FAILED", now);
        }
        self.lots.insert(lot_id.clone(), lot);
        env::log_str(&format!("HIVEBID void_late {}", lot_id));
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    /// Факторы и итоговый score каждой ставки в порядке её подачи.
    /// Значения факторов и score используют фиксированный масштаб SCALE;
    /// для неизвестного lot_id возвращается пустой список.
    pub fn get_bid_scores(&self, lot_id: String) -> Vec<BidScore> {
        let Some(lot) = self.lots.get(&lot_id) else {
            return Vec::new();
        };
        let now_ms = self._now_ms();
        lot.bids
            .iter()
            .map(|bid| {
                let (f_price, f_rating, f_eta) = self._score_factors(lot, bid, now_ms);
                BidScore {
                    agent: bid.agent.clone(),
                    price: bid.price,
                    eta_ms: bid.eta_ms,
                    f_price,
                    f_rating,
                    f_eta,
                    score: Self::_weighted_score(f_price, f_rating, f_eta),
                }
            })
            .collect()
    }

    pub fn get_lot(&self, lot_id: String) -> Option<Lot> {
        self.lots.get(&lot_id).cloned()
    }

    pub fn get_lot_ids(&self) -> Vec<String> {
        self.lots.iter().map(|(k, _)| k.clone()).collect()
    }

    pub fn get_escrow(&self) -> u128 {
        self.escrow
    }

    pub fn get_treasury(&self) -> u128 {
        self.treasury
    }

    pub fn get_burned(&self) -> u128 {
        self.burned
    }

    // ------------------------------------------------------------------
    // Изъятие средств (HB-M05/M06 — счётчики получают путь наружу)
    // ------------------------------------------------------------------

    /// treasury_withdraw (owner): вывод комиссий протокола. Консервация:
    /// баланс контракта = escrow (живые бюджеты) + treasury + burned
    /// (сожжённые стейки остаются на балансе как доказательство слэша).
    pub fn treasury_withdraw(&mut self, amount: NearToken) {
        assert_eq!(env::predecessor_account_id(), self.owner, "owner only");
        let amt = amount.as_yoctonear();
        assert!(
            amt > 0 && amt <= self.treasury,
            "amount out of treasury bounds"
        );
        self.treasury -= amt;
        Promise::new(self.owner.clone()).transfer(amount).detach();
        env::log_str(&format!("HIVEBID treasury_withdraw {}", amt));
    }

    /// withdraw_stake: агент выводит (часть) своего стейка. HB-S18: вывод
    /// ограничен `stake − agent_bonds` — bond, замороженный на каждый
    /// выигранный лот, нельзя увести до его терминала, иначе слэш
    /// обходится. NB: pending-ставки без award bond не резервируют —
    /// защиту на award закрывает assert stake>=min_stake (D7-1b).
    pub fn withdraw_stake(&mut self, amount: NearToken) {
        let account = env::predecessor_account_id();
        let amt = amount.as_yoctonear();
        assert!(amt > 0, "amount must be positive");
        let mut rec = self.agents.get(&account).cloned().expect("agent not registered");
        let bonded = self.agent_bonds.get(&account).copied().unwrap_or(0);
        assert!(
            amt <= rec.stake.saturating_sub(bonded),
            "amount exceeds withdrawable stake (bonded)"
        );
        rec.stake -= amt;
        self.agents.insert(account.clone(), rec);
        Promise::new(account).transfer(amount).detach();
        env::log_str(&format!("HIVEBID withdraw_stake {}", amt));
    }
}

// ---------------------------------------------------------------------------
// Внутренние помощники (не экспонируются в ABI)
// ---------------------------------------------------------------------------

impl Contract {
    fn _now_ms(&self) -> u64 {
        env::block_timestamp() / 1_000_000
    }

    fn _agent_exists(&self, account: &AccountId) -> bool {
        self.agents.contains_key(account)
    }

    /// Счёт аналитика (5) для кандидата — scaled.
    fn _score(&self, lot: &Lot, bid: &Bid, now_ms: u64) -> u128 {
        let (f_price, f_rating, f_eta) = self._score_factors(lot, bid, now_ms);
        Self::_weighted_score(f_price, f_rating, f_eta)
    }

    fn _score_factors(&self, lot: &Lot, bid: &Bid, now_ms: u64) -> (u128, u128, u128) {
        let b = lot.budget;
        // f_price = 1 − P/B
        let f_price = if b == 0 { 0 } else { SCALE - bid.price * SCALE / b };
        // f_rating = R_eff(agent)
        let f_rating = self.get_rating_eff(&bid.agent);
        // f_eta = clamp((work_deadline − eta)/(work_deadline − award_ts),0,1)
        let award_ts = lot.award_ts_ms.unwrap_or(now_ms);
        let denom = lot.work_deadline_ms.saturating_sub(award_ts);
        let f_eta = if denom == 0 {
            0
        } else {
            let num = lot.work_deadline_ms.saturating_sub(bid.eta_ms) as u128;
            (num * SCALE / denom as u128).min(SCALE)
        };
        (f_price, f_rating, f_eta)
    }

    fn _weighted_score(f_price: u128, f_rating: u128, f_eta: u128) -> u128 {
        W_PRICE * f_price / SCALE
            + W_RATING * f_rating / SCALE
            + W_ETA * f_eta / SCALE
    }

    fn _pick_best<'a>(&self, lot: &'a Lot, now_ms: u64) -> Option<&'a Bid> {
        // HB-M17: O(n) — лучший счёт хранится, а не перевзвешивается на
        // каждого кандидата (раньше O(n²) чтений хранилища → gas-DoS).
        let mut best: Option<(&Bid, u128)> = None;
        for bid in &lot.bids {
            let s = self._score(lot, bid, now_ms);
            let better = match best {
                None => true,
                Some((cur, cur_score)) => {
                    s > cur_score || (s == cur_score && bid.price < cur.price)
                }
            };
            if better {
                best = Some((bid, s));
            }
        }
        best.map(|(b, _)| b)
    }

    /// Фактор скорости s ∈ [0,1] scaled (§6.2–6.3): сдача до τ0 доли окна
    /// → s = 1; дальше линейный спад к 0 у work_deadline; после дедлайна
    /// (auto-release) → 0. HB-M15: раньше был захардкожен в SCALE —
    /// «быстро == медленно».
    fn _speed_factor(&self, lot: &Lot, ts_ms: u64) -> u128 {
        let award_ts = lot.award_ts_ms.unwrap_or(lot.opened_ts_ms);
        let window = lot.work_deadline_ms.saturating_sub(award_ts) as u128;
        if window == 0 {
            return SCALE;
        }
        let elapsed = ts_ms.saturating_sub(award_ts) as u128;
        let fast_edge = TAU0 * window / SCALE;
        if elapsed <= fast_edge {
            return SCALE;
        }
        let span = window - fast_edge;
        let late = elapsed.saturating_sub(fast_edge);
        SCALE - (late.min(span) * SCALE / span)
    }

    /// Рейтинг: правило B (6.4) + ledger-append.
    /// `s_scaled` — фактор скорости из _speed_factor (§6.3).
    fn _apply_rating(&mut self, account: &AccountId, q_scaled: u128, s_scaled: u128, term: &str, ts_ms: u64) {
        // HB-S04: молчаливый return терял рейтинг-событие при деньгах уже
        // в пути; агент зарегистрирован с момента bid — expect, не тишина.
        let mut rec = self
            .agents
            .get(account)
            .cloned()
            .expect("agent not registered");
        rec.tasks_completed += u64::from(term == "COMPLETED");
        rec.tasks_failed += u64::from(term == "FAILED" || term == "NO_SHOW" || term == "PARTIAL");
        // Q = q · (s_min + (1−s_min)·s) — реальный s, не константа (HB-M15).
        let q_scaled = q_scaled.min(SCALE);
        let s_scaled = s_scaled.min(SCALE);
        let s_eff = S_MIN + (SCALE - S_MIN) * s_scaled / SCALE;
        let q_final = q_scaled * s_eff / SCALE;
        // HB-M12: PARTIAL — не успех (q=0 по P10), рейтинг снижается.
        let success = term == "COMPLETED";
        if success {
            let step = DELTA_UP * q_final * (SCALE - rec.rating) / (SCALE * SCALE);
            rec.rating = (rec.rating + step).min(SCALE);
        } else {
            let step = DELTA_DN * rec.rating / SCALE;
            rec.rating = rec.rating.saturating_sub(step);
        }
        rec.sum_q += q_final;
        rec.n += 1;
        // HB-S10: ledger — кольцевой буфер; старые события вытесняются,
        // статистика (n, sum_q, rating) накапливается отдельно.
        if rec.ledger.len() >= MAX_LEDGER_EVENTS {
            rec.ledger.remove(0);
        }
        rec.ledger.push(RatingEvent {
            term: term.to_string(),
            q_scaled,
            s_scaled,
            q_final_scaled: q_final,
            ts_ms,
        });
        self.agents.insert(account.clone(), rec);
    }

    /// Платит победителю согласно payout_vector; вызывается из терминальных
    /// переходов. Возвращает (executor_paid, manager_paid, fee_kept).
    fn _release(&mut self, lot: &Lot) -> (u128, u128, u128) {
        let payout = lot.payout.clone().expect("payout not set");
        let fee_bps = self.protocol_fee_bps as u128;
        let fee = payout.executor * fee_bps / 10_000;
        let executor_paid = payout.executor.saturating_sub(fee);
        let manager_paid = payout.manager;
        if executor_paid > 0 {
            Promise::new(lot.winner.clone().expect("winner"))
                .transfer(NearToken::from_yoctonear(executor_paid)).detach();
        }
        if manager_paid > 0 {
            Promise::new(lot.manager.clone()).transfer(NearToken::from_yoctonear(manager_paid)).detach();
        }
        if fee > 0 {
            self.treasury += fee;
        }
        self.escrow = self.escrow.saturating_sub(lot.budget);
        if let Some(winner) = &lot.winner {
            self._release_bond(&lot.lot_id, winner);
        }
        (executor_paid, manager_paid, fee)
    }

    /// HB-S18: освобождение slash-bond на терминале лота. Значение
    /// заморожено при award в `lot_bonds` — терминал вычитает ровно его,
    /// иммунен к set_min_stake и топ-апам (D6-1/D6-2).
    fn _release_bond(&mut self, lot_id: &str, winner: &AccountId) {
        if let Some(bond) = self.lot_bonds.remove(lot_id) {
            let already = self.agent_bonds.get(winner).copied().unwrap_or(0);
            // Нормализация: нулевой остаток удаляем, а не храним 0-entry.
            if already > bond {
                self.agent_bonds.insert(winner.clone(), already - bond);
            } else {
                self.agent_bonds.remove(winner);
            }
        }
    }

    fn _settle(&mut self, lot: &Lot) {
        self._release(lot);
        if let Some(winner) = &lot.winner {
            let k = lot.rework_count as u128;
            let q = SCALE.saturating_sub(k * LAMBDA); // 1 − 0.25·k
            // HB-M15: скорость по ts сабмиссии; auto-release (submission
            // старый/свежий) — s из реальных меток, не константа.
            let submit_ts = lot.submission.as_ref().map(|s| s.ts_ms)
                .unwrap_or_else(|| lot.settle_ts_ms.unwrap_or(self._now_ms()));
            let s = self._speed_factor(lot, submit_ts);
            let ts = lot.settle_ts_ms.unwrap_or(self._now_ms());
            self._apply_rating(winner, q, s, "COMPLETED", ts);
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::*;
    use near_sdk::test_utils::{accounts, VMContextBuilder};
    use near_sdk::testing_env;

    const BASE_MS: u64 = 1_700_000_000_000;

    fn ts(offset_ms: u64) -> u64 {
        (BASE_MS + offset_ms) * 1_000_000
    }

    fn setup() -> Contract {
        let mut c = VMContextBuilder::new();
        c.current_account_id(accounts(0));
        testing_env!(c.build());
        Contract::new(accounts(0))
    }

    fn as_ctx(pre: &AccountId, dep: NearToken, ts_ns: u64) {
        let mut c = VMContextBuilder::new();
        c.predecessor_account_id(pre.clone())
            .attached_deposit(dep)
            .block_timestamp(ts_ns)
            .current_account_id(accounts(0));
        testing_env!(c.build());
    }

    fn task_hash() -> String {
        format!("T:{}", "0".repeat(64))
    }

    fn register(contract: &mut Contract, acct: &AccountId) {
        as_ctx(acct, NearToken::from_near(10), ts(0));
        contract.register_agent();
    }

    const BUDGET_N: u128 = 100;
    const PRICE_N: u128 = 40;

    fn create_lot(contract: &mut Contract, manager: &AccountId, lot_id: &str) {
        let d = NearToken::from_near(BUDGET_N);
        as_ctx(manager, d, ts(0));
        contract.create_task(
            lot_id.to_string(),
            task_hash(),
            BASE_MS + 1000,
            BASE_MS + 20_000,
            BASE_MS + 40_000,
        );
    }

    fn to_award(contract: &mut Contract, manager: &AccountId, bidder: &AccountId, lot_id: &str, price_n: u128) {
        create_lot(contract, manager, lot_id);
        as_ctx(bidder, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid(lot_id.to_string(), NearToken::from_near(price_n).as_yoctonear(), 10_000, "sig".into());
        as_ctx(manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding(lot_id.to_string());
        contract.award_best(lot_id.to_string());
    }

    #[test]
    fn get_bid_scores_exposes_factors_and_matches_award_best() {
        let mut contract = setup();
        assert!(contract.get_bid_scores("missing".into()).is_empty());
        let manager = accounts(1);
        let alice = accounts(2);
        let bob = accounts(3);
        let charlie = accounts(4);
        for agent in [&alice, &bob, &charlie] {
            register(&mut contract, agent);
        }

        // Завершённый лот поднимает эффективный рейтинг Боба с 500_000 до 512_500.
        to_award(&mut contract, &manager, &bob, "training", PRICE_N);
        as_ctx(&bob, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("training".into(), "W:training".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("training".into());
        assert_eq!(contract.get_rating_eff(&bob), 512_500);

        let bid_deadline = BASE_MS + 5_000;
        let work_deadline = BASE_MS + 24_000;
        let verify_deadline = BASE_MS + 45_000;
        as_ctx(&manager, NearToken::from_near(BUDGET_N), ts(4000));
        contract.create_task(
            "scored".into(),
            task_hash(),
            bid_deadline,
            work_deadline,
            verify_deadline,
        );

        for (agent, price_n, eta_ms, signature) in [
            (&alice, 20, BASE_MS + 9_000, "alice"),
            (&bob, 30, BASE_MS + 14_000, "bob"),
            (&charlie, 40, BASE_MS + 19_000, "charlie"),
        ] {
            as_ctx(agent, NearToken::from_yoctonear(0), ts(4100));
            contract.submit_bid(
                "scored".into(),
                NearToken::from_near(price_n).as_yoctonear(),
                eta_ms,
                signature.into(),
            );
        }

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(6000));
        contract.close_bidding("scored".into());
        let scores = contract.get_bid_scores("scored".into());

        assert_eq!(scores.len(), 3);
        assert_eq!(
            (
                scores[0].agent.clone(),
                scores[0].f_price,
                scores[0].f_rating,
                scores[0].f_eta,
                scores[0].score,
            ),
            (alice.clone(), 800_000, 500_000, 833_333, 686_666),
        );
        assert_eq!(
            (
                scores[1].agent.clone(),
                scores[1].f_price,
                scores[1].f_rating,
                scores[1].f_eta,
                scores[1].score,
            ),
            (bob.clone(), 700_000, 512_500, 555_555, 596_111),
        );
        assert_eq!(
            (
                scores[2].agent.clone(),
                scores[2].f_price,
                scores[2].f_rating,
                scores[2].f_eta,
                scores[2].score,
            ),
            (charlie, 600_000, 500_000, 277_777, 495_555),
        );

        let lot = contract.get_lot("scored".into()).unwrap();
        let picked = contract
            ._pick_best(&lot, BASE_MS + 6_000)
            .unwrap()
            .agent
            .clone();
        assert_eq!(picked, scores[0].agent);
        assert_eq!(contract.award_best("scored".into()), picked);
    }

    // ---- Счасливый путь ------------------------------------------------

    #[test]
    fn happy_path_full() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);

        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Awarded);
        assert_eq!(lot.winner.unwrap(), alice);
        let payout = lot.payout.unwrap();
        assert_eq!(payout.executor, NearToken::from_near(PRICE_N).as_yoctonear());
        assert_eq!(
            payout.manager,
            NearToken::from_near(BUDGET_N - PRICE_N).as_yoctonear()
        );
        assert_eq!(payout.fee, 0);

        // эскроу всё ещё держит бюджет
        assert_eq!(contract.escrow, NearToken::from_near(BUDGET_N).as_yoctonear());

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:deadbeef".into());

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Completed);
        assert!(lot.settle_ts_ms.is_some());

        // консервация: эскроу опустошено, казна без комиссии
        assert_eq!(contract.escrow, 0);
        assert_eq!(contract.treasury, 0);

        // рейтинг победителя вырос
        let before = R0;
        let after = contract.get_rating(&alice);
        assert!(after > before, "rating should rise: {} -> {}", before, after);
        let eff = contract.get_rating_eff(&alice);
        assert!(eff >= R0 && eff <= after); // выпуклая комбинация R0 и R
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.tasks_completed, 1);
        assert_eq!(rec.ledger.len(), 1);
    }

    // ---- Рейтинг: правило B -------------------------------------------

    #[test]
    fn rating_success_upper_bound_and_failure_drop() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);

        // успех поднимает рейтинг, но не выше 1.0
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:a".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());
        let r1 = contract.get_rating(&alice);
        assert!(r1 > R0 && r1 <= SCALE);

        // отказ после 3 реворк → FAILED → рейтинг падает
        to_award(&mut contract, &manager, &alice, "lot2", PRICE_N);
        for attempt in 0..3 {
            as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
            contract.submit_work("lot2".into(), format!("W:{}", attempt));
            as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
            contract.reject_work("lot2".into());
        }
        // HB-M16: из Rework(3) менеджер дожимает лот до FAILED
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.reject_work("lot2".into());
        let lot = contract.get_lot("lot2".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Failed);
        assert_eq!(contract.escrow, 0);
        let r2 = contract.get_rating(&alice);
        assert!(r2 < r1, "rating should drop: {} -> {}", r1, r2);
    }

    // ---- Пацтиал из REWORK(3) ----------------------------------------

    #[test]
    fn partial_settle_after_three_rework() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        for attempt in 0..3 {
            as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
            contract.submit_work("lot1".into(), format!("W:{}", attempt));
            as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
            contract.reject_work("lot1".into());
        }
        let lot = contract.get_lot("lot1".into()).unwrap();
        assert!(matches!(lot.status, LotStatus::Rework(3)));

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.settle_partial("lot1".into(), 5000);

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::PartialSettled);
        assert_eq!(contract.escrow, 0);
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.tasks_failed, 1); // HB-M12: PARTIAL — падение (q=0)
        assert_eq!(rec.tasks_completed, 0);
        let ev = rec.ledger.last().unwrap();
        assert_eq!(ev.term, "PARTIAL");
        assert_eq!(ev.q_scaled, 0);
    }

    // ---- NO_SHOW слэш стейка -----------------------------------------

    #[test]
    fn no_show_slashes_stake() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        let stake = contract.get_agent(&alice).unwrap().stake;

        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "s".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());
        contract.award_best("lot1".into());

        // время ушло за work_deadline (BASE+20000), сабмиссии нет
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(25_000));
        contract.mark_no_show("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Failed);
        assert_eq!(contract.escrow, 0);
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.stake, stake - stake * NO_SHOW_SLASH_BPS as u128 / 10_000);
        assert_eq!(rec.tasks_failed, 1);
        assert!(contract.burned > 0);
    }

    // ---- Cancel и возврат бюджета -------------------------------------

    #[test]
    fn cancel_refunds_and_frees_escrow() {
        let mut contract = setup();
        let manager = accounts(1);
        create_lot(&mut contract, &manager, "lot1");
        assert!(contract.escrow > 0);

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(500));
        contract.cancel_lot("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Cancelled);
        assert_eq!(contract.escrow, 0);
    }

    // ---- Комиссия протокола ------------------------------------------

    #[test]
    fn protocol_fee_kept_in_treasury() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);

        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(0));
        contract.set_protocol_fee(1000); // 10%

        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());

        // fee = 10% от 40N = 4N
        assert_eq!(contract.treasury, NearToken::from_near(4).as_yoctonear());
        assert_eq!(contract.escrow, 0);
    }

    // ---- HB-M18: спасение зависших лотов --------------------------------

    /// До work_deadline посторонний лот не отменит — гейт роли.
    #[test]
    #[should_panic(expected = "cancel: manager/owner, or anyone after work deadline")]
    fn stranger_cannot_cancel_before_deadline() {
        let mut contract = setup();
        let manager = accounts(1);
        let anyone = accounts(5);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&anyone, NearToken::from_yoctonear(0), ts(500));
        contract.cancel_lot("lot1".into());
    }

    /// HB-M18: после work_deadline crank открыт любому — эскроу
    /// возвращается менеджеру, зависшего бюджета нет.
    #[test]
    fn stuck_lot_rescued_after_deadline() {
        let mut contract = setup();
        let manager = accounts(1);
        let anyone = accounts(5);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&anyone, NearToken::from_yoctonear(0), ts(25_000));
        contract.cancel_lot("lot1".into());
        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Cancelled);
        assert_eq!(contract.escrow, 0);
    }

    #[test]
    fn no_show_crank_by_anyone() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let anyone = accounts(5);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "s".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());
        contract.award_best("lot1".into());

        // crank — не manager, после verify_deadline: переход
        // детерминирован временем, живых путей у лота уже нет (HB-M19)
        as_ctx(&anyone, NearToken::from_yoctonear(0), ts(40_000));
        contract.mark_no_show("lot1".into());
        assert_eq!(contract.get_lot("lot1".into()).unwrap().status, LotStatus::Failed);
    }

    // ---- HB-M05/M06: изъятие средств -------------------------------------

    #[test]
    fn treasury_withdraw_owner_only() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(0));
        contract.set_protocol_fee(1000);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());
        let fee = contract.treasury;
        assert!(fee > 0);

        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(4000));
        contract.treasury_withdraw(NearToken::from_yoctonear(fee));
        assert_eq!(contract.treasury, 0);
    }

    #[test]
    #[should_panic(expected = "owner only")]
    fn treasury_withdraw_not_owner() {
        let mut contract = setup();
        contract.treasury = 1;
        as_ctx(&accounts(3), NearToken::from_yoctonear(0), ts(0));
        contract.treasury_withdraw(NearToken::from_yoctonear(1));
    }

    #[test]
    fn withdraw_stake_partial() {
        let mut contract = setup();
        let alice = accounts(2);
        register(&mut contract, &alice);
        let stake = contract.get_agent(&alice).unwrap().stake;

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.withdraw_stake(NearToken::from_yoctonear(stake / 2));
        assert_eq!(contract.get_agent(&alice).unwrap().stake, stake / 2);
    }

    /// Верхняя граница: нельзя вывести больше, чем положено стейка.
    /// Иначе агент снимает чужие гарантии, а min_stake/slash считаются от
    /// несуществующего остатка.
    #[test]
    #[should_panic(expected = "withdrawable stake")]
    fn withdraw_stake_over_bound_panics() {
        let mut contract = setup();
        let alice = accounts(2);
        register(&mut contract, &alice);
        let stake = contract.get_agent(&alice).unwrap().stake;

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.withdraw_stake(NearToken::from_yoctonear(stake + 1));
    }

    /// Нижняя граница: нулевой вывод — это пустой promise за газ вызывающего.
    #[test]
    #[should_panic(expected = "amount must be positive")]
    fn withdraw_stake_zero_panics() {
        let mut contract = setup();
        let alice = accounts(2);
        register(&mut contract, &alice);

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.withdraw_stake(NearToken::from_yoctonear(0));
    }

    // ---- HB-S18: slash-bond под живой лот (дельта 7) ----------------------

    /// Bond при award = stake×50% (10 N → 5 N): вывод ограничен
    /// stake − bonded, увести стейк ниже bond нельзя.
    #[test]
    #[should_panic(expected = "withdrawable stake (bonded)")]
    fn hb_s18_winner_cannot_drain_bonded_stake() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);

        assert_eq!(contract.agent_bonds.get(&alice).copied(), Some(5 * NearToken::from_near(1).as_yoctonear()));
        let stake = contract.get_agent(&alice).unwrap().stake;
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.withdraw_stake(NearToken::from_yoctonear(stake - stake / 2 + 1));
    }

    /// Свободная часть (stake − bond) выводится ровно до bond.
    #[test]
    fn hb_s18_withdrawable_part_only() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);

        let stake = contract.get_agent(&alice).unwrap().stake;
        let free = stake / 2; // bond = 50% стейка при award
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.withdraw_stake(NearToken::from_yoctonear(free));
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.stake, stake - free);
        assert_eq!(contract.agent_bonds.get(&alice).copied(), Some(free));
    }

    /// D6-5: вывод до bond НЕ сжимает слэш — no_show снимает полный
    /// замороженный bond (5 N), а не 50% остатка (0.5 N).
    #[test]
    fn hb_s18_no_show_slashes_full_frozen_bond_after_withdraw() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        let bond = *contract.lot_bonds.get("L1").unwrap();
        assert_eq!(bond, NearToken::from_near(5).as_yoctonear());

        // Агент выводит всё, что разрешено (stake − bond).
        let stake = contract.get_agent(&alice).unwrap().stake;
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.withdraw_stake(NearToken::from_yoctonear(stake - bond));

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.mark_no_show("L1".to_string());

        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.stake, 0);
        assert_eq!(contract.burned, bond);
        assert_eq!(contract.agent_bonds.get(&alice), None);
        assert_eq!(contract.lot_bonds.get("L1"), None);
    }

    /// D6-1/D6-2: set_min_stake после award не меняет замороженный bond —
    /// терминал снимает ровно записанное при award значение.
    #[test]
    fn hb_s18_bond_frozen_against_min_stake_changes() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        let bond = *contract.lot_bonds.get("L1").unwrap();

        // Владелец двигает min_stake в обе стороны до терминала.
        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(2000));
        contract.set_min_stake(NearToken::from_near(100));
        contract.set_min_stake(NearToken::from_near(1));

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.mark_no_show("L1".to_string());
        assert_eq!(contract.burned, bond);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// D6-3: bond клампится свободным стейком — Σ bonds никогда не
    /// превышает stake. Здесь: bond₁=5 N, bond₂=min(5 N, 10−5)=5 N →
    /// bonded=10 N=stake, withdrawable=0 (не > stake, не отрицательно).
    #[test]
    #[should_panic(expected = "withdrawable stake (bonded)")]
    fn hb_s18_bond_never_exceeds_stake() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);

        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        to_award(&mut contract, &manager, &alice, "L2", PRICE_N);

        let stake = contract.get_agent(&alice).unwrap().stake;
        let bonded = *contract.agent_bonds.get(&alice).unwrap();
        assert!(bonded <= stake);
        assert_eq!(bonded, stake); // 5+5=10 = весь стейк занят — граница
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        // Любой вывод запрещён: bonded == stake.
        contract.withdraw_stake(NearToken::from_near(1));
    }

    /// D6-3 вторая сторона: при bonded == stake свободный стейк = 0 —
    /// submit_bid отклоняет (гейт по free_stake), третьего лота не будет.
    #[test]
    #[should_panic(expected = "stake below minimum")]
    fn hb_s18_fully_bonded_agent_cannot_bid() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);

        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        to_award(&mut contract, &manager, &alice, "L2", PRICE_N);
        assert_eq!(*contract.agent_bonds.get(&alice).unwrap(), contract.get_agent(&alice).unwrap().stake);

        // L3: alice не может сделать ставку — свободного стейка нет.
        create_lot(&mut contract, &manager, "L3");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("L3".to_string(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());
    }

    /// D7-1b: withdraw уводит stake НИЖЕ min_stake после bid-гейта —
    /// award обязан отклониться (fail-closed), иначе slash_base≈0 и
    /// no_show слэшил бы ~0 вместо ~5N (QODER v2_3/v2_7: stake=2 yocto
    /// обходил бы assert(slash_base>0); stake<min_stake — нет).
    #[test]
    #[should_panic(expected = "winner stake below min_stake at award")]
    fn d7b_withdraw_below_min_stake_blocks_award() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);

        create_lot(&mut contract, &manager, "L1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("L1".to_string(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());
        // 10N -> 0.5N: выводим всё сверх половины min_stake (pending-бондов
        // ещё нет — bid-гейт смотрел только на момент ставки).
        let rec = contract.get_agent(&alice).unwrap();
        let min = contract.min_stake.as_yoctonear();
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(200));
        contract.withdraw_stake(NearToken::from_yoctonear(rec.stake - min / 2));
        // award: паника — победитель не проходит min_stake-гейт.
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("L1".to_string());
        contract.award_best("L1".to_string());
    }

    /// Резерв освобождается на терминале: после COMPLETED bond снят,
    /// весь стейк снова выводим.
    #[test]
    fn hb_s18_bond_released_on_terminal() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(5000));
        contract.submit_work("L1".to_string(), "W:done".to_string());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(6000));
        contract.accept_work("L1".to_string());

        assert_eq!(contract.agent_bonds.get(&alice), None);
        assert_eq!(contract.lot_bonds.get("L1"), None);
        let rec = contract.get_agent(&alice).unwrap();
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(7000));
        contract.withdraw_stake(NearToken::from_yoctonear(rec.stake));
        assert_eq!(contract.get_agent(&alice).unwrap().stake, 0);
    }

    // ---- #378(5): cleanup резерва на КАЖДОМ терминальном переходе --------
    //
    // HERETIC §4 («bonds растут неограниченно») по исходникам не
    // подтверждается: lot_bonds снимается на всех терминалах, а нулевой
    // остаток agent_bonds удаляется (см. _release_bond). Ниже — пины на уже
    // существующее поведение, по одному на терминал: каждый падает, если
    // убрать соответствующий вызов _release_bond (проверено мутацией).

    /// TERMINAL reject_work → FAILED: резерв снят.
    #[test]
    fn hb_s18_bond_released_on_reject_terminal() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        to_rework3(&mut contract, &manager, &alice, "L1");

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3_000));
        contract.reject_work("L1".into());

        assert_eq!(
            contract.get_lot("L1".into()).unwrap().status,
            LotStatus::Failed
        );
        assert_eq!(contract.lot_bonds.get("L1"), None);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// TERMINAL settle_partial → PARTIAL_SETTLED: резерв снят.
    #[test]
    fn hb_s18_bond_released_on_settle_partial() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        to_rework3(&mut contract, &manager, &alice, "L1");

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3_000));
        contract.settle_partial("L1".into(), 5000);

        assert_eq!(
            contract.get_lot("L1".into()).unwrap().status,
            LotStatus::PartialSettled
        );
        assert_eq!(contract.lot_bonds.get("L1"), None);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// TERMINAL accept_work_late → COMPLETED: резерв снят (тот же _release,
    /// что и он-тайм путь).
    #[test]
    fn hb_s18_bond_released_on_accept_work_late() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        late_submit(&mut contract, &alice, "L1");

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("L1".into());

        assert_eq!(
            contract.get_lot("L1".into()).unwrap().status,
            LotStatus::Completed
        );
        assert_eq!(contract.lot_bonds.get("L1"), None);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// TERMINAL void_late → FAILED: резерв снят (без слэша, но резерв
    /// освобождается в любом случае).
    #[test]
    fn hb_s18_bond_released_on_void_late() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        late_submit(&mut contract, &alice, "L1");

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.void_late("L1".into());

        assert_eq!(
            contract.get_lot("L1".into()).unwrap().status,
            LotStatus::Failed
        );
        assert_eq!(contract.lot_bonds.get("L1"), None);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// cancel_lot до award: бонда вообще не было (winner == None), поэтому
    /// отменённый лот не оставляет записей в резерве.
    #[test]
    fn hb_s18_cancel_never_leaves_bond_records() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "L1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("L1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1_500));
        contract.cancel_lot("L1".into());

        assert_eq!(
            contract.get_lot("L1".into()).unwrap().status,
            LotStatus::Cancelled
        );
        assert_eq!(contract.lot_bonds.get("L1"), None);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// Терминал одного лота снимает РОВНО его bond: второй живой лот остаётся
    /// зарезервирован (не полный сброс agent_bonds и не двойное вычитание).
    #[test]
    fn hb_s18_terminal_releases_only_that_lots_bond() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "L1", PRICE_N);
        to_award(&mut contract, &manager, &alice, "L2", PRICE_N);
        let bond1 = *contract.lot_bonds.get("L1").unwrap();
        let bond2 = *contract.lot_bonds.get("L2").unwrap();
        assert_eq!(
            contract.agent_bonds.get(&alice).copied().unwrap(),
            bond1 + bond2
        );

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(5000));
        contract.submit_work("L1".into(), "W:done".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(6000));
        contract.accept_work("L1".into());

        assert_eq!(contract.lot_bonds.get("L1"), None);
        assert_eq!(contract.lot_bonds.get("L2"), Some(&bond2));
        assert_eq!(contract.agent_bonds.get(&alice), Some(&bond2));
    }

    /// Верхняя граница казны: сверх накопленных комиссий выйти нельзя,
    /// иначе owner съедает эскроу живых лотов (conservation: баланс =
    /// escrow + treasury + burned).
    #[test]
    #[should_panic(expected = "amount out of treasury bounds")]
    fn treasury_withdraw_over_balance_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(0));
        contract.set_protocol_fee(1000);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());

        let fee = contract.treasury;
        assert!(fee > 0, "protocol fee must accrue to treasury");
        assert_eq!(contract.escrow, 0, "escrow must be fully released on accept");
        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(4000));
        contract.treasury_withdraw(NearToken::from_yoctonear(fee + 1));
    }

    // ---- Пробы QODER'а (HIVEBID_CONTRACT_AUDIT) — зафиксированы --------
    // как регрессии на новую семантику: каждая проба демонстрировала баг,
    // теперь проверяет, что вектор закрыт.

    /// PROBE-1: посторонний не может ни закрыть торг, ни назначить победителя.
    #[test]
    #[should_panic(expected = "award: manager or analyst(owner) only")]
    fn probe_1_stranger_cannot_award() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(4);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(90).as_yoctonear(), 10_000, "sig".into());
        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());
        contract.award_lot("lot1".into(), alice.clone()); // panic: stranger
    }

    /// PROBE-2: дубль-ставка агента отклоняется — скоринг и payout не расходятся.
    #[test]
    #[should_panic(expected = "duplicate bid from agent")]
    fn probe_2_duplicate_bid_rejected() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(90).as_yoctonear(), 10_000, "a1".into());
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(110));
        contract.submit_bid("lot1".into(), NearToken::from_near(10).as_yoctonear(), 10_000, "a2".into());
    }

    /// PROBE-3: self-bid отклоняется — фарм рейтинга на собственном эскроу закрыт.
    #[test]
    #[should_panic(expected = "self-bid not allowed")]
    fn probe_3_self_bid_rejected() {
        let mut contract = setup();
        let manager = accounts(1);
        register(&mut contract, &manager);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(BUDGET_N).as_yoctonear(), 10_000, "sig".into());
    }

    /// HB-M03/G-1 (HERETIC #405 §5): self-dealing закрыт дважды. submit_bid
    /// не пускает менеджера в bid-набор своего лота, а award_lot отказывает
    /// явно — даже если в наборе вдруг окажется менеджер (собственность
    /// инварианта, а не валидация ввода).
    #[test]
    #[should_panic(expected = "self-dealing: manager cannot win own lot")]
    fn probe_3b_award_lot_rejects_manager_as_winner() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &manager);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());
        contract.award_lot("lot1".into(), manager.clone()); // panic: self-dealing
    }

    /// HB-S15 (канон дедлайнов): eta — абсолютная метка, и обещание
    /// eta == work_deadline невыполнимо он-тайм (сдача требует `now < wd`),
    /// а f_eta по такому обещанию = 0. Такая ставка не должна вообще
    /// участвовать в выборе победителя.
    #[test]
    #[should_panic(expected = "eta_ms must be before work deadline")]
    fn submit_bid_with_eta_exactly_at_work_deadline_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid(
            "lot1".into(),
            NearToken::from_near(PRICE_N).as_yoctonear(),
            BASE_MS + 20_000,
            "sig".into(),
        );
    }

    /// PROBE-4: auto-release без сабмиссии невозможен — Rework(3) не принимается.
    #[test]
    #[should_panic(expected = "lot not submitted")]
    fn probe_4_auto_release_requires_submission() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(4);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        for i in 0..3u64 {
            as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000 + i * 10));
            contract.submit_work("lot1".into(), format!("W:{}", i));
            as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500 + i * 10));
            contract.reject_work("lot1".into());
        }
        assert!(contract.get_lot("lot1".into()).unwrap().submission.is_none());

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(40_000));
        contract.accept_work("lot1".into()); // panic: Rework(3) ≠ Submitted
    }

    /// PROBE-5: s_eff реальный — быстрая сдача даёт больший рейтинг, чем
    /// сдача в последний момент окна.
    #[test]
    fn probe_5_speed_factor_affects_rating() {
        let mut contract = setup();
        let manager = accounts(1);
        let fast = accounts(2);
        let slow = accounts(3);
        register(&mut contract, &fast);
        register(&mut contract, &slow);

        to_award(&mut contract, &manager, &fast, "lotF", PRICE_N);
        to_award(&mut contract, &manager, &slow, "lotS", PRICE_N);

        as_ctx(&fast, NearToken::from_yoctonear(0), ts(1600));
        contract.submit_work("lotF".into(), "W:fast".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1700));
        contract.accept_work("lotF".into());

        as_ctx(&slow, NearToken::from_yoctonear(0), ts(19_999));
        contract.submit_work("lotS".into(), "W:slow".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_100));
        contract.accept_work("lotS".into());

        assert!(
            contract.get_rating(&fast) > contract.get_rating(&slow),
            "fast {} > slow {}",
            contract.get_rating(&fast),
            contract.get_rating(&slow)
        );
    }

    /// PROBE-6a: сдача ровно на work_deadline запрещена (`now < wd`).
    #[test]
    #[should_panic(expected = "work deadline passed")]
    fn probe_6a_submit_exactly_at_deadline_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_000));
        contract.submit_work("lot1".into(), "W:x".into());
    }

    /// PROBE-6b: no_show за 1 ms до work_deadline запрещён (`now >= wd`);
    /// ровно на границе — уже разрешён (HB-S06 закрыл мёртвую зону).
    #[test]
    #[should_panic(expected = "work deadline not passed")]
    fn probe_6b_no_show_exactly_at_deadline_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(19_999));
        contract.mark_no_show("lot1".into());
    }

    /// HB-S06: ровно на work_deadline no_show уже работает — мёртвой зоны нет.
    #[test]
    fn no_show_exactly_at_deadline_works() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Failed
        );
    }

    /// PROBE-7: award после work_deadline отклоняется — мгновенный слэш
    /// за заведомо невыполнимую сдачу невозможен.
    #[test]
    #[should_panic(expected = "work deadline already passed")]
    fn probe_7_award_after_work_deadline_rejected() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(40).as_yoctonear(), 10_000, "sig".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(100_000));
        contract.award_lot("lot1".into(), alice.clone()); // panic
    }

    // ---- HB-S14/S15 (HERETIC #405 §6): канон дедлайнов ------------------
    //
    // «Дедлайн наступил» ⟺ now >= deadline, «до дедлайна» ⟺ now < deadline —
    // ровно как решил HB-S06, закрыв 1-ms мёртвую зону на границе no_show.
    // Строгие `>` (вместо `>=`) и инклюзивные `<=` (вместо `<`) в тех же
    // ветках оставляли один миллисекундный такт, где действие уже нельзя
    // совершить, а встречная защита от него ещё не работала.

    /// Граница wd: сдача запрещена (`now < wd`), значит award в самый wd
    /// создавал лот с заведомо невыполнимой сдачей → мгновенный слэш.
    #[test]
    #[should_panic(expected = "work deadline already passed")]
    fn award_at_exactly_work_deadline_is_rejected() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_000));
        contract.award_lot("lot1".into(), alice.clone()); // panic
    }

    /// Канон не сужает живое окно: за 1 ms до wd award ещё возможен.
    #[test]
    fn award_one_ms_before_work_deadline_succeeds() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(19_999));
        contract.award_lot("lot1".into(), alice.clone());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Awarded
        );
    }

    /// Граница wd для чужого лота: award-путь закрыт тем же `now < wd`,
    /// живых переходов не осталось → спасательный crank открыт с wd, а не
    /// с wd+1 (симметрично mark_no_show).
    #[test]
    fn cancel_by_stranger_at_exactly_work_deadline_works() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(4);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "sig".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_000));
        contract.cancel_lot("lot1".into());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Cancelled
        );
        assert_eq!(contract.get_escrow(), 0);
    }

    /// PROBE-8: PARTIAL — падение (q=0), рейтинг снижается даже при α→1.
    #[test]
    fn probe_8_partial_counts_as_failure() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        for i in 0..3u64 {
            as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000 + i * 10));
            contract.submit_work("lot1".into(), format!("W:{}", i));
            as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500 + i * 10));
            contract.reject_work("lot1".into());
        }

        let before = contract.get_rating(&alice);
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.settle_partial("lot1".into(), 9999);

        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.tasks_completed, 0);
        assert_eq!(rec.tasks_failed, 1);
        assert!(rec.rating < before, "PARTIAL drops rating: {} -> {}", before, rec.rating);
        assert_eq!(contract.escrow, 0);
    }

    // ---- Гварды -------------------------------------------------------

    #[test]
    #[should_panic(expected = "agent not registered")]
    fn bid_requires_registration() {
        let mut contract = setup();
        let manager = accounts(1);
        let stranger = accounts(5);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "s".into());
    }

    #[test]
    #[should_panic(expected = "deadlines must be ordered")]
    fn create_task_requires_ordered_deadlines() {
        let mut contract = setup();
        let manager = accounts(1);
        as_ctx(&manager, NearToken::from_near(BUDGET_N), ts(0));
        contract.create_task(
            "lot-x".into(),
            task_hash(),
            BASE_MS + 40_000,
            BASE_MS + 20_000,
            BASE_MS + 60_000,
        );
    }

    #[test]
    #[should_panic(expected = "lot not open")]
    fn bid_after_close_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(PRICE_N).as_yoctonear(), 10_000, "s".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_bid("lot1".into(), NearToken::from_near(40).as_yoctonear(), 10_000, "s".into());
    }

    #[test]
    #[should_panic(expected = "manager/owner")]
    fn cancel_requires_manager() {
        let mut contract = setup();
        let manager = accounts(1);
        let other = accounts(3);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&other, NearToken::from_yoctonear(0), ts(500));
        contract.cancel_lot("lot1".into());
    }

    #[test]
    #[should_panic(expected = "lot not submitted")]
    fn double_accept_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());
        // второй accept уже недопустим (терминальное)
        contract.accept_work("lot1".into());
    }

    // ---- Аналитик: тай-брейк по цене --------------------------------

    #[test]
    fn analyst_tie_breaks_by_price() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let carol = accounts(3);
        register(&mut contract, &alice);
        register(&mut contract, &carol);

        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid("lot1".into(), NearToken::from_near(50).as_yoctonear(), 10_000, "a".into());
        as_ctx(&carol, NearToken::from_yoctonear(0), ts(150));
        contract.submit_bid("lot1".into(), NearToken::from_near(30).as_yoctonear(), 10_000, "c".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(1500));
        contract.close_bidding("lot1".into());

        // одинаковые рейтинги (R0) и eta → выигрывает более дешёвый Carol
        let winner = contract.award_best("lot1".into());
        assert_eq!(winner, carol);
    }

    // ---- HB-S01/S08: governance-диапазон min_stake ----------------------

    #[test]
    #[should_panic(expected = "min_stake out of bounds [1..1000 N]")]
    fn set_min_stake_below_band_panics() {
        let mut contract = setup();
        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(0));
        contract.set_min_stake(NearToken::from_yoctonear(0));
    }

    #[test]
    #[should_panic(expected = "min_stake out of bounds [1..1000 N]")]
    fn set_min_stake_above_band_panics() {
        let mut contract = setup();
        as_ctx(&accounts(0), NearToken::from_yoctonear(0), ts(0));
        contract.set_min_stake(NearToken::from_near(1_001));
    }

    #[test]
    fn set_min_stake_accepts_band_endpoints() {
        let mut contract = setup();
        let owner = accounts(0);
        as_ctx(&owner, NearToken::from_yoctonear(0), ts(0));
        contract.set_min_stake(NearToken::from_near(1));
        assert_eq!(contract.min_stake, NearToken::from_near(1));
        contract.set_min_stake(NearToken::from_near(1_000));
        assert_eq!(contract.min_stake, NearToken::from_near(1_000));
    }

    #[test]
    #[should_panic(expected = "owner only")]
    fn set_min_stake_requires_owner() {
        let mut contract = setup();
        as_ctx(&accounts(1), NearToken::from_yoctonear(0), ts(0));
        contract.set_min_stake(NearToken::from_near(5));
    }

    /// HB-S08 (mitigated): обнулённый слэшами/выводом стейк не оставляет права
    /// bid-ить — гейт HB-M11 в submit_bid, а не в пороге слэша.
    #[test]
    #[should_panic(expected = "stake below minimum")]
    fn drained_agent_cannot_bid() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(50));
        contract.withdraw_stake(NearToken::from_near(10));
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid(
            "lot1".into(),
            NearToken::from_near(PRICE_N).as_yoctonear(),
            10_000,
            "sig".into(),
        );
    }

    // ---- HB-N04 / HB-S11: валидация входов ------------------------------

    fn create_lot_hashed(contract: &mut Contract, manager: &AccountId, lot_id: &str, hash: &str) {
        as_ctx(manager, NearToken::from_near(BUDGET_N), ts(0));
        contract.create_task(
            lot_id.to_string(),
            hash.to_string(),
            BASE_MS + 1000,
            BASE_MS + 20_000,
            BASE_MS + 40_000,
        );
    }

    #[test]
    #[should_panic(expected = "task_hash must be 'T:<64hex>'")]
    fn create_task_non_hex_task_hash_panics() {
        let mut contract = setup();
        create_lot_hashed(&mut contract, &accounts(1), "lot1", &format!("T:{}", "g".repeat(64)));
    }

    /// hex-алфавит, а не lowercase-алфавит: верхний регистр валиден.
    #[test]
    fn create_task_accepts_uppercase_hex_hash_and_64_char_lot_id() {
        let mut contract = setup();
        let manager = accounts(1);
        let hash = format!("T:{}", "F".repeat(64));
        let lot_id = "L".repeat(64);
        create_lot_hashed(&mut contract, &manager, &lot_id, &hash);
        let lot = contract.get_lot(lot_id.clone()).expect("lot not stored");
        assert_eq!(lot.task_hash, hash);
        assert_eq!(lot_id.len(), 64);
    }

    #[test]
    #[should_panic(expected = "lot_id must be 1..64 chars")]
    fn create_task_empty_lot_id_panics() {
        let mut contract = setup();
        create_lot_hashed(&mut contract, &accounts(1), "", &task_hash());
    }

    #[test]
    #[should_panic(expected = "lot_id must be 1..64 chars")]
    fn create_task_overlong_lot_id_panics() {
        let mut contract = setup();
        create_lot_hashed(
            &mut contract,
            &accounts(1),
            &"L".repeat(65),
            &task_hash(),
        );
    }

    #[test]
    #[should_panic(expected = "signature too long")]
    fn submit_bid_overlong_signature_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid(
            "lot1".into(),
            NearToken::from_near(PRICE_N).as_yoctonear(),
            10_000,
            "s".repeat(257),
        );
    }

    #[test]
    fn submit_bid_accepts_max_length_signature() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        create_lot(&mut contract, &manager, "lot1");
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(100));
        contract.submit_bid(
            "lot1".into(),
            NearToken::from_near(PRICE_N).as_yoctonear(),
            10_000,
            "s".repeat(256),
        );
        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.bids.len(), 1);
        assert_eq!(lot.bids[0].signature.len(), 256);
    }

    #[test]
    #[should_panic(expected = "work_hash must be 1..128 chars")]
    fn submit_work_empty_hash_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), String::new());
    }

    #[test]
    #[should_panic(expected = "work_hash must be 1..128 chars")]
    fn submit_work_overlong_hash_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W".repeat(129));
    }

    #[test]
    fn submit_work_accepts_max_length_hash() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W".repeat(128));
        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Submitted);
        assert_eq!(lot.submission.unwrap().work_hash.len(), 128);
    }

    // ---- HB-S03: fee в settle_partial (та же экономика, что _release) ----

    fn to_rework3(contract: &mut Contract, manager: &AccountId, winner: &AccountId, lot_id: &str) {
        for i in 0..3u64 {
            as_ctx(winner, NearToken::from_yoctonear(0), ts(2000 + i * 10));
            contract.submit_work(lot_id.to_string(), "W:x".into());
            as_ctx(manager, NearToken::from_yoctonear(0), ts(2500 + i * 10));
            contract.reject_work(lot_id.to_string());
        }
    }

    #[test]
    fn settle_partial_takes_fee_from_executor_share() {
        let mut contract = setup();
        let owner = accounts(0);
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        to_rework3(&mut contract, &manager, &alice, "lot1");
        as_ctx(&owner, NearToken::from_yoctonear(0), ts(3_000));
        contract.set_protocol_fee(500);
        // paid = 40 N · 5000 bps = 20 N; fee = 20 N · 500 bps = 1 N
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3_500));
        contract.settle_partial("lot1".into(), 5000);
        assert_eq!(
            contract.get_treasury(),
            NearToken::from_near(1).as_yoctonear()
        );
        assert_eq!(contract.get_escrow(), 0);
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::PartialSettled
        );
    }

    #[test]
    fn settle_partial_keeps_treasury_when_fee_disabled() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        to_rework3(&mut contract, &manager, &alice, "lot1");
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3_500));
        contract.settle_partial("lot1".into(), 9999);
        assert_eq!(contract.get_treasury(), 0);
        assert_eq!(contract.get_escrow(), 0);
    }

    // ---- HB-S04/S06: граница no_show бьёт по деньгам и рейтингу ---------

    #[test]
    fn no_show_at_boundary_slashes_stake_and_records_rating() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());
        // слэш = 50% стейка 10 N ушёл в burned, не растворился (HB-S04)
        assert_eq!(
            contract.get_burned(),
            NearToken::from_near(5).as_yoctonear()
        );
        let rec = contract.get_agent(&alice).expect("agent record");
        assert_eq!(rec.stake, NearToken::from_near(5).as_yoctonear());
        assert_eq!(rec.n, 1);
        assert_eq!(rec.tasks_failed, 1);
        assert_eq!(rec.ledger.len(), 1);
        assert_eq!(rec.ledger[0].term, "NO_SHOW");
        // DELTA_DN: 0.5 − 0.2·0.5 = 0.4 (scaled)
        assert_eq!(rec.rating, 400_000);
        assert_eq!(contract.get_escrow(), 0);
    }

    // ---- HB-S10: ledger — кольцо, статистика копится отдельно -----------

    #[test]
    fn ledger_ring_is_capped_while_statistics_keep_accumulating() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let events = MAX_LEDGER_EVENTS + 1;
        for i in 0..events {
            let id = format!("lot{}", i);
            // top-up: каждый NO_SHOW halves stake; без него HB-M11 заблокировал бы bid
            register(&mut contract, &alice);
            to_award(&mut contract, &manager, &alice, &id, PRICE_N);
            as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
            contract.mark_no_show(id);
        }
        let rec = contract.get_agent(&alice).expect("agent record");
        assert_eq!(rec.ledger.len(), MAX_LEDGER_EVENTS);
        assert_eq!(rec.n, events as u64);
        assert!(rec.n as usize > rec.ledger.len());
        assert_eq!(rec.ledger.last().unwrap().term, "NO_SHOW");
        // эскроу замкнут по всем лотам
        assert_eq!(contract.get_escrow(), 0);
        // R_eff считает по накопленному n, а не по длине кольца
        let r = contract.get_rating(&alice);
        let n = rec.n as u128;
        let ring = rec.ledger.len() as u128;
        let eff = contract.get_rating_eff(&alice);
        assert_eq!(eff, (n * r + K0 * R0) / (n + K0));
        assert_ne!(eff, (ring * r + K0 * R0) / (ring + K0));
    }

    // ---- views для on-chain инвариант-чекера (soak #333) ---------------

    #[test]
    fn agent_enumeration_and_total_staked_track_registry() {
        let mut contract = setup();
        let alice = accounts(2);
        let bob = accounts(3);
        assert_eq!(contract.get_agent_ids(), Vec::<AccountId>::new());
        assert_eq!(contract.get_total_staked(), 0);

        register(&mut contract, &alice);
        register(&mut contract, &bob);
        let mut ids = contract.get_agent_ids();
        ids.sort();
        assert_eq!(ids, vec![alice.clone(), bob.clone()]);
        assert_eq!(
            contract.get_total_staked(),
            NearToken::from_near(20).as_yoctonear()
        );

        // слэш уменьшает Σ stakes — инвариант переживает burned
        let manager = accounts(1);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());
        assert_eq!(
            contract.get_total_staked(),
            NearToken::from_near(15).as_yoctonear()
        );
    }

    // ---- HB-S07: он-тайм путь остаётся дедлайн-связанным ----------------

    /// HB-S07 закрыта второй дельтой (P12–P14): поздняя работа больше не
    /// уничтожается, а уходит в SUBMITTED_LATE отдельным методом. Пин ниже
    /// фиксирует инвариант, который дельта НЕ меняла: submit_work по-прежнему
    /// отвергает сдачу после work_deadline (`now < wd`), то есть новый путь
    /// не размыкает старое окно.
    #[test]
    #[should_panic(expected = "work deadline passed")]
    fn characterization_s07_late_submission_is_rejected() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_001));
        contract.submit_work("lot1".into(), "W:late".into());
    }

    // ---- HB-S07 / P12: late_submit_work ---------------------------------

    fn late_submit(contract: &mut Contract, winner: &AccountId, lot_id: &str) {
        as_ctx(winner, NearToken::from_yoctonear(0), ts(20_001));
        contract.late_submit_work(lot_id.to_string(), "W:late".into());
    }

    #[test]
    fn late_submit_work_enters_submitted_late_without_touching_money() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        let escrow_before = contract.get_escrow();
        assert_eq!(escrow_before, NearToken::from_near(BUDGET_N).as_yoctonear());

        late_submit(&mut contract, &alice, "lot1");

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::SubmittedLate);
        // SUBMITTED_LATE — не терминал: ждёт решения менеджера.
        assert!(!lot.is_terminal());
        assert!(lot.is_live());
        assert_eq!(lot.settle_ts_ms, None);
        let sub = lot.submission.expect("late submission stored");
        assert_eq!(sub.work_hash, "W:late");
        assert_eq!(sub.attempt, 1); // rework_count = 0 → attempt 1
        assert_eq!(sub.ts_ms, BASE_MS + 20_001);
        // P12: денег не сдвинулось вообще
        assert_eq!(contract.get_escrow(), escrow_before);
        assert_eq!(contract.get_treasury(), 0);
        assert_eq!(contract.get_burned(), 0);
    }

    /// HB-S15 (канон дедлайнов): при now == wd он-тайм сдача уже запрещена
    /// (`now < wd`), поэтому именно с wd открывается окно поздней сдачи —
    /// иначе на этом миллисекундном такте победитель не имел бы ни одного
    /// действия, а менеджер мог слэшить (`now >= wd`).
    #[test]
    fn late_submit_work_at_exactly_work_deadline_opens_late_window() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_000));
        contract.late_submit_work("lot1".into(), "W:x".into());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::SubmittedLate
        );
    }

    /// после verify_deadline окно поздней сдачи закрыто — там уже void_late
    /// крэнкается любым (I-8), отдельная сдача не нужна.
    #[test]
    #[should_panic(expected = "verify deadline passed (void_late)")]
    fn late_submit_work_after_verify_deadline_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(40_000));
        contract.late_submit_work("lot1".into(), "W:x".into());
    }

    #[test]
    #[should_panic(expected = "winner only")]
    fn late_submit_work_non_winner_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_001));
        contract.late_submit_work("lot1".into(), "W:x".into());
    }

    /// Асимметрия vs submit_work: обычный submit принимает Awarded | Rework(k),
    /// late_submit_work — только Awarded. Зависший rework закрывает no_show
    /// (I-8), а не поздняя сдача.
    #[test]
    #[should_panic(expected = "lot not awaiting work")]
    fn late_submit_work_from_rework_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500));
        contract.reject_work("lot1".into());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Rework(1)
        );
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_001));
        contract.late_submit_work("lot1".into(), "W:x".into());
    }

    #[test]
    #[should_panic(expected = "work_hash must be 1..128 chars")]
    fn late_submit_work_overlong_work_hash_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_001));
        contract.late_submit_work("lot1".into(), "W".repeat(129));
    }

    #[test]
    fn late_submit_work_accepts_exactly_128_char_work_hash() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_001));
        contract.late_submit_work("lot1".into(), "W:".repeat(64));
        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::SubmittedLate);
        assert_eq!(lot.submission.unwrap().work_hash.len(), 128);
    }

    // ---- Гонка late-сдачи с no_show: решает порядок вызовов --------------

    /// Кто первый вызвал mark_no_show (now == wd, крэнкнут любым), тот и
    /// поставил терминал — поздняя сдача после него невозможна.
    #[test]
    #[should_panic(expected = "lot not awaiting work")]
    fn no_show_before_late_submit_wins_the_race() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());
        late_submit(&mut contract, &alice, "lot1");
    }

    /// Обратно: ушедшая в SUBMITTED_LATE сдача снимает no_show (статус больше
    /// не Awarded/Rework) — деньги не слэшатся, решение у менеджера.
    #[test]
    #[should_panic(expected = "lot not awarded")]
    fn late_submit_before_no_show_blocks_the_terminal() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_002));
        contract.mark_no_show("lot1".into());
    }

    // ---- HB-S07 / P13: accept_work_late ---------------------------------

    #[test]
    #[should_panic(expected = "manager only")]
    fn accept_work_late_non_manager_panics() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");
        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("lot1".into());
    }

    /// Находка (iii): у accept_work_late нет крэнка и нет привязки ко
    /// времени — чужой вызов падает и ПОСЛЕ verify_deadline. SubmittedLate
    /// после vd авто-терминалится только через void_late, то есть авто-
    /// терминация есть, а авто-выплаты — нет. Трипвайер, если крэнка не появится.
    #[test]
    #[should_panic(expected = "manager only")]
    fn accept_work_late_has_no_crank_after_verify_deadline() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");
        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(40_001));
        contract.accept_work_late("lot1".into());
    }

    #[test]
    #[should_panic(expected = "lot not late-submitted")]
    fn accept_work_late_requires_submitted_late() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("lot1".into());
    }

    /// Та же fee-экономика, что у он-тайм release (HB-S03), и fee берётся из
    /// ЖИВОГО protocol_fee_bps: payout.fee, записанный на award, остаётся 0.
    #[test]
    fn accept_work_late_release_uses_live_fee_and_full_budget() {
        let mut contract = setup();
        let owner = accounts(0);
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");

        as_ctx(&owner, NearToken::from_yoctonear(0), ts(29_000));
        contract.set_protocol_fee(500);
        let escrow_before = contract.get_escrow();
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Completed);
        assert_eq!(lot.settle_ts_ms, Some(BASE_MS + 30_000));
        let payout = lot.payout.unwrap();
        assert_eq!(payout.executor, NearToken::from_near(PRICE_N).as_yoctonear());
        assert_eq!(
            payout.manager,
            NearToken::from_near(BUDGET_N - PRICE_N).as_yoctonear()
        );
        assert_eq!(payout.fee, 0); // хэшируется на award, выводится на release
        // fee = 40 N · 500 bps = 2 N в казну, эскроу закрыт по полному бюджету
        assert_eq!(contract.get_treasury(), NearToken::from_near(2).as_yoctonear());
        assert_eq!(
            escrow_before - contract.get_escrow(),
            NearToken::from_near(BUDGET_N).as_yoctonear()
        );
        assert_eq!(contract.get_escrow(), 0);
    }

    /// P13: штраф скорости безусловный (s = 0), но s_eff = S_MIN, поэтому
    /// q_final = q · S_MIN = SCALE/2 — поздний успех всё же апвоут.
    #[test]
    fn accept_work_late_scores_with_zero_speed_factor() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("lot1".into());

        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.n, 1);
        assert_eq!(rec.tasks_completed, 1);
        assert_eq!(rec.tasks_failed, 0);
        assert_eq!(rec.sum_q, SCALE / 2);
        assert_eq!(rec.ledger.len(), 1);
        let ev = rec.ledger.last().unwrap();
        assert_eq!(ev.term, "COMPLETED");
        assert_eq!(ev.q_scaled, SCALE);
        assert_eq!(ev.s_scaled, 0);
        assert_eq!(ev.q_final_scaled, SCALE / 2);
        assert_eq!(ev.ts_ms, BASE_MS + 30_000);
        // step = DELTA_UP · (SCALE/2) · (SCALE−R0) / SCALE² = 25 000
        assert_eq!(rec.rating, R0 + DELTA_UP / 4);
    }

    /// HB-S17 (HERETIC #405 §3): у штрафа скорости должен быть один источник
    /// правды — кривая `_speed_factor`. Тест связывает сохранённый s_scaled с
    /// её значением в момент сабмиссии: если кривая изменится, а поздняя ветка
    /// останется с константой, тест покраснеет.
    #[test]
    fn accept_work_late_speed_matches_the_speed_curve() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1"); // ts = work_deadline + 1

        let lot = contract.get_lot("lot1".into()).unwrap();
        let submit_ts = lot.submission.as_ref().unwrap().ts_ms;
        let expected = contract._speed_factor(&lot, submit_ts);

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("lot1".into());

        let rec = contract.get_agent(&alice).unwrap();
        let ev = rec.ledger.last().unwrap();
        assert_eq!(ev.s_scaled, expected, "поздний s обязан равняться значению кривой");
        assert_eq!(ev.s_scaled, 0, "просрочка за окном — s = 0");
        assert_eq!(ev.q_final_scaled, SCALE / 2);
    }

    /// Поздняя сдача обязана стоить дешевле он-тайм (иначе окно work_deadline
    /// ничего не значит). Два лота в одном контракте: s=SCALE против s=0.
    #[test]
    fn late_completion_scores_below_on_time_completion() {
        let mut contract = setup();
        let manager = accounts(1);
        let on_time = accounts(2);
        let late = accounts(3);
        register(&mut contract, &on_time);
        register(&mut contract, &late);
        to_award(&mut contract, &manager, &on_time, "lot1", PRICE_N);
        to_award(&mut contract, &manager, &late, "lot2", PRICE_N);

        as_ctx(&on_time, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:on".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(3000));
        contract.accept_work("lot1".into());

        late_submit(&mut contract, &late, "lot2");
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.accept_work_late("lot2".into());

        let r_on_time = contract.get_rating(&on_time);
        let r_late = contract.get_rating(&late);
        assert_eq!(r_on_time, R0 + DELTA_UP / 2); // s=SCALE → step 50 000
        assert_eq!(r_late, R0 + DELTA_UP / 4); // s=0 → step 25 000
        assert!(r_late < r_on_time, "late must score below on-time");
    }

    // ---- HB-S07 / P14: void_late ----------------------------------------

    /// void_late = FAILED + весь бюджет менеджеру, но БЕЗ слэша (в отличие
    /// от NO_SHOW) и с рейтингом FAILED (даунвоут до 400 000).
    #[test]
    fn void_late_by_manager_fails_lot_and_restores_full_budget() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(30_000));
        contract.void_late("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Failed);
        assert_eq!(lot.settle_ts_ms, Some(BASE_MS + 30_000));
        assert!(lot.is_terminal());
        assert_eq!(contract.get_escrow(), 0);
        assert_eq!(contract.get_treasury(), 0);
        assert_eq!(contract.get_burned(), 0); // NO_SHOW-слэш тут не применяется
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.stake, NearToken::from_near(10).as_yoctonear());
        assert_eq!(rec.n, 1);
        assert_eq!(rec.tasks_failed, 1);
        assert_eq!(rec.tasks_completed, 0);
        let ev = rec.ledger.last().unwrap();
        assert_eq!(ev.term, "FAILED");
        assert_eq!(ev.q_final_scaled, 0);
        assert_eq!(rec.rating, R0 - DELTA_DN / 2);
    }

    /// I-8: после verify_deadline аннулировать может ЛЮБОЙ — у лота есть
    /// авто-терминал, менеджерская инерция не держит эскроу.
    #[test]
    fn void_late_stranger_cranes_after_verify_deadline() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let anyone = accounts(5);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");

        as_ctx(&anyone, NearToken::from_yoctonear(0), ts(40_001));
        contract.void_late("lot1".into());

        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Failed
        );
        assert_eq!(contract.get_escrow(), 0);
    }

    /// HB-S14 (канон дедлайнов): закрывает найденную асимметрию — accept_work
    /// открывал постороннему авто-релиз по `>= vd`, а void_late держал его на
    /// строгом `> vd`. На exactly vd лот в SubmittedLate уже не имеет живого
    /// пути кроме менеджера, так что crank открыт с самой границы.
    #[test]
    fn void_late_stranger_at_exactly_verify_deadline_works() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let anyone = accounts(5);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        late_submit(&mut contract, &alice, "lot1");
        as_ctx(&anyone, NearToken::from_yoctonear(0), ts(40_000));
        contract.void_late("lot1".into());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Failed
        );
        assert_eq!(contract.get_escrow(), 0);
    }

    /// Проверка статуса идёт ПЕРЕД проверкой полномочий: посторонний на Awarded-лоте
    /// получает "lot not late-submitted", а не "manager only" — иначе крэнк
    /// протекал бы информацию о чужих лотах через сообщение об отказе.
    #[test]
    #[should_panic(expected = "lot not late-submitted")]
    fn void_late_status_precedes_authority_check() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let anyone = accounts(5);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&anyone, NearToken::from_yoctonear(0), ts(30_000));
        contract.void_late("lot1".into());
    }

    /// I-8 для висящих rework: mark_no_show принимает Awarded | Rework(k).
    /// HB-W31 (6.2): из Rework агент уже сдавал — терминал FAILED и
    /// NO_SHOW-рейтинг сохраняются, но слэша bond'а НЕТ: rework-grind
    /// реджектов менеджером не должен конвертироваться в слэш стейка
    /// честного исполнителя; bond возвращается целиком.
    #[test]
    fn no_show_from_rework_terminalizes_without_slash() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500));
        contract.reject_work("lot1".into());
        let stake = contract.get_agent(&alice).unwrap().stake;

        as_ctx(&manager, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Failed);
        assert_eq!(contract.get_escrow(), 0);
        assert_eq!(contract.get_burned(), 0);
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.stake, stake);
        let ev = rec.ledger.last().unwrap();
        assert_eq!(ev.term, "NO_SHOW");
        assert_eq!(rec.rating, R0 - DELTA_DN / 2);
    }

    /// HB-W31 (6.3): из Rework после work_deadline у агента живого хода нет
    /// (submit_work требует now < wd, late_submit — только Awarded), поэтому
    /// менеджерское окно (wd, vd) под Rework не защищает никакую гонку —
    /// crank открыт любому сразу после wd; это же разблокирует hold-up
    /// settle_partial'ом: bond не может висеть вечно.
    #[test]
    fn no_show_rework_stranger_cranks_after_work_deadline() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500));
        contract.reject_work("lot1".into());
        let stake = contract.get_agent(&alice).unwrap().stake;

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Failed);
        assert_eq!(contract.get_escrow(), 0);
        assert_eq!(contract.get_burned(), 0);
        assert_eq!(contract.get_agent(&alice).unwrap().stake, stake);
        assert_eq!(contract.get_agent(&alice).unwrap().ledger.last().unwrap().term, "NO_SHOW");
    }

    /// HB-W31: wd-1 ещё закрыт, а ровно на wd permissionless Rework-crank
    /// разрешён. Пойманная паника не должна изменить состояние лота.
    #[test]
    fn no_show_rework_crank_at_exact_wd() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500));
        contract.reject_work("lot1".into());

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(19_999));
        let panic = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            contract.mark_no_show("lot1".into());
        }))
        .expect_err("Rework crank at wd-1 must panic");
        let panic_message = panic
            .downcast_ref::<String>()
            .map(|message| message.as_str())
            .or_else(|| panic.downcast_ref::<&str>().copied())
            .unwrap_or("");
        assert!(
            panic_message.contains("work deadline not passed"),
            "unexpected wd-1 panic: {panic_message}"
        );
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Rework(1)
        );

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Failed
        );
    }

    /// HB-W31: достижение Rework(3) не меняет permissionless no-slash crank.
    #[test]
    fn no_show_from_rework3_terminalizes_without_slash() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        let stake = contract.get_agent(&alice).unwrap().stake;
        let initial_bond = contract.agent_bonds.get(&alice).copied().unwrap();
        assert!(initial_bond > 0);

        for attempt in 0u64..3 {
            let submitted_at = 2_000 + attempt * 1_000;
            as_ctx(&alice, NearToken::from_yoctonear(0), ts(submitted_at));
            contract.submit_work("lot1".into(), format!("W:{attempt}"));
            as_ctx(&manager, NearToken::from_yoctonear(0), ts(submitted_at + 500));
            contract.reject_work("lot1".into());
        }
        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Rework(MAX_REWORK)
        );

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_001));
        contract.mark_no_show("lot1".into());

        assert_eq!(
            contract.get_lot("lot1".into()).unwrap().status,
            LotStatus::Failed
        );
        assert_eq!(contract.get_agent(&alice).unwrap().stake, stake);
        assert_eq!(contract.get_burned(), 0);
        assert_eq!(contract.lot_bonds.get("lot1"), None);
        assert_eq!(contract.agent_bonds.get(&alice), None);
    }

    /// HB-W31: terminal no-show освобождает оба bond-map; весь свободный
    /// стейк после этого можно вывести одним вызовом.
    #[test]
    fn no_show_rework_releases_bond_maps() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        let stake = contract.get_agent(&alice).unwrap().stake;
        assert!(contract.agent_bonds.get(&alice).copied().unwrap_or(0) > 0);
        assert!(contract.lot_bonds.get("lot1").is_some());

        as_ctx(&alice, NearToken::from_yoctonear(0), ts(2000));
        contract.submit_work("lot1".into(), "W:x".into());
        as_ctx(&manager, NearToken::from_yoctonear(0), ts(2500));
        contract.reject_work("lot1".into());
        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_001));
        contract.mark_no_show("lot1".into());

        assert_eq!(contract.agent_bonds.get(&alice), None);
        assert_eq!(contract.lot_bonds.get("lot1"), None);
        as_ctx(&alice, NearToken::from_yoctonear(0), ts(20_002));
        contract.withdraw_stake(NearToken::from_yoctonear(stake));
        assert_eq!(contract.get_agent(&alice).unwrap().stake, 0);
    }

    /// Enum-вариант добавлен В КОНЕЦ — borsh-порядок существующих статусов
    /// не сдвигается (совместимость уже записанных в хранение лотов).
    #[test]
    fn submitted_late_is_appended_last_preserving_borsh_discriminants() {
        assert_eq!(near_sdk::borsh::to_vec(&LotStatus::Cancelled).unwrap(), vec![8u8]);
        assert_eq!(
            near_sdk::borsh::to_vec(&LotStatus::SubmittedLate).unwrap(),
            vec![9u8]
        );
        assert_eq!(
            near_sdk::borsh::to_vec(&LotStatus::Rework(2)).unwrap(),
            vec![4u8, 2u8]
        );
    }

    /// HB-M19 — закрыт. Окно (wd, vd) принадлежит гонке менеджера с
    /// late-окном победителя: посторонний в нём падает — иначе любой за
    /// газ срезал бы P12-окно и палил чужой стейк без выгоды себе.
    #[test]
    #[should_panic(expected = "no_show: manager until verify deadline, anyone after")]
    fn mark_no_show_by_stranger_before_verify_deadline_panics_hb_m19() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(20_000));
        contract.mark_no_show("lot1".into());
    }

    /// HB-M19: после verify_deadline живых путей нет — открытый crank
    /// сохраняет терминальную достижимость при пропавшем менеджере.
    #[test]
    fn mark_no_show_by_stranger_at_verify_deadline_terminalizes_hb_m19() {
        let mut contract = setup();
        let manager = accounts(1);
        let alice = accounts(2);
        let stranger = accounts(3);
        register(&mut contract, &alice);
        to_award(&mut contract, &manager, &alice, "lot1", PRICE_N);
        let stake = contract.get_agent(&alice).unwrap().stake;

        as_ctx(&stranger, NearToken::from_yoctonear(0), ts(40_000));
        contract.mark_no_show("lot1".into());

        let lot = contract.get_lot("lot1".into()).unwrap();
        assert_eq!(lot.status, LotStatus::Failed);
        assert_eq!(contract.get_escrow(), 0);
        let slash = stake * NO_SHOW_SLASH_BPS as u128 / 10_000;
        assert_eq!(contract.get_burned(), slash);
        let rec = contract.get_agent(&alice).unwrap();
        assert_eq!(rec.stake, stake - slash);
        assert_eq!(rec.ledger.last().unwrap().term, "NO_SHOW");
        assert_eq!(rec.rating, R0 - DELTA_DN / 2);
    }

    /// HB-M20: near-sdk вшивает в extern-обёртку каждого не-payable метода
    /// отклонение депозита (`Method X doesn't accept deposit`), а unit-тесты
    /// зовут методы напрямую и эту обёртку не проходят. Значит сломанная
    /// payable-ность зелёному сьюту не видна — сторож может быть только
    /// статическим: разбор собственного исходника.
    #[test]
    fn every_deposit_reading_entrypoint_is_payable() {
        let src = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lib.rs"),
        )
        .expect("contract source readable");
        let lines: Vec<&str> = src.lines().collect();
        let needle = ["env::attached_", "deposit()"].concat();
        let mut checked = 0usize;
        for (i, ln) in lines.iter().enumerate() {
            if !ln.contains(needle.as_str()) {
                continue;
            }
            let mut f = i as isize;
            while f >= 0 && !lines[f as usize].trim_start().starts_with("pub fn ") {
                f -= 1;
            }
            assert!(f >= 0, ":{i} reads a deposit outside any `pub fn`");
            let fname = lines[f as usize]
                .trim_start()
                .trim_start_matches("pub fn ")
                .split('(')
                .next()
                .unwrap()
                .to_string();
            let mut g = f - 1;
            while g >= 0 {
                let t = lines[g as usize].trim();
                if t == "#[payable]" {
                    break;
                }
                if !(t.starts_with('#') || t.starts_with('/')) {
                    break;
                }
                g -= 1;
            }
            assert!(
                g >= 0 && lines[g as usize].trim() == "#[payable]",
                "{fname} читает депозит, но не помечен #[payable] — обёртка отклонит каждый вызов on-chain"
            );
            checked += 1;
        }
        assert_eq!(checked, 2, "ожидались ровно два депозитных входа");
    }
}
